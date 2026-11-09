"""NEH-329: CameraSettings records the camera's real model.

The capture routes wrote the CameraConfig class name into
CameraSettings.camera_model, so every image recorded "CameraConfig". The
model now comes from the active backend's camera_model(camera_index), which
answers from what the backend already holds for that index and never talks
to the device: the capture path must not rescan, re-enumerate or open a
conversation with a body to name it.

An unknown model is NULL: no cached state for the index, a placeholder such
as picamera2's "unknown", an empty string, or a class name. A lookup that
fails is NULL too, and never fails the capture.

Run with: python -m pytest tests/unit/test_camera_model_recorded.py

Version: 0.1.0-beta
"""

import logging
import types

import pytest

import capture.service as capture_service
import capture.project_manager as project_manager_module
from capture.backends.base import CameraBackend


# ---------------------------------------------------------------------------
# Doubles that fail on any device call
# ---------------------------------------------------------------------------

class _NoDeviceCalls:
    """A stand-in for a hardware library: any use of it fails the test."""

    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        raise AssertionError(f"camera_model touched {self._name}.{attr}")


# ---------------------------------------------------------------------------
# The normaliser
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("raw", [None, "", "   ", "unknown", "Unknown", " UNKNOWN ",
                                 "CameraConfig", "USB PTP Class Camera", 42])
def test_a_placeholder_or_non_string_model_is_none(raw):
    from capture.backends.base import normalise_camera_model
    assert normalise_camera_model(raw) is None


@pytest.mark.unit
def test_a_model_is_stripped_and_truncated_to_the_column():
    from capture.backends.base import normalise_camera_model
    assert normalise_camera_model("  Canon EOS Rebel T7 \n") == "Canon EOS Rebel T7"
    assert normalise_camera_model("x" * 300) == "x" * 255


@pytest.mark.unit
def test_the_base_backend_knows_no_model():
    class Bare(CameraBackend):
        def is_camera_connected(self, camera_index=0):
            return True

        def capture_image(self, output_path, camera_config, capture_output=False):
            return str(output_path)

        def supports_streaming(self):
            return False

        def supports_live_adjustment(self):
            return False

    assert Bare(logging.getLogger("test")).camera_model(0) is None


# ---------------------------------------------------------------------------
# rpicam subprocess
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_subprocess_backend_holds_no_model(monkeypatch):
    import capture.backends.subprocess_backend as sb

    monkeypatch.setattr(sb, "subprocess", _NoDeviceCalls("subprocess"))
    backend = sb.RpicamBackend(logging.getLogger("test"))
    assert backend.camera_model(0) is None
    assert backend.camera_model(1) is None


# ---------------------------------------------------------------------------
# gphoto2
# ---------------------------------------------------------------------------

@pytest.fixture
def gphoto2_backend(monkeypatch, tmp_path):
    from app.core.config import settings
    import capture.backends.gphoto2_backend as gb

    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(settings, "PROJECTS_ROOT", str(root))
    monkeypatch.setattr(gb, "_GP_AVAILABLE", True)
    monkeypatch.setattr(gb, "gp", _NoDeviceCalls("gp"))
    return gb.GPhoto2Backend(logging.getLogger("test-gphoto2-model"))


@pytest.mark.unit
def test_gphoto2_answers_from_the_port_map(gphoto2_backend):
    gphoto2_backend._port_map = {
        0: ("Canon EOS Rebel T7", "usb:001,004"),
        1: ("Canon EOS 80D", "usb:001,005"),
    }
    assert gphoto2_backend.camera_model(0) == "Canon EOS Rebel T7"
    assert gphoto2_backend.camera_model(1) == "Canon EOS 80D"


@pytest.mark.unit
def test_gphoto2_prefers_the_open_session_that_took_the_image(gphoto2_backend):
    gphoto2_backend._port_map = {0: ("Canon EOS Rebel T7", "usb:001,004")}
    gphoto2_backend._sessions = {0: types.SimpleNamespace(model="Canon EOS 2000D")}
    assert gphoto2_backend.camera_model(0) == "Canon EOS 2000D"


@pytest.mark.unit
def test_gphoto2_unknown_index_or_empty_map_is_none_without_autodetect(gphoto2_backend):
    # An empty map is not built here: _get_port_map() would run autodetect().
    assert gphoto2_backend.camera_model(0) is None
    gphoto2_backend._port_map = {0: ("Canon EOS Rebel T7", "usb:001,004")}
    assert gphoto2_backend.camera_model(3) is None


@pytest.mark.unit
def test_gphoto2_placeholder_model_is_none(gphoto2_backend):
    gphoto2_backend._port_map = {0: ("  ", "usb:001,004"), 1: ("unknown", "usb:001,005")}
    assert gphoto2_backend.camera_model(0) is None
    assert gphoto2_backend.camera_model(1) is None


# ---------------------------------------------------------------------------
# CHDK
# ---------------------------------------------------------------------------

def _chdk_backend_with(monkeypatch, *bodies):
    from .chdk_fakes import make_backend, make_pychdk

    fake = make_pychdk(*bodies)
    backend = make_backend(monkeypatch, fake, logger_name="test-chdk-model")
    backend.list_devices()
    return backend, fake


@pytest.mark.unit
def test_chdk_answers_from_the_open_body_without_talking_to_it(monkeypatch):
    import capture.backends.chdk_backend as cb
    from .chdk_fakes import Body

    body = Body(product="Canon PowerShot A2500", card=b"ODD\nid=bbbbbbbbbbbb\n")
    backend, fake = _chdk_backend_with(monkeypatch, body)
    [index] = list(backend._indices)
    list_calls, opens, versions = fake.list_calls, body.opens, body.version_calls

    # Any library or device use from here on fails the test.
    monkeypatch.setattr(cb, "pychdk", _NoDeviceCalls("pychdk"))
    held = backend._body_at(index)
    held.device = _NoDeviceCalls("device")

    assert backend.camera_model(index) == "Canon PowerShot A2500"
    assert (fake.list_calls, body.opens, body.version_calls) == (list_calls, opens, versions)


@pytest.mark.unit
def test_chdk_unknown_index_is_none(monkeypatch):
    from .chdk_fakes import Body

    backend, _ = _chdk_backend_with(monkeypatch, Body())
    assert backend.camera_model(7) is None


@pytest.mark.unit
def test_chdk_before_any_scan_is_none_without_scanning(monkeypatch):
    from .chdk_fakes import Body, make_backend, make_pychdk

    fake = make_pychdk(Body())
    backend = make_backend(monkeypatch, fake, logger_name="test-chdk-model")
    assert backend.camera_model(0) is None
    assert fake.list_calls == 0


@pytest.mark.unit
def test_chdk_placeholder_model_is_none(monkeypatch):
    from .chdk_fakes import Body

    body = Body(product="   ")
    backend, _ = _chdk_backend_with(monkeypatch, body)
    [index] = list(backend._indices)
    assert backend.camera_model(index) is None


# ---------------------------------------------------------------------------
# picamera2
# ---------------------------------------------------------------------------

@pytest.fixture
def picamera2_backend(monkeypatch):
    import capture.backends.picamera2_backend as pb

    monkeypatch.setattr(pb, "Picamera2", _NoDeviceCalls("Picamera2"))
    return pb.Picamera2Backend(logging.getLogger("test-picamera2-model"))


@pytest.mark.unit
def test_picamera2_answers_from_the_cached_camera_info(picamera2_backend):
    picamera2_backend._camera_info = [{"Model": "imx519", "Id": "a"}, {"Model": "imx708"}]
    assert picamera2_backend.camera_model(0) == "imx519"
    assert picamera2_backend.camera_model(1) == "imx708"


@pytest.mark.unit
def test_picamera2_unknown_index_or_uncached_info_is_none(picamera2_backend):
    # Not yet cached: global_camera_info() is a libcamera call and is not made.
    assert picamera2_backend.camera_model(0) is None
    picamera2_backend._camera_info = [{"Model": "imx519"}]
    assert picamera2_backend.camera_model(1) is None
    assert picamera2_backend.camera_model(-1) is None


@pytest.mark.unit
def test_picamera2_placeholder_model_is_none(picamera2_backend):
    picamera2_backend._camera_info = [{}, {"Model": "unknown"}, {"Model": ""}]
    assert picamera2_backend.camera_model(0) is None
    assert picamera2_backend.camera_model(1) is None
    assert picamera2_backend.camera_model(2) is None


# ---------------------------------------------------------------------------
# The capture routes
# ---------------------------------------------------------------------------

def _user(username, role):
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, username, role):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


class _ModelBackend:
    """The active backend, as far as the routes' model lookup sees it."""

    def __init__(self, models=None, error=None):
        self.models = models or {}
        self.error = error
        self.asked = []

    def camera_model(self, camera_index):
        self.asked.append(camera_index)
        if self.error is not None:
            raise self.error
        return self.models.get(camera_index)


def _install_capture_fakes(monkeypatch, tmp_path, backend):
    def fake_config(camera_index, resolution="high", registry=None):
        return {"camera_index": camera_index, "rotate_deg": 0}, "hw"

    def fake_single(project_name, camera_config, check_camera=False,
                    include_resolution=False, collection_name=None, stem=None,
                    filename_index=None, root=None):
        return str(tmp_path / f"cap_c{camera_config.camera_index}.jpg"), "cap-1", None

    def fake_dual(project_name, cam1_config, cam2_config, check_camera=False,
                  include_resolution=False, stagger_ms=0, collection_name=None, stem=None,
                  filename_index=None, root=None, roles=None):
        return (str(tmp_path / f"cap_c{cam1_config.camera_index}.jpg"),
                str(tmp_path / f"cap_c{cam2_config.camera_index}.jpg"),
                "cap-2", "pair-1")

    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    monkeypatch.setattr(capture_service, "single_capture_image", fake_single)
    monkeypatch.setattr(capture_service, "dual_capture_image", fake_dual)
    monkeypatch.setattr(project_manager_module,
                        "default_camera_config_from_registry", fake_config)
    # The lookup reads the backend the capture used; it never constructs one.
    monkeypatch.setattr(capture_service, "_backend", backend)


@pytest.fixture
def volume(db_session, test_project):
    from app.models.collection import Collection
    v = Collection(name="V", project_id=test_project.id, filename_stem="co-test-v")
    db_session.add(v)
    db_session.commit()
    return v


def _settings_by_role(db_session, record_id):
    from app.models.camera import CameraSettings
    from app.models.record import RecordImage
    rows = (
        db_session.query(RecordImage, CameraSettings)
        .join(CameraSettings, CameraSettings.record_image_id == RecordImage.id)
        .filter(RecordImage.record_id == record_id)
        .all()
    )
    return {img.role: (img.filename, cs.camera_model) for img, cs in rows}


@pytest.mark.unit
@pytest.mark.parametrize("camera_index", [0, 1])
def test_a_single_capture_stores_the_backends_model(client, db_session, test_project, volume,
                                                    monkeypatch, tmp_path, camera_index):
    backend = _ModelBackend({0: "Canon EOS Rebel T7", 1: "Canon EOS 80D"})
    _install_capture_fakes(monkeypatch, tmp_path, backend)
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/capture", json={
        "project_name": test_project.name, "camera_index": camera_index,
        "collection_id": volume.id,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"], body

    rows = _settings_by_role(db_session, body["record_id"])
    expected = {0: "Canon EOS Rebel T7", 1: "Canon EOS 80D"}[camera_index]
    assert rows == {"single": (f"cap_c{camera_index}.jpg", expected)}
    assert backend.asked == [camera_index]


@pytest.mark.unit
@pytest.mark.parametrize("left_camera_index", [0, 1])
def test_a_dual_capture_stores_each_cameras_model_on_its_own_image(
        client, db_session, test_project, volume, monkeypatch, tmp_path, left_camera_index):
    backend = _ModelBackend({0: "Canon EOS Rebel T7", 1: "Canon EOS 80D"})
    _install_capture_fakes(monkeypatch, tmp_path, backend)
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/capture/dual", json={
        "project_name": test_project.name, "collection_id": volume.id,
        "left_camera_index": left_camera_index,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"], body

    rows = _settings_by_role(db_session, body["record_id"])
    left, right = (0, 1) if left_camera_index == 0 else (1, 0)
    model = {0: "Canon EOS Rebel T7", 1: "Canon EOS 80D"}
    assert rows == {
        "left": (f"cap_c{left}.jpg", model[left]),
        "right": (f"cap_c{right}.jpg", model[right]),
    }


@pytest.mark.unit
@pytest.mark.parametrize("backend", [
    _ModelBackend({}),
    _ModelBackend({0: "unknown", 1: "  "}),
    _ModelBackend(error=RuntimeError("bus gone")),
    None,  # no backend constructed: nothing is built to answer
], ids=["none", "placeholder", "raises", "no-backend"])
def test_an_unknown_or_failing_lookup_stores_null_and_the_capture_succeeds(
        client, db_session, test_project, volume, monkeypatch, tmp_path, backend):
    _install_capture_fakes(monkeypatch, tmp_path, backend)
    built = []
    monkeypatch.setattr(capture_service, "get_camera_backend",
                        lambda: built.append(1) or _ModelBackend({0: "built"}))
    api = _client_as(client, "op", "operator")

    single = api.post("/cameras/capture", json={
        "project_name": test_project.name, "camera_index": 0, "collection_id": volume.id,
    })
    assert single.status_code == 200, single.text
    assert single.json()["success"], single.json()
    assert _settings_by_role(db_session, single.json()["record_id"]) == {
        "single": ("cap_c0.jpg", None),
    }

    dual = api.post("/cameras/capture/dual", json={
        "project_name": test_project.name, "collection_id": volume.id,
    })
    assert dual.status_code == 200, dual.text
    assert dual.json()["success"], dual.json()
    assert _settings_by_role(db_session, dual.json()["record_id"]) == {
        "left": ("cap_c0.jpg", None),
        "right": ("cap_c1.jpg", None),
    }
    assert built == []


@pytest.mark.unit
def test_no_capture_stores_the_config_class_name(client, db_session, test_project, volume,
                                                 monkeypatch, tmp_path):
    from app.models.camera import CameraSettings
    _install_capture_fakes(monkeypatch, tmp_path, _ModelBackend({}))
    api = _client_as(client, "op", "operator")

    assert api.post("/cameras/capture", json={
        "project_name": test_project.name, "camera_index": 0, "collection_id": volume.id,
    }).json()["success"]
    assert api.post("/cameras/capture/dual", json={
        "project_name": test_project.name, "collection_id": volume.id,
    }).json()["success"]

    models = [cs.camera_model for cs in db_session.query(CameraSettings).all()]
    assert len(models) == 3
    assert "CameraConfig" not in models
