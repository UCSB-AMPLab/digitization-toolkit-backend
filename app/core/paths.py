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

# Masters a browser cannot display. A RAW capture from the gphoto2 backend has
# a JPEG copy of its embedded preview beside it, named by preview_sidecar_of
# (capture/backends/gphoto2_backend.py, _write_raw_preview). Capture writes
# only .cr2 among these today; the rest are recognised so that a RAW master
# of another make is never treated as displayable.
RAW_SUFFIXES = frozenset({".cr2", ".nef", ".arw", ".raf", ".dng", ".raw"})

# What the RAW backend appends to a master's stem to name its preview sidecar.
PREVIEW_SIDECAR_SUFFIX = "_preview.jpg"


def is_raw_suffix(path: Path) -> bool:
    """True if `path` has a RAW master's suffix, in either case (`.CR2`, `.cr2`)."""
    return path.suffix.lower() in RAW_SUFFIXES


def preview_sidecar_of(master: Path) -> Path:
    """The `<stem>_preview.jpg` sidecar a master names, in the master's own directory.

    Pure path arithmetic: nothing is resolved or checked on disk, and the
    master's suffix is not consulted, so `folio.CR2` and `folio.jpg` name the
    same `folio_preview.jpg`. Callers decide whether the sidecar applies to the
    master they hold and confine the result to storage themselves.
    """
    return master.with_name(master.stem + PREVIEW_SIDECAR_SUFFIX)


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
