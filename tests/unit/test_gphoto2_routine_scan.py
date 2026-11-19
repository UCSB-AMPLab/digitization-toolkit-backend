"""is_camera_connected() must not scan the USB bus when the port map is current.

Every preview and every capture asks is_camera_connected() for each camera.
A call that runs gp.Camera.autodetect() runs a USB bus scan inside libgphoto2
from the request thread (NEH-333), and when the backend already knows where
both bodies are, that scan tells it nothing new.

A published entry is current while its port's usbfs node still exists:
`usb:BBB,DDD` is /dev/bus/usb/BBB/DDD, and a body that is unplugged,
power-cycled or re-enumerated loses that node. The routine path therefore
skips the scan only when the asked-for index is in the map and no published
port has lost its node, and still scans at most every _ROUTINE_SCAN_MIN_S
seconds so a newly plugged body is noticed without a restart. rescan() and the
device listing scan exactly as before.

The node check and the clock are module-level indirections
(_port_node_exists, _monotonic), replaced here by a set of live ports and a
counter, so nothing below sleeps or depends on timing.

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


# The real node check, kept before any test replaces it.
_ORIGINAL_NODE_CHECK = getattr(gb, "_port_node_exists", lambda port: False)


MODEL = "Canon EOS Rebel T7"
PORT_0 = "usb:001,004"
PORT_1 = "usb:001,005"
PORT_1_NEW = "usb:001,009"
SERIAL_BY_PORT = {PORT_0: "1111111", PORT_1: "2222222", PORT_1_NEW: "2222222"}


class _Detector:
    """Scriptable gp.Camera.autodetect(); counts calls."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
            return list(self.results)


class _Nodes:
    """Stand-in for _port_node_exists: a port is live while it is in `live`."""

    def __init__(self, live):
        self.live = set(live)
        self.calls = 0

    def __call__(self, port):
        self.calls += 1
        return port in self.live


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


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
            return _Widget(self._serial)
        raise KeyError(name)


def _make_fake_gp(detector):
    class GPhoto2Error(Exception):
        pass

    class Camera:
        def __init__(self):
            self.port = None

        @staticmethod
        def autodetect():
            return detector()

        def set_abilities(self, abilities):
            pass

        def set_port_info(self, port_info):
            self.port = port_info.path

        def init(self):
            pass

        def exit(self):
            pass

        def get_config(self):
            return _Config(SERIAL_BY_PORT.get(self.port, ""))

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
            return types.SimpleNamespace(path=path)

    return types.SimpleNamespace(
        GPhoto2Error=GPhoto2Error,
        Camera=Camera,
        CameraAbilitiesList=CameraAbilitiesList,
        PortInfoList=PortInfoList,
    )


class _FakeSession:
    def __init__(self, port, model, logger):
        self.port = port
        self.model = model
        self.serial = SERIAL_BY_PORT.get(port, "")
        self.body_model = model
        self._cam = None

    def close(self):
        pass


def _backend(monkeypatch, results, live=None):
    """A backend over a scripted bus; returns (backend, detector, nodes, clock).

    The stubs are set with raising=False so this file also runs against a
    backend that has no routine-scan gate at all.
    """
    detector = _Detector(results)
    nodes = _Nodes(live if live is not None else [p for _m, p in results])
    clock = _Clock()
    monkeypatch.setattr(gb, "gp", _make_fake_gp(detector))
    monkeypatch.setattr(gb, "_GP_AVAILABLE", True)
    monkeypatch.setattr(gb, "_PTPSession", _FakeSession)
    monkeypatch.setattr(gb, "_port_node_exists", nodes, raising=False)
    monkeypatch.setattr(gb, "_monotonic", clock, raising=False)
    backend = gb.GPhoto2Backend(logging.getLogger("test-gphoto2-routine-scan"))
    return backend, detector, nodes, clock


def _interval():
    return getattr(gb, "_ROUTINE_SCAN_MIN_S", 10.0)


TWO = [(MODEL, PORT_0), (MODEL, PORT_1)]


# ----------------------------------------------------------------------
# the routine path
# ----------------------------------------------------------------------

def test_repeated_checks_with_both_nodes_present_scan_once(monkeypatch):
    backend, detector, _nodes, clock = _backend(monkeypatch, TWO)

    for _ in range(10):
        assert backend.is_camera_connected(0) is True
        assert backend.is_camera_connected(1) is True
        clock.now += 0.5  # 10 rounds = 5 s, inside the interval

    assert detector.calls == 1


def test_a_lost_node_makes_the_next_check_scan(monkeypatch):
    backend, detector, nodes, clock = _backend(monkeypatch, TWO)
    assert backend.is_camera_connected(0) and backend.is_camera_connected(1)
    assert detector.calls == 1

    # Body 1 is unplugged: its node is gone and the bus reports body 0 only.
    nodes.live.discard(PORT_1)
    detector.results = [(MODEL, PORT_0)]
    clock.now += 1.0

    assert backend.is_camera_connected(1) is False
    assert detector.calls == 2
    assert 1 not in backend._get_port_map()


def test_a_lost_node_on_the_other_index_also_scans(monkeypatch):
    """Asking about index 0 still scans when index 1's node has gone."""
    backend, detector, nodes, clock = _backend(monkeypatch, TWO)
    assert backend.is_camera_connected(0)
    nodes.live.discard(PORT_1)
    detector.results = [(MODEL, PORT_0)]
    clock.now += 1.0

    assert backend.is_camera_connected(0) is True
    assert detector.calls == 2
    assert backend._get_port_map() == {0: (MODEL, PORT_0)}


def test_a_re_enumerated_body_is_found_on_its_new_port(monkeypatch):
    backend, detector, nodes, clock = _backend(monkeypatch, TWO)
    assert backend.is_camera_connected(1)

    # Power-cycle: the old devnum's node goes, a new one appears.
    nodes.live.discard(PORT_1)
    nodes.live.add(PORT_1_NEW)
    detector.results = [(MODEL, PORT_0), (MODEL, PORT_1_NEW)]
    clock.now += 1.0

    assert backend.is_camera_connected(1) is True
    assert detector.calls == 2
    assert backend._get_port_map()[1] == (MODEL, PORT_1_NEW)


def test_an_index_absent_from_the_map_scans_every_time(monkeypatch):
    backend, detector, _nodes, clock = _backend(monkeypatch, [(MODEL, PORT_0)])

    for expected in range(1, 4):
        assert backend.is_camera_connected(1) is False
        assert detector.calls == expected
        clock.now += 0.1


def test_a_body_plugged_later_is_found_after_the_interval(monkeypatch):
    """Only index 0 is asked about; index 1 turns up without a rescan."""
    backend, detector, nodes, clock = _backend(monkeypatch, [(MODEL, PORT_0)])
    assert backend.is_camera_connected(0)
    assert detector.calls == 1

    nodes.live.add(PORT_1)
    detector.results = TWO

    clock.now += _interval() - 0.5
    assert backend.is_camera_connected(0)
    assert detector.calls == 1, "scanned inside the interval"
    assert 1 not in backend._get_port_map()

    clock.now += 1.0
    assert backend.is_camera_connected(0)
    assert detector.calls == 2
    assert backend._get_port_map()[1] == (MODEL, PORT_1)

    # And the interval restarts from that scan.
    clock.now += 1.0
    assert backend.is_camera_connected(0)
    assert detector.calls == 2


def test_non_usb_ports_always_scan(monkeypatch):
    """A port string not in usb:BBB,DDD form cannot be checked, so it scans.

    The real _port_node_exists is used here, not the stub.
    """
    backend, detector, _nodes, clock = _backend(
        monkeypatch, [(MODEL, "ptpip:192.168.1.10"), (MODEL, "usb:")]
    )
    monkeypatch.setattr(gb, "_port_node_exists", _ORIGINAL_NODE_CHECK, raising=False)

    for expected in range(1, 4):
        assert backend.is_camera_connected(0) is True
        assert detector.calls == expected
        clock.now += 0.1


@pytest.mark.parametrize(
    "port",
    ["ptpip:192.168.1.10", "usb:", "usb:1,4", "usb:001,004,1", "serial:/dev/ttyS0", "", None],
)
def test_the_node_check_refuses_ports_it_cannot_parse(port):
    assert _ORIGINAL_NODE_CHECK(port) is False


def test_the_node_check_maps_a_usb_port_to_its_usbfs_path(monkeypatch):
    seen = []
    monkeypatch.setattr(gb.os.path, "exists", lambda p: seen.append(p) or True)
    assert gb._port_node_exists("usb:003,017") is True
    assert seen == ["/dev/bus/usb/003/017"]


def test_two_threads_checking_both_indices_cause_no_scan(monkeypatch):
    backend, detector, _nodes, _clock = _backend(monkeypatch, TWO)
    assert backend.is_camera_connected(0) and backend.is_camera_connected(1)
    assert detector.calls == 1

    barrier = threading.Barrier(2)
    results = {}

    def check(index):
        barrier.wait(timeout=10)
        results[index] = [backend.is_camera_connected(index) for _ in range(50)]

    threads = [threading.Thread(target=check, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)

    assert results == {0: [True] * 50, 1: [True] * 50}
    assert detector.calls == 1


# ----------------------------------------------------------------------
# the explicit paths scan as before
# ----------------------------------------------------------------------

def test_rescan_scans_every_time_even_when_the_map_is_current(monkeypatch):
    backend, detector, _nodes, clock = _backend(monkeypatch, TWO)
    assert backend.is_camera_connected(0)
    before = detector.calls

    backend.rescan()
    backend.rescan()
    clock.now += 0.1
    backend.rescan()

    assert detector.calls == before + 3


def test_the_device_listing_scans_as_before(monkeypatch):
    """list_devices() builds the map when it is empty and otherwise reuses it.

    That is origin/dev behaviour, which the gate does not touch: the listing
    never scanned on a non-empty map, and still does not.
    """
    backend, detector, _nodes, _clock = _backend(monkeypatch, TWO)

    devices = backend.list_devices()
    assert [d["index"] for d in devices] == [0, 1]
    assert detector.calls == 1

    backend.list_devices()
    assert detector.calls == 1


def test_a_scan_from_rescan_restarts_the_interval(monkeypatch):
    backend, detector, nodes, clock = _backend(monkeypatch, [(MODEL, PORT_0)])
    assert backend.is_camera_connected(0)
    clock.now += _interval() - 1.0
    backend.rescan()
    scans = detector.calls

    clock.now += 2.0  # past the first scan's interval, inside rescan's
    assert backend.is_camera_connected(0)
    assert detector.calls == scans


# ----------------------------------------------------------------------
# when the node check cannot tell, nothing changes
# ----------------------------------------------------------------------

def test_an_unknown_node_scans_on_every_call_as_before(monkeypatch):
    """With the node check answering False (no usbfs, as on macOS), the counts
    are origin/dev's: one autodetect per is_camera_connected() call, plus none
    for list_devices() on a map that is already built, plus one per rescan().
    """
    backend, detector, nodes, clock = _backend(monkeypatch, TWO, live=[])

    for _ in range(5):
        assert backend.is_camera_connected(0) is True
        assert backend.is_camera_connected(1) is True
        clock.now += 0.5
    assert detector.calls == 10

    backend.list_devices()
    assert detector.calls == 10

    backend.rescan()
    assert detector.calls == 11

    assert backend.is_camera_connected(0) is True
    assert detector.calls == 12
