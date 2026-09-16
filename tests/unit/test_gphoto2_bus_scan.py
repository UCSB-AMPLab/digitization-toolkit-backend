"""A USB bus scan must not overlap the camera operations it scans past (NEH-250).

`gp.Camera.autodetect()` enumerates the USB devices' information, and the
frontend asks `is_camera_connected()` about once a second per body. An
unconditional scan per ask is 60 enumerations a minute, and `_map_lock` guards
the map, not the bodies - so such a scan runs alongside whatever preview or
capture is in flight. What this backend now attempts is to serialise the two.

The Rionegro unit's `capture_service.log` for 9-15 Sep is what the operator
saw: 6x `[-52] Could not find the requested device on the USB port` and 5x
`[-110] I/O in progress`, against cameras that were otherwise working, and
enumeration itself never failed. An unserialised enumeration is a candidate
cause; the codes do not establish it. `-110` is also what libgphoto2 returns
for a camera's own PTP `DeviceBusy` response, so it is not a diagnostic
equivalent of "two operations overlapped on one body". The tests below assert
the serialisation, not a diagnosis.

Eight outcomes are asserted here, on what an operator or a caller can
observe rather than on which locks get taken:

  - a scan cannot begin while an operation is in flight on a body;
  - nor while a body that had no lock at all when the scan started is in I/O;
  - many rapid connection checks cost far fewer than that many scans, and a
    body that has genuinely gone is still reported as gone;
  - concurrent checks that all see one stale map cost one scan between them,
    while an operator's `rescan()` is never skipped;
  - a successful scan that found nothing stands for the interval, and a bus
    that has never been read is still read on the first ask;
  - a body power-cycled off the bus is reported gone within the interval;
  - a body in use answers for itself, without the bus and without waiting;
  - but a held lock does not outrank a newer scan that found the bus empty.

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


def _answer_without_waiting(backend, index, timeout=5.0):
    """Ask is_camera_connected() off the main thread and require an answer.

    Every assertion about the busy-body shortcut is also an assertion that it
    does not wait: a check that queued behind whatever is holding the body
    would freeze the indicator the operator is watching. Running it in its own
    thread with a deadline makes "it waited" a failure rather than a hung test.
    """
    answered = {}

    def ask():
        try:
            answered["value"] = backend.is_camera_connected(index)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            answered["error"] = exc

    thread = threading.Thread(target=ask, name=f"connection-check-{index}")
    thread.start()
    thread.join(timeout=timeout)
    assert not thread.is_alive(), (
        f"is_camera_connected({index}) waited instead of answering"
    )
    assert "error" not in answered, answered["error"]
    return answered["value"]


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


# ----------------------------------------------------------------------
# (e) a body that starts up mid-scan is waited for too
# ----------------------------------------------------------------------

def test_a_body_that_starts_up_mid_scan_is_not_scanned_through(monkeypatch, clock):
    """A body admitted after the scan began must still not be scanned through.

    Test (a) proves the barrier holds a body that was already in hand when the
    scan started. This one covers the other half, which is the half an
    appliance actually meets: index 1 has no lock at all when the scan begins -
    nothing has ever used it - and its first preview arrives while the scan is
    running. Answering that from a list of the locks that happened to exist
    when the scan started lets the scan run straight through the new body's
    I/O.

    What forces the interleaving: `_refresh_port_map` is wrapped, so the scan
    is pinned at the point where it is inside its own barrier and about to
    enumerate. Only there is the preview released, and the scan then waits for
    the preview to reach the body before enumerating. So an admission rule
    that considers only pre-existing bodies produces io-start before scan,
    every time; one that excludes new operations too cannot let the preview in
    at all, the wait times out, and scan comes first.
    """
    log = _Log()
    hooks = types.SimpleNamespace(
        port=PORT_1,
        entered=threading.Event(),
        release=threading.Event(),
    )
    backend, _claims, detector = _two_body_backend(monkeypatch, hooks=hooks, log=log)

    # A published map, and a lock for index 0 and index 0 only: index 1 has
    # never been touched, so nothing that enumerates the locks in existence
    # when the scan starts can find it.
    backend._scan_bus()
    backend._get_camera_lock(0)
    assert sorted(backend._session_locks) == [0], (
        f"index 1 already had a lock: {sorted(backend._session_locks)}"
    )

    go = threading.Event()
    locks_at_scan_start = []
    original_refresh = backend._refresh_port_map

    def refresh_once_index_1_has_had_its_chance():
        locks_at_scan_start.append(sorted(backend._session_locks))
        # Inside the barrier, about to enumerate: this is the window.
        go.set()
        hooks.entered.wait(timeout=2.0)
        original_refresh()

    monkeypatch.setattr(
        backend, "_refresh_port_map", refresh_once_index_1_has_had_its_chance
    )

    log.clear()
    errors = {}

    def run_preview():
        try:
            assert go.wait(10), "the scan never reached its barrier"
            backend.capture_preview(1)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["preview"] = exc

    def run_check():
        try:
            backend.is_camera_connected(0)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["check"] = exc

    # Stale, so the check has to re-detect.
    clock.advance(gb._PORT_MAP_TTL_S + 1.0)

    io = threading.Thread(target=run_preview, name="preview")
    checker = threading.Thread(target=run_check, name="connection-check")
    started = []

    try:
        io.start()
        started.append(io)
        checker.start()
        started.append(checker)
        checker.join(timeout=15)
    finally:
        hooks.release.set()
        for thread in started:
            thread.join(timeout=30)

    assert not errors, errors
    assert locks_at_scan_start == [[0]], (
        f"index 1 had a lock before the scan started: {locks_at_scan_start}"
    )
    assert log.events == ["scan", "io-start", "io-end"], (
        f"the scan ran while a body that started up mid-scan was in I/O: "
        f"{log.events}"
    )
    assert detector.calls >= 1


# ----------------------------------------------------------------------
# (f) an empty bus is an answer, not the absence of one
# ----------------------------------------------------------------------

def test_an_empty_bus_is_not_re_read_on_every_poll(monkeypatch, clock):
    """"The bus was read and had nothing on it" must stand for the interval.

    Nothing plugged in is the state a rig sits in between sessions, and the
    frontend keeps polling through it. Treating an empty published map as no
    answer puts that rig back to one full USB enumeration per poll, which is
    the cost this work exists to remove.

    Deterministic by construction: single-threaded, on a clock that does not
    move, so every poll after the first is inside the same interval by
    arithmetic rather than by luck.
    """
    backend, _claims, detector = _two_body_backend(monkeypatch)

    detector.results = []
    backend._scan_bus()
    assert backend._published_port_map() == {}
    scans_after_the_successful_empty_scan = detector.calls

    # Asserted on the predicate as well as on the poll count. The two fixes
    # in this area overlap - a stale check retaken after admission would also
    # collapse the poll count here - so freshness is checked directly, where
    # "the bus was read and had nothing on it" either is an answer or is not.
    assert backend._bus_read_recently() is True, (
        "a successful scan that found nothing did not count as the bus having "
        "been read"
    )

    for _ in range(12):
        assert backend.is_camera_connected(0) is False

    assert detector.calls == scans_after_the_successful_empty_scan, (
        f"12 polls on an empty bus ran "
        f"{detector.calls - scans_after_the_successful_empty_scan} more scans; "
        f"a successful scan that found nothing is an answer and stands for "
        f"{gb._PORT_MAP_TTL_S}s"
    )


def test_a_bus_that_has_never_been_read_is_read_on_the_first_ask(monkeypatch, clock):
    """The other half of freshness: never-built is still not fresh.

    An empty map that no scan has ever produced must not be mistaken for an
    empty bus, or the first question this backend is ever asked is answered
    without looking.
    """
    backend, _claims, detector = _two_body_backend(monkeypatch)

    detector.results = []
    assert detector.calls == 0

    assert backend.is_camera_connected(0) is False
    assert detector.calls == 1, (
        "the first ask of a backend that has never scanned did not reach the bus"
    )


# ----------------------------------------------------------------------
# (g) concurrent stale polls cost one scan between them
# ----------------------------------------------------------------------

def test_concurrent_stale_polls_cost_one_scan_between_them(monkeypatch, clock):
    """N pollers that all see a stale map must still cost one scan, not N.

    The interval bounds the cost only if the pollers that queue behind one
    another reconsider it. Two bodies and a dual preview mean several callers
    ask at once as a matter of course, and a stale check taken before the scan
    is admitted is the same check for all of them.

    What forces the interleaving: the freshness primitive is wrapped so that
    each polling thread's *first* reading of it rendezvouses on a barrier. All
    eight therefore compute "stale" before any of them can be admitted to
    scan - the defect's precondition, established by the barrier rather than
    by scheduling. With the check taken only before admission, all eight go on
    to enumerate; with it retaken after admission, seven find the map the
    first one published and return.
    """
    backend, _claims, detector = _two_body_backend(monkeypatch)

    backend._get_or_open_session(0)
    backend._get_or_open_session(1)
    backend._scan_bus()
    assert sorted(backend._published_port_map()) == [0, 1]

    pollers = 8
    arrived = threading.Barrier(pollers, timeout=30)
    seen = set()
    seen_lock = threading.Lock()
    original_recently = backend._bus_read_recently

    def rendezvous_on_the_first_reading():
        ident = threading.get_ident()
        with seen_lock:
            first_reading = ident not in seen
            if first_reading:
                seen.add(ident)
        answer = original_recently()
        if first_reading:
            # Every poller has now read freshness, and none has scanned.
            arrived.wait()
        return answer

    monkeypatch.setattr(
        backend, "_bus_read_recently", rendezvous_on_the_first_reading
    )

    # The map is now old enough that every poller sees it as stale.
    clock.advance(gb._PORT_MAP_TTL_S + 1.0)
    scans_before = detector.calls

    errors = {}
    answers = []
    answers_lock = threading.Lock()

    def poll():
        try:
            value = backend.is_camera_connected(0)
            with answers_lock:
                answers.append(value)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors[threading.get_ident()] = exc

    threads = [
        threading.Thread(target=poll, name=f"poller-{i}") for i in range(pollers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert all(not thread.is_alive() for thread in threads)
    assert answers == [True] * pollers, answers

    scans = detector.calls - scans_before
    assert scans == 1, (
        f"{pollers} pollers that all saw one stale map ran {scans} scans; "
        "the interval is supposed to bound this at one"
    )

    # An operator's explicit rescan is never skipped, however fresh the map.
    before_rescan = detector.calls
    backend.rescan()
    assert detector.calls > before_rescan, (
        "rescan() was skipped because the map was fresh; it is the operator's "
        "lever and must always re-detect"
    )


# ----------------------------------------------------------------------
# (h) a busy body does not outrank newer absence evidence
# ----------------------------------------------------------------------

def test_a_busy_body_does_not_outrank_a_newer_scan_that_found_nothing(
    monkeypatch, clock
):
    """A held lock plus a cached session is not evidence against a newer scan.

    The lock is held for the length of a capture, a preview or a session open -
    and a *disconnected* body's operation can sit inside libgphoto2's USB
    timeouts for the better part of a minute, holding that lock the whole time.
    So "the lock will not come free" establishes neither successful traffic nor
    agreement with what has since been published. When a scan has landed since
    the body's last successful traffic, the scan is the newer evidence and must
    win.

    Deterministic by construction: single-threaded except for the deadline on
    the answer, and both timestamps are set by hand on a clock the test moves.
    The check still may not wait, which is what `_answer_without_waiting`
    asserts.
    """
    backend, _claims, detector = _two_body_backend(monkeypatch)

    backend._get_or_open_session(0)
    backend._get_or_open_session(1)

    # A successful preview, one second after the scan that published the map:
    # the body's own traffic is now the newer evidence.
    clock.advance(1.0)
    assert backend.capture_preview(0) == b"jpeg-preview-bytes"

    lock_0 = backend._get_camera_lock(0)
    assert lock_0.acquire(blocking=False)
    try:
        assert _answer_without_waiting(backend, 0) is True, (
            "a body whose own successful traffic postdates the last scan was "
            "not reported connected"
        )

        # Now a scan lands that found nothing, after that traffic.
        clock.advance(1.0)
        detector.results = []
        scans_before = detector.calls
        backend._scan_bus_without_waiting()
        assert backend._published_port_map() == {}

        assert _answer_without_waiting(backend, 0) is False, (
            "a held lock and a cached session outranked a newer scan that "
            "found the bus empty"
        )
        assert detector.calls == scans_before + 1, (
            "the busy-body path reached the bus; it must answer from what is "
            "published, without waiting and without scanning"
        )
    finally:
        lock_0.release()
