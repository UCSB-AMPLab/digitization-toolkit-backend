"""
Filesystem operations tied to project/collection/record moves and deletes.

These helpers keep the image files on disk consistent with the database when
records are re-parented (move) or their container is removed (delete), and
provide the guard that stops a delete from erasing files still referenced by surviving records.
"""

import glob
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core import config
from app.models.collection import Collection
from app.models.project import Project
from app.models.record import Record, RecordImage
from app.core.paths import is_raw_suffix, preview_sidecar_of, resolve_within_storage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Relocation records for masters renamed after capture
# ---------------------------------------------------------------------------

# (manifest root, capture id, image id, sha256, from, to): the manifest root
# is a project's directory or a stemmed volume's `<stem>/`, and the paths are
# relative to it, as capture.manifestHandler.append_relocation_record takes them.
Relocation = Tuple[Path, str, int, str, str, str]


class FixityIndexOnce:
    """The capture manifests' FixityIndex under the active projects root, built on first use and kept."""

    def __init__(self) -> None:
        self._index = None

    def __call__(self):
        if self._index is None:
            from app.core.integrity import build_fixity_index

            self._index = build_fixity_index(config.settings.projects_dir.resolve())
        return self._index


def relocation_of(img: RecordImage, src: Path, dst: Path, fixity_index: FixityIndexOnce) -> Optional[Relocation]:
    """The relocation record a master moved from `src` to `dst` needs, or None.

    Only a renamed master with a capture id needs one, as in
    app.core.volume_layout: a move that keeps the name is found by the
    integrity check's fallback by file name, scoped to the capture id. The
    record carries the image id and the sha256 the manifests hold for the
    image at `src`, looked up before the record is written. With no such
    checksum there is nothing to carry: no record, and a WARNING; the export
    fixity gate then refuses the image by name. The record goes in the
    manifest of the top directory `dst` is in under the active projects root,
    the root app.core.integrity indexes: the project's for a volume without a
    stem, the volume's own `<stem>/metadata/manifest.jsonl` for one with a
    stem. A `dst` outside the root gets none.
    """
    if not img.capture_id or src.name == dst.name:
        return None
    root = config.settings.projects_dir.resolve()
    src, dst = src.resolve(), dst.resolve()
    if not dst.is_relative_to(root) or len(dst.relative_to(root).parts) < 2:
        return None
    sha = fixity_index().expected(img.capture_id, img.id, src)
    if sha is None:
        logger.warning(f"No capture checksum is held for image {img.id} at {src}; "
                       f"no relocation record is written for its rename to {dst}")
        return None
    top = root / dst.relative_to(root).parts[0]
    return (top, img.capture_id, img.id, sha, os.path.relpath(src, top), os.path.relpath(dst, top))


def write_relocation_records(records: Iterable[Relocation]) -> None:
    """Append each relocation record to its project's or volume's capture manifest.

    A record that cannot be written is logged at ERROR with everything it
    holds and not raised: the rows and files already agree. Until the record
    is written by hand, the export fixity gate refuses the image.
    """
    from capture.manifestHandler import append_relocation_record

    for project_root, capture_id, image_id, sha, frm, to in records:
        try:
            append_relocation_record(project_root, capture_id, frm, to, image_id=image_id, sha256=sha)
        except OSError as e:
            logger.error(
                f"Could not append the relocation record of image {image_id}, capture {capture_id} "
                f"({frm} -> {to}, sha256 {sha}) to {project_root / 'metadata' / 'manifest.jsonl'}: {e}"
            )


def _is_within(path: Path, directory: Path) -> bool:
    """True if `path` is inside `directory` (both resolved)."""
    try:
        return path.resolve().is_relative_to(directory.resolve())
    except (OSError, ValueError):
        return False


def collection_and_descendants(db: Session, collection_id: int) -> List[int]:
    """Return a collection id together with all its nested child collection ids."""
    ids: List[int] = []
    seen: set[int] = set()
    stack = [collection_id]
    while stack:
        cid = stack.pop()
        if cid in seen:  # defend against a malformed cyclic collection tree
            continue
        seen.add(cid)
        ids.append(cid)
        children = db.query(Collection.id).filter(Collection.parent_collection_id == cid).all()
        stack.extend(c[0] for c in children)
    return ids


def collection_ids_under_project(db: Session, project_id: int) -> List[int]:
    """Return every collection id belonging to a project, at any nesting depth."""
    ids: List[int] = []
    tops = db.query(Collection.id).filter(Collection.project_id == project_id).all()
    for (cid,) in tops:
        ids.extend(collection_and_descendants(db, cid))
    return ids


def resolve_project(db: Session, collection: Collection) -> Optional[Project]:
    """Walk up parent collections to find the owning project, or None if there is none."""
    seen: set[int] = set()
    current: Optional[Collection] = collection
    while current is not None and current.id not in seen:
        seen.add(current.id)
        if current.project_id:
            return db.query(Project).filter(Project.id == current.project_id).first()
        if not current.parent_collection_id:
            return None
        current = db.query(Collection).filter(Collection.id == current.parent_collection_id).first()
    return None


def resolve_project_name(db: Session, collection: Collection) -> Optional[str]:
    """Walk up parent collections to find the name of the owning project."""
    project = resolve_project(db, collection)
    return project.name if project else None


def _projects_root_resolved() -> Path:
    return config.settings.projects_dir.resolve()


def image_in_old_layout(collection: Collection, file_path: Optional[str]) -> bool:
    """Whether an image of `collection` is still in the name-based layout, so a move of the volume moves it.

    Every image of a volume without a stem is, as before stems. For a volume
    with a stem, an image under the projects root but outside `<stem>/`: one
    the startup migration has not moved yet. Its files in `<stem>/`, and
    files outside the projects root (uploads in the data directory), are not.
    """
    if not collection.filename_stem:
        return True
    resolved = resolve_within_storage(file_path)
    if resolved is None:
        return False
    root = _projects_root_resolved()
    return resolved.is_relative_to(root) and not resolved.is_relative_to(root / collection.filename_stem)


def collections_in_old_layout(db: Session, collection_ids: Iterable[int]) -> List[int]:
    """The ids among `collection_ids` of volumes whose files are, wholly or partly, in the name-based layout.

    A volume without a stem, or a volume with a stem that still has an image
    under the projects root outside `<stem>/` (image_in_old_layout): a stem
    set while the backend runs moves nothing until the next startup. These
    are the volumes a project rename, a move to another project, a re-parent
    or a rename moves files of, and so holds against their exports. Two
    queries and a path check per image of the stemmed volumes among them.
    """
    ids = list(collection_ids)
    if not ids:
        return []
    cols = {c.id: c for c in db.query(Collection).filter(Collection.id.in_(ids)).all()}
    out = {cid for cid, c in cols.items() if not c.filename_stem}
    stemmed = [cid for cid, c in cols.items() if c.filename_stem]
    if stemmed:
        rows = (db.query(Record.collection_id, RecordImage.file_path)
                .join(RecordImage, RecordImage.record_id == Record.id)
                .filter(Record.collection_id.in_(stemmed)).all())
        for cid, fp in rows:
            if cid not in out and image_in_old_layout(cols[cid], fp):
                out.add(cid)
    return sorted(out)


def legacy_image_dir(db: Session, collection: Collection) -> Optional[Path]:
    """The name-based `<project>/<name>/images/main` of a volume, stem or not, or None with no project.

    Where a move puts the files of a volume still in the old layout.
    """
    from capture.project_manager import image_output_dir

    project_name = resolve_project_name(db, collection)
    return image_output_dir(project_name, collection.name) if project_name else None


def collection_root_dir(db: Session, collection: Collection) -> Optional[Path]:
    """A volume's own directory on disk, in the layout its stem decides, or None.

    `<projects root>/<stem>` for a volume with a stem (volume_root), wherever
    it sits in the catalogue. `<project dir>/<secure name>` for one without
    (collection_capture_root), or None when it has no project to place it
    under.
    """
    from capture.project_manager import collection_capture_root, volume_root

    if collection.filename_stem:
        return volume_root(collection.filename_stem)
    project_name = resolve_project_name(db, collection)
    return collection_capture_root(project_name, collection.name) if project_name else None


def collection_image_dir(db: Session, collection: Collection) -> Optional[Path]:
    """The directory a volume's masters belong in, or None (see collection_root_dir).

    `<stem>/captures` for a volume with a stem, `<project>/<name>/images/main`
    for one without: what capture writes into (image_output_dir) and what a
    move of records into the volume moves their files to.
    """
    from capture.project_manager import image_output_dir

    if collection.filename_stem:
        return image_output_dir("", stem=collection.filename_stem)
    project_name = resolve_project_name(db, collection)
    return image_output_dir(project_name, collection.name) if project_name else None


def surviving_images_under(
    db: Session,
    directory: Path,
    deleted_record_ids: Iterable[int] = (),
) -> List[Tuple[int, str]]:
    """
    Return (record_id, file_path) pairs for images stored under `directory`
    whose database records are not being deleted.

    A non-empty result means deleting `directory` would remove files that are
    still referenced by surviving database records.

    Containment is checked authoritatively: every stored path is resolved and
    tested against the resolved directory. Textual prefix matching is avoided
    because it can silently miss a symlinked or non-normalized path, and this
    guard protects against irreversible data loss on a backup-less appliance.
    Only two columns are loaded; delete is an admin-only action.
    """
    excluded = set(deleted_record_ids)

    candidates = (
        db.query(RecordImage.record_id, RecordImage.file_path)
        .filter(RecordImage.file_path.isnot(None))
        .all()
    )

    hits: List[Tuple[int, str]] = []
    for record_id, file_path in candidates:
        if record_id in excluded:
            continue
        resolved = resolve_within_storage(file_path)
        if resolved is not None and _is_within(resolved, directory):
            hits.append((record_id, file_path))
    return hits


def _raw_sidecar_to_carry(master: Path) -> Optional[Path]:
    """The `_preview.jpg` sidecar that moves or is renamed with `master`, or None.

    Only a RAW master carries one: the RAW backend writes the sidecar for it,
    and `/records/images/{id}/preview` and the thumbnail generator look for it
    (preview_sidecar_of) for a RAW master only. A JPEG master is left alone
    even when its `<stem>_preview.jpg` exists, because in a multi-format capture
    `folio.jpg` and `folio.CR2` name the same sidecar and it belongs to the
    CR2. The sidecar is returned only when it is a file inside the storage
    roots, the confinement the master gets.
    """
    if not is_raw_suffix(master):
        return None
    sidecar = resolve_within_storage(str(preview_sidecar_of(master)))
    if sidecar is None or not sidecar.is_file():
        return None
    return sidecar


def _name_taken(dst: Path, src: Path, sibling_masters: set) -> bool:
    """True if moving `src` to `dst` would clash with something already in dst's directory.

    A clash is a file at `dst`; a `<stem>_preview.jpg` there when `src` is RAW,
    which the moved master would then claim as its own preview; or a master of
    the same stem with another suffix, which names the same sidecar, so that a
    later delete or move of either would take the other's preview. A same-stem
    file that is one of `sibling_masters` (the resolved current paths of the
    record's other images, such as the CR2 of a jpg + CR2 pair moved first)
    is the pair itself and is no clash.
    """
    if dst.exists():
        return True
    if is_raw_suffix(src) and preview_sidecar_of(dst).exists():
        return True
    for other in dst.parent.glob(glob.escape(dst.stem) + ".*"):
        if other.stem == dst.stem and other != dst and other.resolve() not in sibling_masters:
            return True
    return False


def relocate_image(
    img: RecordImage,
    target_dir: Path,
    not_moved: Optional[List[str]] = None,
    moved: Optional[List[Tuple[Path, Path]]] = None,
    old_thumbnails: Optional[List[Path]] = None,
    relocations: Optional[List[Relocation]] = None,
    fixity_index: Optional[FixityIndexOnce] = None,
) -> bool:
    """Move an image's file into `target_dir` and update its stored path.

    A RAW master's `_preview.jpg` sidecar moves with it and is named and
    placed from the master's final path (preview_sidecar_of): beside the
    master in the name-based layout, in the volume's `previews/` when the
    master goes to a `captures/`, where the preview endpoint and later
    deletes look for it (NEH-267). The master is moved
    first; if the sidecar then cannot be moved, the failure is logged at WARNING,
    the sidecar's file name is appended to `not_moved` when the caller passes a
    list, and the sidecar stays in the old directory. Raising there would make
    the caller roll back, and the row would point at the old path while the
    master sat at the new one.

    When the master's name clashes in the target (see `_name_taken`), it is
    moved as `{stem}_{uuid8}{suffix}` instead; `img.filename` follows when it
    held the stored file's name, and is left alone when it holds an upload's
    original name. A renamed master with a capture id needs a relocation
    record in the capture manifest of the project or volume it moves into
    (relocation_of); it is appended
    to `relocations` when the caller passes a list, for the caller to write
    once the commit has landed, never on a move that is undone.

    The row's thumbnail is dropped (set to None) so it is regenerated on demand
    from the new location. The old thumbnail file is not deleted here: its
    path is appended to `old_thumbnails` when the caller passes a list, for
    the caller to unlink once the commit has landed, so that a rolled-back
    row still names a thumbnail that exists. Returns True if a file was
    physically moved.

    When the caller passes `moved`, each file moved is appended to it as an
    (original path, new path) pair as soon as it has moved: the master, then
    its sidecar if that followed. `undo_relocations` takes the list to put the
    files back when the caller's later work or its commit fails.
    """
    src = resolve_within_storage(img.file_path)
    if src is None or not src.exists():
        return False

    target_dir.mkdir(parents=True, exist_ok=True)
    dst = target_dir / src.name
    if src.resolve() == dst.resolve():
        return False  # already in place
    sibling_masters: set = set()
    if img.record is not None:
        for other in img.record.images:
            if other is img:
                continue
            resolved = resolve_within_storage(other.file_path)
            if resolved is not None:
                sibling_masters.add(resolved)
    sidecar = _raw_sidecar_to_carry(src)
    if _name_taken(dst, src, sibling_masters):
        dst = target_dir / f"{src.stem}_{uuid.uuid4().hex[:8]}{src.suffix}"

    shutil.move(str(src), str(dst))
    if moved is not None:
        moved.append((src, dst))
    if relocations is not None:
        record = relocation_of(img, src, dst, fixity_index or FixityIndexOnce())
        if record is not None:
            relocations.append(record)
    img.file_path = str(dst)
    if img.filename == src.name:
        img.filename = dst.name

    if sidecar is not None:
        # In a volume's stem layout the sidecar goes to `previews/`, not beside
        # the master (preview_sidecar_of), so its directory may not exist yet.
        sidecar_dst = preview_sidecar_of(dst)
        try:
            sidecar_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(sidecar), str(sidecar_dst))
            if moved is not None:
                moved.append((sidecar, sidecar_dst))
        except OSError as e:
            logger.warning(f"Moved {src} to {dst} but its preview sidecar could not follow, left at {sidecar}: {e}")
            if not_moved is not None:
                not_moved.append(sidecar.name)

    old_thumb = resolve_within_storage(img.thumbnail_path)
    img.thumbnail_path = None
    if old_thumb is not None and old_thumbnails is not None:
        old_thumbnails.append(old_thumb)

    return True


def undo_relocations(moved: List[Tuple[Path, Path]]) -> List[Path]:
    """Move relocated files back to where they were; return the original paths not restored.

    `moved` holds the (original path, new path) pairs `relocate_image` appended,
    and they are undone in reverse order, so a sidecar goes back before its
    master. A file is never moved onto an existing file. A move back that fails
    is logged at ERROR with both paths, the file is left at its new path, and
    the next pair is tried; nothing is raised, so the caller still reports the
    error that started the undo.
    """
    not_restored: List[Path] = []
    for orig, current in reversed(moved):
        if orig.exists():
            logger.error(f"Move rollback could not put {current} back at {orig}: the name is taken; left at {current}")
            not_restored.append(orig)
            continue
        try:
            shutil.move(str(current), str(orig))
        except Exception as e:
            logger.error(f"Move rollback could not put {current} back at {orig}; left at {current}: {e}")
            not_restored.append(orig)
    return not_restored


def _rollback_logged(db: Session, operation: str) -> bool:
    """Roll the session back; return whether the rollback succeeded.

    A rollback that fails is logged at ERROR and not raised: the callers roll
    back on their way to putting files back, and a failed rollback must
    neither stop that nor replace the error being reported. After a failed
    commit the result matters too, because a session that could not roll back
    may still see its own uncommitted rows.
    """
    try:
        db.rollback()
        return True
    except Exception:
        logger.exception(f"{operation}: rollback failed")
        return False


def _relocation_landed(db: Session, new_paths: dict) -> Optional[bool]:
    """Whether the rows of the moved images hold their new paths, read with a fresh query.

    `new_paths` maps image id to the file_path `relocate_image` set. Every
    moved image is checked, in one query: a transaction lands whole or not at
    all, so all rows agreeing is expected, and a mix is treated as unknown
    rather than guessed from one row. Returns True when every row holds its
    new path, False when none does, and None when they disagree, the query
    fails, or `new_paths` is empty (no row to tell by).
    """
    if not new_paths:
        return None
    try:
        rows = dict(
            db.query(RecordImage.id, RecordImage.file_path)
            .filter(RecordImage.id.in_(list(new_paths)))
            .all()
        )
    except Exception:
        logger.exception("Could not read the moved images' rows to check whether the commit landed")
        return None
    hits = [rows.get(img_id) == path for img_id, path in new_paths.items()]
    if all(hits):
        return True
    if not any(hits):
        return False
    return None


def _unlink_old_thumbnails(paths: List[Path]) -> None:
    """Unlink thumbnails whose rows a committed relocation has cleared; a failure is logged at WARNING."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"Thumbnail of a moved image could not be unlinked, left on disk: {path}: {e}")


def relocate_images_and_commit(
    db: Session,
    moves: Iterable[Tuple[RecordImage, Path]],
    not_moved: List[str],
    operation: str,
) -> None:
    """Relocate each (image, target directory) pair with `relocate_image`, then commit.

    The rows' new paths and the files' new places cannot share one
    transaction, so a failure is handled in the order the two can disagree:

    - A move raises: the session is rolled back, so every row keeps its old
      path, and `undo_relocations` moves every file already moved back to that
      path. An Exception becomes a 500; any other BaseException (an interrupt,
      SystemExit) is re-raised as it is after the undo.
    - The commit raises: it may have landed before the error (a connection
      lost after COMMIT), so after the rollback the moved images' rows are
      read again (`_relocation_landed`). If they hold the new paths, the files
      stay where they are, a WARNING is logged, and the call returns as if the
      commit had succeeded. If they hold the old paths, the files are moved
      back and a 500 is raised. If the check cannot tell, no file is moved,
      every (original, new) pair is logged at ERROR for recovery by hand, and
      a 500 says the outcome could not be determined.
      The outcome is also unknown, and handled the same way, when the
      rollback after the failed commit itself fails: the session may still
      see its own uncommitted rows, so a re-read cannot be trusted.
    - The commit raises a BaseException that is not an Exception (an
      interrupt, SystemExit): the outcome is unknown, so the rollback is
      tried (and logged if it fails), no file is moved, every (original, new)
      pair is logged at ERROR, and the original is re-raised unchanged.
    - A commit that fails when no file moved is rolled back and its error
      re-raised unchanged.

    A rollback that fails is logged and does not stop the undo. A file that
    could not be put back is logged at ERROR by `undo_relocations`, and the
    500's detail says so. Error text and paths go to the log, not to the
    detail. `operation` names the request in the log.

    Sidecars that could not follow their master are appended to `not_moved`,
    as `relocate_image` does. The moved images' old thumbnails are unlinked
    only once the commit is known to have landed; until then the files stay,
    so a rolled-back row's thumbnail_path still names an existing file. The
    relocation records of masters renamed on a clash are written to the
    capture manifest at the same point, and never when the moves are undone.
    When the outcome is unknown they are written after the (original, new)
    pairs are logged: a record names its image and its capture checksum, so
    it holds whichever way the commit went.
    """
    moved: List[Tuple[Path, Path]] = []
    old_thumbnails: List[Path] = []
    relocations: List[Relocation] = []
    fixity_index = FixityIndexOnce()
    new_paths: dict = {}
    try:
        for img, target_dir in moves:
            if relocate_image(img, target_dir, not_moved, moved, old_thumbnails, relocations, fixity_index):
                new_paths[img.id] = img.file_path
    except Exception as e:
        _rollback_logged(db, operation)
        # The rows still name the old paths, so the moves are reversed to match them.
        not_restored = undo_relocations(moved)
        if not_restored:
            logger.exception(
                f"{operation} failed on disk; {len(not_restored)} file(s) could not be put back: "
                f"{[str(p) for p in not_restored]}"
            )
            raise HTTPException(
                status_code=500,
                detail="Failed to move the image files on disk, and some files could not be put back where they were",
            ) from e
        logger.exception(f"{operation} failed on disk; the files moved so far are back where they were")
        raise HTTPException(
            status_code=500,
            detail="Failed to move the image files on disk. The files are back where they were",
        ) from e
    except BaseException:
        _rollback_logged(db, operation)
        undo_relocations(moved)
        raise

    def log_unknown_outcome() -> None:
        for orig, current in moved:
            logger.error(f"{operation}: commit outcome unknown; file left at {current}, originally at {orig}")
        # A record names its image and carries its capture checksum, so it is
        # true whether or not the rows landed: once a row names the file's new
        # path (the commit landed, or the row is fixed by hand from the lines
        # above), the integrity check resolves the file through it.
        write_relocation_records(relocations)

    try:
        db.commit()
    except Exception as e:
        rolled_back = _rollback_logged(db, operation)
        if not moved:
            raise  # no file moved (a metadata-only update, say), so nothing to put back
        landed = _relocation_landed(db, new_paths) if rolled_back else None
        if landed:
            logger.warning(
                f"{operation}: the commit raised but its rows hold the new paths, so it landed; "
                f"the files stay where they were moved: {e}"
            )
            write_relocation_records(relocations)
            _unlink_old_thumbnails(old_thumbnails)
            return
        if landed is None:
            log_unknown_outcome()
            logger.exception(f"{operation} DB commit failed and its outcome could not be determined; no file was moved back")
            raise HTTPException(
                status_code=500,
                detail="The database update failed and its outcome could not be determined; the files were left at their new paths",
            ) from e
        not_restored = undo_relocations(moved)
        if not_restored:
            logger.exception(
                f"{operation} DB commit failed; {len(not_restored)} file(s) could not be put back: "
                f"{[str(p) for p in not_restored]}"
            )
            raise HTTPException(
                status_code=500,
                detail="Files were moved on disk but the database update failed, and some files could not be put back where they were",
            ) from e
        logger.exception(f"{operation} DB commit failed; the files are back where they were")
        raise HTTPException(
            status_code=500,
            detail="The database update failed; the files are back where they were. Please retry",
        ) from e
    except BaseException:
        _rollback_logged(db, operation)
        log_unknown_outcome()
        raise

    write_relocation_records(relocations)
    _unlink_old_thumbnails(old_thumbnails)


def _utc_stamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def retire_project_manifest(project_dir: Path) -> Optional[Path]:
    """Move a project's metadata/manifest.jsonl out of its tree before the tree is removed; return where.

    It goes to `<projects root>/.retired-manifests/<project dir>-<UTC
    timestamp>-manifest.jsonl` (`-2`, `-3`... before `-manifest` if that name
    is taken), where app.core.integrity keeps reading it, with its paths
    still relative to the project directory its name carries. The manifest
    holds the capture-time checksums of every volume captured in the
    project before it had a stem, including volumes moved since to other
    projects or into their stem layout. Renamed, never copied, so nothing
    is lost if a step fails; the directories are fsynced. Returns None when
    the project has no manifest. Raises OSError.
    """
    from capture.utils import _fsync_dir
    from app.core.integrity import RETIRED_MANIFESTS_DIR

    manifest = project_dir / "metadata" / "manifest.jsonl"
    if not manifest.is_file():
        return None
    retired_dir = project_dir.parent / RETIRED_MANIFESTS_DIR
    retired_dir.mkdir(exist_ok=True)
    stamp = _utc_stamp()
    target = retired_dir / f"{project_dir.name}-{stamp}-manifest.jsonl"
    n = 2
    while target.exists():
        target = retired_dir / f"{project_dir.name}-{stamp}-{n}-manifest.jsonl"
        n += 1
    os.rename(manifest, target)
    _fsync_dir(retired_dir)
    _fsync_dir(manifest.parent)
    logger.warning(f"Retired the capture manifest of {project_dir} to {target}")
    return target


def remove_tree(directory: Path) -> None:
    """Remove a directory tree. Raises OSError on failure (never silent)."""
    if directory.exists() and directory.is_dir():
        shutil.rmtree(directory)


def remove_empty_dirs(directory: Path) -> List[Path]:
    """Remove `directory` and the directories under it that hold no file; return the files found.

    No file is removed. A directory that still holds a file, at any depth,
    stays, with the directories above it. The files are returned, in path
    order, for the caller to report. Raises OSError if a directory that holds
    no file cannot be removed.
    """
    if not directory.is_dir():
        return []
    left: List[Path] = []
    for current, dirnames, filenames in os.walk(directory, topdown=False):
        left.extend(Path(current) / f for f in filenames)
        with os.scandir(current) as entries:
            empty = next(entries, None) is None
        if empty:
            os.rmdir(current)
    return sorted(left)


def move_project_tree(old_root: Path, new_root: Path) -> bool:
    """Move an entire project directory tree from old_root to new_root.

    Renaming a project must move the whole tree as a unit (images, the manifest,
    packages, and any collection subdirs) so nothing is stranded under the old
    name. Returns True if a move happened, False if old_root does not exist.
    Raises FileExistsError if new_root already exists (the caller must not
    clobber another project's directory) and OSError on filesystem failure.
    """
    if not old_root.exists():
        return False
    if new_root.exists():
        raise FileExistsError(str(new_root))
    new_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old_root), str(new_root))
    return True


def rebase_stored_path(stored: Optional[str], old_root: Path, new_root: Path) -> Optional[str]:
    """Rewrite a stored path from under old_root to new_root.

    Returns the rewritten path, or None if `stored` is empty or does not point
    inside old_root (e.g. an uploaded image living outside the project tree).
    This is pure path math: the file has usually already been moved, so on-disk
    existence is not required.
    """
    if not stored:
        return None
    try:
        rel = Path(stored).resolve().relative_to(old_root.resolve())
    except (OSError, ValueError):
        return None
    return str(new_root / rel)


# ---------------------------------------------------------------------------
# Crash-safe project rename
#
# A rename moves the on-disk tree and then commits the new name/paths to the DB
# - two steps that cannot share one transaction. On this power-loss-prone
# appliance a crash between them would leave files at the new path while the DB
# still points at the old one, dangling every record path. write_rename_journal
# records intent durably before the move; reconcile_pending_rename replays or
# discards it at startup so recovery is mechanical, not a manual SSH repair.
# ---------------------------------------------------------------------------

def _rename_journal_file() -> Path:
    from app.core.config import settings
    return settings.data_dir / "project-rename.journal"


def write_rename_journal(project_id: int, old_name: str, new_name: str, old_root: Path, new_root: Path) -> None:
    """Durably record intent to rename, before the on-disk move happens."""
    from capture.utils import atomic_write

    path = _rename_journal_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "project_id": project_id,
        "old_name": old_name,
        "new_name": new_name,
        "old_root": str(old_root),
        "new_root": str(new_root),
    }, indent=2)
    atomic_write(path, lambda tmp: Path(tmp).write_text(payload, encoding="utf-8"))


def clear_rename_journal() -> None:
    """Remove the journal once the rename has fully committed (or been undone)."""
    try:
        _rename_journal_file().unlink(missing_ok=True)
    except OSError:
        logger.exception("[ERROR] Failed to clear project rename journal")


def read_rename_journal() -> Optional[dict]:
    path = _rename_journal_file()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("[ERROR] Project rename journal is unreadable; discarding it")
        clear_rename_journal()
        return None


def reconcile_pending_rename(db: Session) -> None:
    """Replay or discard a rename interrupted by a crash. Idempotent; safe at every startup."""
    j = read_rename_journal()
    if not j:
        return

    project = db.query(Project).filter(Project.id == j["project_id"]).first()
    old_root = Path(j["old_root"])
    new_root = Path(j["new_root"])

    if project is None:
        clear_rename_journal()
        return

    # DB already carries the new name, so the move preceded the commit and files
    # belong at new_root; repair only if a crash left them at old_root.
    if project.name == j["new_name"]:
        if old_root.exists() and not new_root.exists():
            try:
                move_project_tree(old_root, new_root)
            except OSError:
                logger.exception("[ERROR] Rename reconciliation could not move %s -> %s", old_root, new_root)
                return  # keep the journal so the next startup retries
        clear_rename_journal()
        return

    # DB still carries the old name: finish the DB side only if the files already moved.
    if project.name == j["old_name"]:
        if new_root.exists() and not old_root.exists():
            project.name = j["new_name"]
            for img in db.query(RecordImage).all():
                new_fp = rebase_stored_path(img.file_path, old_root, new_root)
                if new_fp:
                    img.file_path = new_fp
                new_tp = rebase_stored_path(img.thumbnail_path, old_root, new_root)
                if new_tp:
                    img.thumbnail_path = new_tp
            db.commit()
            logger.warning("Completed interrupted project rename %r -> %r after restart", j["old_name"], j["new_name"])
        # Otherwise the move never happened and the DB is consistent at the old name.
        clear_rename_journal()
        return

    # Name matches neither side (renamed again since): the journal is stale.
    clear_rename_journal()
