from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from datetime import datetime


class CollectionBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    collection_type: Optional[str] = Field(None, max_length=50, description="Type: fonds, series, box, folder, volume, etc.")
    archival_metadata: Optional[Dict[str, Any]] = Field(None, description="Flexible JSON metadata for archival fields")


class CollectionCreate(CollectionBase):
    """Create a collection. Must specify either project_id OR parent_collection_id."""
    project_id: Optional[int] = Field(None, description="Parent project (for top-level collections)")
    parent_collection_id: Optional[int] = Field(None, description="Parent collection (for nested subcollections)")
    created_by: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "name": "Box 42",
                "description": "Contains correspondence from 1920-1925",
                "collection_type": "box",
                "project_id": 1,
                "archival_metadata": {"box_number": "42", "shelf_location": "A-3-2"}
            }
        }


class CollectionUpdate(BaseModel):
    """Update a collection. All fields optional."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    collection_type: Optional[str] = Field(None, max_length=50)
    archival_metadata: Optional[Dict[str, Any]] = None
    parent_collection_id: Optional[int] = Field(None, description="Move to different parent collection")


class CollectionRead(CollectionBase):
    id: int
    project_id: Optional[int] = None
    parent_collection_id: Optional[int] = None
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CollectionWithChildren(CollectionRead):
    """Collection with nested child collections (for hierarchical views)."""
    child_collections: List["CollectionRead"] = []
    record_count: Optional[int] = None

    class Config:
        from_attributes = True
