"""Wiring the CHDK backend into the service, the registry and the manifest.

Three joins, each of which would otherwise leave the backend unreachable:
CAMERA_BACKEND=chdk has to select it; the camera registry has to take its
hardware ids from the backend, as it already does for gphoto2, rather than
from picamera2's global_camera_info; and a dual capture has to record the
pair's wall time, which is the number NEH-173 compares against picamera2's
seven to eight seconds.

The registry join carries one rule of its own. A body with no identity gets a
provisional id so the API can list it, and that id must never be written to
cameras.json: a calibration or an orientation saved under it would be
attached to whichever body happened to be at that index.
"""

import pytest

import capture.backends.chdk_backend as chdk_backend
import capture.service as capture_service
from capture.camera_registry import CameraRegistry

from .chdk_fakes import Body, make_pychdk


CHDK_DEVICE = {
    "index": 0,
    "model": "Canon PowerShot A2500",
    "hardware_id": "canon325b_AAA111",
    "serial": "AAA111",
    "location": "USB usb:001,004",
    "port": "usb:001,004",
    "has_aperture_control": False,
    "supports_zoom": False,
    "side": "even",
    "provisional": False,
    "error": None,
}

PROVISIONAL_DEVICE = {
    **CHDK_DEVICE,
    "index": 1,
    "hardware_id": "canon325b_idx1",
    "serial": None,
    "side": "odd",
    "provisional": True,
    "error": "no identity; assign a parity with POST /cameras/side/1",
}


class _FakeBackend:
    def __init__(self, devices):
        self._devices = devices
        self.calls = 0

    def list_devices(self):
        self.calls += 1
        return [dict(device) for device in self._devices]


def _use_chdk(monkeypatch, devices):
    from app.core.config import settings

    backend = _FakeBackend(devices)
    monkeypatch.setattr(settings, "CAMERA_BACKEND", "chdk")
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    return backend


@pytest.mark.unit
def test_the_setting_selects_the_chdk_backend(monkeypatch):
    # The service holds its own reference to the settings object, and other
    # fixtures in this suite reload app.core.config, so patching the setting
    # anywhere else would be patching a different object than the one the
    # selector reads.
    monkeypatch.setattr(capture_service.settings, "CAMERA_BACKEND", "chdk")
    monkeypatch.setattr(chdk_backend, "pychdk", make_pychdk(Body()))
    monkeypatch.setattr(chdk_backend, "_PYCHDK_AVAILABLE", True)

    backend = capture_service.get_camera_backend()

    assert isinstance(backend, chdk_backend.ChdkBackend)
    assert backend.get_backend_name() == "chdk"


@pytest.mark.unit
def test_the_backends_package_exports_it():
    from capture.backends import ChdkBackend

    assert ChdkBackend is chdk_backend.ChdkBackend


@pytest.mark.unit
def test_the_registry_asks_the_backend_for_a_hardware_id(monkeypatch):
    backend = _use_chdk(monkeypatch, [CHDK_DEVICE])

    hw_id, info = CameraRegistry.get_camera_hardware_id(0)

    assert hw_id == "canon325b_AAA111"
    assert info["model"] == "Canon PowerShot A2500"
    assert info["serial"] == "AAA111"
    assert info["id"] == "usb:001,004"
    assert info["index"] == 0
    assert backend.calls == 1


@pytest.mark.unit
def test_a_provisional_body_is_not_given_an_identity_to_save_under(monkeypatch):
    _use_chdk(monkeypatch, [CHDK_DEVICE, PROVISIONAL_DEVICE])

    hw_id, info = CameraRegistry.get_camera_hardware_id(1)

    assert hw_id is None, "a provisional id reached the registry"
    assert info.get("provisional") is True


@pytest.mark.unit
def test_detection_skips_the_provisional_body_and_keeps_the_other(
    monkeypatch, tmp_path
):
    _use_chdk(monkeypatch, [CHDK_DEVICE, PROVISIONAL_DEVICE])

    registry = CameraRegistry(registry_path=tmp_path / "registry.json")
    detected = registry.detect_cameras()

    assert set(detected) == {0}
    assert detected[0][0] == "canon325b_AAA111"


@pytest.mark.unit
def test_a_dual_capture_logs_the_pairs_wall_time(monkeypatch, tmp_path, caplog):
    """NEH-173 compares a CHDK pair against picamera2's seven to eight seconds."""
    from app.core.config import settings

    from capture.camera import CameraConfig

    monkeypatch.setattr(settings, "PROJECTS_ROOT", str(tmp_path))

    class _Writer:
        def capture_image(self, output_path, camera_config, capture_output=False):
            from pathlib import Path

            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"\xff\xd8\xff\xd9")
            return str(output_path), None

    monkeypatch.setattr(capture_service, "get_backend", lambda: _Writer())

    with caplog.at_level("INFO", logger="capture_service"):
        capture_service.dual_capture_image(
            "pairing",
            CameraConfig(camera_index=0),
            CameraConfig(camera_index=1),
            check_camera=False,
            stagger_ms=0,
        )

    line = "\n".join(
        record.getMessage() for record in caplog.records
        if "Parallel capture" in record.getMessage()
    )
    assert "pair_wall=" in line
    assert "cam0=" in line and "cam1=" in line


TWIN_A = {
    **CHDK_DEVICE,
    "index": 0,
    "hardware_id": "canon325b_aaaaaaaaaaaa",
    "serial": None,
    "identity_ambiguous": True,
    "error": "two bodies answer to the identity canon325b_aaaaaaaaaaaa",
}
TWIN_B = {**TWIN_A, "index": 1, "side": "odd"}


@pytest.mark.unit
def test_two_bodies_sharing_an_identity_are_kept_out_of_the_registry(
    monkeypatch, tmp_path
):
    """One record cannot serve two cameras.

    The backend reports both rows on purpose, so the operator can see the
    clash and repair it, but registering them would file one calibration and
    one orientation against whichever body happened to hold an index.
    """
    _use_chdk(monkeypatch, [TWIN_A, TWIN_B])

    registry = CameraRegistry(registry_path=tmp_path / "registry.json")
    detected = registry.detect_cameras()

    assert detected == {}


@pytest.mark.unit
def test_no_identity_is_handed_out_for_a_body_that_shares_one(monkeypatch):
    _use_chdk(monkeypatch, [TWIN_A, TWIN_B])

    hw_id, info = CameraRegistry.get_camera_hardware_id(0)

    assert hw_id is None, "an ambiguous identity reached the registry"
    assert info.get("identity_ambiguous") is True


@pytest.mark.unit
def test_the_backend_marks_the_rows_the_registry_has_to_skip(monkeypatch):
    """The flag the registry reads has to be the one the backend sets."""
    from .chdk_fakes import Body, make_backend, make_pychdk

    first = Body(bus=1, address=4, serial=None, card=b"EVEN\nid=aaaaaaaaaaaa\n")
    second = Body(bus=1, address=7, serial=None, card=b"ODD\nid=aaaaaaaaaaaa\n")
    backend = make_backend(monkeypatch, make_pychdk(first, second))

    rows = backend.list_devices()

    assert [row["identity_ambiguous"] for row in rows] == [True, True]
    assert all(row["error"] for row in rows), "the rows must stay visible"


@pytest.mark.unit
def test_an_unambiguous_body_is_still_registered(monkeypatch, tmp_path):
    _use_chdk(monkeypatch, [CHDK_DEVICE])

    registry = CameraRegistry(registry_path=tmp_path / "registry.json")

    assert set(registry.detect_cameras()) == {0}
