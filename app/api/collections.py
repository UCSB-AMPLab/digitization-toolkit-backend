from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from typing import List, Optional
from datetime import datetime
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy import and_, or_, select, func, update
from sqlalchemy.exc import IntegrityError
import logging

from app.api.deps import get_db_dependency
from app.api.auth import get_current_user, RoleChecker
from app.models.collection import STEM_RESERVED_MESSAGE, Collection, stem_directory_collision
from app.models.project import Project
from app.models.record import ImmutableFieldError, Record, RecordImage, RecordStatusChange
from app.models.user import User
from app.schemas.collection import CollectionCreate, CollectionRead, CollectionUpdate, CollectionWithChildren
from app.schemas.record import RecordStatusChangeRead, ReorderRecords
# The one place a status transition is validated and applied, so the
# promotion invariant cannot be skipped by reaching a record from here
# (NEH-260). cameras.py imports _supersede_current_images the same way.
from app.api.records import _apply_status_change, _assert_promotable, is_movable
from app.core.audit import log_event
from app.core.record_status import record_bulk_status_changes
from app.core.config import settings
from app.core.db_errors import integrity_conflict
from app.core.export_jobs import ExportBlockedError, export_jobs
from app.core.export_service import run_export, workflow_blockers
from app.core.storage_ops import (
    collection_and_descendants,
    collection_image_dir,
    collection_root_dir,
    relocate_images_and_commit,
    remove_empty_dirs,
    renumber_collection_images,
    restore_original_names,
    surviving_images_under,
)

router = APIRouter()
logger = logging.getLogger(__name__)

allow_contributor = RoleChecker(["admin", "operator"])
allow_read_only = RoleChecker(["admin", "operator", "reviewer"])

EXPORT_IN_SUBTREE_MESSAGE = (
    "An export of this collection, or of a collection inside it, is running. "
    "Wait for it to finish, then try again."
)


def _locked_collection_query(db: Session, collection_id: int):
    """The collection row under SELECT ... FOR UPDATE (no lock clause on SQLite)."""
    return db.query(Collection).filter(Collection.id == collection_id).with_for_update()


def _immutable_message(exc: ImmutableFieldError) -> str:
    """The client-facing message for a refused identity change, without the stored values."""
    text = str(exc)
    if "filename_stem" in text:
        return "The volume has already captured images, so its file name stem cannot be changed."
    return "The volume's external identifier is written once and cannot be changed."


@router.post("/", response_model=CollectionRead, status_code=201)
def create_collection(
    payload: CollectionCreate,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    """
    Create a new collection.
    
    Must specify either project_id (for top-level collection) OR parent_collection_id (for nested subcollection).
    """
    # Validate that exactly one parent is specified
    if payload.project_id is None and payload.parent_collection_id is None:
        raise HTTPException(status_code=400, detail="Must specify either project_id or parent_collection_id")
    
    if payload.project_id is not None and payload.parent_collection_id is not None:
        raise HTTPException(status_code=400, detail="Cannot specify both project_id and parent_collection_id")
    
    # Validate parent exists
    if payload.project_id is not None:
        project = db.query(Project).filter(Project.id == payload.project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail=f"Project {payload.project_id} not found")
    
    if payload.parent_collection_id is not None:
        parent = db.query(Collection).filter(Collection.id == payload.parent_collection_id).first()
        if not parent:
            raise HTTPException(status_code=404, detail=f"Parent collection {payload.parent_collection_id} not found")
    
    collection = Collection(
        name=payload.name,
        description=payload.description,
        collection_type=payload.collection_type,
        archival_metadata=payload.archival_metadata,
        project_id=payload.project_id,
        parent_collection_id=payload.parent_collection_id,
        filename_stem=payload.filename_stem,
        external_id=payload.external_id,
        date=payload.date,
        date_start=payload.date_start,
        date_end=payload.date_end,
        sequence=payload.sequence,
        custom_attributes=payload.custom_attributes,
        created_by=payload.created_by or current_user.username
    )
    
    # A stem may not name a project's directory (app.models.collection).
    if payload.filename_stem is not None:
        message = stem_directory_collision(db, stem=payload.filename_stem)
        if message:
            raise HTTPException(status_code=409, detail={"message": message})

    try:
        db.add(collection)
        db.commit()
    except IntegrityError as e:
        # A duplicate filename_stem or external_id, or a CHECK on the row.
        db.rollback()
        raise integrity_conflict(e)
    db.refresh(collection)
    log_event(db, level="INFO", category="activity", action="collection_created",
              actor=current_user.username, subject=collection.name)
    return CollectionRead.model_validate(collection)


@router.get("/", response_model=List[CollectionRead])
def list_collections(
    project_id: Optional[int] = Query(None, description="Filter by project"),
    parent_collection_id: Optional[int] = Query(None, description="Filter by parent collection (use 'null' for top-level)"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency),
):
    """
    List collections with optional filters.
    
    - No filters: Returns all collections
    - project_id: Returns all collections under this project (top-level only)
    - parent_collection_id: Returns all subcollections of this collection
    """
    query = db.query(Collection)
    
    if project_id is not None:
        query = query.filter(Collection.project_id == project_id)
    
    if parent_collection_id is not None:
        query = query.filter(Collection.parent_collection_id == parent_collection_id)
    
    # Deterministic order: without an ORDER BY, Postgres returns heap order,
    # which shifts when rows are updated. Also required for stable
    # skip/limit pagination.
    items = query.order_by(Collection.id).offset(skip).limit(limit).all()

    # Bulk-count records per collection in one query (avoids N+1 — the
    # project detail page lists every collection and needs each one's
    # count for the "No. of images/records" column, NEH-179).
    # The same pass takes MAX(modified_at): asking per collection would be the
    # N+1 this query exists to avoid.
    counts_by_id: dict[int, int] = {}
    last_activity_by_id: dict[int, datetime] = {}
    if items:
        rows = (
            db.query(
                Record.collection_id,
                func.count(Record.id),
                func.max(Record.modified_at),
            )
            .filter(Record.collection_id.in_([c.id for c in items]))
            .group_by(Record.collection_id)
            .all()
        )
        for collection_id, count, last_activity in rows:
            counts_by_id[collection_id] = count
            last_activity_by_id[collection_id] = last_activity

    results = []
    for i in items:
        r = CollectionRead.model_validate(i)
        r.record_count = counts_by_id.get(i.id, 0)
        # Left as None for a collection with no records: "never worked on" is
        # not the same as a date, and a caller listing volumes in progress needs
        # to tell them apart.
        r.last_activity_at = last_activity_by_id.get(i.id)
        results.append(r)
    return results


@router.get("/count")
def count_collections(
    project_id: Optional[int] = Query(None, description="Filter by project"),
    parent_collection_id: Optional[int] = Query(None, description="Filter by parent collection id"),
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency),
):
    """Return the total number of collections matching the given filters."""
    query = db.query(Collection)
    if project_id is not None:
        query = query.filter(Collection.project_id == project_id)
    if parent_collection_id is not None:
        query = query.filter(Collection.parent_collection_id == parent_collection_id)
    return {"count": query.count()}


@router.get("/{collection_id}", response_model=CollectionRead)
def get_collection(
    collection_id: int,
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency)
):
    """Get a specific collection by ID."""
    collection = db.query(Collection).filter(Collection.id == collection_id).first()
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")
    return CollectionRead.model_validate(collection)


@router.get("/{collection_id}/hierarchy", response_model=CollectionWithChildren)
def get_collection_hierarchy(
    collection_id: int,
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency)
):
    """
    Get collection with nested child collections (full hierarchy tree).
    Includes record counts at each level.
    """
    collection = db.query(Collection).options(
        selectinload(Collection.child_collections)
    ).filter(Collection.id == collection_id).first()
    
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")
    
    # Count records in this collection (collection_id lives on Record)
    record_count = db.query(func.count(Record.id)).filter(
        Record.collection_id == collection_id
    ).scalar()
    
    result = CollectionWithChildren.model_validate(collection)
    result.record_count = record_count
    return result


@router.patch("/{collection_id}", response_model=CollectionRead)
def update_collection(
    collection_id: int,
    payload: CollectionUpdate,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    """
    Update a collection.
    
    Can update name, description, type, metadata, or move to different parent collection.

    A volume with a file name stem keeps its files in `<stem>/` under the
    projects root, outside any project, so a rename or a re-parent moves none
    of them. A volume without a stem keeps the name-based layout,
    `<project>/<name>/images/main/`, so a rename or a re-parent moves the
    files of each such volume in the subtree whose directory changes. That
    is refused with 409 while an export of any of those volumes is queued or
    running. Other edits are not refused.
    """
    # Under the row lock, so that a concurrent PATCH setting a first stem or
    # external_id commits before this one reads the row, and the model's
    # write-once guards compare against the committed value.
    collection = _locked_collection_query(db, collection_id).first()
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")
    
    # Validate new parent if specified
    if payload.parent_collection_id is not None:
        # Prevent circular references
        if payload.parent_collection_id == collection_id:
            raise HTTPException(status_code=400, detail="Collection cannot be its own parent")
        
        new_parent = db.query(Collection).filter(Collection.id == payload.parent_collection_id).first()
        if not new_parent:
            raise HTTPException(status_code=404, detail=f"Parent collection {payload.parent_collection_id} not found")
        
        # Check if new parent is a descendant of this collection (would create cycle)
        current = new_parent
        while current.parent_collection_id is not None:
            if current.parent_collection_id == collection_id:
                raise HTTPException(status_code=400, detail="Cannot create circular collection hierarchy")
            current = db.query(Collection).filter(Collection.id == current.parent_collection_id).first()
            if not current:
                break
    
    # A stem may not name a project's directory (app.models.collection).
    new_stem = payload.model_dump(exclude_unset=True).get("filename_stem")
    if new_stem is not None and new_stem != collection.filename_stem:
        message = stem_directory_collision(db, stem=new_stem)
        if message:
            raise HTTPException(status_code=409, detail={"message": message})

    # Only volumes without a stem have a directory named after the volume
    # and its project, so only they can have their files moved by a rename or
    # a re-parent; a volume with a stem keeps its files where they are.
    update_data = payload.model_dump(exclude_unset=True)
    layout_changed = (
        ("name" in update_data and update_data["name"] != collection.name)
        or ("parent_collection_id" in update_data
            and update_data["parent_collection_id"] != collection.parent_collection_id)
    )
    not_moved: List[str] = []
    moves: List = []
    held_ids: List[int] = []
    old_dirs: dict = {}
    if layout_changed:
        subtree = db.query(Collection).filter(
            Collection.id.in_(collection_and_descendants(db, collection.id))).all()
        old_dirs = {c.id: collection_image_dir(db, c) for c in subtree if not c.filename_stem}
        # Their files are read by path by an export of any of them, so they
        # are held against exports, before anything is changed, until the
        # files have moved and the commit is done.
        held_ids = sorted(old_dirs)
    with export_jobs.hold_for_file_changes(held_ids, EXPORT_IN_SUBTREE_MESSAGE):
        # Update fields. A change to external_id or filename_stem that the
        # model refuses (written once; fixed by the first image) is a 409.
        try:
            for field, value in update_data.items():
                setattr(collection, field, value)
        except ImmutableFieldError as e:
            db.rollback()
            raise HTTPException(status_code=409, detail={"message": _immutable_message(e)})

        # A collection has exactly one parent: nesting it clears its direct project link
        if collection.parent_collection_id is not None:
            collection.project_id = None

        # Each volume without a stem whose directory the change moved has
        # its records' files moved to the new directory. One given a stem in
        # this same request is left to the startup migration.
        if held_ids:
            targets = {}
            for c in db.query(Collection).filter(Collection.id.in_(held_ids)).all():
                if c.filename_stem:
                    continue
                new_dir = collection_image_dir(db, c)
                if new_dir is not None and new_dir != old_dirs.get(c.id):
                    targets[c.id] = new_dir
            if targets:
                records = db.query(Record).filter(Record.collection_id.in_(list(targets))).all()
                for r in records:
                    moves.extend((img, targets[r.collection_id]) for img in r.images)

        # Flushed before any file is moved, so that a duplicate stem or
        # external_id, or an end date before the stored start, is refused
        # while nothing on disk has changed.
        try:
            db.flush()
        except IntegrityError as e:
            db.rollback()
            raise integrity_conflict(e)

        # A failed move or commit puts every file already moved back.
        relocate_images_and_commit(db, moves, not_moved, f"Update of collection {collection_id}")
    db.refresh(collection)
    if not_moved:
        # Preview sidecars that stayed in the old directory (see relocate_image).
        # The key is outside CollectionRead, so this body bypasses response_model.
        body = jsonable_encoder(CollectionRead.model_validate(collection))
        body["not_moved"] = not_moved
        return JSONResponse(content=body)
    return CollectionRead.model_validate(collection)


@router.post("/{collection_id}/move-records", status_code=200)
def move_collection_records(
    collection_id: int,
    target_collection_id: int = Query(..., description="ID of the target collection to move records into"),
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    """
    Move all records from one collection to another, relocating their image
    files on disk so the moved records keep pointing at existing files.
    """
    source = db.query(Collection).filter(Collection.id == collection_id).first()
    if not source:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")

    target = db.query(Collection).filter(Collection.id == target_collection_id).first()
    if not target:
        raise HTTPException(status_code=404, detail=f"Target collection {target_collection_id} not found")

    # `<stem>/captures` for a target with a stem, its name-based directory otherwise
    target_dir = collection_image_dir(db, target)
    if target_dir is None:
        raise HTTPException(status_code=422, detail="Target collection is not attached to a project")

    # Locked, because the refusal below reads each record's status and the
    # relocation that follows is a filesystem move no transaction takes back.
    records = (
        db.query(Record)
        .filter(Record.collection_id == collection_id)
        .order_by(Record.id)
        .with_for_update(of=Record)
        .all()
    )

    # Same rule as the single-record move (NEH-260): a record that has been
    # handed over is not moved out from under the review it is waiting for.
    # All or nothing, because moving some of a collection's records and
    # refusing the rest leaves the operator with no way to tell which went.
    handed_over = [r.id for r in records if not is_movable(r)]
    if handed_over:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"Cannot move: {len(handed_over)} record(s) have been handed over "
                    f"for review: {handed_over}. Take this collection back first."
                ),
                "blocking_record_ids": handed_over,
            },
        )

    not_moved: List[str] = []
    moves: List = []
    for r in records:
        r.collection_id = target_collection_id
        moves.extend((img, target_dir) for img in r.images)
    # Only the source is held: its records' files leave the paths an export of
    # it would read. The target's existing files are not touched, because
    # relocate_image never moves a file onto one that is there.
    with export_jobs.hold_for_file_changes([collection_id]):
        # A failed move or commit puts every file already moved back.
        relocate_images_and_commit(
            db, moves, not_moved,
            f"Move of records from collection {collection_id} to {target_collection_id}",
        )

    logger.info(f"Moved {len(records)} records from collection {collection_id} to {target_collection_id}")
    body = {"moved": len(records), "target_collection_id": target_collection_id}
    if not_moved:
        # Preview sidecars that stayed in the old directory (see relocate_image)
        body["not_moved"] = not_moved
    return body


@router.delete("/{collection_id}", status_code=204)
def delete_collection(
    collection_id: int,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    """
    Delete an empty collection and the empty directories of its own.

    Refuses (409) if the volume has a file name stem and has captured, even
    once emptied: its row keeps the stem so the names issued under it are
    never issued again. Also refuses (409) if the collection still has
    sub-collections or records (those must be moved or deleted first) or if
    its directory still holds files that a surviving record points at. An
    empty collection has no files of its own, so no file is removed: a file
    found in its directory (one no row references, such as a file the volume
    layout migration left there) stays, with the directories above it, and is
    logged at WARNING.
    """
    # Under the row lock, so that a PATCH setting a first stem cannot land
    # between this check and the delete.
    collection = _locked_collection_query(db, collection_id).first()
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")

    if collection.reserves_its_stem():
        raise HTTPException(status_code=409, detail={"message": STEM_RESERVED_MESSAGE})

    # Refuse to delete a non-empty collection; report the blocking counts
    child_count = db.query(func.count(Collection.id)).filter(Collection.parent_collection_id == collection_id).scalar()
    record_count = db.query(func.count(Record.id)).filter(Record.collection_id == collection_id).scalar()
    if child_count or record_count:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete a non-empty collection: {child_count} sub-collection(s) and {record_count} record(s) remain. Move or delete them first.",
        )

    # `<stem>/` for a volume with a stem, its name-based directory otherwise
    col_dir = collection_root_dir(db, collection)

    if col_dir is not None:
        # Hard guard: never remove a directory that still holds a surviving record's files
        if surviving_images_under(db, col_dir):
            raise HTTPException(
                status_code=409,
                detail="Collection directory still holds files referenced by other records. Move those records first.",
            )
        # Remove the directories first so a filesystem failure aborts before touching the DB
        try:
            left = remove_empty_dirs(col_dir)
        except OSError as e:
            logger.error(f"Failed to remove collection directory {col_dir}: {e}")
            raise HTTPException(status_code=500, detail="Failed to remove collection files from disk")
        if left:
            logger.warning(
                f"Deleting collection {collection_id}: {len(left)} file(s) no record references were left in "
                f"{col_dir}: {[str(p) for p in left]}"
            )

    db.delete(collection)
    db.commit()

    return None


# ==============================================================================
# Record ordering
# ==============================================================================

@router.patch("/{collection_id}/records/reorder", status_code=200)
def reorder_collection_records(
    collection_id: int,
    payload: ReorderRecords,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    """
    Set the display order of records in a collection.

    Body: { "ordered_ids": [3, 1, 5, 2, ...] }
    Each record's `sequence` is set to its position (0-based) in the supplied list.
    Records not included in the list keep their current sequence value.
    All supplied IDs must belong to the given collection.
    """
    collection = db.query(Collection).filter(Collection.id == collection_id).first()
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")

    # Verify all IDs belong to this collection
    records = (
        db.query(Record)
        .filter(Record.id.in_(payload.ordered_ids), Record.collection_id == collection_id)
        .all()
    )
    found_ids = {r.id for r in records}
    missing = [rid for rid in payload.ordered_ids if rid not in found_ids]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Record IDs not found in collection {collection_id}: {missing}"
        )

    # Apply sequence values
    id_to_seq = {rid: idx for idx, rid in enumerate(payload.ordered_ids)}
    for rec in records:
        rec.sequence = id_to_seq[rec.id]

    db.commit()
    return {"reordered": len(records)}


# ==============================================================================
# Image renumbering
# ==============================================================================

@router.post("/{collection_id}/images/renumber", status_code=200)
def renumber_images(
    collection_id: int,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    """
    Physically renumber every image file in a collection (documentary unit),
    sequentially from 1, zero-padded to the total capture count. Never
    renames anything else — only assigns fresh sequential filenames based on
    current display order. If a file operation fails, or the commit after
    the renames does, every renamed file is put back under its old name and
    the database is left untouched; a file the unwind could not put back is
    logged at ERROR and left under a temporary name, and the 500's detail
    says that not every file went back.

    Refused with 409 while an export of the collection is queued or running,
    since the export reads the files by their current names.
    """
    collection = db.query(Collection).filter(Collection.id == collection_id).first()
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")

    with export_jobs.hold_for_file_changes([collection_id]):
        return _renumber_held(db, collection, collection_id, current_user)


def _renumber_held(db: Session, collection: Collection, collection_id: int, current_user: User) -> dict:
    """The body of renumber_images, run while the collection is held against exports."""
    applied: List = []
    try:
        result = renumber_collection_images(db, collection, applied)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except OSError as e:
        not_restored = getattr(e, "not_restored", [])
        if not_restored:
            logger.error(
                f"Renumber of collection {collection_id} failed on disk; "
                f"{len(not_restored)} file(s) could not be put back under their old names: "
                f"{[p.name for p in not_restored]}"
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to renumber images on disk: {e}. Some files could not be put back under their old names",
            )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to renumber images on disk: {e}. The files are back under their old names",
        )

    try:
        db.commit()
    except Exception:
        db.rollback()
        # The rows still name the old files, so the renames are reversed to match them.
        not_restored = restore_original_names(applied)
        if not_restored:
            logger.exception(
                f"Renumber DB commit failed for collection {collection_id}; "
                f"{len(not_restored)} file(s) could not be put back under their old names: "
                f"{[p.name for p in not_restored]}"
            )
            raise HTTPException(
                status_code=500,
                detail="Files were renumbered on disk but the database update failed, and some files could not be put back under their old names",
            )
        logger.exception(f"Renumber DB commit failed for collection {collection_id}; files put back under their old names")
        raise HTTPException(
            status_code=500,
            detail="The database update failed; the files are back under their old names. Please retry",
        )

    log_event(db, level="INFO", category="activity", action="images_renumbered",
              actor=current_user.username, subject=collection.name,
              detail=f"Renumbered {result['renumbered']} images")
    return result


# ==============================================================================
# Handover (NEH-260)
# ==============================================================================

@router.post("/{collection_id}/promote", status_code=200)
def promote_collection(
    collection_id: int,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    """
    Hand a finished collection over for review: apply captured -> in_review to
    every captured record in it.

    The act is per collection — an operator promotes a tomo when she has
    finished it — while the transition itself stays per record, which is what
    the exchange contract requires for v1. The collection is what travels in
    the URL because Tomo 7 holds 825 records and that is not a request body.

    All or nothing. A collection reads as handed over when none of its records
    is captured, so promoting some of them would leave it unsubmitted while
    reporting success; the records that cannot go are named instead and
    nothing changes.

    Submitted state is derived, never stored. Capture another folio into a
    promoted collection and it is no longer submitted, which is the right
    answer: there is work in it nobody has looked at.
    """
    collection = db.query(Collection).filter(Collection.id == collection_id).first()
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")

    # Locked for the rest of the transaction, and in id order so this and the
    # bulk endpoint take overlapping rows the same way round. Without the lock
    # the pre-check below validates a snapshot: a reject arriving between the
    # check and the commit puts a rejected image back on a record this call
    # then hands over (NEH-260, reproduced).
    records = (
        db.query(Record)
        .options(joinedload(Record.images))
        .filter(Record.collection_id == collection_id, Record.status == "captured")
        .order_by(Record.id)
        .with_for_update(of=Record)
        .all()
    )
    if not records:
        # Already handed over, or empty. Idempotent rather than an error: she
        # asked for a state that holds.
        return {"collection_id": collection_id, "promoted": 0}

    # Check every record before changing any, so the refusal names the whole
    # problem and the collection is left exactly as it was.
    blocking: list[int] = []
    for rec in records:
        try:
            _assert_promotable(rec)
        except HTTPException:
            blocking.append(rec.id)
    if blocking:
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    f"Cannot hand over: {len(blocking)} record(s) still carry a rejected "
                    f"image or have no image at all: {blocking}"
                ),
                "blocking_record_ids": blocking,
            },
        )

    for rec in records:
        _apply_status_change(db, rec, "in_review", current_user)
    db.commit()

    log_event(db, level="INFO", category="activity", action="collection_promoted",
              actor=current_user.username, subject=collection.name,
              detail=f"{len(records)} records")
    return {"collection_id": collection_id, "promoted": len(records)}


@router.post("/{collection_id}/take-back", status_code=200)
def take_back_collection(
    collection_id: int,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    """
    Take a handover back: return the collection's in_review records to
    captured, so an operator who promoted a tomo too early can carry on with
    it.

    in_review -> captured is deliberately absent from STATUS_TRANSITIONS, so
    it is reachable neither per record nor through the bulk endpoint. What is
    being undone is the promotion of a whole collection; withdrawing one
    record from under a reviewer is not an act this API offers. That is why
    the status is set here rather than through _apply_status_change.

    Rejected and approved records are left where they are: a reviewer has
    ruled on them, and a ruling is not undone by taking the handover back.
    The accepted cost is that a reviewer working the queue can see items
    disappear from it; promoting again returns them.

    One statement, so the predicate and the write are evaluated together by
    the database. Selecting the in_review records and then assigning to each
    of them is not the same thing: an approval committed in between was
    overwritten with 'captured', which undid a reviewer's ruling and handed
    the work back to the operator (reproduced). A collection can hold 825
    records, so that window is as long as the transaction.
    """
    collection = db.query(Collection).filter(Collection.id == collection_id).first()
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")

    # The rows are locked in id order first, and only then written. A bare
    # conditional UPDATE is atomic but visits rows in whatever order the planner
    # chooses, which can interleave with move-records — that locks by id — into a
    # deadlock. The predicate stays on the write, so a row approved between the
    # two statements is still not matched.
    locked_ids = [
        row.id
        for row in db.query(Record.id)
        .filter(Record.collection_id == collection_id, Record.status == "in_review")
        .order_by(Record.id)
        .with_for_update(of=Record)
        .all()
    ]
    taken_back = 0
    if locked_ids:
        # RETURNING names the rows the predicate actually matched, so the
        # history rows below are written for those and no others.
        moved_ids = db.execute(
            update(Record)
            .where(Record.id.in_(locked_ids), Record.status == "in_review")
            .values(status="captured")
            .returning(Record.id)
            .execution_options(synchronize_session=False)
        ).scalars().all()
        record_bulk_status_changes(db, moved_ids, collection_id, "in_review", "captured", current_user.username)
        taken_back = len(moved_ids)
    db.commit()
    if not taken_back:
        return {"collection_id": collection_id, "taken_back": 0}

    log_event(db, level="INFO", category="activity", action="collection_taken_back",
              actor=current_user.username, subject=collection.name,
              detail=f"{taken_back} records")
    return {"collection_id": collection_id, "taken_back": taken_back}


@router.get("/{collection_id}/status-changes", response_model=List[RecordStatusChangeRead])
def list_collection_status_changes(
    collection_id: int,
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency)
):
    """
    The status history of the records in a collection, newest first.

    One row per change, so an approval that was undone shows as two rows.
    Records that existed before the history table was added have no rows for
    their earlier changes. A record moved to another collection takes its
    history with it: rows are looked up through the record's current
    collection. A deleted record's rows stay, with record_id null, listed
    under the collection the record was in when each change was made.
    """
    collection = db.query(Collection).filter(Collection.id == collection_id).first()
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")

    return (
        db.query(RecordStatusChange)
        .outerjoin(Record, RecordStatusChange.record_id == Record.id)
        .filter(or_(
            Record.collection_id == collection_id,
            and_(RecordStatusChange.record_id.is_(None),
                 RecordStatusChange.collection_id == collection_id),
        ))
        .order_by(RecordStatusChange.changed_at.desc(), RecordStatusChange.id.desc())
        .all()
    )


# ==============================================================================
# BagIt export
# ==============================================================================

@router.post("/{collection_id}/export", status_code=202)
def export_collection_bagit(
    collection_id: int,
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency)
):
    """
    Start a background BagIt export of this collection and return a job to poll.

    Requires all records to be approved. The heavy work (integrity verification and
    streaming the zip straight into the exports dir, with no staging copy) runs in a
    background job, so proxied clients never hit nginx's read timeout; poll
    GET /{collection_id}/export/status/{job_id} for progress.
    """
    collection = db.query(Collection).filter(Collection.id == collection_id).first()
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")

    records = (
        db.query(Record)
        .filter(Record.collection_id == collection_id)
        .order_by(Record.sequence.nulls_last(), Record.id)
        .all()
    )
    # Handover first, approval second — two different things to tell her. A
    # collection she has not promoted is work in progress, not work that failed
    # review (NEH-260). The predicate lives in export_service beside the worker
    # that re-checks it, so the two cannot drift apart.
    blocker = workflow_blockers(records)
    if blocker:
        message, blocking_ids = blocker
        raise HTTPException(
            status_code=422,
            detail={"message": message, "blocking_record_ids": blocking_ids},
        )

    image_less = [r.id for r in records if not any(i.is_current for i in r.images)]
    if image_less:
        raise HTTPException(
            status_code=422,
            detail={
                "message": f"Cannot export: {len(image_less)} record(s) have no image: {image_less}",
                "blocking_record_ids": image_less,
            },
        )

    # One export per collection at a time: a retry returns the running job instead of
    # stacking a second export that doubles disk pressure. Integrity checks and the
    # zip build happen in the job, surfaced via the status endpoint.
    try:
        job = export_jobs.start(collection_id, lambda j: run_export(j, collection_id))
    except ExportBlockedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    log_event(db, level="INFO", category="activity", action="export_started",
              actor=current_user.username, subject=collection.name)
    return {
        "job_id": job.id,
        "state": job.state,
        "status_url": f"/collections/{collection_id}/export/status/{job.id}",
    }


@router.get("/{collection_id}/export/status/{job_id}")
def export_status(
    collection_id: int,
    job_id: str,
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency),
):
    """Poll the state and progress of a background export job."""
    job = export_jobs.get(job_id)
    if not job or job.collection_id != collection_id:
        raise HTTPException(status_code=404, detail="Export job not found")
    body = job.to_dict()
    if job.state == "done" and job.zip_filename:
        body["download_url"] = f"/collections/{collection_id}/export/download"
    return body


@router.get("/{collection_id}/export/download")
def download_collection_export(
    collection_id: int,
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency)
):
    """Download the most recent BagIt export zip for a collection."""
    from pathlib import Path as _Path

    collection = db.query(Collection).filter(Collection.id == collection_id).first()
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")

    exports_dir = settings.exports_dir
    pattern = f"collection_{collection_id}_*.zip"
    matches = sorted(exports_dir.glob(pattern), reverse=True)
    if not matches:
        raise HTTPException(status_code=404, detail="No export found. Run POST /export first.")

    latest = matches[0]
    return FileResponse(
        path=latest,
        filename=latest.name,
        media_type="application/zip"
    )
