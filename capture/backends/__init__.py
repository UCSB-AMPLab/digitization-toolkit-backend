"""
Camera backend abstraction layer.

This module provides a unified interface for different camera backends
(subprocess/rpicam-still and picamera2), allowing seamless switching
between implementations.
"""

from .base import CameraBackend
from .subprocess_backend import RpicamBackend
from .gphoto2_backend import GPhoto2Backend

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

__all__ = ['CameraBackend', 'RpicamBackend', 'Picamera2Backend', 'GPhoto2Backend']
