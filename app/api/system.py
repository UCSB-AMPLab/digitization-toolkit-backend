import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import RoleChecker, get_current_user
from app.api.deps import get_db_dependency
from app.models.system_log import SystemLog
from app.models.user import User
from app.schemas.system_log import SystemLogOut

logger = logging.getLogger(__name__)

allow_read_only = RoleChecker(["admin", "operator", "reviewer"])
allow_admin     = RoleChecker(["admin"])

# Single root-privileged entry point installed (root-owned, sudoers-whitelisted)
# by the superproject setup. All privileged shell-outs go through this helper
# rather than raw mount/umount/mkdir/chown; see /etc/sudoers.d/dtk-system-helper.
HELPER = "/usr/local/bin/dtk-system-helper"

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
    max_hash_checks: int = Query(default=0, ge=0, description="Cap on files hashed (0 = no cap)"),
    current_user: User = Depends(allow_admin),
    db: Session        = Depends(get_db_dependency),
):
    """Reconcile the database against the image files and the capture manifest.

    Reports images whose files are missing, image files no row or manifest
    references, bytes that no longer match the capture-time sha256, and parentless
    records. Read-only and admin-only. Run after an SD re-clone or DB restore.
    """
    from app.core.integrity import run_integrity_check
    from app.core import audit

    report = run_integrity_check(
        db,
        verify_hashes=verify_hashes,
        max_hash_checks=(max_hash_checks or None),
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
            f"manifest_mismatches={summary['manifest_mismatches']} hashes_checked={summary['hashes_checked']}"
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


# ---------------------------------------------------------------------------
# Storage management
# ---------------------------------------------------------------------------

# Partitions mounted here are OS-critical and must never be offered as storage
_PROTECTED_MOUNTPOINTS = {"/", "/boot", "/boot/firmware"}

# Filesystem types we're willing to use for storage
_USABLE_FSTYPES = {"ext4", "ext3", "ext2", "vfat", "exfat", "ntfs", "btrfs", "xfs", "f2fs"}

# Strict allowlist for device paths accepted by mount/activate endpoints
_DEVICE_RE = re.compile(r"^/dev/(sd[a-z][0-9]+|mmcblk[0-9]+p[0-9]+|nvme[0-9]+n[0-9]+p[0-9]+)$")

# dtk-managed mount point base directory (pi user owns this)
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
    """Return current projects path and disk usage figures."""
    from app.core.config import settings
    from app.core.storage_override import get_storage_override, StorageOverrideError

    # An unreadable override is surfaced as an error, not treated as "no override"
    try:
        projects_path = settings.projects_dir
        is_override = get_storage_override() is not None
    except StorageOverrideError as e:
        return {
            "projects_path": None,
            "is_override":   True,
            "error":         str(e),
            "total_bytes":   0,
            "used_bytes":    0,
            "free_bytes":    0,
            "available":     False,
        }

    try:
        projects_path.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(projects_path)
    except OSError:
        return {
            "projects_path": str(projects_path),
            "is_override":   is_override,
            "total_bytes":   0,
            "used_bytes":    0,
            "free_bytes":    0,
            "available":     False,
        }

    return {
        "projects_path": str(projects_path),
        "is_override":   is_override,
        "total_bytes":   usage.total,
        "used_bytes":    usage.used,
        "free_bytes":    usage.free,
        "available":     True,
    }


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
    """
    if not _DEVICE_RE.match(body.device):
        raise HTTPException(status_code=400, detail="Invalid device path.")

    # Look up device info so we can build a labelled mount point
    partitions = _parse_lsblk()
    dev_info = next((p for p in partitions if p["path"] == body.device), None)
    if dev_info is None:
        raise HTTPException(status_code=404, detail="Device not found.")
    if dev_info.get("mountpoint"):
        return {"mountpoint": dev_info["mountpoint"], "message": "Device is already mounted."}

    # Build a safe mount point name under the dtk data tree. The mounts root is
    # root-owned; the helper creates the directory itself (and removes it if the
    # mount fails), so we don't mkdir here.
    label = dev_info.get("label") or dev_info["name"]
    safe  = re.sub(r"[^a-zA-Z0-9_\-]", "_", label)[:32]
    mountpoint = _MOUNT_BASE / safe

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
    """Safely unmount a partition that was mounted by DTK.

    If the unmounted path is currently the active storage override, the override
    is cleared automatically so the backend falls back to its default path.

    The helper removes the (root-owned) mountpoint directory after a successful
    umount, so no cleanup happens here.
    """
    from app.core.storage_override import get_storage_override, clear_storage_override, StorageOverrideError
    from app.core.audit import log_event

    target = Path(body.mountpoint)

    # Only allow unmounting paths we own - must be under _MOUNT_BASE
    try:
        target.resolve().relative_to(_MOUNT_BASE.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Only DTK-managed paths can be unmounted.")

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
        msg += "Storage reverted to the default."
    return {"message": msg, "override_cleared": override_cleared}


@router.post("/storage/activate")
def activate_storage(
    body: ActivateStorageRequest,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Set a mounted path as the active projects storage root."""
    from pathlib import Path
    from app.core.storage_override import set_storage_override
    from app.core.audit import log_event

    target = Path(body.path)
    if not target.exists():
        raise HTTPException(status_code=400, detail="The path does not exist.")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="The path is not a directory.")

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
    """Revert to the default DATA_DIR/projects storage path."""
    from app.core.storage_override import clear_storage_override
    from app.core.config import settings
    from app.core.audit import log_event

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
