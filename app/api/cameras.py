from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.models.record import Record, RecordImage
from typing import List, Optional
from pydantic import BaseModel
import logging

from app.api.deps import get_db_dependency
from app.api.auth import get_current_user, RoleChecker
from app.models.camera import CameraSettings
from app.models.user import User
from app.schemas.camera import CameraSettingsCreate, CameraSettingsRead, CameraSettingsUpdate
from app.core.thumbnail import generate_thumbnail

router = APIRouter()
logger = logging.getLogger(__name__)

allow_contributor = RoleChecker(["admin", "operator"])
allow_read_only = RoleChecker(["admin", "operator", "reviewer"])


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
	# Capabilities
	has_aperture_control: bool = False
	supports_zoom: bool = False  # True when ScalerCrop is available (picamera2 backend)


class CaptureRequest(BaseModel):
	"""Request body for capture endpoint."""
	project_name: str
	camera_index: int = 0
	resolution: str = "medium"  # low, medium, high
	include_resolution_in_filename: bool = False
	record_id: Optional[int] = None  # Link to existing record, or create new if None
	record_title: Optional[str] = None  # Used if creating new record
	collection_id: Optional[int] = None  # Collection to link the record to


class DualCaptureRequest(BaseModel):
	"""Request body for dual capture endpoint."""
	project_name: str
	resolution: str = "medium"
	include_resolution_in_filename: bool = False
	stagger_ms: int = 20
	record_id: Optional[int] = None  # Link to existing record, or create new if None
	record_title: Optional[str] = None  # Used if creating new record
	sequence: Optional[int] = None  # Page number/order
	left_camera_index: int = 0  # Which camera index maps to the left page (0 or 1)
	collection_id: Optional[int] = None  # Collection to link the record to


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

		if camera_data:
			focus_cal = camera_data.get("calibration", {}).get("focus", {})
			calibrated = bool(focus_cal.get("success"))
			machine_id = camera_data.get("machine_id")
			label = camera_data.get("label")
			lens_position = focus_cal.get("lens_position")
			awb_raw = camera_data.get("calibration", {}).get("white_balance", {}).get("awb_gains")
			if awb_raw:
				awb_gains = list(awb_raw)

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
			has_aperture_control=dev.get("has_aperture_control", False),
			supports_zoom=dev.get("supports_zoom", False),
		))

	return devices


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
	current_user: User = Depends(allow_read_only),
):
	"""
	Capture a low-resolution preview frame and return it as JPEG.

	Called by the frontend every PREVIEW_INTERVAL_MS milliseconds for the
	live preview view.  Uses a lightweight config (1280×720, no AF, no denoise)
	so frames are returned quickly without interfering with full captures.

	Returns 404 when the requested camera is not connected.
	"""
	from fastapi.responses import Response

	try:
		from capture.service import capture_preview_frame
	except ImportError as e:
		raise HTTPException(status_code=503, detail=f"Capture system not available: {e}")

	try:
		jpeg_bytes = capture_preview_frame(camera_index)
		return Response(content=jpeg_bytes, media_type="image/jpeg")
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
	lens_position: float  # Dioptres: 0 = infinity, 10 ≈ 10 cm


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
	analogue_gain: Optional[float] = None     # Manual gain (ISO 100 ≈ 1.0)
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
		camera_config = CameraConfig(**config_dict)
		
		# Capture image and get manifest IDs
		# Look up collection name so images go to project/collection/images/main/
		collection_name = None
		if request.collection_id:
			from app.models.collection import Collection
			col = db.query(Collection).filter(Collection.id == request.collection_id).first()
			collection_name = col.name if col else None

		output_path, capture_id, pair_id = single_capture_image(
			project_name=request.project_name,
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
		
		# Get or find project by name
		project = db.query(Project).filter(Project.name == request.project_name).first()
		project_id = project.id if project else None
		
		# Records can have either project_id OR collection_id, not both (DB constraint).
		# When a collection is provided, the project association is implicit through it.
		effective_project_id = None if request.collection_id else project_id
		
		# Get or create Record
		if request.record_id:
			# Link to existing record
			record = db.query(Record).filter(Record.id == request.record_id).first()
			if not record:
				raise HTTPException(status_code=404, detail=f"Record {request.record_id} not found")
		else:
			# Create new record for this capture
			record = Record(
				title=request.record_title or f"{request.project_name} - {file_path.stem}",
				description=f"Captured at {request.resolution} resolution",
				object_typology="document",
				project_id=effective_project_id,
				collection_id=request.collection_id,
				created_by=current_user.username,
			)
			db.add(record)
			db.flush()  # Get the ID
		
		# Generate thumbnail alongside the captured images
		thumbnail_path = None
		try:
			thumbnails_dir = file_path.parent.parent / "thumbnails"
			thumbnail_path = generate_thumbnail(file_path, thumbnails_dir)
		except Exception as e:
			logger.warning(f"Failed to generate thumbnail for {file_path.name}: {e}")

		# Create RecordImage with capture linkage
		img = RecordImage(
			record_id=record.id,
			filename=file_path.name,
			file_path=str(output_path),
			thumbnail_path=thumbnail_path,
			file_size=file_size,
			format="jpg",
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
		
		cam0_config = CameraConfig(**config0_dict)
		cam1_config = CameraConfig(**config1_dict)
		
		# Capture both images and get manifest IDs
		# Look up collection name so images go to project/collection/images/main/
		collection_name = None
		if request.collection_id:
			from app.models.collection import Collection
			col = db.query(Collection).filter(Collection.id == request.collection_id).first()
			collection_name = col.name if col else None

		path0, path1, capture_id, pair_id = dual_capture_image(
			project_name=request.project_name,
			cam1_config=cam0_config,
			cam2_config=cam1_config,
			check_camera=False,
			include_resolution=request.include_resolution_in_filename,
			stagger_ms=request.stagger_ms,
			collection_name=collection_name
		)
		
		# Get project
		project = db.query(Project).filter(Project.name == request.project_name).first()
		project_id = project.id if project else None
		
		# Records can have either project_id OR collection_id, not both (DB constraint).
		# When a collection is provided, the project association is implicit through it.
		effective_project_id = None if request.collection_id else project_id
		
		# Get or create Record
		if request.record_id:
			# Link to existing record (adding new pages to multi-page document)
			record = db.query(Record).filter(Record.id == request.record_id).first()
			if not record:
				raise HTTPException(status_code=404, detail=f"Record {request.record_id} not found")
		else:
			# Create new record for this dual capture
			record = Record(
				title=request.record_title or f"{request.project_name} - Dual capture",
				description=f"Dual camera capture at {request.resolution} resolution",
				object_typology="book",  # Default to book for dual captures
				project_id=effective_project_id,
				collection_id=request.collection_id,
				created_by=current_user.username,
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
			thumbnail_path = None
			try:
				thumbnails_dir = file_path.parent.parent / "thumbnails"
				thumbnail_path = generate_thumbnail(file_path, thumbnails_dir)
			except Exception as e:
				logger.warning(f"Failed to generate thumbnail for {file_path.name}: {e}")

			# Create RecordImage with capture linkage
			img = RecordImage(
				record_id=record.id,
				filename=file_path.name,
				file_path=str(file_path_str),
				thumbnail_path=thumbnail_path,
				file_size=file_size,
				format="jpg",
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

		# Route WB calibration through the backend's cached Picamera2 instance —
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
	No camera capture is performed — the supplied gains are validated and
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
	except IntegrityError:
		db.rollback()
		raise HTTPException(status_code=409, detail="Camera settings already exist for this record")
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
