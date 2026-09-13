"""A fake pychdk, injected the way the gphoto2 tests inject a fake gp.

pychdk talks to a Canon compact over USB, so nothing in it can run in the
test container. capture.backends.chdk_backend reaches the library only
through the module attribute ``pychdk``, so a test replaces that attribute
with the object ``make_pychdk`` builds here and drives the backend against
scripted bodies.

parse_own_txt and format_own_txt below must stay character-for-character the
library's own (src/pychdk/util.py): they are pure text functions with no USB
in them, and the backend relies on the library's rules - an id comes back
lowercased, a malformed file parses as nothing rather than raising. A second
implementation with rules of its own would let these tests agree with
themselves while disagreeing with the library.
"""

import re
import threading
from collections import namedtuple


DeviceInfo = namedtuple("DeviceInfo", [
    "vendor_id", "product_id", "bus_num", "device_num", "serial_num",
])


class PTPError(Exception):
    """Stands in for pychdk.ptp.PTPError, which carries a PTP response code."""

    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or f"PTP error 0x{code:04x}")


class TransportError(Exception):
    """Stands in for usb.core.USBError, which is not a PTPError.

    A cable pulled mid-transfer raises from pyusb, below the protocol layer,
    so nothing about it carries a PTP response code and code that only
    catches PTPError lets it straight past.
    """


# --- the library's own OWN.TXT text functions ------------------------------

_CAMERA_ID_RE = re.compile(r"^[0-9a-f]{12,32}$")


def parse_own_txt(data):
    """Read a body's page parity and id from OWN.TXT."""
    if isinstance(data, (bytes, bytearray)):
        text = bytes(data).decode("utf-8-sig", errors="replace")
    elif isinstance(data, str):
        text = data
    else:
        return None, None

    side = None
    camera_id = None
    for line in text.splitlines():
        line = line.replace("\ufeff", "").strip()
        if not line:
            continue
        if side is None and line.upper() in ("ODD", "EVEN"):
            side = line.upper()
        elif camera_id is None and line.upper().startswith("ID="):
            value = line.split("=", 1)[1].strip().lower()
            if _CAMERA_ID_RE.match(value):
                camera_id = value
    return side, camera_id


def format_own_txt(side, camera_id=None):
    """Render the canonical OWN.TXT."""
    lines = []
    if side:
        lines.append(str(side).strip().upper())
    if camera_id:
        lines.append("id=" + str(camera_id).strip())
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


# --- the scripted hardware ------------------------------------------------

class Gate:
    """A scripted pause inside one call on a body.

    A lifetime race needs one thread stopped mid-call while another runs, so
    a test asks a body to pause in close(), shoot() or a frame, waits for it
    to arrive, does its other work, and then lets it go.
    """

    def __init__(self, name):
        self.name = name
        self.entered = threading.Event()
        self.released = threading.Event()

    def arrive(self, timeout=10):
        self.entered.set()
        if not self.released.wait(timeout):
            raise AssertionError(f"the {self.name} gate was never released")

    def wait_until_entered(self, timeout=10):
        assert self.entered.wait(timeout), f"nothing reached the {self.name} gate"

    def release(self):
        self.released.set()


class Body:
    """One scripted camera: what it answers, and what it was asked.

    ``card`` is the content of A/OWN.TXT as bytes, or None for a card with no
    such file. ``download_error`` replaces that answer with an exception, so a
    test can tell a missing file from a body that has gone wrong.
    """

    def __init__(
        self,
        bus=1,
        address=4,
        serial="1111111",
        product="Canon PowerShot A2500",
        card=None,
        download_error=None,
        upload_error=None,
        frame=b"",
        image=b"",
        shoot_error=None,
        preview_error=None,
        mode_error=None,
        product_error=None,
    ):
        self.bus = bus
        self.address = address
        self.serial = serial
        self.product = product
        self.card = card
        self.download_error = download_error
        self.upload_error = upload_error
        self.frame = frame
        self.image = image
        self.shoot_error = shoot_error
        self.preview_error = preview_error
        self.mode_error = mode_error
        self.product_error = product_error
        # what happened to it
        self.opens = 0
        self.closes = 0
        self.mode_switches = []
        self.shots = []
        self.uploads = []
        self.frames_served = 0
        self.device = None
        # Open devices for this one camera, now and at the worst moment. Two
        # at once is the failure a lifetime test is looking for.
        self.live = 0
        self.max_live = 0
        self._live_lock = threading.Lock()
        self.gates = {}

    def gate(self, name):
        """Pause the next call to `name` on this body until it is released."""
        gate = Gate(name)
        self.gates[name] = gate
        return gate

    def _pass(self, name):
        gate = self.gates.pop(name, None)
        if gate is not None:
            gate.arrive()

    def _opened(self):
        with self._live_lock:
            self.live += 1
            self.max_live = max(self.max_live, self.live)

    def _closed(self):
        with self._live_lock:
            self.live -= 1

    @property
    def key(self):
        return (self.bus, self.address)


class _FakeUsbDevice:
    """pyusb reads a string descriptor over the wire, so it can fail."""

    def __init__(self, body):
        self._body = body

    @property
    def product(self):
        if self._body.product_error is not None:
            raise self._body.product_error
        return self._body.product


class _FakeChdkPTP:
    """Stands in for ChdkDevice._chdk, which is where get_display_data lives."""

    def __init__(self, body):
        self._body = body

    def get_display_data(self, flags=0):
        self._body.frames_served += 1
        self._body._pass("frame")
        if self._body.preview_error is not None:
            raise self._body.preview_error
        self.last_flags = flags
        return self._body.frame


class FakeChdkDevice:
    def __init__(self, device_info, body):
        self.info = device_info
        self._body = body
        self._usb_device = _FakeUsbDevice(body)
        self._chdk = _FakeChdkPTP(body)
        self._connected = True
        body.opens += 1
        body._opened()
        body.device = self

    @property
    def is_connected(self):
        return self._connected

    def switch_mode(self, mode):
        self._body.mode_switches.append(mode)
        self._body._pass("switch_mode")
        if self._body.mode_error is not None:
            raise self._body.mode_error

    def download_file(self, remote_path):
        if self._body.download_error is not None:
            raise self._body.download_error
        if self._body.card is None:
            raise PTPError(0x2002)
        return self._body.card

    def upload_file(self, local_path, remote_path):
        if self._body.upload_error is not None:
            raise self._body.upload_error
        with open(local_path, "rb") as handle:
            payload = handle.read()
        self._body.uploads.append((remote_path, payload))
        self._body.card = payload

    def shoot(self, **kwargs):
        self._body.shots.append(kwargs)
        self._body._pass("shoot")
        if self._body.shoot_error is not None:
            raise self._body.shoot_error
        return self._body.image

    def close(self):
        self._connected = False
        self._body._pass("close")
        self._body.closes += 1
        self._body._closed()


class FakePychdk:
    """The module object the backend is given in place of pychdk."""

    PTPError = PTPError
    DeviceInfo = DeviceInfo
    parse_own_txt = staticmethod(parse_own_txt)
    format_own_txt = staticmethod(format_own_txt)

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.list_calls = 0
        self.list_error = None

    def by_key(self, key):
        for body in self.bodies:
            if body.key == key:
                return body
        raise AssertionError(f"no scripted body at {key}")

    def list_devices(self):
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return [
            DeviceInfo(
                vendor_id=0x04A9,
                product_id=0x325B,
                bus_num=body.bus,
                device_num=body.address,
                serial_num=body.serial,
            )
            for body in self.bodies
        ]

    def ChdkDevice(self, device_info, _usb_device=None):
        body = self.by_key((device_info.bus_num, device_info.device_num))
        return FakeChdkDevice(device_info, body)


class HeldLock:
    """A body's lock that parks the first thread to reach it.

    The window a stale-body test is about opens between resolving a camera
    index and acquiring that index's lock. Holding it open by pausing a
    rescan does not work: the rescan releases each body's lock when it has
    read that body's card, well before it publishes the new layout, so the
    queued operation can wake, revalidate against the layout that has not
    changed yet, and succeed - which is correct behaviour and proves nothing.

    So the test parks the queued operation itself, before the lock rather
    than behind it, runs the rescan to completion, and only then lets it
    through. Then the operation is looking at a layout that has definitely
    been published, and there is one reason it can fail.

    Only the first arrival is parked, so the rescan takes the real lock
    normally and a path that re-enters (a failure evicting the body it holds)
    is not parked either.
    """

    def __init__(self, lock):
        self._lock = lock
        self.arrived = threading.Event()
        self._through = threading.Event()
        self._parked = False

    def acquire(self, *args, **kwargs):
        if not self._parked:
            self._parked = True
            self.arrived.set()
            if not self._through.wait(10):
                raise AssertionError("the parked thread was never let through")
        return self._lock.acquire(*args, **kwargs)

    def let_through(self):
        self._through.set()

    def release(self):
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


def park_at_lock(backend, camera_index):
    """Park the next thread that reaches this body's lock until let through."""
    body = backend._body_at(camera_index)
    held = HeldLock(body.lock)
    body.lock = held
    return held


def make_pychdk(*bodies):
    """Build the fake module for these bodies, in the USB order given."""
    return FakePychdk(bodies)


def make_backend(monkeypatch, fake, logger_name="test-chdk"):
    """Install the fake and return a ChdkBackend built on it."""
    import logging

    import capture.backends.chdk_backend as cb

    monkeypatch.setattr(cb, "pychdk", fake)
    monkeypatch.setattr(cb, "_PYCHDK_AVAILABLE", True)
    return cb.ChdkBackend(logging.getLogger(logger_name))


def viewport_frame(
    visible_width=8,
    visible_height=2,
    buffer_width=None,
    margins=(0, 0, 0, 0),
    aspect=0,
    luma=200,
):
    """One well-formed LV_FB_YUV8 live view frame of a flat grey viewport.

    The shapes a decoder must refuse are built in test_chdk_live_view.py; this
    is only ever the good case, for a fake body to serve.
    """
    import struct

    buffer_width = buffer_width or visible_width
    header_size = 28
    desc_size = 36
    data_start = header_size + desc_size
    header = struct.pack("<7i", 2, 1, aspect, 0, 0, header_size, 0)
    desc = struct.pack(
        "<9i", 0, data_start, buffer_width, visible_width, visible_height,
        *margins,
    )
    row = bytes([0, luma, 0, luma, luma, luma]) * (buffer_width // 4)
    return header + desc + row * visible_height
