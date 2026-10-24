from pathlib import Path
import sys
import json
from typing import Optional, Tuple
import re
import unicodedata

backend_dir = Path(__file__).parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from .utils import setup_rotating_logger
from .manifestHandler import ProjectInfo, generate_manifest_project, append_manifest_record
from .camera import IMG_SIZES
from .camera_registry import CameraRegistry

from app.core.config import settings

LOG_FILE = settings.log_dir / "project_manager.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

subprocess_logger = setup_rotating_logger(
    log_file=str(LOG_FILE),
    logger_name="project_manager"
)

def secure_project_filename(project_name):
    project_name = unicodedata.normalize('NFKD', project_name).encode('ascii', 'ignore').decode('ascii')
    project_name = re.sub(r'[^a-zA-Z0-9._-]', '_', project_name)
    return project_name.lstrip('.').lower()


def _projects_root() -> Path:
    """Resolve the active projects root at call time, refusing one that cannot take writes.

    Read from settings on every call (rather than a module-level constant
    captured at import) so a runtime storage-drive switch - POST
    /system/storage/activate flips the storage override in
    app.core.config.Settings.projects_dir - takes effect immediately for new
    captures, keeping writes and reads on the same disk.

    Every path into the projects tree is built here, and the callers are the
    ones that create, move, rename or delete files in it, so the root comes
    from writable_projects_dir: an override naming a missing directory, or one
    that is not on a mounted drive, raises StorageUnavailableError instead of
    letting a later mkdir(parents=True) build the tree on the SD card.
    """
    from app.core.storage_override import writable_projects_dir

    return writable_projects_dir(settings)


def project_capture_root(project_name: str) -> Path:
    """Project-level directory holding the manifest and all image subdirs.
    Args:
        project_name: Name of the project"""
    return Path(_projects_root(), secure_project_filename(project_name))


def volume_root(stem: str) -> Path:
    """On-disk root of a volume that has a file name stem: `<projects root>/<stem>`.

    The volume's directory sits directly under the projects root, with no
    project level, so renaming a project, moving the volume to another
    project or re-parenting it moves no file. It holds the four directories
    of app.core.paths.VOLUME_SUBDIRS: `captures/`, `previews/`,
    `thumbnails/` and `metadata/` (the volume's own manifest.jsonl).

    Raises ValueError for a stem that is not a lowercase slug within the
    model's length cap (app.models.collection), so a stored value can never
    name a path outside the root.
    """
    import re

    from app.models.collection import FILENAME_STEM_MAX_LENGTH, FILENAME_STEM_PATTERN

    if (not isinstance(stem, str) or len(stem) > FILENAME_STEM_MAX_LENGTH
            or not re.fullmatch(FILENAME_STEM_PATTERN, stem)):
        raise ValueError(f"Not a volume file name stem: {stem!r}")
    return Path(_projects_root(), stem)


def ensure_volume_dirs(stem: str) -> Path:
    """Create the volume's four directories if they are missing; return its root."""
    from app.core.paths import VOLUME_SUBDIRS

    root = volume_root(stem)
    for name in VOLUME_SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def collection_capture_root(project_name: str, collection_name: str) -> Path:
    """On-disk root of a volume without a stem: `<project dir>/<secure collection name>`.

    The name-based layout every volume had before stems. Two volumes of one
    name share the directory, and a nested volume's directory sits beside its
    parent's. A volume keeps it until it is given a stem and the startup
    migration (app.core.volume_layout) moves its files.
    """
    return project_capture_root(project_name) / secure_project_filename(collection_name)


def image_output_dir(project_name: str, collection_name: Optional[str] = None,
                     stem: Optional[str] = None) -> Path:
    """Directory where captured images are written.

    `<projects root>/<stem>/captures` for a volume with a stem,
    `<project dir>/<collection name>/images/main` for a volume without one,
    and `<project dir>/images/main` for a project-level capture (neither).
    """
    from app.core.paths import CAPTURES_DIR

    if stem:
        return volume_root(stem) / CAPTURES_DIR
    if collection_name:
        root = collection_capture_root(project_name, collection_name)
    else:
        root = project_capture_root(project_name)
    return root / "images" / "main"


def capture_manifest_root(project_name: str, stem: Optional[str] = None) -> Path:
    """The directory whose `metadata/manifest.jsonl` takes a capture's record.

    The volume's root for a volume with a stem, so each such volume has its
    own manifest; the project's directory otherwise, as before stems. The
    capture record's paths are relative to it.
    """
    if stem:
        return volume_root(stem)
    return project_capture_root(project_name)

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

    # Apply the saved rotation for this body, if one was ever set (NEH-71).
    # None (never set) leaves rotate_deg out of the dict entirely, so
    # CameraConfig's own default (0) and _apply_rotation's `if rotate_deg:`
    # behave exactly as before this key existed.
    # Only the four supported angles count; anything else in the file (a
    # hand edit, a bool, 45) is treated as unset rather than handed to the
    # capture path, where it would be rejected on every request.
    orientation = (camera_data or {}).get("orientation")
    if CameraRegistry.is_valid_orientation(orientation):
        config["rotate_deg"] = orientation
        subprocess_logger.info(
            f"Applied saved orientation for camera {camera_index} ({hw_id}): "
            f"rotate_deg={orientation}"
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
        
    project_path = project_capture_root(project_name)
    packages_dir = Path(project_path, "packages")
    
    for path in [packages_dir]:
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