from __future__ import annotations
from typing import Optional, List
from pydantic import BaseModel
from datetime import datetime


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
# Legacy compatibility type alias (for gradual migration)
# ==============================================================================
# These can help during API transition
RecordWithImages = RecordRead  # Explicit name for record with images loaded
