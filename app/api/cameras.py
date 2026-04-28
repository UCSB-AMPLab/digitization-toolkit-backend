from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.models.record import Record, RecordImage
from typing import List, Optional
from pydantic import BaseModel
import logging

from app.api.deps import get_db_dependency
from app.api.auth import get_current_user
from app.models.camera import CameraSettings
from app.models.user import User
from app.schemas.camera import CameraSettingsCreate, CameraSettingsRead, CameraSettingsUpdate
from app.core.thumbnail import generate_thumbnail

#Catalina agregó esto para intentar resolver el api para el streaming de las cámaras, pero no se llegó a implementar completamente. Queda como referencia para futuras implementaciones de esta funcionalidad.
from fastapi.responses import Response
import os

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
	record_id: Optional[int] = None  # Link to existing record, or create new if None
	record_title: Optional[str] = None  # Used if creating new record


class DualCaptureRequest(BaseModel):
	"""Request body for dual capture endpoint."""
	project_name: str
	resolution: str = "medium"
	include_resolution_in_filename: bool = False
	stagger_ms: int = 20
	record_id: Optional[int] = None  # Link to existing record, or create new if None
	record_title: Optional[str] = None  # Used if creating new record
	sequence: Optional[int] = None  # Page number/order


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
				calibrated=calibrated,
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
		output_path, capture_id, pair_id = single_capture_image(
			project_name=request.project_name,
			camera_config=camera_config,
			check_camera=False,  # Already checked
			include_resolution=request.include_resolution_in_filename
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
				project_id=project_id,
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
	current_user: User = Depends(get_current_user),
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
		path0, path1, capture_id, pair_id = dual_capture_image(
			project_name=request.project_name,
			cam1_config=cam0_config,
			cam2_config=cam1_config,
			check_camera=False,
			include_resolution=request.include_resolution_in_filename,
			stagger_ms=request.stagger_ms
		)
		
		# Get project
		project = db.query(Project).filter(Project.name == request.project_name).first()
		project_id = project.id if project else None
		
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
				project_id=project_id,
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
		img0 = create_image_record(str(path0), 0, "left")
		img1 = create_image_record(str(path1), 1, "right")
		
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


#está es una ruta temporal para el preview de las cámaras, no es parte de la API oficial ni está documentada, solo es para uso interno del frontend mientras se desarrolla la funcionalidad de live preview. Queda como referencia para futuras implementaciones de esta funcionalidad.

@router.get("/preview/{camera_index}")
def get_camera_preview(
    camera_index: int,
    current_user: User = Depends(get_current_user)
):
    """
    Captura un frame de baja resolución y lo devuelve como JPEG.
    
    - Usa resolución 'low' (2312x1736) para máxima velocidad
    - NO escribe al manifest ni a la base de datos
    - Borra el archivo temporal inmediatamente después de leerlo
    - Usado por el frontend para polling del live preview (cada ~2s)
    
    Para cambiar la resolución del preview, modifica "low" por "medium".
    Para cambiar la frecuencia, modificar PREVIEW_INTERVAL_MS en LiveViewport.svelte.
    """
    try:
        from capture.service import capture_image, is_camera_connected
        from capture.camera import CameraConfig
        from capture.project_manager import default_camera_config_from_registry
    except ImportError as e:
        raise HTTPException(status_code=503, detail=f"Camera system not available: {e}")

    # Verificar que la cámara está conectada
    if not is_camera_connected(camera_index):
        raise HTTPException(
            status_code=404,
            detail=f"Camera {camera_index} not connected"
        )

    output_path = None
    try:
        # Obtener config con calibración si existe
        # Usamos "low" para máxima velocidad en el preview
        config_dict, _ = default_camera_config_from_registry(camera_index, "low")
        config = CameraConfig(**config_dict)

        # capture_image devuelve (path, metadata) — NO escribe al manifest
        # Usa "_preview" como nombre de proyecto para separar estos archivos
        result = capture_image(
            project_name="_preview",
            camera_config=config,
            check_camera=False,   # ya verificamos arriba
            include_resolution=False,
        )

        # capture_image puede devolver (path, metadata) o solo path según el backend
        if isinstance(result, tuple):
            output_path = result[0]
        else:
            output_path = result

        # Leer el archivo a memoria
        with open(str(output_path), "rb") as f:
            image_bytes = f.read()

        return Response(
            content=image_bytes,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Preview failed for camera {camera_index}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        # Borrar el archivo temporal siempre, incluso si hubo error
        if output_path:
            try:
                os.remove(str(output_path))
            except Exception:
                pass  # Si falla el borrado, no es crítico