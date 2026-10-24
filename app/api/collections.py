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
from app.core.record_sequence import (
    REORDER_IDS_MESSAGE,
    VOLUME_CHANGED_MESSAGE,
    ReorderIdsError,
    VolumeChangedError,
    listing_order,
    next_sequences,
    renumber_volume_records,
    reorder_volume_records,
)
from app.core.storage_ops import (
    collection_and_descendants,
    collection_image_dir,
    collection_root_dir,
    collections_in_old_layout,
    image_in_old_layout,
    legacy_image_dir,
    relocate_images_and_commit,
    remove_empty_dirs,
    surviving_images_under,
)

router = APIRouter()
logger = logging.getLogger(__name__)

allow_contributor = RoleChecker(["admin", "operator"])
allow_read_only = RoleChecker(["admin", "operator", "reviewer"])
allow_admin = RoleChecker(["admin"])

EXPORT_IN_SUBTREE_MESSAGE = (
    "An export of this collection, or of a collection inside it, is running. "
    "Wait for it to finish, then try again."
)


def _locked_collection_query(db: Session, collection_id: int):
    """The collection row under SELECT ... FOR UPDATE (no lock clause on SQLite)."""
    return db.query(Collection).filter(Collection.id == collection_id).with_for_update()


def _volume_change_detail(collection_id: int, **changes) -> str:
    """The detail of a volume-change event: the id, then each `label old -> new`."""
    parts = [f"id={collection_id}"]
    parts += [f"{label} {old!r} -> {new!r}" for label, (old, new) in changes.items()]
    return "; ".join(parts)


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


@router.get("/{collection_id}/offsite")
def get_collection_offsite_state(
    collection_id: int,
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency)
):
    """The volume's received indicator (NEH-227), derived from its objects' rows.

    `received` is true when the volume has objects to upload and every one is
    verified. `refusals` lists each object refused because the bucket already
    held another file at its key, with both SHA1s. `held` and `hold_file`
    report the unit's offsite upload hold (app.core.offsite_hold), under which
    nothing is uploaded and the volume cannot be freed. A route of its own rather
    than fields on CollectionRead, so listing volumes does not derive this
    for each of them.

    `fisqua` is null when Fisqua is not configured. Otherwise `outbox` lists
    the volume's capture manifests for Fisqua, newest first, each with its
    delivery state (app.models.fisqua), and `status` is the latest status
    Fisqua gave for the volume, or null.
    """
    from app.core.fisqua_outbox import volume_fisqua_state
    from app.core.image_upload import volume_upload_state

    collection = db.query(Collection).filter(Collection.id == collection_id).first()
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")
    return {**volume_upload_state(db, collection), "fisqua": volume_fisqua_state(db, collection.id)}


# ==============================================================================
# Freeing a received volume from the card, and restoring it (app.core.offsite_free)
# ==============================================================================
#
# Under /{id}/offsite, beside the received indicator they depend on. GET of
# /offsite/free is the dry run (it lists the bucket and changes nothing);
# POST runs the free. Both POSTs answer 202 with a job to poll, as the export
# does, because hashing a volume's masters outlasts the proxy's read timeout;
# what can be refused at once is refused before the job starts, with 409.

def _offsite_refused(exc) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.as_detail())


@router.get("/{collection_id}/offsite/free")
def plan_free_collection(
    collection_id: int,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Dry run: which masters and previews freeing would remove, their count and bytes, and what it would refuse and why."""
    from app.core.offsite_free import FreeRefused, plan_free

    try:
        return plan_free(db, collection_id)
    except FreeRefused as exc:
        raise _offsite_refused(exc)


def _job_body(collection_id: int, job) -> dict:
    return {"job_id": job.id, "state": job.state, "action": job.action,
            "status_url": f"/collections/{collection_id}/offsite/jobs/{job.id}"}


@router.post("/{collection_id}/offsite/free", status_code=202)
def free_collection(
    collection_id: int,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Free the volume's masters and RAW previews from the card; thumbnails stay.

    Refused with 409 when the volume has no stem, when not every one of its
    objects is verified in the bucket, while an export of it is queued or
    running or its files are being read for upload, when the bucket
    cannot be listed, and while the offsite upload hold file exists
    (app.core.offsite_hold; the detail carries `hold_file`). Each file is freed only when its local SHA1 and the
    bucket listing's both equal its offsite row's; the job's result lists
    the files freed and those refused.
    """
    from app.core.offsite_free import FreeRefused, start_free

    try:
        job = start_free(db, collection_id, current_user.username)
    except FreeRefused as exc:
        raise _offsite_refused(exc)
    return _job_body(collection_id, job)


@router.post("/{collection_id}/offsite/restore", status_code=202)
def restore_collection(
    collection_id: int,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Copy the volume's freed files back from the bucket, each checked against its SHA1 before it is put in place.

    Refused with 409 when the volume has no stem, when no read remote is
    configured (DTK_OFFSITE_READ_REMOTE), and while an export of it is
    queued or running or its files are being read for upload.
    """
    from app.core.offsite_free import FreeRefused, start_restore

    try:
        job = start_restore(db, collection_id, current_user.username)
    except FreeRefused as exc:
        raise _offsite_refused(exc)
    return _job_body(collection_id, job)


@router.get("/{collection_id}/offsite/jobs/{job_id}")
def offsite_job_status(
    collection_id: int,
    job_id: str,
    current_user: User = Depends(allow_admin),
):
    """The state of a free or restore job, with its result once done."""
    from app.core.offsite_free import offsite_jobs

    job = offsite_jobs.get(job_id)
    if job is None or job.collection_id != collection_id:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@router.get("/{collection_id}/capture-manifest")
def export_capture_manifest(
    collection_id: int,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    """The volume's capture manifest for Fisqua (exchange contract 1.1, artefact 2), as a JSON download.

    Built from the catalogue, the capture manifests and offsite_objects only
    (app.core.capture_manifest), with a fresh id each time. Admins and
    operators only, like promotion: the manifest is how a handed-over volume
    reaches Fisqua, and handing over is the operator's act, not a
    reviewer's. Answers 409 when Fisqua would refuse the file, with a
    message naming the fix and, per problem, the records concerned. Each
    export is written to the system log with its id and counts.
    """
    from datetime import timezone as _tz

    from app.core.capture_manifest import (
        EXPORT_ACTION, ManifestRefused, build_capture_manifest, download_name, manifest_counts,
    )

    collection = db.query(Collection).filter(Collection.id == collection_id).first()
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")

    now = datetime.now(_tz.utc)
    try:
        doc = build_capture_manifest(db, collection, now=now)
    except ManifestRefused as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"message": exc.message, "problems": [p.as_dict() for p in exc.problems]},
        )
    db.commit()  # end the read transaction before the log's own commit

    counts = manifest_counts(doc)
    log_event(db, level="INFO", category="activity", action=EXPORT_ACTION,
              actor=current_user.username, subject=collection.filename_stem,
              detail=(f"volume {collection.id} id={doc['id']} records={counts['records']} "
                      f"images={counts['images']} previews={counts['previews']}"))
    name = download_name(doc["volumes"][0]["stem"], now)
    return JSONResponse(content=doc, headers={"Content-Disposition": f'attachment; filename="{name}"'})


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
    of them. Files in the name-based layout, `<project>/<name>/images/main/`
    (every file of a volume without a stem, and those of a volume given a
    stem that the startup migration has not moved yet), are moved for each
    volume in the subtree whose directory changes, and the rows follow. That
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
        message = stem_directory_collision(db, stem=new_stem, collection=collection)
        if message:
            raise HTTPException(status_code=409, detail={"message": message})

    # Only files in the name-based layout sit in a directory named after the
    # volume and its project, so only volumes with such files have them moved
    # by a rename or a re-parent: a volume without a stem, or one given a stem
    # whose files the startup migration has not moved yet
    # (collections_in_old_layout). Files in `<stem>/` stay where they are.
    update_data = payload.model_dump(exclude_unset=True)
    # An explicit null parent un-nests the volume. A volume has a project or a
    # parent, never neither (check_collection_parent), so it becomes a
    # top-level volume of the project its root ancestor belongs to. The chain
    # is read under the volume's row lock, before anything is changed.
    if "parent_collection_id" in update_data and update_data["parent_collection_id"] is None:
        if collection.parent_collection_id is None:
            del update_data["parent_collection_id"]
        else:
            root, seen = collection, {collection.id}
            while root.parent_collection_id is not None and root.parent_collection_id not in seen:
                root = db.query(Collection).filter(Collection.id == root.parent_collection_id).first()
                seen.add(root.id)
            update_data["project_id"] = root.project_id
    before = (collection.name, collection.filename_stem,
              collection.project_id, collection.parent_collection_id)
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
        old_layout = collections_in_old_layout(db, collection_and_descendants(db, collection.id))
        subtree = db.query(Collection).filter(Collection.id.in_(old_layout)).all() if old_layout else []
        old_dirs = {c.id: legacy_image_dir(db, c) for c in subtree}
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

        # Each such volume whose name-based directory the change moved has the
        # files it still has in the old layout moved to the new one, and the
        # rows follow. A stem set in this same request changes nothing here:
        # the files are still in the old layout until the startup migration.
        if held_ids:
            targets = {}
            cols = {c.id: c for c in db.query(Collection).filter(Collection.id.in_(held_ids)).all()}
            for c in cols.values():
                new_dir = legacy_image_dir(db, c)
                if new_dir is not None and new_dir != old_dirs.get(c.id):
                    targets[c.id] = new_dir
            if targets:
                records = db.query(Record).filter(Record.collection_id.in_(list(targets))).all()
                for r in records:
                    moves.extend((img, targets[r.collection_id]) for img in r.images
                                 if image_in_old_layout(cols[r.collection_id], img.file_path))

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
    # After the commit, so a refused or failed change logs nothing.
    old_name, old_stem, old_project, old_parent = before
    if (old_name, old_stem) != (collection.name, collection.filename_stem):
        log_event(db, level="INFO", category="activity", action="collection_renamed",
                  actor=current_user.username, subject=collection.name,
                  detail=_volume_change_detail(
                      collection_id,
                      stem=(old_stem, collection.filename_stem),
                      name=(old_name, collection.name)))
    if (old_project, old_parent) != (collection.project_id, collection.parent_collection_id):
        log_event(db, level="INFO", category="activity", action="collection_moved",
                  actor=current_user.username, subject=collection.name,
                  detail=_volume_change_detail(
                      collection_id,
                      project=(old_project, collection.project_id),
                      parent=(old_parent, collection.parent_collection_id)))
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
    The moved records take the sequences after the target's highest, in the
    order they were listed in the source.
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
    # Locked in id order, as every path that locks several records does.
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

    # A record whose images are in the bucket stays in its volume: their keys
    # are under this volume's stem and never change. All or nothing, as above.
    from app.core.image_upload import STORED_OFFSITE_MESSAGE, records_stored_offsite

    stored = records_stored_offsite(db, [r.id for r in records])
    if stored and target_collection_id != collection_id:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"Cannot move: {len(stored)} record(s) have files already stored "
                    f"offsite under this volume: {stored}."
                ),
                "blocking_record_ids": stored,
            },
        )

    # The moved records take the sequences after the target's highest, in
    # their listing order in the source, so no two records of the target
    # share one. The
    # target's row is locked after the records, as every path that takes both
    # does, and held until the commit (app/core/record_sequence.py).
    sequences = next_sequences(db, target_collection_id, len(records))
    if sequences is None:
        raise HTTPException(status_code=404, detail=f"Target collection {target_collection_id} not found")

    not_moved: List[str] = []
    moves: List = []
    for r, seq in zip(listing_order(records), sequences):
        r.collection_id = target_collection_id
        r.sequence = seq
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

    # Read before the delete. The checks above leave no record and no
    # sub-collection, so the counts are zero; they are logged anyway, so the
    # entry states what the volume held when it went.
    image_count = (
        db.query(func.count(RecordImage.id))
        .join(Record, RecordImage.record_id == Record.id)
        .filter(Record.collection_id == collection_id)
        .scalar()
    )
    deleted = dict(id=collection.id, name=collection.name, stem=collection.filename_stem,
                   project=collection.project_id, parent=collection.parent_collection_id)
    db.delete(collection)
    db.commit()
    log_event(db, level="WARN", category="activity", action="collection_deleted",
              actor=current_user.username, subject=deleted["name"],
              detail=(f"id={deleted['id']}; stem {deleted['stem']!r}; project {deleted['project']}; "
                      f"parent {deleted['parent']}; records={record_count}; images={image_count}; "
                      f"sub-collections={child_count}"))

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
    Set the order of a volume's records.

    Body: { "ordered_ids": [3, 1, 5, 2, ...] }, every record of the volume
    exactly once. Each record's `sequence` is set to its position in the
    list, counted from 1. A list that leaves a record out, repeats one or
    names one from elsewhere is refused with 422 and REORDER_IDS_MESSAGE,
    and nothing is written. The volume's records and then its row are locked
    for the transaction (app/core/record_sequence.py); if records keep being
    added to the volume meanwhile, it answers 409 with VOLUME_CHANGED_MESSAGE.
    """
    try:
        reordered = reorder_volume_records(db, collection_id, payload.ordered_ids)
    except ReorderIdsError:
        db.rollback()
        raise HTTPException(status_code=422, detail=REORDER_IDS_MESSAGE)
    except VolumeChangedError:
        db.rollback()
        raise HTTPException(status_code=409, detail=VOLUME_CHANGED_MESSAGE)
    if reordered is None:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")

    db.commit()
    return {"reordered": reordered}


# ==============================================================================
# Renumbering (records' sequence only)
# ==============================================================================

@router.post("/{collection_id}/images/renumber", status_code=200)
def renumber_images(
    collection_id: int,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    """
    Renumber a volume's records: set their `sequence` to 1..N in the order
    they are listed in (`sequence`, NULLs last, then id), in one transaction.

    No file is renamed, moved or written, and no manifest line is written
    (contract 1.1 §3): a file's name carries its capture ordinal, and order
    is the records' `sequence`. Records of other volumes are not touched.
    The volume's records and then its row are locked for the transaction
    (app/core/record_sequence.py), so no record added to the volume
    meanwhile can take a sequence the rewrite hands out; if records keep
    being added meanwhile, it answers 409 with VOLUME_CHANGED_MESSAGE. If the
    commit fails, every sequence stays as it was.
    """
    try:
        renumbered = renumber_volume_records(db, collection_id)
    except VolumeChangedError:
        db.rollback()
        raise HTTPException(status_code=409, detail=VOLUME_CHANGED_MESSAGE)
    if renumbered is None:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")
    collection = db.get(Collection, collection_id)
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(f"Renumber DB commit failed for collection {collection_id}; no sequence was changed")
        raise HTTPException(
            status_code=500,
            detail="The database update failed, so no record was renumbered. Please retry",
        )

    log_event(db, level="INFO", category="activity", action="images_renumbered",
              actor=current_user.username, subject=collection.name,
              detail=f"Renumbered {renumbered} records")
    return {"renumbered": renumbered}


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
    # Queued in the handover's transaction, after the records' locks, so a
    # handover and its manifest are committed together or not at all
    # (app.core.fisqua_outbox). Nothing is queued when Fisqua is not configured.
    from app.core.fisqua_outbox import enqueue_handover
    enqueue_handover(db, collection)
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
    if taken_back:
        # The handover is undone, so its manifest, if not yet sent, is not to be.
        from app.core.fisqua_outbox import withdraw_unsent
        withdraw_unsent(db, collection_id)
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

    # A master freed from the card is in the bucket, not here, and a bag is
    # built from the card. 409: the volume's state, which a restore changes.
    from app.core.offsite_free import FREED_EXPORT_MESSAGE
    freed = [i.id for r in records for i in r.images if i.is_current and i.master_freed_at is not None]
    if freed:
        raise HTTPException(
            status_code=409,
            detail={"message": FREED_EXPORT_MESSAGE.format(n=len(freed)), "freed_image_ids": freed},
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
