"""The unit's mode and its projects setting (NEH-311, NEH-313): the one module that reads and writes unit_settings.

A unit runs in one mode, set by an admin (PUT /system/unit):

* `offline`: nothing leaves the scanner. No image upload, no catalogue
  dump, no call to Fisqua.
* `backup`: the images and the catalogue are copied to the bucket; no
  call to Fisqua.
* `fisqua`: all of that, and the link to Fisqua: handovers, status,
  decisions, the digitisation queue and the operators PUT. Volumes come
  from Fisqua's queue: operators may not create a volume, change its stem
  or reference code, or import a list (VOLUMES_FROM_FISQUA); an admin may,
  each logged as an override, and a code an admin gives is checked with
  Fisqua first (check_reference_code, contract 1.8).

The existing gates read the mode, so every caller follows it:
image_upload.run_image_upload and catalogue_backup.run_catalogue_backup
answer OFF_STATUS outside backup and fisqua, and fisqua_outbox's send and
poll and fisqua_queue's sync and operators PUT outside fisqua. Each run
reads the mode again between objects, sends, batches and decisions reads,
so a switch stops a run in progress before its next step. Freeing,
restore and mark-freed are refused in offline mode
(app.core.offsite_free). Changing
mode changes no data: outbox rows, statuses and records stay as they are,
and unsent rows go out if the unit returns to fisqua. The offsite hold
file keeps its meaning in every mode.

**Starting mode.** The backend writes the row at start-up (initialise):
`fisqua` when Fisqua is configured, else `backup` when offsite upload is,
else `offline`, with projects off; logged `unit_mode_initialised` with
the mode and why. Until a row exists (a unit not yet restarted on this
release, or a test), current() answers that inferred mode without
writing it. Anything that needs the row (a change, the default project)
writes it, with the same inference and the same log line.

**Switching.** `backup` needs offsite upload and the catalogue dump
configured, its age recipient included; `fisqua` needs those and Fisqua configured, and then an
operators PUT carrying `mode: fisqua` that Fisqua accepts, sent during the
switch. A missing prerequisite or a failed PUT refuses the switch
(ModeRefused, a 409 naming each problem) and nothing changes; so does
the offsite hold (`offsite_held`), and a change another admin committed
while this one was checked (`mode_changed`, never overwritten, so a lost
race cannot store fisqua without a PUT that carried it). Switching
down from fisqua sends the PUT once with the new mode, best effort: a
failure is logged and does not stop the switch. offline and backup have
no link, so a change between them sends nothing.

**Projects.** Shown only in `offline` mode with projects_enabled on; the
setting is stored in any mode. While they are hidden every new volume goes
into the unit's default project (default_project): the stored one, else
the only project when there is exactly one, else a project named
`default`, created if there is none; the choice is stored and logged
`default_project_set`. Nothing moves an existing volume.

Version: 0.3.3
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import fisqua_contract as fc
from app.core import offsite_hold

logger = logging.getLogger(__name__)

OFFLINE, BACKUP, FISQUA = "offline", "backup", "fisqua"
MODES = (OFFLINE, BACKUP, FISQUA)
# The status a gated run answers when the mode turns it off.
OFF_STATUS = "off_in_this_mode"
DEFAULT_PROJECT_NAME = "default"
ROW_ID = 1

# Problem codes.
OFFSITE_NOT_CONFIGURED = "offsite_not_configured"
CATALOGUE_DUMP_NOT_CONFIGURED = "catalogue_dump_not_configured"
FISQUA_NOT_CONFIGURED = "fisqua_not_configured"
FISQUA_UNREACHABLE = "fisqua_unreachable"
FISQUA_NO_LOOKUP = "fisqua_no_lookup"
UNKNOWN_REFERENCE_CODE = "unknown_reference_code"
VOLUMES_FROM_FISQUA = "volumes_from_fisqua"
STEM_REQUIRED = "stem_required"
OFFSITE_HELD = "offsite_held"
MODE_CHANGED = "mode_changed"

PREREQUISITE_MESSAGES = {
    OFFSITE_NOT_CONFIGURED: "Offsite upload is not configured on this unit (remote and tenant).",
    CATALOGUE_DUMP_NOT_CONFIGURED: "The offsite catalogue dump is not configured on this unit.",
    FISQUA_NOT_CONFIGURED: "The link to Fisqua is not configured on this unit (environment, token and tenant).",
    FISQUA_UNREACHABLE: "Fisqua did not take the unit's new mode: the operators call failed.",
    OFFSITE_HELD: "The offsite hold is in place, so nothing can be sent to Fisqua until it is removed.",
    MODE_CHANGED: "The unit's settings were changed by someone else meanwhile; read them again and retry.",
}
STEM_REQUIRED_MESSAGE = "In Fisqua mode a volume's file name stem is its Fisqua reference code, so it cannot be empty."

VOLUMES_FROM_FISQUA_MESSAGE = (
    "In Fisqua mode, volumes come from Fisqua's queue. Only an admin can create a volume, change its "
    "file name stem or reference code, or import a list."
)
_LIST_INSTEAD = "A list file exported from Fisqua can be imported instead."
UNKNOWN_REFERENCE_CODE_MESSAGE = "Fisqua has no description with the reference code {code} in this workspace."
FISQUA_NO_LOOKUP_MESSAGE = "This Fisqua cannot check reference codes (contract 1.8). " + _LIST_INSTEAD
FISQUA_UNREACHABLE_MESSAGE = "Fisqua could not be reached to check the reference code {code}. " + _LIST_INSTEAD


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UnitState:
    mode: str
    projects_enabled: bool
    default_project_id: Optional[int]

    @property
    def projects_shown(self) -> bool:
        return self.mode == OFFLINE and self.projects_enabled


def inferred() -> tuple[str, str]:
    """The mode the unit's configuration implies, and why."""
    from app.core import fisqua_outbox, image_upload

    if fisqua_outbox.fisqua_configured():
        return FISQUA, "Fisqua is configured (environment, token and offsite tenant)"
    if image_upload.is_enabled():
        return BACKUP, "offsite upload is configured (remote and tenant) and Fisqua is not"
    return OFFLINE, "neither offsite upload nor Fisqua is configured"


def _row(db: Session, lock: bool = False):
    from app.models.unit_settings import UnitSettings

    query = db.query(UnitSettings).filter(UnitSettings.id == ROW_ID)
    if lock:
        query = query.with_for_update()
    return query.first()


def current(db: Session) -> UnitState:
    """The stored settings, or, with no row yet, the inferred mode with projects off (nothing written)."""
    row = _row(db)
    if row is None:
        return UnitState(inferred()[0], False, None)
    return UnitState(row.mode, bool(row.projects_enabled), row.default_project_id)


def mode(db: Session) -> str:
    return current(db).mode


def offsite_on(db: Session) -> bool:
    """Whether images and the catalogue go to the bucket (backup and fisqua)."""
    return mode(db) in (BACKUP, FISQUA)


def link_on(db: Session) -> bool:
    """Whether the unit calls Fisqua (fisqua mode only)."""
    return mode(db) == FISQUA


def off(db: Session) -> dict:
    """What a gated run answers when the mode turns it off."""
    return {"status": OFF_STATUS, "mode": mode(db)}


def _recipient_valid() -> bool:
    """Whether the catalogue dump's age recipient is set and valid (catalogue_backup.dump_recipient; no side effect)."""
    from app.core import catalogue_backup

    try:
        catalogue_backup.dump_recipient()
    except catalogue_backup.BackupError:
        return False
    return True


def prerequisites() -> dict:
    """The missing prerequisites of `backup` and `fisqua`, as codes, from the configuration alone (no call)."""
    from app.core import catalogue_backup, fisqua_outbox, image_upload

    backup = []
    if not image_upload.is_enabled():
        backup.append(OFFSITE_NOT_CONFIGURED)
    if not catalogue_backup.is_configured() or not _recipient_valid():
        backup.append(CATALOGUE_DUMP_NOT_CONFIGURED)
    fisqua = list(backup)
    if not fisqua_outbox.fisqua_configured():
        fisqua.append(FISQUA_NOT_CONFIGURED)
    return {BACKUP: backup, FISQUA: fisqua}


def as_api(db: Session) -> dict:
    """The GET /system/unit body."""
    state = current(db)
    return {
        "mode": state.mode,
        "projectsEnabled": state.projects_enabled,
        "projectsShown": state.projects_shown,
        "defaultProjectId": state.default_project_id,
        "prerequisites": prerequisites(),
    }


# ---------------------------------------------------------------------------
# Writing the row
# ---------------------------------------------------------------------------

def _log_in_session(db: Session, action: str, detail: str, actor: Optional[str] = None,
                    level: str = "INFO", subject: Optional[str] = None) -> None:
    """A system_logs row added to `db`, committed with the caller's change (audit.log_event commits at once)."""
    from app.models.system_log import SystemLog

    db.add(SystemLog(level=level, category="system", action=action,
                     actor=actor[:150] if actor else None,
                     subject=subject[:300] if subject else None,
                     detail=detail[:500] if detail else None))


def _new_row(db: Session):
    from app.models.unit_settings import UnitSettings

    initial, why = inferred()
    row = UnitSettings(id=ROW_ID, mode=initial, projects_enabled=False, updated_at=_now())
    db.add(row)
    _log_in_session(db, "unit_mode_initialised", f"{initial}: {why}")
    logger.info("Unit mode initialised to %s: %s", initial, why)
    return row


def initialise() -> Optional[str]:
    """Write the row at start-up when there is none. Returns the mode written, or None when one existed.

    On its own session, committed. Two writers cannot both insert row 1:
    the loser's IntegrityError is taken as "already written".
    """
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        if _row(db) is not None:
            return None
        row = _new_row(db)
        written = row.mode
        db.commit()
        return written
    except IntegrityError:
        db.rollback()
        return None
    finally:
        db.close()


def _ensure_row(db: Session):
    """The row under FOR UPDATE, inserted (and logged) when missing. Not committed.

    For callers holding no uncommitted change of their own: a concurrent
    insert of row 1 is recovered by a rollback and a second read.
    """
    row = _row(db, lock=True)
    if row is not None:
        return row
    _new_row(db)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        row = _row(db, lock=True)
        if row is None:
            raise
        return row
    return _row(db, lock=True)


class ModeRefused(Exception):
    """A switch refused; `problems` is a list of {code, message[, error]}."""

    def __init__(self, target: str, problems: list):
        super().__init__(target)
        self.target = target
        self.problems = problems

    def detail(self) -> dict:
        return {"message": f"The unit cannot switch to {self.target} mode.", "problems": self.problems}


def _problem(code: str, error: Optional[str] = None) -> dict:
    problem = {"code": code, "message": PREREQUISITE_MESSAGES[code]}
    if error is not None:
        problem["error"] = error
    return problem


def change(db: Session, *, actor: str, mode: Optional[str] = None,
           projects_enabled: Optional[bool] = None, transport=None) -> UnitState:
    """Apply an admin's change; commits, and logs unit_mode_changed / unit_projects_changed.

    Raises ModeRefused, with nothing changed, when the new mode's
    prerequisites are missing or, for fisqua, the operators PUT carrying it
    fails. The caller holds no uncommitted change.

    When Fisqua was told a mode that is then not stored (the change lost a
    race, `mode_changed`, or its commit failed), the mode actually stored is
    sent at once, best effort, and the outcome logged `unit_mode_resent`.
    """
    from app.core import audit, fisqua_queue

    if mode is not None and mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    before = current(db)
    target = before.mode if mode is None else mode
    unsent = None
    told = None  # the mode Fisqua accepted in this change, if any
    if target != before.mode:
        if target in (BACKUP, FISQUA):
            missing = prerequisites()[target]
            if missing:
                raise ModeRefused(target, [_problem(code) for code in missing])
        if target == FISQUA:
            if offsite_hold.held("switch to fisqua mode"):
                raise ModeRefused(target, [_problem(OFFSITE_HELD)])
            ok, error = fisqua_queue.send_operators_with_mode(db, FISQUA, transport=transport)
            if not ok:
                raise ModeRefused(target, [_problem(FISQUA_UNREACHABLE, error)])
            told = FISQUA
        elif before.mode == FISQUA:
            # Before the link stops; best effort.
            ok, error = fisqua_queue.send_operators_with_mode(db, target, transport=transport)
            if ok:
                told = target
            else:
                unsent = error
                logger.warning("Fisqua was not told the unit's new mode %s (%s); the switch goes ahead",
                               target, error)
    try:
        row = _ensure_row(db)
        old_mode, old_projects = row.mode, bool(row.projects_enabled)
        if (old_mode, old_projects) != (before.mode, before.projects_enabled):
            # Committed by another change since `before` was read: the checks
            # and the PUT above were for a state that no longer holds.
            db.rollback()
            _resend_stored(db, told, actor, transport)
            raise ModeRefused(target, [_problem(MODE_CHANGED)])
        row.mode = target
        if projects_enabled is not None:
            row.projects_enabled = bool(projects_enabled)
        row.updated_at = _now()
        row.updated_by = actor
        new_projects = bool(row.projects_enabled)
        db.commit()
    except ModeRefused:
        raise
    except Exception:
        db.rollback()
        _resend_stored(db, told, actor, transport)
        raise
    if old_mode != target:
        detail = f"{old_mode} -> {target}"
        if unsent is not None:
            detail += f"; Fisqua not told: {unsent}"
        audit.log_event(db, level="WARN" if unsent else "INFO", category="system",
                        action="unit_mode_changed", actor=actor, detail=detail)
    if old_projects != new_projects:
        audit.log_event(db, level="INFO", category="system", action="unit_projects_changed", actor=actor,
                        detail=f"projects_enabled {old_projects} -> {new_projects}")
    return current(db)


def _resend_stored(db: Session, told: Optional[str], actor: Optional[str], transport=None) -> None:
    """After a change that told Fisqua `told` and then stored nothing: send the stored mode. Never raises."""
    from app.core import audit, fisqua_queue

    if told is None:
        return
    try:
        stored = current(db).mode
        db.rollback()
    except Exception:
        db.rollback()
        logger.exception("Reading the unit's mode after a failed change raised; Fisqua may show %s", told)
        return
    if stored == told:
        return
    ok, error = fisqua_queue.send_operators_with_mode(db, stored, transport=transport)
    detail = f"Fisqua was told {told}, which was not stored; sent {stored}"
    if ok:
        logger.info("%s", detail)
    else:
        detail = f"Fisqua was told {told}, which was not stored; {stored} not sent: {error}"
        logger.warning("%s", detail)
    audit.log_event(db, level="INFO" if ok else "WARN", category="system", action="unit_mode_resent",
                    actor=actor, detail=detail)


# ---------------------------------------------------------------------------
# The default project
# ---------------------------------------------------------------------------

def stored_default_project(db: Session):
    """The stored default project, or None when none is stored or it no longer exists. Reads only."""
    from app.models.project import Project

    project_id = current(db).default_project_id
    return db.get(Project, project_id) if project_id is not None else None


def default_project(db: Session, actor: Optional[str]):
    """The project new volumes go into while projects are hidden: (project, created now). Commits.

    The stored one when it still exists; else the only project when there is
    exactly one; else the project an import of a list named `default` would
    use, created when there is none (list_import._project_for, under the
    shared project lock, with no admin needed). The choice is stored and
    logged default_project_set. Raises what _project_for raises:
    ProjectLockUnavailable, or ListRefused when `default` would be a
    volume's stem directory. The caller holds no uncommitted change.
    """
    from app.core import list_import
    from app.models.project import Project

    row = _ensure_row(db)
    if row.default_project_id is not None:
        project = db.get(Project, row.default_project_id)
        if project is not None:
            db.commit()
            return project, False
    db.commit()

    projects = db.query(Project).order_by(Project.id).limit(2).all()
    created = False
    if len(projects) == 1:
        project, how = projects[0], "the unit's only project"
    else:
        project, created = list_import._project_for(db, DEFAULT_PROJECT_NAME, actor, True)
        how = "created" if created else f"the existing project named {DEFAULT_PROJECT_NAME}"
    project_id, project_name = project.id, project.name

    row = _ensure_row(db)
    if row.default_project_id is not None and row.default_project_id != project_id:
        chosen = db.get(Project, row.default_project_id)
        if chosen is not None:
            # Another request stored one meanwhile; that one stands.
            db.commit()
            return chosen, False
    row.default_project_id = project_id
    row.updated_at = _now()
    _log_in_session(db, "default_project_set", f"project {project_id} ({how})", actor=actor,
                    subject=project_name)
    db.commit()
    return db.get(Project, project_id), created


# ---------------------------------------------------------------------------
# The reference code lookup (contract 1.8)
# ---------------------------------------------------------------------------

def get_transport():
    """The link's transport (a seam for tests)."""
    from app.core import fisqua_outbox

    return fisqua_outbox.get_transport()


class CodeRefused(Exception):
    """A reference code an admin gave in fisqua mode that cannot be saved: an HTTP status and a detail."""

    def __init__(self, status: int, code: str, message: str, reference_code: str, error: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.reference_code = reference_code
        self.error = error

    def detail(self) -> dict:
        detail = {"code": self.code, "message": self.message, "referenceCode": self.reference_code}
        if self.error is not None:
            detail["error"] = self.error
        return detail


def check_reference_code(db: Session, reference_code: str, *, transport=None,
                         now: Optional[datetime] = None) -> Any:
    """Ask Fisqua whether its workspace holds `reference_code`; return the description, or raise CodeRefused.

    One GET /api/capture/v1/descriptions/{code} through the link's call
    budget, configuration stop, hold and redaction. 200: the description.
    404 with rule unknown-reference-code: 422 unknown_reference_code. A
    200 whose referenceCode is not exactly `reference_code`: 422
    unknown_reference_code; a 200 without a string referenceCode: 409
    fisqua_unreachable. A
    bare 404 or a 405: 409 fisqua_no_lookup (a Fisqua without 1.8). A
    401, 403 or redirect is recorded as the link's configuration stop, as
    on every other path, and with a network error, a timeout, a 429 or
    any other answer is 409 fisqua_unreachable; a Retry-After holds the
    link back as elsewhere.
    """
    from app.core import fisqua_outbox
    from app.core.fisqua_outbox import NullTransport, TransportResult

    def unreachable(error: str) -> CodeRefused:
        return CodeRefused(409, FISQUA_UNREACHABLE, FISQUA_UNREACHABLE_MESSAGE.format(code=reference_code),
                           reference_code, error)

    if not fisqua_outbox.fisqua_configured():
        raise unreachable("not_configured")
    if offsite_hold.held("Fisqua reference code lookup"):
        raise unreachable("held")
    if fisqua_outbox.config_error() is not None:
        raise unreachable("config_error: the link is stopped until its configuration changes")
    transport = transport if transport is not None else get_transport()
    if isinstance(transport, NullTransport):
        raise unreachable("not_configured")
    if fisqua_outbox.call_budget.cooling():
        raise unreachable("cooling_down")
    if not fisqua_outbox.call_budget.take():
        raise unreachable("rate_limited")
    try:
        answer = transport.get_description(reference_code)
    except Exception as exc:
        answer = TransportResult(outcome="error", error=f"{type(exc).__name__}: {exc}")
    answer = fisqua_outbox._redacted(answer)
    if answer.outcome == "description":
        body = answer.response
        answered = body.get(fc.F_REFERENCE_CODE) if isinstance(body, dict) else None
        if not isinstance(answered, str):
            raise unreachable(f"unreadable: the answer has no {fc.F_REFERENCE_CODE}")
        if answered != reference_code:
            # Not the description asked for: the code is not known as given.
            raise CodeRefused(422, UNKNOWN_REFERENCE_CODE,
                              UNKNOWN_REFERENCE_CODE_MESSAGE.format(code=reference_code), reference_code)
        return body
    if answer.outcome == "unknown_code":
        raise CodeRefused(422, UNKNOWN_REFERENCE_CODE, UNKNOWN_REFERENCE_CODE_MESSAGE.format(code=reference_code),
                          reference_code)
    if answer.outcome == "unsupported":
        raise CodeRefused(409, FISQUA_NO_LOOKUP, FISQUA_NO_LOOKUP_MESSAGE, reference_code,
                          f"unsupported: HTTP {answer.status_code}")
    if answer.outcome == "config_error":
        fisqua_outbox.record_config_error(answer.status_code, answer.reason,
                                          fisqua_outbox._naive(now) or fisqua_outbox._now())
    elif answer.retry_after is not None:
        fisqua_outbox.call_budget.cool_down(answer.retry_after)
    raise unreachable(f"{answer.outcome}: {fisqua_outbox.scrub(answer.error or answer.outcome)}")


def check_reference_codes(db: Session, codes) -> None:
    """check_reference_code for each distinct non-empty code, in order."""
    seen = set()
    for code in codes:
        if code is None or code in seen:
            continue
        seen.add(code)
        check_reference_code(db, code)
