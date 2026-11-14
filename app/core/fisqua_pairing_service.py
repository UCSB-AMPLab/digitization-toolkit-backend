"""The process's one pairing registry, wired to the gates and the apply step, and the body of GET /system/pairing.

pairing_registry is a module-level singleton, as the schedulers are
(catalogue_backup_scheduler, image_upload_scheduler), on the same
single-process assumption; app.main stops it at shutdown. The rules are
in app.core.fisqua_pairing_policy, the session in app.core.fisqua_pairing,
the script and the result file in app.core.fisqua_pairing_apply.

**Applying** (apply_link), on the session's thread:

1. Stop is requested of the three schedulers, in the order app.main stops
   them, and the apply waits until each worker thread has exited, up to
   STOP_BOUND_SECONDS (a transfer in flight can be long), so that no upload
   or dump uses the rclone config while the script replaces it. Meanwhile
   the session reads `applying` with reason `finishing_uploads`; the
   payload stays in the session's memory only. New offsite jobs (free,
   restore) are refused from the start (app.core.unit_activity), and
   running ones are then waited for under the same bound, with reason
   `finishing_offsite_jobs`; one still running at the bound is
   `apply_failed`, reason `offsite_jobs_still_running`, nothing written.
2. If a worker is still alive at the bound, the script is not run: the
   answer is `apply_failed` with reason `uploads_still_running`, and the
   link is as it was. The schedulers whose workers exited are started
   again; a live one has been asked to stop, finishes what it is doing,
   and stays stopped until the backend restarts, so the card offers the
   unit's restart for that reason.
3. Otherwise the reason becomes `writing_link` and the script runs. After a
   verified link pairing-result.json is written and the backend restarts
   (restart_after_link); nothing is started in-process, `restart_needed`
   included, since the in-memory settings are the old ones. After a
   refusal that wrote nothing (`bad_payload`, `script_missing`,
   `script_too_old`, and `apply_failed`, which apply_payload answers only
   when the settings and the config and offsite.env digests are unchanged
   after a non-zero exit) the schedulers are started again. After
   `apply_unverified` or `settings_overridden` something may have been
   written, so nothing is started and the card offers the restart.

The invariant: start() is called only on a scheduler whose worker has
exited (wait_stopped answered True), so no path starts one beside a live
worker and none leaves two.

Version: 0.3.3
"""

import logging
import time
from typing import Optional

from sqlalchemy.orm import Session

from app.core import fisqua_contract as fc
from app.core.fisqua_pairing import PairingRegistry

logger = logging.getLogger(__name__)


def _on_own_session(include_capture: bool) -> Optional[str]:
    from app.core import fisqua_pairing_policy
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        return fisqua_pairing_policy.start_refusal(db, include_capture=include_capture)
    finally:
        db.close()


def before_begin() -> Optional[str]:
    return _on_own_session(include_capture=True)


def before_code() -> Optional[str]:
    return _on_own_session(include_capture=False)


def before_apply(payload: dict, session) -> Optional[str]:
    from app.core import fisqua_pairing_policy

    return fisqua_pairing_policy.workspace_gate(payload, session.current_tenant)


def _schedulers() -> list:
    """The three schedulers, in the order app.main stops them."""
    from app.core.catalogue_backup import catalogue_backup_scheduler
    from app.core.fisqua_outbox import fisqua_status_scheduler
    from app.core.image_upload import image_upload_scheduler

    return [fisqua_status_scheduler, image_upload_scheduler, catalogue_backup_scheduler]


STOP_BOUND_SECONDS = 600
STOP_WAIT_STEP_SECONDS = 1.0
# Each wait loop takes at most this many steps of STOP_WAIT_STEP_SECONDS.
_MAX_STEPS = int(STOP_BOUND_SECONDS / STOP_WAIT_STEP_SECONDS) + 1
_clock = time.monotonic
# Refusals after which nothing was written, so the schedulers run again in-process.
NOTHING_WRITTEN = ("bad_payload", "script_missing", "script_too_old", "apply_failed")


def _stop_and_wait(schedulers: list, deadline: float) -> list:
    """Request stop of every scheduler, then wait for their workers until `deadline`. Answers those whose worker exited."""
    for scheduler in schedulers:
        scheduler.request_stop()
    for scheduler in schedulers:
        # Bounded twice: by the deadline, and by a count of steps, so the
        # loop ends even if the clock it reads does not move.
        for _ in range(_MAX_STEPS):
            if scheduler.wait_stopped(min(STOP_WAIT_STEP_SECONDS, max(0.0, deadline - _clock()))):
                break
            if _clock() >= deadline:
                break
    return [scheduler for scheduler in schedulers if not scheduler.worker_alive()]


class _OffsiteWork:
    """Running offsite jobs (free, restore), as app.core.unit_activity counts them."""

    def running(self) -> bool:
        from app.core import unit_activity

        return unit_activity.in_progress(unit_activity.OFFSITE_JOB)

    def wait_idle(self, timeout: float) -> bool:
        from app.core import unit_activity

        return unit_activity.wait_idle(unit_activity.OFFSITE_JOB, timeout)


def _offsite_work():
    return _OffsiteWork()


def _wait_offsite(offsite, deadline: float) -> bool:
    """Wait until no offsite job runs, or until `deadline`. True when none runs."""
    for _ in range(_MAX_STEPS):
        if offsite.wait_idle(min(STOP_WAIT_STEP_SECONDS, max(0.0, deadline - _clock()))):
            return True
        if _clock() >= deadline:
            return False
    return False


def _nothing_written(stopped: list) -> None:
    """The attempt ended with nothing written: offsite jobs may start again, and the stopped schedulers run again."""
    from app.core import unit_activity

    unit_activity.unblock_offsite_jobs()
    _start_again(stopped)


def _start_again(stopped: list) -> None:
    """Start each scheduler again: the only start() pairing makes. Only ever given schedulers whose worker has exited.

    Refused, with nothing started, while the restart latch is set: the
    settings in memory may no longer be the unit's.
    """
    from app.core import restart_latch

    if restart_latch.is_stale():
        logger.error("Pairing did not start the schedulers: this process's settings may be stale, "
                     "and the backend must restart")
        return
    if restart_latch.shutting_down():
        logger.info("Pairing did not start the schedulers: the backend is shutting down")
        return
    for scheduler in reversed(stopped):
        try:
            scheduler.start()
        except Exception:
            logger.exception("A scheduler could not be started again after pairing")


def apply_link(payload: dict, session) -> str:
    """Wait for the schedulers, provision, and restart. Answers the session's final state.

    Offsite jobs are blocked from the start and waited for after the
    schedulers, under the same STOP_BOUND_SECONDS; the card reads
    `finishing_uploads`, then `finishing_offsite_jobs` while one runs, and
    `uploads_still_running` or `offsite_jobs_still_running` on a time-out
    (`apply_failed`, nothing written). The only start() of a scheduler on
    any path here goes through _nothing_written, which also lifts the
    offsite block: at a time-out (only schedulers whose workers exited) and
    under NOTHING_WRITTEN below. apply_payload never raises and answers `apply_failed`
    only on positive evidence that nothing was written, so every other
    outcome, any exception included, leaves the schedulers stopped and the
    unit waiting for a restart.
    """
    from app.core import fisqua_pairing_apply, fisqua_pairing_policy, unit_activity

    # From the collection on, no offsite job starts (lifted only when nothing is written).
    unit_activity.block_offsite_jobs()
    schedulers = _schedulers()
    stopped: list = []
    try:
        session.set_reason("finishing_uploads")
        deadline = _clock() + STOP_BOUND_SECONDS
        stopped = _stop_and_wait(schedulers, deadline)
        if len(stopped) < len(schedulers):
            logger.error("Uploads were still running after %s s; the link was not written", STOP_BOUND_SECONDS)
            session.set_reason("uploads_still_running")
            _nothing_written(stopped)
            return "apply_failed"
        offsite = _offsite_work()
        if offsite.running():
            session.set_reason("finishing_offsite_jobs")
            if not _wait_offsite(offsite, deadline):
                logger.error("An offsite free or restore was still running after %s s; the link was not written",
                             STOP_BOUND_SECONDS)
                session.set_reason("offsite_jobs_still_running")
                _nothing_written(stopped)
                return "apply_failed"
    except Exception as exc:
        # Before the script: nothing can have been written.
        logger.error("Pairing could not prepare the unit for the link (%s); nothing was written",
                     type(exc).__name__)
        _nothing_written([s for s in stopped if not s.worker_alive()])
        return "apply_failed"
    session.set_reason("writing_link")
    try:
        outcome = fisqua_pairing_apply.apply_payload(payload, report=session.set_reason)
    except Exception as exc:
        logger.error("Provisioning raised (%s); the unit must be restarted", type(exc).__name__)
        outcome = "apply_unverified"
    if outcome in NOTHING_WRITTEN:
        _nothing_written(stopped)
        return outcome
    if outcome != "linked":
        return outcome
    _record_result(payload, session)
    try:
        return fisqua_pairing_policy.restart_after_link()
    except Exception as exc:
        logger.error("The restart after the link raised (%s); the unit must be restarted", type(exc).__name__)
        return "restart_needed"


def _record_result(payload: dict, session) -> None:
    """pairing-result.json, best effort: after the restart the card reads the link from the settings, and only `at` and `by` are lost."""
    from app.core import fisqua_pairing_apply

    try:
        fisqua_pairing_apply.write_result(payload[fc.F_TENANT], payload[fc.F_ENVIRONMENT], session.actor)
    except Exception as exc:
        logger.error("pairing-result.json could not be written (%s); the link stands", type(exc).__name__)


pairing_registry = PairingRegistry(apply=apply_link, before_begin=before_begin, before_code=before_code,
                                   before_apply=before_apply)


def as_api(db: Session, registry: Optional[PairingRegistry] = None) -> dict:
    """GET /system/pairing's body. Reading it is the session's heartbeat. Never holds the secret, the token or a key."""
    from app.core import device_id, fisqua_outbox, fisqua_pairing_apply, fisqua_pairing_policy
    from app.core.fisqua_pairing_http import pairing_target

    registry = registry or pairing_registry
    session = registry.current()
    if session is not None:
        session.touch()
        body = session.snapshot()
    else:
        target = pairing_target()
        body = {"state": "idle", "reason": None, "trouble": None, "retryAfterSeconds": None, "code": None,
                "expiresInSeconds": None, "renewalSecondsLeft": None, "environment": target.environment,
                "deviceId": None, "unitName": None, "currentTenant": target.current_tenant, "result": None}
    found = device_id.state()
    body["deviceId"] = found["deviceId"]
    body["deviceIdError"] = found["error"]
    configured = fisqua_outbox.fisqua_configured()
    body["linked"] = {"configured": configured,
                      "tenant": fisqua_outbox._tenant() if configured else None,
                      "environment": fisqua_outbox.fisqua_environment() if configured else None}
    if session is not None and session.running:
        body["eligible"] = {"ok": True, "reason": None}
    else:
        reason = fisqua_pairing_policy.start_refusal(db)
        body["eligible"] = {"ok": reason is None, "reason": reason}
    body["result"] = fisqua_pairing_apply.read_result()
    from app.core import restart_latch

    body["restartRequired"] = restart_latch.is_stale()
    body["lastOutcome"] = session.state if session is not None and not session.running else None
    return body
