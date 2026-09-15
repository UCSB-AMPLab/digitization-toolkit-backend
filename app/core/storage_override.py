"""
Persistent override for the active projects storage path.

Stored in /var/lib/dtk/storage-override.json so it survives backend restarts
without requiring an env var change or service restart.

A missing override file means "no override" and is normal. An override file that
exists but cannot be read raises StorageOverrideError from the low-level reader;
callers use get_storage_override_or_fallback to log it and revert to internal
storage rather than let one corrupt file 500 the whole app.
"""
import json
import logging
from pathlib import Path

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
