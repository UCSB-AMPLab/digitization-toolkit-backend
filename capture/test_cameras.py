"""
Tests for camera capture functionality.
"""

import os
import pytest
from pathlib import Path

# Import from capture module (using package exports)
from capture import CameraConfig, dual_capture_image


@pytest.mark.camera
@pytest.mark.slow
def test_dual_capture_image(override_projects_root, skip_if_single_camera):
    """
    Test dual camera capture with both cameras.
    
    This test requires two cameras to be connected.
    """
    project_name = "test_dual_capture"
    
    cam_config = CameraConfig(
        camera_index=0,
        awb="auto",
        vflip=True,
        hflip=True,
        zsl=True,
        raw=False,  # Set to False for faster testing
        img_size=(1920, 1080),  # Smaller size for faster testing
        quality=85,
    )
    
    cam1 = CameraConfig(**{**cam_config.to_dict(), 'camera_index': 0})
    cam2 = CameraConfig(**{**cam_config.to_dict(), 'camera_index': 1})

    
    # Capture images
    path1, path2 = dual_capture_image(
        project_name,
        cam1_config=cam1,
        cam2_config=cam2,
        include_resolution=True,
    )
    
    # Verify files were created
    assert os.path.exists(path1), f"Camera 0 image not created: {path1}"
    assert os.path.exists(path2), f"Camera 1 image not created: {path2}"
    
    # Verify files have content
    assert os.path.getsize(path1) > 0, "Camera 0 image is empty"
    assert os.path.getsize(path2) > 0, "Camera 1 image is empty"
    
    # Verify filenames contain camera indices
    assert "_c0_" in path1 or "_c0." in path1, "Camera 0 filename doesn't indicate camera index"
    assert "_c1_" in path2 or "_c1." in path2, "Camera 1 filename doesn't indicate camera index"
    
    print(f"\n✓ Dual capture successful:")
    print(f"  Camera 0: {path1} ({os.path.getsize(path1) / 1024:.1f} KB)")
    print(f"  Camera 1: {path2} ({os.path.getsize(path2) / 1024:.1f} KB)")
    
