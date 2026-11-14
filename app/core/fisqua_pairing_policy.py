"""The rules of pairing by a code (NEH-345) that the owner may change: each in one function.

* mode_gate: which unit may ask for a code.
* start_refusal: everything checked before a code is asked for, in order.
* workspace_gate: which workspace a linked unit may pair with.
* restart_after_link: how the new settings take effect.

The host a unit asks is fisqua_pairing_http.pairing_target. After a link
the unit does not change its mode: nothing here or in the apply step
switches it, and the card offers the existing switch (PUT /system/unit).

Version: 0.3.3
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.core import fisqua_contract as fc

logger = logging.getLogger(__name__)

MODE_NOT_ELIGIBLE = "mode_not_eligible"
BUSY_CAPTURING = "busy_capturing"
OFFSITE_HELD = "offsite_held"
RCLONE_MISSING = "rclone_missing"
SCRIPT_MISSING = "script_missing"
SCRIPT_TOO_OLD = "script_too_old"
OTHER_WORKSPACE = "other_workspace"
RESTART_REQUIRED = "restart_required"
OFFSITE_JOB_RUNNING = "offsite_job_running"


def mode_gate(db: Session) -> Optional[str]:
    """None when the unit may pair: its mode is fisqua, or it is offline with Fisqua not configured (no link yet).

    Whoever last changed the unit's settings does not count: asking for a
    code is an administrator's explicit act in Settings, and that is the
    consent this gate needs. Refused (`mode_not_eligible`): backup mode,
    and offline mode on a unit that already has a link configured.
    """
    from app.core import fisqua_outbox, unit_mode

    row = unit_mode._row(db)
    mode = row.mode if row is not None else unit_mode.inferred()[0]
    if mode == unit_mode.FISQUA:
        return None
    if mode == unit_mode.OFFLINE and not fisqua_outbox.fisqua_configured():
        return None
    return MODE_NOT_ELIGIBLE


def start_refusal(db: Session, *, include_capture: bool = True) -> Optional[str]:
    """The first reason a code may not be asked for now, or None.

    In order: the process's restart latch (`restart_required`: a
    provisioning run may have changed the settings this process holds); the
    mode; a capture in progress, then an offsite free or restore (neither
    interrupted: the session is not begun); the offsite hold; rclone; the provisioning
    script, which must exist and, on a unit that already has a credential,
    take --replace-credential. Each is checked here so that no code is
    collected that the unit could not apply: a collection cannot be undone
    in Fisqua. The session asks again before each new code, without the
    capture check, so that a capture begun while a code is shown does not
    end it.
    """
    from app.core import unit_activity, fisqua_pairing_apply, offsite_hold, restart_latch

    if restart_latch.is_stale():
        return RESTART_REQUIRED
    refusal = mode_gate(db)
    if refusal is not None:
        return refusal
    if include_capture and unit_activity.in_progress(unit_activity.CAMERA):
        return BUSY_CAPTURING
    if include_capture and unit_activity.in_progress(unit_activity.OFFSITE_JOB):
        return OFFSITE_JOB_RUNNING
    if offsite_hold.is_held():
        return OFFSITE_HELD
    if not fisqua_pairing_apply.rclone_available():
        return RCLONE_MISSING
    script = fisqua_pairing_apply.provision_script()
    if not script.is_file():
        return SCRIPT_MISSING
    if fisqua_pairing_apply.has_credential() and not fisqua_pairing_apply.script_supports_replace(script):
        return SCRIPT_TOO_OLD
    return None


def workspace_gate(payload: dict, current_tenant: Optional[str]) -> Optional[str]:
    """None when the payload's tenant may be applied: any tenant on a unit not linked, only its own tenant on a linked one.

    A linked unit pairs again only to receive new keys and a new token for
    the workspace it has. Another tenant is `other_workspace`: nothing is
    written and the existing link stays.
    """
    if current_tenant is None or payload.get(fc.F_TENANT) == current_tenant:
        return None
    return OTHER_WORKSPACE


def restart_after_link() -> str:
    """After a verified link: restart the backend through the helper (`restarting`), or `restart_needed`.

    The restart is reserved first (unit_activity.reserve_restart), under
    the same lock that admits camera work: when a capture is in progress
    the answer is `restart_needed` with nothing run, and once reserved no
    new capture is admitted. The reservation is released when the helper
    has no restart-backend verb, when it fails, and on any exception;
    `restart_needed` in the first two cases. The card then offers the
    unit's restart (POST /system/power).
    """
    from app.core import unit_activity, system_helper

    if not unit_activity.reserve_restart():
        logger.info("Linked; camera work or an offsite job is in progress, so the backend is not restarted now")
        return "restart_needed"
    restarting = False
    try:
        if not system_helper.supports(system_helper.RESTART_BACKEND):
            logger.info("Linked; the system helper has no %s verb, so the unit must be restarted",
                        system_helper.RESTART_BACKEND)
            return "restart_needed"
        if not system_helper.run(system_helper.RESTART_BACKEND):
            return "restart_needed"
        restarting = True
        return "restarting"
    finally:
        if not restarting:
            unit_activity.release_restart()
