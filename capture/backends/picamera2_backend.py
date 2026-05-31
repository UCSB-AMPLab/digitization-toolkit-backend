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

    def list_devices(self) -> list:
        """Enumerate all libcamera/picamera2 cameras and return device metadata.

        Builds stable hardware IDs using the libcamera ``Id`` path (i2c bus
        address), matching the logic in ``CameraRegistry``.
        """
        try:
            camera_info = self._get_camera_info()
        except Exception:
            return []

        result = []
        for idx, info in enumerate(camera_info):
            model = info.get("Model", "unknown")
            camera_id = info.get("Id", "")
            location = info.get("Location", "")

            # Build stable hardware ID — same logic as CameraRegistry (picamera2 path)
            if camera_id:
                id_parts = camera_id.split("/")
                i2c_part = [p for p in id_parts if p.startswith("i2c@")]
                if i2c_part:
                    identifier = i2c_part[0].replace("i2c@", "")
                    hw_id = f"{model}_{identifier}"
                else:
                    hw_id = f"{model}_{id_parts[-1]}"
            else:
                hw_id = f"{model}_idx{idx}"

            # Aperture control only exposed if camera is already initialized
            has_aperture = False
            if idx in self._cameras:
                try:
                    has_aperture = "Aperture" in self._cameras[idx].camera_controls
                except Exception:
                    pass

            result.append({
                "index": idx,
                "model": model,
                "hardware_id": hw_id,
                "serial": None,         # IMX519 doesn't expose a serial number
                "location": str(location),
                "has_aperture_control": has_aperture,
                "supports_zoom": True,  # ScalerCrop available on all picamera2 cameras
            })

        return result
    
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

            # Reset ScalerCrop to full sensor — zoom is preview-only.
            # Ensures captures always use the full pixel array regardless of
            # whatever zoom the user had applied to the live preview.
            _pixel_array_size = picam2.camera_properties.get('PixelArraySize')
            if _pixel_array_size:
                picam2.set_controls(
                    {"ScalerCrop": (0, 0, _pixel_array_size[0], _pixel_array_size[1])}
                )

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

    def get_capabilities(self) -> dict:
        return {
            "live_preview": True,
            "focus_control": True,
            "live_controls": True,
            "zoom": True,
            "autofocus_calibration": True,
            "dslr_settings": False,
        }

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

    def run_autofocus_calibration(self, camera_index: int, img_size: tuple) -> dict:
        """
        Run an autofocus calibration cycle using the cached Picamera2 instance.

        Acquires the per-camera lock so this is safe to call while preview
        polling is active — it blocks until any in-flight preview completes,
        then holds the lock for the duration of the AF cycle.

        Unlike the legacy ``CameraCalibration`` class, this method does NOT
        open a second ``Picamera2`` instance.  Creating two libcamera
        connections to the same hardware corrupts both handles and leaves the
        service's cached instance in a broken state.

        After the AF cycle the camera is left *stopped* but not closed.
        ``_last_configs`` and ``_format_mode`` are cleared so the next
        preview/capture call knows to reconfigure from scratch.

        Args:
            camera_index: Camera index (0 or 1).
            img_size: Resolution tuple for the still capture configuration.

        Returns:
            Dict with keys: success, lens_position, distance_meters, af_time
        """
        lock = self._get_camera_lock(camera_index)
        with lock:
            picam2 = self._get_camera(camera_index)

            # Reconfigure to high-res still mode for the AF cycle
            if picam2.started:
                picam2.stop()

            still_config = picam2.create_still_configuration(
                main={"size": img_size}
            )
            picam2.configure(still_config)
            picam2.start()
            picam2.set_controls({"AfMode": 1})

            af_start = time.time()
            success = picam2.autofocus_cycle()
            af_time = time.time() - af_start

            result: dict = {
                "success": success,
                "af_time": af_time,
                "lens_position": None,
                "distance_meters": None,
            }

            if success:
                metadata = picam2.capture_metadata()
                lens_position = metadata.get("LensPosition")
                if lens_position is not None:
                    result["lens_position"] = lens_position
                    result["distance_meters"] = (
                        1.0 / lens_position if lens_position > 0 else float("inf")
                    )
                    self.logger.info(
                        f"Autofocus calibration camera {camera_index}: "
                        f"LensPosition={lens_position:.3f} dpt "
                        f"({af_time:.2f}s)"
                    )
            else:
                self.logger.warning(
                    f"Autofocus calibration failed for camera {camera_index} "
                    f"after {af_time:.2f}s"
                )

            # Leave camera stopped; clear cached config so the next
            # capture_image() / preview call reconfigures cleanly.
            picam2.stop()
            self._last_configs.pop(camera_index, None)
            self._format_mode.pop(camera_index, None)

            return result

    def run_white_balance_calibration(
        self, camera_index: int, stabilization_frames: int = 30
    ) -> dict:
        """
        Run a white balance calibration cycle using the cached Picamera2 instance.

        Acquires the per-camera lock, uses a preview configuration to collect
        ``stabilization_frames`` frames of AWB metadata, then reads the
        converged ColourGains.

        Like ``run_autofocus_calibration``, this method does NOT open a second
        Picamera2 instance — doing so would corrupt the service's cached handle.

        After the cycle the camera is left stopped; ``_last_configs`` and
        ``_format_mode`` are cleared so the next request reconfigures cleanly.

        Args:
            camera_index: Camera index (0 or 1).
            stabilization_frames: Frames to wait for AWB to converge (default 30).

        Returns:
            Dict with keys: success, awb_gains, colour_temperature, converged
        """
        lock = self._get_camera_lock(camera_index)
        with lock:
            picam2 = self._get_camera(camera_index)

            if picam2.started:
                picam2.stop()

            # Preview config is sufficient for metadata reads — much faster than still
            preview_config = picam2.create_preview_configuration(
                main={"size": (1920, 1080)}
            )
            picam2.configure(preview_config)
            picam2.start()

            # Enable AWB and let the algorithm converge over several frames
            picam2.set_controls({"AwbEnable": True, "AwbMode": 0})  # 0 = Auto

            awb_gains_history: list = []
            for _ in range(stabilization_frames):
                metadata = picam2.capture_metadata()
                gains = metadata.get("ColourGains")
                if gains:
                    awb_gains_history.append(gains)

            # Read final settled values
            final_metadata = picam2.capture_metadata()
            final_gains = final_metadata.get("ColourGains")
            colour_temp = final_metadata.get("ColourTemperature")

            result: dict = {
                "success": False,
                "awb_gains": None,
                "colour_temperature": None,
                "converged": None,
            }

            if final_gains:
                result["success"] = True
                result["awb_gains"] = list(final_gains)
                result["colour_temperature"] = colour_temp

                # Convergence: variance of last 10 samples < 0.05
                if len(awb_gains_history) >= 10:
                    recent = awb_gains_history[-10:]
                    r_vals = [g[0] for g in recent]
                    b_vals = [g[1] for g in recent]
                    result["converged"] = (
                        max(r_vals) - min(r_vals) < 0.05
                        and max(b_vals) - min(b_vals) < 0.05
                    )

                self.logger.info(
                    f"WB calibration camera {camera_index}: "
                    f"R={final_gains[0]:.3f}, B={final_gains[1]:.3f}"
                    + (f", ~{colour_temp}K" if colour_temp else "")
                )
            else:
                self.logger.warning(
                    f"WB calibration camera {camera_index}: "
                    "no ColourGains in metadata"
                )

            picam2.stop()
            self._last_configs.pop(camera_index, None)
            self._format_mode.pop(camera_index, None)

            return result

    def apply_zoom(self, camera_index: int, zoom_factor: float) -> None:
        """
        Apply digital zoom via ScalerCrop on the running preview stream.

        Sets the sensor Region of Interest to a centred crop of 1/zoom_factor
        of the full pixel array.  zoom_factor=1.0 restores the full sensor.
        Zoom is preview-only: capture_image() always resets ScalerCrop to the
        full sensor before taking the shot.

        Args:
            camera_index: The camera index.
            zoom_factor:  Zoom multiplier in the range [1.0, 8.0].
        """
        picam2 = self._cameras.get(camera_index)
        if picam2 is None:
            self.logger.debug(
                f"apply_zoom: camera {camera_index} not yet open, skipping"
            )
            return
        if not picam2.started:
            self.logger.debug(
                f"apply_zoom: camera {camera_index} not started, skipping"
            )
            return

        zoom = max(1.0, min(float(zoom_factor), 8.0))
        pixel_array_size = picam2.camera_properties.get('PixelArraySize')
        if pixel_array_size is None:
            self.logger.warning(
                f"apply_zoom: PixelArraySize not available for camera {camera_index}"
            )
            return

        sensor_w, sensor_h = pixel_array_size
        crop_w = int(sensor_w / zoom)
        crop_h = int(sensor_h / zoom)
        crop_x = (sensor_w - crop_w) // 2
        crop_y = (sensor_h - crop_h) // 2
        try:
            picam2.set_controls({"ScalerCrop": (crop_x, crop_y, crop_w, crop_h)})
            self.logger.debug(
                f"Camera {camera_index} zoom {zoom:.1f}x: "
                f"ScalerCrop=({crop_x},{crop_y},{crop_w},{crop_h})"
            )
        except Exception as e:
            self.logger.warning(
                f"Failed to apply zoom to camera {camera_index}: {e}"
            )

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
