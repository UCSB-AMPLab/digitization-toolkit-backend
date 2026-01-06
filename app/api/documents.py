from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse
from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from pathlib import Path
import shutil
import hashlib
import logging

from app.api.deps import get_db_dependency
from app.api.auth import get_current_user
from app.models.document import DocumentImage, ExifData
from app.models.camera import CameraSettings
from app.models.user import User
from app.schemas.document import DocumentCreate, DocumentRead, DocumentUpdate
from app.core.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


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


def _compute_sha256(file_path: Path) -> str:
	"""Compute SHA256 hash of a file."""
	sha256_hash = hashlib.sha256()
	with open(file_path, "rb") as f:
		for chunk in iter(lambda: f.read(8192), b""):
			sha256_hash.update(chunk)
	return sha256_hash.hexdigest()


@router.post("/upload", response_model=DocumentRead)
async def upload_document(
	file: UploadFile = File(...),
	title: Optional[str] = None,
	description: Optional[str] = None,
	project_id: Optional[int] = None,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""
	Upload a document image file.
	
	Creates a document record and stores the file in the uploads directory.
	"""
	# Validate file type
	allowed_types = {"image/jpeg", "image/png", "image/tiff", "image/webp"}
	if file.content_type not in allowed_types:
		raise HTTPException(
			status_code=400,
			detail=f"Invalid file type. Allowed: {', '.join(allowed_types)}"
		)
	
	# Create upload directory
	uploads_dir = settings.data_dir / "uploads"
	uploads_dir.mkdir(parents=True, exist_ok=True)
	
	# Generate unique filename to avoid collisions
	import uuid
	ext = Path(file.filename).suffix if file.filename else ".jpg"
	unique_filename = f"{uuid.uuid4().hex}{ext}"
	file_path = uploads_dir / unique_filename
	
	# Save file
	try:
		with open(file_path, "wb") as buffer:
			shutil.copyfileobj(file.file, buffer)
	except Exception as e:
		logger.exception(f"Failed to save uploaded file: {e}")
		raise HTTPException(status_code=500, detail="Failed to save file")
	
	# Get file info
	file_size = file_path.stat().st_size
	file_format = ext.lstrip(".").lower()
	
	# Try to get image dimensions
	resolution_width = None
	resolution_height = None
	try:
		from PIL import Image
		with Image.open(file_path) as img:
			resolution_width, resolution_height = img.size
	except Exception:
		pass  # PIL not available or invalid image
	
	# Create document record
	doc = DocumentImage(
		filename=file.filename or unique_filename,
		title=title or file.filename,
		description=description,
		file_path=str(file_path),
		file_size=file_size,
		format=file_format,
		resolution_width=resolution_width,
		resolution_height=resolution_height,
		uploaded_by=current_user.username,
		project_id=project_id,
	)
	
	try:
		db.add(doc)
		db.commit()
		db.refresh(doc)
	except IntegrityError:
		db.rollback()
		# Clean up file
		file_path.unlink(missing_ok=True)
		raise HTTPException(status_code=409, detail="Document with this filename already exists")
	
	return DocumentRead.model_validate(doc)


@router.get("/{doc_id}/file")
def download_document_file(
	doc_id: int,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""
	Download the actual image file for a document.
	
	Returns the file as a binary response with appropriate content type.
	"""
	doc = db.query(DocumentImage).filter(DocumentImage.id == doc_id).first()
	if not doc:
		raise HTTPException(status_code=404, detail="Document not found")
	
	if not doc.file_path:
		raise HTTPException(status_code=404, detail="Document has no associated file")
	
	file_path = Path(doc.file_path)
	if not file_path.exists():
		raise HTTPException(status_code=404, detail="File not found on disk")
	
	# Determine media type
	media_type_map = {
		"jpg": "image/jpeg",
		"jpeg": "image/jpeg",
		"png": "image/png",
		"tiff": "image/tiff",
		"tif": "image/tiff",
		"webp": "image/webp",
	}
	ext = file_path.suffix.lstrip(".").lower()
	media_type = media_type_map.get(ext, "application/octet-stream")
	
	return FileResponse(
		path=file_path,
		filename=doc.filename,
		media_type=media_type
	)


@router.get("/{doc_id}/checksum")
def get_document_checksum(
	doc_id: int,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""
	Get the SHA256 checksum of a document's file.
	
	Useful for verifying file integrity.
	"""
	doc = db.query(DocumentImage).filter(DocumentImage.id == doc_id).first()
	if not doc:
		raise HTTPException(status_code=404, detail="Document not found")
	
	if not doc.file_path:
		raise HTTPException(status_code=404, detail="Document has no associated file")
	
	file_path = Path(doc.file_path)
	if not file_path.exists():
		raise HTTPException(status_code=404, detail="File not found on disk")
	
	checksum = _compute_sha256(file_path)
	return {"document_id": doc_id, "sha256": checksum}
