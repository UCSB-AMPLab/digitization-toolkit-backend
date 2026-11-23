"""A client's id per capture: the stored answer, and the requests still running.

A capture request may carry `client_capture_id` (app.api.cameras). Ids are
each space's own: the caller's space (`real`, or `testing` for the testing
account) is part of every key, stored on the row and used in the same
query that selects it, so an id used or in flight in the other space
behaves exactly as an unused one.

When the capture is saved, `store` adds its row (app.models.capture_request)
to the capture's own transaction, so a row exists exactly when the capture
was saved. The row carries the request's `fingerprint`: a SHA-256 over every
field of the request but the id, and the kind of capture. A request with an
id is handled by `answered_first`, on both capture routes, before anything
else the handler does:

* the id is in flight in the caller's space (another request holds it):
  409 `capture_in_progress`, at once. Nothing waits: the client asks the
  lookup route until the original has finished;
* no row: the handler captures as it would without the id;
* a row with another fingerprint: 409 `client_capture_id_in_use`;
* a row whose record or any of whose images has been deleted: 409
  `capture_no_longer_stored`. A superseded image still counts as stored;
* otherwise the stored capture, rebuilt from the row and its images, with
  `replayed` true. Nothing is changed and the camera is not reached.

None of the refusals names anything of the stored capture.

The registry of ids in flight is the process's own: the backend runs as one
uvicorn worker process, whose request threads share it. The unique
constraint on (client_capture_id, space) is the backstop beyond it.
"""

from __future__ import annotations

import functools
import hashlib
import json
import threading
from typing import List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

REAL = "real"
TESTING = "testing"

IN_USE_DETAIL = {
    "code": "client_capture_id_in_use",
    "message": "This capture id was already used for another capture. Send a new id for a new capture.",
}
IN_PROGRESS_DETAIL = {
    "code": "capture_in_progress",
    "message": "A capture with this id is still running. Ask for its result instead of sending it again.",
}
NO_LONGER_STORED_DETAIL = {
    "code": "capture_no_longer_stored",
    "message": "An image of the capture saved with this id has been deleted.",
}

# The ids in flight, as (space, id), in this process only. Nothing of a
# claim survives the process: a process killed after the shutter and before
# its commit leaves no stored capture, since its transaction rolled back,
# so after a restart the lookup answers 404 and a new request with the id
# photographs again. That is the right answer, not a lost one: the files
# and manifest line the killed request wrote are left as for any failed
# capture, and no durable reservation is kept for them.
_lock = threading.Lock()
_in_flight: set = set()


def space_of(user) -> str:
    """The caller's space: `testing` for the testing account, `real` for everyone else."""
    from app.core.testing_space import TESTING_ROLE

    return TESTING if user.role == TESTING_ROLE else REAL


def claim(key: Tuple[str, str]) -> bool:
    """Take `key` (space, id) for one request; False when another request holds it."""
    with _lock:
        if key in _in_flight:
            return False
        _in_flight.add(key)
        return True


def release(key: Tuple[str, str]) -> None:
    with _lock:
        _in_flight.discard(key)


def in_flight(key: Tuple[str, str]) -> bool:
    with _lock:
        return key in _in_flight


def fingerprint(kind: str, request) -> str:
    """SHA-256 of the request as validated: every field but the id, by field name, and the kind of capture.

    Defaults are filled in by validation, so a field sent with its default
    value and one left out give the same fingerprint.
    """
    fields = request.model_dump(exclude={"client_capture_id"})
    fields["kind"] = kind
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def find(db: Session, client_capture_id: str, space: str):
    """The row stored for `client_capture_id` in `space`, or None. The space is read from the row only."""
    from app.models.capture_request import StoredCaptureRequest

    return (db.query(StoredCaptureRequest)
            .filter(StoredCaptureRequest.client_capture_id == client_capture_id,
                    StoredCaptureRequest.space == space)
            .first())


def _rebuilt(db: Session, row) -> Optional[dict]:
    """The stored capture's answer, or None when its record or any of its images is gone.

    The row outlives both: its record is set null when the record is
    deleted. An image counts only while it exists and still belongs to the
    stored record: an image id can be given again to another capture's
    image (SQLite reuses the highest rowid of record_images), and that image
    is not this capture's. A superseded image still belongs to its record,
    and so does one freed from the card (its row is kept), and their paths
    are answered as stored.
    """
    from app.models.record import Record, RecordImage

    if row.record_id is None or db.query(Record.id).filter(Record.id == row.record_id).first() is None:
        return None
    ids: List[int] = list(row.image_ids or [])
    by_id = {img.id: img for img in db.query(RecordImage).filter(
        RecordImage.id.in_(ids), RecordImage.record_id == row.record_id).all()} if ids else {}
    if not ids or any(i not in by_id for i in ids):
        return None
    paths = [by_id[i].file_path for i in ids]
    body = {"success": True, "record_id": row.record_id, "image_ids": ids, "replayed": True}
    if row.kind == "single":
        body["file_path"] = paths[0]
    else:
        body["file_paths"] = paths
    return body


def answer(db: Session, client_capture_id: str, space: str, request_fingerprint: str) -> Optional[dict]:
    """The stored capture's answer for this request; None when nothing is stored; 409 when the row is not its."""
    row = find(db, client_capture_id, space)
    if row is None:
        return None
    if row.request_fingerprint != request_fingerprint:
        raise HTTPException(status_code=409, detail=IN_USE_DETAIL)
    body = _rebuilt(db, row)
    if body is None:
        raise HTTPException(status_code=409, detail=NO_LONGER_STORED_DETAIL)
    return body


def lookup(db: Session, client_capture_id: str, space: str) -> Optional[dict]:
    """In the caller's space: the stored capture's answer with `in_progress` false, `{"in_progress": true}`
    while a request with the id is running, or None.

    The in-flight state is read before the table. A request commits before
    it releases its claim (answered_first), so an id not in flight at the
    first read has either committed its row, which the select then finds,
    or saved nothing. An id in flight at the first read whose row the
    select does not see answers in_progress, and the client asks again.
    Read the other way round, an original that commits and releases
    between the select and the in-flight read would answer 404 for a
    capture that was saved.
    """
    was_in_flight = in_flight((space, client_capture_id))
    row = find(db, client_capture_id, space)
    if row is not None:
        body = _rebuilt(db, row)
        return None if body is None else {**body, "in_progress": False}
    if was_in_flight:
        return {"in_progress": True}
    return None


def store(db: Session, request, kind: str, user, record_id: int, image_ids: List[int]) -> None:
    """Add the saved capture's row to the session, to be committed with the capture; nothing without an id."""
    if request.client_capture_id is None:
        return
    from app.models.capture_request import StoredCaptureRequest

    db.add(StoredCaptureRequest(
        client_capture_id=request.client_capture_id, space=space_of(user), kind=kind,
        collection_id=request.collection_id, record_id=record_id, image_ids=list(image_ids),
        request_fingerprint=fingerprint(kind, request)))


def answer_again(db: Session, request, kind: str, user) -> Optional[dict]:
    """After a commit refused for the id: the answer from the row that holds it, or None when there is none."""
    if request.client_capture_id is None:
        return None
    space = space_of(user)
    if find(db, request.client_capture_id, space) is None:
        return None
    return answer(db, request.client_capture_id, space, fingerprint(kind, request))


def answered_first(kind: str):
    """Decorate a capture route so a request with a client_capture_id is claimed and answered first.

    The route is called with keyword arguments `request` (its body, with
    `client_capture_id`), `current_user` and `db`, as FastAPI calls it. The
    claim is taken before the route's own refusals and the camera, from the
    caller's role alone, and released however the call ends. The route keeps
    its own signature (functools.wraps), so FastAPI reads its parameters and
    dependencies from it.
    """
    def decorate(route):
        @functools.wraps(route)
        def held(**kwargs):
            request = kwargs["request"]
            if request.client_capture_id is None:
                return route(**kwargs)
            space = space_of(kwargs["current_user"])
            key = (space, request.client_capture_id)
            if not claim(key):
                raise HTTPException(status_code=409, detail=IN_PROGRESS_DETAIL)
            try:
                # Read after the claim: a request that released the id has
                # committed its row first, so the row is seen here.
                stored = answer(kwargs["db"], request.client_capture_id, space, fingerprint(kind, request))
                if stored is not None:
                    return stored
                return route(**kwargs)
            except BaseException:
                # A refusal the route raises after it has written (a number
                # taken, then a 507) leaves its transaction open; it is
                # rolled back here, before the release.
                kwargs["db"].rollback()
                raise
            finally:
                # Released only after the route's transaction has ended: its
                # commit, the capture's own rollback (its except, and the
                # IntegrityError path, roll back before returning), or the
                # rollback above for anything raised. lookup relies on this
                # order: a row is committed before its id is released.
                release(key)
        return held
    return decorate
