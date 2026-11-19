"""A failed DSLR preview frame must not close the camera's session.

A Canon body can answer one capture_preview() with a bare [-1] Unspecified
error and the next one normally. Closing the session on that one frame made
the preview route answer 404, which the Overview page reads as "Not
detected" and stops polling, while the body was still on the bus and test
captures still worked.

These tests pin the preview path: a failed frame is retried once, outside
the camera lock and only if the lock is free; a frame that still fails raises
PreviewFrameFailedError without closing the session; the session is closed
only when the port map, re-detected, does not back it, or when a run of
consecutive failures has lasted _ROUTINE_SCAN_MIN_S. A bus scan that raises
is not an absent camera: is_camera_connected() keeps the last published
answer. The route answers 503 with code preview_frame_failed only for
PreviewFrameFailedError, 404 for every other RuntimeError, and never asks
the backend a second time.

Version: 0.1.0-beta
"""

import logging
from types import SimpleNamespace

import pytest

import threading

import capture.backends.gphoto2_backend as gb
from capture.backends.errors import CameraNotConnectedError, PreviewFrameFailedError

pytestmark = pytest.mark.unit

MODEL = "Canon EOS 1500D"
PORT = "usb:001,004"
FRAME = b"\xff\xd8preview-frame"


class _GPError(Exception):
    pass


class _Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


class _Cam:
    """Fake gp.Camera: each capture_preview() takes the next scripted result."""

    def __init__(self, results):
        self.results = list(results)
        self.preview_calls = 0

    def capture_preview(self):
        self.preview_calls += 1
        result = self.results.pop(0) if self.results else FRAME
        if callable(result):
            result = result()
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(get_data_and_size=lambda: result)


class _Session:
    def __init__(self, cam):
        self._cam = cam
        self.port = PORT
        self.model = MODEL
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self._cam = None


def _backend(monkeypatch, results, port_map=None):
    monkeypatch.setattr(gb, "_GP_AVAILABLE", True)
    monkeypatch.setattr(gb, "gp", SimpleNamespace(GPhoto2Error=_GPError))
    monkeypatch.setattr(gb, "_PREVIEW_RETRY_DELAY_S", 0.0, raising=False)
    monkeypatch.setattr(gb, "_port_node_exists", lambda port: True)
    clock = _Clock()
    monkeypatch.setattr(gb, "_monotonic", clock)
    backend = gb.GPhoto2Backend(logging.getLogger("test-gphoto2-preview-retry"))
    state = SimpleNamespace(
        port_map=dict(port_map if port_map is not None else {0: (MODEL, PORT)}),
        refreshes=0,
        clock=clock,
    )
    # The published map is the state's map itself, so a test that changes
    # one changes the other.
    backend._port_map = state.port_map
    monkeypatch.setattr(backend, "_get_port_map", lambda: dict(state.port_map))

    def refresh():
        state.refreshes += 1

    monkeypatch.setattr(backend, "_refresh_port_map", refresh)
    session = _Session(_Cam(results))
    backend._sessions[0] = session
    return backend, session, state


def _unspecified():
    return _GPError("[-1] Unspecified error")


def test_one_failed_frame_is_retried_and_the_session_stays_open(monkeypatch):
    backend, session, _state = _backend(monkeypatch, [_unspecified(), FRAME])

    assert backend.capture_preview(0) == FRAME

    assert session._cam.preview_calls == 2
    assert session.close_calls == 0
    assert backend._sessions.get(0) is session


def test_a_frame_that_fails_twice_is_reported_without_closing_the_session(monkeypatch):
    backend, session, _state = _backend(
        monkeypatch, [_unspecified(), _unspecified(), FRAME]
    )

    with pytest.raises(PreviewFrameFailedError, match="preview capture failed"):
        backend.capture_preview(0)

    assert session.close_calls == 0
    assert backend._sessions.get(0) is session
    # The next poll is served by the same session.
    assert backend.capture_preview(0) == FRAME
    assert session.close_calls == 0


def test_a_body_whose_port_node_is_gone_is_re_detected_and_its_session_closed(monkeypatch):
    backend, session, state = _backend(monkeypatch, [_unspecified()])
    monkeypatch.setattr(gb, "_port_node_exists", lambda port: False)

    def refresh():
        state.refreshes += 1
        state.port_map.clear()

    monkeypatch.setattr(backend, "_refresh_port_map", refresh)

    with pytest.raises(CameraNotConnectedError, match="not found"):
        backend.capture_preview(0)

    assert state.refreshes == 1
    assert session.close_calls == 1
    assert 0 not in backend._sessions


def test_a_node_that_is_still_present_does_not_trigger_a_re_detect(monkeypatch):
    backend, _session, state = _backend(monkeypatch, [_unspecified(), FRAME])

    assert backend.capture_preview(0) == FRAME
    assert state.refreshes == 0


class _BusyAfterFirstLock:
    """A camera lock that something else takes as soon as the first frame fails."""

    def __init__(self):
        self.blocking_acquires = 0
        self.nonblocking_acquires = 0

    def __enter__(self):
        self.blocking_acquires += 1
        return self

    def __exit__(self, *exc):
        return False

    def acquire(self, blocking=True):
        if blocking:
            self.blocking_acquires += 1
            return True
        self.nonblocking_acquires += 1
        return False

    def release(self):
        pass


def test_the_retry_gives_way_to_a_capture_waiting_on_the_lock(monkeypatch):
    backend, session, _state = _backend(monkeypatch, [_unspecified(), FRAME])
    lock = _BusyAfterFirstLock()
    monkeypatch.setattr(backend, "_get_camera_lock", lambda index: lock)

    with pytest.raises(PreviewFrameFailedError, match="preview capture failed"):
        backend.capture_preview(0)

    # One blocking acquire for the first frame; the retry only tried.
    assert lock.blocking_acquires == 1
    assert lock.nonblocking_acquires == 1
    assert session._cam.preview_calls == 1
    assert session.close_calls == 0


def test_a_session_that_fails_every_frame_for_the_scan_interval_is_closed(monkeypatch):
    backend, session, state = _backend(monkeypatch, [_unspecified()] * 20)

    with pytest.raises(RuntimeError):
        backend.capture_preview(0)
    state.clock.now += gb._ROUTINE_SCAN_MIN_S - 1
    with pytest.raises(RuntimeError):
        backend.capture_preview(0)
    assert session.close_calls == 0

    state.clock.now += 1
    with pytest.raises(PreviewFrameFailedError, match="preview capture failed"):
        backend.capture_preview(0)
    assert session.close_calls == 1
    assert 0 not in backend._sessions


def test_a_good_frame_resets_the_failure_window(monkeypatch):
    backend, session, state = _backend(
        monkeypatch,
        [_unspecified(), _unspecified(), FRAME, _unspecified(), _unspecified()],
    )

    with pytest.raises(RuntimeError):
        backend.capture_preview(0)
    state.clock.now += gb._ROUTINE_SCAN_MIN_S - 1
    assert backend.capture_preview(0) == FRAME
    state.clock.now += gb._ROUTINE_SCAN_MIN_S - 1
    with pytest.raises(RuntimeError):
        backend.capture_preview(0)

    assert session.close_calls == 0


def test_a_failure_long_after_the_last_one_starts_a_new_window(monkeypatch):
    backend, session, state = _backend(monkeypatch, [_unspecified()] * 4)

    with pytest.raises(PreviewFrameFailedError):
        backend.capture_preview(0)
    # An hour with no preview polling, then one failed poll.
    state.clock.now += 3600
    with pytest.raises(PreviewFrameFailedError):
        backend.capture_preview(0)

    assert session.close_calls == 0
    assert backend._sessions.get(0) is session


def test_a_dead_session_polled_every_ten_seconds_is_closed(monkeypatch):
    backend, session, state = _backend(monkeypatch, [_unspecified()] * 4)

    with pytest.raises(PreviewFrameFailedError):
        backend.capture_preview(0)
    state.clock.now += 10.5
    with pytest.raises(PreviewFrameFailedError):
        backend.capture_preview(0)

    assert session.close_calls == 1
    assert 0 not in backend._sessions


def test_a_successful_capture_ends_the_failure_run(monkeypatch, tmp_path):
    backend, session, state = _backend(monkeypatch, [_unspecified()] * 4)
    session.apply_dslr_config = lambda config: None
    session.capture = lambda path, **kw: (0.1, path)

    with pytest.raises(PreviewFrameFailedError):
        backend.capture_preview(0)
    state.clock.now += 5
    backend.capture_image(tmp_path / "a.jpg", SimpleNamespace(camera_index=0))
    state.clock.now += 5
    with pytest.raises(PreviewFrameFailedError):
        backend.capture_preview(0)

    assert session.close_calls == 0
    assert backend._sessions.get(0) is session


def test_a_run_of_failures_on_a_closed_session_does_not_close_its_successor(
    monkeypatch, tmp_path
):
    backend, session_a, state = _backend(monkeypatch, [_unspecified()] * 10)
    # Polls at t=0, 2, 4, 6 and 8 all fail on session A: one run, not yet
    # long enough to close it.
    for _ in range(5):
        with pytest.raises(PreviewFrameFailedError):
            backend.capture_preview(0)
        state.clock.now += 2
    assert session_a.close_calls == 0

    # A failed capture closes A and leaves the record of A's run in place.
    def failing_capture(path, **kw):
        raise _GPError("[-1] Unspecified error")

    session_a.apply_dslr_config = lambda config: None
    session_a.capture = failing_capture
    with pytest.raises(RuntimeError, match="DSLR capture failed"):
        backend.capture_image(tmp_path / "a.jpg", SimpleNamespace(camera_index=0))
    assert session_a.close_calls == 1
    assert backend._preview_failing_since[0][0] is session_a

    # Session B opens on the next poll, which fails at t=12: twelve seconds
    # after A's run began, four after its last failure.
    session_b = _Session(_Cam([_unspecified()] * 2))
    monkeypatch.setattr(
        backend, "_open_identified_session", lambda *args: session_b
    )
    state.clock.now += 2
    with pytest.raises(PreviewFrameFailedError):
        backend.capture_preview(0)

    assert session_b.close_calls == 0
    assert backend._sessions.get(0) is session_b
    assert backend._preview_failing_since[0][0] is session_b


def test_the_retry_releases_a_real_lock_when_the_index_is_gone(monkeypatch):
    backend, session, state = _backend(monkeypatch, [_unspecified()])
    lock = threading.Lock()
    monkeypatch.setattr(backend, "_get_camera_lock", lambda index: lock)
    monkeypatch.setattr(gb, "_port_node_exists", lambda port: False)

    def refresh():
        state.refreshes += 1
        state.port_map.clear()

    monkeypatch.setattr(backend, "_refresh_port_map", refresh)

    with pytest.raises(CameraNotConnectedError):
        backend.capture_preview(0)

    assert not lock.locked()


def test_the_retry_releases_a_real_lock_on_the_wrong_body(monkeypatch):
    backend, session, _state = _backend(monkeypatch, [_unspecified()])
    lock = threading.Lock()
    monkeypatch.setattr(backend, "_get_camera_lock", lambda index: lock)
    calls = []

    def resolve(index, *args):
        calls.append(index)
        if len(calls) == 1:
            return session
        raise gb.CameraIdentityError("another body answered")

    monkeypatch.setattr(backend, "_get_or_open_session", resolve)

    with pytest.raises(gb.CameraIdentityError):
        backend.capture_preview(0)

    assert len(calls) == 2
    assert not lock.locked()


def test_the_retry_gives_way_to_a_capture_holding_a_real_lock(monkeypatch):
    backend, session, _state = _backend(monkeypatch, [_unspecified(), FRAME])
    lock = threading.Lock()
    monkeypatch.setattr(backend, "_get_camera_lock", lambda index: lock)
    taken = threading.Event()
    done = threading.Event()

    def simulated_capture():
        with lock:
            taken.set()
            done.wait(5)

    def node_exists(port):
        # Called between the two attempts, with the lock released: a capture
        # takes the lock exactly then.
        threading.Thread(target=simulated_capture, daemon=True).start()
        assert taken.wait(5)
        return True

    monkeypatch.setattr(gb, "_port_node_exists", node_exists)
    try:
        with pytest.raises(PreviewFrameFailedError, match="camera busy"):
            backend.capture_preview(0)
        assert session._cam.preview_calls == 1
        assert lock.locked()
    finally:
        done.set()


# ----------------------------------------------------------------------
# is_camera_connected when the bus scan itself fails
# ----------------------------------------------------------------------


class _Bus:
    """Fake gp.Camera.autodetect(): a list of (model, port), or an error."""

    def __init__(self, detected):
        self.detected = detected
        self.calls = 0

    def autodetect(self):
        self.calls += 1
        if isinstance(self.detected, Exception):
            raise self.detected
        return [list(entry) for entry in self.detected]


def _scanning_backend(monkeypatch, detected):
    monkeypatch.setattr(gb, "_GP_AVAILABLE", True)
    bus = _Bus(detected)
    monkeypatch.setattr(gb, "gp", SimpleNamespace(GPhoto2Error=_GPError, Camera=bus))
    monkeypatch.setattr(gb, "_PREVIEW_RETRY_DELAY_S", 0.0, raising=False)
    # The ports whose usbfs node exists; none until a test says so.
    nodes = set()
    monkeypatch.setattr(gb, "_port_node_exists", lambda port: port in nodes)
    clock = _Clock()
    monkeypatch.setattr(gb, "_monotonic", clock)
    backend = gb.GPhoto2Backend(logging.getLogger("test-gphoto2-preview-scan"))
    backend._pins.clear()
    backend._seeded_serials.clear()
    return backend, bus, clock, nodes


def _scan_errors(caplog):
    return [
        r for r in caplog.records
        if r.levelno >= logging.ERROR and "is_camera_connected" in r.getMessage()
    ]


def test_a_failed_scan_keeps_a_connected_camera_connected(monkeypatch, caplog):
    backend, bus, clock, nodes = _scanning_backend(monkeypatch, [(MODEL, PORT)])
    nodes.add(PORT)
    assert backend.is_camera_connected(0) is True

    bus.detected = _GPError("[-7] I/O problem")
    caplog.set_level(logging.INFO)
    for _ in range(3):
        clock.now += gb._ROUTINE_SCAN_MIN_S + 1
        assert backend.is_camera_connected(0) is True

    assert bus.calls == 4
    # One error for the run of failures, not one per call.
    assert len(_scan_errors(caplog)) == 1


def test_a_failed_scan_keeps_an_absent_camera_absent(monkeypatch):
    backend, bus, clock, nodes = _scanning_backend(monkeypatch, [(MODEL, PORT)])
    assert backend.is_camera_connected(1) is False

    bus.detected = _GPError("[-7] I/O problem")
    clock.now += gb._ROUTINE_SCAN_MIN_S + 1
    assert backend.is_camera_connected(1) is False


def test_a_failed_scan_does_not_keep_a_body_whose_node_is_gone(monkeypatch):
    backend, bus, clock, nodes = _scanning_backend(monkeypatch, [(MODEL, PORT)])
    nodes.add(PORT)
    assert backend.is_camera_connected(0) is True

    # Unplugged: the node goes, and the scan that would say so fails.
    nodes.discard(PORT)
    bus.detected = _GPError("[-7] I/O problem")
    assert backend.is_camera_connected(0) is False
    clock.now += gb._ROUTINE_SCAN_MIN_S + 1
    assert backend.is_camera_connected(0) is False


def test_a_scan_that_succeeds_without_the_body_still_answers_false(monkeypatch, caplog):
    backend, bus, clock, nodes = _scanning_backend(monkeypatch, [(MODEL, PORT)])
    nodes.add(PORT)
    assert backend.is_camera_connected(0) is True

    bus.detected = _GPError("[-7] I/O problem")
    clock.now += gb._ROUTINE_SCAN_MIN_S + 1
    assert backend.is_camera_connected(0) is True

    bus.detected = []
    clock.now += gb._ROUTINE_SCAN_MIN_S + 1
    caplog.set_level(logging.INFO)
    assert backend.is_camera_connected(0) is False
    assert any("bus scan" in r.getMessage() and r.levelno == logging.INFO
               for r in caplog.records)


def test_the_failure_log_rearms_after_a_good_scan(monkeypatch, caplog):
    backend, bus, clock, nodes = _scanning_backend(monkeypatch, [(MODEL, PORT)])
    caplog.set_level(logging.INFO)
    for detected in (_GPError("a"), _GPError("b"), [(MODEL, PORT)], _GPError("c")):
        bus.detected = detected
        clock.now += gb._ROUTINE_SCAN_MIN_S + 1
        backend.is_camera_connected(0)

    assert len(_scan_errors(caplog)) == 2


# ----------------------------------------------------------------------
# The preview route
# ----------------------------------------------------------------------


def _user(username, role):
    from app.models.user import User

    return User(
        username=username,
        email=f"{username}@example.com",
        hashed_password="x",
        role=role,
        is_active=True,
    )


@pytest.fixture
def api(client):
    from app.main import app
    from app.api.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _user("op", "operator")
    return client


def _route_with(monkeypatch, raises):
    """The route over a stub capture service; returns the connection checks made."""
    import capture.service as capture_service

    asked = []

    def frame(camera_index, resolution):
        if raises is None:
            return FRAME
        raise raises

    monkeypatch.setattr(capture_service, "capture_preview_frame", frame)
    monkeypatch.setattr(
        capture_service, "is_camera_connected",
        lambda index: asked.append(index) or True,
    )
    return asked


def test_the_route_answers_503_for_a_failed_frame_on_a_connected_camera(monkeypatch, api):
    asked = _route_with(
        monkeypatch,
        PreviewFrameFailedError("DSLR preview capture failed: [-1] Unspecified error"),
    )

    resp = api.get("/cameras/preview/0")

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "preview_frame_failed"
    assert asked == []


def test_the_route_answers_404_for_a_camera_that_is_not_connected(monkeypatch, api):
    asked = _route_with(monkeypatch, CameraNotConnectedError("Camera 0 is not connected"))

    resp = api.get("/cameras/preview/0")

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Camera 0 is not connected"
    assert asked == []


def test_the_route_answers_404_for_the_wrong_body_at_an_index(monkeypatch, api):
    asked = _route_with(monkeypatch, gb.CameraIdentityError("wrong body"))

    resp = api.get("/cameras/preview/0")

    assert resp.status_code == 404, resp.text
    assert asked == []


def test_any_other_runtime_error_stays_a_404_without_asking_again(monkeypatch, api):
    # A CHDK body that would not switch to shooting mode is evicted and then
    # re-opened by the next connection check, so asking again would answer
    # "connected" every time and hide the remedy in the message.
    message = (
        "usb:001,005: the camera did not enter shooting mode, so it can "
        "neither preview nor capture"
    )
    asked = _route_with(monkeypatch, RuntimeError(message))

    resp = api.get("/cameras/preview/0")

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == message
    assert asked == []


# The route over the real capture service and a GPhoto2Backend whose bus is
# faked at gp.Camera.autodetect().


def _route_over(monkeypatch, backend):
    import capture.service as capture_service

    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)


@pytest.mark.parametrize(
    "detected",
    [[(MODEL, PORT)], []],
    ids=["other-camera-present", "no-camera-at-all"],
)
def test_a_404_for_an_absent_camera_costs_one_bus_scan(monkeypatch, api, detected):
    backend, bus, clock, nodes = _scanning_backend(monkeypatch, detected)
    _route_over(monkeypatch, backend)
    backend.is_camera_connected(0)
    clock.now += gb._ROUTINE_SCAN_MIN_S + 1
    bus.calls = 0

    resp = api.get("/cameras/preview/1")

    assert resp.status_code == 404, resp.text
    assert bus.calls == 1


def _live_session(backend, results):
    session = _Session(_Cam(results))
    backend._sessions[0] = session
    return session


def test_a_good_frame_is_served_while_the_bus_scan_fails(monkeypatch, api):
    backend, bus, clock, nodes = _scanning_backend(monkeypatch, [(MODEL, PORT)])
    nodes.add(PORT)
    _route_over(monkeypatch, backend)
    assert backend.is_camera_connected(0) is True
    _live_session(backend, [FRAME])
    bus.detected = _GPError("[-7] I/O problem")
    clock.now += gb._ROUTINE_SCAN_MIN_S + 1

    resp = api.get("/cameras/preview/0")

    assert resp.status_code == 200, resp.text
    assert resp.content == FRAME


def test_a_failed_frame_while_the_bus_scan_fails_is_a_503(monkeypatch, api):
    backend, bus, clock, nodes = _scanning_backend(monkeypatch, [(MODEL, PORT)])
    nodes.add(PORT)
    _route_over(monkeypatch, backend)
    assert backend.is_camera_connected(0) is True
    session = _live_session(backend, [_unspecified(), _unspecified()])
    bus.detected = _GPError("[-7] I/O problem")
    clock.now += gb._ROUTINE_SCAN_MIN_S + 1

    resp = api.get("/cameras/preview/0")

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "preview_frame_failed"
    assert session.close_calls == 0


@pytest.mark.parametrize("with_session", [True, False], ids=["session", "no-session"])
def test_a_body_whose_node_is_gone_is_a_404_while_the_bus_scan_fails(
    monkeypatch, api, with_session
):
    backend, bus, clock, nodes = _scanning_backend(monkeypatch, [(MODEL, PORT)])
    _route_over(monkeypatch, backend)
    nodes.add(PORT)
    assert backend.is_camera_connected(0) is True
    session = _live_session(backend, [_unspecified()] * 4) if with_session else None
    opened = []
    monkeypatch.setattr(
        backend, "_open_identified_session",
        lambda *args: opened.append(args) or _Session(_Cam([FRAME])),
    )
    nodes.discard(PORT)
    bus.detected = _GPError("[-7] I/O problem")

    for _ in range(3):
        clock.now += 2
        resp = api.get("/cameras/preview/0")
        assert resp.status_code == 404, resp.text

    assert opened == []
    if session is not None:
        assert session._cam.preview_calls == 0


def test_a_failed_re_detect_with_the_node_gone_is_a_404_and_closes_the_session(
    monkeypatch, api
):
    backend, bus, clock, nodes = _scanning_backend(monkeypatch, [(MODEL, PORT)])
    _route_over(monkeypatch, backend)
    nodes.add(PORT)
    assert backend.is_camera_connected(0) is True
    session = _live_session(backend, [_unspecified(), FRAME])
    bus.detected = _GPError("[-7] I/O problem")
    # Present for the connection check, gone from the retry on.
    checks = iter([True])
    monkeypatch.setattr(gb, "_port_node_exists", lambda port: next(checks, False))

    resp = api.get("/cameras/preview/0")

    assert resp.status_code == 404, resp.text
    assert session.close_calls == 1
    assert 0 not in backend._sessions
    assert 0 not in backend._preview_failing_since


def test_a_failed_re_detect_with_the_node_back_is_a_503(monkeypatch, api):
    backend, bus, clock, nodes = _scanning_backend(monkeypatch, [(MODEL, PORT)])
    _route_over(monkeypatch, backend)
    nodes.add(PORT)
    assert backend.is_camera_connected(0) is True
    session = _live_session(backend, [_unspecified(), FRAME])
    bus.detected = _GPError("[-7] I/O problem")
    # Present for the connection check, gone at the retry, present again
    # once the re-detect has failed.
    checks = iter([True, False, True])
    monkeypatch.setattr(gb, "_port_node_exists", lambda port: next(checks))

    resp = api.get("/cameras/preview/0")

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "preview_frame_failed"
    assert session.close_calls == 0
    assert backend._sessions.get(0) is session


# ----------------------------------------------------------------------
# One decision between "no frame" and "not connected"
# ----------------------------------------------------------------------


class _WatchedLock:
    """A real lock that sets `waiting` when a second blocking acquire begins."""

    def __init__(self):
        self._lock = threading.Lock()
        self.blocking_acquires = 0
        self.waiting = threading.Event()

    def acquire(self, blocking=True):
        if blocking:
            self.blocking_acquires += 1
            if self.blocking_acquires > 1:
                self.waiting.set()
        return self._lock.acquire(blocking)

    def release(self):
        self._lock.release()

    def locked(self):
        return self._lock.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def _scans_under(bus, lock):
    """Wrap bus.autodetect; returns the list of calls made with the lock held."""
    under_lock = []
    real = bus.autodetect

    def autodetect():
        if lock.locked():
            under_lock.append(bus.calls + 1)
        return real()

    bus.autodetect = autodetect
    return under_lock


def test_a_completed_re_detect_without_the_body_is_a_404_when_the_lock_is_busy(
    monkeypatch,
):
    # K1: the node is gone at the retry, the re-detect completes without the
    # body, and a capture then holds the lock, so the retry is skipped.
    backend, bus, clock, nodes = _scanning_backend(monkeypatch, [(MODEL, PORT)])
    nodes.add(PORT)
    assert backend.is_camera_connected(0) is True
    lock = _WatchedLock()
    monkeypatch.setattr(backend, "_get_camera_lock", lambda index: lock)
    taken = threading.Event()

    def holder():
        with lock._lock:
            taken.set()
            lock.waiting.wait(5)

    def unplug_and_fail():
        nodes.discard(PORT)
        bus.detected = []
        return _unspecified()

    session = _live_session(backend, [unplug_and_fail])
    real_autodetect = bus.autodetect

    def autodetect():
        result = real_autodetect()
        threading.Thread(target=holder, daemon=True).start()
        assert taken.wait(5)
        return result

    bus.autodetect = autodetect
    try:
        with pytest.raises(CameraNotConnectedError):
            backend.capture_preview(0)
    finally:
        lock.waiting.set()

    assert bus.calls == 2
    assert session.close_calls == 1
    assert 0 not in backend._sessions


def test_a_scan_published_during_the_second_frame_is_read_before_answering(
    monkeypatch,
):
    # K2: while the retried frame waits on USB I/O, another thread's
    # connection check publishes a scan without this camera.
    backend, bus, clock, nodes = _scanning_backend(monkeypatch, [(MODEL, PORT)])
    nodes.add(PORT)
    assert backend.is_camera_connected(0) is True
    lock = threading.Lock()
    monkeypatch.setattr(backend, "_get_camera_lock", lambda index: lock)
    answers = []

    def check_from_another_thread():
        bus.detected = []
        clock.now += gb._ROUTINE_SCAN_MIN_S + 1
        answers.append(backend.is_camera_connected(0))

    def published_during_the_frame():
        thread = threading.Thread(target=check_from_another_thread, daemon=True)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        return _unspecified()

    session = _live_session(backend, [_unspecified(), published_during_the_frame])

    with pytest.raises(CameraNotConnectedError):
        backend.capture_preview(0)

    assert answers == [False]
    assert session.close_calls == 1
    assert 0 not in backend._sessions
    assert not lock.locked()


def test_another_body_at_the_index_after_the_second_frame_is_a_404(monkeypatch):
    backend, session, state = _backend(monkeypatch, [])

    def another_body_published():
        state.port_map[0] = ("Canon EOS 2000D", "usb:001,009")
        return _unspecified()

    session._cam = _Cam([_unspecified(), another_body_published])

    with pytest.raises(CameraNotConnectedError):
        backend.capture_preview(0)

    assert session.close_calls == 1
    assert 0 not in backend._sessions
    assert 0 not in backend._preview_failing_since


def test_two_pollers_skipping_each_others_retries_still_close_a_dead_session(
    monkeypatch,
):
    # K3: two pollers alternate on a dead session whose calls fail slowly;
    # each finds the lock held by the other when it comes to retry.
    backend, session, state = _backend(monkeypatch, [])
    lock = threading.Lock()
    monkeypatch.setattr(backend, "_get_camera_lock", lambda index: lock)
    polls = 10
    entered = [threading.Event() for _ in range(polls)]
    go = [threading.Event() for _ in range(polls)]
    at_gap = [threading.Event() for _ in range(polls)]
    resume = [threading.Event() for _ in range(polls)]
    local = threading.local()

    class _SlowDeadCam:
        preview_calls = 0

        def capture_preview(self):
            self.preview_calls += 1
            entered[local.k].set()
            go[local.k].wait(5)
            raise _unspecified()

    class _FreshCam:
        def capture_preview(self):
            entered[local.k].set()
            return SimpleNamespace(get_data_and_size=lambda: FRAME)

    session._cam = _SlowDeadCam()
    fresh = _Session(_FreshCam())
    monkeypatch.setattr(backend, "_open_identified_session", lambda *args: fresh)

    def node_exists(port):
        # The first call in a poll is the check between its two attempts,
        # made with the lock released.
        if not getattr(local, "past_gap", False):
            local.past_gap = True
            at_gap[local.k].set()
            resume[local.k].wait(5)
        return True

    monkeypatch.setattr(gb, "_port_node_exists", node_exists)
    results = [None] * polls
    threads = []

    def poll(k):
        local.k = k
        try:
            results[k] = backend.capture_preview(0)
        except Exception as exc:
            results[k] = exc

    def start(k):
        thread = threading.Thread(target=poll, args=(k,), daemon=True)
        thread.start()
        threads.append(thread)

    closed_at = None
    try:
        start(0)
        assert entered[0].wait(5)
        for k in range(polls - 1):
            state.clock.now += 2
            go[k].set()
            assert at_gap[k].wait(5)
            start(k + 1)
            assert entered[k + 1].wait(5)
            resume[k].set()
            threads[k].join(5)
            assert not threads[k].is_alive()
            if backend._sessions.get(0) is not session:
                closed_at = k + 1
                break
    finally:
        for event in go + resume:
            event.set()
        for thread in threads:
            thread.join(5)

    # Failures recorded at 2 s intervals from poll 0: poll 5's exit finds the
    # run spanning _ROUTINE_SCAN_MIN_S while poll 6 holds the lock, so the
    # close falls to the next acquisition, poll 7's.
    assert closed_at == 7
    assert session.close_calls == 1
    assert all(isinstance(r, PreviewFrameFailedError) for r in results[:6])
    # Poll 6's retry may find the lock free once poll 7 is done with it.
    assert isinstance(results[6], PreviewFrameFailedError) or results[6] == FRAME
    assert results[7] == FRAME


def test_the_retry_after_an_empty_re_detect_does_not_scan_under_the_lock(
    monkeypatch,
):
    # K4: the re-detect publishes an empty map; the retry must read it as
    # "no bodies" rather than scan again with the camera lock held.
    backend, bus, clock, nodes = _scanning_backend(monkeypatch, [(MODEL, PORT)])
    nodes.add(PORT)
    assert backend.is_camera_connected(0) is True
    lock = threading.Lock()
    monkeypatch.setattr(backend, "_get_camera_lock", lambda index: lock)
    under_lock = _scans_under(bus, lock)
    real_autodetect = bus.autodetect

    def autodetect():
        result = real_autodetect()
        # Any scan after the re-detect fails.
        bus.detected = _GPError("[-7] I/O problem")
        return result

    bus.autodetect = autodetect

    def unplug_and_fail():
        nodes.discard(PORT)
        bus.detected = []
        return _unspecified()

    session = _live_session(backend, [unplug_and_fail])

    with pytest.raises(CameraNotConnectedError):
        backend.capture_preview(0)

    assert under_lock == []
    assert session.close_calls == 1
    assert not lock.locked()


def test_an_unknown_model_on_open_does_not_scan_under_the_lock(monkeypatch):
    backend, bus, clock, nodes = _scanning_backend(monkeypatch, [(MODEL, PORT)])
    nodes.add(PORT)
    assert backend.is_camera_connected(0) is True
    lock = threading.Lock()
    monkeypatch.setattr(backend, "_get_camera_lock", lambda index: lock)
    under_lock = _scans_under(bus, lock)

    def unknown_model(port, model, logger):
        raise _GPError("[-105] Unknown model")

    monkeypatch.setattr(gb, "_PTPSession", unknown_model)

    with pytest.raises(_GPError, match="-105"):
        backend.capture_preview(0)

    assert under_lock == []
    assert not lock.locked()


def test_the_wrong_body_on_open_does_not_scan_under_the_lock(monkeypatch):
    backend, bus, clock, nodes = _scanning_backend(monkeypatch, [(MODEL, PORT)])
    nodes.add(PORT)
    assert backend.is_camera_connected(0) is True
    backend._pins[0] = "PINNED-BODY"
    lock = threading.Lock()
    monkeypatch.setattr(backend, "_get_camera_lock", lambda index: lock)
    under_lock = _scans_under(bus, lock)

    def other_body(port, model, logger):
        opened = _Session(_Cam([FRAME]))
        opened.serial = "OTHER-BODY"
        opened.body_model = None
        return opened

    monkeypatch.setattr(gb, "_PTPSession", other_body)

    with pytest.raises(gb.CameraIdentityError):
        backend.capture_preview(0)

    assert under_lock == []
    assert not lock.locked()
