import sys
from pathlib import Path
import time
from datetime import datetime, timezone
import concurrent.futures
from typing import Optional

backend_dir = Path(__file__).parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from .utils import setup_rotating_logger
from .camera import CameraConfig
from .manifestHandler import generate_manifest_record, append_manifest_record
from .backends import CameraBackend, RpicamBackend, Picamera2Backend

from app.core.config import settings

PROJECTS_ROOT = settings.projects_dir
LOG_FILE = settings.log_dir / "capture_service.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

subprocess_logger = setup_rotating_logger(
    log_file=str(LOG_FILE),
    logger_name="capture_service"
)

# Initialize camera backend based on configuration
def get_camera_backend() -> CameraBackend:
    """
    Get the configured camera backend instance.
    
    Returns:
        CameraBackend: The active camera backend implementation.
    """
    backend_type = settings.CAMERA_BACKEND.lower()
    
    if backend_type == "picamera2":
        return Picamera2Backend(subprocess_logger)
    elif backend_type == "subprocess":
        return RpicamBackend(subprocess_logger)
    else:
        subprocess_logger.warning(f"Unknown backend '{backend_type}', defaulting to subprocess.")
        return RpicamBackend(subprocess_logger)

# Global backend instance (lazy initialization)
_backend: Optional[CameraBackend] = None

def get_backend() -> CameraBackend:
    """Get or initialize the global camera backend."""
    global _backend
    if _backend is None:
        _backend = get_camera_backend()
        subprocess_logger.info(f"Initialized camera backend: {_backend.get_backend_name()}")
    return _backend


def is_camera_connected(camera_index: int = 0) -> bool:
    """
    Check if the camera is connected using --list-cameras (fast, no initialization).
    
    Args:
        camera_index (int): The index of the camera to check (default is 0).
    Returns:
        bool: True if the camera is connected, False otherwise.
    """
    backend = get_backend()
    return backend.is_camera_connected(camera_index)

def image_filename(
    camera_index: int, 
    index: str = None,
    img_size: tuple = None,
    image_encoding: str = "jpg") -> str:
    """
    Generate a compact image filename with timestamp and camera index.
    
    Examples:
        - 20260104_035632_123_c1.jpg  (with milliseconds for uniqueness)
        - 20260104_035632_123_c1_4624x3472.jpg  (with resolution)
        - 0001_c1.jpg  (with custom index like "0001")
    
    Args:
        camera_index (int): The camera index.
        index (str): Custom index/counter. If None, uses UTC timestamp with ms.
        include_resolution (bool): Whether to include resolution in filename.
        img_size (tuple): The image size as (width, height), required if include_resolution=True.
    Returns:
        str: The generated image filename.
    """
    if not index:
        now = datetime.now(timezone.utc)
        index = now.strftime("%Y%m%d_%H%M%S") + f"_{now.microsecond // 1000:03d}"
    
    filename = f"{index}_c{camera_index}"
    
    if img_size:
        width, height = img_size
        filename += f"_{width}x{height}"
    
    filename += image_encoding if image_encoding.startswith('.') else f".{image_encoding}"
    return filename
    

def capture_image(
        project_name: str,
        camera_config: CameraConfig,
        output_filename: Optional[str] = None,
        check_camera: bool = True,
        include_resolution: bool = False,
        capture_output: bool = False) -> str:
    """
    Capture an image using the rpicam-still command.
    
    Args:
        project_name (str): The name of the project to save the image in.
        camera_config (CameraConfig): Camera configuration object with all capture settings.
        output_filename (str): The name of the output image file (optional, auto-generated if None).
        check_camera (bool): Whether to check camera availability before capture (default is True).
        include_resolution (bool): Include resolution in auto-generated filename (default is False).
        capture_output (bool): Capture stderr/stdout for debugging (default is False for performance).
    Returns:
        str: The path to the captured image file.
    """
    
    if check_camera and not is_camera_connected(camera_config.camera_index):
        raise RuntimeError(f"Camera {camera_config.camera_index} is not connected.")
    
    project_path = PROJECTS_ROOT / project_name / "images" / "main"
    project_path.mkdir(parents=True, exist_ok=True)
    
    if not output_filename:
        output_filename = image_filename(
            camera_index=camera_config.camera_index,
            img_size=camera_config.img_size if include_resolution else None,
            image_encoding=camera_config.encoding
        )
    
    output_path = Path(project_path, output_filename)
    
    # Use backend for actual capture
    backend = get_backend()
    result = backend.capture_image(output_path, camera_config, capture_output)
    
    # Handle different return types
    # Result is always (path_or_paths, metadata)
    # path_or_paths can be:
    #   - single path string for JPEG/PNG only
    #   - tuple (jpeg_path, dng_path) for multi-format
    if isinstance(result, tuple) and len(result) == 2:
        return result  # (path_or_paths, metadata)
    else:
        return result, None  # fallback: path only, no metadata
    

def single_capture_image(
        project_name: str,
        camera_config: CameraConfig,
        check_camera: bool = True,
        include_resolution: bool = False) -> str:
    """
    Capture an image from a single camera.
    
    Args:
        project_name (str): The name of the project to save the image in.
        camera_config (CameraConfig): Configuration for the camera.
        check_camera (bool): Whether to check camera availability before capture.
        include_resolution (bool): Include resolution in auto-generated filename.
    Returns:
        str: The path to the captured image file.
    """
    
    if check_camera and not is_camera_connected(camera_config.camera_index):
        raise RuntimeError(f"Camera {camera_config.camera_index} is not connected.")
    
    start_time = time.time()
    
    output_path, metadata = capture_image(
        project_name=project_name,
        camera_config=camera_config,
        check_camera=False,  # Already checked
        include_resolution=include_resolution
    )
    
    elapsed_time = time.time() - start_time
    
    project_root = PROJECTS_ROOT / project_name
    
    record = generate_manifest_record(
        project_name=project_name,
        img_paths=[output_path],
        cam_configs=[camera_config],
        times=[elapsed_time],
        metadata_list=[metadata] if metadata else None
    )
    append_manifest_record(project_root, record)
    
    subprocess_logger.info(
        f"Single capture: cam{camera_config.camera_index}={elapsed_time:.3f}s"
    )
    
    return output_path

def dual_capture_image(
        project_name: str,
        cam1_config: CameraConfig,
        cam2_config: CameraConfig,
        check_camera: bool = True,
        include_resolution: bool = False,
        stagger_ms: int = 20) -> tuple:
    """
    Capture images from two cameras in parallel with independent configurations.
    
    Args:
        project_name (str): The name of the project to save the images in.
        cam1_config (CameraConfig): Configuration for camera 1.
        cam2_config (CameraConfig): Configuration for camera 2.
        check_camera (bool): Whether to check camera availability before capture.
        include_resolution (bool): Include resolution in auto-generated filenames.
        stagger_ms (int): Delay in ms between starting cameras (default is 20ms).
    Returns:
        tuple: (path1, path2, timing_dict) with paths and timing metrics.
        
    Example:
        cam1 = CameraConfig(camera_index=0, vflip=True, awb="auto")
        cam2 = CameraConfig(camera_index=1, hflip=True, awb="indoor")
        path1, path2, timing = dual_capture_image("myproject", cam1, cam2)
    """
    
    if check_camera:
        if not is_camera_connected(cam1_config.camera_index):
            raise RuntimeError(f"Camera {cam1_config.camera_index} is not connected.")
        if not is_camera_connected(cam2_config.camera_index):
            raise RuntimeError(f"Camera {cam2_config.camera_index} is not connected.")
        
    # Pre-generate filenames with same timestamp index for pairing
    now = datetime.now(timezone.utc)
    timestamp_index = now.strftime("%Y%m%d_%H%M%S") + f"_{now.microsecond // 1000:03d}"
    
    filename1 = image_filename(
        camera_index=cam1_config.camera_index,
        index=timestamp_index,
        img_size=cam1_config.img_size if include_resolution else None,
        image_encoding=cam1_config.encoding
    )
    filename2 = image_filename(
        camera_index=cam2_config.camera_index,
        index=timestamp_index,
        img_size=cam2_config.img_size if include_resolution else None,
        image_encoding=cam2_config.encoding
    )
    
    timing = {}
    
    def capture_with_timing(config, fname):
        start = time.time()
        path, metadata = capture_image(
            project_name=project_name,
            camera_config=config,
            output_filename=fname,
            check_camera=False,  # Already checked
            include_resolution=include_resolution,
            capture_output=False  # Max performance
        )
        elapsed = time.time() - start
        return path, elapsed, metadata
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        # Submit first camera
        future1 = executor.submit(capture_with_timing, cam1_config, filename1)
        
        # Stagger second camera start (like bash script)
        if stagger_ms > 0:
            time.sleep(stagger_ms / 1000.0)
        
        future2 = executor.submit(capture_with_timing, cam2_config, filename2)
        
        # Wait for both to complete
        img1_path, time1, metadata1 = future1.result()
        img2_path, time2, metadata2 = future2.result()
        
    project_root = PROJECTS_ROOT / project_name
    
    # Prepare metadata list (filter out None values)
    metadata_list = [m for m in [metadata1, metadata2] if m is not None]
    
    record = generate_manifest_record(
        project_name=project_name,
        pair_id=timestamp_index,
        img_paths=[img1_path, img2_path],
        cam_configs=[cam1_config, cam2_config],
        times=[time1, time2],
        stagger=stagger_ms,
        metadata_list=metadata_list if metadata_list else None
    )
    append_manifest_record(project_root, record)
    
    subprocess_logger.info(
        f"Parallel capture: cam{cam1_config.camera_index}={time1:.3f}s, cam{cam2_config.camera_index}={time2:.3f}s, stagger={stagger_ms}ms"
    )
    
    return img1_path, img2_path
    
if __name__ == "__main__":
    sys.exit(main())
