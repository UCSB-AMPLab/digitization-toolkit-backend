# Version: 0.3.3

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
from app.schemas.record import MissingOpeningsCreate, RecordRead, RecordStatusChangeRead, ReorderRecords
# The one place a status transition is validated and applied, so the
# promotion invariant cannot be skipped by reaching a record from here
# (NEH-260). cameras.py imports _supersede_current_images the same way.
from app.api.records import _apply_status_change, _assert_promotable, _serialize_record, is_movable
from app.core.audit import log_event
from app.core.record_status import record_bulk_status_changes
from app.core.volume_stem import locked_volume_query
from app.core.config import settings
from app.core.db_errors import integrity_conflict
from app.core.export_jobs import ExportBlockedError, export_jobs
from app.core.export_service import exported_records, run_export, workflow_blockers
from app.core.record_sequence import (
    REORDER_IDS_MESSAGE,
    VOLUME_CHANGED_MESSAGE,
    AfterRecordError,
    ReorderIdsError,
    VolumeChangedError,
    listing_order,
    next_sequences,
    renumber_volume_records,
    insert_placeholders,
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


def _volumes_from_fisqua() -> HTTPException:
    """The 403 for an operator creating a volume or changing its codes in fisqua mode (app.core.unit_mode)."""
    from app.core import unit_mode

    return HTTPException(status_code=403, detail={"code": unit_mode.VOLUMES_FROM_FISQUA,
                                                  "message": unit_mode.VOLUMES_FROM_FISQUA_MESSAGE})


def _check_codes_with_fisqua(db: Session, codes) -> None:
    """An admin's new reference codes in fisqua mode, checked with Fisqua (contract 1.8); an HTTPException if refused."""
    from app.core import unit_mode

    try:
        unit_mode.check_reference_codes(db, codes)
    except unit_mode.CodeRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail())


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

    The unit's mode and projects setting (app.core.unit_mode) apply. While
    projects are hidden, a volume without a parent collection goes into the
    unit's default project, and any project_id given is ignored; with
    projects shown, giving neither is still a 400. In fisqua
    mode an operator is refused with a 403 (`volumes_from_fisqua`); an
    admin's volume is saved only once Fisqua knows its stem, and its
    external_id when set (contract 1.8; 422 `unknown_reference_code`, 409
    `fisqua_no_lookup` or `fisqua_unreachable`), and is logged
    `volume_created_override`.
    """
    from app.core import unit_mode
    from app.core.list_import import ListRefused, ProjectLockUnavailable

    unit = unit_mode.current(db)
    from_fisqua = unit.mode == unit_mode.FISQUA
    if from_fisqua and current_user.role != "admin":
        raise _volumes_from_fisqua()

    # While projects are hidden every new volume goes into the unit's default
    # project, whoever creates it: a given project_id is ignored. A
    # subcollection (parent_collection_id) is created as before.
    project_id = payload.project_id if unit.projects_shown else None
    # Validate that exactly one parent is specified
    if project_id is None and payload.parent_collection_id is None and unit.projects_shown:
        raise HTTPException(status_code=400, detail="Must specify either project_id or parent_collection_id")
    
    if project_id is not None and payload.parent_collection_id is not None:
        raise HTTPException(status_code=400, detail="Cannot specify both project_id and parent_collection_id")

    if from_fisqua:
        _check_codes_with_fisqua(db, [payload.filename_stem, payload.external_id])

    if project_id is None and payload.parent_collection_id is None:
        try:
            project_id = unit_mode.default_project(db, current_user.username)[0].id
        except ProjectLockUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except ListRefused as exc:
            raise HTTPException(status_code=409, detail={"message": str(exc)})

    if project_id is not None:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    
    if payload.parent_collection_id is not None:
        parent = db.query(Collection).filter(Collection.id == payload.parent_collection_id).first()
        if not parent:
            raise HTTPException(status_code=404, detail=f"Parent collection {payload.parent_collection_id} not found")
    
    collection = Collection(
        name=payload.name,
        description=payload.description,
        collection_type=payload.collection_type,
        archival_metadata=payload.archival_metadata,
        project_id=project_id,
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
    if from_fisqua:
        log_event(db, level="INFO", category="activity", action="volume_created_override",
                  actor=current_user.username, subject=collection.name,
                  detail=_volume_change_detail(collection.id, stem=(None, collection.filename_stem),
                                               external_id=(None, collection.external_id)))
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
    cannot be listed, while the offsite upload hold file exists
    (app.core.offsite_hold; the detail carries `hold_file`), and in offline
    mode (app.core.unit_mode; the detail's `code` is `off_in_this_mode`). Each file is freed only when its local SHA1 and the
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
    configured (CAPTUA_OFFSITE_READ_REMOTE), and while an export of it is
    queued or running or its files are being read for upload. Allowed in
    offline mode: it reads from the bucket and sends nothing out.
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
    """The volume's capture manifest for Fisqua (exchange contract 1.6, artefact 2), as a JSON download.

    A unit with a Fisqua token sends the manifest itself at handover
    (app.core.fisqua_outbox). This route stays for units without a token
    (contract 1.2 §9).

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

    In fisqua mode (app.core.unit_mode) a change of filename_stem or
    external_id is refused from an operator with a 403
    (`volumes_from_fisqua`); other fields, and a value equal to the stored
    one, stay allowed. An admin's change is saved only once Fisqua knows
    the new code (contract 1.8, as POST /collections/), and is logged
    `volume_code_override`; clearing the stem is refused with a 422
    (`stem_required`), since the stem is the volume's Fisqua reference code.
    """
    from app.core import unit_mode

    from_fisqua = unit_mode.mode(db) == unit_mode.FISQUA
    code_fields = {name: value for name, value in payload.model_dump(exclude_unset=True).items()
                   if name in ("filename_stem", "external_id")}
    checked: set = set()

    def code_changes(stem, external_id) -> dict:
        stored = {"filename_stem": stem, "external_id": external_id}
        return {name: value for name, value in code_fields.items() if value != stored[name]}

    if from_fisqua and code_fields:
        # Before the row lock, so that Fisqua is not asked while the row is
        # held; read as plain columns, so the locked read below loads the
        # row afresh. Checked again under the lock.
        stored = (db.query(Collection.filename_stem, Collection.external_id)
                  .filter(Collection.id == collection_id).first())
        if stored is not None:
            changes = code_changes(*stored)
            if changes and current_user.role != "admin":
                raise _volumes_from_fisqua()
            if "filename_stem" in changes and changes["filename_stem"] is None:
                raise HTTPException(status_code=422, detail={"code": unit_mode.STEM_REQUIRED,
                                                             "message": unit_mode.STEM_REQUIRED_MESSAGE})
            new_codes = [value for value in changes.values() if value is not None]
            _check_codes_with_fisqua(db, new_codes)
            checked.update(new_codes)

    # Under the row lock, so that a concurrent PATCH setting a first stem or
    # external_id commits before this one reads the row, and the model's
    # write-once guards compare against the committed value.
    collection = _locked_collection_query(db, collection_id).first()
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")

    code_override = False
    if from_fisqua and code_fields:
        changes = code_changes(collection.filename_stem, collection.external_id)
        if "filename_stem" in changes and changes["filename_stem"] is None:
            db.rollback()
            raise HTTPException(status_code=422, detail={"code": unit_mode.STEM_REQUIRED,
                                                         "message": unit_mode.STEM_REQUIRED_MESSAGE})
        if changes and current_user.role != "admin":
            db.rollback()
            raise _volumes_from_fisqua()
        unchecked = [value for value in changes.values() if value is not None and value not in checked]
        if unchecked:
            # The row changed since the read above: rare, and checked here.
            try:
                _check_codes_with_fisqua(db, unchecked)
            except HTTPException:
                db.rollback()
                raise
        code_override = bool(changes)
    
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
    old_external_id = collection.external_id
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
    if code_override and (old_stem, old_external_id) != (collection.filename_stem, collection.external_id):
        log_event(db, level="INFO", category="activity", action="volume_code_override",
                  actor=current_user.username, subject=collection.name,
                  detail=_volume_change_detail(
                      collection_id,
                      stem=(old_stem, collection.filename_stem),
                      external_id=(old_external_id, collection.external_id)))
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
# Openings never captured (contract 1.9 §10)
# ==============================================================================

@router.post("/{collection_id}/records/missing-opening", response_model=List[RecordRead])
def mark_missing_openings(
    collection_id: int,
    payload: MissingOpeningsCreate,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    """
    Mark openings never captured: insert placeholders right after a record of the volume.

    Body: {"afterRecordId", "count" (1 to 50, or null for one), "expected",
    "note" (each at most 2,000 characters), "requestId" (at most 64)}, all
    but afterRecordId optional. afterRecordId names a record of this volume,
    of any status, excluded included. Each placeholder is a dual record with
    no image that misses both pages (missing_side "both"), captured, carrying
    what it should hold, the note and the request id
    (app.core.record_sequence.insert_placeholders). They take the sequences
    right after the record, and every later record's sequence moves up by
    the count; no file is renamed or touched (contract 1.9 §10.2). The
    volume's records and then its row are locked for the transaction
    (app/core/record_sequence.py); if records keep being added meanwhile it
    answers 409 with VOLUME_CHANGED_MESSAGE. Returns the placeholders, in
    order. 404 for an unknown volume or record, 422 for a record of another
    volume or a bad body. Writes `missing_openings_marked` to the system log.
    A volume handed over before is handed over again in the same transaction
    when nothing else holds it (app.core.fisqua_outbox.hand_over_again_after_missing_change).
    """
    volume = db.query(Collection).filter(Collection.id == collection_id).first()
    if volume is None:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")
    after = db.get(Record, payload.afterRecordId)
    if after is None:
        raise HTTPException(status_code=404, detail=f"Record {payload.afterRecordId} not found")
    other_volume = (f"Record {payload.afterRecordId} is not in volume {collection_id}, "
                    "so no opening can be marked after it there.")
    if after.collection_id != collection_id:
        raise HTTPException(status_code=422, detail=other_volume)
    count = payload.count or 1
    try:
        made = insert_placeholders(db, collection_id, payload.afterRecordId, count,
                                   expected=payload.expected, note=payload.note,
                                   request_id=payload.requestId, created_by=current_user.username)
    except AfterRecordError:
        # Moved out of the volume while its records were being locked.
        db.rollback()
        raise HTTPException(status_code=422, detail=other_volume)
    except VolumeChangedError:
        db.rollback()
        raise HTTPException(status_code=409, detail=VOLUME_CHANGED_MESSAGE)
    if made is None:
        db.rollback()
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")
    # After the volume's locks, as promotion queues its handover after its own.
    from app.core.fisqua_outbox import hand_over_again_after_missing_change
    queued = hand_over_again_after_missing_change(db, collection_id)
    db.commit()
    if queued is not None:
        from app.core.image_upload import image_upload_scheduler
        image_upload_scheduler.wake()
    for rec in made:
        db.refresh(rec)
    out = [_serialize_record(rec) for rec in made]
    log_event(db, level="INFO", category="activity", action="missing_openings_marked",
              actor=current_user.username, subject=volume.name,
              detail=(f"volume {collection_id} after {payload.afterRecordId} count={count} "
                      f"requestId={payload.requestId} records={[r.id for r in out]}"))
    return out


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
    every captured record in it but the open missing ones (records.missing_side
    set and records.closed_reason not: a page or an opening never captured,
    contract 1.9 §10), which stay captured until they are captured into or
    closed. A missing record the operator closed is promoted like any other.

    The act is per collection — an operator promotes a tomo when she has
    finished it — while the transition itself stays per record, which is what
    the exchange contract requires for v1. The collection is what travels in
    the URL because Tomo 7 holds 825 records and that is not a request body.

    All or nothing. A collection reads as handed over when none of its records
    is captured, so promoting some of them would leave it unsubmitted while
    reporting success; the records that cannot go are named instead and
    nothing changes. The one exception is an open missing record: it is
    pending, not unfinished work to refuse over, so the others go and it
    stays, and the volume's manifest lists it as missing
    (app.core.capture_manifest).

    Submitted state is derived, never stored. Capture another folio into a
    promoted collection and it is no longer submitted, which is the right
    answer: there is work in it nobody has looked at.

    The first successful promotion of a full rescan opens the recapture turn
    for one new volume, as finishing a volume's recaptures does: it writes
    rescan_volume_completed in the handover's transaction
    (recapture_turn.award_rescan_turns). A volume already carrying that event
    writes none, so a take-back and a second promotion open no second turn.
    A refused or empty promotion writes nothing.

    Locks, in the order every path that takes the turn lock keeps: the
    records, then the volume's row (locked_volume_query, as the capture
    paths take it), then the turn. A retake holds its volume's row and then
    asks for the turn; a promotion holding the turn and then needing the row
    (its status-change rows reference the volume) would deadlock against it.
    The records locked are all the volume's, open missing ones included, in
    id order; the ones to promote are chosen from them. Every promotion
    takes the volume's row FOR UPDATE once, after the records and before any
    status change or the outbox: a full rescan
    through lock_rescan_volumes, before the turn, any other volume directly,
    with no turn lock. So promotion is records, volume, history, outbox, as
    take-back and the missing changes (contract 1.9 §10) are; the history
    rows' FOR KEY SHARE on the volume then never needs upgrading.
    """
    collection = db.query(Collection).filter(Collection.id == collection_id).first()
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")

    # Locked for the rest of the transaction, and in id order so this and the
    # bulk endpoint take overlapping rows the same way round. Without the lock
    # the pre-check below validates a snapshot: a reject arriving between the
    # check and the commit puts a rejected image back on a record this call
    # then hands over (NEH-260, reproduced).
    # Every record of the volume is locked, open missing ones included, and
    # the promotable ones are chosen from the locked rows: a close-missing
    # committed between a narrower lock and the volume's row would leave a
    # closed captured record out of this handover, and its manifest would
    # be refused (records_captured).
    locked = (
        db.query(Record)
        .options(joinedload(Record.images))
        .filter(Record.collection_id == collection_id)
        .order_by(Record.id)
        .with_for_update(of=Record)
        .all()
    )
    records = [
        rec for rec in locked
        if rec.status == "captured" and (rec.missing_side is None or rec.closed_reason is not None)
    ]
    # A volume with a record waiting for a retake (rejected, or prepared for
    # retake with its rejected image still current; app.core.recapture_turn)
    # stays on the unit until its retakes are done, for every role: Fisqua
    # would hold a delivery with a rejected record anyway. Counted after the
    # locks, and before the idempotent return, so a volume whose only records
    # left are rejected is refused too. An open missing record is pending
    # but does not hold the volume back: it is left out of the records
    # above, stays captured, and is listed as missing in the manifest until
    # a dual capture writes both pages or the operator closes it.
    from app.core.recapture_turn import volume_retakes_pending
    pending = volume_retakes_pending(db, collection_id)
    if pending:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "recaptures_pending",
                "pending": pending,
                "message": f"The volume could not be sent to review: {pending} retakes left.",
            },
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

    # Records, then the volume's row, then the turn: the capture paths' order.
    # The row is taken before the status changes write their history rows.
    from app.core import volume_stem
    from app.core.recapture_turn import award_rescan_turns, lock_rescan_volumes
    rescans = lock_rescan_volumes(db, [collection_id])
    if not rescans:
        # Any other volume's row as well, once, so every promotion is records,
        # volume, history, outbox, as take-back and the missing changes are.
        volume_stem.locked_volume_query(db, collection_id).first()
    for rec in records:
        _apply_status_change(db, rec, "in_review", current_user)
    award_rescan_turns(db, rescans, current_user.username)
    # Queued in the handover's transaction, after the records' locks, so a
    # handover and its manifest are committed together or not at all
    # (app.core.fisqua_outbox). Nothing is queued when Fisqua is not configured.
    from app.core.fisqua_outbox import enqueue_handover
    enqueue_handover(db, collection)
    db.commit()
    # After the commit, so the woken worker sees the queued manifest: the
    # volume is uploaded, verified and sent now rather than at the next
    # interval. A no-op when offsite upload is not configured.
    from app.core.image_upload import image_upload_scheduler
    image_upload_scheduler.wake()

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
    of them is not the same thing: an approval committed in between would be
    overwritten with 'captured', undoing a reviewer's ruling and handing the
    work back to the operator (reproduced). A collection can hold 825
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
        # The volume's row, FOR UPDATE, after the records and before the
        # history rows (which take FOR KEY SHARE on it) and the outbox: record,
        # volume, history, outbox, the order of every path that hands a
        # volume over or takes it back (app.core.fisqua_outbox).
        locked_volume_query(db, collection_id).first()
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

    Requires all records to be approved, except those Fisqua excluded, which
    are skipped (app.core.export_service.exported_records). The heavy work (integrity verification and
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
    # that re-checks it, so the two cannot drift apart. It is given every
    # record, and leaves out excluded ones and closed placeholders itself.
    blocker = workflow_blockers(records)
    if blocker:
        message, blocking_ids = blocker
        raise HTTPException(
            status_code=422,
            detail={"message": message, "blocking_record_ids": blocking_ids},
        )
    records = exported_records(records)

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
