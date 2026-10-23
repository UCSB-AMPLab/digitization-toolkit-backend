"""
Persistent override for the active projects storage path.

Stored in /var/lib/dtk/storage-override.json so it survives backend restarts
without requiring an env var change or service restart.

A missing override file means "no override" and is normal. An override file that
exists but cannot be read raises StorageOverrideError from the low-level reader;
callers use get_storage_override_or_fallback to log it and revert to internal
storage rather than let one corrupt file 500 the whole app.

An override file that can be read is kept as the projects root even when the
path it names is not usable (the drive is not mounted, or the directory is
gone). Falling back to internal storage then would put new work on the SD card
while the rest of the project is on the drive. Instead, everything that writes
into the projects tree gets its root from writable_projects_dir, which raises
StorageUnavailableError for such a root.
"""
import json
import logging
import os
import stat
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_OVERRIDE_FILE = Path("/var/lib/dtk/storage-override.json")


class StorageOverrideError(RuntimeError):
    """Raised when the storage override file exists but cannot be read or parsed (e.g. a power-cut truncation), so a corrupt file surfaces as an error instead of a silent fallback to the internal SD."""


def get_storage_override() -> str | None:
    """Return the persisted projects_root path, or None if no override is set"""
    if not _OVERRIDE_FILE.exists():
        return None
    try:
        data = json.loads(_OVERRIDE_FILE.read_text(encoding="utf-8"))
        value = data.get("projects_root")
    except Exception as e:
        logger.error("Storage override file %s is unreadable: %s", _OVERRIDE_FILE, e)
        raise StorageOverrideError(
            f"[ERROR] Storage override file '{_OVERRIDE_FILE}' exists but is unreadable; the active storage path is unknown. Re-activate the storage drive or clear the override."
        ) from e
    return str(value) if value else None


def get_storage_override_or_fallback() -> tuple[str | None, bool]:
    """Return (override_path_or_None, corrupt). Never raises: a corrupt override file is logged and reported as corrupt=True so callers fall back to internal storage instead of turning every request into a 500."""
    try:
        return get_storage_override(), False
    except StorageOverrideError:
        logger.error("[ERROR] Storage override unreadable; falling back to internal storage")
        return None, True


def set_storage_override(projects_root: str) -> None:
    """Persist projects_root as the active storage path, durably.

    Writes via temp file + fsync + atomic replace so a power cut mid-write leaves either the old file or the complete new one, never a truncated one.
    """
    from capture.utils import atomic_write  # lazy import avoids an import cycle via config

    _OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"projects_root": projects_root}, indent=2)
    atomic_write(_OVERRIDE_FILE, lambda tmp: Path(tmp).write_text(payload, encoding="utf-8"))


def clear_storage_override() -> None:
    """Remove the override - app reverts to default DATA_DIR/projects path."""
    try:
        _OVERRIDE_FILE.unlink(missing_ok=True)
    except Exception:
        logger.exception("[ERROR] Failed to clear storage override")


class StorageUnavailableError(RuntimeError):
    """The storage override names a projects root that cannot be written to.

    The message is meant for the operator. The app answers it with 503.
    """


def _internal_device() -> int:
    """Device number of the root filesystem, which holds the internal SD card's storage."""
    return os.stat("/").st_dev


def _same_device_as_internal(st: os.stat_result) -> bool:
    return st.st_dev == _internal_device()


def override_root_problem(root: Path) -> Optional[str]:
    """Why an override root cannot take writes, or None if it can.

    The root must be an existing directory on a filesystem other than the root
    one. The check compares device numbers rather than calling
    os.path.ismount: activation stores `<mount point>/dtk-projects`, and
    accepts any existing directory as the mount point, so the root itself is
    never a mount point and its parent need not be one either. A directory with
    the root filesystem's device number is on the SD card, whatever its path,
    which is the case of a mount point with no drive mounted on it.

    The root is resolved first, so a root reached through a symlink is judged
    by the directory it leads to.
    """
    root = Path(root).resolve()
    try:
        st = os.stat(root)
    except OSError:
        return "The storage drive set for projects is not available. Connect and activate the drive, or revert to internal storage, then try again."
    if not stat.S_ISDIR(st.st_mode):
        return "The storage path set for projects is not a directory. Activate the drive again, or revert to internal storage, then try again."
    if _same_device_as_internal(st):
        return "The storage path set for projects is not on a mounted drive. Connect and activate the drive, or revert to internal storage, then try again."
    return None


def projects_root_refusal(settings=None) -> Optional[str]:
    """Why the active projects root cannot take writes, or None if it can.

    None with no override (or an unreadable one); otherwise
    override_root_problem of the override's root."""
    override, _corrupt = get_storage_override_or_fallback()
    if not override:
        return None
    return override_root_problem(Path(override))


def refuse_writes_under_unusable_root(paths) -> None:
    """Raise StorageUnavailableError if any of `paths` lies under an override
    root that cannot take writes.

    For writers that work beside an existing file (a thumbnail next to its
    source, a renumber renaming in place) rather than building a path through
    writable_projects_dir. A path outside the override root, such as an upload
    under the data directory, is not affected. Containment is judged on
    resolved paths, the root's and each path's, so a symlink on either side
    cannot place a file outside the root that is inside it on disk (callers
    often pass a path already resolved by resolve_within_storage).
    """
    override, _corrupt = get_storage_override_or_fallback()
    if not override:
        return
    root = Path(override).resolve()
    for p in paths:
        if Path(p).resolve().is_relative_to(root):
            problem = override_root_problem(root)
            if problem:
                logger.error("Storage override root %s refused for writing beside %s: %s", root, p, problem)
                raise StorageUnavailableError(problem)
            return


def writable_projects_dir(settings=None) -> Path:
    """The projects root to write into; raises StorageUnavailableError if it is not usable.

    With no override (or an unreadable override file, which falls back to
    internal storage as get_storage_override_or_fallback describes) this is the
    default root of `settings` (the app's settings when not given), unchecked.
    With an override, the override's root is returned only if
    override_root_problem finds nothing wrong with it. Nothing is created here.
    """
    if settings is None:
        from app.core.config import settings

    override, _corrupt = get_storage_override_or_fallback()
    if not override:
        return settings.default_projects_dir
    root = Path(override)
    problem = override_root_problem(root)
    if problem:
        logger.error("Storage override root %s refused for writing: %s", root, problem)
        raise StorageUnavailableError(problem)
    return root
