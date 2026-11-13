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

Version: 0.1.0-beta
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


class ScriptSentWhileRunning(BaseException):
    """A script reached a fake body while another script was still running on it.

    CHDK's ExecuteScript kills a running script unless the request carries
    the NOKILL flag. On 12 November 2026 the backend sent enter_alt() 0.2 s
    after a capture's last chunk, while that capture script was still running,
    and the next picture was delivered without its first 17920 bytes. The fake
    raises this whenever a script arrives over a running one. It derives from
    BaseException so that the backend's own `except Exception` handlers cannot
    turn it into an ordinary failed capture: the test fails instead.
    """


# CHDK's script message types (core/ptp.h), as pychdk's MessageType has them.
MSG_NONE = 0
MSG_ERR = 1
MSG_RET = 2

ScriptMessage = namedtuple(
    "ScriptMessage", ["msg_type", "data_type", "script_id", "value"]
)


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
        product_id=0x325B,
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
        version_error=None,
        stuck_in_play=False,
        switch_without_arriving=False,
        half_press_arrives=False,
        half_press_reads=1,
        lua_error=None,
        zoom_steps=None,
        zoom=0,
        zoom_follows=True,
        zoom_error=None,
        port_numbers=None,
        capture_script_reads=0,
        script_reads=None,
        script_reads_after_return=None,
        shot_chunks=None,
        always_ready=False,
        strict_scripts=True,
    ):
        self.bus = bus
        self.address = address
        self.serial = serial
        self.product_id = product_id
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
        self.version_error = version_error
        self.version_calls = 0
        # This is CHDK's *Lua* get_mode(), and it is the opposite way round
        # from uBASIC's: it returns three values - is_record, is_video, mode -
        # and the first is true in record, false in play. CHDK trunk
        # core/luascript.c, luaCB_get_mode, pushes them in that order and
        # the first is (mode & MODE_MASK) != MODE_PLAY; later builds write
        # the same thing as !camera_info.state.mode_play. The library hands
        # back the first RET message, so is_record is what a caller reads,
        # and mode_value stands for it here.
        #
        # Do not carry uBASIC's convention across: a fake that returns 0 for
        # record leaves the polarity untestable.
        #
        # The two ways a switch can fail are separate knobs, because the
        # pinned library produces only one of them. stuck_in_play is a body
        # that never arrives: pychdk 0.1.3 polls get_mode itself and raises
        # once its retries run out, so that is what this fake does, with the
        # library's own message.
        #
        # switch_without_arriving produces the other state - switch_mode
        # returns, and mode_value is left at whatever the test set. That is
        # what 0.1.2 did when its confirmation loop fell through silently,
        # and it is the state the backend's own second read of get_mode
        # exists to catch. No camera makes 0.1.3 behave this way; a library
        # that regressed would, which is the whole reason the backend does
        # not take switch_mode's return as proof.
        self.stuck_in_play = stuck_in_play
        self.switch_without_arriving = switch_without_arriving
        # A body that takes switch_mode_usb(1), extends the lens and stays in
        # playback; a half-press of the shutter then completes the switch
        # without taking a picture. With
        # half_press_arrives, a click("shoot_half") makes get_mode report
        # record after half_press_reads further reads that still say play.
        # Without it, the half-press is counted and changes nothing.
        self.half_press_arrives = half_press_arrives
        self.half_press_reads = half_press_reads
        self.half_presses = 0
        self._reads_until_record = None
        self.mode_value = False
        self.lua_error = lua_error
        self.lua_calls = []
        # The timeout each lua_execute call was given, in the order made.
        self.lua_timeouts = []
        # Optical zoom. zoom_steps is what get_zoom_steps() answers: None by
        # default, which is what this fake answered for every script before
        # zoom was added, and 64 on an A2500 under CHDK 1.5.1. zoom is the
        # step get_zoom() reports. With zoom_follows, set_zoom(n) moves the
        # lens to n; without it the lens stays where it is, which is a camera
        # that does not confirm the step. zoom_error is raised by set_zoom.
        self.zoom_steps = zoom_steps
        self.zoom = zoom
        self.zoom_follows = zoom_follows
        self.zoom_error = zoom_error
        # Every switch_mode and lua_execute call, in the order made.
        self.events = []
        # Scripts and their running state. CHDK runs one script at a time, and
        # ScriptStatus reports whether one is running. Here a running script
        # is a countdown: running_reads is how many more status reads report
        # it running; the read that brings it to zero still reports running,
        # and the next one does not. capture_script_reads is the countdown a
        # capture leaves behind once its last chunk has been delivered: on
        # 12 November 2026 the capture script was still running for a short
        # while after the host had the picture. script_reads maps a substring
        # of a Lua script to the countdown that script starts, so a set_zoom
        # can be made to keep running while the lens travels.
        self.capture_script_reads = capture_script_reads
        self.script_reads = dict(script_reads or {})
        # The same, for a script whose RET message is posted at once and
        # which goes on running afterwards: a host that returns on the RET
        # finds it still running.
        self.script_reads_after_return = dict(script_reads_after_return or {})
        self.running_reads = 0
        self.running_script = None
        self.status_reads = 0
        # Every script that reached the body, in order: its text (or
        # "<capture>" and "switch_mode_usb(...)" for the two the library
        # sends itself), the ExecuteScript flags when the backend sent it
        # (None when the library did), and whether a script was running when
        # it arrived. With strict_scripts, a script arriving over a running
        # one raises ScriptSentWhileRunning instead of being recorded.
        self.scripts = []
        self.strict_scripts = strict_scripts
        self.messages = []
        self._pending_message = None
        self._next_script_id = 1
        # Remote capture, chunk by chunk. Each chunk is (bytes, more,
        # position) as PTP_CHDK_RemoteCaptureGetData describes it: more is
        # False on the chunk the camera flags last, and position is the seek
        # (-1 for none). shot_chunks holds one chunk list per shot; when it is
        # used up, a shot is the whole of `image` in one chunk. The fake's
        # shoot assembles a picture the way pychdk 0.1.3's
        # remote_capture_get_data does: it stops at the first chunk flagged
        # last, zero-fills anything a seek skips over, and leaves the chunks
        # after that one ready on the camera, where the next shot picks them
        # up first. ready_chunks is what the camera still offers;
        # discarded_chunks what a drain read and threw away. always_ready is
        # a camera that never stops offering data.
        self.shot_chunks = [list(chunks) for chunks in (shot_chunks or [])]
        self.ready_chunks = []
        self.discarded_chunks = []
        self.always_ready = always_ready
        self.last_chunks = 0
        self.last_shot_chunks = []
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
        # The hub ports pyusb reports from the root hub to this body's
        # socket, as usb.core.Device.port_numbers; None where pyusb cannot say.
        self.port_numbers = port_numbers

    def gate(self, name):
        """Pause the next call to `name` on this body until it is released."""
        gate = Gate(name)
        self.gates[name] = gate
        return gate

    def _pass(self, name):
        gate = self.gates.pop(name, None)
        if gate is not None:
            gate.arrive()

    def _script_arrives(self, script, flags):
        """Record a script reaching the camera; refuse one sent over another."""
        running = self.running_reads > 0
        if running and self.strict_scripts:
            raise ScriptSentWhileRunning(
                f"{script!r} was sent while {self.running_script!r} was still "
                "running on the camera"
            )
        self.scripts.append(
            {"script": script, "flags": flags, "while_running": running}
        )

    def _start_running(self, script, reads, message=None):
        """Leave a script running for `reads` status reads, then post `message`."""
        if reads > 0:
            self.running_reads = reads
            self.running_script = script
            self._pending_message = message
        elif message is not None:
            self.messages.append(message)

    def _status(self):
        """One ScriptStatus read: (running, has_messages)."""
        self.status_reads += 1
        running = self.running_reads > 0
        if running:
            self.running_reads -= 1
            if self.running_reads == 0:
                self.running_script = None
                if self._pending_message is not None:
                    self.messages.append(self._pending_message)
                    self._pending_message = None
        return running, bool(self.messages)

    def _answer(self, lua_code):
        """What a Lua script returns on this body; may raise a scripted error."""
        if "get_zoom_steps" in lua_code:
            return self.zoom_steps
        if "get_zoom()" in lua_code:
            return self.zoom
        match = re.fullmatch(r"set_zoom\((-?\d+)\)", lua_code.strip())
        if match:
            if self.zoom_error is not None:
                raise self.zoom_error
            if self.zoom_follows:
                self.zoom = int(match.group(1))
            return None
        if 'click("shoot_half")' in lua_code:
            self.half_presses += 1
            if self.half_press_arrives:
                self._reads_until_record = self.half_press_reads
            return None
        if "get_mode" in lua_code:
            if self._reads_until_record is not None:
                if self._reads_until_record == 0:
                    self._reads_until_record = None
                    self.mode_value = True
                else:
                    self._reads_until_record -= 1
            return self.mode_value
        return None

    def _reads_for(self, lua_code):
        for fragment, reads in self.script_reads.items():
            if fragment in lua_code:
                return reads
        return 0

    def _take_picture(self):
        """Assemble one shot the way pychdk 0.1.3 does, and leave the rest ready."""
        if self.shot_chunks:
            planned = self.shot_chunks.pop(0)
        elif self.image:
            planned = [(self.image, False, -1)]
        else:
            planned = []
        chunks = self.ready_chunks + planned
        self.ready_chunks = []
        picture = bytearray()
        cursor = 0
        used = []
        for number, (data, more, position) in enumerate(chunks):
            used.append((len(data), more, position))
            if position >= 0:
                cursor = position
            end = cursor + len(data)
            if len(picture) < end:
                picture.extend(bytes(end - len(picture)))
            picture[cursor:end] = data
            cursor = end
            if not more:
                self.ready_chunks = chunks[number + 1:]
                break
        self.last_chunks = len(used)
        self.last_shot_chunks = used
        return bytes(picture)

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

    @property
    def bus(self):
        return self._body.bus

    @property
    def port_numbers(self):
        return self._body.port_numbers


class _FakeChdkPTP:
    """Stands in for ChdkDevice._chdk, which is where get_display_data lives."""

    def __init__(self, body):
        self._body = body

    def get_version(self):
        """The cheapest CHDK transaction there is: one command, no data."""
        self._body.version_calls += 1
        if self._body.version_error is not None:
            raise self._body.version_error
        return (2, 9)

    @property
    def last_capture_chunks(self):
        """pychdk 0.1.3 keeps only the count of the last capture's chunks."""
        return self._body.last_chunks

    def get_script_status(self):
        return self._body._status()

    def read_script_message(self):
        if self._body.messages:
            return self._body.messages.pop(0)
        return ScriptMessage(MSG_NONE, 1, 0, None)

    def drain_messages(self):
        self._body.messages.clear()

    def execute_script(self, script, language=0, flags=0):
        """Start a script. A Lua `return` posts a RET message when it ends."""
        body = self._body
        body._script_arrives(script, flags)
        body.lua_calls.append(script)
        body.events.append(("lua", script))
        if body.lua_error is not None:
            raise body.lua_error
        value = body._answer(script)
        script_id = body._next_script_id
        body._next_script_id += 1
        message = (
            ScriptMessage(MSG_RET, 0, script_id, value)
            if script.lstrip().startswith("return") else None
        )
        after = next(
            (reads for fragment, reads in body.script_reads_after_return.items()
             if fragment in script),
            0,
        )
        if after:
            if message is not None:
                body.messages.append(message)
            body._start_running(script, after)
        else:
            body._start_running(script, body._reads_for(script), message)
        return script_id

    def remote_capture_is_ready(self):
        if self._body.always_ready or self._body.ready_chunks:
            return True, 0x01
        return False, 0

    def remote_capture_get_chunk(self, format_flag):
        body = self._body
        if body.ready_chunks:
            chunk = body.ready_chunks.pop(0)
        elif body.always_ready:
            chunk = (b"\x00" * 16, True, -1)
        else:
            raise PTPError(0x2002, "no remote capture data")
        body.discarded_chunks.append((len(chunk[0]), chunk[1], chunk[2]))
        return chunk

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

    @property
    def last_capture_chunks(self):
        return self._chdk.last_capture_chunks

    def switch_mode(self, mode):
        self._body._script_arrives(
            f"switch_mode_usb({1 if mode == 'record' else 0})", None
        )
        self._body.mode_switches.append(mode)
        self._body.events.append(("switch", mode))
        self._body._pass("switch_mode")
        if self._body.mode_error is not None:
            raise self._body.mode_error
        if self._body.switch_without_arriving:
            # A library that returned without the camera having arrived.
            # mode_value is untouched, so the caller reads what the test set.
            return
        if self._body.stuck_in_play:
            # What pychdk 0.1.3 does when its own poll runs out of retries.
            raise RuntimeError(
                f"The camera did not switch to {mode} mode: get_mode() last "
                f"reported is_record={self._body.mode_value!r}"
            )
        # is_record: true once the camera has arrived in record.
        self._body.mode_value = mode == "record"

    def lua_execute(self, lua_code, do_return=True, timeout=10.0):
        """pychdk's lua_execute: one script, waited on until it returns."""
        self._body._script_arrives(lua_code, None)
        self._body.lua_calls.append(lua_code)
        self._body.lua_timeouts.append(timeout)
        self._body.events.append(("lua", lua_code))
        if self._body.lua_error is not None:
            raise self._body.lua_error
        return self._body._answer(lua_code)

    def download_file(self, remote_path):
        self._body._pass("download")
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
        """pychdk's shoot(stream=True): it sends the capture script itself."""
        self._body._script_arrives("<capture>", None)
        self._body.shots.append(kwargs)
        self._body._pass("shoot")
        if self._body.shoot_error is not None:
            raise self._body.shoot_error
        picture = self._body._take_picture()
        self._body._start_running("<capture>", self._body.capture_script_reads)
        return picture

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
        self.list_gate = None

    def gate_list(self):
        """Pause the next bus scan, which holds the backend's layout lock."""
        self.list_gate = Gate("list_devices")
        return self.list_gate

    def by_key(self, key):
        for body in self.bodies:
            if body.key == key:
                return body
        raise AssertionError(f"no scripted body at {key}")

    def list_devices(self):
        self.list_calls += 1
        gate, self.list_gate = self.list_gate, None
        if gate is not None:
            gate.arrive()
        if self.list_error is not None:
            raise self.list_error
        return [
            DeviceInfo(
                vendor_id=0x04A9,
                product_id=body.product_id,
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


class ReportingLock:
    """A body's lock that says when a thread has had to wait for it.

    A test cannot see a thread block, so one that needs to know a queued
    operation has reached the lock would otherwise pause for a fixed time and
    hope. This tries the lock without blocking first: a failure there is the
    thread about to wait, which is the moment worth recording. Nothing is
    held back - the blocking acquire follows immediately.

    Re-entrant acquisition by the thread already holding it succeeds without
    blocking, so a path that re-enters is not reported as waiting.
    """

    def __init__(self, lock):
        self._lock = lock
        self.blocked = threading.Event()

    def acquire(self, *args, **kwargs):
        if self._lock.acquire(blocking=False):
            return True
        self.blocked.set()
        return self._lock.acquire(*args, **kwargs)

    def release(self):
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


def watch_lock(backend, camera_index):
    """Report when a thread has to wait for this body's lock."""
    body = backend._body_at(camera_index)
    watched = ReportingLock(body.lock)
    body.lock = watched
    return watched


def park_at_lock(backend, camera_index):
    """Park the next thread that reaches this body's lock until let through."""
    body = backend._body_at(camera_index)
    held = HeldLock(body.lock)
    body.lock = held
    return held


def make_pychdk(*bodies):
    """Build the fake module for these bodies, in the USB order given."""
    return FakePychdk(bodies)


def make_backend(monkeypatch, fake, logger_name="test-chdk", registry=None):
    """Install the fake and return a ChdkBackend built on it.

    The backend reads and writes stored zoom steps through its
    _registry_factory. A test that passes no registry gets a backend with
    none, so nothing is read from or written to a real cameras.json; a test
    about stored steps passes a CameraRegistry on a temporary path.
    """
    import logging

    import capture.backends.chdk_backend as cb

    monkeypatch.setattr(cb, "pychdk", fake)
    monkeypatch.setattr(cb, "_PYCHDK_AVAILABLE", True)
    # The backend waits through its module's _sleep and reads time through
    # its _monotonic; the tests record the waits on the fake instead of
    # spending them, and a wait moves the fake's clock on by its length.
    # fake.advance moves the clock without a wait.
    fake.sleeps = []
    fake.now = 1000.0

    def sleep(seconds):
        fake.sleeps.append(seconds)
        fake.now += seconds

    def advance(seconds):
        fake.now += seconds

    fake.advance = advance
    monkeypatch.setattr(cb, "_sleep", sleep)
    monkeypatch.setattr(cb, "_monotonic", lambda: fake.now)
    backend = cb.ChdkBackend(logging.getLogger(logger_name))
    backend._registry_factory = lambda: registry
    return backend


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
