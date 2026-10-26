"""Rotation reaches every file of a multi-file capture.

Picamera2Backend returns `((jpeg_path, raw_path), metadata)` when it saves the
raw buffer beside the JPEG. Each path of the tuple is rotated on its own; a
path built from the tuple's repr names no file. The JPEG gets the EXIF
Orientation tag; the `.raw` buffer is a preservation master and stays byte
for byte as the camera wrote it.

    python -m pytest tests/unit/test_rotation_multi_file.py

Version: 0.1.0-beta
"""

import io
from pathlib import Path

import pytest

import capture.service as capture_service
from capture.camera import CameraConfig

# EXIF Orientation for a 270 deg clockwise rotation (capture.service).
_ORIENTATION_270 = 8
_RAW_BYTES = b"raw sensor buffer"


def _jpeg() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 6), (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _orientation(path: Path):
    import piexif
    return piexif.load(str(path))["0th"].get(piexif.ImageIFD.Orientation)


class _MultiFormatBackend:
    """Writes a JPEG and a `.raw` buffer and returns them as picamera2 does."""

    def capture_image(self, output_path, camera_config, capture_output=False):
        jpeg = Path(output_path)
        raw = jpeg.with_suffix(".raw")
        jpeg.write_bytes(_jpeg())
        raw.write_bytes(_RAW_BYTES)
        return (str(jpeg), str(raw)), {"SensorTimestamp": 1}


@pytest.fixture
def backend(monkeypatch):
    b = _MultiFormatBackend()
    monkeypatch.setattr(capture_service, "get_backend", lambda: b)
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    return b


@pytest.mark.unit
def test_capture_image_rotates_the_jpeg_of_a_multiformat_capture(backend, tmp_path):
    config = CameraConfig(camera_index=0, rotate_deg=270, raw=True)

    paths, metadata = capture_service.capture_image(
        "proj", config, output_filename="page_c0.jpg", check_camera=False, root=tmp_path)

    jpeg, raw = (Path(p) for p in paths)
    assert metadata == {"SensorTimestamp": 1}
    assert _orientation(jpeg) == _ORIENTATION_270
    assert raw.read_bytes() == _RAW_BYTES


@pytest.mark.unit
def test_dual_capture_rotates_the_jpeg_of_each_multiformat_capture(
        backend, tmp_path, monkeypatch):
    # The manifest is not under test here; only what reaches the files.
    monkeypatch.setattr(capture_service, "append_manifest_record", lambda root, record: None)
    cam0 = CameraConfig(camera_index=0, rotate_deg=270, raw=True)
    cam1 = CameraConfig(camera_index=1, rotate_deg=270, raw=True)

    p0, p1, _capture_id, _pair_id = capture_service.dual_capture_image(
        "proj", cam0, cam1, check_camera=False, stagger_ms=0,
        filename_index="proj-img_0001", root=tmp_path)

    for paths in (p0, p1):
        jpeg, raw = (Path(p) for p in paths)
        assert _orientation(jpeg) == _ORIENTATION_270
        assert raw.read_bytes() == _RAW_BYTES
