"""
Path containment helpers for safely handling file paths stored in the database.

File paths persisted on records (file_path, thumbnail_path) must only ever
point inside the application's storage roots. A database row that points
elsewhere (e.g. /etc/passwd, ~/.ssh/id_rsa, the project .env) must never be
served, unlinked, or otherwise acted upon.
"""

from pathlib import Path
from typing import Optional

# Import the module (not the settings object) so that test fixtures which reload app.core.config are still picked up here
from app.core import config


def storage_roots() -> list[Path]:
    """Return the resolved directories that may contain record files."""
    roots = [config.settings.projects_dir, config.settings.data_dir]
    return [root.resolve() for root in roots]


def resolve_within_storage(raw_path: Optional[str]) -> Optional[Path]:
    """
    Resolve a stored file path and return it only if it is contained in one
    of the allowed storage roots.

    Returns None when the path is empty, cannot be resolved, or escapes the
    storage roots (via absolute path, ../ traversal, or symlink).
    """
    if not raw_path:
        return None
    try:
        # strict=False: the file may legitimately not exist yet (e.g. a
        # thumbnail that will be generated on demand); existence is checked
        # by callers. resolve() still normalizes ../ and follows symlinks.
        resolved = Path(raw_path).resolve(strict=False)
    except (OSError, ValueError):
        return None

    for root in storage_roots():
        if resolved.is_relative_to(root):
            return resolved
    return None
