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
import re
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
            viewport this decoder cannot read.
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
        # Set when another body claims the same parity; capture is refused
        # on both until the side route settles it.
        self.collision = None
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
        return re.sub(r"[^a-z0-9]", "", self.model.lower())

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
        body holds it: capture, preview, the card read. pychdk assumes one
        thread per device, and a dual capture runs one thread per index.
      - _map_lock guards which bodies are open and which index each holds.
        It is taken for short reads and for the one moment a new layout is
        published, never while waiting on a body's lock, so a capture that
        holds a body for thirty seconds never blocks an enumeration's view
        of the other one.
      - _enumerate_lock serialises whole enumerations, so two of them cannot
        open the same body twice or publish their layouts out of order.
      - Lock order is _enumerate_lock, then a body lock, then _map_lock. No
        path takes _map_lock and then waits for a body.
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
        # the bodies evicted by a failure, so their return can be logged as
        # the recovery it is
        self._evicted: set = set()
        self._map_lock = threading.Lock()
        self._enumerate_lock = threading.Lock()

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

    def _read_model(self, device, info):
        """The body's model, from its USB product string if it answers one.

        pyusb reads string descriptors over the wire and can fail or answer
        nothing, so the product id stands in. The model is cosmetic except
        that it is half of the hardware id, which is why the fallback is the
        id of the product rather than a guess at its name.
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

        EVEN is index 0 and ODD is index 1. A body with no parity takes the
        lowest index no parity has claimed, in USB order, so a single
        unassigned body is still usable.

        Two bodies claiming one parity is the case that must not be resolved
        by guessing: they keep their USB-order indices, so both stay
        addressable and the side route can fix either one, and both are
        marked so capture refuses until it is fixed.
        """
        claimants = {}
        for body in bodies:
            body.collision = None
            if body.side:
                claimants.setdefault(body.side, []).append(body)

        contested = [side for side, bs in claimants.items() if len(bs) > 1]
        if contested:
            for side in contested:
                for body in claimants[side]:
                    body.collision = (
                        f"two bodies are set to shoot {side} pages "
                        f"({', '.join(b.port for b in claimants[side])}); "
                        f"give one of them the other parity with {_SIDE_ROUTE} "
                        "before capturing"
                    )
            return {index: body.key for index, body in enumerate(bodies)}

        taken = {}
        for body in bodies:
            if body.side:
                taken[_SIDE_INDEX[body.side]] = body.key
        for body in bodies:
            if body.side:
                continue
            index = 0
            while index in taken:
                index += 1
            taken[index] = body.key
        return taken

    def _row_error(self, body):
        """Why this body may not capture, or None."""
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
            "error": self._row_error(body),
        }

    def _enumerate(self, reread):
        """Scan the bus, reconcile the open bodies with it, and lay them out.

        Bodies that have left are closed and dropped; bodies that are new are
        opened and read; bodies that were already open keep their device, and
        so their lock, so nothing in flight on them is disturbed. `reread`
        asks for every card to be read again, which is what a rescan is for:
        a parity written since the last scan is invisible otherwise.
        """
        with self._enumerate_lock:
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
                with self._map_lock:
                    body = self._bodies.get(key)
                if body is not None and not body.device.is_connected:
                    self._evict(body, "the device reports itself disconnected")
                    body = None
                fresh = body is None
                if fresh:
                    body = self._open_body(info)
                    if body is None:
                        continue
                if fresh or reread or not body.card_read:
                    if not self._read_side_file(body):
                        if not fresh:
                            self._evict(body, "its card could not be read")
                        else:
                            self._close_body(body, "its card could not be read")
                        continue
                if fresh:
                    with self._map_lock:
                        self._bodies[key] = body

            with self._map_lock:
                ordered = [
                    self._bodies[key] for key in present if key in self._bodies
                ]
                self._indices = self._assign_indices(ordered)
                rows = [
                    self._row(index, self._bodies[key])
                    for index, key in sorted(self._indices.items())
                ]

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
        refuse every capture. Later questions read what that found; a body
        that has gone is noticed by the failure of the next call on it, which
        evicts it, or by the next enumeration.
        """
        try:
            with self._map_lock:
                never_scanned = not self._indices and not self._bodies
            if never_scanned:
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
            output_path: Destination for the JPEG; the suffix is forced to
                .jpg, because remote capture only ever returns one.
            camera_config: CameraConfig; camera_index routes it.
            capture_output: Unused - kept for interface compatibility.

        Returns:
            Tuple of (path string, None), the shape the capture service reads.

        Raises:
            CaptureTimeoutError: The camera never delivered the bytes.
            RuntimeError: Anything else, including a body that is refused.
        """
        camera_index = getattr(camera_config, "camera_index", 0)
        shutter = _shutter_seconds(getattr(camera_config, "shutter_speed", None))
        iso = getattr(camera_config, "iso", None)
        destination = Path(output_path).with_suffix(".jpg")

        with self._in_use(camera_index, "capture", refuse_unusable=True) as body:
            self._ensure_record_mode(body)
            started = time.perf_counter()
            try:
                image = body.device.shoot(
                    stream=True, shutter_speed=shutter, market_iso=iso
                )
            except TimeoutError as exc:
                elapsed = time.perf_counter() - started
                self.logger.error(
                    f"[chdk] body {camera_index} ({body.port}): remote capture "
                    f"timed out after {elapsed:.2f}s ({exc})"
                )
                raise CaptureTimeoutError(
                    f"{body.port}: no image after {elapsed:.1f}s"
                ) from exc
            except Exception as exc:
                elapsed = time.perf_counter() - started
                code = getattr(exc, "code", None)
                named = (
                    f"PTP 0x{code:04x}" if isinstance(code, int)
                    else type(exc).__name__
                )
                self.logger.error(
                    f"[chdk] body {camera_index} ({body.port}): remote capture "
                    f"failed with {named} after {elapsed:.2f}s: {exc}"
                )
                if isinstance(exc, pychdk.PTPError):
                    self._evict(body, f"remote capture failed with {named}")
                raise RuntimeError(
                    f"CHDK capture failed on {body.port} with {named}: {exc}"
                ) from exc

            elapsed = time.perf_counter() - started
            if not image:
                self.logger.error(
                    f"[chdk] body {camera_index} ({body.port}): remote capture "
                    f"returned no bytes after {elapsed:.2f}s"
                )
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
            self._ensure_record_mode(body)
            try:
                frame = body.device._chdk.get_display_data(LV_TFR_VIEWPORT)
            except Exception as exc:
                code = getattr(exc, "code", None)
                named = (
                    f"PTP 0x{code:04x}" if isinstance(code, int)
                    else type(exc).__name__
                )
                self.logger.error(
                    f"[chdk] body {camera_index} ({body.port}): live view "
                    f"failed with {named}: {exc}"
                )
                self._evict(body, f"live view failed with {named}")
                raise RuntimeError(
                    f"CHDK preview failed on {body.port} with {named}: {exc}"
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
        """Put the body in record mode once; callers hold the body's lock.

        A viewport and a remote capture both need it, and the switch drives
        the lens, so it is not something to do per frame.
        """
        if body.in_record_mode:
            return
        body.device.switch_mode("record")
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

        Args:
            camera_index: Which body, as the device list numbers them.
            side: "odd" or "even", in any case.

        Returns:
            The device rows a rescan produced, the same shape list_devices
            returns. The body will usually have moved index.

        Raises:
            ValueError: side is not a page parity.
            SideConflictError: another connected body already shoots it.
            RuntimeError: no body at that index, or the write failed.
        """
        parity = str(side).strip().lower()
        if parity not in _SIDE_INDEX:
            raise ValueError(
                f"page parity must be one of {sorted(_SIDE_INDEX)}; got {side!r}"
            )

        # The check and the identity it is based on are read under the map
        # lock, so a rescan cannot move a body between deciding there is no
        # conflict and writing the card.
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
                    f"{holder.port} ({who}) already shoots {parity} pages; "
                    "give that body the other parity first"
                )
            camera_id = body.camera_id or secrets.token_hex(_CAMERA_ID_BYTES)

        payload = pychdk.format_own_txt(parity.upper(), camera_id).encode("utf-8")
        with body.lock:
            handle, temp_path = tempfile.mkstemp(prefix="dtk_own_", suffix=".txt")
            try:
                with os.fdopen(handle, "wb") as scratch:
                    scratch.write(payload)
                body.device.upload_file(temp_path, SIDE_FILE)
            except Exception as exc:
                self.logger.error(
                    f"[chdk] body {camera_index} ({body.port}): writing "
                    f"{SIDE_FILE} failed ({exc!r})"
                )
                raise RuntimeError(
                    f"Could not write {SIDE_FILE} on {body.port}: {exc}"
                ) from exc
            finally:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        self.logger.info(
            f"[chdk] body {camera_index} ({body.port}): now shoots {parity} "
            f"pages, id {camera_id}"
        )
        return self.rescan()

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

        Each body is dropped by its own close, rather than by clearing the
        map first: a shutdown that emptied the map and then closed the
        devices would let a capture keep shooting a camera that was already
        being closed, and would let an enumeration racing it open a second
        claim on the same body.
        """
        with self._map_lock:
            bodies = list(self._bodies.values())
        for body in bodies:
            self._close_body(body, "backend cleanup")
        self.logger.info("[chdk] all cameras closed.")
