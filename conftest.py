"""
Pytest configuration and fixtures for DTK Backend tests.
"""

import os
import sys
import pytest
import tempfile
import shutil
from pathlib import Path

backend_dir = Path(__file__).parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))


@pytest.fixture(scope="session")
def backend_root():
    """Return the backend root directory."""
    return Path(__file__).parent


@pytest.fixture(scope="session")
def test_data_dir(backend_root):
    """Return the test data directory."""
    test_dir = backend_root / "test_data"
    test_dir.mkdir(exist_ok=True)
    return test_dir


@pytest.fixture
def temp_project_dir(tmp_path):
    """
    Create a temporary project directory for testing.
    
    Returns a Path object to a temporary directory that will be
    cleaned up after the test completes.
    """
    project_dir = tmp_path / "test_project"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "images" / "main").mkdir(parents=True, exist_ok=True)
    return project_dir


@pytest.fixture
def mock_camera_config():
    """Return a standard CameraConfig for testing."""
    from capture import CameraConfig
    
    return CameraConfig(
        camera_index=0,
        img_size=(1920, 1080),
        timeout=50,
        nopreview=True,
        quality=85,
        awb="auto",
        vflip=False,
        hflip=False,
    )


@pytest.fixture
def dual_camera_configs():
    """Return a tuple of two CameraConfigs for dual capture testing."""
    from capture import CameraConfig
    
    cam1 = CameraConfig(
        camera_index=0,
        img_size=(1920, 1080),
        timeout=50,
        nopreview=True,
        quality=85,
        awb="auto",
        vflip=False,
        hflip=False,
    )
    
    cam2 = CameraConfig(
        camera_index=1,
        img_size=(1920, 1080),
        timeout=50,
        nopreview=True,
        quality=85,
        awb="auto",
        vflip=False,
        hflip=False,
    )
    
    return cam1, cam2


@pytest.fixture
def override_projects_root(tmp_path, monkeypatch):
    """
    Override the PROJECTS_ROOT setting for testing.
    
    This fixture creates a temporary projects directory and patches
    the settings to use it, preventing tests from affecting real data.
    """
    test_projects_dir = tmp_path / "test_projects"
    test_projects_dir.mkdir(parents=True, exist_ok=True)
    
    # Patch the environment variable
    monkeypatch.setenv("PROJECTS_ROOT", str(test_projects_dir))
    
    # Reload settings to pick up the new value
    from app.core import config
    import importlib
    importlib.reload(config)
    
    return test_projects_dir


@pytest.fixture
def skip_if_no_camera():
    """
    Skip test if no cameras are detected.
    
    Usage:
        def test_camera_function(skip_if_no_camera):
            # Test will be skipped if no cameras found
            ...
    """
    from capture import is_camera_connected
    
    if not is_camera_connected(0):
        pytest.skip("No camera detected - skipping hardware test")


@pytest.fixture
def skip_if_single_camera():
    """
    Skip test if fewer than 2 cameras are detected.
    
    Usage:
        def test_dual_camera_function(skip_if_single_camera):
            # Test will be skipped if not enough cameras
            ...
    """
    from capture import is_camera_connected
    
    if not (is_camera_connected(0) and is_camera_connected(1)):
        pytest.skip("Dual cameras not detected - skipping test")


@pytest.fixture(params=["subprocess", "picamera2"])
def camera_backend_type(request):
    """
    Parametrized fixture that runs tests with both backends.
    
    Usage:
        def test_with_both_backends(camera_backend_type):
            # This test will run twice, once for each backend
            backend_name = camera_backend_type
            ...
    """
    return request.param


@pytest.fixture
def set_camera_backend(monkeypatch, camera_backend_type):
    """
    Set the camera backend for testing via environment variable.
    
    Usage with camera_backend_type parametrization:
        def test_capture(set_camera_backend, camera_backend_type):
            # Backend is automatically set based on parameter
            ...
    """
    monkeypatch.setenv("CAMERA_BACKEND", camera_backend_type)
    
    # Reload settings
    from app.core import config
    import importlib
    importlib.reload(config)
    
    return camera_backend_type


# Pytest hooks
def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line(
        "markers", "camera: mark test as requiring camera hardware"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow running"
    )
    config.addinivalue_line(
        "markers", "integration: mark test as integration test"
    )
    config.addinivalue_line(
        "markers", "unit: mark test as unit test"
    )
    config.addinivalue_line(
        "markers", "backend: mark test as camera backend specific"
    )


def pytest_collection_modifyitems(config, items):
    """
    Modify test collection to add markers automatically.
    
    - Tests with "camera" in the name get @pytest.mark.camera
    - Tests with "slow" in the name get @pytest.mark.slow
    """
    for item in items:
        # Auto-mark camera tests
        if "camera" in item.nodeid.lower():
            item.add_marker(pytest.mark.camera)
        
        # Auto-mark backend tests
        if "backend" in item.nodeid.lower():
            item.add_marker(pytest.mark.backend)
