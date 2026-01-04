import subprocess
import os
import json
import logging
from logging.handlers import RotatingFileHandler
import sys
from pathlib import Path
import time
from datetime import datetime, timezone
import concurrent.futures
from typing import Optional

from utils import compute_sha256
from camera import CameraConfig
from manifestHandler import CaptureRecord, CaptureCamera, CaptureFile

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from app.core.config import settings

PROJECTS_ROOT = settings.projects_dir
LOG_FILE = settings.log_dir / "capture_service.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logger_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=5)

# save logging to a file in /var/log/dtk/capture_service.log
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s',
                    handlers=[logger_handler])

subprocess_logger = logging.getLogger('subprocess_logger')

def generate_manifest_record(
    project_name: str,
    img_paths: list,
    cam_configs: list,
    times: list = None,
    pair_id: str = None,
    stagger: int = None,
    roles: list = None) -> CaptureRecord:
    """
    Generate a manifest record for single or dual captures.
    
    Args:
        project_name: Name of the project
        img_paths: List of captured image paths
        cam_configs: List of CameraConfig objects used
        times: List of capture times in seconds (optional)
        pair_id: Shared ID for dual captures (optional, auto-generated)
        stagger: Delay between camera starts in ms (optional)
        roles: List of role names (e.g., ["left", "right"] or ["single"])
               If None, auto-assigns based on number of captures
    
    Returns:
        CaptureRecord object
    """
    # Auto-assign roles if not provided
    if roles is None:
        if len(img_paths) == 1:
            roles = ["single"]
        elif len(img_paths) == 2:
            roles = ["left", "right"]
        else:
            roles = [f"cam{i}" for i in range(len(img_paths))]
    
    # Build files list
    files = []
    for i, (path, config, role) in enumerate(zip(img_paths, cam_configs, roles)):
        files.append(CaptureFile(
            role=role,
            relative_path=str(Path("images/main") / Path(path).name),
            bytes=os.path.getsize(path),
            mimetype=f"image/{config.encoding}",
            sha256=compute_sha256(path)
        ))
    
    # Build cameras list
    cameras = []
    for config in cam_configs:
        cameras.append(CaptureCamera(
            camera_index=config.camera_index,
            config=config.to_dict()
        ))
    
    # Build timing dict
    timing = {}
    if times:
        for i, t in enumerate(times):
            timing[f'camera{i+1}_seconds'] = t
    if stagger is not None:
        timing['stagger_ms'] = stagger
    
    return CaptureRecord(
        project_name=project_name,
        pair_id=pair_id,
        files=files,
        cameras=cameras,
        timing=timing,
        software={
            "tool": "digitization-toolkit",
            "version": settings.app_version,
        }
    )
    

def append_manifest_record(project_root: Path, record: CaptureRecord):
    """
    Append a capture record to the manifest file in the project directory.
    
    Args:
        project_root (Path): The root directory of the project.
        record (CaptureRecord): The capture record to append.
    """
    
    metadata_dir = project_root / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    
    manifest_path = metadata_dir / "manifest.jsonl"
    
    with open(manifest_path, 'a', encoding="utf-8") as f:
        f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
        subprocess_logger.info(f"Appended capture record {record.capture_id} to manifest.")

def is_camera_connected(camera_index: int = 0) -> bool:
    """
    Check if the camera is connected using --list-cameras (fast, no initialization).
    
    Args:
        camera_index (int): The index of the camera to check (default is 0).
    Returns:
        bool: True if the camera is connected, False otherwise.
    """
    command = [
        "rpicam-still",
        "--list-cameras"
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=5 
        )
        if f"{camera_index} :" in result.stdout:
            subprocess_logger.info(f"Camera {camera_index} is connected.")
            return True
        else:
            subprocess_logger.warning(f"Camera {camera_index} not found in available cameras.")
            return False
    except subprocess.CalledProcessError as e:
        subprocess_logger.error(f"Failed to list cameras: {e.stderr}")
        return False
    except subprocess.TimeoutExpired:
        subprocess_logger.error("Camera list check timed out.")
        return False

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
    
    command = [
        "rpicam-still",
        "-o", str(output_path),
        "--width", str(camera_config.img_size[0]),
        "--height", str(camera_config.img_size[1]),
        "--quality", str(camera_config.quality),
        "--awb", camera_config.awb,
        "--buffer-count", str(camera_config.buffer_count),
        "--camera", str(camera_config.camera_index)
    ]
    
    if camera_config.timeout == 0:
        command.append("--immediate")
    else:
        command.extend(["-t", str(camera_config.timeout)])
    if camera_config.nopreview:
        command.append("-n")
    if camera_config.vflip:
        command.append("--vflip")
    if camera_config.hflip:
        command.append("--hflip")
    if camera_config.autofocus_on_capture:
        command.append("--autofocus-on-capture")
    if camera_config.thumbnail:
        command.extend(["--thumb", "320:240:70"])
    if camera_config.zsl:
        command.append("--zsl")
    if camera_config.encoding != "jpg":
        command.extend(["--encoding", camera_config.encoding])
    if camera_config.raw:
        command.append("--raw")
    
        
    subprocess_logger.info("Executing command: %s", ' '.join(command))
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=capture_output,
            text=capture_output,
            timeout=10
        )
        subprocess_logger.info(f"Image captured successfully: {output_path}")
        return str(output_path)
    except subprocess.CalledProcessError as e:
        if capture_output:
            subprocess_logger.error(f"Error capturing image: {e.stderr}")
        else:
            subprocess_logger.error(f"Error capturing image (exit code: {e.returncode})")
        raise
    except subprocess.TimeoutExpired:
        subprocess_logger.error(f"Image capture timed out after {camera_config.timeout} ms")
        raise
    

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
    
    output_path = capture_image(
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
        times=[elapsed_time]
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
        path = capture_image(
            project_name=project_name,
            camera_config=config,
            output_filename=fname,
            check_camera=False,  # Already checked
            include_resolution=include_resolution,
            capture_output=False  # Max performance
        )
        elapsed = time.time() - start
        return path, elapsed
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        # Submit first camera
        future1 = executor.submit(capture_with_timing, cam1_config, filename1)
        
        # Stagger second camera start (like bash script)
        if stagger_ms > 0:
            time.sleep(stagger_ms / 1000.0)
        
        future2 = executor.submit(capture_with_timing, cam2_config, filename2)
        
        # Wait for both to complete
        img1_path, time1 = future1.result()
        img2_path, time2 = future2.result()
        
    project_root = PROJECTS_ROOT / project_name
    
    record = generate_manifest_record(
        project_name=project_name,
        pair_id=timestamp_index,
        img_paths=[img1_path, img2_path],
        cam_configs=[cam1_config, cam2_config],
        times=[time1, time2],
        stagger=stagger_ms
    )
    append_manifest_record(project_root, record)
    
    subprocess_logger.info(
        f"Parallel capture: cam{cam1_config.camera_index}={time1:.3f}s, cam{cam2_config.camera_index}={time2:.3f}s, stagger={stagger_ms}ms"
    )
    
    return img1_path, img2_path
    
if __name__ == "__main__":
    # Example usage
    cam1 = CameraConfig(camera_index=0, vflip=True, awb="auto")
    cam2 = CameraConfig(camera_index=1, hflip=True, awb="indoor")
    
    try:
        path1, path2 = dual_capture_image("test_project", cam1, cam2)
        print(f"Captured images: {path1}, {path2}")
    except Exception as e:
        print(f"Capture failed: {e}")
