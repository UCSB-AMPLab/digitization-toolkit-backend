"""Startup files stay session-local; failed captures and queued opens stay safe."""
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import capture.backends.gphoto2_backend as gb
from tests.unit.test_gphoto2_recovery import rig, recovery_state, _session
from tests.unit.test_gphoto2_capture_timeout import (
    _file_path, _config, PORT, MODEL, GP_EVENT_FILE_ADDED, GP_EVENT_TIMEOUT,
    GP_EVENT_UNKNOWN,
)


@pytest.mark.parametrize("focus_check", [False, True])
def test_startup_file_is_only_warned_and_remembered(monkeypatch, tmp_path, caplog, recovery_state, focus_check):
    old = _file_path(name="STARTUP.JPG")
    backend, script, gp, _ = _session(monkeypatch, before=[
        (GP_EVENT_FILE_ADDED, old, 0), (GP_EVENT_FILE_ADDED, old, 0)])

    def forbidden(*args):
        raise AssertionError("startup file was downloaded or deleted")

    monkeypatch.setattr(gp.Camera, "file_get", forbidden)
    monkeypatch.setattr(gp.Camera, "file_delete", forbidden)
    with caplog.at_level(logging.WARNING):
        session = backend._get_or_open_session(0)
    assert session._unsolicited == {(old.folder, old.name)}
    warnings = [r.message for r in caplog.records if r.name == gb.__name__]
    assert len(warnings) == 1
    assert PORT in warnings[0] and old.folder in warnings[0] and old.name in warnings[0]
    assert not list(tmp_path.iterdir())
    monkeypatch.setattr(session._cam, "wait_for_event", lambda ms: (GP_EVENT_FILE_ADDED, old))
    with pytest.raises(RuntimeError, match="Take the page again.*switch the camera off and on"):
        backend.capture_image(tmp_path / "page.jpg", _config(), focus_check=focus_check)
    assert 0 not in backend._sessions and not backend._claimed_ports
    assert not script.saved_to and not script.deleted


def test_failed_session_does_not_blacklist_the_next_session(monkeypatch, tmp_path, recovery_state):
    path = _file_path()
    backend, script, gp, clock = _session(monkeypatch, before=[(GP_EVENT_FILE_ADDED, path, 0)])
    session = backend._get_or_open_session(0)
    assert (path.folder, path.name) in session._unsolicited
    backend._close_session(0)
    # A new Camera owns its own exposure; no previous session's path survives.
    def event(cam, ms):
        clock.advance(ms / 1000)
        return ((GP_EVENT_FILE_ADDED, path) if getattr(cam, "shot", False)
                else (GP_EVENT_TIMEOUT, None))

    monkeypatch.setattr(gp.Camera, "wait_for_event", event)
    original = gp.Camera.trigger_capture

    def trigger(cam):
        original(cam)
        cam.shot = True

    monkeypatch.setattr(gp.Camera, "trigger_capture", trigger)
    result, _ = backend.capture_image(tmp_path / "page.jpg", _config())
    assert Path(result).exists()
    assert script.deleted == [(path.folder, path.name)]
    backend._close_session(0)


_STEPS = ["trigger", "event", "candidate", "download", "save", "fsync", "delete", "raw_preview", "complete_log", "format_check"]


@pytest.mark.parametrize("step", _STEPS)
@pytest.mark.parametrize("error_type", [ValueError, OSError, KeyboardInterrupt])
def test_r1_every_post_trigger_exception_evicts_the_session(monkeypatch, tmp_path, recovery_state, step, error_type):
    path = _file_path(name="IMAGE.CR2" if step == "raw_preview" else "IMAGE.JPG")
    backend, script, gp, _ = _session(monkeypatch, after=[(GP_EVENT_FILE_ADDED, path, 0)])
    session = backend._get_or_open_session(0)
    error = error_type(f"injected at {step}")

    def fail(*args, **kwargs):
        raise error

    if step == "trigger":
        original = gp.Camera.trigger_capture

        def trigger(cam):
            original(cam)
            raise error

        monkeypatch.setattr(gp.Camera, "trigger_capture", trigger)
    elif step == "event":
        monkeypatch.setattr(session._cam, "wait_for_event", fail)
    elif step == "candidate":
        monkeypatch.setattr(session, "_capture_file", fail)
    elif step == "download":
        monkeypatch.setattr(session._cam, "file_get", fail)
    elif step == "save":
        monkeypatch.setattr(session._cam, "file_get", lambda *a: SimpleNamespace(save=fail))
    elif step == "fsync":
        import capture.utils as utils
        monkeypatch.setattr(utils, "_fsync_file", fail)
    elif step == "delete":
        monkeypatch.setattr(session._cam, "file_delete", fail)
    elif step == "raw_preview":
        monkeypatch.setattr(gb, "_write_raw_preview", fail)
    elif step == "complete_log":
        original = backend.logger.info

        def info(message, *args, **kwargs):
            if "captured page.jpg" in message:
                raise error
            original(message, *args, **kwargs)

        monkeypatch.setattr(backend.logger, "info", info)
    else:
        monkeypatch.setattr(backend, "_check_produced_format", fail)
    with pytest.raises(error_type) as found:
        backend.capture_image(tmp_path / "page.jpg", _config())
    assert found.value is error
    assert session._cam is None, step
    assert 0 not in backend._sessions, step
    assert not backend._claimed_ports, step
    assert script.exit_calls == 1, step


def test_r1_direct_session_capture_closes_on_arbitrary_exception(monkeypatch, tmp_path, recovery_state):
    backend, script, _, _ = _session(monkeypatch)
    session = backend._get_or_open_session(0)
    error = ValueError("event decoder")
    monkeypatch.setattr(session._cam, "wait_for_event", lambda *a: (_ for _ in ()).throw(error))
    with pytest.raises(ValueError) as found:
        session.capture(tmp_path / "page.jpg")
    assert found.value is error and session._cam is None
    backend._close_session(0)


@pytest.mark.parametrize("ending", ["time", "events", "read"])
def test_r2_failed_startup_explains_power_cycle_and_can_retry(monkeypatch, tmp_path, recovery_state, ending):
    before = [(GP_EVENT_UNKNOWN, None, 6)] if ending == "time" else [(GP_EVENT_UNKNOWN, None, 0)] * 3
    backend, script, gp, _ = _session(monkeypatch, before=before)
    if ending == "events":
        monkeypatch.setattr(gb, "_OPEN_DRAIN_EVENTS", 2)
    original = gp.Camera.wait_for_event
    if ending == "read":
        monkeypatch.setattr(gp.Camera, "wait_for_event", lambda *a: (_ for _ in ()).throw(gp.GPhoto2Error("[-7] read")))
    with pytest.raises(RuntimeError, match="Switch the camera off and on"):
        backend.capture_image(tmp_path / "failed.jpg", _config())
    assert script.trigger_calls == 0 and not backend._claimed_ports
    monkeypatch.setattr(gp.Camera, "wait_for_event", lambda cam, ms: (
        original(cam, ms) if script.trigger_calls else (GP_EVENT_TIMEOUT, None)))
    result, _ = backend.capture_image(tmp_path / "retry.jpg", _config())
    assert Path(result).exists() and script.trigger_calls == 1
    backend._close_session(0)


def test_r3_queued_body_is_not_barged_by_the_previous_owner(rig, monkeypatch):
    other = "usb:003,002"
    rig.ports[other] = ("3-1", 2, 456)
    queued, native, release_native = threading.Event(), threading.Event(), threading.Event()
    errors = []
    original_wait = gb._INIT_LOCK._condition.wait

    def wait(timeout=None):
        queued.set()
        return original_wait(timeout)

    monkeypatch.setattr(gb._INIT_LOCK._condition, "wait", wait)

    def on_init(port):
        native.set()
        if not release_native.wait(2):
            raise AssertionError("native init was not released")

    rig.init_hook = on_init

    def opener():
        try:
            cam = gb._init_camera(MODEL, other, rig.attachment(other), True)
            cam.exit()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=opener, daemon=True)
    held = gb._INIT_LOCK.acquire()
    try:
        worker.start()
        assert queued.wait(1)
        gb._INIT_LOCK.release()
        held = False
        barged = gb._INIT_LOCK.acquire(blocking=False)
        if barged:
            gb._INIT_LOCK.release()
        assert not barged, "the previous owner barged ahead of the queued healthy body"
        assert native.wait(1)
    finally:
        if held:
            gb._INIT_LOCK.release()
        release_native.set()
        worker.join(3)
    assert not worker.is_alive() and not errors


def test_r3_lock_timeout_has_unit_actions(rig, monkeypatch):
    monkeypatch.setattr(gb, "_INIT_LOCK_WAIT_S", .01, raising=False)
    with gb._INIT_LOCK:
        with pytest.raises(RuntimeError, match="The cameras are busy starting. Wait and try again."):
            gb._init_camera(MODEL, PORT, rig.attachment(PORT), True)


def test_r4_identification_drains_once_after_its_read(rig, monkeypatch):
    order = []
    original = rig.gp.Camera.get_config
    monkeypatch.setattr(rig.gp.Camera, "get_config", lambda cam: (order.append("read"), original(cam))[1])
    monkeypatch.setattr(rig.gp.Camera, "wait_for_event", lambda *a: (order.append("drain"), (GP_EVENT_TIMEOUT, None))[1])
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend.list_devices()
    assert order == ["read", "drain"]
    assert not backend._claimed_ports


def test_r6_routine_opens_and_empty_drains_are_quiet(monkeypatch, caplog, recovery_state):
    backend, _, gp, clock = _session(monkeypatch)
    original = gp.Camera.init
    monkeypatch.setattr(gp.Camera, "init", lambda cam: (clock.advance(.4), original(cam))[1])
    with caplog.at_level(logging.INFO):
        session = backend._get_or_open_session(0)
        with gb._wait_log(PORT, "startup events") as detail:
            clock.advance(.4)
            detail["count"] = 0
    lines = [r.message for r in caplog.records if r.name == gb.__name__ and r.levelno == logging.INFO]
    assert not lines, lines
    backend._close_session(0)


def test_r6_fast_nonfile_drain_is_quiet_and_failure_is_logged(monkeypatch, caplog, recovery_state):
    backend, _, _, _ = _session(monkeypatch, before=[(GP_EVENT_UNKNOWN, None, .001)])
    with caplog.at_level(logging.INFO):
        backend._get_or_open_session(0)
        try:
            with gb._wait_log(PORT, "startup events") as detail:
                detail["count"] = 0
                raise ValueError("read failed")
        except ValueError:
            pass
    lines = [r.message for r in caplog.records if r.name == gb.__name__ and r.levelno == logging.INFO]
    assert len(lines) == 1 and "reason=startup events" in lines[0]
    assert "count=0" in lines[0] and "outcome=failed" in lines[0]
    backend._close_session(0)


def test_r1_audit_cancellation_closes_before_propagating(monkeypatch, tmp_path, recovery_state):
    backend, script, _, _ = _session(monkeypatch)
    monkeypatch.setattr(backend, "_check_produced_format", lambda *a: {"detail": "mismatch"})
    error = KeyboardInterrupt("audit cancelled")
    monkeypatch.setattr(backend, "_system_log", lambda **kw: (_ for _ in ()).throw(error))
    with pytest.raises(KeyboardInterrupt) as found:
        backend.capture_image(tmp_path / "page.jpg", _config())
    assert found.value is error
    assert not backend._sessions and not backend._claimed_ports
    assert script.exit_calls == 1


def test_r3_missing_camera_support_asks_the_administrator(monkeypatch):
    monkeypatch.setattr(gb, "_GP_AVAILABLE", False)
    with pytest.raises(RuntimeError, match="Ask the administrator"):
        gb.GPhoto2Backend(logging.getLogger("test"))


def test_r1_retry_uses_a_new_session_and_its_own_file(monkeypatch, tmp_path, recovery_state):
    backend, script, gp, _ = _session(monkeypatch)
    original = gp.Camera.trigger_capture
    fail_once = [True]

    def trigger(cam):
        original(cam)
        if not hasattr(cam, "pending"):
            cam.pending = []
        cam.pending.append("STALE.JPG" if script.trigger_calls == 1 else "FRESH.JPG")

    def event(cam, ms):
        if not getattr(cam, "pending", []):
            return GP_EVENT_TIMEOUT, None
        if fail_once[0]:
            fail_once[0] = False
            raise ValueError("decoder failed after the shutter")
        return GP_EVENT_FILE_ADDED, _file_path(name=cam.pending.pop(0))

    monkeypatch.setattr(gp.Camera, "trigger_capture", trigger)
    monkeypatch.setattr(gp.Camera, "wait_for_event", event)
    monkeypatch.setattr(gp.Camera, "file_get", lambda cam, folder, name, typ: SimpleNamespace(
        save=lambda path: Path(path).write_bytes(name.encode())))
    with pytest.raises(ValueError):
        backend.capture_image(tmp_path / "failed.jpg", _config())
    result, _ = backend.capture_image(tmp_path / "retry.jpg", _config())
    assert Path(result).read_bytes() == b"FRESH.JPG"
    assert script.init_calls == 2 and script.exit_calls == 1
    assert [name for folder, name in script.deleted] == ["FRESH.JPG"]
    backend._close_session(0)


def test_r1_result_conversion_failure_evicts_the_session(monkeypatch, tmp_path, recovery_state):
    backend, script, _, _ = _session(monkeypatch)
    session = backend._get_or_open_session(0)
    original = session.capture
    error = ValueError("result path conversion")

    class ResultPath:
        name = "page.jpg"

        def __str__(self):
            raise error

    def capture(*args, **kwargs):
        elapsed, path = original(*args, **kwargs)
        return elapsed, ResultPath()

    monkeypatch.setattr(session, "capture", capture)
    with pytest.raises(ValueError) as found:
        backend.capture_image(tmp_path / "page.jpg", _config())
    assert found.value is error
    assert not backend._sessions and not backend._claimed_ports
    assert script.exit_calls == 1


def test_r3_timed_out_waiter_does_not_block_the_following_open(rig, monkeypatch):
    monkeypatch.setattr(gb, "_INIT_LOCK_WAIT_S", .01)
    with gb._INIT_LOCK:
        with pytest.raises(RuntimeError):
            gb._init_camera(MODEL, PORT, rig.attachment(PORT), True)
    camera = gb._init_camera(MODEL, PORT, rig.attachment(PORT), True)
    assert rig.inits[PORT] == 1
    camera.exit()
