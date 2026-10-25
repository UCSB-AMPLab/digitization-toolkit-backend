from __future__ import annotations
from typing import Optional, List, Literal
from pydantic import BaseModel, field_validator, model_validator
from datetime import datetime
# Aliased: `date` is already a field name on RecordBase (the free-text display
# date), so importing the type under its own name would shadow it.
from datetime import date as dt_date

# Valid status values. A record is born "captured", the state in which it is
# still the operator's, and leaves it when she promotes the collection she has
# finished (NEH-260).
#
# Without that state, a fresh capture is indistinguishable from work handed
# over for review, the operator can neither redo nor delete her own frame
# until a reviewer formally rejects it, and the state machine does not match
# the one the Fisqua exchange contract describes, which is
# captured -> in_review -> approved | rejected.
#
# A reviewer can undo a mistaken approve or reject back to "in_review" (see
# STATUS_TRANSITIONS below) — a plain status reset, not a recapture and not a
# formal rejection, so it carries no reason and no rejection record. The
# change itself is dated in record_status_changes, like every other.
RecordStatus = Literal["captured", "in_review", "rejected", "approved"]

# Allowed status transitions: (from_status, to_status) -> set of roles that can perform it.
#
# captured -> in_review is promotion: the operator handing finished work over.
# It is reachable per record, but the act she performs is per collection, so
# the endpoint she uses is POST /collections/{id}/promote, which applies this
# transition across the collection's captured records.
#
# rejected -> captured is prepare-for-retake: a rejected record returns to her
# worklist, and is promoted again with its collection. Leaving captured is
# guarded by the replacement-image invariant in app/api/records.py — the
# table checks only the pair and the role.
#
# Three transitions are deliberately absent:
#
# - in_review -> rejected. Formal rejection is its own endpoint (POST
#   /records/{id}/reject) with a mandatory predefined_reason. RecordStatusUpdate
#   types its status as RecordStatus rather than a restricted literal, so this
#   table is the only thing keeping the generic and bulk endpoints from writing
#   "rejected" with no reason and no record_rejections row.
# - in_review -> captured. Taking a handover back is a collection-level act
#   (POST /collections/{id}/take-back) and stays out of the per-record and bulk
#   paths, so one record cannot be withdrawn from a reviewer on its own.
# - A retake, which supersedes the current images and is driven entirely by
#   app/api/cameras.py rather than by a client-chosen status value. It leaves a
#   captured record captured, and returns a rejected one to captured.
#
# approved -> in_review and rejected -> in_review ARE here: these are the
# "undo my mistake" resets a reviewer can trigger directly from the record
# view (NEH-209) — no reason required since nothing formal is being
# recorded, just moving the record back into the review queue.
STATUS_TRANSITIONS: dict[tuple[str, str], set[str]] = {
	("captured", "in_review"): {"operator", "admin"},
	("in_review", "approved"): {"reviewer", "admin"},
	("approved", "in_review"): {"reviewer", "admin"},
	("rejected", "in_review"): {"reviewer", "admin"},
	("rejected", "captured"): {"operator", "admin"},
}

# The predefined rejection reasons. The first six mirror the list the
# annotation feature's "Marcar error" uses client-side; the last three are
# framing (a page cut off or badly framed), obstruction (a finger, weight or
# object in the shot) and other (anything else, which must be described in
# the comment — see RecordRejectRequest). The table
# holds the same list in check_record_rejection_reason; change both together,
# with a migration.
PREDEFINED_REJECTION_REASONS = (
	"blur", "glare", "shadow", "focus", "exposure", "dirt",
	"framing", "obstruction", "other",
)
PredefinedRejectionReason = Literal[
	"blur", "glare", "shadow", "focus", "exposure", "dirt",
	"framing", "obstruction", "other",
]

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
	# Set when the file was freed from the card with its verified object in
	# the offsite bucket (app.core.offsite_free): stored offsite, not missing.
	# The master, and a RAW master's preview, each on its own.
	master_freed_at: Optional[datetime] = None
	preview_freed_at: Optional[datetime] = None
	# Clockwise degrees that display the image upright, recorded at capture;
	# None for an upload or an image captured before it was recorded.
	rotate_deg: Optional[int] = None
	# Fisqua's rotation correction, quarter-turns clockwise (1-3), for
	# display; None when Fisqua has made none (contract 1.2 §9.5).
	rotation_correction_quarter_turns: Optional[int] = None

	class Config:
		from_attributes = True


# ==============================================================================
# Record Schemas (Conceptual document/object)
# ==============================================================================

class RecordBase(BaseModel):
	title: str
	description: Optional[str] = None
	author: Optional[str] = None
	date: Optional[str] = None
	custom_attributes: Optional[str] = None  # JSON string for custom fields
	# The birth state. The create path ignores it — create_record builds the
	# Record without a status, so the model default governs there too.
	status: RecordStatus = "captured"
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
	author: Optional[str] = None
	date: Optional[str] = None
	custom_attributes: Optional[str] = None
	project_id: Optional[int] = None
	collection_id: Optional[int] = None


class RecordRead(RecordBase):
	id: int
	# The queryable form of `date`: the span the record's date covers, with
	# day resolution. Read only: no writer exists yet, and who may set a
	# range, and in what form, is not settled.
	# Either end may be absent ("after 1750", "before 1800"); an end before
	# its start is refused by the database.
	date_start: Optional[dt_date] = None
	date_end: Optional[dt_date] = None
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

# Fisqua keeps the first 2,000 characters of a rejection's note (contract
# 1.2, its MAX_NOTE_CHARS), so a longer comment is refused here, where the
# reviewer can shorten it, rather than cut there without anyone knowing.
REJECTION_COMMENT_MAX_LENGTH = 2000
REJECTION_COMMENT_TOO_LONG = "A rejection comment can be at most 2,000 characters long."


class RecordRejectRequest(BaseModel):
	predefined_reason: PredefinedRejectionReason
	comment: Optional[str] = None

	@field_validator("comment")
	@classmethod
	def _comment_fits_fisqua(cls, v: Optional[str]) -> Optional[str]:
		if v is not None and len(v) > REJECTION_COMMENT_MAX_LENGTH:
			raise ValueError(REJECTION_COMMENT_TOO_LONG)
		return v

	@model_validator(mode="after")
	def _other_needs_a_comment(self) -> "RecordRejectRequest":
		# 'other' names no defect, so the comment is the only record of what
		# was wrong. The other reasons keep the comment optional.
		if self.predefined_reason == "other" and not (self.comment or "").strip():
			raise ValueError("A rejection with reason 'other' needs a comment saying what is wrong.")
		return self


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


class RecordStatusChangeRead(BaseModel):
	id: int
	# None once the record has been deleted; its history is kept.
	record_id: Optional[int] = None
	collection_id: Optional[int] = None
	from_status: RecordStatus
	to_status: RecordStatus
	changed_by: Optional[str] = None
	changed_at: datetime

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
# Type alias
# ==============================================================================
RecordWithImages = RecordRead  # Explicit name for record with images loaded
