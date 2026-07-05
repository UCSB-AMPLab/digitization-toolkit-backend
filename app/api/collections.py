from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from typing import List, Optional
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import select, func
import logging

from app.api.deps import get_db_dependency
from app.api.auth import get_current_user, RoleChecker
from app.models.collection import Collection
from app.models.project import Project
from app.models.record import Record, RecordImage
from app.models.user import User
from app.schemas.collection import CollectionCreate, CollectionRead, CollectionUpdate, CollectionWithChildren
from app.schemas.record import ReorderRecords
from app.core.audit import log_event
from app.core.storage_ops import (
    collection_and_descendants,
    relocate_image,
    remove_tree,
    resolve_project_name,
    surviving_images_under,
)

router = APIRouter()
logger = logging.getLogger(__name__)

allow_admin = RoleChecker(["admin"])
allow_contributor = RoleChecker(["admin", "operator"])
allow_read_only = RoleChecker(["admin", "operator", "reviewer"])


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
        created_by=payload.created_by or current_user.username
    )
    
    db.add(collection)
    db.commit()
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
    
    items = query.offset(skip).limit(limit).all()
    return [CollectionRead.model_validate(i) for i in items]


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
    
    # Count records in this collection
    record_count = db.query(func.count(RecordImage.id)).filter(
        RecordImage.collection_id == collection_id
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
    """
    collection = db.query(Collection).filter(Collection.id == collection_id).first()
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
    
    # Update fields
    old_name = collection.name
    old_parent = collection.parent_collection_id
    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(collection, field, value)

    # A collection has exactly one parent: nesting it clears its direct project link
    if collection.parent_collection_id is not None:
        collection.project_id = None

    # A rename or cross-project re-parent changes the on-disk dir; relocate files
    layout_changed = (
        ("name" in update_data and update_data["name"] != old_name)
        or ("parent_collection_id" in update_data and update_data["parent_collection_id"] != old_parent)
    )
    if layout_changed:
        from capture.project_manager import image_output_dir

        project_name = resolve_project_name(db, collection)
        if project_name:
            subtree_ids = collection_and_descendants(db, collection.id)
            names = {
                c.id: c.name
                for c in db.query(Collection).filter(Collection.id.in_(subtree_ids)).all()
            }
            records = db.query(Record).filter(Record.collection_id.in_(subtree_ids)).all()
            for r in records:
                target_dir = image_output_dir(project_name, names.get(r.collection_id))
                for img in r.images:
                    relocate_image(img, target_dir)

    db.commit()
    db.refresh(collection)
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
    from capture.project_manager import image_output_dir

    source = db.query(Collection).filter(Collection.id == collection_id).first()
    if not source:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")

    target = db.query(Collection).filter(Collection.id == target_collection_id).first()
    if not target:
        raise HTTPException(status_code=404, detail=f"Target collection {target_collection_id} not found")

    target_project_name = resolve_project_name(db, target)
    if not target_project_name:
        raise HTTPException(status_code=422, detail="Target collection is not attached to a project")
    target_dir = image_output_dir(target_project_name, target.name)

    records = db.query(Record).filter(Record.collection_id == collection_id).all()
    for r in records:
        r.collection_id = target_collection_id
        for img in r.images:
            relocate_image(img, target_dir)
    db.commit()

    logger.info(f"Moved {len(records)} records from collection {collection_id} to {target_collection_id}")
    return {"moved": len(records), "target_collection_id": target_collection_id}


@router.delete("/{collection_id}", status_code=204)
def delete_collection(
    collection_id: int,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency)
):
    """
    Delete an empty collection and its (now unused) directory.

    Refuses (409) if the collection still has sub-collections or records (those
    must be moved or deleted first) or if its directory still holds files that a
    surviving record points at.
    """
    from capture.project_manager import collection_capture_root

    collection = db.query(Collection).filter(Collection.id == collection_id).first()
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")

    collection_name = collection.name

    # Refuse to delete a non-empty collection; report the blocking counts
    child_count = db.query(func.count(Collection.id)).filter(Collection.parent_collection_id == collection_id).scalar()
    record_count = db.query(func.count(Record.id)).filter(Record.collection_id == collection_id).scalar()
    if child_count or record_count:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete a non-empty collection: {child_count} sub-collection(s) and {record_count} record(s) remain. Move or delete them first.",
        )

    project_name = resolve_project_name(db, collection)
    col_dir = collection_capture_root(project_name, collection_name) if project_name else None

    if col_dir is not None:
        # Hard guard: never remove a directory that still holds a surviving record's files
        if surviving_images_under(db, col_dir):
            raise HTTPException(
                status_code=409,
                detail="Collection directory still holds files referenced by other records. Move those records first.",
            )
        # Remove files first so a filesystem failure aborts before touching the DB
        try:
            remove_tree(col_dir)
        except OSError as e:
            logger.error(f"Failed to remove collection directory {col_dir}: {e}")
            raise HTTPException(status_code=500, detail="Failed to remove collection files from disk")

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
# BagIt export
# ==============================================================================

@router.post("/{collection_id}/export", status_code=202)
def export_collection_bagit(
    collection_id: int,
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency)
):
    """
    Package all approved records in this collection as a BagIt zip archive.

    Requires ALL records in the collection to have status 'approved'.
    The generated zip is saved to the exports directory and a download URL is returned.
    """
    import bagit
    import json
    import shutil as _shutil
    import tempfile
    import zipfile
    from datetime import datetime, timezone
    from pathlib import Path as _Path

    collection = db.query(Collection).filter(Collection.id == collection_id).first()
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")

    records = (
        db.query(Record)
        .filter(Record.collection_id == collection_id)
        .order_by(Record.sequence.nulls_last(), Record.created_at)
        .all()
    )
    if not records:
        raise HTTPException(status_code=422, detail="Collection has no records to export.")

    # All records must be approved
    non_approved = [r.id for r in records if r.status != "approved"]
    if non_approved:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot export: {len(non_approved)} record(s) are not approved yet: {non_approved}"
        )

    # Gather project info for bag metadata
    project = db.query(Project).filter(Project.id == collection.project_id).first() if collection.project_id else None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bag_name = f"collection_{collection_id}_{timestamp}"
    exports_dir = settings.exports_dir
    exports_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = _Path(tmpdir)
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Copy image files into data/ organised by record sequence
        for idx, rec in enumerate(records):
            seq_label = f"{(rec.sequence if rec.sequence is not None else idx):04d}"
            safe_title = "".join(c if c.isalnum() or c in "-_ " else "_" for c in (rec.title or "record"))[:60]
            rec_dir = data_dir / f"{seq_label}_{safe_title}"
            rec_dir.mkdir(exist_ok=True)

            for img in sorted(rec.images, key=lambda i: (i.role or "z", i.id)):
                if not img.file_path:
                    continue
                src = _Path(img.file_path)
                if not src.exists():
                    logger.warning(f"Missing file for image {img.id}: {img.file_path}")
                    continue
                role_prefix = img.role or f"img_{img.id}"
                dest_name = f"{role_prefix}{src.suffix}"
                _shutil.copy2(src, rec_dir / dest_name)

        # Write metadata sidecar before bagging
        metadata_payload = {
            "exported_at": timestamp,
            "collection": {
                "id": collection.id,
                "name": collection.name,
                "description": collection.description,
                "collection_type": collection.collection_type,
                "archival_metadata": collection.archival_metadata,
                "created_by": collection.created_by,
                "created_at": collection.created_at.isoformat() if collection.created_at else None,
            },
            "project": {
                "id": project.id if project else None,
                "name": project.name if project else None,
            } if project else None,
            "records": [
                {
                    "id": r.id,
                    "sequence": r.sequence,
                    "title": r.title,
                    "description": r.description,
                    "object_typology": r.object_typology,
                    "author": r.author,
                    "material": r.material,
                    "date": r.date,
                    "status": r.status,
                    "created_by": r.created_by,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "images": [
                        {
                            "id": img.id,
                            "filename": img.filename,
                            "role": img.role,
                            "sequence": img.sequence,
                            "format": img.format,
                            "resolution_width": img.resolution_width,
                            "resolution_height": img.resolution_height,
                            "file_size": img.file_size,
                        }
                        for img in r.images
                    ],
                }
                for r in records
            ],
        }
        (data_dir / "metadata.json").write_text(
            json.dumps(metadata_payload, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

        # Create BagIt bag in-place
        bag_metadata = {
            "Source-Organization": project.name if project else "Digitization Toolkit",
            "External-Description": collection.description or collection.name,
            "Bagging-Date": timestamp[:8],
            "External-Identifier": f"collection-{collection_id}",
            "Bag-Count": "1 of 1",
            "Record-Count": str(len(records)),
        }
        bagit.make_bag(str(tmp_path), bag_metadata, checksum=["md5", "sha256"])

        # Zip the bag
        zip_path = exports_dir / f"{bag_name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in tmp_path.rglob("*"):
                if file.is_file():
                    zf.write(file, arcname=_Path(bag_name) / file.relative_to(tmp_path))

    logger.info(f"BagIt export created: {zip_path} ({zip_path.stat().st_size} bytes)")
    return {
        "bag_name": bag_name,
        "zip_filename": zip_path.name,
        "size_bytes": zip_path.stat().st_size,
        "download_url": f"/collections/{collection_id}/export/download",
    }


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
