"""POST /cameras/test-capture/{camera_index}: a real test capture that writes nothing.

NEH-166: the dashboard's "Probar camaras" button takes a real still - exercising
shutter, autofocus and the file path exactly like POST /capture - but returns it
inline as JPEG bytes and never stores it: no file under the projects root, no
manifest record. Modelled on test_orientation_route.py (client/monkeypatch
pattern) and test_gphoto2_rescan.py (the autouse projects-root fixture).

Version: 0.1.0-beta
"""

from pathlib import Path

import pytest

import capture.service as capture_service
import capture.project_manager as project_manager_module
from capture.backends.gphoto2_backend import CaptureTimeoutError


@pytest.fixture(autouse=True)
def isolated_projects_root(monkeypatch, tmp_path):
    """Point PROJECTS_ROOT at a fresh empty tmp_path so a manifest write - if
    the route or service ever regressed into writing one - would be visible
    as a file appearing under it, rather than polluting a real projects dir.
    """
    from app.core.config import settings

    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "PROJECTS_ROOT", str(root))
    return root


FAKE_CONFIG_DICT = {"camera_index": 0, "rotate_deg": 270}


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


def _install_default_config(monkeypatch, config_dict=None):
    def fake_default_camera_config_from_registry(camera_index, resolution="high", registry=None):
        return dict(config_dict or FAKE_CONFIG_DICT), "hw"

    monkeypatch.setattr(
        project_manager_module,
        "default_camera_config_from_registry",
        fake_default_camera_config_from_registry,
    )


class _FakeBackendSingle:
    """Writes a fake JPEG and records the config passed in."""

    def __init__(self, recorded):
        self.recorded = recorded

    def capture_image(self, path, camera_config, capture_output=False):
        self.recorded["camera_config"] = camera_config
        self.recorded["path"] = Path(path)
        Path(path).write_bytes(b"\xff\xd8fake")
        return str(path), {"some": "metadata"}


class _FakeBackendTuple:
    """Multi-format capture: backend returns (jpeg_path, raw_path) as path_or_paths."""

    def __init__(self, recorded):
        self.recorded = recorded

    def capture_image(self, path, camera_config, capture_output=False):
        self.recorded["camera_config"] = camera_config
        jpeg_path = Path(path)
        raw_path = jpeg_path.with_suffix(".raw")
        jpeg_path.write_bytes(b"\xff\xd8fake")
        raw_path.write_bytes(b"rawdata")
        return (str(jpeg_path), str(raw_path)), None


class _FakeBackendRaw:
    """RAW (.cr2) capture with a JPEG preview sidecar extracted beside it."""

    def __init__(self, recorded, with_sidecar=True):
        self.recorded = recorded
        self.with_sidecar = with_sidecar

    def capture_image(self, path, camera_config, capture_output=False):
        self.recorded["camera_config"] = camera_config
        cr2_path = Path(path).with_suffix(".cr2")
        cr2_path.write_bytes(b"fake-raw-master")
        if self.with_sidecar:
            sidecar = cr2_path.with_name(cr2_path.stem + "_preview.jpg")
            sidecar.write_bytes(b"\xff\xd8fakepreview")
        return str(cr2_path), None


class _FakeBackendTupleRotated:
    """Multi-format capture like _FakeBackendTuple, but returns a metadata
    dict (not None) as the picamera2 backend does for raw captures
    (capture/backends/picamera2_backend.py:709 builds the (jpeg, raw) pair it
    returns)."""

    def __init__(self, recorded):
        self.recorded = recorded

    def capture_image(self, path, camera_config, capture_output=False):
        self.recorded["camera_config"] = camera_config
        jpeg_path = Path(path)
        raw_path = jpeg_path.with_suffix(".raw")
        jpeg_path.write_bytes(b"\xff\xd8fake")
        raw_path.write_bytes(b"rawdata")
        self.recorded["jpeg_path"] = jpeg_path
        self.recorded["raw_path"] = raw_path
        return (str(jpeg_path), str(raw_path)), {}


class _FakeBackendWrappedTimeout:
    def capture_image(self, path, camera_config, capture_output=False):
        raise RuntimeError("DSLR capture failed: timed out") from CaptureTimeoutError("no image arrived")


class _FakeBackendBareTimeout:
    def capture_image(self, path, camera_config, capture_output=False):
        raise CaptureTimeoutError("no image arrived")


class _FakeBackendRuntimeError:
    def capture_image(self, path, camera_config, capture_output=False):
        raise RuntimeError("gphoto2 blew up")


def _install_backend(monkeypatch, backend, connected=True):
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: connected)
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)


@pytest.mark.unit
def test_test_capture_returns_jpeg_bytes_and_headers(client, monkeypatch):
    recorded = {}
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendSingle(recorded))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content == b"\xff\xd8fake"
    assert resp.headers["X-Capture-Bytes"] == str(len(b"\xff\xd8fake"))
    assert float(resp.headers["X-Capture-Seconds"]) >= 0


@pytest.mark.unit
def test_test_capture_writes_nothing_under_the_projects_root(client, monkeypatch, isolated_projects_root):
    recorded = {}
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendSingle(recorded))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 200, resp.text
    assert list(isolated_projects_root.rglob("*")) == []


@pytest.mark.unit
def test_test_capture_removes_its_temp_directory_on_success(client, monkeypatch):
    recorded = {}
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendSingle(recorded))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 200, resp.text
    tmpdir = recorded["path"].parent
    assert not tmpdir.exists()


@pytest.mark.unit
def test_test_capture_removes_its_temp_directory_on_failure(client, monkeypatch):
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendRuntimeError())
    api = _client_as(client, "op", "operator")

    # Capture the tmpdir the fake would have received by wrapping mkdtemp.
    created = {}
    import capture.service as svc
    real_mkdtemp = svc.tempfile.mkdtemp

    def spy_mkdtemp(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        created["dir"] = d
        return d

    monkeypatch.setattr(svc.tempfile, "mkdtemp", spy_mkdtemp)

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 500, resp.text
    assert "dir" in created
    assert not Path(created["dir"]).exists()


@pytest.mark.unit
def test_test_capture_passes_the_registry_rotate_deg_to_the_backend(client, monkeypatch):
    recorded = {}
    _install_default_config(monkeypatch, {"camera_index": 0, "rotate_deg": 270})
    _install_backend(monkeypatch, _FakeBackendSingle(recorded))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 200, resp.text
    assert recorded["camera_config"].rotate_deg == 270


@pytest.mark.unit
def test_test_capture_returns_the_jpeg_half_of_a_multiformat_capture(client, monkeypatch):
    recorded = {}
    _install_default_config(monkeypatch, {"camera_index": 0, "rotate_deg": 0})
    _install_backend(monkeypatch, _FakeBackendTuple(recorded))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 200, resp.text
    assert resp.content == b"\xff\xd8fake"


@pytest.mark.unit
def test_test_capture_returns_the_preview_sidecar_for_a_raw_capture(client, monkeypatch):
    recorded = {}
    _install_default_config(monkeypatch, {"camera_index": 0, "rotate_deg": 0})
    _install_backend(monkeypatch, _FakeBackendRaw(recorded, with_sidecar=True))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 200, resp.text
    assert resp.content == b"\xff\xd8fakepreview"


@pytest.mark.unit
def test_test_capture_500s_when_a_raw_capture_has_no_preview_sidecar(client, monkeypatch):
    recorded = {}
    _install_default_config(monkeypatch, {"camera_index": 0, "rotate_deg": 0})
    _install_backend(monkeypatch, _FakeBackendRaw(recorded, with_sidecar=False))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 500, resp.text


@pytest.mark.unit
def test_test_capture_rotates_every_path_of_a_multiformat_capture(client, monkeypatch):
    recorded = {}
    rotation_calls = []
    _install_default_config(monkeypatch, {"camera_index": 0, "rotate_deg": 270})
    _install_backend(monkeypatch, _FakeBackendTupleRotated(recorded))

    import capture.service as capture_service_module

    def spy_apply_rotation(file_path, rotate_deg):
        rotation_calls.append(file_path)

    monkeypatch.setattr(capture_service_module, "_apply_rotation", spy_apply_rotation)
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 200, resp.text
    assert recorded["jpeg_path"] in rotation_calls
    for call in rotation_calls:
        assert isinstance(call, Path)
        assert call in (recorded["jpeg_path"], recorded["raw_path"])


@pytest.mark.unit
def test_test_capture_404s_when_not_connected(client, monkeypatch):
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendSingle({}), connected=False)
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 404, resp.text


@pytest.mark.unit
def test_test_capture_504s_on_a_wrapped_dslr_timeout(client, monkeypatch):
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendWrappedTimeout())
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 504, resp.text


@pytest.mark.unit
def test_test_capture_504s_on_a_bare_capture_timeout(client, monkeypatch):
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendBareTimeout())
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 504, resp.text


@pytest.mark.unit
def test_test_capture_500s_on_a_plain_runtime_error(client, monkeypatch):
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendRuntimeError())
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 500, resp.text


@pytest.mark.unit
def test_test_capture_422s_on_a_bad_resolution(client, monkeypatch):
    recorded = {}
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendSingle(recorded))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0?resolution=absurd")

    assert resp.status_code == 422, resp.text


@pytest.mark.unit
def test_test_capture_is_forbidden_for_a_reviewer(client, monkeypatch):
    recorded = {}
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendSingle(recorded))
    api = _client_as(client, "rev", "reviewer")

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 403, resp.text


@pytest.mark.unit
def test_test_capture_response_exposes_its_headers_across_cors(client, monkeypatch):
    recorded = {}
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendSingle(recorded))
    api = _client_as(client, "op", "operator")

    resp = api.post(
        "/cameras/test-capture/0",
        headers={"Origin": "http://localhost:5173"},
    )

    assert resp.status_code == 200, resp.text
    exposed = resp.headers.get("access-control-expose-headers", "")
    exposed_names = {h.strip() for h in exposed.split(",")}
    assert "X-Capture-Seconds" in exposed_names
    assert "X-Capture-Bytes" in exposed_names


# ----------------------------------------------------------------------
# NEH-339: ?focus_check=true for the Refocus dialog, and X-Focal-Length
# ----------------------------------------------------------------------

def _jpeg_with_focal_length(focal_length=None, exif_blob=None):
    import io

    from PIL import Image, TiffImagePlugin

    buf = io.BytesIO()
    image = Image.new("RGB", (8, 8))
    if exif_blob is not None:
        image.save(buf, "JPEG", exif=exif_blob)
    elif focal_length is None:
        image.save(buf, "JPEG")
    else:
        numerator, denominator = focal_length
        exif = Image.Exif()
        exif.get_ifd(0x8769)[0x920A] = TiffImagePlugin.IFDRational(numerator, denominator)
        image.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


class _FakeBackendFocusCheck:
    """A backend that takes the focus-check option, like GPhoto2Backend."""

    supports_focus_check = True

    def __init__(self, recorded, image_bytes=b"\xff\xd8fake"):
        self.recorded = recorded
        self.image_bytes = image_bytes

    def capture_image(self, path, camera_config, capture_output=False, **kwargs):
        self.recorded["kwargs"] = kwargs
        Path(path).write_bytes(self.image_bytes)
        return str(path), None


class _FakeBackendBytes:
    """A backend without the option, writing given bytes."""

    def __init__(self, image_bytes):
        self.image_bytes = image_bytes

    def capture_image(self, path, camera_config, capture_output=False):
        Path(path).write_bytes(self.image_bytes)
        return str(path), None


@pytest.mark.unit
def test_test_capture_passes_focus_check_to_a_backend_that_takes_it(client, monkeypatch):
    recorded = {}
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendFocusCheck(recorded))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0?focus_check=true")

    assert resp.status_code == 200, resp.text
    assert recorded["kwargs"] == {"focus_check": True}


@pytest.mark.unit
def test_test_capture_without_the_flag_passes_no_option(client, monkeypatch):
    recorded = {}
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendFocusCheck(recorded))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 200, resp.text
    assert recorded["kwargs"] == {}, "a test capture without the flag is unchanged"


@pytest.mark.unit
def test_test_capture_ignores_focus_check_on_a_backend_without_it(client, monkeypatch):
    recorded = {}
    _install_default_config(monkeypatch)
    # _FakeBackendSingle.capture_image takes no focus_check keyword: passing
    # one would raise a TypeError and a 500.
    _install_backend(monkeypatch, _FakeBackendSingle(recorded))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0?focus_check=true")

    assert resp.status_code == 200, resp.text
    assert resp.content == b"\xff\xd8fake"


@pytest.mark.unit
def test_focus_check_busy_trigger_on_a_dslr_is_a_504(client, monkeypatch, tmp_path):
    # End to end through the real GPhoto2Backend with the fake libgphoto2
    # of test_gphoto2_capture_timeout: the body stays busy, the focus check
    # gives up after one attempt and 20 s of (fake) clock, and the route
    # answers 504.
    import capture.backends.gphoto2_backend as gb
    from tests.unit import test_gphoto2_capture_timeout as harness

    clock = harness._Clock()
    monkeypatch.setattr(gb.time, "monotonic", clock)
    script = harness._Script(
        events=[(harness.GP_EVENT_TIMEOUT, None, 0.5)], clock=clock
    )
    backend, fake_gp = harness._make_backend(monkeypatch, script)
    script.triggers = [fake_gp.GPhoto2Error("[-110] I/O in progress")]
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, backend)
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0?focus_check=true")

    assert resp.status_code == 504, resp.text
    detail = resp.json()["detail"]
    assert "no picture arrived within the focus-check deadline" in detail
    assert harness.PORT in detail
    assert script.trigger_calls == 1
    assert 20.0 <= clock.now - script.trigger_times[0] < 20.5


@pytest.mark.unit
@pytest.mark.parametrize(
    "focal_length, expected",
    [((27, 1), "27"), ((55, 2), "27.5")],
)
def test_test_capture_reports_the_exif_focal_length(client, monkeypatch, focal_length, expected):
    # FAKE_CONFIG_DICT rotates by 270: the focal length must survive the
    # Orientation tag being written into the same EXIF block.
    image_bytes = _jpeg_with_focal_length(focal_length)
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendBytes(image_bytes))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0?focus_check=true")

    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Focal-Length"] == expected
    assert resp.headers["X-Capture-Bytes"] == str(len(resp.content))
    assert float(resp.headers["X-Capture-Seconds"]) >= 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "image_bytes",
    [
        pytest.param(_jpeg_with_focal_length(None), id="no-focal-length"),
        pytest.param(
            _jpeg_with_focal_length(exif_blob=b"Exif\x00\x00garbagegarbage"),
            id="garbage-exif",
        ),
        pytest.param(b"\xff\xd8fake", id="not-an-image"),
        pytest.param(_jpeg_with_focal_length((0, 0)), id="zero-over-zero"),
    ],
)
def test_test_capture_omits_the_focal_length_when_it_cannot_be_read(
    client, monkeypatch, image_bytes
):
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendBytes(image_bytes))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 200, resp.text
    assert "X-Focal-Length" not in resp.headers
    assert resp.content, "the picture is still returned"


@pytest.mark.unit
def test_test_capture_exposes_the_focal_length_across_cors(client, monkeypatch):
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendBytes(_jpeg_with_focal_length((27, 1))))
    api = _client_as(client, "op", "operator")

    resp = api.post(
        "/cameras/test-capture/0",
        headers={"Origin": "http://localhost:5173"},
    )

    assert resp.status_code == 200, resp.text
    exposed = resp.headers.get("access-control-expose-headers", "")
    assert "X-Focal-Length" in {h.strip() for h in exposed.split(",")}


# ----------------------------------------------------------------------
# NEH-343: ?layout=true&page_side=... and X-Page-Layout
# ----------------------------------------------------------------------

def _layout_scene(rotation_deg=270):
    from tests.unit.page_scenes import make_scene

    jpeg, corners = make_scene((0.25, 0.10, 0.98, 0.90), page_side="left", rotation_deg=rotation_deg)
    return jpeg, corners


def _record_analyser(monkeypatch, result=None, error=None):
    import capture.page_layout as page_layout

    calls = []

    def fake_analyse_page(jpeg_bytes, *, rotation_deg, page_side):
        calls.append({"bytes": jpeg_bytes, "rotation_deg": rotation_deg, "page_side": page_side})
        if error is not None:
            raise error
        return result if result is not None else {"found": False, "reason": "no_page_sized_region", "analysis_ms": 1.0}

    monkeypatch.setattr(page_layout, "analyse_page", fake_analyse_page)
    return calls


@pytest.mark.unit
def test_test_capture_reports_the_page_layout_when_asked(client, monkeypatch):
    import json

    image_bytes, corners = _layout_scene(rotation_deg=270)
    _install_default_config(monkeypatch)  # saved orientation 270
    _install_backend(monkeypatch, _FakeBackendBytes(image_bytes))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0?layout=true&page_side=left")

    assert resp.status_code == 200, resp.text
    layout = json.loads(resp.headers["X-Page-Layout"])
    assert layout["found"] is True, layout
    for got, want in zip(layout["corners"], corners):
        assert got[0] == pytest.approx(want[0], abs=0.01)
        assert got[1] == pytest.approx(want[1], abs=0.01)
    assert layout["verdict"] == "good", layout
    assert layout["gutter_state"] == "located"
    assert layout["gutter_margin"] == pytest.approx(0.02, abs=0.01)
    assert len(resp.headers["X-Page-Layout"].encode()) < 2048
    # Fractions are rounded to four decimals.
    for x, y in layout["corners"]:
        assert round(x, 4) == x and round(y, 4) == y
    # The picture and the existing headers are unchanged.
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["X-Capture-Bytes"] == str(len(resp.content))
    assert float(resp.headers["X-Capture-Seconds"]) >= 0


@pytest.mark.unit
def test_test_capture_without_layout_runs_no_analysis(client, monkeypatch):
    calls = _record_analyser(monkeypatch)
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendBytes(_layout_scene()[0]))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0?page_side=left")

    assert resp.status_code == 200, resp.text
    assert "X-Page-Layout" not in resp.headers
    assert calls == []


@pytest.mark.unit
def test_test_capture_layout_without_a_page_side_is_a_422(client, monkeypatch):
    calls = _record_analyser(monkeypatch)
    recorded = {}
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendSingle(recorded))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0?layout=true")

    assert resp.status_code == 422, resp.text
    assert calls == []
    assert "camera_config" not in recorded, "no picture is taken for a request that is refused"


@pytest.mark.unit
def test_test_capture_refuses_an_unknown_page_side(client, monkeypatch):
    _record_analyser(monkeypatch)
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendSingle({}))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0?layout=true&page_side=middle")

    assert resp.status_code == 422, resp.text


@pytest.mark.unit
def test_a_failing_analysis_never_fails_the_capture(client, monkeypatch, caplog):
    import logging

    _record_analyser(monkeypatch, error=RuntimeError("analyser broke"))
    image_bytes = _layout_scene()[0]
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendBytes(image_bytes))
    api = _client_as(client, "op", "operator")

    with caplog.at_level(logging.WARNING):
        resp = api.post("/cameras/test-capture/0?layout=true&page_side=left")

    assert resp.status_code == 200, resp.text
    assert "X-Page-Layout" not in resp.headers
    assert resp.content
    assert resp.headers["X-Capture-Bytes"] == str(len(resp.content))
    assert any("layout" in r.getMessage().lower() for r in caplog.records if r.levelno >= logging.WARNING)


@pytest.mark.unit
@pytest.mark.parametrize(
    "config, expected_rotation",
    [
        ({"camera_index": 0, "rotate_deg": 270}, 270),
        ({"camera_index": 0, "rotate_deg": 90}, 90),
        ({"camera_index": 0, "rotate_deg": 0}, 0),
        ({"camera_index": 0}, 90),
    ],
)
def test_the_saved_orientation_is_the_analysis_rotation(client, monkeypatch, config, expected_rotation):
    calls = _record_analyser(monkeypatch)
    _install_default_config(monkeypatch, config)
    _install_backend(monkeypatch, _FakeBackendBytes(_layout_scene()[0]))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0?layout=true&page_side=right")

    assert resp.status_code == 200, resp.text
    assert len(calls) == 1
    assert calls[0]["rotation_deg"] == expected_rotation
    assert calls[0]["page_side"] == "right"
    assert calls[0]["bytes"] == resp.content, "the analyser sees the bytes the route returns"


@pytest.mark.unit
@pytest.mark.parametrize("stored, expected_rotation", [(None, 90), (45, 90), (0, 0), (180, 180)])
def test_the_layout_header_uses_the_effective_orientation_from_the_registry(
    client, monkeypatch, stored, expected_rotation
):
    """No stub of the config builder: a never-set body is analysed turned 90."""
    from capture.camera_registry import CameraRegistry

    class _Registry(CameraRegistry):
        def __init__(self, *args, **kwargs):
            pass

        def get_camera_by_index(self, camera_index):
            return "hw", {"orientation": stored, "calibration": {}}

    monkeypatch.setattr(project_manager_module, "CameraRegistry", _Registry)
    calls = _record_analyser(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendBytes(_layout_scene()[0]))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0?layout=true&page_side=left")

    assert resp.status_code == 200, resp.text
    assert len(calls) == 1
    assert calls[0]["rotation_deg"] == expected_rotation


@pytest.mark.unit
def test_the_layout_header_stays_small_for_a_full_result(client, monkeypatch):
    import json

    full = {
        "found": True,
        "corners": [[0.123456789, 0.987654321]] * 4,
        "margins": {k: 0.123456789 for k in ("left", "right", "top", "bottom")},
        "gutter_margin": None,
        "fore_edge_margin": 0.123456789,
        "gutter_state": "at_or_beyond_edge",
        "verdict": "cut_off",
        "conditions": ["cut_off", "too_close", "too_small", "gutter_inside", "gutter_unknown"],
        "sides": {"left": "cut_off", "top": "too_close", "bottom": "cut_off"},
        "offset": {"along_gutter": -0.123456789},
        "fill": 0.987654321,
        "magnification_to_target": 1.23456789,
        "empty_space": 0.87654321,
        "gutter_uncertainty": 0.0123456789,
        "confidence": 0.87654321,
        "analysis_ms": 123.456789,
    }
    _record_analyser(monkeypatch, result=full)
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendBytes(_layout_scene()[0]))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0?layout=true&page_side=left")

    header = resp.headers["X-Page-Layout"]
    assert len(header.encode()) < 2048
    assert " " not in header, "compact JSON"
    layout = json.loads(header)
    assert layout["corners"][0] == [0.1235, 0.9877]
    assert layout["offset"]["along_gutter"] == -0.1235
    assert layout["gutter_margin"] is None
    assert layout["conditions"] == full["conditions"]
    assert layout["sides"] == full["sides"]


@pytest.mark.unit
def test_test_capture_exposes_the_page_layout_across_cors(client, monkeypatch):
    _record_analyser(monkeypatch)
    _install_default_config(monkeypatch)
    _install_backend(monkeypatch, _FakeBackendBytes(_layout_scene()[0]))
    api = _client_as(client, "op", "operator")

    resp = api.post(
        "/cameras/test-capture/0?layout=true&page_side=left",
        headers={"Origin": "http://localhost:5173"},
    )

    assert resp.status_code == 200, resp.text
    exposed = resp.headers.get("access-control-expose-headers", "")
    assert "X-Page-Layout" in {h.strip() for h in exposed.split(",")}
