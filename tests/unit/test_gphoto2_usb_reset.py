"""A body wedged by an interrupted PTP transfer is recovered by a USB reset.

When the backend dies while a PTP transfer is in flight, a Canon body can be
left unable to accept a new PTP session: every open times out
(`ptp_opensession` fails with PTP Timeout, which libgphoto2 reports as
GP_ERROR_TIMEOUT), the restarted backend cannot read its serial, and the body
lands on a provisional index. Waiting and rescanning do not help; a
USBDEVFS_RESET ioctl on the device node does. NEH-338 makes the backend issue
that reset once, on the port whose open timed out, and retry the open once.

Nothing here touches a real device node or sleeps: the usbfs calls and the
settle delay are module attributes the tests replace.

Version: 0.1.0-beta
"""

import errno
import logging
import os
import types

import pytest

import capture.backends.gphoto2_backend as gb


MODEL = "Canon EOS Rebel T7"
PORT_A = "usb:001,002"
PORT_B = "usb:001,003"
# Where a body comes back after a reset that made it re-enumerate.
PORT_A_NEW = "usb:001,004"
PORT_B_NEW = "usb:001,005"
SERIAL_A = "1111111"
SERIAL_B = "2222222"

# libgphoto2 result codes, as python-gphoto2 reports them in GPhoto2Error.code.
GP_ERROR = -1
GP_ERROR_IO = -7
GP_ERROR_TIMEOUT = -10
GP_ERROR_IO_USB_FIND = -52
GP_ERROR_IO_USB_CLAIM = -53
GP_ERROR_MODEL_NOT_FOUND = -105
GP_ERROR_CAMERA_BUSY = -110

_TEXT = {
    GP_ERROR: "Unspecified error",
    GP_ERROR_IO: "I/O problem",
    GP_ERROR_TIMEOUT: "Timeout reading from or writing to the port",
    GP_ERROR_IO_USB_FIND: "Could not find the requested device on the USB port",
    GP_ERROR_IO_USB_CLAIM: "Could not claim the USB device",
    GP_ERROR_MODEL_NOT_FOUND: "Unknown model",
    GP_ERROR_CAMERA_BUSY: "I/O in progress",
}

BINDINGS = "camera-bindings.json"


class _GPhoto2Error(Exception):
    """Shaped like python-gphoto2's: a `code` and the text "[code] message"."""

    def __init__(self, code):
        super().__init__(f"[{code}] {_TEXT.get(code, 'error')}")
        self.code = code


class _Rig:
    """The bodies on the bus and the device nodes behind them.

    `wedged` maps a port to the error code its init() raises. What a reset
    does to a port is set in `effects`:

      - "in_place" (the default): the kernel resets the device and it stays
        on its port, no longer wedged;
      - "stuck": the reset changes nothing;
      - "moves": the device disconnects and re-enumerates on `new_port`. With
        `gone_by_settle` its node is gone by the time the settle delay ends;
        without it the node is still there for the retry (which fails), and
        the device leaves during the wait that follows. It is back on the bus
        after `appear_after` listings of the bus;
      - "gone": the device disconnects and never comes back.

    `init_by_port` counts every init(), `live_by_port` the claims not yet
    released, and `detect_calls` every autodetect().
    """

    def __init__(self, serials):
        self.serials = dict(serials)
        self.detected = [(MODEL, port) for port in serials]
        self.present = set(serials)
        self.wedged = {}
        self.effects = {}
        self.open_error = None
        self.ioctl_error = None
        self.init_by_port = {}
        self.live_by_port = {}
        self.opened_nodes = []
        self.ioctls = []
        self.closed_fds = []
        self.sleeps = []
        self.detect_calls = 0
        self.bus_listings = 0
        self.now = 1000.0
        self._fd_to_node = {}
        self._pending = None
        # Called with the port at the moment the reset ioctl is issued.
        self.on_ioctl = None

    @staticmethod
    def port_of_node(node):
        bus, dev = node.rsplit("/", 2)[-2:]
        return f"usb:{bus},{dev}"

    def leave(self, port):
        self.present.discard(port)
        self.detected = [(m, p) for m, p in self.detected if p != port]

    def arrive(self, old_port, new_port):
        self.present.add(new_port)
        self.detected.append((MODEL, new_port))
        self.serials[new_port] = self.serials[old_port]

    # usbfs and sysfs fakes ------------------------------------------------

    def usbfs_open(self, path, flags):
        self.opened_nodes.append((path, flags))
        if self.open_error is not None:
            raise self.open_error
        fd = 100 + len(self.opened_nodes)
        self._fd_to_node[fd] = path
        return fd

    def usbfs_ioctl(self, fd, request, arg=0):
        self.ioctls.append((fd, request, arg))
        if self.ioctl_error is not None:
            raise self.ioctl_error
        port = self.port_of_node(self._fd_to_node[fd])
        if self.on_ioctl is not None:
            self.on_ioctl(port)
        effect = dict(self.effects.get(port, {"mode": "in_place"}))
        mode = effect["mode"]
        if mode == "in_place":
            self.wedged.pop(port, None)
        elif mode == "moves":
            if effect.get("gone_by_settle", True):
                self.leave(port)
            self._pending = (port, effect)
        elif mode == "gone":
            self.leave(port)
        return 0

    def usbfs_close(self, fd):
        self.closed_fds.append(fd)

    def node_exists(self, path):
        if path == gb._USBFS_ROOT:
            return True
        return self.port_of_node(path) in self.present

    def list_canon_devices_on_bus(self, bus):
        self.bus_listings += 1
        if self._pending is not None:
            old_port, effect = self._pending
            if old_port in self.present:
                self.leave(old_port)
            effect["appear_after"] = effect.get("appear_after", 1) - 1
            if effect["appear_after"] <= 0:
                self.arrive(old_port, effect["new_port"])
                self._pending = None
        devnums = set()
        for port in self.present:
            b, d = port[4:].split(",")
            if int(b) == bus:
                devnums.add(int(d))
        return devnums

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self):
        return self.now


def _make_fake_gp(rig):
    class _Widget:
        def __init__(self, value):
            self._value = value

        def get_value(self):
            return self._value

        def set_value(self, value):
            self._value = value

    class _Config:
        def __init__(self, port):
            self._port = port

        def get_child_by_name(self, name):
            if name == "serialnumber":
                return _Widget(f"  {rig.serials.get(self._port, '')}  ")
            if name == "focusmode":
                return _Widget("Manual")
            if name == "cameramodel":
                return _Widget(MODEL)
            return _Widget("")

    class Camera:
        def __init__(self):
            self.port = None
            self._initialized = False

        @staticmethod
        def autodetect():
            rig.detect_calls += 1
            return list(rig.detected)

        def set_abilities(self, abilities):
            pass

        def set_port_info(self, info):
            self.port = info.path

        def init(self):
            rig.init_by_port[self.port] = rig.init_by_port.get(self.port, 0) + 1
            code = rig.wedged.get(self.port)
            if code is not None:
                raise _GPhoto2Error(code)
            self._initialized = True
            rig.live_by_port[self.port] = rig.live_by_port.get(self.port, 0) + 1

        def exit(self):
            if self._initialized:
                self._initialized = False
                rig.live_by_port[self.port] -= 1

        def get_config(self):
            return _Config(self.port)

        def set_config(self, cfg):
            pass

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
        GPhoto2Error=_GPhoto2Error,
        Camera=Camera,
        CameraAbilitiesList=CameraAbilitiesList,
        PortInfoList=PortInfoList,
    )


@pytest.fixture(autouse=True)
def bindings_dir(monkeypatch, tmp_path):
    """Each test gets its own projects root, so no pins leak between tests."""
    from app.core.config import settings

    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "PROJECTS_ROOT", str(root))
    return root


@pytest.fixture
def rig(monkeypatch):
    rig = _Rig({PORT_A: SERIAL_A, PORT_B: SERIAL_B})
    monkeypatch.setattr(gb, "gp", _make_fake_gp(rig))
    monkeypatch.setattr(gb, "_GP_AVAILABLE", True)
    monkeypatch.setattr(gb, "_usbfs_open", rig.usbfs_open)
    monkeypatch.setattr(gb, "_usbfs_ioctl", rig.usbfs_ioctl)
    monkeypatch.setattr(gb, "_usbfs_close", rig.usbfs_close)
    monkeypatch.setattr(gb, "_usb_reset_sleep", rig.sleep)
    monkeypatch.setattr(gb, "_usb_node_exists", rig.node_exists)
    monkeypatch.setattr(gb, "_list_canon_devices_on_bus", rig.list_canon_devices_on_bus)
    monkeypatch.setattr(gb, "_monotonic", rig.monotonic)
    return rig


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING]


def _left_bus_warnings(caplog, port):
    return [
        r for r in _warnings(caplog)
        if port in r.getMessage() and "after the USB reset" in r.getMessage()
    ]


def _recovered_lines(caplog, port):
    return [
        r for r in caplog.records
        if r.levelno == logging.INFO and port in r.getMessage()
        and "recovered" in r.getMessage()
    ]


LOG = logging.getLogger("test-gphoto2-usb-reset")


# ----------------------------------------------------------------------
# the port string and the ioctl
# ----------------------------------------------------------------------

@pytest.mark.unit
def test_the_reset_request_is_usbdevfs_reset():
    # _IO('U', 20): no direction, no size, type 'U' (0x55), number 20.
    assert gb._USBDEVFS_RESET == 0x5514


@pytest.mark.unit
@pytest.mark.parametrize("port, node", [
    ("usb:001,002", "/dev/bus/usb/001/002"),
    ("usb:003,127", "/dev/bus/usb/003/127"),
])
def test_a_usb_port_names_its_device_node(port, node):
    assert gb._usb_device_node(port) == node


@pytest.mark.unit
@pytest.mark.parametrize("port", [
    "", "usb:", "usb:001", "usb:001,", "usb:,002", "usb:001,002,003",
    "usb:001;002", "usb:001,002x", " usb:001,002", "usb:001,002\n",
    "USB:001,002", "ptpip:192.168.1.2", "serial:/dev/ttyS0",
    "usb:../../../etc,002", "usb:001,../x", "usb:-01,002", "usb:0001,002",
    "usb:١٢,002", None, 12,
])
def test_a_malformed_port_is_refused(rig, caplog, port):
    with caplog.at_level(logging.INFO):
        assert gb._usb_device_node(port) is None
        assert gb._reset_usb_device(port, LOG) is False
    assert rig.opened_nodes == []
    assert rig.ioctls == []
    assert len(_warnings(caplog)) == 1


@pytest.mark.unit
def test_a_reset_issues_the_ioctl_on_the_device_node(rig, caplog):
    with caplog.at_level(logging.INFO):
        assert gb._reset_usb_device("usb:001,002", LOG) is True
    assert rig.opened_nodes == [("/dev/bus/usb/001/002", os.O_WRONLY)]
    [(fd, request, _arg)] = rig.ioctls
    assert request == gb._USBDEVFS_RESET == 0x5514
    assert rig.closed_fds == [fd]
    assert _warnings(caplog) == []


@pytest.mark.unit
@pytest.mark.parametrize("error", [
    FileNotFoundError(errno.ENOENT, "No such file or directory"),
    PermissionError(errno.EACCES, "Permission denied"),
])
def test_an_open_that_fails_returns_false_and_warns_once(rig, caplog, error):
    rig.open_error = error
    with caplog.at_level(logging.INFO):
        assert gb._reset_usb_device(PORT_A, LOG) is False
    assert rig.ioctls == []
    [warning] = _warnings(caplog)
    assert PORT_A in warning.getMessage()
    assert "refused" in warning.getMessage()


@pytest.mark.unit
def test_an_ioctl_that_fails_returns_false_warns_once_and_closes(rig, caplog):
    rig.ioctl_error = PermissionError(errno.EPERM, "Operation not permitted")
    with caplog.at_level(logging.INFO):
        assert gb._reset_usb_device(PORT_A, LOG) is False
    [(fd, _req, _arg)] = rig.ioctls
    assert rig.closed_fds == [fd]
    [warning] = _warnings(caplog)
    assert PORT_A in warning.getMessage()
    assert "refused" in warning.getMessage()


# ----------------------------------------------------------------------
# the persistent session open
# ----------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("code", [GP_ERROR_TIMEOUT, GP_ERROR_IO])
def test_a_timed_out_session_open_is_reset_and_retried_once(rig, caplog, code):
    rig.wedged[PORT_A] = code
    with caplog.at_level(logging.INFO):
        session = gb._PTPSession(PORT_A, MODEL, LOG)

    assert session._cam is not None
    assert session.serial == SERIAL_A
    assert rig.init_by_port[PORT_A] == 2
    assert [path for path, _flags in rig.opened_nodes] == ["/dev/bus/usb/001/002"]
    assert len(rig.ioctls) == 1
    assert rig.sleeps == [gb._USB_RESET_SETTLE_S]
    assert len(_recovered_lines(caplog, PORT_A)) == 1
    assert _warnings(caplog) == []


@pytest.mark.unit
def test_a_session_open_that_times_out_again_raises_with_no_second_reset(rig, caplog):
    rig.wedged[PORT_A] = GP_ERROR_TIMEOUT
    rig.effects[PORT_A] = {"mode": "stuck"}
    with caplog.at_level(logging.INFO):
        with pytest.raises(_GPhoto2Error) as info:
            gb._PTPSession(PORT_A, MODEL, LOG)

    assert info.value.code == GP_ERROR_TIMEOUT
    assert rig.init_by_port[PORT_A] == 2
    assert len(rig.ioctls) == 1
    assert _recovered_lines(caplog, PORT_A) == []
    # The body did not move, so the wait for it ran its full length.
    steps = int(gb._USB_REENUMERATE_WAIT_S / gb._USB_REENUMERATE_STEP_S)
    assert rig.sleeps == [gb._USB_RESET_SETTLE_S] + [gb._USB_REENUMERATE_STEP_S] * steps
    [warning] = _left_bus_warnings(caplog, PORT_A)
    message = warning.getMessage()
    assert "[-10]" in message and "no new Canon device" in message


@pytest.mark.unit
@pytest.mark.parametrize("code", [
    GP_ERROR,
    GP_ERROR_IO_USB_FIND,
    GP_ERROR_IO_USB_CLAIM,
    GP_ERROR_MODEL_NOT_FOUND,
    GP_ERROR_CAMERA_BUSY,
])
def test_an_open_that_fails_for_another_reason_is_not_reset(rig, code):
    rig.wedged[PORT_A] = code
    with pytest.raises(_GPhoto2Error) as info:
        gb._PTPSession(PORT_A, MODEL, LOG)

    assert info.value.code == code
    assert rig.init_by_port[PORT_A] == 1
    assert rig.opened_nodes == []
    assert rig.ioctls == []
    assert rig.sleeps == []


@pytest.mark.unit
@pytest.mark.parametrize("error, code", [
    (_GPhoto2Error(GP_ERROR_IO), GP_ERROR_IO),
    # An error carrying only its text classifies by the leading code.
    (Exception("[-10] Timeout reading from or writing to the port"), GP_ERROR_TIMEOUT),
    (Exception("-110: I/O in progress"), GP_ERROR_CAMERA_BUSY),
    # A number elsewhere in the text is not a code.
    (Exception("could not open usb:001,002 after -10 tries"), None),
    (Exception("no code at all"), None),
])
def test_an_error_code_is_read_from_the_code_or_the_leading_text(error, code):
    assert gb._gp_error_code(error) == code


@pytest.mark.unit
def test_a_refused_reset_leaves_the_open_failing_as_before(rig, caplog):
    rig.wedged[PORT_A] = GP_ERROR_TIMEOUT
    rig.open_error = PermissionError(errno.EACCES, "Permission denied")
    with caplog.at_level(logging.INFO):
        with pytest.raises(_GPhoto2Error) as info:
            gb._PTPSession(PORT_A, MODEL, LOG)

    assert info.value.code == GP_ERROR_TIMEOUT
    assert rig.init_by_port[PORT_A] == 1, "the open was retried without a reset"
    assert rig.sleeps == []
    [warning] = _warnings(caplog)
    assert PORT_A in warning.getMessage() and "refused" in warning.getMessage()


# ----------------------------------------------------------------------
# through the backend: the session path and the serial-identification path
# ----------------------------------------------------------------------

def _backend(bindings_dir=None, pins=None):
    if pins is not None:
        import json

        (bindings_dir / BINDINGS).write_text(
            json.dumps({"version": 1, "pins": pins})
        )
    return gb.GPhoto2Backend(LOG)


@pytest.mark.unit
def test_the_backend_session_recovers_a_wedged_body(rig, caplog):
    rig.detected = [(MODEL, PORT_A)]
    rig.wedged[PORT_A] = GP_ERROR_TIMEOUT
    backend = _backend()

    with caplog.at_level(logging.INFO):
        with backend._get_camera_lock(0):
            session = backend._get_or_open_session(0)

    assert session.port == PORT_A and session._cam is not None
    assert backend._claimed_ports == {PORT_A: 0}
    assert len(rig.ioctls) == 1
    assert len(_recovered_lines(caplog, PORT_A)) == 1


@pytest.mark.unit
def test_the_backend_session_still_wedged_after_a_reset_fails_as_before(rig):
    rig.detected = [(MODEL, PORT_A)]
    rig.wedged[PORT_A] = GP_ERROR_TIMEOUT
    rig.effects[PORT_A] = {"mode": "stuck"}
    backend = _backend()

    with backend._get_camera_lock(0):
        with pytest.raises(_GPhoto2Error):
            backend._get_or_open_session(0)

    assert backend._sessions == {}
    assert backend._claimed_ports == {}, "a failed open left its claim behind"
    assert rig.init_by_port[PORT_A] == 2
    assert len(rig.ioctls) == 1


@pytest.mark.unit
def test_identification_recovers_a_wedged_body_onto_its_pinned_index(
    rig, bindings_dir, caplog
):
    """After a restart A is wedged; its serial is read after one reset.

    The bindings pin A to 0 and B to 1. B identifies on the first read; A's
    first open times out, the reset clears it, and the retry reads its serial,
    so A returns to index 0 instead of staying provisional.
    """
    rig.wedged[PORT_A] = GP_ERROR_TIMEOUT
    backend = _backend(bindings_dir, {"0": SERIAL_A, "1": SERIAL_B})

    with caplog.at_level(logging.INFO):
        assert backend.is_camera_connected(0) is True

    assert backend._get_port_map() == {0: (MODEL, PORT_A), 1: (MODEL, PORT_B)}
    assert backend._serial_by_port == {PORT_A: SERIAL_A, PORT_B: SERIAL_B}
    assert [path for path, _flags in rig.opened_nodes] == ["/dev/bus/usb/001/002"]
    assert len(_recovered_lines(caplog, PORT_A)) == 1
    assert "stays provisional" not in caplog.text
    assert all(live == 0 for live in rig.live_by_port.values())
    assert backend._claimed_ports == {}


@pytest.mark.unit
def test_identification_of_a_body_still_wedged_leaves_it_provisional(
    rig, bindings_dir, caplog
):
    rig.wedged[PORT_A] = GP_ERROR_TIMEOUT
    rig.effects[PORT_A] = {"mode": "stuck"}
    backend = _backend(bindings_dir, {"0": SERIAL_A, "1": SERIAL_B})

    with caplog.at_level(logging.INFO):
        assert backend.is_camera_connected(0) is False

    assert 0 not in backend._get_port_map()
    assert PORT_A not in backend._serial_by_port
    assert rig.init_by_port[PORT_A] == 2
    assert len(rig.ioctls) == 1, "one failed open earned more than one reset"
    assert f"no serial could be read from {PORT_A}; it stays provisional" in caplog.text
    assert backend._claimed_ports == {}
    # The retry's failure is no longer swallowed without a trace.
    [warning] = _left_bus_warnings(caplog, PORT_A)
    assert warning.getMessage() == (
        f"[gphoto2] {PORT_A}: after the USB reset the retry failed "
        "([-10] Timeout reading from or writing to the port); "
        "no new Canon device appeared on bus 001 within 8 s"
    )


@pytest.mark.unit
def test_identification_does_not_reset_a_busy_body(rig, bindings_dir):
    rig.wedged[PORT_A] = GP_ERROR_CAMERA_BUSY
    backend = _backend(bindings_dir, {"0": SERIAL_A, "1": SERIAL_B})

    assert backend.is_camera_connected(0) is False
    assert rig.init_by_port[PORT_A] == 1
    assert rig.opened_nodes == []


@pytest.mark.unit
def test_an_enumeration_recovers_a_wedged_body(rig, caplog):
    rig.wedged[PORT_A] = GP_ERROR_TIMEOUT
    backend = _backend()

    with caplog.at_level(logging.INFO):
        devices = backend.list_devices()

    assert [(d["index"], d["serial"]) for d in devices] == [
        (0, SERIAL_A), (1, SERIAL_B)
    ]
    assert len(rig.ioctls) == 1
    assert len(_recovered_lines(caplog, PORT_A)) == 1
    assert all(live == 0 for live in rig.live_by_port.values())


# ----------------------------------------------------------------------
# a body that re-enumerates on a new device number after the reset
# ----------------------------------------------------------------------

def _pinned_backend(rig, bindings_dir):
    return _backend(bindings_dir, {"0": SERIAL_A, "1": SERIAL_B})


@pytest.mark.unit
@pytest.mark.parametrize("effect", [
    {"mode": "moves", "new_port": PORT_A_NEW, "gone_by_settle": True},
    {"mode": "moves", "new_port": PORT_A_NEW, "gone_by_settle": False,
     "appear_after": 3},
], ids=["node-gone-at-settle", "retry-fails-then-moves"])
def test_identification_follows_a_body_that_moved_after_the_reset(
    rig, bindings_dir, caplog, effect
):
    rig.wedged[PORT_A] = GP_ERROR_TIMEOUT
    rig.effects[PORT_A] = effect
    backend = _pinned_backend(rig, bindings_dir)

    with caplog.at_level(logging.INFO):
        assert backend.is_camera_connected(0) is True

    assert backend._get_port_map() == {
        0: (MODEL, PORT_A_NEW), 1: (MODEL, PORT_B)
    }
    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B}
    assert len(rig.ioctls) == 1, "one failed open earned more than one reset"
    expected_inits = 1 if effect["gone_by_settle"] else 2
    assert rig.init_by_port[PORT_A] == expected_inits
    [warning] = _left_bus_warnings(caplog, PORT_A)
    assert PORT_A_NEW in warning.getMessage()
    assert backend._claimed_ports == {}
    assert all(live == 0 for live in rig.live_by_port.values())

    # The next poll finds everything in place and resets nothing.
    assert backend.is_camera_connected(1) is True
    assert len(rig.ioctls) == 1


@pytest.mark.unit
def test_an_enumeration_follows_a_body_that_moved_after_the_reset(
    rig, bindings_dir, caplog
):
    rig.wedged[PORT_A] = GP_ERROR_TIMEOUT
    rig.effects[PORT_A] = {"mode": "moves", "new_port": PORT_A_NEW}
    backend = _pinned_backend(rig, bindings_dir)

    with caplog.at_level(logging.INFO):
        first = backend.list_devices()
        second = backend.list_devices()

    # The moved body is on its pinned index in the same call; the body whose
    # row the move interrupted is on its own by the next call.
    assert (0, SERIAL_A, f"USB {PORT_A_NEW}") in [
        (d["index"], d["serial"], d["location"]) for d in first
    ]
    assert [(d["index"], d["serial"], d["port"]) for d in second] == [
        (0, SERIAL_A, PORT_A_NEW), (1, SERIAL_B, PORT_B)
    ]
    assert len(rig.ioctls) == 1
    assert len(_left_bus_warnings(caplog, PORT_A)) == 1
    assert backend._claimed_ports == {}


@pytest.mark.unit
def test_a_body_that_never_comes_back_stays_provisional_without_raising(
    rig, bindings_dir, caplog
):
    rig.wedged[PORT_A] = GP_ERROR_TIMEOUT
    rig.effects[PORT_A] = {"mode": "gone"}
    backend = _pinned_backend(rig, bindings_dir)

    with caplog.at_level(logging.INFO):
        assert backend.is_camera_connected(0) is False
        devices = backend.list_devices()

    assert 0 not in backend._get_port_map()
    assert [d["index"] for d in devices] == [1]
    assert rig.init_by_port[PORT_A] == 1, "a port with no node was retried"
    assert len(rig.ioctls) == 1
    [warning] = _left_bus_warnings(caplog, PORT_A)
    message = warning.getMessage()
    assert "[-10]" in message and "no new Canon device" in message
    steps = int(gb._USB_REENUMERATE_WAIT_S / gb._USB_REENUMERATE_STEP_S)
    assert rig.sleeps == [gb._USB_RESET_SETTLE_S] + [gb._USB_REENUMERATE_STEP_S] * steps
    assert backend._claimed_ports == {}


@pytest.mark.unit
def test_the_session_follows_a_body_that_moved_after_the_reset(
    rig, bindings_dir, caplog
):
    backend = _pinned_backend(rig, bindings_dir)
    assert [d["index"] for d in backend.list_devices()] == [0, 1]
    # The body wedges after it was identified, and the reset moves it.
    rig.wedged[PORT_A] = GP_ERROR_TIMEOUT
    rig.effects[PORT_A] = {"mode": "moves", "new_port": PORT_A_NEW}

    with caplog.at_level(logging.INFO):
        with backend._get_camera_lock(0):
            session = backend._get_or_open_session(0)
            assert backend._session_matches_map(0) is session

    assert session.port == PORT_A_NEW and session.serial == SERIAL_A
    assert backend._get_port_map()[0] == (MODEL, PORT_A_NEW)
    assert backend._claimed_ports == {PORT_A_NEW: 0}
    assert len(rig.ioctls) == 1
    assert len(_left_bus_warnings(caplog, PORT_A)) == 1


@pytest.mark.unit
def test_the_session_open_after_a_move_is_never_reset_again(rig, bindings_dir):
    backend = _pinned_backend(rig, bindings_dir)
    backend.list_devices()
    rig.wedged[PORT_A] = GP_ERROR_TIMEOUT
    rig.effects[PORT_A] = {"mode": "moves", "new_port": PORT_A_NEW}
    rig.wedged[PORT_A_NEW] = GP_ERROR_TIMEOUT

    with backend._get_camera_lock(0):
        with pytest.raises(_GPhoto2Error) as info:
            backend._get_or_open_session(0)

    assert info.value.code == GP_ERROR_TIMEOUT
    assert len(rig.ioctls) == 1
    assert rig.init_by_port[PORT_A_NEW] == 1
    assert backend._sessions == {}
    assert backend._claimed_ports == {}


@pytest.mark.unit
def test_the_session_reports_a_body_that_never_came_back(rig, bindings_dir):
    backend = _pinned_backend(rig, bindings_dir)
    backend.list_devices()
    rig.wedged[PORT_A] = GP_ERROR_TIMEOUT
    rig.effects[PORT_A] = {"mode": "gone"}

    with backend._get_camera_lock(0):
        with pytest.raises(RuntimeError, match="left the bus"):
            backend._get_or_open_session(0)

    assert 0 not in backend._get_port_map()
    assert len(rig.ioctls) == 1
    assert backend._claimed_ports == {}


@pytest.mark.unit
def test_each_reset_runs_under_its_camera_lock_and_outside_the_map_lock(
    rig, bindings_dir
):
    backend = _pinned_backend(rig, bindings_dir)
    seen = []

    def check(port):
        # The rig is single-threaded, so a held lock is this thread's.
        held = sorted(i for i, lock in backend._session_locks.items() if lock.locked())
        seen.append((port, held, backend._map_lock.locked()))

    rig.on_ioctl = check
    # Identification: A is wedged after the restart.
    rig.wedged[PORT_A] = GP_ERROR_TIMEOUT
    assert backend.is_camera_connected(0) is True
    # Session open: B wedges once it has been identified.
    rig.wedged[PORT_B] = GP_ERROR_TIMEOUT
    with backend._get_camera_lock(1):
        backend._get_or_open_session(1)

    provisional_a = seen[0][1]
    assert len(provisional_a) == 1 and provisional_a[0] >= 2, seen
    assert seen[1:] == [(PORT_B, [1], False)]
    assert seen[0][2] is False


# ----------------------------------------------------------------------
# the device listing notices a stale map
# ----------------------------------------------------------------------

@pytest.mark.unit
def test_the_listing_does_not_scan_while_every_body_is_in_place(rig, bindings_dir):
    backend = _pinned_backend(rig, bindings_dir)
    backend.list_devices()
    scans = rig.detect_calls

    for _ in range(3):
        rows = backend.list_devices()

    assert [(d["index"], d["port"]) for d in rows] == [(0, PORT_A), (1, PORT_B)]
    assert rig.detect_calls == scans


@pytest.mark.unit
def test_the_listing_redetects_when_a_published_port_has_left_the_bus(
    rig, bindings_dir
):
    backend = _pinned_backend(rig, bindings_dir)
    backend.list_devices()
    scans = rig.detect_calls
    # B re-enumerates with nothing in the backend asking about it.
    rig.leave(PORT_B)
    rig.arrive(PORT_B, PORT_B_NEW)

    rows = backend.list_devices()

    assert rig.detect_calls == scans + 1
    assert [(d["index"], d["port"]) for d in rows] == [(0, PORT_A), (1, PORT_B_NEW)]
    assert rig.ioctls == []


@pytest.mark.unit
def test_the_listing_redetects_a_pinned_absent_index_at_most_once_per_interval(
    rig, bindings_dir
):
    backend = _pinned_backend(rig, bindings_dir)
    backend.list_devices()
    # B is unplugged: its port leaves the bus, so the next listing scans.
    rig.leave(PORT_B)
    scans = rig.detect_calls
    assert [d["index"] for d in backend.list_devices()] == [0]
    assert rig.detect_calls == scans + 1

    # Index 1 is pinned and absent, but the bus was read just now.
    backend.list_devices()
    assert rig.detect_calls == scans + 1

    rig.now += gb._LISTING_REDETECT_MIN_S
    # B is plugged back in on a new port.
    rig.present.add(PORT_B_NEW)
    rig.detected.append((MODEL, PORT_B_NEW))
    rig.serials[PORT_B_NEW] = SERIAL_B
    rows = backend.list_devices()
    assert rig.detect_calls == scans + 2
    assert [(d["index"], d["port"]) for d in rows] == [(0, PORT_A), (1, PORT_B_NEW)]


# ----------------------------------------------------------------------
# reading the bus
# ----------------------------------------------------------------------

@pytest.mark.unit
def test_the_bus_listing_reads_canon_devices_from_sysfs(monkeypatch, tmp_path):
    sysfs = tmp_path / "sys"
    usbfs = tmp_path / "dev"

    def device(name, busnum, devnum, vendor, node=True):
        d = sysfs / name
        d.mkdir(parents=True)
        (d / "busnum").write_text(f"{busnum}\n")
        (d / "devnum").write_text(f"{devnum}\n")
        (d / "idVendor").write_text(f"{vendor}\n")
        if node:
            n = usbfs / f"{busnum:03d}" / f"{devnum:03d}"
            n.parent.mkdir(parents=True, exist_ok=True)
            n.write_bytes(b"")

    device("usb1", 1, 1, "1d6b")            # the root hub
    device("1-1", 1, 4, "04a9")             # a Canon body
    device("1-2", 1, 6, "04a9", node=False)  # no node yet: not ready
    device("3-1", 3, 2, "04a9")             # another bus
    (sysfs / "1-1:1.0").mkdir()             # an interface, not a device

    monkeypatch.setattr(gb, "_SYSFS_USB_DEVICES", str(sysfs))
    monkeypatch.setattr(gb, "_USBFS_ROOT", str(usbfs))

    assert gb._canon_devices_on_bus(1) == {4}
    assert gb._canon_devices_on_bus(3) == {2}
    assert gb._canon_devices_on_bus(2) == set()
    monkeypatch.setattr(gb, "_SYSFS_USB_DEVICES", str(tmp_path / "missing"))
    assert gb._canon_devices_on_bus(1) == set()
