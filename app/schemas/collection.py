from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime
# Aliased: `date` is a field name on these schemas (the free-text display
# date), so the type is imported under another name, as in schemas/record.py.
from datetime import date as dt_date

from app.models.collection import FILENAME_STEM_MAX_LENGTH, FILENAME_STEM_PATTERN


class VolumeIdentityFields(BaseModel):
    """The volume identity fields of exchange contract 1.1, accepted on create and update.

    The pattern and length here refuse a bad stem with a 422 before the
    database sees it; the CHECKs on the table refuse it from any other writer.
    Whether a stem or external_id may still change is the model's rule
    (app/models/collection.py), since it depends on the stored row.
    """
    filename_stem: Optional[str] = Field(
        None, max_length=FILENAME_STEM_MAX_LENGTH, pattern=FILENAME_STEM_PATTERN,
        description="Lowercase slug the volume's image file names are built from, e.g. co-ahrb-aht-003",
    )
    external_id: Optional[str] = Field(
        None, min_length=1, max_length=255, description="Fisqua reference code; written once",
    )
    date: Optional[str] = Field(None, max_length=50, description="Display date, free text")
    date_start: Optional[dt_date] = None
    date_end: Optional[dt_date] = None
    sequence: Optional[int] = None
    custom_attributes: Optional[str] = Field(None, description="JSON string for custom fields")

    @field_validator("external_id", mode="before")
    @classmethod
    def _strip_external_id(cls, value):
        # A reference code padded with whitespace would be a second code to
        # the unique index, and a blank one is no code; strip, then refuse
        # blank. None passes through (on update it is a request to clear,
        # which the model refuses once a code is stored).
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("external_id cannot be blank")
        return value

    @model_validator(mode="after")
    def _end_not_before_start(self):
        # Only when both ends arrive together; an end checked against a stored
        # start is refused by check_collection_date_range.
        if self.date_start is not None and self.date_end is not None and self.date_end < self.date_start:
            raise ValueError("date_end cannot be before date_start")
        return self


class CollectionBase(BaseModel):
    # Exchange contract 1.1 §3 maps an item's `title` onto the volume's
    # `title`; a collection has no such column, and `name` is that field.
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    collection_type: Optional[str] = Field(None, max_length=50, description="Type: fonds, series, box, folder, volume, etc.")
    archival_metadata: Optional[Dict[str, Any]] = Field(None, description="Flexible JSON metadata for archival fields")


class CollectionCreate(VolumeIdentityFields, CollectionBase):
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


class CollectionUpdate(VolumeIdentityFields):
    """Update a collection. All fields optional."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    collection_type: Optional[str] = Field(None, max_length=50)
    archival_metadata: Optional[Dict[str, Any]] = None
    parent_collection_id: Optional[int] = Field(None, description="Move to different parent collection")


class CollectionRead(CollectionBase):
    id: int
    filename_stem: Optional[str] = None
    external_id: Optional[str] = None
    date: Optional[str] = None
    date_start: Optional[dt_date] = None
    date_end: Optional[dt_date] = None
    sequence: Optional[int] = None
    custom_attributes: Optional[str] = None
    # Read-only: the capture path raises it, and no request schema accepts it.
    capture_counter: int = 0
    project_id: Optional[int] = None
    parent_collection_id: Optional[int] = None
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    # Direct record count (Record.collection_id == this collection's id).
    # None unless the endpoint explicitly populates it (list/hierarchy).
    record_count: Optional[int] = None
    # Most recent Record.modified_at inside this collection — when it was last
    # worked on, as opposed to updated_at, which only moves when the collection
    # row itself is edited (renamed, re-described). Null when it has no records.
    # None unless the endpoint explicitly populates it (list).
    last_activity_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CollectionWithChildren(CollectionRead):
    """Collection with nested child collections (for hierarchical views)."""
    child_collections: List["CollectionRead"] = []

    class Config:
        from_attributes = True
