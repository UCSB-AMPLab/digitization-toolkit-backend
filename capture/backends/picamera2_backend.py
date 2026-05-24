"""
Picamera2-based camera backend.

This backend uses the official picamera2 Python library which provides
direct access to libcamera. It supports advanced features like streaming,
live preview, and dynamic settings adjustment.
"""

import sys
import time
import threading
from pathlib import Path

# Only import picamera2/libcamera on Linux (inside Docker/Raspberry Pi)
if sys.platform == "linux":
    from picamera2 import Picamera2
    from libcamera import Transform
else:
    Picamera2 = None
    Transform = None



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
    - Only works on Linux (Raspberry Pi)
    """
    
    def __init__(self, logger):
        """
        Initialize the Picamera2 backend.
        
        Args:
            logger: Logger instance for logging operations.
        """
        if Picamera2 is None:
            raise RuntimeError("Picamera2Backend requires Linux (Raspberry Pi OS)")
        
        super().__init__(logger)
        self._cameras = {}  # Cache of initialized Picamera2 instances
        self._camera_info = None
        self._last_configs = {}  # Track last configuration for each camera
        self._format_mode = {}  # Track format mode per camera (YUV420 vs RGB888)
        # Per-camera mutex: serialises preview polling and full captures so they
        # never call capture_request() on the same Picamera2 instance simultaneously.
        self._camera_locks: dict = {}
        self._locks_mutex = threading.Lock()
    
    def _get_camera_lock(self, camera_index: int) -> threading.Lock:
        """Return (creating if needed) the per-camera threading.Lock."""
        with self._locks_mutex:
            if camera_index not in self._camera_locks:
                self._camera_locks[camera_index] = threading.Lock()
            return self._camera_locks[camera_index]

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
        
        # Autofocus mode: Use Auto mode for still capture (not Continuous)
        # Auto mode lets us trigger AF before each capture for consistent focus
        if camera_config.autofocus_on_capture:
            controls["AfMode"] = 1  # Auto mode - trigger before capture
        else:
            controls["AfMode"] = 0  # Manual focus
        
        return controls
    
    def _extract_archival_metadata(self, metadata: dict) -> dict:
        """
        Extract relevant metadata for archival documentation.
        
        Captures critical sensor conditions for cultural heritage standards:
        - Exposure settings (time, gain)
        - Focus position
        - Color/white balance
        - Timing information
        
        Args:
            metadata: Raw metadata dict from Picamera2
            
        Returns:
            Dict with archival-relevant metadata fields
        """
        archival = {}
        
        # Exposure information (critical for reproducibility)
        if 'ExposureTime' in metadata:
            archival['ExposureTime'] = metadata['ExposureTime']  # in microseconds
        if 'AnalogueGain' in metadata:
            archival['AnalogueGain'] = float(metadata['AnalogueGain'])
        if 'DigitalGain' in metadata:
            archival['DigitalGain'] = float(metadata['DigitalGain'])
        
        # Focus information
        if 'LensPosition' in metadata:
            archival['LensPosition'] = float(metadata['LensPosition'])  # in dioptres
        if 'FocusFoM' in metadata:
            archival['FocusFoM'] = metadata['FocusFoM']  # Focus Figure of Merit
        
        # Color/white balance information
        if 'ColourGains' in metadata:
            archival['ColourGains'] = list(metadata['ColourGains'])  # [red, blue] gains
        if 'ColourTemperature' in metadata:
            archival['ColourTemperature'] = metadata['ColourTemperature']  # in Kelvin
        
        # Timing information (exact capture moment)
        if 'SensorTimestamp' in metadata:
            archival['SensorTimestamp'] = metadata['SensorTimestamp']  # nanoseconds since boot
        
        # Sensor configuration
        if 'SensorBlackLevels' in metadata:
            archival['SensorBlackLevels'] = list(metadata['SensorBlackLevels'])
        
        # Image quality metrics
        if 'Lux' in metadata:
            archival['Lux'] = float(metadata['Lux'])  # Scene brightness
        
        return archival
    
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
        lock = self._get_camera_lock(camera_config.camera_index)
        with lock:
            return self._capture_image_locked(output_path, camera_config, capture_output)

    def _capture_image_locked(
        self,
        output_path: Path,
        camera_config,
        capture_output: bool = False
    ) -> str:
        """Internal capture implementation — must be called with the camera lock held."""
        try:
            picam2 = self._get_camera(camera_config.camera_index)
            
            # Determine if we need to reconfigure
            # For now, we'll configure each time to ensure settings match
            # In future optimization, we could cache configurations
            
            # Use YUV420 format for JPEG captures (faster, less memory)
            # Use RGB888 for PNG or when raw/DNG is needed
            use_yuv = camera_config.encoding in ["jpg", "jpeg"] and not camera_config.raw
            
            # Create still configuration with transform if needed
            config_args = {
                "main": {
                    "size": camera_config.img_size,
                    "format": "YUV420" if use_yuv else "RGB888"
                },
                "buffer_count": camera_config.buffer_count,
            }
            
            # Add raw stream if DNG capture requested
            if camera_config.raw:
                config_args["raw"] = {}  # Enable raw stream for DNG
            
            # Apply transformations (flip)
            if camera_config.hflip or camera_config.vflip:
                if Transform is None:
                    raise RuntimeError("Transform requires Linux")
                hflip = 1 if camera_config.hflip else 0
                vflip = 1 if camera_config.vflip else 0
                config_args["transform"] = Transform(hflip=hflip, vflip=vflip)
            
            still_config = picam2.create_still_configuration(**config_args)
            
            # Check if camera is already running with the same config
            # Only reconfigure if settings changed - this preserves AE/AF state
            last_config = self._last_configs.get(camera_config.camera_index)
            last_format = self._format_mode.get(camera_config.camera_index)
            needs_reconfigure = (
                last_config is None or
                last_format != use_yuv or
                last_config.img_size != camera_config.img_size or
                last_config.hflip != camera_config.hflip or
                last_config.vflip != camera_config.vflip or
                last_config.buffer_count != camera_config.buffer_count
            )
            
            if needs_reconfigure:
                if picam2.started:
                    self.logger.debug(f"Stopping camera {camera_config.camera_index} to reconfigure")
                    picam2.stop()
                
                picam2.configure(still_config)
                self.logger.debug(f"Camera {camera_config.camera_index} configured: {camera_config.img_size}, format={'YUV420' if use_yuv else 'RGB888'}")
                self._last_configs[camera_config.camera_index] = camera_config
                self._format_mode[camera_config.camera_index] = use_yuv
            else:
                self.logger.debug(f"Camera {camera_config.camera_index} using cached configuration")
            
            # Apply controls
            controls = self._config_to_picamera2_controls(camera_config)
            
            # Start camera if not already running
            if not picam2.started:
                picam2.start()
                self.logger.debug(f"Camera {camera_config.camera_index} started")
            
            # Set JPEG quality via options (applies to capture_file)
            picam2.options["quality"] = camera_config.quality
            
            # Apply controls after start
            if controls:
                picam2.set_controls(controls)
            
            # Manual focus if lens position specified
            if hasattr(camera_config, 'lens_position') and camera_config.lens_position is not None:
                self.logger.debug(f"Setting manual focus: LensPosition={camera_config.lens_position}")
                picam2.set_controls({"LensPosition": camera_config.lens_position})
            
            # Temporal denoise warmup (Pi 5 feature)
            # Skip frames after camera start to let temporal denoise algorithm build history
            # This produces cleaner images with better noise reduction
            if hasattr(camera_config, 'denoise_frames') and camera_config.denoise_frames > 0 and needs_reconfigure:
                # Only apply warmup if we just reconfigured (camera was stopped/restarted)
                # Calculate delay: assuming ~30fps, each frame is ~33ms
                warmup_delay = camera_config.denoise_frames * 0.033
                self.logger.debug(f"Temporal denoise warmup: skipping {camera_config.denoise_frames} frames ({warmup_delay:.2f}s)")
                time.sleep(warmup_delay)
            
            # Trigger autofocus cycle if enabled
            # This ensures sharp images by focusing before capture
            if camera_config.autofocus_on_capture:
                self.logger.debug(f"Triggering autofocus for camera {camera_config.camera_index}")
                success = picam2.autofocus_cycle()
                if success:
                    self.logger.debug(f"Autofocus succeeded")
                else:
                    self.logger.warning(f"Autofocus failed for camera {camera_config.camera_index}")
            
            # Wait for auto-exposure to stabilize
            # Timeout allows AE to converge for proper exposure
            if camera_config.timeout > 0:
                self.logger.debug(f"Waiting {camera_config.timeout}ms for AE stabilization")
                time.sleep(camera_config.timeout / 1000.0)
            
            # Capture image directly to file with metadata
            # YUV420→JPEG is done efficiently by libcamera/picamera2
            # No manual PIL conversion needed
            self.logger.info(f"Capturing image to: {output_path}")
            
            # Use request-based capture to get metadata and save files
            request = picam2.capture_request()
            try:
                # Extract metadata first
                metadata = request.get_metadata()
                
                if camera_config.raw:
                    # Multi-format capture: save both JPEG and raw buffer
                    # Raw buffer contains full sensor data for archival preservation
                    # JPEG provides quick viewing/preview
                    
                    # Generate raw filename (.raw extension for now due to picamera2 DNG bug)
                    raw_path = Path(str(output_path).rsplit('.', 1)[0] + '.raw')
                    
                    # Save JPEG first
                    request.save("main", str(output_path))
                    self.logger.debug(f"Saved JPEG: {Path(output_path).name}")
                    
                    # Save raw buffer directly (workaround for picamera2 save_dng bug)
                    # picamera2 0.3.33 has a bug: Picamera2Camera.__init__() signature mismatch
                    # Saving raw sensor data as binary until library is fixed
                    try:
                        raw_buffer = request.make_buffer("raw")
                        with open(raw_path, 'wb') as f:
                            f.write(raw_buffer)
                        self.logger.debug(f"Saved raw buffer: {raw_path.name}")
                        output_path = (str(output_path), str(raw_path))
                    except Exception as e:
                        self.logger.warning(f"Failed to save raw buffer: {e}, continuing with JPEG only")
                        output_path = str(output_path)
                else:
                    # Standard JPEG/PNG capture only
                    request.save("main", str(output_path))
                    self.logger.debug(f"Saved {'JPEG' if use_yuv else 'PNG'} with quality={camera_config.quality}")
                    
            finally:
                request.release()
            
            # Extract relevant metadata for archival documentation
            archival_metadata = self._extract_archival_metadata(metadata)
            self.logger.debug(f"Captured metadata: {archival_metadata}")
            
            self.logger.info(f"Image captured successfully: {output_path}")
            
            # Note: We keep the camera running for better performance on next capture
            # It will be stopped/reconfigured if settings change or in cleanup()
            
            # Return path (can be string or tuple for multi-format) and metadata
            return output_path, archival_metadata
            
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
    
    def reset_camera(self, camera_index: int) -> None:
        """
        Stop, close and evict a camera instance from the cache.

        Called after a capture error to ensure the next request gets a
        fresh Picamera2 instance rather than one left in a broken state.

        Args:
            camera_index: The camera index to reset.
        """
        picam2 = self._cameras.pop(camera_index, None)
        self._last_configs.pop(camera_index, None)
        self._format_mode.pop(camera_index, None)
        if picam2 is not None:
            try:
                if picam2.started:
                    picam2.stop()
                picam2.close()
                self.logger.info(f"Reset camera {camera_index} (evicted from cache)")
            except Exception as e:
                self.logger.warning(f"Error while resetting camera {camera_index}: {e}")

    def apply_controls(self, camera_index: int, controls: dict) -> None:
        """
        Apply picamera2 controls to a running camera without a full capture.

        Used by the settings and focus endpoints to update live camera
        parameters (exposure, colour gains, lens position, etc.) so that
        the next preview frame reflects the new values.

        Does nothing if the camera has not been initialised yet.

        Args:
            camera_index: The camera index.
            controls: Dict of picamera2 control names → values.
        """
        picam2 = self._cameras.get(camera_index)
        if picam2 is None:
            self.logger.debug(
                f"apply_controls: camera {camera_index} not yet open, skipping"
            )
            return
        if not picam2.started:
            self.logger.debug(
                f"apply_controls: camera {camera_index} not started, skipping"
            )
            return
        try:
            picam2.set_controls(controls)
            self.logger.debug(f"Applied controls to camera {camera_index}: {controls}")
        except Exception as e:
            self.logger.warning(f"Failed to apply controls to camera {camera_index}: {e}")

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
