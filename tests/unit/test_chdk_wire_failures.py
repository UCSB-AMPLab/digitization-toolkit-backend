"""Every conversation with a body can fail below the protocol, and then the
session is dead whatever the protocol thought.

pyusb raises its own error when a cable is pulled or an endpoint stalls, and
it is not a PTPError, so code that evicts on PTPError alone leaves a dead
body cached, still reporting itself connected, for the next operation to
reuse. The rule here is simpler than a list of classes: an operation that
touched the wire and failed drops the body, and the next scan reopens it.

The first thing any capture does is put the body in record mode, which on a
cold start is also the likeliest thing to time out. That switch has to be
inside the same handling as the capture itself, or the commonest
first-contact failure there is reaches the operator as a generic 500 instead
of a timeout.
"""

import pytest

from capture.backends.errors import CaptureTimeoutError
from capture.camera import CameraConfig

from .chdk_fakes import (
    Body,
    TransportError,
    make_backend,
    make_pychdk,
    viewport_frame,
)


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"
JPEG = b"\xff\xd8\xff\xe0 page \xff\xd9"


def _config(index=0):
    return CameraConfig(camera_index=index)


@pytest.mark.unit
def test_a_transport_failure_during_a_capture_drops_the_body(monkeypatch, tmp_path):
    body = Body(
        serial="AAA111", card=EVEN_CARD,
        shoot_error=TransportError("[Errno 19] No such device"),
    )
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(RuntimeError) as exc:
        backend.capture_image(tmp_path / "page.jpg", _config())

    assert "usb:001,004" in str(exc.value)
    assert body.closes == 1, "a body that failed below the protocol was kept"
    assert backend._body_at(0) is None


@pytest.mark.unit
def test_a_transport_failure_during_a_preview_drops_the_body(monkeypatch):
    body = Body(
        serial="AAA111", card=EVEN_CARD, frame=viewport_frame(),
        preview_error=TransportError("[Errno 19] No such device"),
    )
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(RuntimeError):
        backend.capture_preview(0)

    assert body.closes == 1
    assert backend._body_at(0) is None


@pytest.mark.unit
def test_a_transport_failure_writing_a_card_drops_the_body(monkeypatch):
    body = Body(
        serial="AAA111", card=EVEN_CARD,
        upload_error=TransportError("[Errno 19] No such device"),
    )
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(RuntimeError):
        backend.assign_side(0, "odd")

    assert body.closes == 1
    assert backend._body_at(0) is None


@pytest.mark.unit
def test_a_mode_switch_that_times_out_is_a_capture_timeout(monkeypatch, tmp_path):
    """Cold start: the body never reaches record mode, and nobody ever shoots."""
    body = Body(
        serial="AAA111", card=EVEN_CARD, image=JPEG,
        mode_error=TimeoutError("Script still running after 5s"),
    )
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(CaptureTimeoutError) as exc:
        backend.capture_image(tmp_path / "page.jpg", _config())

    assert "record mode" in str(exc.value)
    assert body.shots == []
    assert body.closes == 1


@pytest.mark.unit
def test_a_mode_switch_that_fails_during_a_preview_is_reported(monkeypatch):
    body = Body(
        serial="AAA111", card=EVEN_CARD, frame=viewport_frame(),
        mode_error=TransportError("[Errno 19] No such device"),
    )
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(RuntimeError) as exc:
        backend.capture_preview(0)

    assert "usb:001,004" in str(exc.value)
    assert body.closes == 1


@pytest.mark.unit
def test_a_dropped_body_is_looked_for_again_on_the_next_question(
    monkeypatch, tmp_path
):
    """An eviction must not need an operator's rescan to undo itself."""
    body = Body(
        serial="AAA111", card=EVEN_CARD,
        shoot_error=TransportError("[Errno 19] No such device"),
    )
    fake = make_pychdk(body)
    backend = make_backend(monkeypatch, fake)
    backend.list_devices()

    with pytest.raises(RuntimeError):
        backend.capture_image(tmp_path / "page.jpg", _config())
    scans = fake.list_calls

    assert backend.is_camera_connected(0) is True
    assert fake.list_calls == scans + 1, "the dropped body was never looked for"
    assert body.opens == 2


@pytest.mark.unit
def test_a_body_that_is_really_gone_is_not_looked_for_every_time(monkeypatch):
    """One scan settles it; after that the index is simply empty."""
    body = Body(serial="AAA111", card=EVEN_CARD)
    fake = make_pychdk(body)
    backend = make_backend(monkeypatch, fake)
    backend.list_devices()

    fake.bodies = []
    backend.list_devices()
    scans = fake.list_calls

    assert backend.is_camera_connected(0) is False
    assert backend.is_camera_connected(0) is False
    assert fake.list_calls == scans, "every question re-enumerated the whole bus"
