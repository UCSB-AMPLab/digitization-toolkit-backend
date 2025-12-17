from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_db_dependency
from app.api.auth import get_current_user
from app.models.document import DocumentImage, ExifData
from app.models.camera import CameraSettings
from app.models.user import User
from app.schemas.document import DocumentCreate, DocumentRead, DocumentUpdate

router = APIRouter()


@router.post("/", response_model=DocumentRead)
def create_document(
	doc_in: DocumentCreate,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
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
		uploaded_by=doc_in.uploaded_by or current_user.username,
		object_typology=doc_in.object_typology,
		author=doc_in.author,
		material=doc_in.material,
		date=doc_in.date,
		custom_attributes=doc_in.custom_attributes,
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

	return DocumentRead.model_validate(doc)


@router.get("/", response_model=List[DocumentRead])
def list_documents(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_dependency)
):
	docs = db.query(DocumentImage).offset(skip).limit(limit).all()
	return [DocumentRead.model_validate(d) for d in docs]


@router.get("/{doc_id}", response_model=DocumentRead)
def get_document(doc_id: int, db: Session = Depends(get_db_dependency)):
	doc = db.query(DocumentImage).filter(DocumentImage.id == doc_id).first()
	if not doc:
		raise HTTPException(status_code=404, detail="Document not found")
	return DocumentRead.model_validate(doc)


@router.patch("/{doc_id}", response_model=DocumentRead)
def update_document(
	doc_id: int,
	payload: DocumentUpdate,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	doc = db.query(DocumentImage).filter(DocumentImage.id == doc_id).first()
	if not doc:
		raise HTTPException(status_code=404, detail="Document not found")
	
	# Update only provided fields
	for field, value in payload.dict(exclude_unset=True).items():
		setattr(doc, field, value)
	
	db.add(doc)
	db.commit()
	db.refresh(doc)
	return DocumentRead.model_validate(doc)


@router.put("/{doc_id}", response_model=DocumentRead)
def replace_document(
	doc_id: int,
	payload: DocumentCreate,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	doc = db.query(DocumentImage).filter(DocumentImage.id == doc_id).first()
	if not doc:
		raise HTTPException(status_code=404, detail="Document not found")
	
	# Replace all fields
	doc.filename = payload.filename
	doc.title = payload.title
	doc.description = payload.description
	doc.file_path = payload.file_path
	doc.file_size = payload.file_size
	doc.format = payload.format
	doc.resolution_width = payload.resolution_width
	doc.resolution_height = payload.resolution_height
	doc.uploaded_by = payload.uploaded_by
	doc.object_typology = payload.object_typology
	doc.author = payload.author
	doc.material = payload.material
	doc.date = payload.date
	doc.custom_attributes = payload.custom_attributes
	
	db.add(doc)
	db.commit()
	db.refresh(doc)
	return DocumentRead.model_validate(doc)


@router.delete("/{doc_id}")
def delete_document(
	doc_id: int,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	doc = db.query(DocumentImage).filter(DocumentImage.id == doc_id).first()
	if not doc:
		raise HTTPException(status_code=404, detail="Document not found")
	
	db.delete(doc)
	db.commit()
	return {"detail": "document deleted"}
