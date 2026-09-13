"""The live preview: one CHDK viewport frame, encoded for the dashboard.

The capture service routes every non-picamera2 backend's preview straight to
capture_preview(camera_index), so this is the whole of the polling path. It
shares the body's lock with capture, because pychdk assumes one thread per
device and a preview poll landing inside a capture would interleave two
conversations on one USB endpoint.

The instrumentation here is for the bench, which is the only session with
hardware: the frame geometry once per body, and the frame rate every hundred
frames, so nobody has to stand over the rig with a stopwatch.
"""

import pytest

from .chdk_fakes import Body, PTPError, make_backend, make_pychdk, viewport_frame


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"


@pytest.mark.unit
def test_a_preview_frame_comes_back_as_a_jpeg(monkeypatch):
    body = Body(serial="AAA111", card=EVEN_CARD, frame=viewport_frame(8, 2))
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    jpeg = backend.capture_preview(0)

    assert jpeg[:2] == b"\xff\xd8", "that is not a JPEG"
    assert jpeg[-2:] == b"\xff\xd9"


@pytest.mark.unit
def test_only_the_viewport_block_is_asked_for(monkeypatch):
    """Asking for the bitmap and palette too would cost bytes nobody shows."""
    body = Body(serial="AAA111", card=EVEN_CARD, frame=viewport_frame())
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    backend.capture_preview(0)

    assert body.device._chdk.last_flags == 0x01


@pytest.mark.unit
def test_the_body_is_put_in_record_mode_before_the_first_frame(monkeypatch):
    body = Body(serial="AAA111", card=EVEN_CARD, frame=viewport_frame())
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    backend.capture_preview(0)
    backend.capture_preview(0)

    assert body.mode_switches == ["record"], "record mode is switched per frame"


@pytest.mark.unit
def test_a_body_with_no_identity_can_still_be_previewed(monkeypatch):
    """It may not capture, but the operator has to be able to aim it."""
    body = Body(serial=None, card=b"EVEN\n", frame=viewport_frame())
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    assert backend.capture_preview(0)[:2] == b"\xff\xd8"


@pytest.mark.unit
def test_previewing_an_index_with_no_body_says_so(monkeypatch):
    body = Body(serial="AAA111", card=EVEN_CARD, frame=viewport_frame())
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(RuntimeError) as exc:
        backend.capture_preview(1)

    assert "not connected" in str(exc.value)


@pytest.mark.unit
def test_a_ptp_failure_mid_frame_names_its_code_and_drops_the_body(monkeypatch):
    body = Body(
        serial="AAA111", card=EVEN_CARD, preview_error=PTPError(0x2005),
    )
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(RuntimeError) as exc:
        backend.capture_preview(0)

    assert "PTP 0x2005" in str(exc.value)
    assert body.closes == 1
    assert backend._body_at(0) is None


@pytest.mark.unit
def test_a_frame_the_decoder_refuses_is_reported_not_crashed_on(monkeypatch):
    body = Body(serial="AAA111", card=EVEN_CARD, frame=b"far too short")
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(RuntimeError) as exc:
        backend.capture_preview(0)

    assert "short" in str(exc.value)
    assert body.closes == 0, "a bad frame is not a reason to drop the body"


@pytest.mark.unit
def test_the_frame_geometry_is_logged_once_per_body(monkeypatch, caplog):
    body = Body(
        serial="AAA111", card=EVEN_CARD,
        frame=viewport_frame(704, 232, buffer_width=720, margins=(8, 4, 8, 4)),
    )
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with caplog.at_level("INFO", logger="test-chdk"):
        for _ in range(3):
            backend.capture_preview(0)

    geometry = [
        record.getMessage() for record in caplog.records
        if "live view" in record.getMessage()
    ]
    assert len(geometry) == 1, "the geometry is logged per frame"
    line = geometry[0]
    assert "2.1" in line
    assert "buffer 720" in line
    assert "visible 704x232" in line
    assert "margins" in line
    assert "720x540" in line, "the jpeg the operator actually gets"


@pytest.mark.unit
def test_the_frame_rate_is_reported_every_hundred_frames(monkeypatch, caplog):
    body = Body(serial="AAA111", card=EVEN_CARD, frame=viewport_frame())
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with caplog.at_level("INFO", logger="test-chdk"):
        for _ in range(100):
            backend.capture_preview(0)

    rate = [
        record.getMessage() for record in caplog.records
        if "preview frames" in record.getMessage()
    ]
    assert len(rate) == 1
    assert "100 preview frames" in rate[0]
    assert "mean" in rate[0]
    assert "worst" in rate[0]
