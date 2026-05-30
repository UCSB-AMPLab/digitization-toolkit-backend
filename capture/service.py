import sys
from pathlib import Path
import time
import threading
from datetime import datetime, timezone
import concurrent.futures
from typing import Optional

# Fixed temp-file paths for live preview frames (one per camera).
# Using stable names rather than mkstemp prevents unbounded accumulation when
# the process is killed before the finally-block cleanup runs.
_PREVIEW_TMP_DIR = Path("/tmp")
_PREVIEW_PREFIX = "dtk_preview_c"

# Per-camera locks so concurrent polling requests serialise writes to the
# shared fixed-path temp file.
_preview_locks: dict = {}
_preview_locks_mutex = threading.Lock()

def _get_preview_lock(camera_index: int) -> threading.Lock:
    """Return (creating if necessary) the per-camera lock for preview writes."""
    with _preview_locks_mutex:
        if camera_index not in _preview_locks:
            _preview_locks[camera_index] = threading.Lock()
        return _preview_locks[camera_index]

backend_dir = Path(__file__).parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from .utils import setup_rotating_logger
from .camera import CameraConfig
from .manifestHandler import generate_manifest_record, append_manifest_record
from .backends import CameraBackend, RpicamBackend, Picamera2Backend, GPhoto2Backend
from .project_manager import secure_project_filename

from app.core.config import settings

PROJECTS_ROOT = settings.projects_dir
LOG_FILE = settings.log_dir / "capture_service.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

subprocess_logger = setup_rotating_logger(
    log_file=str(LOG_FILE),
    logger_name="capture_service"
)

# Route picamera2's own log output into the same rotating log file.
# picamera2 uses Python's standard logging under the "picamera2" namespace,
# so we attach a single RotatingFileHandler from the existing logger.
import logging as _logging
from logging.handlers import RotatingFileHandler as _RFH
_picamera2_logger = _logging.getLogger("picamera2")
if not _picamera2_logger.handlers:
    _rfh = next((h for h in subprocess_logger.handlers if isinstance(h, _RFH)), None)
    if _rfh:
        _picamera2_logger.setLevel(_logging.DEBUG)
        _picamera2_logger.addHandler(_rfh)

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
    elif backend_type == "gphoto2":
        return GPhoto2Backend(subprocess_logger)
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
        capture_output: bool = False,
        collection_name: Optional[str] = None) -> str:
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
    
    if collection_name:
        project_path = PROJECTS_ROOT / secure_project_filename(project_name) / secure_project_filename(collection_name) / "images" / "main"
    else:
        project_path = PROJECTS_ROOT / secure_project_filename(project_name) / "images" / "main"
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
        include_resolution: bool = False,
        collection_name: Optional[str] = None) -> tuple:
    """
    Capture an image from a single camera.
    
    Args:
        project_name (str): The name of the project to save the image in.
        camera_config (CameraConfig): Configuration for the camera.
        check_camera (bool): Whether to check camera availability before capture.
        include_resolution (bool): Include resolution in auto-generated filename.
    Returns:
        tuple: (output_path, capture_id, pair_id) - path to image and manifest IDs.
    """
    
    if check_camera and not is_camera_connected(camera_config.camera_index):
        raise RuntimeError(f"Camera {camera_config.camera_index} is not connected.")
    
    start_time = time.time()
    
    output_path, metadata = capture_image(
        project_name=project_name,
        camera_config=camera_config,
        check_camera=False,  # Already checked
        include_resolution=include_resolution,
        collection_name=collection_name
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
        f"Single capture: cam{camera_config.camera_index}={elapsed_time:.3f}s, capture_id={record.capture_id}"
    )
    
    return output_path, record.capture_id, record.pair_id

def dual_capture_image(
        project_name: str,
        cam1_config: CameraConfig,
        cam2_config: CameraConfig,
        check_camera: bool = True,
        include_resolution: bool = False,
        stagger_ms: int = 20,
        collection_name: Optional[str] = None) -> tuple:
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
        tuple: (path1, path2, capture_id, pair_id) - paths to images and manifest IDs.
        
    Example:
        cam1 = CameraConfig(camera_index=0, vflip=True, awb="auto")
        cam2 = CameraConfig(camera_index=1, hflip=True, awb="indoor")
        path1, path2, capture_id, pair_id = dual_capture_image("myproject", cam1, cam2)
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
            capture_output=False,  # Max performance
            collection_name=collection_name
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
        f"Parallel capture: cam{cam1_config.camera_index}={time1:.3f}s, cam{cam2_config.camera_index}={time2:.3f}s, "
        f"stagger={stagger_ms}ms, capture_id={record.capture_id}, pair_id={record.pair_id}"
    )
    
    return img1_path, img2_path, record.capture_id, record.pair_id
    
def capture_preview_frame(camera_index: int) -> bytes:
    """
    Capture a low-resolution preview frame and return JPEG bytes.

    Not saved to the project directory — intended for live preview polling
    from the frontend. Uses a stable per-camera temp file that is overwritten
    on every call (rather than mkstemp), so at most one file per camera ever
    exists in /tmp even if the process is killed unexpectedly.

    The preview uses a lightweight configuration:
      - 1280×720 (native fast mode, no cropping)
      - No autofocus cycle (too slow for live preview)
      - No AE stabilisation wait
      - No temporal denoise warmup
      - Reduced JPEG quality (75) for a smaller payload

    Args:
        camera_index: Camera index (0 or 1).

    Returns:
        JPEG bytes of the preview frame.

    Raises:
        RuntimeError: If the camera is not connected or capture fails.
    """
    if not is_camera_connected(camera_index):
        raise RuntimeError(f"Camera {camera_index} is not connected")

    # Fixed per-camera path — overwrites the same file each poll cycle.
    # A per-camera lock serialises concurrent requests so two tabs never
    # race on the same path.
    tmp_path = _PREVIEW_TMP_DIR / f"{_PREVIEW_PREFIX}{camera_index}.jpg"
    lock = _get_preview_lock(camera_index)

    preview_config = CameraConfig(
        camera_index=camera_index,
        img_size=(1280, 720),        # Native 80 fps mode — fast, no crop
        autofocus_on_capture=False,  # Skip AF cycle for live preview
        timeout=0,                   # No AE stabilisation wait
        denoise_frames=0,            # No temporal denoise warmup
        quality=75,                  # Smaller payload for polling
        encoding="jpg",
        raw=False,
    )

    backend = get_backend()

    with lock:
        for attempt in range(2):
            try:
                backend.capture_image(tmp_path, preview_config)
                data = tmp_path.read_bytes()
                return data
            except Exception as exc:
                subprocess_logger.warning(
                    f"Preview capture failed for camera {camera_index} "
                    f"(attempt {attempt + 1}/2): {exc}"
                )
                # Evict the cached camera instance so the next attempt (or the
                # next polling cycle) gets a clean Picamera2 object.
                if hasattr(backend, "reset_camera"):
                    backend.reset_camera(camera_index)
                if attempt == 1:
                    raise RuntimeError(
                        f"Preview capture failed for camera {camera_index}: {exc}"
                    ) from exc
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass


def flush_preview_tmp() -> int:
    """
    Delete all stale preview temp files from /tmp.

    Useful when the server was previously killed without running cleanup,
    leaving behind dtk_preview_c*.jpg files.  Safe to call at any time;
    files in active use are simply recreated on the next polling cycle.

    Returns:
        Number of files deleted.
    """
    count = 0
    for f in _PREVIEW_TMP_DIR.glob(f"{_PREVIEW_PREFIX}*.jpg"):
        try:
            f.unlink(missing_ok=True)
            subprocess_logger.info(f"Flushed stale preview temp file: {f}")
            count += 1
        except Exception as e:
            subprocess_logger.warning(f"Could not remove preview temp file {f}: {e}")
    return count


# ---------------------------------------------------------------------------
# Focus helpers
# ---------------------------------------------------------------------------

def get_focus(camera_index: int) -> float:
    """Return the current lens position (dioptres) for *camera_index*.

    Reads the value from the running Picamera2 instance's metadata if
    available, otherwise falls back to 0.0 (infinity).
    """
    backend = get_backend()

    # picamera2 backend exposes the cached instance
    if hasattr(backend, "_cameras") and camera_index in backend._cameras:
        picam2 = backend._cameras[camera_index]
        try:
            meta = picam2.capture_metadata()
            # LensPosition is available when AF is in use (may be None otherwise)
            pos = meta.get("LensPosition")
            if pos is not None:
                return float(pos)
        except Exception:
            pass

    return 0.0


def set_focus(camera_index: int, lens_position: float) -> float:
    """Set the lens to *lens_position* dioptres on *camera_index*.

    Switches the camera to manual AF mode and applies the controls.
    Returns the applied lens_position.

    Raises RuntimeError if the camera is not connected or the backend does
    not support manual focus control.
    """
    if not is_camera_connected(camera_index):
        raise RuntimeError(f"Camera {camera_index} is not connected")

    backend = get_backend()

    # Clamp to a reasonable range (0 = infinity, 10 = ~10 cm)
    pos = max(0.0, min(10.0, float(lens_position)))

    if hasattr(backend, "apply_controls"):
        backend.apply_controls(camera_index, {
            "AfMode": 0,        # 0 = Manual AF mode
            "LensPosition": pos,
        })
    else:
        raise RuntimeError("Current camera backend does not support manual focus")

    return pos


def set_camera_controls(camera_index: int, controls: dict) -> None:
    """Apply arbitrary picamera2 controls to *camera_index* live.

    *controls* should use picamera2 control names directly
    (e.g. ``{'AeEnable': False, 'ExposureTime': 125}``).

    Raises RuntimeError if the camera is not available or the backend does not
    support live control updates.
    """
    if not is_camera_connected(camera_index):
        raise RuntimeError(f"Camera {camera_index} is not connected")

    backend = get_backend()
    if not hasattr(backend, "apply_controls"):
        raise RuntimeError("Current camera backend does not support live control updates")

    backend.apply_controls(camera_index, controls)


def apply_zoom(camera_index: int, zoom_factor: float) -> None:
    """Apply ScalerCrop-based digital zoom to the camera preview stream.

    *zoom_factor* 1.0 restores the full sensor field of view.
    Values > 1.0 crop towards the centre of the sensor.

    Raises RuntimeError if the camera is unavailable or the backend does not
    support zoom (e.g. subprocess backend).
    """
    if not is_camera_connected(camera_index):
        raise RuntimeError(f"Camera {camera_index} is not connected")

    backend = get_backend()
    if not hasattr(backend, "apply_zoom"):
        raise RuntimeError("Current camera backend does not support digital zoom")

    backend.apply_zoom(camera_index, zoom_factor)


def main():
    """
    Main entry point for testing the camera connectivity.
    """
    try:
        # Check if cameras are connected
        cam0_connected = is_camera_connected(0)
        cam1_connected = is_camera_connected(1)
        
        subprocess_logger.info(f"Camera 0: {'Connected' if cam0_connected else 'Not connected'}")
        subprocess_logger.info(f"Camera 1: {'Connected' if cam1_connected else 'Not connected'}")
        
        if not cam0_connected and not cam1_connected:
            subprocess_logger.error("No cameras detected!")
            return 1
        
        subprocess_logger.info("Capture service initialized successfully")
        return 0
        
    except Exception as e:
        subprocess_logger.error(f"Service initialization failed: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
