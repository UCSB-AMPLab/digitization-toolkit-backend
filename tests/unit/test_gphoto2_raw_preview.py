"""
Unit tests for _write_raw_preview() in capture.backends.gphoto2_backend.

Covers the thumb.format branch (JPEG bytes written directly vs. a BITMAP
array encoded through Pillow), the rawpy-missing warning path, an
extract_thumb() exception, and an unrecognized thumbnail format - none of
which may raise, since the preview extraction runs after the master file
has already been saved and deleted from the camera.
"""

import logging
from contextlib import contextmanager
from enum import Enum
from types import SimpleNamespace

import numpy as np
from PIL import Image

import capture.backends.gphoto2_backend as gb


class _FakeThumbFormat(Enum):
    JPEG = "jpeg"
    BITMAP = "bitmap"


class _FakeRawFile:
    def __init__(self, thumb=None, raise_on_extract=None):
        self._thumb = thumb
        self._raise_on_extract = raise_on_extract

    def extract_thumb(self):
        if self._raise_on_extract is not None:
            raise self._raise_on_extract
        return self._thumb


class _FakeRawpy:
    """Stand-in for the rawpy module, monkeypatched onto gb.rawpy."""

    ThumbFormat = _FakeThumbFormat

    def __init__(self, thumb=None, raise_on_extract=None):
        self._thumb = thumb
        self._raise_on_extract = raise_on_extract

    @contextmanager
    def imread(self, path):
        yield _FakeRawFile(self._thumb, self._raise_on_extract)


def test_jpeg_thumb_is_written_directly(monkeypatch, tmp_path):
    data = b"\xff\xd8fake-jpeg-bytes"
    thumb = SimpleNamespace(format=_FakeThumbFormat.JPEG, data=data)
    monkeypatch.setattr(gb, "rawpy", _FakeRawpy(thumb=thumb))
    monkeypatch.setattr(gb, "_RAWPY_AVAILABLE", True)

    raw_path = tmp_path / "IMG_0001.CR2"
    raw_path.write_bytes(b"fake-raw-file")
    preview_path = tmp_path / "IMG_0001_preview.jpg"
    logger = logging.getLogger("test.gphoto2.jpeg")

    result = gb._write_raw_preview(raw_path, preview_path, logger)

    assert result is True
    assert preview_path.read_bytes() == data
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [raw_path.name, preview_path.name]
    )


def test_bitmap_thumb_is_encoded_as_jpeg(monkeypatch, tmp_path):
    bitmap = np.zeros((8, 8, 3), dtype="uint8")
    thumb = SimpleNamespace(format=_FakeThumbFormat.BITMAP, data=bitmap)
    monkeypatch.setattr(gb, "rawpy", _FakeRawpy(thumb=thumb))
    monkeypatch.setattr(gb, "_RAWPY_AVAILABLE", True)

    raw_path = tmp_path / "IMG_0002.CR2"
    raw_path.write_bytes(b"fake-raw-file")
    preview_path = tmp_path / "IMG_0002_preview.jpg"
    logger = logging.getLogger("test.gphoto2.bitmap")

    result = gb._write_raw_preview(raw_path, preview_path, logger)

    assert result is True
    assert preview_path.exists()
    with Image.open(preview_path) as img:
        assert img.format == "JPEG"


def test_missing_rawpy_warns_and_writes_nothing(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(gb, "_RAWPY_AVAILABLE", False)

    raw_path = tmp_path / "IMG_0003.CR2"
    raw_path.write_bytes(b"fake-raw-file")
    preview_path = tmp_path / "IMG_0003_preview.jpg"
    logger = logging.getLogger("test.gphoto2.missing")

    with caplog.at_level(logging.WARNING, logger="test.gphoto2.missing"):
        result = gb._write_raw_preview(raw_path, preview_path, logger)

    assert result is False
    assert not preview_path.exists()
    assert any(
        record.levelno == logging.WARNING
        and "rawpy" in record.message
        and "IMG_0003.CR2" in record.message
        for record in caplog.records
    )


def test_extract_thumb_exception_is_caught_and_warned(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        gb, "rawpy", _FakeRawpy(raise_on_extract=RuntimeError("boom"))
    )
    monkeypatch.setattr(gb, "_RAWPY_AVAILABLE", True)

    raw_path = tmp_path / "IMG_0004.CR2"
    raw_path.write_bytes(b"fake-raw-file")
    preview_path = tmp_path / "IMG_0004_preview.jpg"
    logger = logging.getLogger("test.gphoto2.exc")

    with caplog.at_level(logging.WARNING, logger="test.gphoto2.exc"):
        result = gb._write_raw_preview(raw_path, preview_path, logger)

    assert result is False
    assert not preview_path.exists()
    assert any(
        record.levelno == logging.WARNING and "boom" in record.message
        for record in caplog.records
    )


def test_unknown_thumb_format_warns_and_returns_false(monkeypatch, tmp_path, caplog):
    thumb = SimpleNamespace(format="unrecognized-format", data=b"whatever")
    monkeypatch.setattr(gb, "rawpy", _FakeRawpy(thumb=thumb))
    monkeypatch.setattr(gb, "_RAWPY_AVAILABLE", True)

    raw_path = tmp_path / "IMG_0005.CR2"
    raw_path.write_bytes(b"fake-raw-file")
    preview_path = tmp_path / "IMG_0005_preview.jpg"
    logger = logging.getLogger("test.gphoto2.unknown")

    with caplog.at_level(logging.WARNING, logger="test.gphoto2.unknown"):
        result = gb._write_raw_preview(raw_path, preview_path, logger)

    assert result is False
    assert not preview_path.exists()
    assert any(
        record.levelno == logging.WARNING and "unrecognized-format" in record.message
        for record in caplog.records
    )
