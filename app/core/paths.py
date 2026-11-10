"""
Path containment helpers for safely handling file paths stored in the database.

File paths persisted on records (file_path, thumbnail_path) must only ever
point inside the application's storage roots. A database row that points
elsewhere (e.g. /etc/passwd, ~/.ssh/id_rsa, the project .env) must never be
served, unlinked, or otherwise acted upon.

Version: 0.1.0-beta
"""

import os
from pathlib import Path
from typing import Optional

# Import the module (not the settings object) so that test fixtures which reload app.core.config are still picked up here
from app.core import config

# Masters a browser cannot display. A RAW capture from the gphoto2 backend has
# a JPEG copy of its embedded preview, named and placed by preview_sidecar_of
# (capture/backends/gphoto2_backend.py, _write_raw_preview). Capture writes
# only .cr2 among these today; the rest are recognised so that a RAW master
# of another make is never treated as displayable.
#
# The same list as Fisqua's RAW_EXTENSIONS (fisqua app/lib/capture/manifest.ts),
# copied exactly: Fisqua decides from a capture's filename whether its page
# needs a preview, and a capture Fisqua counts as RAW must be counted so here
# too, or a volume the unit sends without a preview is refused by Fisqua
# (preview-missing) rather than here. Keep the two lists the same.
RAW_SUFFIXES = frozenset(f".{ext}" for ext in (
    "3fr", "arw", "cr2", "cr3", "crw", "dcr", "dng", "erf", "iiq", "kdc", "mef", "mos", "mrw",
    "nef", "nrw", "orf", "pef", "raf", "raw", "rw2", "rwl", "sr2", "srf", "srw", "x3f",
))

# What the RAW backend appends to a master's stem to name its preview sidecar.
PREVIEW_SIDECAR_SUFFIX = "_preview.jpg"

# The suffix of the raw sensor buffer Picamera2Backend writes beside a JPEG
# master when a raw stream is requested (NEH-261).
RAW_SIDECAR_SUFFIX = ".raw"

# A volume with a file name stem keeps its files in `<projects root>/<stem>/`,
# in these four directories (capture.project_manager.volume_root). A volume
# without one keeps the name-based layout, `<project>/<name>/images/main/`,
# with each sidecar beside its master and thumbnails in `images/thumbnails/`.
CAPTURES_DIR = "captures"      # masters: CR2, or JPEG/TIFF
PREVIEWS_DIR = "previews"      # the `_preview.jpg` extracted from a RAW master
THUMBNAILS_DIR = "thumbnails"
METADATA_DIR = "metadata"      # the volume's manifest.jsonl
VOLUME_SUBDIRS = (CAPTURES_DIR, PREVIEWS_DIR, THUMBNAILS_DIR, METADATA_DIR)

# Directly under the projects root: the hard links the offsite upload copies
# from (app.core.image_upload). Each is another name for an image already
# under a volume, so the integrity check does not count it as a file.
OFFSITE_STAGING_DIR = ".captua-offsite-staging"


def project_dir_name(project_name: str) -> str:
    """The name of a project's directory under the projects root.

    ASCII-folded, every character outside [a-zA-Z0-9._-] replaced by `_`,
    leading dots stripped, lowercased. capture.project_manager's
    secure_project_filename is this function. A volume's stem names a
    directory in the same place, so the two are compared with
    app.models.collection's StemDirectoryCollisionError check.
    """
    import re
    import unicodedata

    name = unicodedata.normalize('NFKD', project_name).encode('ascii', 'ignore').decode('ascii')
    name = re.sub(r'[^a-zA-Z0-9._-]', '_', name)
    return name.lstrip('.').lower()


def is_raw_suffix(path: Path) -> bool:
    """True if `path` has a RAW master's suffix, in either case (`.CR2`, `.cr2`)."""
    return path.suffix.lower() in RAW_SUFFIXES


def preview_sidecar_of(master: Path) -> Path:
    """The `<stem>_preview.jpg` sidecar a master names.

    A master in a `captures/` directory is in a volume's stem layout, and its
    sidecar is in the volume's `previews/`, a sibling of `captures/`:
    `<volume>/captures/folio.CR2` names `<volume>/previews/folio_preview.jpg`.
    Any other master (the name-based layout, a project-level capture, a test
    capture in a temporary directory) names the sidecar in its own directory.
    No master of the name-based layout sits directly in a directory named
    `captures`: those masters are always in `images/main/`.

    Pure path arithmetic: nothing is resolved or checked on disk, and the
    master's suffix is not consulted, so `folio.CR2` and `folio.jpg` name the
    same `folio_preview.jpg`. Callers decide whether the sidecar applies to the
    master they hold and confine the result to storage themselves.
    """
    name = master.stem + PREVIEW_SIDECAR_SUFFIX
    if master.parent.name == CAPTURES_DIR:
        return master.parent.parent / PREVIEWS_DIR / name
    return master.with_name(name)


def raw_sidecar_of(master: Path) -> Path:
    """The `<stem>.raw` sidecar a JPEG master names (NEH-261).

    A multi-format capture (Picamera2Backend with raw) writes the JPEG master
    and its raw sensor buffer in the same directory, under the same stem. The
    JPEG is the record's image; the raw file has no row of its own and is
    found from the master by this name. Pure path arithmetic, as
    preview_sidecar_of is: nothing is checked on disk, and a master that is
    itself a `.raw` file names itself.
    """
    return master.with_suffix(RAW_SIDECAR_SUFFIX)


def _identity(path) -> "tuple | None":
    """A file's (device, inode), following symlinks, or None when it cannot be stat'ed."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino)


def same_file(a, b) -> bool:
    """os.path.samefile, with any OSError (a missing file, a broken link) read as not the same."""
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


class FileIdentities:
    """The identities of many files, for asking whether a path is one of them as same_file would answer.

    Built once from a full row set (the layout migration, the integrity
    scan), so that each question costs one stat rather than one per row. The
    identity is the (st_dev, st_ino) pair os.path.samefile compares; a path
    that cannot be stat'ed has none and is held by nothing.
    """

    def __init__(self, paths):
        self._ids = {i for i in (_identity(p) for p in paths) if i is not None}

    def holds(self, path) -> bool:
        i = _identity(path)
        return i is not None and i in self._ids


def raw_buffer_of(master: Path, row_masters) -> "Path | None":
    """The `.raw` sidecar `master` may claim, or None (NEH-261).

    A `<stem>.raw` beside `master` is a multi-format capture's raw buffer,
    and so a sidecar of `master`, only when all of these hold:

    - `master` is not itself a RAW master. `.raw` is also a RAW master's
      suffix, so `f.CR2` claims no `f.raw`.
    - The `.raw` exists. One that does not is nothing to carry or delete.
    - No row has it as its own master, whatever record or volume the row
      is in. This is decided by file identity, as os.path.samefile decides
      it, never by comparing path strings: a case-insensitive volume (an
      exFAT drive, APFS by default) makes `F.RAW` and `f.raw` one file, a
      case-sensitive one (ext4) keeps them two, and a directory symlink in
      either path changes the string but not the file.

    `row_masters` is the stored master paths of the rows that could own the
    `.raw` (FileIdentities, or any iterable of paths). A caller that cannot
    pass every row passes at least the record's siblings and every row whose
    stored file name is the `.raw`'s name, or the name its symlink points
    at, compared case-insensitively (app.core.storage_ops.rows_master_paths_named).
    That set is complete for every master path the app writes: masters are
    files the app created under its own names, so a row that owns this file
    stores it under one of those names. A row whose stored master is a
    file-level symlink to the `.raw` under some other name is not found;
    the app never writes such a path, and no provision is made for one.

    Returns the `.raw` path beside `master`, not resolved: a caller that
    moves or unlinks it acts on that entry, never on what a symlink there
    points at.
    """
    if is_raw_suffix(master):
        return None
    buffer = raw_sidecar_of(master)
    if _identity(buffer) is None:
        return None
    if isinstance(row_masters, FileIdentities):
        if row_masters.holds(buffer):
            return None
    elif any(same_file(buffer, m) for m in row_masters):
        return None
    return buffer


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
