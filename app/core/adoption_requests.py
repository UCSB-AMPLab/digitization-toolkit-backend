"""A client's id per adoption: the request recorded, its outcome, and what a repeat or a lookup answers.

POST /collections/{source}/adopt-into/{target} may carry `client_request_id`
(app.api.collections), a UUID in its canonical 36-character form, in either
case, stored lowercase: the form and validation of a capture's
`client_capture_id` (app.core.capture_requests). Adoptions are an
administrator's and real work only, so ids have no space.

The record
----------
With an id, app.core.adopt_apply.adopt_captures, once it has taken
ADOPTION_LOCK, inserts the request's row (app.models.adoption_request) and
commits it through a session of its own before anything else (`begin`). A
failed insert answers 500 and no adoption starts. The journal of a request
with an id carries `request_id`.

`applied` is written by the adoption itself, in its own transaction: the
UPDATE that sets the row to `applied`, with the summary as known at the
commit, is made through the adoption's session just before its commit
(adopt_apply._mark_applied), so the row says `applied` exactly when the
adoption committed. An UPDATE that fails fails the adoption, which is undone
as any other failure there. The flags known only after the commit
(`target_lines_appended`, `target_lines_pending`, `recovery_pending`) are
written to the stored result afterwards, best effort (`finish_applied`);
when that write is lost, the stored result keeps the flags as known at the
commit (lines not appended, both pending).

Every other write is an observer, which never raises: `refused` and
`failed` as the request answered (`end`), and `reverted` from a recovery
that undid the adoption (`end_from_recovery`). A recovery that finished an
adoption writes nothing: its commit had landed, so the row says `applied`
already; a row that does not is logged at ERROR.

Not applied
-----------
A row with no outcome is settled only under ADOPTION_LOCK (`_settle`),
never from the snapshot the report reads. The lookup and a repeated POST
try to take the lock without waiting:

* held: nothing is concluded or written. `running` when the holder's
  request id (adopt_journal.lock_holder_request_id, a plain attribute set
  right after the lock is taken and reset in the finally that releases it)
  is this id, `busy` otherwise;
* taken: no adoption, recovery or repair is running. A journal in place
  that carries this id: `interrupted` (recovery or a repair settles it),
  nothing written; with an unreadable journal, `interrupted` too, since it
  may be this one's. Otherwise the request's transaction never committed,
  or the row would say `applied`: `reverted` is written (nothing of it is
  in place), and the lock released.

A refused or failed answer whose write was lost reads `reverted` the same
way, which is true: nothing changed.

Closing on lookup
-----------------
A lookup of an id with no row closes it, as a capture lookup closes a
capture id (app.core.capture_requests), but durably: it inserts the id's
row with outcome `closed`, and answers 404 `not_found` with `state:
"closed"`, as does every later lookup of the id. A POST inserts its row
right after taking the lock, or, refused at the lock, never inserts one; so
the one unique constraint on `request_id` decides between the lookup's
insert and the POST's, in either order: when the lookup inserts first, the
POST's insert fails (AlreadyRecorded) and it is refused below, having done
nothing; when the POST inserts first, the lookup's fails and it answers the
POST's row.

A repeated POST
---------------
A request with an id is answered before it reaches the lock by `stored`:

* a closed row: 409 `adoption_abandoned` with the id. A closed id never
  adopts;
* no row: the adoption is attempted;
* a row for other volumes than the path names: 409
  `client_request_id_in_use`, naming nothing of that request;
* a row the request answered (`applied`, `refused`, `failed`): the same
  status and body again;
* a row with no outcome: `_settle`. `running` or `busy`: 409
  `adoption_in_progress` with the id of the request holding the lock;
  `interrupted`, or `reverted` written now: 409 `adoption_interrupted`
  with that state;
* a row recovery or `_settle` wrote (`reverted`, no status): 409
  `adoption_interrupted` with that state, since that request never
  answered.

Two requests with one id: the unique constraint decides, and the one whose
insert fails is answered from the row the other wrote.

Version: 0.3.3
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

# A UUID in its canonical 36-character form, in either case (as a capture's client_capture_id).
REQUEST_ID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

IN_USE_MESSAGE = ("This request id was already used for an adoption of other volumes. Send a new id for a new "
                  "adoption.")
ABANDONED_MESSAGE = ("This adoption request was already reported as unknown to the unit. Send a new id to adopt "
                     "again.")
IN_PROGRESS_MESSAGE = "This adoption is still running. Ask for its outcome instead of sending it again."
BUSY_MESSAGE = "Another adoption, a recovery or a repair holds the unit now. Ask again when it has ended."
INTERRUPTED_MESSAGE = ("This adoption was interrupted before it answered. Ask for its outcome instead of "
                       "sending it again.")


class AlreadyRecorded(Exception):
    """Another request recorded the id first; it is answered from that row."""


def validate(v: Optional[str]) -> Optional[str]:
    """None, or the id in lowercase: one id is one adoption whatever case the client sends it in."""
    if v is None:
        return None
    if not re.fullmatch(REQUEST_ID_PATTERN, v):
        raise ValueError("client_request_id must be a UUID in its canonical 36-character form")
    return v.lower()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _session():
    from app.core.db import SessionLocal

    return SessionLocal()


def _find(db, request_id: str):
    from app.models.adoption_request import AdoptionRequest

    return db.query(AdoptionRequest).filter(AdoptionRequest.request_id == request_id).first()


def begin(request_id: str, source_id: int, target_id: int, started_at: datetime) -> None:
    """Insert and commit the request's row; AlreadyRecorded when the id has one already."""
    from app.models.adoption_request import AdoptionRequest

    db = _session()
    try:
        db.add(AdoptionRequest(request_id=request_id, source_id=source_id, target_id=target_id,
                               started_at=started_at))
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise AlreadyRecorded(request_id) from exc
    finally:
        db.close()


def _quietly(step) -> None:
    """Run a session's cleanup step, logging what it raises and raising nothing."""
    try:
        step()
    except Exception:  # noqa: BLE001 - cleanup of an observer's own session
        logger.exception("Adoption request: a session of the record keeping could not be cleaned up")


def _write(request_id: str, what: str, where_outcome, values, body) -> None:
    """UPDATE the row of `request_id` whose outcome is `where_outcome` (None: none yet). Never raises."""
    try:
        from app.models.adoption_request import AdoptionRequest

        values = dict(values)
        if body is not None:
            values["body"] = body() if callable(body) else body
        db = _session()
    except Exception:  # noqa: BLE001 - an observer
        logger.exception(f"Adoption request {request_id}: {what} could not be recorded")
        return
    try:
        outcome = AdoptionRequest.outcome
        (db.query(AdoptionRequest)
         .filter(AdoptionRequest.request_id == request_id,
                 outcome.is_(None) if where_outcome is None else outcome == where_outcome)
         .update(values, synchronize_session=False))
        db.commit()
    except Exception:  # noqa: BLE001 - an observer
        logger.exception(f"Adoption request {request_id}: {what} could not be recorded")
        _quietly(db.rollback)
    finally:
        _quietly(db.close)


def end(request_id: str, outcome: str, status: Optional[int], body) -> None:
    """Write the outcome of a row that has none. Never raises: a failure is logged and the row keeps no outcome.

    `body` is the answer's body, or a callable that makes it (its making is guarded too).
    """
    _write(request_id, f"its outcome ({outcome})", None,
           {"outcome": outcome, "status": status, "ended_at": _now()}, body if body is not None else (lambda: None))


def finish_applied(request_id: str, body) -> None:
    """Write the result with the flags known after the commit to an `applied` row. Never raises."""
    _write(request_id, "the result after the commit", "applied", {}, body)


def end_from_recovery(journal: Optional[dict], result: Optional[dict]) -> None:
    """`reverted` on the row of the journal's request_id when recovery undid the adoption. Never raises.

    A recovery that finished the adoption writes nothing (module header);
    its row is checked to say `applied`, and an ERROR logged when it does not.
    """
    try:
        request_id = journal.get("request_id") if isinstance(journal, dict) else None
        outcome = (result or {}).get("outcome")
        if not isinstance(request_id, str):
            return
        if outcome == "reverted":
            end(request_id, "reverted", None, None)
        elif outcome == "completed":
            db = _session()
            try:
                row = _find(db, request_id)
                if row is not None and row.outcome != "applied":
                    logger.error(f"Adoption request {request_id}: recovery finished its adoption, but its row "
                                 f"says {row.outcome!r}, not 'applied'")
            finally:
                _quietly(db.close)
    except Exception:  # noqa: BLE001 - an observer
        logger.exception("Adoption request: recovery's result could not be recorded for the request")


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _settle(db, request_id: str):
    """(row, settled) for the row of `request_id` (module header, Not applied).

    `settled` is None when the row's own outcome stands (or there is no
    row), else `running`, `busy`, `interrupted`, `interrupted_unreadable`,
    or `reverted` when it was written now and could not be read back.
    """
    from app.core import adopt_journal

    row = _find(db, request_id)
    if row is None or row.outcome is not None:
        return row, None
    if not adopt_journal.ADOPTION_LOCK.acquire(blocking=False):
        holder = adopt_journal.lock_holder_request_id
        return row, ("running" if holder == request_id else "busy")
    try:
        db.expire_all()
        row = _find(db, request_id)
        if row.outcome is not None:
            return row, None
        try:
            journal = adopt_journal.read_journal()
        except adopt_journal.JournalUnreadable:
            return row, "interrupted_unreadable"
        if isinstance(journal, dict) and journal.get("request_id") == request_id:
            return row, "interrupted"
        # Under the lock, with no journal of its own: its transaction never committed, or the row would
        # say `applied`.
        end(request_id, "reverted", None, None)
        db.expire_all()
        row = _find(db, request_id)
        return row, (None if row.outcome is not None else "reverted")
    finally:
        adopt_journal.ADOPTION_LOCK.release()


def state_of(db, request_id: str) -> Optional[dict]:
    """The request's state, or None when the id has no row."""
    row, settled = _settle(db, request_id)
    db.rollback()
    if row is None:
        return None
    if row.outcome == "closed":
        return {"state": "closed"}
    body = {"source_id": row.source_id, "target_id": row.target_id, "started_at": _iso(row.started_at),
            "ended_at": _iso(row.ended_at)}
    if settled in ("running", "busy"):
        return {"state": settled, **body}
    if settled in ("interrupted", "interrupted_unreadable"):
        return {"state": "interrupted", **body, "journal_kept": True if settled == "interrupted" else None}
    if settled == "reverted":
        return {"state": "reverted", **body, "ended_at": _iso(_now()), "result": None, "recovered": True}
    state = {"state": row.outcome, **body}
    if row.status is None:
        return {**state, "result": None, "recovered": True}
    if row.outcome == "applied":
        return {**state, "result": row.body}
    if row.outcome == "failed":
        return {**state, "message": (row.body or {}).get("message"), "restored": (row.body or {}).get("restored")}
    return {**state, "status": row.status, "detail": row.body}


def _close(request_id: str) -> bool:
    """Insert and commit the closed row of an id that has none; False when a request recorded it first."""
    from app.models.adoption_request import AdoptionRequest

    db = _session()
    try:
        db.add(AdoptionRequest(request_id=request_id, outcome="closed", ended_at=_now()))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return False
        return True
    finally:
        db.close()


def lookup(db, request_id: str) -> dict:
    """GET /system/adoption/requests/{id}: the request's state, or {"state": "closed"} (module header).

    An id with no row is closed here, by inserting its closed row; when a
    POST inserted first, its row is answered.
    """
    state = state_of(db, request_id)
    if state is not None:
        return state
    if _close(request_id):
        return {"state": "closed"}
    db.expire_all()
    return state_of(db, request_id)


def stored(db, request_id: str, source_id: int, target_id: int) -> Optional[dict]:
    """The recorded answer to repeat ({"status", "body"}), None when the id has no row; 409 otherwise (module header)."""
    from app.core import adopt_journal

    row = _find(db, request_id)
    db.rollback()
    if row is None:
        return None
    if row.outcome == "closed":
        raise HTTPException(status_code=409, detail={"message": ABANDONED_MESSAGE, "codes": ["adoption_abandoned"],
                                                     "details": {}, "request_id": request_id})
    if (row.source_id, row.target_id) != (source_id, target_id):
        raise HTTPException(status_code=409, detail={"message": IN_USE_MESSAGE, "codes": ["client_request_id_in_use"],
                                                     "details": {}})
    row, settled = _settle(db, request_id)
    db.rollback()
    if settled in ("running", "busy"):
        holder = request_id if settled == "running" else adopt_journal.lock_holder_request_id
        raise HTTPException(status_code=409, detail={
            "message": IN_PROGRESS_MESSAGE if settled == "running" else BUSY_MESSAGE,
            "codes": ["adoption_in_progress"], "details": {}, "request_id": holder})
    if settled is not None or row.status is None:
        state = {"interrupted_unreadable": "interrupted"}.get(settled, settled) or row.outcome
        raise HTTPException(status_code=409, detail={
            "message": INTERRUPTED_MESSAGE, "codes": ["adoption_interrupted"], "details": {},
            "request_id": request_id, "state": state})
    return {"status": row.status, "body": row.body}
