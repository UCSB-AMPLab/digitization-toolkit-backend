"""Registry hardware-ID lookups for gphoto2 must go through the backend singleton.

Opening a private PTP session from the registry is a second claim on a camera
the live preview may already hold. The registry should ask the process-wide
backend (capture.service.get_backend) to enumerate instead, and must never
import gphoto2 itself.
"""

import builtins
import sys

import pytest

import capture.service as capture_service
from capture.camera_registry import CameraRegistry


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
        "location": "USB usb:001,005",
        "port": "usb:001,005",
        "has_aperture_control": True,
        "supports_zoom": False,
    },
]


class _FakeBackend:
    """Stand-in for the process singleton returned by get_backend()."""

    def __init__(self, devices=None, exc=None):
        self._devices = devices if devices is not None else []
        self._exc = exc
        self.calls = 0

    def list_devices(self):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return [dict(d) for d in self._devices]


@pytest.fixture
def fake_backend(monkeypatch):
    backend = _FakeBackend(DEVICES)
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    return backend


def test_hardware_id_comes_from_the_backend_singleton(fake_backend):
    hw_id, info = CameraRegistry._get_camera_hardware_id_gphoto2(1)

    assert hw_id == "canoneosrebelt7_2222222"
    assert info["model"] == "Canon EOS Rebel T7"
    assert info["serial"] == "2222222"
    assert info["location"] == "USB usb:001,005"
    assert info["id"] == "usb:001,005"
    assert info["index"] == 1
    assert fake_backend.calls == 1


def test_unknown_index_returns_empty_info(fake_backend):
    assert CameraRegistry._get_camera_hardware_id_gphoto2(5) == (None, {})


def test_enumeration_failure_is_reported_as_error(monkeypatch):
    backend = _FakeBackend(exc=RuntimeError("no cameras detected"))
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)

    hw_id, info = CameraRegistry._get_camera_hardware_id_gphoto2(0)

    assert hw_id is None
    assert info == {"error": "no cameras detected"}


def test_registry_never_imports_gphoto2(fake_backend, monkeypatch):
    had_gphoto2 = "gphoto2" in sys.modules
    attempted = []
    real_import = builtins.__import__

    def spy(name, *args, **kwargs):
        attempted.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", spy)
    hw_id, _info = CameraRegistry._get_camera_hardware_id_gphoto2(0)
    monkeypatch.setattr(builtins, "__import__", real_import)

    assert hw_id == "canoneosrebelt7_1111111"
    assert not any(
        name == "gphoto2" or name.startswith("gphoto2.") for name in attempted
    ), f"registry imported gphoto2: {attempted}"
    assert ("gphoto2" in sys.modules) == had_gphoto2


def test_dispatch_uses_gphoto2_helper_when_backend_is_gphoto2(fake_backend, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "CAMERA_BACKEND", "gphoto2")

    hw_id, info = CameraRegistry.get_camera_hardware_id(0)

    assert hw_id == "canoneosrebelt7_1111111"
    assert info["id"] == "usb:001,004"
    assert info["index"] == 0
    assert fake_backend.calls == 1
