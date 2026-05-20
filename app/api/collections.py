from fastapi import APIRouter, Depends, HTTPException, Query
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


@router.delete("/{collection_id}", status_code=204)
def delete_collection(
    collection_id: int,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    """
    Delete a collection.
    
    Warning: This will cascade delete all child collections and orphan any records in this collection.
    """
    collection = db.query(Collection).filter(Collection.id == collection_id).first()
    if not collection:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")
    
    # Check if collection has records
    record_count = db.query(func.count(Record.id)).filter(
        Record.collection_id == collection_id
    ).scalar()
    
    if record_count > 0:
        logger.warning(f"Deleting collection {collection_id} with {record_count} records - records will be orphaned")
    
    db.delete(collection)
    db.commit()
    return None
