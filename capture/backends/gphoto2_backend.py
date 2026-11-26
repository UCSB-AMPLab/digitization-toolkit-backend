"""
python-gphoto2 backend for DSLR cameras (Canon EOS, etc.).

Uses persistent PTP sessions for ~1.3s capture times.

Key design decisions:
  - Sessions opened once and kept alive between captures - no reconnect overhead.
  - capturetarget=Internal RAM, reviewtime=None, autopoweroff=0 applied at init.
  - Port map built lazily from gp.Camera.autodetect(); rebuilt automatically on
    session failure (handles USB re-enumeration after camera power-cycle).
  - Camera index means a body, not a position in autodetect(): the first
    serial read at an index pins that index to that body, and only an
    explicit rescan() may drop or move a pin. See GPhoto2Backend's thread
    safety note and _build_port_map().
  - Flash guard: disables camera flash (flashmode=Off) at session open and before
    every capture. Flash (UV/visible) causes photochemical degradation of archival
    paper and ink - use external continuous lighting instead.
  - Focus mode detection: warns if lens is not in MF (AF causes ~12s PTP hangs).
  - Bounded capture: trigger_capture() then a stepped wait_for_event() loop
    instead of one blocking capture() call. The deadline scales with the
    configured exposure and is checked between libgphoto2 calls, each of which
    is bounded by libgphoto2's own USB timeouts. A capture that passes the
    deadline closes the session, so the next call re-opens it.

Tested with: Canon EOS 1500D x 2, USB 2.0, Raspberry Pi.

ISO, shutter speed, aperture and image format are applied from CameraConfig
before each capture, through the `iso`, `shutterspeed`, `aperture` and
`imageformat` PTP widgets (_PTPSession.apply_dslr_config).

The capture format chosen for a body is stored in the camera registry under
the body's hardware id (the entry's "image_format", as on CHDK). The settings
route sets it on the camera and stores it, and succeeds only when both hold. A
session reads the stored format once, at open, and sets it on the camera
unless the camera already holds it. A capture resolves the format under the
camera lock, for the body its session holds: the one its configuration names,
else the stored one; a named format is written only while a stored one is held
(GPhoto2Backend.capture_image); the format is written only when it differs from the one
the session last set, and a capture whose file is of the other type clears
that memory, so the next capture writes it again. When nothing is stored for
a body whose identity is known, a format the camera holds at open is adopted
(see GPhoto2Backend._adopt_format).

Two connected bodies that answer one serial are not told apart before both
have been read, so the first of them to open may be given the format stored
under that serial. Canon bodies have distinct serials.

Version: 0.3.0
"""

import contextlib
import contextvars
import copy
import fcntl
import functools
import logging
import json
import os
import re
import threading
import time
from collections import deque
from pathlib import Path

try:
    import rawpy
    _RAWPY_AVAILABLE = True
except ImportError:
    rawpy = None  # type: ignore[assignment]
    _RAWPY_AVAILABLE = False
try:
    import gphoto2 as gp
    _GP_AVAILABLE = True
except ImportError:
    gp = None  # type: ignore[assignment]
    _GP_AVAILABLE = False

from .base import (
    IMAGE_FORMATS, CameraBackend, normalise_camera_model, normalise_image_format,
    preview_request, in_preview_request,
)
# Re-exported below for the callers that import it from here (app/api/cameras.py,
# tests/unit/test_test_capture_route.py). The class itself is shared, so a CHDK
# timeout and a DSLR timeout are one class and take the same route out.
from .errors import CameraNotConnectedError, CaptureTimeoutError, PreviewFrameFailedError
from ..utils import atomic_write
from . import usb_topology


def _write_raw_preview(raw_path: Path, preview_path: Path, logger) -> bool:
    """
    Extract the embedded preview JPEG from a raw file and write it to preview_path.

    rawpy's extract_thumb() returns either an already-encoded JPEG
    (ThumbFormat.JPEG, .data is bytes) or a raw bitmap (ThumbFormat.BITMAP,
    .data is an HxWx3 uint8 array) that must be encoded via Pillow. Never
    raises - by the time this runs the master file has already been saved
    and deleted from the camera, so a preview failure must not abort capture.
    """
    if not _RAWPY_AVAILABLE:
        logger.warning(
            f"[gphoto2] rawpy is not installed; no preview written for {raw_path.name}"
        )
        return False

    try:
        with rawpy.imread(str(raw_path)) as raw:
            thumb = raw.extract_thumb()

        if thumb.format == rawpy.ThumbFormat.JPEG:
            atomic_write(preview_path, lambda tmp: Path(tmp).write_bytes(thumb.data))
        elif thumb.format == rawpy.ThumbFormat.BITMAP:
            from PIL import Image

            atomic_write(
                preview_path,
                lambda tmp: Image.fromarray(thumb.data).save(
                    tmp, format="JPEG", quality=90
                ),
            )
        else:
            logger.warning(
                f"[gphoto2] unsupported thumbnail format {thumb.format!r} for "
                f"{raw_path.name}; no preview written"
            )
            return False

        logger.info(f"[gphoto2] embedded JPEG saved to {preview_path.name}")
        return True
    except Exception as exc:
        logger.warning(
            f"[gphoto2] failed to extract embedded JPEG from {raw_path.name}: {exc}"
        )
        return False


# Capture format (base.IMAGE_FORMATS, where the rule is stated) -> the PTP
# imageformat widget value. RAW+JPEG is the camera's "RAW": one .cr2, whose
# embedded JPEG is extracted as the preview. "RAW + L" is never set, because
# the camera then writes two files and capture collects one. The old name RAW
# reaches this map through normalise_image_format.
_IMAGE_FORMAT_MAP = {
    "JPEG":     "L",
    "RAW+JPEG": "RAW",
}
# The widget value for a format the map does not hold.
_IMAGE_FORMAT_DEFAULT = "L"
# The camera's widget value -> the format reported for it. A camera left in
# "RAW + L" (set on the body, or by an earlier build) is reported as RAW+JPEG,
# the format that keeps a raw master.
_IMAGE_FORMAT_REVERSE_MAP = {
    "L":       "JPEG",
    "RAW":     "RAW+JPEG",
    "RAW + L": "RAW+JPEG",
}


def _ptp_image_format(image_format):
    """The imageformat widget value for a capture format, old name included."""
    return _IMAGE_FORMAT_MAP.get(
        normalise_image_format(image_format), _IMAGE_FORMAT_DEFAULT
    )


def _set_imageformat(session, widget) -> bool:
    """Set the imageformat widget on `session` and record what it now holds.

    session.imageformat_set is the widget value this session last set
    successfully, or None when the camera's value is not known to be one
    Captua set: before the first set, and after a set that failed.
    """
    took = session._set_config("imageformat", widget)
    session.imageformat_set = widget if took else None
    return took


def _forget_imageformat_if_changed(session, held) -> None:
    """Drop the session's record of the format it set when a read shows another.

    A body whose format was changed on its own menu then has the stored
    format set again by the next capture.
    """
    known = getattr(session, "imageformat_set", None)
    if known is not None and held != known:
        session.imageformat_set = None


def _hardware_id(model: str, serial: str) -> str:
    """The registry's hardware id for a gphoto2 body: "{model slug}_{serial}"."""
    return f"{re.sub(r'[^a-z0-9]', '', model.lower())}_{serial}"


def _write_system_log(*, level: str, action: str, subject: str, detail: str) -> None:
    """Write one row to the system log in a session of its own; never raises.

    Function-local imports: app.core.db reaches the app configuration, and a
    module-level import from this module would be circular through
    capture.service.
    """
    try:
        from app.core import audit
        from app.core.db import SessionLocal

        db = SessionLocal()
        try:
            audit.log_event(
                db, level=level, category="capture", action=action,
                subject=subject, detail=detail,
            )
        finally:
            db.close()
    except Exception:
        pass


def _open_camera_registry():
    """Open the camera registry at its default path.

    Function-local import: capture.camera_registry reaches capture.service,
    which builds this backend, so a module-level import would be circular.
    """
    from capture.camera_registry import CameraRegistry

    return CameraRegistry()

# Capture retry defaults match the exposure-sized busy polling policy.
_DEFAULT_SETTLE = 3.0
_DEFAULT_RETRY = 3

# Capture deadline: how long the camera has to hand over a file after the
# shutter has been triggered, before the capture is abandoned and the session
# closed. This is the part of the deadline that does not depend on the
# exposure; the configured exposure is added on top (see _capture_deadline_s).
_CAPTURE_TIMEOUT_BASE_S = 15.0
# How long a single wait_for_event() call blocks, in milliseconds. The deadline
# can only be checked between calls, so this is the polling granularity.
_EVENT_STEP_MS = 500
# A bulb shutterspeed carries no duration, and a shutterspeed that comes back
# unreadable (a None from a failed config read) or unparseable ("auto", empty,
# or garbage) gives no information at all - neither case is safe to treat as
# a short exposure. Treat both as this many seconds of exposure when sizing
# the deadline.
_UNKNOWN_EXPOSURE_S = 30.0
# A Refocus test picture uses AF and this shorter deadline after an accepted
# trigger, whatever the exposure. A busy response gets no retry on this path.
_FOCUS_CHECK_DEADLINE_S = 20.0

# Where the index -> body bindings are kept between runs, next to cameras.json
# in the projects root. Owned by this backend alone; the picamera2 path has no
# equivalent because a CSI camera's index is its physical connector.
_BINDINGS_FILENAME = "camera-bindings.json"
_BINDINGS_VERSION = 1

# How many camera indices are sides of the rig. Indices below this are the
# operator's left and right - a dual capture asks for exactly 0 and 1 (see
# app/api/cameras.py, which refuses unless both are connected). Indices at or
# above it are parking: somewhere a body that is not one of the two sides can
# sit, visible and usable, until an explicit rescan gives it a side.
_SIDE_COUNT = 2

# is_camera_connected() skips the USB bus scan while the published port map is
# current (every published port's usbfs node still exists), but still scans
# at least this often, so a body plugged in at an index nobody is asking
# about is noticed without a rescan or a restart.
_ROUTINE_SCAN_MIN_S = 10.0

# The clock the routine-scan interval is measured on; a module-level name so
# tests can drive it.
_monotonic = time.monotonic

# A failed live-preview frame is tried once more, after this pause with the
# camera lock released. The pause lets a capture already waiting on the lock
# take it first; a frame normally comes back in ~30 ms, so this is a few
# frames' worth of time, not a wait the operator sees.
_PREVIEW_RETRY_DELAY_S = 0.1

# Two failed preview polls belong to the same run of failures only when the
# second comes within this many seconds of the first. The capture screen polls
# every 2 s and the dashboard every 2.5 s, so a dead session polled by either
# fails well inside it; a poller as slow as once per _ROUTINE_SCAN_MIN_S still
# keeps its run together, with the rest of the bound as room for a poll
# delayed behind a capture on the lock. A failure after a longer silence
# starts a new run, so an old failure cannot close a session at once.
_PREVIEW_FAILURE_GAP_S = 2 * _ROUTINE_SCAN_MIN_S

_USB_PORT_RE = re.compile(r"usb:([0-9]{3}),([0-9]{3})")

# Where the USB topology path of a port is looked up; a module-level name so
# tests can point it at a directory of their own.
_SYSFS_USB_DEVICES = usb_topology.SYSFS_USB_DEVICES

# The role a bindings pin suggests to the operator in the device list: index 0
# shoots the left page and index 1 the right. Nothing places, gates or files a
# capture by it.
_ROLE_GUESS_BY_PIN_INDEX = {0: "left", 1: "right"}


def _port_node_exists(port) -> bool:
    """Does this libgphoto2 port's usbfs device node exist?

    A port `usb:BBB,DDD` is the device node /dev/bus/usb/BBB/DDD. Unplugging,
    power-cycling or re-enumerating a body removes that node (a re-enumerated
    body gets a new DDD), so a port whose node exists still names a device on
    the bus. False for any port not in that form and wherever the node cannot
    be seen (no usbfs, as on macOS): the caller then scans the bus. No
    libgphoto2 call is made.
    """
    match = _USB_PORT_RE.fullmatch(port) if isinstance(port, str) else None
    if match is None:
        return False
    return os.path.exists(f"/dev/bus/usb/{match.group(1)}/{match.group(2)}")


class CameraIdentityError(RuntimeError):
    """The body answering at a camera index is not the body pinned to it.

    Raised instead of handing back a session on the wrong body: an index is
    the operator's left/right, so shooting the left page on the right camera
    is a silent data error, not a recoverable one. It is a RuntimeError, so
    it travels the existing failure paths (a 404 from the preview route, a
    success:false from the capture route) with no API change.
    """


def _parse_exposure_seconds(shutterspeed) -> float:
    """Convert a Canon EOS shutterspeed widget value to seconds.

    Canon bodies report shutterspeed as "auto", "bulb", whole seconds ("30"),
    decimals ("20.3", "0.8") or fractions ("1/125"). Returns
    _UNKNOWN_EXPOSURE_S - never None - when there is no usable number: "auto",
    "bulb", an empty value, an unreadable widget (a None from a failed config
    read), or anything unparseable. The caller must size the deadline as if
    the exposure could be long, since "unknown" is not evidence that it is
    short.
    """
    if shutterspeed is None:
        return _UNKNOWN_EXPOSURE_S
    text = str(shutterspeed).strip()
    if not text or text.lower() == "auto":
        return _UNKNOWN_EXPOSURE_S
    if text.lower() == "bulb":
        return _UNKNOWN_EXPOSURE_S
    try:
        if "/" in text:
            numerator, _, denominator = text.partition("/")
            return float(numerator) / float(denominator)
        return float(text)
    except (ValueError, ZeroDivisionError):
        return _UNKNOWN_EXPOSURE_S


def _is_busy_trigger_error(exc) -> bool:
    """True for the transient I/O-busy errors trigger_capture() can raise."""
    err = str(exc).lower()
    return "-110" in err or "i/o in progress" in err or "busy" in err


def _capture_deadline_s(exposure: float) -> float:
    """Deadline in seconds for one capture at the given exposure.

    The exposure is counted twice: once for the shutter being open, once for
    the post-exposure processing libgphoto2 budgets on top of it - it allows
    30 s of noise reduction after a 30 s exposure.
    """
    return _CAPTURE_TIMEOUT_BASE_S + 2 * exposure


class _InitGate:
    """FIFO admission prevents an opener's retry overtaking queued bodies."""

    def __init__(self):
        self._condition = threading.Condition()
        self._waiters = deque()
        self._active = False
        self._holder = None

    def acquire(self, blocking=True, timeout=-1):
        return self._acquire(blocking, timeout, None)[0]

    def acquire_for_port(self, port, timeout):
        return self._acquire(True, timeout, (_OPEN_HOLDER_LABELS.get().get(port), port))

    def _acquire(self, blocking, timeout, port):
        if not blocking and timeout != -1:
            raise ValueError("a nonblocking acquire cannot have a timeout")
        deadline = None if timeout < 0 else _init_wait_clock() + timeout
        ticket = object()
        with self._condition:
            if not blocking and (self._active or self._waiters):
                return False, self._holder
            self._waiters.append(ticket)
            try:
                while self._active or self._waiters[0] is not ticket:
                    remaining = None if deadline is None else deadline - _init_wait_clock()
                    if remaining is not None and remaining <= 0:
                        self._waiters.remove(ticket)
                        self._condition.notify_all()
                        return False, self._holder
                    self._condition.wait(remaining)
                self._waiters.popleft()
                self._active = True
                self._holder = port
                return True, None
            except BaseException:
                if ticket in self._waiters:
                    self._waiters.remove(ticket)
                self._condition.notify_all()
                raise

    def release(self):
        with self._condition:
            if not self._active:
                raise RuntimeError("release of an unlocked init gate")
            self._active = False
            self._holder = None
            self._condition.notify_all()

    def locked(self):
        with self._condition:
            return self._active

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc_info):
        self.release()


# Back-off metadata never waits for the gate; native init acquires no backend locks.
# A native init that never returns keeps the gate until process exit;
# unplugging releases it only if the native call then returns.
_init_wait_clock = time.monotonic
_INIT_LOCK = _InitGate()
_INIT_BACKOFF_LOCK = threading.Lock()
_init_backoff = {}
_INIT_BACKOFF_S = 20.0
_INIT_SLOW_S = 1.0
_RESET_LOCK = threading.Lock()
_WAIT_LOG = logging.getLogger(__name__)
_OPEN_POLICY = contextvars.ContextVar("gphoto2_open_policy", default=(None, True))
_OPEN_LABELS = contextvars.ContextVar("gphoto2_open_labels", default={})
_OPEN_HOLDER_LABELS = contextvars.ContextVar("gphoto2_holder_labels", default={})
_RESET_CALL = contextvars.ContextVar("gphoto2_reset_call", default=None)
_recent_resets = {}
_reset_attempts = {}
_RESET_LIMIT = 2
_INIT_LOCK_WAIT_S = 5.0
_WAIT_INFO_THRESHOLD_S = 0.250
_CAMERA_LOCK_INFO_THRESHOLD_S = 10.0
_RESET_INTERVAL_S = 20.0
_STARTUP_INFO_THRESHOLD_S = 1.0
_OPEN_DRAIN_S = 5.0
_OPEN_DRAIN_EVENTS = 1000
_usb_open, _usb_read, _usb_close = os.open, os.read, os.close
_usb_ioctl = fcntl.ioctl
_usb_sleep = time.sleep


@contextlib.contextmanager
def _wait_log(port, reason):
    start = time.monotonic()
    outcome = "ok"
    details = {}
    try:
        yield details
    except BaseException as exc:
        outcome = "failed"
        details.setdefault("error", repr(str(exc)))
        raise
    finally:
        elapsed = time.monotonic() - start
        if reason == "startup events":
            report = (outcome == "failed" or details.get("files", 0) > 0
                      or elapsed > _STARTUP_INFO_THRESHOLD_S)
        elif reason == "camera lock":
            report = outcome == "failed" or elapsed > _CAMERA_LOCK_INFO_THRESHOLD_S
        elif reason == "camera init":
            report = outcome == "failed" or details.get("held_s", elapsed) > _INIT_SLOW_S
        elif (reason in {"session close", "capture download/save", "captured file delete"}
              or reason.startswith(("identification ", "capture events", "trigger"))):
            report = outcome == "failed"
        else:
            report = (reason.startswith("USB ") or reason == "busy capture events"
                      or elapsed >= _WAIT_INFO_THRESHOLD_S)
        deferred = details.pop("_defer", None)
        if report:
            emit = functools.partial(_WAIT_LOG.info,
                "[gphoto2] %s: wait reason=%s duration=%.3fs outcome=%s%s%s",
                _journal_camera(port), reason, elapsed, outcome,
                "".join(f" {key}={value}" for key, value in details.items()),
                f" ({port})" if str(port).startswith("usb:") else "",
            )
            if deferred is None:
                emit()
            else:
                deferred.append(emit)


class _CameraLock:
    """A per-index lock reports the time a caller waits for that camera."""

    def __init__(self, index):
        self._lock = threading.Lock()
        self._index = index

    def acquire(self, blocking=True, timeout=-1):
        with _wait_log(f"camera {self._index}", "camera lock") as detail:
            acquired = self._lock.acquire(blocking, timeout)
            detail["acquired"] = acquired
            return acquired

    def release(self):
        self._lock.release()

    def locked(self):
        return self._lock.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc_info):
        self.release()


def _one_reset_call(method):
    @functools.wraps(method)
    def run(*args, **kwargs):
        if _RESET_CALL.get() is not None:
            return method(*args, **kwargs)
        token = _RESET_CALL.set(set())
        try:
            return method(*args, **kwargs)
        finally:
            _RESET_CALL.reset(token)
    return run


def _usb_attachment(port):
    """Socket, address, sysfs lifetime, vendor and product; None without sysfs."""
    name = usb_topology.from_gphoto2_port(port, sysfs_root=_SYSFS_USB_DEVICES)
    if name is None:
        return None
    base = Path(_SYSFS_USB_DEVICES) / name
    try:
        inode = base.stat().st_ino
        dev = int((base / "devnum").read_text())
        vendor = int((base / "idVendor").read_text(), 16)
        product = int((base / "idProduct").read_text(), 16)
        if base.stat().st_ino != inode or dev != int(port.rsplit(",", 1)[1]):
            return None
        return name, dev, inode, vendor, product
    except (OSError, ValueError):
        return None


def _port_at_socket(socket):
    base = Path(_SYSFS_USB_DEVICES) / socket
    try:
        bus = int((base / "busnum").read_text())
        dev = int((base / "devnum").read_text())
        return f"usb:{bus:03d},{dev:03d}"
    except (OSError, ValueError):
        return None


class _PortMoved(RuntimeError):
    def __init__(self, new_port, source_port=None):
        self.new_port = new_port
        label = _operator_camera(source_port or new_port)
        super().__init__(
            f"{label}: the camera reconnected. Wait and try again; "
            "if it repeats, switch the camera off and on."
        )


class _PortClaimedError(RuntimeError):
    """An in-flight claim prevents identification at this index."""

    def __init__(self, port, holder, label):
        self.port, self.holder = port, holder
        super().__init__(f"{label}: the camera is already in use. Wait and try again.")


class CaptureAttributionError(RuntimeError):
    """A startup object cannot safely become this capture's page."""


def _new_camera(model, port):
    abilities = gp.CameraAbilitiesList()
    abilities.load()
    cam = gp.Camera()
    cam.set_abilities(abilities[abilities.lookup_model(model)])
    ports = gp.PortInfoList()
    ports.load()
    cam.set_port_info(ports[ports.lookup_path(port)])
    return cam


def _operator_camera(port):
    return _OPEN_LABELS.get().get(port, "This camera")


def _journal_camera(port):
    if str(port).startswith("usb:"):
        return _operator_camera(port)
    return port


def _backoff_key(port, attachment):
    return attachment[0] if attachment is not None else port


def _prune_init_backoff(now):
    for key, (_, until, _) in list(_init_backoff.items()):
        if now >= until:
            del _init_backoff[key]


class _PreviewInitPaused(RuntimeError):
    """Human labels are rendered after the init gate has been released."""

    def __init__(self, label, remaining, error):
        super().__init__("Preview startup is paused")
        self.label, self.remaining, self.error = label, remaining, error

    def __str__(self):
        label = str(self.label) if self.label is not None else None
        return (
            f"{label or 'This camera'}: preview startup is paused for {self.remaining:.1f}s. "
            f"Wait and try again, or switch {label or 'this camera'} off and on."
        )


def _check_init_backoff(port, attachment):
    """Expiry or a changed USB attachment admits another native attempt."""
    now = _monotonic()
    key = _backoff_key(port, attachment)
    with _INIT_BACKOFF_LOCK:
        _prune_init_backoff(now)
        entry = _init_backoff.get(key)
        if entry is not None and entry[0] != attachment:
            del _init_backoff[key]
            entry = None
    if entry is not None:
        _, until, error = entry
        raise _PreviewInitPaused(_OPEN_LABELS.get().get(port), until - now, error)


def _record_init_failure(port, attachment, exc, held_s):
    if held_s <= _INIT_SLOW_S:
        return
    now = _monotonic()
    error = f"{type(exc).__name__}: {exc}"
    with _INIT_BACKOFF_LOCK:
        _prune_init_backoff(now)
        # Text retains the error without retaining its native session or traceback.
        _init_backoff[_backoff_key(port, attachment)] = (
            attachment, now + _INIT_BACKOFF_S, error
        )
    return True


def _clear_init_failure(port, attachment):
    key = _backoff_key(port, attachment)
    with _INIT_BACKOFF_LOCK:
        entry = _init_backoff.get(key)
        if entry is not None and entry[0] == attachment:
            del _init_backoff[key]


def _init_camera(model, port, expected, may_reset):
    """One reset at most, bound to a descriptor opened before the failing init.

    The descriptor pins the kernel device across address reuse. Sysfs lifetime
    and socket checks reject a replacement before opening that descriptor.
    A reset follows only a transport init error, never a configuration error.
    """
    may_reset = may_reset and not in_preview_request()
    attachment = _usb_attachment(port)
    # Back-off throttles previews only; reset-capable opens follow the reset cooldown alone.
    if not may_reset:
        _check_init_backoff(port, attachment)
    fd = None
    if may_reset and expected is not None and expected[3] == 0x04a9:
        match = _USB_PORT_RE.fullmatch(port)
        if match and _usb_attachment(port) == expected:
            try:
                fd = _usb_open(f"/dev/bus/usb/{match[1]}/{match[2]}", os.O_RDWR)
                descriptor = _usb_read(fd, 18)
                valid = (len(descriptor) == 18
                         and int.from_bytes(descriptor[8:10], "little") == expected[3]
                         and int.from_bytes(descriptor[10:12], "little") == expected[4]
                         and _usb_attachment(port) == expected)
                if not valid:
                    _usb_close(fd)
                    fd = None
            except OSError:
                if fd is not None:
                    _usb_close(fd)
                fd = None
    try:
        try:
            cam = _init_once(model, port, attachment, may_reset=may_reset)
            if expected is not None:
                with _RESET_LOCK:
                    _reset_attempts.pop(expected[0], None)
            return cam
        except gp.GPhoto2Error as exc:
            code = getattr(exc, "code", None)
            if code is None:
                match = re.match(r"\s*\[(-\d+)\]", str(exc))
                code = int(match[1]) if match else None
            if code not in (-7, -10):
                raise
            if fd is None:
                _WAIT_LOG.info(
                    "[gphoto2] %s: USB reset unavailable; no validated descriptor for init error %s",
                    port, code,
                )
                raise
            socket = expected[0]
            now = _monotonic()
            in_call = _RESET_CALL.get()
            # Only recent socket attempts remain in the rate table.
            with _RESET_LOCK:
                for key, at in list(_recent_resets.items()):
                    if now - at >= _RESET_INTERVAL_S:
                        del _recent_resets[key]
                exhausted = _reset_attempts.get(socket, 0) >= _RESET_LIMIT
                refused = (exhausted or socket in _recent_resets
                           or (in_call is not None and socket in in_call))
                if not refused:
                    _recent_resets[socket] = now
                    _reset_attempts[socket] = _reset_attempts.get(socket, 0) + 1
                    if in_call is not None:
                        in_call.add(socket)
            if refused:
                _WAIT_LOG.info(
                    "[gphoto2] %s: USB reset suppressed reason=%s; switch the camera off and on, then try again",
                    port, "two-attempt limit" if exhausted else "call/cooldown",
                )
                raise
            _WAIT_LOG.info("[gphoto2] %s: USB reset reason=init error %s socket=%s", port, code, socket)
            try:
                with _wait_log(port, "USB reset ioctl"):
                    _usb_ioctl(fd, 0x5514, 0)
            except OSError as reset_error:
                _WAIT_LOG.warning("[gphoto2] %s: USB reset refused: %s", port, reset_error)
                raise exc from reset_error
            with _wait_log(port, "USB reset settle"):
                _usb_sleep(2.0)
            retry_error = exc
            retried = False
            new_port = None
            with _wait_log(port, "USB socket reappearance"):
                for step in range(17):
                    current = _port_at_socket(socket)
                    if current and current != port:
                        _WAIT_LOG.info("[gphoto2] %s: USB reset new address=%s socket=%s", port, current, socket)
                        new_port = current
                        break
                    if current == port and not retried:
                        retried = True
                        try:
                            cam = _init_once(model, port, attachment, may_reset=True)
                        except gp.GPhoto2Error as err:
                            retry_error = err
                        else:
                            with _RESET_LOCK:
                                _reset_attempts.pop(socket, None)
                            _WAIT_LOG.info("[gphoto2] %s: USB reset recovered body", port)
                            return cam
                    if step < 16:
                        _usb_sleep(0.5)
            if new_port is not None:
                raise _PortMoved(new_port, port)
            raise retry_error
    finally:
        if fd is not None:
            try:
                _usb_close(fd)
            except OSError as exc:
                _WAIT_LOG.warning("[gphoto2] %s: USB descriptor close failed: %s", port, exc)


def _init_once(model, port, attachment, may_reset=False):
    if not may_reset:
        _check_init_backoff(port, attachment)
    pending = []
    with _wait_log(port, "init lock") as detail:
        acquired, holder = _INIT_LOCK.acquire_for_port(port, _INIT_LOCK_WAIT_S)
        detail["acquired"] = acquired
        if not acquired:
            if holder is None or holder[0] is None:
                raise RuntimeError("The cameras are busy starting. Wait and try again.")
            label, address = holder
            detail["holder"] = f"{label} ({address})"
            waiting = _OPEN_LABELS.get().get(port, "This camera")
            if waiting == "the other camera":
                waiting = "This camera"
            raise RuntimeError(
                f"{waiting} is waiting for {label} to finish starting. "
                f"Switch {label} off and on, then wait and try again; "
                "if it repeats, ask the administrator."
            )
        detail["_defer"] = pending
    started = _monotonic()
    paused = False
    failure = None
    initialized = handed_back = False
    try:
        # A queued preview observes a slow failure recorded by the preceding owner.
        if not may_reset:
            _check_init_backoff(port, attachment)
        try:
            with _wait_log(port, "camera init") as detail:
                detail["_defer"] = pending
                try:
                    cam = _new_camera(model, port)
                    cam.init()
                    initialized = True
                finally:
                    held_s = _monotonic() - started
                    detail["held_s"] = held_s
        except BaseException as exc:
            if not initialized:
                paused = _record_init_failure(port, attachment, exc, held_s)
            failure = exc
            raise
        _clear_init_failure(port, attachment)
        handed_back = True
        return cam
    finally:
        _INIT_LOCK.release()
        # Diagnostic role reads and journal writes never hold the init gate.
        try:
            for emit in pending:
                emit()
            if paused:
                _WAIT_LOG.info(
                    "[gphoto2] %s: preview init back-off held=%.3fs interval=%gs error=%r (%s)",
                    _journal_camera(port), held_s, _INIT_BACKOFF_S, str(failure), port,
                )
        except BaseException:
            handed_back = False
            raise
        finally:
            if initialized and not handed_back:
                try:
                    cam.exit()
                except BaseException:
                    pass


def _close_failed_capture(method):
    @functools.wraps(method)
    def run(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except BaseException:
            try:
                self.close()
            except BaseException:
                pass
            raise
    return run


class _PTPSession:
    """
    An open PTP/USB session to one DSLR camera.

    Created once per camera; held alive between captures. Call close() when done.
    Not thread-safe - callers must hold the per-camera lock.
    """

    def __init__(self, port: str, model: str, logger):
        self.port = port
        self.model = model
        self._logger = logger
        self._label = _operator_camera(port)
        self._cam = None
        # Focus mode as read at session open; named in capture timeout messages.
        self._focus_mode = None
        # Body serial as read at session open; "" when the body does not
        # answer one. This is what pins the session's index to a body.
        self.serial = ""
        # The body's own model name as read at session open, e.g. "Canon EOS
        # Rebel T7" where libgphoto2's abilities name (self.model) is "Canon
        # EOS 1500D"; None when the body does not answer one. self.model stays
        # the abilities name: lookup_model and session matching depend on it.
        self.body_model = None
        # The imageformat widget value as read at session open, or None when
        # the body does not answer one.
        self.opened_imageformat = None
        # The imageformat widget value this session last set successfully, or
        # None (see _set_imageformat).
        self.imageformat_set = None
        # The capture format stored for this session's body, as read at open
        # and kept equal to every store made through this session; None when
        # nothing is stored, the registry could not be read, or the body's
        # identity is unknown.
        self.stored_format = None
        # Only files announced before this session sends any trigger live here.
        # The startup event cap bounds this set for the entire session.
        self._unsolicited = set()
        self._open()

    def _open(self):
        expected, may_reset = _OPEN_POLICY.get()
        self._cam = _init_camera(self.model, self.port, expected, may_reset)
        # Every exit after native init releases this session's USB claim.
        try:
            self._logger.info(f"[gphoto2] Opened session: {self.model} on {self.port}")
            self._drain_startup()
            self._apply_speed_preset()
            self._disable_autopoweroff()
            self._warn_if_af()
            self._enforce_flash_off()
            # Read once, here: the identity of the body on this port is fixed
            # for the life of the session, and every later use of it happens
            # without a second PTP round trip. The serial and the body's own
            # model come off one fetch of the config tree.
            try:
                with _wait_log(self.port, "session identity read"):
                    cfg = self._cam.get_config()
            except gp.GPhoto2Error:
                cfg = None
            serial = self._child_value(cfg, "serialnumber")
            self.serial = str(serial).strip() if serial is not None else ""
            self.body_model = self._read_body_model(cfg)
            self.opened_imageformat = self._child_value(cfg, "imageformat")
        except BaseException:
            try:
                self.close()
            except BaseException:
                pass
            raise

    def _drain_startup(self, step_ms=100):
        """Read startup events before configuration without fetching camera files.

        A timeout means the queue answered empty, not that an exposure finished.
        The time and event caps stop noisy bodies before a shutter is requested.
        Native event processing can outlast the Python budget.
        """
        start = time.monotonic()
        count = 0
        with _wait_log(self.port, "startup events") as detail:
            try:
                while count < _OPEN_DRAIN_EVENTS and time.monotonic() - start < _OPEN_DRAIN_S:
                    event, data = self._cam.wait_for_event(step_ms)
                    if event == gp.GP_EVENT_TIMEOUT:
                        return
                    count += 1
                    if event == gp.GP_EVENT_FILE_ADDED:
                        key = (data.folder, data.name)
                        if key not in self._unsolicited:
                            self._unsolicited.add(key)
                            _WAIT_LOG.warning(
                                "[gphoto2] %s: startup file=%s/%s; not fetched or deleted; excluded from this session's captures (%s)",
                                _journal_camera(self.port), *key, self.port,
                            )
                raise CaptureAttributionError(
                    f"{_operator_camera(self.port)}: startup events did not clear; no picture was requested. "
                    "Switch the camera off and on, then try again."
                )
            except gp.GPhoto2Error as exc:
                detail["error"] = repr(str(exc))
                raise CaptureAttributionError(
                    f"{_operator_camera(self.port)}: could not read startup events; no picture was requested. "
                    "Switch the camera off and on, then try again."
                ) from exc
            finally:
                detail.update(count=count, files=len(self._unsolicited))

    def _capture_file(self, data):
        if (data.folder, data.name) in self._unsolicited:
            _WAIT_LOG.warning(
                "[gphoto2] %s: refused startup file=%s/%s (%s)",
                getattr(self, "_label", _operator_camera(self.port)), data.folder, data.name, self.port,
            )
            raise CaptureAttributionError(
                f"{getattr(self, '_label', _operator_camera(self.port))}: a file was announced at startup; "
                "nothing saved or deleted. Take the page again; if it repeats, switch the camera off and on."
            )
        return data

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _set_config(self, key: str, value) -> bool:
        try:
            with _wait_log(self.port, f"set config {key}"):
                cfg = self._cam.get_config()
                widget = cfg.get_child_by_name(key)
                widget.set_value(value)
                self._cam.set_config(cfg)
            return True
        except (gp.GPhoto2Error, TypeError, ValueError):
            return False

    def _get_config(self, key: str):
        try:
            with _wait_log(self.port, f"read config {key}"):
                cfg = self._cam.get_config()
                return cfg.get_child_by_name(key).get_value()
        except gp.GPhoto2Error:
            return None

    @staticmethod
    def _child_value(cfg, key: str):
        """`key`'s value in an already fetched config tree, as _get_config reads it."""
        if cfg is None:
            return None
        try:
            return cfg.get_child_by_name(key).get_value()
        except gp.GPhoto2Error:
            return None

    # ------------------------------------------------------------------
    # Session init helpers
    # ------------------------------------------------------------------

    def _apply_speed_preset(self):
        """Set capturetarget=RAM and reviewtime=None once at session start."""
        self._set_config("capturetarget", "Internal RAM")
        self._set_config("reviewtime", "None")

    def apply_dslr_config(self, camera_config, image_format=None) -> None:
        """Apply DSLR-specific CameraConfig fields to the camera via PTP.

        Called before each capture so settings reflect the current config.
        Fields that are None are left at their current camera value.
        image_format is the format applied when the configuration names none
        (the body's stored format); a format the configuration names wins.
        Flash is always enforced off here as an archival safety guard.
        """
        # -- Flash guard: must be first, before shutter opens ----------
        self._enforce_flash_off()

        # A format is written only when it differs from the one this session
        # last set; with no format in the configuration the camera's value is
        # left as it is.
        fmt = getattr(camera_config, "image_format", None)
        if fmt is None:
            fmt = image_format
        if fmt is not None:
            widget = _ptp_image_format(fmt)
            if widget != getattr(self, "imageformat_set", None):
                _set_imageformat(self, widget)

        iso = getattr(camera_config, "iso", None)
        if iso is not None:
            self._set_config("iso", str(iso))

        shutter = getattr(camera_config, "shutter_speed", None)
        if shutter is not None:
            self._set_config("shutterspeed", shutter)

        aperture = getattr(camera_config, "aperture", None)
        if aperture is not None:
            self._set_config("aperture", aperture)

    def _disable_autopoweroff(self):
        """Disable camera auto-power-off to prevent sleep mid-session."""
        # Canon EOS PTP widget expects a string, not an integer.
        # Try "0" first (some bodies), fall back to "Off" (others).
        if not self._set_config("autopoweroff", "0"):
            if not self._set_config("autopoweroff", "Off"):
                self._logger.debug(
                    f"[gphoto2] {self.port}: autopoweroff widget not available on {self.model}"
                )

    def _warn_if_af(self):
        """Log a warning if the lens is not in manual focus mode."""
        focusmode = self._get_config("focusmode")
        self._focus_mode = focusmode
        if focusmode and focusmode not in ("Manual", "MF"):
            self._logger.warning(
                f"[gphoto2] {self.port}: focusmode={focusmode!r} - "
                "AF causes ~12s PTP hangs. Flip lens barrel switch to MF."
            )

    def _read_body_model(self, cfg) -> "str | None":
        """The model name the body reports for itself in `cfg`, or None.

        libgphoto2's "cameramodel" widget is the PTP DeviceInfo Model string,
        which is what the body writes into its own files; "None" is
        libgphoto2's text when DeviceInfo carries no Model. Canon's "model"
        widget is not tried: on EOS it is the numeric ModelID. Any failure
        gives None and never fails the session open.
        """
        try:
            value = self._child_value(cfg, "cameramodel")
        except Exception as exc:
            self._logger.debug(f"[gphoto2] {self.port}: cameramodel not read ({exc})")
            return None
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not value or value == "None":
            return None
        return value

    def _enforce_flash_off(self) -> bool:
        """Actively disable camera flash before every capture.

        Tries to set the flashmode PTP widget to 'Off'.  If the widget is
        read-only or unavailable (some bodies don't expose it), falls back to
        reading the current value and logging a warning if flash is still
        active.  Never blocks the capture - returns False when flash cannot
        be confirmed off so callers can decide.

        Flash (UV/visible) causes photochemical degradation of archival paper
        and ink.  Use stable external continuous lighting instead.
        """
        if self._set_config("flashmode", "Off"):
            self._logger.debug(
                f"[gphoto2] {self.port}: flash disabled (flashmode=Off)"
            )
            return True
        # Widget is read-only or not present - check current value
        flashmode = self._get_config("flashmode")
        if flashmode is None:
            # Camera doesn't support the widget; assume no flash
            return True
        if "off" not in str(flashmode).lower():
            self._logger.warning(
                f"[gphoto2] {self.port}: flashmode={flashmode!r} and cannot be "
                "set to Off automatically. "
                "Flash is harmful to archival materials - "
                "manually disable the flash before capturing."
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _trigger_with_retry(self, retry: int, retry_delay: float, deadline_s: float):
        """Fire the shutter, retrying only once the event queue proves it safe.

        libgphoto2's EOS trigger_capture() full-presses the shutter and can
        then fail on the half- or full-release call that follows, so a
        "busy"/-110 error from trigger_capture() does not prove no exposure
        happened - and that is just as true of the last attempt as of any
        earlier one. A long exposure's file can take far longer to land than
        any short settle delay, so every retriable trigger error - including
        one on the final attempt - is first followed by polling the event
        queue for the same capture deadline the post-trigger wait rests on:
        the recovery poll rests on exactly the same evidence as the timeout
        path, an event queue that stayed empty for the whole deadline. On a
        retriable error this polls wait_for_event() via
        self._poll_for_file(deadline_s) instead of sleeping. If a
        GP_EVENT_FILE_ADDED turns up during that window, the shutter already
        fired and its CameraFilePath is returned instead of re-triggering.
        If the window is empty and attempts remain, the trigger is retried;
        if the window is empty and this was the last attempt, the original
        error is re-raised. retry_delay is accepted for signature stability;
        it plays no part in sizing the wait.

        Returns the CameraFilePath if one was seen while waiting out a retry,
        otherwise None (the normal case - the trigger succeeded outright and
        the caller collects the file the usual way).
        """
        for attempt in range(1, retry + 1):
            try:
                with _wait_log(self.port, "trigger"):
                    self._cam.trigger_capture()
                return None
            except gp.GPhoto2Error as exc:
                if not _is_busy_trigger_error(exc):
                    raise
                self._logger.warning(
                    f"[gphoto2] {self.port}: trigger busy (attempt {attempt}/{retry}): "
                    f"waiting the capture deadline for a file before retrying"
                )
                file_path = self._poll_for_file(deadline_s)
                if file_path is not None:
                    return file_path
                if attempt < retry:
                    continue
                raise


    def _poll_for_file(self, window_s: float):
        """Poll the event queue for window_s seconds; return a file if one lands.

        Used only to make a busy-trigger retry safe: if the exposure already
        happened, the event queue will show GP_EVENT_FILE_ADDED during this
        window even though trigger_capture() itself raised. Returns None if
        the window elapses with no file seen.
        """
        with _wait_log(self.port, "busy capture events"):
            start = time.monotonic()
            while time.monotonic() - start < window_s:
                event_type, event_data = self._cam.wait_for_event(_EVENT_STEP_MS)
                if event_type == gp.GP_EVENT_FILE_ADDED:
                    return self._capture_file(event_data)
            return None


    def _wait_for_file_added(self, deadline_s: float, shutterspeed=None):
        """Poll the camera's event queue until a file lands, or the deadline passes.

        Returns the gp.CameraFilePath (.folder / .name) carried by the
        GP_EVENT_FILE_ADDED event. Every other event - capture complete,
        timeout, unknown - just continues the loop. The deadline is checked
        between wait_for_event() calls, each of which is bounded by
        libgphoto2's own USB timeouts, and only after a call comes back
        without an acceptable file. Paths announced before this session's
        first trigger are excluded. Any other delivered file is accepted even
        after the deadline; the deadline bounds polling, not file attribution.
        The event supplies no token that links it to the accepted trigger.

        Errors raised by wait_for_event() propagate untouched: the shutter has
        already fired by this point, so re-triggering would take a second
        photograph rather than recover the first.
        """
        start = time.monotonic()
        while True:
            event_type, event_data = self._cam.wait_for_event(_EVENT_STEP_MS)
            if event_type == gp.GP_EVENT_FILE_ADDED:
                return self._capture_file(event_data)
            elapsed = time.monotonic() - start
            if elapsed >= deadline_s:
                hint = ""
                mode = self._focus_mode
                if mode and mode not in ("Manual", "MF"):
                    hint = (
                        f" - focusmode={mode!r}: AF hangs capture, "
                        "flip the lens barrel switch to MF"
                    )
                raise CaptureTimeoutError(
                    f"{self.port}: no image after {elapsed:.1f}s "
                    f"(deadline {deadline_s:.1f}s, shutterspeed={shutterspeed!r})"
                    f"{hint}"
                )

    def _focus_check_timeout(self, reason: str) -> CaptureTimeoutError:
        return CaptureTimeoutError(
            f"{self.port}: no picture arrived within the focus-check deadline "
            f"of {_FOCUS_CHECK_DEADLINE_S:g}s ({reason})"
        )

    def _trigger_for_focus_check(self, shutterspeed):
        """Trigger once and collect the file within _FOCUS_CHECK_DEADLINE_S.

        The deadline runs from the trigger call to the file. A busy trigger
        is never retried: the event queue is polled for whatever is left of
        the deadline, since a busy error does not prove the shutter stayed
        shut, and a file seen there is returned. An empty queue at the
        deadline - after a busy trigger or after a clean one - raises
        CaptureTimeoutError, which the caller treats like any capture
        timeout (the session is closed). A non-busy trigger error and an
        error out of the event wait propagate as they come.
        """
        start = time.monotonic()
        try:
            with _wait_log(self.port, "trigger (focus check)"):
                self._cam.trigger_capture()
        except gp.GPhoto2Error as exc:
            if not _is_busy_trigger_error(exc):
                raise
            self._logger.warning(
                f"[gphoto2] {self.port}: trigger busy on a focus check: "
                f"waiting what is left of the {_FOCUS_CHECK_DEADLINE_S:g}s "
                f"deadline for a file, with no retry"
            )
            remaining = _FOCUS_CHECK_DEADLINE_S - (time.monotonic() - start)
            file_path = self._poll_for_file(remaining)
            if file_path is None:
                raise self._focus_check_timeout(f"trigger busy: {exc}") from exc
            return file_path
        remaining = _FOCUS_CHECK_DEADLINE_S - (time.monotonic() - start)
        try:
            with _wait_log(self.port, "capture events (focus check)"):
                return self._wait_for_file_added(remaining, shutterspeed)
        except CaptureTimeoutError as exc:
            # Re-worded without the "flip to MF" hint: a focus check runs on
            # AF by design.
            raise self._focus_check_timeout(
                f"shutterspeed={shutterspeed!r}"
            ) from exc


    @_close_failed_capture
    def capture(
        self,
        outpath: Path,
        retry: int = _DEFAULT_RETRY,
        retry_delay: float = _DEFAULT_SETTLE,
        focus_check: bool = False,
    ) -> tuple[float, Path]:
        """Capture one image to outpath. Returns (elapsed_seconds, actual_path).

        actual_path may differ from outpath when the camera is in RAW mode
        (extension becomes .cr2 instead of .jpg). Callers should use
        actual_path to reference the saved file.

        When imageformat is RAW, the camera produces a .cr2 file. The embedded
        full-resolution JPEG (6000x4000, 11 ms to extract) is saved alongside
        the CR2 as ``{stem}_preview.jpg`` so the existing thumbnail/review
        pipeline has a JPEG to work with.

        The shutter is triggered and the file is then collected from the
        camera's event queue under a deadline sized from the configured
        exposure, so a camera that never delivers an image raises
        CaptureTimeoutError instead of blocking the per-camera lock. Only the
        trigger is retried, on transient I/O-busy errors, and only once the
        event queue has stayed empty for that same capture deadline - a
        retriable error can follow a successful exposure, so a shorter wait
        risks re-triggering (a second photograph) while the first file is
        still on its way. A failure after the shutter has fired is raised
        as it comes.

        focus_check=True replaces that policy for one capture: one trigger
        attempt and _FOCUS_CHECK_DEADLINE_S from the trigger to the file
        (see _trigger_for_focus_check); retry and retry_delay are then
        unused.
        """
        outpath.parent.mkdir(parents=True, exist_ok=True)
        shutterspeed = self._get_config("shutterspeed")

        t0 = time.perf_counter()
        if focus_check:
            file_path = self._trigger_for_focus_check(shutterspeed)
        else:
            deadline_s = _capture_deadline_s(_parse_exposure_seconds(shutterspeed))
            file_path = self._trigger_with_retry(retry, retry_delay, deadline_s)
            if file_path is None:
                with _wait_log(self.port, "capture events"):
                    file_path = self._wait_for_file_added(deadline_s, shutterspeed)

        # The captured filename tells us the actual format (.cr2 vs .jpg)
        is_raw = file_path.name.lower().endswith(".cr2")
        if is_raw:
            # Save CR2 with .cr2 extension regardless of outpath stem
            actual_outpath = outpath.with_suffix(".cr2")
        else:
            actual_outpath = outpath

        with _wait_log(self.port, "capture download/save"):
            camera_file = self._cam.file_get(
                file_path.folder, file_path.name, gp.GP_FILE_TYPE_NORMAL
            )
            atomic_write(actual_outpath, lambda tmp: camera_file.save(tmp))
        with _wait_log(self.port, "captured file delete"):
            self._cam.file_delete(file_path.folder, file_path.name)
        elapsed = time.perf_counter() - t0

        # Extract embedded JPEG from CR2 for thumbnail/review pipeline
        if is_raw:
            # Function-local, as for the app config below: a module-level app
            # import from this module would be circular through capture.service.
            from app.core.paths import preview_sidecar_of

            _write_raw_preview(
                actual_outpath,
                preview_sidecar_of(actual_outpath),
                self._logger,
            )

        return elapsed, actual_outpath


    def get_info(self) -> dict:
        """Return current camera settings as a dict (for logging / future API)."""
        keys = [
            "focusmode", "capturetarget", "reviewtime",
            "iso", "shutterspeed", "aperture", "imageformat", "flashmode",
        ]
        return {k: self._get_config(k) for k in keys}

    def close(self):
        if self._cam is not None:
            cam, self._cam = self._cam, None
            try:
                with _wait_log(self.port, "session close"):
                    cam.exit()
            except Exception:
                pass
            self._logger.info(f"[gphoto2] Closed session: {self.model} on {self.port}")


_EventReader = _PTPSession


class GPhoto2Backend(CameraBackend):
    """
    DSLR camera backend using python-gphoto2 with persistent PTP sessions.

    Activate by setting CAMERA_BACKEND=gphoto2 in the environment / .env.

    Thread safety:
      - One threading.Lock per camera index serialises concurrent capture calls.
      - The port map is protected by a separate map_lock, and so are the two
        pieces of identity state it is built from: the pins (index -> body
        serial, for the life of the process) and the serial cache (usb port
        -> body serial, learned whenever a serial is read on a port and
        pruned of every port an autodetect no longer reports).
      - Lock order is camera lock(s) then map_lock, never the reverse. Two
        camera locks are only ever held together through the non-blocking
        try in the ownership rule (a port may not be opened under one index
        while a cached session under another index still holds it), so that
        pairing cannot deadlock.
      - A serial is published to the cache only after the PTP claim that read
        it has been released: a brief identification read caches after
        cam.exit() returns, and a session that contradicted its pin caches
        after its session has been closed. Anything else would let another
        thread act on an identity while the body was still claimed.
    """

    def __init__(self, logger):
        if not _GP_AVAILABLE:
            raise RuntimeError(
                "The cameras are unavailable: camera support (python-gphoto2) is not installed on this unit. Ask the administrator."
            )
        super().__init__(logger)
        # camera_index -> (model_name, usb_port)
        self._port_map: dict[int, tuple[str, str]] = {}
        # the map last written to the log (None before the first build), so
        # only changes are logged; read and written under _map_lock
        self._logged_port_map: "dict[int, tuple[str, str]] | None" = None
        # camera_index -> body serial, for the life of the process and, via
        # the bindings file, across restarts
        self._pins: dict[int, str] = {}
        # the serials seeded from the bindings file, until the first complete
        # identification settles whether any of those bodies is still here
        self._seeded_serials: set[str] = set()
        # usb port -> body serial, as last read on that port
        self._serial_by_port: dict[str, str] = {}
        # A role belongs to the last identity read on this published port.
        self._roles_by_port: dict[str, tuple[str, str]] = {}
        self._usb_attachments = {}
        # usb port -> (abilities model, serial or "", body's own model) as
        # read by the last session opened on that port. It outlives the
        # session, so a capture's model lookup that lands after the session
        # was closed still names the body as the camera does; camera_model
        # returns it only while the port map and the serial cache still
        # describe the same body. Pruned with the serial cache.
        self._body_model_by_port: dict[str, tuple[str, str, str]] = {}
        # the last autodetect() result, so the map can be rebuilt against new
        # identity knowledge without a second USB enumeration
        self._last_detected: list[tuple[str, str]] = []
        # _monotonic() at the last autodetect(), or None before the first;
        # written by _build_port_map under _map_lock
        self._last_scan_at: "float | None" = None
        # camera_index -> open _PTPSession
        self._sessions: dict[int, _PTPSession] = {}
        # camera_index -> (session, _monotonic() of the first and of the last
        # failed preview poll in the current run of failures on it); read and
        # written under _preview_record_lock (see _note_preview_failure)
        self._preview_failing_since: dict[
            int, tuple[_PTPSession, float, float]
        ] = {}
        # guards _preview_failing_since alone, so a poll that cannot have the
        # camera lock can still record its failure; it may be taken with a
        # camera lock held, and no other lock is taken while it is held
        self._preview_record_lock = threading.Lock()
        # usb port -> the index that currently holds a PTP claim on it. This
        # is the record of what is claimed *now*, which self._sessions is not:
        # a session claims its body before it is stored and is popped before
        # its exit() returns, and a brief read never appears there at all.
        self._claimed_ports: dict[str, int] = {}
        # per-camera lock for capture serialisation
        self._session_locks: dict[int, threading.Lock] = {}
        self._map_lock = threading.Lock()
        # True from a bus scan in is_camera_connected() that raised until the
        # next one there that completes, so a run of failed scans is logged
        # once; read and written under _map_lock
        self._scan_failing = False
        # serialises rescan() so overlapping snapshots publish in order
        self._rescan_lock = threading.Lock()
        # serialises saves of the bindings file, snapshot and write together
        self._save_lock = threading.Lock()
        # Opens the camera registry, where a body's stored capture format
        # lives. It is opened at a session open and for a store, never for a
        # capture or a settings read, which use the session's stored_format.
        # A test replaces it; a factory that returns None means no registry,
        # and a format is then kept on the session only.
        self._registry_factory = _open_camera_registry

        # Writes a row to the system log (_write_system_log); a test replaces it.
        self._system_log = _write_system_log
        self._load_pins()

    # ------------------------------------------------------------------
    # Persistent index -> body bindings
    # ------------------------------------------------------------------

    def _bindings_path(self) -> "Path | None":
        """Where the bindings live, or None when there is nowhere to keep them.

        The import is function-local, exactly as camera_registry does it: this
        module is reached from capture.service, so a module-level import of the
        app config would be circular. When it fails - a test process with no
        app config - persistence is simply off. It is off too while the storage
        override names a root that cannot take writes (writable_projects_dir
        raises StorageUnavailableError), so the save's mkdir never builds the
        projects tree on the SD card.
        """
        try:
            from app.core.config import settings
            from app.core.storage_override import writable_projects_dir

            return writable_projects_dir(settings) / _BINDINGS_FILENAME
        except Exception:
            return None

    def _load_pins(self):
        """Seed the pins from the bindings file, once, at construction.

        Which index is the left-hand camera is the operator's decision, and a
        restart is not a reason to ask them again: without this, a reboot with
        the bodies enumerating the other way round silently swaps the pages.
        Only the pins are restored - the port cache starts empty, because a usb
        devnum from a previous boot means nothing - so both indices begin
        reserved and the bodies land on provisional indices until the first
        identification puts them back where they belong, the same path this
        backend takes after a power-cycle.

        A missing file is the normal first-run case. A file that cannot be read
        or does not hold the expected shape is reported and ignored: bad
        bindings must degrade to a positional map, never to an exception on a
        camera backend that is about to be asked for a preview.
        """
        path = self._bindings_path()
        # No path means the file could not be read at all (no app config, or
        # a storage root that cannot take writes); _save_pins then merges
        # with whatever the file holds by the time it can be written.
        self._bindings_unread = path is None
        if path is None or not path.exists():
            return
        try:
            pins = self._parse_bindings(path)
        except Exception as exc:
            self.logger.warning(
                f"[gphoto2] {path} is unreadable ({exc}); starting with no "
                "camera bindings - the indices will be assigned positionally "
                "until the bodies are read again"
            )
            return
        if not pins:
            return
        self._pins = pins
        self._seeded_serials = set(pins.values())
        self.logger.info(f"[gphoto2] seeded camera bindings from {path}: {pins}")

    @staticmethod
    def _parse_bindings(path) -> dict:
        """The pins a bindings file holds; raises on any file that is not in canonical form."""
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != _BINDINGS_VERSION:
            raise ValueError(
                f"version {data.get('version')!r} is not {_BINDINGS_VERSION}"
            )
        pins = {}
        for key, serial in data["pins"].items():
            # The file is only ever written by _save_pins, in canonical
            # form: decimal index strings and stripped serials. Anything
            # else is a hand edit or corruption, and a key that is not
            # canonical can collapse onto another index ("00" onto 0) or
            # a serial can never match the stripped value a body answers.
            if not isinstance(serial, str) or not serial or serial != serial.strip():
                raise ValueError(f"binding {key!r} has no canonical serial")
            index = int(key)
            if index < 0 or str(index) != key:
                raise ValueError(f"binding {key!r} is not a camera index")
            if index in pins:
                raise ValueError(f"index {index} is bound twice")
            if serial in pins.values():
                # Two indices bound to one body would reserve a side for
                # a body that can only ever be laid on the other one;
                # that is not a binding set, it is a corrupt one.
                raise ValueError(f"serial {serial!r} is bound to two indices")
            pins[index] = serial
        return pins

    def _save_pins(self):
        """Persist the pins after any change, so a restart keeps left and right.

        The snapshot is taken under _map_lock and the write happens outside it:
        an atomic_write is a temp file, an fsync and a rename, and holding the
        map lock across that would block every capture and preview on the SD
        card. Callers must therefore not already hold _map_lock.

        _save_lock is held across both, though, so saves happen one at a time
        and each writes the pins as they stand when its turn comes. Without it
        two saves can take their snapshots in one order and complete in the
        other, leaving an obsolete set on disk - and they would share
        atomic_write's single temp name besides.

        Nothing here raises. A bindings file that cannot be written costs the
        operator a re-confirmation after the next restart; it must never cost
        them the capture they are in the middle of.
        """
        path = self._bindings_path()
        if path is None:
            return
        with self._save_lock:
            with self._map_lock:
                snapshot = {
                    str(index): serial for index, serial in self._pins.items()
                }
            if getattr(self, "_bindings_unread", False):
                # This backend never read the file (the root could not take
                # writes when it started), so its pins were seeded from
                # nothing. The file is read now, under the same lock, and its
                # pins kept wherever neither the index nor the body is one of
                # this backend's: the current pins win, and the operator's
                # bindings for the rest are not overwritten. The pins in
                # memory are left as they are.
                try:
                    on_disk = self._parse_bindings(path) if path.exists() else {}
                except Exception as exc:
                    self.logger.warning(f"[gphoto2] {path} is unreadable ({exc}); not merged")
                    on_disk = {}
                taken = set(snapshot.values())
                for index, serial in on_disk.items():
                    if str(index) not in snapshot and serial not in taken:
                        snapshot[str(index)] = serial
                        taken.add(serial)
                snapshot = dict(sorted(snapshot.items(), key=lambda kv: int(kv[0])))
            payload = json.dumps(
                {"version": _BINDINGS_VERSION, "pins": snapshot}, indent=2
            )
            try:
                # The projects root is created the same way CameraRegistry
                # creates it, so a unit that has not held a project yet still
                # keeps its left/right across the first restart.
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(
                    path, lambda tmp: Path(tmp).write_text(payload, encoding="utf-8")
                )
            except Exception as exc:
                self.logger.warning(
                    f"[gphoto2] could not save the camera bindings to {path} "
                    f"({exc}); they hold for this process but will not survive "
                    "a restart"
                )

    def _drop_seeded_pins_if_none_present(self) -> bool:
        """Forget the seeded bindings when the whole rig has been replaced.

        Seeded pins reserve indices for bodies from a previous run. If a
        complete identification shows that not one of those bodies is on the
        bus, the reservations are meaningless - no left/right can be confused,
        because neither of the remembered bodies is here - and holding them
        would leave a working pair of cameras sitting on provisional indices
        until someone thought to rescan. So the pins are dropped, the map is
        laid out again, and the bodies that are actually present are pinned
        where they land.

        Two guards. Only complete knowledge counts: one port whose serial could
        not be read means nothing is dropped, because an unreadable
        body is not an absent one. And one surviving seeded body is enough to
        keep every pin, so the camera that is still there keeps its side and
        the newcomer waits at a provisional index for the operator's rescan -
        the same rule that applies within a running process.
        """
        with self._map_lock:
            if not self._seeded_serials:
                return False
            published = list(self._port_map.values())
            if not published:
                return False
            if any(port not in self._serial_by_port for _model, port in published):
                return False
            present = {self._serial_by_port[port] for _model, port in published}
            if self._seeded_serials & present:
                # At least one remembered body is here; the bindings stand.
                self._seeded_serials.clear()
                return False
            seeded = dict(self._pins)
            self._pins.clear()
            self._seeded_serials.clear()

        self.logger.info(
            f"[gphoto2] none of the bound bodies {sorted(seeded.values())} is "
            f"present; dropping those bindings and binding what is here"
        )
        self._republish_port_map()
        with self._map_lock:
            fresh = [
                (index, port)
                for index, (_model, port) in sorted(self._port_map.items())
            ]
            cache = dict(self._serial_by_port)
        for index, port in fresh:
            serial = cache.get(port)
            if serial:
                self._learn_serial(index, port, serial)
        self._save_pins()
        return True

    # ------------------------------------------------------------------
    # Port map management
    # ------------------------------------------------------------------

    def _build_port_map(
        self,
        pins: dict[int, str],
        serial_by_port: dict[str, str],
        detected: "list[tuple[str, str]] | None" = None,
    ) -> dict[int, tuple[str, str]]:
        """Lay the detected bodies out on camera indices, by identity where known.

        An index is the operator's left/right, so it must follow the body, not
        the body's position in autodetect(). The rule, applied in autodetect
        order: a body whose serial is known and pinned takes its pinned index;
        every other body - serial unknown, unreadable, or known but not pinned
        anywhere - takes the lowest index that is neither already taken by this
        build nor reserved by a pin. An index pinned to a body that is not
        present therefore stays empty rather than being handed to whichever
        body happened to enumerate first. With no pins and an empty cache the
        result is the positional map, in autodetect order.

        Ports the given detection does not report are dropped from the serial
        cache and the body-model cache: a usb devnum is not reused until the
        bus counter wraps, so a port that is gone carries no information about
        the body that left it.

        The pins and the serial cache are passed in rather than read from
        self, and the body-model cache is pruned on self directly: this is the
        single builder for the map and every caller of it already holds
        _map_lock, so taking that lock here would deadlock. Passing
        the live dicts (not copies) is deliberate - the cache prune above is a
        write the callers want to keep. `detected` is the autodetect result to
        lay out; when it is None a fresh autodetect() runs and is remembered,
        so a caller that only wants the same detection re-laid against new
        identity knowledge can pass it back in.
        """
        if detected is None:
            raw = gp.Camera.autodetect()
            detected = [(raw[i][0], raw[i][1]) for i in range(len(raw))]
            self._usb_attachments = {port: _usb_attachment(port) for _, port in detected}
            self._last_detected = list(detected)
            self._last_scan_at = _monotonic()

        present = {port for _model, port in detected}
        for port in list(serial_by_port):
            if port not in present:
                del serial_by_port[port]
        for port in list(self._body_model_by_port):
            if port not in present:
                del self._body_model_by_port[port]
        for port in list(self._roles_by_port):
            if port not in present:
                del self._roles_by_port[port]

        port_map: dict[int, tuple[str, str]] = {}
        for model, port in detected:
            serial = serial_by_port.get(port)
            index = None
            if serial:
                for pinned_index, pinned_serial in pins.items():
                    if pinned_serial == serial:
                        index = pinned_index
                        break
            if index is None or index in port_map:
                index = 0
                while index in port_map or index in pins:
                    index += 1
            port_map[index] = (model, port)
        self._log_port_map_changes(port_map)
        if not port_map:
            self.logger.warning("[gphoto2] No cameras detected by autodetect().")
        return port_map

    def _log_port_map_changes(self, port_map: dict[int, tuple[str, str]]) -> None:
        """Log the cameras that were added or moved, and those that went away.

        The map is rebuilt on every preview and capture request, so logging
        every entry each time fills the log with identical lines. Only the
        difference from the map last logged is written: an entry is logged as
        detected when its index is new or now holds another model or port,
        and an index that left the map is logged as no longer detected. An
        unchanged map logs nothing. Called from _build_port_map(), whose
        callers hold _map_lock, which also guards _logged_port_map.
        """
        previous = self._logged_port_map
        for index in sorted(port_map):
            entry = port_map[index]
            if previous is None or previous.get(index) != entry:
                model, port = entry
                self.logger.info(
                    f"[gphoto2] Detected camera {index}: {model} on {port}"
                )
        if previous:
            for index in sorted(previous):
                if index not in port_map:
                    model, port = previous[index]
                    self.logger.info(
                        f"[gphoto2] Camera {index}: {model} on {port} "
                        "is no longer detected"
                    )
        self._logged_port_map = dict(port_map)

    def _refresh_port_map(self):
        with self._map_lock:
            self._port_map = self._build_port_map(self._pins, self._serial_by_port)
            self._published_body_count = len(self._port_map)

    def _port_map_is_current(self, camera_index: int) -> bool:
        """Can is_camera_connected() answer for this index without a bus scan?

        Only when all of these hold: a scan has run within
        _ROUTINE_SCAN_MIN_S, the published map has an entry for this index,
        and every published port's device node still exists (see
        _port_node_exists). A body that left or re-enumerated has lost its
        node, so any other answer means the map may be out of date and the
        caller scans. The map and the scan time are read under _map_lock; the
        node checks are filesystem lookups made after it is released.
        """
        with self._map_lock:
            port_map = dict(self._port_map)
            last_scan_at = self._last_scan_at
        if last_scan_at is None or _monotonic() - last_scan_at >= _ROUTINE_SCAN_MIN_S:
            return False
        if camera_index not in port_map:
            return False
        return all(_port_node_exists(port) for _model, port in port_map.values())

    def _republish_port_map(self):
        """Re-lay the last detection against the pins and cache as they are now.

        Identification and re-packing change where a body belongs without
        changing which bodies are on the bus, so the map they produce must
        come from the detection already in hand - a second autodetect() would
        be a wasted USB enumeration and a chance for the two halves of one
        reconcile to disagree.
        """
        with self._map_lock:
            self._port_map = self._build_port_map(
                self._pins, self._serial_by_port, self._last_detected
            )
            self._published_body_count = len(self._port_map)

    def _get_port_map(self) -> dict[int, tuple[str, str]]:
        with self._map_lock:
            if not self._port_map:
                self._port_map = self._build_port_map(
                    self._pins, self._serial_by_port
                )
                self._published_body_count = len(self._port_map)
            return dict(self._port_map)

    def _published_port_map(self) -> dict[int, tuple[str, str]]:
        """The port map as last published, read under _map_lock; never scans.

        Unlike _get_port_map(), an empty map is returned as it is: after a
        completed scan it means no bodies are on the bus.
        """
        with self._map_lock:
            return dict(self._port_map)

    def _port_map_for(self, may_scan: bool) -> dict[int, tuple[str, str]]:
        """The port map for a session lookup: _get_port_map(), or with may_scan
        False the published map without a scan.

        The one exception to "without a scan" is a backend on which no scan
        has completed since start (_last_scan_at is None): there the
        published map is empty because nothing has looked yet, not because
        the bus is empty, so _get_port_map() builds it as before.
        """
        if may_scan:
            return self._get_port_map()
        with self._map_lock:
            scanned = self._last_scan_at is not None
        if not scanned:
            return self._get_port_map()
        return self._published_port_map()

    def camera_model(self, camera_index: int) -> "str | None":
        """The model of the body at this index, from the session or the map.

        The open session is the one a capture at this index just used, so
        its model is preferred: first the body's own name, read once at
        session open, then libgphoto2's abilities name. With no session (one
        can be closed between the capture and this lookup) the port map's
        entry decides: the body's own name cached at its last open, when that
        cache still describes the same body (see _cached_body_model), else
        the entry's own model, which is autodetect()'s name. Nothing is built
        here: an empty map stays empty rather than going through
        _get_port_map(), which would run autodetect(). All of it is read
        without _map_lock, so a lookup never waits behind an enumeration: the
        map and the cache entries are only ever replaced whole, and a dict
        lookup is atomic.
        """
        session = self._sessions.get(camera_index)
        if session is not None:
            for attr in ("body_model", "model"):
                model = normalise_camera_model(getattr(session, attr, None))
                if model is not None:
                    return model
        entry = self._port_map.get(camera_index)
        if not entry:
            return None
        return self._cached_body_model(*entry) or normalise_camera_model(entry[0])

    def _remember_body_model(self, model: str, port: str, serial: str,
                             body_model: "str | None"):
        """Record what the session just opened on `port` called itself.

        Written on every open, whether or not the body turns out to belong at
        the index, since it is a true observation about the port; a body that
        answered no model clears what an earlier one left.
        """
        with self._map_lock:
            if body_model:
                self._body_model_by_port[port] = (model, serial, body_model)
            else:
                self._body_model_by_port.pop(port, None)

    def _cached_body_model(self, model: str, port: str) -> "str | None":
        """The body model cached for `port`, if it is still the same body.

        Same body means the map entry names the same abilities model on the
        same port, and the backend's serial for that port is the serial the
        session read: a different serial, or a serial now known where the
        session read none, is a different body and gets nothing.
        """
        cached = self._body_model_by_port.get(port)
        if cached is None:
            return None
        cached_model, cached_serial, body_model = cached
        if cached_model != model or self._serial_by_port.get(port, "") != cached_serial:
            return None
        return normalise_camera_model(body_model)

    def _learn_serial(self, camera_index: int, port: str, serial: str) -> bool:
        """Record that `port` answered `serial`, and pin the index if it is free.

        Two separate facts are written here. The cache entry (port -> serial)
        is what keeps a body on its index across refreshes while its port
        lives. The pin (index -> serial) is what keeps it on that index after
        the port has gone.

        A pin is written on the *first* serial read at an index, not after some
        later confirmation: an index that has been used but left unpinned is an
        index a different body can silently be published on, which is exactly
        the substitution the pins exist to prevent. The pin is refused only
        when it would overwrite one - the index is already pinned, or the
        serial is already pinned elsewhere - and in that case the cache entry
        is still written, since it is a true observation either way.

        Callers must have released any PTP claim on `port` before calling
        this: what is written here is visible to every other thread at once.
        A new pin is written through to the bindings file, outside the lock,
        so it survives a restart. Returns True when the pins or the cache
        changed.
        """
        if not serial:
            return False
        changed = False
        pinned_now = False
        with self._map_lock:
            if self._serial_by_port.get(port) != serial:
                self._serial_by_port[port] = serial
                changed = True
            if (
                camera_index not in self._pins
                and serial not in self._pins.values()
            ):
                self._pins[camera_index] = serial
                changed = True
                pinned_now = True
                self.logger.info(
                    f"[gphoto2] Camera {camera_index}: pinned to body "
                    f"{serial!r} (on {port})"
                )
        if pinned_now:
            self._save_pins()
        return changed

    def _pinned_serial_confirmed_by_port(self, camera_index: int, port: str) -> str:
        """The serial pinned at `camera_index`, if `port` is known to hold it; else "".

        For a row whose body did not answer a serial just now. A pin on its
        own does not say which body is on a port: the map never lays an
        unidentified body on a pinned index, so a body found there without a
        serial is there either because its port answered the pinned serial
        before, or because the map predates the pin. Only the first is
        evidence, so the pin is used only when the serial cached for this
        same port is the pinned one. A usb devnum is not reused until the bus
        counter wraps, so that earlier read still names the body on it.
        """
        with self._map_lock:
            pinned = self._pins.get(camera_index)
            if pinned and self._serial_by_port.get(port) == pinned:
                return pinned
        return ""

    def _contradicts_pins(self, camera_index: int, serial: str) -> bool:
        """True if `serial` at `camera_index` disagrees with the pins."""
        if not serial:
            return False
        with self._map_lock:
            pinned = self._pins.get(camera_index)
            elsewhere = next(
                (i for i, s in self._pins.items() if s == serial), None
            )
        return (pinned is not None and pinned != serial) or (
            elsewhere is not None and elsewhere != camera_index
        )

    def _repack_pins(self):
        """Give a parked body a side that a dropped pin has freed.

        Called only from rescan(), and only once every published body has been
        positively identified.

        Only pins at or above _SIDE_COUNT move, and only into a side index that
        is free. A body already on a side stays on it: the operator has that
        camera physically placed and labelled, and moving it is not a tidy-up,
        it is silently swapping their left and right. So when A is unplugged
        from {0: A, 1: B}, the answer is {1: B} - a hole at index 0 waiting for
        a replacement - and not {0: B}. What does move is a newcomer that had
        nowhere to go: {1: B, 2: C} becomes {0: C, 1: B}, with B still on the
        side it was on.

        Lowest parked pin first, so several waiting bodies fill the free sides
        in the order they were first seen. Callers must hold _map_lock.
        """
        while True:
            parked = sorted(i for i in self._pins if i >= _SIDE_COUNT)
            free = [i for i in range(_SIDE_COUNT) if i not in self._pins]
            if not parked or not free:
                return
            self._pins[free[0]] = self._pins.pop(parked[0])

    def _get_camera_lock(self, camera_index: int) -> threading.Lock:
        with self._map_lock:
            if camera_index not in self._session_locks:
                self._session_locks[camera_index] = _CameraLock(camera_index)
            return self._session_locks[camera_index]

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _session_matches_map(
        self, camera_index: int, may_scan: bool = True
    ) -> "_PTPSession | None":
        """Return the cached session for camera_index if the current map still backs it.

        Reads the port map fresh on every call - via self._port_map_for(), so
        with may_scan False an empty map after a completed scan is not
        rebuilt by a bus scan - rather than accepting a map the
        caller copied earlier, because
        _refresh_port_map() can publish a new map at any time, outside
        _rescan_lock, and a caller's snapshot can predate that publish. A
        session validated against a stale snapshot could be closed for no
        longer matching a map that was already out of date, or kept alive
        past a publish that actually invalidated it.

        A session is usable only while it is open and the current port map
        still puts the same model on the same port at that index. A cached
        session that fails either test is dead weight holding a PTP claim on
        a body that has moved or gone, so it is closed here and None is
        returned; the caller then treats the index as having no session.
        Callers must hold the per-camera lock for camera_index, since closing
        a session and the reads that follow must not interleave with a
        capture, preview or open on the same body.
        """
        session = self._sessions.get(camera_index)
        if session is None:
            return None
        port_map = self._port_map_for(may_scan)
        entry = port_map.get(camera_index)
        if session._cam is not None and entry == (session.model, session.port):
            return session
        self.logger.info(
            f"[gphoto2] Camera {camera_index}: cached session "
            f"({session.model} on {session.port}) does not match the port map "
            f"entry {entry!r}; closing it"
        )
        self._close_session(camera_index)
        return None

    def _claim_port(self, camera_index: int, port: str) -> "int | None":
        """Register a PTP claim on `port` for `camera_index`, or report the holder.

        One body can be claimed once. The check and the registration have to be
        one step, or two threads both find the port free and both open it, so
        this is the only place either happens: under _map_lock, an unheld port
        is registered to `camera_index` and None is returned; a held one is
        reported by returning the index holding it, and nothing is registered.

        Every claim goes through here, and that is the point. self._sessions
        cannot answer "is this body claimed" - a session claims its body inside
        _PTPSession.__init__, before it is stored; _close_session pops it before
        exit() returns; and a brief identification read never appears there at
        all. Each of those is a window in which a re-pack can move the body to
        another index and a caller there would open it a second time.

        Whoever gets None owns the claim and must give it back through
        _release_port once the body is released - after cam.exit() returns for
        a brief read, after session.close() for a session.
        """
        with self._map_lock:
            holder = self._claimed_ports.get(port)
            if holder is not None:
                return holder
            self._claimed_ports[port] = camera_index
            return None

    def _release_port(self, camera_index: int, port: str):
        """Give back the claim `camera_index` holds on `port`, if it still holds it."""
        with self._map_lock:
            if self._claimed_ports.get(port) == camera_index:
                del self._claimed_ports[port]

    def _take_port(
        self, camera_index: int, port: str, may_scan: bool = True
    ) -> "int | None":
        """Claim `port` for `camera_index`, clearing a stale session if it can.

        A body that moves index leaves a window between the publish that moved
        it and the reconcile that closes its old session, and in that window
        the same port is reachable from two indices.

        When the holder is a cached session at an index nobody is using, that
        session is closed here - through _session_matches_map, under that
        index's own lock, which is the only place a session may be closed - and
        the claim is taken on the second attempt. When the holder is anything
        else - a session at a busy index, or a claim in flight that has no
        entry in self._sessions to close - nothing can safely be done about it
        and the holding index is returned for the caller to refuse on: the
        capture path raises the transient error, enumeration and identification
        skip and try again later.

        Returns None when the claim is now held (the caller must release it),
        otherwise the index still holding the port. may_scan is passed to
        _session_matches_map.
        """
        holder = self._claim_port(camera_index, port)
        if holder is None:
            return None

        session = self._sessions.get(holder)
        if session is None or session.port != port:
            return holder

        other_lock = self._get_camera_lock(holder)
        if not other_lock.acquire(blocking=False):
            return holder
        try:
            self._session_matches_map(holder, may_scan)
        finally:
            other_lock.release()

        return self._claim_port(camera_index, port)

    def _open_labels(self, index, port, original=None, holder=False):
        with self._map_lock:
            labels = {}
            roles = {}
            for i, (model, address) in self._port_map.items():
                serial = self._serial_by_port.get(address)
                suffix = ""
                if serial:
                    hardware_id = _hardware_id(model, serial)
                    cached = self._roles_by_port.get(address)
                    if cached is not None and cached[0] == hardware_id:
                        suffix = f" ({cached[1]})"
                roles[address] = suffix
                # Another camera is named only by a verified binding.
                labels[address] = (f"Camera {i}{suffix}" if serial and self._pins.get(i) == serial
                                   else "the other camera")
            if holder:
                # A new address has no holder identity until its serial is known.
                labels.setdefault(port, "the other camera")
            else:
                # The caller's index identifies its request before any serial read.
                suffix = roles.get(port, roles.get(original, ""))
                labels[port] = f"Camera {index}{suffix}"
            return labels

    def _open_claimed(self, index, model, port, may_scan, factory):
        """Claim the address before opening; a reset move transfers the claim once."""
        original = port
        for attempt in range(2):
            held = self._take_port(index, port, may_scan)
            if held is not None:
                label = self._open_labels(index, port, original)[port]
                raise _PortClaimedError(port, held, label)
            expected = getattr(self, "_usb_attachments", {}).get(port)
            if port != original:
                expected = _usb_attachment(port)
            token = _OPEN_POLICY.set((expected, may_scan))
            names_token = holder_token = None
            try:
                names_token = _OPEN_LABELS.set(self._open_labels(index, port, original))
                holder_token = _OPEN_HOLDER_LABELS.set(self._open_labels(index, port, original, True))
                obj = factory(port)
                if port != original:
                    _WAIT_LOG.info("[gphoto2] %s: USB reset followed new address=%s", original, port)
                return obj, port
            except _PortMoved as moved:
                self._release_port(index, port)
                if not may_scan or attempt:
                    raise
                port = moved.new_port
            except BaseException:
                self._release_port(index, port)
                raise
            finally:
                if holder_token is not None:
                    _OPEN_HOLDER_LABELS.reset(holder_token)
                if names_token is not None:
                    _OPEN_LABELS.reset(names_token)
                _OPEN_POLICY.reset(token)
        raise RuntimeError(
            f"{self._open_labels(index, original)[original]}: the camera did not finish reconnecting. Wait and try again; "
            "if it repeats, switch the camera off and on or ask the administrator."
        )

    def _record_port_move(self, old, new):
        if old == new:
            return
        with self._map_lock:
            self._serial_by_port.pop(old, None)
            self._body_model_by_port.pop(old, None)
            self._roles_by_port.pop(old, None)
            self._last_detected = [(m, new if p == old else p) for m, p in self._last_detected]
            self._usb_attachments.pop(old, None)
            self._usb_attachments[new] = _usb_attachment(new)
            self._port_map = {i: (m, new if p == old else p) for i, (m, p) in self._port_map.items()}

    def _brief_camera(self, index, model, port):
        return self._open_claimed(index, model, port, True, lambda current:
            _init_camera(model, current, *_OPEN_POLICY.get()))

    def _finish_identification(self, cam, port, index):
        token = None
        try:
            token = _OPEN_LABELS.set(self._open_labels(index, port))
            reader = object.__new__(_EventReader)
            reader.port, reader._cam = port, cam
            reader._unsolicited = set()
            reader._drain_startup(step_ms=0)
        finally:
            try:
                with _wait_log(port, "identification close"):
                    cam.exit()
            finally:
                if token is not None:
                    _OPEN_LABELS.reset(token)

    def _open_identified_session(
        self, camera_index: int, model: str, port: str, may_scan: bool = True
    ) -> _PTPSession:
        """Open a session on `port` and hand it back only if it is the right body.

        The identity check runs before the session is cached, so a body that
        contradicts the pin never becomes the session an index serves. With
        may_scan False the map is laid out again from the last detection
        (_republish_port_map) instead of a new bus scan.
        """
        original = port
        session, port = self._open_claimed(
            camera_index, model, port, may_scan,
            lambda current: _PTPSession(current, model, self.logger),
        )
        if port != original:
            if not getattr(session, "serial", ""):
                session.close()
                self._release_port(camera_index, port)
                raise CameraIdentityError(
                    f"{self._open_labels(camera_index, port)[port]}: the camera's identity could not be read; "
                    "no picture was requested. Switch the camera off and on, then try again; "
                    "if it repeats, ask the administrator."
                )
            self._record_port_move(original, port)
        # From here the session owns the claim; _close_session gives it back.
        serial = getattr(session, "serial", "") or ""
        self._remember_body_model(
            model, port, serial, getattr(session, "body_model", None)
        )
        with self._map_lock:
            pinned = self._pins.get(camera_index)
        if not serial and pinned:
            session.close()
            self._release_port(camera_index, port)
            raise CameraIdentityError(
                f"{self._open_labels(camera_index, port)[port]}: the camera's identity could not be read; "
                "no picture was requested. Switch the camera off and on, then try again; "
                "if it repeats, ask the administrator."
            )
        if not serial:
            # A body that does not answer a serial
            # cannot be pinned, so it stays positional.
            self.logger.warning(
                f"[gphoto2] Camera {camera_index}: {model} on {port} answered "
                "no serial number; this index stays positional"
            )
            return session

        with self._map_lock:
            pinned = self._pins.get(camera_index)
            elsewhere = next(
                (i for i, s in self._pins.items() if s == serial), None
            )

        if pinned == serial or (pinned is None and elsewhere is None):
            self._learn_serial(camera_index, port, serial)
            self._set_stored_format_at_open(camera_index, session)
            return session

        # The body is not the one this index is for. Release the claim first,
        # then publish what was learned, then republish the map so the body
        # turns up at the index it belongs to.
        session.close()
        self._release_port(camera_index, port)
        self._learn_serial(camera_index, port, serial)
        if may_scan:
            self._refresh_port_map()
        else:
            self._republish_port_map()
        raise CameraIdentityError(
            f"Camera {camera_index}: expected body {pinned!r}, found {serial!r}"
            + (f" (pinned to index {elsewhere})" if elsewhere is not None else "")
            + "; the cameras have re-enumerated - re-confirm left/right and retry"
        )

    def _identify_unknown_ports(self) -> bool:
        """Read a serial from every published port whose body is unknown.

        A body that re-enumerates arrives on a port nothing has been read from,
        so the map can only place it provisionally, on a free index. This is
        what turns that guess into knowledge: one brief PTP claim per
        unidentified port, under that index's own lock so it cannot race a
        capture or an open, and never more than once per re-enumeration
        because the answer is cached.

        Where a session is already cached at that index and still matches the
        map, its serial is taken from the session instead - it was read at
        session open and needs no second claim.

        The brief read takes the same ownership rule the enumeration takes: a
        port a cached session under another index still holds is not opened
        here. Bodies without a readable serial make that reachable - two of
        them stay positional, so one leaving slides the other onto a lower
        index while its session is still cached under the old one, and the
        port at the new index is unidentified. Identification is never worth a
        second claim on a body, so a port whose other index is busy is simply
        left for the next pass.

        A read that fails is not an answer: nothing is cached, no pin moves,
        the port is named in the log and stays provisional. Never raises.
        Returns True if anything was learned.
        """
        with self._map_lock:
            snapshot = dict(self._port_map)
            known = set(self._serial_by_port)

        learned = False
        for camera_index, (model, port) in sorted(snapshot.items()):
            if port in known:
                continue
            serial = ""
            # The open session that got its serial here, if one did.
            identified = None
            with self._get_camera_lock(camera_index):
                session = self._session_matches_map(camera_index)
                if session is not None:
                    serial = getattr(session, "serial", "") or ""
                    read_port = session.port
                    if not serial:
                        # The read at session open failed. Ask the body
                        # again through the session that already holds it -
                        # still no second claim.
                        try:
                            cfg = session._cam.get_config()
                            serial = cfg.get_child_by_name(
                                "serialnumber"
                            ).get_value().strip()
                        except Exception:
                            serial = ""
                        if serial:
                            session.serial = serial
                            identified = session
                else:
                    read_port = port
                    if self._get_port_map().get(camera_index) != (model, port):
                        # The map moved on while this loop was waiting for the
                        # lock; opening (model, port) would claim a port
                        # nothing backs any more.
                        continue
                    cam = None
                    initialized = False
                    try:
                        cam, actual_port = self._brief_camera(camera_index, model, port)
                        initialized = True
                        self._record_port_move(port, actual_port)
                        port = actual_port
                        read_port = port
                        with _wait_log(port, "identification read"):
                            cfg = cam.get_config()
                            serial = cfg.get_child_by_name("serialnumber").get_value().strip()
                    except _PortClaimedError as exc:
                        self.logger.info(
                            f"[gphoto2] Camera {camera_index}: identification skipped for "
                            f"{exc.port}: held by index {exc.holder}"
                        )
                        continue
                    except Exception:
                        serial = ""
                    finally:
                        # init() may have succeeded even if the read raised;
                        # the claim is released either way, and only then is
                        # anything published from it.
                        if initialized:
                            try:
                                self._finish_identification(cam, port, camera_index)
                            except Exception:
                                pass
                        self._release_port(camera_index, port)
                if serial:
                    learned = self._learn_serial(camera_index, read_port, serial) or learned
                    if identified is not None:
                        self._set_stored_format_when_identified(
                            camera_index, identified
                        )
                else:
                    self.logger.info(
                        f"[gphoto2] Camera {camera_index}: no serial could be "
                        f"read from {port}; it stays provisional"
                    )
        return learned

    @_one_reset_call
    def _get_or_open_session(
        self, camera_index: int, may_scan: bool = True
    ) -> _PTPSession:
        """Return an existing open session, or open a new one.

        The cached session is validated against the port map as currently
        published (see _session_matches_map), never a snapshot held only by
        this call. When validation finds no usable session, a new one is
        opened against whatever the map says at that point; the map is free
        to have moved on again by then, and that is fine - the open below
        uses whatever is published now, and the -105 retry path further down
        still refreshes and retries on top of that.

        The body that answers is then checked against this index's pin before
        the session is cached, so a re-enumeration that put a different body
        on this port raises CameraIdentityError instead of quietly serving
        the wrong camera.

        With may_scan False no bus scan is made anywhere below once a scan
        has completed (see _port_map_for), so a caller holding the camera
        lock does not scan under it: every map read is the published map (an
        empty one after a completed scan is "no bodies", so
        CameraNotConnectedError), and a -105 on open re-reads the
        published map instead of re-detecting, retrying only if another
        thread has since published a different entry for this index.
        """
        existing = self._session_matches_map(camera_index, may_scan)
        if existing is not None:
            existing._label = self._open_labels(camera_index, existing.port)[existing.port]
            return existing

        port_map = self._port_map_for(may_scan)
        if camera_index not in port_map:
            raise CameraNotConnectedError(
                f"Camera index {camera_index} not found. "
                f"Detected indices: {sorted(port_map.keys())}. "
                "Ensure the camera is on and USB is connected, then try again."
            )

        model, port = port_map[camera_index]
        try:
            session = self._open_identified_session(
                camera_index, model, port, may_scan
            )
            session._label = self._open_labels(camera_index, session.port)[session.port]
            self._sessions[camera_index] = session
            return session
        except gp.GPhoto2Error as exc:
            # -105 Unknown model usually means ports changed (USB re-enumeration
            # after a power-cycle). Refresh the port map once and retry.
            if "-105" in str(exc) or "Unknown model" in str(exc):
                if may_scan:
                    self.logger.warning(
                        f"[gphoto2] Camera {camera_index}: session init failed ({exc}). "
                        "Refreshing port map and retrying ..."
                    )
                    self._refresh_port_map()
                    port_map = self._get_port_map()
                else:
                    self.logger.warning(
                        f"[gphoto2] Camera {camera_index}: session init failed "
                        f"({exc}); re-reading the published port map"
                    )
                    port_map = self._published_port_map()
                    if port_map.get(camera_index) == (model, port):
                        raise
                if camera_index not in port_map:
                    raise CameraNotConnectedError(
                        f"Camera index {camera_index} not found after port map refresh."
                    ) from exc
                model, port = port_map[camera_index]
                session = self._open_identified_session(
                    camera_index, model, port, may_scan
                )
                session._label = self._open_labels(camera_index, session.port)[session.port]
                self._sessions[camera_index] = session
                return session
            raise

    def _close_session(self, camera_index: int):
        session = self._sessions.pop(camera_index, None)
        if session is None:
            return
        try:
            session.close()
        finally:
            # Only once exit() has come back is the body actually free; the
            # entry above was popped before that, so the claim registry is
            # what covers the gap.
            self._release_port(camera_index, session.port)

    # ------------------------------------------------------------------
    # CameraBackend interface
    # ------------------------------------------------------------------

    @_one_reset_call
    def is_camera_connected(self, camera_index: int = 0) -> bool:
        """Is a body present at this index right now?

        Re-detects unless the published map is current (_port_map_is_current):
        a scan within the last _ROUTINE_SCAN_MIN_S, an entry for this index,
        and every published port's device node still present. A body that is
        unplugged or power-cycled loses its node, so the next call scans and
        the change is reflected; a body plugged in at another index is found
        by the next scan the interval allows. A body that came back on a new port is unidentified at that point, so
        the fresh map can only place it provisionally and this index would
        read as disconnected. When - and only when - this index is pinned,
        absent from the fresh map, and at least one published port has no
        known body, the unidentified ports are read once and the map is laid
        out again. That costs one brief PTP claim per re-enumeration, not one
        per call, because the answer is cached; and it can only ever move a
        body onto the index its own serial is pinned to. The one exception is
        a rig in which not a single body from the saved bindings is present
        (see _drop_seeded_pins_if_none_present): those reservations cannot
        confuse anyone's left and right, so they are dropped rather than left
        holding a working pair of cameras on provisional indices. A pin
        learned in this process is never dropped here - only rescan(), which
        the operator asks for, may do that.

        A scan that raises is not an answer: the scan failed, which says
        nothing about whether the body is there. "The scan failed" means
        gp.Camera.autodetect() raised gp.GPhoto2Error inside the
        re-detection; the published map is then left as the last completed
        scan made it, and the answer is True only if that map has the index
        and the port's device node still exists (_last_scan_still_backs), so
        a body that was connected stays connected until its node goes and
        one that was absent stays absent. The
        first failure in a run is logged as an error and the scan that ends
        the run at info; the failures between are logged at debug. Any other
        exception is still logged and answered as False. The answer to a scan
        that completes is unchanged.
        """
        try:
            # Detect afresh unless the map still points at live devices, so a
            # power-cycle since the last call is reflected without scanning
            # the bus on every preview and capture.
            if not self._port_map_is_current(camera_index):
                try:
                    self._refresh_port_map()
                except gp.GPhoto2Error as exc:
                    return self._answer_from_last_scan(camera_index, exc)
                self._note_scan_completed()
            with self._map_lock:
                pinned = camera_index in self._pins
                absent = camera_index not in self._port_map
                unidentified = any(
                    port not in self._serial_by_port
                    for _model, port in self._port_map.values()
                )
            if pinned and absent:
                # Ask the seeded-bindings question first: an enumeration may
                # already have identified every body, in which case there is
                # nothing left to read and the rule below would never get a
                # chance to run. It guards itself on complete knowledge and on
                # there being seeded pins at all, so calling it here is free.
                self._drop_seeded_pins_if_none_present()
                if unidentified:
                    self._identify_unknown_ports()
                    self._drop_seeded_pins_if_none_present()
                self._republish_port_map()
            # The map just checked or published, read without the rebuild
            # _get_port_map() does for an empty map: an empty map here is a
            # scan's answer, and a second autodetect() would only repeat it.
            with self._map_lock:
                return camera_index in self._port_map
        except Exception as exc:
            self.logger.error(f"[gphoto2] is_camera_connected({camera_index}): {exc}")
            return False

    def _last_scan_still_backs(self, camera_index: int) -> bool:
        """Does the last completed scan still put a body at this index?

        The rule: a body recorded in the last completed scan counts as
        connected only while its usbfs device node still exists
        (_port_node_exists). The node is direct evidence that the device is
        on the bus; the published map is only what the bus said when it was
        last read. Where the node cannot be seen (no usbfs), the answer is
        False.

        Every answer from is_camera_connected() or capture_preview() that a
        body is connected, made without a scan completed in the same call,
        is listed here, and each checks the node:
        1. is_camera_connected() with a current map (_port_map_is_current):
           a scan completed within _ROUTINE_SCAN_MIN_S, and every published
           port's node exists, checked there.
        2. is_camera_connected() when its scan raised gp.GPhoto2Error
           (_answer_from_last_scan): this method.
        3. capture_preview()'s PreviewFrameFailedError, the 503 that tells
           the client the body is connected. It is raised in exactly one
           place, _raise_no_frame_or_not_connected, and only after that
           method has read the map as published at that moment, found the
           session's own model and port at this index, and found the port's
           node present; any other finding raises CameraNotConnectedError
           and closes the session if it is still the cached one. Every exit
           of capture_preview() after a frame failed with gp.GPhoto2Error
           goes through it: a retry skipped because the camera lock was
           busy, a re-detect before the retry that raised, and a retried
           frame that failed. capture_preview()'s other exits are a frame
           returned, which is the body answering in that call; the errors
           raised while the session is resolved (_get_or_open_session); and
           any exception other than gp.GPhoto2Error from a frame call, which
           propagates as it is. None of those is PreviewFrameFailedError.
        Every other True from is_camera_connected() follows a scan that
        completed in that call, any other exception there answers False, and
        before any scan has completed the map is empty, so the answer is
        False.
        """
        with self._map_lock:
            entry = self._port_map.get(camera_index)
        return entry is not None and _port_node_exists(entry[1])

    def _answer_from_last_scan(self, camera_index: int, exc: Exception) -> bool:
        """is_camera_connected()'s answer when its bus scan raised exc."""
        connected = self._last_scan_still_backs(camera_index)
        with self._map_lock:
            first = not self._scan_failing
            self._scan_failing = True
        message = (
            f"[gphoto2] is_camera_connected({camera_index}): the bus scan "
            f"failed ({exc}); answering {connected} from the last completed "
            "scan and the device node"
        )
        if first:
            self.logger.error(message)
        else:
            self.logger.debug(message)
        return connected

    def _note_scan_completed(self):
        """End a run of failed scans in is_camera_connected(), logging that it ended."""
        with self._map_lock:
            ended = self._scan_failing
            self._scan_failing = False
        if ended:
            self.logger.info("[gphoto2] The bus scan completes again")

    # capture_image() takes focus_check (NEH-339); capture.service passes it
    # only to a backend class that sets this.
    supports_focus_check = True

    def capture_image(
        self,
        output_path: Path,
        camera_config,
        capture_output: bool = False,
        focus_check: bool = False,
    ) -> str:
        """Capture one image to output_path. Returns (path as str, None).

        Most CameraConfig fields (awb, vflip, lens_position, etc.) are
        picamera2-specific and are silently ignored here. The DSLR fields
        (iso, shutter_speed, aperture, image_format) are applied through
        _PTPSession.apply_dslr_config before the shutter fires.

        The capture's format, resolved under the camera lock: a format the
        configuration names, when the session holds a stored format for the
        body (session.stored_format, for a body whose identity is known); the
        stored format, when the configuration names none; otherwise none, and
        the camera's own format is left as it is. A named format is written
        only when a stored one is held, because only then does the next
        capture without a named format set the camera back. When none is
        held the named format is not written, one line is logged at INFO,
        and the capture is taken in whatever the camera holds, with nothing
        checked (_check_produced_format gets no intended format).

        Args:
            output_path: Destination path for the captured JPEG.
            camera_config: CameraConfig (camera_index used for routing).
            capture_output: Unused - kept for interface compatibility.
            focus_check: One trigger attempt and a _FOCUS_CHECK_DEADLINE_S
                deadline after an accepted trigger
                (see _PTPSession.capture). False uses the exposure-sized deadline.

        Returns:
            (path, None): the saved image file's path as a string (`.cr2`
            when the body shoots RAW), and no metadata.

        Raises:
            RuntimeError: If the capture fails, a timeout included.
        """
        camera_index = getattr(camera_config, "camera_index", 0)
        lock = self._get_camera_lock(camera_index)

        with lock:
            session = self._get_or_open_session(camera_index)
            # The format this capture is meant to take: the one its
            # configuration names, else the one stored for the body the open
            # session holds, resolved here under the camera lock.
            intended = getattr(camera_config, "image_format", None)
            stored = None
            if self._body_hardware_id(camera_index, session):
                stored = getattr(session, "stored_format", None)
            if intended is None:
                intended = stored
            elif stored is None:
                # Nothing would set the camera back after this capture, so the
                # explicit format is not written and nothing is checked.
                self.logger.info(
                    f"[gphoto2] Camera {camera_index}: the capture's format "
                    f"{normalise_image_format(intended) or intended} was not applied: "
                    "the session holds no stored format for this body, so the "
                    "capture is taken in the format the camera holds"
                )
                camera_config = copy.copy(camera_config)
                camera_config.image_format = None
                intended = None
            try:
                if stored is None:
                    session.apply_dslr_config(camera_config)
                else:
                    session.apply_dslr_config(camera_config, image_format=stored)
                if focus_check:
                    elapsed, actual_path = session.capture(output_path, focus_check=True)
                else:
                    elapsed, actual_path = session.capture(output_path)
                self.logger.info(
                    f"[gphoto2] Camera {camera_index}: captured {actual_path.name} "
                    f"in {elapsed:.2f}s"
                )
                # The session works, so any run of failed preview polls on it
                # is over.
                self._end_preview_failure_run(camera_index, session)
                mismatch = self._check_produced_format(
                    camera_index, session, intended, actual_path
                )
                result = (str(actual_path), None)
            except BaseException as exc:
                try:
                    self._close_session(camera_index)
                except BaseException:
                    pass
                if isinstance(exc, (gp.GPhoto2Error, CaptureTimeoutError, CaptureAttributionError)):
                    self._raise_capture_failure(camera_index, exc)
                raise
        if mismatch is not None:
            try:
                self._system_log(**mismatch)
            except BaseException as exc:
                with lock:
                    if self._sessions.get(camera_index) is session:
                        try:
                            self._close_session(camera_index)
                        except BaseException:
                            pass
                if not isinstance(exc, Exception):
                    raise
                self.logger.warning(
                    f"[gphoto2] Camera {camera_index}: the system log row for a "
                    f"capture format mismatch was not written ({exc!r})"
                )
        return result

    def _raise_capture_failure(self, camera_index: int, exc: Exception):
        """Report a typed capture failure after its session and claim are closed."""
        reason = "capture timed out" if isinstance(exc, CaptureTimeoutError) else "capture failed"
        self.logger.error(f"[gphoto2] Camera {camera_index}: {reason}: {exc}; session closed")
        if isinstance(exc, CaptureAttributionError):
            raise RuntimeError(str(exc)) from exc
        raise RuntimeError(f"DSLR capture failed: {exc}") from exc

    @_one_reset_call
    def list_devices(self) -> list:
        """Enumerate all connected DSLR cameras and return device metadata.

        Uses the persistent port map (refreshed if empty) to pick the set of
        indices to look at, but every session match is against the map as
        currently published (see _session_matches_map), not this call's
        snapshot - a publish can land while this call is paused on a
        per-camera lock. For each camera, takes the serial an already-open PTP
        session read at open, asking the body through that session only if
        that read failed; if no session is open yet, opens a brief one just
        for the read and closes it. A body that answers no serial either way
        is reported under its pinned serial when the serial cached for its
        port is that one, and under the `<model>_idx<N>` placeholder
        otherwise. A cached session is re-used only
        while the current map still puts the same model on the same port at
        that index - one that does not match is a claim on a body that has
        moved or gone, so it is closed and the temporary read path runs
        against the map's port instead. A row built from a matched session
        reports the session's own port, which can be newer than this call's
        snapshot.

        When no session is cached and the current map disagrees with this
        call's snapshot for an index - a different port, a different model,
        or the index gone entirely - the temporary open is skipped for that
        row rather than risking a claim on a port nothing backs any more; the
        row is left out of the result, and the caller's next
        list_devices()/rescan() call sees the current map.

        The per-camera lock is held while the serial is read, so enumeration
        serialises with capture, preview and session opening. Without it, an
        enumeration racing _get_or_open_session sees no cached session (the
        session is only stored once _PTPSession.__init__ returns) and would
        claim a camera another thread is already opening.

        Every serial read here is also learned: a brief read caches its answer
        once its own claim has been released, which pins a body that had not
        been read at that index before. A row whose serial contradicts the
        pins is left out rather than reported at the wrong index, and if the
        walk learned enough to move a body, the map is laid out again and the
        enumeration is run once more - at most once - so the rows come out at
        the indices the pins give them.
        """
        port_map = self._get_port_map()
        if not port_map:
            return []

        result, learned = self._enumerate_port_map(port_map)
        if not learned:
            return result

        # This walk may have completed the picture, so the seeded-bindings
        # question gets asked here too - an enumeration can identify every body
        # before is_camera_connected ever sees an unknown port.
        self._drop_seeded_pins_if_none_present()

        # Something was identified during the walk. If that changes where the
        # bodies belong, republish and enumerate once more - bounded to one
        # retry - so the rows come out at their pinned indices rather than the
        # provisional ones this pass started from.
        with self._map_lock:
            rebuilt = self._build_port_map(
                self._pins, self._serial_by_port, self._last_detected
            )
            differs = rebuilt != port_map
            if differs:
                self._port_map = rebuilt
                self._published_body_count = len(self._port_map)
        if differs:
            result, _ = self._enumerate_port_map(rebuilt)
        return result

    def _enumerate_port_map(self, port_map) -> tuple[list, bool]:
        """One enumeration pass over `port_map`; returns (rows, learned_anything)."""
        result = []
        learned = False
        for idx, (model_raw, port) in sorted(port_map.items()):
            serial = ""
            row_port = port
            row_model = model_raw
            with self._get_camera_lock(idx):
                # Re-use an existing session, but only while the current map
                # still backs it; a session on a stale port is closed here
                # and the temporary read path below runs instead.
                session = self._session_matches_map(idx)
                if session is not None:
                    # The match above is against the current map, not this
                    # snapshot, so report the session's own port rather than
                    # replay a port that may not be what this index's
                    # snapshot row holds any more.
                    row_port = session.port
                    row_model = session.model
                    # The body on a session's port cannot change while the
                    # session holds it, so the serial read at open still
                    # names it. Asking again only when that read failed keeps
                    # a busy body (a live preview, say) from losing its
                    # identity to a read it is too busy to answer.
                    serial = getattr(session, "serial", "") or ""
                    newly_identified = False
                    if not serial:
                        try:
                            cfg = session._cam.get_config()
                            serial = cfg.get_child_by_name("serialnumber").get_value().strip()
                        except Exception:
                            serial = ""
                        newly_identified = bool(serial)
                    if serial:
                        # Learn it here too. A body whose read failed at
                        # session open has no pin, and reporting a serial
                        # without learning it leaves the index unpinned and
                        # free for a later arrival to take. There is nothing
                        # to release first: the rule that a serial is published
                        # only once its claim is released is about a claim that
                        # is about to be dropped, and this session's claim
                        # lives on either way.
                        session.serial = serial
                        learned = self._learn_serial(
                            idx, session.port, serial
                        ) or learned
                        if newly_identified:
                            self._set_stored_format_when_identified(idx, session)
                else:
                    current_entry = self._get_port_map().get(idx)
                    if current_entry != (model_raw, port):
                        # The current map disagrees with this row's
                        # snapshot; opening against (model_raw, port) would
                        # claim a port nothing backs any more. Leave this row
                        # out - the next enumeration sees the current map.
                        continue
                    cam = None
                    initialized = False
                    try:
                        cam, actual_port = self._brief_camera(idx, model_raw, port)
                        initialized = True
                        self._record_port_move(port, actual_port)
                        port = actual_port
                        row_port = port
                        with _wait_log(port, "identification read"):
                            cfg = cam.get_config()
                            serial = cfg.get_child_by_name("serialnumber").get_value().strip()
                    except _PortClaimedError as exc:
                        self.logger.info(
                            f"[gphoto2] Camera {idx}: listing skipped for "
                            f"{exc.port}: held by index {exc.holder}"
                        )
                        continue
                    except Exception:
                        pass
                    finally:
                        # init() may have succeeded even if a later step (e.g.
                        # get_config() or the serial read) raised. Always
                        # release the PTP claim in that case, or the camera
                        # stays claimed by this dangling `cam` object while
                        # the lock is dropped and enumeration continues.
                        if initialized:
                            try:
                                self._finish_identification(cam, port, idx)
                            except Exception:
                                pass
                        self._release_port(idx, port)
                    # The claim is gone, so what it read may now be
                    # published.
                    if serial:
                        learned = self._learn_serial(idx, port, serial) or learned

            if not serial:
                serial = self._pinned_serial_confirmed_by_port(idx, row_port)

            if self._contradicts_pins(idx, serial):
                # This index is pinned to another body, or this body is pinned
                # to another index. Reporting the row would tell the operator
                # a camera is somewhere it is not; the next enumeration, after
                # the map has been laid out again, reports it correctly.
                self.logger.warning(
                    f"[gphoto2] Camera {idx}: body {serial!r} on {row_port} "
                    "contradicts the pins; leaving this row out"
                )
                continue

            # The role the pin suggests, shown to the operator and acted on
            # by nothing: only a body whose serial is pinned at index 0 or 1
            # has one.
            role_guess = None
            if serial:
                with self._map_lock:
                    pinned_here = self._pins.get(idx) == serial
                if pinned_here:
                    role_guess = _ROLE_GUESS_BY_PIN_INDEX.get(idx)

            hw_id = (
                _hardware_id(row_model, serial) if serial
                else _hardware_id(row_model, f"idx{idx}")
            )
            result.append({
                "index": idx,
                "model": row_model,
                "hardware_id": hw_id,
                "serial": serial or None,
                "location": f"USB {row_port}",
                "port": row_port,                # raw USB port, e.g. "usb:001,005"
                "has_aperture_control": True,   # DSLRs always expose aperture via PTP
                "supports_zoom": False,          # No digital zoom for DSLRs
                # The USB socket the body is plugged into, as a topology
                # path ("1-1.2"), looked up in sysfs from the port's bus and
                # device numbers. No claim is made on the device for it.
                "usb_port": usb_topology.from_gphoto2_port(
                    row_port, sysfs_root=_SYSFS_USB_DEVICES
                ),
                "role_guess": role_guess,
                "role_guess_source": "gphoto2_pin" if role_guess else None,
            })

        return result, learned

    @_one_reset_call
    def rescan(self) -> list:
        """Re-detect the bodies, republish the port map, drop stale sessions.

        The recovery lever for a DSLR that drops off USB or re-enumerates onto
        a different port mid-session: without it the only way to clear a stale
        PTP session is a restart.

        The new map is published before any session is reconciled, so a session
        that finishes opening after this point is validated against the port
        map as currently published at its next use, under its own lock - the
        reconcile loop cannot see a session that is still inside its
        constructor, and publishing first is what makes that harmless.
        Reconcile, and the list_devices() enumeration returned below, both
        read the port map fresh at the point of each check (see
        _session_matches_map); a _refresh_port_map() from another caller
        landing between this rescan's own publish and its reconcile is
        honoured on that fresh read rather than masked by this rescan's own
        snapshot. The rescan lock serialises overlapping rescans so their
        snapshots publish in order rather than racing.

        This is also the only place a pin may be dropped or moved. The order
        is: publish a provisional map; read a serial from every port whose
        body is unknown; then, and only if every published port now has a
        known body, drop the pins whose bodies are positively absent and
        close the gaps that leaves (see _repack_pins). "Positively absent"
        is the whole point of the guard: a serial that could not be read is
        not evidence a body is gone, so one unreadable port keeps every pin
        exactly where it is and the port is named in the log instead. The map
        is then laid out again against the surviving pins and the sessions
        are reconciled under their own locks.

        Returns:
            list: The device dicts for the freshly detected bodies, in the same
            shape list_devices() returns.
        """
        with self._rescan_lock:
            self._refresh_port_map()
            self._identify_unknown_ports()
            self._drop_seeded_pins_if_none_present()

            pins_changed = False
            with self._map_lock:
                before = dict(self._pins)
                published = list(self._port_map.values())
                unidentified = [
                    port for _model, port in published
                    if port not in self._serial_by_port
                ]
                if unidentified:
                    self.logger.info(
                        f"[gphoto2] rescan: no serial could be read from "
                        f"{unidentified}; every pin is kept"
                    )
                else:
                    present = {
                        self._serial_by_port[port] for _model, port in published
                    }
                    dropped = {
                        i: s for i, s in self._pins.items() if s not in present
                    }
                    for index in dropped:
                        del self._pins[index]
                    if dropped:
                        self.logger.info(
                            f"[gphoto2] rescan: dropped pins {dropped} for "
                            "bodies that are positively absent"
                        )
                    self._repack_pins()
                pins_changed = self._pins != before

            if pins_changed:
                self._save_pins()

            self._republish_port_map()

            closed = []
            for idx in list(self._sessions):
                with self._get_camera_lock(idx):
                    if self._session_matches_map(idx) is None:
                        closed.append(idx)

            with self._map_lock:
                pins = dict(self._pins)
                new_map = dict(self._port_map)
            self.logger.info(
                f"[gphoto2] rescan: pins are now {pins}; "
                f"closed stale sessions {closed}; "
                f"port map is now {new_map}"
            )
            return self.list_devices()

    @preview_request()
    def capture_preview(self, camera_index: int) -> bytes:
        """Return a live-preview JPEG frame from the camera.

        Uses the same persistent PTP session as full captures so there is no
        reconnect overhead. The first frame takes ~1.3s (camera warms up video
        subsystem); subsequent frames are ~30ms at ~33 fps.

        The per-camera lock serialises preview and full-capture calls so they
        never interleave on the same session. Once a scan has completed, no
        bus scan is made while it is held: the session is resolved with
        _get_or_open_session(may_scan=False), against the published map only.

        A Canon body can fail one frame ([-1] Unspecified error) and serve the
        next, so a failed frame does not close the session. It is tried once
        more, after _PREVIEW_RETRY_DELAY_S with the lock released, and only if
        the lock is free then: a capture waiting on the lock goes first and
        this poll is reported as failed. Before the retry, a port whose usbfs
        node has gone (_port_node_exists) sends the map through a re-detect,
        as is_camera_connected() would, with the lock released; the session
        is then resolved again, which closes it if the published map no
        longer backs it and raises when the index is gone
        (CameraNotConnectedError) or holds another body (CameraIdentityError).

        Every exit at which no frame was obtained - a retry skipped for a
        busy lock, a re-detect that raised gp.GPhoto2Error, a retried frame
        that failed - goes through _raise_no_frame_or_not_connected, which
        decides between PreviewFrameFailedError and CameraNotConnectedError
        from the evidence the backend holds at that moment. A run of failed
        polls that lasts _ROUTINE_SCAN_MIN_S closes the session (see
        _note_preview_failure), at once when the camera lock is free and
        otherwise at the next acquisition here (_close_session_if_run_due).
        """
        lock = self._get_camera_lock(camera_index)
        with lock:
            self._close_session_if_run_due(camera_index)
            session = self._get_or_open_session(camera_index, False)
            try:
                return self._preview_frame(camera_index, session)
            except gp.GPhoto2Error as exc:
                first_exc = exc
                self.logger.warning(
                    f"[gphoto2] Camera {camera_index}: preview capture failed: "
                    f"{exc}; trying once more"
                )

        with _wait_log(camera_index, "preview retry pause"):
            time.sleep(_PREVIEW_RETRY_DELAY_S)
        if not _port_node_exists(session.port):
            try:
                self._refresh_port_map()
            except gp.GPhoto2Error as exc:
                self.logger.warning(
                    f"[gphoto2] Camera {camera_index}: re-detect before the "
                    f"preview retry failed: {exc}"
                )
                self._raise_no_frame_or_not_connected(
                    camera_index, session, first_exc,
                    f"re-detect failed: {exc}; not retried",
                )

        if not lock.acquire(blocking=False):
            self._raise_no_frame_or_not_connected(
                camera_index, session, first_exc, "camera busy; not retried"
            )
        try:
            session = self._get_or_open_session(camera_index, False)
            try:
                return self._preview_frame(camera_index, session)
            except gp.GPhoto2Error as exc:
                self.logger.error(
                    f"[gphoto2] Camera {camera_index}: preview capture failed: {exc}"
                )
                failed = exc
        finally:
            lock.release()
        self._raise_no_frame_or_not_connected(camera_index, session, failed)

    def _raise_no_frame_or_not_connected(
        self,
        camera_index: int,
        session: "_PTPSession",
        cause: Exception,
        note: str = "",
    ):
        """Raise for a preview poll that got no frame on session: "no frame" or "not connected".

        Raises CameraNotConnectedError, after closing session through
        _close_gone_session, when the backend's current evidence says the
        body is not there: the map as published now (read under _map_lock;
        no bus scan is made) has no entry at camera_index, or has an entry
        other than session's own model and port, or session's port has no
        usbfs node (_port_node_exists). Otherwise records the failure in the
        run (_record_preview_failure) and raises PreviewFrameFailedError.
        This is the only place in the backend that raises
        PreviewFrameFailedError. The caller must not hold the camera lock.
        """
        with self._map_lock:
            entry = self._port_map.get(camera_index)
        if entry is None:
            reason = f"the port map has no camera at index {camera_index}"
        elif entry != (session.model, session.port):
            reason = f"the port map has {entry[0]} on {entry[1]} at this index"
        elif not _port_node_exists(session.port):
            reason = f"the device node for {session.port} is gone"
        else:
            self._record_preview_failure(camera_index, session)
            raise PreviewFrameFailedError(
                f"DSLR preview capture failed: {cause}"
                + (f" ({note})" if note else "")
            ) from cause
        self._close_gone_session(camera_index, session, reason)
        raise CameraNotConnectedError(
            f"Camera {camera_index} is not connected: {reason} "
            f"(preview capture failed: {cause}"
            + (f"; {note})" if note else ")")
        ) from cause

    def _close_gone_session(
        self, camera_index: int, session: "_PTPSession", reason: str
    ):
        """Close session, whose body is not there (reason), if it is still the cached one.

        Takes the camera lock, waiting for it, then _close_session takes
        _map_lock inside it; no bus scan is made under the camera lock. A
        session already replaced or closed by someone else is left alone.
        """
        with self._get_camera_lock(camera_index):
            if self._sessions.get(camera_index) is not session:
                return
            self._end_preview_failure_run(camera_index, session)
            self.logger.info(
                f"[gphoto2] Camera {camera_index}: {reason}; closing the session"
            )
            self._close_session(camera_index)

    def _preview_frame(self, camera_index: int, session: "_PTPSession") -> bytes:
        """One capture_preview() on session; a good frame clears the failure record.

        Caller holds the camera lock.
        """
        camera_file = session._cam.capture_preview()
        data = bytes(camera_file.get_data_and_size())
        self._end_preview_failure_run(camera_index)
        return data

    def _end_preview_failure_run(
        self, camera_index: int, session: "_PTPSession | None" = None
    ):
        """Forget the run of failed polls at camera_index (only session's, if given)."""
        with self._preview_record_lock:
            recorded = self._preview_failing_since.get(camera_index)
            if recorded is not None and (session is None or recorded[0] is session):
                del self._preview_failing_since[camera_index]

    def _note_preview_failure(self, camera_index: int, session: "_PTPSession") -> bool:
        """Record a failed poll; True once the run of failures spans the scan interval.

        Takes _preview_record_lock and nothing else, so it may be called with
        or without the camera lock. A run is a sequence of failed polls on
        one session, each within _PREVIEW_FAILURE_GAP_S of the one before,
        with no good frame and no successful capture on that session in
        between; a failure after a longer gap, or on another session, starts
        a new run. The window is _ROUTINE_SCAN_MIN_S because the capture
        service asks is_camera_connected() before every poll and that
        re-detects the bus at least that often: once a run has lasted that
        long, the body has been seen on the bus again during it and the
        session is what is failing, so it is to be closed and the next poll
        re-opens it. A run of one poll never spans the window, so a single
        failed poll never closes the session. The record stays in place
        until the session is closed (_close_session_if_run_due) or the run
        ends.
        """
        now = _monotonic()
        with self._preview_record_lock:
            recorded = self._preview_failing_since.get(camera_index)
            if (
                recorded is None
                or recorded[0] is not session
                or now - recorded[2] > _PREVIEW_FAILURE_GAP_S
            ):
                self._preview_failing_since[camera_index] = (session, now, now)
                return False
            first = recorded[1]
            self._preview_failing_since[camera_index] = (session, first, now)
            return now - first >= _ROUTINE_SCAN_MIN_S

    def _record_preview_failure(self, camera_index: int, session: "_PTPSession"):
        """Record a failed poll in the run, closing session if the run now spans the window.

        Called with the camera lock not held, and never waits for it: the
        record has its own lock (_preview_record_lock), and the close is made
        here only if the camera lock can be taken at once. When it cannot,
        the record is left spanning the window and whoever next takes the
        lock in capture_preview() closes the session
        (_close_session_if_run_due).
        """
        if not self._note_preview_failure(camera_index, session):
            return
        lock = self._get_camera_lock(camera_index)
        if not lock.acquire(blocking=False):
            return
        try:
            self._close_session_if_run_due(camera_index)
        finally:
            lock.release()

    def _close_session_if_run_due(self, camera_index: int):
        """Close the cached session if its recorded run of failed polls spans the window.

        Caller holds the camera lock. A run spans the window when its first
        and last failures are _ROUTINE_SCAN_MIN_S or more apart; a record
        whose last failure is more than _PREVIEW_FAILURE_GAP_S old is not
        acted on, as a new failure would start a new run. A record for a
        session that is no longer the cached one closes nothing.
        """
        now = _monotonic()
        with self._preview_record_lock:
            recorded = self._preview_failing_since.get(camera_index)
            if recorded is None:
                return
            session, first, last = recorded
            if (
                self._sessions.get(camera_index) is not session
                or last - first < _ROUTINE_SCAN_MIN_S
                or now - last > _PREVIEW_FAILURE_GAP_S
            ):
                return
            del self._preview_failing_since[camera_index]
        self.logger.error(
            f"[gphoto2] Camera {camera_index}: every preview frame has failed "
            f"for {last - first:.0f}s; closing the session"
        )
        self._close_session(camera_index)

    def get_dslr_settings(self, camera_index: int) -> dict:
        """Read current DSLR settings from the open PTP session.

        Returns a dict with keys:
            iso (str | None), shutter_speed (str | None), aperture (str | None),
            image_format (str | None), focus_mode (str | None),
            flash_mode (str | None), stored_image_format (str | None)

        image_format is what the camera holds; stored_image_format is the
        format stored for the body (see capture_image_format), None when
        nothing is stored or the body's identity is unknown or ambiguous.
        The stored format is the one the session read at open, so no
        registry file is read here.
        """
        lock = self._get_camera_lock(camera_index)
        with lock:
            session = self._get_or_open_session(camera_index)
            try:
                raw = session.get_info()
                _forget_imageformat_if_changed(session, raw.get("imageformat"))
                return self._settings_dict(camera_index, session, raw)
            except gp.GPhoto2Error as exc:
                self.logger.error(
                    f"[gphoto2] Camera {camera_index}: get_dslr_settings failed: {exc}"
                )
                raise RuntimeError(f"Failed to read DSLR settings: {exc}") from exc

    def apply_dslr_settings(self, camera_index: int, settings: dict) -> dict:
        """Apply a partial dict of DSLR settings via PTP and return updated values.

        Accepted keys (all optional):
            iso (str): PTP iso value e.g. "400"
            shutter_speed (str): PTP shutterspeed e.g. "1/125"
            aperture (str): PTP aperture e.g. "5.6"
            image_format (str): "JPEG" or "RAW+JPEG" ("RAW", the old name,
                is taken as "RAW+JPEG")

        An image_format is kept only when the camera holds it and it is
        stored for the body. The camera's imageformat is read before the
        write; after the write it is read back, and the format is stored
        only when the read-back is the value written. In every other outcome
        (the write refused, the read-back unreadable or different, the store
        or the body's registration failing) the camera is set back to the
        value read before the request, the session's record of the format it
        set is cleared, and RuntimeError is raised, naming the state the
        camera is in as read after the set-back. The format is handled
        before ISO, shutter speed and aperture, so a format that is not kept
        leaves those three unwritten. A body whose identity is unknown or ambiguous has the
        format set on the camera only: nothing is stored, a warning is
        logged, and the settings are returned with stored_image_format None;
        when its camera does not take the value (the write refused, the
        read-back unreadable or different) it is set back as above. ISO,
        shutter speed and aperture are set as they always were.

        Returns the full settings dict (same shape as get_dslr_settings) after
        applying the requested changes.
        """
        asked = settings.get("image_format")
        wanted = normalise_image_format(asked) if asked is not None else None
        if asked is not None and wanted is None:
            raise RuntimeError(
                f"A DSLR captures {' or '.join(IMAGE_FORMATS)}, not {asked!r}"
            )
        lock = self._get_camera_lock(camera_index)
        with lock:
            session = self._get_or_open_session(camera_index)
            hardware_id = None
            if wanted is not None:
                hardware_id = self._body_hardware_id(camera_index, session)
            # The format goes first: when it is not kept, RuntimeError is
            # raised here and nothing else is written. _get_config and
            # _set_config answer a PTP error with None and False.
            if wanted is not None:
                before = session._get_config("imageformat")
                took = _set_imageformat(session, _ptp_image_format(wanted))
                self._keep_chosen_format(
                    camera_index, session, hardware_id, wanted, before, took,
                    session._get_config("imageformat"),
                )
            try:
                if settings.get("iso") is not None:
                    session._set_config("iso", str(settings["iso"]))
                if settings.get("shutter_speed") is not None:
                    session._set_config("shutterspeed", settings["shutter_speed"])
                if settings.get("aperture") is not None:
                    session._set_config("aperture", settings["aperture"])
                raw = session.get_info()
                _forget_imageformat_if_changed(session, raw.get("imageformat"))
            except gp.GPhoto2Error as exc:
                self.logger.error(
                    f"[gphoto2] Camera {camera_index}: apply_dslr_settings failed: {exc}"
                )
                raise RuntimeError(f"Failed to apply DSLR settings: {exc}") from exc
            return self._settings_dict(camera_index, session, raw)

    def _keep_chosen_format(
        self, camera_index, session, hardware_id, wanted, before, took, held
    ) -> None:
        """Store a format the settings route has set, or set the camera back.

        Called with the camera lock held. Returns only when the camera holds
        the format (`held`, the value read back, is the value written) and it
        is stored, or, for a body with no identity (hardware_id None), when
        the camera holds it; otherwise the camera is set back to `before` and
        RuntimeError is raised.
        """
        widget = _ptp_image_format(wanted)
        if not took:
            problem = "the camera refused it"
        elif held is None:
            problem = "the camera's format could not be read back"
        elif held != widget:
            problem = f"the camera reports imageformat={held!r}"
        elif hardware_id is None:
            self.logger.warning(
                f"[gphoto2] Camera {camera_index}: capture format {wanted} is "
                "held by the camera only; the body has no identity of its own, "
                "so nothing is stored for it"
            )
            return
        else:
            try:
                self._write_stored_format(hardware_id, wanted, camera_index, session)
                self.logger.info(
                    f"[gphoto2] Camera {camera_index}: capture format {wanted} "
                    f"stored for {hardware_id}"
                )
                return
            except Exception as exc:
                problem = f"it was not stored ({exc})"
        if before is None:
            state = (
                "the camera could not be set back, because its format before "
                f"the request could not be read; it reports imageformat={held!r}"
            )
        else:
            # The write's success says only that it was sent; the camera is
            # read again to say what it holds.
            session._set_config("imageformat", before)
            now = session._get_config("imageformat")
            if now == before:
                state = f"the camera was set back to imageformat={before!r}"
            elif now is None:
                state = (
                    f"the camera could not be set back to imageformat={before!r}; "
                    "its format could not be read"
                )
            else:
                state = (
                    f"the camera could not be set back to imageformat={before!r}; "
                    f"it holds imageformat={now!r}"
                )
        session.imageformat_set = None
        message = f"Capture format {wanted} was not kept: {problem}; {state}"
        self.logger.error(f"[gphoto2] Camera {camera_index}: {message}")
        raise RuntimeError(message)

    def _settings_dict(self, camera_index: int, session, raw: dict) -> dict:
        return {
            "iso": raw.get("iso"),
            "shutter_speed": raw.get("shutterspeed"),
            "aperture": raw.get("aperture"),
            "image_format": _IMAGE_FORMAT_REVERSE_MAP.get(
                raw.get("imageformat", ""), raw.get("imageformat")
            ),
            "focus_mode": raw.get("focusmode"),
            "flash_mode": raw.get("flashmode"),
            "stored_image_format": self._session_stored_format(camera_index, session),
        }

    # ------------------------------------------------------------------
    # Stored capture format
    # ------------------------------------------------------------------

    # The record capture routes put no stored format into a gphoto2
    # capture's configuration: capture_image resolves it under the camera
    # lock (CameraBackend.resolves_capture_format).
    resolves_capture_format = True

    def capture_image_format(self, camera_index: int) -> "str | None":
        """The capture format stored for the body the open session holds, or None.

        None when no session is open at the index, when nothing is stored or
        the registry could not be read at the session's open, and for a body
        whose identity is unknown or ambiguous. No registry file is read.
        """
        return self._session_stored_format(
            camera_index, self._sessions.get(camera_index)
        )

    def _session_stored_format(self, camera_index: int, session) -> "str | None":
        if session is None or self._body_hardware_id(camera_index, session) is None:
            return None
        return getattr(session, "stored_format", None)

    def _body_hardware_id(self, camera_index: int, session) -> "str | None":
        """The hardware id of the body `session` holds at this index, or None.

        The serial is the one the session read at open. None for a body that
        answered no serial; for one whose serial is not the one pinned at
        this index, when a pin exists; and for an ambiguous one: its serial
        is pinned at another index, or cached for another port the published
        map holds.
        """
        if session is None:
            return None
        serial = getattr(session, "serial", "") or ""
        model = getattr(session, "model", None)
        port = getattr(session, "port", None)
        if not serial or not isinstance(model, str):
            return None
        with self._map_lock:
            pinned = self._pins.get(camera_index)
            if pinned is not None and pinned != serial:
                return None
            if any(i != camera_index and s == serial for i, s in self._pins.items()):
                return None
            published = {p for _m, p in self._port_map.values()}
            if any(
                p != port and s == serial and p in published
                for p, s in self._serial_by_port.items()
            ):
                return None
        return _hardware_id(model, serial)

    def _read_stored_format(self, hardware_id: str) -> "tuple[bool, str | None, object]":
        """(readable, format, registry) for the body, read from the file now.

        readable is False when the registry cannot be opened, when it refuses
        writes (it then reads as empty, which says nothing about what is
        stored), and when it holds a value normalise_image_format does not
        recognise; nothing is then set at session open and nothing is
        adopted. A stored RAW, the old name, is read as RAW+JPEG. With no
        registry (the factory returns None) nothing is stored.
        """
        try:
            registry = self._registry_factory()
            if registry is None:
                return True, None, None
            refusal = getattr(registry, "write_refusal", None)
            if refusal:
                self.logger.warning(
                    f"[gphoto2] {hardware_id}: the camera registry cannot be used "
                    f"({refusal}); the camera's own format is left as it is"
                )
                return False, None, registry
            entry = registry.get_camera_by_id(hardware_id)
        except Exception as exc:
            self.logger.warning(
                f"[gphoto2] {hardware_id}: could not read the stored capture "
                f"format ({exc!r}); the camera's own format is left as it is"
            )
            return False, None, None
        role = entry.get("role") if entry else None
        with self._map_lock:
            for _index, (model, port) in self._port_map.items():
                serial = self._serial_by_port.get(port)
                if serial and _hardware_id(model, serial) == hardware_id:
                    if role in ("left", "right"):
                        self._roles_by_port[port] = (hardware_id, role)
                    else:
                        self._roles_by_port.pop(port, None)
        stored = entry.get("image_format") if entry else None
        if stored is None:
            return True, None, registry
        image_format = normalise_image_format(stored)
        if image_format is None:
            self.logger.warning(
                f"[gphoto2] {hardware_id}: the stored capture format {stored!r} "
                f"is not one of {', '.join(IMAGE_FORMATS)}; it is ignored and "
                "the camera's own format is left as it is"
            )
            return False, None, registry
        return True, image_format, registry

    def _body_info(self, camera_index: int, session) -> dict:
        port = getattr(session, "port", None)
        return {
            "model": getattr(session, "model", None),
            "serial": getattr(session, "serial", None) or None,
            "location": f"USB {port}",
            "id": port,
            "index": camera_index,
        }

    def _write_stored_format(
        self, hardware_id: str, image_format: str, camera_index: int, session
    ) -> None:
        """Store the format for the body, registering the body first when needed.

        A registry write that fails propagates, and the session's
        stored_format is then left as it was. With no registry the format is
        kept on the session only.
        """
        registry = self._registry_factory()
        if registry is None:
            self.logger.warning(
                f"[gphoto2] {hardware_id}: capture format {image_format} kept "
                "on the session only; the camera registry is not available"
            )
        else:
            if registry.get_camera_by_id(hardware_id) is None:
                registry.register_resolved(
                    hardware_id, self._body_info(camera_index, session), camera_index
                )
            registry.update_image_format(hardware_id, image_format)
        session.stored_format = image_format

    def _check_produced_format(self, camera_index: int, session, intended, produced):
        """Compare the file a capture produced with the format it was meant to take.

        Called with the camera lock held, after the download. When nothing
        was intended nothing is checked. When the file's type (`.cr2` or
        not) disagrees with the intended format, the session's record of the
        format it set is cleared, so the next capture writes the format
        again, and a warning is logged; the system log row to write is
        returned, for the caller to write once the lock is released.
        Otherwise None. The capture's file is kept either way.
        """
        if intended is None:
            return None
        meant_raw = _ptp_image_format(intended) == _IMAGE_FORMAT_MAP["RAW+JPEG"]
        made_raw = Path(produced).suffix.lower() == ".cr2"
        if meant_raw == made_raw:
            return None
        meant = normalise_image_format(intended) or "JPEG"
        made = "RAW+JPEG" if made_raw else "JPEG"
        session.imageformat_set = None
        detail = (
            f"camera {camera_index} was meant to capture {meant} and produced "
            f"{made} ({Path(produced).name}); the next capture sets the format "
            "again"
        )
        self.logger.warning(f"[gphoto2] Camera {camera_index}: {detail}")
        return {
            "level": "WARN",
            "action": "capture_format_mismatch",
            "subject": f"camera {camera_index}",
            "detail": detail,
        }

    def _set_stored_format_when_identified(self, camera_index: int, session) -> None:
        """Load and set the stored format for a session whose serial was read after open.

        Called with the camera lock held, by list_devices and
        _identify_unknown_ports, once they have given an open session the
        serial its open could not read. The camera's imageformat is read
        again first, since the value read at open may no longer hold; the
        step is then the one a session open runs, with the same
        no-write-when-equal and adoption rules.
        """
        try:
            session.opened_imageformat = session._get_config("imageformat")
        except Exception:
            session.opened_imageformat = None
        self._set_stored_format_at_open(camera_index, session)

    def _set_stored_format_at_open(self, camera_index: int, session) -> None:
        """Read the body's stored format and set it on a session that has just opened.

        Called from _open_identified_session, with the camera lock held, once
        the body has been confirmed as the one this index is for. This is the
        one registry read a session makes; the format read is kept as
        session.stored_format. When the camera already holds the stored
        format nothing is written and the session records it as set. When
        nothing is stored for a body whose identity is known, the format may
        be adopted from the camera (see _adopt_format). A body whose identity
        is unknown or ambiguous, or whose registry entry cannot be read, has
        nothing set. No failure here fails the open.
        """
        try:
            hardware_id = self._body_hardware_id(camera_index, session)
            if hardware_id is None:
                return
            readable, stored, _registry = self._read_stored_format(hardware_id)
            if not readable:
                return
            if stored is None:
                stored = self._adopt_format(camera_index, session, hardware_id)
                if stored is None:
                    return
            session.stored_format = stored
            widget = _ptp_image_format(stored)
            if getattr(session, "opened_imageformat", None) == widget:
                session.imageformat_set = widget
                return
            if _set_imageformat(session, widget):
                self.logger.info(
                    f"[gphoto2] Camera {camera_index}: capture format {stored} "
                    "set at session open"
                )
            else:
                self.logger.warning(
                    f"[gphoto2] Camera {camera_index}: the stored capture format "
                    f"{stored} could not be set at session open; the next "
                    "record capture sets it"
                )
        except Exception as exc:
            self.logger.warning(
                f"[gphoto2] Camera {camera_index}: the stored capture format was "
                f"not applied at session open ({exc!r})"
            )

    def _adopt_format(self, camera_index: int, session, hardware_id: str):
        """Store the format the camera held at session open; return the stored one.

        After a unit's first session with a body whose camera holds "L" or
        "RAW", the unit owns that body's format: it is stored, and a format
        later changed on the camera's own menu is set back at the next
        session open or after the next capture that produces the other file
        type. Only a widget value Captua itself sets for a format is adopted:
        the values of _IMAGE_FORMAT_MAP, "L" for JPEG and "RAW" for
        RAW+JPEG. Any other value ("RAW + L", another JPEG size, anything
        unknown) is logged and not adopted: nothing is stored and nothing is
        set, and the camera's own format governs until a format is chosen.

        The entry is read again inside the registry's write lock
        (CameraRegistry.adopt_image_format): when it already holds a format,
        that format is returned and nothing is written. Returns None when
        nothing is stored.
        """
        held = getattr(session, "opened_imageformat", None)
        if held is None:
            self.logger.info(
                f"[gphoto2] Camera {camera_index}: no capture format is stored "
                "and the camera's could not be read; nothing is stored"
            )
            return None
        adopted = next(
            (name for name, widget in _IMAGE_FORMAT_MAP.items() if widget == held),
            None,
        )
        if adopted is None:
            self.logger.warning(
                f"[gphoto2] Camera {camera_index}: the camera holds "
                f"imageformat={held!r}, which is not the value Captua sets for "
                f"{' or '.join(IMAGE_FORMATS)}; nothing is stored, and the "
                "camera's own format is used until a format is chosen"
            )
            return None
        try:
            registry = self._registry_factory()
            if registry is None:
                return None
            value = registry.adopt_image_format(
                hardware_id, adopted, self._body_info(camera_index, session),
                camera_index,
            )
        except Exception as exc:
            self.logger.warning(
                f"[gphoto2] Camera {camera_index}: the camera's capture format "
                f"{adopted} could not be stored ({exc!r}); it is adopted at the "
                "next session open"
            )
            return None
        stored = normalise_image_format(value)
        if value != adopted:
            if stored is None:
                self.logger.warning(
                    f"[gphoto2] {hardware_id}: the stored capture format "
                    f"{value!r} is not one of {', '.join(IMAGE_FORMATS)}; it is "
                    "ignored and the camera's own format is left as it is"
                )
            return stored
        self.logger.info(
            f"[gphoto2] Camera {camera_index}: adopted the camera's capture "
            f"format {adopted} (imageformat={held!r}) as the stored format "
            f"for {hardware_id}"
        )
        return adopted

    def supports_streaming(self) -> bool:
        return False

    def supports_live_adjustment(self) -> bool:
        return False

    def get_capabilities(self) -> dict:
        return {
            "live_preview": True,
            "continuous_preview": False,
            "focus_control": False,
            "live_controls": False,
            "zoom": False,
            "zoom_steps": False,
            "focus_lock": False,
            "autofocus_calibration": False,
            "dslr_settings": True,
            "image_formats": list(IMAGE_FORMATS),
        }

    def get_backend_name(self) -> str:
        return "gphoto2"

    def held_body_count(self) -> "int | None":
        """How many bodies the port map holds; no bus scan, no session, no wait.

        _map_lock is held across an autodetect, so the lock is only tried:
        when it is taken, the count last published with the map is given
        (None before the first).
        """
        if not self._map_lock.acquire(blocking=False):
            return getattr(self, "_published_body_count", None)
        try:
            count = len(self._port_map)
            self._published_body_count = count
            return count
        finally:
            self._map_lock.release()

    def cleanup(self):
        """Close all open PTP sessions and forget every learned identity."""
        for idx in list(self._sessions.keys()):
            try:
                self._close_session(idx)
            except Exception as exc:
                self.logger.warning(
                    f"[gphoto2] cleanup: error closing session {idx}: {exc}"
                )
        with self._map_lock:
            # In-memory state only. The bindings file is deliberately left
            # exactly as it is: it is what the next run seeds from, and a
            # shutdown is not the operator telling us they have re-cabled the
            # rig. Rewriting or deleting it here would throw away the left/
            # right assignment on every restart, which is the whole point of
            # persisting it.
            self._pins.clear()
            self._serial_by_port.clear()
            self._body_model_by_port.clear()
            self._roles_by_port.clear()
            self._seeded_serials.clear()
        self.logger.info("[gphoto2] All sessions closed.")
