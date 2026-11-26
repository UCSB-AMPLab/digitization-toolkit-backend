"""Failed sockets yield init time; routine camera operations stay journal-quiet."""
import logging
import threading

import pytest

import capture.backends.gphoto2_backend as gb
from tests.unit.test_gphoto2_recovery import rig, recovery_state, _session
from tests.unit.test_gphoto2_capture_timeout import MODEL, PORT, GP_EVENT_UNKNOWN

OTHER = "usb:003,002"


@pytest.fixture(autouse=True)
def backoff_state(monkeypatch):
    monkeypatch.setattr(gb, "_init_backoff", {}, raising=False)


def test_f1_preview_polls_back_off_only_the_failed_socket(rig):
    rig.ports[OTHER] = ("3-1", 2, 456)
    rig.wedged[PORT] = -10
    rig.init_hook = lambda port: rig.clock.advance(1.1)
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._refresh_port_map()
    for attempt in range(5):
        with pytest.raises(Exception) as caught:
            backend.capture_preview(0)
        if not attempt:
            assert "-10" in str(caught.value)
        else:
            assert str(caught.value).startswith("Camera 0: preview startup is paused")
            assert "other camera" not in str(caught.value)
    assert rig.inits[PORT] == 1
    assert not rig.resets and not backend._claimed_ports
    healthy = backend._get_or_open_session(1, may_scan=False)
    assert healthy.port == OTHER and rig.inits[OTHER] == 1
    backend._close_session(1)


@pytest.mark.parametrize("change", ["expiry", "address", "lifetime"])
def test_f1_backoff_clears_after_expiry_or_reenumeration(rig, change):
    rig.wedged[PORT] = -10
    rig.init_hook = lambda port: rig.clock.advance(1.1)
    with pytest.raises(rig.gp.GPhoto2Error):
        gb._init_camera(MODEL, PORT, rig.attachment(PORT), False)
    with pytest.raises(RuntimeError, match="preview startup is paused"):
        gb._init_camera(MODEL, PORT, rig.attachment(PORT), False)
    rig.wedged.clear()
    port = PORT
    if change == "expiry":
        rig.clock.advance(20)
    elif change == "address":
        port = "usb:001,009"
        rig.ports.pop(PORT)
        rig.ports[port] = ("1-1", 9, 124)
    else:
        rig.ports[PORT] = ("1-1", 5, 124)
    cam = gb._init_camera(MODEL, port, rig.attachment(port), False)
    cam.exit()
    assert sum(rig.inits.values()) == 2


def test_f1_reset_retry_is_exempt_and_success_clears_backoff(rig):
    rig.wedged[PORT] = -10
    rig.init_hook = lambda port: rig.clock.advance(1.1)
    cam = gb._init_camera(MODEL, PORT, rig.attachment(PORT), True)
    cam.exit()
    assert rig.inits[PORT] == 2 and len(rig.resets) == 1
    cam = gb._init_camera(MODEL, PORT, rig.attachment(PORT), False)
    cam.exit()
    assert rig.inits[PORT] == 3


def test_f1_unlabelled_holder_uses_the_busy_message(rig, monkeypatch):
    rig.ports[OTHER] = ("3-1", 2, 456)
    entered, release = threading.Event(), threading.Event()
    errors = []

    def native(port):
        if port == PORT:
            entered.set()
            assert release.wait(2), "holder was not released"

    def holder():
        try:
            gb._init_camera(MODEL, PORT, rig.attachment(PORT), False).exit()
        except BaseException as exc:
            errors.append(exc)

    rig.init_hook = native
    monkeypatch.setattr(gb, "_INIT_LOCK_WAIT_S", .01)
    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    try:
        assert entered.wait(1)
        with pytest.raises(RuntimeError) as caught:
            gb._init_camera(MODEL, OTHER, rig.attachment(OTHER), False)
        message = str(caught.value)
        assert message == "The cameras are busy starting. Wait and try again."
        assert rig.inits[OTHER] == 0
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and not errors


@pytest.mark.parametrize("reason", ["identification read", "identification close",
                                   "capture events", "capture events (focus check)", "trigger",
                                   "trigger (focus check)", "capture download/save", "captured file delete"])
def test_f2_routine_success_is_quiet_even_when_slow(monkeypatch, caplog, reason):
    backend, _, _, clock = _session(monkeypatch)
    with caplog.at_level(logging.INFO, logger=gb.__name__):
        with gb._wait_log(PORT, reason):
            clock.advance(10)
    assert not [r.message for r in caplog.records if r.name == gb.__name__], reason


def test_f2_normal_startup_backlog_is_quiet(monkeypatch, caplog):
    backend, _, _, _ = _session(monkeypatch, before=[(GP_EVENT_UNKNOWN, None, .003)] * 98)
    with caplog.at_level(logging.INFO, logger=gb.__name__):
        backend._get_or_open_session(0)
    assert not [r.message for r in caplog.records if r.name == gb.__name__ and r.levelno == logging.INFO]
    backend._close_session(0)


@pytest.mark.parametrize("mode", ["file", "slow", "failed"])
def test_f2_recovery_summaries_remain_visible(monkeypatch, caplog, mode):
    backend, _, _, clock = _session(monkeypatch)
    with caplog.at_level(logging.INFO, logger=gb.__name__):
        try:
            with gb._wait_log(PORT, "startup events") as details:
                details.update(count=0, files=mode == "file")
                if mode == "slow":
                    clock.advance(1.001)
                elif mode == "failed":
                    raise ValueError("event read")
        except ValueError:
            pass
    lines = [r.message for r in caplog.records if r.name == gb.__name__ and r.levelno == logging.INFO]
    assert len(lines) == 1, lines


@pytest.mark.parametrize("operation", ["identify", "list"])
def test_f3_claim_refusal_names_the_holder_without_claiming_a_read(rig, caplog, operation):
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._refresh_port_map()
    backend._claim_port(7, PORT)
    try:
        with caplog.at_level(logging.INFO):
            if operation == "identify":
                backend._identify_unknown_ports()
            else:
                assert backend.list_devices() == []
        lines = [r.message for r in caplog.records]
        assert any(PORT in line and "held by index 7" in line for line in lines), lines
        assert not any("no serial could be read" in line for line in lines), lines
        assert not rig.inits
    finally:
        backend._release_port(7, PORT)


@pytest.mark.parametrize("step", ["opened_log", "drain", "preset", "poweroff", "focus", "flash", "identity"])
def test_f4_any_open_exit_closes_and_releases_the_claim(monkeypatch, recovery_state, step):
    backend, script, gp, _ = _session(monkeypatch)
    error = KeyboardInterrupt(step)

    def fail(*args, **kwargs):
        raise error

    if step == "opened_log":
        original = backend.logger.info

        def info(message, *args, **kwargs):
            if "Opened session" in message:
                raise error
            return original(message, *args, **kwargs)

        monkeypatch.setattr(backend.logger, "info", info)
    else:
        method = dict(drain="_drain_startup", preset="_apply_speed_preset", poweroff="_disable_autopoweroff",
                      focus="_warn_if_af", flash="_enforce_flash_off", identity="_read_body_model").get(step)
        monkeypatch.setattr(gb._PTPSession if method else gp.Camera, method or "get_config", fail)
    with pytest.raises(KeyboardInterrupt) as caught:
        backend._get_or_open_session(0)
    assert caught.value is error
    assert script.exit_calls == 1, step
    assert not backend._sessions and not backend._claimed_ports


@pytest.mark.parametrize("duration,expected", [(4.9, 0), (10.0, 0), (10.001, 1)])
def test_f2_camera_lock_reports_only_unusual_contention(monkeypatch, caplog, duration, expected):
    _, _, _, clock = _session(monkeypatch)
    with caplog.at_level(logging.INFO, logger=gb.__name__):
        with gb._wait_log("camera 0", "camera lock"):
            clock.advance(duration)
    assert len([r for r in caplog.records if r.name == gb.__name__]) == expected
