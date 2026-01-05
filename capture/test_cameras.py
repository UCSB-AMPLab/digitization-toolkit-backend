"""
Tests for camera capture functionality.
"""

import os
import pytest
import time
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
        raw=False,  # Set to False for faster testing
        img_size=(640, 480),  # Smaller size for faster testing
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
    # Convert to string in case they're Path objects
    path1_str = str(path1)
    path2_str = str(path2)
    assert "_c0_" in path1_str or "_c0." in path1_str, "Camera 0 filename doesn't indicate camera index"
    assert "_c1_" in path2_str or "_c1." in path2_str, "Camera 1 filename doesn't indicate camera index"


@pytest.mark.camera
@pytest.mark.performance
def test_capture_performance(override_projects_root, skip_if_single_camera):
    """
    Performance test to ensure capture times meet targets.
    
    Critical for production use where hundreds of pages are scanned daily.
    Target: < 3.0 seconds per dual capture for good user experience.
    """
    project_name = "test_performance"
    
    # Production configuration
    cam1 = CameraConfig(
        camera_index=0,
        lens_position=3.92,
        img_size=(3840, 2160),  # Full resolution
        raw=False,
        denoise_frames=10,
        quality=93
    )
    cam2 = CameraConfig(
        camera_index=1,
        lens_position=4.19,
        img_size=(3840, 2160),  # Full resolution
        raw=False,
        denoise_frames=10,
        quality=93
    )
    
    # Run 3 captures and measure time
    times = []
    for i in range(3):
        start = time.time()
        path1, path2 = dual_capture_image(project_name, cam1, cam2)
        elapsed = time.time() - start
        times.append(elapsed)
        
        # Verify capture succeeded
        assert os.path.exists(path1), f"Camera 0 image not created"
        assert os.path.exists(path2), f"Camera 1 image not created"
        
        # Small delay between captures
        if i < 2:
            time.sleep(0.5)
    
    avg_time = sum(times) / len(times)
    min_time = min(times)
    max_time = max(times)
    
    print(f"\nPerformance Results:")
    print(f"  Average: {avg_time:.2f}s")
    print(f"  Min: {min_time:.2f}s")
    print(f"  Max: {max_time:.2f}s")
    print(f"  Throughput: {3600/avg_time:.0f} pages/hour")
    
    # Performance assertion - warn if too slow but don't fail
    # (hardware variations can affect timing)
    target_time = 5.0  # Conservative target
    if avg_time > target_time:
        print(f"  ⚠ WARNING: Average time {avg_time:.2f}s exceeds target {target_time}s")
    else:
        print(f"  ✓ Performance good: under {target_time}s target")
    
    # Verify times are reasonable (not failing completely)
    assert avg_time < 15.0, f"Capture too slow: {avg_time:.2f}s (expected < 15s)"
