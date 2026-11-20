# Version: 0.3.3

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
    # Required: contract 1.1 §3 says a volume created on the device must be
    # given a stem, and Captua never makes one up. The column stays nullable
    # for volumes created before stems existed, which get theirs through an
    # update.
    filename_stem: str = Field(
        ..., max_length=FILENAME_STEM_MAX_LENGTH, pattern=FILENAME_STEM_PATTERN,
        description="Lowercase slug the volume's image file names are built from, e.g. co-ahrb-aht-003",
    )
    project_id: Optional[int] = Field(None, description="Parent project (for top-level collections)")
    parent_collection_id: Optional[int] = Field(None, description="Parent collection (for nested subcollections)")
    created_by: Optional[str] = None
    # Keep the new volume on the unit (NEH-320): Captua copies none of its
    # images offsite and sends none of its manifests to Fisqua until an
    # admin releases it. Only an admin may set it; in offline mode every new
    # volume is kept whatever is sent.
    kept_on_unit: Optional[bool] = None

    class Config:
        json_schema_extra = {
            "example": {
                "name": "Box 42",
                "description": "Contains correspondence from 1920-1925",
                "collection_type": "box",
                "project_id": 1,
                "filename_stem": "co-ahrb-aht-003",
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
    # Whether the volume keeps its row to keep its stem: it has a stem and
    # has captured, so DELETE refuses it even once its records are moved
    # out. Lets the interface warn before "move the records, then delete".
    # Read-only. Costs an image query, and an offsite-object query when that
    # finds no image, per volume that has a stem and a counter of 0.
    stem_reserved: bool = False
    project_id: Optional[int] = None
    parent_collection_id: Optional[int] = None
    # Set only by the queue sync with Fisqua (app.core.fisqua_queue), and
    # read-only here: the unit username the volume's queue entry is
    # assigned to, and when a withdrawn entry hid it from Cola.
    assignee_username: Optional[str] = None
    queue_hidden_at: Optional[datetime] = None
    # Read-only here (NEH-320): whether Captua keeps the volume on the unit.
    # Set at creation, by POST /collections/{id}/keep-on-unit, and cleared by
    # POST /collections/{id}/release-from-unit or a switch out of offline mode.
    kept_on_unit: bool = False
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


class AdoptRenameRead(BaseModel):
    """One source image's names before and after an adoption (app.core.adopt_captures.ImageRename)."""
    image_id: int
    record_id: int
    is_current: bool
    number: int
    old_name: str
    new_name: str
    old_path: str
    new_path: str
    old_preview_path: Optional[str] = None
    new_preview_path: Optional[str] = None
    old_raw_buffer_path: Optional[str] = None
    new_raw_buffer_path: Optional[str] = None
    old_thumbnail_path: Optional[str] = None
    new_thumbnail_path: Optional[str] = None
    old_pair_id: Optional[str] = None
    new_pair_id: Optional[str] = None
    capture_id: Optional[str] = None
    old_keys: Dict[str, str] = {}
    new_keys: Dict[str, str] = {}


class AdoptPlanRead(BaseModel):
    """The dry run of an adoption: the plan, with the first `limit` renames."""
    source_id: int
    target_id: int
    may_adopt: bool
    refusals: List[str]
    details: Dict[str, Any]
    source_stem: Optional[str] = None
    target_stem: Optional[str] = None
    source_project_id: Optional[int] = None
    target_project_id: Optional[int] = None
    project_changes: bool
    records: int
    images_current: int
    images_superseded: int
    bytes: Dict[str, int]
    counter_before: int
    counter_after: int
    renames_count: int
    renames_truncated: bool
    renames: List[AdoptRenameRead]
    uncovered: List[Dict[str, str]]
    thumbnails_missing: List[Dict[str, Any]]
    capture_lines: int
    unaccounted_capture_lines: List[str]


class AdoptResultRead(BaseModel):
    """What an adoption did (app.core.adopt_apply.adopt_captures)."""
    source_id: int
    target_id: int
    records: int
    images: int
    files_renamed: int
    rows_retired: int
    counter_after: int
    source_lines_appended: int
    target_lines_appended: bool
    target_lines_pending: bool
    recovery_pending: bool
