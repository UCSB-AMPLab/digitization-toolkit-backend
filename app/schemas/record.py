from __future__ import annotations
from typing import Optional, List, Literal
from pydantic import BaseModel, field_validator
from datetime import datetime

# Valid status values
RecordStatus = Literal["captured", "in_review", "rejected", "approved"]

# Allowed status transitions: (from_status, to_status) -> set of roles that can perform it
STATUS_TRANSITIONS: dict[tuple[str, str], set[str]] = {
	("captured",  "in_review"): {"operator", "admin", "reviewer"},
	("in_review", "rejected"):  {"reviewer", "admin"},
	("in_review", "approved"):  {"reviewer", "admin"},
	("in_review", "captured"):  {"operator", "admin"},
	("rejected",  "captured"):  {"operator", "admin"},
	("approved",  "rejected"):  {"reviewer", "admin"},
	("approved",  "captured"):  {"admin"},
}


# ==============================================================================
# ExifData Schemas
# ==============================================================================

class ExifDataBase(BaseModel):
	make: Optional[str] = None
	model: Optional[str] = None
	orientation: Optional[int] = None
	software: Optional[str] = None
	datetime_original: Optional[datetime] = None
	datetime_digitized: Optional[datetime] = None
	raw_exif: Optional[str] = None


class ExifDataCreate(ExifDataBase):
	pass


class ExifDataRead(ExifDataBase):
	id: int
	created_at: Optional[datetime]

	class Config:
		from_attributes = True


# ==============================================================================
# CameraSettings Schemas
# ==============================================================================

class CameraSettingsBase(BaseModel):
	camera_model: Optional[str] = None
	camera_manufacturer: Optional[str] = None
	lens_model: Optional[str] = None
	iso: Optional[int] = None
	aperture: Optional[float] = None
	shutter_speed: Optional[str] = None
	focal_length: Optional[float] = None
	exposure_compensation: Optional[float] = None
	white_balance: Optional[str] = None
	flash_used: Optional[bool] = None


class CameraSettingsCreate(CameraSettingsBase):
	pass


class CameraSettingsRead(CameraSettingsBase):
	id: int
	record_image_id: int
	created_at: Optional[datetime]

	class Config:
		from_attributes = True


# ==============================================================================
# RecordImage Schemas (Individual capture/image)
# ==============================================================================

class RecordImageBase(BaseModel):
	filename: str
	file_path: str
	thumbnail_path: Optional[str] = None
	file_size: Optional[int] = None
	format: str
	resolution_width: Optional[int] = None
	resolution_height: Optional[int] = None
	capture_id: Optional[str] = None
	pair_id: Optional[str] = None
	sequence: Optional[int] = None
	role: Optional[str] = None  # "left", "right", "single", "overview"
	uploaded_by: Optional[str] = None


class RecordImageCreate(RecordImageBase):
	camera_settings: Optional[CameraSettingsCreate] = None
	exif_data: Optional[ExifDataCreate] = None


class RecordImageUpdate(BaseModel):
	sequence: Optional[int] = None
	role: Optional[str] = None
	thumbnail_path: Optional[str] = None


class RecordImageRead(RecordImageBase):
	id: int
	record_id: int
	created_at: Optional[datetime]
	camera_settings: Optional[CameraSettingsRead] = None
	exif_data: Optional[ExifDataRead] = None

	class Config:
		from_attributes = True


# ==============================================================================
# Record Schemas (Conceptual document/object)
# ==============================================================================

class RecordBase(BaseModel):
	title: str
	description: Optional[str] = None
	object_typology: Optional[str] = None  # book, dossier, document, map, planimetry, other
	author: Optional[str] = None
	material: Optional[str] = None
	date: Optional[str] = None
	custom_attributes: Optional[str] = None  # JSON string for custom fields
	status: str = "captured"
	sequence: Optional[int] = None
	rejection_note: Optional[str] = None


class RecordCreate(RecordBase):
	project_id: Optional[int] = None
	collection_id: Optional[int] = None
	created_by: Optional[str] = None


class RecordUpdate(BaseModel):
	title: Optional[str] = None
	description: Optional[str] = None
	object_typology: Optional[str] = None
	author: Optional[str] = None
	material: Optional[str] = None
	date: Optional[str] = None
	custom_attributes: Optional[str] = None
	project_id: Optional[int] = None
	collection_id: Optional[int] = None


class RecordRead(RecordBase):
	id: int
	project_id: Optional[int] = None
	collection_id: Optional[int] = None
	created_by: Optional[str] = None
	created_at: Optional[datetime]
	modified_at: Optional[datetime]
	images: List[RecordImageRead] = []

	class Config:
		from_attributes = True


# ==============================================================================
# Status update schemas
# ==============================================================================

class RecordStatusUpdate(BaseModel):
	status: RecordStatus
	rejection_note: Optional[str] = None


class BulkStatusUpdate(BaseModel):
	record_ids: List[int]
	status: RecordStatus
	rejection_note: Optional[str] = None

	@field_validator("record_ids")
	@classmethod
	def ids_not_empty(cls, v: List[int]) -> List[int]:
		if not v:
			raise ValueError("record_ids must not be empty")
		return v


# ==============================================================================
# Reorder schema
# ==============================================================================

class ReorderRecords(BaseModel):
	ordered_ids: List[int]

	@field_validator("ordered_ids")
	@classmethod
	def ids_not_empty(cls, v: List[int]) -> List[int]:
		if not v:
			raise ValueError("ordered_ids must not be empty")
		return v


# ==============================================================================
# Legacy compatibility type alias (for gradual migration)
# ==============================================================================
# These can help during API transition
RecordWithImages = RecordRead  # Explicit name for record with images loaded
