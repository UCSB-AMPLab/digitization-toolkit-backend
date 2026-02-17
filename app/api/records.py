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
from app.models.record import RecordImage, ExifData
from app.models.camera import CameraSettings
from app.models.user import User
from app.schemas.record import RecordCreate, RecordRead, RecordUpdate
from app.core.config import settings
from app.core.thumbnail import generate_thumbnail, delete_thumbnail

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/", response_model=RecordRead)
def create_record(
	rec_in: RecordCreate,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	# create record
	rec = RecordImage(
		filename=rec_in.filename,
		title=rec_in.title,
		description=rec_in.description,
		file_path=rec_in.file_path,
		file_size=rec_in.file_size,
		format=rec_in.format,
		resolution_width=rec_in.resolution_width,
		resolution_height=rec_in.resolution_height,
		uploaded_by=rec_in.uploaded_by or current_user.username,
		object_typology=rec_in.object_typology,
		author=rec_in.author,
		material=rec_in.material,
		date=rec_in.date,
		custom_attributes=rec_in.custom_attributes,
	)
	try:
		db.add(rec)
		db.commit()
		db.refresh(rec)
	except IntegrityError:
		db.rollback()
		raise HTTPException(status_code=409, detail="Record with this filename already exists")

	# optional camera settings
	if rec_in.camera_settings:
		cs = CameraSettings(record_image_id=rec.id, **rec_in.camera_settings.dict())
		db.add(cs)

	# optional exif
	if rec_in.exif_data:
		ex = ExifData(record_image_id=rec.id, **rec_in.exif_data.dict())
		db.add(ex)

	db.commit()
	db.refresh(rec)

	return RecordRead.model_validate(rec)


@router.get("/", response_model=List[RecordRead])
def list_records(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_dependency)
):
	recs = db.query(RecordImage).offset(skip).limit(limit).all()
	return [RecordRead.model_validate(r) for r in recs]


@router.get("/{rec_id}", response_model=RecordRead)
def get_record(rec_id: int, db: Session = Depends(get_db_dependency)):
	rec = db.query(RecordImage).filter(RecordImage.id == rec_id).first()
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")
	return RecordRead.model_validate(rec)


@router.patch("/{rec_id}", response_model=RecordRead)
def update_record(
	rec_id: int,
	payload: RecordUpdate,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	rec = db.query(RecordImage).filter(RecordImage.id == rec_id).first()
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")
	
	# Update only provided fields
	for field, value in payload.dict(exclude_unset=True).items():
		setattr(rec, field, value)
	
	db.add(rec)
	db.commit()
	db.refresh(rec)
	return RecordRead.model_validate(rec)


@router.put("/{rec_id}", response_model=RecordRead)
def replace_record(
	rec_id: int,
	payload: RecordCreate,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	rec = db.query(RecordImage).filter(RecordImage.id == rec_id).first()
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")
	
	# Replace all fields
	rec.filename = payload.filename
	rec.title = payload.title
	rec.description = payload.description
	rec.file_path = payload.file_path
	rec.file_size = payload.file_size
	rec.format = payload.format
	rec.resolution_width = payload.resolution_width
	rec.resolution_height = payload.resolution_height
	rec.uploaded_by = payload.uploaded_by
	rec.object_typology = payload.object_typology
	rec.author = payload.author
	rec.material = payload.material
	rec.date = payload.date
	rec.custom_attributes = payload.custom_attributes
	
	db.add(rec)
	db.commit()
	db.refresh(rec)
	return RecordRead.model_validate(rec)


@router.delete("/{rec_id}")
def delete_record(
	rec_id: int,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	rec = db.query(RecordImage).filter(RecordImage.id == rec_id).first()
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")
	
	# Clean up thumbnail if it exists
	if rec.thumbnail_path:
		delete_thumbnail(rec.thumbnail_path)
	
	db.delete(rec)
	db.commit()
	return {"detail": "record deleted"}


def _compute_sha256(file_path: Path) -> str:
	"""Compute SHA256 hash of a file."""
	sha256_hash = hashlib.sha256()
	with open(file_path, "rb") as f:
		for chunk in iter(lambda: f.read(8192), b""):
			sha256_hash.update(chunk)
	return sha256_hash.hexdigest()


@router.post("/upload", response_model=RecordRead)
async def upload_record(
	file: UploadFile = File(...),
	title: Optional[str] = None,
	description: Optional[str] = None,
	project_id: Optional[int] = None,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""
	Upload a record image file.
	
	Creates a record and stores the file in the uploads directory.
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
	thumbnail_path = None
	try:
		from PIL import Image
		with Image.open(file_path) as img:
			resolution_width, resolution_height = img.size
	except Exception:
		pass  # PIL not available or invalid image
	
	# Generate thumbnail
	try:
		thumbnails_dir = settings.data_dir / "thumbnails"
		thumbnail_path = generate_thumbnail(file_path, thumbnails_dir)
	except Exception as e:
		logger.warning(f"Failed to generate thumbnail for {file.filename}: {e}")
		# Don't fail the upload if thumbnail generation fails
	
	# Create record
	rec = RecordImage(
		filename=file.filename or unique_filename,
		title=title or file.filename,
		description=description,
		file_path=str(file_path),
		thumbnail_path=thumbnail_path,
		file_size=file_size,
		format=file_format,
		resolution_width=resolution_width,
		resolution_height=resolution_height,
		uploaded_by=current_user.username,
		project_id=project_id,
	)
	
	try:
		db.add(rec)
		db.commit()
		db.refresh(rec)
	except IntegrityError:
		db.rollback()
		# Clean up file
		file_path.unlink(missing_ok=True)
		raise HTTPException(status_code=409, detail="Record with this filename already exists")
	
	return RecordRead.model_validate(rec)


@router.get("/{rec_id}/file")
def download_record_file(
	rec_id: int,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""
	Download the actual image file for a record.
	
	Returns the file as a binary response with appropriate content type.
	"""
	rec = db.query(RecordImage).filter(RecordImage.id == rec_id).first()
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")
	
	if not rec.file_path:
		raise HTTPException(status_code=404, detail="Record has no associated file")
	
	file_path = Path(rec.file_path)
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
		filename=rec.filename,
		media_type=media_type
	)


@router.get("/{rec_id}/checksum")
def get_record_checksum(
	rec_id: int,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""
	Get the SHA256 checksum of a record's file.
	
	Useful for verifying file integrity.
	"""
	rec = db.query(RecordImage).filter(RecordImage.id == rec_id).first()
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")
	
	if not rec.file_path:
		raise HTTPException(status_code=404, detail="Record has no associated file")
	
	file_path = Path(rec.file_path)
	if not file_path.exists():
		raise HTTPException(status_code=404, detail="File not found on disk")
	
	checksum = _compute_sha256(file_path)
	return {"record_id": rec_id, "sha256": checksum}


@router.get("/{rec_id}/thumbnail")
def get_record_thumbnail(
	rec_id: int,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""
	Download the thumbnail image for a record.
	
	Returns the thumbnail file as a JPEG image response.
	"""
	rec = db.query(RecordImage).filter(RecordImage.id == rec_id).first()
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")
	
	if not rec.thumbnail_path:
		raise HTTPException(status_code=404, detail="Record has no thumbnail")
	
	thumbnail_path = Path(rec.thumbnail_path)
	if not thumbnail_path.exists():
		raise HTTPException(status_code=404, detail="Thumbnail file not found on disk")
	
	return FileResponse(
		path=thumbnail_path,
		filename=f"{Path(rec.filename).stem}_thumb.jpg",
		media_type="image/jpeg"
	)
