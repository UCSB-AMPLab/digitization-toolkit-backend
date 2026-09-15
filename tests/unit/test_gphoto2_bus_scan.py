"""A USB bus scan must not race the bodies it is scanning (NEH-250).

`gp.Camera.autodetect()` re-enumerates the USB ports, and the frontend asks
`is_camera_connected()` about once a second per body. An unconditional scan
per ask is 60 bus enumerations a minute, and `_map_lock` guards the map, not
the bodies - so such a scan overlaps whatever preview or capture is in
flight. The Rionegro unit's `capture_service.log` for 9-15 Sep is what that
looks like from the operator's side: 6x `[-52] Could not find the requested
device on the USB port` (libgphoto2 re-enumerating ports while another thread
holds that device) and 5x `[-110] I/O in progress` (two operations
overlapping on one body). Enumeration never failed; using a body afterwards
did.

Four outcomes are asserted here, on what an operator or a caller can
observe rather than on which locks get taken:

  - a scan cannot begin while an operation is in flight on a body;
  - many rapid connection checks cost far fewer than that many scans, and a
    body that has genuinely gone is still reported as gone;
  - a body power-cycled off the bus is reported gone within the interval;
  - a body in use answers for itself, without the bus and without waiting.

The clock is faked so the scan interval can be crossed deliberately rather
than waited out: `_PORT_MAP_TTL_S` is the real constant in every assertion.
"""

import logging
import threading
import time
import types

import pytest

import capture.backends.gphoto2_backend as gb


@pytest.fixture(autouse=True)
def isolated_projects_root(monkeypatch, tmp_path):
    """Keep this file's backends off any real projects root.

    GPhoto2Backend persists its index -> body bindings to
    <projects_dir>/camera-bindings.json and seeds them at construction, so
    without a per-test root one test's rig would seed the next test's backend.
    """
    from app.core.config import settings

    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "PROJECTS_ROOT", str(root))
    return root


MODEL = "Canon EOS 1500D"
PORT_0 = "usb:001,004"
PORT_1 = "usb:001,005"
SERIAL_0 = "1111111"
SERIAL_1 = "2222222"
SERIAL_BY_PORT = {PORT_0: SERIAL_0, PORT_1: SERIAL_1}


class _Clock:
    """A monotonic clock the test moves by hand.

    The backend reads time.monotonic() through its module-level `time`, so
    substituting that namespace lets a test cross the scan interval without
    sleeping for it - and lets it prove that nothing was crossed when it did
    not mean to.
    """

    def __init__(self, start=10_000.0):
        self.now = start

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(
        gb,
        "time",
        types.SimpleNamespace(
            monotonic=c.monotonic,
            perf_counter=time.perf_counter,
            sleep=time.sleep,
            time=time.time,
        ),
    )
    return c


class _Log:
    """An ordered record of what happened, across threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self.events = []

    def note(self, event):
        with self._lock:
            self.events.append(event)

    def clear(self):
        with self._lock:
            self.events.clear()


class _Claims:
    """Counts PTP claims (gp Camera.init()/exit()) and concurrent ones."""

    def __init__(self):
        self._lock = threading.Lock()
        self.init_calls = 0
        self.exit_calls = 0
        self.live = 0
        self.max_live = 0

    def claim(self):
        with self._lock:
            self.init_calls += 1
            self.live += 1
            self.max_live = max(self.max_live, self.live)

    def release(self):
        with self._lock:
            self.exit_calls += 1
            self.live -= 1


class _Detector:
    """Scriptable gp.Camera.autodetect() that records every bus scan.

    `results` can be rewired between calls, so a test can take a body off the
    bus. `explode` makes the next call raise, which is how a test asserts that
    a code path does not touch the bus at all: a path that does becomes an
    error rather than a silently-counted extra scan.
    """

    def __init__(self, results, log=None):
        self.results = list(results)
        self.calls = 0
        self.explode = False
        self._log = log
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
            snapshot = list(self.results)
            explode = self.explode
        if self._log is not None:
            self._log.note("scan")
        if explode:
            raise AssertionError(
                "gp.Camera.autodetect() ran on a path that must not touch the bus"
            )
        return snapshot


class _Widget:
    def __init__(self, value):
        self._value = value

    def get_value(self):
        return self._value


class _Config:
    def __init__(self, serial):
        self._serial = serial

    def get_child_by_name(self, name):
        if name == "serialnumber":
            # Padded exactly as libgphoto2 reports it; the backend strips it.
            return _Widget(f"  {self._serial}  ")
        raise KeyError(name)


class _PreviewFile:
    def get_data_and_size(self):
        return b"jpeg-preview-bytes"


class _PortInfo:
    def __init__(self, path):
        self.path = path


def _make_fake_gp(claims, detector, hooks=None, log=None):
    """A fake `gphoto2` module.

    When `hooks` is given, capture_preview() on the body at hooks.port notes
    that it has the body, signals hooks.entered, and blocks until
    hooks.release is set - the window in which a real preview is streaming
    frames off the body and a bus scan must not run.
    """

    class GPhoto2Error(Exception):
        pass

    class Camera:
        def __init__(self):
            self.port = None
            self._initialized = False

        @staticmethod
        def autodetect():
            return detector()

        def set_abilities(self, abilities):
            pass

        def set_port_info(self, port_info):
            self.port = port_info.path

        def init(self):
            self._initialized = True
            claims.claim()

        def exit(self):
            if self._initialized:
                self._initialized = False
                claims.release()

        def get_config(self):
            return _Config(SERIAL_BY_PORT.get(self.port, ""))

        def capture_preview(self):
            if hooks is not None and self.port == hooks.port:
                log.note("io-start")
                hooks.entered.set()
                if not hooks.release.wait(timeout=30):
                    raise AssertionError("the preview was never released")
                log.note("io-end")
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
            return path

        def __getitem__(self, path):
            return _PortInfo(path)

    return types.SimpleNamespace(
        GPhoto2Error=GPhoto2Error,
        Camera=Camera,
        CameraAbilitiesList=CameraAbilitiesList,
        PortInfoList=PortInfoList,
    )


def _make_session_cls(fake_gp):
    class _FakeSession:
        instances = []

        def __init__(self, port, model, logger):
            self.port = port
            self.model = model
            self._logger = logger
            self._cam = None
            self.close_calls = 0
            type(self).instances.append(self)
            cam = fake_gp.Camera()
            port_info_list = fake_gp.PortInfoList()
            port_info_list.load()
            cam.set_port_info(port_info_list[port_info_list.lookup_path(port)])
            cam.init()
            self.serial = SERIAL_BY_PORT.get(port, "")
            self._cam = cam

        def close(self):
            self.close_calls += 1
            if self._cam is not None:
                self._cam.exit()
                self._cam = None

    _FakeSession.instances = []
    return _FakeSession


def _two_body_backend(monkeypatch, hooks=None, log=None):
    claims = _Claims()
    detector = _Detector([(MODEL, PORT_0), (MODEL, PORT_1)], log=log)
    fake_gp = _make_fake_gp(claims, detector, hooks=hooks, log=log)
    session_cls = _make_session_cls(fake_gp)
    monkeypatch.setattr(gb, "gp", fake_gp)
    monkeypatch.setattr(gb, "_GP_AVAILABLE", True)
    monkeypatch.setattr(gb, "_PTPSession", session_cls)
    backend = gb.GPhoto2Backend(logging.getLogger("test-gphoto2-bus-scan"))
    return backend, claims, detector


# ----------------------------------------------------------------------
# (a) a scan may not begin while a body is in use
# ----------------------------------------------------------------------

def test_a_preview_in_flight_keeps_the_bus_scan_waiting(monkeypatch, clock):
    """The observable order must be: preview starts, preview ends, then scan.

    Asserted on the order of the three events, not on any lock being taken,
    so it holds however the barrier is built. A scan that runs through the
    middle of the preview shows up here as the order io-start, scan, io-end.
    """
    log = _Log()
    hooks = types.SimpleNamespace(
        port=PORT_0,
        entered=threading.Event(),
        release=threading.Event(),
    )
    backend, claims, detector = _two_body_backend(monkeypatch, hooks=hooks, log=log)

    # Both bodies in hand, so both indices have a per-body lock for the
    # barrier to take.
    backend._get_or_open_session(0)
    backend._get_or_open_session(1)
    log.clear()
    scans_before = detector.calls

    errors = {}

    def run_preview():
        try:
            backend.capture_preview(0)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["preview"] = exc

    def run_check():
        try:
            backend.is_camera_connected(1)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["check"] = exc

    io = threading.Thread(target=run_preview, name="preview")
    checker = threading.Thread(target=run_check, name="connection-check")
    started = []

    try:
        io.start()
        started.append(io)
        assert hooks.entered.wait(10), "the preview never reached the body"

        # Let the map go stale, so the connection check has to re-detect.
        clock.advance(gb._PORT_MAP_TTL_S + 1.0)

        checker.start()
        started.append(checker)
        checker.join(timeout=2.0)
        blocked = checker.is_alive()

        assert detector.calls == scans_before, (
            f"gp.Camera.autodetect() ran while a preview held a body "
            f"(scans: {scans_before} -> {detector.calls}, events: {log.events})"
        )
    finally:
        # Both threads can only be blocked on the preview gate or on the
        # barrier the preview holds, so both terminate once the gate is set.
        hooks.release.set()
        for thread in started:
            thread.join(timeout=30)

    assert not errors, errors
    assert not io.is_alive() and not checker.is_alive()
    assert blocked, "the bus scan did not wait for the body that was in use"
    assert log.events == ["io-start", "io-end", "scan"], (
        f"the bus scan did not wait for the preview to finish: {log.events}"
    )
    assert claims.max_live == 2, (
        f"a third PTP claim appeared (max live {claims.max_live})"
    )


# ----------------------------------------------------------------------
# (b) many checks, few scans - without going blind
# ----------------------------------------------------------------------

def test_rapid_connection_checks_scan_the_bus_far_fewer_times_than_asked(
    monkeypatch, clock
):
    """N rapid checks must cost far fewer than N scans.

    The frontend asks about once a second per body, and one full USB
    enumeration per ask is what this rules out: 60 asks inside one interval
    may cost at most a single scan.
    """
    backend, _claims, detector = _two_body_backend(monkeypatch)

    backend._get_or_open_session(0)
    backend._get_or_open_session(1)
    scans_before = detector.calls

    asks = 60
    for _ in range(asks):
        assert backend.is_camera_connected(0) is True
        assert backend.is_camera_connected(1) is True

    scans = detector.calls - scans_before
    assert scans <= 1, (
        f"{2 * asks} connection checks ran {scans} bus scans; the map is "
        f"supposed to stand for {gb._PORT_MAP_TTL_S}s at a time"
    )

    # ... and the economy must not make it blind. Body 0 is switched off and
    # unplugged; once the interval has passed, the check says so.
    detector.results = [(MODEL, PORT_1)]
    clock.advance(gb._PORT_MAP_TTL_S + 1.0)

    assert backend.is_camera_connected(0) is False, (
        "a body that has genuinely gone was still reported as connected"
    )
    assert backend.is_camera_connected(1) is True
    assert detector.calls > scans_before + scans, (
        "the bus was never re-detected after the interval elapsed"
    )


# ----------------------------------------------------------------------
# (c) a power-cycled body is noticed within the interval
# ----------------------------------------------------------------------

def test_a_body_that_is_power_cycled_is_noticed_within_the_scan_interval(
    monkeypatch, clock
):
    """The interval is an upper bound on how long a stale answer may stand.

    Both bodies are power-cycled off the bus with their sessions still
    cached. Exactly `_PORT_MAP_TTL_S` later the next check re-detects and
    reports them gone - no rescan, no restart, nobody pressing anything.
    """
    backend, _claims, detector = _two_body_backend(monkeypatch)

    backend._get_or_open_session(0)
    backend._get_or_open_session(1)
    assert backend.is_camera_connected(0) is True

    # Both bodies power-cycle: nothing on the bus, sessions still cached.
    detector.results = []
    clock.advance(gb._PORT_MAP_TTL_S)

    assert backend.is_camera_connected(0) is False, (
        f"a power-cycled body was still reported connected "
        f"{gb._PORT_MAP_TTL_S}s later"
    )
    assert backend.is_camera_connected(1) is False
    assert backend._published_port_map() == {}


# ----------------------------------------------------------------------
# (d) a body in use answers for itself, without the bus and without waiting
# ----------------------------------------------------------------------

def test_a_body_in_use_answers_a_connection_check_without_waiting_for_it(
    monkeypatch, clock
):
    """A check on the body that is mid-preview returns at once, and without a scan.

    The body's own lock is the evidence it is there, so the check needs
    neither the bus nor the barrier. A check that took the barrier instead
    would sit out the whole operation - on a wedged body, the better part of
    a minute inside libgphoto2's USB timeouts - and freeze the indicator the
    operator is watching, on the body that is demonstrably working.

    The map is deliberately stale here, so the only thing that can answer
    without a scan is the traffic in flight. The detector is armed to raise,
    so any path that reaches the bus fails outright.
    """
    log = _Log()
    hooks = types.SimpleNamespace(
        port=PORT_0,
        entered=threading.Event(),
        release=threading.Event(),
    )
    backend, _claims, detector = _two_body_backend(monkeypatch, hooks=hooks, log=log)

    backend._get_or_open_session(0)
    backend._get_or_open_session(1)

    errors = {}
    answered = {}

    def run_preview():
        try:
            backend.capture_preview(0)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["preview"] = exc

    def run_check():
        try:
            answered["value"] = backend.is_camera_connected(0)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["check"] = exc

    io = threading.Thread(target=run_preview, name="preview")
    checker = threading.Thread(target=run_check, name="connection-check")
    started = []

    try:
        io.start()
        started.append(io)
        assert hooks.entered.wait(10), "the preview never reached the body"

        clock.advance(gb._PORT_MAP_TTL_S + 1.0)
        detector.explode = True

        checker.start()
        started.append(checker)
        checker.join(timeout=5.0)
        finished = not checker.is_alive()
    finally:
        hooks.release.set()
        for thread in started:
            thread.join(timeout=30)

    assert not errors, errors
    assert finished, (
        "the check waited for the body instead of being answered by it"
    )
    assert answered["value"] is True, (
        "a body with a preview in flight on it was not reported connected"
    )
