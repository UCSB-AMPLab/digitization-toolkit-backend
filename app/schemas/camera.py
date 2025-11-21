from typing import Optional
from pydantic import BaseModel
from datetime import datetime


class CameraSettingsBase(BaseModel):
	document_image_id: Optional[int] = None
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
	document_image_id: int


class CameraSettingsRead(CameraSettingsBase):
	id: int
	created_at: Optional[datetime]

	class Config:
		form_attributes = True
