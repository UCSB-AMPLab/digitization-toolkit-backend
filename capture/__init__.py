"""
DTK Capture Module

Provides camera capture services with multiple backend support.
"""

from .camera import CameraConfig
from .service import (
    is_camera_connected,
    capture_image,
    single_capture_image,
    dual_capture_image,
    get_backend,
    get_camera_backend,
)
from .backends import CameraBackend, RpicamBackend, Picamera2Backend

__all__ = [
    'CameraConfig',
    'is_camera_connected',
    'capture_image',
    'single_capture_image',
    'dual_capture_image',
    'get_backend',
    'get_camera_backend',
    'CameraBackend',
    'RpicamBackend',
    'Picamera2Backend',
]