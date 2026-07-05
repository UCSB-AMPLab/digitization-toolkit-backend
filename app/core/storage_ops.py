"""
Filesystem operations tied to project/collection/record moves and deletes.

These helpers keep the image files on disk consistent with the database when
records are re-parented (move) or their container is removed (delete), and
provide the guard that stops a delete from erasing files still referenced by
surviving records.
"""

import shutil
import uuid
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.collection import Collection
from app.models.project import Project
from app.models.record import RecordImage
from app.core.paths import resolve_within_storage


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
