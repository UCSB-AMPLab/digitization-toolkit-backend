"""Synthetic test-capture scenes for capture/page_layout.py (NEH-343).

The tests do not use real photographs. This module draws, with Pillow, a
scene that has the parts the analyser was tuned to see in the A2500 test
captures: a dark, uneven background (the room seen through the glass), a
bright page with grey text lines, the facing page of the opening, the
shadow of the fold where the two pages meet at the gutter, a thin bright
line (the cut edge of the glass) running on from the gutter over the
background, and a bright tube at one border.

make_scene draws the scene in the upright picture, where the page is known
exactly, and then turns it counter-clockwise by rotation_deg, so that the
analyser has to turn it clockwise by rotation_deg to make it upright again,
as it does with a camera's saved orientation.
"""

import io

import numpy as np
from PIL import Image, ImageDraw

# The upright picture at a quarter of the A2500's 3456 x 4608 (portrait).
UPRIGHT_SIZE = (864, 1152)

PAPER = (222, 214, 196)
TEXT = (95, 92, 88)
GLASS_EDGE = (236, 246, 236)
TUBE = (255, 255, 252)
SHADOW_EDGE = (170, 164, 150)
SHADOW = (118, 113, 104)
PLATE = (64, 62, 60)
PLATE_FRAME = (30, 30, 30)


def _background(width, height, seed):
    """A dark, uneven background: two gradients, three soft patches and noise."""
    rng = np.random.default_rng(seed)
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
    lum = 22 + 28 * xs / width + 14 * ys / height
    for cx, cy, amp in ((0.2, 0.3, 18), (0.75, 0.15, 14), (0.6, 0.85, 20)):
        r2 = ((xs / width - cx) ** 2 + (ys / height - cy) ** 2) / 0.02
        lum += amp * np.exp(-r2)
    lum += rng.normal(0, 3, size=lum.shape)
    lum = np.clip(lum, 0, 255)
    rgb = np.stack([lum * 1.02, lum, lum * 0.95], axis=-1)
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB")


def page_corners(page, page_side, slant):
    """The page's four corners (top-left, top-right, bottom-right, bottom-left)
    as fractions of the upright picture.

    The gutter edge is vertical. The outer edge is slanted by `slant` (a
    fraction of the picture's width), so the page is a trapezoid.
    """
    x0, y0, x1, y1 = page
    if page_side == "left":
        return [(x0 + slant, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [(x0, y0), (x1 - slant, y0), (x1, y1), (x0, y1)]


def make_scene(
    page=(0.15, 0.10, 0.885, 0.90),
    *,
    page_side="left",
    rotation_deg=0,
    size=UPRIGHT_SIZE,
    slant=0.01,
    with_page=True,
    facing=True,
    tube=True,
    seed=0,
    quality=90,
    paper=PAPER,
    glass=True,
    illustration=None,
):
    """Draw a scene and return (jpeg_bytes, corners).

    page is (x0, y0, x1, y1) as fractions of the upright picture and may lie
    partly outside it. page_side says which page of the opening the camera
    photographs: for "left" the gutter is on the page's right, for "right"
    on its left. paper is the colour of both pages. glass=False leaves out
    the cut edge of the glass at the gutter. illustration, when given, is
    (x0, y0, x1, y1) as fractions of the page's width and height: a dark
    plate printed there, with a thin darker frame line round it, as on a
    page carrying a photograph. corners are the true
    corners in the upright picture, in the order top-left, top-right,
    bottom-right, bottom-left (None when with_page is False).
    """
    width, height = size
    scale = width / UPRIGHT_SIZE[0]
    img = _background(width, height, seed)
    draw = ImageDraw.Draw(img)

    def px(points):
        return [(x * width, y * height) for x, y in points]

    x0, y0, x1, y1 = page
    gutter_x = x1 if page_side == "left" else x0
    page_w = x1 - x0

    # The cut edge of the glass runs the full height of the picture; the
    # pages, drawn after it, hide it where they lie on the glass.
    if glass:
        draw.line(px([(gutter_x, 0.0), (gutter_x, 1.0)]), fill=GLASS_EDGE,
                  width=max(1, round(3 * scale)))

    if tube:
        tx0, tx1 = (0.0, 0.03) if page_side == "left" else (0.97, 1.0)
        draw.rectangle(px([(tx0, 0.08), (tx1, 0.92)]), fill=TUBE)

    if with_page and facing:
        # The facing page is seen obliquely: narrower, and taller towards the
        # far edge, so its outline differs from the page's at the gutter.
        reach = 0.55 * page_w
        far_x = gutter_x + reach if page_side == "left" else gutter_x - reach
        draw.polygon(
            px([(gutter_x, y0 - 0.02), (far_x, y0 - 0.05), (far_x, y1 + 0.05), (gutter_x, y1 + 0.02)]),
            fill=paper,
        )

    corners = None
    if with_page:
        corners = page_corners(page, page_side, slant)
        draw.polygon(px(corners), fill=paper)
        # Text lines inside the page.
        line_w = max(1, round(2 * scale))
        ty = y0 + 0.12 * (y1 - y0)
        while ty < y1 - 0.12 * (y1 - y0):
            draw.line(
                px([(x0 + 0.14 * page_w, ty), (x1 - 0.14 * page_w, ty)]),
                fill=TEXT,
                width=line_w,
            )
            ty += 0.035 * (y1 - y0)
        if illustration is not None:
            ix0, iy0, ix1, iy1 = illustration
            box = px([(x0 + ix0 * page_w, y0 + iy0 * (y1 - y0)),
                      (x0 + ix1 * page_w, y0 + iy1 * (y1 - y0))])
            draw.rectangle(box, fill=PLATE, outline=PLATE_FRAME, width=max(1, round(3 * scale)))
        # The fold at the gutter: a shadow 2% of the width across, darkest
        # in the middle, along the page's height.
        for half, colour in ((0.010, SHADOW_EDGE), (0.005, SHADOW)):
            draw.rectangle(px([(gutter_x - half, y0), (gutter_x + half, y1)]), fill=colour)

    raw = img.rotate(rotation_deg, expand=True) if rotation_deg % 360 else img
    buf = io.BytesIO()
    raw.save(buf, "JPEG", quality=quality)
    return buf.getvalue(), corners


def truth_margins(corners):
    """Margins of the page's bounding box, as fractions of the upright picture."""
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return {"left": min(xs), "right": 1 - max(xs), "top": min(ys), "bottom": 1 - max(ys)}


def tiny_jpeg():
    """An 8 x 8 JPEG."""
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 200, 200)).save(buf, "JPEG")
    return buf.getvalue()
