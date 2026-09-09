"""NEH-222: the live preview is a second stream of the still's own configuration.

A preview built from its own small CameraConfig maps to a different (cropped)
sensor mode than the still, so the operator frames the page against a narrower
field of view than the capture records, and every poll/capture pair
reconfigures the body.

These tests pin the contract: one still configuration carrying a "lores"
stream, a capture_preview() that saves that stream, and - R39-1/R40-1 - a
still that skips the frames libcamera has already queued under the preview's
zoom before accepting one taken at the configuration's own unzoomed crop.

The unzoomed reference is the configuration's default ScalerCrop, not the
pixel array: on a 4:3 readout a 16:9 output configuration starts on a centred
16:9 band, so preview, zoom and still must all reference that band or the
preview shows a narrower field than the capture.

picamera2 is not importable here (no libcamera, no hardware), so the module
global Picamera2 is monkeypatched with a fake whose surface matches the
documented API: global_camera_info(), create_still_configuration(**kw),
configure(), start()/stop()/started, options, set_controls(), camera_controls,
camera_properties and capture_request() -> CompletedRequest with
get_metadata()/save()/release().
"""

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import capture.backends.picamera2_backend as pb
import capture.service as capture_service
from capture.camera import CameraConfig, IMG_SIZES

pytestmark = pytest.mark.unit


PIXEL_ARRAY = (4656, 3496)
FULL_RECT = (0, 0, PIXEL_ARRAY[0], PIXEL_ARRAY[1])
MIN_RECT = (0, 0, 64, 64)

# The default ScalerCrop libcamera hands a 16:9 output configuration taken off
# this 4:3 readout: the full width, a centred band of the height.
BAND_RECT = (0, 438, 4656, 2620)

GLOBAL_INFO = [
    {"Model": "imx519", "Id": "/base/soc/i2c0mux/i2c@80000/imx519@1a", "Location": 2},
    {"Model": "imx519", "Id": "/base/soc/i2c0mux/i2c@88000/imx519@1a", "Location": 2},
]


class FakeRequest:
    """Stand-in for picamera2's CompletedRequest."""

    def __init__(self, owner, metadata):
        self._owner = owner
        self._metadata = dict(metadata)
        self.saved = []
        self.released = False

    def get_metadata(self):
        return dict(self._metadata)

    def save(self, name, path):
        self.saved.append(name)
        self._owner.saved.append(name)
        Path(path).write_bytes(b"\xff\xd8" + name.encode())

    def make_buffer(self, name):
        return b"buffer:" + name.encode()

    def release(self):
        self.released = True


class FakePicam:
    """Stand-in for a Picamera2 instance.

    Models two things the backend depends on and the Mac cannot run:

    * one-frame control latency - set_controls() stores a *pending* ScalerCrop
      and each capture_request() reports the older of the two crops it holds
      before advancing, so the request handed back was captured before the
      caller's control landed;
    * a configuration-shaped default crop - camera_controls["ScalerCrop"][2]
      is the centred 16:9 band for a 16:9 output configuration and the whole
      array for a 4:3 one, which is how libcamera's rpi pipeline shapes the
      initial crop to the output aspect.
    """

    INSTANCES: list = []
    REPORT_CROP = True
    STUCK_CROP = None
    QUEUED_CROPS: tuple = ()
    # Fault knobs: each makes one documented picamera2 call raise, so the
    # backend's cache invalidation can be exercised on the failure paths.
    FAIL_START_ONCE = False
    FAIL_AUTOFOCUS = False
    FAIL_CAPTURE_METADATA = False

    @classmethod
    def global_camera_info(cls):
        return [dict(entry) for entry in GLOBAL_INFO]

    def __init__(self, camera_index=0):
        self.index = camera_index
        self.configs = []
        self.started = False
        self.options = {}
        self.controls = []
        self.requests = []
        self.saved = []
        self.stops = 0
        self._pending_crop = None
        self._reported_crop = None
        self._queued_crops = list(type(self).QUEUED_CROPS)
        self._starts_left_to_fail = 1 if type(self).FAIL_START_ONCE else 0
        type(self).INSTANCES.append(self)

    # --- documented picamera2 surface -------------------------------------
    @property
    def default_crop(self):
        """The crop libcamera would start the current configuration on."""
        if not self.configs:
            return FULL_RECT
        main_w, main_h = self.configs[-1]["main"]["size"]
        if abs((main_w / main_h) - (16 / 9)) < 0.01:
            return BAND_RECT
        return FULL_RECT

    @property
    def camera_controls(self):
        return {"ScalerCrop": (MIN_RECT, FULL_RECT, self.default_crop)}

    @property
    def camera_properties(self):
        return {
            "PixelArraySize": PIXEL_ARRAY,
            "ScalerCropMaximum": self.default_crop,
        }

    def create_still_configuration(self, **kwargs):
        return kwargs

    def create_preview_configuration(self, **kwargs):
        return kwargs

    def configure(self, config):
        self.configs.append(config)

    def start(self):
        if self._starts_left_to_fail:
            self._starts_left_to_fail -= 1
            raise RuntimeError("start() failed")
        self.started = True

    def stop(self):
        self.started = False
        self.stops += 1

    def close(self):
        self.started = False

    def set_controls(self, controls):
        self.controls.append(dict(controls))
        if "ScalerCrop" in controls:
            self._pending_crop = tuple(controls["ScalerCrop"])

    def autofocus_cycle(self):
        if self.FAIL_AUTOFOCUS:
            raise RuntimeError("autofocus_cycle() failed")
        return True

    def capture_metadata(self):
        if self.FAIL_CAPTURE_METADATA:
            raise RuntimeError("capture_metadata() failed")
        return {}

    def capture_request(self):
        if self._queued_crops:
            metadata = {"ScalerCrop": tuple(self._queued_crops.pop(0))}
        elif self.STUCK_CROP is not None:
            metadata = {"ScalerCrop": tuple(self.STUCK_CROP)}
        elif self.REPORT_CROP and self._reported_crop is not None:
            metadata = {"ScalerCrop": self._reported_crop}
        else:
            metadata = {}
        if self.STUCK_CROP is None:
            self._reported_crop = self._pending_crop
        request = FakeRequest(self, metadata)
        self.requests.append(request)
        return request

    # --- test helpers ------------------------------------------------------
    @property
    def crops(self):
        return [c["ScalerCrop"] for c in self.controls if "ScalerCrop" in c]


def _fake_class(report_crop=True, stuck_crop=None, queued_crops=(), **faults):
    """A fresh FakePicam subclass with its own instance registry."""

    attrs = {
        "INSTANCES": [],
        "REPORT_CROP": report_crop,
        "STUCK_CROP": stuck_crop,
        "QUEUED_CROPS": tuple(queued_crops),
    }
    attrs.update(faults)
    return type("_FakePicamVariant", (FakePicam,), attrs)


def _install(monkeypatch, report_crop=True, stuck_crop=None, queued_crops=(), **faults):
    """Patch the module global and return (backend, fake_class)."""

    cls = _fake_class(
        report_crop=report_crop,
        stuck_crop=stuck_crop,
        queued_crops=queued_crops,
        **faults,
    )
    monkeypatch.setattr(pb, "Picamera2", cls)
    backend = pb.Picamera2Backend(logging.getLogger("test.neh222.preview"))
    return backend, cls


def _caches_for(backend, camera_index=0):
    """The three per-camera caches that must move together."""
    return (
        backend._last_configs.get(camera_index),
        backend._format_mode.get(camera_index),
        backend._unzoomed_crop.get(camera_index),
    )


def _still_config(camera_index=0, resolution="medium", **overrides):
    kwargs = dict(
        camera_index=camera_index,
        img_size=IMG_SIZES[resolution],
        autofocus_on_capture=False,
        timeout=0,
        denoise_frames=0,
        encoding="jpg",
        raw=False,
    )
    kwargs.update(overrides)
    return CameraConfig(**kwargs)


# --------------------------------------------------------------------------
# preview_stream_size
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "still_size,expected",
    [
        ((3840, 2160), (1280, 720)),
        ((4624, 3472), (1280, 960)),
        ((2312, 1736), (1280, 960)),
        ((1000, 750), (1000, 750)),
    ],
)
def test_preview_stream_size_matches_the_still_aspect(still_size, expected):
    assert pb.preview_stream_size(still_size) == expected


def test_preview_stream_size_rounds_odd_results_down_to_even():
    assert pb.preview_stream_size((2562, 1000), max_width=1281) == (1280, 500)


def test_preview_stream_size_is_never_larger_than_the_still():
    for still_size in IMG_SIZES.values():
        width, height = pb.preview_stream_size(still_size)
        assert width <= still_size[0]
        assert height <= still_size[1]


# --------------------------------------------------------------------------
# the lores stream lives on every still configuration
# --------------------------------------------------------------------------

@pytest.mark.parametrize("resolution", ["low", "medium", "high"])
def test_every_still_configuration_carries_a_lores_stream(monkeypatch, tmp_path, resolution):
    backend, cls = _install(monkeypatch)
    backend.capture_image(tmp_path / "shot.jpg", _still_config(resolution=resolution))

    cam = cls.INSTANCES[0]
    assert len(cam.configs) == 1
    config = cam.configs[0]
    assert "lores" in config, "the still configuration has no second stream"
    assert config["lores"]["format"] == "YUV420"

    main_w, main_h = config["main"]["size"]
    lores_w, lores_h = config["lores"]["size"]
    assert lores_w <= main_w and lores_h <= main_h
    assert abs((lores_w / lores_h) - (main_w / main_h)) < 0.01


# --------------------------------------------------------------------------
# one configuration is shared by the preview and the still
# --------------------------------------------------------------------------

def test_preview_then_still_of_the_same_size_configures_once(monkeypatch, tmp_path):
    backend, cls = _install(monkeypatch)
    backend.capture_preview(0, img_size=IMG_SIZES["medium"], tmp_path=tmp_path / "p.jpg")
    backend.capture_image(tmp_path / "shot.jpg", _still_config(resolution="medium"))

    assert len(cls.INSTANCES[0].configs) == 1


def test_still_then_preview_of_the_same_size_configures_once(monkeypatch, tmp_path):
    backend, cls = _install(monkeypatch)
    backend.capture_image(tmp_path / "shot.jpg", _still_config(resolution="medium"))
    backend.capture_preview(0, img_size=IMG_SIZES["medium"], tmp_path=tmp_path / "p.jpg")

    assert len(cls.INSTANCES[0].configs) == 1


def test_a_preview_at_a_different_size_reconfigures(monkeypatch, tmp_path):
    backend, cls = _install(monkeypatch)
    backend.capture_image(tmp_path / "shot.jpg", _still_config(resolution="medium"))
    backend.capture_preview(0, img_size=IMG_SIZES["high"], tmp_path=tmp_path / "p.jpg")

    assert len(cls.INSTANCES[0].configs) == 2


# --------------------------------------------------------------------------
# which stream each path saves
# --------------------------------------------------------------------------

def test_preview_bytes_come_from_lores_and_the_still_from_main(monkeypatch, tmp_path):
    backend, cls = _install(monkeypatch)

    data = backend.capture_preview(0, img_size=IMG_SIZES["medium"], tmp_path=tmp_path / "p.jpg")
    assert data == b"\xff\xd8lores"

    out = tmp_path / "shot.jpg"
    backend.capture_image(out, _still_config(resolution="medium"))
    assert out.read_bytes() == b"\xff\xd8main"

    assert cls.INSTANCES[0].saved == ["lores", "main"]


def test_a_reconfigure_pins_the_configurations_default_crop_once(monkeypatch, tmp_path):
    """R40-1: a 16:9 configuration off a 4:3 readout starts on a centred band.

    The backend pins that band explicitly on (re)configure so preview, zoom and
    still all reference the same rectangle, and the preview's own frames are
    reported at it.
    """
    backend, cls = _install(monkeypatch)

    backend.capture_preview(0, img_size=IMG_SIZES["medium"], tmp_path=tmp_path / "p0.jpg")
    cam = cls.INSTANCES[0]
    assert cam.crops == [BAND_RECT]

    backend.capture_preview(0, img_size=IMG_SIZES["medium"], tmp_path=tmp_path / "p1.jpg")
    assert cam.crops == [BAND_RECT], "a cached configuration must not re-pin the crop"
    assert cam.requests[-1].get_metadata()["ScalerCrop"] == BAND_RECT


def test_preview_adds_no_crop_of_its_own_and_never_sleeps(monkeypatch, tmp_path):
    backend, cls = _install(monkeypatch)
    backend.capture_preview(0, img_size=IMG_SIZES["medium"], tmp_path=tmp_path / "p0.jpg")

    def _boom(_seconds):
        raise AssertionError("the preview path slept")

    monkeypatch.setattr(pb, "time", SimpleNamespace(sleep=_boom, time=lambda: 0.0))

    cam = cls.INSTANCES[0]
    before = len(cam.controls)
    backend.capture_preview(0, img_size=IMG_SIZES["medium"], tmp_path=tmp_path / "p1.jpg")

    assert all("ScalerCrop" not in call for call in cam.controls[before:])


# --------------------------------------------------------------------------
# R39-1: the still must not accept a frame queued under the preview's zoom
# --------------------------------------------------------------------------

def test_a_still_after_a_zoomed_preview_skips_the_queued_zoomed_frame(monkeypatch, tmp_path):
    backend, cls = _install(monkeypatch)

    backend.capture_preview(0, img_size=IMG_SIZES["medium"], tmp_path=tmp_path / "p0.jpg")
    backend.apply_zoom(0, 2.0)
    backend.capture_preview(0, img_size=IMG_SIZES["medium"], tmp_path=tmp_path / "p1.jpg")

    cam = cls.INSTANCES[0]
    before = len(cam.requests)

    out = tmp_path / "shot.jpg"
    backend.capture_image(out, _still_config(resolution="medium"))

    still_requests = cam.requests[before:]
    assert len(still_requests) == 2, "expected exactly one skipped frame before the capture"

    skipped, kept = still_requests
    assert skipped.saved == []
    assert skipped.released is True
    assert kept.saved == ["main"]
    assert kept.get_metadata()["ScalerCrop"] == BAND_RECT
    assert out.read_bytes() == b"\xff\xd8main"


def test_the_first_request_is_kept_when_the_crop_is_not_reported(monkeypatch, tmp_path):
    backend, cls = _install(monkeypatch, report_crop=False)

    backend.capture_preview(0, img_size=IMG_SIZES["medium"], tmp_path=tmp_path / "p.jpg")
    backend.apply_zoom(0, 2.0)

    cam = cls.INSTANCES[0]
    before = len(cam.requests)
    backend.capture_image(tmp_path / "shot.jpg", _still_config(resolution="medium"))

    still_requests = cam.requests[before:]
    assert len(still_requests) == 1
    assert still_requests[0].saved == ["main"]


def test_a_crop_stuck_at_zoom_raises_after_eight_released_requests(monkeypatch, tmp_path):
    zoomed = (1164, 874, 2328, 1748)
    backend, cls = _install(monkeypatch, stuck_crop=zoomed)

    backend.capture_preview(0, img_size=IMG_SIZES["medium"], tmp_path=tmp_path / "p.jpg")
    cam = cls.INSTANCES[0]
    before = len(cam.requests)

    with pytest.raises(RuntimeError) as excinfo:
        backend.capture_image(tmp_path / "shot.jpg", _still_config(resolution="medium"))

    assert "unzoomed" in str(excinfo.value)

    still_requests = cam.requests[before:]
    assert len(still_requests) == 8
    assert all(request.released for request in still_requests)
    assert all(request.saved == [] for request in still_requests)


def test_a_still_at_medium_captures_at_the_band_not_the_full_array(monkeypatch, tmp_path):
    """R40-2: the target is the configuration's reachable crop.

    In a 16:9 configuration the full pixel array is unreachable, so waiting for
    it would burn every retry and fail; the band frame is accepted first time.
    """
    backend, cls = _install(monkeypatch)

    # One preview first, so the fake's one-frame queue has a crop to report
    # on the still's own request rather than an empty first frame.
    backend.capture_preview(0, img_size=IMG_SIZES["medium"], tmp_path=tmp_path / "p.jpg")
    cam = cls.INSTANCES[0]
    before = len(cam.requests)

    out = tmp_path / "shot.jpg"
    backend.capture_image(out, _still_config(resolution="medium"))

    assert cam.crops[-1] == BAND_RECT
    assert FULL_RECT not in cam.crops
    still_requests = cam.requests[before:]
    assert len(still_requests) == 1
    assert still_requests[0].saved == ["main"]
    assert still_requests[0].get_metadata()["ScalerCrop"] == BAND_RECT


def test_waiting_for_the_full_array_in_a_banded_mode_gives_up(monkeypatch, tmp_path):
    """A body that only ever reports the full array under a 16:9 configuration
    is reporting something the target is not, so the still must give up rather
    than silently keep a frame at the wrong crop."""
    backend, cls = _install(monkeypatch, stuck_crop=FULL_RECT)

    with pytest.raises(RuntimeError) as excinfo:
        backend.capture_image(tmp_path / "shot.jpg", _still_config(resolution="medium"))

    assert "unzoomed" in str(excinfo.value)
    assert all(request.released for request in cls.INSTANCES[0].requests)


def test_a_frame_at_a_hair_of_zoom_is_released_not_accepted(monkeypatch, tmp_path):
    """R40-3: apply_zoom permits 1.005x, whose crop is within 1% of the target
    in both dimensions; a per-value tolerance must still reject it."""
    backend, cls = _install(monkeypatch, queued_crops=[(12, 9, 4632, 3478)])

    out = tmp_path / "shot.jpg"
    backend.capture_image(out, _still_config(resolution="high"))

    cam = cls.INSTANCES[0]
    assert len(cam.requests) == 2
    assert cam.requests[0].saved == []
    assert cam.requests[0].released is True
    assert cam.requests[1].saved == ["main"]
    assert cam.requests[1].get_metadata()["ScalerCrop"] == FULL_RECT


def test_a_crop_within_the_alignment_slack_is_accepted(monkeypatch, tmp_path):
    backend, cls = _install(monkeypatch, queued_crops=[(8, 8, 4656, 3496)])

    backend.capture_image(tmp_path / "shot.jpg", _still_config(resolution="high"))

    cam = cls.INSTANCES[0]
    assert len(cam.requests) == 1
    assert cam.requests[0].saved == ["main"]


def test_a_crop_beyond_the_alignment_slack_is_released(monkeypatch, tmp_path):
    backend, cls = _install(monkeypatch, queued_crops=[(32, 32, 4656, 3496)])

    backend.capture_image(tmp_path / "shot.jpg", _still_config(resolution="high"))

    cam = cls.INSTANCES[0]
    assert len(cam.requests) == 2
    assert cam.requests[0].saved == []
    assert cam.requests[0].released is True
    assert cam.requests[1].saved == ["main"]


def test_zoom_is_centred_inside_the_configurations_own_rectangle(monkeypatch, tmp_path):
    backend, cls = _install(monkeypatch)
    backend.capture_preview(0, img_size=IMG_SIZES["medium"], tmp_path=tmp_path / "p.jpg")
    cam = cls.INSTANCES[0]

    backend.apply_zoom(0, 2.0)
    zoomed = cam.crops[-1]

    base_x, base_y, base_w, base_h = BAND_RECT
    assert zoomed == (
        base_x + (base_w - base_w // 2) // 2,
        base_y + (base_h - base_h // 2) // 2,
        base_w // 2,
        base_h // 2,
    )
    # Same centre as the band, not the centre of the pixel array.
    assert zoomed[0] + zoomed[2] // 2 == base_x + base_w // 2
    assert zoomed[1] + zoomed[3] // 2 == base_y + base_h // 2

    backend.apply_zoom(0, 1.0)
    assert cam.crops[-1] == BAND_RECT


def test_apply_zoom_holds_the_per_body_lock(monkeypatch, tmp_path):
    backend, cls = _install(monkeypatch)
    backend.capture_preview(0, img_size=IMG_SIZES["medium"], tmp_path=tmp_path / "p.jpg")

    cam = cls.INSTANCES[0]
    lock = backend._get_camera_lock(0)
    observed = {}
    original = cam.set_controls

    def watching(controls):
        # threading.Lock is not reentrant, so a successful acquire from this
        # same thread proves apply_zoom is running unlocked.
        observed["free"] = lock.acquire(blocking=False)
        if observed["free"]:
            lock.release()
        original(controls)

    cam.set_controls = watching
    backend.apply_zoom(0, 2.0)

    assert observed.get("free") is False, "the per-body lock was free while apply_zoom ran"


# --------------------------------------------------------------------------
# R42-1: a half-finished reconfigure must leave no cache behind
# --------------------------------------------------------------------------

def test_a_failed_start_leaves_no_cached_configuration(monkeypatch, tmp_path):
    """configure() lands, start() raises: the caches must not claim the new
    configuration is in force, or the next call skips the reconfigure and
    keeps targeting the previous mode's crop."""
    backend, cls = _install(monkeypatch, FAIL_START_ONCE=True)

    with pytest.raises(RuntimeError):
        backend.capture_preview(0, img_size=IMG_SIZES["medium"], tmp_path=tmp_path / "p.jpg")

    assert _caches_for(backend) == (None, None, None)

    cam = cls.INSTANCES[0]
    assert len(cam.configs) == 1
    assert cam.crops == [], "the crop cannot be pinned when start() failed"


def test_the_call_after_a_failed_start_reconfigures_and_pins(monkeypatch, tmp_path):
    backend, cls = _install(monkeypatch, FAIL_START_ONCE=True)

    with pytest.raises(RuntimeError):
        backend.capture_preview(0, img_size=IMG_SIZES["medium"], tmp_path=tmp_path / "p0.jpg")

    backend.capture_preview(0, img_size=IMG_SIZES["medium"], tmp_path=tmp_path / "p1.jpg")

    cam = cls.INSTANCES[0]
    assert len(cam.configs) == 2, "the second call must reconfigure from scratch"
    assert cam.crops == [BAND_RECT]
    assert backend._unzoomed_crop[0] == BAND_RECT


def test_a_failed_autofocus_calibration_leaves_no_stale_target(monkeypatch, tmp_path):
    """The AF routine reconfigures and only clears the caches on its way out,
    so a failure part-way leaves them describing a mode that is gone."""
    backend, cls = _install(monkeypatch, FAIL_AUTOFOCUS=True)

    # A 4:3 configuration first: its unzoomed rectangle is the whole array.
    backend.capture_preview(0, img_size=IMG_SIZES["high"], tmp_path=tmp_path / "p.jpg")
    assert backend._unzoomed_crop[0] == FULL_RECT

    with pytest.raises(RuntimeError):
        backend.run_autofocus_calibration(0, IMG_SIZES["medium"])

    assert _caches_for(backend) == (None, None, None)

    # The body is now on the calibration's 16:9 configuration, so the
    # rectangle re-derived from it is the band, not the array it was.
    cam = cls.INSTANCES[0]
    assert backend._unzoomed_rect(cam, 0) == BAND_RECT


def test_a_failed_white_balance_calibration_leaves_no_stale_target(monkeypatch, tmp_path):
    backend, cls = _install(monkeypatch, FAIL_CAPTURE_METADATA=True)

    backend.capture_preview(0, img_size=IMG_SIZES["high"], tmp_path=tmp_path / "p.jpg")
    assert backend._unzoomed_crop[0] == FULL_RECT

    with pytest.raises(RuntimeError):
        backend.run_white_balance_calibration(0, stabilization_frames=1)

    assert _caches_for(backend) == (None, None, None)

    cam = cls.INSTANCES[0]
    assert backend._unzoomed_rect(cam, 0) == BAND_RECT


def test_a_still_after_a_failed_calibration_targets_the_current_mode(monkeypatch, tmp_path):
    backend, cls = _install(monkeypatch, FAIL_AUTOFOCUS=True)

    backend.capture_preview(0, img_size=IMG_SIZES["high"], tmp_path=tmp_path / "p.jpg")
    with pytest.raises(RuntimeError):
        backend.run_autofocus_calibration(0, IMG_SIZES["medium"])

    cam = cls.INSTANCES[0]
    configs_before = len(cam.configs)

    backend.capture_image(tmp_path / "shot.jpg", _still_config(resolution="medium"))

    assert len(cam.configs) == configs_before + 1, "the still must reconfigure"
    assert backend._unzoomed_crop[0] == BAND_RECT
    assert cam.crops[-1] == BAND_RECT
    assert FULL_RECT not in cam.crops[configs_before:]


# --------------------------------------------------------------------------
# service layer
# --------------------------------------------------------------------------

class _RecordingPicamera2Backend(pb.Picamera2Backend):
    """A Picamera2Backend whose capture_preview only records its arguments."""

    def __init__(self):  # deliberately does not call super()
        self.calls = []

    def capture_preview(self, camera_index, img_size=None, tmp_path=None):
        self.calls.append((camera_index, img_size, tmp_path))
        return b"\xff\xd8lores"


class _NativePreviewBackend:
    """A gphoto2-style backend whose capture_preview takes no size."""

    def __init__(self):
        self.calls = []

    def capture_preview(self, camera_index):
        self.calls.append(camera_index)
        return b"\xff\xd8native"


def test_the_service_passes_the_requested_size_to_the_pi_backend(monkeypatch):
    backend = _RecordingPicamera2Backend()
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda index: True)

    data = capture_service.capture_preview_frame(0, "high")

    assert data == b"\xff\xd8lores"
    assert len(backend.calls) == 1
    assert backend.calls[0][0] == 0
    assert backend.calls[0][1] == IMG_SIZES["high"] == (4624, 3472)


def test_the_service_rejects_an_unknown_resolution():
    with pytest.raises(ValueError):
        capture_service.capture_preview_frame(0, "bogus")


def test_a_native_preview_backend_is_called_without_a_size(monkeypatch):
    backend = _NativePreviewBackend()
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda index: True)

    data = capture_service.capture_preview_frame(0, "high")

    assert data == b"\xff\xd8native"
    assert backend.calls == [0]


# --------------------------------------------------------------------------
# route
# --------------------------------------------------------------------------

def _user(username, role):
    from app.models.user import User

    return User(
        username=username,
        email=f"{username}@example.com",
        hashed_password="x",
        role=role,
        is_active=True,
    )


def _client_as(client, username, role):
    from app.main import app
    from app.api.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


def test_the_preview_route_rejects_an_unknown_resolution(client):
    api = _client_as(client, "op", "operator")

    resp = api.get("/cameras/preview/0", params={"resolution": "bogus"})

    assert resp.status_code == 422, resp.text


def test_a_rectangle_object_crop_is_read_like_a_sequence():
    """A real libcamera build may report ScalerCrop as a Rectangle with
    x/y/width/height attributes rather than a 4-sequence; both must be read."""
    from types import SimpleNamespace
    from capture.backends.picamera2_backend import _crop_size

    assert _crop_size((0, 0, 4656, 3496)) == (4656, 3496)
    assert _crop_size([10, 20, 100, 50]) == (100, 50)
    assert _crop_size(SimpleNamespace(x=0, y=0, width=4656, height=3496)) == (4656, 3496)


def test_a_rectangle_object_crop_yields_all_four_values():
    from types import SimpleNamespace
    from capture.backends.picamera2_backend import _crop_rect

    assert _crop_rect((0, 438, 4656, 2620)) == (0, 438, 4656, 2620)
    assert _crop_rect([10, 20, 100, 50]) == (10, 20, 100, 50)
    assert _crop_rect(
        SimpleNamespace(x=12, y=9, width=4632, height=3478)
    ) == (12, 9, 4632, 3478)


def test_turning_raw_on_with_the_same_encoding_reconfigures(monkeypatch, tmp_path):
    """A PNG still with raw=False and one with raw=True share use_yuv (both
    RGB888), so without `raw` in the reconfigure key the second would reuse
    the first configuration and have no raw stream to save a sidecar from
    (Copilot, C91b-2)."""
    backend, cls = _install(monkeypatch)
    backend.capture_image(tmp_path / "a.png", _still_config(resolution="high", encoding="png", raw=False))
    configures_before = len(cls.INSTANCES[0].configs)
    backend.capture_image(tmp_path / "b.png", _still_config(resolution="high", encoding="png", raw=True))
    assert len(cls.INSTANCES[0].configs) == configures_before + 1
    assert "raw" in cls.INSTANCES[0].configs[-1]


def test_without_the_control_the_fallback_is_the_modes_maximum_crop(monkeypatch, tmp_path):
    """A build that does not expose camera_controls["ScalerCrop"] must not
    fall back to the pixel array in a banded mode: ScalerCropMaximum is the
    reachable rectangle there (Copilot, second review)."""
    backend, cls = _install(monkeypatch)
    cam_cls = cls
    backend.capture_preview(0, img_size=IMG_SIZES["medium"], tmp_path=tmp_path / "p.jpg")
    cam = cam_cls.INSTANCES[0]
    monkeypatch.setattr(type(cam), "camera_controls", property(lambda self: {}), raising=False)
    backend._invalidate_camera_caches(0)
    rect = backend._unzoomed_rect(cam, 0)
    assert rect == pb._crop_rect(cam.camera_properties["ScalerCropMaximum"])
