"""A body that accepts the mode switch and reports playback gets a half-press.

On 12 November 2026 two A2500 bodies under CHDK 1.5.1 accepted
switch_mode_usb(1), extended the lens and went on reporting playback. A
half-press of the shutter completed the switch without taking a picture, so
the backend sends one before it gives up on the body.

Version: 0.1.0-beta
"""

import logging

import pytest

import capture.backends.chdk_backend as cb
from capture.camera import CameraConfig

from .chdk_fakes import Body, TransportError, make_backend, make_pychdk, viewport_frame


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"
JPEG = b"\xff\xd8\xff\xe0 page \xff\xd9"
HALF_PRESS = 'click("shoot_half")'
ENTER_ALT = "enter_alt()"
OPERATOR_HINT = "take the battery out and put it back"


def _config(index=0):
    return CameraConfig(camera_index=index)


def _backend(monkeypatch, body):
    fake = make_pychdk(body)
    backend = make_backend(monkeypatch, fake)
    backend.list_devices()
    return backend, fake


@pytest.mark.unit
def test_a_switch_that_arrives_at_once_sends_no_half_press(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend, fake = _backend(monkeypatch, body)

    backend.capture_image(tmp_path / "page.jpg", _config())

    assert body.half_presses == 0
    assert HALF_PRESS not in body.lua_calls
    assert fake.sleeps == []
    assert len(body.shots) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "knob", ["stuck_in_play", "switch_without_arriving"],
    ids=["pychdk-raises", "pychdk-returns-silently"],
)
def test_a_half_press_completes_a_switch_that_did_not_arrive(
    monkeypatch, tmp_path, caplog, knob
):
    """Both ways a switch can fail to arrive take the half-press path.

    pychdk 0.1.3 raises its "did not switch" error; 0.1.2 returned with the
    camera still in playback. Either way one half-press is sent, get_mode is
    polled, and the capture goes ahead once it reports record.
    """
    body = Body(
        serial="AAA111", card=EVEN_CARD, image=JPEG,
        half_press_arrives=True, half_press_reads=1, **{knob: True},
    )
    backend, fake = _backend(monkeypatch, body)
    caplog.set_level(logging.INFO, logger="test-chdk")

    backend.capture_image(tmp_path / "page.jpg", _config())

    assert body.half_presses == 1
    assert backend._body_at(0).in_record_mode is True
    assert len(body.shots) == 1, "the capture did not go ahead after the switch"
    assert fake.sleeps, "get_mode was polled without waiting between reads"
    assert all(s == cb._RECORD_MODE_POLL_INTERVAL_S for s in fake.sleeps)
    half_press_lines = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "half-press" in r.getMessage()
    ]
    assert len(half_press_lines) == 1
    assert "usb:001,004" in half_press_lines[0].getMessage()


@pytest.mark.unit
def test_a_half_press_completes_the_switch_for_a_live_view(monkeypatch):
    body = Body(
        serial="AAA111", card=EVEN_CARD, frame=viewport_frame(),
        stuck_in_play=True, half_press_arrives=True,
    )
    backend, _ = _backend(monkeypatch, body)

    backend.capture_preview(0)

    assert body.half_presses == 1
    assert body.frames_served == 1


@pytest.mark.unit
def test_a_body_still_in_playback_after_the_wait_fails_with_advice(
    monkeypatch, tmp_path
):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, stuck_in_play=True)
    backend, fake = _backend(monkeypatch, body)
    held = backend._body_at(0)

    with pytest.raises(RuntimeError) as exc:
        backend._ensure_record_mode(held)

    message = str(exc.value)
    assert "usb:001,004" in message
    assert "shooting mode" in message
    assert OPERATOR_HINT in message
    assert body.half_presses == 1
    assert held.in_record_mode is False
    # The whole wait was spent, through the module's sleep and nothing else.
    assert sum(fake.sleeps) == pytest.approx(cb._RECORD_MODE_HALF_PRESS_WAIT_S)

    # A later call tries the switch again rather than remembering a result.
    with pytest.raises(RuntimeError):
        backend._ensure_record_mode(held)
    assert body.mode_switches == ["record", "record"]
    assert body.half_presses == 2


@pytest.mark.unit
def test_the_advice_reaches_a_capture_and_a_live_view(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG,
                frame=viewport_frame(), stuck_in_play=True)
    backend, _ = _backend(monkeypatch, body)

    with pytest.raises(RuntimeError) as exc:
        backend.capture_image(tmp_path / "page.jpg", _config())
    assert OPERATOR_HINT in str(exc.value)
    assert body.shots == []

    assert backend.is_camera_connected(0) is True
    with pytest.raises(RuntimeError) as exc:
        backend.capture_preview(0)
    assert OPERATOR_HINT in str(exc.value)
    assert body.frames_served == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [
        TransportError("[Errno 19] No such device"),
        RuntimeError("Script did not start: busy"),
        TimeoutError("Script still running after 5s"),
    ],
    ids=["transport", "other-runtime-error", "timeout"],
)
def test_other_failures_of_the_switch_propagate_without_a_half_press(
    monkeypatch, error
):
    body = Body(serial="AAA111", card=EVEN_CARD, mode_error=error)
    backend, fake = _backend(monkeypatch, body)
    held = backend._body_at(0)

    with pytest.raises(type(error)) as exc:
        backend._ensure_record_mode(held)

    assert exc.value is error
    assert body.half_presses == 0
    assert body.lua_calls == [ENTER_ALT]
    assert fake.sleeps == []
    assert held.in_record_mode is False


@pytest.mark.unit
def test_a_body_already_in_record_is_sent_nothing(monkeypatch):
    body = Body(serial="AAA111", card=EVEN_CARD)
    backend, fake = _backend(monkeypatch, body)
    held = backend._body_at(0)
    held.in_record_mode = True

    backend._ensure_record_mode(held)

    assert body.mode_switches == []
    assert body.lua_calls == []
    assert fake.sleeps == []


@pytest.mark.unit
def test_alt_mode_is_entered_before_the_switch_and_only_until_record(
    monkeypatch,
):
    body = Body(serial="AAA111", card=EVEN_CARD)
    backend, _ = _backend(monkeypatch, body)
    held = backend._body_at(0)

    backend._ensure_record_mode(held)

    assert body.events[:2] == [("lua", ENTER_ALT), ("switch", "record")]
    assert held.in_record_mode is True

    before = list(body.events)
    backend._ensure_record_mode(held)
    assert body.events == before


@pytest.mark.unit
def test_alt_mode_is_entered_again_after_the_body_is_dropped_and_reopened(
    monkeypatch,
):
    body = Body(serial="AAA111", card=EVEN_CARD)
    backend, _ = _backend(monkeypatch, body)
    backend._ensure_record_mode(backend._body_at(0))
    assert body.lua_calls.count(ENTER_ALT) == 1

    backend._evict(backend._body_at(0), "test")
    backend.list_devices()
    reopened = backend._body_at(0)
    assert reopened is not None and reopened.in_record_mode is False
    body.mode_value = False
    backend._ensure_record_mode(reopened)

    assert body.lua_calls.count(ENTER_ALT) == 2
    assert body.mode_switches == ["record", "record"]


@pytest.mark.unit
def test_a_failed_enter_alt_propagates_and_no_switch_is_attempted(monkeypatch):
    error = TimeoutError("Script did not complete within 10.0s")
    body = Body(serial="AAA111", card=EVEN_CARD, lua_error=error)
    backend, fake = _backend(monkeypatch, body)
    held = backend._body_at(0)

    with pytest.raises(TimeoutError) as exc:
        backend._ensure_record_mode(held)

    assert exc.value is error
    assert body.lua_calls == [ENTER_ALT]
    assert body.mode_switches == []
    assert body.half_presses == 0
    assert held.in_record_mode is False
