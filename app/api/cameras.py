from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func
from app.models.record import Record, RecordImage
from typing import List, Literal, Optional
from pydantic import BaseModel, field_validator
import logging

from app.api.deps import get_db_dependency
from app.api.auth import get_current_user, RoleChecker
from app.models.camera import CameraSettings
from app.models.user import User
from app.models.project import Project
from app.models.collection import Collection
from app.schemas.camera import CameraSettingsCreate, CameraSettingsRead, CameraSettingsUpdate
from app.core.thumbnail import generate_thumbnail
from app.core.storage_ops import resolve_project_name
from app.core.db_errors import integrity_conflict

router = APIRouter()
logger = logging.getLogger(__name__)

allow_contributor = RoleChecker(["admin", "operator"])
allow_read_only = RoleChecker(["admin", "operator", "reviewer"])


def _next_record_sequence(db: Session, project_id: Optional[int], collection_id: Optional[int]) -> int:
	"""Next monotonic record sequence within a collection (or project).

	Assigned at capture time so export order never depends on the wall clock,
	which is unreliable on a Pi without an RTC after a power cut.
	"""
	query = db.query(func.max(Record.sequence))
	if collection_id is not None:
		query = query.filter(Record.collection_id == collection_id)
	else:
		query = query.filter(Record.project_id == project_id)
	current_max = query.scalar()
	return (current_max + 1) if current_max is not None else 0


def _resolve_capture_target(db: Session, project_name: str, collection_id: Optional[int]) -> tuple[str, Optional[str]]:
	"""Authoritative (project_name, collection_name) for the on-disk capture path."""
	if collection_id:
		col = db.query(Collection).filter(Collection.id == collection_id).first()
		if not col:
			raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")
		resolved = resolve_project_name(db, col)
		if not resolved:
			raise HTTPException(status_code=422, detail=f"Collection {collection_id} is not attached to a project")
		return resolved, col.name
	project = db.query(Project).filter(Project.name == project_name).first()
	if not project:
		raise HTTPException(status_code=422, detail="project_name does not match a known project")
	return project.name, None


class DeviceInfo(BaseModel):
	"""Information about a detected camera device."""
	hardware_id: str
	model: str
	index: int
	location: Optional[str] = None
	machine_id: Optional[str] = None
	label: Optional[str] = None
	calibrated: bool = False
	# Calibration data (populated when calibrated=True)
	lens_position: Optional[float] = None
	awb_gains: Optional[List[float]] = None
	orientation: Optional[int] = None  # Saved rotation for this body, if ever set (NEH-71)
	# Capabilities
	has_aperture_control: bool = False
	supports_zoom: bool = False  # True when ScalerCrop is available (picamera2 backend)


_VALID_ROTATIONS = (0, 90, 180, 270)


def _validate_rotate_deg(v: Optional[int]) -> Optional[int]:
	if v is not None and v not in _VALID_ROTATIONS:
		raise ValueError(f"rotate_deg must be one of {_VALID_ROTATIONS}; got {v}")
	return v


class CaptureRequest(BaseModel):
	"""Request body for capture endpoint."""
	project_name: str
	camera_index: int = 0
	resolution: str = "medium"  # low, medium, high
	include_resolution_in_filename: bool = False
	# Clockwise rotation applied post-capture: 0, 90, 180, 270, or None.
	# None (the default) leaves the registry's saved orientation from
	# default_camera_config_from_registry in place; an explicit value,
	# including 0, overrides it (NEH-71 - the one behaviour change of the ticket).
	rotate_deg: Optional[int] = None
	record_id: Optional[int] = None  # Link to existing record, or create new if None
	record_title: Optional[str] = None  # Used if creating new record
	collection_id: Optional[int] = None  # Collection to link the record to

	@field_validator("rotate_deg")
	@classmethod
	def _check_rotate_deg(cls, v: Optional[int]) -> Optional[int]:
		return _validate_rotate_deg(v)


class DualCaptureRequest(BaseModel):
	"""Request body for dual capture endpoint."""
	project_name: str
	resolution: str = "medium"
	include_resolution_in_filename: bool = False
	stagger_ms: int = 20
	# Clockwise rotation per camera: 0, 90, 180, 270, or None. None leaves the
	# registry's saved orientation in place; an explicit value, including 0,
	# overrides it (NEH-71 - see CaptureRequest.rotate_deg).
	rotate_deg_cam0: Optional[int] = None
	rotate_deg_cam1: Optional[int] = None
	record_id: Optional[int] = None  # Link to existing record, or create new if None
	record_title: Optional[str] = None  # Used if creating new record
	sequence: Optional[int] = None  # Page number/order
	left_camera_index: int = 0  # Which camera index maps to the left page (0 or 1)
	collection_id: Optional[int] = None  # Collection to link the record to

	@field_validator("rotate_deg_cam0", "rotate_deg_cam1")
	@classmethod
	def _check_rotate_deg(cls, v: Optional[int]) -> Optional[int]:
		return _validate_rotate_deg(v)


class CaptureResponse(BaseModel):
	"""Response from capture endpoints."""
	success: bool
	file_path: Optional[str] = None
	file_paths: Optional[List[str]] = None
	record_id: Optional[int] = None
	image_ids: Optional[List[int]] = None
	timing: Optional[dict] = None
	error: Optional[str] = None


class CalibrationRequest(BaseModel):
	"""Request for camera calibration."""
	camera_index: int = 0
	resolution: str = "high"


class CalibrationResponse(BaseModel):
	"""Response from calibration endpoint."""
	success: bool
	lens_position: Optional[float] = None
	distance_meters: Optional[float] = None
	af_time: Optional[float] = None
	error: Optional[str] = None


class WhiteBalanceCalibrationRequest(BaseModel):
	"""Request for white balance calibration."""
	camera_index: int = 0
	resolution: str = "high"
	stabilization_frames: int = 30


class WhiteBalanceCalibrationResponse(BaseModel):
	"""Response from white balance calibration endpoint."""
	success: bool
	awb_gains: Optional[List[float]] = None
	colour_temperature: Optional[int] = None
	converged: Optional[bool] = None
	error: Optional[str] = None


class WhiteBalanceManualRequest(BaseModel):
	"""Request to commit manually-sampled AWB gains to the registry."""
	camera_index: int = 0
	awb_gains: List[float]  # [red_gain, blue_gain]


class DSLRSettingsResponse(BaseModel):
	"""Current DSLR camera settings read via PTP."""
	iso: Optional[str] = None
	shutter_speed: Optional[str] = None
	aperture: Optional[str] = None
	image_format: Optional[str] = None
	focus_mode: Optional[str] = None
	flash_mode: Optional[str] = None


class DSLRSettingsUpdate(BaseModel):
	"""Request body for updating DSLR settings (all fields optional)."""
	iso: Optional[str] = None           # PTP iso value e.g. "400"
	shutter_speed: Optional[str] = None  # PTP shutterspeed e.g. "1/125"
	aperture: Optional[str] = None       # PTP aperture e.g. "5.6"
	image_format: Optional[str] = None   # "JPEG", "RAW", or "RAW+JPEG"


def _get_camera_registry():
	"""Get or create camera registry. Handles import errors gracefully."""
	try:
		from capture.camera_registry import CameraRegistry
		return CameraRegistry()
	except ImportError as e:
		logger.warning(f"Camera registry not available: {e}")
		return None
	except Exception as e:
		logger.error(f"Failed to initialize camera registry: {e}")
		return None


def _device_infos(raw_devices, registry) -> List[DeviceInfo]:
	"""Enrich raw backend device dicts with registry calibration data.

	Shared by the enumeration and rescan routes so both return the same
	DeviceInfo shape from the same backend dicts. A registry of None (import or
	init failure) simply means nothing is enriched; enumeration still works.
	"""
	devices = []
	for dev in raw_devices:
		hw_id = dev["hardware_id"]
		idx = dev["index"]

		# Enrich with registry calibration data
		camera_data = registry.get_camera_by_id(hw_id) if registry else None
		calibrated = False
		machine_id = None
		label = None
		lens_position = None
		awb_gains = None
		orientation = None

		if camera_data:
			focus_cal = camera_data.get("calibration", {}).get("focus", {})
			calibrated = bool(focus_cal.get("success"))
			machine_id = camera_data.get("machine_id")
			label = camera_data.get("label")
			lens_position = focus_cal.get("lens_position")
			awb_raw = camera_data.get("calibration", {}).get("white_balance", {}).get("awb_gains")
			if awb_raw:
				awb_gains = list(awb_raw)
			# Report only a supported angle; a malformed stored value is
			# shown as unset so the UI never receives an angle it cannot use.
			stored = camera_data.get("orientation")
			orientation = stored if type(stored) is int and stored in _VALID_ROTATIONS else None

		devices.append(DeviceInfo(
			hardware_id=hw_id,
			model=dev.get("model", "unknown"),
			index=idx,
			location=dev.get("location"),
			machine_id=machine_id,
			label=label,
			calibrated=calibrated,
			lens_position=lens_position,
			awb_gains=awb_gains,
			orientation=orientation,
			has_aperture_control=dev.get("has_aperture_control", False),
			supports_zoom=dev.get("supports_zoom", False),
		))

	return devices


@router.get("/devices", response_model=List[DeviceInfo])
def list_camera_devices(current_user: User = Depends(allow_read_only)):
	"""
	Return available camera devices detected by the active camera backend.

	Returns hardware IDs, models, and calibration status for each camera.
	Works with both picamera2 (IMX519) and gphoto2 (DSLR) backends.
	On non-Pi systems or if camera libraries aren't available, returns empty list.
	"""
	registry = _get_camera_registry()

	try:
		from capture.service import get_backend
		backend = get_backend()
		raw_devices = backend.list_devices()
	except Exception as e:
		logger.error(f"Failed to list camera devices: {e}")
		return []

	return _device_infos(raw_devices, registry)


@router.post("/rescan", response_model=List[DeviceInfo])
def rescan_camera_devices(current_user: User = Depends(allow_contributor)):
	"""
	Re-detect the attached cameras and drop any stale device sessions.

	The operator's recovery lever when a DSLR drops off USB or re-enumerates
	onto a different port mid-session: the backend rebuilds its port map and
	closes the sessions that no longer match it, so the next capture opens
	against the hardware as it actually is. Returns the same DeviceInfo list as
	GET /devices.

	This mutates backend state, so it sits behind allow_contributor rather than
	allow_read_only. A backend failure is a 503 rather than an empty list: an
	empty list reads as "no cameras attached" and would hide the failure from
	the operator who just asked for a rescan.
	"""
	registry = _get_camera_registry()

	try:
		from capture.service import get_backend
		backend = get_backend()
		raw_devices = backend.rescan()
	except Exception as exc:
		logger.exception(f"Camera rescan failed: {exc}")
		raise HTTPException(status_code=503, detail=f"Camera rescan failed: {exc}")

	return _device_infos(raw_devices, registry)


@router.get("/capabilities")
def get_camera_capabilities(current_user: User = Depends(allow_read_only)):
	"""
	Return the capability flags of the active camera backend.

	The frontend uses these flags to show/hide controls that are only
	available for specific backends (e.g. focus slider for picamera2,
	ISO/shutter/aperture dropdowns for gphoto2 DSLRs).

	Example response:
	    {
	        "backend": "gphoto2",
	        "live_preview": true,
	        "focus_control": false,
	        "live_controls": false,
	        "zoom": false,
	        "autofocus_calibration": false,
	        "dslr_settings": true
	    }
	"""
	try:
		from capture.service import get_backend
		backend = get_backend()
		caps = backend.get_capabilities()
		return {"backend": backend.get_backend_name(), **caps}
	except HTTPException:
		raise
	except Exception as e:
		logger.error(f"Failed to get capabilities: {e}")
		raise HTTPException(status_code=503, detail=f"Capture system not available: {e}")


@router.get("/preview/{camera_index}")
def get_camera_preview(
	camera_index: int,
	resolution: str = Query("medium"),
	current_user: User = Depends(allow_read_only),
):
	"""
	Capture a live preview frame and return it as JPEG.

	Called by the frontend every PREVIEW_INTERVAL_MS milliseconds for the
	live preview view.  The frame rides on the still configuration for
	`resolution`, so it shows the field of view a capture at that resolution
	would record, with no AF cycle and no denoise warmup.

	Returns 422 for an unknown resolution, 404 when the requested camera is
	not connected or the frame could not be captured (any RuntimeError from
	the capture service), 500 on anything else.
	"""
	from fastapi.responses import Response

	try:
		from capture.service import capture_preview_frame
	except ImportError as e:
		raise HTTPException(status_code=503, detail=f"Capture system not available: {e}")

	try:
		jpeg_bytes = capture_preview_frame(camera_index, resolution)
		return Response(content=jpeg_bytes, media_type="image/jpeg")
	except ValueError as e:
		raise HTTPException(status_code=422, detail=str(e))
	except RuntimeError as e:
		raise HTTPException(status_code=404, detail=str(e))
	except Exception as e:
		logger.exception(f"Preview capture failed for camera {camera_index}: {e}")
		raise HTTPException(status_code=500, detail="Preview capture failed")


@router.delete("/preview/tmp")
def flush_preview_tmp_files(
	current_user: User = Depends(allow_contributor),
):
	"""
	Delete stale preview temp files left in /tmp.

	These files (dtk_preview_c*.jpg) are normally removed immediately after each
	preview poll, but can be left behind if the backend process was killed
	unexpectedly.  Call this from the admin settings page to reclaim disk space.

	Returns the number of files deleted.
	"""
	try:
		from capture.service import flush_preview_tmp
	except ImportError as e:
		raise HTTPException(status_code=503, detail=f"Capture system not available: {e}")

	deleted = flush_preview_tmp()
	return {"deleted": deleted, "detail": f"Removed {deleted} stale preview file(s) from /tmp"}


# ---------------------------------------------------------------------------
# Focus endpoints
# ---------------------------------------------------------------------------

class FocusRequest(BaseModel):
	"""Request body for manual focus endpoint."""
	lens_position: float  # Dioptres: 0 = infinity, 10 ~ 10 cm


class FocusResponse(BaseModel):
	camera_index: int
	lens_position: float


@router.get("/focus/{camera_index}", response_model=FocusResponse)
def get_focus(
	camera_index: int,
	current_user: User = Depends(allow_read_only),
):
	"""Return the current lens position (dioptres) for the given camera."""
	try:
		from capture.service import get_focus as _get_focus
	except ImportError as e:
		raise HTTPException(status_code=503, detail=f"Capture system not available: {e}")

	try:
		pos = _get_focus(camera_index)
		return FocusResponse(camera_index=camera_index, lens_position=pos)
	except RuntimeError as e:
		raise HTTPException(status_code=404, detail=str(e))
	except Exception as e:
		logger.exception(f"get_focus failed for camera {camera_index}: {e}")
		raise HTTPException(status_code=500, detail="Failed to get focus")


@router.post("/focus/{camera_index}", response_model=FocusResponse)
def set_focus(
	camera_index: int,
	request: FocusRequest,
	current_user: User = Depends(allow_contributor),
):
	"""Set manual lens position (dioptres) on the given camera."""
	try:
		from capture.service import set_focus as _set_focus
	except ImportError as e:
		raise HTTPException(status_code=503, detail=f"Capture system not available: {e}")

	try:
		pos = _set_focus(camera_index, request.lens_position)
		return FocusResponse(camera_index=camera_index, lens_position=pos)
	except RuntimeError as e:
		raise HTTPException(status_code=404, detail=str(e))
	except Exception as e:
		logger.exception(f"set_focus failed for camera {camera_index}: {e}")
		raise HTTPException(status_code=500, detail="Failed to set focus")


# ---------------------------------------------------------------------------
# Camera settings / controls endpoint
# ---------------------------------------------------------------------------

class CameraSettingsRequest(BaseModel):
	"""Arbitrary camera controls to apply live (all fields optional)."""
	ae_enable: Optional[bool] = None          # Auto-exposure on/off
	awb_enable: Optional[bool] = None         # Auto white-balance on/off
	exposure_value: Optional[float] = None    # EV compensation (requires ae_enable=True)
	exposure_time_us: Optional[int] = None    # Manual shutter time in microseconds
	analogue_gain: Optional[float] = None     # Manual gain (ISO 100 ~ 1.0)
	colour_gains: Optional[List[float]] = None  # Manual WB as [red_gain, blue_gain]
	zoom_factor: Optional[float] = None       # ScalerCrop digital zoom (1.0 = full sensor)


@router.post("/settings/{camera_index}")
def apply_camera_settings(
	camera_index: int,
	request: CameraSettingsRequest,
	current_user: User = Depends(allow_contributor),
):
	"""Apply live camera controls without triggering a capture."""
	try:
		from capture.service import set_camera_controls
	except ImportError as e:
		raise HTTPException(status_code=503, detail=f"Capture system not available: {e}")

	# Map request fields to picamera2 control names
	controls: dict = {}
	if request.ae_enable is not None:
		controls["AeEnable"] = request.ae_enable
	if request.awb_enable is not None:
		controls["AwbEnable"] = request.awb_enable
	if request.exposure_value is not None:
		controls["ExposureValue"] = float(request.exposure_value)
	if request.exposure_time_us is not None:
		controls["ExposureTime"] = int(request.exposure_time_us)
	if request.analogue_gain is not None:
		controls["AnalogueGain"] = float(request.analogue_gain)
	if request.colour_gains is not None and len(request.colour_gains) == 2:
		controls["ColourGains"] = (float(request.colour_gains[0]), float(request.colour_gains[1]))

	# Zoom is handled separately: requires picam2 instance to compute ScalerCrop
	if request.zoom_factor is not None:
		try:
			from capture.service import apply_zoom
			apply_zoom(camera_index, float(request.zoom_factor))
		except RuntimeError as e:
			raise HTTPException(status_code=404, detail=str(e))
		except Exception as e:
			logger.exception(f"apply_zoom failed for camera {camera_index}: {e}")
			raise HTTPException(status_code=500, detail="Failed to apply zoom")

	if not controls and request.zoom_factor is None:
		return {"detail": "No controls specified"}

	try:
		set_camera_controls(camera_index, controls)
		return {"detail": "Controls applied", "controls": list(controls.keys())}
	except RuntimeError as e:
		raise HTTPException(status_code=404, detail=str(e))
	except Exception as e:
		logger.exception(f"apply_camera_settings failed for camera {camera_index}: {e}")
		raise HTTPException(status_code=500, detail="Failed to apply camera settings")


def _is_capture_timeout(exc: BaseException) -> bool:
	"""True if exc, or something it was raised from, is a CaptureTimeoutError.

	capture/backends/gphoto2_backend.py:1418 wraps a CaptureTimeoutError in a
	plain RuntimeError ("DSLR capture failed: ...") so a stalled DSLR body
	can travel the same failure path as any other capture error, keeping the
	original timeout as __cause__. Catching RuntimeError (or CaptureTimeoutError
	itself, which is a RuntimeError subclass) would therefore either be too
	broad or miss every real DSLR timeout wrapped this way, so this walks a
	few links of the __cause__ / __context__ chain looking for the real class.
	"""
	from capture.backends.gphoto2_backend import CaptureTimeoutError

	current = exc
	for _ in range(5):
		if current is None:
			return False
		if isinstance(current, CaptureTimeoutError):
			return True
		current = current.__cause__ or current.__context__
	return False


@router.post("/test-capture/{camera_index}")
def test_capture(
	camera_index: int,
	resolution: str = Query("medium"),
	current_user: User = Depends(allow_contributor),
):
	"""
	Take a real still capture - shutter, autofocus, the full backend path -
	and return it inline as a JPEG, never stored (dashboard "Probar camaras"
	button; NEH-166, option (a)).

	Uses the literal-first path (/test-capture/{camera_index}, not
	/{camera_index}/test-capture) to match this router's existing convention
	for POST routes that take a camera_index (/focus/{camera_index},
	/settings/{camera_index}), and to keep it unambiguous against any future
	/{camera_index}-shaped route.

	Checks the camera is connected before calling the service, mirroring
	trigger_capture, rather than sniffing the RuntimeError message for
	"not connected" after the fact.
	"""
	from capture.camera import IMG_SIZES

	if resolution not in IMG_SIZES:
		raise HTTPException(status_code=422, detail=f"Invalid resolution: {resolution}")

	from capture.service import test_capture_bytes, is_camera_connected

	if not is_camera_connected(camera_index):
		raise HTTPException(status_code=404, detail=f"Camera {camera_index} is not connected")

	try:
		image_bytes, elapsed_time = test_capture_bytes(camera_index, resolution)
	except RuntimeError as e:
		if _is_capture_timeout(e):
			raise HTTPException(status_code=504, detail=str(e))
		raise HTTPException(status_code=500, detail=str(e))
	except HTTPException:
		raise
	except Exception:
		logger.exception("Test capture failed")
		raise HTTPException(status_code=500, detail="Test capture failed")

	return Response(
		content=image_bytes,
		media_type="image/jpeg",
		headers={
			"X-Capture-Seconds": f"{elapsed_time:.3f}",
			"X-Capture-Bytes": str(len(image_bytes)),
		},
	)


@router.post("/capture", response_model=CaptureResponse)
def trigger_capture(
	request: CaptureRequest,
	current_user: User = Depends(allow_contributor),
	db: Session = Depends(get_db_dependency)
):
	"""
	Trigger a single image capture on the specified camera.
	
	Creates or links to existing Record, then creates RecordImage with capture manifest linkage.
	"""
	try:
		from capture.service import single_capture_image, is_camera_connected
		from capture.camera import CameraConfig, IMG_SIZES
		from capture.project_manager import default_camera_config_from_registry
		from PIL import Image
		from PIL.ExifTags import TAGS
		from app.models.project import Project
		from app.models.record import ExifData
	except ImportError as e:
		return CaptureResponse(success=False, error=f"Capture system not available: {e}")
	
	# Validate camera is connected
	if not is_camera_connected(request.camera_index):
		return CaptureResponse(
			success=False, 
			error=f"Camera {request.camera_index} is not connected"
		)
	
	try:
		# Get camera config from registry (with calibration if available)
		config_dict, hw_id = default_camera_config_from_registry(
			request.camera_index,
			request.resolution
		)
		# NEH-71: omitted (None) leaves the registry's saved orientation from
		# default_camera_config_from_registry in place; an explicit value,
		# including 0, overrides it. This is the one behaviour change of the ticket.
		if request.rotate_deg is not None:
			config_dict["rotate_deg"] = request.rotate_deg
		camera_config = CameraConfig(**config_dict)
		
		# Resolve the path from collection_id via the DB
		project_name, collection_name = _resolve_capture_target(db, request.project_name, request.collection_id)

		output_path, capture_id, pair_id = single_capture_image(
			project_name=project_name,
			camera_config=camera_config,
			check_camera=False,  # Already checked
			include_resolution=request.include_resolution_in_filename,
			collection_name=collection_name
		)
		
		# Extract image dimensions and EXIF data
		from pathlib import Path
		file_path = Path(output_path)
		file_size = file_path.stat().st_size if file_path.exists() else 0
		resolution_width = None
		resolution_height = None
		exif_dict = {}
		
		try:
			with Image.open(output_path) as img:
				resolution_width, resolution_height = img.size
				# Extract EXIF data if available
				try:
					exif_data = img._getexif()
					if exif_data:
						for tag_id, value in exif_data.items():
							tag_name = TAGS.get(tag_id, tag_id)
							exif_dict[tag_name] = str(value)
				except:
					pass  # No EXIF data or error reading
		except Exception as e:
			logger.warning(f"Could not extract image metadata: {e}")
		
		# Reuse the resolved project name so the DB record matches the on-disk tree
		project = db.query(Project).filter(Project.name == project_name).first()
		project_id = project.id if project else None

		# Records can have either project_id OR collection_id, not both (DB constraint).
		# When a collection is provided, the project association is implicit through it.
		effective_project_id = None if request.collection_id else project_id

		# Get or create Record
		is_recapture = False
		if request.record_id:
			# Link to existing record
			record = db.query(Record).filter(Record.id == request.record_id).first()
			if not record:
				raise HTTPException(status_code=404, detail=f"Record {request.record_id} not found")
			if record.status == "rejected":
				# Recapture (NEH-208): a rejected record can only be redone with
				# the same capture mode it was originally taken with — a single
				# capture can't turn a dual-mode record's pair back into one image.
				if record.capture_mode != "single":
					raise HTTPException(
						status_code=422,
						detail=f"Record {record.id} was captured in '{record.capture_mode}' mode; use the dual-capture endpoint to recapture it."
					)
				is_recapture = True
				from app.api.records import _supersede_current_images
				_supersede_current_images(record)
		else:
			# Create new record for this capture
			record = Record(
				title=request.record_title or f"{project_name} - {file_path.stem}",
				description=f"Captured at {request.resolution} resolution",
				object_typology="document",
				project_id=effective_project_id,
				collection_id=request.collection_id,
				sequence=_next_record_sequence(db, effective_project_id, request.collection_id),
				created_by=current_user.username,
				capture_mode="single",
			)
			db.add(record)
			db.flush()  # Get the ID
		
		# Generate thumbnail alongside the captured images
		# For RAW (.cr2), use the _preview.jpg extracted by rawpy if available
		thumbnail_path = None
		try:
			thumbnails_dir = file_path.parent.parent / "thumbnails"
			thumb_source = file_path
			if file_path.suffix.lower() == ".cr2":
				preview = file_path.with_name(file_path.stem + "_preview.jpg")
				if preview.exists():
					thumb_source = preview
			thumbnail_path = generate_thumbnail(thumb_source, thumbnails_dir)
		except Exception as e:
			logger.warning(f"Failed to generate thumbnail for {file_path.name}: {e}")

		file_format = file_path.suffix.lower().lstrip(".")

		# Create RecordImage with capture linkage
		img = RecordImage(
			record_id=record.id,
			filename=file_path.name,
			file_path=str(output_path),
			thumbnail_path=thumbnail_path,
			file_size=file_size,
			format=file_format,
			resolution_width=resolution_width,
			resolution_height=resolution_height,
			capture_id=capture_id,
			pair_id=pair_id,
			role="single",
			uploaded_by=current_user.username,
		)
		
		db.add(img)
		db.flush()  # Get the ID
		
		# Save camera settings
		cs = CameraSettings(
			record_image_id=img.id,
			camera_model=camera_config.__class__.__name__,
			iso=None,
			aperture=None,
			focal_length=None,
			white_balance=camera_config.awb,
		)
		db.add(cs)
		
		# Save EXIF data
		if exif_dict:
			ex = ExifData(
				record_image_id=img.id,
				raw_exif=str(exif_dict),
			)
			db.add(ex)

		if is_recapture:
			record.status = "in_review"

		db.commit()
		db.refresh(record)
		db.refresh(img)

		logger.info(f"Created record {record.id}, image {img.id}, capture_id={capture_id}")
		
		return CaptureResponse(
			success=True,
			file_path=str(output_path),
			record_id=record.id,
			image_ids=[img.id]
		)
	except HTTPException:
		raise
	except Exception as e:
		logger.exception(f"Capture failed: {e}")
		# DB-only rollback: a file that already has a manifest entry is kept (recoverable via ?orphaned=true), never auto-unlinked
		db.rollback()
		return CaptureResponse(success=False, error=str(e))


@router.post("/capture/dual", response_model=CaptureResponse)
def trigger_dual_capture(
	request: DualCaptureRequest,
	current_user: User = Depends(allow_contributor),
	db: Session = Depends(get_db_dependency)
):
	"""
	Trigger simultaneous capture on both cameras (index 0 and 1).
	
	Used for book scanning where left and right pages are captured together.
	Creates or links to existing Record, then creates two linked RecordImages.
	"""
	try:
		from capture.service import dual_capture_image, is_camera_connected
		from capture.camera import CameraConfig
		from capture.project_manager import default_camera_config_from_registry
		from PIL import Image
		from PIL.ExifTags import TAGS
		from pathlib import Path
		from app.models.project import Project
		from app.models.record import ExifData
	except ImportError as e:
		return CaptureResponse(success=False, error=f"Capture system not available: {e}")
	
	# Validate both cameras are connected
	for idx in [0, 1]:
		if not is_camera_connected(idx):
			return CaptureResponse(
				success=False,
				error=f"Camera {idx} is not connected"
			)
	
	try:
		# Get configs from registry with calibration
		config0_dict, _ = default_camera_config_from_registry(0, request.resolution)
		config1_dict, _ = default_camera_config_from_registry(1, request.resolution)

		# NEH-71: see trigger_capture - omitted (None) leaves the registry's
		# saved orientation in place; an explicit value, including 0, overrides it.
		if request.rotate_deg_cam0 is not None:
			config0_dict["rotate_deg"] = request.rotate_deg_cam0
		if request.rotate_deg_cam1 is not None:
			config1_dict["rotate_deg"] = request.rotate_deg_cam1

		cam0_config = CameraConfig(**config0_dict)
		cam1_config = CameraConfig(**config1_dict)
		
		# Resolve the path from collection_id via the DB
		project_name, collection_name = _resolve_capture_target(db, request.project_name, request.collection_id)

		path0, path1, capture_id, pair_id = dual_capture_image(
			project_name=project_name,
			cam1_config=cam0_config,
			cam2_config=cam1_config,
			check_camera=False,
			include_resolution=request.include_resolution_in_filename,
			stagger_ms=request.stagger_ms,
			collection_name=collection_name
		)

		# Reuse the resolved project name so the DB record matches the on-disk tree
		project = db.query(Project).filter(Project.name == project_name).first()
		project_id = project.id if project else None
		
		# Records can have either project_id OR collection_id, not both (DB constraint).
		# When a collection is provided, the project association is implicit through it.
		effective_project_id = None if request.collection_id else project_id
		
		# Get or create Record
		is_recapture = False
		if request.record_id:
			# Link to existing record (adding new pages to multi-page document)
			record = db.query(Record).filter(Record.id == request.record_id).first()
			if not record:
				raise HTTPException(status_code=404, detail=f"Record {request.record_id} not found")
			if record.status == "rejected":
				# Recapture (NEH-208): a rejected record can only be redone with
				# the same capture mode it was originally taken with — a dual
				# pair can't turn a single-mode record into two images.
				if record.capture_mode != "dual":
					raise HTTPException(
						status_code=422,
						detail=f"Record {record.id} was captured in '{record.capture_mode}' mode; use the single-capture endpoint to recapture it."
					)
				is_recapture = True
				from app.api.records import _supersede_current_images
				_supersede_current_images(record)
		else:
			# Create new record for this dual capture
			record = Record(
				title=request.record_title or f"{project_name} - Dual capture",
				description=f"Dual camera capture at {request.resolution} resolution",
				object_typology="book",  # Default to book for dual captures
				project_id=effective_project_id,
				collection_id=request.collection_id,
				sequence=_next_record_sequence(db, effective_project_id, request.collection_id),
				created_by=current_user.username,
				capture_mode="dual",
			)
			db.add(record)
			db.flush()  # Get the ID
		
		# Helper to process captured image
		def create_image_record(file_path_str: str, camera_idx: int, role: str):
			file_path = Path(file_path_str)
			file_size = file_path.stat().st_size if file_path.exists() else 0
			
			# Extract image info
			resolution_width = None
			resolution_height = None
			exif_dict = {}
			
			try:
				with Image.open(file_path_str) as img:
					resolution_width, resolution_height = img.size
					try:
						exif_data = img._getexif()
						if exif_data:
							for tag_id, value in exif_data.items():
								tag_name = TAGS.get(tag_id, tag_id)
								exif_dict[tag_name] = str(value)
					except:
						pass
			except Exception as e:
				logger.warning(f"Could not extract image metadata for {file_path}: {e}")
			
			# Generate thumbnail alongside the captured images
			# For RAW (.cr2), use the _preview.jpg extracted by rawpy if available
			thumbnail_path = None
			try:
				thumbnails_dir = file_path.parent.parent / "thumbnails"
				thumb_source = file_path
				if file_path.suffix.lower() == ".cr2":
					preview = file_path.with_name(file_path.stem + "_preview.jpg")
					if preview.exists():
						thumb_source = preview
				thumbnail_path = generate_thumbnail(thumb_source, thumbnails_dir)
			except Exception as e:
				logger.warning(f"Failed to generate thumbnail for {file_path.name}: {e}")

			file_format = file_path.suffix.lower().lstrip(".")

			# Create RecordImage with capture linkage
			img = RecordImage(
				record_id=record.id,
				filename=file_path.name,
				file_path=str(file_path_str),
				thumbnail_path=thumbnail_path,
				file_size=file_size,
				format=file_format,
				resolution_width=resolution_width,
				resolution_height=resolution_height,
				capture_id=capture_id,  # Both images share same capture event
				pair_id=pair_id,  # Both images share same pair_id
				sequence=request.sequence,
				role=role,
				uploaded_by=current_user.username,
			)
			
			db.add(img)
			db.flush()
			
			# Camera settings
			cam_config = cam0_config if camera_idx == 0 else cam1_config
			cs = CameraSettings(
				record_image_id=img.id,
				camera_model=cam_config.__class__.__name__,
				iso=None,
				aperture=None,
				focal_length=None,
				white_balance=cam_config.awb,
			)
			db.add(cs)
			
			# EXIF data
			if exif_dict:
				ex = ExifData(
					record_image_id=img.id,
					raw_exif=str(exif_dict),
				)
				db.add(ex)
			
			return img
		
		# Create RecordImages for both captures with appropriate roles
		# left_camera_index controls which physical camera maps to the "left" page
		role0 = "left" if request.left_camera_index == 0 else "right"
		role1 = "right" if request.left_camera_index == 0 else "left"
		img0 = create_image_record(str(path0), 0, role0)
		img1 = create_image_record(str(path1), 1, role1)

		if is_recapture:
			record.status = "in_review"

		db.commit()
		db.refresh(record)
		
		logger.info(
			f"Created dual capture: record {record.id}, images [{img0.id}, {img1.id}], "
			f"capture_id={capture_id}, pair_id={pair_id}"
		)
		
		return CaptureResponse(
			success=True,
			file_paths=[str(path0), str(path1)],
			record_id=record.id,
			image_ids=[img0.id, img1.id]
		)
	except HTTPException:
		raise
	except Exception as e:
		logger.exception(f"Dual capture failed: {e}")
		# DB-only rollback: a file that already has a manifest entry is kept (recoverable via ?orphaned=true), never auto-unlinked
		db.rollback()
		return CaptureResponse(success=False, error=str(e))


@router.post("/calibrate", response_model=CalibrationResponse)
def calibrate_camera(
	request: CalibrationRequest,
	current_user: User = Depends(allow_contributor)
):
	"""
	Run autofocus calibration on a camera to find optimal lens position.
	
	For fixed-distance setups (book scanning), this determines the best
	focus position which is then stored and reused for faster captures.
	"""
	try:
		from capture.camera_registry import CameraRegistry
		from capture.camera import IMG_SIZES
		from capture.service import get_backend
	except ImportError as e:
		return CalibrationResponse(success=False, error=f"Calibration system not available: {e}")

	try:
		backend = get_backend()
		if not backend.get_capabilities().get("autofocus_calibration", False):
			raise HTTPException(
				status_code=501,
				detail=f"{backend.get_backend_name()} backend does not support autofocus calibration",
			)

		img_size = IMG_SIZES.get(request.resolution, IMG_SIZES["high"])

		# Route the AF cycle through the backend's own Picamera2 instance.
		# The old approach (CameraCalibration / calibration.py) opened a brand-new
		# Picamera2(camera_index) independently of the service cache.  Two concurrent
		# libcamera handles on the same hardware corrupt both, leaving the service
		# instance in a broken state so the next capture hangs indefinitely.
		#
		# run_autofocus_calibration() acquires the per-camera lock, reuses the cached
		# instance, and leaves the camera stopped-but-not-closed for the next request.
		backend = get_backend()
		result = backend.run_autofocus_calibration(request.camera_index, img_size)

		if result["success"]:
			# Save calibration to registry
			registry = CameraRegistry()
			hw_id, _ = registry.get_camera_hardware_id(request.camera_index)

			if hw_id:
				registry.register_camera(request.camera_index)
				calibration_data = {
					"camera_index": request.camera_index,
					"calibrated_at": datetime.now(timezone.utc).isoformat(),
					"focus": result,
					"white_balance": {},
					"exposure": {},
				}
				registry.update_calibration(hw_id, calibration_data)
				logger.info(
					f"Saved autofocus calibration for {hw_id}: "
					f"lens_position={result['lens_position']}"
				)

		return CalibrationResponse(
			success=result["success"],
			lens_position=result.get("lens_position"),
			distance_meters=result.get("distance_meters"),
			af_time=result.get("af_time"),
		)
	except Exception as e:
		logger.exception(f"Autofocus calibration failed: {e}")
		return CalibrationResponse(success=False, error=str(e))


@router.post("/calibrate/white-balance", response_model=WhiteBalanceCalibrationResponse)
def calibrate_white_balance(
	request: WhiteBalanceCalibrationRequest,
	current_user: User = Depends(allow_contributor)
):
	"""
	Calibrate white balance for consistent color reproduction.
	
	For best results, place a neutral gray card or white paper in the frame
	before running calibration. The camera will run AWB until it converges,
	then save the gains for future captures.
	"""
	try:
		from capture.camera_registry import CameraRegistry
		from capture.service import get_backend
	except ImportError as e:
		return WhiteBalanceCalibrationResponse(success=False, error=f"Calibration system not available: {e}")

	try:
		backend = get_backend()
		if not backend.get_capabilities().get("autofocus_calibration", False):
			raise HTTPException(
				status_code=501,
				detail=f"{backend.get_backend_name()} backend does not support white balance calibration",
			)

		# Route WB calibration through the backend's cached Picamera2 instance -
		# same reason as autofocus: calibration.py would open a second handle and
		# corrupt the service's cached one.
		backend = get_backend()
		result = backend.run_white_balance_calibration(
			request.camera_index,
			stabilization_frames=request.stabilization_frames,
		)

		if result["success"]:
			registry = CameraRegistry()
			hw_id, _ = registry.get_camera_hardware_id(request.camera_index)

			if hw_id:
				registry.register_camera(request.camera_index)
				# Merge into existing calibration data so focus entry is preserved
				existing = registry.cameras.get("cameras", {}).get(hw_id, {}).get("calibration", {})
				calibration_data = {
					**existing,
					"camera_index": request.camera_index,
					"calibrated_at": datetime.now(timezone.utc).isoformat(),
					"white_balance": result,
				}
				registry.update_calibration(hw_id, calibration_data)
				logger.info(
					f"Saved WB calibration for {hw_id}: gains={result['awb_gains']}"
				)

		return WhiteBalanceCalibrationResponse(
			success=result["success"],
			awb_gains=result.get("awb_gains"),
			colour_temperature=result.get("colour_temperature"),
			converged=result.get("converged"),
		)
	except Exception as e:
		logger.exception(f"White balance calibration failed: {e}")
		return WhiteBalanceCalibrationResponse(success=False, error=str(e))


@router.post("/calibrate/white-balance/manual", response_model=WhiteBalanceCalibrationResponse)
def commit_manual_white_balance(
	request: WhiteBalanceManualRequest,
	current_user: User = Depends(allow_contributor)
):
	"""
	Commit manually-sampled AWB gains to the camera registry.

	Called after the user clicks on a neutral area in the live preview.
	No camera capture is performed - the supplied gains are validated and
	saved directly to the registry, the same way as after AWB convergence.
	"""
	if len(request.awb_gains) < 2:
		return WhiteBalanceCalibrationResponse(success=False, error="awb_gains must be [red, blue]")

	gains = [float(request.awb_gains[0]), float(request.awb_gains[1])]
	if not all(0.1 <= g <= 8.0 for g in gains):
		return WhiteBalanceCalibrationResponse(
			success=False,
			error=f"gains out of range [0.1, 8.0]: {gains}"
		)

	try:
		from capture.camera_registry import CameraRegistry

		registry = CameraRegistry()
		hw_id, _ = registry.get_camera_hardware_id(request.camera_index)

		if hw_id:
			registry.register_camera(request.camera_index)
			existing = registry.cameras.get("cameras", {}).get(hw_id, {}).get("calibration", {})
			wb_result = {
				"success": True,
				"awb_gains": gains,
				"colour_temperature": None,
				"converged": True,
				"source": "manual_sample",
			}
			calibration_data = {
				**existing,
				"camera_index": request.camera_index,
				"calibrated_at": datetime.now(timezone.utc).isoformat(),
				"white_balance": wb_result,
			}
			registry.update_calibration(hw_id, calibration_data)
			logger.info(f"Saved manual WB for {hw_id}: gains={gains}")

		return WhiteBalanceCalibrationResponse(success=True, awb_gains=gains)
	except Exception as e:
		logger.exception(f"Manual WB commit failed: {e}")
		return WhiteBalanceCalibrationResponse(success=False, error=str(e))


@router.get("/dslr/{camera_index}/settings", response_model=DSLRSettingsResponse)
def get_dslr_settings(
	camera_index: int,
	current_user: User = Depends(allow_read_only),
):
	"""
	Read current DSLR settings (ISO, shutter speed, aperture, format) from the
	PTP session for the given camera.

	Returns HTTP 501 when the active backend does not support DSLR settings
	(e.g. picamera2 or subprocess).
	"""
	from capture.service import get_backend
	backend = get_backend()
	caps = backend.get_capabilities()
	if not caps.get("dslr_settings"):
		raise HTTPException(
			status_code=501,
			detail="Active camera backend does not support DSLR settings.",
		)
	try:
		raw = backend.get_dslr_settings(camera_index)
		return DSLRSettingsResponse(**raw)
	except RuntimeError as e:
		raise HTTPException(status_code=502, detail=str(e))


@router.put("/dslr/{camera_index}/settings", response_model=DSLRSettingsResponse)
def apply_dslr_settings(
	camera_index: int,
	request: DSLRSettingsUpdate,
	current_user: User = Depends(allow_contributor),
):
	"""
	Apply DSLR settings (ISO, shutter speed, aperture, image format) to the
	given camera via PTP.  Only fields present in the request body are applied;
	omitted fields are left at their current camera value.

	Returns HTTP 501 when the active backend does not support DSLR settings.
	"""
	from capture.service import get_backend
	backend = get_backend()
	caps = backend.get_capabilities()
	if not caps.get("dslr_settings"):
		raise HTTPException(
			status_code=501,
			detail="Active camera backend does not support DSLR settings.",
		)
	# Validate image_format if provided
	if request.image_format is not None and request.image_format not in ("JPEG", "RAW", "RAW+JPEG"):
		raise HTTPException(
			status_code=422,
			detail=f"image_format must be one of JPEG, RAW, RAW+JPEG; got {request.image_format!r}",
		)
	try:
		updated = backend.apply_dslr_settings(
			camera_index,
			request.model_dump(exclude_none=True),
		)
		return DSLRSettingsResponse(**updated)
	except RuntimeError as e:
		raise HTTPException(status_code=502, detail=str(e))


class OrientationRequest(BaseModel):
	"""Request body for setting a camera body's saved rotation (NEH-71)."""
	orientation: Literal[0, 90, 180, 270]
	# Hardware id the client resolved for this index (from GET /devices or
	# POST /rescan). Checked against the index's current identity so a
	# rescan racing this request can't save the value onto the wrong body.
	hardware_id: str


@router.put("/{camera_index}/orientation", response_model=DeviceInfo)
def set_camera_orientation(
	camera_index: int,
	request: OrientationRequest,
	current_user: User = Depends(allow_contributor),
):
	"""
	Persist the clockwise rotation to apply for the camera body at this index.

	Saved to the registry keyed by hardware id (NEH-71), not by index, so it
	survives reopening the app and rebooting the Pi - unlike the per-capture
	rotate_deg fields, which are never persisted. Hardware ids are stable
	across re-enumeration (NEH-129) while indices are not.

	The request body carries the hardware id the client resolved for this
	index. A rescan can put a different body on the same index between that
	read and this write; if the index's current identity does not match the
	one in the request, this returns 409 instead of silently saving the new
	value under someone else's body (R30-4). The client should reload the
	device list and try again.
	"""
	try:
		from capture.camera_registry import CameraRegistry
		from capture.service import get_backend
	except ImportError as e:
		raise HTTPException(status_code=503, detail=f"Capture system not available: {e}")

	registry = CameraRegistry()
	hw_id, info = registry.get_camera_hardware_id(camera_index)

	if hw_id is None:
		raise HTTPException(status_code=404, detail=f"Camera {camera_index} is not connected")

	if hw_id != request.hardware_id:
		raise HTTPException(
			status_code=409,
			detail=(
				f"Camera {camera_index} is now {hw_id}, not {request.hardware_id}; "
				"reload the device list and try again"
			),
		)

	if registry.get_camera_by_id(hw_id) is None:
		# A body that was never calibrated must still be able to hold an
		# orientation. Register it under the identity just checked above -
		# never re-resolve it, per the same guard as the 409 check.
		registry.register_resolved(hw_id, info, camera_index)

	registry.update_orientation(hw_id, request.orientation)

	try:
		backend = get_backend()
		raw_devices = backend.list_devices()
	except Exception as exc:
		logger.exception(f"Camera enumeration failed after orientation update: {exc}")
		raise HTTPException(status_code=503, detail=f"Camera enumeration failed: {exc}")

	for device_info in _device_infos(raw_devices, registry):
		if device_info.index == camera_index:
			return device_info

	raise HTTPException(status_code=404, detail=f"Camera {camera_index} is not connected")


@router.post("/", response_model=CameraSettingsRead)
def create_camera_settings(
	payload: CameraSettingsCreate,
	current_user: User = Depends(allow_contributor),
	db: Session = Depends(get_db_dependency)
):
	if not db.query(RecordImage).filter(RecordImage.id == payload.record_image_id).first():
		raise HTTPException(status_code=404, detail="Record not found")

	try:
		cs = CameraSettings(**payload.dict())
		db.add(cs)
		db.commit()
		db.refresh(cs)
	except IntegrityError as e:
		db.rollback()
		raise integrity_conflict(e)
	return CameraSettingsRead.model_validate(cs)


@router.get("/", response_model=List[CameraSettingsRead])
def list_camera_settings(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency)
):
	items = db.query(CameraSettings).offset(skip).limit(limit).all()
	return [CameraSettingsRead.model_validate(i) for i in items]


@router.get("/{id}", response_model=CameraSettingsRead)
def get_camera_settings(
	id: int,
	current_user: User = Depends(allow_read_only),
	db: Session = Depends(get_db_dependency)
):
	cs = db.query(CameraSettings).filter(CameraSettings.id == id).first()
	if not cs:
		raise HTTPException(status_code=404, detail="Camera settings not found")
	return CameraSettingsRead.model_validate(cs)


@router.put("/settings/{id}", response_model=CameraSettingsRead)
def update_camera_settings(
	id: int,
	payload: CameraSettingsUpdate,
	current_user: User = Depends(allow_contributor),
	db: Session = Depends(get_db_dependency)
):
	"""Update camera settings by ID."""
	cs = db.query(CameraSettings).filter(CameraSettings.id == id).first()
	if not cs:
		raise HTTPException(status_code=404, detail="Camera settings not found")
	
	for field, value in payload.model_dump(exclude_unset=True).items():
		setattr(cs, field, value)
	
	db.add(cs)
	db.commit()
	db.refresh(cs)
	return CameraSettingsRead.model_validate(cs)


@router.delete("/settings/{id}")
def delete_camera_settings(
	id: int,
	current_user: User = Depends(allow_contributor),
	db: Session = Depends(get_db_dependency)
):
	"""Delete camera settings by ID."""
	cs = db.query(CameraSettings).filter(CameraSettings.id == id).first()
	if not cs:
		raise HTTPException(status_code=404, detail="Camera settings not found")
	
	db.delete(cs)
	db.commit()
	return {"detail": "Camera settings deleted"}
