from __future__ import annotations
from typing import Optional
from pydantic import BaseModel
from datetime import datetime


class ExifDataBase(BaseModel):
	make: Optional[str] = None
	model: Optional[str] = None
	orientation: Optional[int] = None
	software: Optional[str] = None
	datetime_original: Optional[datetime] = None
	datetime_digitized: Optional[datetime] = None
	raw_exif: Optional[str] = None


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


class ExifDataCreate(ExifDataBase):
	pass


class DocumentBase(BaseModel):
	filename: str
	title: Optional[str] = None
	description: Optional[str] = None
	file_path: str
	file_size: Optional[int] = None
	format: str
	resolution_width: Optional[int] = None
	resolution_height: Optional[int] = None
	uploaded_by: Optional[str] = None


class DocumentCreate(DocumentBase):
	camera_settings: Optional[CameraSettingsCreate] = None
	exif_data: Optional[ExifDataCreate] = None


class ExifDataRead(ExifDataBase):
	id: int
	created_at: Optional[datetime]

	class Config:
		orm_mode = True


class CameraSettingsRead(CameraSettingsBase):
	id: int
	document_image_id: int
	created_at: Optional[datetime]

	class Config:
		orm_mode = True


class DocumentRead(DocumentBase):
	id: int
	created_at: Optional[datetime]
	modified_at: Optional[datetime]
	camera_settings: Optional[CameraSettingsRead] = None
	exif_data: Optional[ExifDataRead] = None

	class Config:
		form_attributes = True
