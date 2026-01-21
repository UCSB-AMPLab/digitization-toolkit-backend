"""
Subprocess-based camera backend using rpicam-still.
"""

import subprocess
from pathlib import Path
from typing import Optional

from .base import CameraBackend


class RpicamBackend(CameraBackend):
    """
    Camera backend using rpicam-still subprocess calls.
    Implements the CameraBackend interface.
    """
    
    def __init__(self, logger):
        """
        Initialize the rpicam subprocess backend.
        
        Args:
            logger: Logger instance for logging operations.
        """
        super().__init__(logger)
    
    def is_camera_connected(self, camera_index: int = 0) -> bool:
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
                self.logger.info(f"Camera {camera_index} is connected.")
                return True
            else:
                self.logger.warning(f"Camera {camera_index} not found in available cameras.")
                return False
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to list cameras: {e.stderr}")
            return False
        except subprocess.TimeoutExpired:
            self.logger.error("Camera list check timed out.")
            return False
    
    def capture_image(
        self,
        output_path: Path,
        camera_config,
        capture_output: bool = False
    ) -> str:
        """
        Capture an image using the rpicam-still command.
        
        Args:
            output_path (Path): Full path where the image should be saved.
            camera_config: CameraConfig object with all capture settings.
            capture_output (bool): Capture stderr/stdout for debugging (default is False for performance).
            
        Returns:
            str: The path to the captured image file.
            
        Raises:
            RuntimeError: If capture fails.
        """
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
        # Manual focus via lens position (optional float, in dioptres)
        lens_pos = getattr(camera_config, "lens_position", None)
        if lens_pos is not None:
            command.extend(["--lens-position", str(lens_pos)])
        if camera_config.encoding != "jpg":
            command.extend(["--encoding", camera_config.encoding])
        if camera_config.raw:
            command.append("--raw")
        
        self.logger.info("Executing command: %s", ' '.join(command))
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=capture_output,
                text=capture_output,
                timeout=10
            )
            self.logger.info(f"Image captured successfully: {output_path}")
            return str(output_path)
        except subprocess.CalledProcessError as e:
            if capture_output:
                self.logger.error(f"Error capturing image: {e.stderr}")
            else:
                self.logger.error(f"Error capturing image (exit code: {e.returncode})")
            raise RuntimeError(f"Failed to capture image: {e}")
        except subprocess.TimeoutExpired:
            self.logger.error(f"Image capture timed out after 10s")
            raise RuntimeError("Image capture timed out")
    
    def supports_streaming(self) -> bool:
        """
        Check if this backend supports video streaming/preview.
        
        Returns:
            bool: False - subprocess backend doesn't support streaming.
        """
        return False
    
    def supports_live_adjustment(self) -> bool:
        """
        Check if this backend supports adjusting settings on the fly.
        
        Returns:
            bool: False - subprocess backend requires new process for settings changes.
        """
        return False
    
    def get_backend_name(self) -> str:
        """
        Get a human-readable name for this backend.
        
        Returns:
            str: "rpicam-subprocess"
        """
        return "rpicam-subprocess"
