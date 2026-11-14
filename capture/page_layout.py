"""Where the page is in a test capture, and whether it is well framed (NEH-343).

The camera check screen asks for this after a test capture so that it can
draw the page's outline and the guide band over the picture. This module
returns measurements, a verdict and a list of conditions; the interface owns
the wording and the order of any advice. It does not measure camera
alignment and does not convert anything into zoom steps.

Framing rule. Each camera's picture is anchored at the gutter: the camera
is placed so that the picture's gutter-side edge sits as near the gutter as
the mount allows. Some of the facing page usually shows; the less, the
better. page_side says which page the camera photographs
("left": the gutter is on the picture's right; "right": on its left).
- Gutter side: the gutter must be located, between GUTTER_MIN and GUTTER_MAX
  (0-4%) of the picture's width inside the gutter-side edge. Further in,
  the facing page shows (gutter_inside). When the gutter cannot be located
  and the page runs to that edge, whether the page's inner margin is cut
  cannot be told (gutter_unknown), and the page is not called good.
- Head and foot: each margin between MARGIN_MIN and MARGIN_MAX (6-14%) of
  the picture's height. Below, too_close. This is the direction that
  judges the page's size, so fill and magnification_to_target come from
  the page's height, and too_small means the page's height is below
  1 - 2 x MARGIN_MAX (72%) of the picture's. A page tall enough but off
  centre along the gutter is reported by offset and by too_close or cut_off
  on its near side, not as too small: on the zoom sweep of 13 November
  2026, from zoom step 32 to 44, the left page filled 75-83% of the
  picture's height, its head ran off the top and 17-25% of the height lay
  clear below its foot.
- Fore edge: a margin of at least MARGIN_MIN of the width, with no upper
  limit. Below, too_close.
- A head, foot or fore edge touching the picture's edge is cut_off.

Method. The JPEG is decoded as luminance with Pillow's draft mode (a 4608 x
3456 picture is decoded at 1/4 scale, 1152 x 864), reduced to the analysis
size (long side ANALYSIS_LONG_SIDE, 576 px), and turned upright by the
camera's saved rotation; the EXIF orientation tag is ignored. The picture
can have any proportions. Then:

1. Paper mask: Otsu threshold on the blurred luminance, and a morphological
   opening that removes thin bright lines such as the cut edge of the glass.
2. Candidate: connected components, scored by area, distance from the
   centre, contact with the picture's border except the gutter-side edge,
   and how fully each fills its bounding box.
3. Gutter: candidate columns come from the cut edge of the glass at the
   apex of the platen (a thin vertical ridge measured outside the paper,
   where the line crosses the dark background), from the jump in the
   candidate's top and bottom extent where the facing page begins, and from
   the candidate's gutter-side end. A candidate is accepted only where the
   fold of the page shows: a dark trough along the gutter with brighter
   paper on both sides of it, inside the picture, on at least FOLD_MIN_ROWS
   of the page's rows. The gutter line is fitted through the bottom of the
   trough, and the candidate region is cut there.
4. Plausibility gates on area, shape, border contact, contrast and fill.
5. Robust straight-line fits for the head, foot and fore edges; the gutter
   line is the fourth side, or, when no gutter is located, the region's own
   boundary. The corners are the line intersections.
6. Margins, empty space, fill, offset along the gutter, the conditions and
   the verdict.

What it was tuned on: ten pictures of one book (five openings, both cameras)
and four pictures with no page in them, all from Canon PowerShot A2500
bodies on one V-shaped glass platen under one lighting set-up, on 12 November
2026, and crops of the ten made on the gutter side so that the gutter lies 2%
inside the edge, on it, or 1% beyond it. None of those pictures was taken
with the camera in its designed position: in all ten the gutter lay a
quarter of the width inside the frame, with the facing page showing. The
crops stand in for the designed framing; they do not show what a camera
placed at the gutter sees (the angle at which the glass and the fold are
seen, the binding's shadow, the glass apex at the frame's edge). On the ten
pictures it found all ten pages and none in the four empty pictures; on the
crops it located the gutter in all ten 2% crops and reported the gutter as
unknown in all twenty others.

The fold walls, the filling of holes, the check of the cut at the gutter and
the too_small rule were then set on a zoom sweep of 13 November 2026: 34
pictures from the same two A2500 bodies on the bench unit, an open bound
book under the lowered glass, zoom steps 0 to 63, the left page text and
the right page a plate (a photograph of a document with a dark band across
it). On all 34 it found the page. The gutter was reported inside up to step
24, located at step 28 and at or beyond the edge from step 32 on the left
page, as seen in the pictures. On the right page it was reported inside up
to step 24 and at or beyond the edge from step 28; at step 28 the dark band
at the gutter lies 1-2% of the width inside the edge, and its outer wall is
too faint for the fold to be confirmed, so the gutter is reported as
unknown rather than located.
Where the plate's dark band runs off the foot of the picture, the foot is
reported as cut off, but the foot margin is measured to the top of the
band (3-7% of the height at steps 52-60 on the right page).

It has not been shown to handle more than one book in the designed position,
dark, coloured, glossy or damaged pages, blank pages with show-through,
fold-outs, a page with text or illustration running into the gutter, a
tight binding with no visible fold, a different platen or backdrop, or other
lighting. Its thresholds were set on those fourteen pictures, their crops
and the 34 pictures of the zoom sweep.

A gutter can be located only when the fold's outer wall lies inside the
picture. On crops of the ten pictures the gutter was located from 0.5% of
the width inside the edge on three of them, from 1% on four more, from 1.5%
on two and from 2% on one; nearer the edge it is reported as unknown, not as
good. Near the 4% limit the measured margin differed from the crop's by up
to 0.3 points of the width (up to 0.7 points at 3%).
"""

from __future__ import annotations

import io
import math
import time
from typing import Optional

import numpy as np
from PIL import Image, ImageFilter

# The framing rule (see the module docstring). Head and foot margins lie
# between MARGIN_MIN and MARGIN_MAX of the picture's height; the fore-edge
# margin is at least MARGIN_MIN of its width; the gutter lies between
# GUTTER_MIN and GUTTER_MAX of the width inside the gutter-side edge.
MARGIN_MIN = 0.06
MARGIN_MAX = 0.14
MARGIN_TARGET = (MARGIN_MIN + MARGIN_MAX) / 2
GUTTER_MIN = 0.0
GUTTER_MAX = 0.04

ANALYSIS_LONG_SIDE = 576   # analysis picture's long side, px
DRAFT_FACTOR = 2           # draft decode at least this many times the analysis size
MIN_ANALYSIS_SIDE = 64     # a smaller picture is not analysed

OPEN_RADIUS = 2            # opening radius on the paper mask, analysis px
MIN_COMPONENT_AREA = 0.01  # a component smaller than this is never a candidate
MIN_CANDIDATE_AREA = 0.03  # the chosen component must cover this much
MIN_PAGE_AREA = 0.02       # the page, after the cut at the gutter, must cover this much
MIN_CONTRAST = 40.0        # grey levels between the region and a ring around it
PAGE_ASPECT = (1.0, 2.4)   # height / width of the page's bounding box
MIN_BOX_FILL = 0.75        # the page must fill this share of its bounding box

GUTTER_WINDOW = 6          # per-row fold search, +/- px around the gutter column
GUTTER_SMOOTH_ROWS = 9     # luminance is averaged over this many rows before the fold search
END_ZONE = 0.06            # share of the width, at the component's gutter-side end, searched second
# Each wall of the fold is looked for 2-14 px from its bottom. On the right
# page of the 13 November 2026 zoom sweep, from zoom step 12 to 24, the dark
# band between the facing page and the page was 12-15 px across with two
# minima, and the far wall lay 9-14 px from the deeper one; with walls
# looked for only up to 8 px away the gutter was not located on any of them.
FOLD_WALL = (2, 14)
FOLD_DEPTH = 30.0          # both walls this many grey levels above the bottom
FOLD_MIN_ROWS = 0.8        # share of the page's rows that must show the fold
BIMODAL_MIN_SEPARATION = 3.0  # px between the two groups of gutter positions
BIMODAL_MIN_SHARE = 0.2    # each group must hold this share of the rows

SIDES = ("left", "right", "top", "bottom")
_ROTATIONS = {0: None, 90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
              270: Image.Transpose.ROTATE_90}


class _NotFound(Exception):
    """Raised inside the analysis to return found: False with a reason code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ----------------------------------------------------------------------------- decoding

def analysis_size(width: int, height: int) -> tuple[int, int]:
    """The analysis size for a picture of width x height, before rotation."""
    scale = ANALYSIS_LONG_SIDE / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def decode_upright(jpeg_bytes: bytes, rotation_deg: int) -> Image.Image:
    """Decode a JPEG as an upright luminance picture at the analysis size.

    Draft mode lets libjpeg decode at 1/2, 1/4 or 1/8 scale and skip the
    colour channels, so a 16-megapixel picture is never decoded at full size
    and only one decoded copy exists at a time. Raises _NotFound when the
    bytes are not a usable picture.
    """
    try:
        img = Image.open(io.BytesIO(jpeg_bytes))
        aw, ah = analysis_size(*img.size)
        if min(aw, ah) < MIN_ANALYSIS_SIDE or min(img.size) < MIN_ANALYSIS_SIDE:
            raise _NotFound("image_too_small")
        img.draft("L", (aw * DRAFT_FACTOR, ah * DRAFT_FACTOR))
        img = img.convert("L")
    except _NotFound:
        raise
    except Exception:
        raise _NotFound("unreadable_image")
    if img.size != (aw, ah):
        img = img.resize((aw, ah), Image.Resampling.BILINEAR)
    turn = _ROTATIONS[rotation_deg]
    return img.transpose(turn) if turn is not None else img


# ----------------------------------------------------------------------------- image operations

def otsu_threshold(values: np.ndarray) -> float:
    """Otsu's threshold for 8-bit values: the level that best splits them in two."""
    hist, _ = np.histogram(values, bins=256, range=(0, 256))
    p = hist.astype(np.float64) / max(hist.sum(), 1)
    omega = np.cumsum(p)
    mu = np.cumsum(p * np.arange(256))
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (mu[-1] * omega - mu) ** 2 / (omega * (1 - omega))
    between[~np.isfinite(between)] = 0
    return float(np.argmax(between))


def _disc_offsets(radius: int) -> list[tuple[int, int]]:
    return [(dy, dx) for dy in range(-radius, radius + 1) for dx in range(-radius, radius + 1)
            if dx * dx + dy * dy <= radius * radius]


def _shift_reduce(mask: np.ndarray, radius: int, op) -> np.ndarray:
    """Combine the mask with its shifts over a disc of the given radius.

    The loop runs over the disc's offsets (13 for radius 2, 49 for radius 4),
    each step a whole-array operation.
    """
    h, w = mask.shape
    pad = np.pad(mask, radius, mode="edge")
    out = mask.copy()
    for dy, dx in _disc_offsets(radius):
        op(out, pad[radius + dy:radius + dy + h, radius + dx:radius + dx + w], out=out)
    return out


def erode(mask: np.ndarray, radius: int) -> np.ndarray:
    """Binary erosion by a disc."""
    return _shift_reduce(mask, radius, np.logical_and)


def dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """Binary dilation by a disc."""
    return _shift_reduce(mask, radius, np.logical_or)


def label_components(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """4-connected components of a binary mask, labelled 1..n in raster order.

    Works on horizontal runs: the runs of every row are found at once, the
    pairs of overlapping runs on neighbouring rows are found with
    searchsorted, and the runs are joined by repeated hooking of each pair to
    the smaller root with pointer jumping. Every step is a whole-array
    operation; the number of rounds grows with how winding the components
    are, typically a few dozen at most.
    """
    h, w = mask.shape
    labels = np.zeros((h, w), np.int32)
    padded = np.zeros((h, w + 2), np.int8)
    padded[:, 1:-1] = mask
    d = np.diff(padded, axis=1)
    run_y, run_s = np.nonzero(d == 1)
    _, run_e = np.nonzero(d == -1)
    n_runs = len(run_s)
    if n_runs == 0:
        return labels, 0

    stride = w + 2
    start_key = run_y * stride + run_s
    end_key = run_y * stride + run_e
    # For run i on row y, the runs j on row y + 1 with s_j < e_i and e_j > s_i.
    lo = np.searchsorted(end_key, (run_y + 1) * stride + run_s, side="right")
    hi = np.searchsorted(start_key, (run_y + 1) * stride + run_e, side="left")
    count = np.maximum(hi - lo, 0)
    a = np.repeat(np.arange(n_runs), count)
    first = np.repeat(lo - np.cumsum(count) + count, count)
    b = first + np.arange(len(a))

    parent = np.arange(n_runs)
    while len(a):
        pa, pb = parent[a], parent[b]
        if np.array_equal(pa, pb):
            break
        low = np.minimum(pa, pb)
        np.minimum.at(parent, pa, low)
        np.minimum.at(parent, pb, low)
        while True:
            jumped = parent[parent]
            if np.array_equal(jumped, parent):
                break
            parent = jumped

    roots, component = np.unique(parent, return_inverse=True)
    lengths = run_e - run_s
    run_of_pixel = np.repeat(np.arange(n_runs), lengths)
    offsets = np.arange(lengths.sum()) - np.repeat(np.cumsum(lengths) - lengths, lengths)
    flat = labels.reshape(-1)
    flat[(run_y * w + run_s)[run_of_pixel] + offsets] = component[run_of_pixel] + 1
    return labels, len(roots)


def robust_line(t: np.ndarray, v: np.ndarray, iters: int = 3) -> tuple[float, float, float, float]:
    """Fit v = a * t + b, trimming points more than 2.5 robust deviations away.

    Returns (a, b, rms of the kept points, share of points kept).
    """
    keep = np.ones(len(t), bool)
    a = b = 0.0
    for _ in range(iters):
        if keep.sum() < 5:
            break
        a, b = np.polyfit(t[keep], v[keep], 1)
        res = v - (a * t + b)
        mad = np.median(np.abs(res[keep])) + 0.5
        keep = np.abs(res) < 2.5 * 1.4826 * mad
    if not keep.any():
        return float(a), float(b), float("inf"), 0.0
    res = v[keep] - (a * t[keep] + b)
    return float(a), float(b), float(np.sqrt(np.mean(res ** 2))), float(keep.mean())


def bimodal_separation(values: np.ndarray) -> float:
    """Distance between the two groups of values when they form two groups, else 0.

    The values are split where the between-group variance is largest. They
    count as two groups when each holds at least BIMODAL_MIN_SHARE of them,
    the group means lie at least BIMODAL_MIN_SEPARATION apart, and that
    distance is at least twice the larger spread within a group.
    """
    v = np.sort(np.asarray(values, np.float64))
    n = len(v)
    if n < 10:
        return 0.0
    k = np.arange(1, n)
    csum = np.cumsum(v)
    m1 = csum[:-1] / k
    m2 = (csum[-1] - csum[:-1]) / (n - k)
    between = k * (n - k) * (m1 - m2) ** 2
    split = int(np.argmax(between)) + 1
    low, high = v[:split], v[split:]
    if min(len(low), len(high)) < BIMODAL_MIN_SHARE * n:
        return 0.0
    separation = float(high.mean() - low.mean())
    spread = max(float(low.std()), float(high.std()))
    if separation < BIMODAL_MIN_SEPARATION or separation < 2 * spread:
        return 0.0
    return separation


# ----------------------------------------------------------------------------- detection

def paper_mask(lum_blur: np.ndarray) -> np.ndarray:
    """Pixels brighter than Otsu's threshold, opened to cut thin bright links."""
    mask = lum_blur > otsu_threshold(lum_blur)
    return dilate(erode(mask, OPEN_RADIUS), OPEN_RADIUS)


def choose_component(labels: np.ndarray, n: int, gutter_side: str) -> Optional[int]:
    """The label most likely to hold the page, or None.

    Score: 4 x area - 2 x distance of the centroid from the centre - 1.5 x
    border contact + bounding-box fill, over components covering at least
    MIN_COMPONENT_AREA of the picture. Contact with the gutter-side edge
    does not count against a component: in the designed framing the page
    reaches that edge.
    """
    if n == 0:
        return None
    h, w = labels.shape
    flat = labels.reshape(-1)
    ys, xs = np.divmod(np.arange(h * w), w)
    area_px = np.bincount(flat, minlength=n + 1).astype(np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        cy = np.bincount(flat, weights=ys, minlength=n + 1) / area_px / h
        cx = np.bincount(flat, weights=xs, minlength=n + 1) / area_px / w
    side_cols = [labels[:, -1]] if gutter_side == "left" else [labels[:, 0]]
    border = ((np.bincount(labels[0], minlength=n + 1) + np.bincount(labels[-1], minlength=n + 1)) / w
              + sum(np.bincount(c, minlength=n + 1) for c in side_cols) / h)
    y0 = np.full(n + 1, h); y1 = np.full(n + 1, -1)
    x0 = np.full(n + 1, w); x1 = np.full(n + 1, -1)
    np.minimum.at(y0, flat, ys); np.maximum.at(y1, flat, ys)
    np.minimum.at(x0, flat, xs); np.maximum.at(x1, flat, xs)
    with np.errstate(divide="ignore", invalid="ignore"):
        box_fill = area_px / ((y1 - y0 + 1) * (x1 - x0 + 1))
    area = area_px / (h * w)
    score = area * 4 - np.hypot(cx - 0.5, cy - 0.5) * 2 - border * 1.5 + box_fill
    eligible = area >= MIN_COMPONENT_AREA
    eligible[0] = False
    if not eligible.any():
        return None
    return int(np.argmax(np.where(eligible, score, -np.inf)))


def fill_holes(region: np.ndarray) -> np.ndarray:
    """The region with every enclosed hole filled.

    A hole is a part of the picture outside the region that the region
    surrounds and that does not reach the picture's border: on a page, a
    dark illustration or photograph. It belongs to the page's outline, so
    it counts in the page's area and bounding-box fill.
    """
    labels, n = label_components(~region)
    if n == 0:
        return region
    outside = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    hole = np.ones(n + 1, bool)
    hole[outside] = False
    hole[0] = False
    return region | hole[labels]


def ring_contrast(lum: np.ndarray, region: np.ndarray, paper: Optional[np.ndarray] = None) -> float:
    """Mean luminance of the region minus that of a ring 2-4 px outside it.

    When paper is given, the region's mean is taken over its paper pixels
    only, so that a dark illustration inside the page does not lower it.
    """
    ring = dilate(region, 4) & ~dilate(region, 1)
    inner = region if paper is None else region & paper
    if not ring.any() or not inner.any():
        return 0.0
    return float(lum[inner].mean() - lum[ring].mean())


def ridge_response(lum_blur: np.ndarray) -> np.ndarray:
    """Response to a thin vertical line: |l(x) - (l(x - 2) + l(x + 2)) / 2|."""
    ridge = np.abs(lum_blur[:, 2:-2] - 0.5 * (lum_blur[:, :-4] + lum_blur[:, 4:]))
    return np.pad(ridge, ((0, 0), (2, 2)))


def _extent_jump(comp: np.ndarray, cx0: int, cx1: int) -> np.ndarray:
    """Per column, the change in the component's top and bottom across the column.

    Medians of 5 columns on each side, 2-6 columns away; 0 where the
    component is missing from any column within 6 of it.
    """
    h, w = comp.shape
    present = comp.any(0)
    top = np.where(present, comp.argmax(0), -1).astype(float)
    bot = np.where(present, h - 1 - comp[::-1].argmax(0), -1).astype(float)
    jump = np.zeros(w)
    xs = np.arange(cx0 + 6, cx1 - 6)
    if len(xs) == 0 or w < 13:
        return jump
    windows = np.lib.stride_tricks.sliding_window_view
    med_top = np.median(windows(top, 5), axis=1)
    med_bot = np.median(windows(bot, 5), axis=1)
    valid = (windows(top, 13)[xs - 6] >= 0).all(1)
    jump[xs] = np.where(valid, np.abs(med_top[xs - 6] - med_top[xs + 2])
                        + np.abs(med_bot[xs - 6] - med_bot[xs + 2]), 0.0)
    return jump


def _column_scores(comp: np.ndarray, ridge: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per column, the ridge strength outside the paper and the extent jump.

    The ridge is averaged over the rows outside the component (dilated by 3),
    where the cut edge of the glass crosses the dark background; when the
    book fills the frame vertically, a median over the whole column is used.
    """
    h, w = comp.shape
    cols = np.flatnonzero(comp.any(0))
    outside = ~dilate(comp, 3)
    n_out = outside.sum(0)
    if (n_out > 10).sum() < 0.5 * w:
        col_ridge = np.median(np.sort(ridge, axis=0)[h // 4:], axis=0)
    else:
        col_ridge = np.where(n_out > 10, (ridge * outside).sum(0) / np.maximum(n_out, 1), 0.0)
    return col_ridge, _extent_jump(comp, int(cols[0]), int(cols[-1]))


def _best_column(col_ridge: np.ndarray, jump: np.ndarray, lo: int, hi: int) -> Optional[int]:
    """The column in [lo, hi) with the highest score: ridge strength, normalised
    to the strongest in the range, plus 0.7 x the extent jump, clipped at 12 px."""
    if hi <= lo:
        return None
    score = (col_ridge[lo:hi] / (col_ridge[lo:hi].max() + 1e-6)
             + 0.7 * np.minimum(jump[lo:hi], 12) / 12)
    return lo + int(np.argmax(score))


def gutter_columns(comp: np.ndarray, ridge: np.ndarray, page_side: str) -> list[int]:
    """Candidate gutter columns, in the order they are to be tried.

    First, the prototype's search: inside the central 70% of the component,
    opened towards the facing page when that page runs off the frame, never
    in the frame's outer 5%, and kept only when the column shows an extent
    jump of at least 4 px or a ridge above 3 x the median plus 1. It finds a
    gutter that lies well inside the picture with the facing page joined to
    the page in the paper mask. Second, the component's gutter-side end:
    the best column from END_ZONE of the width inside it to 8 px beyond it,
    and the column 3 px beyond it. That is where the gutter lies in the
    designed framing, and where it lies when the fold's shadow has separated
    the page from the facing page in the mask. Each candidate is then
    confirmed or rejected by fit_fold.
    """
    h, w = comp.shape
    col_ridge, jump = _column_scores(comp, ridge)
    cols = np.flatnonzero(comp.any(0))
    cx0, cx1 = int(cols[0]), int(cols[-1])
    span = cx1 - cx0
    candidates = []

    lo, hi = cx0 + int(0.15 * span), cx1 - int(0.15 * span)
    if page_side == "left" and comp[:, -1].any():
        hi = cx1 - max(7, int(0.04 * span))
    if page_side == "right" and comp[:, 0].any():
        lo = cx0 + max(7, int(0.04 * span))
    lo, hi = max(lo, int(0.05 * w)), min(hi, int(0.95 * w))
    x = _best_column(col_ridge, jump, lo, hi)
    if x is not None and (jump[x] >= 4 or col_ridge[x] > 3 * np.median(col_ridge[lo:hi]) + 1):
        candidates.append(x)

    zone = int(round(END_ZONE * w))
    reach = FOLD_WALL[1]
    if page_side == "left":
        ends = [_best_column(col_ridge, jump, max(cx1 - zone, 0), min(cx1 + reach + 1, w)),
                min(cx1 + 3, w - 1)]
    else:
        ends = [_best_column(col_ridge, jump, max(cx0 - reach, 0), min(cx0 + zone + 1, w)),
                max(cx0 - 3, 0)]
    for x in ends:
        if x is not None and x not in candidates:
            candidates.append(x)
    return candidates


def fit_fold(lum_blur: np.ndarray, gutter_x: int, rows: np.ndarray) -> Optional[tuple[tuple, float]]:
    """Fit the gutter as a line x = a * y + b through the bottom of the fold.

    Where the two pages meet, the page's surface turns into the binding and
    shows as a dark trough along the gutter. On each row the luminance,
    averaged over GUTTER_SMOOTH_ROWS rows, is searched for its minimum within
    GUTTER_WINDOW px of gutter_x. The row shows the fold when, on both sides
    of that minimum and 2-8 px from it, the luminance rises at least
    FOLD_DEPTH above it inside the picture. A fold whose outer wall lies
    beyond the picture's edge cannot be told from a page cut at the gutter,
    so such a row does not count.

    Returns None unless at least FOLD_MIN_ROWS of the rows show the fold;
    otherwise the line fit (a, b, rms, share kept) through those rows and the
    distance in px between the two groups of positions when they form two
    groups, else 0.
    """
    h, w = lum_blur.shape
    k = GUTTER_SMOOTH_ROWS
    csum = np.cumsum(np.pad(lum_blur, ((k // 2 + 1, k // 2), (0, 0)), mode="edge"), axis=0)
    smooth = (csum[k:] - csum[:-k]) / k
    lo = max(gutter_x - GUTTER_WINDOW, 0)
    hi = min(gutter_x + GUTTER_WINDOW + 1, w)
    seg = smooth[rows, lo:hi]
    bottom = lo + seg.argmin(1)
    depth = seg.min(1)

    offsets = np.arange(FOLD_WALL[0], FOLD_WALL[1] + 1)

    def wall(sign):
        idx = bottom[:, None] + sign * offsets[None, :]
        inside = (idx >= 0) & (idx < w)
        vals = smooth[rows[:, None], np.clip(idx, 0, w - 1)]
        return np.where(inside, vals, -np.inf).max(1)

    shows = (wall(-1) - depth >= FOLD_DEPTH) & (wall(+1) - depth >= FOLD_DEPTH)
    if shows.mean() < FOLD_MIN_ROWS:
        return None
    t = rows[shows].astype(float)
    v = bottom[shows].astype(float)
    fit = robust_line(t, v)
    return fit, bimodal_separation(v - (fit[0] * t + fit[1]))


def check_page(page: np.ndarray, lum: np.ndarray, gutter_side: str,
               paper: Optional[np.ndarray] = None) -> None:
    """Raise _NotFound unless the region is plausibly a page.

    Contact with the gutter-side edge is not counted among the edges the
    region runs off. paper, when given, is the paper mask, over which the
    region's luminance is taken for the contrast test.
    """
    h, w = page.shape
    area = page.sum()
    if area < MIN_PAGE_AREA * h * w:
        raise _NotFound("no_page_sized_region")
    py = np.flatnonzero(page.any(1))
    px = np.flatnonzero(page.any(0))
    bh, bw = py[-1] - py[0] + 1, px[-1] - px[0] + 1
    if not PAGE_ASPECT[0] <= bh / bw <= PAGE_ASPECT[1]:
        raise _NotFound("not_page_shaped")
    touched = (int(page[0].any()) + int(page[-1].any())
               + int(page[:, 0].any() and gutter_side != "left")
               + int(page[:, -1].any() and gutter_side != "right"))
    if touched >= 3:
        raise _NotFound("runs_off_edges")
    if ring_contrast(lum, page, paper) < MIN_CONTRAST:
        raise _NotFound("low_contrast")
    if area / (bh * bw) < MIN_BOX_FILL:
        raise _NotFound("not_page_shaped")


def fit_edges(page: np.ndarray) -> dict:
    """Robust line fits to the page mask's boundary on the central 70% of each side.

    top and bottom are y = a * x + b; left and right are x = a * y + b.
    """
    h, w = page.shape
    py = np.flatnonzero(page.any(1))
    px = np.flatnonzero(page.any(0))

    def central(lo, hi):
        m = int(0.15 * (hi - lo))
        return np.arange(lo + m, hi - m + 1)

    cols = central(px[0], px[-1])
    cols = cols[page[:, cols].any(0)]
    rows = central(py[0], py[-1])
    rows = rows[page[rows].any(1)]
    return {
        "top": robust_line(cols.astype(float), page[:, cols].argmax(0).astype(float)),
        "bottom": robust_line(cols.astype(float), (h - 1 - page[::-1, cols].argmax(0)).astype(float)),
        "left": robust_line(rows.astype(float), page[rows].argmax(1).astype(float)),
        "right": robust_line(rows.astype(float), (w - 1 - page[rows, ::-1].argmax(1)).astype(float)),
    }


def corners_from_fits(fits: dict) -> np.ndarray:
    """The four corners, top-left, top-right, bottom-right, bottom-left, in px."""
    def meet(horizontal, vertical):
        a1, b1 = horizontal[:2]   # y = a1 x + b1
        a2, b2 = vertical[:2]     # x = a2 y + b2
        y = (a1 * b2 + b1) / (1 - a1 * a2)
        return a2 * y + b2, y

    return np.array([meet(fits["top"], fits["left"]), meet(fits["top"], fits["right"]),
                     meet(fits["bottom"], fits["right"]), meet(fits["bottom"], fits["left"])])


# ----------------------------------------------------------------------------- measures

def assess_framing(margins: dict, touching: dict, *, page_side: str, gutter_located: bool) -> dict:
    """The verdict, the conditions and the fill for given margins.

    margins are fractions of the upright picture; touching says which
    picture edges the page touches (on the gutter side: comes so near the
    edge that a fold there could not be confirmed); gutter_located says
    whether the gutter was found, in which case the gutter-side margin is
    the distance from that edge to the gutter's nearest point.

    Conditions, in this order:
    - cut_off: the head, foot or fore edge touches the picture's edge or has
      a negative margin;
    - too_close: one of those margins is below MARGIN_MIN;
    - too_small: the page's height leaves less than 1 - 2 x MARGIN_MAX of
      the picture's height to the page, so that centred along the gutter
      both head and foot margins would be above MARGIN_MAX. A page that is
      tall enough but off centre along the gutter is not too small: its near
      side is too_close or cut_off, and offset says by how much it is off;
    - gutter_inside: the gutter is located more than GUTTER_MAX inside the
      gutter-side edge, so the facing page shows;
    - gutter_unknown: the gutter is not located, so whether the page's inner
      margin is cut cannot be told.
    The verdict is the first condition, or good when there is none. sides
    maps each side that is cut off or too close to that state; the gutter
    side is never listed there.
    """
    gutter_side = "right" if page_side == "left" else "left"
    fore_side = "left" if page_side == "left" else "right"
    fill = 1 - margins["top"] - margins["bottom"]
    sides = {}
    for side in ("top", "bottom", fore_side):
        if touching.get(side) or margins[side] < 0:
            sides[side] = "cut_off"
    for side in ("top", "bottom", fore_side):
        if side not in sides and margins[side] < MARGIN_MIN:
            sides[side] = "too_close"

    if gutter_located:
        gutter_margin = margins[gutter_side]
        gutter_state = "located" if GUTTER_MIN <= gutter_margin <= GUTTER_MAX else "inside"
    else:
        gutter_margin = None
        gutter_state = "at_or_beyond_edge" if touching.get(gutter_side) else "not_located"

    conditions = []
    if "cut_off" in sides.values():
        conditions.append("cut_off")
    if "too_close" in sides.values():
        conditions.append("too_close")
    if fill < 1 - 2 * MARGIN_MAX:
        conditions.append("too_small")
    if gutter_state == "inside":
        conditions.append("gutter_inside")
    if not gutter_located:
        conditions.append("gutter_unknown")

    return {
        "gutter_margin": gutter_margin,
        "fore_edge_margin": margins[fore_side],
        "gutter_state": gutter_state,
        "verdict": conditions[0] if conditions else "good",
        "conditions": conditions,
        "sides": sides,
        "fill": fill,
        "magnification_to_target": (1 - 2 * MARGIN_TARGET) / fill if fill > 0 else None,
    }


def page_offset(margins: dict) -> dict:
    """The page centre's offset from the picture centre along the gutter.

    along_gutter is positive towards the top of the upright picture, as a
    fraction of the picture's height. On the scanner it is corrected by
    sliding the camera along its bar.
    """
    return {"along_gutter": (margins["bottom"] - margins["top"]) / 2}


def outline_confidence(fits: dict, corners: np.ndarray, page: np.ndarray,
                       gutter_found: bool, touching: bool) -> float:
    """A 0-1 figure for how well the outline fits the mask.

    Multiplies: edge straightness (rms against 0.8% of the mean side length),
    the share of edge points the fits kept, how fully the mask fills the
    quadrilateral, a book-like height/width (1.15-1.9), whether a gutter was
    found, and a penalty when the page touches the picture's head, foot or
    fore-edge side (not the gutter side). On the tuning pictures it mostly
    measures the gutter fit.
    """
    def length(p, q):
        return float(np.hypot(*(corners[p] - corners[q])))

    top, bottom = length(0, 1), length(3, 2)
    left, right = length(0, 3), length(1, 2)
    xs, ys = corners[:, 0], corners[:, 1]
    area = 0.5 * abs(np.dot(xs, np.roll(ys, 1)) - np.dot(ys, np.roll(xs, 1)))
    rms = max(f[2] for f in fits.values())
    kept = min(f[3] for f in fits.values())
    tol = max(1.0, 0.008 * (top + bottom + left + right) / 4)
    aspect = ((left + right) / 2) / max((top + bottom) / 2, 1e-6)
    conf = math.exp(-max(rms - tol, 0) / (2 * tol))
    conf *= min(1.0, kept / 0.8)
    conf *= math.exp(-abs(page.sum() / max(area, 1) - 1) / 0.08)
    conf *= 1.0 if 1.15 <= aspect <= 1.9 else 0.3
    conf *= 1.0 if gutter_found else 0.6
    conf *= 0.7 if touching else 1.0
    return float(min(max(conf, 0.0), 1.0))


# ----------------------------------------------------------------------------- entry point

def _locate(lum_img: Image.Image, page_side: str) -> dict:
    """Find the page in an upright luminance picture and measure it."""
    lum = np.asarray(lum_img, dtype=np.float32)
    lum_blur = np.asarray(lum_img.filter(ImageFilter.BoxBlur(1)), dtype=np.float32)
    h, w = lum.shape

    gutter_side = "right" if page_side == "left" else "left"
    paper = paper_mask(lum_blur)
    labels, n = label_components(paper)
    best = choose_component(labels, n, gutter_side)
    if best is None:
        raise _NotFound("no_page_sized_region")
    # A dark illustration leaves a hole in the paper mask. On the plate page
    # of the 13 November 2026 zoom sweep (a photograph of a document with a
    # dark band across its lower third) the hole took up to 11% of the
    # region's bounding box; a plate with a larger dark illustration leaves
    # less than MIN_BOX_FILL. The hole is filled, so that it counts as page in the
    # bounding-box fill and the outline, and the photograph's edges are not
    # treated as background beside the paper.
    comp = fill_holes(labels == best)
    del labels
    if comp.sum() < MIN_CANDIDATE_AREA * h * w:
        raise _NotFound("no_page_sized_region")
    if ring_contrast(lum, comp, paper) < MIN_CONTRAST:
        raise _NotFound("low_contrast")

    # The fold is looked for on the central 70% of the component's rows,
    # away from the head and foot, where the facing page can stand taller.
    # A fold is accepted only when cutting the component there leaves a
    # page-shaped region: the dark frame line of a photograph printed on
    # the page, with paper on both sides, has the profile of a fold. On the
    # plate page at zoom step 60 the frame line was taken for the gutter and
    # the cut left a strip five times as tall as it was wide.
    present = np.flatnonzero(comp.any(1))
    margin = int(0.15 * (present[-1] - present[0]))
    rows = np.arange(present[0] + margin, present[-1] - margin + 1)
    gutter_fit, separation, page = None, 0.0, comp
    for x in gutter_columns(comp, ridge_response(lum_blur), page_side):
        fold = fit_fold(lum_blur, x, rows)
        if fold is None:
            continue
        line_x = fold[0][0] * np.arange(h)[:, None] + fold[0][1]
        cols = np.arange(w)[None, :]
        cut = comp & ((cols < line_x - 1) if page_side == "left" else (cols > line_x + 1))
        try:
            check_page(cut, lum, gutter_side, paper)
        except _NotFound as nf:
            if nf.reason in ("not_page_shaped", "no_page_sized_region"):
                continue
            raise
        gutter_fit, separation = fold
        page = cut
        break
    gutter_located = gutter_fit is not None
    if not gutter_located:
        check_page(page, lum, gutter_side, paper)

    fits = fit_edges(page)
    if gutter_fit is not None:
        fits["right" if page_side == "left" else "left"] = gutter_fit
    corners = corners_from_fits(fits)
    if not np.all(np.isfinite(corners)):
        raise _NotFound("not_page_shaped")

    margins = {
        "left": float(corners[[0, 3], 0].min() / w),
        "right": float(1 - corners[[1, 2], 0].max() / w),
        "top": float(corners[[0, 1], 1].min() / h),
        "bottom": float(1 - corners[[2, 3], 1].max() / h),
    }
    touching = {"left": bool(page[:, 0].any()), "right": bool(page[:, -1].any()),
                "top": bool(page[0].any()), "bottom": bool(page[-1].any())}
    # On the gutter side the page counts as reaching the edge when it comes
    # nearer than the fold's outer wall can be looked for: a fold there
    # cannot be confirmed inside the picture.
    reach = FOLD_WALL[1] + 1
    touching[gutter_side] = bool(page[:, -reach:].any() if gutter_side == "right"
                                 else page[:, :reach].any())
    xs, ys = corners[:, 0], corners[:, 1]
    quad_area = 0.5 * abs(np.dot(xs, np.roll(ys, 1)) - np.dot(ys, np.roll(xs, 1)))

    framing = assess_framing(margins, touching, page_side=page_side,
                             gutter_located=gutter_located)
    return {
        "found": True,
        "corners": [[float(x / w), float(y / h)] for x, y in corners],
        "margins": margins,
        "gutter_margin": framing["gutter_margin"],
        "fore_edge_margin": framing["fore_edge_margin"],
        "gutter_state": framing["gutter_state"],
        "verdict": framing["verdict"],
        "conditions": framing["conditions"],
        "sides": framing["sides"],
        "offset": page_offset(margins),
        "fill": framing["fill"],
        "magnification_to_target": framing["magnification_to_target"],
        "empty_space": float(1 - quad_area / (w * h)),
        "gutter_uncertainty": float(separation / w),
        "confidence": outline_confidence(
            fits, corners, page, gutter_located,
            any(touched for side, touched in touching.items() if side != gutter_side)),
    }


def analyse_page(jpeg_bytes: bytes, *, rotation_deg: int, page_side: str) -> dict:
    """Find the page in a test capture and judge its framing.

    rotation_deg is the clockwise turn (0, 90, 180 or 270) that makes the
    picture upright, normally the camera's saved orientation. page_side is
    "left" or "right": which page of the open book this camera photographs
    (for "left" the gutter is on the picture's right, for "right" on its
    left). All positions are fractions 0-1 of the upright picture.

    Returns found: False with a reason code when no page is found or the
    image cannot be used; it never raises on the image. It raises ValueError
    for a rotation or page_side outside the values above.
    """
    if type(rotation_deg) is not int or rotation_deg not in _ROTATIONS:
        raise ValueError(f"rotation_deg must be one of 0, 90, 180, 270; got {rotation_deg!r}")
    if page_side not in ("left", "right"):
        raise ValueError(f"page_side must be 'left' or 'right'; got {page_side!r}")
    start = time.perf_counter()
    try:
        result = _locate(decode_upright(jpeg_bytes, rotation_deg), page_side)
    except _NotFound as nf:
        result = {"found": False, "reason": nf.reason}
    except Exception:
        result = {"found": False, "reason": "analysis_failed"}
    result["analysis_ms"] = round((time.perf_counter() - start) * 1000, 1)
    return result
