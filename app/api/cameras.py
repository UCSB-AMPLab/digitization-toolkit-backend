from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.models.document import DocumentImage
from typing import List

from app.api.deps import get_db_dependency
from app.models.camera import CameraSettings
from app.schemas.camera import CameraSettingsCreate, CameraSettingsRead

router = APIRouter()


@router.post("/", response_model=CameraSettingsRead)
def create_camera_settings(payload: CameraSettingsCreate, db: Session = Depends(get_db_dependency)):
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
	return CameraSettingsRead.from_orm(cs)


@router.get("/", response_model=List[CameraSettingsRead])
def list_camera_settings(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db_dependency)
):
	items = db.query(CameraSettings).offset(skip).limit(limit).all()
	return [CameraSettingsRead.from_orm(i) for i in items]


@router.get("/{id}", response_model=CameraSettingsRead)
def get_camera_settings(id: int, db: Session = Depends(get_db_dependency)):
	cs = db.query(CameraSettings).filter(CameraSettings.id == id).first()
	if not cs:
		raise HTTPException(status_code=404, detail="Camera settings not found")
	return CameraSettingsRead.from_orm(cs)
