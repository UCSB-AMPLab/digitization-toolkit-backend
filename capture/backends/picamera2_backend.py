"""
Picamera2-based camera backend.

This backend uses the official picamera2 Python library which provides
direct access to libcamera. It supports advanced features like streaming,
live preview, and dynamic settings adjustment.

The unzoomed crop
-----------------
"Unzoomed" here means the *configuration's own default ScalerCrop*, not the
pixel array. libcamera's rpi pipeline shapes the initial crop to the output's
aspect ratio, so a 16:9 output configuration taken off a 4:3 readout starts on
a centred 16:9 band and can never reach the full array. Preview, zoom and
still therefore all reference that default rectangle: ``_ensure_configured``
reads it from ``camera_controls["ScalerCrop"][2]`` and pins it explicitly on
every (re)configure, ``apply_zoom`` crops inside it, and a still waits for a
frame actually exposed at it. Bench item B14 confirms the band on the
appliance.
"""

import os
import sys
import tempfile
import time
import threading
from pathlib import Path
from typing import Optional, Tuple

# Only import picamera2/libcamera on Linux (inside Docker/Raspberry Pi).
# Catch ValueError too: a numpy ABI mismatch in simplejpeg raises ValueError
# at import time on some Pi OS + pixi env combinations.
_PICAMERA2_AVAILABLE = False
_PICAMERA2_IMPORT_ERROR = None
Picamera2 = None
Transform = None
if sys.platform == "linux":
    try:
        from picamera2 import Picamera2
        from libcamera import Transform
        _PICAMERA2_AVAILABLE = True
    except (ImportError, ValueError) as _picamera2_err:
        # The except-as name is unbound once this block exits, so persist the
        # message for the error raised when the backend is actually used.
        # repr keeps the exception type visible even with an empty message.
        _PICAMERA2_IMPORT_ERROR = repr(_picamera2_err)
        Picamera2 = None
        Transform = None
        _PICAMERA2_AVAILABLE = False



from .base import CameraBackend
from ..camera import CameraConfig, IMG_SIZES
from ..utils import atomic_write

# Per-value slack, in sensor pixels, when comparing a reported ScalerCrop with
# the rectangle that was asked for. libcamera aligns crop rectangles to
# hardware granularity, so an accepted crop can come back a few pixels off in
# any of the four values. Small enough to reject the 1.005x that apply_zoom
# permits, which moves the width of a 4656-wide array by 24 px.
_CROP_ALIGN_PX = 16

# Frames a still may discard while waiting for the unzoomed reset to take
# effect. Controls apply with a pipeline delay, so the first request handed
# back can still be a frame completed under the preview's zoom.
_UNZOOMED_MAX_FRAMES = 8


def _crop_rect(crop) -> Tuple[int, int, int, int]:
    """(x, y, width, height) of a ScalerCrop value, however the build reports it.

    picamera2 normally hands rectangles back as (x, y, w, h) sequences, but a
    libcamera Rectangle object with x/y/width/height attributes is accepted
    too, so the crop comparison never fails on the shape of the value.
    """
    width = getattr(crop, "width", None)
    height = getattr(crop, "height", None)
    if width is not None and height is not None:
        return (int(getattr(crop, "x", 0)), int(getattr(crop, "y", 0)),
                int(width), int(height))
    return (int(crop[0]), int(crop[1]), int(crop[2]), int(crop[3]))


def _crop_size(crop) -> Tuple[int, int]:
    """(width, height) of a ScalerCrop value, however the build reports it."""
    return _crop_rect(crop)[2:]


def _crops_match(a, b, tolerance: int = _CROP_ALIGN_PX) -> bool:
    """True when two rectangles agree on all four values within ``tolerance``.

    All four, not just the dimensions: a crop of the right size in the wrong
    place is a different field of view.
    """
    return all(abs(int(a[i]) - int(b[i])) <= tolerance for i in range(4))


def preview_stream_size(
    img_size: Tuple[int, int], max_width: int = 1280
) -> Tuple[int, int]:
    """Size of the preview stream that rides alongside a still of ``img_size``.

    The preview keeps the still's aspect ratio so the operator frames the page
    against exactly what the capture records. Both dimensions are rounded down
    to even numbers because YUV420 subsamples chroma 2x2, and the result is
    never larger than the still itself.

    Args:
        img_size: The still's (width, height).
        max_width: Widest preview to produce.

    Returns:
        The preview (width, height); ``img_size`` unchanged when the still is
        already no wider than ``max_width``.
    """
    width, height = int(img_size[0]), int(img_size[1])
    if width <= max_width:
        return (width, height)

    scaled_height = (height * max_width) // width
    even_width = max_width - (max_width % 2)
    even_height = scaled_height - (scaled_height % 2)
    return (max(even_width, 2), max(even_height, 2))


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
            if sys.platform != "linux":
                raise RuntimeError("Picamera2Backend requires Linux (Raspberry Pi OS)")
            # On a real Pi the import failed for a concrete reason (missing
            # system package, numpy ABI mismatch, ...) — report that, not a
            # misleading claim about the OS.
            raise RuntimeError(f"picamera2 failed to import: {_PICAMERA2_IMPORT_ERROR}")
        
        super().__init__(logger)
        self._cameras = {}  # Cache of initialized Picamera2 instances
        self._camera_info = None
        self._last_configs = {}  # Track last configuration for each camera
        self._format_mode = {}  # Track format mode per camera (YUV420 vs RGB888)
        # The configuration's own default ScalerCrop per camera - see the
        # module docstring. Preview, zoom and still all reference it.
        self._unzoomed_crop: dict = {}
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

            # Build stable hardware ID - same logic as CameraRegistry (picamera2 path)
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

    @staticmethod
    def _uses_yuv(camera_config) -> bool:
        """True when the capture goes out as YUV420 rather than RGB888.

        YUV420 is faster and uses less memory for JPEG; RGB888 is needed for
        PNG or whenever a raw/DNG stream is also requested.
        """
        return camera_config.encoding in ["jpg", "jpeg"] and not camera_config.raw

    def _still_config_args(self, camera_config) -> dict:
        """Build the create_still_configuration() keyword arguments.

        Every still configuration also declares a ``lores`` stream sized by
        ``preview_stream_size``. The lores stream shares the sensor mode and
        the ScalerCrop of ``main``, so a preview read from it shows exactly the
        field of view the capture records, and a preview and a still of the
        same size need only one configuration between them.
        """
        use_yuv = self._uses_yuv(camera_config)

        config_args = {
            "main": {
                "size": camera_config.img_size,
                "format": "YUV420" if use_yuv else "RGB888"
            },
            "lores": {
                "size": preview_stream_size(camera_config.img_size),
                "format": "YUV420",
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

        return config_args

    def _invalidate_camera_caches(self, camera_index: int) -> None:
        """Drop everything cached about a camera's current configuration.

        The three caches describe one configuration between them, so they are
        always dropped together and always *before* the configuration they
        describe is disturbed. Anything that reconfigures a body calls this
        first, so a failure part-way through leaves no entry claiming a mode
        that is no longer in force.
        """
        self._last_configs.pop(camera_index, None)
        self._format_mode.pop(camera_index, None)
        self._unzoomed_crop.pop(camera_index, None)

    def _default_crop(self, picam2) -> Optional[Tuple[int, int, int, int]]:
        """The ScalerCrop libcamera starts the *current* configuration on.

        picamera2 exposes each control as a (min, max, default) tuple that
        reflects the configuration in force, so the default is the reachable
        unzoomed rectangle for this output aspect. On a build that does not
        report the control, falls back to the mode's ScalerCropMaximum
        property, and only then to the whole pixel array.

        Returns:
            (x, y, width, height), or None if neither source is available.
        """
        controls = getattr(picam2, "camera_controls", None)
        entry = controls.get("ScalerCrop") if hasattr(controls, "get") else None
        if entry is not None:
            try:
                return _crop_rect(entry[2])
            except (IndexError, TypeError, ValueError) as e:
                self.logger.warning(f"Unreadable ScalerCrop control range: {e}")

        # No control range: the mode's own maximum crop is the reachable
        # rectangle (a banded mode cannot deliver the pixel array), so it
        # comes before the pixel array as a fallback.
        crop_maximum = picam2.camera_properties.get('ScalerCropMaximum')
        if crop_maximum is not None:
            try:
                return _crop_rect(crop_maximum)
            except (IndexError, TypeError, ValueError) as e:
                self.logger.warning(f"Unreadable ScalerCropMaximum: {e}")

        pixel_array_size = picam2.camera_properties.get('PixelArraySize')
        if pixel_array_size is None:
            return None
        return (0, 0, int(pixel_array_size[0]), int(pixel_array_size[1]))

    def _unzoomed_rect(self, picam2, camera_index: int) -> Optional[Tuple[int, int, int, int]]:
        """The stored unzoomed rectangle, computing and caching it if unset."""
        rect = self._unzoomed_crop.get(camera_index)
        if rect is None:
            rect = self._default_crop(picam2)
            if rect is not None:
                self._unzoomed_crop[camera_index] = rect
        return rect

    def _ensure_configured(self, picam2, camera_config) -> bool:
        """Configure (only when something changed), start, set quality, pin the crop.

        The reconfigure test keys on the CameraConfig fields that alter the
        stream layout, so paths that differ only in AF/AE/quality settings -
        a preview poll and a still of the same size - reuse the running
        configuration and preserve its AE/AF state.

        On a (re)configure the configuration's own default ScalerCrop is read
        and pinned explicitly, so preview, zoom and still all start from the
        same stated rectangle rather than from libcamera's implicit one. A
        cached configuration is left alone, keeping whatever zoom the operator
        set.

        The three per-camera caches are dropped before the camera is touched
        and recorded only once the new configuration is fully in force, so a
        failure anywhere in between leaves nothing behind: the next call
        reconfigures from scratch instead of reusing a target rectangle that
        describes a mode the body is no longer in.

        Args:
            picam2: The Picamera2 instance for this body.
            camera_config: CameraConfig describing the wanted configuration.

        Returns:
            True if the camera was reconfigured by this call.
        """
        use_yuv = self._uses_yuv(camera_config)
        still_config = picam2.create_still_configuration(
            **self._still_config_args(camera_config)
        )

        last_config = self._last_configs.get(camera_config.camera_index)
        last_format = self._format_mode.get(camera_config.camera_index)
        needs_reconfigure = (
            last_config is None or
            last_format != use_yuv or
            last_config.img_size != camera_config.img_size or
            last_config.raw != camera_config.raw or
            last_config.hflip != camera_config.hflip or
            last_config.vflip != camera_config.vflip or
            last_config.buffer_count != camera_config.buffer_count
        )

        if needs_reconfigure:
            self._invalidate_camera_caches(camera_config.camera_index)

            if picam2.started:
                self.logger.debug(f"Stopping camera {camera_config.camera_index} to reconfigure")
                picam2.stop()

            picam2.configure(still_config)
            self.logger.debug(f"Camera {camera_config.camera_index} configured: {camera_config.img_size}, format={'YUV420' if use_yuv else 'RGB888'}")
        else:
            self.logger.debug(f"Camera {camera_config.camera_index} using cached configuration")

        # Start camera if not already running
        if not picam2.started:
            picam2.start()
            self.logger.debug(f"Camera {camera_config.camera_index} started")

        # Set JPEG quality via options (applies to capture_file and to
        # CompletedRequest.save)
        picam2.options["quality"] = camera_config.quality

        if needs_reconfigure:
            rect = self._unzoomed_rect(picam2, camera_config.camera_index)
            if rect is None:
                self.logger.warning(
                    f"Camera {camera_config.camera_index}: no ScalerCrop default "
                    "available; zoom and the still's frame check are unavailable"
                )
            else:
                picam2.set_controls({"ScalerCrop": rect})
                self.logger.debug(
                    f"Camera {camera_config.camera_index} unzoomed crop pinned: {rect}"
                )

            # The configuration is in force only now; recording earlier would
            # let a failed start() or pin be mistaken for a working camera.
            self._last_configs[camera_config.camera_index] = camera_config
            self._format_mode[camera_config.camera_index] = use_yuv

        return needs_reconfigure

    def _capture_unzoomed_request(self, picam2, target_rect, max_frames: int = _UNZOOMED_MAX_FRAMES):
        """Return a completed request whose frame was taken at ``target_rect``.

        Zoom is preview-only, so a still resets ScalerCrop to the
        configuration's unzoomed rectangle first. That reset is not enough on
        its own: libcamera applies controls with a pipeline delay and hands
        back frames that completed earlier, so the first request after the
        reset can still carry the preview's zoomed crop. Discard requests until
        the metadata reports a crop matching the target on all four values
        within the alignment slack, or reports no crop at all (a build that
        does not surface ScalerCrop).

        Args:
            picam2: The Picamera2 instance, already started.
            target_rect: (x, y, width, height) the frame must have been taken at.
            max_frames: How many requests may be discarded before giving up.

        Returns:
            The accepted CompletedRequest; the caller owns its release().

        Raises:
            RuntimeError: If no matching frame arrives within max_frames.
        """
        target = _crop_rect(target_rect)
        picam2.set_controls({"ScalerCrop": target})

        skipped = 0
        for _ in range(max_frames):
            request = picam2.capture_request()
            crop = request.get_metadata().get("ScalerCrop")
            if crop is None or _crops_match(_crop_rect(crop), target):
                if skipped:
                    self.logger.debug(
                        f"Discarded {skipped} queued frame(s) before an unzoomed capture"
                    )
                return request
            request.release()
            skipped += 1

        raise RuntimeError(
            f"could not obtain an unzoomed capture at {target} after {max_frames} frames"
        )

    def _capture_image_locked(
        self,
        output_path: Path,
        camera_config,
        capture_output: bool = False
    ) -> str:
        """Internal capture implementation - must be called with the camera lock held."""
        try:
            picam2 = self._get_camera(camera_config.camera_index)

            use_yuv = self._uses_yuv(camera_config)
            needs_reconfigure = self._ensure_configured(picam2, camera_config)

            # Apply controls
            controls = self._config_to_picamera2_controls(camera_config)

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
            # YUV420->JPEG is done efficiently by libcamera/picamera2
            # No manual PIL conversion needed
            self.logger.info(f"Capturing image to: {output_path}")

            # Reset ScalerCrop to the configuration's unzoomed rectangle -
            # zoom is preview-only - and take the first frame actually exposed
            # at it, not one the pipeline had already completed under the
            # preview's zoom.
            _unzoomed_rect = self._unzoomed_rect(picam2, camera_config.camera_index)
            if _unzoomed_rect is not None:
                request = self._capture_unzoomed_request(picam2, _unzoomed_rect)
            else:
                # Nothing to reset to and nothing to check against; take the
                # next request as it comes.
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
                    
                    # Save JPEG first (durable: temp + fsync + atomic replace)
                    atomic_write(output_path, lambda tmp: request.save("main", tmp))
                    self.logger.debug(f"Saved JPEG: {Path(output_path).name}")

                    # Save raw buffer directly (workaround for picamera2 save_dng bug)
                    # picamera2 0.3.33 has a bug: Picamera2Camera.__init__() signature mismatch
                    # Saving raw sensor data as binary until library is fixed
                    try:
                        raw_buffer = request.make_buffer("raw")
                        atomic_write(raw_path, lambda tmp: Path(tmp).write_bytes(raw_buffer))
                        self.logger.debug(f"Saved raw buffer: {raw_path.name}")
                        output_path = (str(output_path), str(raw_path))
                    except Exception as e:
                        self.logger.warning(f"Failed to save raw buffer: {e}, continuing with JPEG only")
                        output_path = str(output_path)
                else:
                    # Standard JPEG/PNG capture only (durable: temp + fsync + atomic replace)
                    atomic_write(output_path, lambda tmp: request.save("main", tmp))
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
    
    def capture_preview(
        self,
        camera_index: int,
        img_size: Optional[Tuple[int, int]] = None,
        tmp_path: Optional[Path] = None,
    ) -> bytes:
        """Capture one live-preview frame from the still's own configuration.

        The frame comes off the ``lores`` stream that ``_still_config_args``
        declares on every still configuration. That stream shares the sensor
        mode and the ScalerCrop of ``main``, which is what makes the preview's
        field of view the capture's: the operator frames the page against
        exactly what a still would record, and no reconfiguration happens
        between a poll and a capture of the same size.

        Nothing here disturbs the live camera state: no autofocus cycle, no
        AE stabilisation wait, no denoise warmup and no ScalerCrop change, so
        whatever zoom the operator set stays in force.

        Args:
            camera_index: The camera index.
            img_size: The still size whose configuration to preview from.
                Defaults to the medium preset.
            tmp_path: Where to write the encoded frame. A caller that owns a
                stable per-body path (the capture service) passes it in;
                otherwise a temporary file is used and removed.

        Returns:
            JPEG bytes of the preview frame.

        Raises:
            RuntimeError: If the capture fails.
        """
        own_tmp = tmp_path is None
        if own_tmp:
            handle, generated = tempfile.mkstemp(
                prefix=f"dtk_preview_frame_c{camera_index}_", suffix=".jpg"
            )
            os.close(handle)
            tmp_path = Path(generated)
        else:
            tmp_path = Path(tmp_path)

        try:
            lock = self._get_camera_lock(camera_index)
            with lock:
                picam2 = self._get_camera(camera_index)

                preview_config = CameraConfig(
                    camera_index=camera_index,
                    img_size=img_size or IMG_SIZES["medium"],
                    autofocus_on_capture=False,  # Skip AF cycle for live preview
                    timeout=0,                   # No AE stabilisation wait
                    denoise_frames=0,            # No temporal denoise warmup
                    encoding="jpg",
                    raw=False,
                    quality=75,                  # Smaller payload for polling
                )
                self._ensure_configured(picam2, preview_config)

                request = picam2.capture_request()
                try:
                    request.save("lores", str(tmp_path))
                finally:
                    request.release()

                return tmp_path.read_bytes()
        except Exception as e:
            self.logger.error(f"Failed to capture preview frame: {e}")
            raise RuntimeError(f"Picamera2 preview capture failed: {e}")
        finally:
            if own_tmp:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

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
        self._invalidate_camera_caches(camera_index)
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
        polling is active - it blocks until any in-flight preview completes,
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

            # Drop the caches before the body's configuration changes: the AF
            # cycle can raise, and a target left over from the previous mode
            # would be applied to this one.
            self._invalidate_camera_caches(camera_index)

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

            # Leave the camera stopped. The caches were cleared on the way
            # in, so the next capture_image() / preview call reconfigures
            # cleanly whether or not this routine got here.
            picam2.stop()

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
        Picamera2 instance - doing so would corrupt the service's cached handle.

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

            # Drop the caches before the body's configuration changes: the
            # metadata reads below can raise, and this preview configuration
            # has its own unzoomed rectangle.
            self._invalidate_camera_caches(camera_index)

            if picam2.started:
                picam2.stop()

            # Preview config is sufficient for metadata reads - much faster than still
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

            return result

    def apply_zoom(self, camera_index: int, zoom_factor: float) -> None:
        """
        Apply digital zoom via ScalerCrop on the running preview stream.

        Sets the sensor Region of Interest to a crop of 1/zoom_factor of the
        configuration's own unzoomed rectangle, centred inside it (see the
        module docstring - on a 16:9 configuration off a 4:3 readout that
        rectangle is a band, not the pixel array). zoom_factor=1.0 restores
        that rectangle exactly. Zoom is preview-only: capture_image() resets
        ScalerCrop to it and then waits for a frame actually exposed at it.

        Takes the per-camera lock for the whole body, so a zoom change cannot
        interleave with that frame selection and re-crop the sensor between
        the reset and the request the still keeps.

        Args:
            camera_index: The camera index.
            zoom_factor:  Zoom multiplier in the range [1.0, 8.0].
        """
        lock = self._get_camera_lock(camera_index)
        with lock:
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
            unzoomed = self._unzoomed_rect(picam2, camera_index)
            if unzoomed is None:
                self.logger.warning(
                    f"apply_zoom: no unzoomed ScalerCrop known for camera {camera_index}"
                )
                return

            base_x, base_y, base_w, base_h = unzoomed
            crop_w = int(base_w / zoom)
            crop_h = int(base_h / zoom)
            crop_x = base_x + (base_w - crop_w) // 2
            crop_y = base_y + (base_h - crop_h) // 2
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
            controls: Dict of picamera2 control names -> values.
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
