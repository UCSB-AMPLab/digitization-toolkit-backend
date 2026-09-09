"""rescan() must republish the port map and drop sessions that stop matching it.

A DSLR body that drops off USB mid-session (NEH-229) leaves the backend holding
a PTP session pointed at a port that is gone. Detection already tracks reality -
autodetect runs on every is_camera_connected() call - but nothing rebuilds the
port map and closes the stale sessions, so the operator's only lever is a
reboot.

rescan() closes that gap: it rebuilds the map, publishes it, and then reconciles
every cached session against it under that session's own per-camera lock. The
fakes here model the two windows that make this delicate:

  - a session that is still being opened when the map is republished (its
    constructor holds the per-camera lock and has not yet published itself in
    self._sessions), which must be validated against the new map at its next
    use rather than being missed by the reconcile loop;
  - two rescans overlapping on a slow autodetect, which must publish their
    snapshots in order rather than racing.

Both are counted in PTP claims: at no point may two claims on one body be live
at once.
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

    GPhoto2Backend now persists its index -> body bindings to
    <projects_dir>/camera-bindings.json and seeds them at construction, so
    without a per-test root one test's rig would seed the next test's backend.
    """
    from app.core.config import settings

    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "PROJECTS_ROOT", str(root))
    return root


MODEL = "Canon EOS Rebel T7"
# A different body can take over an index after a rescan; the row must then
# carry that body's model, not the one the enumeration snapshot remembered.
MODEL_NEW = "Canon EOS 80D"
# Two bodies, each with a port it may re-enumerate onto after a power-cycle.
PORT_0 = "usb:001,004"
PORT_0_NEW = "usb:001,011"
PORT_1 = "usb:001,005"
PORT_1_NEW = "usb:001,009"

SERIAL_0 = "1111111"
SERIAL_1 = "2222222"
SERIAL_BY_PORT = {
    PORT_0: SERIAL_0,
    PORT_0_NEW: SERIAL_0,
    PORT_1: SERIAL_1,
    PORT_1_NEW: SERIAL_1,
}
HW_0 = f"canoneosrebelt7_{SERIAL_0}"
HW_1 = f"canoneosrebelt7_{SERIAL_1}"


class _Claims:
    """Counts PTP claims (gp Camera.init()/exit()) and tracks concurrent ones."""

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
    """Scriptable gp.Camera.autodetect(); tests mutate `results` between calls.

    The result is snapshotted at call entry, so a test can change `results`
    while a paused call is still blocked and the paused call still returns the
    detection it started with.
    """

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.entered = threading.Event()
        self._gate = None
        self._lock = threading.Lock()

    def pause_next(self, gate):
        """Make the next autodetect() block until `gate` is set."""
        self.entered.clear()
        self._gate = gate

    def __call__(self):
        with self._lock:
            self.calls += 1
            snapshot = list(self.results)
            gate = self._gate
            self._gate = None
        if gate is not None:
            self.entered.set()
            if not gate.wait(timeout=30):
                raise AssertionError("autodetect was never released")
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


def _make_fake_gp(claims, detector):
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


def _make_session_cls(fake_gp, gate=None, entered=None):
    """Fake _PTPSession recording port, model and close() calls.

    When `gate` is given, only the first instance pauses in its constructor -
    after it has claimed the body but before it publishes self._cam - which is
    the window a rescan can land in.
    """

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
            if gate is not None and len(type(self).instances) == 1:
                entered.set()
                if not gate.wait(timeout=30):
                    raise AssertionError("session constructor was never released")
            self._cam = cam

        def close(self):
            self.close_calls += 1
            if self._cam is not None:
                self._cam.exit()
                self._cam = None

    return _FakeSession


def _make_backend(monkeypatch, fake_gp, session_cls):
    monkeypatch.setattr(gb, "gp", fake_gp)
    monkeypatch.setattr(gb, "_GP_AVAILABLE", True)
    monkeypatch.setattr(gb, "_PTPSession", session_cls)
    return gb.GPhoto2Backend(logging.getLogger("test-gphoto2-rescan"))


def _two_body_backend(monkeypatch):
    claims = _Claims()
    detector = _Detector([(MODEL, PORT_0), (MODEL, PORT_1)])
    fake_gp = _make_fake_gp(claims, detector)
    session_cls = _make_session_cls(fake_gp)
    backend = _make_backend(monkeypatch, fake_gp, session_cls)
    return backend, claims, detector, session_cls


# ----------------------------------------------------------------------
# (a) a body that vanished
# ----------------------------------------------------------------------

def test_rescan_closes_the_session_of_a_body_that_vanished(monkeypatch):
    backend, claims, detector, _cls = _two_body_backend(monkeypatch)

    s0 = backend._get_or_open_session(0)
    s1 = backend._get_or_open_session(1)
    assert (s0.port, s1.port) == (PORT_0, PORT_1)
    assert claims.live == 2

    # Body 1 drops off USB; body 0 stays exactly where it was.
    detector.results = [(MODEL, PORT_0)]

    devices = backend.rescan()

    assert s1.close_calls == 1, "the vanished body's session was not closed"
    assert s0.close_calls == 0, "the surviving body's session was closed needlessly"
    assert list(backend._sessions) == [0]
    assert backend._sessions[0] is s0
    assert claims.live == 1

    assert len(devices) == 1
    assert devices[0]["index"] == 0
    assert devices[0]["port"] == PORT_0
    assert devices[0]["serial"] == SERIAL_0
    assert devices[0]["hardware_id"] == HW_0


# ----------------------------------------------------------------------
# (b) a body that moved
# ----------------------------------------------------------------------

def test_rescan_drops_a_moved_session_so_the_next_open_uses_the_new_port(monkeypatch):
    backend, claims, detector, session_cls = _two_body_backend(monkeypatch)

    backend._get_or_open_session(0)
    s1 = backend._get_or_open_session(1)
    assert s1.port == PORT_1

    # Body 1 re-enumerates onto a different port, still at index 1.
    detector.results = [(MODEL, PORT_0), (MODEL, PORT_1_NEW)]

    devices = backend.rescan()

    assert s1.close_calls == 1
    assert 1 not in backend._sessions
    assert [d["port"] for d in devices] == [PORT_0, PORT_1_NEW]
    assert [d["hardware_id"] for d in devices] == [HW_0, HW_1]

    reopened = backend._get_or_open_session(1)

    assert reopened is not s1
    assert reopened.port == PORT_1_NEW, "reopened on the stale port"
    assert reopened.model == MODEL
    assert session_cls.instances[-1] is reopened
    assert backend._sessions[1] is reopened


# ----------------------------------------------------------------------
# (c) a rescan landing in the window of a session that is still opening
# ----------------------------------------------------------------------

def test_rescan_publishes_the_map_before_it_waits_on_a_paused_open(monkeypatch):
    claims = _Claims()
    detector = _Detector([(MODEL, PORT_0)])
    fake_gp = _make_fake_gp(claims, detector)
    entered = threading.Event()
    release = threading.Event()
    session_cls = _make_session_cls(fake_gp, gate=release, entered=entered)
    backend = _make_backend(monkeypatch, fake_gp, session_cls)

    opened = {}
    errors = {}

    def open_slowly():
        try:
            with backend._get_camera_lock(0):
                opened["session"] = backend._get_or_open_session(0)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["open"] = exc

    opener = threading.Thread(target=open_slowly, name="slow-opener")

    rescan_result = {}

    def run_rescan():
        try:
            rescan_result["devices"] = backend.rescan()
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["rescan"] = exc

    rescanner = threading.Thread(target=run_rescan, name="rescanner")
    started = []

    try:
        opener.start()
        started.append(opener)
        assert entered.wait(10), "opener never reached the session constructor"

        # The body re-enumerates while that open is still in flight.
        detector.results = [(MODEL, PORT_0_NEW)]

        rescanner.start()
        started.append(rescanner)

        # Publish first: the new map must be visible even though the reconcile
        # cannot yet run, because the per-camera lock is held by the opener.
        deadline = time.monotonic() + 10
        while backend._port_map != {0: (MODEL, PORT_0_NEW)} and time.monotonic() < deadline:
            time.sleep(0.01)
        assert backend._port_map == {0: (MODEL, PORT_0_NEW)}, (
            f"rescan did not publish the new map while lock 0 was held "
            f"(map is {backend._port_map!r}, errors: {errors})"
        )
        rescanner.join(timeout=1.0)
        assert rescanner.is_alive(), (
            "rescan finished without waiting for the per-camera lock; it cannot "
            "have reconciled the session that was still opening"
        )
    finally:
        # Unbounded joins: the opener can only be blocked on the session-
        # constructor gate (freed by release.set()), and the rescanner can
        # only be blocked on lock 0 (freed once the opener leaves), so both
        # terminate before the monkeypatch teardown runs. A bounded join
        # would only attempt cleanup; a worker still running past it could
        # then execute against a torn-down module state.
        release.set()
        for thread in started:
            thread.join()

    assert not errors, errors
    assert not opener.is_alive() and not rescanner.is_alive()

    stale = opened["session"]
    assert stale.port == PORT_0, "the opener opened against the wrong map"
    assert stale.close_calls == 1, "the stale session was never closed"
    assert 0 not in backend._sessions

    devices = rescan_result["devices"]
    assert len(devices) == 1
    assert devices[0]["port"] == PORT_0_NEW
    assert devices[0]["serial"] == SERIAL_0
    assert devices[0]["hardware_id"] == HW_0, (
        "the rescan response fell back to an index-derived id instead of "
        "reading the serial from the reopened body (R17-1)"
    )

    fresh = backend._get_or_open_session(0)
    assert fresh is not stale
    assert fresh.port == PORT_0_NEW
    assert claims.max_live == 1, (
        f"two PTP claims were live at once (max {claims.max_live})"
    )


# ----------------------------------------------------------------------
# (d) two rescans overlapping
# ----------------------------------------------------------------------

def test_overlapping_rescans_serialise_and_publish_in_order(monkeypatch):
    backend, claims, detector, _cls = _two_body_backend(monkeypatch)

    s0 = backend._get_or_open_session(0)
    s1 = backend._get_or_open_session(1)
    assert detector.calls == 1, "the map should have been built exactly once"

    results = {}
    errors = {}

    def rescan_into(name):
        def _run():
            try:
                results[name] = backend.rescan()
            except BaseException as exc:  # noqa: BLE001 - reported to main
                errors[name] = exc

        return _run

    # First snapshot: body 1 has vanished. It blocks inside autodetect.
    gate = threading.Event()
    detector.results = [(MODEL, PORT_0)]
    detector.pause_next(gate)

    first = threading.Thread(target=rescan_into("first"), name="rescan-first")
    second = threading.Thread(target=rescan_into("second"), name="rescan-second")
    started = []

    try:
        first.start()
        started.append(first)
        assert detector.entered.wait(10), "the first rescan never reached autodetect"

        # Second snapshot: body 0 has moved too. It must wait for the first.
        detector.results = [(MODEL, PORT_0_NEW)]
        second.start()
        started.append(second)
        second.join(timeout=1.0)
        assert second.is_alive(), "the second rescan did not wait on the rescan lock"
        assert detector.calls == 2, (
            f"the second rescan detected before the first published "
            f"(autodetect calls: {detector.calls})"
        )
    finally:
        # Unbounded joins: each rescan can only be blocked inside autodetect
        # (freed by gate.set()) or waiting on the rescan lock (freed once the
        # first rescan finishes), so both terminate before the monkeypatch
        # teardown runs.
        gate.set()
        for thread in started:
            thread.join()

    assert not errors, errors
    assert detector.calls == 3
    assert backend._port_map == {0: (MODEL, PORT_0_NEW)}, (
        "the published map is not the second rescan's snapshot"
    )
    assert [d["port"] for d in results["first"]] == [PORT_0]
    assert [d["port"] for d in results["second"]] == [PORT_0_NEW]

    assert s1.close_calls == 1, "the vanished body's session was closed twice"
    assert s0.close_calls == 1, "the moved body's session was closed twice"
    assert backend._sessions == {}
    assert claims.live == 0


# ----------------------------------------------------------------------
# (e) the _get_or_open_session fast path
# ----------------------------------------------------------------------

def test_open_session_fast_path_validates_the_cached_session_against_the_map(monkeypatch):
    claims = _Claims()
    detector = _Detector([(MODEL, PORT_0)])
    fake_gp = _make_fake_gp(claims, detector)
    session_cls = _make_session_cls(fake_gp)
    backend = _make_backend(monkeypatch, fake_gp, session_cls)

    s0 = backend._get_or_open_session(0)
    again = backend._get_or_open_session(0)

    assert again is s0, "a session matching the map should be reused"
    assert claims.init_calls == 1, "a matching session was reopened"
    assert s0.close_calls == 0

    # The body re-enumerates; the map is refreshed without a rescan.
    detector.results = [(MODEL, PORT_0_NEW)]
    backend._refresh_port_map()

    reopened = backend._get_or_open_session(0)

    assert reopened is not s0
    assert s0.close_calls == 1, "the stale session was reused instead of closed"
    assert reopened.port == PORT_0_NEW
    assert backend._sessions[0] is reopened
    assert claims.init_calls == 2
    assert claims.live == 1


# ----------------------------------------------------------------------
# (f) an enumeration paused across a rescan and reopen
# ----------------------------------------------------------------------

def test_list_devices_paused_across_a_rescan_does_not_close_the_fresh_session(monkeypatch):
    claims = _Claims()
    detector = _Detector([(MODEL, PORT_0)])
    fake_gp = _make_fake_gp(claims, detector)
    session_cls = _make_session_cls(fake_gp)
    backend = _make_backend(monkeypatch, fake_gp, session_cls)

    entered = threading.Event()
    release = threading.Event()
    real_get_camera_lock = backend._get_camera_lock
    paused = {"done": False}

    def pausing_get_camera_lock(idx):
        # Only the first caller pauses - the enumeration below reaching lock
        # 0 for the first time. Every later call (the rescan's own internal
        # list_devices(), or a caller acquiring the lock directly) passes
        # straight through to the real lock.
        if not paused["done"]:
            paused["done"] = True
            entered.set()
            if not release.wait(timeout=30):
                raise AssertionError("list_devices was never released")
        return real_get_camera_lock(idx)

    monkeypatch.setattr(backend, "_get_camera_lock", pausing_get_camera_lock)

    results = {}
    errors = {}
    fresh = {}

    def run_list_devices():
        try:
            results["devices"] = backend.list_devices()
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["list_devices"] = exc

    enumerator = threading.Thread(target=run_list_devices, name="enumerator")
    started = []

    try:
        enumerator.start()
        started.append(enumerator)
        assert entered.wait(10), "the enumerator never reached lock 0"

        # A different body takes index 0 on a new port while E is paused,
        # holding only its copy of the old map.
        detector.results = [(MODEL_NEW, PORT_0_NEW)]
        rescanned = backend.rescan()
        assert rescanned[0]["port"] == PORT_0_NEW, (
            "rescan did not pick up the new port"
        )
        assert rescanned[0]["model"] == MODEL_NEW

        # A capture request opens the fresh session on the new map before E
        # resumes.
        fresh["session"] = backend._get_or_open_session(0)
        assert fresh["session"].port == PORT_0_NEW
    finally:
        # Unbounded join: the enumerator can only be blocked on the lock-0
        # gate, freed by release.set(); nothing else holds it up.
        release.set()
        for thread in started:
            thread.join()

    assert not errors, errors
    assert not enumerator.is_alive()

    fresh_session = fresh["session"]
    assert fresh_session.close_calls == 0, (
        "the fresh session was closed by the paused enumeration"
    )
    assert claims.live == 1, "more than one PTP claim was live at once"
    assert claims.max_live == 1, "more than one PTP claim was live at once"

    devices = results["devices"]
    row0 = [d for d in devices if d["index"] == 0]
    if row0:
        assert row0[0]["port"] == PORT_0_NEW, (
            "E's row for index 0 named the stale port instead of the fresh "
            "session's"
        )
        assert row0[0]["model"] == MODEL_NEW, (
            "E's row for index 0 kept the snapshot's model instead of the "
            "fresh session's"
        )
        assert row0[0]["hardware_id"].startswith("canoneos80d_"), (
            "the hardware id was built from the stale model"
        )
    # else: the row was left out entirely, which is equally acceptable - the
    # point is that neither outcome closes or misreports the fresh session.


# ----------------------------------------------------------------------
# (g) a concurrent _refresh_port_map() between rescan's publish and reconcile
# ----------------------------------------------------------------------

def test_rescan_reconcile_honours_a_refresh_that_lands_before_it_runs(monkeypatch):
    claims = _Claims()
    detector = _Detector([(MODEL, PORT_0)])
    fake_gp = _make_fake_gp(claims, detector)
    session_cls = _make_session_cls(fake_gp)
    backend = _make_backend(monkeypatch, fake_gp, session_cls)

    s0 = backend._get_or_open_session(0)
    assert s0.port == PORT_0

    entered = threading.Event()
    release = threading.Event()
    real_get_camera_lock = backend._get_camera_lock
    paused = {"done": False}

    def pausing_get_camera_lock(idx):
        # Only rescan's own reconcile-loop entry to lock 0 pauses - the
        # nested list_devices() call rescan makes for its return value comes
        # after and passes straight through.
        if not paused["done"]:
            paused["done"] = True
            entered.set()
            if not release.wait(timeout=30):
                raise AssertionError("rescan was never released")
        return real_get_camera_lock(idx)

    monkeypatch.setattr(backend, "_get_camera_lock", pausing_get_camera_lock)

    # Rescan's own snapshot detects the body on a different port - as far as
    # its build_port_map() is concerned, PORT_0 is stale.
    detector.results = [(MODEL, PORT_0_NEW)]

    rescan_result = {}
    errors = {}

    def run_rescan():
        try:
            rescan_result["devices"] = backend.rescan()
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["rescan"] = exc

    rescanner = threading.Thread(target=run_rescan, name="rescanner")
    started = []

    try:
        rescanner.start()
        started.append(rescanner)
        assert entered.wait(10), "rescan never reached its reconcile-loop lock"
        assert backend._port_map == {0: (MODEL, PORT_0_NEW)}, (
            "rescan did not publish its own snapshot before reconcile"
        )

        # Between rescan's publish and its reconcile, another caller detects
        # the body is actually still on its original port and republishes
        # that - the newest map disagrees with rescan's own stale snapshot.
        detector.results = [(MODEL, PORT_0)]
        backend._refresh_port_map()
        assert backend._port_map == {0: (MODEL, PORT_0)}, (
            "the concurrent refresh did not take effect"
        )
    finally:
        # Unbounded join: rescan's reconcile loop can only be blocked on
        # lock 0, freed by release.set().
        release.set()
        for thread in started:
            thread.join()

    assert not errors, errors
    assert not rescanner.is_alive()

    assert s0.close_calls == 0, (
        "rescan closed a session that matches the newest published map, "
        "using its own stale snapshot instead of a fresh read"
    )
    assert backend._sessions == {0: s0}
    assert claims.live == 1

    devices = rescan_result["devices"]
    assert [d["port"] for d in devices] == [PORT_0], (
        "the returned enumeration does not reflect the newest published map"
    )
