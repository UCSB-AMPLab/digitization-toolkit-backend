"""
CHDK backend for Canon compacts running the CHDK firmware add-on.

Talks to the bodies over PTP/USB through pychdk. Activate it by setting
CAMERA_BACKEND=chdk in the environment / .env.

Page parity, not a side of the table
------------------------------------
A compact has no left and right. What it has is a page parity, written on its
own card as ``A/OWN.TXT``: ODD or EVEN says which pages that body shoots. The
mapping to a camera index is fixed and direction-agnostic - EVEN is index 0,
ODD is index 1 - and which parity the operator sees on the left is the
kiosk's swap toggle, so a right-to-left volume needs nothing from here.

Identity is the other half of that file. pyusb reads a USB serial from some
bodies and not others, so the card may carry a second line, ``id=<hex>``, and
a body with neither is provisional: listed and previewable, but refused for
capture until the side route gives it one, and never written to the registry.

Locks belong to a body, not to an index. A rescan or a side assignment moves
a body between indices, so a lock keyed by index would be held on one body
and released on another. Each body carries its own; the map of which body
holds which index is guarded separately, and is never held while waiting on a
body's lock.

Live view
---------
``PTP_CHDK_GetDisplayData`` answers with CHDK's own sub-protocol, defined in
its ``core/live_view.h``: a 28-byte ``lv_data_header`` of seven int32 values
(version_major, version_minor, lcd_aspect_ratio, palette_type,
palette_data_start, vp_desc_start, bm_desc_start), and at ``vp_desc_start`` a
36-byte ``lv_framebuffer_desc`` of nine (fb_type, data_start, buffer_width,
visible_width, visible_height, margin_left, margin_top, margin_right,
margin_bot). Everything is little endian.

For ``LV_FB_YUV8`` the viewport is UYVYYY: six bytes carry one U, one V and
four Y values, so four pixels, and a row is ``buffer_width * 12 / 8`` bytes of
which only the first ``visible_width`` pixels are image ("if buffer_width is >
width, the additional data should be skipped"). The colour conversion is
chdkptp's, from its ``liveimg.c``::

    r = clip(((y<<12) +          v*5743 + 2048)>>12)
    g = clip(((y<<12) - u*1411 - v*2925 + 2048)>>12)
    b = clip(((y<<12) + u*7258          + 2048)>>12)

which is full-range BT.601 in 12-bit fixed point, with U and V read as signed
bytes (the viewport's chroma is centred on zero; the bitmap formats that
centre it on 0x80 are a later protocol and are refused here).

Viewport pixels are not square, so the frame cannot simply be cropped to
visible_width and encoded. chdkptp sizes its canvas as the screen's width -
margins included - over the LCD's aspect ratio and stretches the frame into
it (``gui_live.lua``, update_canvas_size and the put_to_cd_canvas call under
it); the same geometry is reproduced here so a page looks on the dashboard
the way it looks on the camera's screen.
"""

import contextlib
import os
import secrets
import struct
import tempfile
import threading
import time
from pathlib import Path

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:  # pragma: no cover - numpy is a hard requirement on the Pi
    np = None  # type: ignore[assignment]
    _NUMPY_AVAILABLE = False

# Imported as a module, never `from pychdk import ...`: every call goes
# through this name so a test can put a fake library in its place, the way
# the gphoto2 tests replace gp. The package resolves its own submodules
# lazily, so this import does not pull in pyusb until a device is opened.
try:
    import pychdk
    _PYCHDK_AVAILABLE = True
except ImportError:
    pychdk = None  # type: ignore[assignment]
    _PYCHDK_AVAILABLE = False

from .base import CameraBackend
from .errors import CaptureTimeoutError
from ..utils import atomic_write


# --- CHDK live view protocol (core/live_view.h) ----------------------------

# Control flag selecting the viewport block of PTP_CHDK_GetDisplayData.
LV_TFR_VIEWPORT = 0x01

# The only live view protocol this decoder speaks. A major version bump is a
# backwards-incompatible change by CHDK's own definition, so a frame from one
# is refused rather than guessed at.
_LV_VERSION_MAJOR = 2

# lv_fb_type. LV_FB_YUV8 is the viewport; LV_FB_PAL8 is the bitmap overlay,
# and the YUV8B/YUV8C viewports belong to a later minor protocol than the one
# this decoder was written against.
_LV_FB_YUV8 = 0

_LV_HEADER_FMT = "<7i"
_LV_HEADER_SIZE = struct.calcsize(_LV_HEADER_FMT)
_LV_HEADER_FIELDS = (
    "version_major", "version_minor", "lcd_aspect_ratio", "palette_type",
    "palette_data_start", "vp_desc_start", "bm_desc_start",
)

_LV_FB_DESC_FMT = "<9i"
_LV_FB_DESC_SIZE = struct.calcsize(_LV_FB_DESC_FMT)
_LV_FB_DESC_FIELDS = (
    "fb_type", "data_start", "buffer_width", "visible_width",
    "visible_height", "margin_left", "margin_top", "margin_right",
    "margin_bot",
)

# lv_aspect_ratio. The header names 4:3 and 16:9; chdkptp carries a third
# value, 3:2, and falls back to 4:3 for anything it does not know rather than
# refusing the frame, which is what a preview wants.
_LCD_ASPECTS = {0: 4 / 3, 1: 16 / 9, 2: 3 / 2}
_DEFAULT_LCD_ASPECT = 4 / 3

# JPEG quality for a preview frame. High enough to focus on, small enough to
# poll.
_PREVIEW_JPEG_QUALITY = 85

# A sanity bound, in pixels, on the screen a viewport descriptor may describe
# - margins included - in either dimension.
#
# The descriptor's four margins are signed int32 and live_view.h states no
# range for them. Unlike the viewport itself, whose size is bounded by the
# bytes that have to be present for it, a margin costs nothing on the wire
# and still sizes the canvas encode_viewport_jpeg allocates: on a four-pixel
# viewport a margin_left of 30000 asks for a 30004x22503 image, and one near
# INT_MAX raises MemoryError. Neither is the ValueError the preview path
# reports a bad frame with, and on the appliance the backend runs natively,
# with nothing around it to absorb the difference.
#
# This number is a limit this decoder chooses, not a statement about any
# camera's screen. A frame describing a screen past it in either dimension is
# treated as malformed rather than allocated for.
_LV_MAX_SCREEN = 4096


# --- the side file ---------------------------------------------------------

# Where the parity lives. CHDK addresses the card root as A/, and pychdk's
# download_file/upload_file both take a card path.
SIDE_FILE = "A/OWN.TXT"

# Page parity to camera index. Fixed, and not a statement about the table:
# which parity the operator sees on the left is the kiosk's swap toggle.
_SIDE_INDEX = {"even": 0, "odd": 1}

# PTP_RC_GeneralError. A download of a file the card does not have comes back
# with this, and only this means "no such file"; any other response code is a
# body that has gone wrong and is evicted rather than read as unassigned.
_PTP_GENERAL_ERROR = 0x2002

# What remote capture returns, and so the only output name this backend can
# honour. The capture service picks the name and guarantees it is free; a
# name that would have to be changed here is refused rather than written
# somewhere nothing checked.
_CAPTURE_SUFFIXES = (".jpg", ".jpeg")

# The route that writes a parity, named in every message that asks the
# operator to fix one.
_SIDE_ROUTE = "POST /cameras/side/{camera_index}"

# Hex characters in a minted body id. The library's parser accepts twelve to
# thirty-two, and the flasher mints twelve, so a body keeps the same shape of
# id whether the card was prepared at a workbench or assigned here.
_CAMERA_ID_BYTES = 6


class CameraMovedError(RuntimeError):
    """The body that held this index when the operation started no longer does.

    A rescan or a side assignment moves bodies between indices, and an
    operation that resolved an index before waiting for a lock would
    otherwise run on whichever body holds that index now. Filing the left
    page as the right one is a silent data error, so this is raised instead.
    """


class SideConflictError(RuntimeError):
    """The parity asked for is already another connected body's.

    Its own class because the route answers it with a 409 rather than a 500:
    nothing has gone wrong, the operator has asked for something that would
    leave two bodies shooting the same pages.
    """


def parse_live_view(data):
    """Read the header and the viewport descriptor out of one frame.

    Args:
        data: The bytes PTP_CHDK_GetDisplayData answered with.

    Returns:
        Dict of every header and viewport-descriptor field, by the names
        CHDK's own structures give them.

    Raises:
        ValueError: If the frame is too short, speaks another major version
            of the protocol, carries no viewport descriptor, or describes a
            viewport this decoder cannot read - which includes a margin below
            zero and a screen, margins included, past _LV_MAX_SCREEN in
            either dimension.
    """
    if len(data) < _LV_HEADER_SIZE:
        raise ValueError(
            f"live view frame is short: {len(data)} bytes, header needs "
            f"{_LV_HEADER_SIZE}"
        )
    header = dict(zip(
        _LV_HEADER_FIELDS, struct.unpack_from(_LV_HEADER_FMT, data)
    ))
    if header["version_major"] != _LV_VERSION_MAJOR:
        raise ValueError(
            f"live view protocol version {header['version_major']}."
            f"{header['version_minor']} is not "
            f"{_LV_VERSION_MAJOR}.x; this decoder cannot read it"
        )

    start = header["vp_desc_start"]
    if start <= 0:
        raise ValueError("live view frame carries no viewport descriptor")
    if start + _LV_FB_DESC_SIZE > len(data):
        raise ValueError(
            f"live view frame is short: viewport descriptor at {start} needs "
            f"{_LV_FB_DESC_SIZE} bytes of {len(data)}"
        )
    vp = dict(zip(
        _LV_FB_DESC_FIELDS, struct.unpack_from(_LV_FB_DESC_FMT, data, start)
    ))
    if vp["fb_type"] != _LV_FB_YUV8:
        raise ValueError(
            f"viewport fb_type {vp['fb_type']} is not LV_FB_YUV8; this "
            "decoder reads only the UYVYYY viewport"
        )
    if vp["visible_width"] > vp["buffer_width"]:
        raise ValueError(
            f"viewport visible_width {vp['visible_width']} is wider than "
            f"buffer_width {vp['buffer_width']}"
        )
    if vp["visible_width"] <= 0 or vp["visible_height"] <= 0:
        raise ValueError(
            f"viewport is empty: {vp['visible_width']}x{vp['visible_height']}"
        )
    # The margins are bounded in both directions here, where the
    # malformed-frame contract is documented, because encode_viewport_jpeg
    # measures the camera's screen from them and neither builds nor divides
    # by that screen safely on its own.
    for field in ("margin_left", "margin_top", "margin_right", "margin_bot"):
        if vp[field] < 0:
            # A negative margin describes a screen smaller than the viewport
            # inside it. The screen height is the divisor - margin_top +
            # visible_height + margin_bot - so the one combination that
            # cancels it exactly raises ZeroDivisionError, past the except
            # ValueError the preview path uses to report a bad frame as a
            # failed poll.
            raise ValueError(f"viewport {field} is negative: {vp[field]}")
    screen_width = vp["margin_left"] + vp["visible_width"] + vp["margin_right"]
    screen_height = vp["margin_top"] + vp["visible_height"] + vp["margin_bot"]
    if screen_width > _LV_MAX_SCREEN or screen_height > _LV_MAX_SCREEN:
        raise ValueError(
            f"viewport describes a {screen_width}x{screen_height} screen, "
            f"past the {_LV_MAX_SCREEN} px in either dimension this decoder "
            "will build a frame for"
        )
    if vp["data_start"] <= 0:
        # CHDK sends the descriptions whether or not the data is available,
        # and zeroes the offset when it is not.
        raise ValueError("live view frame has no viewport data")

    row_bytes = (vp["buffer_width"] * 12) // 8
    needed = vp["data_start"] + row_bytes * vp["visible_height"]
    if needed > len(data):
        raise ValueError(
            f"live view frame is short: viewport needs {needed} bytes of "
            f"{len(data)}"
        )

    info = dict(header)
    info.update(vp)
    info["row_bytes"] = row_bytes
    return info


def decode_viewport_rgb(data):
    """Decode one UYVYYY viewport into an (h, visible_width, 3) uint8 array.

    The pixels are the camera's own, un-stretched: the display aspect is
    applied later, by encode_viewport_jpeg.

    Args:
        data: The bytes PTP_CHDK_GetDisplayData answered with.

    Returns:
        Tuple of (rgb array, info dict from parse_live_view).

    Raises:
        ValueError: For any frame parse_live_view refuses.
        RuntimeError: If numpy is missing.
    """
    if not _NUMPY_AVAILABLE:
        raise RuntimeError(
            "numpy is not installed; the CHDK live view cannot be decoded"
        )

    info = parse_live_view(data)
    height = info["visible_height"]
    width = info["visible_width"]
    row_bytes = info["row_bytes"]

    # Six bytes are four pixels. A row that does not divide by four is read a
    # whole group at a time and trimmed afterwards, so no group ever reaches
    # past the row it belongs to.
    groups = min(-(-width // 4), row_bytes // 6)
    if groups * 4 < width:
        raise ValueError(
            f"viewport row of {row_bytes} bytes cannot hold {width} pixels"
        )

    flat = np.frombuffer(
        data, dtype=np.uint8, count=row_bytes * height, offset=info["data_start"]
    )
    rows = flat.reshape(height, row_bytes)[:, :groups * 6].reshape(
        height, groups, 6
    )

    def _signed(plane):
        value = plane.astype(np.int32)
        return np.where(value >= 128, value - 256, value)

    u = _signed(rows[:, :, 0])
    v = _signed(rows[:, :, 2])
    # 1.402, 0.344136, 0.714136 and 1.772 in 12-bit fixed point: chdkptp's
    # yuv_to_r/g/b, shifted arithmetically exactly as the C does.
    r_chroma = v * 5743 + 2048
    g_chroma = -u * 1411 - v * 2925 + 2048
    b_chroma = u * 7258 + 2048

    rgb = np.empty((height, groups * 4, 3), dtype=np.uint8)
    for offset, column in enumerate((1, 3, 4, 5)):
        y = rows[:, :, column].astype(np.int32) << 12
        for plane, chroma in enumerate((r_chroma, g_chroma, b_chroma)):
            rgb[:, offset::4, plane] = np.clip((y + chroma) >> 12, 0, 255)

    return rgb[:, :width, :], info


def encode_viewport_jpeg(data, quality=_PREVIEW_JPEG_QUALITY):
    """Decode one viewport and encode it as a JPEG at the display's aspect.

    The output is the camera's screen: ``margin_left + visible_width +
    margin_right`` wide, and that width over the LCD's aspect ratio high, with
    the viewport stretched into the rectangle the margins leave for it. That
    is chdkptp's geometry, and without it a 720x240 viewport on a 4:3 screen
    reaches the operator squashed to a third of its height.

    Args:
        data: The bytes PTP_CHDK_GetDisplayData answered with.
        quality: JPEG quality.

    Returns:
        Tuple of (jpeg bytes, info dict). The info carries every field the
        bench needs to read - protocol version, aspect, framebuffer geometry,
        margins - plus jpeg_bytes.

    Raises:
        ValueError: For any frame parse_live_view refuses.
    """
    from io import BytesIO

    from PIL import Image

    rgb, info = decode_viewport_rgb(data)

    screen_width = (
        info["margin_left"] + info["visible_width"] + info["margin_right"]
    )
    screen_height = (
        info["margin_top"] + info["visible_height"] + info["margin_bot"]
    )
    aspect = _LCD_ASPECTS.get(info["lcd_aspect_ratio"], _DEFAULT_LCD_ASPECT)
    out_width = max(1, screen_width)
    out_height = max(1, round(screen_width / aspect))
    factor = out_height / screen_height

    viewport = Image.fromarray(rgb, mode="RGB")
    scaled_height = max(1, round(info["visible_height"] * factor))
    if (viewport.width, viewport.height) != (info["visible_width"], scaled_height):
        viewport = viewport.resize(
            (info["visible_width"], scaled_height), Image.BILINEAR
        )

    if (out_width, out_height) == (viewport.width, viewport.height):
        canvas = viewport
    else:
        canvas = Image.new("RGB", (out_width, out_height), (0, 0, 0))
        canvas.paste(viewport, (info["margin_left"], round(info["margin_top"] * factor)))

    buffer = BytesIO()
    canvas.save(buffer, format="JPEG", quality=quality)
    payload = buffer.getvalue()

    info = dict(info)
    info["jpeg_width"] = out_width
    info["jpeg_height"] = out_height
    info["jpeg_bytes"] = len(payload)
    return payload, info


def _name_failure(exc):
    """Name a failure for a log line and an error message.

    A PTP response code says exactly what the camera refused, and is the
    thing the bench needs in the line itself. Anything else - a pyusb
    transport error, a decode, a timeout - has no code, so its class name
    stands in.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return f"PTP 0x{code:04x}"
    return type(exc).__name__


def _shutter_seconds(value):
    """Read a CameraConfig shutter speed as seconds, or None if it says nothing.

    CameraConfig carries the DSLR's own string ("1/250", "0.8", "30"), while
    pychdk's shoot() takes seconds and converts them to CHDK's TV96. A value
    that names no duration - "auto", "bulb", empty, unparseable, zero or
    negative - returns None, which leaves the camera on whatever it is set to
    rather than inventing an exposure for it.

    Args:
        value: CameraConfig.shutter_speed, or None.

    Returns:
        Float seconds, or None.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("auto", "bulb"):
        return None
    try:
        if "/" in text:
            numerator, _, denominator = text.partition("/")
            seconds = float(numerator) / float(denominator)
        else:
            seconds = float(text)
    except (ValueError, ZeroDivisionError):
        return None
    return seconds if seconds > 0 else None


class _ChdkBody:
    """One open camera, and everything the backend has learned about it.

    Keyed by its USB bus and address, which is what identifies the thing on
    the wire; the hardware id is what identifies the body across replugs and
    is read from the card or the USB serial.

    The lock is the body's own, and serialises everything that talks to it -
    capture, preview, the card read - because pychdk assumes one thread per
    device.
    """

    def __init__(self, info, device, model):
        self.info = info
        self.device = device
        self.model = model
        self.lock = threading.RLock()
        # From A/OWN.TXT: the parity, lowercased, and the body's own id.
        self.side = None
        self.camera_id = None
        self.card_read = False
        # Set when another body claims the same parity, and when another
        # body answers to the same hardware id. Either refuses capture on
        # both bodies until the side route settles it; they are different
        # faults and say so.
        self.collision = None
        self.identity_clash = None
        # Record mode is switched once per body: a viewport and a remote
        # capture both need it, and the switch costs seconds.
        self.in_record_mode = False
        # Live view instrumentation. The geometry is logged on the first frame
        # only; the rate every _FRAME_LOG_INTERVAL frames.
        self.geometry_logged = False
        self.frames = 0
        self.frames_at_last_report = 0
        self.last_frame_at = None
        self.interval_total = 0.0
        self.interval_count = 0
        self.interval_worst = 0.0

    @property
    def key(self):
        return (self.info.bus_num, self.info.device_num)

    @property
    def serial(self):
        return self.info.serial_num or None

    @property
    def port(self):
        return f"usb:{self.info.bus_num:03d},{self.info.device_num:03d}"

    @property
    def slug(self):
        """The prefix of the hardware id: the camera's USB product.

        Deliberately not the model name. The model is a string descriptor
        read over the wire, and a read that fails for its own reasons would
        otherwise give this body a second hardware id and lose the
        orientation and calibration saved under the first. The product id is
        in the enumeration record itself, is the same for every body of a
        model, and costs no conversation to obtain.
        """
        return f"canon{self.info.product_id:04x}"

    @property
    def hardware_id(self):
        """The body's stable identity, or None when it has none to give.

        The USB serial first, because it needs no card; the id line on the
        card when pyusb reads no serial. A body with neither is provisional
        and must not reach the registry under a made-up name.
        """
        identity = self.serial or self.camera_id
        return f"{self.slug}_{identity}" if identity else None


class ChdkBackend(CameraBackend):
    """Camera backend for Canon compacts under CHDK, over pychdk.

    Thread safety:
      - Every body carries its own lock, and everything that talks to that
        body holds it: capture, preview, the card read, and closing it.
        pychdk assumes one thread per device, and a dual capture runs one
        thread per index. The lock is re-entrant, because an operation that
        fails drops the body it is already holding.
      - A camera index is not an identity. An operation resolves its index to
        a body, and resolves it again once it has the lock, because a rescan
        or an assignment in between moves bodies from index to index; one
        that finds the index means another body now is refused rather than
        run on the wrong camera.
      - A body leaves the map only after its device is closed, so its port
        never looks free while a claim on it is still open.
      - A new layout is published while every open body is held, so it never
        lands under a capture that is running on the index it is about to
        change.
      - _map_lock guards which bodies are open and which index each holds.
        It is taken for short reads and for the one moment a new layout is
        published, never while waiting on a body's lock, so a capture that
        holds a body for thirty seconds never blocks an enumeration's view
        of the other one.
      - _layout_lock serialises whole enumerations, side assignments and
        cleanup - everything that opens bodies, closes them all, or changes
        where they belong - so two of them cannot open the same body twice,
        publish their layouts out of order, both decide a parity is free and
        both write it, or leave a body open behind a shutdown.
      - Lock order is _layout_lock, then a body lock, then _map_lock. No path
        takes _map_lock and then waits for a body.
    """

    # How often the preview rate is reported, in frames. The bench needs
    # frames per second without a stopwatch, and a line per frame would bury
    # everything else in the log.
    _FRAME_LOG_INTERVAL = 100

    def __init__(self, logger):
        if not _PYCHDK_AVAILABLE:
            raise RuntimeError(
                "pychdk is not installed. Add it to pixi.toml and "
                "requirements.txt, or set CAMERA_BACKEND to another backend."
            )
        super().__init__(logger)
        # (usb bus, usb address) -> _ChdkBody, for every body that is open
        self._bodies: dict = {}
        # camera index -> the key of the body holding it
        self._indices: dict = {}
        # the bodies evicted by a failure: their return is logged as the
        # recovery it is, and until the next scan has looked for them their
        # index is worth re-enumerating for
        self._evicted: set = set()
        # whether the bus has been looked at even once
        self._scanned = False
        self._map_lock = threading.Lock()
        # Held for a whole enumeration, and for a side assignment, which is a
        # check against the layout followed by a write that changes it.
        # Re-entrant so an assignment can republish through rescan() without
        # handing the lock over in between.
        self._layout_lock = threading.RLock()

    # ------------------------------------------------------------------
    # Bodies
    # ------------------------------------------------------------------

    def _body_at(self, camera_index):
        """The body holding this index right now, or None."""
        with self._map_lock:
            key = self._indices.get(camera_index)
            return self._bodies.get(key) if key is not None else None

    def _open_body(self, info):
        """Open one camera and read enough of it to name it. None on failure."""
        key = (info.bus_num, info.device_num)
        try:
            device = pychdk.ChdkDevice(info)
        except Exception as exc:
            self.logger.error(
                f"[chdk] {key}: could not open the camera ({exc!r}); "
                "it is left out of the device list"
            )
            return None
        body = _ChdkBody(info, device, self._read_model(device, info))
        if key in self._evicted:
            self._evicted.discard(key)
            self.logger.info(
                f"[chdk] {body.port}: back on the bus after an earlier "
                "failure; re-enumeration recovered it"
            )
        return body

    def _is_the_same_body(self, body, info):
        """Is the camera at this address the one this session was opened on?

        A bus address is a place, not a camera: unplug one and plug in
        another and the second can land on the address the first had. The
        enumeration record is what the bus says about the thing that is there
        now, so anything in it that differs - a serial, a product - is a
        different camera, and the session pointed at the old one has to go.
        """
        return tuple(body.info) == tuple(info)

    def _still_answering(self, body):
        """Prove the session is on a camera that is still there, or drop it.

        The library's connected flag is local: it records what the host last
        did with the session, not whether the camera survived. So a body that
        was unplugged and replaced by an identical one - same product, no
        serial to tell them apart, and the same address - would otherwise
        keep its session, its card state and its index, and the bench's
        reconnect test would read as the camera never coming back.

        One CHDK version transaction settles it. It is the cheapest thing the
        protocol has, one command with no data phase, and a handle whose
        device was unplugged cannot answer it whatever has taken its place.
        A body that fails it is closed here and opened again by the caller.
        """
        if not body.device.is_connected:
            self._close_body(body, "the device reports itself disconnected")
            return False
        with body.lock:
            try:
                body.device._chdk.get_version()
            except Exception as exc:
                self.logger.info(
                    f"[chdk] {body.port}: stopped answering "
                    f"({_name_failure(exc)}); opening it again"
                )
                self._close_body(body, "it stopped answering")
                return False
        return True

    def _read_model(self, device, info):
        """The body's model, from its USB product string if it answers one.

        pyusb reads string descriptors over the wire and can fail or answer
        nothing, so the product id stands in. This is for people to read -
        the device list, the log, the registry row - and nothing depends on
        it: the hardware id is built from the product id instead, so a
        failed read costs a name and never an identity.
        """
        try:
            product = getattr(device._usb_device, "product", None)
        except Exception:
            product = None
        if product:
            return str(product).strip()
        return f"Canon {info.product_id:#06x}"

    def _close_body(self, body, reason):
        """Close one body and drop it from the map, under the body's own lock.

        The order matters in both directions. The device is closed while the
        body's lock is held, so a capture or a preview is never cut off
        mid-conversation - whoever wants to close a body waits for whoever is
        using it. And the map entry is removed only once the close has
        returned, so there is no moment in which the port looks free while a
        claim on it is still open: an enumeration racing this either sees the
        body and waits on its lock, or sees nothing and opens the one
        replacement there should ever be.

        The lock is re-entrant, so the paths that already hold it - a capture
        evicting the body it is holding - can call this directly.
        """
        with body.lock:
            try:
                body.device.close()
            except Exception as exc:
                self.logger.warning(
                    f"[chdk] {body.port}: error while closing it ({exc!r})"
                )
            with self._map_lock:
                if self._bodies.get(body.key) is body:
                    del self._bodies[body.key]
                    self._indices = {
                        index: key for index, key in self._indices.items()
                        if key != body.key
                    }
            self.logger.info(f"[chdk] {body.port}: closed ({reason})")

    def _evict(self, body, reason):
        """Drop a body that has stopped answering, so the next scan re-opens it."""
        self._evicted.add(body.key)
        self._close_body(body, reason)

    def _is_absent(self, exc):
        """True when a download failed because the card has no such file."""
        return (
            isinstance(exc, pychdk.PTPError)
            and getattr(exc, "code", None) == _PTP_GENERAL_ERROR
        )

    def _read_side_file(self, body):
        """Read A/OWN.TXT into the body. False means the body has to go.

        A general error from the download is the card saying it has no such
        file, which is an unassigned body and perfectly normal. Anything else
        is a body that has stopped answering properly, and reading that as
        "unassigned" would quietly put it on whichever index was free.

        The download and the three fields it publishes - the parity, the id
        and the flag that says the card has been read - are one operation
        under the body's own lock. What that buys is bounded and worth
        stating exactly. A capture is admitted by _row_error, which reads the
        body's hardware id, and that id is the card's when pyusb reads no USB
        serial; publishing outside the lock let a rescan clear it between the
        check and the shutter, and a page was written for a body that by then
        had no identity to record it under. Under the lock a capture sees the
        card state whole: all three fields as they were before the read, or
        all three as they are after it.

        It does not make a capture safe against the rest of _row_error. A
        body's collision and identity_clash are derived from the whole layout
        and are recomputed only where the layout is - in _assign_indices,
        when a scan republishes. So between a read that gives one body an id
        another body already answers to and the publish that re-derives the
        clashes, a capture can still be admitted against a clash nothing has
        computed yet. Closing that needs the derivation under this lock too,
        or the read folded into the publish; neither is done here.

        The parity is not part of this. Nothing on the capture path reads
        body.side - it is read by _assign_indices, _row and assign_side - so
        a parity reaches a filed page only through the index the layout gives
        the body, and the layout is what the publish barrier in _enumerate
        holds back while a capture is running.
        """
        with body.lock:
            try:
                raw = body.device.download_file(SIDE_FILE)
            except Exception as exc:
                if self._is_absent(exc):
                    body.side = None
                    body.camera_id = None
                    body.card_read = True
                    self.logger.warning(
                        f"[chdk] {body.port}: no {SIDE_FILE} on the card, so "
                        "this body has no page parity; it takes a free index "
                        f"in USB order until {_SIDE_ROUTE} gives it one"
                    )
                    return True
                self.logger.error(
                    f"[chdk] {body.port}: reading {SIDE_FILE} failed "
                    f"({exc!r}); dropping the body"
                )
                return False
            side, camera_id = pychdk.parse_own_txt(raw)
            body.side = side.lower() if side else None
            body.camera_id = camera_id
            body.card_read = True
            return True

    # ------------------------------------------------------------------
    # Enumeration
    # ------------------------------------------------------------------

    def _assign_indices(self, bodies):
        """Lay the open bodies out on camera indices. Callers hold _map_lock.

        EVEN is index 0 and ODD is index 1, and a parity any connected body
        claims holds its index whatever else is on the bus. Every other body
        - one with no parity, and the second and later claimants of a
        contested one - takes the lowest index that no parity has claimed and
        nothing else has taken, in USB order. So a single unassigned body is
        still usable, and no body is ever laid on top of a parity in use.

        Two bodies claiming one parity is the case that must not be resolved
        by guessing. The first of them in USB order keeps the parity's index
        and the rest take free indices above the reserved ones, so both stay
        addressable and the side route can fix either; both are marked, and
        capture refuses them until it is.

        What a contest must not do is move the bodies that are not in it. A
        layout that fell back to USB order for everything put a healthy ODD
        body on index 0, where it carried no refusal of its own and shot odd
        pages the service filed as even ones - no failure, no log line, the
        pages simply in the wrong place.
        """
        self._mark_identity_clashes(bodies)

        claimants = {}
        for body in bodies:
            body.collision = None
            if body.side:
                claimants.setdefault(body.side, []).append(body)

        for side, sharing in claimants.items():
            if len(sharing) < 2:
                continue
            for body in sharing:
                body.collision = (
                    f"two bodies are set to shoot {side} pages "
                    f"({', '.join(b.port for b in sharing)}); "
                    f"give one of them the other parity with {_SIDE_ROUTE} "
                    "before capturing"
                )

        # Reserved for a parity that has a claimant, contested or not, so
        # nothing else can be laid on an index a parity is using.
        reserved = {_SIDE_INDEX[side] for side in claimants}
        taken = {}
        overflow = []
        for body in bodies:
            if not body.side:
                continue
            if claimants[body.side][0] is body:
                taken[_SIDE_INDEX[body.side]] = body.key
            else:
                overflow.append(body)

        floating = [body for body in bodies if not body.side]
        for body in overflow + floating:
            index = 0
            while index in reserved or index in taken:
                index += 1
            taken[index] = body.key
        return taken

    def _mark_identity_clashes(self, bodies):
        """Flag every body that shares its hardware id with another.

        Two cards cloned from one image carry the same id line, and the
        appliance then has one identity for two cameras: they collapse onto a
        single registry entry, share its orientation and calibration, and
        whichever shoots a page is recorded as the other. Nothing about that
        is recoverable after the fact, so both are refused until one of them
        is given an identity of its own.

        A body with no identity at all is not part of this: it is provisional
        already, and refused for its own reason.

        The error names the bodies a card write can actually repair, which is
        those whose identity comes off their card. A clash can just as easily
        run between one body's USB serial and another's card id, and then
        only the second one can be rewritten - sending the operator at the
        first would have them rewriting a card that was never the fault.
        """
        claimants = {}
        for body in bodies:
            body.identity_clash = None
            identity = body.hardware_id
            if identity is not None:
                claimants.setdefault(identity, []).append(body)
        for identity, sharing in claimants.items():
            if len(sharing) < 2:
                continue
            ports = ", ".join(other.port for other in sharing)
            # A body with a USB serial answers to that serial whatever its
            # card says, so writing its card cannot change what it answers to.
            repairable = [other.port for other in sharing if other.serial is None]
            if repairable:
                remedy = (
                    f"Assign a page parity to {' or '.join(repairable)} with "
                    f"{_SIDE_ROUTE}, which writes that body a fresh id"
                )
            else:
                remedy = (
                    "Both bodies report it as their USB serial, which no card "
                    "can change: take one of them off the rig"
                )
            for body in sharing:
                body.identity_clash = (
                    f"two bodies answer to the identity {identity} ({ports}), "
                    "so the appliance cannot tell them apart and neither may "
                    f"capture. {remedy}"
                )

    def _row_error(self, body):
        """Why this body may not capture, or None."""
        if body.identity_clash:
            return body.identity_clash
        if body.collision:
            return body.collision
        if body.hardware_id is None:
            return (
                f"this body answers no USB serial and its card carries no id "
                f"line, so it has no identity to record; assign its page "
                f"parity with {_SIDE_ROUTE} - which writes one - before "
                "capturing"
            )
        return None

    def _row(self, camera_index, body):
        """One device dict, in the shape CameraBackend.list_devices promises."""
        provisional = body.hardware_id is None
        return {
            "index": camera_index,
            "model": body.model,
            "hardware_id": body.hardware_id or f"{body.slug}_idx{camera_index}",
            "serial": body.serial,
            "location": f"USB {body.port}",
            "port": body.port,
            "has_aperture_control": False,
            "supports_zoom": False,
            "side": body.side,
            "provisional": provisional,
            # Another connected body answers to this one's hardware id, so
            # the id identifies two cameras and cannot be recorded against
            # either. The row is still reported: seeing both is how the
            # operator repairs it.
            "identity_ambiguous": body.identity_clash is not None,
            "error": self._row_error(body),
        }

    def _enumerate(self, reread):
        """Scan the bus, reconcile the open bodies with it, and lay them out.

        Bodies that have left are closed and dropped; bodies that are new are
        opened and read; bodies that were already open keep their device, and
        so their lock, so nothing in flight on them is disturbed - but only
        once they have been shown to be the same camera still answering, and
        not an address another body has taken over. `reread` asks for every
        card to be read again, which is what a rescan is for: a parity
        written since the last scan is invisible otherwise.
        """
        with self._layout_lock:
            infos = pychdk.list_devices()
            present = {
                (info.bus_num, info.device_num): info for info in infos
            }

            with self._map_lock:
                departed = [
                    body for key, body in self._bodies.items()
                    if key not in present
                ]
            for body in departed:
                # Closing waits for anything still using the body, so an
                # enumeration can be as slow as the capture it interrupts.
                self._close_body(body, "no longer on the bus")

            for key, info in present.items():
                # Read before anything below can clear it: opening a body
                # that had been dropped counts as its recovery, so by the
                # time a card read fails the mark that says this is already
                # the retry would be gone.
                retried = key in self._evicted
                with self._map_lock:
                    body = self._bodies.get(key)
                if body is not None and not self._is_the_same_body(body, info):
                    self._close_body(
                        body, "another body is at this address now"
                    )
                    body = None
                if body is not None and not self._still_answering(body):
                    body = None
                fresh = body is None
                if fresh:
                    body = self._open_body(info)
                    if body is None:
                        continue
                if fresh or reread or not body.card_read:
                    if not self._read_side_file(body):
                        if retried:
                            # A scan has already retried this one and it has
                            # failed again, so it is the card rather than the
                            # moment. Drop the mark: the index stays empty
                            # until someone asks for a rescan, instead of
                            # every connection check scanning the bus for a
                            # body that will not answer.
                            self._evicted.discard(key)
                            self._close_body(
                                body, "its card could not be read again"
                            )
                        else:
                            # Marked like any other failure, including on a
                            # body being opened for the first time, so the
                            # next connection check looks at the bus again
                            # and a read that failed for the moment rather
                            # than for good is retried without anyone having
                            # to rescan by hand.
                            self._evict(body, "its card could not be read")
                        continue
                if fresh:
                    with self._map_lock:
                        self._bodies[key] = body

            # Publishing is where a body changes index, and a capture that is
            # already running on one is using the index it had when it
            # started. Revalidating before the shutter cannot cover that: the
            # capture has passed the check and is holding the body while this
            # runs. So the new layout waits for every body in hand. Nothing
            # is in flight when it lands, and anything that arrives after it
            # revalidates against it.
            #
            # The locks are taken in key order, and only an enumeration ever
            # holds more than one: a capture or a preview holds exactly the
            # body it is using and reaches for nothing else, and enumerations
            # are serialised by the layout lock, so there is no pair of
            # threads that can each hold what the other wants.
            with contextlib.ExitStack() as held:
                with self._map_lock:
                    in_hand = [
                        self._bodies[key] for key in sorted(self._bodies)
                    ]
                for body in in_hand:
                    held.enter_context(body.lock)

                with self._map_lock:
                    ordered = [
                        self._bodies[key]
                        for key in present if key in self._bodies
                    ]
                    self._indices = self._assign_indices(ordered)
                    rows = [
                        self._row(index, self._bodies[key])
                        for index, key in sorted(self._indices.items())
                    ]

            with self._map_lock:
                # A body a scan has looked for and not found is no longer a
                # reason to scan again; one that is on the bus was reopened
                # above and is not in here either.
                self._evicted &= set(present)
                self._scanned = True

            self._log_enumeration(rows)
            return rows

    def _log_enumeration(self, rows):
        """Say, per body, everything the bench would otherwise have to probe."""
        if not rows:
            self.logger.warning("[chdk] no CHDK cameras found on the bus")
            return
        for row in rows:
            serial = (
                f"usb serial {row['serial']}" if row["serial"]
                else "no usb serial"
            )
            parity = row["side"] or "unassigned"
            body = self._body_at(row["index"])
            card_id = (body.camera_id if body else None) or "none"
            self.logger.info(
                f"[chdk] body {row['index']}: {row['model']}, {serial}, "
                f"parity {parity}, card id {card_id}, "
                f"hardware id {row['hardware_id']}"
                + (" (provisional)" if row["provisional"] else "")
            )
            if row["error"]:
                self.logger.error(
                    f"[chdk] body {row['index']} ({row['location']}) may not "
                    f"capture: {row['error']}"
                )

    def list_devices(self) -> list:
        """Enumerate the bodies, opening any that are new and reading their cards."""
        return self._enumerate(reread=False)

    def rescan(self) -> list:
        """Enumerate, and read every card again.

        The operator's lever after moving a card, rewriting a parity by hand,
        or replugging a body: unlike list_devices, this does not trust what
        the last scan read off the cards.
        """
        return self._enumerate(reread=True)

    # ------------------------------------------------------------------
    # CameraBackend interface
    # ------------------------------------------------------------------

    def is_camera_connected(self, camera_index: int = 0) -> bool:
        """Is a body holding this index, with its device still open?

        The first question enumerates, because a process that has not yet
        listed its devices would otherwise report a working rig as absent and
        refuse every capture. After that it reads what the last scan found,
        with one exception: an index that is empty because a failure dropped
        the body is worth one more look, so a body that broke off
        mid-conversation comes back by itself rather than waiting for someone
        to press rescan. That costs one enumeration per failure, not one per
        question - a scan that does not find the body stops hoping for it.
        """
        try:
            with self._map_lock:
                unscanned = not self._scanned
                missing = camera_index not in self._indices
                dropped = bool(self._evicted)
            if unscanned or (missing and dropped):
                self._enumerate(reread=False)
            body = self._body_at(camera_index)
            return body is not None and bool(body.device.is_connected)
        except Exception as exc:
            self.logger.error(f"[chdk] is_camera_connected({camera_index}): {exc!r}")
            return False

    @contextlib.contextmanager
    def _in_use(self, camera_index, operation, refuse_unusable):
        """Hold the body at this index, having checked it is still that body.

        Resolving an index to a body and acquiring that body's lock are two
        moments, and a rescan or a side assignment in between moves bodies
        from index to index. So the index is resolved a second time once the
        lock is held: if it no longer means this body, the operation is
        refused rather than run on the camera that took its place, which
        would file one page as the other with nothing to show for it
        afterwards.

        The refusal rules are read inside the lock for the same reason - a
        second body can arrive on this one's parity while a capture waits -
        and only for the operations they apply to. A preview is not one of
        them: an operator has to be able to aim a body that may not yet
        capture.

        Args:
            camera_index: Which body, as the device list numbers them.
            operation: Named in the message when the body has moved.
            refuse_unusable: Apply the capture refusal rules.

        Yields:
            The body, with its lock held.

        Raises:
            CameraMovedError: The index means another body now.
            RuntimeError: No body at the index, its device has gone, or it
                may not be used for this.
        """
        body = self._body_at(camera_index)
        if body is None:
            raise RuntimeError(
                f"Camera {camera_index} is not connected. Detected indices: "
                f"{sorted(self._indices)}."
            )
        with body.lock:
            current = self._body_at(camera_index)
            if current is None:
                # The body did not move; it went. A failing operation on
                # another thread drops the body it was using, and that is
                # what this looks like from here.
                raise RuntimeError(
                    f"Camera {camera_index} is not connected: {body.port} was "
                    f"dropped while this {operation} was waiting for it."
                )
            if current is not body:
                raise CameraMovedError(
                    f"Camera {camera_index} is no longer {body.port}; the "
                    f"cameras were re-laid out while this {operation} was "
                    "waiting, so it was refused rather than run on the wrong "
                    "body. Reload the device list and try again."
                )
            if not body.device.is_connected:
                raise RuntimeError(
                    f"Camera {camera_index} ({body.port}) is no longer "
                    "connected."
                )
            if refuse_unusable:
                refusal = self._row_error(body)
                if refusal:
                    raise RuntimeError(
                        f"Camera {camera_index} may not capture: {refusal}"
                    )
            yield body

    def capture_image(
        self,
        output_path: Path,
        camera_config,
        capture_output: bool = False,
    ) -> str:
        """Capture one still over USB and write it to output_path.

        The picture never touches the card: CHDK's remote capture hands the
        JPEG straight down the wire (pychdk's shoot(stream=True)), which is
        both faster and one less thing to go wrong in the field. The body has
        to be in record mode for that, which is done once and remembered.

        Only the two DSLR-ish fields of CameraConfig mean anything here -
        shutter speed and ISO - and either may be left alone. Everything else
        is picamera2's and is ignored.

        Args:
            output_path: Destination for the JPEG, written exactly as given.
                A name that is not a JPEG is refused: remote capture returns
                nothing else, and renaming it here would put the file where
                the capture service never checked for a collision.
            camera_config: CameraConfig; camera_index routes it.
            capture_output: Unused - kept for interface compatibility.

        Returns:
            Tuple of (path string, None), the shape the capture service reads.

        Raises:
            CaptureTimeoutError: The camera never delivered the bytes.
            RuntimeError: Anything else - a body that is refused, or an
                output name this backend cannot honour.
        """
        camera_index = getattr(camera_config, "camera_index", 0)
        shutter = _shutter_seconds(getattr(camera_config, "shutter_speed", None))
        iso = getattr(camera_config, "iso", None)
        destination = Path(output_path)
        if destination.suffix.lower() not in _CAPTURE_SUFFIXES:
            raise RuntimeError(
                f"CHDK remote capture returns a JPEG, so it cannot be saved "
                f"as {destination.suffix or 'a file with no suffix'} "
                f"({destination.name}). The camera's configured encoding and "
                "the name the capture service chose have to agree, and this "
                "backend must not choose a different one: the service checks "
                "that the name it picked is free, and a name chosen here "
                "would not have been checked."
            )

        with self._in_use(camera_index, "capture", refuse_unusable=True) as body:
            started = time.perf_counter()
            # The mode switch is part of the capture and fails the same way:
            # on a cold start it is the likeliest thing to time out, and it
            # has to reach the operator as a timeout rather than as a
            # generic failure.
            stage = "switching to record mode"
            try:
                self._ensure_record_mode(body)
                stage = "remote capture"
                image = body.device.shoot(
                    stream=True, shutter_speed=shutter, market_iso=iso
                )
            except TimeoutError as exc:
                elapsed = time.perf_counter() - started
                self.logger.error(
                    f"[chdk] body {camera_index} ({body.port}): {stage} timed "
                    f"out after {elapsed:.2f}s ({exc})"
                )
                self._evict(body, f"{stage} timed out")
                raise CaptureTimeoutError(
                    f"{body.port}: {stage} timed out after {elapsed:.1f}s"
                ) from exc
            except Exception as exc:
                elapsed = time.perf_counter() - started
                named = _name_failure(exc)
                self.logger.error(
                    f"[chdk] body {camera_index} ({body.port}): {stage} failed "
                    f"with {named} after {elapsed:.2f}s: {exc}"
                )
                # Whatever the class - a PTP response code, a pyusb transport
                # error, anything else - the conversation broke in the middle
                # and what the camera is doing now is unknown. The body is
                # dropped and the next scan opens it again.
                self._evict(body, f"{stage} failed with {named}")
                raise RuntimeError(
                    f"CHDK capture failed on {body.port} during {stage} with "
                    f"{named}: {exc}"
                ) from exc

            elapsed = time.perf_counter() - started
            if not image:
                self.logger.error(
                    f"[chdk] body {camera_index} ({body.port}): remote capture "
                    f"returned no bytes after {elapsed:.2f}s"
                )
                # The call returned, so nothing says where the conversation
                # ended - only that a shutter was asked for and nothing came
                # back. That is the same unknown camera state as any other
                # post-wire failure, and keeping the session would hand the
                # next capture a body whose CHDK capture state nobody has
                # established. The body is dropped and the next scan opens it
                # again.
                self._evict(body, "remote capture returned no bytes")
                raise RuntimeError(
                    f"CHDK capture on {body.port} returned no image data"
                )

            atomic_write(
                destination, lambda tmp: Path(tmp).write_bytes(bytes(image))
            )
            self.logger.info(
                f"[chdk] body {camera_index} ({body.port}): remote capture ok, "
                f"{len(image)} bytes in {elapsed:.2f}s from shutter to disk, "
                f"saved as {destination.name}"
            )
            return str(destination), None

    def capture_preview(self, camera_index: int) -> bytes:
        """Return one live-preview JPEG frame from the body at this index.

        The capture service routes every non-picamera2 backend here, so this
        is the whole polling path. The body's own lock is held for the frame,
        which is what keeps a poll from landing in the middle of a capture on
        the same USB endpoint.

        A body with no identity is previewable: it may not capture, but the
        operator still has to be able to aim it. A frame the decoder refuses
        is reported as a failed poll and nothing more - the body is fine, it
        just sent something this decoder does not read - whereas a PTP failure
        drops the body so the next enumeration re-opens it.

        Raises:
            RuntimeError: No body at this index, or the frame could not be
                fetched or decoded.
        """
        with self._in_use(camera_index, "preview", refuse_unusable=False) as body:
            stage = "switching to record mode"
            try:
                self._ensure_record_mode(body)
                stage = "live view"
                frame = body.device._chdk.get_display_data(LV_TFR_VIEWPORT)
            except Exception as exc:
                named = _name_failure(exc)
                self.logger.error(
                    f"[chdk] body {camera_index} ({body.port}): {stage} failed "
                    f"with {named}: {exc}"
                )
                self._evict(body, f"{stage} failed with {named}")
                raise RuntimeError(
                    f"CHDK preview failed on {body.port} during {stage} with "
                    f"{named}: {exc}"
                ) from exc

            try:
                jpeg, info = encode_viewport_jpeg(frame)
            except ValueError as exc:
                self.logger.error(
                    f"[chdk] body {camera_index} ({body.port}): live view "
                    f"frame could not be decoded: {exc}"
                )
                raise RuntimeError(
                    f"CHDK preview frame from {body.port} could not be "
                    f"decoded: {exc}"
                ) from exc

            self._log_frame(camera_index, body, info)
            return jpeg

    def _log_frame(self, camera_index, body, info):
        """Record the frame geometry once, and the frame rate every so often.

        The geometry answers what the viewport actually is on this model -
        which the bench would otherwise have to read off a hex dump - and the
        rate answers how fast it arrives, without a stopwatch. Callers hold
        the body's lock, so the counters need none of their own.
        """
        if not body.geometry_logged:
            body.geometry_logged = True
            self.logger.info(
                f"[chdk] body {camera_index} ({body.port}): live view "
                f"{info['version_major']}.{info['version_minor']}, "
                f"lcd aspect {info['lcd_aspect_ratio']}, "
                f"fb_type {info['fb_type']}, "
                f"buffer {info['buffer_width']} wide, "
                f"visible {info['visible_width']}x{info['visible_height']}, "
                f"margins l{info['margin_left']} t{info['margin_top']} "
                f"r{info['margin_right']} b{info['margin_bot']}, "
                f"jpeg {info['jpeg_width']}x{info['jpeg_height']} in "
                f"{info['jpeg_bytes']} bytes"
            )

        now = time.monotonic()
        if body.last_frame_at is not None:
            interval = now - body.last_frame_at
            body.interval_total += interval
            body.interval_count += 1
            body.interval_worst = max(body.interval_worst, interval)
        body.last_frame_at = now
        body.frames += 1

        counted = body.frames - body.frames_at_last_report
        if counted < self._FRAME_LOG_INTERVAL:
            return
        mean = (
            body.interval_total / body.interval_count
            if body.interval_count else 0.0
        )
        rate = f"{1 / mean:.1f} fps" if mean > 0 else "faster than the clock"
        self.logger.info(
            f"[chdk] body {camera_index} ({body.port}): {counted} preview "
            f"frames, mean interval {mean * 1000:.1f} ms ({rate}), "
            f"worst {body.interval_worst * 1000:.1f} ms"
        )
        body.frames_at_last_report = body.frames
        body.interval_total = 0.0
        body.interval_count = 0
        body.interval_worst = 0.0

    def _ensure_record_mode(self, body):
        """Put the body in record mode once, and confirm it got there.

        A viewport and a remote capture both need record mode, and the switch
        drives the lens, so it is done once per body rather than per frame -
        which is exactly why it has to be confirmed. The library's
        switch_mode polls for the camera to arrive and then returns whether
        or not it did: its loop can run out and it reports that the same way
        it reports success, by returning nothing. Taking the call as proof
        would leave every later capture and live view running from playback,
        with the flag set here suppressing any further attempt.

        So the camera is asked, on the convention the library's own loop
        polls on: get_mode() is falsy in record and nonzero in play. The flag
        is set only on that answer, so a switch that did not arrive is tried
        again by the next capture instead of being remembered as done.

        Callers hold the body's lock, and call this inside their own failure
        handling: a mode switch that times out is a capture timeout, and one
        that fails on the wire drops the body like any other failed
        conversation.
        """
        if body.in_record_mode:
            return
        body.device.switch_mode("record")
        if body.device.lua_execute("return get_mode()"):
            raise RuntimeError(
                f"{body.port}: the camera is still in play mode after being "
                "switched to record, so it can neither capture nor show a "
                "live view"
            )
        body.in_record_mode = True
        self.logger.info(f"[chdk] {body.port}: switched to record mode")

    def assign_side(self, camera_index: int, side: str) -> list:
        """Write a page parity onto the card of the body at this index.

        The parity lives on the camera's own card, so this is the only way to
        change it without a card reader: A/OWN.TXT is rewritten with the
        parity and the body's id, and every body is re-enumerated afterwards
        so the caller sees the layout the write produced rather than the one
        that went into it.

        The body's existing id is kept. A body that has none is given one,
        which is the point of assigning a parity to a compact whose USB serial
        pyusb cannot read: it is what lifts it out of provisional and lets it
        capture.

        The body is held from before the write until the new layout has been
        published, so nothing can use it while its card and its index
        disagree.

        Args:
            camera_index: Which body, as the device list numbers them.
            side: "odd" or "even", in any case.

        Returns:
            The device rows a rescan produced, the same shape list_devices
            returns. The body will usually have moved index.

        Raises:
            ValueError: side is not a page parity.
            SideConflictError: another connected body already shoots it.
                Two connected bodies cannot exchange parities directly; the
                error says how to do it in two steps.
            RuntimeError: no body at that index - including one dropped by a
                failing capture while this was waiting for it - the write
                failed, or the write landed but the rig could not be
                re-enumerated afterwards, in which case the body is dropped
                rather than left on a layout its card has left.
        """
        parity = str(side).strip().lower()
        if parity not in _SIDE_INDEX:
            raise ValueError(
                f"page parity must be one of {sorted(_SIDE_INDEX)}; got {side!r}"
            )

        # The check, the write and the republished layout are one operation.
        # Two requests for two unassigned bodies would otherwise both find the
        # parity free and both write it, and an enumeration landing in between
        # would publish a layout the write had already made wrong.
        with self._layout_lock:
            with self._map_lock:
                key = self._indices.get(camera_index)
                body = self._bodies.get(key) if key is not None else None
                if body is None:
                    raise RuntimeError(
                        f"Camera {camera_index} is not connected. Detected "
                        f"indices: {sorted(self._indices)}."
                    )
                holder = next(
                    (
                        other for other in self._bodies.values()
                        if other.side == parity and other.key != body.key
                    ),
                    None,
                )
                if holder is not None:
                    who = holder.serial or holder.camera_id or "no identity"
                    raise SideConflictError(
                        f"{holder.port} ({who}) already shoots {parity} pages. "
                        "Two connected bodies cannot exchange parities "
                        "directly: disconnect one, rescan, give the parity to "
                        "the body that is still connected, then reconnect the "
                        "other and give it the parity it should have."
                    )
                camera_id = body.camera_id
                if camera_id is None:
                    camera_id = secrets.token_hex(_CAMERA_ID_BYTES)
                elif body.serial is None and any(
                    other.key != body.key
                    and other.hardware_id is not None
                    and other.hardware_id == body.hardware_id
                    for other in self._bodies.values()
                ):
                    # This body answers to its card id, and another body
                    # answers to the same thing - two cards cut from one
                    # image. Writing it back would leave the pair as
                    # indistinguishable as it found them, and this route is
                    # the only repair tool the operator has, so the body is
                    # given an identity of its own.
                    #
                    # A body with a USB serial is deliberately not covered:
                    # it answers to that serial whatever its card says, so
                    # minting would destroy a good card id and repair
                    # nothing. The clash it is in belongs to the other body.
                    camera_id = secrets.token_hex(_CAMERA_ID_BYTES)
                    self.logger.info(
                        f"[chdk] body {camera_index} ({body.port}): its card "
                        f"id {body.camera_id} is another connected body's "
                        "identity as well; writing it a fresh one"
                    )

            payload = pychdk.format_own_txt(
                parity.upper(), camera_id
            ).encode("utf-8")
            # Waiting for the body is waiting, and a capture that fails in
            # that window evicts and closes it. So the same revalidation a
            # capture does: the index still has to mean this body, and its
            # device still has to be open, or the upload would go through a
            # session that is already closed.
            with self._in_use(
                camera_index, "side assignment", refuse_unusable=False
            ) as body:
                handle, temp_path = tempfile.mkstemp(
                    prefix="dtk_own_", suffix=".txt"
                )
                try:
                    with os.fdopen(handle, "wb") as scratch:
                        scratch.write(payload)
                    body.device.upload_file(temp_path, SIDE_FILE)
                except Exception as exc:
                    named = _name_failure(exc)
                    self.logger.error(
                        f"[chdk] body {camera_index} ({body.port}): writing "
                        f"{SIDE_FILE} failed with {named}: {exc}"
                    )
                    self._evict(body, f"writing {SIDE_FILE} failed with {named}")
                    raise RuntimeError(
                        f"Could not write {SIDE_FILE} on {body.port} "
                        f"({named}): {exc}"
                    ) from exc
                finally:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass

                self.logger.info(
                    f"[chdk] body {camera_index} ({body.port}): now shoots "
                    f"{parity} pages, id {camera_id}"
                )
                # The body stays reserved across the rescan, and that is the
                # point of doing it here rather than after. Between the write
                # and the new layout the card says one parity while the
                # published indices still say the other, and a capture that
                # got the body in that gap would pass its revalidation
                # against the layout being replaced and file its page under
                # an index the body no longer has. It would not fail; it
                # would quietly be wrong. The rescan re-enters this lock on
                # this thread, so holding it costs nothing but the wait it is
                # there to impose.
                try:
                    return self.rescan()
                except Exception as exc:
                    # The card has already changed, so the published layout
                    # is now a statement about a body that has left it. A
                    # rescan that fails leaves no way to correct that here,
                    # and leaving the body usable would accept a capture
                    # against an index it no longer has - the same wrong
                    # camera, reached through an error path. So it is
                    # dropped, which unmaps it and marks it for the retry
                    # every other failure gets, and the operator is told
                    # plainly that the write landed and the rig has not been
                    # re-read.
                    self.logger.error(
                        f"[chdk] body {camera_index} ({body.port}): "
                        f"{SIDE_FILE} was written but the rig could not be "
                        f"re-enumerated ({_name_failure(exc)}: {exc}); "
                        "dropping the body rather than leaving it on a layout "
                        "it has left"
                    )
                    self._evict(
                        body, f"{SIDE_FILE} was written but the rescan failed"
                    )
                    raise RuntimeError(
                        f"{SIDE_FILE} on {body.port} now says {parity}, but "
                        f"the cameras could not be re-enumerated afterwards "
                        f"({_name_failure(exc)}: {exc}), so the body was "
                        "dropped rather than left on the layout it has left. "
                        "Rescan once the bus is answering again."
                    ) from exc

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
            "dslr_settings": False,
        }

    def get_backend_name(self) -> str:
        return "chdk"

    def cleanup(self):
        """Close every open body, waiting for whatever is using it.

        When this returns, nothing is open. Two things are needed for that to
        be true rather than merely likely. It runs under the layout lock, so
        an enumeration cannot be in flight across it - a snapshot of the map
        taken here would otherwise miss a body that arrived while cleanup
        waited behind a capture, and leave it mapped with a live claim under
        a line saying every camera was closed. And it drains the map rather
        than walking a copy of it, so the invariant is enforced by the loop
        instead of assumed: nothing can add a body while the layout lock is
        held, and each close removes the one it closed.

        Each body is dropped by its own close, for the same reason as
        everywhere else: emptying the map first would let a capture keep
        shooting a camera that was already being closed.

        The backend stays usable afterwards. Forgetting that the bus was ever
        scanned is part of that: the next question about a camera looks at
        the hardware again rather than reporting the rig it has just closed
        as absent.
        """
        with self._layout_lock:
            while True:
                with self._map_lock:
                    bodies = list(self._bodies.values())
                if not bodies:
                    break
                for body in bodies:
                    self._close_body(body, "backend cleanup")
            with self._map_lock:
                self._scanned = False
                self._evicted.clear()
        self.logger.info("[chdk] all cameras closed.")
