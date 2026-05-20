import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import RoleChecker
from app.api.deps import get_db_dependency
from app.models.system_log import SystemLog
from app.models.user import User
from app.schemas.system_log import SystemLogOut

allow_read_only = RoleChecker(["admin", "operator", "reviewer"])
allow_admin     = RoleChecker(["admin"])

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
    from app.core.storage_override import get_storage_override

    projects_path = settings.projects_dir
    try:
        projects_path.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(projects_path)
    except OSError:
        return {
            "projects_path": str(projects_path),
            "is_override": get_storage_override() is not None,
            "total_bytes": 0,
            "used_bytes":  0,
            "free_bytes":  0,
            "available":   False,
        }

    return {
        "projects_path": str(projects_path),
        "is_override":   get_storage_override() is not None,
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
    """Mount an unmounted partition via sudo mount (no polkit/D-Bus required).

    Requires /etc/sudoers.d/dtk-storage to grant the service user passwordless
    sudo for /usr/bin/mount and /usr/bin/umount.  This is set up by setup.sh.
    """
    if not _DEVICE_RE.match(body.device):
        raise HTTPException(status_code=400, detail="Ruta de dispositivo no válida.")

    # Look up device info so we can build a labelled mount point
    partitions = _parse_lsblk()
    dev_info = next((p for p in partitions if p["path"] == body.device), None)
    if dev_info is None:
        raise HTTPException(status_code=404, detail="Dispositivo no encontrado.")
    if dev_info.get("mountpoint"):
        return {"mountpoint": dev_info["mountpoint"], "message": "El dispositivo ya está montado."}

    # Build a safe mount point directory under the dtk data tree
    label = dev_info.get("label") or dev_info["name"]
    safe  = re.sub(r"[^a-zA-Z0-9_\-]", "_", label)[:32]
    mountpoint = _MOUNT_BASE / safe
    mountpoint.mkdir(parents=True, exist_ok=True)

    # Build mount command.
    # FAT-based filesystems (exfat, vfat) don't store Unix ownership in the
    # filesystem — ownership is controlled entirely by mount options.
    # Pass uid/gid so all files appear owned by the running user (pi).
    fstype = dev_info.get("fstype") or ""
    cmd = ["sudo", "mount"]
    if fstype in ("vfat", "exfat"):
        cmd += ["-o", f"uid={os.getuid()},gid={os.getgid()}"]
    cmd += [body.device, str(mountpoint)]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        # Clean up the (empty) directory we just created
        try:
            mountpoint.rmdir()
        except OSError:
            pass
        detail = result.stderr.strip() or result.stdout.strip() or "Error desconocido al montar."
        raise HTTPException(status_code=500, detail=detail)

    from app.core.audit import log_event
    log_event(db, level="INFO", category="system", action="storage_mount",
              actor=current_user.username, subject=body.device,
              detail=str(mountpoint))

    return {"mountpoint": str(mountpoint), "message": f"Montado en {mountpoint}"}


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
        raise HTTPException(status_code=400, detail="La ruta no existe.")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="La ruta no es un directorio.")

    # Use a clearly-labelled subdirectory so files aren't dumped into the root.
    projects_path = target / "dtk-projects"
    try:
        # Works directly when the filesystem is mounted with uid/gid options (exfat/vfat)
        # or when the pi user already owns the mount root.
        projects_path.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        # ext4 / ext2 partitions freshly formatted have a root-owned filesystem root.
        # Use sudo to create the directory, then hand ownership to pi.
        r1 = subprocess.run(
            ["sudo", "mkdir", "-p", str(projects_path)],
            capture_output=True, text=True, timeout=10,
        )
        if r1.returncode != 0:
            detail = r1.stderr.strip() or "Error al crear el directorio."
            raise HTTPException(status_code=500, detail=f"No se puede crear el directorio: {detail}")
        subprocess.run(
            ["sudo", "chown", "pi:pi", str(projects_path)],
            capture_output=True, text=True, timeout=10,
        )

    set_storage_override(str(projects_path))

    log_event(db, level="INFO", category="system", action="storage_activated",
              actor=current_user.username, subject=str(projects_path))

    return {"projects_path": str(projects_path), "message": "Almacenamiento activo actualizado."}


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
    return {"projects_path": str(settings.projects_dir), "message": "Restaurado al almacenamiento predeterminado."}
