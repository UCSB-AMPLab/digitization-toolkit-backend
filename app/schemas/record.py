from __future__ import annotations
from typing import Optional, List, Literal
from pydantic import BaseModel, field_validator, model_validator
from datetime import datetime

# Valid status values. A record has no "captured" resting state — it enters
# the queue as "in_review" the moment it's captured (NEH-208). A reviewer can
# undo a mistaken approve/reject back to "in_review" (see STATUS_TRANSITIONS
# below) — that's a plain status reset, not a recapture and not a formal
# rejection, so it carries no reason/audit entry.
RecordStatus = Literal["in_review", "rejected", "approved"]

# Allowed status transitions: (from_status, to_status) -> set of roles that can perform it.
#
# Formal rejection is deliberately NOT here — it's its own endpoint (POST
# /records/{id}/reject) with a mandatory predefined_reason, not reachable
# through this generic status-update path. Recapture (rejected -> in_review
# as a side effect of a new capture actually arriving) is also not here —
# that's driven entirely by app/api/cameras.py, not a client-chosen status
# value.
#
# approved -> in_review and rejected -> in_review ARE here: these are the
# "undo my mistake" resets a reviewer can trigger directly from the record
# view (NEH-209) — no reason required since nothing formal is being
# recorded, just moving the record back into the review queue.
STATUS_TRANSITIONS: dict[tuple[str, str], set[str]] = {
	("in_review", "approved"): {"reviewer", "admin"},
	("approved", "in_review"): {"reviewer", "admin"},
	("rejected", "in_review"): {"reviewer", "admin"},
}

# The predefined rejection reasons, mirroring the list the annotation
# feature's "Marcar error" already uses client-side
# (LeftSidebar.svelte ERROR_TYPES) — this is the first place either list is
# enforced server-side.
PREDEFINED_REJECTION_REASONS = ("blur", "glare", "shadow", "focus", "exposure", "dirt")
PredefinedRejectionReason = Literal["blur", "glare", "shadow", "focus", "exposure", "dirt"]

# Which camera setup produced a document: one image, or an L+R pair fired
# together on a single shutter press. Set once at capture time.
CaptureMode = Literal["single", "dual"]


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
	# File paths are managed server-side and must never be client-settable
	sequence: Optional[int] = None
	role: Optional[str] = None


class RecordImageRead(RecordImageBase):
	id: int
	record_id: int
	created_at: Optional[datetime]
	camera_settings: Optional[CameraSettingsRead] = None
	exif_data: Optional[ExifDataRead] = None
	# Audit trail (NEH-208): False once a recapture has superseded this
	# image — it stays queryable via GET /records/{id}/rejections but drops
	# out of the default image list/gallery/export.
	is_current: bool = True
	superseded_at: Optional[datetime] = None

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
	status: RecordStatus = "in_review"
	sequence: Optional[int] = None
	# Required: the camera capture endpoints always set this explicitly;
	# manual record creation (POST /records/) must declare it up front since
	# rejection scope resolution and the recapture mode-match guard both
	# depend on it (NEH-208).
	capture_mode: CaptureMode


class RecordCreate(RecordBase):
	project_id: Optional[int] = None
	collection_id: Optional[int] = None
	created_by: Optional[str] = None

	@model_validator(mode="after")
	def _single_parent(self):
		if self.project_id is not None and self.collection_id is not None:
			raise ValueError("A record belongs to either a project or a collection, not both.")
		return self


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


class BulkStatusUpdate(BaseModel):
	record_ids: List[int]
	status: RecordStatus

	@field_validator("record_ids")
	@classmethod
	def ids_not_empty(cls, v: List[int]) -> List[int]:
		if not v:
			raise ValueError("record_ids must not be empty")
		return v


# ==============================================================================
# Rejection schemas (NEH-208)
# ==============================================================================

class RecordRejectRequest(BaseModel):
	predefined_reason: PredefinedRejectionReason
	comment: Optional[str] = None


class RecordRejectionRead(BaseModel):
	id: int
	record_id: int
	predefined_reason: str
	comment: Optional[str] = None
	rejected_by: Optional[str] = None
	rejected_at: Optional[datetime]
	# The image(s) this specific rejection flagged — both L and R for a
	# dual-mode document, the one image for single-mode.
	images: List[RecordImageRead] = []

	class Config:
		from_attributes = True


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
# RecordAnnotation Schemas (QA "Anotaciones" tab: flagged errors + notes)
# ==============================================================================

class RecordAnnotationCreate(BaseModel):
	error_types: List[str] = []
	note: Optional[str] = None

	@field_validator("note")
	@classmethod
	def blank_note_is_none(cls, v: Optional[str]) -> Optional[str]:
		if v is not None and not v.strip():
			return None
		return v

	@model_validator(mode="after")
	def at_least_one_field(self) -> "RecordAnnotationCreate":
		if not self.error_types and not self.note:
			raise ValueError("An annotation needs at least one error type or a note")
		return self


class RecordAnnotationRead(BaseModel):
	id: int
	record_id: int
	error_types: List[str] = []
	note: Optional[str] = None
	created_at: Optional[datetime]
	created_by: Optional[str] = None

	class Config:
		from_attributes = True


# ==============================================================================
# Legacy compatibility type alias (for gradual migration)
# ==============================================================================
# These can help during API transition
RecordWithImages = RecordRead  # Explicit name for record with images loaded
