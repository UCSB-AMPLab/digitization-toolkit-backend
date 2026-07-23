from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse
from typing import List, Optional
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from pathlib import Path
import uuid
import logging

from app.api.deps import get_db_dependency
from app.api.auth import get_current_user, RoleChecker
from app.models.record import Record, RecordImage, ExifData, RecordAnnotation
from app.models.camera import CameraSettings
from app.models.user import User
from app.schemas.record import (
	RecordCreate, RecordRead, RecordUpdate,
	RecordImageCreate, RecordImageRead, RecordImageUpdate,
	RecordStatusUpdate, BulkStatusUpdate, STATUS_TRANSITIONS,
	RecordAnnotationCreate, RecordAnnotationRead,
)
from app.core.config import settings
from app.core.paths import resolve_within_storage
from app.core.thumbnail import generate_thumbnail, delete_thumbnail

router = APIRouter()
logger = logging.getLogger(__name__)

allow_contributor = RoleChecker(["admin", "operator"])
allow_read_only = RoleChecker(["admin", "operator", "reviewer"])

# Streamed uploads are copied in 1 MiB chunks so the size cap is enforced as bytes arrive, even when Content-Length is missing or wrong (e.g. chunked encoding).
_UPLOAD_CHUNK = 1024 * 1024


class _UploadTooLarge(Exception):
	"""Raised when a streamed upload exceeds the configured size cap."""


def _save_upload_capped(src, dst_path: Path, max_bytes: int) -> int:
	"""Copy src to dst_path, aborting if more than max_bytes are read. Returns the number of bytes written. Callers unlink dst_path on failure."""
	total = 0
	with open(dst_path, "wb") as buffer:
		while True:
			chunk = src.read(_UPLOAD_CHUNK)
			if not chunk:
				break
			total += len(chunk)
			if total > max_bytes:
				raise _UploadTooLarge()
			buffer.write(chunk)
	return total


# ==============================================================================
# Record endpoints (archival documents/objects)
# ==============================================================================

@router.post("/", response_model=RecordRead)
def create_record(
	rec_in: RecordCreate,
	current_user: User = Depends(allow_contributor),
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
	orphaned: Optional[bool] = Query(default=None, description="If true, return only records with no project or collection"),
	current_user: User = Depends(allow_read_only),
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
	if orphaned is True:
		query = query.filter(Record.project_id == None, Record.collection_id == None)
	
	# Deterministic order: without an ORDER BY, Postgres returns heap order,
	# which shifts when rows are updated (NEH-159). Also required for stable
	# skip/limit pagination.
	#
	# Within a collection, listing order must match export order (see
	# export_collection_bagit in collections.py, which orders by
	# sequence.nulls_last(), id) and must reflect operator reordering, which
	# writes `sequence`. The id tiebreaker keeps the order total and
	# pagination stable (still NEH-159-safe). Project-wide listings (no
	# collection_id filter) keep pure id order, since per-collection
	# sequences would interleave meaninglessly across collections.
	if collection_id is not None:
		query = query.order_by(Record.sequence.nulls_last(), Record.id)
	else:
		query = query.order_by(Record.id)
	recs = query.offset(skip).limit(limit).all()
	return [RecordRead.model_validate(r) for r in recs]


@router.get("/count")
def count_records(
	project_id: Optional[int] = Query(default=None, description="Filter by project ID"),
	collection_id: Optional[int] = Query(default=None, description="Filter by collection ID"),
	current_user: User = Depends(allow_read_only),
	db: Session = Depends(get_db_dependency)
):
	"""Return the total number of records matching the given filters."""
	query = db.query(Record)
	if project_id is not None:
		query = query.filter(Record.project_id == project_id)
	if collection_id is not None:
		query = query.filter(Record.collection_id == collection_id)
	return {"count": query.count()}


@router.get("/{rec_id}", response_model=RecordRead)
def get_record(
	rec_id: int,
	current_user: User = Depends(allow_read_only),
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
	current_user: User = Depends(allow_contributor),
	db: Session = Depends(get_db_dependency)
):
	"""Update a record's descriptive metadata."""
	rec = db.query(Record).filter(Record.id == rec_id).first()
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")
	
	data = payload.model_dump(exclude_unset=True)

	# A record has at most one parent: reject setting both, null the opposite when one is set
	set_project = data.get("project_id") is not None
	set_collection = data.get("collection_id") is not None
	if set_project and set_collection:
		raise HTTPException(status_code=400, detail="A record cannot belong to both a project and a collection")
	if set_project:
		data["collection_id"] = None
	elif set_collection:
		data["project_id"] = None

	for field, value in data.items():
		setattr(rec, field, value)

	db.add(rec)
	try:
		db.commit()
	except IntegrityError:
		db.rollback()
		raise HTTPException(status_code=409, detail="Record parent assignment violates a database constraint")
	db.refresh(rec)
	return RecordRead.model_validate(rec)


@router.delete("/{rec_id}")
def delete_record(
	rec_id: int,
	current_user: User = Depends(allow_contributor),
	db: Session = Depends(get_db_dependency)
):
	"""Delete a record and all its associated images (CASCADE)."""
	rec = db.query(Record).filter(Record.id == rec_id).first()
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")

	# Locked records (in_review, approved) cannot be deleted; reviewer/admin must move them to rejected first
	if rec.status in ("in_review", "approved") and current_user.role != "admin":
		raise HTTPException(
			status_code=409,
			detail=f"Cannot delete a record with status '{rec.status}'. Move it to 'rejected' first."
		)
	
	# Count images before delete/commit; rec.images is unusable once the row is expired
	image_count = len(rec.images)

	# Clean up image files and thumbnails; only unlink paths inside storage
	for img in rec.images:
		file_path = resolve_within_storage(img.file_path)
		if file_path:
			file_path.unlink(missing_ok=True)
		thumbnail_path = resolve_within_storage(img.thumbnail_path)
		if thumbnail_path:
			delete_thumbnail(str(thumbnail_path))

	db.delete(rec)
	db.commit()
	return {"detail": f"Record {rec_id} and {image_count} images deleted"}


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
	current_user: User = Depends(allow_contributor),
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
	
	max_bytes = settings.MAX_UPLOAD_BYTES
	# Fast reject before writing anything if the declared size already exceeds the cap
	declared_size = getattr(file, "size", None)
	if declared_size is not None and declared_size > max_bytes:
		raise HTTPException(status_code=413, detail=f"File too large. Maximum size is {max_bytes} bytes")

	# Save file, enforcing the cap while streaming so a wrong or absent Content-Length cannot fill the SD. Unlink the partial file on any failure.
	try:
		file_size = _save_upload_capped(file.file, file_path, max_bytes)
	except _UploadTooLarge:
		file_path.unlink(missing_ok=True)
		raise HTTPException(status_code=413, detail=f"File too large. Maximum size is {max_bytes} bytes")
	except Exception as e:
		file_path.unlink(missing_ok=True)
		logger.exception(f"Failed to save uploaded file: {e}")
		raise HTTPException(status_code=500, detail="Failed to save file")

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
	try:
		db.commit()
	except Exception as e:
		db.rollback()
		# The DB row never persisted, so unlink the saved file and thumbnail to avoid orphaned files no record will reference.
		file_path.unlink(missing_ok=True)
		if thumbnail_path:
			delete_thumbnail(str(thumbnail_path))
		logger.exception(f"Failed to persist image record: {e}")
		raise HTTPException(status_code=500, detail="Failed to save image record")
	db.refresh(img)

	return RecordImageRead.model_validate(img)


@router.get("/{rec_id}/images", response_model=List[RecordImageRead])
def list_record_images(
	rec_id: int,
	current_user: User = Depends(allow_read_only),
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
	current_user: User = Depends(allow_read_only),
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
	current_user: User = Depends(allow_contributor),
	db: Session = Depends(get_db_dependency)
):
	"""Update image metadata (sequence, role, etc.)."""
	img = db.query(RecordImage).filter(RecordImage.id == img_id).first()
	if not img:
		raise HTTPException(status_code=404, detail="Image not found")

	# Explicit whitelist: file paths and other fields stay server-managed
	mutable_fields = {"sequence", "role"}
	for field, value in payload.model_dump(exclude_unset=True).items():
		if field in mutable_fields:
			setattr(img, field, value)
	
	db.add(img)
	db.commit()
	db.refresh(img)
	return RecordImageRead.model_validate(img)


@router.delete("/images/{img_id}")
def delete_image(
	img_id: int,
	current_user: User = Depends(allow_contributor),
	db: Session = Depends(get_db_dependency)
):
	"""Delete a specific image from a record."""
	img = db.query(RecordImage).filter(RecordImage.id == img_id).first()
	if not img:
		raise HTTPException(status_code=404, detail="Image not found")
	
	# Clean up files; only unlink paths inside storage
	file_path = resolve_within_storage(img.file_path)
	if file_path:
		file_path.unlink(missing_ok=True)
	thumbnail_path = resolve_within_storage(img.thumbnail_path)
	if thumbnail_path:
		delete_thumbnail(str(thumbnail_path))
	
	db.delete(img)
	db.commit()
	return {"detail": "Image deleted"}


@router.get("/images/{img_id}/file")
def download_image_file(
	img_id: int,
	current_user: User = Depends(allow_read_only),
	db: Session = Depends(get_db_dependency)
):
	"""Download the actual image file."""
	img = db.query(RecordImage).filter(RecordImage.id == img_id).first()
	if not img:
		raise HTTPException(status_code=404, detail="Image not found")
	
	if not img.file_path:
		raise HTTPException(status_code=404, detail="Image has no associated file")

	# Serve only files contained in the storage roots
	file_path = resolve_within_storage(img.file_path)
	if file_path is None or not file_path.exists():
		raise HTTPException(status_code=404, detail="File not found on disk")

	# For RAW files (e.g. CR2), serve the JPEG preview sidecar so browsers can display it
	if file_path.suffix.lower() in (".cr2", ".nef", ".arw", ".raf", ".dng"):
		preview_path = file_path.with_name(file_path.stem + "_preview.jpg")
		if preview_path.exists():
			file_path = preview_path

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
	current_user: User = Depends(allow_read_only),
	db: Session = Depends(get_db_dependency)
):
	"""Download the thumbnail for an image. Generates it on demand if missing."""
	img = db.query(RecordImage).filter(RecordImage.id == img_id).first()
	if not img:
		raise HTTPException(status_code=404, detail="Image not found")

	# If the thumbnail is missing, deleted, or outside storage, regenerate it from the source
	thumbnail_path = resolve_within_storage(img.thumbnail_path)
	if thumbnail_path is None or not thumbnail_path.exists():
		if not img.file_path:
			raise HTTPException(status_code=404, detail="Image has no source file for thumbnail generation")
		source_path = resolve_within_storage(img.file_path)
		if source_path is None or not source_path.exists():
			raise HTTPException(status_code=404, detail="Source image file not found on disk")
		try:
			# Store alongside the source: PROJECTS_ROOT/{project}/images/thumbnails/
			thumbnails_dir = source_path.parent.parent / "thumbnails"
			generated = generate_thumbnail(source_path, thumbnails_dir)
			if generated:
				img.thumbnail_path = generated
				db.add(img)
				db.commit()
				thumbnail_path = Path(generated)
		except Exception as e:
			logger.warning(f"On-demand thumbnail generation failed for image {img_id}: {e}")
			raise HTTPException(status_code=500, detail="Failed to generate thumbnail")

	if thumbnail_path is None or not thumbnail_path.exists():
		raise HTTPException(status_code=404, detail="Thumbnail could not be generated")

	return FileResponse(
		path=thumbnail_path,
		filename=f"{Path(img.filename).stem}_thumb.jpg",
		media_type="image/jpeg"
	)


# ==============================================================================
# Annotation endpoints (QA "Anotaciones" tab: flagged errors + notes)
# ==============================================================================

@router.get("/{rec_id}/annotations", response_model=List[RecordAnnotationRead])
def list_record_annotations(
	rec_id: int,
	current_user: User = Depends(allow_read_only),
	db: Session = Depends(get_db_dependency)
):
	"""List all annotations for a record, newest first."""
	rec = db.query(Record).filter(Record.id == rec_id).first()
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")

	annotations = (
		db.query(RecordAnnotation)
		.filter(RecordAnnotation.record_id == rec_id)
		.order_by(RecordAnnotation.created_at.desc())
		.all()
	)
	return [RecordAnnotationRead.model_validate(a) for a in annotations]


@router.post("/{rec_id}/annotations", response_model=RecordAnnotationRead)
def create_record_annotation(
	rec_id: int,
	payload: RecordAnnotationCreate,
	current_user: User = Depends(allow_read_only),
	db: Session = Depends(get_db_dependency)
):
	"""Add an annotation (flagged error and/or note) to a record."""
	rec = db.query(Record).filter(Record.id == rec_id).first()
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")

	annotation = RecordAnnotation(
		record_id=rec_id,
		error_types=payload.error_types,
		note=payload.note,
		created_by=current_user.username,
	)
	db.add(annotation)
	db.commit()
	db.refresh(annotation)
	return RecordAnnotationRead.model_validate(annotation)


@router.delete("/annotations/{annotation_id}")
def delete_record_annotation(
	annotation_id: int,
	current_user: User = Depends(allow_read_only),
	db: Session = Depends(get_db_dependency)
):
	"""Delete a single annotation."""
	annotation = db.query(RecordAnnotation).filter(RecordAnnotation.id == annotation_id).first()
	if not annotation:
		raise HTTPException(status_code=404, detail="Annotation not found")

	db.delete(annotation)
	db.commit()
	return {"detail": "Annotation deleted"}


# ==============================================================================
# Status management endpoints
# ==============================================================================

def _apply_status_change(
	rec: Record,
	new_status: str,
	rejection_note: Optional[str],
	user_role: str,
) -> None:
	"""
	Validate and apply a status transition on a Record.
	Raises HTTPException on invalid transition or insufficient role.
	"""
	current_status = rec.status or "captured"
	if current_status == new_status:
		return  # no-op

	allowed_roles = STATUS_TRANSITIONS.get((current_status, new_status))
	if allowed_roles is None:
		raise HTTPException(
			status_code=422,
			detail=f"Transition '{current_status}' -> '{new_status}' is not allowed."
		)
	if user_role not in allowed_roles:
		raise HTTPException(
			status_code=403,
			detail=f"Your role '{user_role}' cannot perform the '{current_status}' -> '{new_status}' transition."
		)

	rec.status = new_status
	rec.rejection_note = rejection_note if new_status == "rejected" else None


@router.patch("/{rec_id}/status", response_model=RecordRead)
def update_record_status(
	rec_id: int,
	payload: RecordStatusUpdate,
	current_user: User = Depends(allow_read_only),
	db: Session = Depends(get_db_dependency)
):
	"""
	Change the QA status of a record.

	Valid transitions and required roles:
	- captured  > in_review : operator, admin, reviewer
	- in_review > rejected  : reviewer, admin
	- in_review > approved  : reviewer, admin
	- in_review > captured  : operator, admin  (cancel review)
	- rejected  > captured  : operator, admin  (prepare for retake)
	- approved  > rejected  : reviewer, admin  (flag for rework)
	- approved  > captured  : admin only       (full reset)
	"""
	rec = db.query(Record).options(joinedload(Record.images)).filter(Record.id == rec_id).first()
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")

	_apply_status_change(rec, payload.status, payload.rejection_note, current_user.role)

	db.commit()
	db.refresh(rec)
	return RecordRead.model_validate(rec)


@router.post("/bulk-status", response_model=List[RecordRead])
def bulk_update_status(
	payload: BulkStatusUpdate,
	current_user: User = Depends(allow_read_only),
	db: Session = Depends(get_db_dependency)
):
	"""
	Change the QA status of multiple records at once.
	Records where the transition is not valid are skipped and reported in the response.

	Returns the updated records.
	"""
	records = (
		db.query(Record)
		.options(joinedload(Record.images))
		.filter(Record.id.in_(payload.record_ids))
		.all()
	)
	if not records:
		raise HTTPException(status_code=404, detail="No matching records found")

	skipped: list[int] = []
	updated: list[Record] = []

	for rec in records:
		current_status = rec.status or "captured"
		if current_status == payload.status:
			updated.append(rec)
			continue

		allowed_roles = STATUS_TRANSITIONS.get((current_status, payload.status))
		if allowed_roles is None or current_user.role not in allowed_roles:
			skipped.append(rec.id)
			continue

		rec.status = payload.status
		rec.rejection_note = payload.rejection_note if payload.status == "rejected" else None
		updated.append(rec)

	db.commit()
	for rec in updated:
		db.refresh(rec)

	if skipped:
		logger.info(f"bulk-status: skipped {len(skipped)} records (invalid transition or insufficient role): {skipped}")

	return [RecordRead.model_validate(r) for r in updated]
