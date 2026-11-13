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

Version: 0.1.0-beta
"""

import contextlib
import errno
import io
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

from .base import CameraBackend, normalise_camera_model
from .errors import CaptureTimeoutError
from ..utils import atomic_write
from . import usb_topology


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

# The role each parity suggests to the operator in the device list. Nothing
# places, gates or files a capture by it.
_ROLE_GUESS_BY_SIDE = {"even": "left", "odd": "right"}

# PTP_RC_GeneralError. A download of a file the card does not have comes back
# with this, and only this means "no such file"; any other response code is a
# body that has gone wrong and is evicted rather than read as unassigned.
_PTP_GENERAL_ERROR = 0x2002

# Model names by USB product id, for bodies whose USB product string is
# generic. The A2500 reports "Canon Digital Camera"; its pictures' EXIF says
# "Canon PowerShot A2500". Only the A2500 has been verified on hardware, so it
# is the only entry. Add another model here only after checking its EXIF name
# on a body.
_MODEL_BY_PRODUCT_ID = {
    0x3271: "Canon PowerShot A2500",
}

# What remote capture returns, and so the only output name this backend can
# honour. The capture service picks the name and guarantees it is free; a
# name that would have to be changed here is refused rather than written
# somewhere nothing checked.
_CAPTURE_SUFFIXES = (".jpg", ".jpeg")

# Record mode on a body that switch_mode_usb(1) left in playback. On
# 12 November 2026 two A2500 bodies under CHDK 1.5.1 accepted
# switch_mode_usb(1), extended the lens and went on reporting playback. A
# half-press of the shutter, click("shoot_half"), sent by hand completed the
# switch on those bodies without taking a picture. The condition was not tied
# to how the body was switched on, and the same bodies later hung and needed
# their batteries taken out, so it may belong to that fault. The half-press
# costs nothing on a body already in record mode. After it, get_mode() is
# polled every _RECORD_MODE_POLL_INTERVAL_S for up to
# _RECORD_MODE_HALF_PRESS_WAIT_S.
_RECORD_MODE_HALF_PRESS_WAIT_S = 4.0
_RECORD_MODE_POLL_INTERVAL_S = 0.5
_HALF_PRESS_LUA = 'click("shoot_half")'

# ALT mode is entered before the switch to record mode. Spreads, the earlier
# scanner software (spreadsplug/dev/chdkcamera.py), sends enter_alt() and then
# switch_mode('record') every time it prepares a camera, with the comment "Try
# to go into alt mode to prevent weird behaviour", and leaves the camera in
# ALT mode. CHDK's "Disable LCD Off" setting defaults to "Alt", which blocks
# the camera's power saving while ALT mode is on. On 12 November 2026 two
# A2500 bodies outside ALT mode, with Canon's "Auto Power Down" on, switched
# themselves off about three minutes after the last capture.
#
# Measured on the same day: with Canon's "Auto Power Down" on, two A2500
# bodies freshly put into ALT mode by enter_alt() stayed on through seven idle
# minutes with no USB traffic and then captured normally. The same bodies,
# after an operator had gone through the camera menus (which takes a body out
# of ALT mode; the backend had not sent enter_alt() again because
# in_record_mode was already set), behaved as follows about three minutes after
# the last capture: one switched itself off, and the other stayed attached to
# USB with its lens out and answered nothing until its battery was taken out.
# So ALT mode is asserted again during a session. Every still capture sends
# enter_alt() before the shot, except one that _ensure_record_mode has just
# sent it for. A preview sends it at most every _ALT_REASSERT_INTERVAL_S
# seconds per body, and a capture counts for that timer.
_ENTER_ALT_LUA = "enter_alt()"
_ALT_REASSERT_INTERVAL_S = 30.0

# Optical zoom. Measured on 12 November 2026 on an A2500 under CHDK 1.5.1:
# get_zoom_steps() returns 64, get_zoom() returns 0 at the widest, and
# set_zoom(n) moves the lens and takes one to a few seconds. Spreads, the
# earlier scanner software (spreadsplug/dev/chdkcamera.py, _set_zoom), set the
# zoom once per start with set_zoom(level) after the switch to record mode and
# refused a level at or above get_zoom_steps(). Here the step is stored per
# body in the camera registry and applied each time a body enters record mode.
#
# On the same day a second Lua script sent while an earlier one was still
# running, and commands sent to a body that had just timed out, knocked a
# camera off USB. Every zoom script runs under the body's lock and waits for
# its result before the next is sent, set_zoom has a timeout long enough for
# the lens to travel, and after a timeout or an error nothing more is sent to
# that body in the same operation.
_GET_ZOOM_STEPS_LUA = "return get_zoom_steps()"
_GET_ZOOM_LUA = "return get_zoom()"
_SET_ZOOM_TIMEOUT_S = 15.0

# One script at a time. CHDK runs one script at a time, and its ExecuteScript
# kills a running script unless the request carries PTP_CHDK_SCRIPT_FL_NOKILL
# (0x100 in CHDK's core/ptp.h; pychdk 0.1.3 names it ScriptFlag.NOKILL), in
# which case the camera refuses the new script instead.
#
# What happened on 12 November 2026, from a USB recording of an A2500 under
# CHDK 1.5.1 driven through pychdk 0.1.3: the capture script
# (init_usb_capture(1) ... shoot()) was still running on the camera for a
# short while after the host had received the picture's last chunk, and
# pychdk's shoot() returns as soon as the data is in. The backend sent its
# next script, enter_alt(), 0.2 s after the last chunk, while that capture
# script was still running, and CHDK killed it. The next shot's picture then
# arrived with the camera's per-shot remote-capture state not reset: its first
# chunk came flagged last (response parameters 1461760/0/17920, that is
# more=0 with a seek to offset 17920). pychdk stopped there, zero-filled the
# first 17920 bytes and returned 1,479,680 bytes, and the backend logged
# "remote capture ok". The next three captures each received one leftover
# chunk of that picture within 0.3 s, with no exposure, and were logged as ok
# too. Of 257 captures that day, the only ones affected were those where a
# running script was killed right after capture data; every healthy capture
# had at least 2.5 s before the next script.
#
# So every script the backend sends goes through _script, which reads the
# script status first and waits while a script is running, for up to
# _SCRIPT_END_WAIT_S. That check is the only place a script is waited for
# before another is sent. A whole capture returns as soon as its picture has
# passed the completeness check, without waiting for its script to end: on
# two A2500 bodies one camera's capture script ended about 0.5 s after its
# data and the other's at once, and waiting there made every pair about
# 0.5 s slower even when pairs were seconds apart. The next script sent to
# that body waits instead. Status reads are not scripts and are always
# allowed, as are RemoteCaptureIsReady and RemoteCaptureGetData.
_SCRIPT_NOKILL = 0x100
_SCRIPT_POLL_INTERVAL_S = 0.1
_SCRIPT_END_WAIT_S = 10.0
# A wait for a running script longer than this is logged as a warning.
_SCRIPT_SLOW_WAIT_S = 2.0
# How long a Lua script the backend sends is waited on, and how often its
# status is read meanwhile. These are pychdk 0.1.3's execute_lua_wait
# defaults, which the backend used before it sent scripts itself.
_LUA_TIMEOUT_S = 10.0
_LUA_POLL_INTERVAL_S = 0.05
# CHDK's script message types (ptp_chdk_script_msg_type, core/ptp.h).
_MSG_ERR = 1
_MSG_RET = 2

# A refused picture leaves the camera's capture state unknown, so before the
# body is used again the backend drains it: it waits for any script to end,
# reads and discards remote-capture data while the camera offers any (at most
# _DRAIN_MAX_CHUNKS chunks and _DRAIN_WAIT_S seconds), and then sends
# init_usb_capture(0) to clear the camera's capture state. A body that cannot
# be drained is dropped.
_DRAIN_MAX_CHUNKS = 8
_DRAIN_WAIT_S = 10.0
_REMOTE_CAP_JPEG = 0x01
_CLEAR_CAPTURE_LUA = "init_usb_capture(0)"

# A JPEG begins with the start-of-image marker and ends with the end-of-image
# marker. The 16:53 picture began with 17920 zero bytes.
_JPEG_START = b"\xff\xd8"
_JPEG_END = b"\xff\xd9"
# The Canon maker note (EXIF tag 0x927C) carries the image file number as tag
# 0x0008, a LONG; on 12 November 2026 two consecutive A2500 pictures carried
# 1490830 and 1490831.
_EXIF_IFD = 0x8769
_EXIF_MAKE = 0x010F
_EXIF_MAKER_NOTE = 0x927C
_CANON_FILE_NUMBER = 0x0008

# How pychdk 0.1.3's switch_mode says the camera never reported record mode.
# It raises a plain RuntimeError, as it does for a script that would not
# start or one that failed, so the message is the only thing that tells this
# failure from the others.
_PYCHDK_DID_NOT_SWITCH = "The camera did not switch to record mode"

# Every wait in this module goes through this name, so a test can record the
# waits instead of spending them.
_sleep = time.sleep
_monotonic = time.monotonic

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


class _BodyGoneError(RuntimeError):
    """An operation failed because its body has left the bus.

    The body has already been dropped when this is raised. `identity` is the
    body's hardware id, or None when it had none, so the caller can tell
    whether the body now at the index is the same camera.
    """

    identity = None


class _IdentityChangedError(RuntimeError):
    """The body locked for a retry is not the one the retry was meant for."""


class IncompleteCaptureError(RuntimeError):
    """The camera delivered a picture that is not whole, and it was not saved.

    The message says what was wrong and names the port. The capture is not
    retried: the operator has to see that it failed and take it again.
    """


class _ScriptStillRunningError(TimeoutError):
    """A script was still running on the camera when the wait for it ran out.

    A TimeoutError, so a capture reports it as a capture timeout and every
    operation evicts the body as it does for any other timeout.
    """


def _canon_file_number(image):
    """The Canon image file number in a JPEG's EXIF maker note, or None.

    Pillow opens the JPEG lazily and reads only its header segments, so this
    does not decode the picture. A picture with no EXIF, one that is not from
    a Canon camera, or one whose maker note has no tag 0x0008 of type LONG
    gives None.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image)) as picture:
            exif = picture.getexif()
            make = exif.get(_EXIF_MAKE)
            note = exif.get_ifd(_EXIF_IFD).get(_EXIF_MAKER_NOTE)
            endian = getattr(exif, "endian", None) or "<"
    except Exception:
        return None
    if isinstance(make, bytes):
        make = make.decode("ascii", errors="ignore")
    if not isinstance(make, str) or not make.strip().lower().startswith("canon"):
        return None
    if not isinstance(note, bytes) or len(note) < 2:
        return None
    try:
        (count,) = struct.unpack_from(endian + "H", note, 0)
        for entry in range(count):
            offset = 2 + 12 * entry
            if offset + 12 > len(note):
                return None
            tag, kind, number, value = struct.unpack_from(
                endian + "HHII", note, offset
            )
            if tag == _CANON_FILE_NUMBER and kind == 4 and number == 1:
                return value
    except struct.error:
        return None
    return None


class ZoomFailedError(RuntimeError):
    """The camera did not take a requested zoom step.

    Raised by set_zoom_step for a wire failure, a timeout, a camera that
    reports another step afterwards, and a camera that reported no zoom
    steps. Its own class because the route answers it with a 502, and a
    camera that is not connected with a 404.
    """


class _ZoomNotConfirmedError(ZoomFailedError):
    """The camera answered after set_zoom and reported another step.

    The conversation completed, so the body is in a known state and need not
    be dropped for this alone; the step it reports is recorded on the body.
    """


def _open_camera_registry():
    """Open the camera registry at its default path.

    An error opening it propagates; _stored_zoom_step catches it and
    _store_zoom_step does not. Function-local import: capture.camera_registry reaches capture.service,
    which builds this backend, so a module-level import would be circular.
    """
    from capture.camera_registry import CameraRegistry

    return CameraRegistry()


def _is_gone_from_the_bus(exc):
    """True for a pyusb error whose errno is ENODEV: the device is not there."""
    return getattr(exc, "errno", None) == errno.ENODEV


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
        # When ALT mode was last asserted, by _monotonic; None on a body that
        # has not had it asserted since it was opened.
        self.alt_asserted_at = None
        # Optical zoom. zoom_steps is get_zoom_steps(), read once after the
        # body first reaches record mode; None until then, or when the camera
        # does not answer a positive integer. zoom_step is the step the
        # backend last read back with get_zoom(); None until known.
        self.zoom_steps = None
        self.zoom_step = None
        # The Canon image file number of the last picture saved from this
        # body, when its EXIF carried one; None otherwise.
        self.last_file_number = None
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
        """The USB serial with NUL characters and surrounding whitespace removed.

        pychdk 0.1.3 returns the A2500's serial with a trailing NUL byte. The
        raw value cannot be stored in a PostgreSQL text column, so every
        reader of the serial uses this property and none reads
        info.serial_num. None means the body reports no serial.
        """
        cleaned = (self.info.serial_num or "").replace("\x00", "").strip()
        return cleaned or None

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
        # Opens the camera registry, where a body's stored zoom step lives.
        # A test replaces it; a factory that returns None means no registry,
        # so nothing is read or stored.
        self._registry_factory = _open_camera_registry

    # ------------------------------------------------------------------
    # Bodies
    # ------------------------------------------------------------------

    def _body_at(self, camera_index):
        """The body holding this index right now, or None."""
        with self._map_lock:
            key = self._indices.get(camera_index)
            return self._bodies.get(key) if key is not None else None

    def camera_model(self, camera_index):
        """The model of the body holding this index.

        _ChdkBody.model starts as the table's name for the product id, else
        the USB product string, else the product id label, read once in
        _open_body. The EXIF model of each saved picture replaces it after
        every capture (_take_model_from_picture). Nothing here
        talks to the camera or scans the bus: an index no open body holds,
        including every index before the first scan, is None.
        """
        body = self._body_at(camera_index)
        return normalise_camera_model(body.model) if body is not None else None

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
        """The body's model: the table's name for its product id, else its USB
        product string if it answers one.

        pyusb reads string descriptors over the wire and can fail or answer
        nothing, so the product id stands in. This is for people to read -
        the device list, the log, the registry row - and nothing depends on
        it: the hardware id is built from the product id instead, so a
        failed read costs a name and never an identity.
        """
        known = _MODEL_BY_PRODUCT_ID.get(info.product_id)
        if known:
            return known
        try:
            product = getattr(device._usb_device, "product", None)
        except Exception:
            product = None
        if product:
            return str(product).replace("\x00", "").strip()
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
        serial; publishing outside the lock would let a rescan clear it between
        the check and the shutter, and a page would be written for a body that
        by then had no identity to record it under. Under the lock a capture sees the
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
        and the rest fall in with every other unplaced body, so both stay
        addressable and the side route can fix either; both are marked, and
        capture refuses them until it is. A free index is not always a higher
        one: two ODD bodies reserve only index 1, so the second of them takes
        index 0, which no parity has claimed.

        What a contest must not do is move a body that holds an uncontested
        parity. That is the whole of the guarantee, and it is narrower than
        it sounds in two directions. A body with no parity holds no index of
        its own and is pushed along by a claimant that arrives ahead of it: a
        second EVEN claimant takes index 1 and an unassigned body sitting
        there moves to 2, with nothing wrong with it and no refusal on it.
        And a body whose parity becomes contested moves as readily - a
        claimant earlier in bus order takes the parity's index, and the body
        that held it alone until then takes the next free one - which is
        harmless only because the contest refuses them both.

        A move is published by the same scan that makes it, and what keeps a
        capture off the wrong body across one is two mechanisms, neither
        sufficient alone. A capture that resolved an index but had not yet
        taken the body's lock revalidates once it has it, and is refused
        rather than run on whichever body holds that index now. A capture
        that already holds the lock is not refused at all: it runs to
        completion against the layout it was admitted under, because
        _enumerate takes every open body before it publishes and so waits for
        it. Revalidation covers the captures that straddle a publication; the
        barrier covers the ones that do not.

        Moving a body that holds an uncontested parity is the failure this
        rule prevents: a layout that fell back to USB order for everything
        would put a healthy ODD body on index 0, where it would carry no
        refusal of its own and shoot odd pages the service filed as even
        ones - no failure, no log line, the pages simply in the wrong place.
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
        #
        # _SIDE_INDEX is indexed directly: a side it does not hold raises here
        # rather than being quietly laid on whichever index was free, which is
        # the failure _read_side_file is careful about. The invariant is
        # upstream - assign_side refuses a parity this map does not hold, and
        # every other side is whatever pychdk.parse_own_txt answered - so a
        # value outside it is a bug elsewhere, and a scan that stops is a
        # better answer to that than a body placed by a rule nobody wrote.
        reserved = {_SIDE_INDEX[side] for side in claimants}
        taken = {}
        for side, sharing in claimants.items():
            taken[_SIDE_INDEX[side]] = sharing[0].key

        # Everything not placed by its own parity, in USB order: the second
        # and later claimants of a contested side and the bodies with no
        # parity alike, each taking the lowest index no parity has claimed and
        # nothing else has taken.
        for body in bodies:
            if body.side and claimants[body.side][0] is body:
                continue
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
            # supports_zoom is the Pi cameras' digital ScalerCrop zoom. A CHDK
            # body has stepped optical zoom instead, reported by these two.
            "supports_zoom": False,
            "zoom_steps": body.zoom_steps,
            "zoom_step": body.zoom_step,
            "side": body.side,
            "provisional": provisional,
            # Another connected body answers to this one's hardware id, so
            # the id identifies two cameras and cannot be recorded against
            # either. The row is still reported: seeing both is how the
            # operator repairs it.
            "identity_ambiguous": body.identity_clash is not None,
            "error": self._row_error(body),
            # The USB socket the body is plugged into, as a topology path
            # ("1-1.2"), from pyusb's enumeration record. None where pyusb
            # cannot say.
            "usb_port": usb_topology.from_pyusb_device(
                getattr(body.device, "_usb_device", None)
            ),
            # The role the card parity suggests: EVEN shoots the left page
            # and ODD the right. It is shown to the operator as a starting
            # point and nothing acts on it.
            "role_guess": _ROLE_GUESS_BY_SIDE.get(body.side),
            "role_guess_source": "own_txt" if body.side in _ROLE_GUESS_BY_SIDE else None,
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
    def _in_use(
        self, camera_index, operation, refuse_unusable, expect_identity=None
    ):
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
            expect_identity: Set only for a retry. The hardware id the body
                must have, checked on the body actually locked, after the
                lock is taken.

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
            if expect_identity is not None and body.hardware_id != expect_identity:
                raise _IdentityChangedError(
                    f"Camera {camera_index} is {body.port}, which is not the "
                    f"body ({expect_identity}) this {operation} was retried for"
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

    def _retry_once_if_gone(self, camera_index, operation, run):
        """Run `run(expect_identity)`; if its body had left the bus, retry once.

        A camera that restarts with its USB cable connected leaves the bus
        and returns at a new device number. The first request after that
        fails on the old session with ENODEV, after which the body has been
        dropped. This rescans once, and when the body now at the index has the
        same hardware identity as the one that failed, runs `run` once more,
        passing that identity so the retry checks it again on the body it
        locks (a layout can be published between this check and the lock).

        The first error is raised when there is no identity, when the rescan
        fails, when the body at the index is not the same one, when the
        locked body is not the same one, and when the retry fails with
        another _BodyGoneError. Any other exception from the retry
        propagates as itself. No lock is held here: the failed body's lock
        was released when `run` raised.
        """
        try:
            return run(None)
        except _BodyGoneError as gone:
            if gone.identity is None:
                raise
            self.logger.info(
                f"[chdk] camera {camera_index}: {operation} failed because "
                f"the body left the bus; rescanning once for {gone.identity}"
            )
            try:
                self.rescan()
            except Exception as exc:
                self.logger.error(
                    f"[chdk] camera {camera_index}: rescan after a body left "
                    f"the bus failed: {exc!r}"
                )
                raise gone
            current = self._body_at(camera_index)
            if current is None or current.hardware_id != gone.identity:
                self.logger.info(
                    f"[chdk] camera {camera_index}: not retrying {operation}, "
                    f"{gone.identity} is not the body at that index now"
                )
                raise gone
            self.logger.info(
                f"[chdk] camera {camera_index}: retrying {operation} on "
                f"{current.port}"
            )
            try:
                return run(gone.identity)
            except (_BodyGoneError, _IdentityChangedError):
                raise gone

    def capture_image(
        self,
        output_path: Path,
        camera_config,
        capture_output: bool = False,
    ) -> str:
        """Capture one still over USB; see _capture_image_once.

        A body that has left the bus is replaced and the capture retried
        once, by _retry_once_if_gone.
        """
        camera_index = getattr(camera_config, "camera_index", 0)
        return self._retry_once_if_gone(
            camera_index, "capture",
            lambda expect: self._capture_image_once(
                output_path, camera_config, capture_output, expect
            ),
        )

    def _capture_image_once(
        self,
        output_path: Path,
        camera_config,
        capture_output: bool = False,
        expect_identity=None,
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

        with self._in_use(
            camera_index, "capture", refuse_unusable=True,
            expect_identity=expect_identity,
        ) as body:
            started = time.perf_counter()
            # The mode switch is part of the capture and fails the same way:
            # on a cold start it is the likeliest thing to time out, and it
            # has to reach the operator as a timeout rather than as a
            # generic failure.
            stage = "switching to record mode"
            try:
                just_sent = self._ensure_record_mode(body)
                if not just_sent:
                    stage = "entering ALT mode"
                    self._assert_alt(body)
                stage = "remote capture"
                # pychdk's shoot() sends the capture script itself, without
                # the NOKILL flag, so the status check in _script is the only
                # guard on it. shoot() returns once the data is in, and the
                # capture script can still be running then; the next script
                # sent to this body waits for it in _script.
                image = self._script(
                    body, "the capture script",
                    lambda: body.device.shoot(
                        stream=True, shutter_speed=shutter, market_iso=iso
                    ),
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
                identity = body.hardware_id
                self._evict(body, f"{stage} failed with {named}")
                error = (
                    _BodyGoneError if _is_gone_from_the_bus(exc) else RuntimeError
                )(
                    f"CHDK capture failed on {body.port} during {stage} with "
                    f"{named}: {exc}"
                )
                if isinstance(error, _BodyGoneError):
                    error.identity = identity
                raise error from exc

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

            problem, file_number = self._picture_problem(body, image)
            if problem is not None:
                self._refuse_picture(camera_index, body, image, problem)

            atomic_write(
                destination, lambda tmp: Path(tmp).write_bytes(bytes(image))
            )
            self.logger.info(
                f"[chdk] body {camera_index} ({body.port}): remote capture ok, "
                f"{len(image)} bytes in {elapsed:.2f}s from shutter to disk, "
                f"saved as {destination.name}"
            )
            body.last_file_number = file_number
            self._take_model_from_picture(body, destination)
            return str(destination), None

    def _picture_problem(self, body, image):
        """Say what is wrong with a picture, or None when it is whole.

        Returns (problem, file_number). problem is a plain sentence fragment
        or None; file_number is the Canon image file number read from the
        picture's EXIF, or None when it cannot be read.

        pychdk 0.1.3 keeps only a count of the chunks a capture arrived in
        (last_capture_chunks), not their sizes, seek positions or "more"
        flags, so the backend cannot check that the chunks covered the whole
        file. A picture assembled around a hole at its start begins with the
        zero bytes pychdk filled the hole with, so the start marker check
        refuses it. A hole further in, between a whole header and a whole
        tail, is not detected here.
        """
        if image[:2] != _JPEG_START:
            return (
                "it does not begin with the JPEG start marker FF D8 (it "
                f"begins with {image[:2].hex(' ').upper()}), so the camera "
                "sent it without its beginning",
                None,
            )
        if image[-2:] != _JPEG_END:
            return (
                "it does not end with the JPEG end marker FF D9, so the "
                "camera sent it without its end",
                None,
            )
        file_number = _canon_file_number(image)
        if (
            file_number is not None
            and body.last_file_number is not None
            and file_number == body.last_file_number
        ):
            return (
                f"it carries the same image file number, {file_number}, as "
                "the previous picture from this camera",
                file_number,
            )
        return None, file_number

    def _refuse_picture(self, camera_index, body, image, problem):
        """Log a picture that is not whole, drain the body, and raise.

        Nothing is written. The body is drained so that the next capture
        starts clean, or dropped when it cannot be drained. The capture is
        not retried.

        Raises:
            IncompleteCaptureError: Always.
        """
        chunks = getattr(body.device, "last_capture_chunks", None)
        self.logger.error(
            f"[chdk] body {camera_index} ({body.port}): remote capture "
            f"refused, {problem}: {len(image)} bytes in {chunks} chunk(s) "
            "(pychdk 0.1.3 reports only the number of chunks); nothing was "
            "saved"
        )
        drained = self._drain_capture(camera_index, body)
        message = (
            f"The picture from {body.port} is not whole: {problem}. It was "
            "not saved. Take the picture again."
        )
        if not drained:
            message += (
                " The camera's capture state could not be cleared, so the "
                "camera was disconnected; it is opened again on the next scan."
            )
        raise IncompleteCaptureError(message)

    def _drain_capture(self, camera_index, body):
        """Clear what a refused capture left on the camera. False if it could not.

        Only requests that are not scripts are used until no script is
        running: the script status is read until no script runs (for up to
        _SCRIPT_END_WAIT_S), and remote-capture data is read and discarded
        while the camera reports any ready, at most _DRAIN_MAX_CHUNKS chunks
        and for at most _DRAIN_WAIT_S seconds. Then init_usb_capture(0) is sent through
        _script to clear the camera's capture state. A failure of any step
        drops the body.
        """
        discarded = []
        try:
            self._wait_for_no_script(body)
            chdk = body.device._chdk
            started = _monotonic()
            while True:
                ready, status = chdk.remote_capture_is_ready()
                if not ready:
                    break
                if (
                    len(discarded) >= _DRAIN_MAX_CHUNKS
                    or _monotonic() - started >= _DRAIN_WAIT_S
                ):
                    raise RuntimeError(
                        "the camera still offered capture data after "
                        f"{len(discarded)} chunks had been discarded"
                    )
                wanted = (
                    _REMOTE_CAP_JPEG if status & _REMOTE_CAP_JPEG
                    else status & -status
                )
                data, more, position = chdk.remote_capture_get_chunk(wanted)
                discarded.append((len(data), more, position))
            self._send_lua(body, _CLEAR_CAPTURE_LUA)
        except Exception as exc:
            named = _name_failure(exc)
            self.logger.error(
                f"[chdk] body {camera_index} ({body.port}): clearing the "
                f"camera's capture state failed with {named}: {exc}; "
                f"discarded chunks (size, more, seek): {discarded}"
            )
            self._evict(body, f"clearing the capture state failed with {named}")
            return False
        self.logger.warning(
            f"[chdk] body {camera_index} ({body.port}): capture state cleared; "
            f"discarded chunks (size, more, seek): {discarded}"
        )
        return True

    # ------------------------------------------------------------------
    # Scripts
    # ------------------------------------------------------------------

    def _wait_for_no_script(self, body, limit=_SCRIPT_END_WAIT_S):
        """Read the script status until no script is running on the body.

        The status is read every _SCRIPT_POLL_INTERVAL_S for up to `limit`
        seconds. A read is not a script, so it never disturbs one. Callers
        hold the body's lock.

        Returns:
            (reads, seconds): how many status reads it took, and how long.

        Raises:
            _ScriptStillRunningError: A script is still running at the end.
        """
        chdk = body.device._chdk
        started = _monotonic()
        deadline = started + limit
        most = int(round(limit / _SCRIPT_POLL_INTERVAL_S)) + 1
        reads = 0
        while True:
            running, _ = chdk.get_script_status()
            reads += 1
            if not running:
                return reads, _monotonic() - started
            if reads >= most or _monotonic() >= deadline:
                raise _ScriptStillRunningError(
                    f"a script was still running on the camera after "
                    f"{limit:.1f}s"
                )
            _sleep(_SCRIPT_POLL_INTERVAL_S)

    def _script(self, body, what, send):
        """Send one script to the body, once no script is running on it.

        Every script the backend sends goes through here: the Lua it sends
        itself (by way of _send_lua) and the scripts pychdk's shoot() and
        switch_mode() send. `what` names the script for the log. The script
        status is read once, and while a script is running it is read again
        every _SCRIPT_POLL_INTERVAL_S for up to _SCRIPT_END_WAIT_S. A wait is
        logged at debug, or at warning when it took more than
        _SCRIPT_SLOW_WAIT_S. A script still running at the end fails the
        operation as a timeout does, and `send` is not called.

        This is where the capture script that a previous capture left
        running is waited for: a capture returns as soon as its picture is
        whole.

        Returns:
            Whatever `send` returns.
        """
        reads, waited = self._wait_for_no_script(body)
        if reads > 1:
            line = (
                f"[chdk] {body.port}: waited {waited:.2f}s ({reads} status "
                f"reads) for a running script to end before sending {what}"
            )
            if waited > _SCRIPT_SLOW_WAIT_S:
                self.logger.warning(line)
            else:
                self.logger.debug(line)
        return send()

    def _send_lua(self, body, code, timeout=_LUA_TIMEOUT_S):
        """Run one Lua script with the NOKILL flag and return its result.

        This does what pychdk 0.1.3's lua_execute does (execute_lua_wait:
        clear stale messages, start the script, wait for its RET message),
        with two differences: the script is started with NOKILL, so a camera
        that is still running a script refuses this one instead of killing
        the other, and the waits go through _sleep and _monotonic.

        Returns:
            The script's first return value, or None when it ended without
            one.

        Raises:
            _ScriptStillRunningError: A script was running and did not end.
            RuntimeError: The camera refused the script or reported an error.
            TimeoutError: The script did not finish within `timeout`.
        """
        return self._script(
            body, code, lambda: self._run_lua(body, code, timeout)
        )

    def _run_lua(self, body, code, timeout):
        chdk = body.device._chdk
        chdk.drain_messages()
        script_id = chdk.execute_script(code, flags=_SCRIPT_NOKILL)
        deadline = _monotonic() + timeout
        # A bound on reads as well as on time, so a clock that does not move
        # cannot keep this loop going.
        most = int(round(timeout / _LUA_POLL_INTERVAL_S)) + 1
        reads = 0
        while True:
            running, has_messages = chdk.get_script_status()
            reads += 1
            if has_messages:
                message = chdk.read_script_message()
                if message.script_id == script_id:
                    if message.msg_type == _MSG_RET:
                        return message.value
                    if message.msg_type == _MSG_ERR:
                        raise RuntimeError(
                            f"Script error in {code!r}: {message.value}"
                        )
            elif not running:
                return None
            if reads >= most or _monotonic() >= deadline:
                raise TimeoutError(
                    f"Script {code!r} did not complete within {timeout}s"
                )
            if not has_messages:
                _sleep(_LUA_POLL_INTERVAL_S)

    def _take_model_from_picture(self, body, path):
        """Set the body's model from the EXIF Model tag of the picture just saved.

        The picture names the camera better than the USB descriptors do: the
        A2500's USB product string is the generic "Canon Digital Camera". The
        tag is cleaned and passed through normalise_camera_model; a file with
        no EXIF, an unreadable file or a placeholder value changes nothing.
        The capture service reads camera_model after capture_image returns,
        so the first picture's recorded model is this one. Nothing here may
        fail the capture.
        """
        try:
            from PIL import Image

            with Image.open(path) as picture:
                raw = picture.getexif().get(0x0110)
        except Exception as exc:
            self.logger.debug(
                f"[chdk] {body.port}: no EXIF model read from {path.name}: "
                f"{exc!r}"
            )
            return
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", errors="ignore")
        if not isinstance(raw, str):
            return
        model = normalise_camera_model(raw.replace("\x00", "").strip())
        if model is None:
            self.logger.debug(
                f"[chdk] {body.port}: EXIF model of {path.name} names no camera"
            )
            return
        if model != body.model:
            self.logger.info(
                f"[chdk] {body.port}: model is {model!r} by the EXIF of "
                f"{path.name} (was {body.model!r})"
            )
            body.model = model

    def capture_preview(self, camera_index: int) -> bytes:
        """Return one live-preview JPEG frame; see _capture_preview_once.

        A body that has left the bus is replaced and the request retried
        once, by _retry_once_if_gone.
        """
        return self._retry_once_if_gone(
            camera_index, "preview",
            lambda expect: self._capture_preview_once(camera_index, expect),
        )

    def _capture_preview_once(
        self, camera_index: int, expect_identity=None
    ) -> bytes:
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
        with self._in_use(
            camera_index, "preview", refuse_unusable=False,
            expect_identity=expect_identity,
        ) as body:
            stage = "switching to record mode"
            try:
                just_sent = self._ensure_record_mode(body)
                if not just_sent and (
                    body.alt_asserted_at is None
                    or _monotonic() - body.alt_asserted_at
                    >= _ALT_REASSERT_INTERVAL_S
                ):
                    stage = "entering ALT mode"
                    self._assert_alt(body)
                stage = "live view"
                frame = body.device._chdk.get_display_data(LV_TFR_VIEWPORT)
            except Exception as exc:
                named = _name_failure(exc)
                self.logger.error(
                    f"[chdk] body {camera_index} ({body.port}): {stage} failed "
                    f"with {named}: {exc}"
                )
                identity = body.hardware_id
                self._evict(body, f"{stage} failed with {named}")
                error = (
                    _BodyGoneError if _is_gone_from_the_bus(exc) else RuntimeError
                )(
                    f"CHDK preview failed on {body.port} during {stage} with "
                    f"{named}: {exc}"
                )
                if isinstance(error, _BodyGoneError):
                    error.identity = identity
                raise error from exc

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

    def _assert_alt(self, body):
        """Send enter_alt() and note when. A failure propagates and notes nothing."""
        self._send_lua(body, _ENTER_ALT_LUA)
        body.alt_asserted_at = _monotonic()

    def _ensure_record_mode(self, body):
        """Put the body in record mode once, and confirm it got there.

        A viewport and a remote capture both need record mode, and the switch
        drives the lens, so it is done once per body rather than per frame -
        which is exactly why it has to be confirmed.

        On 12 November 2026 two A2500 bodies under CHDK 1.5.1 accepted
        switch_mode_usb(1), extended the lens and went on reporting playback.
        A half-press of the shutter (click("shoot_half")) sent by hand
        completed the switch on those bodies without taking a picture. The
        condition was not tied to how the body was switched on, and the same
        bodies later hung and needed their batteries taken out, so it may
        belong to that fault. A half-press sent to a body already in record
        mode costs nothing.

        So the switch is asked for first, and a half-press is sent only when
        it has not arrived. pychdk 0.1.3's switch_mode polls get_mode itself
        and raises a RuntimeError once its retries run out; that error, and
        only that one, is taken to mean the switch has not arrived. pychdk
        0.1.2 does not raise: its loop runs out and returns as it does on
        success, so the camera is asked directly afterwards as well. Any
        other failure of the switch - a transport error, a timeout, a script
        that would not run - propagates unchanged and no half-press is sent.

        After the half-press, get_mode is polled for a bounded time. If the
        body is still in playback at the end of it, the error tells the
        operator to switch the camera off and on again, and to take the
        battery out if it does not respond to its buttons.

        The question is Lua, not uBASIC, and the two read opposite ways
        round: CHDK's Lua get_mode() returns three values - is_record,
        is_video, mode - and the first of them is true in record and false in
        play. The library returns the script's first return value, so what
        comes back here is is_record, and a truthy answer is the camera
        confirming it arrived. The flag is set only on that answer, so a
        switch that did not arrive is tried again by the next capture instead
        of being remembered as done.

        Before the switch, enter_alt() is sent, as Spreads did (see
        _ENTER_ALT_LUA). With ALT mode on, two A2500 bodies with Canon's
        "Auto Power Down" on stayed on through seven idle minutes (measured
        on 12 November 2026). Returns True when this call sent enter_alt(),
        and False when the body was already in record mode and nothing was
        sent; the capture and preview paths use that to avoid sending it
        twice.

        Once record mode is confirmed, _settle_zoom reads the body's zoom
        steps and current step and moves the lens to its stored step. A
        failure there is a failure of this method like any other.

        Callers hold the body's lock, and call this inside their own failure
        handling: a mode switch that times out is a capture timeout, and one
        that fails on the wire drops the body like any other failed
        conversation.
        """
        if body.in_record_mode:
            return False
        # A failure here propagates like a failure of the switch, and no
        # switch is attempted after it.
        self._assert_alt(body)
        try:
            # pychdk's switch_mode() sends switch_mode_usb itself, without
            # the NOKILL flag, so the status check in _script is the only
            # guard on it.
            self._script(
                body, "switch_mode_usb(1)",
                lambda: body.device.switch_mode("record"),
            )
        except RuntimeError as exc:
            if not str(exc).startswith(_PYCHDK_DID_NOT_SWITCH):
                raise
            not_arrived = exc
        else:
            not_arrived = None
            if self._send_lua(body, "return get_mode()"):
                body.in_record_mode = True
                self.logger.info(f"[chdk] {body.port}: switched to record mode")
                self._settle_zoom(body)
                return True

        self._send_lua(body, _HALF_PRESS_LUA)
        waited = 0.0
        while waited < _RECORD_MODE_HALF_PRESS_WAIT_S:
            _sleep(_RECORD_MODE_POLL_INTERVAL_S)
            waited += _RECORD_MODE_POLL_INTERVAL_S
            if self._send_lua(body, "return get_mode()"):
                body.in_record_mode = True
                self.logger.info(
                    f"[chdk] {body.port}: switched to record mode after a "
                    f"half-press of the shutter ({waited:.1f}s after it)"
                )
                self._settle_zoom(body)
                return True

        raise RuntimeError(
            f"{body.port}: the camera did not enter shooting mode, so it can "
            "neither capture nor show a live view. Switch the camera off "
            "and on again. If it does not respond to its buttons, take the "
            "battery out and put it back."
        ) from not_arrived

    # ------------------------------------------------------------------
    # Optical zoom
    # ------------------------------------------------------------------

    def _read_zoom(self, body):
        """Ask the camera for its zoom step; record and return it.

        The answer is recorded as body.zoom_step when it is an int, and as
        None otherwise. Callers hold the body's lock.
        """
        answer = self._send_lua(body, _GET_ZOOM_LUA)
        body.zoom_step = answer if type(answer) is int else None
        return body.zoom_step

    def _move_zoom(self, body, step):
        """Send set_zoom(step), then read the step back.

        _send_lua waits for set_zoom to finish before it returns, and _script
        reads the script status before the read-back, so the read-back is
        never sent while set_zoom is still running. A timeout
        or an error from either script propagates and nothing more is sent.

        Raises:
            _ZoomNotConfirmedError: The camera reports another step.
        """
        self._send_lua(body, f"set_zoom({step})", timeout=_SET_ZOOM_TIMEOUT_S)
        reported = self._read_zoom(body)
        if reported != step:
            raise _ZoomNotConfirmedError(
                f"{body.port}: the lens was sent to zoom step {step} and the "
                f"camera reports step {reported}"
            )
        self.logger.info(f"[chdk] {body.port}: zoom step {step} confirmed")

    def _stored_zoom_step(self, body):
        """The zoom step stored for this body, or None.

        A body with no identity has nothing stored, and an ambiguous one has
        an identity that another body answers to, so neither is looked up. A
        registry that cannot be opened or read is logged and treated as
        holding nothing: the camera is left where its lens is.
        """
        hardware_id = body.hardware_id
        if hardware_id is None or body.identity_clash:
            return None
        try:
            registry = self._registry_factory()
            entry = registry.get_camera_by_id(hardware_id) if registry else None
        except Exception as exc:
            self.logger.warning(
                f"[chdk] {body.port}: could not read the stored zoom step "
                f"({exc!r}); the lens is left where it is"
            )
            return None
        return entry.get("zoom_step") if entry else None

    def _settle_zoom(self, body):
        """Read the zoom steps and step, and apply the stored step if any.

        Called by _ensure_record_mode once record mode is confirmed. The
        number of steps is read once per body. A camera that does not answer
        it with a positive integer gets no further zoom scripts. A stored
        step outside 0..steps-1 is ignored with a warning. A stored step
        that differs from the current one is sent and read back; a camera
        that reports another step is logged with a warning and the
        operation carries on. A timeout or a wire failure propagates.
        """
        if body.zoom_steps is None:
            answer = self._send_lua(body, _GET_ZOOM_STEPS_LUA)
            if type(answer) is not int or answer <= 0:
                self.logger.warning(
                    f"[chdk] {body.port}: get_zoom_steps() answered "
                    f"{answer!r}, not a positive integer; the zoom is not "
                    "read or set on this body"
                )
                return
            body.zoom_steps = answer
            self.logger.info(f"[chdk] {body.port}: {answer} zoom steps")
        current = self._read_zoom(body)
        stored = self._stored_zoom_step(body)
        if stored is None:
            return
        if type(stored) is not int or not 0 <= stored < body.zoom_steps:
            self.logger.warning(
                f"[chdk] {body.port}: the stored zoom step {stored!r} is not "
                f"between 0 and {body.zoom_steps - 1}; it is ignored and the "
                "lens is left where it is"
            )
            return
        if stored == current:
            return
        try:
            self._move_zoom(body, stored)
        except _ZoomNotConfirmedError:
            # The camera answered, so the body is in a known state; the
            # capture or preview goes on at the step it reports. A wire
            # failure or a timeout is not caught here.
            self.logger.warning(
                f"[chdk] {body.port}: the stored zoom step is {stored} and "
                f"the camera reports step {body.zoom_step} after set_zoom; "
                "carrying on at the reported step"
            )

    def set_zoom_step(self, camera_index: int, step: int) -> int:
        """Move the lens of the body at this index to a zoom step; see
        _set_zoom_step_once.

        A body that has left the bus is replaced and the request retried
        once, by _retry_once_if_gone, as for a capture and a preview: a
        camera that has just restarted is when an operator adjusts its zoom.
        A body that left the bus and was not retried, or failed again, is
        reported as a ZoomFailedError like any other failed conversation.
        """
        try:
            return self._retry_once_if_gone(
                camera_index, "zoom",
                lambda expect: self._set_zoom_step_once(camera_index, step, expect),
            )
        except _BodyGoneError as gone:
            raise ZoomFailedError(str(gone)) from gone

    def _set_zoom_step_once(self, camera_index, step, expect_identity=None):
        """Move the lens of the body at this index to a zoom step, and store it.

        The body is put in record mode first, which reads its zoom steps and
        applies any step already stored. The step is then checked against
        the range the camera reported, sent, and read back. The registry
        stores it under the body's hardware id only after the camera has
        confirmed it. A body with no identity, or an ambiguous one, is
        zoomed and nothing is stored.

        A failure on the wire - a timeout, a transport error, a script error
        - drops the body like a failed capture, and nothing more is sent to
        it. A camera that answers with another step is not dropped; the step
        it reports is recorded on the body.

        Args:
            camera_index: Which body, as the device list numbers them.
            step: The zoom step, 0 at the widest.
            expect_identity: Set only for a retry; see _in_use.

        Returns:
            The step the camera confirmed.

        Raises:
            ValueError: The step is not an int in 0..zoom_steps-1. The
                message names the valid range.
            ZoomFailedError: The body has no zoom steps, the conversation
                failed, or the camera did not confirm.
            _BodyGoneError: The body left the bus; set_zoom_step retries.
            RuntimeError: No body at the index.
        """
        with self._in_use(
            camera_index, "zoom", refuse_unusable=False,
            expect_identity=expect_identity,
        ) as body:
            try:
                self._ensure_record_mode(body)
            except Exception as exc:
                self._zoom_failed(body, camera_index, "switching to record mode", exc)
            steps = body.zoom_steps
            if steps is None:
                raise ZoomFailedError(
                    f"Camera {camera_index} ({body.port}) did not report how "
                    "many zoom steps it has, so no zoom step can be set on it"
                )
            if type(step) is not int or not 0 <= step < steps:
                raise ValueError(
                    f"zoom step must be an integer from 0 to {steps - 1} on "
                    f"camera {camera_index}; got {step!r}"
                )
            try:
                self._move_zoom(body, step)
            except _ZoomNotConfirmedError:
                raise
            except Exception as exc:
                self._zoom_failed(body, camera_index, "setting the zoom", exc)
            self._store_zoom_step(body, camera_index, step)
            return step

    def _zoom_failed(self, body, camera_index, stage, exc):
        """Log a failed zoom conversation, drop the body, and raise.

        The conversation broke and the camera's state is unknown, so the body
        is evicted and the next scan opens it again; nothing more is sent to
        it here.
        """
        named = _name_failure(exc)
        self.logger.error(
            f"[chdk] body {camera_index} ({body.port}): {stage} failed with "
            f"{named}: {exc}"
        )
        identity = body.hardware_id
        self._evict(body, f"{stage} failed with {named}")
        error = (
            _BodyGoneError if _is_gone_from_the_bus(exc) else ZoomFailedError
        )(
            f"CHDK zoom failed on {body.port} during {stage} with {named}: {exc}"
        )
        if isinstance(error, _BodyGoneError):
            error.identity = identity
        raise error from exc

    def _store_zoom_step(self, body, camera_index, step):
        """Write a confirmed step to the registry under the body's identity.

        A body that is not yet registered is registered first, as the
        orientation route does. A registry write that fails propagates.
        """
        hardware_id = body.hardware_id
        if hardware_id is None or body.identity_clash:
            self.logger.info(
                f"[chdk] {body.port}: zoom step {step} set and not stored; "
                "this body has no identity of its own to store it under"
            )
            return
        registry = self._registry_factory()
        if registry is None:
            self.logger.warning(
                f"[chdk] {body.port}: zoom step {step} set and not stored; "
                "the camera registry is not available"
            )
            return
        if registry.get_camera_by_id(hardware_id) is None:
            registry.register_resolved(
                hardware_id,
                {
                    "model": body.model,
                    "serial": body.serial,
                    "location": f"USB {body.port}",
                    "id": body.port,
                    "index": camera_index,
                },
                camera_index,
            )
        registry.update_zoom_step(hardware_id, step)
        self.logger.info(
            f"[chdk] {body.port}: zoom step {step} stored for {hardware_id}"
        )

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
                    prefix="captua_own_", suffix=".txt"
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
            "continuous_preview": False,
            "focus_control": False,
            "live_controls": False,
            "zoom": False,
            "zoom_steps": True,
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
