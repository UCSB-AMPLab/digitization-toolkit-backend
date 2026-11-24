# Version: 0.3.0

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_validator, model_validator
from sqlalchemy.orm import Session

from app.api.auth import RoleChecker, get_current_user
from app.api.deps import get_db_dependency
from app.core.testing_space import (
    MAX_EXPIRY_DAYS, MAX_RESERVE_GB, MIN_EXPIRY_DAYS, MIN_RESERVE_GB, USERNAME_MAX_LENGTH,
)
from app.models.system_log import SystemLog
from app.models.user import User
from app.schemas.system_log import SystemLogOut

logger = logging.getLogger(__name__)

allow_read_only = RoleChecker(["admin", "operator", "reviewer"])
allow_admin     = RoleChecker(["admin"])
allow_contributor = RoleChecker(["admin", "operator"])

# Single root-privileged entry point installed (root-owned, sudoers-whitelisted)
# by the superproject setup. All privileged shell-outs go through this helper
# rather than raw mount/umount/mkdir/chown; see /etc/sudoers.d/dtk-system-helper.
# The path is app.core.system_helper's, which the pairing restart also uses.
from app.core.system_helper import HELPER  # noqa: E402

router = APIRouter()


@router.get("/logs", response_model=list[SystemLogOut])
def get_system_logs(
    limit:    int            = Query(default=50, ge=1, le=500),
    category: Optional[str] = Query(default=None),
    level:    Optional[str] = Query(default=None),
    current_user: User    = Depends(allow_admin),
    db: Session           = Depends(get_db_dependency),
):
    """Return recent audit log entries, newest first. Admin-only."""
    query = db.query(SystemLog).order_by(SystemLog.created_at.desc())
    if category:
        query = query.filter(SystemLog.category == category)
    if level:
        query = query.filter(SystemLog.level == level)
    return query.limit(limit).all()


@router.get("/integrity")
def integrity_check(
    verify_hashes:  bool = Query(default=True, description="Recompute sha256 and compare against the capture manifest"),
    full: bool = Query(default=False, description="Hash every manifested file. Off by default: a full sweep re-hashes every capture, which is I/O-punishing on a unit holding tens of thousands of images"),
    max_hash_checks: int = Query(default=1000, ge=1, description="Upper bound on files hashed when not running a full sweep; a random sample is taken above this many eligible files"),
    current_user: User = Depends(allow_admin),
    db: Session        = Depends(get_db_dependency),
):
    """Reconcile the database against the image files and the capture manifest.

    Reports images whose files are missing, image files with no row or manifest
    reference, bytes that no longer match the capture-time sha256, and parentless
    records. Read-only and admin-only. Run after an SD re-clone or DB restore.

    Hash verification defaults to a bounded random sample; pass full=true for the
    exhaustive (and much slower) sweep.
    """
    from app.core.integrity import run_integrity_check
    from app.core import audit

    report = run_integrity_check(
        db,
        verify_hashes=verify_hashes,
        max_hash_checks=(None if full else max_hash_checks),
    )
    summary = report["summary"]
    audit.log_event(
        db,
        level="INFO" if summary["ok"] else "WARN",
        category="system",
        action="integrity_check",
        actor=current_user.username,
        detail=(
            f"missing_files={summary['missing_files']} orphan_files={summary['orphan_files']} "
            f"manifest_mismatches={summary['manifest_mismatches']} "
            f"hashes_checked={summary['hashes_checked']}/{summary['hashes_eligible']} "
            f"sampled={summary['hash_sampled']}"
        ),
    )
    return report


@router.get("/temperature")
def get_temperature(current_user: User = Depends(allow_read_only)):
    """Get Raspberry Pi CPU temperature via vcgencmd measure_temp.

    Returns temperature in Celsius, or available=False if vcgencmd is not
    present (e.g. development environment without camera hardware).
    """
    try:
        result = subprocess.run(
            ["vcgencmd", "measure_temp"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # Output format: temp=47.2'C
        match = re.search(r"temp=([\d.]+)", result.stdout)
        if match:
            temperature = float(match.group(1))
            return {"temperature": temperature, "unit": "C", "available": True}
        return {"temperature": None, "unit": "C", "available": False}
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return {"temperature": None, "unit": "C", "available": False}


@router.get("/catalogue-backup")
def get_catalogue_backup_state(
    current_user: User = Depends(allow_read_only),
    db: Session      = Depends(get_db_dependency),
):
    """Report when the catalogue was last verified offsite (NEH-249), and the
    image upload's unit-level summary (NEH-227) under `images`.

    Read-only, and deliberately visible to every role that can see the rest of
    the system panel: a backup whose state nobody can see is a backup nobody
    knows has stopped. `last_verified_at` is null on a unit that has never
    completed a verified dump. The Rionegro unit was in that state for the
    six weeks before its card filled.

    `held` is true while the offsite upload hold file exists
    (app.core.offsite_hold): no dump is taken and no image is uploaded until
    it is removed. `hold_file` is its path. Both are under `images` too.

    `enabled`, `images.enabled` and `fisqua.configured` are the
    configuration's answers; `mode_allows`, `images.mode_allows` and
    `fisqua.mode_allows` are the unit mode's (app.core.unit_mode: offsite_on,
    link_on), so the interface can say that a configured unit sends nothing in
    its current mode (NEH-319). The top-level `mode_allows` is false in
    offline mode, where no catalogue dump is taken, so the age of the last
    verified dump says nothing about whether the copy has stopped.
    """
    from app.core import unit_mode
    from app.core.catalogue_backup import is_configured, last_verified_backup
    from app.core.fisqua_outbox import unit_fisqua_summary
    from app.core.image_upload import unit_upload_summary
    from app.core.offsite_hold import hold_state

    state = last_verified_backup(db)
    return {
        "enabled":          is_configured(),
        "mode_allows":      unit_mode.offsite_on(db),
        "last_verified_at": state["verified_at"] if state else None,
        "object":           state["object"] if state else None,
        "detail":           state["detail"] if state else None,
        **hold_state(),
        # Per-volume counts are on GET /collections/{id}/offsite.
        "images":           unit_upload_summary(db),
        # Delivery to Fisqua: configured, whether the mode allows the link, environment,
        # the unit-level configuration error while Fisqua refuses the configuration,
        # the last send and poll.
        "fisqua":           unit_fisqua_summary(db),
    }


# ---------------------------------------------------------------------------
# The unit's mode and projects setting (app.core.unit_mode)
# ---------------------------------------------------------------------------

class UnitUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Optional[Literal["offline", "backup", "fisqua"]] = None
    projectsEnabled: Optional[bool] = None
    # Leaving offline mode while volumes are kept on the unit (NEH-320).
    keptVolumes: Optional[Literal["include", "keep"]] = None
    # The unit's language (NEH-336); null or absent leaves it.
    defaultLanguage: Optional[Literal["es", "en"]] = None
    # Whether operators are asked for colour card shots (NEH-340); null or absent leaves it.
    colourCardShots: Optional[StrictBool] = None


# The keys a capture chord may never be (the frontend's capture-key.ts):
# a modifier on its own, a dead key, and a key the browser cannot name.
UNBINDABLE_KEYS = frozenset({"Control", "Alt", "Shift", "Meta", "Dead", "Unidentified"})
CHORD_KEY_MAX = 32


class CaptureChord(BaseModel):
    """One chord as the frontend stores it (CaptureChord in capture-key.ts): KeyboardEvent.key and four modifiers.

    Strict: the modifiers are JSON booleans and the key a string of 1 to
    CHORD_KEY_MAX characters, none a control character, that is not in
    UNBINDABLE_KEYS; no other field.
    """
    model_config = ConfigDict(extra="forbid", strict=True)

    key: str = Field(min_length=1, max_length=CHORD_KEY_MAX)
    ctrl: bool
    alt: bool
    meta: bool
    shift: bool

    @field_validator("key")
    @classmethod
    def _bindable(cls, v: str) -> str:
        if v in UNBINDABLE_KEYS:
            raise ValueError("this key cannot start a capture on its own")
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
            raise ValueError("the key holds a control character")
        return v


class CaptureKeyUpdate(BaseModel):
    """The body of PUT /system/unit/capture-key: a chord, or null for the built-in default. Required."""
    model_config = ConfigDict(extra="forbid")

    capture_key: Optional[CaptureChord]


@router.get("/unit")
def get_unit(
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency),
):
    """The unit's mode and projects setting, for every role.

    `mode` is `offline`, `backup` or `fisqua`. `projectsEnabled` is the
    admin's setting; `projectsShown` is whether the interface shows
    projects, true only in offline mode with projects enabled.
    `defaultProjectId` is the project new volumes go into while projects
    are hidden, null until first needed. `prerequisites` lists, for
    `backup` and `fisqua`, the codes of what the configuration lacks for
    that mode (`offsite_not_configured`, `catalogue_dump_not_configured`,
    `fisqua_not_configured`); an empty list means the switch may be tried.
    A switch to fisqua also needs Fisqua to take the operators call.
    `keptVolumes` lists every volume Captua keeps on the unit (NEH-320) as
    {id, name, files}, `files` being the count it would copy offsite.
    `colourCardShots` is whether operators are asked for colour card shots
    (app.core.colour_card).

    The testing account receives the same keys, with the mode and the
    projects setting (`mode`, `projectsEnabled`) only: `projectsShown` is
    true, since the account always works in its own projects and never in
    the default project, `defaultProjectId` is null and `prerequisites`
    and `keptVolumes` are empty, since they name or count real work.
    """
    from app.core import unit_mode
    from app.core.testing_space import TESTING_ROLE

    body = unit_mode.as_api(db)
    if current_user.role == TESTING_ROLE:
        body = {**body, "projectsShown": True, "defaultProjectId": None, "prerequisites": [], "keptVolumes": []}
    return body


@router.put("/unit")
def put_unit(
    payload: UnitUpdate,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Change the unit's mode, its projects setting, or both. Admin only.

    Returns the GET /system/unit body. A switch whose prerequisites are
    missing, or a switch to fisqua whose operators call (carrying the new
    mode) Fisqua does not take, is a 409 whose detail lists each problem
    as {code, message} (with `error` for `fisqua_unreachable`), and nothing
    changes: `offsite_held` while the offsite hold is in place, and
    `mode_changed` when another change was committed meanwhile. Switching down from fisqua tells Fisqua the new mode, best
    effort. `projectsEnabled` is stored in any mode, and matters only in
    offline mode. Each change is logged: `unit_mode_changed`,
    `unit_projects_changed`.

    Leaving offline mode while volumes are kept on the unit (NEH-320) needs
    `keptVolumes`: absent, a 409 with the problem `kept_volumes_decision`
    and nothing changes; `include` releases every kept volume with the
    switch (`kept_volumes_released`); `keep` leaves them kept
    (`kept_volumes_kept`). Into offline, or with no kept volume, it is
    ignored.

    `defaultLanguage` (`es` or `en`) sets the unit's language, after any
    change of mode has been stored; logged `unit_default_language_changed`.
    `colourCardShots` (a JSON boolean) sets whether operators are asked for
    colour card shots (app.core.colour_card), after the language; logged
    `unit_colour_card_shots_changed`.
    """
    from app.core import unit_mode

    mode_fields = (payload.mode, payload.projectsEnabled, payload.keptVolumes)
    if any(v is not None for v in mode_fields) or (payload.defaultLanguage is None
                                                   and payload.colourCardShots is None):
        try:
            unit_mode.change(db, actor=current_user.username, mode=payload.mode,
                             projects_enabled=payload.projectsEnabled, kept=payload.keptVolumes)
        except unit_mode.ModeRefused as exc:
            raise HTTPException(status_code=409, detail=exc.detail())
    if payload.defaultLanguage is not None:
        unit_mode.set_default_language(db, payload.defaultLanguage, actor=current_user.username)
    if payload.colourCardShots is not None:
        unit_mode.set_colour_card_shots(db, payload.colourCardShots, actor=current_user.username)
    return unit_mode.as_api(db)


@router.put("/unit/capture-key")
def put_capture_key(
    payload: CaptureKeyUpdate,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency),
):
    """Set the unit's capture key, or clear it with null for the built-in default. Admin and operator.

    The body is `{"capture_key": {key, ctrl, alt, meta, shift} | null}`
    (CaptureChord). Returns the GET /system/unit body; logged
    `unit_capture_key_changed` when the key changes.
    """
    from app.core import unit_mode

    chord = payload.capture_key.model_dump() if payload.capture_key is not None else None
    unit_mode.set_capture_key(db, chord, actor=current_user.username)
    return unit_mode.as_api(db)


# ---------------------------------------------------------------------------
# The testing space and its account (app.core.testing_space)
# ---------------------------------------------------------------------------

# The testing account's username is stripped of surrounding whitespace and is then 1 to
# USERNAME_MAX_LENGTH characters. Its password is kept as sent and carries no constraint
# here, so a 422 validation body never echoes it; app.core.testing_space refuses an
# empty one (password_empty).
_TestingUsername = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                                    max_length=USERNAME_MAX_LENGTH)]


class TestingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: Annotated[bool, Field(strict=True)]
    username: Optional[_TestingUsername] = None
    password: Optional[str] = None
    # Absent leaves the stored value; null is refused, as is anything but an integer in range.
    expiry_days: Annotated[int, Field(strict=True, ge=MIN_EXPIRY_DAYS, le=MAX_EXPIRY_DAYS)] = None
    reserve_gb: Annotated[int, Field(strict=True, ge=MIN_RESERVE_GB, le=MAX_RESERVE_GB)] = None


class TestingAccountUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Optional[_TestingUsername] = None
    password: Optional[str] = None

    @model_validator(mode="after")
    def _something_to_change(self):
        if self.username is None and self.password is None:
            raise ValueError("Give a username, a password, or both.")
        return self


def _space_refused(exc) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail())


@router.get("/testing")
def get_testing(
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """The testing space's settings and account. Admin only, in every mode.

    {enabled, username, account_active, expiry_days, reserve_gb, free_gb,
    projects, volumes}: `reserve_gb` is the free space, in GB of 10^9 bytes,
    kept for real work, below which the testing account's captures are
    refused; `free_gb` is the free space on the file system that holds the
    active projects root, rounded to one decimal, or null when it cannot be
    read. `username` is null while the unit has no testing account; `projects`
    and `volumes` count the testing projects and the testing volumes at
    every level. The password is never part of it.
    """
    from app.core import testing_space

    return testing_space.as_api(db)


@router.put("/testing")
def put_testing(
    payload: TestingUpdate,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Switch the testing space on or off, and set how long testing volumes are kept. Admin only.

    Returns the GET /system/testing body. Switching on with no testing
    account creates it from `username` and `password` (422
    `account_required` when either is missing); with one, it is made active
    again and the `username` and `password` sent are applied. Switching off
    makes the account inactive and ends its sessions: every token issued
    before is refused, also after the space is switched on again. A new
    password ends them too; a new username or `expiry_days` does not. The
    testing material is left as it is; it expires on its own dates. `username` or `password` sent with
    `enabled: false` is 422 `space_off`; an empty password, 422
    `password_empty`; one that cannot be encoded, 422 `password_invalid`. A username another user
    holds is 409 `username_taken`; a second testing account created
    meanwhile, 409 `account_exists`. A new `expiry_days` (1 to 365) shifts
    every testing volume's testing_expires_at by the difference, in the same
    transaction. `reserve_gb` (1 to 10000) is stored. Nothing is sent to Fisqua. Logged `testing_space_changed`,
    `testing_account_created`, `testing_account_changed`.
    """
    from app.core import testing_space

    try:
        testing_space.change(db, actor=current_user.username, enabled=payload.enabled,
                             username=payload.username, password=payload.password,
                             expiry_days=payload.expiry_days, reserve_gb=payload.reserve_gb)
    except testing_space.SpaceRefused as exc:
        raise _space_refused(exc)
    return testing_space.as_api(db)


@router.patch("/testing/account")
def patch_testing_account(
    payload: TestingAccountUpdate,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Give the testing account a new username, a new password, or both. Admin only.

    Returns the GET /system/testing body. 404 `no_testing_account` when the
    unit has none; 409 `username_taken` for a username another user holds;
    422 `password_empty` for an empty password, `password_invalid` for one
    that cannot be encoded; 422 when neither is given.
    A new password ends the account's sessions: every token issued before
    is refused. A new username leaves them. The account's active flag is
    left as it is. Nothing is sent to Fisqua. Logged
    `testing_account_changed`.
    """
    from app.core import testing_space

    try:
        testing_space.change_account(db, actor=current_user.username,
                                     username=payload.username, password=payload.password)
    except testing_space.SpaceRefused as exc:
        raise _space_refused(exc)
    return testing_space.as_api(db)


@router.post("/testing/clear")
def clear_testing(
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Remove every testing volume and then every testing project, whatever their dates. Admin only.

    Answers {projects, volumes, skipped}: the projects and volumes removed,
    and the volumes left because a capture held them. A held sub-volume
    leaves its whole top-level volume, and `skipped` counts every volume in
    that tree. A project that still holds a volume is left. Each volume
    tree and project is removed in its own try; one that fails is logged and
    counted in `skipped`, and the answer is always 200 with the three counts.
    409 `testing_purge_running`, at once, while the scheduled run holds the
    purge. The testing account and the settings stay. No
    body. Logged `testing_space_cleared`.
    """
    from app.core import testing_space

    try:
        return testing_space.clear(db, actor=current_user.username)
    except testing_space.PurgeRunning:
        raise HTTPException(status_code=409, detail={"code": testing_space.TESTING_PURGE_RUNNING,
                                                     "message": testing_space.TESTING_PURGE_RUNNING_MESSAGE})


@router.get("/testing/usb")
def list_testing_usb_drives(
    current_user: User = Depends(allow_admin),
):
    """The USB drives a testing volume can be copied to, and the copy running now.

    Admin and the testing account. {drives: [{name, label, size, fstype,
    free_bytes}], active: {collection_id, job_id} | null}. `drives` is
    app.core.usb_copy.eligible_drives: unmounted vfat or exFAT partitions of
    USB disks that are not the unit's own; `size` is in bytes and
    `free_bytes` is always null, since an offered drive is not mounted.
    `active` is the copy that holds the unit's one slot (queued or running),
    whoever started it, and null otherwise. The testing account starts
    copies of testing volumes only, and the status route answers only for
    a volume the caller reaches.
    """
    from app.core import usb_copy

    return {"drives": usb_copy.eligible_drives(), "active": usb_copy.usb_copies.active()}


def _refuse_usb_copy_drive(*, device=None, path=None) -> None:
    """HTTPException 409 `usb_copy_running` when a USB copy holds this device or a path at or below its mountpoint."""
    from app.core import usb_copy

    if usb_copy.usb_copies.holds_drive(device=device, path=path):
        raise HTTPException(status_code=409, detail={
            "code": usb_copy.USB_COPY_RUNNING, "message": usb_copy.MESSAGES[usb_copy.USB_COPY_RUNNING]})


# ---------------------------------------------------------------------------
# Pairing with Fisqua by a code (NEH-345, contract 1.11 §12)
# ---------------------------------------------------------------------------

class PairingStart(BaseModel):
    unitName: str = ""


_PAIRING_REFUSALS = {
    "mode_not_eligible": "Only a unit in Fisqua mode, or one in offline mode that is not yet linked, can be linked by a code.",
    "busy_capturing": "A capture is in progress; try again when it ends.",
    "offsite_held": "The offsite hold is in place; try again once it is removed.",
    "rclone_missing": "This unit lacks the program that uploads to the bucket; it must be linked by hand.",
    "script_missing": "The unit's provisioning script was not found; the unit must be updated.",
    "script_too_old": "The unit's provisioning script cannot replace its storage credential; the unit must be updated.",
    "not_renewable": "There is no lapsed code to replace.",
    "restart_required": "A link was written or may have been; restart the unit before asking for a new code.",
    "unit_name": "The unit name must be 1 to 64 characters.",
}


def _pairing_refused(reason: str, status: int = 409) -> HTTPException:
    message = _PAIRING_REFUSALS.get(reason, "This unit cannot ask for a pairing code now.")
    return HTTPException(status_code=status, detail={"code": reason, "message": message})


@router.get("/pairing")
def get_pairing(
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """The pairing card's state. Admin only. Reading it is the session's heartbeat.

    {state, reason, trouble, retryAfterSeconds, code, expiresInSeconds,
    renewalSecondsLeft, environment, deviceId, deviceIdError, unitName,
    currentTenant, linked: {configured, tenant, environment},
    eligible: {ok, reason}, result: {tenant, environment, at, by} | null,
    restartRequired, lastOutcome}. `restartRequired` is true once a
    provisioning run may have changed this process's settings, until the
    backend restarts (app.core.restart_latch); `lastOutcome` is the final
    state of the last session, or null.
    `state` is `idle` with no session, else the session's
    (app.core.fisqua_pairing). The collect secret, the token and the keys
    are never part of it.
    """
    from app.core import fisqua_pairing_service

    return fisqua_pairing_service.as_api(db)


@router.post("/pairing")
def start_pairing(
    body: PairingStart,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Ask Fisqua for a code. Admin only. Returns the GET body.

    422 `unit_name` for a name that is not 1 to 64 characters. 409 with
    {code, message} when the unit may not pair now: `mode_not_eligible`,
    `busy_capturing`, `offsite_held`, `rclone_missing`, `script_missing`,
    `script_too_old`, `restart_required`, `device_id_missing`, `device_id_invalid`,
    `device_id_unreadable`. A session already running is returned as it is.
    """
    from app.core import fisqua_pairing, fisqua_pairing_service

    name = body.unitName
    if not 1 <= len(name) <= 64:
        raise _pairing_refused("unit_name", status=422)
    try:
        fisqua_pairing_service.pairing_registry.begin(name, actor=current_user.username)
    except fisqua_pairing.PairingRefused as exc:
        raise _pairing_refused(exc.reason)
    return fisqua_pairing_service.as_api(db)


@router.post("/pairing/renew")
def renew_pairing(
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """A new code after `lapsed`, `lost_answer` or `unknown`, with the same unit name. Admin only.

    409 `not_renewable` otherwise, or any refusal of POST /system/pairing.
    """
    from app.core import fisqua_pairing, fisqua_pairing_service

    try:
        fisqua_pairing_service.pairing_registry.renew(actor=current_user.username)
    except fisqua_pairing.PairingRefused as exc:
        raise _pairing_refused(exc.reason)
    return fisqua_pairing_service.as_api(db)


@router.delete("/pairing")
def cancel_pairing(
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Cancel the session, unless it is applying. Admin only. Returns the GET body."""
    from app.core import fisqua_pairing_service

    fisqua_pairing_service.pairing_registry.cancel()
    return fisqua_pairing_service.as_api(db)


# ---------------------------------------------------------------------------
# Storage management
# ---------------------------------------------------------------------------

# Partitions mounted here are OS-critical and must never be offered as storage
_PROTECTED_MOUNTPOINTS = {"/", "/boot", "/boot/firmware"}

# Filesystem types we're willing to use for storage
_USABLE_FSTYPES = {"ext4", "ext3", "ext2", "vfat", "exfat", "ntfs", "btrfs", "xfs", "f2fs"}

# Strict allowlist for device paths accepted by mount/activate endpoints
_DEVICE_RE = re.compile(r"^/dev/(sd[a-z][0-9]+|mmcblk[0-9]+p[0-9]+|nvme[0-9]+n[0-9]+p[0-9]+)$")

# Base directory of the mount points Captua manages (pi user owns this)
_MOUNT_BASE = Path("/var/lib/dtk/mounts")


def _parse_lsblk() -> list[dict]:
    """Return a flat list of partition dicts from lsblk JSON output."""
    result = subprocess.run(
        ["lsblk", "-J", "-o", "NAME,SIZE,FSTYPE,MOUNTPOINT,LABEL,RM,TYPE"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return []

    data = json.loads(result.stdout)
    partitions: list[dict] = []

    def _walk(devices: list[dict]) -> None:
        for dev in devices:
            if dev.get("type") == "part":
                mountpoint = dev.get("mountpoint") or None
                # Skip OS-critical partitions
                if mountpoint in _PROTECTED_MOUNTPOINTS:
                    if "children" in dev:
                        _walk(dev["children"])
                    continue
                fstype = dev.get("fstype") or None
                # Skip partitions with filesystems we can't use (e.g. swap)
                if fstype and fstype not in _USABLE_FSTYPES:
                    if "children" in dev:
                        _walk(dev["children"])
                    continue
                partitions.append({
                    "name":       dev["name"],
                    "path":       f"/dev/{dev['name']}",
                    "size":       dev.get("size") or "",
                    "fstype":     fstype,
                    "mountpoint": mountpoint,
                    "label":      dev.get("label") or None,
                    "removable":  bool(dev.get("rm", False)),
                    "type":       dev.get("type") or "part",
                })
            if "children" in dev:
                _walk(dev["children"])

    _walk(data.get("blockdevices", []))
    return partitions


class MountRequest(BaseModel):
    device: str  # e.g. /dev/sda2


class ActivateStorageRequest(BaseModel):
    path: str  # mountpoint to use as storage root, e.g. /media/pi/data


@router.get("/storage")
def get_storage_info(current_user: User = Depends(allow_read_only)):
    """Return current projects path and disk usage figures.

    The default root is created if it is missing. An override root never is:
    when it is missing or not on a mounted drive (see
    storage_override.override_root_problem) the response says available:false
    and gives the reason in unavailable_reason, which is null otherwise.

    `reserve_bytes` is the space Captua keeps for its own work
    (settings.STORAGE_RESERVE_GB, storage_override.refuse_below_storage_reserve),
    and `usable_free_bytes` the free space above it, never below 0; 0 when
    the figures cannot be read.
    """
    from app.core.config import settings
    from app.core.storage_override import (
        get_storage_override_or_fallback, projects_root_usage, storage_reserve_bytes)

    # A corrupt override reverts to internal storage; override_invalid flags the fallback for a UI warning.
    override, override_invalid = get_storage_override_or_fallback()
    projects_path = settings.projects_dir

    def unavailable(reason: str) -> dict:
        return {
            "projects_path":      str(projects_path),
            "is_override":        override is not None,
            "override_invalid":   override_invalid,
            "total_bytes":        0,
            "used_bytes":         0,
            "free_bytes":         0,
            "available":          False,
            "unavailable_reason": reason,
            "reserve_bytes":      reserve,
            "usable_free_bytes":  0,
        }

    reserve = storage_reserve_bytes(settings)
    usage, problem = projects_root_usage(settings)
    if usage is None:
        return unavailable(problem)

    return {
        "projects_path":      str(projects_path),
        "is_override":        override is not None,
        "override_invalid":   override_invalid,
        "total_bytes":        usage.total,
        "used_bytes":         usage.used,
        "free_bytes":         usage.free,
        "available":          True,
        "unavailable_reason": None,
        "reserve_bytes":      reserve,
        "usable_free_bytes":  max(0, usage.free - reserve),
    }


@router.get("/storage/freeable")
def list_freeable_volumes(
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency),
):
    """The real volumes freeing is on offer for, largest first, with what freeing each would remove.

    From the database only (app.core.offsite_free.freeable_volumes): no
    call to the bucket and no file read. `mode_allows` is false in offline
    mode, where freeing is refused (`off_in_this_mode`).
    """
    from app.core.offsite_free import freeable_volumes

    return freeable_volumes(db)


@router.get("/storage/devices")
def list_storage_devices(current_user: User = Depends(allow_admin)):
    """List removable / non-OS partitions available for use as storage."""
    try:
        return _parse_lsblk()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"lsblk failed: {exc}") from exc


@router.post("/storage/mount")
def mount_device(
    body: MountRequest,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Mount an unmounted partition via the dtk-system-helper (no polkit/D-Bus).

    Requires /etc/sudoers.d/dtk-system-helper to grant the service user
    passwordless sudo for /usr/local/bin/dtk-system-helper. The helper adds the
    uid/gid options for vfat/exfat itself and always mounts nosuid,nodev. This
    is set up by the superproject installer.

    The mounts root is root-owned and the helper is its only writer: the backend
    just decides the mountpoint *name*; the helper creates the directory and
    removes it again if the mount fails.

    409 `usb_copy_running` while a testing volume's USB copy holds the device
    or the mountpoint (app.core.usb_copy).
    """
    if not _DEVICE_RE.match(body.device):
        raise HTTPException(status_code=400, detail="Invalid device path.")
    _refuse_usb_copy_drive(device=body.device)

    # Look up device info so we can build a labelled mount point
    partitions = _parse_lsblk()
    dev_info = next((p for p in partitions if p["path"] == body.device), None)
    if dev_info is None:
        raise HTTPException(status_code=404, detail="Device not found.")
    if dev_info.get("mountpoint"):
        return {"mountpoint": dev_info["mountpoint"], "message": "Device is already mounted."}

    # Build a safe mount point name under the data directory (/var/lib/dtk). The mounts root is
    # root-owned; the helper creates the directory itself (and removes it if the
    # mount fails), so we don't mkdir here.
    label = dev_info.get("label") or dev_info["name"]
    safe  = re.sub(r"[^a-zA-Z0-9_\-]", "_", label)[:32]
    mountpoint = _MOUNT_BASE / safe
    _refuse_usb_copy_drive(path=mountpoint)

    # Mount through the privileged helper. It handles fstype-specific options
    # (uid/gid for vfat/exfat) and always mounts nosuid,nodev, so we don't build
    # -o options here.
    cmd = ["sudo", HELPER, "mount", body.device, str(mountpoint)]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Unknown error while mounting."
        raise HTTPException(status_code=500, detail=detail)

    from app.core.audit import log_event
    log_event(db, level="INFO", category="system", action="storage_mount",
              actor=current_user.username, subject=body.device,
              detail=str(mountpoint))

    return {"mountpoint": str(mountpoint), "message": f"Mounted at {mountpoint}"}


class UnmountRequest(BaseModel):
    mountpoint: str


@router.delete("/storage/mount")
def unmount_device(
    body: UnmountRequest,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Safely unmount a partition that Captua mounted.

    If the unmounted path is currently the active storage override, the override
    is cleared automatically so the backend falls back to its default path.

    The helper removes the (root-owned) mountpoint directory after a successful
    umount, so no cleanup happens here.

    409 `usb_copy_running` while a testing volume's USB copy holds the mountpoint.
    409 `adoption_running` or `adoption_unfinished` while an adoption is
    running or unfinished, for every drive (adopt_journal.storage_switch_guard).
    """
    from app.core.storage_override import get_storage_override, clear_storage_override, StorageOverrideError
    from app.core.audit import log_event

    target = Path(body.mountpoint)

    # Only allow unmounting paths we own - must be under _MOUNT_BASE
    try:
        target.resolve().relative_to(_MOUNT_BASE.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Only paths Captua mounted can be unmounted.")
    _refuse_usb_copy_drive(path=target)

    # No drive is unmounted while an adoption is running or unfinished
    # (app.core.adopt_journal.storage_switch_guard), for the whole unmount:
    # the umount below is bounded by its timeout.
    from app.core.adopt_journal import storage_switch_guard

    with storage_switch_guard():
        # If this mountpoint (or a subpath) is the active storage override, clear it first, so the backend doesn't try to write to a stale path
        override_cleared = False
        try:
            override = get_storage_override()
        except StorageOverrideError:
            # Override file is corrupt; clear it defensively rather than leave it behind.
            clear_storage_override()
            override = None
            override_cleared = True
        else:
            if override and Path(override).resolve().is_relative_to(target.resolve()):
                clear_storage_override()
                override_cleared = True

        result = subprocess.run(
            ["sudo", HELPER, "umount", str(target)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            # Restore the override if umount failed (best-effort)
            if override_cleared and override:
                from app.core.storage_override import set_storage_override
                set_storage_override(override)
            detail = result.stderr.strip() or result.stdout.strip() or "Unknown error while unmounting."
            raise HTTPException(status_code=500, detail=detail)

        log_event(db, level="INFO", category="system", action="storage_unmount",
                  actor=current_user.username, subject=str(target))

        msg = "Device unmounted successfully."
        if override_cleared:
            msg += " Storage reverted to the default."
        return {"message": msg, "override_cleared": override_cleared}


@router.post("/storage/activate")
def activate_storage(
    body: ActivateStorageRequest,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Set a mounted path as the active projects storage root.

    409 `adoption_running` while an adoption, its recovery or its repair is
    running, and `adoption_unfinished` while an adoption's journal exists
    (app.core.adopt_journal.storage_switch_guard), checked first.
    409 `usb_copy_running` for a path at or below the mountpoint of a running
    testing volume's USB copy (app.core.usb_copy).
    """
    from pathlib import Path
    from app.core.storage_override import set_storage_override
    from app.core.audit import log_event

    from app.core.adopt_journal import storage_switch_guard

    # The resulting projects root is `<path>/dtk-projects` (below). Not while
    # an adoption is running or unfinished, unless that root is the one the
    # unfinished adoption's files are on (app.core.adopt_journal.storage_switch_guard).
    with storage_switch_guard(Path(body.path) / "dtk-projects"):
        target = Path(body.path)
        _refuse_usb_copy_drive(path=target)
        if not target.exists():
            raise HTTPException(status_code=400, detail="The path does not exist.")
        if not target.is_dir():
            raise HTTPException(status_code=400, detail="The path is not a directory.")
        # The same test every writer applies to the stored root (see
        # storage_override.override_root_problem), applied to the target before
        # anything is created in it: a directory on the root filesystem is on the
        # SD card, as an empty mount point with no drive mounted is.
        from app.core.storage_override import override_root_problem
        if override_root_problem(target):
            raise HTTPException(status_code=400, detail="The path is not on a mounted drive.")

        # Use a clearly-labelled subdirectory so files aren't dumped into the root.
        projects_path = target / "dtk-projects"
        try:
            # Works directly when the filesystem is mounted with uid/gid options (exfat/vfat)
            # or when the pi user already owns the mount root.
            projects_path.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            # ext4 / ext2 partitions freshly formatted have a root-owned filesystem root.
            # The helper does mkdir -p and hands ownership to the invoking user in one
            # step (the path must end in dtk-projects, which it does).
            r1 = subprocess.run(
                ["sudo", HELPER, "prepare-projects", str(projects_path)],
                capture_output=True, text=True, timeout=10,
            )
            if r1.returncode != 0:
                detail = r1.stderr.strip() or "Error creating the directory."
                raise HTTPException(status_code=500, detail=f"Cannot create the directory: {detail}")

        set_storage_override(str(projects_path))

        log_event(db, level="INFO", category="system", action="storage_activated",
                  actor=current_user.username, subject=str(projects_path))

        return {"projects_path": str(projects_path), "message": "Active storage updated."}


@router.delete("/storage/activate")
def reset_storage(
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Revert to the default DATA_DIR/projects storage path.

    409 `adoption_running` or `adoption_unfinished` as for POST /storage/activate.
    """
    from app.core.storage_override import clear_storage_override
    from app.core.config import settings
    from app.core.audit import log_event

    from app.core.adopt_journal import storage_switch_guard

    # The resulting projects root is the default one. Not while an adoption
    # is running or unfinished, unless that is the root the unfinished
    # adoption's files are on (app.core.adopt_journal.storage_switch_guard).
    with storage_switch_guard(settings.default_projects_dir):
        clear_storage_override()

    log_event(db, level="INFO", category="system", action="storage_reset",
              actor=current_user.username)

    from app.core.storage_override import get_storage_override  # should be None now
    return {"projects_path": str(settings.projects_dir), "message": "Reverted to the default storage."}


# ---------------------------------------------------------------------------
# Power management
# ---------------------------------------------------------------------------

class PowerRequest(BaseModel):
    action: Literal["poweroff", "reboot"]


def _run_power_action(action: str) -> None:
    """Invoke the privileged helper to power off or reboot the appliance.

    Runs in a background task after the HTTP response has been sent, so the
    reply isn't lost when the system goes down. Failures are logged (there is
    no client left to inform by the time this runs); stdin is closed so a
    misconfigured sudoers can never sit waiting for a password until the
    timeout.
    """
    try:
        result = subprocess.run(
            ["sudo", HELPER, action],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            logger.error("Power action %s failed (rc=%s): %s",
                         action, result.returncode, detail)
    except Exception:
        logger.exception("Power action %s failed", action)


@router.post("/power")
def power_control(
    body: PowerRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_dependency),
):
    """Power off or reboot the appliance. Any authenticated user may call this.

    This is intentionally NOT admin-only: the operators who run the toolkit are
    often non-technical staff, and anyone with physical access can already pull
    the plug - a graceful shutdown from the UI is strictly safer than that.

    The action is audit-logged and committed *before* it is triggered (the DB is
    about to go down), then dispatched via a BackgroundTask so the HTTP response
    is sent before the machine powers off.
    """
    # Refuse honestly on machines without the helper (dev Docker, non-Pi hosts)
    # rather than pretending we scheduled a shutdown that will never happen.
    if shutil.which("sudo") is None or not os.path.exists(HELPER):
        raise HTTPException(
            status_code=501,
            detail="Power control is not available on this device.",
        )

    from app.core.audit import log_event
    log_event(db, level="INFO", category="system", action="power_" + body.action,
              actor=current_user.username)

    background_tasks.add_task(_run_power_action, body.action)

    if body.action == "poweroff":
        message = "The device will shut down in a few seconds."
    else:
        message = "The device will restart in a few seconds."
    return {"action": body.action, "message": message}


# ==============================================================================
# The old objects an adoption left in the bucket (NEH-387, app.core.offsite_retired)
# ==============================================================================

RETIRED_KEYS_NOTE = ("This unit cannot delete anything in the bucket: its key can upload and list only. A key "
                     "marked deletable has its bytes verified at its new key, and can be deleted by someone "
                     "holding the bucket's own credentials.")


class RetiredKeyOut(BaseModel):
    key: str
    kind: str
    retired_at: Optional[datetime] = None
    volume_id: Optional[int] = None
    volume_stem: Optional[str] = None
    image_id: Optional[int] = None
    shot_id: Optional[int] = None
    current_key: Optional[str] = None
    deletable: bool


class RetiredKeysOut(BaseModel):
    note: str
    totals: dict
    keys: list[RetiredKeyOut]


@router.get("/offsite/retired-keys", response_model=RetiredKeysOut)
def retired_offsite_keys(
    format: Literal["json", "text"] = Query("json", description="text: the deletable keys only, one per line"),
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Every retired offsite key, for the person who deletes the old objects; admin only. Deletes nothing.

    Each key with its kind, when it was retired, the volume and the image or
    colour card shot it now belongs to (a page's kinds are `capture` and
    `preview`, a shot's `colour_card_shot` and `colour_card_preview`), its
    new key and whether it is deletable
    (app.core.offsite_retired.retired_keys), and the totals. With
    `format=text`, the deletable keys alone as plain text, one per line,
    relative to the remote, to hand to a deletion run with the bucket's own
    credentials.
    """
    from fastapi.responses import PlainTextResponse

    from app.core.offsite_retired import retired_keys

    keys = retired_keys(db)
    db.rollback()  # no transaction stays open from the read
    if format == "text":
        return PlainTextResponse("".join(f"{k['key']}\n" for k in keys if k["deletable"]))
    deletable = sum(1 for k in keys if k["deletable"])
    return {"note": RETIRED_KEYS_NOTE,
            "totals": {"keys": len(keys), "deletable": deletable, "not_deletable": len(keys) - deletable},
            "keys": keys}


# ---------------------------------------------------------------------------
# An unfinished adoption of captures (app.core.adopt_repair)
# ---------------------------------------------------------------------------

@router.get("/adoption")
def get_unfinished_adoption(
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """The adoption whose journal is still on the unit, or {"unfinished": null}; admin only. Changes nothing.

    The two volumes it names, whether it is running now or was left
    (`recovering`, `blocked`), why recovery stopped (a code and a sentence),
    each file at neither its old nor its new path with whether the unit can
    bring it back, and whether a repair can run
    (app.core.adopt_repair.report).
    """
    from app.core.adopt_repair import report

    return report(db)


@router.post("/adoption/repair")
def repair_unfinished_adoption(
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Fetch the files an unfinished adoption lacks from the bucket and run its recovery again; admin only.

    Answers {"result": "finished" | "reverted" | "unfinished", ...}, or 409
    with a code when it changed nothing (app.core.adopt_repair.repair).
    """
    from app.core.adopt_repair import RepairRefused, repair

    try:
        return repair(db, actor=current_user.username)
    except RepairRefused as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc


# ---------------------------------------------------------------------------
# The kind of camera the unit drives (app.core.camera_kind)
# ---------------------------------------------------------------------------

_CAMERA_KIND_REFUSALS = {
    "busy_capturing": "A capture is in progress; try again when it ends.",
    "offsite_job_running": "A volume is being freed or restored; try again when it ends.",
    "backend_restarting": "Captua is restarting; try again in a minute.",
    "driver_unavailable": "This unit lacks the driver for that kind of camera.",
}


class CameraKindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str

    @field_validator("kind", mode="before")
    @classmethod
    def _known_kind(cls, value):
        from app.core import camera_kind
        from capture.service import canonical_backend_name

        if not isinstance(value, str):
            raise ValueError("kind must be one of " + ", ".join(camera_kind.KINDS))
        kind = canonical_backend_name(value)
        if kind not in camera_kind.KINDS:
            raise ValueError("kind must be one of " + ", ".join(camera_kind.KINDS))
        return kind


def _camera_kind_refused(reason: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": reason, "message": _CAMERA_KIND_REFUSALS[reason]})


# Held across each PUT and DELETE of the camera kind, so two changes are
# made one after the other.
_camera_kind_lock = threading.Lock()


def _refuse_camera_work() -> None:
    """409 while camera work or an offsite job is counted: what reserve_restart refuses on, checked without reserving.

    Called inside unit_activity.no_restart_reserved, which has already
    refused a reserved restart. Nothing can be counted or reserved while
    that hold lasts, so reserve_restart, called later in the same hold,
    admits what this admits.
    """
    from app.core import unit_activity

    if unit_activity.in_progress(unit_activity.CAMERA):
        raise _camera_kind_refused("busy_capturing")
    if unit_activity.in_progress(unit_activity.OFFSITE_JOB):
        raise _camera_kind_refused("offsite_job_running")


def _store_or_500(change) -> None:
    try:
        change()
    except Exception as exc:
        logger.error("The camera kind could not be stored (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="The camera kind could not be stored.") from None


def _put_back(previous) -> None:
    from app.core import camera_kind

    try:
        if previous is None:
            camera_kind.clear_choice()
        else:
            camera_kind.store_choice(previous)
    except Exception as exc:
        logger.error("The camera kind stored before could not be put back (%s)", type(exc).__name__)


def _change_camera_kind(change, *, restart: bool) -> str:
    """Make `change` with no restart reserved, under the lock reservations are taken under; restart after it when asked.

    In one hold of unit_activity's lock (no_restart_reserved): refuse a
    reserved restart, and with `restart` camera work or an offsite job;
    write the change; with `restart`, reserve the restart only now, after
    the write, so a slow write cannot spend the reservation's window. A
    change that cannot be written answers 500 with nothing reserved. A
    reservation refused after the write (it cannot be, see
    _refuse_camera_work) puts the stored choice back and answers 409.
    The hold is left and the helper called next, with nothing between:
    `restarting`, or `restart_needed` (no restart-backend verb, or it
    failed) with the change kept and the reservation released, so the
    unit's restart (POST /system/power) takes it up. Without `restart`:
    the change only, `not_needed`.
    """
    from app.core import camera_kind, system_helper, unit_activity

    try:
        with unit_activity.no_restart_reserved():
            if restart:
                _refuse_camera_work()
            previous = camera_kind.stored_choice()
            _store_or_500(change)
            if restart and not unit_activity.reserve_restart():
                _put_back(previous)
                raise _camera_kind_refused("backend_restarting")
    except unit_activity.Refused:
        raise _camera_kind_refused("backend_restarting") from None
    if not restart:
        return "not_needed"
    restarting = False
    try:
        if not system_helper.supports(system_helper.RESTART_BACKEND):
            logger.info("Camera kind stored; the system helper has no %s verb, so the unit must be restarted",
                        system_helper.RESTART_BACKEND)
            return "restart_needed"
        if not system_helper.run(system_helper.RESTART_BACKEND):
            return "restart_needed"
        restarting = True
        return "restarting"
    finally:
        if not restarting:
            unit_activity.release_restart()


# Whether a PUT or DELETE restarts, by the choice this process started with
# (applied, camera_kind.applied_choice) and the kind in effect, A; B is
# another kind. Pending after: the stored choice differs from applied.
#
#   applied  stored before  request    stored after  answer                      pending
#   none     none           PUT A      A             not_needed (applied := A)   no
#   none     none           PUT B      B             restarting/restart_needed   yes
#   none     B              PUT B      B             restarting/restart_needed   yes
#   none     B              PUT A      A             not_needed (applied := A)   no
#   none     B              DELETE     none          not_needed                  no
#   none     none           DELETE     none          not_needed                  no
#   A        A              DELETE     none          restarting/restart_needed   yes
#   A        none           DELETE     none          restarting/restart_needed   yes
#   A        none           PUT A      A             not_needed                  no
#   A        A              PUT B      B             restarting/restart_needed   yes
#   A        A              PUT A      A             not_needed                  no
#
# A PUT of the kind in effect never needs a restart (the process already
# runs it), and becomes the applied choice. Any other change that leaves the
# stored choice different from applied restarts, again on each request.


@router.get("/camera-kind")
def get_camera_kind(current_user: User = Depends(allow_read_only)):
    """Which kind of camera the unit drives and what is connected, for every role and the testing account.

    The same object GET /cameras/capabilities carries beside its flags
    (app.core.camera_kind.report), answered without building the camera
    backend, so it is there when the backend cannot be built. A backend
    already built is asked for its own count only for a kind whose probe
    could not run.
    """
    from app.core import camera_kind
    from capture import service as capture_service

    return camera_kind.report(capture_service._backend)


@router.put("/camera-kind")
def put_camera_kind(
    payload: CameraKindRequest,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Store the kind of camera the unit drives, and restart the backend to use it. Admin only.

    `kind` is picamera2, gphoto2 or chdk, read as CAMERA_BACKEND is (case
    and surrounding spaces ignored); anything else is 422. A kind whose
    driver library this unit cannot import is 409 `driver_unavailable`,
    with nothing stored or reserved. The stored
    choice wins over CAMERA_BACKEND and over detection at every start
    (app.core.camera_kind).

    While a restart is reserved, every PUT is refused with 409
    `backend_restarting` and nothing is stored, the kind in effect
    included. The kind already in effect is stored and nothing is
    restarted: `restart` is `not_needed`. Otherwise the restart is reserved as the
    pairing restart reserves it (app.core.unit_activity): 409
    `busy_capturing` while camera work runs, `offsite_job_running` while a
    volume is freed or restored, `backend_restarting` while a restart is
    pending, each with nothing stored. Then the choice is stored and the
    helper's restart-backend verb run: `restart` is `restarting`, and the
    client polls GET /health until the backend answers again, then
    reloads; or `restart_needed` when the helper cannot restart it, with
    the choice stored for the unit's next start.

    Answer: {"kind": the kind stored, "configured": the kind in effect in
    this process, "restart": "restarting" | "restart_needed" | "not_needed"}.
    Logged `camera_kind_changed`.
    """
    from app.core import camera_kind
    from app.core.audit import log_event

    kind = payload.kind
    if not camera_kind.driver_available(kind):
        raise _camera_kind_refused("driver_unavailable")
    configured = camera_kind.in_effect()["kind"]

    def store():
        camera_kind.store_choice(kind)
        if kind == configured:
            camera_kind.mark_stored(kind)

    with _camera_kind_lock:
        restart = _change_camera_kind(store, restart=kind != configured)
    log_event(db, level="INFO", category="system", action="camera_kind_changed", actor=current_user.username,
              subject=kind, detail=f"in effect {configured}; restart {restart}")
    return {"kind": kind, "configured": configured, "restart": restart}


@router.delete("/camera-kind")
def delete_camera_kind(
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Clear the stored camera kind, so CAMERA_BACKEND or detection chooses it again, and restart. Admin only.

    While a restart is reserved, 409 `backend_restarting`, with nothing
    cleared. The choice is cleared; the backend is restarted when this
    process started with a stored choice (the table above), with PUT's
    refusals, and again on a repeated DELETE until a restart applies it;
    otherwise `not_needed`. The kind last used is kept.
    Answer: {"kind": null, "configured", "restart"}. Logged
    `camera_kind_cleared`.
    """
    from app.core import camera_kind
    from app.core.audit import log_event

    configured = camera_kind.in_effect()["kind"]
    with _camera_kind_lock:
        restart = _change_camera_kind(camera_kind.clear_choice, restart=camera_kind.applied_choice() is not None)
    log_event(db, level="INFO", category="system", action="camera_kind_cleared", actor=current_user.username,
              detail=f"in effect {configured}; restart {restart}")
    return {"kind": None, "configured": configured, "restart": restart}
