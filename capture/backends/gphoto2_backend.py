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

Future hooks (wire up when DSLRCameraConfig is introduced):
  - ISO control via `iso` PTP widget
  - Shutter speed control via `shutterspeed` PTP widget
  - Aperture control via `aperture` PTP widget
"""

import json
import threading
import time
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

from .base import CameraBackend
from ..utils import atomic_write


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


# Image format mapping: CameraConfig.image_format -> PTP imageformat widget value
_IMAGE_FORMAT_MAP = {
    "JPEG":     "L",
    "RAW":      "RAW",
    "RAW+JPEG": "RAW + L",
}
# Default when image_format is None
_IMAGE_FORMAT_DEFAULT = "L"
# Reverse mapping: PTP widget value -> user-facing label
_IMAGE_FORMAT_REVERSE_MAP = {v: k for k, v in _IMAGE_FORMAT_MAP.items()}

# Minimum settle time used as retry_delay in capture (seconds).
# Canon EOS 1500D needs ~3s between shots for reliable PTP operation.
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


class CaptureTimeoutError(RuntimeError):
    """No image arrived from the camera before the capture deadline."""


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


def _capture_deadline_s(exposure: float) -> float:
    """Deadline in seconds for one capture at the given exposure.

    The exposure is counted twice: once for the shutter being open, once for
    the post-exposure processing libgphoto2 budgets on top of it - it allows
    30 s of noise reduction after a 30 s exposure.
    """
    return _CAPTURE_TIMEOUT_BASE_S + 2 * exposure


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
        self._cam = None
        # Focus mode as read at session open; named in capture timeout messages.
        self._focus_mode = None
        # Body serial as read at session open; "" when the body does not
        # answer one. This is what pins the session's index to a body.
        self.serial = ""
        self._open()

    def _open(self):
        abilities_list = gp.CameraAbilitiesList()
        abilities_list.load()
        self._cam = gp.Camera()
        abilities_idx = abilities_list.lookup_model(self.model)
        self._cam.set_abilities(abilities_list[abilities_idx])

        port_info_list = gp.PortInfoList()
        port_info_list.load()
        port_idx = port_info_list.lookup_path(self.port)
        self._cam.set_port_info(port_info_list[port_idx])

        self._cam.init()
        self._logger.info(f"[gphoto2] Opened session: {self.model} on {self.port}")
        # Run post-init config; on any error release the USB claim so the
        # next attempt isn't blocked by a half-open session.
        try:
            self._apply_speed_preset()
            self._disable_autopoweroff()
            self._warn_if_af()
            self._enforce_flash_off()
            # Read once, here: the identity of the body on this port is fixed
            # for the life of the session, and every later use of it happens
            # without a second PTP round trip.
            serial = self._get_config("serialnumber")
            self.serial = str(serial).strip() if serial is not None else ""
        except Exception as exc:
            self._logger.warning(
                f"[gphoto2] {self.port}: error during session init ({exc}), releasing device"
            )
            try:
                self._cam.exit()
            except Exception:
                pass
            raise

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _set_config(self, key: str, value) -> bool:
        try:
            cfg = self._cam.get_config()
            widget = cfg.get_child_by_name(key)
            widget.set_value(value)
            self._cam.set_config(cfg)
            return True
        except (gp.GPhoto2Error, TypeError, ValueError):
            return False

    def _get_config(self, key: str):
        try:
            cfg = self._cam.get_config()
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

    def apply_dslr_config(self, camera_config) -> None:
        """Apply DSLR-specific CameraConfig fields to the camera via PTP.

        Called before each capture so settings reflect the current config.
        Fields that are None are left at their current camera value.
        Flash is always enforced off here as an archival safety guard.
        """
        # -- Flash guard: must be first, before shutter opens ----------
        self._enforce_flash_off()

        fmt = getattr(camera_config, "image_format", None)
        if fmt is not None:
            ptp_fmt = _IMAGE_FORMAT_MAP.get(fmt, _IMAGE_FORMAT_DEFAULT)
            self._set_config("imageformat", ptp_fmt)
        # If fmt is None, leave the camera's current imageformat unchanged

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
                self._cam.trigger_capture()
                return None
            except gp.GPhoto2Error as exc:
                err = str(exc).lower()
                retriable = (
                    "-110" in err
                    or "i/o in progress" in err
                    or "busy" in err
                )
                if not retriable:
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
        start = time.monotonic()
        while time.monotonic() - start < window_s:
            event_type, event_data = self._cam.wait_for_event(_EVENT_STEP_MS)
            if event_type == gp.GP_EVENT_FILE_ADDED:
                return event_data
        return None

    def _wait_for_file_added(self, deadline_s: float, shutterspeed=None):
        """Poll the camera's event queue until a file lands, or the deadline passes.

        Returns the gp.CameraFilePath (.folder / .name) carried by the
        GP_EVENT_FILE_ADDED event. Every other event - capture complete,
        timeout, unknown - just continues the loop. The deadline is checked
        between wait_for_event() calls, each of which is bounded by
        libgphoto2's own USB timeouts, and only after a call comes back
        without a file. A file that arrives after the deadline has already
        passed is still returned, never treated as a timeout - the check
        only ever runs on an empty queue, and a delivered image is a good
        capture regardless of when it lands. The deadline bounds how long an
        empty queue is waited on, not whether a delivered file is accepted.

        Errors raised by wait_for_event() propagate untouched: the shutter has
        already fired by this point, so re-triggering would take a second
        photograph rather than recover the first.
        """
        start = time.monotonic()
        while True:
            event_type, event_data = self._cam.wait_for_event(_EVENT_STEP_MS)
            if event_type == gp.GP_EVENT_FILE_ADDED:
                return event_data
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

    def capture(
        self,
        outpath: Path,
        retry: int = _DEFAULT_RETRY,
        retry_delay: float = _DEFAULT_SETTLE,
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
        """
        outpath.parent.mkdir(parents=True, exist_ok=True)
        shutterspeed = self._get_config("shutterspeed")
        deadline_s = _capture_deadline_s(_parse_exposure_seconds(shutterspeed))

        t0 = time.perf_counter()
        file_path = self._trigger_with_retry(retry, retry_delay, deadline_s)
        if file_path is None:
            file_path = self._wait_for_file_added(deadline_s, shutterspeed)

        # The captured filename tells us the actual format (.cr2 vs .jpg)
        is_raw = file_path.name.lower().endswith(".cr2")
        if is_raw:
            # Save CR2 with .cr2 extension regardless of outpath stem
            actual_outpath = outpath.with_suffix(".cr2")
        else:
            actual_outpath = outpath

        camera_file = self._cam.file_get(
            file_path.folder, file_path.name, gp.GP_FILE_TYPE_NORMAL
        )
        # Durable save: temp + fsync + atomic replace, so no partial master survives a crash
        atomic_write(actual_outpath, lambda tmp: camera_file.save(tmp))
        self._cam.file_delete(file_path.folder, file_path.name)
        elapsed = time.perf_counter() - t0

        # Extract embedded JPEG from CR2 for thumbnail/review pipeline
        if is_raw:
            _write_raw_preview(
                actual_outpath,
                actual_outpath.with_name(actual_outpath.stem + "_preview.jpg"),
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
            try:
                self._cam.exit()
            except Exception:
                pass
            self._cam = None
            self._logger.info(f"[gphoto2] Closed session: {self.model} on {self.port}")


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
                "python-gphoto2 is not installed. "
                "Add it to pixi.toml with: pixi add python-gphoto2"
            )
        super().__init__(logger)
        # camera_index -> (model_name, usb_port)
        self._port_map: dict[int, tuple[str, str]] = {}
        # camera_index -> body serial, for the life of the process and, via
        # the bindings file, across restarts
        self._pins: dict[int, str] = {}
        # the serials seeded from the bindings file, until the first complete
        # identification settles whether any of those bodies is still here
        self._seeded_serials: set[str] = set()
        # usb port -> body serial, as last read on that port
        self._serial_by_port: dict[str, str] = {}
        # the last autodetect() result, so the map can be rebuilt against new
        # identity knowledge without a second USB enumeration
        self._last_detected: list[tuple[str, str]] = []
        # camera_index -> open _PTPSession
        self._sessions: dict[int, _PTPSession] = {}
        # usb port -> the index that currently holds a PTP claim on it. This
        # is the record of what is claimed *now*, which self._sessions is not:
        # a session claims its body before it is stored and is popped before
        # its exit() returns, and a brief read never appears there at all.
        self._claimed_ports: dict[str, int] = {}
        # per-camera lock for capture serialisation
        self._session_locks: dict[int, threading.Lock] = {}
        self._map_lock = threading.Lock()
        # serialises rescan() so overlapping snapshots publish in order
        self._rescan_lock = threading.Lock()
        # serialises saves of the bindings file, snapshot and write together
        self._save_lock = threading.Lock()
        self._load_pins()

    # ------------------------------------------------------------------
    # Persistent index -> body bindings
    # ------------------------------------------------------------------

    def _bindings_path(self) -> "Path | None":
        """Where the bindings live, or None when there is nowhere to keep them.

        The import is function-local, exactly as camera_registry does it: this
        module is reached from capture.service, so a module-level import of the
        app config would be circular. When it fails - a test process with no
        app config - persistence is simply off.
        """
        try:
            from app.core.config import settings

            return Path(settings.projects_dir) / _BINDINGS_FILENAME
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
        identification puts them back where they belong. That is the path this
        backend already takes after a power-cycle; nothing here is new.

        A missing file is the normal first-run case. A file that cannot be read
        or does not hold the expected shape is reported and ignored: bad
        bindings must degrade to a positional map, never to an exception on a
        camera backend that is about to be asked for a preview.
        """
        path = self._bindings_path()
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pins = {}
            for key, serial in data["pins"].items():
                if not isinstance(serial, str) or not serial:
                    raise ValueError(f"binding {key!r} has no serial")
                pins[int(key)] = serial
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
        not be read means nothing is dropped (R23-4), because an unreadable
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
        result is exactly the positional map this backend published before
        identities were tracked.

        Ports the given detection does not report are dropped from the serial
        cache: a usb devnum is not reused until the bus counter wraps, so a
        port that is gone carries no information about the body that left it.

        The pins and the cache are passed in rather than read from self,
        because this is the single builder for the map and every caller of it
        already holds _map_lock; taking that lock here would deadlock. Passing
        the live dicts (not copies) is deliberate - the cache prune above is a
        write the callers want to keep. `detected` is the autodetect result to
        lay out; when it is None a fresh autodetect() runs and is remembered,
        so a caller that only wants the same detection re-laid against new
        identity knowledge can pass it back in.
        """
        if detected is None:
            raw = gp.Camera.autodetect()
            detected = [(raw[i][0], raw[i][1]) for i in range(len(raw))]
            self._last_detected = list(detected)

        present = {port for _model, port in detected}
        for port in list(serial_by_port):
            if port not in present:
                del serial_by_port[port]

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
            self.logger.info(f"[gphoto2] Detected camera {index}: {model} on {port}")
        if not port_map:
            self.logger.warning("[gphoto2] No cameras detected by autodetect().")
        return port_map

    def _refresh_port_map(self):
        with self._map_lock:
            self._port_map = self._build_port_map(self._pins, self._serial_by_port)

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

    def _get_port_map(self) -> dict[int, tuple[str, str]]:
        with self._map_lock:
            if not self._port_map:
                self._port_map = self._build_port_map(
                    self._pins, self._serial_by_port
                )
            return dict(self._port_map)

    def _learn_serial(self, camera_index: int, port: str, serial: str) -> bool:
        """Record that `port` answered `serial`, and pin the index if it is free.

        Two separate facts are written here. The cache entry (port -> serial)
        is what keeps a body on its index across refreshes while its port
        lives. The pin (index -> serial) is what keeps it on that index after
        the port has gone.

        A pin is written on the *first* serial read at an index, not after some
        later confirmation: an index that has been used but left unpinned is an
        index a different body can silently be published on, which is exactly
        the substitution this work exists to prevent. The pin is refused only
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
                self._session_locks[camera_index] = threading.Lock()
            return self._session_locks[camera_index]

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _session_matches_map(self, camera_index: int) -> "_PTPSession | None":
        """Return the cached session for camera_index if the current map still backs it.

        Reads the port map fresh on every call - via self._get_port_map() -
        rather than accepting a map the caller copied earlier, because
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
        entry = self._get_port_map().get(camera_index)
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

    def _take_port(self, camera_index: int, port: str) -> "int | None":
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
        otherwise the index still holding the port.
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
            self._session_matches_map(holder)
        finally:
            other_lock.release()

        return self._claim_port(camera_index, port)

    def _open_identified_session(
        self, camera_index: int, model: str, port: str
    ) -> _PTPSession:
        """Open a session on `port` and hand it back only if it is the right body.

        The identity check runs before the session is cached, so a body that
        contradicts the pin never becomes the session an index serves.
        """
        held_by = self._take_port(camera_index, port)
        if held_by is not None:
            raise RuntimeError(
                f"Camera {camera_index}: port {port} is still held by index "
                f"{held_by} while the cameras are being reconciled; retry"
            )

        try:
            session = _PTPSession(port, model, self.logger)
        except BaseException:
            # No session came back, so nothing else will ever release this
            # claim - including the -105 path, which comes straight back here
            # for a second attempt.
            self._release_port(camera_index, port)
            raise
        # From here the session owns the claim; _close_session gives it back.
        serial = getattr(session, "serial", "") or ""
        if not serial:
            # Documented limitation: a body that does not answer a serial
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
            return session

        # The body is not the one this index is for. Release the claim first,
        # then publish what was learned, then republish the map so the body
        # turns up at the index it belongs to.
        session.close()
        self._release_port(camera_index, port)
        self._learn_serial(camera_index, port, serial)
        self._refresh_port_map()
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
            with self._get_camera_lock(camera_index):
                session = self._session_matches_map(camera_index)
                if session is not None:
                    serial = getattr(session, "serial", "") or ""
                    read_port = session.port
                    if not serial:
                        # The read at session open failed, or this session
                        # predates serials being read at all. Ask the body
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
                else:
                    read_port = port
                    if self._get_port_map().get(camera_index) != (model, port):
                        # The map moved on while this loop was waiting for the
                        # lock; opening (model, port) would claim a port
                        # nothing backs any more.
                        continue
                    held_by = self._take_port(camera_index, port)
                    if held_by is not None:
                        # Another index still holds a claim on this port.
                        # Identification is never worth a second claim on a
                        # body: leave the port unidentified and let the next
                        # pass, after that claim is gone, read it.
                        self.logger.info(
                            f"[gphoto2] Camera {camera_index}: port {port} is "
                            f"still held by index {held_by}; leaving it "
                            "unidentified for now"
                        )
                        continue
                    cam = None
                    initialized = False
                    try:
                        al = gp.CameraAbilitiesList()
                        al.load()
                        cam = gp.Camera()
                        cam.set_abilities(al[al.lookup_model(model)])
                        pil = gp.PortInfoList()
                        pil.load()
                        cam.set_port_info(pil[pil.lookup_path(port)])
                        cam.init()
                        initialized = True
                        cfg = cam.get_config()
                        serial = cfg.get_child_by_name("serialnumber").get_value().strip()
                    except Exception:
                        serial = ""
                    finally:
                        # init() may have succeeded even if the read raised;
                        # the claim is released either way, and only then is
                        # anything published from it.
                        if initialized:
                            try:
                                cam.exit()
                            except Exception:
                                pass
                        self._release_port(camera_index, port)
                if serial:
                    learned = self._learn_serial(camera_index, read_port, serial) or learned
                else:
                    self.logger.info(
                        f"[gphoto2] Camera {camera_index}: no serial could be "
                        f"read from {port}; it stays provisional"
                    )
        return learned

    def _get_or_open_session(self, camera_index: int) -> _PTPSession:
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
        """
        existing = self._session_matches_map(camera_index)
        if existing is not None:
            return existing

        port_map = self._get_port_map()
        if camera_index not in port_map:
            raise RuntimeError(
                f"Camera index {camera_index} not found. "
                f"Detected indices: {sorted(port_map.keys())}. "
                "Ensure the camera is on and USB is connected, then try again."
            )

        model, port = port_map[camera_index]
        try:
            session = self._open_identified_session(camera_index, model, port)
            self._sessions[camera_index] = session
            return session
        except gp.GPhoto2Error as exc:
            # -105 Unknown model usually means ports changed (USB re-enumeration
            # after a power-cycle). Refresh the port map once and retry.
            if "-105" in str(exc) or "Unknown model" in str(exc):
                self.logger.warning(
                    f"[gphoto2] Camera {camera_index}: session init failed ({exc}). "
                    "Refreshing port map and retrying ..."
                )
                self._refresh_port_map()
                port_map = self._get_port_map()
                if camera_index not in port_map:
                    raise RuntimeError(
                        f"Camera index {camera_index} not found after port map refresh."
                    ) from exc
                model, port = port_map[camera_index]
                session = self._open_identified_session(camera_index, model, port)
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

    def is_camera_connected(self, camera_index: int = 0) -> bool:
        """Is a body present at this index right now?

        Always re-detects, so a power-cycle since the last call is reflected.
        A body that came back on a new port is unidentified at that point, so
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
        """
        try:
            # Always do a fresh detection so this method reflects reality even
            # if cameras have been power-cycled since last call.
            self._refresh_port_map()
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
            return camera_index in self._get_port_map()
        except Exception as exc:
            self.logger.error(f"[gphoto2] is_camera_connected({camera_index}): {exc}")
            return False

    def capture_image(
        self,
        output_path: Path,
        camera_config,
        capture_output: bool = False,
    ) -> str:
        """Capture one image to output_path. Returns path as str.

        Most CameraConfig fields (awb, vflip, lens_position, etc.) are
        picamera2-specific and are silently ignored here. DSLR-specific
        controls (ISO, shutter speed, aperture) will be wired up once a
        dedicated DSLRCameraConfig is introduced.

        Args:
            output_path: Destination path for the captured JPEG.
            camera_config: CameraConfig (camera_index used for routing).
            capture_output: Unused - kept for interface compatibility.

        Returns:
            Absolute path string to the saved image file.

        Raises:
            RuntimeError: If the capture fails.
        """
        camera_index = getattr(camera_config, "camera_index", 0)
        lock = self._get_camera_lock(camera_index)

        with lock:
            session = self._get_or_open_session(camera_index)
            try:
                session.apply_dslr_config(camera_config)
                elapsed, actual_path = session.capture(output_path)
                self.logger.info(
                    f"[gphoto2] Camera {camera_index}: captured {actual_path.name} "
                    f"in {elapsed:.2f}s"
                )
                return str(actual_path), None
            except (gp.GPhoto2Error, CaptureTimeoutError) as exc:
                if isinstance(exc, CaptureTimeoutError):
                    self.logger.error(
                        f"[gphoto2] Camera {camera_index}: capture timed out "
                        f"({exc}); closing session"
                    )
                    # Closing an EOS session runs several PTP operations, each
                    # bounded by libgphoto2's own USB timeouts; on a wedged body
                    # they are slow, so the elapsed time is logged.
                    t_close = time.perf_counter()
                    self._close_session(camera_index)
                    self.logger.error(
                        f"[gphoto2] Camera {camera_index}: session closed after "
                        f"timeout in {time.perf_counter() - t_close:.1f}s"
                    )
                else:
                    self.logger.error(
                        f"[gphoto2] Camera {camera_index}: capture failed: {exc}"
                    )
                    # Close the failed session so the next call re-opens it cleanly.
                    self._close_session(camera_index)
                raise RuntimeError(f"DSLR capture failed: {exc}") from exc

    def list_devices(self) -> list:
        """Enumerate all connected DSLR cameras and return device metadata.

        Uses the persistent port map (refreshed if empty) to pick the set of
        indices to look at, but every session match is against the map as
        currently published (see _session_matches_map), not this call's
        snapshot - a publish can land while this call is paused on a
        per-camera lock. For each camera, re-uses an already-open PTP session
        to read the serial number; if no session is open yet, opens a brief
        one just for the read and closes it. A cached session is re-used only
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
        if differs:
            result, _ = self._enumerate_port_map(rebuilt)
        return result

    def _enumerate_port_map(self, port_map) -> tuple[list, bool]:
        """One enumeration pass over `port_map`; returns (rows, learned_anything)."""
        import re

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
                    try:
                        cfg = session._cam.get_config()
                        serial = cfg.get_child_by_name("serialnumber").get_value().strip()
                    except Exception:
                        pass
                    if serial:
                        # Learn it here too. A body whose read failed at
                        # session open has no pin, and reporting a serial
                        # without learning it leaves the index unpinned and
                        # free for a later arrival to take. There is nothing
                        # to release first: the ordering rule (R23-2) is about
                        # a claim that is about to be dropped, and this
                        # session's claim lives on either way.
                        session.serial = serial
                        learned = self._learn_serial(
                            idx, session.port, serial
                        ) or learned
                else:
                    current_entry = self._get_port_map().get(idx)
                    if current_entry != (model_raw, port):
                        # The current map disagrees with this row's
                        # snapshot; opening against (model_raw, port) would
                        # claim a port nothing backs any more. Leave this row
                        # out - the next enumeration sees the current map.
                        continue
                    held_by = self._take_port(idx, port)
                    if held_by is not None:
                        # Another index still holds a claim on this port, so
                        # opening it here would be a second PTP claim on one
                        # body. Leave the row out.
                        self.logger.warning(
                            f"[gphoto2] Camera {idx}: port {port} is still held "
                            f"by index {held_by} while the cameras are being "
                            "reconciled; leaving this row out"
                        )
                        continue
                    # Nobody holds this camera, so a brief PTP connection just
                    # to read the serial number is not a competing claim.
                    cam = None
                    initialized = False
                    try:
                        al = gp.CameraAbilitiesList()
                        al.load()
                        cam = gp.Camera()
                        cam.set_abilities(al[al.lookup_model(model_raw)])
                        pil = gp.PortInfoList()
                        pil.load()
                        cam.set_port_info(pil[pil.lookup_path(port)])
                        cam.init()
                        initialized = True
                        cfg = cam.get_config()
                        serial = cfg.get_child_by_name("serialnumber").get_value().strip()
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
                                cam.exit()
                            except Exception:
                                pass
                        self._release_port(idx, port)
                    # R23-2: the claim is gone, so what it read may now be
                    # published.
                    if serial:
                        learned = self._learn_serial(idx, port, serial) or learned

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

            model_slug = re.sub(r"[^a-z0-9]", "", row_model.lower())
            hw_id = (
                f"{model_slug}_{serial}" if serial
                else f"{model_slug}_idx{idx}"
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
            })

        return result, learned

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

    def capture_preview(self, camera_index: int) -> bytes:
        """Return a live-preview JPEG frame from the camera.

        Uses the same persistent PTP session as full captures so there is no
        reconnect overhead. The first frame takes ~1.3s (camera warms up video
        subsystem); subsequent frames are ~30ms at ~33 fps.

        The per-camera lock serialises preview and full-capture calls so they
        never interleave on the same session.
        """
        lock = self._get_camera_lock(camera_index)
        with lock:
            session = self._get_or_open_session(camera_index)
            try:
                camera_file = session._cam.capture_preview()
                data = camera_file.get_data_and_size()
                return bytes(data)
            except gp.GPhoto2Error as exc:
                self.logger.error(
                    f"[gphoto2] Camera {camera_index}: preview capture failed: {exc}"
                )
                self._close_session(camera_index)
                raise RuntimeError(
                    f"DSLR preview capture failed: {exc}"
                ) from exc

    def get_dslr_settings(self, camera_index: int) -> dict:
        """Read current DSLR settings from the open PTP session.

        Returns a dict with keys:
            iso (str | None), shutter_speed (str | None), aperture (str | None),
            image_format (str | None), focus_mode (str | None), flash_mode (str | None)
        """
        lock = self._get_camera_lock(camera_index)
        with lock:
            session = self._get_or_open_session(camera_index)
            try:
                raw = session.get_info()
                return {
                    "iso": raw.get("iso"),
                    "shutter_speed": raw.get("shutterspeed"),
                    "aperture": raw.get("aperture"),
                    "image_format": _IMAGE_FORMAT_REVERSE_MAP.get(
                        raw.get("imageformat", ""), raw.get("imageformat")
                    ),
                    "focus_mode": raw.get("focusmode"),
                    "flash_mode": raw.get("flashmode"),
                }
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
            image_format (str): "JPEG", "RAW", or "RAW+JPEG"

        Returns the full settings dict (same shape as get_dslr_settings) after
        applying the requested changes.
        """
        lock = self._get_camera_lock(camera_index)
        with lock:
            session = self._get_or_open_session(camera_index)
            try:
                if settings.get("iso") is not None:
                    session._set_config("iso", str(settings["iso"]))
                if settings.get("shutter_speed") is not None:
                    session._set_config("shutterspeed", settings["shutter_speed"])
                if settings.get("aperture") is not None:
                    session._set_config("aperture", settings["aperture"])
                if settings.get("image_format") is not None:
                    ptp_fmt = _IMAGE_FORMAT_MAP.get(
                        settings["image_format"], _IMAGE_FORMAT_DEFAULT
                    )
                    session._set_config("imageformat", ptp_fmt)
                # Return updated state
                raw = session.get_info()
                return {
                    "iso": raw.get("iso"),
                    "shutter_speed": raw.get("shutterspeed"),
                    "aperture": raw.get("aperture"),
                    "image_format": _IMAGE_FORMAT_REVERSE_MAP.get(
                        raw.get("imageformat", ""), raw.get("imageformat")
                    ),
                    "focus_mode": raw.get("focusmode"),
                    "flash_mode": raw.get("flashmode"),
                }
            except gp.GPhoto2Error as exc:
                self.logger.error(
                    f"[gphoto2] Camera {camera_index}: apply_dslr_settings failed: {exc}"
                )
                raise RuntimeError(f"Failed to apply DSLR settings: {exc}") from exc

    def supports_streaming(self) -> bool:
        return False

    def supports_live_adjustment(self) -> bool:
        return False

    def get_capabilities(self) -> dict:
        return {
            "live_preview": True,
            "focus_control": False,
            "live_controls": False,
            "zoom": False,
            "autofocus_calibration": False,
            "dslr_settings": True,
        }

    def get_backend_name(self) -> str:
        return "gphoto2"

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
            self._seeded_serials.clear()
        self.logger.info("[gphoto2] All sessions closed.")
