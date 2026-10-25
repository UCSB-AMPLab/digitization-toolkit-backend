"""Delivering each handed-over volume's capture manifest to Fisqua, and polling its status (contract 1.2).

Every call is the unit's: HTTPS to `<tenant>.fisqua.org` (or the staging
host) with this unit's token (DTK_FISQUA_ENV, DTK_FISQUA_TOKEN; see
app.core.config). The HTTP calls and the reading of Fisqua's answers are in
app.core.fisqua_http, and every name on Fisqua's side in
app.core.fisqua_contract. Here:

* **Enqueue at handover** (enqueue_handover). A handover that promotes at
  least one record adds a `waiting_for_upload` row in the same transaction,
  with a fresh manifest id, when Fisqua is configured. A volume's reference
  code is its recorded external_id, else its stem
  (capture_manifest.reference_code); a volume whose recorded external_id is
  not its stem is `refused` (`not_exportable`) at once, with the problem
  `reference_code_differs` (contract 1.2 §8). A volume with no stem is
  refused at promotion, for `no_stem`. Any older row of the
  volume not yet sent (`waiting_for_upload`, `ready`, `failed`) becomes
  `superseded`, and a take-back makes those rows `withdrawn`
  (withdraw_unsent), whatever the configuration. A row being sent, or
  already delivered or refused, is left as it is: nothing is sent to Fisqua
  on a take-back, and Fisqua replaces an older manifest with a newer one
  until a run has started on it, after which it refuses the newer one.
* **Promotion to ready** (run_fisqua_outbox). A `waiting_for_upload` row is
  promoted once the volume is received (app.core.image_upload's received
  indicator: every object verified in the bucket). The manifest is built
  then, under export_jobs.hold_for_reading, with the row's manifest id, and
  stored on the row, so a retry sends the same document; Fisqua answers a
  repeated id with its original answer, so a resend is safe. A volume the
  builder refuses is `refused` with refusal `not_exportable` and the
  builder's problems on the row. A volume with no stem can never be
  received, so it is built at once, and refused for that.
* **Sending**, after each image upload run. Each `ready` row, and each
  `failed` row whose `next_attempt_at` has come, is claimed (`sending`, one
  more attempt) and handed to the transport. Its answer makes the row
  `delivered` or `refused` (refusal `fisqua`); a transient error makes it
  `failed`, to be tried again after retry_delay, or after Fisqua's
  Retry-After when that is longer. Retries never stop: the unit is often
  offline for days. A configuration error (401, 403, 404 or any redirect)
  stops sending and polling for the whole unit (record_config_error) until
  the environment, the base URL or the token changes: the row goes back to
  `ready`. A 400 for `version`, `envelope` or `shape` (Fisqua cannot read
  what this software writes) refuses its manifest for good and stops the
  unit with reason `version` until the software changes; nothing is built
  meanwhile, manifests built but never sent are built again under new ids,
  and once an update clears the stop each volume whose newest manifest was
  refused for it is handed over again under a new id.
* **The offsite upload hold** (app.core.offsite_hold). While its file exists
  a send pass and a poll tick each answer "held" at their start, with no
  call to Fisqua and nothing promoted; removing it resumes the next pass.
* **Status poll** (poll_fisqua_status), on its own ten-minute schedule
  (FisquaStatusScheduler). Every volume with a manifest that has reached
  Fisqua, or may have, is asked for by its reference code
  (the recorded external_id, else the stem: the manifest's `externalId`), in batches of at
  most MAX_REFS_PER_CALL, leaving DECISIONS_RESERVED_CALLS of the shared
  call budget for decisions reads and starting each tick after the last
  reference asked for, so no batch is starved (by the reference the manifest was sent under,
  fisqua_outbox.reference_code), until Fisqua reports a final state for the
  volume's latest manifest. The answer is kept in fisqua_status. `unknown`
  (Fisqua does not know the volume; usually its manifest was not received)
  is kept and shown, and is not final. An answer naming the manifest id of
  a row whose send failed marks that row delivered, so a lost answer to a
  POST is recovered. An entry whose `decisionsId` is neither the set last
  applied nor one already seen stale has its QC decisions read and applied
  (§9.5, app.core.fisqua_decisions), within the same tick, the same
  transport rules and the same hold.

Every state change is a conditional UPDATE on the state the worker read, so
a take-back or a new handover committed while the worker runs is not
overwritten. The worker takes no lock on records or on the volume row. The
handover paths write these rows after locking the volume's records, which is
the order every path takes (records, then the volume). The send step and
the poll run in different threads and are serialised by _step_lock.

The token is never logged, stored or returned: redact() takes it out of
every string the transport returns (answer, problems, status, ids, errors,
at any depth) before anything is written to a row or a log, scrub() out of
every error text, and the settings hold it as a SecretStr. The unit-level
configuration error keeps only a short fingerprint of the configuration
(environment, base URL, token) that failed.
"""

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Protocol

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core import config, offsite_hold
from app.core import fisqua_contract as fc

logger = logging.getLogger(__name__)

UNSENT = ("waiting_for_upload", "ready", "failed")

# The problem recorded for a handover of a volume with no Fisqua reference
# code (collections.external_id), in the shape of capture_manifest's problems.
def reference_code_differs_problem(stem: str, reference_code: str) -> dict:
    """The problem for a volume whose reference code is not its file name stem (contract 1.2 §8)."""
    return {
        "rule": "reference_code_differs",
        "message": (f"The volume's Fisqua reference code ({reference_code}) is not its file name stem "
                    f"({stem}). Fisqua takes a volume's file name stem as its reference code, unchanged, "
                    "so the volume is not sent to Fisqua while the two differ."),
        "record_ids": [],
        "image_ids": [],
    }


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


def fisqua_environment() -> Optional[str]:
    """`production` or `staging`, or None when DTK_FISQUA_ENV is empty or names neither."""
    env = config.settings.FISQUA_ENV.strip().lower()
    return env if env in fc.ENVIRONMENTS else None


def _tenant() -> Optional[str]:
    from app.core.image_upload import configured_tenant_or_none

    return configured_tenant_or_none()


def token_invalid() -> bool:
    """True when a token is set but is not 64 lowercase hex characters (contract 1.2)."""
    token = _token()
    return bool(token) and fc.TOKEN_PATTERN.match(token) is None


def fisqua_configured() -> bool:
    """True when the environment, a well-formed token and a valid offsite tenant are all set."""
    token = _token()
    return (fisqua_environment() is not None and bool(token) and fc.TOKEN_PATTERN.match(token) is not None
            and _tenant() is not None)


_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
_warned_overrides: set = set()


def _usable_override(value: str) -> Optional[str]:
    """DTK_FISQUA_BASE_URL when it is https://, or http:// to a loopback host (tests); else None, with a warning."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        parts, host = None, None
    if parts is not None and host and parts.scheme == "https":
        return value.rstrip("/")
    if parts is not None and parts.scheme == "http" and host in _LOOPBACK_HOSTS:
        return value.rstrip("/")
    if value not in _warned_overrides:
        _warned_overrides.add(value)
        logger.warning("DTK_FISQUA_BASE_URL %r is ignored: it must be https://, or http:// to 127.0.0.1, "
                       "localhost or [::1] for tests; the environment's host is used", value)
    return None


def base_url() -> Optional[str]:
    """Fisqua's base URL for this unit: DTK_FISQUA_BASE_URL if set, else the environment's host for the tenant."""
    if not fisqua_configured():
        return None
    override = config.settings.FISQUA_BASE_URL.strip()
    if override:
        usable = _usable_override(override)
        if usable is not None:
            return usable
    return fc.ENVIRONMENTS[fisqua_environment()].format(tenant=_tenant())


def config_fingerprint() -> str:
    """8 hex digits of the sha256 of (environment, base URL, token, installed code).

    Enough to tell two configurations apart. The base URL is the one calls
    go to, so a change of DTK_FISQUA_BASE_URL or of the tenant changes it
    too. The installed code is app.core.version.code_identity (the backend
    commit and the release, or the process start when no commit can be
    read), so that an update, or failing that a restart, clears a stop for
    a manifest Fisqua could not read (reason `version`). Neither the token
    nor a full hash of it is kept anywhere.
    """
    from app.core import version

    parts = [fisqua_environment() or "", base_url() or "", _token(), version.code_identity()]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:8]


def scrub(text: Any) -> str:
    """`text` as a string with the token taken out, truncated to LAST_ERROR_LIMIT."""
    return redact(str(text))[:LAST_ERROR_LIMIT]


def redact(value: Any) -> Any:
    """`value` with the token taken out of every string in it, keys included, at any depth.

    Applied to everything a transport returns before it is stored, logged or
    served: Fisqua's answer, its problems, its status and ids.
    """
    token = _token()
    if not token:
        return value
    if isinstance(value, str):
        return value.replace(token, "[token]")
    if isinstance(value, dict):
        return {redact(k): redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


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
    get_status, `decisions` for get_decisions, `config_error` for any of them
    when Fisqua refuses the unit's configuration, and `error` when Fisqua
    could not be reached or gave no usable answer (retried). `response` is Fisqua's answer as it
    returned it, when there is one.
    """

    outcome: str
    response: Any = None
    external_id: Optional[str] = None
    error: Optional[str] = None
    problems: Any = None
    status: Optional[str] = None
    status_at: Optional[datetime] = None
    # The HTTP status of the answer, when there was one.
    status_code: Optional[int] = None
    # Seconds Fisqua asked the unit to wait (Retry-After), when it did.
    retry_after: Optional[float] = None
    # get_status: one entry per volume Fisqua reported.
    volumes: Optional[list] = None
    # config_error: `token`, `not_found` or `redirect`.
    reason: Optional[str] = None


class FisquaTransport(Protocol):
    def send_manifest(self, manifest: dict) -> TransportResult: ...

    def get_status(self, refs: list) -> TransportResult: ...

    def get_decisions(self, ref: str) -> TransportResult: ...


class NullTransport:
    """Sends nothing, for a unit with Fisqua not configured. The worker leaves `ready` rows as they are."""

    def send_manifest(self, manifest: dict) -> TransportResult:
        return TransportResult(outcome="error", error="Fisqua is not configured")

    def get_status(self, refs: list) -> TransportResult:
        return TransportResult(outcome="error", error="Fisqua is not configured")

    def get_decisions(self, ref: str) -> TransportResult:
        return TransportResult(outcome="error", error="Fisqua is not configured")


def get_transport() -> FisquaTransport:
    """The HTTPS transport to the configured Fisqua, or NullTransport when it is not configured."""
    url = base_url()
    if url is None:
        return NullTransport()
    from app.core.fisqua_http import HttpFisquaTransport

    return HttpFisquaTransport(url, _token(), timeout=config.settings.FISQUA_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# The unit-level configuration error
# ---------------------------------------------------------------------------
#
# A file in the data directory rather than a row: it is one fact about the
# unit, not about a volume, and it has to survive a restart, or each restart
# would send once more to a Fisqua that has already said the configuration
# is wrong.

CONFIG_ERROR_FILE = "fisqua-config-error.json"
_config_lock = threading.Lock()


def _config_error_path():
    return config.settings.data_dir / CONFIG_ERROR_FILE


def record_config_error(status_code: Optional[int], reason: Optional[str], now: datetime) -> None:
    """Stop sending and polling until the configuration changes. Keeps the time, the HTTP status, why, and the fingerprint."""
    path = _config_error_path()
    entry = {"at": now.isoformat(), "status_code": status_code, "reason": reason,
             "config_fingerprint": config_fingerprint()}
    with _config_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        os.replace(tmp, path)
    logger.error("Fisqua answered HTTP %s (%s) for this unit's configuration (fingerprint %s); sending and "
                 "polling stop until DTK_FISQUA_ENV, DTK_FISQUA_BASE_URL or DTK_FISQUA_TOKEN is changed",
                 status_code, reason, entry["config_fingerprint"])


def config_error() -> Optional[dict]:
    """The recorded configuration error, while the configuration is still the one that failed; None otherwise.

    A record for another configuration is removed: it was changed, so sending resumes.
    """
    path = _config_error_path()
    with _config_lock:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            entry = None
        if isinstance(entry, dict) and entry.get("config_fingerprint") == config_fingerprint():
            try:
                at = datetime.fromisoformat(entry["at"])
            except (KeyError, TypeError, ValueError):
                at = None
            return {"at": at, "status_code": entry.get("status_code"), "reason": entry.get("reason"),
                    "config_fingerprint": entry["config_fingerprint"]}
        try:
            path.unlink()
        except OSError:
            pass
        if entry is not None:
            logger.info("The Fisqua configuration was changed since Fisqua refused it; sending resumes")
        return None


# Serialises the send step (the upload thread) and the status poll (its own thread).
_step_lock = threading.Lock()


class CallBudget:
    """The contract's calls a minute (fisqua_contract.CALLS_PER_MINUTE), shared by every request to Fisqua.

    A sliding window: a call may be made when fewer than `limit` were made
    in the last `window` seconds. One per process, taken by each manifest
    POST, status GET and decisions GET before it is made; a pass that finds
    it spent stops, and its remaining work waits for the next pass.
    Thread-safe: the send step and the poll run in different threads.
    """

    def __init__(self, limit: int = fc.CALLS_PER_MINUTE, window: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self._limit = limit
        self._window = window
        self._clock = clock
        self._calls: deque = deque()
        self._lock = threading.Lock()

    def cool_down(self, seconds: float) -> None:
        """Hold back polling and decisions requests for `seconds` (Fisqua's Retry-After); a later, longer one wins."""
        with self._lock:
            until = self._clock() + max(0.0, float(seconds))
            self._cool_until = max(getattr(self, "_cool_until", 0.0), until)

    def cooling(self) -> bool:
        """Whether a Retry-After from a status or decisions answer has not yet passed."""
        with self._lock:
            return self._clock() < getattr(self, "_cool_until", float("-inf"))

    def take(self, reserve: int = 0) -> bool:
        """Count one call and return True, or return False when the window is full.

        With `reserve`, False already when only that many calls are left:
        status batches keep DECISIONS_RESERVED_CALLS for decisions reads.
        """
        with self._lock:
            now = self._clock()
            while self._calls and self._calls[0] <= now - self._window:
                self._calls.popleft()
            if len(self._calls) >= self._limit - max(0, reserve):
                return False
            self._calls.append(now)
            return True


call_budget = CallBudget()

# The last reference a status batch asked for. The next poll starts after
# it, so when the budget cannot cover every batch in one tick the later
# references are asked for at the next tick instead of never.
_status_cursor: Optional[str] = None
# The same for decisions reads: the reference of the last set read. A set
# that keeps waiting (pending_handover) or failing goes behind the others at
# the next tick instead of taking the decisions calls every tick.
_decisions_cursor: Optional[str] = None


# ---------------------------------------------------------------------------
# Enqueue, at handover and take-back
# ---------------------------------------------------------------------------

def enqueue_handover(db: Session, collection):
    """Queue a new manifest for a volume just handed over; supersede its unsent ones.

    Does not commit: the caller commits it with the handover. Returns the new
    row, or None when Fisqua is not configured (the unsent rows are
    superseded all the same).
    """
    from app.models.fisqua import FisquaOutbox

    # Whatever the configuration: a manifest queued while Fisqua was
    # configured is no longer the volume's handover, and must not be sent
    # if the configuration returns.
    db.execute(
        update(FisquaOutbox)
        .where(FisquaOutbox.collection_id == collection.id, FisquaOutbox.state.in_(UNSENT))
        .values(state="superseded", next_attempt_at=None)
        .execution_options(synchronize_session=False)
    )
    _cancel_sending(db, collection.id, "superseded")
    if not fisqua_configured():
        return None
    row = FisquaOutbox(collection_id=collection.id, manifest_id=str(uuid.uuid4()),
                       stem=collection.filename_stem, state="waiting_for_upload", attempts=0,
                       created_at=_now())
    problem = _reference_problem(collection)
    if problem is not None:
        row.state, row.refusal, row.problems = "refused", "not_exportable", [problem]
        row.last_error = problem["message"]
    db.add(row)
    return row


def _reference_problem(collection) -> Optional[dict]:
    """The reference_code_differs problem when the volume's recorded code is not its stem; else None.

    Contract 1.2 §8: the stem is the reference code, unchanged. Keys use the
    stem and status is polled by the reference code, so a volume whose
    recorded code differs would be filed under one and asked for by the
    other. With no code recorded the stem is the code; a volume with no stem
    is refused for that when its manifest is built.

    The model now refuses such a pair for every ORM writer
    (app.models.collection, ReferenceCodeDiffersFromStemError), so the API
    cannot produce one. This stays, at handover and again when the manifest
    is built, for rows written before that rule or around it (raw SQL).
    """
    recorded = (collection.external_id or "").strip()
    if collection.filename_stem is not None and recorded and recorded != collection.filename_stem:
        return reference_code_differs_problem(collection.filename_stem, collection.external_id)
    return None


def withdraw_unsent(db: Session, collection_id: int) -> None:
    """Withdraw a volume's unsent manifests when its handover is taken back. Does not commit.

    Whatever the configuration, so a row queued while Fisqua was configured
    is not sent for an undone handover once the configuration returns.
    """
    from app.models.fisqua import FisquaOutbox

    db.execute(
        update(FisquaOutbox)
        .where(FisquaOutbox.collection_id == collection_id, FisquaOutbox.state.in_(UNSENT))
        .values(state="withdrawn", next_attempt_at=None)
        .execution_options(synchronize_session=False)
    )
    _cancel_sending(db, collection_id, "withdrawn")


def _cancel_sending(db: Session, collection_id: int, state: str) -> None:
    """Mark the volume's row being sent, if any, to become `state` unless the send delivers it. Does not commit.

    The worker owns a `sending` row until the send ends, so the request asks
    rather than writes the state: delivered or refused stand (Fisqua's
    supersede rule handles a later manifest), anything else takes `state`
    instead of a retry.
    """
    from app.models.fisqua import FisquaOutbox

    db.execute(
        update(FisquaOutbox)
        .where(FisquaOutbox.collection_id == collection_id, FisquaOutbox.state == "sending")
        .values(cancel_requested=state)
        .execution_options(synchronize_session=False)
    )


def _end_send(db: Session, row_id: int, **values) -> Optional[str]:
    """End a `sending` row that was not delivered or refused: `values`, or the state a cancel asked for.

    Commits. Returns the state the row took (None if it was no longer
    sending). One conditional UPDATE each way, so a cancel committed while
    the answer was being read is not lost.
    """
    from app.models.fisqua import FisquaOutbox

    done = db.execute(
        update(FisquaOutbox)
        .where(FisquaOutbox.id == row_id, FisquaOutbox.state == "sending",
               FisquaOutbox.cancel_requested.is_(None))
        .values(**values)
        .execution_options(synchronize_session=False)
    ).rowcount
    if done:
        db.commit()
        return values.get("state")
    cancelled = db.execute(
        update(FisquaOutbox)
        .where(FisquaOutbox.id == row_id, FisquaOutbox.state == "sending",
               FisquaOutbox.cancel_requested.isnot(None))
        .values(state=FisquaOutbox.cancel_requested, next_attempt_at=None,
                last_error=values.get("last_error"))
        .execution_options(synchronize_session=False)
    ).rowcount
    state = db.query(FisquaOutbox.state).filter(FisquaOutbox.id == row_id).scalar() if cancelled else None
    db.commit()
    return state


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
    problem = _reference_problem(collection)
    if problem is not None:
        db.rollback()
        if _move(db, row.id, "waiting_for_upload", state="refused", refusal="not_exportable",
                 problems=[problem], last_error=problem["message"]):
            result.not_exportable += 1
            logger.warning("Fisqua manifest %s for volume %s is not exportable: reference_code_differs",
                           row.manifest_id, collection.id)
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
    sent_ref = (doc.get("volumes") or [{}])[0].get("externalId")
    if _move(db, row.id, "waiting_for_upload", state="ready", manifest=doc,
             reference_code=sent_ref[:255] if isinstance(sent_ref, str) and sent_ref else None):
        result.promoted += 1


def _redacted(answer: TransportResult) -> TransportResult:
    """The transport's answer with the token taken out of every payload it carries."""
    return TransportResult(outcome=answer.outcome, response=redact(answer.response),
                           external_id=redact(answer.external_id), error=redact(answer.error),
                           problems=redact(answer.problems), status=redact(answer.status),
                           status_at=answer.status_at, status_code=answer.status_code,
                           retry_after=answer.retry_after, volumes=redact(answer.volumes),
                           reason=answer.reason)


def _keep_status(db: Session, collection_id: int, manifest_id: Optional[str], state: Optional[str],
                 entered_at: Optional[datetime], problems: Any, now: datetime) -> bool:
    """Keep Fisqua's latest word on a volume. Commits. True when its manifest, state or problems changed."""
    from app.models.fisqua import FisquaStatus

    status = db.get(FisquaStatus, collection_id)
    changed = status is None or (status.manifest_id, status.status, status.problems) != (manifest_id, state, problems)
    if status is None:
        status = FisquaStatus(collection_id=collection_id, status_checked_at=now)
        db.add(status)
    status.manifest_id = manifest_id
    status.status = state
    status.status_at = _naive(entered_at)
    status.status_checked_at = now
    status.problems = problems
    db.commit()
    return changed


OBJECT_MISSING_ACTION = "fisqua_object_missing"


def _log_object_missing(db: Session, row, problems: Any) -> None:
    """An incident, logged at ERR: Fisqua did not find objects this unit had verified in the bucket (§9.2)."""
    from app.core import audit

    missing = [p for p in problems or [] if isinstance(p, dict) and p.get(fc.F_RULE) == fc.RULE_OBJECT_MISSING]
    if not missing:
        return
    keys = [f"{p.get(fc.F_OBJECT_KEY)} ({p.get(fc.F_KIND)})" for p in missing]
    logger.error("Fisqua found %s object(s) of manifest %s missing from the masters bucket, which this unit "
                 "had verified: %s", len(missing), row.manifest_id, ", ".join(keys))
    audit.log_event(db, level="ERR", category="system", action=OBJECT_MISSING_ACTION, subject=row.stem,
                    detail=scrub(f"manifest {row.manifest_id} missing={len(missing)} keys: " + ", ".join(keys)))


def _version_refusal(problems: Any) -> bool:
    """Whether a refusal's problems include a rule meaning Fisqua cannot read this software's manifests."""
    return any(isinstance(p, dict) and p.get(fc.F_RULE) in fc.VERSION_RULES for p in problems or [])


def _rebuild_unsent(db: Session) -> None:
    """Put manifests built but never sent back to be built again, under new ids. Commits.

    After a version refusal, a `ready` row with no attempt was built by the
    software Fisqua refused. Fisqua has never seen its id, so a new one is
    safe; the manifest is built again, by the updated software, once the
    stop clears.
    """
    from app.models.fisqua import FisquaOutbox

    for (row_id,) in (db.query(FisquaOutbox.id)
                      .filter(FisquaOutbox.state == "ready", FisquaOutbox.attempts == 0).all()):
        _move(db, row_id, "ready", state="waiting_for_upload", manifest=None, reference_code=None,
              manifest_id=str(uuid.uuid4()))


def _resend_version_refusals(db: Session) -> int:
    """Hand over again, under a new manifest id, each volume whose newest manifest was refused for version.

    Run only while no stop is recorded, so after the update that cleared it.
    Fisqua replays a stored refusal for the same id, hence the new one. Only
    a volume whose newest outbox row is that refusal, and which has no
    captured record, is handed over again: a newer row means a later
    handover already stands for it, and a captured record means it was taken
    back or is being captured, and its next handover will be sent. Returns
    how many were handed over again.
    """
    from sqlalchemy import func

    from app.models.collection import Collection
    from app.models.fisqua import FisquaOutbox
    from app.models.record import Record

    newest = (db.query(FisquaOutbox.collection_id, func.max(FisquaOutbox.id))
              .group_by(FisquaOutbox.collection_id).subquery())
    rows = (db.query(FisquaOutbox)
            .join(newest, FisquaOutbox.id == newest.c[1])
            .filter(FisquaOutbox.state == "refused", FisquaOutbox.refusal == "fisqua").all())
    again = 0
    for row in rows:
        if not _version_refusal(row.problems):
            continue
        captured = db.query(Record.id).filter(Record.collection_id == row.collection_id,
                                              Record.status == "captured").first()
        collection = db.get(Collection, row.collection_id)
        if captured is not None or collection is None:
            continue
        enqueue_handover(db, collection)
        db.commit()
        again += 1
        logger.info("Volume %s handed to Fisqua again under a new manifest id after a version refusal",
                    row.collection_id)
    return again


def _send(db: Session, row, transport, clock: Callable[[], datetime], result: _Run) -> bool:
    """Send one row. False when Fisqua refused the token, and sending is to stop.

    Times are read from `clock` when each thing happens: the attempt when the
    row is claimed, and every deadline from when the answer came, so a long
    send or a long pass neither shortens the backoff nor breaks a Retry-After.
    """
    from app.models.fisqua import FisquaOutbox

    attempts = row.attempts + 1
    if not _move(db, row.id, ("ready", "failed"), state="sending", attempts=attempts,
                 last_attempt_at=clock(), next_attempt_at=None):
        return True
    manifest = db.query(FisquaOutbox.manifest).filter(FisquaOutbox.id == row.id).scalar()
    try:
        answer = transport.send_manifest(manifest)
    except Exception as exc:  # the transport is not trusted to classify every failure
        answer = TransportResult(outcome="error", error=f"{type(exc).__name__}: {exc}")
    now = _naive(clock())
    answer = _redacted(answer)

    if answer.outcome == "delivered":
        if _move(db, row.id, "sending", state="delivered", delivered_at=now, response=answer.response,
                 external_id=answer.external_id, last_error=None, cancel_requested=None):
            _keep_status(db, row.collection_id, row.manifest_id, answer.status, None, None, now)
            logger.info("Fisqua accepted manifest %s of volume %s: %s",
                        row.manifest_id, row.collection_id, answer.status or "no state given")
        result.delivered += 1
    elif answer.outcome == "config_error":
        # Not this manifest's fault: it goes back to ready, to be sent as it
        # is once the configuration is changed.
        _end_send(db, row.id, state="ready", attempts=attempts - 1,
                  last_error=scrub(answer.error or "Fisqua refused this unit's configuration"))
        record_config_error(answer.status_code, answer.reason, now)
        result.status = "config_error"
        return False
    elif answer.outcome == "refused":
        _move(db, row.id, "sending", state="refused", refusal="fisqua", response=answer.response,
              problems=answer.problems, last_error=scrub(answer.error) if answer.error else None,
              cancel_requested=None)
        result.refused += 1
        logger.warning("Fisqua refused manifest %s of volume %s", row.manifest_id, row.collection_id)
        _log_object_missing(db, row, answer.problems)
        if answer.status_code == 400 and _version_refusal(answer.problems):
            # Fisqua cannot read what this software writes: final for this
            # manifest, as the contract has it, and a stop for the unit, so
            # later handovers wait for an update instead of each being sent
            # and refused (_rebuild_unsent rebuilds those already built).
            record_config_error(400, fc.VERSION_REASON, now)
            _rebuild_unsent(db)
            result.status = "config_error"
            return False
    else:
        error = scrub(answer.error or f"unexpected outcome {answer.outcome!r}")
        wait = retry_delay(attempts)
        if answer.retry_after is not None:
            wait = max(wait, timedelta(seconds=answer.retry_after))
        ended = _end_send(db, row.id, state="failed", last_error=error, response=answer.response,
                          next_attempt_at=now + wait)
        if ended == "failed":
            result.failed += 1
            logger.warning("Sending manifest %s of volume %s to Fisqua failed (attempt %s): %s",
                           row.manifest_id, row.collection_id, attempts, error)
        else:
            logger.info("Sending manifest %s of volume %s to Fisqua failed after it was %s; not retried",
                        row.manifest_id, row.collection_id, ended)
    return True


def run_fisqua_outbox(db: Session, *, transport: Optional[FisquaTransport] = None,
                      now: Optional[datetime] = None, stop: Optional[threading.Event] = None,
                      clock: Optional[Callable[[], datetime]] = None) -> dict:
    """One pass: recover, promote, send. Never raises; the caller gets counts and a status.

    Statuses: "rate_limited" (the shared call_budget was spent: the rest is
    sent at a later pass), "disabled" (Fisqua not configured), "held" (the offsite upload
    hold exists, app.core.offsite_hold: nothing is promoted or sent, and no
    call is made), "no_transport" (rows are promoted, nothing is sent), "config_error" (Fisqua refused the
    configuration: rows are promoted, nothing is sent until it changes), "done",
    "stopped", "error".
    """
    from app.models.fisqua import FisquaOutbox

    if not fisqua_configured():
        return {"status": "disabled"}
    # Promotion too: while the hold exists the one-time commands are still
    # completing the objects' state, and a volume can read as received, or
    # not exportable, on what they have not yet written.
    if offsite_hold.held("fisqua outbox"):
        return {"status": "held", "hold_file": str(offsite_hold.hold_file())}
    transport = transport if transport is not None else get_transport()
    if clock is None:
        fixed = _naive(now)
        clock = (lambda: fixed) if fixed is not None else _now
    now = _naive(clock())
    result = _Run()

    def stopping() -> bool:
        if stop is not None and stop.is_set():
            result.status = "stopped"
            return True
        return False

    with _step_lock:
        try:
            # Only this step sends, and one at a time, so a row found
            # `sending` was left by a run that did not finish. Fisqua may or
            # may not have it; it is sent again with the same manifest id,
            # and Fisqua answers a repeated id with its original answer.
            for (row_id,) in db.query(FisquaOutbox.id).filter(FisquaOutbox.state == "sending").all():
                _end_send(db, row_id, state="failed", next_attempt_at=now,
                          last_error="interrupted while sending")

            # Not `stop`: that is the caller's shutdown event, which stopping() reads.
            unit_stop = config_error()
            if unit_stop is not None and unit_stop.get("reason") == fc.VERSION_REASON:
                # Nothing is built under the software Fisqua could not read.
                result.status = "config_error"
                return result.as_dict()
            if unit_stop is None:
                _resend_version_refusals(db)

            for row in (db.query(FisquaOutbox).filter(FisquaOutbox.state == "waiting_for_upload")
                        .order_by(FisquaOutbox.id).all()):
                if stopping():
                    return result.as_dict()
                _promote(db, row, result)

            if isinstance(transport, NullTransport):
                result.status = "no_transport"
                return result.as_dict()
            if config_error() is not None:
                result.status = "config_error"
                return result.as_dict()

            due = (db.query(FisquaOutbox)
                   .filter((FisquaOutbox.state == "ready")
                           | ((FisquaOutbox.state == "failed") & (FisquaOutbox.next_attempt_at <= now)))
                   .order_by(FisquaOutbox.id).limit(fc.MAX_SENDS_PER_PASS).all())
            # At most MAX_SENDS_PER_PASS (the contract's calls a minute); the
            # rest are sent on the next pass.
            for row in due:
                if stopping():
                    return result.as_dict()
                if not call_budget.take():
                    result.status = "rate_limited"
                    break
                if not _send(db, row, transport, clock, result):
                    break
        except Exception:
            db.rollback()
            logger.exception("The Fisqua outbox step raised")
            result.status = "error"
    _log_pass(result)
    return result.as_dict()


def _log_pass(result: "_Run") -> None:
    """One line for the journal for a send pass that built, refused, sent or failed anything."""
    if not (result.promoted or result.not_exportable or result.delivered or result.refused or result.failed):
        return
    quiet = result.status in ("done", "stopped", "rate_limited") and not (result.refused or result.failed)
    logger.log(logging.INFO if quiet else logging.WARNING,
               "Fisqua outbox pass %s: promoted=%s not_exportable=%s delivered=%s refused=%s failed=%s waiting=%s",
               result.status, result.promoted, result.not_exportable, result.delivered, result.refused,
               result.failed, result.waiting)


# ---------------------------------------------------------------------------
# The status poll
# ---------------------------------------------------------------------------

SENT = ("delivered", "failed")


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _naive(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
    except ValueError:
        return None


def volumes_to_poll(db: Session) -> dict:
    """{reference code: (collection id, manifest id)} for every volume whose newest sent manifest is not final.

    A row counts as sent once it is delivered, or once an attempt failed:
    Fisqua may have it, and a poll by reference recovers a lost answer. A
    volume with neither a recorded reference code nor a stem cannot be asked
    for. One query, selecting
    only ids and names: the outbox keeps every manifest and answer a volume
    ever had, and none of them is needed to choose what to ask for.
    """
    from sqlalchemy import and_, func, not_, or_

    from app.models.collection import Collection
    from app.models.fisqua import FisquaOutbox, FisquaStatus

    newest = (db.query(FisquaOutbox.collection_id.label("collection_id"),
                       func.max(FisquaOutbox.id).label("row_id"))
              .filter(FisquaOutbox.state.in_(SENT), FisquaOutbox.attempts > 0)
              .group_by(FisquaOutbox.collection_id)
              .subquery())
    # COALESCE, so a status row with no state or no manifest id reads as not
    # final rather than as NULL, which NOT would also drop.
    # A set offered and neither applied nor seen stale keeps the volume
    # polled whatever its state, so a fetch that failed is tried again.
    offered = FisquaStatus.decisions_offered_id
    unread = and_(offered.isnot(None),
                  offered != func.coalesce(FisquaStatus.decisions_id, ""),
                  offered != func.coalesce(FisquaStatus.decisions_stale_id, ""))
    final = and_(func.coalesce(FisquaStatus.status, "").in_(fc.FINAL_STATES),
                 func.coalesce(FisquaStatus.manifest_id, "") == FisquaOutbox.manifest_id,
                 not_(unread))
    # The reference the newest sent manifest carried (fisqua_outbox.reference_code),
    # not the volume's code now: a first external_id set after the manifest
    # was sent under the stem would ask Fisqua for a volume it does not
    # know. A row without one falls back to the code as
    # capture_manifest.reference_code gives it.
    ref = func.coalesce(FisquaOutbox.reference_code,
                        func.nullif(func.trim(Collection.external_id), ""), Collection.filename_stem)
    rows = (db.query(ref, Collection.id, FisquaOutbox.manifest_id)
            .select_from(newest)
            .join(FisquaOutbox, FisquaOutbox.id == newest.c.row_id)
            .join(Collection, Collection.id == newest.c.collection_id)
            .outerjoin(FisquaStatus, FisquaStatus.collection_id == newest.c.collection_id)
            .filter(ref.isnot(None))
            .filter(or_(FisquaStatus.collection_id.is_(None), not_(final)))
            .all())
    return {ref: (cid, manifest_id) for ref, cid, manifest_id in rows}


def poll_fisqua_status(db: Session, *, transport: Optional[FisquaTransport] = None,
                       now: Optional[datetime] = None) -> dict:
    """Ask Fisqua for every volume not yet final, in batches; keep each answer. Never raises.

    Answers "held", with no call, while the offsite upload hold exists
    (app.core.offsite_hold).
    """
    from app.models.fisqua import FisquaOutbox

    if not fisqua_configured():
        return {"status": "disabled"}
    if offsite_hold.held("fisqua status poll"):
        return {"status": "held", "hold_file": str(offsite_hold.hold_file())}
    transport = transport if transport is not None else get_transport()
    if isinstance(transport, NullTransport):
        return {"status": "no_transport"}
    now = _naive(now) or _now()
    result = {"status": "done", "asked": 0, "calls": 0, "answered": 0, "changed": 0, "recovered": 0,
              "decisions_fetched": 0, "decisions_applied": 0, "decisions_stale": 0}
    with _step_lock:
        try:
            if config_error() is not None:
                return {**result, "status": "config_error"}
            # Fisqua asked the unit to wait (Retry-After on a status or
            # decisions answer); sends keep their own per-row backoff.
            if call_budget.cooling():
                return {**result, "status": "cooling_down"}
            wanted = volumes_to_poll(db)
            db.rollback()
            global _status_cursor
            refs = sorted(wanted)
            if _status_cursor is not None:
                from bisect import bisect_right
                at = bisect_right(refs, _status_cursor)
                refs = refs[at:] + refs[:at]
            result["asked"] = len(refs)
            # {collection id: (ref, decisionsId)} for each entry offering a set.
            offered = {}
            for start in range(0, len(refs), fc.MAX_REFS_PER_CALL):
                chunk = refs[start:start + fc.MAX_REFS_PER_CALL]
                if not call_budget.take(reserve=fc.DECISIONS_RESERVED_CALLS):
                    result["status"] = "rate_limited"
                    break
                result["calls"] += 1
                _status_cursor = chunk[-1]
                try:
                    answer = transport.get_status(chunk)
                except Exception as exc:
                    answer = TransportResult(outcome="error", error=f"{type(exc).__name__}: {exc}")
                answer = _redacted(answer)
                if answer.outcome == "config_error":
                    record_config_error(answer.status_code, answer.reason, now)
                    return {**result, "status": "config_error"}
                if answer.outcome != "status":
                    logger.warning("Asking Fisqua for the status of %s volume(s) failed: %s",
                                   len(chunk), scrub(answer.error or answer.outcome))
                    if answer.retry_after is not None:
                        call_budget.cool_down(answer.retry_after)
                        return {**result, "status": "cooling_down"}
                    result["status"] = "error"
                    continue
                for entry in answer.volumes or []:
                    ref = entry.get(fc.F_REF)
                    if ref not in wanted or ref not in chunk:
                        continue
                    cid, _manifest_id = wanted[ref]
                    manifest_id = entry.get(fc.F_MANIFEST_ID)
                    manifest_id = manifest_id if isinstance(manifest_id, str) else None
                    state = entry.get(fc.F_STATE)
                    problems = entry.get(fc.F_PROBLEMS)
                    if _keep_status(db, cid, manifest_id, state if isinstance(state, str) else None,
                                    _parse_time(entry.get(fc.F_ENTERED_AT)),
                                    problems if problems else None, now):
                        result["changed"] += 1
                    result["answered"] += 1
                    decisions_id = entry.get(fc.F_DECISIONS_ID)
                    if isinstance(decisions_id, str) and decisions_id:
                        offered[cid] = (ref, decisions_id)
                        _keep_offered(db, cid, decisions_id[:255])
                    # Recover a lost answer only when Fisqua accepted the
                    # manifest. One it refused is left failed and sent again
                    # with the same id: Fisqua replays its stored answer, and
                    # the refusal takes the one path every refusal takes
                    # (_send), object-missing incident included.
                    if manifest_id is not None and state != fc.STATE_REFUSED:
                        lost = (db.query(FisquaOutbox.id)
                                .filter(FisquaOutbox.collection_id == cid,
                                        FisquaOutbox.manifest_id == manifest_id,
                                        FisquaOutbox.state == "failed")
                                .scalar())
                        db.rollback()
                        volume_id = entry.get(fc.F_VOLUME_ID)
                        if lost is not None and _move(
                                db, lost, "failed", state="delivered", delivered_at=now,
                                response=entry, last_error=None, next_attempt_at=None,
                                external_id=str(volume_id) if volume_id not in (None, "") else None):
                            result["recovered"] += 1
            if _fetch_decisions(db, transport, offered, now, result) is False:
                return {**result, "status": "config_error"}
        except Exception:
            db.rollback()
            logger.exception("The Fisqua status poll raised")
            result["status"] = "error"
    _log_poll(result)
    return result


def _log_poll(result: dict) -> None:
    """One line for the journal for a poll that asked Fisqua anything."""
    if not result.get("calls"):
        return
    logger.log(logging.INFO if result["status"] in ("done", "rate_limited") else logging.WARNING,
               "Fisqua status poll %s: asked=%s calls=%s answered=%s changed=%s recovered=%s "
               "decisions_fetched=%s decisions_applied=%s decisions_stale=%s",
               result["status"], result["asked"], result["calls"], result["answered"], result["changed"],
               result["recovered"], result["decisions_fetched"], result["decisions_applied"],
               result["decisions_stale"])


def _keep_offered(db: Session, collection_id: int, decisions_id: str) -> None:
    from app.models.fisqua import FisquaStatus

    status = db.get(FisquaStatus, collection_id)
    if status is not None and status.decisions_offered_id != decisions_id:
        status.decisions_offered_id = decisions_id
        db.commit()


def _fetch_decisions(db: Session, transport, offered: dict, now: datetime, result: dict) -> bool:
    """Read and apply each offered decisions set not applied or seen stale already (§9.5).

    Each read takes a call from the shared call_budget; when it is spent,
    the rest are read at a later tick. Reads start after the reference read
    last (_decisions_cursor), so no volume's set is starved by others that
    keep waiting or failing. False when Fisqua refused the unit's
    configuration, and the poll is to stop.
    """
    global _decisions_cursor
    from bisect import bisect_right

    from app.core.fisqua_decisions import apply_decisions
    from app.models.fisqua import FisquaStatus

    order = sorted(offered.items(), key=lambda item: item[1][0])
    if _decisions_cursor is not None:
        at = bisect_right([ref for _cid, (ref, _d) in order], _decisions_cursor)
        order = order[at:] + order[:at]
    for cid, (ref, decisions_id) in order:
        status = db.get(FisquaStatus, cid)
        seen = (status.decisions_id, status.decisions_stale_id) if status is not None else (None, None)
        db.rollback()
        if decisions_id in seen:
            continue
        if not call_budget.take():
            result["status"] = "rate_limited"
            break
        result["decisions_fetched"] += 1
        _decisions_cursor = ref
        try:
            answer = transport.get_decisions(ref)
        except Exception as exc:
            answer = TransportResult(outcome="error", error=f"{type(exc).__name__}: {exc}")
        answer = _redacted(answer)
        if answer.outcome == "config_error":
            record_config_error(answer.status_code, answer.reason, now)
            return False
        if answer.outcome != "decisions":
            logger.warning("Reading Fisqua's decisions for volume %s failed: %s",
                           cid, scrub(answer.error or answer.outcome))
            if answer.retry_after is not None:
                call_budget.cool_down(answer.retry_after)
                result["status"] = "cooling_down"
                break
            result["status"] = "error"
            continue
        applied = apply_decisions(db, cid, answer.response, now)
        if applied["status"] == "applied":
            result["decisions_applied"] += 1
        elif applied["status"] == "stale":
            result["decisions_stale"] += 1
    return True


class FisquaStatusScheduler:
    """Polls Fisqua every FISQUA_POLL_INTERVAL_SECONDS (ten minutes), in a thread of its own.

    A tick with no volume to ask for makes no call, so the poll runs only
    while some sent volume is not final.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_result: Optional[dict] = None

    def start(self) -> bool:
        if not fisqua_configured():
            logger.info("Fisqua is not configured; no status poll started")
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="fisqua-status", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        if self._stop.wait(config.settings.OFFSITE_BACKUP_DELAY_SECONDS):
            return
        while True:
            self.tick()
            if self._stop.wait(config.settings.FISQUA_POLL_INTERVAL_SECONDS):
                return

    def tick(self) -> Optional[dict]:
        from app.core.db import SessionLocal

        db = SessionLocal()
        try:
            self.last_result = poll_fisqua_status(db)
            return self.last_result
        except Exception:
            logger.exception("Scheduled Fisqua status poll raised")
            return None
        finally:
            db.close()


fisqua_status_scheduler = FisquaStatusScheduler()


# ---------------------------------------------------------------------------
# The volume page
# ---------------------------------------------------------------------------

_OUTBOX_FIELDS = ("manifest_id", "stem", "state", "created_at", "attempts", "next_attempt_at",
                  "last_attempt_at", "last_error", "refusal", "problems", "response",
                  "external_id", "delivered_at")


def volume_fisqua_state(db: Session, collection_id: int) -> Optional[dict]:
    """The `fisqua` object of the volume's offsite state, or None when Fisqua is not configured.

    `held` and `hold_file` are the offsite upload hold's (app.core.offsite_hold),
    under which nothing is sent or polled. `environment` is `production` or
    `staging`. `config_error` is the
    unit-level stop (time, HTTP status, reason `token`, `not_found` or
    `redirect`, configuration fingerprint) while Fisqua refuses this unit's
    configuration, else None. `outbox` lists the volume's
    manifests newest first (at most OUTBOX_LISTED), without the manifest
    itself; `status` is Fisqua's latest word on the volume, or None, with its
    problems whatever the state.
    """
    from app.models.fisqua import FisquaOutbox, FisquaStatus

    if not fisqua_configured():
        return None
    rows = (db.query(FisquaOutbox).filter(FisquaOutbox.collection_id == collection_id)
            .order_by(FisquaOutbox.id.desc()).limit(OUTBOX_LISTED).all())
    from app.core.fisqua_decisions import decisions_state

    status = db.get(FisquaStatus, collection_id)
    return {
        "environment": fisqua_environment(),
        "config_error": config_error(),
        **offsite_hold.hold_state(),
        "outbox": [{name: getattr(row, name) for name in _OUTBOX_FIELDS} for row in rows],
        "status": None if status is None else {
            "status": status.status, "status_at": status.status_at,
            "status_checked_at": status.status_checked_at, "problems": status.problems,
            "manifest_id": status.manifest_id,
        },
        # Fisqua's QC decisions, as last applied (app.core.fisqua_decisions), or None.
        "decisions": decisions_state(status),
    }


def unit_fisqua_summary() -> dict:
    """The unit-level Fisqua state for the system panel: configured, environment, configuration error, last runs."""
    from app.core.image_upload import image_upload_scheduler

    configured = fisqua_configured()
    return {
        "configured": configured,
        # A token is set but is not of the contract's shape; its value is never shown.
        "token_invalid": token_invalid(),
        "environment": fisqua_environment() if configured else None,
        "config_error": config_error() if configured else None,
        **offsite_hold.hold_state(),
        "last_send": image_upload_scheduler.last_fisqua_result,
        "last_poll": fisqua_status_scheduler.last_result,
    }
