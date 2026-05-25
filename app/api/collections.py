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

router = APIRouter()
logger = logging.getLogger(__name__)

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
    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(collection, field, value)
    
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
    Move all records from one collection to another.
    Used before deleting a collection to preserve its contents.
    """
    source = db.query(Collection).filter(Collection.id == collection_id).first()
    if not source:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")

    target = db.query(Collection).filter(Collection.id == target_collection_id).first()
    if not target:
        raise HTTPException(status_code=404, detail=f"Target collection {target_collection_id} not found")

    moved = db.query(Record).filter(Record.collection_id == collection_id).update(
        {"collection_id": target_collection_id},
        synchronize_session=False
    )
    db.commit()

    logger.info(f"Moved {moved} records from collection {collection_id} to {target_collection_id}")
    return {"moved": moved, "target_collection_id": target_collection_id}


@router.delete("/{collection_id}", status_code=204)
def delete_collection(
    collection_id: int,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    """
    Delete a collection and its filesystem directory.

    Warning: This will cascade delete all child collections and orphan any records in this collection.
    Use POST /{collection_id}/move-records first if you want to preserve the records.
    """
    import shutil
    from app.core.config import settings
    from capture.project_manager import secure_project_filename

    collection = db.query(Collection).filter(Collection.id == collection_id).first()
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")

    # Check if collection has records
    record_count = db.query(func.count(Record.id)).filter(
        Record.collection_id == collection_id
    ).scalar()

    if record_count > 0:
        logger.warning(f"Deleting collection {collection_id} with {record_count} records - records will be orphaned")

    # Capture names before deleting from DB
    collection_name = collection.name
    project = db.query(Project).filter(Project.id == collection.project_id).first() if collection.project_id else None
    project_name = project.name if project else None

    db.delete(collection)
    db.commit()

    # Remove the collection directory from disk (project/collection/images/).
    # Try both raw and sanitized names to handle historic inconsistencies.
    if project_name:
        safe_col = secure_project_filename(collection_name)
        for proj_dir in [settings.projects_dir / project_name, settings.projects_dir / secure_project_filename(project_name)]:
            for col_dir in ([proj_dir / collection_name] + ([proj_dir / safe_col] if safe_col != collection_name else [])):
                if col_dir.exists() and col_dir.is_dir():
                    try:
                        shutil.rmtree(col_dir)
                        logger.info(f"Removed collection directory: {col_dir}")
                    except Exception as e:
                        logger.warning(f"Could not remove collection directory {col_dir}: {e}")
                    break

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
