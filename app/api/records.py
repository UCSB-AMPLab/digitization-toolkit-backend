from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse
from typing import List, Optional
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from pathlib import Path
import shutil
import uuid
import logging

from app.api.deps import get_db_dependency
from app.api.auth import get_current_user
from app.models.record import Record, RecordImage, ExifData
from app.models.camera import CameraSettings
from app.models.user import User
from app.schemas.record import (
	RecordCreate, RecordRead, RecordUpdate,
	RecordImageCreate, RecordImageRead, RecordImageUpdate
)
from app.core.config import settings
from app.core.thumbnail import generate_thumbnail, delete_thumbnail

router = APIRouter()
logger = logging.getLogger(__name__)


# ==============================================================================
# Record endpoints (archival documents/objects)
# ==============================================================================

@router.post("/", response_model=RecordRead)
def create_record(
	rec_in: RecordCreate,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""Create a new archival record (document/object like a book, map, document)."""
	rec = Record(
		title=rec_in.title,
		description=rec_in.description,
		object_typology=rec_in.object_typology,
		author=rec_in.author,
		material=rec_in.material,
		date=rec_in.date,
		custom_attributes=rec_in.custom_attributes,
		project_id=rec_in.project_id,
		collection_id=rec_in.collection_id,
		created_by=rec_in.created_by or current_user.username,
	)
	try:
		db.add(rec)
		db.commit()
		db.refresh(rec)
	except IntegrityError as e:
		db.rollback()
		raise HTTPException(status_code=409, detail=f"Database integrity error: {str(e)}")
	
	return RecordRead.model_validate(rec)


@router.get("/", response_model=List[RecordRead])
def list_records(
	skip: int = Query(default=0, ge=0),
	limit: int = Query(default=100, ge=1, le=1000),
	project_id: Optional[int] = Query(default=None, description="Filter by project ID"),
	collection_id: Optional[int] = Query(default=None, description="Filter by collection ID"),
	object_typology: Optional[str] = Query(default=None, description="Filter by object type"),
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""List all records with optional filtering."""
	query = db.query(Record).options(joinedload(Record.images))
	
	# Apply filters if provided
	if project_id is not None:
		query = query.filter(Record.project_id == project_id)
	if collection_id is not None:
		query = query.filter(Record.collection_id == collection_id)
	if object_typology is not None:
		query = query.filter(Record.object_typology == object_typology)
	
	recs = query.offset(skip).limit(limit).all()
	return [RecordRead.model_validate(r) for r in recs]


@router.get("/{rec_id}", response_model=RecordRead)
def get_record(
	rec_id: int,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""Get a specific record with all its images."""
	rec = db.query(Record).options(joinedload(Record.images)).filter(Record.id == rec_id).first()
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
	"""Update a record's descriptive metadata."""
	rec = db.query(Record).filter(Record.id == rec_id).first()
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")
	
	# Update only provided fields
	for field, value in payload.model_dump(exclude_unset=True).items():
		setattr(rec, field, value)
	
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
	"""Delete a record and all its associated images (CASCADE)."""
	rec = db.query(Record).filter(Record.id == rec_id).first()
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")
	
	# Clean up image files and thumbnails
	for img in rec.images:
		if img.file_path:
			Path(img.file_path).unlink(missing_ok=True)
		if img.thumbnail_path:
			delete_thumbnail(img.thumbnail_path)
	
	db.delete(rec)
	db.commit()
	return {"detail": f"Record {rec_id} and {len(rec.images)} images deleted"}


# ==============================================================================
# RecordImage endpoints (individual captures/images)
# ==============================================================================

@router.post("/{rec_id}/images", response_model=RecordImageRead)
async def add_image_to_record(
	rec_id: int,
	file: UploadFile = File(...),
	capture_id: Optional[str] = None,
	pair_id: Optional[str] = None,
	sequence: Optional[int] = None,
	role: Optional[str] = None,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""
	Upload and attach an image to an existing record.
	This is used when adding captures to a multi-page document.
	"""
	# Verify record exists
	rec = db.query(Record).filter(Record.id == rec_id).first()
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")
	
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
	
	# Generate unique filename
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
	
	# Generate thumbnail
	thumbnail_path = None
	try:
		thumbnails_dir = settings.data_dir / "thumbnails"
		thumbnail_path = generate_thumbnail(file_path, thumbnails_dir)
	except Exception as e:
		logger.warning(f"Failed to generate thumbnail for {file.filename}: {e}")
	
	# Create RecordImage
	img = RecordImage(
		record_id=rec_id,
		filename=file.filename or unique_filename,
		file_path=str(file_path),
		thumbnail_path=thumbnail_path,
		file_size=file_size,
		format=file_format,
		resolution_width=resolution_width,
		resolution_height=resolution_height,
		capture_id=capture_id,
		pair_id=pair_id,
		sequence=sequence,
		role=role,
		uploaded_by=current_user.username,
	)
	
	db.add(img)
	db.commit()
	db.refresh(img)
	
	return RecordImageRead.model_validate(img)


@router.get("/{rec_id}/images", response_model=List[RecordImageRead])
def list_record_images(
	rec_id: int,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""Get all images for a specific record, ordered by sequence."""
	# Verify record exists
	rec = db.query(Record).filter(Record.id == rec_id).first()
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")
	
	images = db.query(RecordImage).filter(
		RecordImage.record_id == rec_id
	).order_by(RecordImage.sequence.nullslast(), RecordImage.created_at).all()
	
	return [RecordImageRead.model_validate(img) for img in images]


@router.get("/images/{img_id}", response_model=RecordImageRead)
def get_image(
	img_id: int,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""Get details about a specific image."""
	img = db.query(RecordImage).filter(RecordImage.id == img_id).first()
	if not img:
		raise HTTPException(status_code=404, detail="Image not found")
	return RecordImageRead.model_validate(img)


@router.patch("/images/{img_id}", response_model=RecordImageRead)
def update_image(
	img_id: int,
	payload: RecordImageUpdate,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""Update image metadata (sequence, role, etc.)."""
	img = db.query(RecordImage).filter(RecordImage.id == img_id).first()
	if not img:
		raise HTTPException(status_code=404, detail="Image not found")
	
	for field, value in payload.model_dump(exclude_unset=True).items():
		setattr(img, field, value)
	
	db.add(img)
	db.commit()
	db.refresh(img)
	return RecordImageRead.model_validate(img)


@router.delete("/images/{img_id}")
def delete_image(
	img_id: int,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""Delete a specific image from a record."""
	img = db.query(RecordImage).filter(RecordImage.id == img_id).first()
	if not img:
		raise HTTPException(status_code=404, detail="Image not found")
	
	# Clean up files
	if img.file_path:
		Path(img.file_path).unlink(missing_ok=True)
	if img.thumbnail_path:
		delete_thumbnail(img.thumbnail_path)
	
	db.delete(img)
	db.commit()
	return {"detail": "Image deleted"}


@router.get("/images/{img_id}/file")
def download_image_file(
	img_id: int,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""Download the actual image file."""
	img = db.query(RecordImage).filter(RecordImage.id == img_id).first()
	if not img:
		raise HTTPException(status_code=404, detail="Image not found")
	
	if not img.file_path:
		raise HTTPException(status_code=404, detail="Image has no associated file")
	
	file_path = Path(img.file_path)
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
		filename=img.filename,
		media_type=media_type
	)


@router.get("/images/{img_id}/thumbnail")
def get_image_thumbnail(
	img_id: int,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""Download the thumbnail for an image."""
	img = db.query(RecordImage).filter(RecordImage.id == img_id).first()
	if not img:
		raise HTTPException(status_code=404, detail="Image not found")
	
	if not img.thumbnail_path:
		raise HTTPException(status_code=404, detail="Image has no thumbnail")
	
	thumbnail_path = Path(img.thumbnail_path)
	if not thumbnail_path.exists():
		raise HTTPException(status_code=404, detail="Thumbnail file not found on disk")
	
	return FileResponse(
		path=thumbnail_path,
		filename=f"{Path(img.filename).stem}_thumb.jpg",
		media_type="image/jpeg"
	)
