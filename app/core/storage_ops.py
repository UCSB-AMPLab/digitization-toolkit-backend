"""
Filesystem operations tied to project/collection/record moves and deletes.

These helpers keep the image files on disk consistent with the database when
records are re-parented (move) or their container is removed (delete), and
provide the guard that stops a delete from erasing files still referenced by surviving records.
"""

import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.collection import Collection
from app.models.project import Project
from app.models.record import Record, RecordImage
from app.core.paths import resolve_within_storage

logger = logging.getLogger(__name__)


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


def resolve_project_name(db: Session, collection: Collection) -> Optional[str]:
    """Walk up parent collections to find the name of the owning project."""
    seen: set[int] = set()
    current: Optional[Collection] = collection
    while current is not None and current.id not in seen:
        seen.add(current.id)
        if current.project_id:
            project = db.query(Project).filter(Project.id == current.project_id).first()
            return project.name if project else None
        if not current.parent_collection_id:
            return None
        current = db.query(Collection).filter(Collection.id == current.parent_collection_id).first()
    return None


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


def relocate_image(img: RecordImage, target_dir: Path) -> bool:
    """Move an image's file into `target_dir` and update its stored path.

    The thumbnail is dropped (set to None) so it is regenerated on demand from
    the new location. Returns True if a file was physically moved.
    """
    src = resolve_within_storage(img.file_path)
    if src is None or not src.exists():
        return False

    target_dir.mkdir(parents=True, exist_ok=True)
    dst = target_dir / src.name
    if src.resolve() == dst.resolve():
        return False  # already in place
    if dst.exists():
        # Avoid clobbering an unrelated file with the same name
        dst = target_dir / f"{src.stem}_{uuid.uuid4().hex[:8]}{src.suffix}"

    shutil.move(str(src), str(dst))
    img.file_path = str(dst)

    old_thumb = resolve_within_storage(img.thumbnail_path)
    img.thumbnail_path = None
    if old_thumb is not None and old_thumb.exists():
        try:
            old_thumb.unlink()
        except OSError:
            pass

    return True


def remove_tree(directory: Path) -> None:
    """Remove a directory tree. Raises OSError on failure (never silent)."""
    if directory.exists() and directory.is_dir():
        shutil.rmtree(directory)


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


def _do_renames(moves: List[Tuple[Path, Path]]) -> List[Tuple[Path, Path]]:
    """Execute a list of (src, dst) renames in order.

    If any rename fails partway through, every rename that already succeeded
    is reversed (in reverse order) before the exception is re-raised, so a
    partial filesystem failure never leaves files scattered under a mix of
    old and new names. Returns the list of renames that ended up applied
    (only relevant on the success path — on failure everything is undone).
    """
    done: List[Tuple[Path, Path]] = []
    try:
        for src, dst in moves:
            os.replace(src, dst)
            done.append((src, dst))
    except OSError:
        for src, dst in reversed(done):
            try:
                os.replace(dst, src)
            except OSError:
                logger.error(f"Renumber rollback failed to restore {dst} -> {src}; manual recovery needed")
        raise
    return done


def renumber_collection_images(db: Session, collection: Collection) -> dict:
    """Renumber every image file in a collection (documentary unit) sequentially
    from 1, zero-padded to the total capture count (min width 4).

    Each image is renumbered as an independent file — a left/right pair does
    NOT share a number, every RecordImage gets the next consecutive integer.
    Never renames based on content, only on current display order
    (Record.sequence, then creation time; within a record, left/single
    before right, then image id as a stable tie-breaker).

    All-or-nothing on the filesystem: renames go through a two-phase
    old-name -> unique temp name -> final sequential name sequence so the
    old and new numbering schemes can never collide (e.g. file "0002"
    becoming "0001" while "0001" hasn't been vacated yet), and any OSError
    during either phase unwinds every rename already applied. The caller is
    responsible for committing (or rolling back) the DB session afterwards.

    Raises ValueError if an image's file is missing on disk or the computed
    plan would produce duplicate filenames (defensive; should not happen).
    Raises OSError if a filesystem rename fails after the plan validated.
    """
    from capture.project_manager import secure_project_filename

    records = (
        db.query(Record)
        .filter(Record.collection_id == collection.id)
        .order_by(Record.sequence.nullslast(), Record.created_at)
        .all()
    )

    def image_sort_key(img: RecordImage):
        role_priority = {"left": 0, "single": 0, "overview": 0, "right": 1}
        return (role_priority.get(img.role, 0), img.id)

    images_in_order: List[RecordImage] = []
    for rec in records:
        images_in_order.extend(sorted(rec.images, key=image_sort_key))

    total = len(images_in_order)
    if total == 0:
        return {"renumbered": 0, "prefix": "", "width": 0}

    width = max(4, len(str(total)))
    signatura = ((collection.archival_metadata or {}).get("signatura") or "").strip()
    prefix = secure_project_filename(signatura) if signatura else secure_project_filename(collection.name)

    plan: List[Tuple[RecordImage, Path, Path]] = []
    for idx, img in enumerate(images_in_order, start=1):
        src = resolve_within_storage(img.file_path)
        if src is None or not src.exists():
            raise ValueError(f"Image {img.id} file is missing on disk")
        seq_str = str(idx).zfill(width)
        final_path = src.parent / f"{prefix}_{seq_str}{src.suffix}"
        plan.append((img, src, final_path))

    if len({str(p) for _, _, p in plan}) != len(plan):
        raise ValueError("Renumber plan produced duplicate filenames")

    # Phase 1: move every source to a unique temp name so the old and new
    # numbering schemes never collide with each other.
    to_temp = [(src, src.parent / f".renumber_{uuid.uuid4().hex}{src.suffix}") for _, src, _ in plan]
    _do_renames(to_temp)

    # Phase 2: move each temp file to its final sequential name.
    to_final = [(temp, final) for (_, temp), (_, _, final) in zip(to_temp, plan)]
    try:
        _do_renames(to_final)
    except OSError:
        # Phase 2 failed partway through: restore everything to its
        # ORIGINAL name (not just undo phase 2), whether a given file is
        # still sitting under its temp name or already at its final name.
        for (orig, temp), (_, final) in zip(to_temp, to_final):
            try:
                if final.exists():
                    os.replace(final, orig)
                elif temp.exists():
                    os.replace(temp, orig)
            except OSError:
                logger.error(f"Renumber rollback could not restore {orig}; manual recovery needed")
        raise

    for idx, (img, _, final_path) in enumerate(plan, start=1):
        old_thumb = resolve_within_storage(img.thumbnail_path)
        img.filename = final_path.name
        img.file_path = str(final_path)
        img.thumbnail_path = None  # regenerated on demand, same convention as relocate_image
        img.sequence = idx
        if old_thumb is not None and old_thumb.exists():
            try:
                old_thumb.unlink()
            except OSError:
                pass

    return {"renumbered": len(plan), "prefix": prefix, "width": width}
