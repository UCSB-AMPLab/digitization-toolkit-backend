"""Which kind of camera the unit drives: picamera2 (Pi cameras), gphoto2 (Canon DSLRs) or chdk (PowerShots running CHDK).

The kind is resolved once, in the lifespan, before the start-up guard
(capture.service.assert_camera_backend_configured), in this order:

1. the administrator's stored choice (PUT /system/camera-kind);
2. an explicit CAMERA_BACKEND in the process environment or an env file
   that names a driver this build has (capture.service.KNOWN_CAMERA_BACKENDS),
   passed on as written. A value that names none is logged, set aside, and
   reported (`ignored_environment`), and the kind is chosen as for an unset
   value, so the service starts (NEH-405);
3. detection: the kind of the cameras connected now, when there is
   exactly one kind;
4. the kind the unit last started with, else picamera2.

The kind resolved is written to CAMERA_BACKEND on the settings object
capture.service reads, and on app.core.config's when that is another
object, so the driver map, the camera registry and the routes that read
capture.service._backend all follow the same kind. The kind is
remembered as the last kind used only once its driver has been built
(driver_built, from capture.service.get_backend), and a kind whose driver
this unit cannot import is never the fallback. A backend built
without the lifespan (a hardware test, a script) with CAMERA_BACKEND unset
is resolved the same way by the guard itself.

Detection claims no device. libcamera's camera list
(Picamera2.global_camera_info) enumerates without acquiring a camera.
Canon bodies are found by the vendor and product ids of every USB
device's descriptor, as libusb copied it at enumeration, through pyusb;
no device is opened and no interface claimed. A Canon device with a
product id in CHDK_PRODUCT_IDS is a chdk body; any other Canon device is
a gphoto2 body. A probe that cannot run, or a scan in which any device
cannot be classified, gives unknown (None) counts, logged; detection
never raises.

GET /cameras/capabilities reports the same counts at request time
(report). For the kind in effect, a driver that holds its bodies (the
chdk and gphoto2 drivers, held_body_count) is asked how many it holds,
read from its own maps under its map lock; no camera lock is taken and no
session opened, so a capture or preview in progress is not disturbed.

The store is one JSON file, `camera-kind.json` in the data directory
(CAMERA_KIND_FILE replaces the path): {"choice": kind or null,
"last_used": kind or null}, written whole and atomically. A missing file
is no choice; a file that cannot be read or holds anything else is no
choice, logged, and never a failed start.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

PICAMERA2 = "picamera2"
GPHOTO2 = "gphoto2"
CHDK = "chdk"
# The kinds an administrator may choose, and that detection reports.
KINDS = (PICAMERA2, GPHOTO2, CHDK)
DEFAULT_KIND = PICAMERA2

STORED = "stored"
ENVIRONMENT = "environment"
DETECTED = "detected"
LAST_USED = "last_used"
DEFAULT = "default"

# USB product ids of the Canon bodies driven through CHDK. Only the
# PowerShot A2500 has been verified on hardware (capture.backends.chdk_backend,
# _MODEL_BY_PRODUCT_ID).
CHDK_PRODUCT_IDS = frozenset({0x3271})

STORE_NAME = "camera-kind.json"

# The kind and source resolved at this process's start, for the
# capabilities answer. None until the lifespan resolves it.
_in_effect: Optional[dict] = None

# Held across every read-modify-write of the store, so `choice` and
# `last_used` written at the same moment cannot overwrite each other.
_store_lock = threading.Lock()

# The reason last logged for each probe that could not run, so a probe
# failing on every request is logged once, and again only when its reason
# changes or after it has run.
_probe_failures: dict = {}


class Resolution(NamedTuple):
    kind: str
    source: str
    detection: Optional[dict]
    # The explicit CAMERA_BACKEND set aside because it names no driver, as written.
    ignored: Optional[str] = None


def _canonical(value) -> str:
    from capture.service import canonical_backend_name

    return canonical_backend_name(value)


def _capture_settings():
    from capture import service as capture_service

    return capture_service.settings


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

def store_path_for(settings) -> Path:
    configured = getattr(settings, "CAMERA_KIND_FILE", "") or ""
    return Path(configured) if configured else Path(settings.data_dir) / STORE_NAME


def store_path() -> Path:
    from app.core import config

    return store_path_for(config.settings)


def _valid_or_none(value, field: str, path: Path) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str) and value in KINDS:
        return value
    logger.warning("Camera-kind store %s has %s %r, which is not a camera kind; ignored", path, field, value)
    return None


def read_store() -> dict:
    """{"choice": kind or None, "last_used": kind or None}. Never raises."""
    path = store_path()
    empty = {"choice": None, "last_used": None}
    try:
        if not path.exists():
            return empty
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Camera-kind store %s could not be read (%s); no stored choice", path, type(exc).__name__)
        return empty
    if not isinstance(data, dict):
        logger.warning("Camera-kind store %s does not hold an object; no stored choice", path)
        return empty
    return {
        "choice": _valid_or_none(data.get("choice"), "choice", path),
        "last_used": _valid_or_none(data.get("last_used"), "last_used", path),
    }


def _write_store(data: dict) -> None:
    """Write the whole store: a temporary file of its own in the same directory, synced, then renamed over the store."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"choice": data.get("choice"), "last_used": data.get("last_used")}, indent=2)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def stored_choice() -> Optional[str]:
    return read_store()["choice"]


def last_used() -> Optional[str]:
    return read_store()["last_used"]


def store_choice(kind: str) -> None:
    """Store the administrator's choice. Raises when the file cannot be written."""
    if kind not in KINDS:
        raise ValueError(f"not a camera kind: {kind!r}")
    with _store_lock:
        data = read_store()
        data["choice"] = kind
        _write_store(data)


def clear_choice() -> None:
    """Remove the administrator's choice, keeping the last kind used. Raises when the file cannot be written."""
    with _store_lock:
        data = read_store()
        data["choice"] = None
        _write_store(data)


def record_last_used(kind: str) -> None:
    """Remember the kind this start uses. Logged, never raised, when it cannot be written."""
    if kind not in KINDS:
        return
    try:
        with _store_lock:
            data = read_store()
            if data["last_used"] == kind:
                return
            data["last_used"] = kind
            _write_store(data)
    except Exception as exc:
        logger.warning("The camera kind in use could not be remembered in %s (%s)", store_path(), type(exc).__name__)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class ProbeUnavailable(RuntimeError):
    """A probe that cannot run here (its library is missing): its count is unknown."""


def _libcamera_count() -> int:
    """How many cameras libcamera lists. ProbeUnavailable where picamera2 cannot be imported."""
    from capture.backends import picamera2_backend

    if not picamera2_backend._PICAMERA2_AVAILABLE or picamera2_backend.Picamera2 is None:
        raise ProbeUnavailable("picamera2 cannot be imported")
    return len(picamera2_backend.Picamera2.global_camera_info())


# Canon's USB vendor id (pychdk.usb_transport.CANON_VENDOR_ID).
CANON_VENDOR_ID = 0x04A9


def _real_canon_product_ids() -> list:
    """The USB product id of every attached Canon device, or an exception when any device cannot be classified.

    Every device on the bus is read, through pyusb with no filter: pyusb's
    own idVendor filter, and pychdk's find_ptp_devices, drop a device whose
    descriptor cannot be read instead of reporting it, which would measure
    an incomplete bus. idVendor and idProduct are the device descriptor
    pyusb copies when it builds each Device, from libusb's copy made at
    enumeration (libusb_get_device_descriptor); no device is opened and
    no string is read. A CHDK body counts on its product id alone. Any
    other Canon device counts, as a gphoto2 body, only when one of its
    interfaces is a Still Image interface (_has_still_image_interface), so
    a Canon scanner or printer is left out. A device that raises on any of
    these reads, or an enumeration that fails part way, raises here, so
    both Canon counts are unknown for this scan.
    """
    import usb.core

    ids = []
    for device in usb.core.find(find_all=True):
        if int(device.idVendor) != CANON_VENDOR_ID:
            continue
        product = int(device.idProduct)
        if product in CHDK_PRODUCT_IDS or _has_still_image_interface(device):
            ids.append(product)
    return ids


# USB interface class of a Still Image (PTP) device.
STILL_IMAGE_CLASS = 0x06


def _has_still_image_interface(device) -> bool:
    """Whether any interface of any configuration of `device` has class 0x06, read without opening it.

    pyusb's Configuration and Interface are built from the libusb device
    (libusb_get_config_descriptor on the device, not on a handle), so no
    handle is opened. Each is built directly rather than by iterating the
    configuration, whose iterator swallows a USBError and would leave an
    unreadable interface out silently; here any failure raises. Alternate
    setting 0 of each interface is read.
    """
    import usb.core

    for c in range(int(device.bNumConfigurations)):
        configuration = usb.core.Configuration(device, c)
        for i in range(int(configuration.bNumInterfaces)):
            if int(usb.core.Interface(device, i, 0, c).bInterfaceClass) == STILL_IMAGE_CLASS:
                return True
    return False


# Replaced in tests.
_canon_product_ids = _real_canon_product_ids


def classify(libcamera_count: Optional[int], canon_product_ids) -> dict:
    """What is connected, by kind, and the one kind connected.

    None for either argument is a probe that could not run: its counts are
    None (unknown), never zero. See _summary for `detected` and `mixed`.
    """
    if canon_product_ids is None:
        gphoto2 = chdk = None
    else:
        ids = list(canon_product_ids)
        chdk = sum(1 for pid in ids if pid in CHDK_PRODUCT_IDS)
        gphoto2 = len(ids) - chdk
    connected = {
        PICAMERA2: None if libcamera_count is None else int(libcamera_count),
        GPHOTO2: gphoto2,
        CHDK: chdk,
    }
    return _summary(connected)


def _measured_positive(count) -> bool:
    return count is not None and count > 0


def _summary(connected: dict) -> dict:
    """`detected`: the one kind with a measured positive count, when every other kind is a measured 0;
    None otherwise. `mixed`: more than one kind with a measured positive count. An unknown (None)
    count is never read as zero, so it blocks `detected` and never makes `mixed`."""
    present = [kind for kind in KINDS if _measured_positive(connected[kind])]
    unknown = any(connected[kind] is None for kind in KINDS)
    return {
        "connected": connected,
        "detected": present[0] if len(present) == 1 and not unknown else None,
        "mixed": len(present) > 1,
    }


def _probe(fn, what: str):
    """fn(), or None when it cannot run. A failure is logged once, and again when its reason changes."""
    try:
        result = fn()
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        if _probe_failures.get(what) != reason:
            _probe_failures[what] = reason
            logger.warning("The %s probe could not run (%s); its count is unknown", what, reason)
        return None
    _probe_failures.pop(what, None)
    return result


def _real_availability() -> dict:
    """Whether each kind's driver library can be imported here, from the flags the driver modules set at import."""
    from capture.backends import chdk_backend, gphoto2_backend, picamera2_backend

    return {
        PICAMERA2: bool(picamera2_backend._PICAMERA2_AVAILABLE and picamera2_backend.Picamera2 is not None),
        GPHOTO2: bool(gphoto2_backend._GP_AVAILABLE),
        CHDK: bool(chdk_backend._PYCHDK_AVAILABLE),
    }


def _real_rpicam_available() -> bool:
    """Whether an rpicam-still executable is on PATH. Looked up, never run."""
    import shutil

    return shutil.which("rpicam-still") is not None


# Replaced in tests.
_availability = _real_availability
_rpicam_available = _real_rpicam_available


def driver_available(kind: str) -> bool:
    """Whether this unit can import the driver library for `kind`."""
    return bool(_availability().get(kind, False))


def detect() -> dict:
    """classify() of the cameras connected now. Never raises."""
    return classify(_probe(_libcamera_count, "libcamera"), _probe(_canon_product_ids, "Canon USB"))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _explicit(value) -> Optional[str]:
    if value is None or not str(value).strip():
        return None
    return value


def resolve() -> Resolution:
    """The kind this start uses, and why. Never raises."""
    choice = stored_choice()
    if choice is not None:
        return Resolution(choice, STORED, None)
    explicit = _explicit(_capture_settings().CAMERA_BACKEND)
    ignored = None
    if explicit is not None:
        from capture.service import KNOWN_CAMERA_BACKENDS

        if _canonical(explicit) in KNOWN_CAMERA_BACKENDS:
            return Resolution(explicit, ENVIRONMENT, None)
        ignored = explicit
        logger.warning("CAMERA_BACKEND is set to %r, which is not a driver this build has (%s); "
                       "ignored, and the kind is chosen as if it were unset",
                       explicit, ", ".join(KNOWN_CAMERA_BACKENDS))
    resolution = _resolve_without_explicit()
    return resolution._replace(ignored=ignored)


def _resolve_without_explicit() -> Resolution:
    """Detection, then the kind last used, then picamera2."""
    detection = detect()
    available = _availability()
    choosing = dict(detection["connected"])
    # A unit with neither picamera2 nor rpicam-still cannot drive Pi
    # cameras, so for choosing a kind its unknown Pi count is 0. With
    # rpicam-still present it stays unknown.
    if choosing[PICAMERA2] is None and not available[PICAMERA2] and not _rpicam_available():
        choosing[PICAMERA2] = 0
    chosen = _summary(choosing)["detected"]
    if chosen is not None and not available[chosen]:
        logger.warning("Cameras of kind %s are connected, but this unit lacks the %s driver; not choosing it",
                       chosen, chosen)
        chosen = None
    if chosen is not None:
        return Resolution(chosen, DETECTED, detection)
    if detection["mixed"]:
        logger.warning("Cameras of more than one kind are connected (%s); not choosing among them",
                       ", ".join(f"{k} {n}" for k, n in detection["connected"].items() if n))
    elif any(n is None for n in detection["connected"].values()):
        logger.warning("Some camera kinds could not be counted (%s); not choosing a kind by detection",
                       ", ".join(k for k, n in detection["connected"].items() if n is None))
    used = last_used()
    if used is not None and not available.get(used, True):
        logger.warning("The kind last used, %s, has no driver on this unit; not falling back to it", used)
        used = None
    if used is not None:
        return Resolution(used, LAST_USED, detection)
    return Resolution(DEFAULT_KIND, DEFAULT, detection)


def apply_at_startup() -> Resolution:
    """Resolve the kind and write it where every reader reads it. Called by the lifespan before the start-up guard."""
    global _in_effect
    from app.core import config

    resolution = resolve()
    targets = {id(s): s for s in (_capture_settings(), config.settings)}
    for settings in targets.values():
        if settings.CAMERA_BACKEND != resolution.kind:
            settings.CAMERA_BACKEND = resolution.kind
    global _applied_choice, _ignored_environment
    _applied_choice = resolution.kind if resolution.source == STORED else None
    _ignored_environment = resolution.ignored
    _in_effect = {"kind": _canonical(resolution.kind), "source": resolution.source}
    logger.info("Camera kind %s (%s)", _in_effect["kind"], resolution.source)
    return resolution


# The stored choice this process started with (None: it started with none),
# set once by apply_at_startup. A restart is pending whenever the stored
# choice differs from it.
_applied_choice: Optional[str] = None


def applied_choice() -> Optional[str]:
    return _applied_choice


# The explicit CAMERA_BACKEND this process set aside at its start (None: none
# was), set once by apply_at_startup.
_ignored_environment: Optional[str] = None


def ignored_environment() -> Optional[str]:
    return _ignored_environment


# Whether this process has remembered the kind of a driver it built.
_built_recorded = False


def driver_built(kind: str) -> None:
    """The driver for `kind` was constructed (capture.service.get_backend): remember it as the last kind used, once per process.

    Never raises: a store that cannot be written is logged by record_last_used.
    """
    global _built_recorded
    if _built_recorded:
        return
    _built_recorded = True
    record_last_used(_canonical(kind))


def in_effect() -> dict:
    """{"kind", "source"} of this process. Before the lifespan has resolved it, read from the settings."""
    if _in_effect is not None:
        return dict(_in_effect)
    explicit = _explicit(_capture_settings().CAMERA_BACKEND)
    if explicit is None:
        return {"kind": DEFAULT_KIND, "source": DEFAULT}
    return {"kind": _canonical(explicit), "source": ENVIRONMENT}


def mark_stored(kind: str) -> None:
    """The administrator stored the kind already in effect: it is now in effect because it is stored, as a restart would make it."""
    global _in_effect, _applied_choice
    current = in_effect()
    if current["kind"] == kind:
        _in_effect = {"kind": kind, "source": STORED}
        _applied_choice = kind


# ---------------------------------------------------------------------------
# The capabilities answer
# ---------------------------------------------------------------------------

def _counted_kind(kind: str) -> str:
    """The detection key a driver's cameras are counted under: rpicam-still drives libcamera cameras too."""
    return PICAMERA2 if kind == "subprocess" else kind


def _held(backend) -> Optional[int]:
    """The driver's own count of what it holds or has listed, or None when it has none to give."""
    count = getattr(backend, "held_body_count", None)
    if not callable(count):
        return None
    try:
        held = count()
    except Exception as exc:
        logger.warning("The camera driver could not say how many bodies it holds (%s)", type(exc).__name__)
        return None
    return held if isinstance(held, int) and not isinstance(held, bool) else None


# What report() answers, kind by kind:
#
#   source of the count                              count
#   ----------------------------------------------   ----------------------------
#   the probe ran (libcamera list / USB scan)        what it found, held maps ignored
#   it could not run, and the driver in effect       the driver's own count
#     counts under this kind and has one             (held_body_count)
#   it could not run, and no such count              null (unknown)
#
#   detected   the one kind with a measured count > 0, every other kind a
#              measured 0; null when any count is unknown
#   mixed      two or more kinds with a measured count > 0
#   mismatch   the kind in effect has a measured 0 and another kind a measured
#              count > 0; an unknown count never makes a mismatch
#   available  per kind, whether its driver library can be imported here
#   stored     the administrator's stored choice, or null
#   pending_restart  the stored choice differs from the one this process
#              started with (null counts as a value): the next start
#              applies it
#   ignored_environment  the CAMERA_BACKEND this start set aside because it
#              names no driver, as written; null when none was
#
# Start-up detection (resolve) differs in two ways: with neither picamera2
# importable nor rpicam-still on PATH the Pi count is 0, not unknown; and a
# kind whose driver is not available is never chosen, though `detected`
# still names it.
#
# rpicam-still (subprocess) counts under picamera2 and lists nothing of its
# own, so with the libcamera list unavailable its count is unknown.
def report(backend) -> dict:
    """The kind fields of GET /cameras/capabilities and GET /system/camera-kind. Never raises; claims no device.

    `backend` is the driver in effect, or None when none is built; its
    count is read only for a kind whose probe could not run.
    """
    current = in_effect()
    configured = current["kind"]
    connected = dict(detect()["connected"])
    key = _counted_kind(configured)
    if key in connected and connected[key] is None and backend is not None:
        connected[key] = _held(backend)
    found = _summary(connected)
    mine = connected.get(key)
    stored = stored_choice()
    return {
        "configured": configured,
        "source": current["source"],
        "stored": stored,
        "pending_restart": stored != _applied_choice,
        "detected": found["detected"],
        "connected": connected,
        "available": _availability(),
        "mixed": found["mixed"],
        "mismatch": mine == 0 and any(_measured_positive(n) for k, n in connected.items() if k != key),
        "ignored_environment": _ignored_environment,
    }
