"""
Abstract base class for camera backends.

Defines the interface that all camera backend implementations must follow.
"""

from abc import ABC, abstractmethod
from typing import Optional
from pathlib import Path


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
