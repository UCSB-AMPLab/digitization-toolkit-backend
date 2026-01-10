from dataclasses import dataclass, asdict
from typing import Optional, Tuple
import logging

subprocess_logger = logging.getLogger(__name__)
IMG_SIZES = {
    "low": (2312, 1736),      # ~4 MP, ~195 DPI for A4, 1379 pph - Preview only
    "medium": (3840, 2160),   # ~8 MP, ~350 DPI for A4, 883 pph - RECOMMENDED for production
    "high": (4624, 3472),     # 16 MP, ~420 DPI for A4, 474 pph - Special collections
}


@dataclass
class CameraConfig:
    """
    Configuration for a specific camera with all capture parameters.
    
    This allows each camera to have independent settings (orientation, white balance, etc.)
    and makes configurations easy to save/load from files or database.
    
    Default resolution is MEDIUM (3840x2160) which provides:
    - 350 DPI for A4 pages (exceeds 300 DPI archival standard)
    - 883 pages/hour throughput (meets 800 pph production target)
    - Optimal use of Raspberry Pi 5 ISP capacity
    
    For special collections requiring maximum quality, use HIGH (4624x3472):
    - 420 DPI for A4 pages
    - 474 pages/hour (slower but higher quality)
    - Pushes Pi 5 ISP to hardware limits
    """
    camera_index: int
    img_size: Tuple[int, int] = IMG_SIZES["medium"]
    vflip: bool = False
    hflip: bool = False
    awb: str = "indoor"  # auto, indoor, tungsten, fluorescent, etc. See https://www.raspberrypi.com/documentation/computers/camera_software.html#awb for all options
    timeout: int = 50  # Preview timeout in ms (needed for autofocus/auto-exposure)
    autofocus_on_capture: bool = True
    lens_position: Optional[float] = None  # Manual focus lens position in dioptres (overrides autofocus)
    buffer_count: int = 2
    thumbnail: bool = False
    nopreview: bool = True
    quality: int = 93  # JPEG quality (1-100, default 93)
    encoding: str = "jpg"  # Options: jpg, png, bmp, rgb, yuv420
    raw: bool = False  # Capture RAW alongside JPEG
    denoise_frames: int = 10  # Number of frames to skip for temporal denoise warmup (Pi 5 feature, 0 to disable)
    zsl: bool = False  # Zero shutter lag mode (enables faster captures, may affect exposure)

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