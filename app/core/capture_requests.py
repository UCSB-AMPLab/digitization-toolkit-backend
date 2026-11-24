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
* the id was answered as not found by the lookup in the caller's space
  (closed): 409 `capture_abandoned`, at once, before anything else;
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
from collections import OrderedDict
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
ABANDONED_DETAIL = {
    "code": "capture_abandoned",
    "message": "This capture was already reported as not saved. Send a new id to capture again.",
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
#
# Closed ids and pending marks: two different things, never confused.
#
# An answered close is final. `_closed[space]` holds the keys for which a
# lookup HAS ANSWERED 404, and the screen then told the operator that
# nothing was saved. A POST with that id may still be on its way (queued
# for a thread, waiting on a dependency or a connection), and it is refused
# at the claim (409 `capture_abandoned`): an id answered as not found never
# executes afterwards. Nothing removes a key from `_closed` but the size
# bound, and nothing needs to: a key is closed only when it was neither in
# flight nor stored, and every POST with it after the close is refused at
# the claim, so no row for it can ever appear and no stored capture can
# have a closed key.
#
# A pending mark belongs to one lookup. No database I/O is done under the
# lock. When a lookup's first select finds no row:
#
#   a. holding the lock: a key in flight answers in_progress; otherwise the
#      lookup adds its own token to `_pending[key]`, and lets go;
#   b. without the lock, it reads the table again;
#   c. holding the lock, it removes its own token, whatever happened at b
#      (a `finally`), and then: no row closes the key and answers 404; a row
#      answers the stored capture (or 410), closing nothing; a select that
#      raised closes nothing and the error goes on (the screen treats a
#      failed ask as a failed ask, never as "not saved").
#
# A lookup removes only its own token and never touches `_closed` except to
# add to it, so one lookup's failure or success cannot undo what another
# answered. The claim is refused while the key is closed OR has any pending
# mark, so no POST can slip between a lookup's decision and its answer. A
# lookup whose first select finds a row does no registry work.
#
# Each POST with the id, against a lookup's step a (ordered by the lock):
#
# * in flight at step a (it claimed before and has not released): the
#   lookup answers in_progress, adds no mark and closes nothing;
# * committed and released before step a: a request commits before it
#   releases its key (answered_first), so its row is found at step b; the
#   lookup answers it and closes nothing;
# * claims while a mark is pending: refused (409 `capture_abandoned`), no
#   camera, nothing stored. The lookup then ends 404, and the key is closed;
#   or it ends with the stored row, which means the capture was committed
#   before step a and the refused POST was a second send of a stored id,
#   which no client makes (the screen never sends an id again, it asks the
#   lookup); its sender sees 409 `capture_abandoned` for a capture that is
#   stored, the one imprecise answer;
# * claims after an answered close: refused, and finally.
#
# Two lookups at once, in any order: each adds and removes only its own
# token, and the claim is refused while either holds one. A row can be
# committed only by a POST that claimed, and no POST can claim between the
# first of their steps a and the last of their steps c, so both second
# selects see the same thing: a row committed before the first step a
# (both answer it, nothing closed) or none (both answer 404, the key
# closed by each, which is idempotent). One finding a row and the other
# not cannot happen. In the schedule where A has a mark and its select is
# slow, B misses twice and answers 404 (closing the key), and A's select
# then raises: A removes its own token and adds nothing, B's close stays,
# and the late POST is refused.
#
# Pending marks are bounded by the lookups in progress, and every exit
# from a lookup's steps b and c removes its own (`try/finally`).
#
# The closed keys are kept per space, the most recent CLOSED_LIMIT of each,
# the oldest dropped first, so nothing done in one space evicts the other's.
# A key is a space and a 36-character id: about 240 bytes held with its
# entry (measured on CPython 3.12), so a full set is about 24 MB per space,
# 48 MB for both. An evicted key could be claimed again;
# that needs an account of the same space to make CLOSED_LIMIT lookups of
# unknown ids while a POST whose id a lookup answered as not found is still
# on its way, within the minutes a request can wait. That is accepted: it
# takes an operator account of that unit acting against its own captures,
# and the bound is the only limit on what a GET that closes ids can write.
# A restart empties the sets, as it ends every request that was on its way.
CLOSED_LIMIT = 100_000
CLAIMED, IN_FLIGHT, CLOSED = "claimed", "in_flight", "closed"
_lock = threading.Lock()
_in_flight: set = set()
_closed: "dict[str, OrderedDict[Tuple[str, str], None]]" = {REAL: OrderedDict(), TESTING: OrderedDict()}
_pending: "dict[Tuple[str, str], set]" = {}


def space_of(user) -> str:
    """The caller's space: `testing` for the testing account, `real` for everyone else."""
    from app.core.testing_space import TESTING_ROLE

    return TESTING if user.role == TESTING_ROLE else REAL


def claim(key: Tuple[str, str]) -> str:
    """Take `key` (space, id) for one request: CLAIMED; IN_FLIGHT when another request holds it;
    CLOSED when a lookup answered it as not found or a lookup is deciding it (a pending mark)."""
    with _lock:
        if key in _closed[key[0]] or key in _pending:
            return CLOSED
        if key in _in_flight:
            return IN_FLIGHT
        _in_flight.add(key)
        return CLAIMED


def _close_locked(key: Tuple[str, str]) -> None:
    """Close `key`, the newest of its space's closed keys; the caller holds _lock."""
    closed_here = _closed[key[0]]
    closed_here[key] = None
    closed_here.move_to_end(key)
    while len(closed_here) > CLOSED_LIMIT:
        closed_here.popitem(last=False)


def _reset_for_tests() -> None:
    """No id in flight, none closed and no mark pending: the registry is process-wide, as a fresh
    process has it."""
    with _lock:
        _in_flight.clear()
        _pending.clear()
        for closed_here in _closed.values():
            closed_here.clear()


def close(key: Tuple[str, str]) -> None:
    with _lock:
        _close_locked(key)


def closed(key: Tuple[str, str]) -> bool:
    with _lock:
        return key in _closed[key[0]]


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
    while a request with the id is running, or None (404) when no row holds the id, after which the id
    is closed.

    A row found by the first select is answered, with no registry work: a
    row, once committed, stays. Otherwise steps a to c of the comment at the
    registry. A row whose record or an image is gone raises 410
    `capture_no_longer_stored` (the code and message the POST answers with
    409) and closes nothing: a capture was saved, and the screen must not
    read it as one that was not. The other space never sees the row: it
    answers 404 there.
    """
    key = (space, client_capture_id)
    row = find(db, client_capture_id, space)
    if row is None:
        token = object()
        with _lock:
            if key in _in_flight:
                return {"in_progress": True}
            _pending.setdefault(key, set()).add(token)
        read = False
        try:
            row = find(db, client_capture_id, space)
            read = True
        finally:
            with _lock:
                marks = _pending[key]
                marks.discard(token)
                if not marks:
                    del _pending[key]
                if read and row is None:
                    _close_locked(key)
        if row is None:
            return None
    body = _rebuilt(db, row)
    if body is None:
        raise HTTPException(status_code=410, detail=NO_LONGER_STORED_DETAIL)
    return {**body, "in_progress": False}


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
            state = claim(key)
            if state == CLOSED:
                raise HTTPException(status_code=409, detail=ABANDONED_DETAIL)
            if state == IN_FLIGHT:
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
