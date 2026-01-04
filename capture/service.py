import subprocess
import os
import logging
from logging.handlers import RotatingFileHandler
import sys
from pathlib import Path
import time
from datetime import datetime, timezone
import concurrent.futures

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from app.core.config import settings

PROJECTS_ROOT = getattr(settings, "PROJECTS_ROOT", None)
LOG_FILE = Path(getattr(settings, "DTK_LOG_DIR", None), 'capture_service.log')

logger_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=5)

# save logging to a file in /var/log/dtk/capture_service.log
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s',
                    handlers=[logger_handler])

subprocess_logger = logging.getLogger('subprocess_logger')

IMG_SIZES = {
    "low": (2312, 1736),
    "medium": (3840, 2160),
    "high": (4624, 3472),
}

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
            subprocess_logger.info("Camera %d is connected.", camera_index)
            return True
        else:
            subprocess_logger.warning("Camera %d not found in available cameras.", camera_index)
            return False
    except subprocess.CalledProcessError as e:
        subprocess_logger.error("Failed to list cameras: %s", e.stderr)
        return False
    except subprocess.TimeoutExpired:
        subprocess_logger.error("Camera list check timed out.")
        return False

def image_filename(
    camera_index: int, 
    index: str = None,
    img_size: tuple = None) -> str:
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
    
    filename += ".jpg"
    return filename
    

def capture_image(
        project_name:str, 
        output_filename:str = None, 
        camera: int = 1, 
        timeout: int = 0, 
        img_size: tuple = IMG_SIZES.get("high"),
        thumbnail: bool = False,
        buffer_count: int = 2,
        vflip: bool = False,
        hflip: bool = False,
        awb: str = "indoor",
        autofocus_on_capture: bool = True,
        nopreview: bool = True,
        check_camera: bool = True,
        include_resolution: bool = False,
        capture_output: bool = False) -> str:
    """
    Capture an image using the rpicam-still command.
    
    Args:
        project_name (str): The name of the project to save the image in.
        output_filename (str): The name of the output image file.
        camera (int): The camera index to use (default is 1).
        timeout (int): Preview timeout in ms before capture (default is 0 for immediate).
        img_size (tuple): The image size as (width, height) (default is 4624x3472).
        thumbnail (bool): Whether to create a thumbnail (default is False).
        buffer_count (int): The number of buffers to use (default is 2).
        vflip (bool): Whether to vertically flip the image (default is False).
        hflip (bool): Whether to horizontally flip the image (default is False).
        awb (str): The auto white balance mode (default is "indoor").
        autofocus_on_capture (bool): Whether to enable autofocus on capture (default is True).
        check_camera (bool): Whether to check camera availability before capture (default is True).
        include_resolution (bool): Include resolution in auto-generated filename (default is False).
        capture_output (bool): Capture stderr/stdout for debugging (default is False for performance).
    Returns:
        str: The path to the captured image file.
    """
    
    if check_camera and not is_camera_connected(camera):
        raise RuntimeError(f"Camera {camera} is not connected.")
    
    project_path = Path(PROJECTS_ROOT, project_name)
    os.makedirs(project_path, exist_ok=True)
    
    if not output_filename:
        output_filename = image_filename(
            camera_index=camera,
            img_size=img_size if include_resolution else None
        )
    
    output_path = Path(project_path, output_filename)
    
    command = [
        "rpicam-still",
        "-o", str(output_path),
        "--width", str(img_size[0]),
        "--height", str(img_size[1]),
        "--awb", awb,
        "--buffer-count", str(buffer_count),
        "--camera", str(camera)
    ]
    
    if timeout == 0:
        command.append("--immediate")
    else:
        command.extend(["-t", str(timeout)])
    if nopreview:
        command.append("-n")
    if vflip:
        command.append("--vflip")
    if hflip:
        command.append("--hflip")
    if autofocus_on_capture:
        command.append("--autofocus-on-capture")
    if thumbnail:
        command.extend(["--thumb", "320:240:70"])
    
        
    subprocess_logger.info("Executing command: %s", ' '.join(command))
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=capture_output,
            text=capture_output,
            timeout=10
        )
        subprocess_logger.info("Image captured successfully: %s", output_path)
        return str(output_path)
    except subprocess.CalledProcessError as e:
        if capture_output:
            subprocess_logger.error("Error capturing image: %s", e.stderr)
        else:
            subprocess_logger.error("Error capturing image (exit code: %s)", e.returncode)
        raise
    except subprocess.TimeoutExpired:
        subprocess_logger.error("Image capture timed out after %d ms", timeout)
        raise
    

def dual_capture_image(
        project_name:str, 
        camera1: int = 0, 
        camera2: int = 1, 
        timeout: int = 0, 
        img_size: tuple = IMG_SIZES.get("high"),
        thumbnail: bool = False,
        buffer_count: int = 2,
        vflip: bool = False,
        hflip: bool = False,
        awb: str = "indoor",
        autofocus_on_capture: bool = True,
        nopreview: bool = True,
        check_camera: bool = True,
        include_resolution: bool = False,
        stagger_ms: int = 20) -> tuple:
    """
    Capture images from two cameras in parallel (like bash script) or sequentially.
    
    Parallel mode launches both rpicam-still processes simultaneously with optional
    stagger delay, matching the performance of dual_shoot.sh.
    
    Args:
        project_name (str): The name of the project to save the images in.
        camera1 (int): The index of the first camera (default is 0).
        camera2 (int): The index of the second camera (default is 1).
        parallel (bool): Run captures in parallel for max speed (default is True).
        stagger_ms (int): Delay in ms between starting cameras (default is 20ms).
        Other args are the same as in capture_image().
    Returns:
        tuple: (path1, path2, timing_dict) with paths and timing metrics.
    """
    
    if check_camera:
        if not is_camera_connected(camera1):
            raise RuntimeError(f"Camera {camera1} is not connected.")
        if not is_camera_connected(camera2):
            raise RuntimeError(f"Camera {camera2} is not connected.")
    
    check_camera = False
        
    # Pre-generate filenames with same timestamp index for pairing
    now = datetime.now(timezone.utc)
    timestamp_index = now.strftime("%Y%m%d_%H%M%S") + f"_{now.microsecond // 1000:03d}"
    
    filename1 = image_filename(
        camera_index=camera1,
        index=timestamp_index,
        img_size=img_size if include_resolution else None
    )
    filename2 = image_filename(
        camera_index=camera2,
        index=timestamp_index,
        img_size=img_size if include_resolution else None
    )
    
    timing = {}
    
    def capture_with_timing(cam, fname):
        start = time.time()
        path = capture_image(
            project_name=project_name,
            output_filename=fname,
            camera=cam,
            timeout=timeout,
            img_size=img_size,
            thumbnail=thumbnail,
            buffer_count=buffer_count,
            vflip=vflip,
            hflip=hflip,
            awb=awb,
            autofocus_on_capture=autofocus_on_capture,
            nopreview=nopreview,
            check_camera=check_camera,
            include_resolution=include_resolution,
            capture_output=False  # Max performance
        )
        elapsed = time.time() - start
        return path, elapsed
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        # Submit first camera
        future1 = executor.submit(capture_with_timing, camera1, filename1)
        
        # Stagger second camera start
        if stagger_ms > 0:
            time.sleep(stagger_ms / 1000.0)
        
        future2 = executor.submit(capture_with_timing, camera2, filename2)
        
        # Wait for both to complete
        img1_path, time1 = future1.result()
        img2_path, time2 = future2.result()
    
    timing = {
        'camera1_seconds': time1,
        'camera2_seconds': time2,
        'stagger_ms': stagger_ms
    }
    subprocess_logger.info(
        "Capture: cam%d=%.3fs, cam%d=%.3fs, stagger=%dms",
        camera1, time1, camera2, time2, stagger_ms
    )
    
    return img1_path, img2_path, timing
    

if __name__ == "__main__":
    start_time = time.time()
    dual_capture_image(
        project_name="testproject",
        camera1=0,
        camera2=1,
        timeout=500,
        img_size=IMG_SIZES.get("high"),
        thumbnail=True,
        buffer_count=4,
        vflip=False,
        hflip=True,
        awb="auto",
        autofocus_on_capture=True,
        nopreview=True,
        check_camera=True,
        include_resolution=True
    )
    end_time = time.time()
    subprocess_logger.info("Total dual capture time: %.3f seconds", end_time - start_time)
    # captures per hour
    captures_per_hour = 3600 / (end_time - start_time)
    subprocess_logger.info("Estimated captures per hour: %.2f", captures_per_hour)
    
