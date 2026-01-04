import subprocess
import os
import logging
from logging.handlers import RotatingFileHandler
import sys
from pathlib import Path
import time
from datetime import datetime, timezone
import concurrent.futures
from dataclasses import dataclass, asdict
from typing import Optional, Tuple

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

@dataclass
class CameraConfig:
    """
    Configuration for a specific camera with all capture parameters.
    
    This allows each camera to have independent settings (orientation, white balance, etc.)
    and makes configurations easy to save/load from files or database.
    """
    camera_index: int
    img_size: Tuple[int, int] = IMG_SIZES["high"]
    vflip: bool = False
    hflip: bool = False
    awb: str = "indoor"  # auto, indoor, tungsten, fluorescent, etc. See https://www.raspberrypi.com/documentation/computers/camera_software.html#awb for all options
    timeout: int = 0  # 0 = immediate capture
    autofocus_on_capture: bool = True
    buffer_count: int = 2
    thumbnail: bool = False
    nopreview: bool = True
    zsl: bool = False  # Zero Shutter Lag (ZSL) mode
    
    def to_dict(self):
        """Convert to dictionary for logging/serialization."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict):
        """Create from dictionary (e.g., from JSON config file)."""
        return cls(**data)
    
    def __repr__(self):
        return f"CameraConfig(cam{self.camera_index}, {self.img_size[0]}x{self.img_size[1]}, awb={self.awb})"


# Helper functions for saving/loading camera configs
def save_camera_configs(filepath: str, configs: dict):
    """
    Save camera configurations to a JSON file.
    
    Args:
        filepath: Path to save the JSON file.
        configs: Dict mapping camera names/IDs to CameraConfig objects.
        
    Example:
        configs = {
            "left_camera": CameraConfig(camera_index=0, vflip=True),
            "right_camera": CameraConfig(camera_index=1, hflip=True)
        }
        save_camera_configs("camera_setup.json", configs)
    """
    import json
    data = {name: config.to_dict() for name, config in configs.items()}
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)
    subprocess_logger.info(f"Saved camera configs to {filepath}")


def load_camera_configs(filepath: str) -> dict:
    """
    Load camera configurations from a JSON file.
    
    Args:
        filepath: Path to the JSON file.
        
    Returns:
        Dict mapping camera names/IDs to CameraConfig objects.
        
    Example:
        configs = load_camera_configs("camera_setup.json")
        path1, path2, _ = dual_capture_image(
            "myproject",
            cam1_config=configs["left_camera"],
            cam2_config=configs["right_camera"]
        )
    """
    import json
    with open(filepath, 'r') as f:
        data = json.load(f)
    configs = {name: CameraConfig.from_dict(cfg) for name, cfg in data.items()}
    subprocess_logger.info(f"Loaded {len(configs)} camera configs from {filepath}")
    return configs


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
    
    project_path = Path(PROJECTS_ROOT, project_name)
    os.makedirs(project_path, exist_ok=True)
    
    if not output_filename:
        output_filename = image_filename(
            camera_index=camera_config.camera_index,
            img_size=camera_config.img_size if include_resolution else None
        )
    
    output_path = Path(project_path, output_filename)
    
    command = [
        "rpicam-still",
        "-o", str(output_path),
        "--width", str(camera_config.img_size[0]),
        "--height", str(camera_config.img_size[1]),
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
        img_size=cam1_config.img_size if include_resolution else None
    )
    filename2 = image_filename(
        camera_index=cam2_config.camera_index,
        index=timestamp_index,
        img_size=cam2_config.img_size if include_resolution else None
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
    
    timing = {
        'camera1_seconds': time1,
        'camera2_seconds': time2,
        'stagger_ms': stagger_ms,
        'cam1_config': cam1_config.to_dict(),
        'cam2_config': cam2_config.to_dict()
    }
    subprocess_logger.info(
        "Parallel capture: cam%d=%.3fs, cam%d=%.3fs, stagger=%dms",
        cam1_config.camera_index, time1, cam2_config.camera_index, time2, stagger_ms
    )
    
    return img1_path, img2_path, timing
    

if __name__ == "__main__":
    # Example 1: Independent Camera Configurations
    print("=== Example 1: Independent Camera Configurations ===")
    cam1 = CameraConfig(
        camera_index=0,
        vflip=True,
        hflip=False,
        awb="auto",
        img_size=IMG_SIZES["high"],
        zsl=True
    )
    cam2 = CameraConfig(
        camera_index=1,
        vflip=False,
        hflip=True,
        awb="indoor",
        img_size=IMG_SIZES["high"]
    )
    
    start_time = time.time()
    path1, path2, timing = dual_capture_image(
        project_name="testproject",
        cam1_config=cam1,
        cam2_config=cam2,
        check_camera=False
    )
    print(f"Captured in {time.time() - start_time:.3f}s")
    print(f"Files: {path1.split('/')[-1]}, {path2.split('/')[-1]}")
    print(f"Config 1: {cam1}")
    print(f"Config 2: {cam2}")
    print()
    
    # Example 2: Same settings for both cameras
    print("=== Example 2: Same Configuration ===")
    default_config = CameraConfig(
        camera_index=0,  # Will be overridden
        timeout=0,
        vflip=False,
        hflip=True,
        awb="auto",
        zsl=False
    )
    
    cam1 = CameraConfig(**{**default_config.to_dict(), 'camera_index': 0})
    cam2 = CameraConfig(**{**default_config.to_dict(), 'camera_index': 1})
    
    start_time = time.time()
    path1, path2, timing = dual_capture_image(
        project_name="testproject",
        cam1_config=cam1,
        cam2_config=cam2,
        check_camera=False
    )
    print(f"Captured in {time.time() - start_time:.3f}s")
    print(f"Timing: {timing}")

    
