"""The camera's clock is set from the unit's clock, once per opened body.

A CHDK camera writes its own clock into each picture's EXIF. On 12 November
2026 two Canon PowerShot A2500 bodies under CHDK 1.5.1 were about two minutes
behind the unit, and a body whose clock battery has drained starts with no
date. After record mode is confirmed and the zoom is settled, the backend
reads the camera's clock with one script and compares it with the unit's
local time (the camera keeps local wall-clock time, with no zone). A
difference above _CLOCK_TOLERANCE_S is corrected with set_clock, and the
clock is read back once. A camera that answers oddly never fails a capture;
a wire failure or a timeout fails the operation as it does for the zoom.

Version: 0.1.0-beta
"""

import datetime
import logging

import pytest

import capture.backends.chdk_backend as cb
from capture.backends.errors import CaptureTimeoutError
from capture.camera import CameraConfig

from .chdk_fakes import Body, make_backend, make_pychdk


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"
JPEG = b"\xff\xd8\xff\xe0 page \xff\xd9"
UNIT = datetime.datetime(2026, 11, 12, 15, 30, 0)


def _config(index=0):
    return CameraConfig(camera_index=index)


def _backend(monkeypatch, body):
    fake = make_pychdk(body)
    backend = make_backend(monkeypatch, fake)
    backend.list_devices()
    return backend, fake


def _body(**kwargs):
    return Body(serial="AAA111", card=EVEN_CARD, image=JPEG, **kwargs)


def _clock_warnings(caplog):
    return [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "clock" in r.getMessage()
    ]


def _reads(body):
    return [c for c in body.lua_calls if "os.date" in c]


def _sets(body):
    return [c for c in body.lua_calls if c.startswith("set_clock(")]


@pytest.mark.unit
def test_a_clock_within_tolerance_is_read_once_and_not_set(monkeypatch, tmp_path):
    body = _body(clock_offset_s=4)
    backend, _ = _backend(monkeypatch, body)

    backend.capture_image(tmp_path / "one.jpg", CameraConfig(camera_index=0))
    backend.capture_image(tmp_path / "two.jpg", CameraConfig(camera_index=0))

    assert len(_reads(body)) == 1
    assert _sets(body) == []
    assert backend._body_at(0).clock_offset_s == 4


@pytest.mark.unit
def test_the_clock_is_read_after_record_mode_and_zoom(monkeypatch, tmp_path):
    body = _body(zoom_steps=64)
    backend, _ = _backend(monkeypatch, body)

    backend.capture_image(tmp_path / "one.jpg", _config())

    calls = body.lua_calls
    read = next(i for i, c in enumerate(calls) if "os.date" in c)
    assert read > calls.index("return get_mode()")
    assert read > calls.index("return get_zoom()")


@pytest.mark.unit
def test_a_clock_two_minutes_behind_is_set_and_read_back(
    monkeypatch, tmp_path, caplog
):
    body = _body(clock_offset_s=-120)
    backend, fake = _backend(monkeypatch, body)

    with caplog.at_level(logging.INFO):
        backend.capture_image(tmp_path / "one.jpg", _config())

    assert body.set_clocks == [(2026, 11, 12, 15, 30, 0)]
    assert len(_reads(body)) == 2
    # The order is read, set, read back.
    kinds = [
        "read" if "os.date" in c else "set"
        for c in body.lua_calls
        if "os.date" in c or c.startswith("set_clock(")
    ]
    assert kinds == ["read", "set", "read"]
    assert backend._body_at(0).clock_offset_s == pytest.approx(0, abs=1)
    text = caplog.text
    assert "usb:001,004" in text
    assert "2026-11-12 15:28:00" in text
    assert "-120" in text
    assert _clock_warnings(caplog) == []


@pytest.mark.unit
def test_a_camera_ahead_of_the_unit_is_set_back(monkeypatch, tmp_path):
    body = _body(clock_offset_s=3600)
    backend, _ = _backend(monkeypatch, body)

    backend.capture_image(tmp_path / "one.jpg", _config())

    assert len(body.set_clocks) == 1
    assert body.clock_offset_s == 0


@pytest.mark.unit
def test_a_camera_that_ignores_set_clock_is_warned_about_and_capture_goes_on(
    monkeypatch, tmp_path, caplog
):
    body = _body(clock_offset_s=-120, clock_ignores_set=True)
    backend, _ = _backend(monkeypatch, body)

    with caplog.at_level(logging.INFO):
        backend.capture_image(tmp_path / "one.jpg", _config())
        backend.capture_image(tmp_path / "two.jpg", _config())

    assert len(body.shots) == 2
    assert len(body.set_clocks) == 1, "set_clock is not retried"
    assert len(_reads(body)) == 2
    warnings = _clock_warnings(caplog)
    assert len(warnings) == 1
    assert "2026-11-12 15:28:00" in warnings[0].getMessage()
    assert "2026-11-12 15:30:00" in warnings[0].getMessage()
    assert backend._body_at(0).clock_offset_s == -120


@pytest.mark.unit
def test_an_answer_that_is_no_date_is_warned_about_and_nothing_is_set(
    monkeypatch, tmp_path, caplog
):
    body = _body(clock_garbage=True)
    backend, _ = _backend(monkeypatch, body)

    with caplog.at_level(logging.INFO):
        backend.capture_image(tmp_path / "one.jpg", _config())

    assert len(body.shots) == 1
    assert _sets(body) == []
    assert len(_reads(body)) == 1
    warnings = _clock_warnings(caplog)
    assert len(warnings) == 1
    assert "not a date" in warnings[0].getMessage()
    assert backend._body_at(0).clock_offset_s is None


@pytest.mark.unit
@pytest.mark.parametrize("answer", [None, "", 5, "2026-13-45 99:99:99"])
def test_other_odd_answers_are_not_parsed_and_do_not_fail(
    monkeypatch, tmp_path, answer
):
    body = _body()
    backend, _ = _backend(monkeypatch, body)
    real = body._answer

    def answer_oddly(code):
        return answer if "os.date" in code else real(code)

    body._answer = answer_oddly

    backend.capture_image(tmp_path / "one.jpg", _config())

    assert _sets(body) == []
    assert len(body.shots) == 1


@pytest.mark.unit
def test_a_timeout_on_the_read_fails_the_operation_and_sends_nothing_more(
    monkeypatch, tmp_path
):
    body = _body(clock_error=TimeoutError("Script did not complete within 10.0s"))
    backend, _ = _backend(monkeypatch, body)

    with pytest.raises(CaptureTimeoutError):
        backend.capture_image(tmp_path / "one.jpg", _config())

    assert "os.date" in body.lua_calls[-1]
    assert _sets(body) == []
    assert body.shots == []
    assert body.closes == 1
    assert backend._body_at(0) is None


@pytest.mark.unit
def test_a_timeout_on_set_clock_fails_the_operation_and_sends_nothing_more(
    monkeypatch, tmp_path
):
    body = _body(
        clock_offset_s=-120,
        set_clock_error=TimeoutError("Script did not complete within 10.0s"),
    )
    backend, _ = _backend(monkeypatch, body)

    with pytest.raises(CaptureTimeoutError):
        backend.capture_image(tmp_path / "one.jpg", _config())

    assert body.lua_calls[-1].startswith("set_clock(")
    assert body.shots == []
    assert body.closes == 1


@pytest.mark.unit
def test_the_clock_is_synchronised_once_per_opened_body(monkeypatch, tmp_path):
    body = _body(clock_offset_s=-120)
    backend, _ = _backend(monkeypatch, body)
    backend.capture_image(tmp_path / "one.jpg", _config())
    backend.capture_image(tmp_path / "two.jpg", _config())
    assert len(body.set_clocks) == 1
    assert len(_reads(body)) == 2

    backend._evict(backend._body_at(0), "test")
    body.mode_value = False
    body.clock_offset_s = -120
    backend.list_devices()
    backend.capture_image(tmp_path / "three.jpg", _config())

    assert len(body.set_clocks) == 2
    assert len(_reads(body)) == 4


@pytest.mark.unit
def test_both_scripts_go_through_send_lua(monkeypatch, tmp_path):
    body = _body(clock_offset_s=-120)
    backend, _ = _backend(monkeypatch, body)
    sent = []
    real = backend._send_lua

    def spy(held, code, timeout=cb._LUA_TIMEOUT_S):
        sent.append(code)
        return real(held, code, timeout)

    monkeypatch.setattr(backend, "_send_lua", spy)

    backend.capture_image(tmp_path / "one.jpg", _config())

    assert len([c for c in sent if "os.date" in c]) == 2
    assert len([c for c in sent if c.startswith("set_clock(")]) == 1
    # No script reached the camera over a running one (the strict fake
    # would have raised), and none was sent around _send_lua.
    assert all(not s["while_running"] for s in body.scripts)


@pytest.mark.unit
def test_a_script_still_running_is_waited_for_before_the_clock_is_read(
    monkeypatch, tmp_path
):
    body = _body(script_reads={"return get_zoom_steps()": 3})
    backend, _ = _backend(monkeypatch, body)

    backend.capture_image(tmp_path / "one.jpg", _config())

    assert len(_reads(body)) == 1


@pytest.mark.unit
def test_the_set_time_is_the_units_time_taken_just_before_sending(
    monkeypatch, tmp_path
):
    body = _body(clock_offset_s=-120)
    backend, fake = _backend(monkeypatch, body)
    times = iter([
        datetime.datetime(2026, 11, 12, 15, 30, 0),   # after the first answer
        datetime.datetime(2026, 11, 12, 15, 30, 1),   # just before set_clock
        datetime.datetime(2026, 11, 12, 15, 30, 2),   # after the read-back
    ])
    monkeypatch.setattr(cb, "_now", lambda: next(times))

    backend.capture_image(tmp_path / "one.jpg", _config())

    assert body.set_clocks == [(2026, 11, 12, 15, 30, 1)]


@pytest.mark.unit
def test_the_device_row_carries_the_offset_at_the_end(monkeypatch, tmp_path):
    body = _body(clock_offset_s=-120)
    backend, _ = _backend(monkeypatch, body)

    before = backend.list_devices()[0]
    assert before["clock_offset_s"] is None

    backend.capture_image(tmp_path / "one.jpg", _config())
    row = backend.list_devices()[0]

    assert row["clock_offset_s"] == pytest.approx(0, abs=1)
    assert list(row)[-1] == "clock_offset_s"
