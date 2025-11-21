from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_db_dependency
from app.models.document import DocumentImage, ExifData
from app.models.camera import CameraSettings
from app.schemas.document import DocumentCreate, DocumentRead

router = APIRouter()


@router.post("/", response_model=DocumentRead)
def create_document(doc_in: DocumentCreate, db: Session = Depends(get_db_dependency)):
	# create document
	doc = DocumentImage(
		filename=doc_in.filename,
		title=doc_in.title,
		description=doc_in.description,
		file_path=doc_in.file_path,
		file_size=doc_in.file_size,
		format=doc_in.format,
		resolution_width=doc_in.resolution_width,
		resolution_height=doc_in.resolution_height,
		uploaded_by=doc_in.uploaded_by,
	)
	try:
		db.add(doc)
		db.commit()
		db.refresh(doc)
	except IntegrityError:
		db.rollback()
		raise HTTPException(status_code=409, detail="Document with this filename already exists")

	# optional camera settings
	if doc_in.camera_settings:
		cs = CameraSettings(document_image_id=doc.id, **doc_in.camera_settings.dict())
		db.add(cs)

	# optional exif
	if doc_in.exif_data:
		ex = ExifData(document_image_id=doc.id, **doc_in.exif_data.dict())
		db.add(ex)

	db.commit()
	db.refresh(doc)

	return DocumentRead.from_orm(doc)


@router.get("/", response_model=List[DocumentRead])
def list_documents(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db_dependency)
):
	docs = db.query(DocumentImage).offset(skip).limit(limit).all()
	return [DocumentRead.from_orm(d) for d in docs]


@router.get("/{doc_id}", response_model=DocumentRead)
def get_document(doc_id: int, db: Session = Depends(get_db_dependency)):
	doc = db.query(DocumentImage).filter(DocumentImage.id == doc_id).first()
	if not doc:
		raise HTTPException(status_code=404, detail="Document not found")
	return DocumentRead.from_orm(doc)
