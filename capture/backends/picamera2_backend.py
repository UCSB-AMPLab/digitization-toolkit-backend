"""
Picamera2-based camera backend.

This backend uses the official picamera2 Python library which provides
direct access to libcamera. It supports advanced features like streaming,
live preview, and dynamic settings adjustment.
"""

import time
from pathlib import Path
from typing import Optional
from picamera2 import Picamera2

from .base import CameraBackend


class Picamera2Backend(CameraBackend):
    """
    Camera backend using the Picamera2 library.
    
    This implementation uses picamera2 for all camera operations. Benefits:
    - Supports streaming and live preview
    - Can adjust settings on the fly without restarting
    - Better performance for repeated captures (camera stays initialized)
    - Access to frame metadata and sensor information
    
    Trade-offs:
    - Slightly more memory usage (keeps camera resources)
    - Requires picamera2 Python library
    """
    
    def __init__(self, logger):
        """
        Initialize the Picamera2 backend.
        
        Args:
            logger: Logger instance for logging operations.
        """
        super().__init__(logger)
        self._cameras = {}  # Cache of initialized Picamera2 instances
        self._camera_info = None
    
    def _get_camera_info(self):
        """Get global camera information (cached)."""
        if self._camera_info is None:
            self._camera_info = Picamera2.global_camera_info()
        return self._camera_info
    
    def _get_camera(self, camera_index: int) -> Picamera2:
        """
        Get or create a Picamera2 instance for the given camera index.
        
        This caches camera instances for better performance.
        
        Args:
            camera_index (int): The camera index.
            
        Returns:
            Picamera2: The camera instance.
        """
        if camera_index not in self._cameras:
            self.logger.info(f"Initializing Picamera2 for camera {camera_index}")
            try:
                picam2 = Picamera2(camera_index)
                self._cameras[camera_index] = picam2
            except Exception as e:
                self.logger.error(f"Failed to initialize camera {camera_index}: {e}")
                raise RuntimeError(f"Failed to initialize camera {camera_index}: {e}")
        
        return self._cameras[camera_index]
    
    def is_camera_connected(self, camera_index: int = 0) -> bool:
        """
        Check if a camera is connected and available.
        
        Args:
            camera_index (int): The index of the camera to check.
            
        Returns:
            bool: True if the camera is connected, False otherwise.
        """
        try:
            cameras = self._get_camera_info()
            if camera_index < len(cameras):
                self.logger.info(f"Camera {camera_index} is connected: {cameras[camera_index].get('Model', 'Unknown')}")
                return True
            else:
                self.logger.warning(f"Camera {camera_index} not found (only {len(cameras)} camera(s) detected)")
                return False
        except Exception as e:
            self.logger.error(f"Failed to detect cameras: {e}")
            return False
    
    def _config_to_picamera2_controls(self, camera_config):
        """
        Convert CameraConfig to Picamera2 control parameters.
        
        Args:
            camera_config: CameraConfig object.
            
        Returns:
            dict: Control parameters for Picamera2.
        """
        controls = {}
        
        # Map AWB mode strings to Picamera2 values
        awb_map = {
            "auto": 0,
            "indoor": 1,
            "tungsten": 2,
            "fluorescent": 3,
            "outdoor": 4,
            "cloudy": 5,
            "custom": 6,
        }
        
        if camera_config.awb.lower() in awb_map:
            controls["AwbMode"] = awb_map[camera_config.awb.lower()]
        
        # Autofocus mode
        if camera_config.autofocus_on_capture:
            controls["AfMode"] = 2  # Continuous autofocus
        else:
            controls["AfMode"] = 0  # Manual focus
        
        return controls
    
    def capture_image(
        self,
        output_path: Path,
        camera_config,
        capture_output: bool = False
    ) -> str:
        """
        Capture a single image using Picamera2.
        
        Args:
            output_path (Path): Full path where the image should be saved.
            camera_config: CameraConfig object with capture settings.
            capture_output (bool): Not used for picamera2 (kept for interface compatibility).
            
        Returns:
            str: Path to the captured image file.
            
        Raises:
            RuntimeError: If capture fails.
        """
        try:
            picam2 = self._get_camera(camera_config.camera_index)
            
            # Determine if we need to reconfigure
            # For now, we'll configure each time to ensure settings match
            # In future optimization, we could cache configurations
            
            # Create still configuration
            transform_args = {}
            if camera_config.hflip:
                transform_args['hflip'] = 1
            if camera_config.vflip:
                transform_args['vflip'] = 1
            
            # Build configuration
            config_dict = {
                "main": {
                    "size": camera_config.img_size,
                    "format": "RGB888" if camera_config.encoding == "rgb" else "BGR888"
                },
                "buffer_count": camera_config.buffer_count,
            }
            
            if transform_args:
                config_dict["transform"] = Picamera2.Transform(**transform_args)
            
            still_config = picam2.create_still_configuration(**config_dict)
            
            # Check if camera is already running with different config
            if picam2.started:
                self.logger.debug(f"Stopping camera {camera_config.camera_index} to reconfigure")
                picam2.stop()
            
            picam2.configure(still_config)
            self.logger.debug(f"Camera {camera_config.camera_index} configured: {camera_config.img_size}")
            
            # Apply controls
            controls = self._config_to_picamera2_controls(camera_config)
            
            # Start camera
            picam2.start()
            
            # Apply controls after start
            if controls:
                picam2.set_controls(controls)
            
            # Wait for sensor to stabilize (similar to timeout in rpicam-still)
            if camera_config.timeout > 0:
                time.sleep(camera_config.timeout / 1000.0)
            
            # Capture image
            self.logger.info(f"Capturing image to: {output_path}")
            
            # Determine file format from encoding
            if camera_config.encoding in ["jpg", "jpeg"]:
                # Capture and save with format specified
                # Note: quality is controlled by JPEG encoder options in config, not capture_file
                picam2.capture_file(str(output_path), format='jpeg')
            elif camera_config.encoding == "png":
                picam2.capture_file(str(output_path), format='png')
            else:
                # For other formats, capture array and save
                picam2.capture_file(str(output_path))
            
            self.logger.info(f"Image captured successfully: {output_path}")
            
            # Note: We keep the camera running for better performance on next capture
            # It will be stopped/reconfigured if settings change or in cleanup()
            
            return str(output_path)
            
        except Exception as e:
            self.logger.error(f"Failed to capture image: {e}")
            raise RuntimeError(f"Picamera2 capture failed: {e}")
    
    def supports_streaming(self) -> bool:
        """
        Check if this backend supports video streaming/preview.
        
        Returns:
            bool: True - Picamera2 supports streaming.
        """
        return True
    
    def supports_live_adjustment(self) -> bool:
        """
        Check if this backend supports adjusting settings on the fly.
        
        Returns:
            bool: True - Picamera2 supports live adjustments.
        """
        return True
    
    def get_backend_name(self) -> str:
        """
        Get a human-readable name for this backend.
        
        Returns:
            str: "picamera2"
        """
        return "picamera2"
    
    def cleanup(self):
        """
        Cleanup camera resources.
        
        Stops and closes all initialized cameras.
        """
        self.logger.info("Cleaning up Picamera2 backend")
        for camera_index, picam2 in self._cameras.items():
            try:
                if picam2.started:
                    picam2.stop()
                picam2.close()
                self.logger.debug(f"Closed camera {camera_index}")
            except Exception as e:
                self.logger.warning(f"Error closing camera {camera_index}: {e}")
        
        self._cameras.clear()
