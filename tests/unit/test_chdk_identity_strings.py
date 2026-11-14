"""What the USB descriptors say, and what the backend makes of it.

Real pychdk 0.1.3 returns the A2500's serial with a trailing NUL byte, and the
A2500's USB product string is the generic "Canon Digital Camera".

Version: 0.1.0-beta
"""

import pytest

from .chdk_fakes import Body, make_backend, make_pychdk


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"


def _row(monkeypatch, body):
    backend = make_backend(monkeypatch, make_pychdk(body))
    return backend.list_devices()[0]


@pytest.mark.unit
def test_a_trailing_nul_is_removed_from_the_serial(monkeypatch):
    body = Body(serial="ABC\x00", product_id=0x3271, card=EVEN_CARD)

    row = _row(monkeypatch, body)

    assert row["serial"] == "ABC"
    assert row["hardware_id"] == "canon3271_ABC"
    assert "\x00" not in repr(row)


@pytest.mark.unit
@pytest.mark.parametrize("serial", ["\x00", " \x00 ", "", None])
def test_a_serial_with_nothing_but_nul_falls_back_to_the_card_id(
    monkeypatch, serial
):
    body = Body(serial=serial, product_id=0x3271, card=EVEN_CARD)

    row = _row(monkeypatch, body)

    assert row["serial"] is None
    assert row["hardware_id"] == "canon3271_aaaaaaaaaaaa"
    assert row["provisional"] is False


@pytest.mark.unit
def test_a_known_product_id_names_the_model_over_a_generic_string(monkeypatch):
    body = Body(product_id=0x3271, product="Canon Digital Camera", card=EVEN_CARD)

    assert _row(monkeypatch, body)["model"] == "Canon PowerShot A2500"


@pytest.mark.unit
def test_a_known_product_id_names_the_model_without_a_product_string(monkeypatch):
    body = Body(product_id=0x3271, product=None, card=EVEN_CARD)

    assert _row(monkeypatch, body)["model"] == "Canon PowerShot A2500"


@pytest.mark.unit
def test_an_unknown_product_id_keeps_the_product_string(monkeypatch):
    body = Body(product_id=0x1234, product=" Canon Other\x00 ", card=EVEN_CARD)

    assert _row(monkeypatch, body)["model"] == "Canon Other"


@pytest.mark.unit
def test_an_unknown_product_id_without_a_string_gets_the_id_label(monkeypatch):
    body = Body(product_id=0x1234, product=None, card=EVEN_CARD)

    assert _row(monkeypatch, body)["model"] == "Canon 0x1234"


# The saved picture's own EXIF is the primary source of the model. The USB
# string and the product-id table serve only until the first picture.

import io

from PIL import Image

from capture.camera import CameraConfig


def _jpeg(model=None):
    image = Image.new("RGB", (4, 4), "white")
    buffer = io.BytesIO()
    if model is None:
        image.save(buffer, "JPEG")
    else:
        exif = Image.Exif()
        exif[0x0110] = model
        image.save(buffer, "JPEG", exif=exif)
    return buffer.getvalue()


def _capture(monkeypatch, tmp_path, body):
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()
    path, _ = backend.capture_image(tmp_path / "page.jpg", CameraConfig(camera_index=0))
    return backend, path


@pytest.mark.unit
def test_the_exif_model_replaces_the_generic_name_after_a_capture(
    monkeypatch, tmp_path
):
    body = Body(
        product_id=0x1234, product="Canon Digital Camera", card=EVEN_CARD,
        image=_jpeg("Canon PowerShot A2500"),
    )
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()
    assert backend.camera_model(0) == "Canon Digital Camera"

    backend.capture_image(tmp_path / "page.jpg", CameraConfig(camera_index=0))

    assert backend.camera_model(0) == "Canon PowerShot A2500"
    assert backend.list_devices()[0]["model"] == "Canon PowerShot A2500"


@pytest.mark.unit
@pytest.mark.parametrize("tag", ["Canon PowerShot A2500 ", "Canon PowerShot A2500\x00 "])
def test_the_exif_model_is_cleaned(monkeypatch, tmp_path, tag):
    body = Body(
        product_id=0x1234, product="Canon Digital Camera", card=EVEN_CARD,
        image=_jpeg(tag),
    )

    backend, _ = _capture(monkeypatch, tmp_path, body)

    assert backend.camera_model(0) == "Canon PowerShot A2500"


@pytest.mark.unit
def test_a_jpeg_without_exif_leaves_the_model_and_the_capture_succeeds(
    monkeypatch, tmp_path
):
    body = Body(
        product_id=0x1234, product="Canon Digital Camera", card=EVEN_CARD,
        image=_jpeg(None),
    )

    backend, path = _capture(monkeypatch, tmp_path, body)

    assert (tmp_path / "page.jpg").exists()
    assert backend.camera_model(0) == "Canon Digital Camera"


@pytest.mark.unit
def test_an_unreadable_file_or_a_placeholder_changes_nothing(monkeypatch, tmp_path):
    for n, image in enumerate((b"\xff\xd8 not a real jpeg \xff\xd9", _jpeg("Unknown"))):
        body = Body(
            product_id=0x1234, product="Canon Digital Camera", card=EVEN_CARD,
            image=image,
        )
        backend = make_backend(monkeypatch, make_pychdk(body))
        backend.list_devices()

        backend.capture_image(tmp_path / f"page{n}.jpg", CameraConfig(camera_index=0))

        assert backend.camera_model(0) == "Canon Digital Camera"


@pytest.mark.unit
def test_the_first_pictures_recorded_model_is_its_exif_model(monkeypatch, tmp_path):
    """The service reads backend.camera_model after capture_image returns."""
    body = Body(
        product_id=0x1234, product="Canon Digital Camera", card=EVEN_CARD,
        image=_jpeg("Canon PowerShot A2500 "),
    )

    backend, _ = _capture(monkeypatch, tmp_path, body)

    assert backend.camera_model(0) == "Canon PowerShot A2500"
    assert body.shots, "a picture was taken"
