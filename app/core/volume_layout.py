"""
Move image files from the name-based collection layout to the id-based one.

A collection's directory is `<project dir>/collection-<id>/` (see
capture.project_manager.image_output_dir). Units that captured before that
layout hold their files in `<project dir>/<secure collection name>/`, where two
collections of one name share a directory and nested collections sit beside
their parents. This module moves each file a row references into the
directory of that row's record's collection, and rewrites the row.

What is moved, and how:

- The rows decide what to do. A row of a collection's record whose stored
  path is under the active projects root but not in its collection's
  directory is out of place; nothing else is looked at. Files are attributed
  by the rows, never by the directory they are in, so each image of two
  same-named collections sharing a directory goes to its own collection.
- A row whose old path is gone but whose file is already in its collection's
  directory, under its own name or under the name the migration gives a
  clashing file (`<stem>_img<image id>`), and that no other row names, is
  pointed at that file, but only when the file's sha256 matches the one its
  capture record in the project manifest holds (the only checksum the
  system keeps for an image). This is the state after a database restored
  from before the migration (rollback-update.sh) on files the migration
  moved. A row with no such checksum, or whose candidate does not match, is
  reported as missing and the candidate as a file no row references.
- A RAW master's `_preview.jpg` sidecar moves with it and keeps the name its
  master's final name derives. A sidecar that cannot follow is recorded in a
  second file in the data directory, retried at every run, and reported until
  it has moved.
- A thumbnail moves to the collection's `images/thumbnails/`. A thumbnail row
  naming a missing file is cleared, so the thumbnail endpoint regenerates it
  beside the moved master. A thumbnail whose name is taken in the target
  stays where it is, its row is cleared, and it is reported with the files no
  row references.
- No file is ever moved onto an existing one. Names are compared casefolded,
  because exFAT, vfat and APFS by default treat names differing only in case
  as one. A master whose name is taken in the target (by a file on disk or by
  another planned move, or by a file of another record with the same stem,
  which would name the same sidecar) is moved as `<stem>_img<image id>`
  (`_2`, `_3`... after that if that is taken too), and the row's filename
  follows when it held the stored file's name. The suffix comes from the
  image id, so a dry run and the run after it choose the same names. A
  renamed master with a capture id gets a relocation record in its project's
  capture manifest before it moves, so the capture-time fixity check follows
  it to its new name.
- Only files under the active projects root move, and they stay on it. Files
  elsewhere (uploads under the data directory, files on another drive) are
  not looked at. Files no row references stay where they are and are
  reported.
- Directories left empty by the moves are removed (rmdir only), never a
  project directory or a project's own `images/`, `metadata/` or `packages/`.

Crash safety. The work is done one collection at a time. Before a
collection's files move, the planned moves are written durably to a journal
in the data directory. After the moves, the source and target directories
are fsynced, then the rows are committed, then the journal is removed. A run
that finds a journal completes it first: each file is found at its old or new
path, moved if it has not been, and the rows are set to where the files are.
A run is idempotent: a second run finds no row out of place and moves
nothing.

The projects root comes from storage_override.writable_projects_dir, so an
override root that cannot take writes raises StorageUnavailableError before
anything is read or created.

Triggered at every backend startup (migrate_volume_layout_at_startup, after
the project rename reconciliation). On a migrated unit that costs three
queries and a string comparison per image row, with no filesystem access per
row. Run by hand, with the backend stopped, as
`python -m app.core.volume_layout [--dry-run]`; a dry run reports what would
be done and writes nothing.
"""

import argparse
import errno
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from app.core import config
from app.core.paths import is_raw_suffix, preview_sidecar_of, resolve_within_storage
from app.core.storage_override import StorageUnavailableError
from app.models.collection import Collection
from app.models.project import Project
from app.models.record import Record, RecordImage

logger = logging.getLogger(__name__)

JOURNAL_NAME = "volume-layout.journal"
SIDECARS_NAME = "volume-layout.sidecars"
JOURNAL_VERSION = 2

# A project directory's own subdirectories; never a collection's directory.
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
    """Durably record the moves planned for one collection, before any of them happens."""
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
    """Each collection's id-based image directory, as the strings a stored path's directory may equal.

    Two queries (projects, collections); the tree is walked in memory.
    """
    from capture.project_manager import collection_dir_name, secure_project_filename

    projects = dict(db.query(Project.id, Project.name).all())
    cols = {cid: (parent, pid) for cid, parent, pid in
            db.query(Collection.id, Collection.parent_collection_id, Collection.project_id).all()}

    def project_name(cid: int) -> Optional[str]:
        seen: Set[int] = set()
        while cid in cols and cid not in seen:
            seen.add(cid)
            parent, pid = cols[cid]
            if pid:
                return projects.get(pid)
            if not parent:
                return None
            cid = parent
        return None

    out: Dict[int, Set[str]] = {}
    for cid in cols:
        name = project_name(cid)
        if name is None:
            continue
        tail = os.path.join(secure_project_filename(name), collection_dir_name(cid), "images", "main")
        out[cid] = {os.path.join(r, tail) for r in root_forms}
    return out


def _out_of_place(db: Session, root_forms: Set[str]) -> List[Tuple[int, int]]:
    """(image id, collection id) of every collection image stored under the root but not in its collection's directory.

    One query over the image rows (id, file_path, collection_id) and a string
    comparison each; nothing on disk is touched.
    """
    expected = _expected_dirs(db, root_forms)
    prefixes = tuple(r.rstrip(os.sep) + os.sep for r in root_forms)
    rows = (
        db.query(RecordImage.id, RecordImage.file_path, Record.collection_id)
        .join(Record, RecordImage.record_id == Record.id)
        .filter(Record.collection_id.isnot(None))
        .all()
    )
    out: List[Tuple[int, int]] = []
    for iid, fp, cid in rows:
        if not fp or not fp.startswith(prefixes):
            continue
        if os.path.dirname(fp) in expected.get(cid, ()):
            continue
        out.append((iid, cid))
    return out


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

class _Dir:
    """What a target directory holds, on disk and by planned moves: casefolded names and stems by owning record."""

    def __init__(self, path: Path, owners: Dict[Path, int]):
        self.names: Dict[str, Optional[int]] = {}
        self.stems: Dict[str, Set[Optional[int]]] = {}
        for p in _listdir(path):
            if p.is_file():
                self.add(p.name, owners.get(p.resolve()))

    def add(self, name: str, record_id: Optional[int]) -> None:
        self.names[name.casefold()] = record_id
        self.stems.setdefault(Path(name).stem.casefold(), set()).add(record_id)

    def has(self, name: str) -> bool:
        return name.casefold() in self.names

    def master_taken(self, stem: str, suffix: str, record_id: int, raw: bool) -> bool:
        if self.has(f"{stem}{suffix}"):
            return True
        if raw and self.has(preview_sidecar_of(Path(f"{stem}{suffix}")).name):
            return True
        return bool(self.stems.get(stem.casefold(), set()) - {record_id})


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


def _capture_checksum(index: Tuple[dict, dict], capture_id: Optional[str], src: Path) -> Optional[str]:
    """The capture-time sha256 the manifest holds for the file a row stored at `src`, or None.

    The capture manifest is the only checksum the system holds for an image
    (the image row has no checksum column), so a row with no capture id, or
    whose capture record does not list the file, has none. The lookup is the
    one app.core.integrity makes: by resolved path, then by file name, after
    the relocation records have been followed.
    """
    if not capture_id:
        return None
    by_path, by_name = index
    return by_path.get(capture_id, {}).get(str(src)) or by_name.get(capture_id, {}).get(src.name)


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


def _plan(db: Session, root: Path, out_of_place: List[Tuple[int, int]]) -> Tuple[List[dict], List[dict], List[str]]:
    """Plan the work for the out-of-place rows under `root` (resolved). Reads only.

    Returns (batches, skipped, left_unreferenced). Each batch is
    {"collection_id", "entries"}; each entry names one image's master move (or,
    with "adopt", the file its row is pointed at) and the sidecar and
    thumbnail moves that go with it.
    """
    from capture.project_manager import image_output_dir
    from app.core.storage_ops import resolve_project

    owners = _owners(db)
    manifest_index: List[Tuple[dict, dict]] = []  # built on the first row whose file is missing
    refused: Set[Path] = set()
    targets: Dict[int, Optional[Path]] = {}
    dirs: Dict[Path, _Dir] = {}
    stem_for: Dict[Tuple[Path, int, str], str] = {}
    carried: Set[Path] = set()
    batches: Dict[int, List[dict]] = {}
    skipped: List[dict] = []
    sources: Set[Path] = set()

    def dir_state(path: Path) -> _Dir:
        if path not in dirs:
            dirs[path] = _Dir(path, owners)
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
            project = resolve_project(db, collection) if collection is not None else None
            targets[cid] = image_output_dir(project.name, cid).resolve() if project is not None else None
        target = targets[cid]
        if target is None:
            skipped.append({"image_id": iid, "path": str(src), "reason": "no_project"})
            continue
        if src.parent == target:
            continue  # stored through another spelling of the same directory

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
        }
        thumb = resolve_within_storage(img.thumbnail_path) if img.thumbnail_path else None
        thumb_dir = target.parent / "thumbnails"

        if not src.is_file():
            if not manifest_index:
                from app.core.integrity import _build_manifest_index
                manifest_index.append(_build_manifest_index(root))
            checksum = _capture_checksum(manifest_index[0], img.capture_id, src)
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
        key = (target, img.record_id, src.stem.casefold())
        if key in stem_for:
            stem = stem_for[key]
        else:
            stem = src.stem
            if state.master_taken(stem, src.suffix, img.record_id, raw):
                stem = next(s for s in _suffixed_stems(src.stem, iid)
                            if not state.master_taken(s, src.suffix, img.record_id, raw))
            stem_for[key] = stem
        dst = target / f"{stem}{src.suffix}"
        state.add(dst.name, img.record_id)
        entry["to"] = str(dst)
        sources.add(src)

        if raw:
            sidecar = preview_sidecar_of(src)
            if sidecar.is_file() and sidecar not in carried and resolve_within_storage(str(sidecar)) is not None:
                carried.add(sidecar)
                sidecar_dst = preview_sidecar_of(dst)
                entry["sidecar_from"], entry["sidecar_to"] = str(sidecar), str(sidecar_dst)
                state.add(sidecar_dst.name, img.record_id)

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
    # A refused candidate sits in an id-based directory, which the scan above
    # does not walk, so it is added here unless a row has since claimed it.
    left += [str(p) for p in sorted(refused) if p not in owners and str(p) not in left]
    return ([{"collection_id": cid, "entries": e} for cid, e in batches.items()], skipped, left)


def _collection_top(path: Path, root: Path) -> Optional[Path]:
    """`<root>/<project>/<top>` for a path under a collection directory, or None."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return None
    if len(parts) < 3 or parts[1] in PROJECT_SUBDIRS:
        return None
    return root / parts[0] / parts[1]


def _unreferenced_in_collection_dirs(db: Session, root: Path, owners: Dict[Path, int], sources: Set[Path]) -> List[str]:
    """Files no row references, in the name-based collection directories and in those files move out of.

    A name-based directory is any directory directly under a project's that is
    neither one of the project's own (images, metadata, packages) nor
    `collection-<id>` for an existing collection. The directories files move
    out of are scanned whatever their name.
    """
    from capture.project_manager import collection_dir_name

    id_dirs = {collection_dir_name(cid) for (cid,) in db.query(Collection.id).all()}
    tops: Set[Path] = set()
    # A directory that cannot be listed (a drive's lost+found, say) is passed over.
    for project_dir in _listdir(root):
        if not project_dir.is_dir():
            continue
        for top in _listdir(project_dir):
            if top.is_dir() and top.name not in PROJECT_SUBDIRS and top.name not in id_dirs:
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


def _free_name(e: dict, src: Path, dst: Path) -> Path:
    """A name in dst's directory for a master whose planned name was taken after planning."""
    for stem in _suffixed_stems(src.stem, e["image_id"]):
        cand = dst.with_name(f"{stem}{src.suffix}")
        if not _taken_in(cand.parent, cand.name) and not (
                is_raw_suffix(src) and _taken_in(cand.parent, preview_sidecar_of(cand).name)):
            return cand
    raise AssertionError("unreachable")


def _record_relocation(root: Path, e: dict, src: Path, dst: Path) -> None:
    """Append a relocation record for a renamed master with a capture id to its project's manifest."""
    if not e.get("capture_id") or src.name == dst.name:
        return
    from capture.manifestHandler import append_relocation_record

    project_root = root / dst.relative_to(root).parts[0]
    append_relocation_record(project_root, e["capture_id"],
                             os.path.relpath(src, project_root), os.path.relpath(dst, project_root))


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

        if src.exists() and dst.exists():
            new_dst = _free_name(e, src, dst)
            logger.warning(f"Volume layout: {dst} was taken after planning; moving {src} to {new_dst}")
            e["to"] = str(new_dst)
            if e["sidecar_from"]:
                e["sidecar_to"] = str(preview_sidecar_of(new_dst))
            write_layout_journal({"version": JOURNAL_VERSION, "root": str(root),
                                  "collection_id": batch["collection_id"], "entries": batch["entries"]})
            dst = new_dst
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


def _commit_batch(db: Session, root: Path, batch: dict, report: dict) -> None:
    pending = _read_pending_sidecars()
    before = len(pending)
    originals, touched = _execute(db, root, batch, report, pending)
    # The renames are durable before the rows that name their results are.
    for d in sorted(touched):
        _fsync_dir(d)
    if len(pending) != before:
        _write_pending_sidecars(pending)
    _commit(db, f"the batch of collection {batch['collection_id']} (the journal is kept for the next run)")
    clear_layout_journal()
    _remove_emptied_dirs(originals, root)


def _retry_sidecars(db: Session, root: Path, report: dict, dry_run: bool) -> None:
    """Move each sidecar a previous run could not move to beside its master's current file.

    The sidecar's old and new paths were recorded when its move failed,
    because only then are both known: the capture manifest holds capture-time
    paths, which a later relocation makes stale, and a sidecar need not be in
    it. The entry is keyed by image id. When the master's row names another
    file than the one recorded (a renumber renamed it while the sidecar
    waited), the sidecar goes beside that file, under the name its stem
    derives (preview_sidecar_of), as a renumber would have named it, and the
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
        "sidecars_moved": [],
        "sidecars_not_moved": [],
        "sidecars_pending": [],
        "thumbnails_moved": [],
        "thumbnails_cleared": [],
        "thumbnails_not_moved": [],
        "failed": [],
        "skipped": [],
        "left_unreferenced": [],
    }


def _summary(entry: dict) -> dict:
    return {"image_id": entry["image_id"], "record_id": entry["record_id"],
            "collection_id": entry["collection_id"], "from": entry["from"], "to": entry["to"]}


def migrate_volume_layout(db: Session, dry_run: bool = False) -> dict:
    """Move every collection's out-of-place files into its id-based directory; return a report.

    In order: complete a pending journal, retry the sidecars that could not
    follow their masters, find the out-of-place rows, and move their files a
    collection at a time. With dry_run, nothing is moved, written or created:
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
            logger.warning(f"Volume layout: completing the interrupted batch of collection {pending.get('collection_id')}")
            _commit_batch(db, root, pending, report)
            report["completed_journal"] = True

    _retry_sidecars(db, root, report, dry_run)

    out_of_place = _out_of_place(db, root_forms)
    report["out_of_place"] = len(out_of_place)
    if not out_of_place:
        return report

    batches, skipped, left = _plan(db, root, out_of_place)
    report["skipped"].extend(skipped)
    report["left_unreferenced"] += [p for p in left if p not in report["left_unreferenced"]]
    if dry_run:
        for b in batches:
            for e in b["entries"]:
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
    return report


def _log_report(report: dict) -> None:
    if report["moved"] or report["adopted"] or report["completed_journal"]:
        logger.warning(
            f"Volume layout: moved {len(report['moved'])} image(s) and pointed {len(report['adopted'])} row(s) at files "
            f"already in id-based collection directories under {report['root']}"
        )
    for f in report["failed"]:
        logger.error(f"Volume layout: image {f['image_id']} not moved, left at {f['path']}: {f['error']}")
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
        description="Move collection files into id-based directories (collection-<id>). Run with the backend stopped.",
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
