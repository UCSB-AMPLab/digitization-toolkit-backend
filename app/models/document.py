from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.orm import relationship

from app.core.db import Base


class DocumentImage(Base):
	__tablename__ = "document_images"

	id = Column(Integer, primary_key=True, index=True)
	filename = Column(String(255), unique=True, index=True, nullable=False)
	title = Column(String(255), nullable=True)
	description = Column(Text, nullable=True)
	file_path = Column(String(512), nullable=False)
	file_size = Column(Integer, nullable=True)
	format = Column(String(50), nullable=False)
	resolution_width = Column(Integer, nullable=True)
	resolution_height = Column(Integer, nullable=True)
	created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
	modified_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
	uploaded_by = Column(String(255), nullable=True)

	camera_settings = relationship("CameraSettings", back_populates="document_image", uselist=False, cascade="all, delete-orphan")
	exif_data = relationship("ExifData", back_populates="document_image", uselist=False, cascade="all, delete-orphan")


class ExifData(Base):
	__tablename__ = "exif_data"

	id = Column(Integer, primary_key=True, index=True)
	document_image_id = Column(Integer, nullable=False)

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

	document_image = relationship("DocumentImage", back_populates="exif_data")
