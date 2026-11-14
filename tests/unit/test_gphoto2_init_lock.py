"""Everything done at camera open and close must never overlap across threads.

libgphoto2's settings code (gp_setting_get / gp_setting_set) keeps its state in
unlocked process-wide statics and parses the settings file with strtok. The
ptp2 driver's camera_init reads a setting, and so does the configuration the
backend applies right after it: reading the config tree reads the capture
target setting, and writing capturetarget writes it. Two threads in that code
at once can crash the whole process. The backend has per-camera locks, but dual
capture opens both bodies at once on two pool threads, each under its own
camera's lock, so those locks do not keep the two opens apart.

The fake gp module here records every thread inside a tracked call - the
abilities and port list loads, set_abilities, set_port_info, init(), exit(),
get_config() and set_config() - and the first call of one chosen name waits
until a second thread has either entered a tracked call (an overlap, which the
test then reports) or is waiting on the backend's process-wide lock. Both
outcomes are reached without depending on timing; the waits carry timeouts only
so a broken run fails instead of hanging.

The last test checks the other direction: the lock covers only what happens
at open and close, so a preview or a settings read on an already open camera
still runs while the other camera is inside init().

Version: 0.1.0-beta
"""

import logging
import threading
import types

import pytest

import capture.backends.gphoto2_backend as gb


@pytest.fixture(autouse=True)
def isolated_projects_root(monkeypatch, tmp_path):
    """Keep this file's backends off any real projects root (bindings file)."""
    from app.core.config import settings

    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "PROJECTS_ROOT", str(root))
    return root


MODEL = "Canon EOS 1500D"
PORTS = ["usb:001,005", "usb:001,006"]
SERIALS = {PORTS[0]: "1111111", PORTS[1]: "2222222"}

# Hang guard only: every wait below is released by an event the test itself
# causes, so in a correct run none of them comes near this.
_GUARD_S = 10.0


class _Probe:
    """Shared record of which threads are inside a tracked call."""

    def __init__(self, gate_on="init"):
        self.cond = threading.Condition()
        self.inside = {}  # thread ident -> name of the call it is inside
        self.overlaps = []  # (call entered, calls already in progress)
        self.entries = []  # (thread ident, call name), in order
        self.waiting_on_lock = 0
        # The first call of this name is held at the gate; None disables it.
        self.gate_on = gate_on
        self.gate_entered = threading.Event()
        self._gate_claimed = False

    def enter(self, name):
        me = threading.get_ident()
        with self.cond:
            others = {t: n for t, n in self.inside.items() if t != me}
            if others:
                self.overlaps.append((name, sorted(others.values())))
            self.inside[me] = name
            self.entries.append((me, name))
            self.cond.notify_all()

    def leave(self):
        with self.cond:
            self.inside.pop(threading.get_ident(), None)
            self.cond.notify_all()

    def maybe_gate(self, name):
        """Hold the first `gate_on` call until a second thread enters or waits.

        Called inside the tracked call, after enter(). On unlocked code the
        second thread enters a tracked call and the overlap is recorded; on
        locked code it is found waiting on the process-wide lock instead.
        """
        me = threading.get_ident()
        with self.cond:
            if name != self.gate_on or self._gate_claimed:
                return
            self._gate_claimed = True
            start = len(self.entries)
        self.gate_entered.set()
        with self.cond:
            ok = self.cond.wait_for(
                lambda: self.waiting_on_lock > 0
                or any(t != me for t, _ in self.entries[start:]),
                timeout=_GUARD_S,
            )
        assert ok, "the second thread never reached a tracked call or the lock"


class _ProbeLock:
    """Stands in for the backend's process-wide lock and reports waiters."""

    def __init__(self, probe):
        self._lock = threading.Lock()
        self._probe = probe

    def acquire(self, blocking=True, timeout=-1):
        if self._lock.acquire(blocking=False):
            return True
        if not blocking:
            return False
        with self._probe.cond:
            self._probe.waiting_on_lock += 1
            self._probe.cond.notify_all()
        try:
            return self._lock.acquire(True, timeout)
        finally:
            with self._probe.cond:
                self._probe.waiting_on_lock -= 1

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


class _Widget:
    def __init__(self, value):
        self._value = value

    def get_value(self):
        return self._value

    def set_value(self, value):
        self._value = value


class _GPhoto2Error(Exception):
    pass


class _Config:
    def __init__(self, port):
        self._values = {
            "serialnumber": f"  {SERIALS.get(port, '')}  ",
            "cameramodel": "Canon EOS Rebel T7",
            "focusmode": "Manual",
            "flashmode": "Off",
            "capturetarget": "Memory card",
            "reviewtime": "2 seconds",
            "autopoweroff": "30",
        }

    def get_child_by_name(self, name):
        if name not in self._values:
            raise _GPhoto2Error(f"no widget {name}")
        return _Widget(self._values[name])


class _PreviewFile:
    def get_data_and_size(self):
        return b"jpeg-preview-bytes"


def _make_fake_gp(probe, hooks):
    """A gp module whose open/close and config calls report to `probe`.

    `hooks` maps a call name to a callable run inside that call, so a test can
    hold one camera inside init() or note when a preview or settings read ran.
    """

    def tracked(name, fn=None):
        def wrapper(*args, **kwargs):
            probe.enter(name)
            try:
                probe.maybe_gate(name)
                if fn is not None:
                    return fn(*args, **kwargs)
                return None
            finally:
                probe.leave()

        return wrapper

    class Camera:
        @staticmethod
        def autodetect():
            return [(MODEL, p) for p in PORTS]

        def __init__(self):
            self.port = None

        def _set_port_info(self, port_info):
            self.port = port_info

        def _init(self):
            hooks.get("init", lambda cam: None)(self)

        def _get_config(self):
            hooks.get("get_config", lambda cam: None)(self)
            return _Config(self.port)

        def _set_config(self, cfg):
            hooks.get("set_config", lambda cam: None)(self)

        set_abilities = tracked("set_abilities")
        set_port_info = tracked("set_port_info", _set_port_info)
        init = tracked("init", _init)
        exit = tracked("exit")
        get_config = tracked("get_config", _get_config)
        set_config = tracked("set_config", _set_config)

        def capture_preview(self):
            hooks.get("capture_preview", lambda cam: None)(self)
            return _PreviewFile()

    class CameraAbilitiesList:
        load = tracked("abilities.load")

        def lookup_model(self, model):
            return 0

        def __getitem__(self, idx):
            return object()

    class PortInfoList:
        load = tracked("portinfo.load")

        def lookup_path(self, path):
            return PORTS.index(path)

        def __getitem__(self, idx):
            return PORTS[idx]

    return types.SimpleNamespace(
        GPhoto2Error=_GPhoto2Error,
        Camera=Camera,
        CameraAbilitiesList=CameraAbilitiesList,
        PortInfoList=PortInfoList,
    )


def _make_backend(monkeypatch, probe, hooks=None):
    fake_gp = _make_fake_gp(probe, hooks or {})
    monkeypatch.setattr(gb, "gp", fake_gp)
    monkeypatch.setattr(gb, "_GP_AVAILABLE", True)
    # On code without the lock there is nothing to replace; the overlap
    # assertions are what fail there.
    if hasattr(gb, "_GPHOTO2_INIT_LOCK"):
        monkeypatch.setattr(gb, "_GPHOTO2_INIT_LOCK", _ProbeLock(probe))
    return gb.GPhoto2Backend(logging.getLogger("test-gphoto2-init-lock"))


def _run(target, results, errors, key):
    def body():
        try:
            results[key] = target()
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors[key] = exc

    t = threading.Thread(target=body, name=key)
    t.start()
    return t


def _no_overlaps(probe):
    return (
        f"two threads were inside libgphoto2 open-time calls at once: {probe.overlaps}"
    )


def test_two_sessions_opened_on_two_threads_never_overlap(monkeypatch):
    probe = _Probe(gate_on="init")
    backend = _make_backend(monkeypatch, probe)
    backend._refresh_port_map()

    results, errors = {}, {}
    first = _run(lambda: backend.capture_preview(0), results, errors, "cam0")
    assert probe.gate_entered.wait(_GUARD_S), "camera 0 never reached init()"
    second = _run(lambda: backend.capture_preview(1), results, errors, "cam1")

    first.join(_GUARD_S)
    second.join(_GUARD_S)
    assert not first.is_alive() and not second.is_alive()
    assert not errors, errors
    assert results == {"cam0": b"jpeg-preview-bytes", "cam1": b"jpeg-preview-bytes"}
    assert probe.overlaps == [], _no_overlaps(probe)

    # Closing both sessions on two threads must not overlap either.
    closers = [
        threading.Thread(target=backend._close_session, args=(i,)) for i in (0, 1)
    ]
    for t in closers:
        t.start()
    for t in closers:
        t.join(_GUARD_S)
    assert probe.overlaps == [], _no_overlaps(probe)
    assert sum(1 for _, n in probe.entries if n == "exit") == 2


def test_post_init_config_write_never_overlaps_another_init(monkeypatch):
    """Camera 0's open-time capturetarget write and camera 1's open stay apart."""
    probe = _Probe(gate_on="set_config")
    backend = _make_backend(monkeypatch, probe)
    backend._refresh_port_map()

    results, errors = {}, {}
    first = _run(lambda: backend.capture_preview(0), results, errors, "cam0")
    # The first set_config() is camera 0's capturetarget write, made by the
    # session open after init() returned.
    assert probe.gate_entered.wait(_GUARD_S), "camera 0 never wrote its config"
    second = _run(lambda: backend.capture_preview(1), results, errors, "cam1")

    first.join(_GUARD_S)
    second.join(_GUARD_S)
    assert not first.is_alive() and not second.is_alive()
    assert not errors, errors
    assert results == {"cam0": b"jpeg-preview-bytes", "cam1": b"jpeg-preview-bytes"}
    assert probe.overlaps == [], _no_overlaps(probe)


@pytest.mark.parametrize("reader", ["list_devices", "_identify_unknown_ports"])
def test_session_open_never_overlaps_a_brief_serial_read(monkeypatch, reader):
    """Both brief serial reads hold the lock from open through exit.

    The reader takes camera 0, which has no session, through its brief read
    and is held inside the serial read's get_config() - between its init() and
    its exit(). Camera 1's session open, started then, must wait.
    """
    probe = _Probe(gate_on="get_config")
    backend = _make_backend(monkeypatch, probe)
    backend._refresh_port_map()

    results, errors = {}, {}
    reader_thread = _run(getattr(backend, reader), results, errors, "read")
    assert probe.gate_entered.wait(_GUARD_S), "the brief read never read the serial"
    opener = _run(lambda: backend.capture_preview(1), results, errors, "open1")

    reader_thread.join(_GUARD_S)
    opener.join(_GUARD_S)
    assert not reader_thread.is_alive() and not opener.is_alive()
    assert not errors, errors
    assert results["open1"] == b"jpeg-preview-bytes"
    assert probe.overlaps == [], _no_overlaps(probe)
    assert backend._serial_by_port.get(PORTS[0]) == SERIALS[PORTS[0]]


@pytest.mark.parametrize("reader", ["list_devices", "_identify_unknown_ports"])
def test_brief_serial_read_never_overlaps_a_session_open(monkeypatch, reader):
    """Both brief serial reads: enumeration's and identification's."""
    probe = _Probe(gate_on="init")
    backend = _make_backend(monkeypatch, probe)
    backend._refresh_port_map()

    results, errors = {}, {}
    # Camera 1 opens its session; while it is inside init(), the reader takes
    # camera 0, which has no session, through its brief serial read.
    opener = _run(lambda: backend.capture_preview(1), results, errors, "open1")
    assert probe.gate_entered.wait(_GUARD_S), "camera 1 never reached init()"
    lister = _run(getattr(backend, reader), results, errors, "list")

    opener.join(_GUARD_S)
    lister.join(_GUARD_S)
    assert not opener.is_alive() and not lister.is_alive()
    assert not errors, errors
    assert results["open1"] == b"jpeg-preview-bytes"
    assert probe.overlaps == [], _no_overlaps(probe)
    if reader == "list_devices":
        by_index = {d["index"]: d for d in results["list"]}
        assert by_index[0]["serial"] == SERIALS[PORTS[0]]
        assert by_index[1]["serial"] == SERIALS[PORTS[1]]
    else:
        assert results["list"] is True
        assert backend._serial_by_port == {p: SERIALS[p] for p in PORTS}
    # The brief read on camera 0 opened and closed its own connection.
    assert sum(1 for _, n in probe.entries if n == "init") == 2
    assert sum(1 for _, n in probe.entries if n == "exit") == 1


def test_preview_and_settings_on_one_camera_run_while_the_other_is_in_init(monkeypatch):
    probe = _Probe(gate_on=None)
    hold_b = threading.Event()
    b_in_init = threading.Event()
    a_preview = threading.Event()
    a_config = threading.Event()

    def on_init(cam):
        if cam.port == PORTS[1]:
            b_in_init.set()
            assert hold_b.wait(_GUARD_S), "camera 1's init() was never released"

    def on_preview(cam):
        if cam.port == PORTS[0]:
            a_preview.set()

    def on_get_config(cam):
        if cam.port == PORTS[0] and b_in_init.is_set():
            a_config.set()

    backend = _make_backend(
        monkeypatch,
        probe,
        {"init": on_init, "capture_preview": on_preview, "get_config": on_get_config},
    )
    backend._refresh_port_map()

    # Camera 0's session is opened first, on its own.
    results, errors = {}, {}
    assert backend.capture_preview(0) == b"jpeg-preview-bytes"
    a_preview.clear()

    holder = _run(lambda: backend.capture_preview(1), results, errors, "cam1")
    assert b_in_init.wait(_GUARD_S), "camera 1 never reached init()"

    try:
        user = _run(
            lambda: (backend.capture_preview(0), backend.get_dslr_settings(0)),
            results,
            errors,
            "cam0",
        )
        # Camera 1 is still inside init() - and so inside the lock - until
        # hold_b is set below. A camera-0 call that waited on the lock would
        # never set these events.
        assert a_preview.wait(_GUARD_S), "camera 0's preview waited on camera 1's init()"
        assert a_config.wait(_GUARD_S), "camera 0's settings read waited on camera 1's init()"
        user.join(_GUARD_S)
        assert not user.is_alive()
        assert holder.is_alive(), "camera 1 left init() before it was released"
    finally:
        hold_b.set()
        holder.join(_GUARD_S)

    assert not errors, errors
    assert results["cam1"] == b"jpeg-preview-bytes"
    assert results["cam0"][1]["focus_mode"] == "Manual"
