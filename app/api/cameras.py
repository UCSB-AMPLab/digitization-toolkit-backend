from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.models.document import DocumentImage
from typing import List
from pydantic import BaseModel

from app.api.deps import get_db_dependency
from app.api.auth import get_current_user
from app.models.camera import CameraSettings
from app.models.user import User
from app.schemas.camera import CameraSettingsCreate, CameraSettingsRead

router = APIRouter()


class DeviceInfo(BaseModel):
	name: str
	id: str


@router.get("/devices", response_model=List[DeviceInfo])
def list_camera_devices():
	"""Return available camera devices. This is a platform stub; implement camera discovery per platform."""
	# For Raspberry Pi 5 or other platforms the implementation will enumerate V4L2 devices or platform APIs.
	# Here we return a minimal stub.
	return [DeviceInfo(name="simulated-camera", id="sim-0")]


@router.post("/capture")
def trigger_capture(device_id: str = "sim-0"):
	"""Trigger a single capture on the given device. This is a stub; integrate with camera control code per-device."""
	# TODO: integrate with picamera2 or other capture libraries
	return {"detail": f"capture triggered on {device_id}"}


@router.post("/", response_model=CameraSettingsRead)
def create_camera_settings(
	payload: CameraSettingsCreate,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	if not db.query(DocumentImage).filter(DocumentImage.id == payload.document_image_id).first():
		raise HTTPException(status_code=404, detail="Document not found")

	try:
		cs = CameraSettings(**payload.dict())
		db.add(cs)
		db.commit()
		db.refresh(cs)
	except IntegrityError:
		db.rollback()
		raise HTTPException(status_code=409, detail="Camera settings already exist for this document")
	return CameraSettingsRead.model_validate(cs)


@router.get("/", response_model=List[CameraSettingsRead])
def list_camera_settings(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_dependency)
):
	items = db.query(CameraSettings).offset(skip).limit(limit).all()
	return [CameraSettingsRead.model_validate(i) for i in items]


@router.get("/{id}", response_model=CameraSettingsRead)
def get_camera_settings(
	id: int,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	cs = db.query(CameraSettings).filter(CameraSettings.id == id).first()
	if not cs:
		raise HTTPException(status_code=404, detail="Camera settings not found")
	return CameraSettingsRead.model_validate(cs)
