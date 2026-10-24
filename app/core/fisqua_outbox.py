"""Delivering each handed-over volume's capture manifest to Fisqua, and asking for its status.

Every call is the unit's: HTTPS to Fisqua with this unit's token
(DTK_FISQUA_URL, DTK_FISQUA_TOKEN). Fisqua's side (paths, token format,
answers, status names, what a second manifest for a volume does) is not
agreed yet, so the calls themselves sit behind FisquaTransport, and this
version has only NullTransport, which sends nothing. What is here does not
depend on that:

* **Enqueue at handover** (enqueue_handover). A handover that promotes at
  least one record adds a `waiting_for_upload` row in the same transaction,
  with a fresh manifest id. Any older row of the volume not yet sent
  (`waiting_for_upload`, `ready`, `failed`) becomes `superseded`. A take-back
  makes those rows `withdrawn` (withdraw_unsent). A row being sent, or
  already delivered or refused, is left as it is.
* **Promotion to ready** (run_fisqua_outbox). A `waiting_for_upload` row is
  promoted once the volume is received (app.core.image_upload's received
  indicator: every object verified in the bucket). The manifest is built
  then, under export_jobs.hold_for_reading, with the row's manifest id, and
  stored on the row, so a retry sends the same document. A volume the
  builder refuses is `refused` with refusal `not_exportable` and the
  builder's problems on the row. A volume with no stem can never be
  received, so it is built at once, and refused for that.
* **Sending.** Each `ready` row, and each `failed` row whose
  `next_attempt_at` has come, is claimed (`sending`, one more attempt) and
  handed to the transport. Its answer makes the row `delivered` or `refused`
  (refusal `fisqua`); a transport error, or an exception from it, makes it
  `failed`, to be tried again after retry_delay. Retries never stop: the
  unit is often offline for days, and a manifest it gave up on would never
  reach Fisqua.
* **Status poll.** For each volume whose latest delivered row has an
  `external_id`, the transport is asked for its status, and the answer is
  kept in fisqua_status. A poll that fails leaves the last answer in place.

Every state change is a conditional UPDATE on the state the worker read, so
a take-back or a new handover committed while the worker runs is not
overwritten. The worker takes no lock on records or on the volume row. The
handover paths write these rows after locking the volume's records, which is
the order every path takes (records, then the volume).

The token is never logged, stored or returned: scrub() takes it out of every
error text before it is written to a row or a log, and the settings hold it
as a SecretStr.
"""

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core import config

logger = logging.getLogger(__name__)

UNSENT = ("waiting_for_upload", "ready", "failed")
LAST_ERROR_LIMIT = 2000
OUTBOX_LISTED = 20


def _now() -> datetime:
    """Naive UTC, as every DateTime column here holds it."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(value: Optional[datetime]) -> Optional[datetime]:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _token() -> str:
    return config.settings.FISQUA_TOKEN.get_secret_value().strip()


def fisqua_configured() -> bool:
    """True when both the Fisqua URL and this unit's token are set."""
    return bool(config.settings.FISQUA_URL.strip()) and bool(_token())


def scrub(text: Any) -> str:
    """`text` as a string with the token taken out, truncated to LAST_ERROR_LIMIT."""
    out = str(text)
    token = _token()
    if token:
        out = out.replace(token, "[token]")
    return out[:LAST_ERROR_LIMIT]


def retry_delay(attempts: int) -> timedelta:
    """The wait after the `attempts`-th failed attempt: the base, doubled each time, capped."""
    base = max(1, config.settings.FISQUA_RETRY_BASE_SECONDS)
    cap = max(base, config.settings.FISQUA_RETRY_MAX_SECONDS)
    exponent = min(max(attempts, 1) - 1, 32)
    return timedelta(seconds=min(base * (2 ** exponent), cap))


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------

@dataclass
class TransportResult:
    """What one call to Fisqua came to.

    `outcome` is `delivered` or `refused` for send_manifest, `status` for
    get_status, and `error` for either when Fisqua could not be reached or
    gave no usable answer (retried). `response` is Fisqua's answer as it
    returned it, when there is one.
    """

    outcome: str
    response: Any = None
    external_id: Optional[str] = None
    error: Optional[str] = None
    problems: Any = None
    status: Optional[str] = None
    status_at: Optional[datetime] = None


class FisquaTransport(Protocol):
    def send_manifest(self, manifest: dict) -> TransportResult: ...

    def get_status(self, external_id: str) -> TransportResult: ...


class NullTransport:
    """Sends nothing. The worker leaves `ready` rows as they are and polls nothing."""

    def send_manifest(self, manifest: dict) -> TransportResult:
        return TransportResult(outcome="error", error="no transport to Fisqua in this version")

    def get_status(self, external_id: str) -> TransportResult:
        return TransportResult(outcome="error", error="no transport to Fisqua in this version")


def get_transport() -> FisquaTransport:
    """The transport for the configured Fisqua. NullTransport until Fisqua's side is agreed."""
    return NullTransport()


# ---------------------------------------------------------------------------
# Enqueue, at handover and take-back
# ---------------------------------------------------------------------------

def enqueue_handover(db: Session, collection):
    """Queue a new manifest for a volume just handed over; supersede its unsent ones.

    Does not commit: the caller commits it with the handover. Returns the new
    row, or None when Fisqua is not configured.
    """
    from app.models.fisqua import FisquaOutbox

    if not fisqua_configured():
        return None
    db.execute(
        update(FisquaOutbox)
        .where(FisquaOutbox.collection_id == collection.id, FisquaOutbox.state.in_(UNSENT))
        .values(state="superseded", next_attempt_at=None)
        .execution_options(synchronize_session=False)
    )
    row = FisquaOutbox(collection_id=collection.id, manifest_id=str(uuid.uuid4()),
                       stem=collection.filename_stem, state="waiting_for_upload", attempts=0,
                       created_at=_now())
    db.add(row)
    return row


def withdraw_unsent(db: Session, collection_id: int) -> None:
    """Withdraw a volume's unsent manifests when its handover is taken back. Does not commit."""
    from app.models.fisqua import FisquaOutbox

    if not fisqua_configured():
        return
    db.execute(
        update(FisquaOutbox)
        .where(FisquaOutbox.collection_id == collection_id, FisquaOutbox.state.in_(UNSENT))
        .values(state="withdrawn", next_attempt_at=None)
        .execution_options(synchronize_session=False)
    )


# ---------------------------------------------------------------------------
# The worker step
# ---------------------------------------------------------------------------

def _move(db: Session, row_id: int, from_states, **values) -> bool:
    """Set `values` on the row if it is still in one of `from_states`; commit. True if it was."""
    from app.models.fisqua import FisquaOutbox

    if isinstance(from_states, str):
        from_states = (from_states,)
    done = db.execute(
        update(FisquaOutbox)
        .where(FisquaOutbox.id == row_id, FisquaOutbox.state.in_(from_states))
        .values(**values)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    return bool(done)


@dataclass
class _Run:
    status: str = "done"
    promoted: int = 0
    not_exportable: int = 0
    delivered: int = 0
    refused: int = 0
    failed: int = 0
    polled: int = 0
    waiting: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _promote(db: Session, row, result: _Run) -> None:
    from app.core import capture_manifest
    from app.core.export_jobs import FilesChanging, export_jobs
    from app.core.image_upload import volume_upload_state
    from app.models.collection import Collection

    collection = db.get(Collection, row.collection_id)
    if collection is None:
        return
    state = volume_upload_state(db, collection)
    if state["uploadable"] and not state["received"]:
        result.waiting += 1
        return
    manifest_uuid = uuid.UUID(row.manifest_id)
    try:
        with export_jobs.hold_for_reading([collection.id]):
            doc = capture_manifest.build_capture_manifest(db, collection, new_id=lambda: manifest_uuid)
    except FilesChanging:
        db.rollback()
        result.waiting += 1
        return
    except capture_manifest.ManifestRefused as exc:
        db.rollback()
        if _move(db, row.id, "waiting_for_upload", state="refused", refusal="not_exportable",
                 problems=[p.as_dict() for p in exc.problems], last_error=scrub(exc.message)):
            result.not_exportable += 1
            logger.warning("Fisqua manifest %s for volume %s is not exportable: %s",
                           row.manifest_id, collection.id, ", ".join(p.rule for p in exc.problems))
        return
    db.rollback()  # end the build's read transaction
    if _move(db, row.id, "waiting_for_upload", state="ready", manifest=doc):
        result.promoted += 1


def _send(db: Session, row, transport, now: datetime, result: _Run) -> None:
    from app.models.fisqua import FisquaOutbox

    attempts = row.attempts + 1
    if not _move(db, row.id, ("ready", "failed"), state="sending", attempts=attempts,
                 last_attempt_at=now, next_attempt_at=None):
        return
    manifest = db.query(FisquaOutbox.manifest).filter(FisquaOutbox.id == row.id).scalar()
    try:
        answer = transport.send_manifest(manifest)
    except Exception as exc:  # the transport is not trusted to classify every failure
        answer = TransportResult(outcome="error", error=f"{type(exc).__name__}: {exc}")

    if answer.outcome == "delivered":
        _move(db, row.id, "sending", state="delivered", delivered_at=now, response=answer.response,
              external_id=answer.external_id, last_error=None)
        result.delivered += 1
    elif answer.outcome == "refused":
        _move(db, row.id, "sending", state="refused", refusal="fisqua", response=answer.response,
              problems=answer.problems, last_error=scrub(answer.error) if answer.error else None)
        result.refused += 1
        logger.warning("Fisqua refused manifest %s of volume %s", row.manifest_id, row.collection_id)
    else:
        error = scrub(answer.error or f"unexpected outcome {answer.outcome!r}")
        _move(db, row.id, "sending", state="failed", last_error=error, response=answer.response,
              next_attempt_at=now + retry_delay(attempts))
        result.failed += 1
        logger.warning("Sending manifest %s of volume %s to Fisqua failed (attempt %s): %s",
                       row.manifest_id, row.collection_id, attempts, error)


def _poll(db: Session, row, transport, now: datetime, result: _Run) -> None:
    from app.models.fisqua import FisquaStatus

    try:
        answer = transport.get_status(row.external_id)
    except Exception as exc:
        answer = TransportResult(outcome="error", error=f"{type(exc).__name__}: {exc}")
    if answer.outcome != "status":
        logger.warning("Asking Fisqua for the status of volume %s failed: %s",
                       row.collection_id, scrub(answer.error or answer.outcome))
        return
    status = db.get(FisquaStatus, row.collection_id)
    if status is None:
        status = FisquaStatus(collection_id=row.collection_id, status_checked_at=now)
        db.add(status)
    status.manifest_id = row.manifest_id
    status.status = answer.status
    status.status_at = _naive(answer.status_at)
    status.status_checked_at = now
    status.problems = answer.problems
    db.commit()
    result.polled += 1


def _latest_delivered(db: Session) -> list:
    """Per volume, its most recently delivered row."""
    from app.models.fisqua import FisquaOutbox

    latest = {}
    for row in (db.query(FisquaOutbox).filter(FisquaOutbox.state == "delivered")
                .order_by(FisquaOutbox.delivered_at, FisquaOutbox.id)):
        latest[row.collection_id] = row
    return list(latest.values())


def run_fisqua_outbox(db: Session, *, transport: Optional[FisquaTransport] = None,
                      now: Optional[datetime] = None, stop: Optional[threading.Event] = None) -> dict:
    """One pass: recover, promote, send, poll. Never raises; the caller gets counts and a status.

    Statuses: "disabled" (Fisqua not configured), "no_transport" (rows are
    promoted, nothing is sent or polled), "done", "stopped".
    """
    from app.models.fisqua import FisquaOutbox

    if not fisqua_configured():
        return {"status": "disabled"}
    transport = transport if transport is not None else get_transport()
    now = _naive(now) or _now()
    result = _Run()

    def stopping() -> bool:
        if stop is not None and stop.is_set():
            result.status = "stopped"
            return True
        return False

    try:
        # Only this step sends, and one at a time, so a row found `sending`
        # was left by a run that did not finish. Fisqua may or may not have
        # it; it is sent again, with the same manifest id.
        for (row_id,) in db.query(FisquaOutbox.id).filter(FisquaOutbox.state == "sending").all():
            _move(db, row_id, "sending", state="failed", next_attempt_at=now,
                  last_error="interrupted while sending")

        for row in (db.query(FisquaOutbox).filter(FisquaOutbox.state == "waiting_for_upload")
                    .order_by(FisquaOutbox.id).all()):
            if stopping():
                return result.as_dict()
            _promote(db, row, result)

        if isinstance(transport, NullTransport):
            result.status = "no_transport"
            return result.as_dict()

        due = (db.query(FisquaOutbox)
               .filter((FisquaOutbox.state == "ready")
                       | ((FisquaOutbox.state == "failed") & (FisquaOutbox.next_attempt_at <= now)))
               .order_by(FisquaOutbox.id).all())
        for row in due:
            if stopping():
                return result.as_dict()
            _send(db, row, transport, now, result)

        for row in _latest_delivered(db):
            if row.external_id is None:
                continue
            if stopping():
                return result.as_dict()
            _poll(db, row, transport, now, result)
    except Exception:
        db.rollback()
        logger.exception("The Fisqua outbox step raised")
        result.status = "error"
    return result.as_dict()


# ---------------------------------------------------------------------------
# The volume page
# ---------------------------------------------------------------------------

_OUTBOX_FIELDS = ("manifest_id", "stem", "state", "created_at", "attempts", "next_attempt_at",
                  "last_attempt_at", "last_error", "refusal", "problems", "response",
                  "external_id", "delivered_at")


def volume_fisqua_state(db: Session, collection_id: int) -> Optional[dict]:
    """The `fisqua` object of the volume's offsite state, or None when Fisqua is not configured.

    `outbox` lists the volume's manifests newest first (at most
    OUTBOX_LISTED), without the manifest itself; `status` is Fisqua's latest
    word on the volume, or None.
    """
    from app.models.fisqua import FisquaOutbox, FisquaStatus

    if not fisqua_configured():
        return None
    rows = (db.query(FisquaOutbox).filter(FisquaOutbox.collection_id == collection_id)
            .order_by(FisquaOutbox.id.desc()).limit(OUTBOX_LISTED).all())
    status = db.get(FisquaStatus, collection_id)
    return {
        "outbox": [{name: getattr(row, name) for name in _OUTBOX_FIELDS} for row in rows],
        "status": None if status is None else {
            "status": status.status, "status_at": status.status_at,
            "status_checked_at": status.status_checked_at, "problems": status.problems,
            "manifest_id": status.manifest_id,
        },
    }
