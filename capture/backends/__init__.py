"""
Camera backend abstraction layer.

This module provides a unified interface for different camera backends
(subprocess/rpicam-still and picamera2), allowing seamless switching
between implementations.
"""

from .base import CameraBackend
from .subprocess_backend import RpicamBackend
from .picamera2_backend import Picamera2Backend
from .gphoto2_backend import GPhoto2Backend

__all__ = ['CameraBackend', 'RpicamBackend', 'Picamera2Backend', 'GPhoto2Backend']
