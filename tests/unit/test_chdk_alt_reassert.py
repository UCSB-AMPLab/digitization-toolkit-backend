"""ALT mode is asserted again during a session, not once per connection.

On 12 November 2026, with Canon's "Auto Power Down" on, two A2500 bodies
freshly put into ALT mode by enter_alt() stayed on through seven idle minutes
and then captured normally. After an operator went through the camera menus,
which takes a body out of ALT mode, one body switched itself off about three
minutes after the last capture and the other stayed attached to USB with its
lens out and answered nothing until its battery was taken out. So a capture
sends enter_alt() every time, and a preview sends it at most every
_ALT_REASSERT_INTERVAL_S seconds.

Version: 0.1.0-beta
"""

import pytest

import capture.backends.chdk_backend as cb
from capture.camera import CameraConfig

from .chdk_fakes import Body, make_backend, make_pychdk, viewport_frame


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"
JPEG = b"\xff\xd8\xff\xe0 a page \xff\xd9"
ENTER_ALT = "enter_alt()"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _config():
    return CameraConfig(camera_index=0)


def _backend(monkeypatch, *bodies):
    fake = make_pychdk(*bodies)
    backend = make_backend(monkeypatch, fake)
    clock = Clock()
    monkeypatch.setattr(cb, "_monotonic", clock)
    backend.list_devices()
    return backend, fake, clock


def _alts(body):
    return body.lua_calls.count(ENTER_ALT)


@pytest.mark.unit
def test_enter_alt_is_sent_before_each_of_two_captures(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend, _, _ = _backend(monkeypatch, body)

    backend.capture_image(tmp_path / "one.jpg", _config())
    backend.capture_image(tmp_path / "two.jpg", _config())

    assert _alts(body) == 2
    # The second capture asserts ALT mode before its shot.
    second_alt = [i for i, e in enumerate(body.events) if e == ("lua", ENTER_ALT)][1]
    assert second_alt < len(body.events)
    assert len(body.shots) == 2


@pytest.mark.unit
def test_the_capture_that_switches_to_record_sends_it_once(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend, _, _ = _backend(monkeypatch, body)

    backend.capture_image(tmp_path / "one.jpg", _config())

    assert _alts(body) == 1
    assert body.events[0] == ("lua", ENTER_ALT)
    assert body.events[1] == ("switch", "record")


@pytest.mark.unit
def test_previews_within_the_interval_send_none_and_the_next_one_sends_one(
    monkeypatch,
):
    body = Body(serial="AAA111", card=EVEN_CARD, frame=viewport_frame())
    backend, _, clock = _backend(monkeypatch, body)

    backend.capture_preview(0)
    assert _alts(body) == 1, "the switch asserted it"
    clock.now += cb._ALT_REASSERT_INTERVAL_S - 1
    backend.capture_preview(0)
    assert _alts(body) == 1
    clock.now += 2
    backend.capture_preview(0)
    assert _alts(body) == 2
    backend.capture_preview(0)
    assert _alts(body) == 2


@pytest.mark.unit
def test_a_capture_resets_the_preview_timer(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, frame=viewport_frame())
    backend, _, clock = _backend(monkeypatch, body)
    backend.capture_preview(0)
    clock.now += cb._ALT_REASSERT_INTERVAL_S - 1
    backend.capture_image(tmp_path / "one.jpg", _config())
    assert _alts(body) == 2

    clock.now += cb._ALT_REASSERT_INTERVAL_S - 1
    backend.capture_preview(0)

    assert _alts(body) == 2, "the capture counted as an assertion"


@pytest.mark.unit
def test_the_timer_is_reset_when_the_body_is_reopened(monkeypatch):
    body = Body(serial="AAA111", card=EVEN_CARD, frame=viewport_frame())
    backend, _, clock = _backend(monkeypatch, body)
    backend.capture_preview(0)
    backend._evict(backend._body_at(0), "test")
    backend.list_devices()
    body.mode_value = False

    backend.capture_preview(0)

    assert _alts(body) == 2


@pytest.mark.unit
def test_a_reassertion_that_fails_fails_the_capture_with_no_shot(
    monkeypatch, tmp_path
):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend, _, _ = _backend(monkeypatch, body)
    backend.capture_image(tmp_path / "one.jpg", _config())
    body.lua_error = RuntimeError("Script did not start: busy")

    with pytest.raises(RuntimeError) as exc:
        backend.capture_image(tmp_path / "two.jpg", _config())

    assert len(body.shots) == 1
    assert "Script did not start" in str(exc.value)
    assert body.closes == 1, "the body was dropped like any failed conversation"


@pytest.mark.unit
def test_a_reassertion_that_raises_enodev_goes_through_the_gone_body_retry(
    monkeypatch, tmp_path
):
    old = Body(address=16, serial="AAA111", card=EVEN_CARD, image=JPEG)
    new = Body(address=17, serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend, fake, _ = _backend(monkeypatch, old)
    backend.capture_image(tmp_path / "one.jpg", _config())
    old.lua_error = OSError(19, "No such device")
    fake.bodies = [new]

    backend.capture_image(tmp_path / "two.jpg", _config())

    assert len(old.shots) == 1
    assert len(new.shots) == 1
    assert (tmp_path / "two.jpg").exists()


@pytest.mark.unit
def test_a_failed_preview_reassertion_fails_the_preview(monkeypatch):
    body = Body(serial="AAA111", card=EVEN_CARD, frame=viewport_frame())
    backend, _, clock = _backend(monkeypatch, body)
    backend.capture_preview(0)
    clock.now += cb._ALT_REASSERT_INTERVAL_S + 1
    body.lua_error = TimeoutError("Script did not complete within 10.0s")

    with pytest.raises(RuntimeError):
        backend.capture_preview(0)

    assert body.frames_served == 1
