"""
Camera backend abstraction layer.

This module provides a unified interface for the camera backends
(subprocess/rpicam-still, picamera2, gphoto2 and CHDK), allowing switching
between implementations.

Version: 0.1.0-beta
"""

from .base import CameraBackend
from .subprocess_backend import RpicamBackend
from .gphoto2_backend import GPhoto2Backend
from .chdk_backend import ChdkBackend

# picamera2 depends on simplejpeg which can raise ValueError on numpy ABI
# mismatch at import time (e.g. pixi env vs system numpy).  Guard the import
# so a broken picamera2 install doesn't prevent the whole capture module from
# loading when CAMERA_BACKEND=gphoto2 or subprocess.
try:
    from .picamera2_backend import Picamera2Backend
except (ImportError, ValueError):
    class Picamera2Backend:  # type: ignore[no-redef]
        """Stub used when picamera2 is unavailable."""
        def __init__(self, *a, **kw):
            raise RuntimeError(
                "picamera2 is not available on this system "
                "(missing library or numpy ABI mismatch). "
                "Set CAMERA_BACKEND=gphoto2 or CAMERA_BACKEND=subprocess."
            )

__all__ = [
    'CameraBackend', 'RpicamBackend', 'Picamera2Backend', 'GPhoto2Backend',
    'ChdkBackend',
]
