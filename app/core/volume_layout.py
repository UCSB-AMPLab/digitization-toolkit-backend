"""
Move image files from the name-based collection layout to the id-based one.

A collection's directory is `<project dir>/collection-<id>/` (see
capture.project_manager.image_output_dir). Units that captured before that
layout hold their files in `<project dir>/<secure collection name>/`, where two
collections of one name share a directory and nested collections sit beside
their parents. This module moves each file a row references into the
directory of that row's record's collection, and rewrites the row:

- Files are attributed by the rows, never by the directory they are in, so
  each image of two same-named collections sharing a directory goes to its own
  record's collection's directory.
- A RAW master's `_preview.jpg` sidecar moves with it and keeps the name its
  master's final name derives; a thumbnail moves to the collection's
  `images/thumbnails/`. A thumbnail row naming a missing file is cleared, so
  the thumbnail endpoint regenerates it beside the moved master.
- No file is ever moved onto an existing one. A master whose name is taken in
  the target (by a file on disk or by another planned move, or a file of
  another record with the same stem, which would name the same sidecar) is
  moved as `<stem>_<8 hex><suffix>`, and the row's filename follows when it
  held the stored file's name. A thumbnail whose name is taken stays where it
  is and the row's thumbnail_path is cleared.
- Only files under the active projects root move, and they stay on it. Files
  elsewhere (uploads under the data directory, files on another drive) stay
  where they are and are reported. Files no row references stay where they
  are and are reported.
- Directories left empty by the moves are removed (rmdir only), never a
  project directory or a project's own `images/`, `metadata/` or `packages/`.

The work is done one collection at a time. Before a collection's files move,
the planned moves are written durably to a journal in the data directory; the
rows are committed after the moves, and the journal is removed after the
commit. A run that finds a journal completes it first: each file is found at
its old or new path, moved if it has not been, and the rows are set to where
the files are. A run is idempotent: files already in their collection's
directory are not touched, and a second run moves nothing.

The projects root comes from storage_override.writable_projects_dir, so an
override root that cannot take writes raises StorageUnavailableError before
anything is read or created.

Triggered at backend startup (migrate_volume_layout_at_startup, after the
project rename reconciliation), which records each root it has completed in
a marker file so later startups skip the scan. Run by hand, with the backend
stopped, as `python -m app.core.volume_layout [--dry-run]`; a dry run reports
what would be done and writes nothing.
"""

import argparse
import json
import logging
import shutil
import sys
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from app.core import config
from app.core.paths import is_raw_suffix, preview_sidecar_of, resolve_within_storage
from app.core.storage_override import StorageUnavailableError
from app.models.collection import Collection
from app.models.record import Record, RecordImage

logger = logging.getLogger(__name__)

JOURNAL_NAME = "volume-layout.journal"
MARKER_NAME = "volume-layout.done"
JOURNAL_VERSION = 1

# A project directory's own subdirectories; never a collection's directory.
PROJECT_SUBDIRS = frozenset({"images", "metadata", "packages"})


class VolumeLayoutError(RuntimeError):
    """The migration cannot proceed safely, for a reason the message gives."""


# ---------------------------------------------------------------------------
# Journal and marker
# ---------------------------------------------------------------------------

def _journal_file() -> Path:
    return config.settings.data_dir / JOURNAL_NAME


def _marker_file() -> Path:
    return config.settings.data_dir / MARKER_NAME


def _write_json(path: Path, payload: dict) -> None:
    from capture.utils import atomic_write

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2)
    atomic_write(path, lambda tmp: Path(tmp).write_text(text, encoding="utf-8"))


def write_layout_journal(payload: dict) -> None:
    """Durably record the moves planned for one collection, before any of them happens."""
    _write_json(_journal_file(), payload)


def read_layout_journal() -> Optional[dict]:
    """The pending journal, or None. An unreadable journal raises VolumeLayoutError.

    It is not discarded: it is the only record of where the files of an
    interrupted batch went.
    """
    path = _journal_file()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise VolumeLayoutError(f"The volume layout journal {path} is unreadable: {e}") from e


def clear_layout_journal() -> None:
    """Remove the journal once its batch has been committed."""
    _journal_file().unlink(missing_ok=True)


def _done_roots() -> Set[str]:
    try:
        return set(json.loads(_marker_file().read_text(encoding="utf-8")).get("roots", []))
    except (OSError, ValueError):
        return set()


def _mark_done(root: Path) -> None:
    roots = _done_roots()
    roots.add(str(root))
    _write_json(_marker_file(), {"roots": sorted(roots)})


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _move(src: Path, dst: Path) -> None:
    """Move one file. Callers check that `dst` does not exist first."""
    shutil.move(str(src), str(dst))


class _Dir:
    """What a target directory holds, on disk and by planned moves: names and stems by owning record."""

    def __init__(self, path: Path, owners: Dict[Path, int]):
        self.names: Dict[str, Optional[int]] = {}
        self.stems: Dict[str, Set[Optional[int]]] = {}
        if path.is_dir():
            for p in path.iterdir():
                if p.is_file():
                    self.add(p.name, owners.get(p.resolve()))

    def add(self, name: str, record_id: Optional[int]) -> None:
        self.names[name] = record_id
        self.stems.setdefault(Path(name).stem, set()).add(record_id)

    def master_taken(self, stem: str, suffix: str, record_id: int, raw: bool) -> bool:
        if f"{stem}{suffix}" in self.names:
            return True
        if raw and preview_sidecar_of(Path(f"{stem}{suffix}")).name in self.names:
            return True
        return bool(self.stems.get(stem, set()) - {record_id})


def _plan(db: Session, root: Path) -> Tuple[List[dict], List[dict], List[str]]:
    """Plan the moves under `root` (resolved). Reads only.

    Returns (batches, skipped, left_unreferenced). Each batch is
    {"collection_id", "entries"}; each entry names one image's master move and
    the sidecar and thumbnail moves that go with it.
    """
    from capture.project_manager import image_output_dir
    from app.core.storage_ops import resolve_project

    rows = (
        db.query(RecordImage, Record.collection_id)
        .join(Record, RecordImage.record_id == Record.id)
        .order_by(Record.collection_id, Record.id, RecordImage.id)
        .all()
    )

    # Every file a row references, by resolved path, with its record: masters,
    # RAW masters' sidecars, thumbnails.
    owners: Dict[Path, int] = {}
    for img, _cid in rows:
        master = resolve_within_storage(img.file_path)
        if master is not None:
            owners[master] = img.record_id
            if is_raw_suffix(master):
                owners[preview_sidecar_of(master)] = img.record_id
        thumb = resolve_within_storage(img.thumbnail_path)
        if thumb is not None:
            owners[thumb] = img.record_id

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

    for img, cid in rows:
        if cid is None:
            continue  # a project-level record: its files belong in <project>/images/main
        src = resolve_within_storage(img.file_path) if img.file_path else None
        if src is None:
            skipped.append({"image_id": img.id, "path": img.file_path, "reason": "outside_storage"})
            continue
        if not src.is_file():
            skipped.append({"image_id": img.id, "path": str(src), "reason": "missing"})
            continue
        if not src.is_relative_to(root):
            skipped.append({"image_id": img.id, "path": str(src), "reason": "outside_projects_root"})
            continue
        if cid not in targets:
            collection = db.get(Collection, cid)
            project = resolve_project(db, collection) if collection is not None else None
            targets[cid] = image_output_dir(project.name, cid).resolve() if project is not None else None
        target = targets[cid]
        if target is None:
            skipped.append({"image_id": img.id, "path": str(src), "reason": "no_project"})
            continue
        if src.parent == target:
            continue  # already in place

        state = dir_state(target)
        raw = is_raw_suffix(src)
        key = (target, img.record_id, src.stem)
        if key in stem_for:
            stem = stem_for[key]
        else:
            stem = src.stem
            if state.master_taken(stem, src.suffix, img.record_id, raw):
                stem = f"{src.stem}_{uuid.uuid4().hex[:8]}"
            stem_for[key] = stem
        dst = target / f"{stem}{src.suffix}"
        state.add(dst.name, img.record_id)
        entry = {
            "image_id": img.id,
            "record_id": img.record_id,
            "collection_id": cid,
            "file_path": img.file_path,
            "filename": img.filename,
            "from": str(src),
            "to": str(dst),
            "sidecar_from": None,
            "sidecar_to": None,
            "thumb_from": None,
            "thumb_to": None,
            "thumb_clear": None,
        }
        sources.add(src)

        if raw:
            sidecar = preview_sidecar_of(src)
            if sidecar.is_file() and sidecar not in carried and resolve_within_storage(str(sidecar)) is not None:
                carried.add(sidecar)
                sidecar_dst = preview_sidecar_of(dst)
                entry["sidecar_from"], entry["sidecar_to"] = str(sidecar), str(sidecar_dst)
                state.names[sidecar_dst.name] = img.record_id

        if img.thumbnail_path:
            thumb = resolve_within_storage(img.thumbnail_path)
            if thumb is None or not thumb.is_file():
                entry["thumb_clear"] = "missing"
            elif thumb.is_relative_to(root):
                thumb_dir = target.parent / "thumbnails"
                if thumb.parent != thumb_dir:
                    tstate = dir_state(thumb_dir)
                    if thumb.name in tstate.names:
                        entry["thumb_clear"] = "name_taken"
                    else:
                        tstate.names[thumb.name] = img.record_id
                        entry["thumb_from"], entry["thumb_to"] = str(thumb), str(thumb_dir / thumb.name)
                        sources.add(thumb)

        batches.setdefault(cid, []).append(entry)

    left = _unreferenced_in_collection_dirs(db, root, owners, sources)
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
    `collection-<id>` for an existing collection.
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


def _listdir(path: Path) -> List[Path]:
    try:
        return sorted(path.iterdir())
    except OSError:
        return []


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


def _execute(db: Session, entries: List[dict], report: dict) -> List[Path]:
    """Carry out a batch's moves and set its rows; return the files' original paths. Does not commit.

    Each entry is settled from where its files are now, so a batch
    interrupted at any point is completed by running it again. A move that
    raises an OSError leaves that image's files and row as they were and is
    reported under "failed"; the other entries go on.
    """
    originals: List[Path] = []
    for e in entries:
        src, dst = Path(e["from"]), Path(e["to"])
        img = db.get(RecordImage, e["image_id"])
        if img is None or img.file_path not in (e["file_path"], str(dst)):
            report["skipped"].append({"image_id": e["image_id"], "path": e["from"], "reason": "row_changed"})
            continue
        try:
            at = _settle(src, dst)
        except OSError as err:
            logger.error(f"Volume layout: could not move {src} to {dst}; left at {src}: {err}")
            report["failed"].append({"image_id": e["image_id"], "path": e["from"], "error": str(err)})
            continue
        if at != dst:
            reason = "target_taken" if at == src else "missing"
            report["skipped"].append({"image_id": e["image_id"], "path": e["from"], "reason": reason})
            continue
        if img.file_path != str(dst):
            img.file_path = str(dst)
            if img.filename == src.name:
                img.filename = dst.name
        originals.append(src)
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
                report["sidecars_moved"].append({"from": e["sidecar_from"], "to": e["sidecar_to"]})
            else:
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
                report["thumbnails_moved"].append({"from": e["thumb_from"], "to": e["thumb_to"]})
            elif t_at is None:
                img.thumbnail_path = None
                report["thumbnails_cleared"].append({"image_id": e["image_id"], "reason": "missing"})
            else:
                # Left where it is; the row keeps naming it, and it still exists.
                report["thumbnails_not_moved"].append({"image_id": e["image_id"], "path": e["thumb_from"]})
    return originals


def _remove_emptied_dirs(paths: List[Path], root: Path) -> None:
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


def _commit_batch(db: Session, root: Path, entries: List[dict], report: dict) -> None:
    originals = _execute(db, entries, report)
    try:
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            logger.exception("Volume layout: rollback after a failed commit failed")
        logger.exception("Volume layout: commit failed; the journal is kept so the next run completes the batch")
        raise
    clear_layout_journal()
    _remove_emptied_dirs(originals, root)


def _new_report(root: Path, dry_run: bool) -> dict:
    return {
        "root": str(root),
        "dry_run": dry_run,
        "pending_journal": False,
        "completed_journal": False,
        "moved": [],
        "sidecars_moved": [],
        "sidecars_not_moved": [],
        "thumbnails_moved": [],
        "thumbnails_cleared": [],
        "thumbnails_not_moved": [],
        "failed": [],
        "skipped": [],
        "left_unreferenced": [],
    }


def migrate_volume_layout(db: Session, dry_run: bool = False) -> dict:
    """Move every collection's files into its id-based directory; return a report.

    With dry_run, nothing is moved, written or created: the report lists the
    moves a run would make now (a pending journal is reported under
    "pending_journal" and its entries are not replayed). Raises
    StorageUnavailableError when the active projects root is an override that
    cannot take writes, VolumeLayoutError when a pending journal names another
    root or cannot be read, and re-raises a failed commit, keeping the journal.
    """
    from app.core.storage_override import writable_projects_dir

    root = writable_projects_dir(config.settings).resolve()
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
            _commit_batch(db, root, pending.get("entries", []), report)
            report["completed_journal"] = True

    batches, skipped, left = _plan(db, root)
    report["skipped"].extend(skipped)
    report["left_unreferenced"] = left
    if dry_run:
        for b in batches:
            report["moved"].extend({"image_id": e["image_id"], "record_id": e["record_id"],
                                    "collection_id": e["collection_id"], "from": e["from"], "to": e["to"]}
                                   for e in b["entries"])
        return report

    for b in batches:
        write_layout_journal({
            "version": JOURNAL_VERSION,
            "root": str(root),
            "collection_id": b["collection_id"],
            "entries": b["entries"],
        })
        _commit_batch(db, root, b["entries"], report)

    if not report["failed"]:
        _mark_done(root)
    return report


def _log_report(report: dict) -> None:
    if report["moved"] or report["completed_journal"]:
        logger.warning(
            f"Volume layout: moved {len(report['moved'])} image(s) into id-based collection directories under {report['root']}"
        )
    for f in report["failed"]:
        logger.error(f"Volume layout: image {f['image_id']} not moved, left at {f['path']}: {f['error']}")
    for s in report["skipped"]:
        if s["reason"] != "outside_projects_root":
            logger.warning(f"Volume layout: image {s['image_id']} not moved ({s['reason']}): {s['path']}")
    for p in report["left_unreferenced"]:
        logger.warning(f"Volume layout: file no record references, left where it is: {p}")


def migrate_volume_layout_at_startup(db: Session) -> None:
    """Run the migration at startup, unless this root is already done and no journal is pending.

    An unusable override root, or a journal for another root, is logged and
    the backend serves: the files under it cannot be reached either way. Any
    other failure is logged; it is re-raised, so the backend does not serve,
    only when a journal is left, because then some rows may name a file's old
    path while the file is at its new one.
    """
    from app.core.storage_override import writable_projects_dir

    try:
        root = writable_projects_dir(config.settings).resolve()
        if not _journal_file().exists() and str(root) in _done_roots():
            return
        report = migrate_volume_layout(db)
    except StorageUnavailableError as e:
        logger.error(f"Volume layout: not run, the projects root is not usable: {e}")
        return
    except VolumeLayoutError as e:
        logger.error(f"Volume layout: not run: {e}")
        return
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
    except StorageUnavailableError as e:
        print(f"Refused: {e}", file=sys.stderr)
        return 2
    except VolumeLayoutError as e:
        print(f"Refused: {e}", file=sys.stderr)
        return 2
    finally:
        db.close()
    print(json.dumps(report, indent=2))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
