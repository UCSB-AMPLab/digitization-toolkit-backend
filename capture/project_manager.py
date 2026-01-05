from pathlib import Path
import logging
import sys
import json
from typing import Optional, Tuple

backend_dir = Path(__file__).parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from .utils import setup_rotating_logger
from .manifestHandler import ProjectInfo, generate_manifest_project, append_manifest_record
from .camera import IMG_SIZES
from .camera_registry import CameraRegistry

from app.core.config import settings

PROJECTS_ROOT = settings.projects_dir
LOG_FILE = settings.log_dir / "project_manager.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

subprocess_logger = setup_rotating_logger(
    log_file=str(LOG_FILE),
    logger_name="project_manager"
)


def load_calibration_profile(camera_index: int, calibration_dir: Path = None) -> dict:
    """
    Load calibration profile for a camera.
    
    Args:
        camera_index: Camera index (0 or 1)
        calibration_dir: Directory containing calibration files (defaults to backend root)
        
    Returns:
        Calibration data dict, or empty dict if not found
    """
    if calibration_dir is None:
        calibration_dir = Path(__file__).parent.parent
    
    profile_path = calibration_dir / f"calibration_camera{camera_index}.json"
    
    if profile_path.exists():
        try:
            with open(profile_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            subprocess_logger.warning(f"Failed to load calibration for camera {camera_index}: {e}")
    
    return {}


def default_camera_config_from_registry(
    camera_index: int,
    resolution: str = "high",
    registry: Optional[CameraRegistry] = None
) -> Tuple[dict, Optional[str]]:
    """
    Generate camera configuration from registry data.
    
    Args:
        camera_index: Current camera index
        resolution: Resolution preset
        registry: CameraRegistry instance (creates new if None)
        
    Returns:
        Tuple of (config_dict, hardware_id)
    """
    if registry is None:
        registry = CameraRegistry()
    
    # Get hardware ID and calibration for current camera
    hw_id, camera_data = registry.get_camera_by_index(camera_index)
    
    # Base configuration
    config = {
        "camera_index": camera_index,
        "img_size": IMG_SIZES.get(resolution, IMG_SIZES["high"]),
        "vflip": False,
        "hflip": False,
        "awb": "indoor",
        "timeout": 50,
        "autofocus_on_capture": True,
        "buffer_count": 2,
        "thumbnail": False,
        "nopreview": True,
        "quality": 93,
        "zsl": False,
        "encoding": "jpg",
        "raw": False
    }
    
    # Apply calibration if available
    if camera_data and camera_data.get("calibration", {}).get("focus", {}).get("success"):
        lens_position = camera_data["calibration"]["focus"]["lens_position"]
        config["autofocus_on_capture"] = False
        config["lens_position"] = lens_position
        
        subprocess_logger.info(
            f"Applied calibration for camera {camera_index} ({hw_id}): "
            f"lens_position={lens_position:.2f} dioptres"
        )
    elif hw_id:
        subprocess_logger.warning(
            f"Camera {camera_index} ({hw_id}) has no calibration data. "
            f"Using autofocus (slow). Run calibration for better performance."
        )
    
    return config, hw_id


def project_init(
    project_name: str,
    default_resolution: str = "high"
) -> Path:
    """
    Initialize a new project directory structure with camera configurations.
    
    Cameras are identified by hardware ID from the global registry.
    Configuration is inherited from registry calibration data.
    
    Args:
        project_name: Name of the project
        default_resolution: Default resolution preset for cameras
        
    Returns:
        Path to the created project directory
    """
    project_path = Path(PROJECTS_ROOT, project_name)
    images_main = Path(project_path, "images", "main")
    images_temp = Path(project_path, "images", "temp")
    images_trash = Path(project_path, "images", "trash")
    packages_dir = Path(project_path, "packages")
    
    for path in [images_main, images_temp, images_trash, packages_dir]:
        path.mkdir(parents=True, exist_ok=True)
    
    subprocess_logger.info(f"Created project directory structure: {project_path}")
    
    # Get camera configurations from registry
    registry = CameraRegistry()
    
    # Ensure cameras are detected and registered
    detected = registry.detect_cameras()
    for idx, (hw_id, info) in detected.items():
        if hw_id not in registry.cameras["cameras"]:
            registry.register_camera(idx)
    
    cam0_config, cam0_hw_id = default_camera_config_from_registry(0, default_resolution, registry)
    cam1_config, cam1_hw_id = default_camera_config_from_registry(1, default_resolution, registry)
    
    # Get camera hardware info
    cam0_info = registry.get_camera_by_id(cam0_hw_id) if cam0_hw_id else None
    cam1_info = registry.get_camera_by_id(cam1_hw_id) if cam1_hw_id else None
    
    # Generate and save project manifest
    project_info = generate_manifest_project(
        project_name=project_name,
        paths={
            "project_root": str(project_path),
            "images_main": str(images_main),
            "images_temp": str(images_temp),
            "images_trash": str(images_trash),
            "packages": str(packages_dir),
        },
        default_camera_config={
            "resolution_preset": default_resolution,
            "cameras": {
                "left": {
                    "hardware_id": cam0_hw_id,
                    "model": cam0_info.get("model") if cam0_info else None,
                    "serial": cam0_info.get("serial") if cam0_info else None,
                    "current_index": 0,
                    "config": cam0_config
                },
                "right": {
                    "hardware_id": cam1_hw_id,
                    "model": cam1_info.get("model") if cam1_info else None,
                    "serial": cam1_info.get("serial") if cam1_info else None,
                    "current_index": 1,
                    "config": cam1_config
                }
            }
        }
    )
    
    append_manifest_record(project_path, project_info, record_type="project")
    subprocess_logger.info(f"Project manifest created for: {project_name}")
    
    return project_path