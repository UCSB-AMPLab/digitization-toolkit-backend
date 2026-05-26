"""
Persistent override for the active projects storage path.

Stored in /var/lib/dtk/storage-override.json so it survives backend restarts
without requiring an env var change or service restart.

All functions are safe to call from any context — failures are logged and
silently swallowed so a corrupt/missing override file never crashes the app.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_OVERRIDE_FILE = Path("/var/lib/dtk/storage-override.json")


def get_storage_override() -> str | None:
    """Return the persisted projects_root path, or None if not set."""
    try:
        if _OVERRIDE_FILE.exists():
            data = json.loads(_OVERRIDE_FILE.read_text())
            value = data.get("projects_root")
            return str(value) if value else None
    except Exception:
        logger.exception("Failed to read storage override from %s", _OVERRIDE_FILE)
    return None


def set_storage_override(projects_root: str) -> None:
    """Persist projects_root as the active storage path."""
    _OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _OVERRIDE_FILE.write_text(
        json.dumps({"projects_root": projects_root}, indent=2)
    )


def clear_storage_override() -> None:
    """Remove the override — app reverts to default DATA_DIR/projects path."""
    try:
        _OVERRIDE_FILE.unlink(missing_ok=True)
    except Exception:
        logger.exception("Failed to clear storage override")
