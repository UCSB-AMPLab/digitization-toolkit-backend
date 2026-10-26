# Version: 0.1.0-beta

from datetime import datetime, timezone
from sqlalchemy import BigInteger, Column, Integer, String, Date, DateTime, Text, ForeignKey, CheckConstraint, JSON, Boolean
from sqlalchemy.orm import relationship

from app.core.db import Base


class ImmutableFieldError(ValueError):
	"""A write would change a value that is set once and never again."""


class Record(Base):
	"""
	Represents a conceptual archival document/object (book, map, document, etc.).
	A Record can have multiple associated images (captures).
	"""
	__tablename__ = "records"

	id = Column(Integer, primary_key=True, index=True)
	title = Column(String(255), nullable=False)
	description = Column(Text, nullable=True)
	
	# Archival/descriptive metadata.
	author = Column(String(255), nullable=True)
	date = Column(String(50), nullable=True)
	custom_attributes = Column(Text, nullable=True)  # JSON string for custom fields

	# The queryable form of the same date `date` holds verbally. `date` is a
	# display field — "1750", "c. 1750", "mediados del siglo XVIII" — and none
	# of that can be compared, sorted or filtered in SQL; parking a structured
	# range in custom_attributes would not help, since that column is Text
	# holding encoded JSON rather than a JSON column. So the range gets two
	# real DATE columns beside the display field, and `date` stays.
	#
	# A span with day resolution. A document known only to its year is the
	# whole of that year, one known to its month the whole of that month, one
	# dated to a day a span of one day. What the pair cannot express is the
	# precision of what is known — "sometime in 1750" and "throughout 1750"
	# are the same two values — or approximation; `date` carries that.
	#
	# Independently nullable, because "after 1750" and "before 1800" are real
	# archival dates and both ends are not always known. An end before its
	# start is refused by check_record_date_range below — a database
	# constraint rather than an ORM guard, because an ordering rule compares
	# two columns of one row, which is what a CHECK does, so it holds against
	# a bulk UPDATE and raw SQL as well.
	#
	# Nothing writes them yet: they are absent from every request schema and
	# exposed read-only on RecordRead.
	date_start = Column(Date, nullable=True)
	date_end = Column(Date, nullable=True)

	# Organizational hierarchy
	project_id = Column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
	collection_id = Column(Integer, ForeignKey("collections.id", ondelete="SET NULL"), nullable=True, index=True)
	
	# QA workflow. A record is born "captured" and stays there until the
	# operator promotes the collection it belongs to (NEH-260); "in_review"
	# means handed over for review, which is why deletion is locked from that
	# point on. See app/schemas/record.py for the transition table.
	status = Column(String(20), nullable=False, default="captured")  # captured, in_review, rejected, approved
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
	# No delete cascade: a deleted record's history is kept. On delete the ORM
	# sets record_id to NULL on these rows, as the FK's ON DELETE SET NULL does
	# in the database.
	status_changes = relationship("RecordStatusChange", back_populates="record", order_by="RecordStatusChange.id.desc()")

	# Constraint: must have either project_id OR collection_id (or neither, but not both)
	__table_args__ = (
		CheckConstraint(
			'NOT (project_id IS NOT NULL AND collection_id IS NOT NULL)',
			name='check_record_single_parent'
		),
		CheckConstraint(
			"status IN ('captured','in_review','rejected','approved')",
			name='check_record_status'
		),
		CheckConstraint(
			"capture_mode IN ('single','dual')",
			name='check_record_capture_mode'
		),
		# An end before its start. A half-open range passes: with either end
		# NULL there is nothing to compare. The two NULL clauses say so
		# explicitly, which a bare `date_end >= date_start` would also do —
		# that comparison is NULL when either end is NULL, and a CHECK accepts
		# NULL — so the explicit form is for a reader, not for the database,
		# and the two are interchangeable. `>=` and not `>` is not: a document
		# dated to a single day has a start equal to its end.
		CheckConstraint(
			'date_start IS NULL OR date_end IS NULL OR date_end >= date_start',
			name='check_record_date_range'
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

	# Clockwise rotation in degrees (0, 90, 180, 270) that displays the image
	# upright, as the capturing body was set (NEH-71). A JPEG master also
	# carries it as its EXIF Orientation; a RAW master is never rewritten, so
	# this and the capture record's `files` entry are where its rotation is
	# kept. NULL for an upload and for an image captured before it was recorded.
	rotate_deg = Column(Integer, nullable=True)
	# Fisqua's correction of the image's rotation (contract 1.2 §9.5), in
	# quarter-turns clockwise, 1 to 3, on top of the displayed orientation.
	# For display only: no file is changed, and Fisqua applies it in
	# processing. NULL when Fisqua has made none (app.core.fisqua_decisions).
	rotation_correction_quarter_turns = Column(Integer, nullable=True)
	
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

	# Freed from the card (app.core.offsite_free): the file was removed with
	# its verified object still in the offsite bucket, so it is stored
	# offsite, not missing. The master and a RAW master's preview are each
	# freed on their own; the size is kept because a freed file cannot be
	# measured on the card. Each pair is set or unset together
	# (check_record_image_*_freed). Cleared by a restore.
	master_freed_at = Column(DateTime, nullable=True)
	master_freed_bytes = Column(BigInteger, nullable=True)
	preview_freed_at = Column(DateTime, nullable=True)
	preview_freed_bytes = Column(BigInteger, nullable=True)

	__table_args__ = (
		CheckConstraint(
			"rotate_deg IS NULL OR rotate_deg IN (0, 90, 180, 270)",
			name="check_record_image_rotate_deg",
		),
		CheckConstraint(
			"rotation_correction_quarter_turns IS NULL OR rotation_correction_quarter_turns IN (1, 2, 3)",
			name="check_record_image_rotation_correction",
		),
		CheckConstraint(
			"(master_freed_at IS NULL AND master_freed_bytes IS NULL) OR "
			"(master_freed_at IS NOT NULL AND master_freed_bytes IS NOT NULL AND master_freed_bytes >= 0)",
			name="check_record_image_master_freed",
		),
		CheckConstraint(
			"(preview_freed_at IS NULL AND preview_freed_bytes IS NULL) OR "
			"(preview_freed_at IS NOT NULL AND preview_freed_bytes IS NOT NULL AND preview_freed_bytes >= 0)",
			name="check_record_image_preview_freed",
		),
	)

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

	Created once per rejection with a mandatory predefined reason
	(PREDEFINED_REJECTION_REASONS) plus a free-text comment, optional except
	for the reason 'other', where the API requires it. Links to every RecordImage that was current at the
	time of rejection via RecordImage.rejection_id — those images are never
	deleted; a later recapture supersedes them (is_current=False) without
	touching this row, so the rejected file(s) + reason + timestamp + user
	stay queryable indefinitely via GET /records/{id}/rejections.
	"""
	__tablename__ = "record_rejections"

	id = Column(Integer, primary_key=True, index=True)
	record_id = Column(Integer, ForeignKey("records.id", ondelete="CASCADE"), nullable=False, index=True)

	predefined_reason = Column(String(20), nullable=False)  # one of PREDEFINED_REJECTION_REASONS (app/schemas/record.py)
	comment = Column(Text, nullable=True)

	rejected_by = Column(String(255), nullable=True)
	rejected_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

	record = relationship("Record", back_populates="rejections")
	images = relationship("RecordImage", back_populates="rejection")

	__table_args__ = (
		CheckConstraint(
			"predefined_reason IN ('blur','glare','shadow','focus','exposure','dirt',"
			"'framing','obstruction','other')",
			name='check_record_rejection_reason'
		),
	)


class RecordStatusChange(Base):
	"""
	One row per change of a Record's QA status: from what, to what, by whom
	and when.

	records.status holds only where a record is now, and modified_at moves
	with any edit, so neither says when a record was approved. A row is written by
	app.core.record_status.change_record_status, which every writer of
	records.status goes through; take-back writes its rows in bulk beside its
	single UPDATE. Rows are not corrected: undoing an approval is a second
	row, not an edit of the first. The one change a row takes is record_id
	going NULL when its record is deleted.

	A record's history outlives the record. Deleting it sets record_id to
	NULL on its rows; collection_id, stored at each change, keeps them
	listed under the collection the record was in, so a count of that
	collection's reviews still includes the work done on it.

	Records that existed before this table have no rows for their earlier
	changes. Nothing is backfilled, because the only candidate, modified_at,
	dates the last edit rather than the change.
	"""
	__tablename__ = "record_status_changes"

	id = Column(Integer, primary_key=True, index=True)
	record_id = Column(Integer, ForeignKey("records.id", ondelete="SET NULL"), nullable=True, index=True)
	# The collection the record was in when the change was made. Read only
	# once the record is deleted: a live record's history follows the record
	# to its current collection.
	collection_id = Column(Integer, ForeignKey("collections.id", ondelete="SET NULL"), nullable=True, index=True)

	from_status = Column(String(20), nullable=False)
	to_status = Column(String(20), nullable=False)

	changed_by = Column(String(255), nullable=True)
	changed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)

	record = relationship("Record", back_populates="status_changes")

	__table_args__ = (
		CheckConstraint(
			"from_status IN ('captured','in_review','rejected','approved')",
			name='check_record_status_change_from'
		),
		CheckConstraint(
			"to_status IN ('captured','in_review','rejected','approved')",
			name='check_record_status_change_to'
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
