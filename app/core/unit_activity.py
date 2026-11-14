"""Work in progress on the unit that a restart, or a change of its storage credential, must not cut off.

Two kinds are counted, under one lock:

* CAMERA: every route in app/api/cameras.py that takes a picture or holds
  a camera for more than a moment holds `tracked` as a dependency, so the
  count is raised while each request runs and lowered when it ends,
  however it ends: POST /capture, /capture/dual, /test-capture/{i} (the
  test picture and the focus check), /focus/{i}, /focus-lock/{i},
  /settings/{i} (zoom moves), /calibrate, /calibrate/white-balance and
  /rescan, and PUT /dslr/{i}/settings. The live preview (GET /preview/{i})
  is not counted: the card polls it every few hundred milliseconds for as
  long as the preview is open, so counting it would keep pairing refused
  while anyone watches it, and a restart in the middle of one frame loses
  only that frame. Quick reads are not counted either.
* OFFSITE_JOB: a volume's free or restore (app.core.offsite_free), counted
  from the moment its job is reserved until its thread ends. Restore runs
  rclone with the unit's credential; free deletes local files after
  checking the remote copies, and is never interrupted.

**One mechanism.** Work in progress prevents a restart, and a pending
restart prevents new work:

* reserve_restart() succeeds only when nothing of either kind is counted,
  and marks a restart pending; while it is pending every new camera
  request and offsite job is refused. The caller releases it
  (release_restart) when the restart does not happen; it also lapses after
  RESTART_RESERVATION_SECONDS, so a restart that never comes cannot refuse
  work for good.
* block_offsite_jobs() is set by pairing from the moment a payload is
  collected (app.core.fisqua_pairing_service.apply_link), so no offsite job
  starts while the credential may be replaced; it is lifted only when the
  attempt ends with nothing written. After a link it holds until the
  process restarts.

A refusal answers 503 with detail code `backend_restarting` and a
Retry-After, on the camera routes and the offsite job routes alike.

One backend process, as for the schedulers: nothing here is shared
between processes.

Version: 0.3.3
"""

import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

CAMERA = "camera"
OFFSITE_JOB = "offsite_job"
KINDS = (CAMERA, OFFSITE_JOB)

_lock = threading.Condition()
_counts = {kind: 0 for kind in KINDS}
_restart_until: Optional[float] = None
_offsite_blocked = False
_clock = time.monotonic
RESTART_RESERVATION_SECONDS = 120
RETRY_AFTER_SECONDS = 60
BACKEND_RESTARTING = "backend_restarting"
REFUSAL_MESSAGE = "Captua is restarting; try again in a minute."


class Refused(Exception):
    """New work is not admitted now: a restart is pending, or offsite jobs are blocked."""

    code = BACKEND_RESTARTING


def _pending_locked() -> bool:
    return _restart_until is not None and _clock() < _restart_until


def enter(kind: str, *, refuse: bool = True) -> None:
    """Count one piece of work. Refused (Refused) while a restart is pending, and for an offsite job while those are blocked."""
    with _lock:
        if refuse and (_pending_locked() or (kind == OFFSITE_JOB and _offsite_blocked)):
            raise Refused()
        _counts[kind] += 1


def leave(kind: str) -> None:
    with _lock:
        _counts[kind] -= 1
        _lock.notify_all()


@contextmanager
def tracking(kind: str = CAMERA) -> Iterator[None]:
    """Count work for the duration of the block, without refusal."""
    enter(kind, refuse=False)
    try:
        yield
    finally:
        leave(kind)


def tracked() -> Iterator[None]:
    """The camera routes' dependency: refused with 503 while a restart is pending, else counted for the request."""
    from fastapi import HTTPException

    try:
        enter(CAMERA)
    except Refused:
        raise HTTPException(status_code=503, detail={"code": BACKEND_RESTARTING, "message": REFUSAL_MESSAGE},
                            headers={"Retry-After": str(RETRY_AFTER_SECONDS)}) from None
    try:
        yield
    finally:
        leave(CAMERA)


def in_progress(kind: Optional[str] = None) -> bool:
    with _lock:
        return any(_counts[k] > 0 for k in ((kind,) if kind else KINDS))


def wait_idle(kind: str, timeout: float) -> bool:
    """Wait at most `timeout` seconds for no work of `kind`. True when none is counted."""
    with _lock:
        _lock.wait_for(lambda: _counts[kind] == 0, timeout=max(0.0, timeout))
        return _counts[kind] == 0


def reserve_restart() -> bool:
    """Mark a restart pending if no work is counted and none is pending. False otherwise, with nothing changed."""
    global _restart_until
    with _lock:
        if any(_counts.values()) or _pending_locked():
            return False
        _restart_until = _clock() + RESTART_RESERVATION_SECONDS
        return True


def release_restart() -> None:
    global _restart_until
    with _lock:
        _restart_until = None


def restart_pending() -> bool:
    with _lock:
        return _pending_locked()


def block_offsite_jobs() -> None:
    global _offsite_blocked
    with _lock:
        _offsite_blocked = True


def unblock_offsite_jobs() -> None:
    global _offsite_blocked
    with _lock:
        _offsite_blocked = False


def offsite_jobs_blocked() -> bool:
    with _lock:
        return _offsite_blocked


def _reset_for_tests() -> None:
    global _restart_until, _offsite_blocked
    with _lock:
        _restart_until = None
        _offsite_blocked = False
        for kind in KINDS:
            _counts[kind] = 0
        _lock.notify_all()
