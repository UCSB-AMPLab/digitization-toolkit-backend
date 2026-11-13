"""Focus lock on CHDK bodies: autofocus once, lock it, restore it on reopening.

A Canon PowerShot A2500 has no AF/MF switch, so it autofocuses on every
picture, and on a blank page it focuses on the scanner's glass. Spreads, the
earlier scanner software (spreadsplug/dev/chdkcamera.py, _set_focus), locked
the focus with one script, a half-press held for a second and then
set_aflock(1), and set a fixed distance with a second sequence followed by
set_focus(distance). It unlocked with set_aflock(0).

Measured on 12 November 2026 on an A2500 under CHDK 1.5.1: set_aflock,
get_focus and get_focus_mode exist as Lua functions, and get_focus() answered
1489 and 1557, a distance in mm. Not measured: whether the lock holds across
remote captures, and whether set_focus moves the lens on this model. So the
backend asks the camera for its focus after every lock and restore, and
reports what it answered.

On the same day a second script sent on top of a stuck one took a camera off
USB, so the backend sends one script at a time under the body's lock and
sends nothing more to a body in the same operation after a timeout or an
error.

Version: 0.1.0-beta
"""

import logging

import pytest

import capture.backends.chdk_backend as cb
import capture.service as capture_service
from capture.backends.errors import CaptureTimeoutError
from capture.camera import CameraConfig
from capture.camera_registry import CameraRegistry

from .chdk_fakes import Body, make_backend, make_pychdk


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"
JPEG = b"\xff\xd8\xff\xe0 page \xff\xd9"
HW_ID = "canon325b_AAA111"
GET_ZOOM = "return get_zoom()"
GET_FOCUS = "return get_focus()"
LOCK = "press('shoot_half'); sleep(1000); release('shoot_half'); set_aflock(1);"
RESTORE = (
    "press('shoot_half'); sleep(1000); click('left'); set_aflock(1); "
    "release('shoot_half');"
)
UNLOCK = "set_aflock(0)"


def _config(index=0):
    return CameraConfig(camera_index=index)


def _registry(tmp_path):
    return CameraRegistry(registry_path=tmp_path / "cameras.json")


def _store(registry, hardware_id, **values):
    """Write stored values into the registry the way a hand-edited file would."""
    registry.register_resolved(hardware_id, {"model": "A2500"}, 0)

    def change(cameras):
        cameras["cameras"][hardware_id].update(values)

    registry._mutate(change)


def _stored(tmp_path):
    return _registry(tmp_path).get_camera_by_id(HW_ID)


def _backend(monkeypatch, body, registry=None):
    fake = make_pychdk(body)
    backend = make_backend(monkeypatch, fake, registry=registry)
    backend.list_devices()
    return backend, fake


def _focus_calls(body):
    """Every script that touches the focus or the lock."""
    return [
        c for c in body.lua_calls
        if "focus" in c or "aflock" in c or "shoot_half')" in c
    ]


def _after(body, script):
    """The Lua calls made after the last call of `script`."""
    last = max(i for i, c in enumerate(body.lua_calls) if c == script)
    return body.lua_calls[last + 1:]


def _spy_timeouts(monkeypatch, backend):
    """Record the timeout the backend gives each Lua script it sends.

    The backend sends Lua itself, through _send_lua, so the timeout is the
    backend's own wait and never reaches the fake device.
    """
    sent = {}
    real = backend._send_lua

    def send(body, code, timeout=cb._LUA_TIMEOUT_S):
        sent[code] = timeout
        return real(body, code, timeout)

    monkeypatch.setattr(backend, "_send_lua", send)
    return sent


# --- locking on request ----------------------------------------------------


@pytest.mark.unit
def test_lock_focus_locks_reads_the_distance_and_stores_it(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64, af_distance=1489)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    timeouts = _spy_timeouts(monkeypatch, backend)

    assert backend.lock_focus(0) == 1489

    assert body.lua_calls[-2:] == [LOCK, GET_FOCUS]
    assert timeouts[LOCK] == cb._FOCUS_SCRIPT_TIMEOUT_S == 15.0
    assert timeouts[GET_FOCUS] == cb._LUA_TIMEOUT_S
    # Sent by the backend itself, with NOKILL, so the strict fake saw them.
    sent = {s["script"]: s["flags"] for s in body.scripts}
    assert sent[LOCK] == cb._SCRIPT_NOKILL
    assert sent[GET_FOCUS] == cb._SCRIPT_NOKILL
    assert body.aflock is True
    held = backend._body_at(0)
    assert held.focus_locked is True
    assert held.focus_distance == 1489
    assert held.focus_stale is False
    assert held.focus_restore_failed is False
    stored = _stored(tmp_path)
    assert stored is not None, "an unregistered body was not registered"
    assert stored["focus_distance"] == 1489
    assert "focus_distance_updated_at" in stored


@pytest.mark.unit
def test_the_lock_script_follows_record_mode_and_one_enter_alt(monkeypatch, tmp_path):
    """A body entering record mode in this call has had enter_alt() sent."""
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))

    backend.lock_focus(0)

    assert body.lua_calls.count("enter_alt()") == 1
    assert body.lua_calls.index(LOCK) > body.lua_calls.index("return get_mode()")
    assert body.lua_calls.index(LOCK) > body.lua_calls.index(GET_ZOOM)


@pytest.mark.unit
def test_alt_mode_is_reasserted_before_the_lock_script(monkeypatch, tmp_path):
    """A body already in record mode gets enter_alt() as a capture does."""
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    backend.capture_image(tmp_path / "one.jpg", _config())
    sent_before = len(body.lua_calls)

    backend.lock_focus(0)

    assert body.lua_calls[sent_before:] == ["enter_alt()", LOCK, GET_FOCUS]


@pytest.mark.unit
def test_get_focus_waits_for_the_lock_script_to_end(monkeypatch, tmp_path):
    """The lock script holds a half-press for a second on the camera.

    The strict fake raises ScriptSentWhileRunning if get_focus() arrives
    while the lock script is still running; the backend waits for the lock
    script to end before it sends anything more.
    """
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64,
                script_reads={"press('shoot_half')": 14})
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))

    assert backend.lock_focus(0) == 1489

    assert body.lua_calls[-2:] == [LOCK, GET_FOCUS]
    lock, read = body.scripts[-2:]
    assert lock["script"] == LOCK and read["script"] == GET_FOCUS
    assert read["while_running"] is False


@pytest.mark.unit
def test_get_focus_waits_for_a_lock_script_that_runs_on_after_returning(
    monkeypatch, tmp_path
):
    """A script still running after the host had its answer is waited for."""
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64,
                script_reads_after_return={"press('shoot_half')": 6})
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))

    assert backend.lock_focus(0) == 1489

    assert body.scripts[-1]["script"] == GET_FOCUS
    assert body.scripts[-1]["while_running"] is False


@pytest.mark.unit
def test_a_script_that_never_ends_before_the_lock_sends_no_lock(
    monkeypatch, tmp_path
):
    """A script left running past the wait fails the lock as a timeout does."""
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    backend.capture_image(tmp_path / "one.jpg", _config())
    sent_before = len(body.lua_calls)
    body._start_running("<stuck>", 10_000)

    with pytest.raises(cb.FocusFailedError) as exc:
        backend.lock_focus(0)

    assert "usb:001,004" in str(exc.value)
    assert body.lua_calls[sent_before:] == [], "a script was sent over a running one"
    assert body.closes == 1
    assert _stored(tmp_path) is None


@pytest.mark.unit
@pytest.mark.parametrize("answer", [None, 0, -5, True, 1489.0, "1489"])
def test_a_lock_with_no_positive_distance_fails_and_sends_nothing_further(
    monkeypatch, tmp_path, answer
):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64, focus_answer=answer)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))

    with pytest.raises(cb.FocusFailedError) as exc:
        backend.lock_focus(0)

    assert "usb:001,004" in str(exc.value)
    assert body.lua_calls[-1] == GET_FOCUS, "something was sent after get_focus"
    held = backend._body_at(0)
    assert held.focus_locked is not True
    assert held.focus_distance is None
    assert _stored(tmp_path) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [TimeoutError("Script did not complete within 15.0s"),
     RuntimeError("Script did not start: busy")],
    ids=["timeout", "error"],
)
def test_a_lock_script_that_fails_sends_nothing_further_and_drops_the_body(
    monkeypatch, tmp_path, error
):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64, focus_error=error)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))

    with pytest.raises(cb.FocusFailedError):
        backend.lock_focus(0)

    assert body.lua_calls[-1] == LOCK, "something was sent after the lock script"
    assert body.closes == 1, "the body was not dropped"
    assert backend._body_at(0) is None
    assert _stored(tmp_path) is None


@pytest.mark.unit
def test_a_failed_switch_before_the_lock_sends_no_lock(monkeypatch, tmp_path):
    body = Body(
        serial="AAA111", card=EVEN_CARD, zoom_steps=64,
        mode_error=TimeoutError("Script still running after 5s"),
    )
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))

    with pytest.raises(cb.FocusFailedError):
        backend.lock_focus(0)

    assert body.lua_calls == ["enter_alt()"]
    assert body.closes == 1


@pytest.mark.unit
def test_a_provisional_body_may_be_locked_but_nothing_is_stored(
    monkeypatch, tmp_path
):
    registry = _registry(tmp_path)
    body = Body(serial=None, card=b"EVEN\n", zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, registry)
    assert backend._body_at(0).hardware_id is None

    assert backend.lock_focus(0) == 1489

    assert backend._body_at(0).focus_locked is True
    assert _registry(tmp_path).list_cameras() == []


@pytest.mark.unit
def test_an_ambiguous_body_may_be_locked_but_nothing_is_stored(
    monkeypatch, tmp_path
):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    backend._body_at(0).identity_clash = "another body answers to this id"

    assert backend.lock_focus(0) == 1489

    assert _stored(tmp_path) is None


@pytest.mark.unit
def test_a_body_that_left_the_bus_during_the_lock_is_retried_once(
    monkeypatch, tmp_path
):
    """A camera restarted with its cable in comes back at a new address."""
    old = Body(address=16, serial="AAA111", card=EVEN_CARD, zoom_steps=64,
               focus_error=OSError(19, "No such device"))
    new = Body(address=17, serial="AAA111", card=EVEN_CARD, zoom_steps=64,
               af_distance=1557)
    fake = make_pychdk(old)
    backend = make_backend(monkeypatch, fake, registry=_registry(tmp_path))
    backend.list_devices()
    fake.bodies = [new]

    assert backend.lock_focus(0) == 1557

    assert old.lua_calls[-1] == LOCK, "the old body was sent more"
    assert new.aflock is True
    assert _stored(tmp_path)["focus_distance"] == 1557


@pytest.mark.unit
def test_a_different_body_at_the_index_is_not_locked(monkeypatch, tmp_path):
    old = Body(address=16, serial="AAA111", card=EVEN_CARD, zoom_steps=64,
               focus_error=OSError(19, "No such device"))
    other = Body(address=17, serial="BBB222", card=EVEN_CARD, zoom_steps=64)
    fake = make_pychdk(old)
    backend = make_backend(monkeypatch, fake, registry=_registry(tmp_path))
    backend.list_devices()
    fake.bodies = [other]

    with pytest.raises(cb.FocusFailedError) as exc:
        backend.lock_focus(0)

    assert "usb:001,016" in str(exc.value)
    assert LOCK not in other.lua_calls
    assert _registry(tmp_path).list_cameras() == []


# --- unlocking ---------------------------------------------------------------


@pytest.mark.unit
def test_unlock_focus_sends_set_aflock_0_and_clears_the_stored_distance(
    monkeypatch, tmp_path
):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    backend.lock_focus(0)

    backend.unlock_focus(0)

    assert body.lua_calls[-1] == UNLOCK
    assert body.aflock is False
    held = backend._body_at(0)
    assert held.focus_locked is False
    assert held.focus_distance is None
    assert held.focus_stale is False
    assert "focus_distance" not in _stored(tmp_path)


@pytest.mark.unit
def test_unlock_focus_on_a_body_with_nothing_stored_stores_nothing(
    monkeypatch, tmp_path
):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))

    backend.unlock_focus(0)

    assert body.lua_calls[-1] == UNLOCK
    assert backend._body_at(0).focus_locked is False
    assert _stored(tmp_path) is None


@pytest.mark.unit
def test_an_unlock_that_fails_drops_the_body_and_keeps_the_stored_distance(
    monkeypatch, tmp_path
):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    backend.lock_focus(0)
    body.aflock_error = TimeoutError("Script did not complete within 10.0s")

    with pytest.raises(cb.FocusFailedError):
        backend.unlock_focus(0)

    assert body.lua_calls[-1] == UNLOCK
    assert body.closes == 1
    assert _stored(tmp_path)["focus_distance"] == 1489


# --- zoom after a lock ---------------------------------------------------------


@pytest.mark.unit
def test_a_zoom_move_after_a_lock_unlocks_and_marks_the_focus_stale(
    monkeypatch, tmp_path
):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64, zoom=0)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    backend.lock_focus(0)

    assert backend.set_zoom_step(0, 30) == 30

    assert _after(body, "set_zoom(30)") == [GET_ZOOM, UNLOCK]
    assert body.aflock is False
    held = backend._body_at(0)
    assert held.focus_locked is False
    assert held.focus_stale is True
    assert held.focus_distance is None
    stored = _stored(tmp_path)
    assert stored["zoom_step"] == 30
    assert "focus_distance" not in stored


@pytest.mark.unit
def test_a_zoom_request_to_the_current_step_leaves_the_lock_alone(
    monkeypatch, tmp_path
):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64, zoom=12)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    backend.lock_focus(0)

    assert backend.set_zoom_step(0, 12) == 12

    assert UNLOCK not in body.lua_calls
    assert body.aflock is True
    held = backend._body_at(0)
    assert held.focus_locked is True
    assert held.focus_stale is False
    assert _stored(tmp_path)["focus_distance"] == 1489


@pytest.mark.unit
def test_a_zoom_move_with_no_lock_sends_no_unlock(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64, zoom=0)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))

    backend.set_zoom_step(0, 30)

    assert _focus_calls(body) == []
    assert backend._body_at(0).focus_stale is False


@pytest.mark.unit
def test_an_unlock_after_a_zoom_move_that_fails_fails_the_zoom(
    monkeypatch, tmp_path
):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64, zoom=0)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    backend.lock_focus(0)
    body.aflock_error = RuntimeError("Script did not start: busy")

    with pytest.raises(cb.ZoomFailedError):
        backend.set_zoom_step(0, 30)

    assert body.lua_calls[-1] == UNLOCK
    assert body.closes == 1
    stored = _stored(tmp_path)
    assert "zoom_step" not in stored
    assert stored["focus_distance"] == 1489


# --- restoring on entering record mode -------------------------------------


@pytest.mark.unit
def test_no_stored_distance_sends_no_focus_script(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))

    backend.capture_image(tmp_path / "one.jpg", _config())

    assert _focus_calls(body) == []
    assert backend._body_at(0).focus_locked is False


@pytest.mark.unit
def test_a_stored_distance_is_restored_on_entering_record_mode(
    monkeypatch, tmp_path
):
    registry = _registry(tmp_path)
    _store(registry, HW_ID, focus_distance=1500)
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64,
                af_distance=900)
    backend, fake = _backend(monkeypatch, body, registry)
    timeouts = _spy_timeouts(monkeypatch, backend)

    backend.capture_image(tmp_path / "one.jpg", _config())

    assert _focus_calls(body) == [RESTORE, "set_focus(1500)", GET_FOCUS]
    assert _after(body, GET_ZOOM)[:3] == [RESTORE, "set_focus(1500)", GET_FOCUS]
    assert timeouts[RESTORE] == 15.0
    assert timeouts["set_focus(1500)"] == 15.0
    assert cb._FOCUS_RESTORE_PAUSE_S in fake.sleeps
    held = backend._body_at(0)
    assert held.focus_locked is True
    assert held.focus_distance == 1500
    assert held.focus_restore_failed is False
    assert len(body.shots) == 1

    backend.capture_image(tmp_path / "two.jpg", _config())
    assert _focus_calls(body) == [RESTORE, "set_focus(1500)", GET_FOCUS], (
        "the focus was restored again"
    )


@pytest.mark.unit
def test_a_restore_within_the_tolerance_counts_as_locked(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    _store(registry, HW_ID, focus_distance=1500)
    # 1450 is 3.3% short of 1500.
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64,
                af_distance=1450, focus_follows=False)
    backend, _ = _backend(monkeypatch, body, registry)

    backend.capture_image(tmp_path / "one.jpg", _config())

    assert cb._FOCUS_RESTORE_TOLERANCE == 0.05
    held = backend._body_at(0)
    assert held.focus_locked is True
    assert held.focus_distance == 1450
    assert UNLOCK not in body.lua_calls


@pytest.mark.unit
@pytest.mark.parametrize(
    "knobs, reported",
    [({"af_distance": 900, "focus_follows": False}, "900"),
     ({"focus_answer": None}, "None"),
     ({"focus_answer": 0}, "0")],
    ids=["outside", "none", "zero"],
)
def test_a_restore_that_misses_warns_unlocks_and_the_capture_goes_on(
    monkeypatch, tmp_path, caplog, knobs, reported
):
    registry = _registry(tmp_path)
    _store(registry, HW_ID, focus_distance=1500)
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64, **knobs)
    backend, _ = _backend(monkeypatch, body, registry)
    caplog.set_level(logging.WARNING, logger="test-chdk")

    backend.capture_image(tmp_path / "one.jpg", _config())

    assert _focus_calls(body) == [RESTORE, "set_focus(1500)", GET_FOCUS, UNLOCK]
    assert body.aflock is False
    assert len(body.shots) == 1
    assert body.closes == 0, "the body was dropped"
    held = backend._body_at(0)
    assert held.focus_locked is False
    assert held.focus_restore_failed is True
    assert held.focus_distance is None
    warnings = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert any(
        "usb:001,004" in m and "1500" in m and reported in m for m in warnings
    ), warnings
    assert _stored(tmp_path)["focus_distance"] == 1500, "the stored distance was lost"


@pytest.mark.unit
@pytest.mark.parametrize(
    "knobs, last",
    [({"focus_error": TimeoutError("Script did not complete within 15.0s")},
      RESTORE),
     ({"set_focus_error": TimeoutError("Script did not complete within 15.0s")},
      "set_focus(1500)")],
    ids=["restore-script", "set-focus"],
)
def test_a_restore_that_times_out_fails_the_capture_and_sends_nothing_more(
    monkeypatch, tmp_path, knobs, last
):
    registry = _registry(tmp_path)
    _store(registry, HW_ID, focus_distance=1500)
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64, **knobs)
    backend, _ = _backend(monkeypatch, body, registry)

    with pytest.raises(CaptureTimeoutError):
        backend.capture_image(tmp_path / "one.jpg", _config())

    assert body.lua_calls[-1] == last
    assert body.shots == []
    assert body.closes == 1


@pytest.mark.unit
@pytest.mark.parametrize("stored", [0, -10, True, "1500", 1500.0])
def test_a_stored_distance_that_is_not_a_positive_integer_is_ignored(
    monkeypatch, tmp_path, caplog, stored
):
    registry = _registry(tmp_path)
    _store(registry, HW_ID, focus_distance=stored)
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, registry)
    caplog.set_level(logging.WARNING, logger="test-chdk")

    backend.capture_image(tmp_path / "one.jpg", _config())

    assert _focus_calls(body) == []
    assert len(body.shots) == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("focus distance" in r.getMessage() for r in warnings)


@pytest.mark.unit
def test_an_ambiguous_body_does_not_read_a_stored_distance(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    _store(registry, HW_ID, focus_distance=1500)
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, registry)
    held = backend._body_at(0)
    held.identity_clash = "another body answers to this id"

    backend._ensure_record_mode(held)

    assert _focus_calls(body) == []


@pytest.mark.unit
def test_a_lock_is_restored_after_the_body_is_opened_again(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64,
                af_distance=1489)
    backend, _ = _backend(monkeypatch, body, registry)
    backend.lock_focus(0)

    backend._evict(backend._body_at(0), "test")
    body.mode_value = False
    body.aflock = False
    body.af_distance = 700
    backend.list_devices()
    assert backend._body_at(0).focus_locked is None
    backend.capture_image(tmp_path / "one.jpg", _config())

    assert _after(body, LOCK).count(RESTORE) == 1
    assert _after(body, RESTORE)[:2] == ["set_focus(1489)", GET_FOCUS]
    assert body.focus == 1489
    assert body.aflock is True
    assert backend._body_at(0).focus_locked is True


@pytest.mark.unit
def test_a_zoom_move_after_a_failed_restore_clears_the_stored_distance(
    monkeypatch, tmp_path
):
    """The stored distance belonged to the old step; it is not restored again."""
    registry = _registry(tmp_path)
    _store(registry, HW_ID, focus_distance=1500)
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64,
                af_distance=900, focus_follows=False)
    backend, _ = _backend(monkeypatch, body, registry)
    backend.capture_image(tmp_path / "one.jpg", _config())
    assert backend._body_at(0).focus_restore_failed is True
    unlocks = body.lua_calls.count(UNLOCK)

    backend.set_zoom_step(0, 30)

    assert body.lua_calls.count(UNLOCK) == unlocks, "the lock was dropped twice"
    held = backend._body_at(0)
    assert held.focus_stale is True
    assert "focus_distance" not in _stored(tmp_path)


# --- what the backend reports ---------------------------------------------


@pytest.mark.unit
def test_device_rows_carry_the_focus_fields(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))

    row = backend.list_devices()[0]
    assert row["focus_locked"] is None
    assert row["focus_distance"] is None
    assert row["focus_stale"] is False
    assert row["focus_restore_failed"] is False

    backend.lock_focus(0)
    row = backend.list_devices()[0]
    assert row["focus_locked"] is True
    assert row["focus_distance"] == 1489

    backend.set_zoom_step(0, 20)
    row = backend.list_devices()[0]
    assert row["focus_locked"] is False
    assert row["focus_distance"] is None
    assert row["focus_stale"] is True


@pytest.mark.unit
def test_only_the_chdk_backend_reports_focus_lock(monkeypatch):
    from capture.backends.base import CameraBackend
    from capture.backends.gphoto2_backend import GPhoto2Backend
    from capture.backends.subprocess_backend import RpicamBackend

    backend, _ = _backend(monkeypatch, Body(serial="AAA111", card=EVEN_CARD))
    assert backend.get_capabilities()["focus_lock"] is True

    for cls in (GPhoto2Backend, RpicamBackend):
        assert cls.get_capabilities(None)["focus_lock"] is False
    assert CameraBackend.get_capabilities(None)["focus_lock"] is False


@pytest.mark.unit
def test_the_picamera2_backend_reports_no_focus_lock():
    from capture.backends import picamera2_backend

    assert picamera2_backend.Picamera2Backend.get_capabilities(None)["focus_lock"] is False


# --- the registry ----------------------------------------------------------


@pytest.mark.unit
def test_the_registry_stores_and_clears_one_distance_per_hardware_id(tmp_path):
    registry = _registry(tmp_path)
    registry.register_resolved(HW_ID, {"model": "A2500"}, 0)

    registry.update_focus_distance(HW_ID, 1489)
    stored = _stored(tmp_path)
    assert stored["focus_distance"] == 1489
    assert "focus_distance_updated_at" in stored

    registry.update_focus_distance(HW_ID, None)
    stored = _stored(tmp_path)
    assert "focus_distance" not in stored
    assert "focus_distance_updated_at" in stored


@pytest.mark.unit
@pytest.mark.parametrize("distance", [0, -1, True, 2.5, "4"])
def test_the_registry_refuses_a_distance_that_is_not_a_positive_int(
    tmp_path, distance
):
    registry = _registry(tmp_path)
    registry.register_resolved(HW_ID, {"model": "A2500"}, 0)

    with pytest.raises(ValueError):
        registry.update_focus_distance(HW_ID, distance)


@pytest.mark.unit
def test_the_registry_refuses_a_distance_for_an_unknown_body(tmp_path):
    with pytest.raises(KeyError):
        _registry(tmp_path).update_focus_distance(HW_ID, 1489)


# --- the routes ----------------------------------------------------------------


def _user(username, role):
    from app.models.user import User

    return User(
        username=username, email=f"{username}@example.com",
        hashed_password="x", role=role, is_active=True,
    )


def _client_as(client, username, role):
    from app.main import app
    from app.api.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


def _install(monkeypatch, backend):
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)


@pytest.mark.unit
def test_the_lock_route_locks_and_reports_the_distance(client, monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    _install(monkeypatch, backend)
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/focus-lock/0")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"detail": "Focus locked", "focus_distance": 1489}
    assert _stored(tmp_path)["focus_distance"] == 1489


@pytest.mark.unit
def test_the_unlock_route_unlocks(client, monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    backend.lock_focus(0)
    _install(monkeypatch, backend)
    api = _client_as(client, "op", "operator")

    resp = api.delete("/cameras/focus-lock/0")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"detail": "Focus unlocked"}
    assert body.aflock is False
    assert "focus_distance" not in _stored(tmp_path)


@pytest.mark.unit
@pytest.mark.parametrize("method", ["post", "delete"])
def test_the_routes_answer_501_for_a_backend_without_focus_lock(
    client, monkeypatch, method
):
    class _Other:
        def get_backend_name(self):
            return "gphoto2"

        def get_capabilities(self):
            return {"focus_lock": False}

        def is_camera_connected(self, camera_index=0):
            return True

    _install(monkeypatch, _Other())
    api = _client_as(client, "op", "operator")

    resp = getattr(api, method)("/cameras/focus-lock/0")

    assert resp.status_code == 501, resp.text
    assert "gphoto2" in resp.json()["detail"]


@pytest.mark.unit
@pytest.mark.parametrize("method", ["post", "delete"])
def test_the_routes_answer_404_for_an_index_with_no_camera(
    client, monkeypatch, tmp_path, method
):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    _install(monkeypatch, backend)
    api = _client_as(client, "op", "operator")

    resp = getattr(api, method)("/cameras/focus-lock/1")

    assert resp.status_code == 404, resp.text
    assert "not connected" in resp.json()["detail"]
    assert body.lua_calls == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "knobs",
    [{"focus_error": TimeoutError("Script did not complete within 15.0s")},
     {"focus_error": RuntimeError("Script did not start: busy")},
     {"focus_answer": None}],
    ids=["timeout", "wire-error", "no-distance"],
)
def test_the_lock_route_answers_502_for_a_camera_failure(
    client, monkeypatch, tmp_path, knobs
):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64, **knobs)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    _install(monkeypatch, backend)
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/focus-lock/0")

    assert resp.status_code == 502, resp.text
    assert "usb:001,004" in resp.json()["detail"]


@pytest.mark.unit
def test_the_unlock_route_answers_502_for_a_camera_failure(
    client, monkeypatch, tmp_path
):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64,
                aflock_error=TimeoutError("Script did not complete within 10.0s"))
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    _install(monkeypatch, backend)
    api = _client_as(client, "op", "operator")

    resp = api.delete("/cameras/focus-lock/0")

    assert resp.status_code == 502, resp.text


@pytest.mark.unit
@pytest.mark.parametrize("method", ["post", "delete"])
def test_a_reviewer_may_not_lock_or_unlock(client, monkeypatch, tmp_path, method):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    _install(monkeypatch, backend)
    api = _client_as(client, "rev", "reviewer")

    resp = getattr(api, method)("/cameras/focus-lock/0")

    assert resp.status_code == 403, resp.text
    assert body.lua_calls == []


@pytest.mark.unit
def test_the_device_list_carries_the_focus_fields(client, monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    backend.lock_focus(0)
    _install(monkeypatch, backend)
    api = _client_as(client, "op", "operator")

    resp = api.get("/cameras/devices")

    assert resp.status_code == 200, resp.text
    row = resp.json()[0]
    assert row["focus_locked"] is True
    assert row["focus_distance"] == 1489
    assert row["focus_stale"] is False
    assert row["focus_restore_failed"] is False


@pytest.mark.unit
def test_the_capabilities_route_reports_focus_lock(client, monkeypatch):
    backend, _ = _backend(monkeypatch, Body(serial="AAA111", card=EVEN_CARD))
    _install(monkeypatch, backend)
    api = _client_as(client, "op", "operator")

    resp = api.get("/cameras/capabilities")

    assert resp.status_code == 200, resp.text
    assert resp.json()["focus_lock"] is True
