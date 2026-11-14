"""capture/page_layout.py: where the page is in a test capture (NEH-343).

The camera's picture is anchored at the gutter: its gutter-side edge sits on
the gutter and none of the facing page shows. The scenes are drawn by
tests/unit/page_scenes.py, so the page's corners are known exactly. Two
tests also run over the real A2500 samples the method was tuned on, and
over crops of them made on the gutter side, when the samples are present on
this machine; they are skipped otherwise. Four pictures from the zoom
sweep of 13 November 2026, reduced to the analysis size, are kept under
tests/unit/data/page_layout/ and always run.
"""

import io
import time
from pathlib import Path

import pytest
from PIL import Image

from capture.page_layout import (
    GUTTER_MAX,
    GUTTER_MIN,
    MARGIN_MAX,
    MARGIN_MIN,
    analyse_page,
    assess_framing,
)
from tests.unit.page_scenes import make_scene, tiny_jpeg, truth_margins

pytestmark = pytest.mark.unit

# One analysis pixel is 1/432 of the upright picture's width (0.23%) and
# 1/576 of its height (0.17%); the outline is held to about four of them.
OUTLINE_TOL = 0.01

NOT_FOUND_REASONS = {
    "unreadable_image",
    "image_too_small",
    "no_page_sized_region",
    "low_contrast",
    "not_page_shaped",
    "runs_off_edges",
    "analysis_failed",
}

FOUND_KEYS = {
    "found", "corners", "margins", "gutter_margin", "fore_edge_margin",
    "gutter_state", "verdict", "conditions", "sides", "offset", "fill",
    "magnification_to_target", "empty_space", "gutter_uncertainty",
    "confidence", "analysis_ms",
}

# The designed framing: the gutter 2% inside the picture's gutter-side edge,
# head and foot margins of 10%, a wide fore-edge margin.
LEFT_GOOD = (0.25, 0.10, 0.98, 0.90)
RIGHT_GOOD = (0.02, 0.10, 0.75, 0.90)


def _assert_corners_close(result, corners, tol=OUTLINE_TOL):
    assert result["found"], result
    for got, want in zip(result["corners"], corners):
        assert got[0] == pytest.approx(want[0], abs=tol), (result["corners"], corners)
        assert got[1] == pytest.approx(want[1], abs=tol), (result["corners"], corners)


def _assert_margins_close(result, corners, tol=OUTLINE_TOL):
    want = truth_margins(corners)
    for side in ("left", "right", "top", "bottom"):
        assert result["margins"][side] == pytest.approx(want[side], abs=tol), (side, result["margins"], want)


# ----------------------------------------------------------------------
# The rule, on numbers
# ----------------------------------------------------------------------

def _margins(left, right, top, bottom):
    return {"left": left, "right": right, "top": top, "bottom": bottom}


NO_TOUCH = {"left": False, "right": False, "top": False, "bottom": False}


def _judge(margins, touching=NO_TOUCH, page_side="left", gutter_located=True):
    return assess_framing(margins, touching, page_side=page_side, gutter_located=gutter_located)


def test_the_band_constants():
    assert (MARGIN_MIN, MARGIN_MAX) == (0.06, 0.14)
    assert (GUTTER_MIN, GUTTER_MAX) == (0.0, 0.04)


@pytest.mark.parametrize("page_side, gutter_side", [("left", "right"), ("right", "left")])
def test_a_page_anchored_at_the_gutter_is_good(page_side, gutter_side):
    margins = _margins(0.30, 0.30, 0.10, 0.10)
    margins[gutter_side] = 0.02
    out = _judge(margins, page_side=page_side)
    assert out["verdict"] == "good"
    assert out["conditions"] == []
    assert out["sides"] == {}
    assert out["gutter_state"] == "located"
    assert out["gutter_margin"] == pytest.approx(0.02)
    assert out["fore_edge_margin"] == pytest.approx(0.30)


def test_the_band_edges_are_inside_the_band():
    out = _judge(_margins(MARGIN_MIN, GUTTER_MAX, MARGIN_MIN, MARGIN_MAX))
    assert out["verdict"] == "good", out
    out = _judge(_margins(0.30, GUTTER_MIN, 0.10, 0.10))
    assert out["verdict"] == "good", out


def test_a_gutter_further_in_than_four_percent_means_the_facing_page_shows():
    out = _judge(_margins(0.30, 0.05, 0.10, 0.10))
    assert out["gutter_state"] == "inside"
    assert out["conditions"] == ["gutter_inside"]
    assert out["verdict"] == "gutter_inside"


def test_a_gutter_not_located_at_the_picture_edge_is_unknown():
    touching = dict(NO_TOUCH, right=True)
    out = _judge(_margins(0.30, 0.0, 0.10, 0.10), touching, gutter_located=False)
    assert out["gutter_state"] == "at_or_beyond_edge"
    assert out["gutter_margin"] is None
    assert out["conditions"] == ["gutter_unknown"]
    assert out["verdict"] == "gutter_unknown"
    assert out["sides"] == {}, "the gutter side is not reported as cut off"


def test_a_gutter_not_located_inside_the_picture_is_unknown_too():
    out = _judge(_margins(0.30, 0.10, 0.10, 0.10), gutter_located=False)
    assert out["gutter_state"] == "not_located"
    assert out["conditions"] == ["gutter_unknown"]


def test_a_wide_fore_edge_margin_is_accepted():
    out = _judge(_margins(0.45, 0.01, 0.10, 0.10))
    assert out["verdict"] == "good"


def test_a_page_shorter_than_the_band_allows_is_too_small():
    out = _judge(_margins(0.30, 0.01, 0.15, 0.15))
    assert out["conditions"] == ["too_small"]


def test_a_page_tall_enough_but_off_centre_along_the_gutter_is_not_too_small():
    # Head 3% from the top, foot 20% from the bottom: 77% of the height is
    # page, so the page is large enough, and the head is too close.
    out = _judge(_margins(0.30, 0.01, 0.03, 0.20))
    assert out["conditions"] == ["too_close"]
    assert out["sides"] == {"top": "too_close"}
    out = _judge(_margins(0.30, 0.01, 0.10, 0.16))
    assert out["conditions"] == []


def test_the_too_small_limit_is_the_page_height_the_band_leaves():
    limit = 1 - 2 * MARGIN_MAX
    assert "too_small" not in _judge(_margins(0.30, 0.01, 0.10, 0.90 - limit - 0.005))["conditions"]
    assert "too_small" in _judge(_margins(0.30, 0.01, 0.10, 0.90 - limit + 0.005))["conditions"]


@pytest.mark.parametrize(
    "margins, sides",
    [
        (_margins(0.05, 0.01, 0.10, 0.10), {"left": "too_close"}),   # fore edge
        (_margins(0.30, 0.01, 0.05, 0.10), {"top": "too_close"}),    # head
        (_margins(0.30, 0.01, 0.10, 0.04), {"bottom": "too_close"}), # foot
    ],
)
def test_a_margin_below_six_percent_off_the_gutter_side_is_too_close(margins, sides):
    out = _judge(margins)
    assert out["verdict"] == "too_close"
    assert out["sides"] == sides


def test_touching_the_edge_is_cut_off_and_every_condition_is_listed():
    touching = dict(NO_TOUCH, top=True)
    out = _judge(_margins(0.03, 0.08, 0.0, 0.30), touching)
    assert out["conditions"] == ["cut_off", "too_close", "too_small", "gutter_inside"]
    assert out["verdict"] == "cut_off"
    assert out["sides"] == {"top": "cut_off", "left": "too_close"}


def test_a_negative_margin_is_cut_off_even_without_contact():
    out = _judge(_margins(-0.01, 0.01, 0.10, 0.10))
    assert out["verdict"] == "cut_off"
    assert out["sides"] == {"left": "cut_off"}


def test_fill_and_magnification_are_taken_from_the_page_height():
    out = _judge(_margins(0.05, 0.01, 0.25, 0.25))
    assert out["fill"] == pytest.approx(0.5)
    # Head and foot margins of 10% leave 80% of the height to the page.
    assert out["magnification_to_target"] == pytest.approx(1.6)


def test_a_page_too_tall_has_a_magnification_below_one():
    out = _judge(_margins(0.30, 0.01, 0.04, 0.04))
    assert out["magnification_to_target"] < 1


# ----------------------------------------------------------------------
# Drawn scenes in the designed framing
# ----------------------------------------------------------------------

@pytest.mark.parametrize("page_side, page", [("left", LEFT_GOOD), ("right", RIGHT_GOOD)])
def test_a_page_anchored_at_the_gutter_is_found_and_good(page_side, page):
    jpeg, corners = make_scene(page, page_side=page_side)
    result = analyse_page(jpeg, rotation_deg=0, page_side=page_side)
    _assert_corners_close(result, corners)
    _assert_margins_close(result, corners)
    assert set(result) == FOUND_KEYS
    assert result["gutter_state"] == "located", result
    assert result["gutter_margin"] == pytest.approx(0.02, abs=0.01)
    gutter_side = "right" if page_side == "left" else "left"
    fore_side = "left" if page_side == "left" else "right"
    assert result["gutter_margin"] == result["margins"][gutter_side]
    assert result["fore_edge_margin"] == result["margins"][fore_side]
    assert result["verdict"] == "good", result
    assert result["conditions"] == []
    assert 0 <= result["confidence"] <= 1
    assert result["analysis_ms"] >= 0
    assert result["gutter_uncertainty"] == 0


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_the_saved_rotation_is_undone_before_analysis(rotation):
    jpeg, corners = make_scene(LEFT_GOOD, page_side="left", rotation_deg=rotation)
    result = analyse_page(jpeg, rotation_deg=rotation, page_side="left")
    _assert_corners_close(result, corners)
    assert result["gutter_state"] == "located", result


@pytest.mark.parametrize(
    "page_side, page",
    [("left", (0.15, 0.10, 0.885, 0.90)), ("right", (0.115, 0.10, 0.85, 0.90))],
)
def test_a_facing_page_showing_is_cut_at_the_gutter_and_reported(page_side, page):
    jpeg, corners = make_scene(page, page_side=page_side)
    result = analyse_page(jpeg, rotation_deg=0, page_side=page_side)
    _assert_corners_close(result, corners)
    assert result["gutter_state"] == "inside"
    assert result["gutter_margin"] == pytest.approx(0.115, abs=0.01)
    assert result["conditions"] == ["gutter_inside"]
    assert result["verdict"] == "gutter_inside"


@pytest.mark.parametrize(
    "page_side, page, glass",
    [
        pytest.param("left", (0.25, 0.10, 1.00, 0.90), True, id="left-on-edge"),
        pytest.param("left", (0.25, 0.10, 1.03, 0.90), True, id="left-beyond-edge"),
        pytest.param("right", (0.00, 0.10, 0.75, 0.90), True, id="right-on-edge"),
        pytest.param("right", (-0.03, 0.10, 0.75, 0.90), True, id="right-beyond-edge"),
        pytest.param("left", (0.25, 0.10, 1.00, 0.90), False, id="left-no-glass-line"),
    ],
)
def test_a_page_running_to_the_gutter_side_edge_is_found_and_reported_unknown(page_side, page, glass):
    jpeg, _ = make_scene(page, page_side=page_side, glass=glass)
    result = analyse_page(jpeg, rotation_deg=0, page_side=page_side)
    assert result["found"], result
    assert result["gutter_state"] == "at_or_beyond_edge", result
    assert result["gutter_margin"] is None
    assert "gutter_unknown" in result["conditions"]
    assert result["verdict"] != "good"
    # The rest of the outline is still measured.
    assert result["margins"]["top"] == pytest.approx(0.10, abs=OUTLINE_TOL)
    assert result["margins"]["bottom"] == pytest.approx(0.10, abs=OUTLINE_TOL)


# ----------------------------------------------------------------------
# Verdicts and conditions
# ----------------------------------------------------------------------

def test_a_wide_fore_edge_margin_is_good():
    jpeg, _ = make_scene((0.40, 0.10, 0.98, 0.90), page_side="left")
    result = analyse_page(jpeg, rotation_deg=0, page_side="left")
    assert result["fore_edge_margin"] > MARGIN_MAX
    assert result["verdict"] == "good", result


def test_a_small_page_is_too_small():
    jpeg, _ = make_scene((0.60, 0.25, 0.98, 0.75), page_side="left")
    result = analyse_page(jpeg, rotation_deg=0, page_side="left")
    assert result["conditions"] == ["too_small"], result
    assert result["sides"] == {}


def test_a_small_page_with_the_facing_page_showing_lists_both():
    jpeg, _ = make_scene((0.30, 0.25, 0.65, 0.75), page_side="left")
    result = analyse_page(jpeg, rotation_deg=0, page_side="left")
    assert result["conditions"] == ["too_small", "gutter_inside"], result
    assert result["verdict"] == "too_small"


def test_a_page_near_the_top_is_too_close_at_the_top():
    jpeg, _ = make_scene((0.20, 0.04, 0.98, 0.87), page_side="left")
    result = analyse_page(jpeg, rotation_deg=0, page_side="left")
    assert result["conditions"] == ["too_close"], result
    assert result["sides"] == {"top": "too_close"}


def test_a_page_near_its_fore_edge_is_too_close_there():
    jpeg, _ = make_scene((0.035, 0.10, 0.78, 0.90), page_side="left", tube=False)
    result = analyse_page(jpeg, rotation_deg=0, page_side="left")
    assert result["verdict"] == "too_close", result
    assert result["sides"] == {"left": "too_close"}


def test_a_page_running_off_the_top_is_cut_off_at_the_top():
    jpeg, _ = make_scene((0.20, -0.05, 0.98, 0.80), page_side="left")
    result = analyse_page(jpeg, rotation_deg=0, page_side="left")
    assert result["found"], result
    assert result["verdict"] == "cut_off", result
    assert result["sides"] == {"top": "cut_off"}


def test_a_right_page_running_off_its_fore_edge_is_cut_off_there():
    jpeg, _ = make_scene((0.30, 0.10, 1.05, 0.90), page_side="right", tube=False)
    result = analyse_page(jpeg, rotation_deg=0, page_side="right")
    assert result["found"], result
    assert result["verdict"] == "cut_off", result
    assert result["sides"] == {"right": "cut_off"}
    assert "gutter_inside" in result["conditions"]


# ----------------------------------------------------------------------
# Pages carrying dark illustrations
# ----------------------------------------------------------------------

@pytest.mark.parametrize("page_side, page", [("left", LEFT_GOOD), ("right", RIGHT_GOOD)])
@pytest.mark.parametrize(
    "illustration",
    [
        pytest.param((0.12, 0.15, 0.88, 0.55), id="plate-30pc"),
        pytest.param((0.10, 0.10, 0.90, 0.75), id="plate-52pc"),
    ],
)
def test_a_page_with_a_large_dark_plate_is_found(page_side, page, illustration):
    # The plate leaves a hole in the paper mask; with it unfilled the page
    # filled less than MIN_BOX_FILL of its bounding box and was refused as
    # not page-shaped.
    jpeg, corners = make_scene(page, page_side=page_side, illustration=illustration)
    result = analyse_page(jpeg, rotation_deg=0, page_side=page_side)
    _assert_corners_close(result, corners)
    assert result["gutter_state"] == "located", result
    assert result["verdict"] == "good", result
    assert result["confidence"] > 0.5, result


# ----------------------------------------------------------------------
# Downscaled pictures from the zoom sweep of 13 November 2026
# ----------------------------------------------------------------------

# Each file is a picture from a Canon PowerShot A2500 on the bench unit,
# an open bound book under the lowered glass, reduced to the analysis size
# (576 x 432, as the sensor delivered it, not turned upright) the way
# decode_upright reduces it, and saved as a greyscale JPEG.
# - sweep_c0_s28: camera 0, the left page (text), zoom step 28: the whole
#   page in the picture, the gutter just inside the right edge, the head
#   near the top because the camera is aimed lower along the gutter than
#   the page's centre.
# - sweep_c0_s36: camera 0, step 36: the gutter beyond the right edge and
#   the head running off the top.
# - sweep_c1_s20: camera 1, the right page (a plate: a photograph of a
#   document with a dark band and a seal), step 20: the facing page shows
#   at the left, and the dark band between it and the page is 12-15 px
#   across with two minima.
# - sweep_c1_s60: camera 1, step 60: the gutter beyond the left edge, the
#   photograph's dark frame line running down the page.
SWEEP = Path(__file__).parent / "data" / "page_layout"
SWEEP_CAMERA = {"c0": (270, "left"), "c1": (90, "right")}


def _analyse_sweep(name, rotation=None):
    saved, side = SWEEP_CAMERA[name.split("_")[1]]
    jpeg = (SWEEP / f"{name}.jpg").read_bytes()
    return analyse_page(jpeg, rotation_deg=saved if rotation is None else rotation, page_side=side)


@pytest.mark.parametrize("name", ["sweep_c1_s20", "sweep_c1_s60"])
def test_a_plate_page_is_found_only_when_turned_upright(name):
    # Analysed as the sensor delivered it, the page lies on its side and is
    # wider than it is tall.
    sideways = _analyse_sweep(name, rotation=0)
    assert sideways["found"] is False
    assert sideways["reason"] == "not_page_shaped"
    assert _analyse_sweep(name)["found"], _analyse_sweep(name)


def test_a_wide_dark_band_at_the_gutter_is_located_as_the_gutter():
    result = _analyse_sweep("sweep_c1_s20")
    assert result["found"], result
    assert result["gutter_state"] == "inside", result
    assert result["gutter_margin"] == pytest.approx(0.09, abs=0.015), result
    assert "gutter_unknown" not in result["conditions"]
    assert "gutter_inside" in result["conditions"]


def test_the_frame_line_of_a_photograph_is_not_taken_for_the_gutter():
    result = _analyse_sweep("sweep_c1_s60")
    assert result["found"], result
    assert result["gutter_state"] == "at_or_beyond_edge", result
    # The page runs from the left edge to its fore edge, 27% from the right.
    assert result["margins"]["left"] == pytest.approx(0.0, abs=0.01), result
    assert result["fore_edge_margin"] == pytest.approx(0.27, abs=0.02), result
    assert result["verdict"] == "cut_off"
    assert "gutter_unknown" in result["conditions"]


def test_the_best_framing_of_the_left_page_reports_the_offset_along_the_gutter():
    result = _analyse_sweep("sweep_c0_s28")
    assert result["found"], result
    assert result["gutter_state"] == "located", result
    assert result["gutter_margin"] == pytest.approx(0.039, abs=0.008), result
    # The head is 2% from the top and the foot 28% from the bottom: the
    # camera is aimed 13% of the height below the page's centre.
    assert result["sides"] == {"top": "too_close"}, result
    assert result["offset"]["along_gutter"] == pytest.approx(0.13, abs=0.01), result
    assert result["fill"] == pytest.approx(0.70, abs=0.01), result


def test_a_page_cut_off_at_the_head_with_the_gutter_beyond_the_edge_is_not_too_small():
    result = _analyse_sweep("sweep_c0_s36")
    assert result["found"], result
    assert result["fill"] > 1 - 2 * MARGIN_MAX, result
    assert result["conditions"] == ["cut_off", "gutter_unknown"], result
    assert result["sides"] == {"top": "cut_off"}


# ----------------------------------------------------------------------
# Offset, fill and magnification
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "page_side, page, along",
    [
        ("left", (0.25, 0.05, 0.98, 0.75), +0.10),
        ("left", (0.25, 0.25, 0.98, 0.95), -0.10),
        ("right", (0.02, 0.05, 0.75, 0.75), +0.10),
        ("right", (0.02, 0.25, 0.75, 0.95), -0.10),
    ],
)
def test_the_offset_is_reported_along_the_gutter(page_side, page, along):
    jpeg, _ = make_scene(page, page_side=page_side, slant=0.0)
    result = analyse_page(jpeg, rotation_deg=0, page_side=page_side)
    assert result["found"], result
    assert set(result["offset"]) == {"along_gutter"}
    assert result["offset"]["along_gutter"] == pytest.approx(along, abs=0.01), result["offset"]


def test_fill_and_magnification_on_a_small_page():
    jpeg, _ = make_scene((0.60, 0.25, 0.98, 0.75), page_side="left", slant=0.0)
    result = analyse_page(jpeg, rotation_deg=0, page_side="left")
    assert result["fill"] == pytest.approx(0.5, abs=0.01)
    assert result["magnification_to_target"] == pytest.approx(1.6, abs=0.04)


# ----------------------------------------------------------------------
# Nothing to find, and images that are not usable
# ----------------------------------------------------------------------

def _assert_not_found(result):
    assert result["found"] is False, result
    assert result["reason"] in NOT_FOUND_REASONS, result
    assert "corners" not in result
    assert result["analysis_ms"] >= 0


def test_an_empty_scene_is_not_found():
    jpeg, _ = make_scene(with_page=False)
    _assert_not_found(analyse_page(jpeg, rotation_deg=0, page_side="left"))


def test_a_bright_strip_is_not_page_shaped():
    jpeg, _ = make_scene((0.10, 0.45, 0.90, 0.60), page_side="left", facing=False)
    result = analyse_page(jpeg, rotation_deg=0, page_side="left")
    _assert_not_found(result)
    assert result["reason"] == "not_page_shaped"


def test_a_page_as_dark_as_the_room_is_low_contrast():
    jpeg, _ = make_scene((0.15, 0.10, 0.885, 0.90), page_side="left", paper=(78, 76, 72), tube=False)
    result = analyse_page(jpeg, rotation_deg=0, page_side="left")
    _assert_not_found(result)
    assert result["reason"] == "low_contrast"


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"\xff\xd8fake", id="not-a-jpeg"),
        pytest.param(b"\xff\xd8\xff\xe0" + bytes(range(256)) * 8, id="garbage-after-marker"),
        pytest.param(tiny_jpeg(), id="tiny"),
    ],
)
def test_a_bad_image_is_not_found_and_does_not_raise(data):
    _assert_not_found(analyse_page(data, rotation_deg=0, page_side="left"))


def test_a_truncated_jpeg_does_not_raise():
    jpeg, _ = make_scene()
    result = analyse_page(jpeg[: len(jpeg) // 3], rotation_deg=0, page_side="left")
    assert result["found"] in (True, False)
    assert result["analysis_ms"] >= 0


@pytest.mark.parametrize("kwargs", [{"rotation_deg": 45, "page_side": "left"}, {"rotation_deg": 0, "page_side": "middle"}])
def test_bad_arguments_are_refused(kwargs):
    jpeg, _ = make_scene()
    with pytest.raises(ValueError):
        analyse_page(jpeg, **kwargs)


# ----------------------------------------------------------------------
# Cost
# ----------------------------------------------------------------------

def test_a_sixteen_megapixel_picture_is_decoded_in_draft_mode(monkeypatch):
    from PIL import JpegImagePlugin

    calls = []
    real_draft = JpegImagePlugin.JpegImageFile.draft

    def spy(self, mode, size, *args, **kwargs):
        out = real_draft(self, mode, size, *args, **kwargs)
        calls.append((mode, self.size))
        return out

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", spy)
    jpeg, corners = make_scene(LEFT_GOOD, size=(3456, 4608), rotation_deg=270)

    start = time.perf_counter()
    result = analyse_page(jpeg, rotation_deg=270, page_side="left")
    elapsed = time.perf_counter() - start

    assert calls and calls[0][1] == (1152, 864), calls
    _assert_corners_close(result, corners)
    assert elapsed < 3.0


# ----------------------------------------------------------------------
# The real A2500 samples (present on the development machine only)
# ----------------------------------------------------------------------

SAMPLES = Path("/Users/iris/code/toolkit/_scratch/layout-samples")
needs_samples = pytest.mark.skipif(not SAMPLES.is_dir(), reason="real A2500 samples are not on this machine")

# Margins (left, right, top, bottom, in percent) from the spike's table.md,
# measured by the prototype on 12 November 2026.
SPIKE_MARGINS = {
    "bartlett-img_0001_c0.jpg": (42.5, 26.9, 26.0, 38.9),
    "bartlett-img_0001_c1.jpg": (25.9, 45.2, 42.5, 22.7),
    "bartlett-img_0011_c0.jpg": (41.9, 27.4, 25.7, 39.5),
    "bartlett-img_0011_c1.jpg": (25.4, 46.8, 42.3, 23.0),
    "bartlett-img_0031_c0.jpg": (41.7, 27.9, 25.1, 39.9),
    "bartlett-img_0031_c1.jpg": (25.2, 47.2, 41.9, 23.1),
    "bartlett-img_0051_c0.jpg": (41.3, 27.8, 24.0, 40.9),
    "bartlett-img_0051_c1.jpg": (25.4, 46.7, 41.0, 24.5),
    "bartlett-img_0070_c0.jpg": (43.1, 27.0, 23.8, 41.2),
    "bartlett-img_0070_c1.jpg": (25.5, 45.4, 41.1, 24.2),
}
CRASH = [
    "xx-neh333-crash-img_3440_c0.jpg",
    "xx-neh333-crash-img_3440_c1.jpg",
    "xx-neh333-crash-img_3460_c0.jpg",
    "xx-neh333-crash-img_3460_c1.jpg",
]
# c0 photographs the left page and is made upright by 270 degrees clockwise;
# c1 the right page, by 90 degrees clockwise.
CAMERA = {"c0": (270, "left"), "c1": (90, "right")}
_TURN = {90: Image.Transpose.ROTATE_270, 270: Image.Transpose.ROTATE_90}

# Where each crop puts the gutter, as a fraction of the cropped picture's
# width measured inwards from its gutter-side edge.
CROP_CASES = {"inside_2pc": 0.02, "on_edge": 0.0, "beyond_1pc": -0.01}


def _analyse_sample(name):
    rotation, side = CAMERA[name[-6:-4]]
    return analyse_page((SAMPLES / name).read_bytes(), rotation_deg=rotation, page_side=side)


def crop_at_gutter(name, corners, gutter_inset):
    """An upright JPEG of a real sample cut on the gutter side.

    The cut is placed so that the gutter, taken from the given corners of the
    uncropped picture, lies gutter_inset of the new width inside the new
    edge; a negative inset puts it beyond the edge. Of the two gutter-side
    corners, the one nearer the gutter-side edge is used, because
    gutter_margin is measured at the gutter's nearest point to that edge. The other three edges are kept. The result is upright,
    so it is analysed with rotation 0.
    """
    rotation, side = CAMERA[name[-6:-4]]
    img = Image.open(SAMPLES / name)
    img = img.transpose(_TURN[rotation])
    width, height = img.size
    if side == "left":
        gutter = max(corners[1][0], corners[2][0]) * width
        box = (0, 0, round(gutter / (1 - gutter_inset)), height)
    else:
        gutter = min(corners[0][0], corners[3][0]) * width
        box = (round((gutter - gutter_inset * width) / (1 - gutter_inset)), 0, width, height)
    buf = io.BytesIO()
    img.crop(box).save(buf, "JPEG", quality=92)
    return buf.getvalue()


@needs_samples
def test_the_real_samples_match_the_spike_and_show_the_facing_page():
    for name, spike in SPIKE_MARGINS.items():
        result = _analyse_sample(name)
        assert result["found"], (name, result)
        got = result["margins"]
        for key, want in zip(("left", "right", "top", "bottom"), spike):
            assert abs(got[key] * 100 - want) <= 2.0, (name, key, got, spike)
        # The page is small and the gutter well inside the frame on every sample.
        assert result["gutter_state"] == "inside", (name, result)
        assert "too_small" in result["conditions"], (name, result)
        assert "gutter_inside" in result["conditions"], (name, result)
    for name in CRASH:
        result = _analyse_sample(name)
        assert result["found"] is False, (name, result)


@pytest.fixture(scope="module")
def uncropped():
    return {name: _analyse_sample(name) for name in SPIKE_MARGINS}


@needs_samples
@pytest.mark.parametrize("case", list(CROP_CASES))
@pytest.mark.parametrize("name", list(SPIKE_MARGINS))
def test_real_samples_cropped_at_the_gutter(uncropped, name, case):
    base = uncropped[name]
    side = CAMERA[name[-6:-4]][1]
    jpeg = crop_at_gutter(name, base["corners"], CROP_CASES[case])
    result = analyse_page(jpeg, rotation_deg=0, page_side=side)
    assert result["found"], result
    if case == "inside_2pc":
        assert result["gutter_state"] == "located", result
        assert result["gutter_margin"] == pytest.approx(0.02, abs=0.01), result
        assert result["margins"]["top"] == pytest.approx(base["margins"]["top"], abs=0.01)
        assert result["margins"]["bottom"] == pytest.approx(base["margins"]["bottom"], abs=0.01)
    else:
        assert result["gutter_state"] == "at_or_beyond_edge", result
        assert result["verdict"] != "good", result
