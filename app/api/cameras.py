from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.models.document import DocumentImage
from typing import List, Optional
from pydantic import BaseModel
import logging

from app.api.deps import get_db_dependency
from app.api.auth import get_current_user
from app.models.camera import CameraSettings
from app.models.user import User
from app.schemas.camera import CameraSettingsCreate, CameraSettingsRead, CameraSettingsUpdate

router = APIRouter()
logger = logging.getLogger(__name__)


class DeviceInfo(BaseModel):
	"""Information about a detected camera device."""
	hardware_id: str
	model: str
	index: int
	location: Optional[str] = None
	machine_id: Optional[str] = None
	label: Optional[str] = None
	calibrated: bool = False


class CaptureRequest(BaseModel):
	"""Request body for capture endpoint."""
	project_name: str
	camera_index: int = 0
	resolution: str = "medium"  # low, medium, high
	include_resolution_in_filename: bool = False


class DualCaptureRequest(BaseModel):
	"""Request body for dual capture endpoint."""
	project_name: str
	resolution: str = "medium"
	include_resolution_in_filename: bool = False
	stagger_ms: int = 20


class CaptureResponse(BaseModel):
	"""Response from capture endpoints."""
	success: bool
	file_path: Optional[str] = None
	file_paths: Optional[List[str]] = None
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
def list_camera_devices():
	"""
	Return available camera devices detected via libcamera/picamera2.
	
	Returns hardware IDs, models, and calibration status for each camera.
	On non-Pi systems or if camera libraries aren't available, returns empty list.
	"""
	registry = _get_camera_registry()
	if registry is None:
		return []
	
	try:
		detected = registry.detect_cameras()
		devices = []
		
		for idx, (hw_id, info) in detected.items():
			# Check if camera is registered and has calibration
			camera_data = registry.get_camera_by_id(hw_id)
			calibrated = False
			machine_id = None
			label = None
			
			if camera_data:
				calibrated = bool(
					camera_data.get("calibration", {}).get("focus", {}).get("success")
				)
				machine_id = camera_data.get("machine_id")
				label = camera_data.get("label")
			
			devices.append(DeviceInfo(
				hardware_id=hw_id,
				model=info.get("model", "unknown"),
				index=idx,
				location=info.get("location"),
				machine_id=machine_id,
				label=label,
				calibrated=calibrated
			))
		
		return devices
	except Exception as e:
		logger.error(f"Failed to detect cameras: {e}")
		return []


@router.post("/capture", response_model=CaptureResponse)
def trigger_capture(
	request: CaptureRequest,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""
	Trigger a single image capture on the specified camera.
	
	Requires a project to exist (use /projects/{id}/initialize first).
	Captures image locally to microSD, then creates database record with metadata.
	"""
	try:
		from capture.service import single_capture_image, is_camera_connected
		from capture.camera import CameraConfig, IMG_SIZES
		from capture.project_manager import default_camera_config_from_registry
		from PIL import Image
		from PIL.ExifTags import TAGS
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
		
		output_path = single_capture_image(
			project_name=request.project_name,
			camera_config=camera_config,
			check_camera=False,  # Already checked
			include_resolution=request.include_resolution_in_filename
		)
		
		# Extract image dimensions and EXIF data
		resolution_width = None
		resolution_height = None
		exif_dict = {}
		
		try:
			from PIL import Image
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
		
		# Get file size
		from pathlib import Path
		file_path = Path(output_path)
		file_size = file_path.stat().st_size if file_path.exists() else 0
		
		# Create database record for the captured image
		from app.models.document import DocumentImage, ExifData
		from app.models.camera import CameraSettings
		from app.models.project import Project
		
		# Get or find project by name
		project = db.query(Project).filter(Project.name == request.project_name).first()
		project_id = project.id if project else None
		
		# Create document record
		doc = DocumentImage(
			filename=file_path.name,
			title=f"{request.project_name} - Camera {request.camera_index}",
			description=f"Captured via API at {request.resolution} resolution",
			file_path=str(output_path),
			file_size=file_size,
			format="jpg",
			resolution_width=resolution_width,
			resolution_height=resolution_height,
			uploaded_by=current_user.username,
			project_id=project_id,
			object_typology="document",
		)
		
		db.add(doc)
		db.flush()  # Get the ID
		
		# Save camera settings
		cs = CameraSettings(
			document_image_id=doc.id,
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
				document_image_id=doc.id,
				raw_exif=str(exif_dict),
			)
			db.add(ex)
		
		db.commit()
		db.refresh(doc)
		
		logger.info(f"Created database record for captured image: {doc.id}")
		
		return CaptureResponse(
			success=True,
			file_path=str(output_path)
		)
	except Exception as e:
		logger.exception(f"Capture failed: {e}")
		db.rollback()
		return CaptureResponse(success=False, error=str(e))


@router.post("/capture/dual", response_model=CaptureResponse)
def trigger_dual_capture(
	request: DualCaptureRequest,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""
	Trigger simultaneous capture on both cameras (index 0 and 1).
	
	Used for book scanning where left and right pages are captured together.
	Both images stored locally; metadata saved to database.
	"""
	try:
		from capture.service import dual_capture_image, is_camera_connected
		from capture.camera import CameraConfig
		from capture.project_manager import default_camera_config_from_registry
		from PIL import Image
		from PIL.ExifTags import TAGS
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
		
		path0, path1 = dual_capture_image(
			project_name=request.project_name,
			cam1_config=cam0_config,
			cam2_config=cam1_config,
			check_camera=False,
			include_resolution=request.include_resolution_in_filename,
			stagger_ms=request.stagger_ms
		)
		
		from pathlib import Path
		from app.models.document import DocumentImage, ExifData
		from app.models.camera import CameraSettings
		from app.models.project import Project
		
		# Get project
		project = db.query(Project).filter(Project.name == request.project_name).first()
		project_id = project.id if project else None
		
		# Helper to process captured image
		def create_document_from_capture(file_path_str: str, camera_idx: int):
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
			
			# Create document record
			doc = DocumentImage(
				filename=file_path.name,
				title=f"{request.project_name} - Camera {camera_idx} (Dual)",
				description=f"Dual capture via API at {request.resolution} resolution",
				file_path=str(file_path_str),
				file_size=file_size,
				format="jpg",
				resolution_width=resolution_width,
				resolution_height=resolution_height,
				uploaded_by=current_user.username,
				project_id=project_id,
				object_typology="document",
			)
			
			db.add(doc)
			db.flush()
			
			# Camera settings
			cam_config = cam0_config if camera_idx == 0 else cam1_config
			cs = CameraSettings(
				document_image_id=doc.id,
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
					document_image_id=doc.id,
					raw_exif=str(exif_dict),
				)
				db.add(ex)
			
			return doc
		
		# Create records for both captures
		doc0 = create_document_from_capture(str(path0), 0)
		doc1 = create_document_from_capture(str(path1), 1)
		
		db.commit()
		
		logger.info(f"Created dual capture database records: {doc0.id}, {doc1.id}")
		
		return CaptureResponse(
			success=True,
			file_paths=[str(path0), str(path1)]
		)
	except Exception as e:
		logger.exception(f"Dual capture failed: {e}")
		db.rollback()
		return CaptureResponse(success=False, error=str(e))


@router.post("/calibrate", response_model=CalibrationResponse)
def calibrate_camera(
	request: CalibrationRequest,
	current_user: User = Depends(get_current_user)
):
	"""
	Run autofocus calibration on a camera to find optimal lens position.
	
	For fixed-distance setups (book scanning), this determines the best
	focus position which is then stored and reused for faster captures.
	"""
	try:
		from capture.calibration import CameraCalibration
		from capture.camera_registry import CameraRegistry
		from capture.camera import IMG_SIZES
	except ImportError as e:
		return CalibrationResponse(success=False, error=f"Calibration system not available: {e}")
	
	try:
		# Get resolution
		img_size = IMG_SIZES.get(request.resolution, IMG_SIZES["high"])
		
		# Run calibration
		cal = CameraCalibration(request.camera_index)
		result = cal.calibrate_focus(img_size=img_size, verbose=False)
		
		if result["success"]:
			# Save calibration to registry
			registry = CameraRegistry()
			hw_id, _ = registry.get_camera_hardware_id(request.camera_index)
			
			if hw_id:
				# Ensure camera is registered
				registry.register_camera(request.camera_index)
				# Update calibration data
				registry.update_calibration(hw_id, cal.calibration_data)
				logger.info(f"Saved calibration for {hw_id}: lens_position={result['lens_position']}")
		
		return CalibrationResponse(
			success=result["success"],
			lens_position=result.get("lens_position"),
			distance_meters=result.get("distance_meters"),
			af_time=result.get("af_time")
		)
	except Exception as e:
		logger.exception(f"Calibration failed: {e}")
		return CalibrationResponse(success=False, error=str(e))


@router.post("/calibrate/white-balance", response_model=WhiteBalanceCalibrationResponse)
def calibrate_white_balance(
	request: WhiteBalanceCalibrationRequest,
	current_user: User = Depends(get_current_user)
):
	"""
	Calibrate white balance for consistent color reproduction.
	
	For best results, place a neutral gray card or white paper in the frame
	before running calibration. The camera will run AWB until it converges,
	then save the gains for future captures.
	"""
	try:
		from capture.calibration import CameraCalibration
		from capture.camera_registry import CameraRegistry
		from capture.camera import IMG_SIZES
	except ImportError as e:
		return WhiteBalanceCalibrationResponse(success=False, error=f"Calibration system not available: {e}")
	
	try:
		img_size = IMG_SIZES.get(request.resolution, IMG_SIZES["high"])
		
		cal = CameraCalibration(request.camera_index)
		result = cal.calibrate_white_balance(
			img_size=img_size,
			stabilization_frames=request.stabilization_frames,
			verbose=False
		)
		
		if result["success"]:
			# Save calibration to registry
			registry = CameraRegistry()
			hw_id, _ = registry.get_camera_hardware_id(request.camera_index)
			
			if hw_id:
				registry.register_camera(request.camera_index)
				registry.update_calibration(hw_id, cal.calibration_data)
				logger.info(f"Saved WB calibration for {hw_id}: gains={result['awb_gains']}")
		
		return WhiteBalanceCalibrationResponse(
			success=result["success"],
			awb_gains=list(result["awb_gains"]) if result.get("awb_gains") else None,
			colour_temperature=result.get("colour_temperature"),
			converged=result.get("converged")
		)
	except Exception as e:
		logger.exception(f"White balance calibration failed: {e}")
		return WhiteBalanceCalibrationResponse(success=False, error=str(e))


@router.post("/", response_model=CameraSettingsRead)
def create_camera_settings(
	payload: CameraSettingsCreate,
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
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
	return CameraSettingsRead.model_validate(cs)


@router.get("/", response_model=List[CameraSettingsRead])
def list_camera_settings(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_dependency)
):
	items = db.query(CameraSettings).offset(skip).limit(limit).all()
	return [CameraSettingsRead.model_validate(i) for i in items]


@router.get("/{id}", response_model=CameraSettingsRead)
def get_camera_settings(
	id: int,
	current_user: User = Depends(get_current_user),
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
	current_user: User = Depends(get_current_user),
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
	current_user: User = Depends(get_current_user),
	db: Session = Depends(get_db_dependency)
):
	"""Delete camera settings by ID."""
	cs = db.query(CameraSettings).filter(CameraSettings.id == id).first()
	if not cs:
		raise HTTPException(status_code=404, detail="Camera settings not found")
	
	db.delete(cs)
	db.commit()
	return {"detail": "Camera settings deleted"}
