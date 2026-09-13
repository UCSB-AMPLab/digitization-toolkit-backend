"""Capturing a page over USB, and refusing to when the rig is not ready.

CHDK's remote capture hands the JPEG straight down the wire, so the picture
never touches the card. What can go wrong is bounded and each failure has to
arrive carrying its own evidence: the bench is the only session with hardware,
and "capture failed" in a log costs that session.

Two refusals happen before the shutter. A body with no identity may not
capture, because nothing it produces could be attributed to a body; and
neither may a body whose parity another body also claims, because one of the
two would be filed as the wrong page.
"""

import pytest

from capture.backends.errors import CaptureTimeoutError
from capture.camera import CameraConfig

from .chdk_fakes import Body, PTPError, make_backend, make_pychdk


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"
ODD_CARD = b"ODD\nid=bbbbbbbbbbbb\n"
JPEG = b"\xff\xd8\xff\xe0 pretend this is a page \xff\xd9"


def _config(index=0, **kwargs):
    return CameraConfig(camera_index=index, **kwargs)


@pytest.mark.unit
def test_a_capture_writes_the_bytes_and_reports_the_path(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    path, metadata = backend.capture_image(tmp_path / "page.jpg", _config())

    assert metadata is None
    assert path == str(tmp_path / "page.jpg")
    assert (tmp_path / "page.jpg").read_bytes() == JPEG
    assert list(tmp_path.iterdir()) == [tmp_path / "page.jpg"], "a temp file was left"
    assert body.shots[0]["stream"] is True, "the card must not be involved"


@pytest.mark.unit
def test_the_saved_file_is_a_jpeg_whatever_it_was_asked_for(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    path, _ = backend.capture_image(tmp_path / "page.cr2", _config())

    assert path.endswith("page.jpg")


@pytest.mark.unit
def test_record_mode_is_switched_once_and_then_remembered(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    backend.capture_image(tmp_path / "one.jpg", _config())
    backend.capture_image(tmp_path / "two.jpg", _config())

    assert body.mode_switches == ["record"]


@pytest.mark.unit
def test_the_shutter_string_reaches_the_library_as_seconds(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    backend.capture_image(
        tmp_path / "page.jpg", _config(shutter_speed="1/250", iso=400)
    )

    assert body.shots[0]["shutter_speed"] == pytest.approx(1 / 250)
    assert body.shots[0]["market_iso"] == 400


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, "auto", "bulb", "", "nonsense", "0"])
def test_a_shutter_speed_that_names_no_duration_is_left_to_the_body(
    monkeypatch, tmp_path, value
):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    backend.capture_image(tmp_path / "page.jpg", _config(shutter_speed=value))

    assert body.shots[0]["shutter_speed"] is None


@pytest.mark.unit
def test_a_body_that_never_delivers_raises_the_shared_timeout(monkeypatch, tmp_path):
    """The route turns this class into a 504; a plain RuntimeError is a 500."""
    body = Body(
        serial="AAA111", card=EVEN_CARD,
        shoot_error=TimeoutError("Remote capture did not complete"),
    )
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(CaptureTimeoutError) as exc:
        backend.capture_image(tmp_path / "page.jpg", _config())

    assert "usb:001,004" in str(exc.value)
    assert not list(tmp_path.iterdir()), "a failed capture left a file behind"


@pytest.mark.unit
def test_a_ptp_failure_names_its_code_and_drops_the_body(monkeypatch, tmp_path):
    """0x2002 from remote capture is this row's load-bearing risk (NEH-231)."""
    body = Body(
        serial="AAA111", card=EVEN_CARD, shoot_error=PTPError(0x2002),
    )
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(RuntimeError) as exc:
        backend.capture_image(tmp_path / "page.jpg", _config())

    message = str(exc.value)
    assert "PTP 0x2002" in message
    assert "usb:001,004" in message
    assert not isinstance(exc.value, CaptureTimeoutError)
    assert body.closes == 1, "a body that failed over PTP was left open"
    assert backend._body_at(0) is None, "the failed body is still in the map"


@pytest.mark.unit
def test_a_body_dropped_by_a_failure_comes_back_on_the_next_scan(
    monkeypatch, tmp_path, caplog
):
    body = Body(serial="AAA111", card=EVEN_CARD, shoot_error=PTPError(0x2002))
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(RuntimeError):
        backend.capture_image(tmp_path / "page.jpg", _config())

    with caplog.at_level("INFO", logger="test-chdk"):
        rows = backend.list_devices()

    assert [row["index"] for row in rows] == [0]
    assert body.opens == 2
    line = "\n".join(record.getMessage() for record in caplog.records)
    assert "re-enumeration recovered it" in line


@pytest.mark.unit
def test_a_body_with_no_identity_refuses_to_capture(monkeypatch, tmp_path):
    body = Body(serial=None, card=b"EVEN\n", image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(RuntimeError) as exc:
        backend.capture_image(tmp_path / "page.jpg", _config())

    assert "/side/" in str(exc.value)
    assert body.shots == [], "the shutter fired on a body with no identity"


@pytest.mark.unit
def test_two_bodies_on_one_parity_refuse_to_capture(monkeypatch, tmp_path):
    first = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD, image=JPEG)
    second = Body(bus=1, address=7, serial="BBB222", card=EVEN_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(first, second))
    backend.list_devices()

    for index in (0, 1):
        with pytest.raises(RuntimeError) as exc:
            backend.capture_image(tmp_path / f"page{index}.jpg", _config(index))
        assert "even" in str(exc.value)

    assert first.shots == [] and second.shots == []


@pytest.mark.unit
def test_capturing_from_an_index_with_no_body_says_so(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(RuntimeError) as exc:
        backend.capture_image(tmp_path / "page.jpg", _config(1))

    assert "not connected" in str(exc.value)


@pytest.mark.unit
def test_a_capture_logs_what_it_cost(monkeypatch, tmp_path, caplog):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with caplog.at_level("INFO", logger="test-chdk"):
        backend.capture_image(tmp_path / "page.jpg", _config())

    line = "\n".join(record.getMessage() for record in caplog.records)
    assert f"{len(JPEG)} bytes" in line
    assert "usb:001,004" in line
    assert "s from shutter to disk" in line


@pytest.mark.unit
def test_the_backend_reports_what_it_can_do(monkeypatch):
    backend = make_backend(monkeypatch, make_pychdk(Body()))

    assert backend.get_backend_name() == "chdk"
    assert backend.supports_streaming() is False
    assert backend.supports_live_adjustment() is False
    capabilities = backend.get_capabilities()
    assert capabilities["live_preview"] is True
    assert not any(
        value for key, value in capabilities.items() if key != "live_preview"
    )
