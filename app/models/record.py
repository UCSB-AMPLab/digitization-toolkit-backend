from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey, CheckConstraint, JSON, Boolean
from sqlalchemy.orm import relationship

from app.core.db import Base


class Record(Base):
	"""
	Represents a conceptual archival document/object (book, map, document, etc.).
	A Record can have multiple associated images (captures).
	"""
	__tablename__ = "records"

	id = Column(Integer, primary_key=True, index=True)
	title = Column(String(255), nullable=False)
	description = Column(Text, nullable=True)
	
	# Archival/descriptive metadata
	object_typology = Column(String(50), nullable=True)  # book, dossier, document, map, planimetry, other
	author = Column(String(255), nullable=True)
	material = Column(String(255), nullable=True)
	date = Column(String(50), nullable=True)
	custom_attributes = Column(Text, nullable=True)  # JSON string for custom fields
	
	# Organizational hierarchy
	project_id = Column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
	collection_id = Column(Integer, ForeignKey("collections.id", ondelete="SET NULL"), nullable=True, index=True)
	
	# QA workflow — a record enters the queue as "in_review" as soon as it's
	# captured; there is no separate "captured" resting state (NEH-208).
	status = Column(String(20), nullable=False, default="in_review")  # in_review, rejected, approved
	sequence = Column(Integer, nullable=True)  # ordering within a collection

	# Which camera setup produced this document: "single" (one image) or
	# "dual" (an L+R pair fired together on one shutter press). Set once at
	# capture time and never changed — it's what rejection scope resolution
	# and the recapture mode-match guard key off of (NEH-208).
	capture_mode = Column(String(10), nullable=False)  # single, dual

	# Audit fields
	created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
	modified_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
	created_by = Column(String(255), nullable=True)

	# Relationships
	images = relationship("RecordImage", back_populates="record", cascade="all, delete-orphan")
	project = relationship("Project", back_populates="records")
	collection = relationship("Collection", back_populates="records")
	annotations = relationship("RecordAnnotation", back_populates="record", cascade="all, delete-orphan", order_by="RecordAnnotation.created_at.desc()")
	rejections = relationship("RecordRejection", back_populates="record", cascade="all, delete-orphan", order_by="RecordRejection.rejected_at.desc()")

	# Constraint: must have either project_id OR collection_id (or neither, but not both)
	__table_args__ = (
		CheckConstraint(
			'NOT (project_id IS NOT NULL AND collection_id IS NOT NULL)',
			name='check_record_single_parent'
		),
		CheckConstraint(
			"status IN ('in_review','rejected','approved')",
			name='check_record_status'
		),
		CheckConstraint(
			"capture_mode IN ('single','dual')",
			name='check_record_capture_mode'
		),
	)


class RecordImage(Base):
	"""
	Represents a single captured image file that belongs to a Record.
	Links to the capture manifest via capture_id.
	"""
	__tablename__ = "record_images"

	id = Column(Integer, primary_key=True, index=True)
	
	# Link to parent Record
	record_id = Column(Integer, ForeignKey("records.id", ondelete="CASCADE"), nullable=False, index=True)
	
	# Capture traceability - links to manifest.jsonl entries
	capture_id = Column(String(36), nullable=True, index=True)  # UUID from CaptureRecord
	pair_id = Column(String(36), nullable=True, index=True)     # Groups dual-camera captures
	
	# Ordering/sequencing within the record
	sequence = Column(Integer, nullable=True)  # Page number, capture order, etc.
	role = Column(String(50), nullable=True)   # "left", "right", "single", "overview"
	
	# File metadata
	filename = Column(String(255), nullable=False, index=True)
	file_path = Column(String(512), nullable=False)
	thumbnail_path = Column(String(512), nullable=True)
	file_size = Column(Integer, nullable=True)
	format = Column(String(50), nullable=False)
	
	# Image technical properties
	resolution_width = Column(Integer, nullable=True)
	resolution_height = Column(Integer, nullable=True)
	
	# Audit fields
	created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
	uploaded_by = Column(String(255), nullable=True)

	# Rejection/recapture audit trail (NEH-208). A rejected image is never
	# deleted or overwritten: it stays in place with is_current flipped to
	# False and rejection_id pointing at the RecordRejection that flagged
	# it, so it remains queryable via GET /records/{id}/rejections after a
	# recapture installs its replacement as the new current image.
	is_current = Column(Boolean, nullable=False, default=True)
	superseded_at = Column(DateTime, nullable=True)
	rejection_id = Column(Integer, ForeignKey("record_rejections.id", ondelete="SET NULL"), nullable=True, index=True)

	# Relationships
	record = relationship("Record", back_populates="images")
	camera_settings = relationship("CameraSettings", back_populates="record_image", uselist=False, cascade="all, delete-orphan")
	exif_data = relationship("ExifData", back_populates="record_image", uselist=False, cascade="all, delete-orphan")
	rejection = relationship("RecordRejection", back_populates="images")


class RecordAnnotation(Base):
	"""
	A reviewer annotation on a Record: a flagged error typology and/or a free-text note.
	Created during the QA pass ("Anotaciones" tab) and scoped to a single record.
	"""
	__tablename__ = "record_annotations"

	id = Column(Integer, primary_key=True, index=True)
	record_id = Column(Integer, ForeignKey("records.id", ondelete="CASCADE"), nullable=False, index=True)

	error_types = Column(JSON, nullable=True)  # list of error type ids, e.g. ["blur", "glare"]
	note = Column(Text, nullable=True)

	created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
	created_by = Column(String(255), nullable=True)

	record = relationship("Record", back_populates="annotations")


class RecordRejection(Base):
	"""
	Audit record of a single rejection event on a Record (NEH-208).

	Created once per rejection with a mandatory predefined reason (the same
	list the annotation feature's "Marcar error" uses) plus an optional
	free-text comment. Links to every RecordImage that was current at the
	time of rejection via RecordImage.rejection_id — those images are never
	deleted; a later recapture supersedes them (is_current=False) without
	touching this row, so the rejected file(s) + reason + timestamp + user
	stay queryable indefinitely via GET /records/{id}/rejections.
	"""
	__tablename__ = "record_rejections"

	id = Column(Integer, primary_key=True, index=True)
	record_id = Column(Integer, ForeignKey("records.id", ondelete="CASCADE"), nullable=False, index=True)

	predefined_reason = Column(String(20), nullable=False)  # blur, glare, shadow, focus, exposure, dirt
	comment = Column(Text, nullable=True)

	rejected_by = Column(String(255), nullable=True)
	rejected_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

	record = relationship("Record", back_populates="rejections")
	images = relationship("RecordImage", back_populates="rejection")

	__table_args__ = (
		CheckConstraint(
			"predefined_reason IN ('blur','glare','shadow','focus','exposure','dirt')",
			name='check_record_rejection_reason'
		),
	)


class ExifData(Base):
	__tablename__ = "exif_data"

	id = Column(Integer, primary_key=True, index=True)
	record_image_id = Column(Integer, ForeignKey("record_images.id", ondelete="CASCADE"), unique=True, nullable=False)

	make = Column(String(255), nullable=True)
	model = Column(String(255), nullable=True)
	orientation = Column(Integer, nullable=True)
	x_resolution = Column(Integer, nullable=True)
	y_resolution = Column(Integer, nullable=True)
	resolution_unit = Column(String(50), nullable=True)
	software = Column(String(255), nullable=True)
	datetime_original = Column(DateTime, nullable=True)
	datetime_digitized = Column(DateTime, nullable=True)

	thumbnail_data = Column(String(255), nullable=True)

	exposure_time = Column(String(50), nullable=True)
	f_number = Column(String(50), nullable=True)
	iso_speed_ratings = Column(Integer, nullable=True)
	focal_length = Column(String(50), nullable=True)
	focal_length_in_35mm = Column(Integer, nullable=True)
	lens_model = Column(String(255), nullable=True)
	flash = Column(String(100), nullable=True)
	white_balance = Column(String(100), nullable=True)
	exposure_compensation = Column(String(50), nullable=True)
	metering_mode = Column(String(100), nullable=True)
	light_source = Column(String(100), nullable=True)
	color_space = Column(String(100), nullable=True)

	gps_latitude = Column(String(100), nullable=True)
	gps_longitude = Column(String(100), nullable=True)
	gps_altitude = Column(String(100), nullable=True)
	gps_timestamp = Column(DateTime, nullable=True)

	raw_exif = Column(Text, nullable=True)

	created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

	record_image = relationship("RecordImage", back_populates="exif_data")
