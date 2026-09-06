"""list_devices() must serialise with the per-camera lock.

Enumeration reads self._sessions and calls get_config() on the open session.
Without the per-camera lock, an enumeration that races a session open sees no
cached session (the session is cached only after _PTPSession.__init__ returns)
and falls back to its temporary init() - a second PTP claim on a camera the
preview thread is already opening.

The fakes here model exactly that window: the session constructor claims the
camera and then blocks, so a concurrent list_devices() runs while the claim is
held but before the session is visible in self._sessions.
"""

import logging
import threading
import types

import pytest

import capture.backends.gphoto2_backend as gb


MODEL = "Canon EOS Rebel T7"
PORT = "usb:001,005"
SERIAL_RAW = "  3456789  "
SERIAL = "3456789"


class _Claims:
    """Counts PTP claims (gp Camera.init() calls) across threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self.init_calls = 0

    def claim(self):
        with self._lock:
            self.init_calls += 1


class _Widget:
    def __init__(self, value):
        self._value = value

    def get_value(self):
        return self._value


class _Config:
    def get_child_by_name(self, name):
        if name == "serialnumber":
            return _Widget(SERIAL_RAW)
        raise KeyError(name)


class _PreviewFile:
    def get_data_and_size(self):
        return b"jpeg-preview-bytes"


def _make_fake_gp(claims):
    class GPhoto2Error(Exception):
        pass

    class Camera:
        @staticmethod
        def autodetect():
            return [(MODEL, PORT)]

        def set_abilities(self, abilities):
            pass

        def set_port_info(self, port_info):
            pass

        def init(self):
            claims.claim()

        def exit(self):
            pass

        def get_config(self):
            return _Config()

        def capture_preview(self):
            return _PreviewFile()

    class CameraAbilitiesList:
        def load(self):
            pass

        def lookup_model(self, model):
            return 0

        def __getitem__(self, idx):
            return object()

    class PortInfoList:
        def load(self):
            pass

        def lookup_path(self, path):
            return 0

        def __getitem__(self, idx):
            return object()

    return types.SimpleNamespace(
        GPhoto2Error=GPhoto2Error,
        Camera=Camera,
        CameraAbilitiesList=CameraAbilitiesList,
        PortInfoList=PortInfoList,
    )


def _make_blocking_session_cls(fake_gp, claims, entered, release):
    class _BlockingPTPSession:
        """Claims the camera, then blocks before publishing self._cam."""

        def __init__(self, port, model, logger):
            self.port = port
            self.model = model
            self._cam = None
            cam = fake_gp.Camera()
            cam.init()  # the one legitimate PTP claim
            entered.set()
            if not release.wait(timeout=30):
                raise AssertionError("session constructor was never released")
            self._cam = cam

        def close(self):
            self._cam = None

    return _BlockingPTPSession


def _make_backend(monkeypatch, fake_gp):
    monkeypatch.setattr(gb, "gp", fake_gp)
    monkeypatch.setattr(gb, "_GP_AVAILABLE", True)
    return gb.GPhoto2Backend(logging.getLogger("test-gphoto2-backend"))


def test_list_devices_reports_port_and_reads_serial(monkeypatch):
    claims = _Claims()
    fake_gp = _make_fake_gp(claims)
    backend = _make_backend(monkeypatch, fake_gp)

    devices = backend.list_devices()

    assert len(devices) == 1
    device = devices[0]
    assert device["index"] == 0
    assert device["port"] == PORT
    assert device["model"] == MODEL
    assert device["serial"] == SERIAL
    assert device["hardware_id"] == "canoneosrebelt7_3456789"
    assert device["location"] == f"USB {PORT}"
    # No session was open, so a single brief claim is expected here.
    assert claims.init_calls == 1


def test_list_devices_does_not_claim_a_camera_being_opened(monkeypatch):
    claims = _Claims()
    fake_gp = _make_fake_gp(claims)
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(
        gb, "_PTPSession", _make_blocking_session_cls(fake_gp, claims, entered, release)
    )
    backend = _make_backend(monkeypatch, fake_gp)

    results = {}
    errors = {}

    def run_preview():
        try:
            results["preview"] = backend.capture_preview(0)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["preview"] = exc

    def run_enumerate():
        try:
            results["devices"] = backend.list_devices()
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["enumerate"] = exc

    opener = threading.Thread(target=run_preview, name="preview-opener")
    opener.start()
    assert entered.wait(10), "preview thread never reached the session constructor"

    enumerator = threading.Thread(target=run_enumerate, name="enumerator")
    enumerator.start()
    # The opener still holds the per-camera lock and has not published its
    # session yet. A correctly locked list_devices() must block here; an
    # unlocked one runs straight through and opens its own PTP session.
    enumerator.join(timeout=2.0)
    blocked_on_lock = enumerator.is_alive()

    release.set()
    opener.join(timeout=30)
    enumerator.join(timeout=30)

    assert not errors, errors
    assert not opener.is_alive() and not enumerator.is_alive()
    assert claims.init_calls == 1, (
        f"expected 1 PTP claim, got {claims.init_calls} "
        f"(concurrent extra claims: {claims.init_calls - 1})"
    )
    assert blocked_on_lock, (
        "list_devices() enumerated while the per-camera lock was held; "
        "it does not serialise with session opening"
    )

    devices = results["devices"]
    assert len(devices) == 1
    assert devices[0]["port"] == PORT
    assert devices[0]["serial"] == SERIAL, "enumeration should reuse the open session"
    assert devices[0]["hardware_id"] == "canoneosrebelt7_3456789"
    assert results["preview"] == b"jpeg-preview-bytes"


def test_list_devices_closes_camera_when_serial_read_raises(monkeypatch):
    """A temporary-session enumeration (no cached session) must still call
    exit() when get_config()/the serial read raises after init() succeeded.

    Without a finally, an exception between init() and exit() leaves the lock
    released but the camera claimed by the dangling `cam` object - a resource
    leak that keeps the camera unusable for the next open. The device should
    still be reported, with serial None and an "_idxN" hardware id fallback.
    """
    claims = _Claims()
    fake_gp = _make_fake_gp(claims)

    exit_calls = {"count": 0}
    original_exit = fake_gp.Camera.exit
    original_get_config = fake_gp.Camera.get_config

    def _raising_get_config(self):
        raise RuntimeError("boom: config read failed")

    def _counting_exit(self):
        exit_calls["count"] += 1
        original_exit(self)

    fake_gp.Camera.get_config = _raising_get_config
    fake_gp.Camera.exit = _counting_exit

    backend = _make_backend(monkeypatch, fake_gp)

    devices = backend.list_devices()

    assert len(devices) == 1
    device = devices[0]
    assert device["index"] == 0
    assert device["serial"] is None
    assert device["hardware_id"] == "canoneosrebelt7_idx0"
    assert exit_calls["count"] == 1, (
        f"expected exit() called exactly once, got {exit_calls['count']}"
    )

    # restore, defensive against any future test using the same fake_gp instance
    fake_gp.Camera.get_config = original_get_config
    fake_gp.Camera.exit = original_exit
