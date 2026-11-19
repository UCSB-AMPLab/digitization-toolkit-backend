"""
Abstract base class for camera backends.

Defines the interface that all camera backend implementations must follow.

Version: 0.1.0-beta
"""

from abc import ABC, abstractmethod
from typing import Optional
from pathlib import Path


# Capture formats, as CameraConfig.image_format and the settings route name
# them. There are two on every camera type:
#
#   JPEG      the camera's JPEG is the master.
#   RAW+JPEG  a raw master with a JPEG preview, `<name>_preview.jpg`, where
#             app.core.paths.preview_sidecar_of puts it. On a gphoto2 DSLR
#             the camera writes one .cr2 (imageformat "RAW") and the preview
#             is extracted from it; on a CHDK compact the master is a .dng and
#             the preview is the camera's own JPEG of the same exposure.
#
# "RAW" is the old name for RAW+JPEG and is accepted wherever a format comes
# in - a request, a stored value, a CameraConfig - and read as RAW+JPEG. It is
# never stored or reported. The gphoto2 side of this rule has not been run on
# a T7 since the formats were renamed.
IMAGE_FORMATS = ("JPEG", "RAW+JPEG")
_IMAGE_FORMAT_ALIASES = {"RAW": "RAW+JPEG"}


def normalise_image_format(value) -> Optional[str]:
    """The current name of a capture format, or None when value names none.

    "RAW" becomes "RAW+JPEG"; "JPEG" and "RAW+JPEG" are returned as they
    are. Anything else - None, another string, a value that is not a string -
    is None, and the caller decides whether that means "nothing chosen" or a
    value to refuse.
    """
    if type(value) is not str:
        return None
    value = _IMAGE_FORMAT_ALIASES.get(value, value)
    return value if value in IMAGE_FORMATS else None


# CameraSettings.camera_model is String(255).
_CAMERA_MODEL_MAX = 255
# Strings that name no camera: picamera2's fallback, libgphoto2's generic
# label for a body it cannot name, and the CameraConfig class name the
# capture routes once recorded in its place (NEH-329).
_MODEL_PLACEHOLDERS = {"unknown", "usb ptp class camera", "cameraconfig"}


def normalise_camera_model(value) -> Optional[str]:
    """A model string fit to store, or None when it names no camera.

    Whitespace is stripped; an empty string, a placeholder or anything that
    is not a string is None; the rest is cut to the column's 255 characters.
    """
    if not isinstance(value, str):
        return None
    model = value.strip()
    if not model or model.lower() in _MODEL_PLACEHOLDERS:
        return None
    return model[:_CAMERA_MODEL_MAX]


class CameraBackend(ABC):
    """
    Abstract base class for camera capture backends.
    
    All backend implementations (subprocess, picamera2, etc.) must implement
    these methods to provide a consistent interface for the capture service.
    """
    
    def __init__(self, logger):
        """
        Initialize the camera backend.
        
        Args:
            logger: Logger instance for logging operations.
        """
        self.logger = logger
    
    @abstractmethod
    def is_camera_connected(self, camera_index: int = 0) -> bool:
        """
        Check if a camera is connected and available.
        
        Args:
            camera_index (int): The index of the camera to check.
            
        Returns:
            bool: True if the camera is connected, False otherwise.
        """
        pass
    
    @abstractmethod
    def capture_image(
        self,
        output_path: Path,
        camera_config,
        capture_output: bool = False
    ) -> str:
        """
        Capture a single image to the specified path.
        
        Args:
            output_path (Path): Full path where the image should be saved.
            camera_config: CameraConfig object with capture settings.
            capture_output (bool): Whether to capture stderr/stdout for debugging.
            
        Returns:
            str: Path to the captured image file.
            
        Raises:
            RuntimeError: If capture fails.
        """
        pass
    
    @abstractmethod
    def supports_streaming(self) -> bool:
        """
        Check if this backend supports video streaming/preview.
        
        Returns:
            bool: True if streaming is supported, False otherwise.
        """
        pass
    
    @abstractmethod
    def supports_live_adjustment(self) -> bool:
        """
        Check if this backend supports adjusting settings on the fly.
        
        Returns:
            bool: True if live adjustment is supported, False otherwise.
        """
        pass
    
    def get_backend_name(self) -> str:
        """
        Get a human-readable name for this backend.
        
        Returns:
            str: Backend name (e.g., "rpicam-subprocess", "picamera2").
        """
        return self.__class__.__name__
    
    def cleanup(self):
        """
        Cleanup any resources held by the backend.

        This is called when switching backends or shutting down.
        Subclasses should override if they need to release resources.
        """
        pass

    def list_devices(self) -> list:
        """
        List all camera devices currently detected by this backend.

        Returns a list of dicts with at minimum these keys:
            index (int): Camera index used in all capture calls.
            model (str): Human-readable model/product name.
            hardware_id (str): Stable ID derived from hardware identity
                (not the USB index, which changes on power-cycle).
            serial (str | None): Serial number if available.
            location (str | None): Physical connection info (bus/port).
            has_aperture_control (bool): True if aperture can be set via API.
            supports_zoom (bool): True if digital/ScalerCrop zoom is available.

        A backend with stepped optical zoom (capabilities "zoom_steps") adds:
            zoom_steps (int | None): How many zoom steps the lens has, or None
                until the camera has reported it.
            zoom_step (int | None): The step the backend last confirmed by
                reading it back from the camera, or None until known.

        A backend with focus lock (capabilities "focus_lock") adds:
            focus_locked (bool | None): True while the camera holds a focus
                lock it confirmed, False otherwise, and None until the body
                is in shooting mode.
            focus_distance (int | None): The distance in mm the camera
                reported for that lock, or None.
            focus_stale (bool): The zoom moved after the last lock.
            focus_restore_failed (bool): A stored distance was sent on
                entering shooting mode and the camera did not report it back.

        The default implementation returns an empty list. Backends should
        override to provide accurate enumeration for their hardware type.
        """
        return []

    def camera_model(self, camera_index: int) -> Optional[str]:
        """The model of the camera at this index, from state already held.

        Called after a capture to record which camera took the image, so it
        must not talk to the hardware: no enumeration, no rescan, no device
        conversation. A backend answers from what it cached when it last
        enumerated or opened the camera, and returns None when it holds
        nothing for the index. The default holds nothing.

        Returns:
            str | None: The model, normalised by normalise_camera_model.
        """
        return None

    def rescan(self) -> list:
        """
        Re-detect hardware and drop stale state, then enumerate.

        Called when a body has been power-cycled, unplugged or moved between
        USB ports mid-session and the backend's cached view of the hardware
        has to be rebuilt without restarting the process.

        Backends without persistent sessions have no stale state to drop, so
        this just enumerates. Backends that hold open device sessions should
        override to reconcile them against the fresh detection first.

        Returns:
            list: The same device dicts list_devices() returns.
        """
        return self.list_devices()

    def capture_preview(self, camera_index: int) -> bytes:
        """
        Capture a single live-preview frame and return raw JPEG bytes.

        Not saved to disk. Intended for frontend polling loops.

        Returns:
            JPEG bytes of the preview frame.

        Raises:
            NotImplementedError: If this backend does not support live preview.
            RuntimeError: If the capture fails.
        """
        raise NotImplementedError(
            f"{self.get_backend_name()} does not support live preview"
        )

    def get_capabilities(self) -> dict:
        """
        Return a dict of boolean capability flags for this backend.

        Keys:
            live_preview (bool): Can return JPEG frames for frontend polling.
            continuous_preview (bool): The capture screen may poll preview frames
                continuously.
            focus_control (bool): Supports `get_focus` / `set_focus`.
            live_controls (bool): Supports `set_camera_controls` (live picamera2 controls).
            zoom (bool): Supports ScalerCrop-based digital zoom.
            zoom_steps (bool): Supports stepped optical zoom, set by step
                number through `set_zoom_step` (CHDK compacts). Not the same
                thing as `zoom`, which is digital.
            focus_lock (bool): Supports `lock_focus` / `unlock_focus`, an
                autofocus taken once and held (CHDK compacts).
            autofocus_calibration (bool): Can run AF + WB calibration routines.
            dslr_settings (bool): Exposes ISO / shutter speed / aperture via PTP.
            image_formats (list): The capture formats (IMAGE_FORMATS) the
                settings route accepts for this backend: ["JPEG",
                "RAW+JPEG"] on gphoto2 and CHDK. Without dslr_settings the
                route takes the format alone (CHDK). Empty when the backend
                offers no choice.

        Subclasses should override this to reflect their actual capabilities.
        """
        return {
            "live_preview": False,
            "continuous_preview": False,
            "focus_control": False,
            "live_controls": False,
            "zoom": False,
            "zoom_steps": False,
            "focus_lock": False,
            "autofocus_calibration": False,
            "dslr_settings": False,
            "image_formats": [],
        }

    def capture_image_format(self, camera_index: int) -> Optional[str]:
        """The capture format stored for this camera, for the capture's CameraConfig.

        None, the default, leaves CameraConfig.image_format unset. A backend
        whose format is a choice per capture rather than a setting on the
        camera (CHDK) returns the format stored for the body at the index.
        """
        return None
