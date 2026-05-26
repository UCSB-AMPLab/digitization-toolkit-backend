from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey, CheckConstraint
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
	project_id = Column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
	collection_id = Column(Integer, ForeignKey("collections.id", ondelete="SET NULL"), nullable=True)
	
	# QA workflow
	status = Column(String(20), nullable=False, default="captured")  # captured, in_review, rejected, approved
	sequence = Column(Integer, nullable=True)  # ordering within a collection
	rejection_note = Column(Text, nullable=True)  # optional note when status=rejected

	# Audit fields
	created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
	modified_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
	created_by = Column(String(255), nullable=True)
	
	# Relationships
	images = relationship("RecordImage", back_populates="record", cascade="all, delete-orphan")
	project = relationship("Project", back_populates="records")
	collection = relationship("Collection", back_populates="records")
	
	# Constraint: must have either project_id OR collection_id (or neither, but not both)
	__table_args__ = (
		CheckConstraint(
			'NOT (project_id IS NOT NULL AND collection_id IS NOT NULL)',
			name='check_record_single_parent'
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
	
	# Relationships
	record = relationship("Record", back_populates="images")
	camera_settings = relationship("CameraSettings", back_populates="record_image", uselist=False, cascade="all, delete-orphan")
	exif_data = relationship("ExifData", back_populates="record_image", uselist=False, cascade="all, delete-orphan")


class ExifData(Base):
	__tablename__ = "exif_data"

	id = Column(Integer, primary_key=True, index=True)
	record_image_id = Column(Integer, ForeignKey("record_images.id"), nullable=False)

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
