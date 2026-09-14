"""Decoding one CHDK live view frame into something a browser can show.

The wire format is CHDK's own (core/live_view.h): a 28-byte lv_data_header
followed, at the offset it names, by a 36-byte lv_framebuffer_desc and then
the viewport itself. For LV_FB_YUV8 the viewport is UYVYYY - six bytes carry
one U, one V and four Y, so four pixels - packed buffer_width pixels to a
row, of which only the first visible_width are image.

The colour conversion is chdkptp's (liveimg.c yuv_to_r/g/b): fixed point,
full-range BT.601, U and V read as signed bytes, arithmetic shift by 12. The
expected values below are worked by hand from those three expressions, so a
change to the coefficients fails here rather than at the bench.

The viewport's pixels are not square. chdkptp sizes its canvas as the
screen's width (margins included) over the LCD's aspect ratio and stretches
the frame into it (gui_live.lua update_canvas_size); a preview that skipped
that step would hand the operator a squashed page.
"""

import struct

import pytest

import capture.backends.chdk_backend as cb


HEADER_SIZE = 28
FB_DESC_SIZE = 36


def _frame(
    rows,
    buffer_width,
    visible_width,
    visible_height,
    margins=(0, 0, 0, 0),
    aspect=0,
    fb_type=0,
    version=(2, 1),
    vp_desc_start=HEADER_SIZE,
    data_start=HEADER_SIZE + FB_DESC_SIZE,
    truncate=0,
):
    """Build one live view frame. `rows` is the raw UYVYYY payload."""
    margin_left, margin_top, margin_right, margin_bot = margins
    header = struct.pack(
        "<7i", version[0], version[1], aspect, 0, 0, vp_desc_start, 0
    )
    desc = struct.pack(
        "<9i",
        fb_type, data_start, buffer_width, visible_width, visible_height,
        margin_left, margin_top, margin_right, margin_bot,
    )
    body = header + desc
    body += b"\x00" * max(0, data_start - len(body))
    body += rows
    if truncate:
        body = body[:-truncate]
    return body


def _uyvyyy(u, y1, v, y2, y3, y4):
    return bytes(
        [u & 0xFF, y1 & 0xFF, v & 0xFF, y2 & 0xFF, y3 & 0xFF, y4 & 0xFF]
    )


@pytest.mark.unit
def test_a_row_of_four_pixels_comes_back_as_four_rgb_triples():
    data = _frame(_uyvyyy(0, 255, 0, 0, 128, 255), 4, 4, 1)

    rgb, info = cb.decode_viewport_rgb(data)

    assert rgb.shape == (1, 4, 3)
    assert list(rgb[0][0]) == [255, 255, 255]
    assert list(rgb[0][1]) == [0, 0, 0]
    assert list(rgb[0][2]) == [128, 128, 128]
    assert list(rgb[0][3]) == [255, 255, 255]
    assert info["visible_width"] == 4
    assert info["fb_type"] == 0


@pytest.mark.unit
def test_chroma_is_read_as_signed_bytes():
    """u = -128 arrives as 0x80; read unsigned it would turn the pixel yellow."""
    data = _frame(
        _uyvyyy(0, 128, 127, 128, 128, 128)          # v = +127
        + _uyvyyy(127, 128, 0, 128, 128, 128)        # u = +127
        + _uyvyyy(0x80, 128, 0, 128, 128, 128),      # u = -128
        4, 4, 3,
    )

    rgb, _info = cb.decode_viewport_rgb(data)

    assert list(rgb[0][0]) == [255, 37, 128]
    assert list(rgb[1][0]) == [128, 84, 255]
    assert list(rgb[2][0]) == [128, 172, 0]


@pytest.mark.unit
def test_the_padding_past_the_visible_width_is_skipped():
    """buffer_width is the stride; only visible_width pixels are image."""
    white = _uyvyyy(0, 255, 0, 255, 255, 255)
    black = _uyvyyy(0, 0, 0, 0, 0, 0)
    data = _frame(white + black + black + white, buffer_width=8,
                  visible_width=4, visible_height=2)

    rgb, _info = cb.decode_viewport_rgb(data)

    assert rgb.shape == (2, 4, 3)
    assert list(rgb[0][0]) == [255, 255, 255]
    assert list(rgb[1][0]) == [0, 0, 0]


@pytest.mark.unit
def test_a_protocol_from_another_major_version_is_refused():
    data = _frame(_uyvyyy(0, 255, 0, 255, 255, 255), 4, 4, 1, version=(3, 0))

    with pytest.raises(ValueError) as exc:
        cb.decode_viewport_rgb(data)

    assert "version" in str(exc.value)
    assert "3" in str(exc.value)


@pytest.mark.unit
def test_a_framebuffer_type_this_protocol_does_not_define_is_refused():
    data = _frame(_uyvyyy(0, 255, 0, 255, 255, 255), 4, 4, 1, fb_type=1)

    with pytest.raises(ValueError) as exc:
        cb.decode_viewport_rgb(data)

    assert "fb_type" in str(exc.value)


@pytest.mark.unit
def test_a_frame_with_no_viewport_descriptor_is_refused():
    data = _frame(b"", 4, 4, 1, vp_desc_start=0)

    with pytest.raises(ValueError) as exc:
        cb.decode_viewport_rgb(data)

    assert "descriptor" in str(exc.value)


@pytest.mark.unit
def test_a_frame_with_no_viewport_data_is_refused():
    data = _frame(b"", 4, 4, 1, data_start=0)

    with pytest.raises(ValueError) as exc:
        cb.decode_viewport_rgb(data)

    assert "no viewport data" in str(exc.value)


@pytest.mark.unit
def test_a_short_frame_is_refused_rather_than_read_past_its_end():
    data = _frame(_uyvyyy(0, 255, 0, 255, 255, 255), 4, 4, 1, truncate=2)

    with pytest.raises(ValueError) as exc:
        cb.decode_viewport_rgb(data)

    assert "short" in str(exc.value)


@pytest.mark.unit
@pytest.mark.parametrize("field,margins", [
    ("margin_left", (-1, 0, 0, 0)),
    ("margin_top", (0, -1, 0, 0)),
    ("margin_right", (0, 0, -1, 0)),
    ("margin_bot", (0, 0, 0, -1)),
])
def test_a_negative_margin_is_refused_as_a_malformed_frame(field, margins):
    """All four margins are signed int32, and a negative one is nonsense."""
    data = _frame(_uyvyyy(0, 255, 0, 255, 255, 255), 4, 4, 1, margins=margins)

    with pytest.raises(ValueError) as exc:
        cb.parse_live_view(data)

    assert field in str(exc.value)
    assert "-1" in str(exc.value)


@pytest.mark.unit
@pytest.mark.parametrize("margins", [
    (4100, 0, 0, 0),
    (0, 4100, 0, 0),
    (0, 0, 4100, 0),
    (0, 0, 0, 4100),
    (2147483647, 0, 0, 0),
    (0, 0, 0, 2147483647),
])
def test_a_margin_past_the_sanity_bound_is_refused(margins):
    """The margins cost nothing on the wire and size the canvas.

    A four-pixel viewport with a margin of 30000 asks for a 30004x22503
    image, and one near INT_MAX raises MemoryError - which is not the
    ValueError the preview path reports a bad frame with, and on the
    appliance the backend is not in a container to absorb it.
    """
    data = _frame(_uyvyyy(0, 255, 0, 255, 255, 255), 4, 4, 1, margins=margins)

    with pytest.raises(ValueError) as exc:
        cb.encode_viewport_jpeg(data)

    assert "screen" in str(exc.value)
    assert str(cb._LV_MAX_SCREEN) in str(exc.value)


@pytest.mark.unit
def test_a_screen_inside_the_sanity_bound_still_encodes():
    """The bound must not refuse the frames the decoder is there to read."""
    rows = _uyvyyy(0, 200, 0, 200, 200, 200) * (704 // 4) * 232
    data = _frame(rows, 704, 704, 232, margins=(8, 4, 8, 4))

    info = cb.parse_live_view(data)

    assert info["visible_width"] == 704
    assert cb._LV_MAX_SCREEN > 720, "the bound is tighter than a frame we read"


@pytest.mark.unit
def test_a_margin_that_cancels_the_visible_height_does_not_divide_by_zero():
    """The screen height the frame is stretched by is the divisor.

    margin_top + visible_height + margin_bot, with nothing guarding it the
    way out_width is guarded. A frame that makes it zero has to arrive at the
    preview path as the malformed frame it is - the ValueError that path
    reports as a failed poll - rather than as a ZeroDivisionError going
    straight past the handler.
    """
    data = _frame(
        _uyvyyy(0, 255, 0, 255, 255, 255), 4, 4, 1, margins=(0, 0, 0, -1)
    )

    with pytest.raises(ValueError):
        cb.encode_viewport_jpeg(data)


@pytest.mark.unit
def test_a_visible_width_wider_than_the_buffer_is_refused():
    data = _frame(_uyvyyy(0, 255, 0, 255, 255, 255) * 2, buffer_width=4,
                  visible_width=8, visible_height=1)

    with pytest.raises(ValueError) as exc:
        cb.decode_viewport_rgb(data)

    assert "buffer_width" in str(exc.value)


@pytest.mark.unit
def test_the_jpeg_is_sized_to_the_screen_over_the_lcd_aspect():
    """720 wide over 4:3 is 540 high, whatever the 240 rows the sensor sent."""
    rows = _uyvyyy(0, 200, 0, 200, 200, 200) * (720 // 4) * 240
    data = _frame(rows, 720, 720, 240, aspect=0)

    jpeg, info = cb.encode_viewport_jpeg(data)

    from io import BytesIO

    from PIL import Image

    image = Image.open(BytesIO(jpeg))
    assert image.format == "JPEG"
    assert image.size == (720, 540)
    assert info["jpeg_bytes"] == len(jpeg)
    assert info["visible_height"] == 240


@pytest.mark.unit
def test_the_margins_are_part_of_the_screen_the_aspect_is_taken_from():
    rows = _uyvyyy(0, 200, 0, 200, 200, 200) * (704 // 4) * 232
    data = _frame(rows, 704, 704, 232, margins=(8, 4, 8, 4), aspect=0)

    jpeg, _info = cb.encode_viewport_jpeg(data)

    from io import BytesIO

    from PIL import Image

    assert Image.open(BytesIO(jpeg)).size == (720, 540)


@pytest.mark.unit
def test_a_sixteen_by_nine_screen_gets_a_sixteen_by_nine_frame():
    rows = _uyvyyy(0, 200, 0, 200, 200, 200) * (640 // 4) * 480
    data = _frame(rows, 640, 640, 480, aspect=1)

    jpeg, _info = cb.encode_viewport_jpeg(data)

    from io import BytesIO

    from PIL import Image

    assert Image.open(BytesIO(jpeg)).size == (640, 360)


@pytest.mark.unit
def test_an_aspect_ratio_nobody_has_defined_falls_back_to_four_by_three():
    rows = _uyvyyy(0, 200, 0, 200, 200, 200) * (640 // 4) * 100
    data = _frame(rows, 640, 640, 100, aspect=99)

    jpeg, info = cb.encode_viewport_jpeg(data)

    from io import BytesIO

    from PIL import Image

    assert Image.open(BytesIO(jpeg)).size == (640, 480)
    assert info["lcd_aspect_ratio"] == 99


@pytest.mark.unit
def test_a_viewport_whose_width_is_not_a_multiple_of_four_still_encodes():
    """Six bytes are four pixels, so an odd width leaves a part-used group.

    The decoded array is then a view into a wider one, and the encoder has to
    take it as it is rather than assume a contiguous buffer.
    """
    rows = _uyvyyy(0, 200, 0, 200, 200, 200) * (640 // 4) * 3
    data = _frame(rows, buffer_width=640, visible_width=602, visible_height=3,
                  aspect=1)

    rgb, _info = cb.decode_viewport_rgb(data)
    assert rgb.shape == (3, 602, 3)
    assert not rgb.flags["C_CONTIGUOUS"], "this test no longer covers the case"

    jpeg, info = cb.encode_viewport_jpeg(data)

    from io import BytesIO

    from PIL import Image

    assert Image.open(BytesIO(jpeg)).size == (602, info["jpeg_height"])
    assert info["jpeg_height"] > 0
