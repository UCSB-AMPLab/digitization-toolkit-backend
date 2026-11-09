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
def test_gphoto2_prefers_the_bodys_own_model_without_a_device_call(gphoto2_backend):
    # gp is already a _NoDeviceCalls; the session's camera handle is one too,
    # so reading the cached body model must not go back to the body.
    gphoto2_backend._port_map = {0: ("Canon EOS 1500D", "usb:001,004")}
    gphoto2_backend._sessions = {0: types.SimpleNamespace(
        model="Canon EOS 1500D", body_model="Canon EOS Rebel T7",
        _cam=_NoDeviceCalls("cam"),
    )}
    assert gphoto2_backend.camera_model(0) == "Canon EOS Rebel T7"


@pytest.mark.unit
@pytest.mark.parametrize("body_model", [None, "", "unknown"])
def test_gphoto2_without_a_body_model_falls_back_to_the_session_model(
        gphoto2_backend, body_model):
    gphoto2_backend._sessions = {0: types.SimpleNamespace(
        model="Canon EOS 1500D", body_model=body_model, _cam=_NoDeviceCalls("cam"),
    )}
    assert gphoto2_backend.camera_model(0) == "Canon EOS 1500D"


# A real _PTPSession opened against a fake libgphoto2 whose config answers
# from a dict. A name it does not hold raises GPhoto2Error, as libgphoto2's
# get_child_by_name does; a value that is an exception is raised instead.

class _FakeGPhoto2Error(Exception):
    pass


def _fake_gp_for_session(widgets, fetches=None):
    """`fetches`, when given, gets one entry per get_config() call: the list
    of widget names read from that fetched tree."""
    class Widget:
        def __init__(self, name):
            self._name = name

        def get_value(self):
            value = widgets[self._name]
            if isinstance(value, BaseException):
                raise value
            return value

        def set_value(self, value):
            widgets[self._name] = value

    class Config:
        def __init__(self):
            self.read = []
            if fetches is not None:
                fetches.append(self.read)

        def get_child_by_name(self, name):
            self.read.append(name)
            if name not in widgets:
                raise _FakeGPhoto2Error(f"-2: no widget {name}")
            return Widget(name)

    class Camera:
        def set_abilities(self, abilities):
            pass

        def set_port_info(self, info):
            pass

        def init(self):
            pass

        def exit(self):
            pass

        def get_config(self):
            return Config()

        def set_config(self, cfg):
            pass

    class _List:
        def load(self):
            pass

        def lookup_model(self, model):
            return 0

        def lookup_path(self, path):
            return 0

        def __getitem__(self, idx):
            return object()

    return types.SimpleNamespace(
        GPhoto2Error=_FakeGPhoto2Error, Camera=Camera,
        CameraAbilitiesList=_List, PortInfoList=_List,
    )


def _base_widgets(widgets):
    base = {"focusmode": "Manual", "flashmode": "Off", "autopoweroff": "0",
            "serialnumber": " 0123 "}
    base.update(widgets)
    return {k: v for k, v in base.items() if v is not _ABSENT}


_ABSENT = object()


def _open_session(monkeypatch, widgets, fetches=None):
    import capture.backends.gphoto2_backend as gb

    monkeypatch.setattr(gb, "gp", _fake_gp_for_session(_base_widgets(widgets), fetches))
    return gb._PTPSession("usb:001,004", "Canon EOS 1500D", logging.getLogger("t"))


# Fetches of the whole config tree at session open before NEH-329b, on the
# widgets _base_widgets() gives: capturetarget, reviewtime, autopoweroff,
# focusmode, flashmode and serialnumber each fetched their own tree.
_OPEN_FETCHES_BEFORE = 6


@pytest.mark.unit
def test_session_open_fetches_the_config_tree_no_more_often_than_before(monkeypatch):
    fetches = []
    session = _open_session(monkeypatch, {"cameramodel": "Canon EOS Rebel T7"}, fetches)
    assert (session.serial, session.body_model) == ("0123", "Canon EOS Rebel T7")
    assert len(fetches) <= _OPEN_FETCHES_BEFORE
    # The serial and the body model come off one fetched tree.
    [shared] = [read for read in fetches if "serialnumber" in read]
    assert "cameramodel" in shared


@pytest.mark.unit
@pytest.mark.parametrize("model_widget", [
    _ABSENT, "", _FakeGPhoto2Error("-1: read failed"), RuntimeError("bus gone"),
], ids=["missing", "empty", "gphoto2-error", "other-error"])
def test_a_failed_body_model_read_keeps_the_serial(monkeypatch, model_widget):
    session = _open_session(monkeypatch, {"cameramodel": model_widget})
    assert (session.serial, session.body_model) == ("0123", None)


@pytest.mark.unit
@pytest.mark.parametrize("serial_widget", [_ABSENT, _FakeGPhoto2Error("-1: read failed")],
                         ids=["missing", "gphoto2-error"])
def test_a_failed_serial_read_keeps_the_body_model(monkeypatch, serial_widget):
    session = _open_session(monkeypatch, {"serialnumber": serial_widget,
                                          "cameramodel": "Canon EOS Rebel T7"})
    assert (session.serial, session.body_model) == ("", "Canon EOS Rebel T7")


# ---------------------------------------------------------------------------
# gphoto2: the body's own model outlives its session, for the same body only
# ---------------------------------------------------------------------------

_PORT = "usb:001,004"
_ABILITIES = "Canon EOS 1500D"


def _open_and_close(monkeypatch, backend, widgets, index=0):
    """Open a session through the backend's identity path, then close it."""
    import capture.backends.gphoto2_backend as gb

    monkeypatch.setattr(gb, "gp", _fake_gp_for_session(_base_widgets(widgets)))
    backend._port_map = {index: (_ABILITIES, _PORT)}
    session = backend._open_identified_session(index, _ABILITIES, _PORT)
    backend._sessions[index] = session
    assert backend.camera_model(index) == (session.body_model or _ABILITIES)
    backend._close_session(index)
    assert index not in backend._sessions
    # From here any use of the library fails the test.
    monkeypatch.setattr(gb, "gp", _NoDeviceCalls("gp"))
    return session


@pytest.mark.unit
def test_a_closed_sessions_body_keeps_its_own_name(monkeypatch, gphoto2_backend):
    _open_and_close(monkeypatch, gphoto2_backend, {"cameramodel": "Canon EOS Rebel T7"})
    assert gphoto2_backend.camera_model(0) == "Canon EOS Rebel T7"


@pytest.mark.unit
def test_a_closed_sessions_body_without_a_serial_keeps_its_own_name(
        monkeypatch, gphoto2_backend):
    _open_and_close(monkeypatch, gphoto2_backend,
                    {"serialnumber": _ABSENT, "cameramodel": "Canon EOS Rebel T7"})
    assert gphoto2_backend.camera_model(0) == "Canon EOS Rebel T7"


@pytest.mark.unit
@pytest.mark.parametrize("entry, serial, expected", [
    (("Canon EOS 1500D", "usb:001,009"), None, "Canon EOS 1500D"),   # another port
    (("Canon EOS 80D", _PORT), None, "Canon EOS 80D"),               # another model
    ((_ABILITIES, _PORT), "9999", "Canon EOS 1500D"),                # another serial
], ids=["other-port", "other-model", "other-serial"])
def test_the_cached_name_is_never_given_to_a_different_body(
        monkeypatch, gphoto2_backend, entry, serial, expected):
    _open_and_close(monkeypatch, gphoto2_backend, {"cameramodel": "Canon EOS Rebel T7"})
    gphoto2_backend._port_map = {0: entry}
    if serial is not None:
        gphoto2_backend._serial_by_port[entry[1]] = serial
    assert gphoto2_backend.camera_model(0) == expected


@pytest.mark.unit
def test_a_body_without_a_serial_does_not_lend_its_name_to_one_with_a_serial(
        monkeypatch, gphoto2_backend):
    _open_and_close(monkeypatch, gphoto2_backend,
                    {"serialnumber": _ABSENT, "cameramodel": "Canon EOS Rebel T7"})
    gphoto2_backend._serial_by_port[_PORT] = "9999"
    assert gphoto2_backend.camera_model(0) == _ABILITIES


@pytest.mark.unit
def test_a_reopen_that_reads_no_body_model_forgets_the_old_one(monkeypatch, gphoto2_backend):
    _open_and_close(monkeypatch, gphoto2_backend, {"cameramodel": "Canon EOS Rebel T7"})
    _open_and_close(monkeypatch, gphoto2_backend, {"cameramodel": RuntimeError("bus gone")})
    assert gphoto2_backend.camera_model(0) == _ABILITIES


@pytest.mark.unit
def test_a_port_that_leaves_the_detection_drops_its_cached_name(monkeypatch, gphoto2_backend):
    _open_and_close(monkeypatch, gphoto2_backend, {"cameramodel": "Canon EOS Rebel T7"})
    with gphoto2_backend._map_lock:
        gphoto2_backend._port_map = gphoto2_backend._build_port_map(
            gphoto2_backend._pins, gphoto2_backend._serial_by_port,
            [("Canon EOS 80D", "usb:001,007")],
        )
    assert _PORT not in gphoto2_backend._body_model_by_port
    # The same port coming back is a different body to the cache.
    gphoto2_backend._port_map = {0: (_ABILITIES, _PORT)}
    assert gphoto2_backend.camera_model(0) == _ABILITIES


@pytest.mark.unit
def test_opening_a_session_caches_the_bodys_own_model(monkeypatch, gphoto2_backend):
    session = _open_session(monkeypatch, {"cameramodel": "Canon EOS Rebel T7"})
    assert session.body_model == "Canon EOS Rebel T7"
    assert session.model == "Canon EOS 1500D"
    assert session.serial == "0123"

    gphoto2_backend._sessions = {0: session}
    session._cam = _NoDeviceCalls("cam")
    assert gphoto2_backend.camera_model(0) == "Canon EOS Rebel T7"


@pytest.mark.unit
@pytest.mark.parametrize("widgets", [
    {},
    {"cameramodel": ""},
    {"cameramodel": "   "},
    {"cameramodel": "None"},  # libgphoto2's value when DeviceInfo has no Model
    {"cameramodel": None},
    {"cameramodel": _FakeGPhoto2Error("-1: read failed")},
    {"cameramodel": RuntimeError("bus gone")},
], ids=["missing", "empty", "blank", "none-text", "none", "gphoto2-error", "other-error"])
def test_no_body_model_leaves_it_none_and_the_session_opens(
        monkeypatch, gphoto2_backend, widgets):
    session = _open_session(monkeypatch, widgets)
    assert session.body_model is None
    assert session.model == "Canon EOS 1500D"
    assert session.serial == "0123"

    gphoto2_backend._sessions = {0: session}
    session._cam = _NoDeviceCalls("cam")
    assert gphoto2_backend.camera_model(0) == "Canon EOS 1500D"


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
