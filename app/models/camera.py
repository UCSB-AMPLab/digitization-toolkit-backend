from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship

from app.core.db import Base


class CameraSettings(Base):
    """Camera configuration settings."""
    __tablename__ = "camera_settings"

    id = Column(Integer, primary_key=True, index=True)
    document_image_id = Column(Integer, ForeignKey("document_images.id"), unique=True, nullable=False)

    camera_model = Column(String(255), nullable=True)
    camera_manufacturer = Column(String(255), nullable=True)
    lens_model = Column(String(255), nullable=True)

    iso = Column(Integer, nullable=True)
    aperture = Column(Float, nullable=True)
    shutter_speed = Column(String(50), nullable=True)
    focal_length = Column(Float, nullable=True)
    exposure_compensation = Column(Float, nullable=True)
    white_balance = Column(String(100), nullable=True)
    flash_used = Column(Boolean, nullable=True)
    metering_mode = Column(String(100), nullable=True)
    focus_mode = Column(String(100), nullable=True)
    light_source = Column(String(100), nullable=True)
    zsl = Column(Boolean, default=False, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    document_image = relationship("DocumentImage", back_populates="camera_settings")
