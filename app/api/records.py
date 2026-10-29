# Version: 0.2.0

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse
from typing import Iterable, List, Optional
from sqlalchemy.orm import Session, aliased, joinedload
from sqlalchemy.exc import IntegrityError
from pathlib import Path
import logging
import uuid

from app.api.deps import get_db_dependency
from app.api.auth import get_current_user, RoleChecker
from datetime import datetime, timezone

from app.models.collection import Collection
from app.models.record import Record, RecordImage, ExifData, RecordAnnotation, RecordRejection
from app.models.camera import CameraSettings
from app.models.user import User
from app.schemas.record import (
	RecordCreate, RecordRead, RecordUpdate,
	RecordImageCreate, RecordImageRead, RecordImageUpdate,
	RecordStatusUpdate, BulkStatusUpdate, STATUS_TRANSITIONS,
	RecordAnnotationCreate, RecordAnnotationRead,
	RecordRejectRequest, RecordRejectionRead, RecordToRetake,
)
from app.core.config import settings
from app.core.db_errors import integrity_conflict
from app.core.paths import is_raw_suffix, preview_sidecar_of, resolve_within_storage
from app.core.record_sequence import next_sequences
from app.core.record_status import change_record_status
from app.core.capture_naming import CaptureNameRefused, take_filename_index
from app.core.volume_stem import STEM_REQUIRED_DETAIL, VOLUME_REQUIRED_DETAIL, locked_volume_query, volume_lacks_stem
from app.core.thumbnail import generate_thumbnail, delete_thumbnail
from app.core.audit import log_event
from app.core.export_jobs import export_jobs
from app.core.storage_override import refuse_writes_under_unusable_root

router = APIRouter()
logger = logging.getLogger(__name__)

allow_contributor = RoleChecker(["admin", "operator"])
allow_read_only = RoleChecker(["admin", "operator", "reviewer"])
allow_reviewer = RoleChecker(["admin", "reviewer"])

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


def _serialize_record(rec: Record, include_superseded: bool = False) -> RecordRead:
	"""RecordRead.model_validate(rec), with images narrowed to current-only by default (NEH-208)."""
	data = RecordRead.model_validate(rec)
	if not include_superseded:
		data.images = [img for img in data.images if img.is_current]
	return data


def _image_files_to_unlink(
	raw_path: Optional[str],
	raw_thumbnail_path: Optional[str],
	sibling_paths: Iterable[Optional[str]] = (),
) -> List[Path]:
	"""The files on the card an image row's delete removes: master, `_preview.jpg` sidecar, thumbnail.

	Nothing is unlinked here. The delete handlers call this while they hold the
	record's row lock, commit the row deletion, and only then pass the list to
	`_unlink_after_commit` (NEH-262). Unlinking first would leave, after a failed
	commit, rows pointing at masters that are already gone.

	A RAW capture occupies two files on the card: the master the row points at,
	and a JPEG preview the RAW backend writes as `<stem>_preview.jpg`, beside it
	in the name-based layout and in the volume's `previews/` in the stem layout,
	which is what the review UI and the thumbnail generator can read. Removing
	only the master leaves the preview with nothing referencing it, about 2.3 MB
	per capture; on the Rionegro unit 2,403 such previews hold 5.5 GB of a 115 GB
	card that reached 100% on 2026-10-14 and stopped capture (NEH-257). They also
	make the card unreadable as evidence: a preview with no master and no row
	looks, on the filesystem, exactly like a master that was lost.

	The sidecar's name is derived from the *resolved* path, so it stays inside
	the storage roots `resolve_within_storage` confines the master to; a row
	pointing outside them loses neither file. The name is the whole rule, with
	no suffix check: a JPEG master's `_preview.jpg` is an orphan in the
	same way, and a suffix list here would be a second copy of the backends'
	RAW formats to keep in step.

	Because the name comes from the stem, two masters of one frame that differ
	only in extension — `folio.jpg` and `folio.CR2`, which a multi-format
	capture writes — name the same sidecar, and deleting either would take the
	preview the other still needs. `sibling_paths` carries the stored paths of
	the rows that will survive this delete; the sidecar is left out of the list
	if any of them resolves to a master naming it. The master is in the list
	either way, since the row pointing at it is going.

	The thumbnail is included only when its stored path resolves inside the
	storage roots, the same confinement the master gets.

	The sidecar's name comes from `preview_sidecar_of` in app/core/paths.py,
	which every site that derives it shares (NEH-264). `capture/service.py`
	uses it too when it rolls back a capture that never reached the manifest;
	that rollback and these handlers share the name rule and nothing else.
	"""
	paths: List[Path] = []
	master = resolve_within_storage(raw_path)
	if master is not None:
		paths.append(master)
		sidecar = preview_sidecar_of(master)
		shared = False
		for raw_sibling in sibling_paths:
			sibling = resolve_within_storage(raw_sibling)
			if sibling is not None and preview_sidecar_of(sibling) == sidecar:
				shared = True
				break
		if not shared:
			paths.append(sidecar)
	thumbnail = resolve_within_storage(raw_thumbnail_path)
	if thumbnail is not None:
		paths.append(thumbnail)
	return paths


def _unlink_after_commit(paths: Iterable[Path]) -> List[str]:
	"""Unlink files whose rows a committed transaction has already removed (NEH-262).

	Called only after the commit succeeds. A file that cannot be unlinked is
	logged at WARNING with its path and the next one is tried; the request still
	succeeds, because the rows are gone and cannot be brought back from here.
	That leaves a file on the card with no row, which can be found and removed
	later. The opposite order would leave a row pointing at a master that no
	longer exists, which cannot be repaired. A file already missing is not an error.

	Returns the names of the files that could not be unlinked, in order, so the
	handler can report them to the client (NEH-263); empty when every file went.
	"""
	not_removed: List[str] = []
	for path in paths:
		try:
			path.unlink(missing_ok=True)
		except OSError as e:
			logger.warning(f"Row deleted but file could not be unlinked, left on disk: {path}: {e}")
			not_removed.append(path.name)
	return not_removed


def _delete_response(detail: str, not_removed: List[str]) -> dict:
	"""A delete endpoint's JSON body: `detail`, plus `not_removed` only when a file stayed on disk."""
	body = {"detail": detail}
	if not_removed:
		body["not_removed"] = not_removed
	return body


def _collection_of(rec: Optional[Record]) -> List[int]:
	"""The record's collection id as a list for export_jobs.hold_for_file_changes; empty when it has none."""
	return [rec.collection_id] if rec is not None and rec.collection_id is not None else []


def _supersede_current_images(record: Record) -> None:
	"""Flip every current image on a record to superseded. Used when a recapture is about to add new current image(s) (NEH-208)."""
	now = datetime.now(timezone.utc)
	for img in record.images:
		if img.is_current:
			img.is_current = False
			img.superseded_at = now


# ==============================================================================
# Record endpoints (archival documents/objects)
# ==============================================================================

@router.post("/", response_model=RecordRead)
def create_record(
	rec_in: RecordCreate,
	current_user: User = Depends(allow_contributor),
	db: Session = Depends(get_db_dependency)
):
	"""Create a new archival record (document/object like a book, map, document).

	A record created in a volume takes the sequence after the highest there,
	under the volume's row lock (app/core/record_sequence.py); `sequence` in
	the body is not used. A record outside a volume, or naming a volume that
	does not exist, is given no sequence.
	"""
	sequence = None
	if rec_in.collection_id is not None:
		sequences = next_sequences(db, rec_in.collection_id, 1)
		if sequences is not None:
			sequence = sequences[0]
	rec = Record(
		title=rec_in.title,
		description=rec_in.description,
		author=rec_in.author,
		date=rec_in.date,
		custom_attributes=rec_in.custom_attributes,
		project_id=rec_in.project_id,
		collection_id=rec_in.collection_id,
		created_by=rec_in.created_by or current_user.username,
		capture_mode=rec_in.capture_mode,
		sequence=sequence,
	)
	try:
		db.add(rec)
		db.commit()
		db.refresh(rec)
	except IntegrityError as e:
		db.rollback()
		raise integrity_conflict(e)
	
	return RecordRead.model_validate(rec)


@router.get("/", response_model=List[RecordRead])
def list_records(
	skip: int = Query(default=0, ge=0),
	limit: int = Query(default=100, ge=1, le=1000),
	project_id: Optional[int] = Query(default=None, description="Filter by project ID"),
	collection_id: Optional[int] = Query(default=None, description="Filter by collection ID"),
	orphaned: Optional[bool] = Query(default=None, description="If true, return only records with no project or collection"),
	current_user: User = Depends(allow_read_only),
	db: Session = Depends(get_db_dependency)
):
	"""List all records with optional filtering."""
	query = db.query(Record).options(joinedload(Record.images))
	
	if project_id is not None:
		query = query.filter(Record.project_id == project_id)
	if collection_id is not None:
		query = query.filter(Record.collection_id == collection_id)
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
	return [_serialize_record(r) for r in recs]


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


def _to_retake_query(db: Session):
	"""The rejected records, each with its volume and its latest rejection, in one query.

	The latest rejection is the one with the newest rejected_at (the highest
	id on a tie), found by a correlated subquery in the join, so the rows
	come from one statement whatever their number. Both joins are outer: a
	record in no volume, or rejected with no RecordRejection row, is still
	waiting.
	"""
	other = aliased(RecordRejection)
	latest = (
		db.query(other.id)
		.filter(other.record_id == Record.id)
		.order_by(other.rejected_at.desc(), other.id.desc())
		.limit(1)
		.correlate(Record)
		.scalar_subquery()
	)
	return (
		db.query(Record, Collection, RecordRejection)
		.outerjoin(Collection, Collection.id == Record.collection_id)
		.outerjoin(RecordRejection, RecordRejection.id == latest)
		.filter(Record.status == "rejected")
	)


@router.get("/to-retake", response_model=List[RecordToRetake])
def list_records_to_retake(
	current_user: User = Depends(allow_read_only),
	db: Session = Depends(get_db_dependency)
):
	"""Every rejected record, across all volumes, oldest latest rejection first.

	Each row has the record, its volume's project id, name and stem, and its latest
	rejection's reason code, comment, author and time. A rejected record
	with no rejection row comes last; ties go by record id.
	"""
	rows = (
		_to_retake_query(db)
		.order_by(RecordRejection.rejected_at.asc().nulls_last(), Record.id)
		.all()
	)
	return [
		RecordToRetake(
			recordId=rec.id,
			title=rec.title,
			sequence=rec.sequence,
			collectionId=rec.collection_id,
			projectId=col.project_id if col else None,
			name=col.name if col else None,
			filenameStem=col.filename_stem if col else None,
			reason=rej.predefined_reason if rej else None,
			comment=rej.comment if rej else None,
			rejectedBy=rej.rejected_by if rej else None,
			rejectedAt=rej.rejected_at if rej else None,
		)
		for rec, col, rej in rows
	]


@router.get("/to-retake/count")
def count_records_to_retake(
	current_user: User = Depends(allow_read_only),
	db: Session = Depends(get_db_dependency)
):
	"""How many records GET /records/to-retake lists, for the side menu's badge."""
	return {"count": db.query(Record).filter(Record.status == "rejected").count()}


@router.get("/to-retake/summary")
def summarise_records_to_retake(
	current_user: User = Depends(allow_read_only),
	db: Session = Depends(get_db_dependency)
):
	"""The retakes by volume, and the unit's recapture turn (app.core.recapture_turn).

	pendingRecords is the unit's rejected count, as /to-retake/count gives it.
	Each volume with a rejected record is listed, most pending first, then by
	name: `pending` is its rejected records, `total` its records with at least
	one rejection whatever their status now, and firstPendingRecordId its
	rejected record with the lowest sequence (then the lowest id). projectId
	is the project that owns the volume, through its parents for a nested one.
	"""
	from sqlalchemy import case, func
	from app.core import recapture_turn
	from app.core.storage_ops import resolve_project

	ever_rejected = (
		db.query(RecordRejection.record_id).filter(RecordRejection.record_id == Record.id).exists()
	)
	rows = (
		db.query(
			Record.collection_id,
			func.sum(case((Record.status == "rejected", 1), else_=0)).label("pending"),
			func.sum(case((ever_rejected, 1), else_=0)).label("total"),
		)
		.filter(Record.collection_id.isnot(None))
		.group_by(Record.collection_id)
		.having(func.sum(case((Record.status == "rejected", 1), else_=0)) > 0)
		.all()
	)
	volumes = []
	for collection_id, pending, total in rows:
		col = db.get(Collection, collection_id)
		first = (
			db.query(Record.id)
			.filter(Record.collection_id == collection_id, Record.status == "rejected")
			.order_by(Record.sequence.asc().nulls_last(), Record.id)
			.first()
		)
		project = resolve_project(db, col)
		volumes.append({
			"collectionId": collection_id,
			"projectId": project.id if project else None,
			"name": col.name,
			"filenameStem": col.filename_stem,
			"pending": int(pending),
			"total": int(total),
			"firstPendingRecordId": first[0],
		})
	volumes.sort(key=lambda v: (-v["pending"], v["name"]))
	pending_records = recapture_turn.pending_records(db)
	return {
		"pendingRecords": pending_records,
		"volumes": volumes,
		"turn": recapture_turn.turn(db, pending_records),
	}


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
	return _serialize_record(rec)


@router.patch("/{rec_id}", response_model=RecordRead)
def update_record(
	rec_id: int,
	payload: RecordUpdate,
	current_user: User = Depends(allow_contributor),
	db: Session = Depends(get_db_dependency)
):
	"""Update a record's descriptive metadata, and its parent while it is still
	the operator's.

	The row is locked because the decision below reads the status (NEH-260).
	"""
	rec = _locked_record(db, rec_id, with_images=False)
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

	# Moving a record between collections is a withdrawal once it has been
	# handed over, and the handover is a per-collection act by ruling
	# (NEH-260): take the collection back, move what you like, promote it
	# again. Without this check the whole-collection rule would be reachable
	# one record at a time — move it out, take the collection it landed in
	# back, move it home — and the record would come back as the operator's
	# while its siblings stay in the queue, in three ordinary calls with no
	# race.
	#
	# Only a move is refused. Retitling or redating a record under review is
	# ordinary cataloguing work and stays open to whoever may edit it.
	if "project_id" in data or "collection_id" in data:
		new_parent = (data.get("project_id", rec.project_id),
		              data.get("collection_id", rec.collection_id))
		if new_parent != (rec.project_id, rec.collection_id):
			assert_movable(rec)
			# A record whose images are in the bucket stays in its volume:
			# their keys are under the volume's stem and never change. A
			# record with such images that is outside every volume (one taken
			# out before add_record checked this) may go back only into the
			# volume all its keys are under, `<tenant>/<stem>/`.
			if new_parent[1] != rec.collection_id:
				from app.core.image_upload import (
					STORED_OFFSITE_MESSAGE, offsite_keys_under_volume, records_stored_offsite,
					stranded_offsite_message)
				if records_stored_offsite(db, [rec.id]):
					if rec.collection_id is not None or new_parent[1] is None:
						raise HTTPException(status_code=409, detail=STORED_OFFSITE_MESSAGE)
					# The volume's row is locked after the record's, as on every
					# path that takes both, so its stem cannot change before the
					# commit; next_sequences below takes the same row again.
					target = locked_volume_query(db, new_parent[1]).first()
					if target is None or not offsite_keys_under_volume(db, rec.id, target.filename_stem):
						raise HTTPException(status_code=409, detail=stranded_offsite_message(db, rec.id))

	# A record moved into another volume takes the sequence after the
	# highest there, so no two records of that volume share one. The volume's
	# row is locked after the record's (app/core/record_sequence.py). None is
	# assigned for a volume id that matches no volume.
	new_collection = data.get("collection_id")
	if new_collection is not None and new_collection != rec.collection_id:
		sequences = next_sequences(db, new_collection, 1)
		if sequences is not None:
			data["sequence"] = sequences[0]

	for field, value in data.items():
		setattr(rec, field, value)

	db.add(rec)
	try:
		db.commit()
	except IntegrityError as e:
		db.rollback()
		raise integrity_conflict(e)
	db.refresh(rec)
	return _serialize_record(rec)


@router.delete("/{rec_id}")
def delete_record(
	rec_id: int,
	current_user: User = Depends(allow_contributor),
	db: Session = Depends(get_db_dependency)
):
	"""Delete a record and all its associated images (CASCADE)."""
	rec = _locked_record(db, rec_id, with_images=False)
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

	# Collect the image files, their preview sidecars and thumbnails while the
	# row lock is held; only paths inside storage are collected. They are
	# unlinked after the commit, so a failed commit leaves every file in place
	# (NEH-262). No sibling check here, unlike the single-image delete: every
	# image of the record is going, so no surviving row can still need a
	# sidecar two of these masters share.
	files_to_unlink: List[Path] = []
	for img in rec.images:
		files_to_unlink.extend(_image_files_to_unlink(img.file_path, img.thumbnail_path))

	# An export of the record's collection reads these files; refused while one
	# runs. No check of the storage root here: a delete creates nothing.
	with export_jobs.hold_for_file_changes(_collection_of(rec)):
		db.delete(rec)
		db.commit()
		not_removed = _unlink_after_commit(files_to_unlink)
	return _delete_response(f"Record {rec_id} and {image_count} images deleted", not_removed)


# ==============================================================================
# RecordImage endpoints (individual captures/images)
# ==============================================================================

# The suffix an upload is stored under: the client's, lowercased, when it is
# one of these, otherwise the usual one for the declared content type.
_UPLOAD_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
_UPLOAD_DEFAULT_SUFFIX = {"image/jpeg": ".jpg", "image/png": ".png", "image/tiff": ".tif", "image/webp": ".webp"}


def _upload_suffix(file: UploadFile) -> str:
	suffix = Path(file.filename).suffix.lower() if file.filename else ""
	if suffix in _UPLOAD_SUFFIXES:
		return suffix
	return _UPLOAD_DEFAULT_SUFFIX.get(file.content_type, ".jpg")


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
	# Lock order, the same on every path that takes both: the record's row,
	# then its volume's. The record is locked here, and the stem check below
	# locks the volume. Read unlocked, the record would still be locked later,
	# by the image insert's foreign-key check (FOR KEY SHARE), after the
	# volume: the opposite order to a retake, and the two deadlock on
	# PostgreSQL.
	rec = _locked_record(db, rec_id, with_images=False)
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")
	# An upload is a capture event for the record's volume, refused on the
	# same terms as a capture, before anything is written.
	if rec.collection_id is None:
		raise HTTPException(status_code=409, detail=VOLUME_REQUIRED_DETAIL)
	if volume_lacks_stem(db, rec.collection_id):
		raise HTTPException(status_code=409, detail=STEM_REQUIRED_DETAIL)

	allowed_types = {"image/jpeg", "image/png", "image/tiff", "image/webp"}
	if file.content_type not in allowed_types:
		raise HTTPException(
			status_code=400,
			detail=f"Invalid file type. Allowed: {', '.join(allowed_types)}"
		)
	
	# The record's volume has a stem (refused above otherwise), so the upload
	# goes to `<stem>/captures/` and its thumbnail to `<stem>/thumbnails/`, as
	# a capture does.
	from capture.project_manager import ensure_volume_dirs
	from app.core.paths import CAPTURES_DIR, THUMBNAILS_DIR

	max_bytes = settings.MAX_UPLOAD_BYTES
	# Fast reject before writing anything if the declared size already exceeds the cap
	declared_size = getattr(file, "size", None)
	if declared_size is not None and declared_size > max_bytes:
		raise HTTPException(status_code=413, detail=f"File too large. Maximum size is {max_bytes} bytes")

	stem = rec.collection.filename_stem
	volume = ensure_volume_dirs(stem)
	uploads_dir = volume / CAPTURES_DIR
	thumbnails_dir = volume / THUMBNAILS_DIR

	# An upload is one capture event for the record's volume: it takes the
	# next number, in the transaction that writes its image, and is named
	# as a single capture is, `<stem>-img_<nnnn>_c0.<ext>` (contract 1.1,
	# section 3), with the same refusals. The name the client sent is logged, not kept.
	try:
		filename_index = take_filename_index(db, rec.collection_id, stem, uploads_dir)
	except CaptureNameRefused as refused:
		db.rollback()
		raise HTTPException(status_code=409, detail=refused.detail)
	stored_filename = f"{filename_index}_c0{_upload_suffix(file)}"
	file_path = uploads_dir / stored_filename
	logger.info(f"Upload into record {rec_id}: the client's file {file.filename!r} is stored as {stored_filename}")
	# Every image needs a sha256 the fixity index can find (the capture
	# manifest export and Fisqua refuse an image without one), and the index
	# keys the upload record by capture id, so an upload the client sent
	# without one is given its own.
	if not capture_id:
		capture_id = str(uuid.uuid4())

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

	file_format = file_path.suffix.lstrip(".")
	
	resolution_width = None
	resolution_height = None
	try:
		from PIL import Image
		with Image.open(file_path) as img:
			resolution_width, resolution_height = img.size
	except Exception:
		pass  # PIL not available or invalid image
	
	thumbnail_path = None
	try:
		thumbnail_path = generate_thumbnail(file_path, thumbnails_dir)
	except Exception as e:
		logger.warning(f"Failed to generate thumbnail for {file.filename}: {e}")
	
	img = RecordImage(
		record_id=rec_id,
		filename=stored_filename,
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
		# The volume's manifest names the file before the row is committed,
		# so its number is reserved even if the commit fails or the image is
		# deleted later (append_upload_record).
		from capture.manifestHandler import append_upload_record
		from capture.utils import compute_sha256
		append_upload_record(
			volume,
			capture_id=capture_id,
			relative_path=f"{CAPTURES_DIR}/{stored_filename}",
			size=file_size,
			mimetype=file.content_type,
			sha256=compute_sha256(str(file_path)),
		)
		db.commit()
	except Exception as e:
		db.rollback()
		# The DB row never persisted, so unlink the saved file and thumbnail to avoid orphaned files no record will reference.
		# A manifest line already written stays: the manifest is append-only, and it keeps the number reserved.
		# A file that cannot be unlinked is logged at WARNING with its path and left on disk; the response is the 500 below either way.
		try:
			file_path.unlink(missing_ok=True)
		except OSError as unlink_error:
			logger.warning(f"Upload not recorded and its file could not be unlinked, left on disk: {file_path}: {unlink_error}")
		try:
			delete_thumbnail(thumbnail_path)
		except OSError as unlink_error:
			logger.warning(f"Upload not recorded and its thumbnail could not be unlinked, left on disk: {thumbnail_path}: {unlink_error}")
		logger.exception(f"Failed to persist image record: {e}")
		raise HTTPException(status_code=500, detail="Failed to save image record")
	db.refresh(img)

	return RecordImageRead.model_validate(img)


@router.get("/{rec_id}/images", response_model=List[RecordImageRead])
def list_record_images(
	rec_id: int,
	include_superseded: bool = Query(default=False, description="Include images superseded by a recapture (NEH-208)"),
	current_user: User = Depends(allow_read_only),
	db: Session = Depends(get_db_dependency)
):
	"""Get all images for a specific record, ordered by sequence. Superseded (rejected-and-replaced) images are excluded by default."""
	rec = db.query(Record).filter(Record.id == rec_id).first()
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")

	query = db.query(RecordImage).filter(RecordImage.record_id == rec_id)
	if not include_superseded:
		query = query.filter(RecordImage.is_current.is_(True))
	images = query.order_by(RecordImage.sequence.nullslast(), RecordImage.created_at).all()

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

	# The same lock delete_record carries, for the same reason (NEH-260).
	# Until both guards say the same thing, emptying a record is a way around
	# a locked record. 'rejected' stays unlocked by design, so an emptied
	# rejected record is still reachable — which is why the promotion
	# invariant also insists on at least one current image.
	#
	# The row is locked, not just read. Unlinking a master is not something a
	# transaction can take back. Without the lock, a promotion and approval
	# can commit between this check and the unlink, and the unlink then
	# removes the file of an approved record on the strength of a status that
	# has already changed (reproduced). The lock also stops this delete
	# emptying a
	# record that another request is promoting (see _apply_status_change).
	# The files are chosen under this lock and unlinked only once the commit
	# that removes the row has succeeded (NEH-262).
	rec = _locked_record(db, img.record_id, with_images=False)
	# Read the image again now the record is locked: a move that relocated
	# its file and committed between the first read and the lock would
	# otherwise leave the old paths in the list below.
	img = (
		db.query(RecordImage)
		.filter(RecordImage.id == img_id)
		.populate_existing()
		.first()
	)
	if not img:
		raise HTTPException(status_code=404, detail="Image not found")
	if rec and rec.status in ("in_review", "approved") and current_user.role != "admin":
		raise HTTPException(
			status_code=409,
			detail=f"Cannot delete an image from a record with status '{rec.status}'. Move it to 'rejected' first."
		)

	# Collect the file, its preview sidecar and the thumbnail; only paths inside
	# storage are collected. The record's other images survive this delete, and one of
	# them may be a master of the same frame in another format, sharing this
	# master's stem and therefore its `_preview.jpg` — so they are passed in and
	# the sidecar is kept if any of them still names it. Superseded images count:
	# their rows still point at files on the card (NEH-208).
	siblings = [
		other.file_path
		for other in db.query(RecordImage).filter(
			RecordImage.record_id == img.record_id,
			RecordImage.id != img.id,
		)
	]
	files_to_unlink = _image_files_to_unlink(img.file_path, img.thumbnail_path, siblings)

	# An export of the record's collection reads this file if it is current;
	# refused while one runs, and held from before the delete until the file
	# is unlinked. A superseded image is not read by an export, so it is not
	# held. No check of the storage root here: a delete creates nothing.
	with export_jobs.hold_for_file_changes(_collection_of(rec) if img.is_current else []):
		record_id = img.record_id
		db.delete(img)

		# An emptied approved record goes back to the queue, in the same
		# transaction and under the same row lock as the delete that emptied it.
		# A second read-and-write after the commit could act on a status that
		# had already moved, which this guard exists to prevent. Counting the
		# siblings rather than reading rec.images keeps the answer independent of when the delete is flushed.
		remaining_current = (
			db.query(RecordImage)
			.filter(
				RecordImage.record_id == record_id,
				RecordImage.is_current.is_(True),
				RecordImage.id != img_id,
			)
			.count()
		)
		if rec and rec.status == "approved" and remaining_current == 0:
			change_record_status(db, rec, "in_review", current_user.username)
			db.add(rec)

		db.commit()
		not_removed = _unlink_after_commit(files_to_unlink)
	return _delete_response("Image deleted", not_removed)


# Masters whose bytes a browser cannot display are recognised by
# `is_raw_suffix` (app/core/paths.py); the RAW backend writes a
# `<stem>_preview.jpg` sidecar for each one, where preview_sidecar_of places it
# (capture/backends/gphoto2_backend.py).

# Masters a browser displays as they are, so they are their own preview.
_BROWSER_DISPLAYABLE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")

# Content-Type for the stored master. Capture only writes .cr2 among the RAW
# formats, so that one carries its conventional type; the other RAW suffixes
# have no registered type and go out as application/octet-stream, which every
# client saves as a file.
_MASTER_MEDIA_TYPES = {
	"jpg": "image/jpeg",
	"jpeg": "image/jpeg",
	"png": "image/png",
	"tiff": "image/tiff",
	"tif": "image/tiff",
	"webp": "image/webp",
	"cr2": "image/x-canon-cr2",
}


def _freed(db: Session, img: RecordImage, kind: str) -> HTTPException:
	"""410 for a file freed from the card (app.core.offsite_free), with its object key.

	410 Gone and not 404: the file was removed on purpose and is stored
	offsite, and a client must never read it as lost (NEH-248). Not a 409
	either: nothing the client can change in this request would make the
	file servable; an admin's restore of the volume does.
	"""
	from app.core.offsite_free import freed_detail

	return HTTPException(status_code=410, detail=freed_detail(db, img, kind))


def _image_row(db: Session, img_id: int) -> tuple[RecordImage, Path]:
	"""The image row and its master's resolved path (which may not exist), or a 404.

	A master whose stored path escapes the storage roots is reported as not
	found, the same as a missing file.
	"""
	img = db.query(RecordImage).filter(RecordImage.id == img_id).first()
	if not img:
		raise HTTPException(status_code=404, detail="Image not found")
	if not img.file_path:
		raise HTTPException(status_code=404, detail="Image has no associated file")
	file_path = resolve_within_storage(img.file_path)
	if file_path is None:
		raise HTTPException(status_code=404, detail="File not found on disk")
	return img, file_path


def _stored_master(db: Session, img_id: int) -> tuple[RecordImage, Path]:
	"""The image row and its master's resolved path, a 410 for a freed master, or a 404."""
	img, file_path = _image_row(db, img_id)
	if img.master_freed_at is not None:
		raise _freed(db, img, "capture")
	if not file_path.exists():
		raise HTTPException(status_code=404, detail="File not found on disk")
	return img, file_path


def _raw_preview_sidecar(master: Path) -> Optional[Path]:
	"""A RAW master's `_preview.jpg` sidecar, if it exists inside the storage roots.

	Returns None for a master that is not RAW, and for a sidecar that is
	missing or (through a symlink) resolves outside the storage roots.
	"""
	if not is_raw_suffix(master):
		return None
	sidecar = resolve_within_storage(str(preview_sidecar_of(master)))
	if sidecar is None or not sidecar.is_file():
		return None
	return sidecar


@router.get("/images/{img_id}/file")
def download_image_file(
	img_id: int,
	current_user: User = Depends(allow_read_only),
	db: Session = Depends(get_db_dependency)
):
	"""Download the stored master, unchanged, under its own filename.

	A RAW master is sent as RAW bytes. Use `/images/{img_id}/preview` for
	something a browser can display.
	"""
	img, file_path = _stored_master(db, img_id)
	ext = file_path.suffix.lstrip(".").lower()
	media_type = _MASTER_MEDIA_TYPES.get(ext, "application/octet-stream")
	return FileResponse(
		path=file_path,
		filename=img.filename,
		media_type=media_type
	)


@router.get("/images/{img_id}/preview")
def get_image_preview(
	img_id: int,
	current_user: User = Depends(allow_read_only),
	db: Session = Depends(get_db_dependency)
):
	"""A browser-displayable image of the capture, sent inline.

	A JPEG, PNG or WebP master is its own preview. A RAW master is shown
	through its `_preview.jpg` sidecar, also when the master itself was
	freed from the card. Anything else (TIFF, a RAW master without a
	sidecar) is a 404: this endpoint never sends RAW bytes. A freed file is
	a 410 with its object key.
	"""
	img, file_path = _image_row(db, img_id)
	suffix = file_path.suffix.lower()

	if is_raw_suffix(file_path):
		if img.preview_freed_at is not None:
			raise _freed(db, img, "preview")
		if img.master_freed_at is None and not file_path.exists():
			raise HTTPException(status_code=404, detail="File not found on disk")
	else:
		img, file_path = _stored_master(db, img_id)

	if suffix in _BROWSER_DISPLAYABLE_SUFFIXES:
		ext = suffix.lstrip(".")
		return FileResponse(
			path=file_path,
			filename=img.filename,
			media_type=_MASTER_MEDIA_TYPES[ext],
			content_disposition_type="inline",
		)

	if is_raw_suffix(file_path):
		sidecar = _raw_preview_sidecar(file_path)
		if sidecar is None:
			raise HTTPException(
				status_code=404,
				detail="No JPEG preview exists for this RAW image",
			)
		return FileResponse(
			path=sidecar,
			filename=preview_sidecar_of(Path(img.filename)).name,
			media_type="image/jpeg",
			content_disposition_type="inline",
		)

	raise HTTPException(
		status_code=404,
		detail=f"No browser-displayable preview for {suffix or 'this'} images",
	)


@router.get("/images/{img_id}/thumbnail")
def get_image_thumbnail(
	img_id: int,
	current_user: User = Depends(allow_read_only),
	db: Session = Depends(get_db_dependency)
):
	"""Download the thumbnail for an image. Generates it on demand if missing.

	For a RAW master the thumbnail is generated from its `_preview.jpg`
	sidecar when one exists, as capture does. Freeing keeps thumbnails; one
	that is missing for an image whose source was freed is a 410.
	"""
	img = db.query(RecordImage).filter(RecordImage.id == img_id).first()
	if not img:
		raise HTTPException(status_code=404, detail="Image not found")

	# If the thumbnail is missing, deleted, or outside storage, regenerate it from the source
	thumbnail_path = resolve_within_storage(img.thumbnail_path)
	if thumbnail_path is None or not thumbnail_path.exists():
		if not img.file_path:
			raise HTTPException(status_code=404, detail="Image has no source file for thumbnail generation")
		source_path = resolve_within_storage(img.file_path)
		# A RAW master's thumbnail is made from its preview, never from the
		# RAW (which Pillow cannot read), so a freed preview is a 410 whether
		# or not the master is on the card (a partial free or restore).
		if source_path is not None and is_raw_suffix(source_path) and img.preview_freed_at is not None:
			raise _freed(db, img, "preview")
		sidecar = _raw_preview_sidecar(source_path) if source_path is not None else None
		if source_path is not None and img.master_freed_at is not None and sidecar is None:
			raise _freed(db, img, "capture")
		if source_path is None or not (source_path.exists() or sidecar is not None):
			raise HTTPException(status_code=404, detail="Source image file not found on disk")
		thumb_source = sidecar or source_path
		# The thumbnail is written beside the source. A source under an
		# override root that cannot take writes is refused (503) before the
		# thumbnail's mkdir, rather than writing onto the SD card.
		refuse_writes_under_unusable_root([source_path])
		try:
			# Next to the source's directory: `<stem>/thumbnails/` for a master in
			# `<stem>/captures/`, `.../images/thumbnails/` for one in `.../images/main/`.
			thumbnails_dir = source_path.parent.parent / "thumbnails"
			generated = generate_thumbnail(thumb_source, thumbnails_dir)
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
	"""Delete a single annotation.

	Operators and reviewers may only delete annotations they created; admins
	may delete any. If created_by is NULL (legacy row with no owner recorded),
	ownership cannot be enforced, so the delete is allowed.
	"""
	annotation = db.query(RecordAnnotation).filter(RecordAnnotation.id == annotation_id).first()
	if not annotation:
		raise HTTPException(status_code=404, detail="Annotation not found")

	if (
		annotation.created_by
		and annotation.created_by != current_user.username
		and current_user.role != "admin"
	):
		raise HTTPException(
			status_code=403,
			detail="Only the annotation's creator or an admin can delete it"
		)

	record_id = annotation.record_id
	db.delete(annotation)
	db.commit()
	log_event(db, level="INFO", category="activity", action="annotation_deleted",
	          actor=current_user.username, subject=f"record {record_id} annotation {annotation_id}")
	return {"detail": "Annotation deleted"}


# ==============================================================================
# Status management endpoints
# ==============================================================================

def _locked_record(db: Session, rec_id: int, with_images: bool = True):
	"""Load a record with its row locked for the rest of the transaction.

	The record row is the mutex for its own workflow state: status moves and
	changes to which images are current both take it, so a check made under it
	is still true at commit. `of=Record` keeps the lock off the images, which
	arrive through a LEFT OUTER JOIN — PostgreSQL refuses FOR UPDATE on the
	nullable side of one.

	On SQLite, which the test suite runs on, SQLAlchemy emits no FOR UPDATE at
	all, so the tests exercise the surrounding logic and cannot demonstrate the
	locking. That is verified by reading the emitted SQL, not by the suite.
	"""
	return _locked_record_query(db, rec_id, with_images).first()


def _locked_record_query(db: Session, rec_id: int, with_images: bool = True):
	"""The query _locked_record runs. Split out so a test can compile it
	against the PostgreSQL dialect and read the lock clause off the SQL, which
	is the only way to check it without a server — see
	tests/unit/test_workflow_row_locking.py."""
	query = db.query(Record)
	if with_images:
		query = query.options(joinedload(Record.images))
	return query.filter(Record.id == rec_id).with_for_update(of=Record)


def is_movable(rec: Record) -> bool:
	"""May this record change parents?

	Only while it is still the operator's. Moving a handed-over record out of
	its collection withdraws it from the review it is waiting for, and the
	ruling of 2026-10-17 is that a withdrawal is a per-collection act
	(NEH-260).
	"""
	return rec.status == "captured"


def assert_movable(rec: Record) -> None:
	"""Refuse a parent change on a record that is no longer the operator's.

	Four routes change an existing record's parent: PATCH /records/{id},
	POST /collections/{id}/move-records, and the two project attach/detach
	routes. The other three call this; move-records checks `is_movable` on
	every record first, so it can refuse all of them at once. A
	parent-changing route without the check would let the rule be bypassed in
	two calls, so every new one has to apply it too.
	"""
	if not is_movable(rec):
		raise HTTPException(
			status_code=409,
			detail=(
				f"Record {rec.id} has status '{rec.status}' and cannot be moved. "
				f"Take its collection back first, which returns the whole handover "
				f"to the operator, then move it."
			)
		)


def _assert_promotable(rec: Record) -> None:
	"""
	Refuse to let a record leave 'captured' unless the work is there to hand
	over: at least one current image, and no current image carrying a
	rejection_id (NEH-260).

	The second clause is the substantive test. rejection_id is written in
	reject_record below, on the images current at the moment of rejection, and
	a retake supersedes those while the replacements carry none — so a marked
	current image means the rejection has not been answered. The first clause
	closes a vacuous pass: delete every current image and a purely negative
	predicate holds on a record with nothing in it.

	Two limits, stated rather than left to be found. This proves that no marked
	image is still current, not that the work was redone — nothing links a
	replacement image back to the rejection it answers. And rejection_id is
	declared ondelete="SET NULL", so deleting a RecordRejection row would null
	the link on images that are still current and still the rejected bytes. No
	endpoint deletes rejections today; one added later has to account for it.
	"""
	current = [img for img in rec.images if img.is_current]
	if not current:
		raise HTTPException(
			status_code=422,
			detail=f"Record {rec.id} has no current image, so there is nothing to hand over."
		)
	marked = sorted(img.id for img in current if img.rejection_id is not None)
	if marked:
		raise HTTPException(
			status_code=422,
			detail=(
				f"Record {rec.id} still carries the rejected image(s) {marked}. "
				f"Retake it before handing it over."
			)
		)


def _apply_status_change(
	db: Session,
	rec: Record,
	new_status: str,
	user: User,
) -> None:
	"""
	Validate and apply a status transition on a Record.
	Raises HTTPException on invalid transition or insufficient role.

	Every promotion goes through here — PATCH /{id}/status, POST /bulk-status
	and POST /collections/{id}/promote all call it — so the invariant on
	leaving 'captured' is enforced here rather than at each endpoint.

	Callers must have the record's images loaded AND must hold the record row
	under SELECT ... FOR UPDATE. Validating a snapshot is not enough: the
	check and the write have to be one atomic step, or a second request can
	change what was checked in between. Without the lock, two sequences break
	the invariant (both reproduced): a concurrent reject lands a rejected
	image back on a record this call then promotes, and a concurrent delete
	empties a record this call then promotes. Locking the record row serialises both,
	because every path that moves a record's status or changes which of its
	images are current takes the same lock first.
	"""
	current_status = rec.status
	if current_status == new_status:
		return  # no-op

	allowed_roles = STATUS_TRANSITIONS.get((current_status, new_status))
	if allowed_roles is None:
		raise HTTPException(
			status_code=422,
			detail=f"Transition '{current_status}' -> '{new_status}' is not allowed."
		)
	if user.role not in allowed_roles:
		raise HTTPException(
			status_code=403,
			detail=f"Your role '{user.role}' cannot perform the '{current_status}' -> '{new_status}' transition."
		)
	# Role first, then state: the wrong role learns nothing about the record.
	if current_status == "captured":
		_assert_promotable(rec)

	change_record_status(db, rec, new_status, user.username)


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
	- captured  > in_review : operator, admin  (promotion, NEH-260)
	- in_review > approved  : reviewer, admin
	- approved  > in_review : reviewer, admin  (undo a mistaken approve, NEH-209)
	- rejected  > in_review : reviewer, admin  (undo a mistaken reject, NEH-209)
	- rejected  > captured  : operator, admin  (prepare for retake, NEH-260)

	Promotion is per record here, but the act an operator performs is per
	collection: POST /collections/{id}/promote applies it across a
	collection's captured records, and its inverse takes a handover back.
	Leaving 'captured' also requires a current image carrying no rejection_id
	— see _assert_promotable.

	Formal rejection is not reachable through this endpoint — use POST
	/{rec_id}/reject, which requires a mandatory predefined_reason (NEH-208).
	A retake only happens via the capture endpoints in app/api/cameras.py: it
	supersedes the current images, leaves a captured record captured, and
	returns a rejected one to captured, writing the same rejected > captured
	history row as the prepare-for-retake transition. The record reaches
	review again only when an operator promotes its volume. The undo transitions above are plain
	status resets with no new image, no reason and no rejection record; the
	change itself is dated in record_status_changes, like every other.
	"""
	rec = _locked_record(db, rec_id)
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")

	_apply_status_change(db, rec, payload.status, current_user)

	db.commit()
	db.refresh(rec)
	return _serialize_record(rec)


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
	# Locked for the same reason the single-record path is, and ordered by id
	# so two bulk calls over overlapping sets take the rows in the same order
	# and cannot deadlock against each other.
	records = (
		db.query(Record)
		.options(joinedload(Record.images))
		.filter(Record.id.in_(payload.record_ids))
		.order_by(Record.id)
		.with_for_update(of=Record)
		.all()
	)
	if not records:
		raise HTTPException(status_code=404, detail="No matching records found")

	skipped: list[int] = []
	updated: list[Record] = []

	for rec in records:
		try:
			_apply_status_change(db, rec, payload.status, current_user)
		except HTTPException:
			skipped.append(rec.id)
			continue
		updated.append(rec)

	db.commit()
	for rec in updated:
		db.refresh(rec)

	if skipped:
		logger.info(f"bulk-status: skipped {len(skipped)} records (invalid transition or insufficient role): {skipped}")

	return [_serialize_record(r) for r in updated]


# ==============================================================================
# Rejection endpoints (NEH-208)
# ==============================================================================

def reject_locked_record(db: Session, rec: Record, reason: str, comment: Optional[str], actor: Optional[str]) -> RecordRejection:
	"""Reject an in_review record: a RecordRejection, its id on every current image, and the status change.

	The one place a rejection is written, for POST /{rec_id}/reject and for
	Fisqua's decisions (app.core.fisqua_decisions). The caller holds the
	record row lock with its images loaded, has checked that the record is
	in_review and the reason is one of PREDEFINED_REJECTION_REASONS, and
	commits.
	"""
	current_images = [img for img in rec.images if img.is_current]

	rejection = RecordRejection(
		record_id=rec.id,
		predefined_reason=reason,
		comment=comment,
		rejected_by=actor,
	)
	db.add(rejection)
	db.flush()  # assign rejection.id before linking images

	for img in current_images:
		img.rejection_id = rejection.id

	change_record_status(db, rec, "rejected", actor)
	return rejection


@router.post("/{rec_id}/reject", response_model=RecordRead)
def reject_record(
	rec_id: int,
	payload: RecordRejectRequest,
	current_user: User = Depends(allow_reviewer),
	db: Session = Depends(get_db_dependency)
):
	"""
	Reject a record's current capture with a mandatory predefined reason.
	The comment is optional, except for the reason 'other', which is refused
	with 422 unless the comment says what is wrong.

	Flags every current image as part of this rejection's audit trail (for a
	dual-mode record that's both L and R — a dual-camera pair can only ever be
	redone together, never one side alone) and moves the record to
	'rejected'. Rejected images are never deleted or overwritten; they stay
	queryable via GET /{rec_id}/rejections after a recapture supersedes them.
	"""
	# Locked: this writes rejection_id onto the current images and moves the
	# status, both of which the promotion invariant reads (NEH-260).
	rec = _locked_record(db, rec_id)
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")
	if rec.status != "in_review":
		raise HTTPException(
			status_code=422,
			detail=f"Cannot reject a record with status '{rec.status}'. Only 'in_review' records can be rejected."
		)

	reject_locked_record(db, rec, payload.predefined_reason, payload.comment, current_user.username)

	db.commit()
	db.refresh(rec)
	return _serialize_record(rec)


@router.get("/{rec_id}/rejections", response_model=List[RecordRejectionRead])
def list_record_rejections(
	rec_id: int,
	current_user: User = Depends(allow_read_only),
	db: Session = Depends(get_db_dependency)
):
	"""List a record's rejection history, newest first, each with the image(s) it flagged."""
	rec = db.query(Record).filter(Record.id == rec_id).first()
	if not rec:
		raise HTTPException(status_code=404, detail="Record not found")

	rejections = (
		db.query(RecordRejection)
		.options(joinedload(RecordRejection.images))
		.filter(RecordRejection.record_id == rec_id)
		.order_by(RecordRejection.rejected_at.desc(), RecordRejection.id.desc())
		.all()
	)
	return [RecordRejectionRead.model_validate(r) for r in rejections]
