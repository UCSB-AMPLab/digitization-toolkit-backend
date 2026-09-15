"""PUT /cameras/{camera_index}/orientation: persist a body's saved rotation
(NEH-71) keyed by hardware id, and the Optional rotate_deg semantics on
POST /cameras/capture that let a saved orientation survive an omitted field.

Modelled on test_rescan_route.py: a fake backend stands in for the process
singleton; the registry itself is a real CameraRegistry pointed at a fresh
file through the client fixture's projects-root override.
"""

import capture.service as capture_service
import capture.project_manager as project_manager_module

import pytest


DEVICES = [
    {
        "index": 0,
        "model": "Canon EOS Rebel T7",
        "hardware_id": "canoneosrebelt7_1111111",
        "serial": "1111111",
        "location": "USB usb:001,004",
        "port": "usb:001,004",
        "has_aperture_control": True,
        "supports_zoom": False,
    },
    {
        "index": 1,
        "model": "Canon EOS Rebel T7",
        "hardware_id": "canoneosrebelt7_2222222",
        "serial": "2222222",
        "location": "USB usb:001,009",
        "port": "usb:001,009",
        "has_aperture_control": True,
        "supports_zoom": False,
    },
]


class _FakeBackend:
    """Stand-in for the process singleton returned by get_backend()."""

    def __init__(self, devices=None):
        self._devices = devices if devices is not None else []

    def list_devices(self):
        return [dict(d) for d in self._devices]


def _user(username, role):
    from app.models.user import User

    return User(
        username=username,
        email=f"{username}@example.com",
        hashed_password="x",
        role=role,
        is_active=True,
    )


def _client_as(client, username, role):
    from app.main import app
    from app.api.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


def _install_gphoto2_backend(monkeypatch, devices):
    """Point both hardware-id resolution and enumeration at a fake backend.

    The orientation route resolves the index's current identity via
    CameraRegistry.get_camera_hardware_id, which dispatches on
    settings.CAMERA_BACKEND; GET /devices and the route's own re-enumeration
    read straight from get_backend().list_devices().
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "CAMERA_BACKEND", "gphoto2")
    backend = _FakeBackend(devices)
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    return backend


@pytest.mark.unit
def test_operator_can_set_orientation_and_it_is_saved_and_reported(client, monkeypatch):
    _install_gphoto2_backend(monkeypatch, DEVICES)
    api = _client_as(client, "op", "operator")

    resp = api.put(
        "/cameras/1/orientation",
        json={"orientation": 270, "hardware_id": "canoneosrebelt7_2222222"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["orientation"] == 270
    assert body["hardware_id"] == "canoneosrebelt7_2222222"

    from capture.camera_registry import CameraRegistry

    registry = CameraRegistry()
    saved = registry.get_camera_by_id("canoneosrebelt7_2222222")
    assert saved is not None
    assert saved["orientation"] == 270

    resp = api.get("/cameras/devices")
    assert resp.status_code == 200, resp.text
    devices_by_index = {d["index"]: d for d in resp.json()}
    assert devices_by_index[1]["orientation"] == 270
    assert devices_by_index[0]["orientation"] is None


@pytest.mark.unit
def test_orientation_is_forbidden_for_a_reviewer(client, monkeypatch):
    backend = _install_gphoto2_backend(monkeypatch, DEVICES)
    api = _client_as(client, "rev", "reviewer")

    resp = api.put(
        "/cameras/1/orientation",
        json={"orientation": 270, "hardware_id": "canoneosrebelt7_2222222"},
    )

    assert resp.status_code == 403, resp.text

    from capture.camera_registry import CameraRegistry

    registry = CameraRegistry()
    assert registry.get_camera_by_id("canoneosrebelt7_2222222") is None


@pytest.mark.unit
def test_orientation_for_a_disconnected_index_is_404(client, monkeypatch):
    _install_gphoto2_backend(monkeypatch, DEVICES)
    api = _client_as(client, "op", "operator")

    resp = api.put(
        "/cameras/5/orientation",
        json={"orientation": 270, "hardware_id": "does-not-matter"},
    )

    assert resp.status_code == 404, resp.text


@pytest.mark.unit
def test_orientation_rejects_an_invalid_degree_value(client, monkeypatch):
    _install_gphoto2_backend(monkeypatch, DEVICES)
    api = _client_as(client, "op", "operator")

    resp = api.put(
        "/cameras/1/orientation",
        json={"orientation": 45, "hardware_id": "canoneosrebelt7_2222222"},
    )

    assert resp.status_code == 422, resp.text


@pytest.mark.unit
def test_orientation_with_a_stale_hardware_id_is_409_and_writes_nothing(client, monkeypatch):
    _install_gphoto2_backend(monkeypatch, DEVICES)
    api = _client_as(client, "op", "operator")

    resp = api.put(
        "/cameras/1/orientation",
        json={"orientation": 270, "hardware_id": "some-other-body"},
    )

    assert resp.status_code == 409, resp.text

    from capture.camera_registry import CameraRegistry

    registry = CameraRegistry()
    assert registry.get_camera_by_id("canoneosrebelt7_2222222") is None
    assert registry.get_camera_by_id("some-other-body") is None


def _install_capture_fakes(monkeypatch, recorded, fake_config_dict):
    def fake_default_camera_config_from_registry(camera_index, resolution="high", registry=None):
        return dict(fake_config_dict), "hw"

    def fake_single_capture_image(project_name, camera_config, check_camera=False,
                                   include_resolution=False, collection_name=None):
        recorded["camera_config"] = camera_config
        return "/tmp/nonexistent-capture-for-test.jpg", "cap-1", None

    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    monkeypatch.setattr(capture_service, "single_capture_image", fake_single_capture_image)
    monkeypatch.setattr(
        project_manager_module,
        "default_camera_config_from_registry",
        fake_default_camera_config_from_registry,
    )


@pytest.mark.unit
def test_capture_with_no_rotate_deg_keeps_the_registrys_saved_orientation(client, monkeypatch, test_project):
    recorded = {}
    _install_capture_fakes(monkeypatch, recorded, {"camera_index": 0, "rotate_deg": 270})
    api = _client_as(client, "op", "operator")

    resp = api.post(
        "/cameras/capture",
        json={"project_name": test_project.name, "camera_index": 0},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True, resp.text
    assert recorded["camera_config"].rotate_deg == 270


@pytest.mark.unit
def test_capture_with_explicit_zero_overrides_the_saved_orientation(client, monkeypatch, test_project):
    recorded = {}
    _install_capture_fakes(monkeypatch, recorded, {"camera_index": 0, "rotate_deg": 270})
    api = _client_as(client, "op", "operator")

    resp = api.post(
        "/cameras/capture",
        json={"project_name": test_project.name, "camera_index": 0, "rotate_deg": 0},
    )

    assert resp.status_code == 200, resp.text
    assert recorded["camera_config"].rotate_deg == 0


@pytest.mark.unit
def test_capture_rejects_an_invalid_rotate_deg(client, monkeypatch, test_project):
    recorded = {}
    _install_capture_fakes(monkeypatch, recorded, {"camera_index": 0, "rotate_deg": 270})
    api = _client_as(client, "op", "operator")

    resp = api.post(
        "/cameras/capture",
        json={"project_name": test_project.name, "camera_index": 0, "rotate_deg": 45},
    )

    assert resp.status_code == 422, resp.text
    assert "camera_config" not in recorded
