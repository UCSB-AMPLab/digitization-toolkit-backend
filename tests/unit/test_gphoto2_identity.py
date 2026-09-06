"""A camera index must name a body, not a position in autodetect().

The port map is positional: index = position in gp.Camera.autodetect(). When a
body power-cycles and re-enumerates onto a new usb:bus,dev, the next refresh can
publish the two bodies the other way round; the left page is then shot on the
right body with no error at all. NEH-129 pins each index to the body serial that
was first read there, so the map is keyed on identity and only the operator's
explicit rescan may re-pack it.

The fakes here are modelled on tests/unit/test_gphoto2_rescan.py, with two
additions the identity work needs:

  - claims are counted per port as well as globally, because a legitimate
    identification pass can hold a brief claim on a new port while a stale
    session still holds an old one - two live claims, but never two on the
    same body;
  - the fake Camera.exit() can be paused, so a test can sit in the window
    between a serial being read and the claim that read it being released.

Every test states the sequence of autodetect results it scripts.
"""

import json
import logging
import threading
import time
import types

import pytest

import capture.backends.gphoto2_backend as gb


MODEL = "Canon EOS Rebel T7"
# Four identifiable bodies. A and B each have a port they may re-enumerate
# onto after a power-cycle; C and D are replacement bodies that arrive on
# ports of their own.
PORT_A = "usb:001,004"
PORT_A_NEW = "usb:001,011"
PORT_B = "usb:001,005"
PORT_B_NEW = "usb:001,009"
PORT_C = "usb:001,014"
PORT_C_NEW = "usb:001,017"
PORT_D = "usb:001,018"
# Two bodies that answer no serial at all - they never appear in
# SERIAL_BY_PORT, so the fake reports "" for them and they stay positional.
PORT_S1 = "usb:001,020"
PORT_S2 = "usb:001,021"

SERIAL_A = "1111111"
SERIAL_B = "2222222"
SERIAL_C = "3333333"
SERIAL_D = "4444444"
SERIAL_BY_PORT = {
    PORT_A: SERIAL_A,
    PORT_A_NEW: SERIAL_A,
    PORT_B: SERIAL_B,
    PORT_B_NEW: SERIAL_B,
    PORT_C: SERIAL_C,
    PORT_C_NEW: SERIAL_C,
    PORT_D: SERIAL_D,
}
HW_A = f"canoneosrebelt7_{SERIAL_A}"
HW_B = f"canoneosrebelt7_{SERIAL_B}"
HW_C = f"canoneosrebelt7_{SERIAL_C}"
HW_D = f"canoneosrebelt7_{SERIAL_D}"

BINDINGS = "camera-bindings.json"


class _Claims:
    """Counts PTP claims (gp Camera.init()/exit()), globally and per port.

    Two claims live at once are fine as long as they are on different bodies -
    an identification read on a freshly enumerated port while a stale session
    still holds the port that body left. Two live claims on one port are the
    bug the ownership rule exists to prevent, so the per-port maxima are what
    the tests assert on.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.init_calls = 0
        self.exit_calls = 0
        self.live = 0
        self.max_live = 0
        self.init_by_port = {}
        self.live_by_port = {}
        self.max_live_by_port = {}

    def claim(self, port):
        with self._lock:
            self.init_calls += 1
            self.init_by_port[port] = self.init_by_port.get(port, 0) + 1
            self.live += 1
            self.max_live = max(self.max_live, self.live)
            live = self.live_by_port.get(port, 0) + 1
            self.live_by_port[port] = live
            self.max_live_by_port[port] = max(
                self.max_live_by_port.get(port, 0), live
            )

    def release(self, port):
        with self._lock:
            self.exit_calls += 1
            self.live -= 1
            self.live_by_port[port] = self.live_by_port.get(port, 0) - 1


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


class _Bodies:
    """What the bodies on each port answer, and how they misbehave.

    `serials` maps port -> serial and can be rewired mid-test (a body that
    moved, or a different body arriving on a port). `unreadable` holds ports
    whose serial read raises. `exit_gate` is (port, entered, release): the
    next exit() on that port signals `entered` and blocks until `release` is
    set, still holding its PTP claim. `open_gate` is the same shape for a
    session constructor, which pauses after init() has claimed the body and
    before the session is stored - the window in which a claim exists that no
    scan of self._sessions can see.
    """

    def __init__(self, serials):
        self.serials = dict(serials)
        self.unreadable = set()
        self.exit_gate = None
        self.open_gate = None


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


def _make_fake_gp(claims, detector, bodies):
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
            claims.claim(self.port)

        def exit(self):
            if self._initialized:
                gate = bodies.exit_gate
                if gate is not None and gate[0] == self.port:
                    bodies.exit_gate = None
                    gate[1].set()
                    if not gate[2].wait(timeout=30):
                        raise AssertionError("exit() was never released")
                self._initialized = False
                claims.release(self.port)

        def get_config(self):
            if self.port in bodies.unreadable:
                raise GPhoto2Error(f"-1: could not read config on {self.port}")
            return _Config(bodies.serials.get(self.port, ""))

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


def _make_session_cls(fake_gp, bodies):
    """Fake _PTPSession that reads and stores .serial the way the real one does."""

    class _FakeSession:
        instances = []

        def __init__(self, port, model, logger):
            self.port = port
            self.model = model
            self._logger = logger
            self._cam = None
            self.serial = ""
            self.close_calls = 0
            type(self).instances.append(self)
            cam = fake_gp.Camera()
            port_info_list = fake_gp.PortInfoList()
            port_info_list.load()
            cam.set_port_info(port_info_list[port_info_list.lookup_path(port)])
            cam.init()
            gate = bodies.open_gate
            if gate is not None and gate[0] == port:
                bodies.open_gate = None
                gate[1].set()
                if not gate[2].wait(timeout=30):
                    raise AssertionError("the session open was never released")
            try:
                widget = cam.get_config().get_child_by_name("serialnumber")
                self.serial = widget.get_value().strip()
            except Exception:
                self.serial = ""
            self._cam = cam

        def close(self):
            self.close_calls += 1
            if self._cam is not None:
                self._cam.exit()
                self._cam = None

    return _FakeSession


@pytest.fixture(autouse=True)
def bindings_dir(monkeypatch, tmp_path):
    """Give every test in this file its own projects root.

    The backend persists its pins to <projects_dir>/camera-bindings.json and
    seeds them at construction, so without an isolated root one test's rig
    would seed the next one's backend.
    """
    from app.core.config import settings

    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "PROJECTS_ROOT", str(root))
    return root


def _write_bindings(bindings_dir, pins, raw=None):
    text = raw if raw is not None else json.dumps({"version": 1, "pins": pins})
    (bindings_dir / BINDINGS).write_text(text)


def _read_bindings(bindings_dir):
    return json.loads((bindings_dir / BINDINGS).read_text())


def _backend_with(monkeypatch, results):
    claims = _Claims()
    detector = _Detector(results)
    bodies = _Bodies(SERIAL_BY_PORT)
    fake_gp = _make_fake_gp(claims, detector, bodies)
    session_cls = _make_session_cls(fake_gp, bodies)
    monkeypatch.setattr(gb, "gp", fake_gp)
    monkeypatch.setattr(gb, "_GP_AVAILABLE", True)
    monkeypatch.setattr(gb, "_PTPSession", session_cls)
    backend = gb.GPhoto2Backend(logging.getLogger("test-gphoto2-identity"))
    return backend, claims, detector, bodies, session_cls


def _max_per_port(claims):
    return max(claims.max_live_by_port.values()) if claims.max_live_by_port else 0


# ----------------------------------------------------------------------
# (g) first run: with no pins and an empty cache the map is positional
# ----------------------------------------------------------------------

def test_first_run_is_positional_and_pins_both_bodies(monkeypatch):
    """autodetect: [A on PORT_A, B on PORT_B]."""
    backend, claims, _detector, _bodies, _cls = _backend_with(
        monkeypatch, [(MODEL, PORT_A), (MODEL, PORT_B)]
    )

    assert backend._pins == {}
    assert backend._get_port_map() == {0: (MODEL, PORT_A), 1: (MODEL, PORT_B)}

    devices = backend.list_devices()

    assert [(d["index"], d["hardware_id"]) for d in devices] == [
        (0, HW_A), (1, HW_B)
    ]
    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B}
    assert backend._serial_by_port == {PORT_A: SERIAL_A, PORT_B: SERIAL_B}
    assert _max_per_port(claims) == 1


# ----------------------------------------------------------------------
# (f) a pinned body keeps its index when autodetect reorders
# ----------------------------------------------------------------------

def test_a_pinned_body_keeps_its_index_when_autodetect_reorders(monkeypatch):
    """autodetect: [A] then [B, A] - A must stay at index 0."""
    backend, claims, detector, _bodies, _cls = _backend_with(
        monkeypatch, [(MODEL, PORT_A)]
    )

    session_a = backend._get_or_open_session(0)
    assert session_a.serial == SERIAL_A
    assert backend._pins == {0: SERIAL_A}

    detector.results = [(MODEL, PORT_B), (MODEL, PORT_A)]
    backend._refresh_port_map()

    assert backend._get_port_map() == {0: (MODEL, PORT_A), 1: (MODEL, PORT_B)}
    assert backend._get_or_open_session(0) is session_a, (
        "the pinned body's session was dropped by a refresh that only "
        "reordered autodetect"
    )
    assert session_a.close_calls == 0
    assert claims.exit_calls == 0


# ----------------------------------------------------------------------
# (a) both bodies re-enumerate and swap places in autodetect order
# ----------------------------------------------------------------------

def test_a_swap_returns_each_body_to_its_own_index(monkeypatch):
    """autodetect: [A, B] then [B on PORT_B_NEW, A on PORT_A_NEW]."""
    backend, claims, detector, _bodies, _cls = _backend_with(
        monkeypatch, [(MODEL, PORT_A), (MODEL, PORT_B)]
    )

    session_a = backend._get_or_open_session(0)
    session_b = backend._get_or_open_session(1)
    assert (session_a.port, session_b.port) == (PORT_A, PORT_B)
    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B}

    # Both bodies power-cycle; autodetect now lists B first.
    detector.results = [(MODEL, PORT_B_NEW), (MODEL, PORT_A_NEW)]

    assert backend.is_camera_connected(0) is True
    assert backend._get_port_map() == {
        0: (MODEL, PORT_A_NEW),
        1: (MODEL, PORT_B_NEW),
    }, "the swap was published positionally instead of by body"
    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B}

    fresh_a = backend._get_or_open_session(0)
    assert fresh_a is not session_a
    assert fresh_a.port == PORT_A_NEW
    assert fresh_a.serial == SERIAL_A
    assert session_a.close_calls == 1

    fresh_b = backend._get_or_open_session(1)
    assert fresh_b is not session_b
    assert fresh_b.port == PORT_B_NEW
    assert session_b.close_calls == 1

    assert _max_per_port(claims) == 1, (
        f"two claims were live on one port at once "
        f"({claims.max_live_by_port})"
    )


# ----------------------------------------------------------------------
# (b1) a body that answers someone else's pin is refused
# ----------------------------------------------------------------------

def test_a_body_that_contradicts_its_pin_is_refused(monkeypatch):
    """autodetect: [PORT_A] - the cache says A lives there, the body says B."""
    backend, claims, _detector, bodies, session_cls = _backend_with(
        monkeypatch, [(MODEL, PORT_A)]
    )

    # Pins and cache as an earlier run left them; B has since taken PORT_A.
    backend._pins.update({0: SERIAL_A, 1: SERIAL_B})
    backend._serial_by_port[PORT_A] = SERIAL_A
    bodies.serials[PORT_A] = SERIAL_B

    assert backend._get_port_map() == {0: (MODEL, PORT_A)}

    with pytest.raises(gb.CameraIdentityError) as excinfo:
        backend._get_or_open_session(0)

    message = str(excinfo.value)
    assert SERIAL_A in message and SERIAL_B in message, message
    assert "pinned to index 1" in message, message

    opened = session_cls.instances[-1]
    assert opened.close_calls == 1, "the contradicting session was not closed"
    assert 0 not in backend._sessions
    assert claims.live == 0
    assert backend._get_port_map() == {1: (MODEL, PORT_A)}, (
        "the body that answered B's serial was not republished at B's index"
    )


# ----------------------------------------------------------------------
# (b2) a learned serial is published only after the claim that read it is gone
# ----------------------------------------------------------------------

def test_a_learned_serial_is_published_only_after_the_claim_is_released(monkeypatch):
    """autodetect: [A on PORT_A_NEW, B on PORT_B] with pins {0: A, 1: B}.

    Index 0 is reserved for A, whose new port is unidentified, so A lands
    provisionally on index 2. The identification pass pauses between reading
    A's serial and releasing the claim; nothing may be published from inside
    that window.
    """
    backend, claims, _detector, bodies, _cls = _backend_with(
        monkeypatch, [(MODEL, PORT_A_NEW), (MODEL, PORT_B)]
    )

    backend._pins.update({0: SERIAL_A, 1: SERIAL_B})
    backend._serial_by_port[PORT_B] = SERIAL_B

    assert backend._get_port_map() == {2: (MODEL, PORT_A_NEW), 1: (MODEL, PORT_B)}

    entered = threading.Event()
    release = threading.Event()
    bodies.exit_gate = (PORT_A_NEW, entered, release)

    results = {}
    errors = {}

    def probe():
        try:
            results["connected"] = backend.is_camera_connected(0)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["probe"] = exc

    prober = threading.Thread(target=probe, name="prober")
    started = []

    try:
        prober.start()
        started.append(prober)
        assert entered.wait(10), (
            "the identification pass never reached the exit gate"
        )
        assert claims.live_by_port[PORT_A_NEW] == 1
        assert PORT_A_NEW not in backend._serial_by_port, (
            "the serial was cached while the claim that read it was still held"
        )
        assert backend._port_map.get(0) != (MODEL, PORT_A_NEW), (
            "the map moved A onto its pinned index before the claim was released"
        )
    finally:
        # Unbounded join: the prober can only be blocked on the exit gate,
        # freed by release.set().
        release.set()
        for thread in started:
            thread.join()

    assert not errors, errors
    assert results["connected"] is True
    assert backend._serial_by_port[PORT_A_NEW] == SERIAL_A
    assert backend._get_port_map() == {
        0: (MODEL, PORT_A_NEW),
        1: (MODEL, PORT_B),
    }

    assert backend.capture_preview(0) == b"jpeg-preview-bytes"
    assert backend._sessions[0].port == PORT_A_NEW
    assert claims.max_live_by_port[PORT_A_NEW] == 1


# ----------------------------------------------------------------------
# (c) a body that is gone leaves its index reserved and empty
# ----------------------------------------------------------------------

def test_a_body_that_is_gone_leaves_its_index_empty(monkeypatch):
    """autodetect: [A, B] then [B] - index 0 stays reserved for A."""
    backend, claims, detector, _bodies, _cls = _backend_with(
        monkeypatch, [(MODEL, PORT_A), (MODEL, PORT_B)]
    )

    backend._get_or_open_session(0)
    session_b = backend._get_or_open_session(1)
    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B}

    backend._close_session(0)          # A is switched off and unplugged
    exits_before = claims.exit_calls
    detector.results = [(MODEL, PORT_B)]

    assert backend.is_camera_connected(0) is False
    assert backend.is_camera_connected(1) is True
    assert backend._get_port_map() == {1: (MODEL, PORT_B)}
    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B}

    devices = backend.list_devices()

    assert [(d["index"], d["hardware_id"]) for d in devices] == [(1, HW_B)]
    assert session_b.close_calls == 0, "the surviving body's session was closed"
    assert claims.exit_calls == exits_before, (
        "the surviving body was released and re-claimed"
    )


# ----------------------------------------------------------------------
# (d) a replacement body is only re-packed by an explicit rescan
# ----------------------------------------------------------------------

def test_a_replacement_body_is_repacked_only_by_rescan(monkeypatch):
    """autodetect: [A, B] then [B, C] - C is provisional at 2 until rescan."""
    backend, claims, detector, _bodies, _cls = _backend_with(
        monkeypatch, [(MODEL, PORT_A), (MODEL, PORT_B)]
    )

    backend.list_devices()
    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B}

    # A is replaced by a different body, C, on a port of its own.
    detector.results = [(MODEL, PORT_B), (MODEL, PORT_C)]

    assert backend.is_camera_connected(0) is False
    assert backend._get_port_map() == {1: (MODEL, PORT_B), 2: (MODEL, PORT_C)}

    devices = backend.list_devices()
    assert [(d["index"], d["hardware_id"]) for d in devices] == [
        (1, HW_B), (2, HW_C)
    ]
    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B, 2: SERIAL_C}

    rescanned = backend.rescan()

    assert [(d["index"], d["hardware_id"]) for d in rescanned] == [
        (0, HW_C), (1, HW_B)
    ]
    assert backend._pins == {0: SERIAL_C, 1: SERIAL_B}
    assert backend._get_port_map() == {0: (MODEL, PORT_C), 1: (MODEL, PORT_B)}
    assert _max_per_port(claims) == 1


# ----------------------------------------------------------------------
# (h) a later arrival never takes an index a usable body already holds
# ----------------------------------------------------------------------

def test_a_later_arrival_never_displaces_a_body_already_read(monkeypatch):
    """autodetect: [A], then [B], then [C, B] - B must keep index 1."""
    backend, claims, detector, _bodies, _cls = _backend_with(
        monkeypatch, [(MODEL, PORT_A)]
    )

    backend.list_devices()
    assert backend._pins == {0: SERIAL_A}

    # A is gone; B is the first body read after it, on the next free index.
    detector.results = [(MODEL, PORT_B)]
    backend._refresh_port_map()
    assert backend._get_port_map() == {1: (MODEL, PORT_B)}
    backend.list_devices()
    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B}

    # C now arrives and is listed ahead of B.
    detector.results = [(MODEL, PORT_C), (MODEL, PORT_B)]
    backend._refresh_port_map()

    assert backend._get_port_map() == {1: (MODEL, PORT_B), 2: (MODEL, PORT_C)}, (
        "a later arrival was published on the index an already-read body holds"
    )
    session = backend._get_or_open_session(1)
    assert session.port == PORT_B
    assert session.serial == SERIAL_B


# ----------------------------------------------------------------------
# (i) ownership: a port held under another index is never opened twice
# ----------------------------------------------------------------------

def _repack_backend(monkeypatch):
    """pins {0: A, 1: B}, A gone, C's session cached at provisional index 2."""
    backend, claims, detector, bodies, session_cls = _backend_with(
        monkeypatch, [(MODEL, PORT_B), (MODEL, PORT_C)]
    )
    backend._pins.update({0: SERIAL_A, 1: SERIAL_B})
    backend._serial_by_port[PORT_B] = SERIAL_B

    assert backend._get_port_map() == {1: (MODEL, PORT_B), 2: (MODEL, PORT_C)}
    session_c = backend._get_or_open_session(2)
    assert session_c.port == PORT_C
    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B, 2: SERIAL_C}
    return backend, claims, detector, bodies, session_c


def test_a_port_held_by_a_busy_index_is_refused_not_stolen(monkeypatch):
    """autodetect: [B, C] throughout; rescan re-packs C from index 2 to 0."""
    backend, claims, _detector, _bodies, session_c = _repack_backend(monkeypatch)

    rescan_result = {}
    errors = {}

    def run_rescan():
        try:
            rescan_result["devices"] = backend.rescan()
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["rescan"] = exc

    rescanner = threading.Thread(target=run_rescan, name="rescanner")
    lock_2 = backend._get_camera_lock(2)
    started = []

    lock_2.acquire()
    try:
        rescanner.start()
        started.append(rescanner)

        repacked = {0: (MODEL, PORT_C), 1: (MODEL, PORT_B)}
        deadline = time.monotonic() + 10
        while backend._port_map != repacked and time.monotonic() < deadline:
            time.sleep(0.01)
        assert backend._port_map == repacked, (
            f"rescan did not publish the re-packed map while lock 2 was held "
            f"(map is {backend._port_map!r}, errors: {errors})"
        )

        with pytest.raises(RuntimeError) as excinfo:
            backend.capture_preview(0)
        assert "still held by index 2" in str(excinfo.value), str(excinfo.value)
        assert claims.live_by_port[PORT_C] == 1, (
            "the refused open claimed the body anyway"
        )
    finally:
        # Unbounded join: rescan's reconcile loop can only be blocked on
        # lock 2, freed by the release below.
        lock_2.release()
        for thread in started:
            thread.join()

    assert not errors, errors
    assert session_c.close_calls == 1
    assert 2 not in backend._sessions

    assert backend.capture_preview(0) == b"jpeg-preview-bytes"
    assert backend._sessions[0].port == PORT_C
    assert claims.max_live_by_port[PORT_C] == 1, (
        f"two claims were live on C at once ({claims.max_live_by_port})"
    )


def test_a_port_held_by_a_free_index_is_reclaimed_before_the_open(monkeypatch):
    """autodetect: [B, C] throughout; rescan is paused before its reconcile.

    The pause is put on rescan's own reconcile-loop entry to lock 2 by wrapping
    backend._get_camera_lock, exactly as tests/unit/test_gphoto2_rescan.py does;
    nothing else in rescan reaches a per-camera lock first, because every
    published port already has a cached serial.
    """
    backend, claims, _detector, _bodies, session_c = _repack_backend(monkeypatch)

    entered = threading.Event()
    release = threading.Event()
    real_get_camera_lock = backend._get_camera_lock
    paused = {"done": False}

    def pausing_get_camera_lock(idx):
        if idx == 2 and not paused["done"]:
            paused["done"] = True
            entered.set()
            if not release.wait(timeout=30):
                raise AssertionError("rescan was never released")
        return real_get_camera_lock(idx)

    monkeypatch.setattr(backend, "_get_camera_lock", pausing_get_camera_lock)

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
        assert backend._port_map == {0: (MODEL, PORT_C), 1: (MODEL, PORT_B)}

        assert backend.capture_preview(0) == b"jpeg-preview-bytes"
        assert session_c.close_calls == 1, (
            "the index-2 session was not closed before C was opened at index 0"
        )
        assert backend._sessions[0].port == PORT_C
        assert claims.max_live_by_port[PORT_C] == 1, (
            f"two claims were live on C at once ({claims.max_live_by_port})"
        )
    finally:
        # Unbounded join: rescan is blocked only on the gate above.
        release.set()
        for thread in started:
            thread.join()

    assert not errors, errors
    assert [d["index"] for d in rescan_result["devices"]] == [0, 1]


# ----------------------------------------------------------------------
# (e) an unreadable serial is never evidence that a body is absent
# ----------------------------------------------------------------------

def test_an_unreadable_serial_never_drops_a_pin(monkeypatch, caplog):
    """autodetect: [A, B] then [B on PORT_B_NEW, A on PORT_A_NEW].

    The serial reads fail for as long as the test keeps both new ports in
    `bodies.unreadable`, which covers the identification pass and the
    enumeration rescan() runs for its return value. Failing only the first
    read per port would let that trailing enumeration repair the map inside
    the same rescan, which is not the state R23-4 is about.
    """
    backend, _claims, detector, bodies, _cls = _backend_with(
        monkeypatch, [(MODEL, PORT_A), (MODEL, PORT_B)]
    )

    backend.list_devices()
    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B}

    detector.results = [(MODEL, PORT_B_NEW), (MODEL, PORT_A_NEW)]
    bodies.unreadable.update({PORT_A_NEW, PORT_B_NEW})

    with caplog.at_level(logging.INFO):
        devices = backend.rescan()

    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B}, (
        "an unreadable serial was treated as evidence the body is gone"
    )
    assert backend._get_port_map() == {
        2: (MODEL, PORT_B_NEW),
        3: (MODEL, PORT_A_NEW),
    }, "the unidentified bodies were not left provisional"
    assert [d["index"] for d in devices] == [2, 3]
    assert PORT_A_NEW in caplog.text and PORT_B_NEW in caplog.text, (
        "the log does not name the ports that could not be identified"
    )

    # The reads start working again; the next connectivity poll identifies
    # both bodies and returns them to their own indices.
    bodies.unreadable.clear()

    assert backend.is_camera_connected(0) is True
    assert backend._get_port_map() == {
        0: (MODEL, PORT_A_NEW),
        1: (MODEL, PORT_B_NEW),
    }
    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B}


# ----------------------------------------------------------------------
# (j) the pins survive a restart
# ----------------------------------------------------------------------

def test_pins_survive_a_restart_and_restore_both_sides(monkeypatch, bindings_dir):
    """autodetect: [A, B] for the first backend, [B, A] on new ports for the second."""
    first, _claims, _detector, _bodies, _cls = _backend_with(
        monkeypatch, [(MODEL, PORT_A), (MODEL, PORT_B)]
    )
    first.list_devices()
    assert first._pins == {0: SERIAL_A, 1: SERIAL_B}
    assert _read_bindings(bindings_dir) == {
        "version": 1,
        "pins": {"0": SERIAL_A, "1": SERIAL_B},
    }

    # A restart: a brand-new backend over the same projects root, with both
    # bodies back on ports it has never read.
    second, claims, _d2, _b2, _cls2 = _backend_with(
        monkeypatch, [(MODEL, PORT_B_NEW), (MODEL, PORT_A_NEW)]
    )
    assert second._pins == {0: SERIAL_A, 1: SERIAL_B}, "the bindings were not seeded"
    assert second._serial_by_port == {}, "the port cache must start empty"

    assert second.is_camera_connected(0) is True
    assert second._get_port_map() == {
        0: (MODEL, PORT_A_NEW),
        1: (MODEL, PORT_B_NEW),
    }, "the seeded pins did not bring the bodies back to their own indices"
    assert second._sessions == {}, (
        "identification opened a capture session instead of a brief read"
    )

    session = second._get_or_open_session(0)
    assert session.port == PORT_A_NEW
    assert session.serial == SERIAL_A
    assert list(second._sessions) == [0], "a session was opened at index 2"
    assert _max_per_port(claims) == 1


# ----------------------------------------------------------------------
# (k) a malformed bindings file is not an error
# ----------------------------------------------------------------------

def test_a_malformed_bindings_file_is_ignored(monkeypatch, bindings_dir, caplog):
    """autodetect: [A, B], with a truncated bindings file on disk."""
    _write_bindings(bindings_dir, None, raw='{"version": 1, "pins": {"0": ')

    with caplog.at_level(logging.WARNING):
        backend, _claims, _detector, _bodies, _cls = _backend_with(
            monkeypatch, [(MODEL, PORT_A), (MODEL, PORT_B)]
        )

    assert backend._pins == {}
    assert BINDINGS in caplog.text, (
        "a bindings file that could not be read was not reported"
    )
    assert backend._get_port_map() == {0: (MODEL, PORT_A), 1: (MODEL, PORT_B)}


# ----------------------------------------------------------------------
# (l) a rig where neither seeded body is present
# ----------------------------------------------------------------------

def test_a_fully_replaced_rig_drops_the_seeded_pins(monkeypatch, bindings_dir):
    """autodetect: [C, D], against a bindings file that pins A and B."""
    _write_bindings(bindings_dir, {"0": SERIAL_A, "1": SERIAL_B})
    backend, claims, _detector, _bodies, _cls = _backend_with(
        monkeypatch, [(MODEL, PORT_C), (MODEL, PORT_D)]
    )
    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B}

    assert backend.is_camera_connected(0) is True

    assert backend._pins == {0: SERIAL_C, 1: SERIAL_D}, (
        "the rig was replaced wholesale but the old pins still reserved the "
        "indices"
    )
    assert backend._get_port_map() == {0: (MODEL, PORT_C), 1: (MODEL, PORT_D)}
    assert _read_bindings(bindings_dir)["pins"] == {"0": SERIAL_C, "1": SERIAL_D}
    assert _max_per_port(claims) == 1


# ----------------------------------------------------------------------
# (m) a rig where one seeded body survives
# ----------------------------------------------------------------------

def test_a_partially_replaced_rig_keeps_the_surviving_side(monkeypatch, bindings_dir):
    """autodetect: [C, B], against a bindings file that pins A and B.

    C is listed first, so the provisional index it lands on is the lowest one
    neither taken nor reserved - 2, with 0 and 1 held by the seeded pins.
    """
    _write_bindings(bindings_dir, {"0": SERIAL_A, "1": SERIAL_B})
    backend, claims, _detector, _bodies, _cls = _backend_with(
        monkeypatch, [(MODEL, PORT_C), (MODEL, PORT_B)]
    )

    assert backend.is_camera_connected(0) is False
    assert backend._get_port_map() == {1: (MODEL, PORT_B), 2: (MODEL, PORT_C)}
    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B, 2: SERIAL_C}, (
        "one seeded body is present, so no pin may be dropped outside a rescan"
    )

    rescanned = backend.rescan()

    assert [(d["index"], d["hardware_id"]) for d in rescanned] == [
        (0, HW_C), (1, HW_B)
    ]
    assert backend._pins == {0: SERIAL_C, 1: SERIAL_B}
    assert backend._get_port_map() == {0: (MODEL, PORT_C), 1: (MODEL, PORT_B)}
    assert _read_bindings(bindings_dir)["pins"] == {"0": SERIAL_C, "1": SERIAL_B}
    assert _max_per_port(claims) == 1


# ----------------------------------------------------------------------
# (n) the identification pass obeys the ownership rule too
# ----------------------------------------------------------------------

def test_identification_never_opens_a_port_another_index_still_holds(monkeypatch):
    """autodetect: [S1, S2, C] then [S2, C on a new port].

    S1 and S2 answer no serial, so they stay positional. When S1 leaves, the
    next build lays S2 on index 0 while S2's own session is still cached under
    index 1 - and the port at index 0 is unidentified, so the identification
    pass would brief-open a body another index is already holding. It must
    take the same ownership rule the enumeration takes: refuse while index 1
    is busy, and go ahead once it is free.
    """
    backend, claims, detector, _bodies, _cls = _backend_with(
        monkeypatch, [(MODEL, PORT_S1), (MODEL, PORT_S2), (MODEL, PORT_C)]
    )

    session_s1 = backend._get_or_open_session(0)
    session_s2 = backend._get_or_open_session(1)
    session_c = backend._get_or_open_session(2)
    assert (session_s1.serial, session_s2.serial) == ("", "")
    assert backend._pins == {2: SERIAL_C}

    # S1 is unplugged; C power-cycles onto a new port.
    detector.results = [(MODEL, PORT_S2), (MODEL, PORT_C_NEW)]

    results = {}
    errors = {}

    def probe():
        try:
            results["connected"] = backend.is_camera_connected(2)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["probe"] = exc

    prober = threading.Thread(target=probe, name="prober")
    lock_1 = backend._get_camera_lock(1)
    started = []

    lock_1.acquire()
    try:
        prober.start()
        started.append(prober)

        # The pass takes index 0 first, under lock 0, and everything it may do
        # there - closing S1's stale session, the ownership decision, and the
        # brief open the unfixed code makes - happens before it releases that
        # lock; it then blocks on lock 1 (C's provisional index), which this
        # thread holds. So the deterministic marker is "lock 0 was taken and
        # released again": only then is index 0's whole block over, and an
        # assertion taken earlier could pass by schedule on the unfixed code.
        lock_0 = backend._get_camera_lock(0)
        deadline = time.monotonic() + 10
        index_0_done = False
        while time.monotonic() < deadline:
            if session_s1.close_calls == 1 and lock_0.acquire(blocking=False):
                lock_0.release()
                index_0_done = True
                break
            time.sleep(0.01)
        assert index_0_done, (
            f"the identification pass never finished index 0 (errors: {errors})"
        )

        assert claims.init_by_port[PORT_S2] == 1, (
            "the identification pass opened a port index 1's session still holds"
        )
        assert claims.max_live_by_port[PORT_S2] == 1
        assert session_s2.close_calls == 0, (
            "index 1 was busy, so its session must not have been closed"
        )
    finally:
        lock_1.release()
        for thread in started:
            thread.join()

    assert not errors, errors
    assert results["connected"] is True
    assert backend._get_port_map() == {0: (MODEL, PORT_S2), 2: (MODEL, PORT_C_NEW)}

    # Index 1 is free now, so the same read goes ahead: one brief claim on
    # S2's port, and never two live at once.
    backend._identify_unknown_ports()

    assert claims.init_by_port[PORT_S2] == 2, (
        "the read was still skipped after index 1 was released"
    )
    assert claims.max_live_by_port[PORT_S2] == 1


# ----------------------------------------------------------------------
# (o, p, q) R25-1: a claim that no scan of self._sessions can see
# ----------------------------------------------------------------------

def _repacked_to_zero(backend):
    """Publish C at index 0 and B at index 1, as a rescan re-pack would."""
    with backend._map_lock:
        backend._pins.clear()
        backend._pins.update({0: SERIAL_C, 1: SERIAL_B})
        backend._serial_by_port[PORT_C] = SERIAL_C
    backend._republish_port_map()
    assert backend._get_port_map() == {0: (MODEL, PORT_C), 1: (MODEL, PORT_B)}


def _refused_at_zero(backend, claims):
    """capture_preview(0) must refuse, and must not claim C a second time."""
    with pytest.raises(RuntimeError) as excinfo:
        backend.capture_preview(0)
    assert "still held by index 2" in str(excinfo.value), str(excinfo.value)
    assert claims.init_by_port[PORT_C] == 1, (
        "the open went ahead against a claim that is not in self._sessions"
    )


def _parked_c_backend(monkeypatch):
    """pins {0: A, 1: B} with A gone, so C is published at index 2."""
    backend, claims, detector, bodies, session_cls = _backend_with(
        monkeypatch, [(MODEL, PORT_B), (MODEL, PORT_C)]
    )
    backend._pins.update({0: SERIAL_A, 1: SERIAL_B})
    backend._serial_by_port[PORT_B] = SERIAL_B
    assert backend._get_port_map() == {1: (MODEL, PORT_B), 2: (MODEL, PORT_C)}
    return backend, claims, detector, bodies, session_cls


def test_a_session_still_being_opened_holds_its_port(monkeypatch):
    """autodetect: [B, C]; C's session is paused inside its constructor.

    The claim exists but self._sessions does not have it yet, so a scan of the
    cached sessions sees a free port and opens the same body again.
    """
    backend, claims, _detector, bodies, _cls = _parked_c_backend(monkeypatch)

    entered = threading.Event()
    release = threading.Event()
    bodies.open_gate = (PORT_C, entered, release)

    errors = {}

    def open_slowly():
        try:
            backend._get_or_open_session(2)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["open"] = exc

    opener = threading.Thread(target=open_slowly, name="slow-opener")
    started = []

    try:
        opener.start()
        started.append(opener)
        assert entered.wait(10), "the opener never reached the session constructor"
        assert 2 not in backend._sessions, "the fake published the session too early"

        _repacked_to_zero(backend)
        _refused_at_zero(backend, claims)
    finally:
        # Unbounded join: the opener can only be blocked on the gate above.
        release.set()
        for thread in started:
            thread.join()

    # The body it opened belongs to index 0 by the time it comes back, so the
    # open is refused on identity - and that path has to give the claim back,
    # or the open at index 0 below could never happen.
    assert isinstance(errors.get("open"), gb.CameraIdentityError), errors
    assert "pinned to index 0" in str(errors["open"])
    assert 2 not in backend._sessions

    assert backend.capture_preview(0) == b"jpeg-preview-bytes"
    assert backend._sessions[0].port == PORT_C
    assert claims.max_live_by_port[PORT_C] == 1


def test_a_session_still_closing_holds_its_port(monkeypatch):
    """autodetect: [B, C]; C's session is paused inside exit().

    _close_session pops the entry before close() returns, so between those two
    points the claim is live and invisible to a scan of self._sessions.
    """
    backend, claims, _detector, bodies, _cls = _parked_c_backend(monkeypatch)

    session_c = backend._get_or_open_session(2)
    assert session_c.port == PORT_C
    _repacked_to_zero(backend)

    entered = threading.Event()
    release = threading.Event()
    bodies.exit_gate = (PORT_C, entered, release)

    errors = {}

    def close_slowly():
        try:
            backend._close_session(2)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["close"] = exc

    closer = threading.Thread(target=close_slowly, name="slow-closer")
    started = []

    try:
        closer.start()
        started.append(closer)
        assert entered.wait(10), "the closer never reached exit()"
        assert 2 not in backend._sessions, "the entry should already be popped"

        _refused_at_zero(backend, claims)
    finally:
        # Unbounded join: the closer can only be blocked on the exit gate.
        release.set()
        for thread in started:
            thread.join()

    assert not errors, errors
    assert backend.capture_preview(0) == b"jpeg-preview-bytes"
    assert backend._sessions[0].port == PORT_C
    assert claims.max_live_by_port[PORT_C] == 1


def test_a_brief_read_in_flight_holds_its_port(monkeypatch):
    """autodetect: [B, C]; the enumeration's brief read on C pauses in exit().

    A brief read never appears in self._sessions at all, so its claim was
    invisible to every ownership check.
    """
    backend, claims, _detector, bodies, _cls = _parked_c_backend(monkeypatch)

    entered = threading.Event()
    release = threading.Event()
    bodies.exit_gate = (PORT_C, entered, release)

    errors = {}
    results = {}

    def enumerate_slowly():
        try:
            results["devices"] = backend.list_devices()
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["enumerate"] = exc

    enumerator = threading.Thread(target=enumerate_slowly, name="enumerator")
    started = []

    try:
        enumerator.start()
        started.append(enumerator)
        assert entered.wait(10), "the enumeration never reached its brief read"
        assert backend._sessions == {}, "a brief read must not cache a session"

        _repacked_to_zero(backend)
        _refused_at_zero(backend, claims)
    finally:
        # Unbounded join: the enumerator can only be blocked on the exit gate.
        release.set()
        for thread in started:
            thread.join()

    assert not errors, errors
    assert backend.capture_preview(0) == b"jpeg-preview-bytes"
    assert backend._sessions[0].port == PORT_C
    assert claims.max_live_by_port[PORT_C] == 1


# ----------------------------------------------------------------------
# (r) R25-2: a re-pack may not move a body off its side
# ----------------------------------------------------------------------

def test_rescan_never_moves_a_surviving_body_off_its_side(monkeypatch, bindings_dir):
    """autodetect: [A, B] then [B] - B is the right-hand camera and stays."""
    backend, _claims, detector, _bodies, _cls = _backend_with(
        monkeypatch, [(MODEL, PORT_A), (MODEL, PORT_B)]
    )
    backend.list_devices()
    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B}

    detector.results = [(MODEL, PORT_B)]
    rescanned = backend.rescan()

    assert backend._pins == {1: SERIAL_B}, (
        "the surviving body was moved to the other side of the rig"
    )
    assert backend._get_port_map() == {1: (MODEL, PORT_B)}
    assert [(d["index"], d["hardware_id"]) for d in rescanned] == [(1, HW_B)]
    assert _read_bindings(bindings_dir)["pins"] == {"1": SERIAL_B}


def test_rescan_repacks_a_newcomer_into_the_freed_side(monkeypatch, bindings_dir):
    """autodetect: [A], then [B], then [C, B], then a rescan."""
    backend, _claims, detector, _bodies, _cls = _backend_with(
        monkeypatch, [(MODEL, PORT_A)]
    )
    backend.list_devices()

    detector.results = [(MODEL, PORT_B)]
    backend._refresh_port_map()
    backend.list_devices()
    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B}

    detector.results = [(MODEL, PORT_C), (MODEL, PORT_B)]
    backend._refresh_port_map()
    assert backend._get_port_map() == {1: (MODEL, PORT_B), 2: (MODEL, PORT_C)}

    rescanned = backend.rescan()

    assert backend._pins == {0: SERIAL_C, 1: SERIAL_B}
    assert backend._get_port_map() == {0: (MODEL, PORT_C), 1: (MODEL, PORT_B)}
    assert [(d["index"], d["hardware_id"]) for d in rescanned] == [
        (0, HW_C), (1, HW_B)
    ]
    assert _read_bindings(bindings_dir)["pins"] == {"0": SERIAL_C, "1": SERIAL_B}


# ----------------------------------------------------------------------
# (s) R25-3: two saves must not land out of order
# ----------------------------------------------------------------------

def test_two_saves_leave_the_newest_pins_on_disk(monkeypatch, bindings_dir):
    """autodetect: [A, B]; the first save is paused inside atomic_write."""
    backend, _claims, _detector, _bodies, _cls = _backend_with(
        monkeypatch, [(MODEL, PORT_A)]
    )
    with backend._map_lock:
        backend._pins.update({0: SERIAL_A})

    entered = threading.Event()
    release = threading.Event()
    real_atomic_write = gb.atomic_write
    paused = {"done": False}

    def pausing_atomic_write(path, write_fn):
        if not paused["done"]:
            paused["done"] = True
            entered.set()
            if not release.wait(timeout=30):
                raise AssertionError("the first save was never released")
        return real_atomic_write(path, write_fn)

    monkeypatch.setattr(gb, "atomic_write", pausing_atomic_write)

    errors = {}

    def save(name):
        def _run():
            try:
                backend._save_pins()
            except BaseException as exc:  # noqa: BLE001 - reported to main
                errors[name] = exc

        return _run

    first = threading.Thread(target=save("first"), name="save-first")
    second = threading.Thread(target=save("second"), name="save-second")
    started = []

    try:
        first.start()
        started.append(first)
        assert entered.wait(10), "the first save never reached the write"

        # The pins move on while that write is still in flight.
        with backend._map_lock:
            backend._pins.update({1: SERIAL_B})

        # The proof that saves serialise is taken here, from this thread,
        # while the first writer is parked inside its write: the lock that
        # covers snapshot and write must be held, so a non-blocking acquire
        # must fail. Whether the second writer has been scheduled yet says
        # nothing, so no assertion rests on it.
        if backend._save_lock.acquire(blocking=False):
            backend._save_lock.release()
            raise AssertionError(
                "the save lock was free while a write was in flight; a second "
                "save could snapshot and write in either order"
            )

        second.start()
        started.append(second)
    finally:
        # Unbounded joins: the first save is blocked on the gate, the second
        # on the save lock the first holds.
        release.set()
        for thread in started:
            thread.join()

    assert not errors, errors
    assert _read_bindings(bindings_dir)["pins"] == {"0": SERIAL_A, "1": SERIAL_B}, (
        "an older set of pins was written last"
    )


# ----------------------------------------------------------------------
# (t) R25-4: the seeded-drop rule must not depend on who asks first
# ----------------------------------------------------------------------

def test_an_enumeration_first_still_drops_the_seeded_pins(monkeypatch, bindings_dir):
    """autodetect: [C, D] against a bindings file that pins A and B."""
    _write_bindings(bindings_dir, {"0": SERIAL_A, "1": SERIAL_B})
    backend, _claims, _detector, _bodies, _cls = _backend_with(
        monkeypatch, [(MODEL, PORT_C), (MODEL, PORT_D)]
    )
    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B}

    devices = backend.list_devices()

    assert [(d["index"], d["hardware_id"]) for d in devices] == [
        (0, HW_C), (1, HW_D)
    ]
    assert backend._pins == {0: SERIAL_C, 1: SERIAL_D}
    assert backend.is_camera_connected(0) is True
    assert backend.is_camera_connected(1) is True
    assert _read_bindings(bindings_dir)["pins"] == {"0": SERIAL_C, "1": SERIAL_D}


# ----------------------------------------------------------------------
# (u) R25-5: a serial read late, from a session that is already open
# ----------------------------------------------------------------------

def test_a_serial_read_from_an_open_session_is_learned(monkeypatch):
    """autodetect: [A] with the open-time read failing, then [B, A]."""
    backend, _claims, detector, bodies, _cls = _backend_with(
        monkeypatch, [(MODEL, PORT_A)]
    )

    bodies.unreadable.add(PORT_A)
    session_a = backend._get_or_open_session(0)
    assert session_a.serial == "", "the open-time read was supposed to fail"
    assert backend._pins == {}

    # The body answers now; enumeration reads it from the session it holds.
    bodies.unreadable.clear()
    devices = backend.list_devices()

    assert [(d["index"], d["hardware_id"]) for d in devices] == [(0, HW_A)]
    assert backend._pins == {0: SERIAL_A}, (
        "a serial read from the open session was reported but never learned"
    )
    assert backend._serial_by_port == {PORT_A: SERIAL_A}
    assert session_a.serial == SERIAL_A

    # A later arrival must not now be able to take index 0.
    detector.results = [(MODEL, PORT_B), (MODEL, PORT_A)]
    backend._refresh_port_map()

    assert backend._get_port_map() == {0: (MODEL, PORT_A), 1: (MODEL, PORT_B)}
    assert backend._get_or_open_session(0) is session_a
    assert session_a.close_calls == 0
