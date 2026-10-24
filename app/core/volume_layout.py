"""
Move the files of each volume that has a file name stem into its stem layout.

A volume with a stem keeps its files in `<projects root>/<stem>/`, with no
project level (capture.project_manager.volume_root):

    captures/     masters: CR2, or JPEG/TIFF
    previews/     the `_preview.jpg` extracted from a RAW master
    thumbnails/   thumbnails
    metadata/     the volume's own manifest.jsonl, for captures made there

Units that captured before stems hold their files in the name-based layout,
`<project dir>/<secure volume name>/images/main/`, with each sidecar beside
its master, thumbnails in `images/thumbnails/` and one `metadata/manifest.jsonl`
per project. There two volumes of one name share a directory and nested
volumes sit beside their parents. This module moves each file a row
references into the stem layout of that row's record's volume, and rewrites
the row. A volume without a stem is left exactly where it is; once an
operator gives it one, the next run moves it. A run happens at every
startup, so a stem set while the backend runs takes effect at the next
restart; nothing moves when the stem is set.

What is moved, and how:

- The rows decide what to do. A row of a stemmed volume's record whose stored
  path is under the active projects root but not in the volume's `captures/`
  is out of place; nothing else is looked at. Files are attributed by the
  rows, never by the directory they are in, so each image of two same-named
  volumes sharing a directory goes to its own volume.
- A row whose old path is gone but whose file is already in its volume's
  `captures/`, under its own name or under the name the migration gives a
  clashing file (`<stem>_img<image id>`), and that no other row names, is
  pointed at that file, but only when the file's sha256 matches the one its
  capture record in a capture manifest holds (the only checksum the system
  keeps for an image). This is the state after a database restored from
  before the migration (rollback-update.sh) on files the migration moved. A
  row with no such checksum, or whose candidate does not match, is reported
  as missing and the candidate as a file no row references.
- A RAW master's `_preview.jpg` sidecar moves to the volume's `previews/`,
  under the name its master's final name derives (preview_sidecar_of). A
  sidecar that cannot follow is recorded in a second file in the data
  directory, retried at every run, and reported until it has moved.
- A thumbnail moves to the volume's `thumbnails/`. A thumbnail row naming a
  missing file is cleared, so the thumbnail endpoint regenerates it beside
  the moved master. A thumbnail whose name is taken in the target stays
  where it is, its row is cleared, and it is reported with the files no row
  references.
- File names are kept. No file is ever moved onto an existing one. Names are
  compared casefolded, because exFAT, vfat and APFS by default treat names
  differing only in case as one. A master whose name is taken in
  `captures/` (by a file on disk or by another planned move, or by a file of
  another record with the same stem, which would name the same sidecar), or
  whose sidecar's name is taken in `previews/`, is moved as
  `<stem>_img<image id>` (`_2`, `_3`... after that if that is taken too), and
  the row's filename follows when it held the stored file's name. The suffix
  comes from the image id, so a dry run and the run after it choose the same
  names. A renamed master with a capture id gets a relocation record in its
  volume's capture manifest before it moves, so the capture-time fixity
  check follows it to its new name. A move that keeps the name needs no
  record: the integrity check finds the file by name under its capture id,
  and the old per-project manifests stay where they are and are still read.
- A row whose master was freed from the card (app.core.offsite_free) and
  whose file is absent is never "missing": the file is stored offsite. No
  file moves for it. Its row is pointed at the name the master would have
  been moved to, by the same naming and collision rules, and reported under
  "freed_paths_moved"; a restore writes the file there. That name, and a
  freed RAW preview's, stays taken for every later move (_freed_paths),
  though nothing is on disk at it. The master's kept preview and its
  thumbnail move as usual.
- Only files under the active projects root move, and they stay on it. Files
  elsewhere (uploads under the data directory, files on another drive) are
  not looked at. Files no row references stay where they are and are
  reported.
- A volume whose `<stem>/`, or one of its four directories, resolves
  outside the projects root (a symbolic link) is not moved, and each of its
  images is reported as skipped with reason "stem_dir_outside_projects_root".
- A volume whose `<stem>/` is a live project tree (a project of that
  directory name, or a directory holding `images/`, `packages/` or
  `metadata/project_manifest.jsonl`) is not moved, and each of its images is
  reported as skipped with reason "stem_dir_is_project_tree".
- Before a volume's files move, its capture records and their relocation
  records are copied from every other manifest into
  `<stem>/metadata/manifest.jsonl`, marked `copied_from` and with their
  paths rebased onto `<stem>/`, so the volume carries its own fixity. The
  manifests they come from are left as they are; the index gets the same
  checksums from both (_volume_records_to_copy).
- A volume whose `<stem>/` already holds what is not its own (see
  _foreign_entries) is not moved, and each of its images is reported as
  skipped with reason "stem_dir_occupied" and the foreign entries. A file
  named exactly as a writer into those directories names its temporary
  file (_writer_temp) is not foreign: it is left where it is and listed
  under "temp_files_left"; nor is a generated thumbnail no row owns
  (`<uuid4 hex>_thumb.jpg`), listed under "orphan_thumbnails_left". A
  volume's leftover `metadata/manifest.jsonl.part` is removed at the start
  of every run ("stale_parts_removed").
- Directories left empty by the moves are removed (rmdir only), never a
  project directory or a project's own `images/`, `metadata/` or `packages/`.

Crash safety. The work is done one volume at a time. Before a volume's files
move, the planned moves are written durably to a journal in the data
directory. After the moves, the source and target directories are fsynced,
then the rows are committed, then the journal is removed. A run that finds a
journal completes it first: each file is found at its old or new path, moved
if it has not been, and the rows are set to where the files are. A run is
idempotent: a second run finds no row out of place and moves nothing.

The projects root comes from storage_override.writable_projects_dir, so an
override root that cannot take writes raises StorageUnavailableError before
anything is read or created.

Triggered at every backend startup (migrate_volume_layout_at_startup, after
the project rename reconciliation). On a migrated unit that costs two
queries and a string comparison per image row, with no filesystem access per
row. Run by hand, with the backend stopped, as
`python -m app.core.volume_layout [--dry-run]`; a dry run reports what would
be done and writes nothing.
"""

import argparse
import errno
import itertools
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from app.core import config
from app.core.paths import (
    CAPTURES_DIR, PREVIEW_SIDECAR_SUFFIX, PREVIEWS_DIR, THUMBNAILS_DIR, is_raw_suffix, preview_sidecar_of,
    resolve_within_storage,
)
from app.core.storage_override import StorageUnavailableError
from app.models.collection import Collection
from app.models.record import Record, RecordImage

logger = logging.getLogger(__name__)

JOURNAL_NAME = "volume-layout.journal"
SIDECARS_NAME = "volume-layout.sidecars"
MISSING_RECORDS_NAME = "volume-layout.missing-records"
JOURNAL_VERSION = 2

# A project directory's own subdirectories; never a volume's directory.
PROJECT_SUBDIRS = frozenset({"images", "metadata", "packages"})


class VolumeLayoutError(RuntimeError):
    """The migration cannot proceed safely, for a reason the message gives."""


# ---------------------------------------------------------------------------
# Journal and pending sidecars
# ---------------------------------------------------------------------------

def _journal_file() -> Path:
    return config.settings.data_dir / JOURNAL_NAME


def _sidecars_file() -> Path:
    return config.settings.data_dir / SIDECARS_NAME


def _write_json(path: Path, payload: dict) -> None:
    from capture.utils import atomic_write

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2)
    atomic_write(path, lambda tmp: Path(tmp).write_text(text, encoding="utf-8"))


def _read_json(path: Path, what: str) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise VolumeLayoutError(f"The volume layout {what} {path} is unreadable: {e}") from e


def write_layout_journal(payload: dict) -> None:
    """Durably record the moves planned for one volume, before any of them happens."""
    _write_json(_journal_file(), payload)


def read_layout_journal() -> Optional[dict]:
    """The pending journal, or None. An unreadable journal raises VolumeLayoutError.

    It is not discarded: it is the only record of where the files of an
    interrupted batch went.
    """
    return _read_json(_journal_file(), "journal")


def clear_layout_journal() -> None:
    """Remove the journal once its batch has been committed."""
    _journal_file().unlink(missing_ok=True)


def _read_pending_sidecars() -> List[dict]:
    data = _read_json(_sidecars_file(), "sidecar list")
    return list((data or {}).get("entries", []))


def _write_pending_sidecars(entries: List[dict]) -> None:
    if entries:
        _write_json(_sidecars_file(), {"version": 1, "entries": entries})
    else:
        _sidecars_file().unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Filesystem primitives
# ---------------------------------------------------------------------------

def _fsync_dir(directory: Path) -> None:
    from capture.utils import _fsync_dir as fsync_dir

    fsync_dir(directory)


def _move(src: Path, dst: Path) -> None:
    """Move one file; callers check first that nothing is at `dst`.

    Inside one filesystem this is a rename, which moves the directory entry
    and not the bytes, so the file needs no fsync; the directories are
    fsynced by the caller before the rows are committed. Across filesystems
    (a projects root with a mount point inside it) the bytes are copied to a
    temporary name beside `dst`, fsynced, renamed into place and the
    directory fsynced, and only then is the source unlinked, so a crash never
    leaves the only copy unflushed.
    """
    try:
        os.rename(src, dst)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    from capture.utils import _fsync_file

    tmp = dst.with_name(f".{dst.name}.part")
    shutil.copy2(src, tmp)
    _fsync_file(tmp)
    os.replace(tmp, dst)
    _fsync_dir(dst.parent)
    os.unlink(src)


def _listdir(path: Path) -> List[Path]:
    try:
        return sorted(path.iterdir())
    except OSError:
        return []


def _taken_in(directory: Path, name: str) -> bool:
    """True if `directory` holds an entry whose name casefolds to `name`'s."""
    folded = name.casefold()
    return any(p.name.casefold() == folded for p in _listdir(directory))


# ---------------------------------------------------------------------------
# Which rows are out of place
# ---------------------------------------------------------------------------

def _root_forms() -> Tuple[Path, Set[str]]:
    """The active projects root, resolved, and the spellings stored paths may use for it."""
    from app.core.storage_override import writable_projects_dir

    raw = writable_projects_dir(config.settings)
    root = raw.resolve()
    return root, {str(raw), str(root)}


def _expected_dirs(db: Session, root_forms: Set[str]) -> Dict[int, Set[str]]:
    """Each stemmed volume's `<stem>/captures`, as the strings a stored path's directory may equal.

    One query; volumes without a stem are not in the result, so their rows
    are never out of place.
    """
    out: Dict[int, Set[str]] = {}
    for cid, stem in db.query(Collection.id, Collection.filename_stem).filter(
            Collection.filename_stem.isnot(None)).all():
        out[cid] = {os.path.join(r, stem, CAPTURES_DIR) for r in root_forms}
    return out


def _out_of_place(db: Session, root_forms: Set[str]) -> List[Tuple[int, int]]:
    """(image id, collection id) of every stemmed volume's image stored under the root but not in its `captures/`.

    One query over the image rows (id, file_path, collection_id) and a string
    comparison each; nothing on disk is touched.
    """
    expected = _expected_dirs(db, root_forms)
    if not expected:
        return []
    prefixes = tuple(r.rstrip(os.sep) + os.sep for r in root_forms)
    rows = (
        db.query(RecordImage.id, RecordImage.file_path, Record.collection_id)
        .join(Record, RecordImage.record_id == Record.id)
        .filter(Record.collection_id.isnot(None))
        .all()
    )
    out: List[Tuple[int, int]] = []
    for iid, fp, cid in rows:
        if cid not in expected or not fp or not fp.startswith(prefixes):
            continue
        if os.path.dirname(fp) in expected[cid]:
            continue
        out.append((iid, cid))
    return out


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

class _Dir:
    """What a target directory holds, on disk and by planned moves: casefolded names and stems by owning record."""

    def __init__(self, path: Path, owners: Dict[Path, int], reserved: Optional[Dict[Path, int]] = None):
        self.names: Dict[str, Optional[int]] = {}
        self.stems: Dict[str, Set[Optional[int]]] = {}
        for p in _listdir(path):
            if p.is_file():
                self.add(p.name, owners.get(p.resolve()))
        # A freed file's name is taken though no file is there: its row
        # names it, and a restore will write it (_freed_paths).
        for p, record_id in (reserved or {}).items():
            if p.parent == path and not self.has(p.name):
                self.add(p.name, record_id)

    def add(self, name: str, record_id: Optional[int]) -> None:
        self.names[name.casefold()] = record_id
        self.stems.setdefault(Path(name).stem.casefold(), set()).add(record_id)

    def has(self, name: str) -> bool:
        return name.casefold() in self.names

    def master_taken(self, stem: str, suffix: str, record_id: int, previews: Optional["_Dir"]) -> bool:
        """Whether `<stem><suffix>` is taken here, or (`previews` given, for a RAW master) its sidecar's name there."""
        if self.has(f"{stem}{suffix}"):
            return True
        if previews is not None and previews.has(f"{stem}{PREVIEW_SIDECAR_SUFFIX}"):
            return True
        return bool(self.stems.get(stem.casefold(), set()) - {record_id})


def _clashing_files(target: Path, previews_dir: Optional[Path], stems: List[str],
                    owners: Dict[Path, int]) -> List[Path]:
    """The files on disk, no row's, that took the names a master was denied in `target` (resolved).

    `stems` are the stems passed over: the master's own, then each suffixed
    one (_suffixed_stems) that was taken, up to the one it is given. In
    `target`, a file whose stem casefolds to one of them (the name, or the
    same stem with another suffix, which would name the same sidecar); in
    `previews_dir` (for a RAW master), the `_preview.jpg` each derives.
    Files of other rows, whether on disk or planned, are not listed.
    """
    out: List[Path] = []
    folded = {s.casefold() for s in stems}
    for p in _listdir(target):
        if p.is_file() and Path(p.name).stem.casefold() in folded and p.resolve() not in owners:
            out.append(p.resolve())
    if previews_dir is not None:
        names = {f"{s}{PREVIEW_SIDECAR_SUFFIX}".casefold() for s in stems}
        for p in _listdir(previews_dir):
            if p.is_file() and p.name.casefold() in names and p.resolve() not in owners:
                out.append(p.resolve())
    return out


def _suffixed_stems(stem: str, image_id: int) -> Iterable[str]:
    """`<stem>_img<id>`, then `<stem>_img<id>_2`, `_3`, ...: the names a clashing master is given, in order."""
    base = f"{stem}_img{image_id}"
    yield base
    n = 2
    while True:
        yield f"{base}_{n}"
        n += 1


def _owners(db: Session) -> Dict[Path, int]:
    """Every file a row references, by resolved path, with its record: masters, RAW sidecars, thumbnails."""
    owners: Dict[Path, int] = {}
    for record_id, fp, tp in db.query(RecordImage.record_id, RecordImage.file_path, RecordImage.thumbnail_path).all():
        master = resolve_within_storage(fp)
        if master is not None:
            owners[master] = record_id
            if is_raw_suffix(master):
                owners[preview_sidecar_of(master)] = record_id
        thumb = resolve_within_storage(tp)
        if thumb is not None:
            owners[thumb] = record_id
    return owners


def _freed_paths(db: Session) -> Dict[Path, int]:
    """The paths of files freed from the card (app.core.offsite_free), resolved, with their record.

    A freed master's stored path, and the `_preview.jpg` a RAW master with a
    freed preview names. No file is at them, and each is reserved: a restore
    writes the file back there, so no move may give the name to another.
    """
    out: Dict[Path, int] = {}
    rows = (db.query(RecordImage.record_id, RecordImage.file_path,
                     RecordImage.master_freed_at, RecordImage.preview_freed_at)
            .filter((RecordImage.master_freed_at.isnot(None)) | (RecordImage.preview_freed_at.isnot(None)))
            .all())
    for record_id, fp, m_at, p_at in rows:
        master = resolve_within_storage(fp)
        if master is None:
            continue
        if m_at is not None:
            out[master] = record_id
        if p_at is not None and is_raw_suffix(master):
            out[preview_sidecar_of(master)] = record_id
    return out


def _capture_checksum(index, capture_id: Optional[str], image_id: int, src: Path) -> Optional[str]:
    """The capture-time sha256 the manifest holds for the image a row stored at `src`, or None.

    The capture manifest is the only checksum the system holds for an image
    (the image row has no checksum column), so a row with no capture id, or
    whose capture record does not list the file, has none. The lookup is
    app.core.integrity.FixityIndex.expected: a relocation record naming the
    image first, then the resolved path, then the file name.
    """
    return index.expected(capture_id, image_id, src)


def _adoptable(target: Path, src: Path, image_id: int, owners: Dict[Path, int],
               checksum: Optional[str]) -> Tuple[Optional[Path], List[Path]]:
    """The file in `target` a row whose old path is gone already has, and the candidates refused.

    A candidate is a file no row references under the row's own name or one
    of the names the migration gives a clashing master. It is adopted only
    when its bytes hash to `checksum`, the capture-time sha256 held for the
    row; with no checksum nothing is adopted, since a file of the same name
    may be another page. Returns (the adopted file or None, the candidates
    looked at and refused), the latter for reporting as unreferenced.
    """
    from app.core.integrity import _sha256

    names = [src.name]
    for i, stem in enumerate(_suffixed_stems(src.stem, image_id)):
        if i >= 20:
            break
        names.append(f"{stem}{src.suffix}")
    refused: List[Path] = []
    for name in names:
        cand = target / name
        if not cand.is_file() or cand.resolve() in owners:
            continue
        if checksum is not None:
            try:
                if _sha256(cand) == checksum:
                    return cand, refused
            except OSError as e:
                logger.warning(f"Volume layout: could not read {cand} to compare it with the capture checksum: {e}")
        refused.append(cand)
    return None, refused


# The target given to a volume whose `<stem>/` is a project's tree: nothing is moved.
PROJECT_TREE = object()


# The target given to a volume whose `<stem>/` holds what is not its own: nothing is moved.
OCCUPIED = object()

# The target given to a volume whose `<stem>/`, or one of its four
# directories, resolves outside the projects root (a symbolic link): nothing is moved.
OUTSIDE_ROOT = object()


def _volume_dirs_inside(root: Path, stem: str) -> bool:
    """Whether `<root>/<stem>/` and each of its four directories resolve inside `root` (resolved).

    A symbolic link at any of them pointing elsewhere would take the
    masters off the storage, where every reader refuses them.
    """
    from app.core.paths import VOLUME_SUBDIRS

    volume = root / stem
    for p in [volume] + [volume / d for d in VOLUME_SUBDIRS]:
        try:
            if not p.resolve().is_relative_to(root):
                return False
        except (OSError, RuntimeError):  # unreadable, or a symlink loop
            return False
    return True

# app.core.thumbnail.generate_thumbnail: `<uuid4().hex>_thumb.jpg`, in thumbnails/ only.
_GENERATED_THUMBNAIL = re.compile(r"^[0-9a-f]{32}_thumb\.jpg$")

# `.renumber_<uuid4().hex><suffix>`: the first-phase temporary name of a renumber that renamed files.
# Units in service may hold one left by such a renumber that was interrupted.
_RENUMBER_TEMP = re.compile(r"^\.renumber_[0-9a-f]{32}(\.[^./]+)?$")


def _atomic_write_temp(name: str) -> bool:
    """Whether `name` is the temporary name capture.utils.atomic_write gives some final file.

    atomic_write writes `<final stem>.part<final suffix>` beside the final
    file: `IMG.cr2` -> `IMG.part.cr2`, `IMG` -> `IMG.part`. True only when a
    final name exists that gives exactly `name`.
    """
    p = Path(name)
    if p.suffix == ".part":
        final = p.stem
        return bool(final) and Path(final).suffix == ""
    if p.stem.endswith(".part") and len(p.stem) > len(".part"):
        final_stem = p.stem[: -len(".part")]
        return Path(final_stem + p.suffix).stem == final_stem
    return False


def _move_temp(name: str) -> bool:
    """Whether `name` is the temporary name of this module's cross-filesystem _move: `.<final name>.part`."""
    return name.startswith(".") and name.endswith(".part") and len(name) > len("..part")


def _writer_temp(sub: str, name: str) -> bool:
    """Whether a file in `<stem>/<sub>/` is named exactly as a writer into that directory names its temporary file.

    captures/ and previews/: atomic_write (masters, the RAW preview, the EXIF
    rewrite), a file-renaming renumber's phase-1 name, and _move. thumbnails/: _move only
    (thumbnails are written directly under a uuid name, never through a
    temporary file).
    """
    from app.core.paths import CAPTURES_DIR, PREVIEWS_DIR, THUMBNAILS_DIR

    if sub in (CAPTURES_DIR, PREVIEWS_DIR):
        return _atomic_write_temp(name) or bool(_RENUMBER_TEMP.match(name)) or _move_temp(name)
    if sub == THUMBNAILS_DIR:
        return _move_temp(name)
    return False


def _foreign_entries(db: Session, volume_dir: Path, collection_id: int,
                     temps: Optional[List[str]] = None,
                     orphan_thumbnails: Optional[List[str]] = None) -> List[str]:
    """What `<stem>/` holds that is not the volume's, as paths relative to it; empty when nothing is.

    A file named as a writer names its temporary file (_writer_temp), the
    leftover of a write a power cut interrupted, is neither foreign nor
    owned: its resolved path is appended to `temps` and it is left where it
    is. A file in `thumbnails/` named as the thumbnail generator names its
    files (`<uuid4 hex>_thumb.jpg`) that no row owns is regenerable, so it is
    not foreign either: its resolved path is appended to `orphan_thumbnails`
    and it is left where it is.

    The volume's are, inside the four directories only:
    - in `captures/`, a file named as one of its rows' masters is, or as the
      migration names a clashing one (`<stem>_img<image id>...`), so a
      restored row's moved file can still be adopted;
    - in `previews/`, the `_preview.jpg` of such a name;
    - in `thumbnails/`, a row's thumbnail, or `<stem>_thumb.jpg` of such a name;
    - in `metadata/`, `manifest.jsonl`, always (see owned()), the
      `manifest.jsonl.part` an earlier version's crashed copy left, and the
      `offsite-freed-*.jsonl` / `offsite-restored-*.jsonl` run manifests.
    Anything else is foreign: another entry at the top, a directory inside
    one of the four, or any other file. A directory is listed, not walked.
    Nothing when `<stem>/` does not exist.
    """
    from app.core.offsite_free import RUN_MANIFEST_PATTERN
    from app.core.paths import CAPTURES_DIR, METADATA_DIR, PREVIEWS_DIR, THUMBNAILS_DIR
    from capture.manifestHandler import MANIFEST_PART_NAME

    if not volume_dir.is_dir():
        return []
    rows = (db.query(RecordImage.id, RecordImage.file_path, RecordImage.thumbnail_path, RecordImage.capture_id)
            .join(Record, RecordImage.record_id == Record.id)
            .filter(Record.collection_id == collection_id).all())
    stems: List[Tuple[str, str, str]] = []  # (exact stem, clash prefix, suffix), casefolded
    thumbs: Set[str] = set()
    for iid, fp, tp, _ in rows:
        if fp:
            p = Path(fp)
            stems.append((p.stem.casefold(), f"{p.stem}_img{iid}".casefold(), p.suffix.casefold()))
        if tp:
            thumbs.add(Path(tp).name.casefold())

    def stem_owned(stem: str) -> bool:
        stem = stem.casefold()
        return any(stem == exact or stem.startswith(prefix) for exact, prefix, _ in stems)

    def owned(sub: str, f: Path) -> bool:
        name = f.name.casefold()
        if sub == CAPTURES_DIR:
            return any((Path(name).stem == exact or Path(name).stem.startswith(prefix)) and Path(name).suffix == suffix
                       for exact, prefix, suffix in stems)
        if sub == PREVIEWS_DIR:
            return name.endswith(PREVIEW_SIDECAR_SUFFIX) and stem_owned(f.name[: -len(PREVIEW_SIDECAR_SUFFIX)])
        if sub == THUMBNAILS_DIR:
            return name in thumbs or (name.endswith("_thumb.jpg") and stem_owned(f.name[: -len("_thumb.jpg")]))
        if sub == METADATA_DIR and f.name == MANIFEST_PART_NAME:
            return True  # left by an earlier version's copy; removed at the start of a run
        if sub == METADATA_DIR and RUN_MANIFEST_PATTERN.match(f.name):
            return True  # a free's or a restore's record (app.core.offsite_free)
        if sub == METADATA_DIR and f.name == "manifest.jsonl":
            # Never foreign: append-only, it keeps records of deleted captures,
            # of records moved out and of copies naming other volumes'
            # directories; and a stray one does no harm, since the index, the
            # fixity gate and the bag select records by the volume's capture ids.
            return True
        return False

    foreign: List[str] = []
    for entry in _listdir(volume_dir):
        if entry.name not in (CAPTURES_DIR, PREVIEWS_DIR, THUMBNAILS_DIR, METADATA_DIR) or not entry.is_dir():
            foreign.append(entry.name)
            continue
        for f in _listdir(entry):
            if f.is_file() and not owned(entry.name, f) and _writer_temp(entry.name, f.name):
                if temps is not None:
                    temps.append(str(f.resolve()))
                continue
            if (f.is_file() and entry.name == THUMBNAILS_DIR and _GENERATED_THUMBNAIL.match(f.name)
                    and not owned(entry.name, f)):
                if orphan_thumbnails is not None:
                    orphan_thumbnails.append(str(f.resolve()))
                continue
            if f.is_dir() or not owned(entry.name, f):
                foreign.append(f"{entry.name}/{f.name}")
    return foreign


def _project_dir_names(db: Session) -> Set[str]:
    from app.core.paths import project_dir_name
    from app.models.project import Project

    return {project_dir_name(name) for (name,) in db.query(Project.name).all()}


def _is_project_tree(volume_dir: Path, project_dirs: Set[str]) -> bool:
    """Whether `<stem>/` is a live project tree, so no volume's files may be moved into it.

    Live: a project in the database has that directory name (a collision
    written before the model refused it, app.models.collection), or the
    directory holds what only a project's tree holds: `images/`,
    `packages/` or `metadata/project_manifest.jsonl`, as a project no longer
    in the database can leave it.
    """
    if volume_dir.name in project_dirs:
        return True
    return ((volume_dir / "images").exists() or (volume_dir / "packages").exists()
            or (volume_dir / "metadata" / "project_manifest.jsonl").exists())


def _plan(db: Session, root: Path, out_of_place: List[Tuple[int, int]]) -> Tuple[List[dict], List[dict], List[str], List[str], List[str]]:
    """Plan the work for the out-of-place rows under `root` (resolved). Reads only.

    Returns (batches, skipped, left_unreferenced, temp_files_left, orphan_thumbnails_left). Each batch is
    {"collection_id", "entries"}; each entry names one image's master move to
    `<stem>/captures/` (or, with "adopt", the file its row is pointed at) and
    the moves to `<stem>/previews/` and `<stem>/thumbnails/` that go with it.
    """
    from capture.project_manager import image_output_dir

    owners = _owners(db)
    project_dirs = _project_dir_names(db)
    occupied: Dict[int, List[str]] = {}
    temps: List[str] = []
    orphan_thumbnails: List[str] = []
    manifest_index: list = []  # a FixityIndex, built on the first row whose file is missing
    refused: Set[Path] = set()
    clashing: Set[Path] = set()
    targets: Dict[int, Optional[Path]] = {}
    dirs: Dict[Path, _Dir] = {}
    stem_for: Dict[Tuple[Path, int, str], str] = {}
    carried: Set[Path] = set()
    batches: Dict[int, List[dict]] = {}
    skipped: List[dict] = []
    sources: Set[Path] = set()

    reserved = _freed_paths(db)

    def dir_state(path: Path) -> _Dir:
        if path not in dirs:
            dirs[path] = _Dir(path, owners, reserved)
        return dirs[path]

    for iid, cid in sorted(out_of_place, key=lambda r: (r[1], r[0])):
        img = db.get(RecordImage, iid)
        src = resolve_within_storage(img.file_path)
        if src is None:
            skipped.append({"image_id": iid, "path": img.file_path, "reason": "outside_storage"})
            continue
        if not src.is_relative_to(root):
            skipped.append({"image_id": iid, "path": str(src), "reason": "outside_projects_root"})
            continue
        if cid not in targets:
            collection = db.get(Collection, cid)
            stem = collection.filename_stem if collection is not None else None
            try:
                targets[cid] = image_output_dir("", stem=stem).resolve() if stem else None
            except ValueError:
                targets[cid] = None  # a stem the model's pattern would refuse
            if targets[cid] is not None and not _volume_dirs_inside(root, stem):
                logger.warning(f"Volume layout: {root / stem} or one of its directories resolves outside the "
                               f"projects root {root}; volume {cid}'s files are not moved there")
                targets[cid] = OUTSIDE_ROOT
            if isinstance(targets[cid], Path) and _is_project_tree(targets[cid].parent, project_dirs):
                targets[cid] = PROJECT_TREE
            if isinstance(targets[cid], Path):
                foreign = _foreign_entries(db, targets[cid].parent, cid, temps, orphan_thumbnails)
                if foreign:
                    occupied[cid] = foreign
                    logger.warning(f"Volume layout: {targets[cid].parent} already holds what is not volume {cid}'s, "
                                   f"so its files are not moved there: {foreign}")
                    targets[cid] = OCCUPIED
        target = targets[cid]
        if target is None:
            skipped.append({"image_id": iid, "path": str(src), "reason": "no_stem"})
            continue
        if target is OCCUPIED:
            skipped.append({"image_id": iid, "path": str(src), "reason": "stem_dir_occupied",
                            "foreign": occupied[cid]})
            continue
        if target is OUTSIDE_ROOT:
            skipped.append({"image_id": iid, "path": str(src), "reason": "stem_dir_outside_projects_root"})
            continue
        if target is PROJECT_TREE:
            skipped.append({"image_id": iid, "path": str(src), "reason": "stem_dir_is_project_tree"})
            continue
        if src.parent == target:
            continue  # stored through another spelling of the same directory
        # Stored offsite and freed from the card: not missing (NEH-248), and
        # nothing to move. Planned as a move, by the same naming and
        # collision rules, so the name it gets is reserved; _execute points
        # the row at it and moves no master, and a restore writes it there.
        freed = img.master_freed_at is not None and not src.is_file()

        entry = {
            "image_id": iid,
            "record_id": img.record_id,
            "collection_id": cid,
            "capture_id": img.capture_id,
            "file_path": img.file_path,
            "filename": img.filename,
            "adopt": False,
            "from": str(src),
            "to": None,
            "sidecar_from": None,
            "sidecar_to": None,
            "thumb_from": None,
            "thumb_to": None,
            "thumb_clear": None,
            "freed": freed,
            "preview_freed": False,
        }
        thumb = resolve_within_storage(img.thumbnail_path) if img.thumbnail_path else None
        thumb_dir = target.parent / THUMBNAILS_DIR
        previews_dir = target.parent / PREVIEWS_DIR

        if not src.is_file() and not freed:
            if not manifest_index:
                from app.core.integrity import build_fixity_index
                manifest_index.append(build_fixity_index(root))
            checksum = _capture_checksum(manifest_index[0], img.capture_id, iid, src)
            found, not_taken = _adoptable(target, src, iid, owners, checksum)
            refused.update(p.resolve() for p in not_taken)
            if found is None:
                skipped.append({"image_id": iid, "path": str(src), "reason": "missing"})
                continue
            refused.discard(found.resolve())
            entry["adopt"], entry["to"] = True, str(found)
            owners[found.resolve()] = img.record_id
            if img.thumbnail_path and (thumb is None or not thumb.is_file()):
                cand = thumb_dir / Path(img.thumbnail_path).name
                if cand.is_file() and cand.resolve() not in owners:
                    entry["thumb_from"], entry["thumb_to"] = str(thumb or img.thumbnail_path), str(cand)
                    owners[cand.resolve()] = img.record_id
                else:
                    entry["thumb_clear"] = "missing"
            batches.setdefault(cid, []).append(entry)
            continue

        state = dir_state(target)
        raw = is_raw_suffix(src)
        previews = dir_state(previews_dir) if raw else None
        key = (target, img.record_id, src.stem.casefold())
        if key in stem_for:
            stem = stem_for[key]
        else:
            stem = src.stem
            if state.master_taken(stem, src.suffix, img.record_id, previews):
                passed_over = [src.stem]
                for candidate in _suffixed_stems(src.stem, iid):
                    if not state.master_taken(candidate, src.suffix, img.record_id, previews):
                        stem = candidate
                        break
                    passed_over.append(candidate)
                for f in _clashing_files(target, previews_dir if raw else None, passed_over, owners):
                    if f not in clashing:
                        clashing.add(f)
                        logger.warning(f"Volume layout: {f} is no row's and takes the name of image {iid}'s master "
                                       f"{src.name}, which is moved as {stem}{src.suffix}; the file is left where "
                                       "it is, and the volume will be skipped as stem_dir_occupied until it is removed")
            stem_for[key] = stem
        dst = target / f"{stem}{src.suffix}"
        state.add(dst.name, img.record_id)
        entry["to"] = str(dst)
        if not freed:
            sources.add(src)

        if raw:
            sidecar = preview_sidecar_of(src)
            if sidecar.is_file() and sidecar not in carried and resolve_within_storage(str(sidecar)) is not None:
                carried.add(sidecar)
                sidecar_dst = preview_sidecar_of(dst)  # in previews_dir
                entry["sidecar_from"], entry["sidecar_to"] = str(sidecar), str(sidecar_dst)
                previews.add(sidecar_dst.name, img.record_id)
            elif img.preview_freed_at is not None and not sidecar.is_file():
                # A freed preview follows its master's name; reserved like the master.
                entry["preview_freed"] = True
                previews.add(preview_sidecar_of(dst).name, img.record_id)

        if img.thumbnail_path:
            if thumb is None or not thumb.is_file():
                entry["thumb_clear"] = "missing"
            elif thumb.is_relative_to(root) and thumb.parent != thumb_dir:
                tstate = dir_state(thumb_dir)
                sources.add(thumb)
                if tstate.has(thumb.name):
                    # Left where it is with its row cleared, so no row names it any more.
                    entry["thumb_clear"] = "name_taken"
                    owners.pop(thumb, None)
                else:
                    tstate.add(thumb.name, img.record_id)
                    entry["thumb_from"], entry["thumb_to"] = str(thumb), str(thumb_dir / thumb.name)

        batches.setdefault(cid, []).append(entry)

    left = _unreferenced_in_collection_dirs(db, root, owners, sources)
    # A refused candidate sits in a volume's `captures/`, which the scan above
    # does not walk, so it is added here unless a row has since claimed it.
    left += [str(p) for p in sorted(refused) if p not in owners and str(p) not in left]
    # So is a file that forced a rename on a clash: no row's, in a volume's
    # `captures/` or `previews/`, and foreign to the next run.
    left += [str(p) for p in sorted(clashing) if p not in owners and str(p) not in left]
    return ([{"collection_id": cid, "entries": e} for cid, e in batches.items()], skipped, left, temps,
            orphan_thumbnails)


def _collection_top(path: Path, root: Path) -> Optional[Path]:
    """`<root>/<project>/<top>` for a path under a name-based volume directory, or None."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return None
    if len(parts) < 3 or parts[1] in PROJECT_SUBDIRS:
        return None
    return root / parts[0] / parts[1]


def _unreferenced_in_collection_dirs(db: Session, root: Path, owners: Dict[Path, int], sources: Set[Path]) -> List[str]:
    """Files no row references, in the name-based volume directories and in those files move out of.

    A name-based directory is any directory directly under a project's that
    is not one of the project's own (images, metadata, packages). A
    directory directly under the root named by a volume's stem is a stem
    layout, not a project's, and is not walked. The directories files move
    out of are scanned whatever their name.
    """
    stems = {stem for (stem,) in db.query(Collection.filename_stem).filter(Collection.filename_stem.isnot(None)).all()}
    tops: Set[Path] = set()
    # A directory that cannot be listed (a drive's lost+found, say) is passed over.
    for project_dir in _listdir(root):
        if not project_dir.is_dir() or project_dir.name in stems:
            continue
        for top in _listdir(project_dir):
            if top.is_dir() and top.name not in PROJECT_SUBDIRS:
                tops.add(top)
    for p in sources:
        top = _collection_top(p, root)
        if top is not None:
            tops.add(top)
    left: List[str] = []
    for top in sorted(tops):
        try:
            found = sorted(top.rglob("*"))
        except OSError as e:
            logger.warning(f"Volume layout: could not list {top}: {e}")
            continue
        for p in found:
            if p.is_file() and p.resolve() not in owners:
                left.append(str(p.resolve()))
    return left


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _settle(src: Path, dst: Path) -> Optional[Path]:
    """Put a file at `dst` if it is still at `src`; return where it is.

    Returns `dst` once the file is there, `src` if `dst` is held by another
    file (nothing is overwritten), and None if the file is at neither. Raises
    what the move raises.
    """
    at_src, at_dst = src.exists(), dst.exists()
    if at_src and not at_dst:
        dst.parent.mkdir(parents=True, exist_ok=True)
        _move(src, dst)
        return dst
    if at_dst and not at_src:
        return dst
    if at_src:
        return src
    return None


def _free_name(e: dict, src: Path, dst: Path, reserved: Optional[Set[str]] = None) -> Tuple[Path, List[Path]]:
    """A name in dst's directory for a master whose planned name was taken after planning, and what took the others.

    For a RAW master the name its sidecar would take in `previews/` must be
    free too. A name is taken by any entry, a directory included. Returns
    (the name, the files found at every name passed over,
    `dst` included: masters, and previews for a RAW master), the files
    resolved and listed by their names on disk, which may differ in case.
    A name in `reserved` (casefolded resolved paths: freed files, which have
    no file on disk) is taken too.
    """
    taken: List[Path] = []
    raw = is_raw_suffix(src)
    reserved = reserved or set()
    for stem in [dst.stem] + list(itertools.islice(_suffixed_stems(src.stem, e["image_id"]), 10000)):
        cand = dst.with_name(f"{stem}{src.suffix}")
        sidecar = preview_sidecar_of(cand)
        # Any entry takes a name, a directory included; only files are
        # listed for the report, which names files no row references.
        if (not _taken_in(cand.parent, cand.name) and not (raw and _taken_in(sidecar.parent, sidecar.name))
                and str(cand).casefold() not in reserved
                and not (raw and str(sidecar).casefold() in reserved)):
            return cand, taken
        taken.extend(_entries_named(cand.parent, cand.name) + (_entries_named(sidecar.parent, sidecar.name) if raw else []))
    raise AssertionError("unreachable")


def _taken_late(e: dict, dst: Path, reserved: Set[str]) -> bool:
    """Whether a freed master's planned name was taken after planning: by a file, or another freed row's reservation.

    For a RAW master, its preview's name in `previews/` too, unless the file
    there is the entry's own preview, already moved by an interrupted run.
    """
    if dst.exists() or (str(dst).casefold() in reserved and str(dst) != e["file_path"]):
        return True
    if is_raw_suffix(dst):
        side = preview_sidecar_of(dst)
        own = bool(e["sidecar_from"]) and Path(e["sidecar_to"]) == side and not Path(e["sidecar_from"]).exists()
        if side.exists() and not own:
            return True
    return False


def _placed_this_run(report: dict) -> Set[Path]:
    """Every file this run has moved or pointed a row at so far, resolved: masters, sidecars, thumbnails."""
    out: Set[Path] = set()
    for key in ("moved", "adopted", "sidecars_moved", "thumbnails_moved"):
        for item in report[key]:
            try:
                out.add(Path(item["to"]).resolve())
            except (KeyError, OSError, TypeError):
                continue
    return out


def _entries_named(directory: Path, name: str) -> List[Path]:
    """The files in `directory` whose names casefold to `name`'s, resolved."""
    folded = name.casefold()
    return [p.resolve() for p in _listdir(directory) if p.name.casefold() == folded and p.is_file()]


def _record_relocation(root: Path, e: dict, src: Path, dst: Path) -> None:
    """Append a relocation record for a renamed master with a capture id to its volume's manifest.

    The manifest is the one under the top directory `dst` is in, which for a
    master moved to `<stem>/captures/` is the volume's own
    `<stem>/metadata/manifest.jsonl`, with both paths relative to `<stem>/`.

    The record carries the image id and the capture-time sha256 the
    manifests hold for the image now, before the move. With no such
    checksum there is nothing to carry: no record is written and a WARNING
    says so; the export fixity gate then refuses the image by name.
    """
    if not e.get("capture_id") or src.name == dst.name:
        return
    from app.core.integrity import build_fixity_index
    from capture.manifestHandler import append_relocation_record

    sha = build_fixity_index(root).expected(e["capture_id"], e["image_id"], src)
    if sha is None:
        logger.warning(f"Volume layout: no capture checksum is held for image {e['image_id']} at {src}; "
                       f"no relocation record is written for its move to {dst}")
        return
    top = root / dst.relative_to(root).parts[0]
    append_relocation_record(top, e["capture_id"],
                             os.path.relpath(src, top), os.path.relpath(dst, top),
                             image_id=e["image_id"], sha256=sha)


def _execute(db: Session, root: Path, batch: dict, report: dict, pending_sidecars: List[dict]) -> Tuple[List[Path], Set[Path]]:
    """Carry out a batch's moves and set its rows. Does not commit.

    Returns the files' original paths (for removing emptied directories) and
    the directories touched (for fsyncing before the commit). Each entry is
    settled from where its files are now, so a batch interrupted at any point
    is completed by running it again. A master whose name was taken after
    planning gets the next suffixed name, written to the journal before it
    moves. A move that raises an OSError leaves that image's files and row
    as they were and is reported under "failed"; the other entries go on. A
    sidecar that cannot follow is added to `pending_sidecars`.
    """
    originals: List[Path] = []
    touched: Set[Path] = set()
    cache: Dict[str, Set[str]] = {}

    def reserved_now(db: Session, report: dict) -> Set[str]:
        """Freed names, casefolded: the committed rows', and those this run has pointed rows at."""
        if "db" not in cache:
            cache["db"] = {str(p).casefold() for p in _freed_paths(db)}
        out = set(cache["db"])
        for m in report["freed_paths_moved"]:
            out.add(str(Path(m["to"])).casefold())
            if is_raw_suffix(Path(m["to"])):
                out.add(str(preview_sidecar_of(Path(m["to"]))).casefold())
        return out

    for e in batch["entries"]:
        src, dst = Path(e["from"]), Path(e["to"])
        img = db.get(RecordImage, e["image_id"])
        if img is None or img.file_path not in (e["file_path"], str(dst)):
            report["skipped"].append({"image_id": e["image_id"], "path": e["from"], "reason": "row_changed"})
            continue

        # A version-1 journal (written before adoption existed) has no "adopt"
        # and no "capture_id": its entries are all moves.
        if e.get("adopt", False):
            if not dst.is_file():
                report["skipped"].append({"image_id": e["image_id"], "path": e["from"], "reason": "missing"})
                continue
            img.file_path = str(dst)
            if img.filename == src.name:
                img.filename = dst.name
            report["adopted"].append({"image_id": e["image_id"], "from": e["from"], "to": e["to"]})
            if e["thumb_to"] and Path(e["thumb_to"]).is_file():
                img.thumbnail_path = e["thumb_to"]
            elif e["thumb_clear"]:
                img.thumbnail_path = None
                report["thumbnails_cleared"].append({"image_id": e["image_id"], "reason": e["thumb_clear"]})
            continue

        freed = e.get("freed", False)
        if freed and src.exists():
            # Freed, yet a file is at its path again: not this run's to decide.
            report["skipped"].append({"image_id": e["image_id"], "path": e["from"], "reason": "freed_but_present"})
            continue
        if freed and img.file_path == str(dst):
            # A journal replayed after its batch committed: the row already
            # names the destination, which is this image's own reservation,
            # not another's, and its preview and thumbnail moved with that
            # commit (a preview that could not follow is in the pending list,
            # written before it). Nothing is left to do, as for a moved file
            # found at its destination.
            continue
        if (src.exists() and dst.exists()) or (freed and _taken_late(e, dst, reserved_now(db, report))):
            new_dst, taken = _free_name(e, src, dst, reserved_now(db, report))
            logger.warning(f"Volume layout: {dst} was taken after planning; "
                           f"{'pointing freed' if freed else 'moving'} {src} to {new_dst}")
            # Only files no row references are strays: one a row names (a
            # master written since the plan, or a pending journal's) is that
            # row's, and so is every file this run has already put in place.
            # Those rows are not in the database yet: the batch commits at
            # its end and the session does not autoflush.
            owned = (set(_owners(db)) | _placed_this_run(report)) if taken else set()
            for f in taken:
                if f in owned or str(f) in report["left_unreferenced"]:
                    continue
                logger.warning(f"Volume layout: {f} took a name image {e['image_id']}'s master was planned to "
                               "take, after planning, and is left where it is")
                report["left_unreferenced"].append(str(f))
            e["to"] = str(new_dst)
            if e["sidecar_from"]:
                e["sidecar_to"] = str(preview_sidecar_of(new_dst))
            write_layout_journal({"version": JOURNAL_VERSION, "root": str(root),
                                  "collection_id": batch["collection_id"], "entries": batch["entries"]})
            dst = new_dst
        if freed:
            # No master to move: the row is pointed at the reserved name, and
            # the capture checksum follows a renamed one (_record_relocation).
            try:
                _record_relocation(root, e, src, dst)
            except OSError as err:
                logger.error(f"Volume layout: could not record the relocation of freed {src} to {dst}: {err}")
                report["failed"].append({"image_id": e["image_id"], "path": e["from"], "error": str(err)})
                continue
            if img.file_path != str(dst):
                img.file_path = str(dst)
                if img.filename == src.name:
                    img.filename = dst.name
            report["freed_paths_moved"].append({**_summary(e), "kind": "capture"})
        else:
            try:
                if src.exists() and not dst.exists():
                    _record_relocation(root, e, src, dst)
                at = _settle(src, dst)
            except OSError as err:
                logger.error(f"Volume layout: could not move {src} to {dst}; left at {src}: {err}")
                report["failed"].append({"image_id": e["image_id"], "path": e["from"], "error": str(err)})
                continue
            if at != dst:
                report["skipped"].append({"image_id": e["image_id"], "path": e["from"], "reason": "missing"})
                continue
            if img.file_path != str(dst):
                img.file_path = str(dst)
                if img.filename == src.name:
                    img.filename = dst.name
            originals.append(src)
            touched.update({src.parent, dst.parent})
            report["moved"].append({"image_id": e["image_id"], "record_id": e["record_id"],
                                    "collection_id": e["collection_id"], "from": e["from"], "to": e["to"]})
        if e.get("preview_freed", False):
            report["freed_paths_moved"].append({**_summary(e), "kind": "preview",
                                                "from": str(preview_sidecar_of(src)),
                                                "to": str(preview_sidecar_of(dst))})

        if e["sidecar_from"]:
            s_src, s_dst = Path(e["sidecar_from"]), Path(e["sidecar_to"])
            try:
                s_at = _settle(s_src, s_dst)
            except OSError as err:
                logger.warning(f"Volume layout: moved {src} to {dst} but its sidecar could not follow, left at {s_src}: {err}")
                s_at = s_src
            if s_at == s_dst:
                originals.append(s_src)
                touched.update({s_src.parent, s_dst.parent})
                report["sidecars_moved"].append({"from": e["sidecar_from"], "to": e["sidecar_to"]})
            elif s_at == s_src:
                pending_sidecars.append({"image_id": e["image_id"], "master": str(dst),
                                         "from": e["sidecar_from"], "to": e["sidecar_to"]})
                report["sidecars_not_moved"].append({"image_id": e["image_id"], "path": e["sidecar_from"]})

        if e["thumb_clear"]:
            img.thumbnail_path = None
            report["thumbnails_cleared"].append({"image_id": e["image_id"], "reason": e["thumb_clear"]})
        elif e["thumb_from"]:
            t_src, t_dst = Path(e["thumb_from"]), Path(e["thumb_to"])
            try:
                t_at = _settle(t_src, t_dst)
            except OSError as err:
                logger.warning(f"Volume layout: thumbnail {t_src} could not be moved to {t_dst}: {err}")
                t_at = t_src
            if t_at == t_dst:
                img.thumbnail_path = str(t_dst)
                originals.append(t_src)
                touched.update({t_src.parent, t_dst.parent})
                report["thumbnails_moved"].append({"from": e["thumb_from"], "to": e["thumb_to"]})
            elif t_at is None:
                img.thumbnail_path = None
                report["thumbnails_cleared"].append({"image_id": e["image_id"], "reason": "missing"})
            else:
                # Left where it is; the row keeps naming it, and it still exists.
                report["thumbnails_not_moved"].append({"image_id": e["image_id"], "path": e["thumb_from"]})
    return originals, touched


def _remove_emptied_dirs(paths: Iterable[Path], root: Path) -> None:
    """rmdir each directory the moves left empty, up to (not including) the project directory."""
    for p in paths:
        d = p.parent
        while True:
            try:
                parts = d.relative_to(root).parts
            except ValueError:
                break
            if len(parts) < 2 or parts[1] in PROJECT_SUBDIRS:
                break
            try:
                d.rmdir()
            except OSError:
                break  # not empty, or already gone
            d = d.parent


def _commit(db: Session, what: str) -> None:
    try:
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            logger.exception("Volume layout: rollback after a failed commit failed")
        logger.exception(f"Volume layout: commit of {what} failed")
        raise


def _volume_records_to_copy(db: Session, root: Path, collection_id: int) -> Tuple[Optional[Path], List[str]]:
    """The volume's `<stem>/` and the lines its manifest still lacks: its records from every other manifest.

    A volume's records are the capture records of the capture ids of its
    images, and the relocation records of those captures. See
    _records_to_copy for how each copy is made.
    """
    collection = db.get(Collection, collection_id)
    if collection is None or not collection.filename_stem:
        return None, []
    vol = root / collection.filename_stem
    capture_ids = {cid for (cid,) in db.query(RecordImage.capture_id)
                   .join(Record, RecordImage.record_id == Record.id)
                   .filter(Record.collection_id == collection_id, RecordImage.capture_id.isnot(None)).all()}
    if not capture_ids:
        return vol, []
    return vol, _records_to_copy(root, {collection_id: (vol, capture_ids)})[collection_id]


def _records_to_copy(root: Path, wanted: Dict[int, Tuple[Path, Set[str]]]) -> Dict[int, List[str]]:
    """For each volume in `wanted` (id -> (`<stem>/`, capture ids)), the lines to add to its own manifest.

    Every manifest is read once, whatever the number of volumes. For each
    volume, the capture and relocation records of its capture ids found in
    any manifest but its own are copied. Each copy keeps every field of the
    line it is taken from, adds `copied_from` (the source manifest, relative
    to the root; a line that is itself a copy keeps the original's), and has
    its paths (`files[].relative_path`, `from`, `to`) rebased from the source
    manifest's directory onto `<stem>/`, so each names the same absolute
    file and the fixity index gets from the copy the checksum it gets from
    the source. A line the volume's manifest already holds is not copied
    again, so a replayed batch adds nothing.
    """
    from app.core.integrity import _manifests, manifest_base, read_manifest_lines

    own = {cid: vol / "metadata" / "manifest.jsonl" for cid, (vol, _) in wanted.items()}
    present: Dict[int, Set[str]] = {}
    for cid, path in own.items():
        try:
            present[cid] = set(read_manifest_lines(path)[0]) if path.exists() else set()
        except OSError as e:
            raise VolumeLayoutError(f"The manifest {path} is unreadable: {e}") from e
    by_capture: Dict[str, List[int]] = {}
    for cid, (_, ids) in wanted.items():
        for capture_id in ids:
            by_capture.setdefault(capture_id, []).append(cid)
    out: Dict[int, List[str]] = {cid: [] for cid in wanted}
    if not by_capture:
        return out
    for manifest in sorted(_manifests(root)):
        resolved = manifest.resolve()
        base = manifest_base(manifest, root)
        try:
            lines, _ = read_manifest_lines(manifest)
        except OSError as e:
            logger.warning(f"Volume layout: could not read {manifest} to copy volumes' records: {e}")
            continue
        for line in lines:
            try:
                source = json.loads(line)
            except ValueError:
                continue
            if not isinstance(source, dict) or source.get("capture_id") not in by_capture:
                continue
            for cid in by_capture[source["capture_id"]]:
                if resolved == own[cid].resolve():
                    continue
                vol = wanted[cid][0]
                entry = json.loads(line)

                def rebase(rel: str) -> str:
                    return os.path.relpath(os.path.normpath(base / rel), vol)

                if entry.get("record_type") == "relocation":
                    for key in ("from", "to"):
                        if isinstance(entry.get(key), str):
                            entry[key] = rebase(entry[key])
                else:
                    for f in entry.get("files") or []:
                        if isinstance(f, dict) and isinstance(f.get("relative_path"), str):
                            f["relative_path"] = rebase(f["relative_path"])
                entry.setdefault("copied_from", os.path.relpath(manifest, root))
                copy = json.dumps(entry, ensure_ascii=False)
                if copy not in present[cid]:
                    present[cid].add(copy)
                    out[cid].append(copy)
    return out


def _copy_volume_records(db: Session, root: Path, collection_id: int, report: dict) -> None:
    """Add the volume's records still missing from `<stem>/metadata/manifest.jsonl` to it (append_manifest_lines).

    Done before the batch's files move, so the volume carries its own
    fixity from the moment its files are in `<stem>/`. A copy that cannot
    be written is logged at ERROR and reported under "manifest_copy_failed";
    the records stay in the manifests they were copied from, which the
    index keeps reading, so the move goes on.
    """
    from capture.manifestHandler import append_manifest_lines

    vol, lines = _volume_records_to_copy(db, root, collection_id)
    if not lines:
        return
    try:
        append_manifest_lines(vol, lines)
        _fsync_dir(vol / "metadata")
    except OSError as e:
        logger.error(f"Volume layout: could not copy {len(lines)} record(s) of volume {collection_id} "
                     f"into {vol / 'metadata' / 'manifest.jsonl'}: {e}")
        report["manifest_copy_failed"].append({"collection_id": collection_id, "error": str(e)})
        return
    report["manifest_records_copied"] += len(lines)


def _commit_batch(db: Session, root: Path, batch: dict, report: dict) -> None:
    _copy_volume_records(db, root, batch["collection_id"], report)
    pending = _read_pending_sidecars()
    before = len(pending)
    originals, touched = _execute(db, root, batch, report, pending)
    # The renames are durable before the rows that name their results are.
    for d in sorted(touched):
        _fsync_dir(d)
    if len(pending) != before:
        _write_pending_sidecars(pending)
    _commit(db, f"the batch of volume {batch['collection_id']} (the journal is kept for the next run)")
    clear_layout_journal()
    _remove_emptied_dirs(originals, root)


def _retry_sidecars(db: Session, root: Path, report: dict, dry_run: bool) -> None:
    """Move each sidecar a previous run could not move to where its master's current file names it.

    The sidecar's old and new paths were recorded when its move failed,
    because only then are both known: the capture manifest holds capture-time
    paths, which a later relocation makes stale, and a sidecar need not be in
    it. The entry is keyed by image id. When the master's row names another
    file than the one recorded (the master was renamed or moved while the
    sidecar waited), the sidecar goes where that file names it, under the name its
    stem derives (preview_sidecar_of), as relocate_image would have named it, and the
    entry is updated to match. An entry is dropped when the sidecar is at its
    destination and gone from its old path, when it is gone from both
    (logged), or when its image's row is gone: that is logged once, the file
    is left where it is and reported under "left_unreferenced" in that run.
    An entry whose master file is missing or is not RAW, whose destination is taken or outside the root, or
    whose move fails is kept, logged with both paths, and reported under
    "sidecars_pending"; nothing is dropped without a log line.
    """
    entries = _read_pending_sidecars()
    if not entries:
        return
    keep: List[dict] = []
    originals: List[Path] = []

    def pending(s: dict, why: str) -> None:
        logger.warning(f"Volume layout: sidecar {s['from']} of image {s['image_id']} not moved to {s['to']}: {why}; "
                       "kept for the next run")
        keep.append(s)
        report["sidecars_pending"].append(s)

    for s in entries:
        frm = Path(s["from"])
        img = db.get(RecordImage, s["image_id"])
        if img is None:
            # No row will name the sidecar again, so there is nothing to move
            # it beside. The file stays; the entry goes.
            logger.warning(f"Volume layout: pending sidecar {frm} of image {s['image_id']}: the image's row is gone; "
                           "the file is left where it is and no longer retried")
            if frm.exists():
                report["left_unreferenced"].append(str(frm.resolve()))
            continue
        master = resolve_within_storage(img.file_path)
        if master is None or not master.is_file():
            pending(s, f"its master is not at {img.file_path}")
            continue
        if img.file_path != s["master"]:
            if not is_raw_suffix(master):
                pending(s, f"its master's row now names {img.file_path}, which is not a RAW master")
                continue
            logger.warning(f"Volume layout: the master of pending sidecar {frm} was renamed from {s['master']} "
                           f"to {img.file_path}; the sidecar follows it")
            s = dict(s, master=img.file_path, to=str(preview_sidecar_of(master)))
        to = Path(s["to"])
        if not frm.exists():
            if not to.exists():
                logger.warning(f"Volume layout: the sidecar of {master} is gone from {frm} and is not at {to}; "
                               "no longer retried")
            continue
        if not to.is_relative_to(root):
            pending(s, f"{to} is outside the projects root {root}")
            continue
        if dry_run:
            keep.append(s)
            report["sidecars_pending"].append(s)
            continue
        try:
            if _taken_in(to.parent, to.name):
                raise FileExistsError(errno.EEXIST, "name taken", str(to))
            to.parent.mkdir(parents=True, exist_ok=True)  # `previews/` may not exist yet
            _move(frm, to)
            _fsync_dir(frm.parent)
            _fsync_dir(to.parent)
        except OSError as err:
            pending(s, str(err))
            continue
        originals.append(frm)
        report["sidecars_moved"].append({"from": s["from"], "to": s["to"]})
    if not dry_run:
        _write_pending_sidecars(keep)
        _remove_emptied_dirs(originals, root)


def _new_report(root: Path, dry_run: bool) -> dict:
    return {
        "root": str(root),
        "dry_run": dry_run,
        "pending_journal": False,
        "completed_journal": False,
        "out_of_place": 0,
        "moved": [],
        "adopted": [],
        "freed_paths_moved": [],
        "sidecars_moved": [],
        "sidecars_not_moved": [],
        "sidecars_pending": [],
        "thumbnails_moved": [],
        "thumbnails_cleared": [],
        "thumbnails_not_moved": [],
        "failed": [],
        "skipped": [],
        "left_unreferenced": [],
        "manifest_records_copied": 0,
        "manifest_copy_failed": [],
        "manifests_repaired": {},
        "stale_parts_removed": 0,
        "temp_files_left": [],
        "orphan_thumbnails_left": [],
        "captures_without_record": {},
    }


def _summary(entry: dict) -> dict:
    return {"image_id": entry["image_id"], "record_id": entry["record_id"],
            "collection_id": entry["collection_id"], "from": entry["from"], "to": entry["to"]}


def migrate_volume_layout(db: Session, dry_run: bool = False) -> dict:
    """Move every stemmed volume's out-of-place files into its stem layout; return a report.

    In order: complete a pending journal, retry the sidecars that could not
    follow their masters, find the out-of-place rows, move their files a
    volume at a time, then check every migrated volume's own manifest and
    copy in the capture records it lacks (_repair_volume_manifests). With dry_run, nothing is moved, written or created:
    the report lists the moves and row updates a run would make now, and a
    pending journal's entries are listed under "pending_journal_entries"
    without being replayed. Raises StorageUnavailableError when the active
    projects root is an override that cannot take writes, VolumeLayoutError
    when a pending journal names another root or a journal or sidecar list
    cannot be read, and re-raises a failed commit, keeping the journal.
    """
    root, root_forms = _root_forms()
    report = _new_report(root, dry_run)

    pending = read_layout_journal()
    if pending is not None:
        report["pending_journal"] = True
        if pending.get("root") != str(root):
            raise VolumeLayoutError(
                f"A volume layout journal for {pending.get('root')} is pending, but the active projects root is {root}. "
                "Activate that storage again and restart, so the interrupted batch can be completed."
            )
        if dry_run:
            report["pending_journal_entries"] = [
                {"image_id": e["image_id"], "from": e["from"], "to": e["to"]} for e in pending.get("entries", [])
            ]
        else:
            logger.warning(f"Volume layout: completing the interrupted batch of volume {pending.get('collection_id')}")
            _commit_batch(db, root, pending, report)
            report["completed_journal"] = True

    _remove_stale_parts(db, root, report, dry_run)
    _retry_sidecars(db, root, report, dry_run)

    out_of_place = _out_of_place(db, root_forms)
    report["out_of_place"] = len(out_of_place)
    if out_of_place:
        batches, skipped, left, temps, orphan_thumbnails = _plan(db, root, out_of_place)
        report["temp_files_left"] = temps
        report["orphan_thumbnails_left"] = orphan_thumbnails
        for t in temps:
            logger.warning(f"Volume layout: temporary file of an interrupted write, left where it is: {t}")
        for t in orphan_thumbnails:
            logger.warning(f"Volume layout: generated thumbnail no row references, left where it is: {t}")
        report["skipped"].extend(skipped)
        report["left_unreferenced"] += [p for p in left if p not in report["left_unreferenced"]]
        if dry_run:
            for b in batches:
                report["manifest_records_copied"] += len(_volume_records_to_copy(db, root, b["collection_id"])[1])
                for e in b["entries"]:
                    if e.get("freed", False):
                        report["freed_paths_moved"].append({**_summary(e), "kind": "capture"})
                        continue
                    (report["adopted"] if e["adopt"] else report["moved"]).append(
                        {"image_id": e["image_id"], "from": e["from"], "to": e["to"]} if e["adopt"] else _summary(e))
            return report
        for b in batches:
            write_layout_journal({
                "version": JOURNAL_VERSION,
                "root": str(root),
                "collection_id": b["collection_id"],
                "entries": b["entries"],
            })
            _commit_batch(db, root, b, report)

    _repair_volume_manifests(db, root, root_forms, report, dry_run)
    return report


def _remove_stale_parts(db: Session, root: Path, report: dict, dry_run: bool) -> None:
    """Remove each stemmed volume's leftover `metadata/manifest.jsonl.part`; count them.

    An earlier version of append_manifest_lines wrote a volume's manifest
    whole to that name and renamed it over the manifest; nothing writes it
    now, so a `.part` is what a crash of that version left, and the manifest
    it would have replaced is intact. With dry_run it is counted and kept.
    One query and one stat per stemmed volume.
    """
    from capture.manifestHandler import MANIFEST_PART_NAME

    for (stem,) in db.query(Collection.filename_stem).filter(Collection.filename_stem.isnot(None)).all():
        part = root / stem / "metadata" / MANIFEST_PART_NAME
        if not part.is_file():
            continue
        report["stale_parts_removed"] += 1
        if dry_run:
            continue
        try:
            part.unlink()
            logger.warning(f"Volume layout: removed {part}, left by an interrupted manifest copy")
        except OSError as e:
            logger.error(f"Volume layout: could not remove {part}: {e}")


def _repair_volume_manifests(db: Session, root: Path, root_forms: Set[str], report: dict, dry_run: bool) -> None:
    """Copy into each migrated volume's own manifest the capture records it lacks.

    A volume qualifies when it has a stem and every image of it under the
    projects root is in its `<stem>/captures/`; a volume with images still
    out of place is left to the migration, which copies when it moves them.
    Its own `metadata/manifest.jsonl` must hold a capture record that parses
    for each capture id of its images. The missing ones (a copy that failed
    or was torn by a power cut) are copied from the other manifests, with
    their relocation records, as the migration copies (_records_to_copy), so
    the next start repairs them. A capture id no manifest holds (an upload
    whose client sent one, a capture whose record was never written) is
    logged and reported under "captures_without_record".

    Cost on a migrated unit with nothing missing: one query over the images
    of the stemmed volumes, a string comparison per row, and reading and
    parsing each stemmed volume's own manifest once; no other manifest is
    read. When something is missing, every manifest is read once more in
    one pass, whatever the number of volumes concerned. The capture ids that
    pass finds in no manifest are kept, with each manifest's path, size and
    modification time, in `volume-layout.missing-records` in the data
    directory; a later start whose missing ids and manifests are the same
    skips the pass (a walk of the tree for the manifests' paths and a stat
    of each remain) and still reports them.
    """
    from capture.manifestHandler import is_capture_record

    from app.core.integrity import read_manifest_lines
    from capture.manifestHandler import append_manifest_lines

    rows = (db.query(Collection.id, Collection.filename_stem, RecordImage.capture_id, RecordImage.file_path)
            .join(Record, Record.collection_id == Collection.id)
            .join(RecordImage, RecordImage.record_id == Record.id)
            .filter(Collection.filename_stem.isnot(None)).all())
    prefixes = tuple(r.rstrip(os.sep) + os.sep for r in root_forms)
    capture_ids: Dict[int, Set[str]] = {}
    stems: Dict[int, str] = {}
    out_of_place: Set[int] = set()
    for cid, stem, capture_id, fp in rows:
        stems[cid] = stem
        if fp and fp.startswith(prefixes) and os.path.dirname(fp) not in {
                os.path.join(r, stem, CAPTURES_DIR) for r in root_forms}:
            out_of_place.add(cid)
        if capture_id:
            capture_ids.setdefault(cid, set()).add(capture_id)
    wanted: Dict[int, Tuple[Path, Set[str]]] = {}
    for cid, ids in capture_ids.items():
        if cid in out_of_place:
            continue
        vol = root / stems[cid]
        own = vol / "metadata" / "manifest.jsonl"
        have: Set[str] = set()
        try:
            lines = read_manifest_lines(own)[0] if own.exists() else []
        except OSError as e:
            logger.error(f"Volume layout: could not read {own} to check volume {cid}'s records: {e}")
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            # A relocation or preview_digest record names the capture but is
            # not its capture record, so it does not stand in for one.
            if is_capture_record(rec) and rec.get("capture_id"):
                have.add(rec["capture_id"])
        missing = ids - have
        if missing:
            wanted[cid] = (vol, missing)
    if not wanted:
        _write_missing_memo(None)
        return
    from app.core.integrity import _manifests

    missing_now = {str(cid): sorted(ids) for cid, (_, ids) in wanted.items()}
    memo = _read_missing_memo()
    if memo is not None and memo.get("missing") == missing_now \
            and memo.get("fingerprint") == _manifest_fingerprint(_manifests(root)):
        # The same capture ids were found in no manifest by the last full
        # pass, and no manifest has changed since, so a pass would find
        # nothing either.
        for cid, (_, ids) in wanted.items():
            logger.warning(f"Volume layout: no manifest holds a capture record for capture(s) {sorted(ids)} "
                           f"of volume {cid} (no manifest changed since the last full search)")
            report["captures_without_record"][cid] = sorted(ids)
        return
    copies = _records_to_copy(root, wanted)
    for cid, (vol, missing) in wanted.items():
        found = set()
        for line in copies[cid]:
            rec = json.loads(line)
            if is_capture_record(rec):
                found.add(rec.get("capture_id"))
        nowhere = sorted(missing - found)
        if nowhere:
            logger.warning(f"Volume layout: no manifest holds a capture record for capture(s) {nowhere} "
                           f"of volume {cid}")
            report["captures_without_record"][cid] = nowhere
        if not copies[cid]:
            continue
        if not dry_run:
            try:
                append_manifest_lines(vol, copies[cid])
            except OSError as e:
                logger.error(f"Volume layout: could not repair {vol / 'metadata' / 'manifest.jsonl'}: {e}")
                report["manifest_copy_failed"].append({"collection_id": cid, "error": str(e)})
                continue
            logger.warning(f"Volume layout: copied {len(copies[cid])} missing record(s) into volume {cid}'s manifest")
        report["manifests_repaired"][cid] = len(copies[cid])
    if not dry_run:
        nowhere = report["captures_without_record"]
        _write_missing_memo({
            "version": 1,
            "missing": {str(cid): ids for cid, ids in sorted(nowhere.items())},
            # Taken after the copies above, so they do not count as a change.
            "fingerprint": _manifest_fingerprint(_manifests(root)),
        } if nowhere else None)


def _missing_memo_file() -> Path:
    return config.settings.data_dir / MISSING_RECORDS_NAME


def _manifest_fingerprint(manifests) -> List[list]:
    """Each manifest's path, size and modification time in nanoseconds, sorted by path."""
    out = []
    for m in sorted(manifests):
        try:
            st = m.stat()
        except OSError:
            continue
        out.append([str(m), st.st_size, st.st_mtime_ns])
    return out


def _read_missing_memo() -> Optional[dict]:
    """The last full search's capture ids found in no manifest, with the manifests' fingerprint; None if absent or unreadable."""
    try:
        return _read_json(_missing_memo_file(), "missing-records memo")
    except VolumeLayoutError as e:
        logger.warning(f"Volume layout: {e}; searching every manifest again")
        return None


def _write_missing_memo(payload: Optional[dict]) -> None:
    """Store the memo in the data directory (beside the journal), or remove it when nothing is missing."""
    try:
        if payload is None:
            _missing_memo_file().unlink(missing_ok=True)
        else:
            _write_json(_missing_memo_file(), payload)
    except OSError as e:
        logger.warning(f"Volume layout: could not store {_missing_memo_file()}: {e}")


def _log_report(report: dict) -> None:
    if report["moved"] or report["adopted"] or report["completed_journal"]:
        logger.warning(
            f"Volume layout: moved {len(report['moved'])} image(s) and pointed {len(report['adopted'])} row(s) at files "
            f"already in volume directories under {report['root']}"
        )
    for f in report["failed"]:
        logger.error(f"Volume layout: image {f['image_id']} not moved, left at {f['path']}: {f['error']}")
    if report["freed_paths_moved"]:
        logger.info(f"Volume layout: pointed {len(report['freed_paths_moved'])} freed file(s) (stored offsite, "
                    "not on the card) at their names in their volumes' folders; nothing was moved for them")
    for s in report["skipped"]:
        logger.warning(f"Volume layout: image {s['image_id']} not moved ({s['reason']}): {s['path']}")
    for s in report["sidecars_pending"] + report["sidecars_not_moved"]:
        logger.warning(f"Volume layout: sidecar not yet beside its master, still at {s.get('from') or s.get('path')}")
    for p in report["left_unreferenced"]:
        logger.warning(f"Volume layout: file no record references, left where it is: {p}")


def migrate_volume_layout_at_startup(db: Session) -> None:
    """Run the migration at startup.

    An override root that cannot take writes is logged and the backend
    serves: the files under it cannot be reached either way. A journal that
    cannot be read or names another root, or a failure that leaves a journal,
    is re-raised, so the backend does not serve while some rows may name a
    file's old path when the file is at its new one. Any other failure is
    logged and the backend serves.
    """
    try:
        report = migrate_volume_layout(db)
    except StorageUnavailableError as e:
        logger.error(f"Volume layout: not run, the projects root is not usable: {e}")
        return
    except VolumeLayoutError:
        logger.exception("Volume layout: refusing to serve")
        raise
    except Exception:
        logger.exception("Volume layout: failed")
        if _journal_file().exists():
            raise
        return
    _log_report(report)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.core.volume_layout",
        description="Move the files of volumes with a file name stem into <stem>/{captures,previews,thumbnails}. "
                    "Run with the backend stopped.",
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would be done; write nothing")
    args = parser.parse_args(argv)

    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        report = migrate_volume_layout(db, dry_run=args.dry_run)
    except (StorageUnavailableError, VolumeLayoutError) as e:
        print(f"Refused: {e}", file=sys.stderr)
        return 2
    finally:
        db.close()
    print(json.dumps(report, indent=2))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
