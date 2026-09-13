"""
CHDK backend for Canon compacts running the CHDK firmware add-on.

Talks to the bodies over PTP/USB through pychdk. This module holds the live
view protocol decode; the backend that uses it follows.

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

import struct

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:  # pragma: no cover - numpy is a hard requirement on the Pi
    np = None  # type: ignore[assignment]
    _NUMPY_AVAILABLE = False


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
