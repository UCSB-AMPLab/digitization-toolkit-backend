from pathlib import Path
import logging
import sys
import json

backend_dir = Path(__file__).parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from .utils import setup_rotating_logger
from .manifestHandler import ProjectInfo, generate_manifest_project, append_manifest_record
from .camera import IMG_SIZES

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


def default_camera_config(
    camera_index: int,
    resolution: str = "high",
    use_calibration: bool = True,
    calibration_dir: Path = None
) -> dict:
    """
    Generate default camera configuration dictionary with optional calibration data.
    
    Args:
        camera_index: Camera index (0 or 1)
        resolution: Resolution preset ("low", "medium", "high") or tuple
        use_calibration: Whether to load and apply calibration data
        calibration_dir: Directory containing calibration files
        
    Returns:
        Dictionary with camera configuration parameters
    """
    # Base configuration matching CameraConfig defaults
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
    
    # Apply calibration if available and requested
    if use_calibration:
        calibration = load_calibration_profile(camera_index, calibration_dir)
        
        if calibration.get("focus", {}).get("success"):
            # Use manual focus at calibrated position (much faster!)
            lens_position = calibration["focus"]["lens_position"]
            config["autofocus_on_capture"] = False
            config["lens_position"] = lens_position
            
            subprocess_logger.info(
                f"Applied calibration for camera {camera_index}: "
                f"lens_position={lens_position:.2f} dioptres"
            )
        
        # Future: Apply white balance calibration
        # if calibration.get("white_balance", {}).get("gains"):
        #     config["awb"] = "custom"
        #     config["awb_gains"] = calibration["white_balance"]["gains"]
    
    return config


def project_init(
    project_name: str,
    default_resolution: str = "high",
    use_calibration: bool = True
) -> Path:
    """
    Initialize a new project directory structure with default camera configurations.
    
    Args:
        project_name: Name of the project
        default_resolution: Default resolution preset for cameras
        use_calibration: Whether to use calibrated camera settings
        
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
    
    # Generate default camera configurations for dual camera setup
    cam0_config = default_camera_config(0, default_resolution, use_calibration)
    cam1_config = default_camera_config(1, default_resolution, use_calibration)
    
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
            "camera_0": cam0_config,
            "camera_1": cam1_config,
            "resolution_preset": default_resolution,
            "calibration_applied": use_calibration
        }
    )
    
    append_manifest_record(project_path, project_info, record_type="project")
    subprocess_logger.info(f"Project manifest created for: {project_name}")
    
    return project_path