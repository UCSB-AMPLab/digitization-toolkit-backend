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
from collections import namedtuple


DeviceInfo = namedtuple("DeviceInfo", [
    "vendor_id", "product_id", "bus_num", "device_num", "serial_num",
])


class PTPError(Exception):
    """Stands in for pychdk.ptp.PTPError, which carries a PTP response code."""

    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or f"PTP error 0x{code:04x}")


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
        # what happened to it
        self.opens = 0
        self.closes = 0
        self.mode_switches = []
        self.shots = []
        self.uploads = []
        self.frames_served = 0
        self.device = None

    @property
    def key(self):
        return (self.bus, self.address)


class _FakeUsbDevice:
    def __init__(self, body):
        self.product = body.product


class _FakeChdkPTP:
    """Stands in for ChdkDevice._chdk, which is where get_display_data lives."""

    def __init__(self, body):
        self._body = body

    def get_display_data(self, flags=0):
        self._body.frames_served += 1
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
        body.device = self

    @property
    def is_connected(self):
        return self._connected

    def switch_mode(self, mode):
        self._body.mode_switches.append(mode)

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
        if self._body.shoot_error is not None:
            raise self._body.shoot_error
        return self._body.image

    def close(self):
        self._connected = False
        self._body.closes += 1


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
