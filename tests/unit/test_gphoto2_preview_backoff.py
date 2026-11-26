"""Preview throttling leaves reset-capable recovery and fast retries available."""
import logging
import threading

import pytest

import capture.backends.gphoto2_backend as gb
from tests.unit.test_gphoto2_recovery import rig, recovery_state, _session
from tests.unit.test_gphoto2_capture_timeout import MODEL, PORT, _config

OTHER = "usb:003,002"


def _slow_failure(rig, seconds=1.1):
    rig.wedged[PORT] = -10
    rig.init_hook = lambda port: rig.clock.advance(seconds)


def test_r2_preview_backoff_does_not_delay_the_next_capture_reset(rig, tmp_path):
    _slow_failure(rig)
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._refresh_port_map()
    for _ in range(5):
        with pytest.raises(Exception):
            backend.capture_preview(0)
    assert rig.inits[PORT] == 1 and not rig.resets
    result, _ = backend.capture_image(tmp_path / "page.jpg", _config())
    assert result and len(rig.resets) == 1 and rig.inits[PORT] == 3
    assert rig.clock.now < 1006
    backend._close_session(0)


@pytest.mark.parametrize("duration", [0, .07, 1.0])
def test_r1_fast_failure_has_no_backoff_and_the_next_poll_calls_init(rig, duration):
    _slow_failure(rig, duration)
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._refresh_port_map()
    for _ in range(2):
        with pytest.raises(rig.gp.GPhoto2Error):
            backend.capture_preview(0)
    assert rig.inits[PORT] == 2 and not gb._init_backoff


def test_r3_second_reset_is_reachable_at_its_cooldown_even_with_new_backoff(rig, monkeypatch):
    _slow_failure(rig, 1.1)
    rig.reset_error = OSError("reset failed before retry")
    release = gb._INIT_LOCK.release
    first = [True]

    def delayed_release():
        if first[0]:
            first[0] = False
            rig.clock.advance(2)
        release()

    monkeypatch.setattr(gb._INIT_LOCK, "release", delayed_release)
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._refresh_port_map()
    backend.list_devices()
    assert len(rig.resets) == 1
    first_reset = gb._recent_resets["1-1"]
    # Preview back-off expires before the reset cooldown in this schedule.
    rig.clock.advance(first_reset + 18 - rig.clock.now)
    with pytest.raises(rig.gp.GPhoto2Error):
        backend.capture_preview(0)
    assert rig.inits[PORT] == 2 and len(rig.resets) == 1
    # That preview's slow failure re-arms back-off past the reset's due time.
    rig.clock.advance(first_reset + 20 - rig.clock.now)
    backend.list_devices()
    assert len(rig.resets) == 2
    assert rig.clock.now - first_reset < 22


@pytest.mark.parametrize("holder_index,expected", [(0, "Camera 0 (left)"), (7, "the other camera")])
def test_r4_timeout_names_the_holder_as_the_interface_does(rig, monkeypatch, holder_index, expected):
    rig.ports[OTHER] = ("3-1", 2, 456)
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._port_map = {holder_index: (MODEL, PORT), 1: (MODEL, OTHER)}
    backend._serial_by_port = {PORT: "A", OTHER: "B"}
    backend._roles_by_port[OTHER] = (gb._hardware_id(MODEL, "B"), "right")
    if holder_index == 0:
        backend._pins[holder_index] = "A"
        backend._roles_by_port[PORT] = (gb._hardware_id(MODEL, "A"), "left")
    entered, release = threading.Event(), threading.Event()
    errors = []

    def native(port):
        if port == PORT:
            entered.set()
            assert release.wait(2)

    def holder():
        try:
            cam, _ = backend._brief_camera(holder_index, MODEL, PORT)
            cam.exit()
        except BaseException as exc:
            errors.append(exc)
        finally:
            backend._release_port(holder_index, PORT)

    rig.init_hook = native
    monkeypatch.setattr(gb, "_INIT_LOCK_WAIT_S", .01)
    worker = threading.Thread(target=holder, daemon=True)
    worker.start()
    try:
        assert entered.wait(1)
        with pytest.raises(RuntimeError) as caught:
            backend._brief_camera(1, MODEL, OTHER)
        message = str(caught.value)
        assert f"waiting for {expected}" in message, message
        assert f"Switch {expected} off and on" in message, message
        assert "Camera 1 (right)" in message and "usb:" not in message
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors and not backend._claimed_ports


def test_r4_no_holder_has_a_complete_generic_message(rig, monkeypatch):
    monkeypatch.setattr(gb._INIT_LOCK, "acquire_for_port", lambda *a: (False, None))
    with pytest.raises(RuntimeError) as caught:
        gb._init_camera(MODEL, PORT, rig.attachment(PORT), False)
    assert str(caught.value) == "The cameras are busy starting. Wait and try again."


@pytest.mark.parametrize("index,expected", [(0, "Camera 0 (left)"), (7, "Camera 7")])
def test_r4_preview_pause_names_the_camera_without_a_usb_address(rig, index, expected):
    _slow_failure(rig)
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._port_map = {index: (MODEL, PORT)}
    backend._serial_by_port[PORT] = "A"
    if index == 0:
        backend._roles_by_port[PORT] = (gb._hardware_id(MODEL, "A"), "left")
    with pytest.raises(rig.gp.GPhoto2Error):
        backend._open_claimed(index, MODEL, PORT, False, lambda port:
            gb._init_camera(MODEL, port, rig.attachment(port), False))
    with pytest.raises(RuntimeError) as caught:
        backend._open_claimed(index, MODEL, PORT, False, lambda port:
            gb._init_camera(MODEL, port, rig.attachment(port), False))
    assert expected in str(caught.value) and "usb:" not in str(caught.value)


def test_r5_failed_init_and_entering_backoff_reach_the_journal(rig, caplog, monkeypatch):
    _slow_failure(rig)
    monkeypatch.setattr(gb.time, "monotonic", rig.clock)
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._port_map = {0: (MODEL, PORT)}
    backend._serial_by_port[PORT] = "A"
    backend._roles_by_port[PORT] = (gb._hardware_id(MODEL, "A"), "left")
    with caplog.at_level(logging.INFO, logger=gb.__name__):
        with pytest.raises(rig.gp.GPhoto2Error):
            backend._open_claimed(0, MODEL, PORT, False, lambda port:
                gb._init_camera(MODEL, port, rig.attachment(port), False))
    lines = [r.message for r in caplog.records if r.name == gb.__name__]
    failed = [s for s in lines if "reason=camera init" in s and "outcome=failed" in s]
    paused = [s for s in lines if "preview init back-off" in s]
    assert len(failed) == 1 and "error=" in failed[0] and "-10" in failed[0], lines
    assert len(paused) == 1, lines
    assert "Camera 0 (left)" in paused[0] and "held=1.100s" in paused[0] and "interval=20s" in paused[0]
    assert paused[0].endswith(f"({PORT})")


def test_r5_failed_drain_includes_the_exception_text(monkeypatch, caplog):
    backend, _, gp, _ = _session(monkeypatch)
    monkeypatch.setattr(gp.Camera, "wait_for_event", lambda *a: (_ for _ in ()).throw(gp.GPhoto2Error("[-7] event read")))
    with caplog.at_level(logging.INFO, logger=gb.__name__):
        with pytest.raises(RuntimeError):
            backend._get_or_open_session(0)
    lines = [r.message for r in caplog.records if r.name == gb.__name__ and "reason=startup events" in r.message]
    assert len(lines) == 1 and "error=" in lines[0] and "[-7] event read" in lines[0], lines


@pytest.mark.parametrize("duration,expected", [(.07, 0), (1.0, 0), (1.1, 1)])
def test_r5_slow_success_is_visible_and_fast_success_is_quiet(rig, caplog, monkeypatch, duration, expected):
    rig.init_hook = lambda port: rig.clock.advance(duration)
    monkeypatch.setattr(gb.time, "monotonic", rig.clock)
    with caplog.at_level(logging.INFO, logger=gb.__name__):
        gb._init_camera(MODEL, PORT, rig.attachment(PORT), False).exit()
    lines = [r.message for r in caplog.records if r.name == gb.__name__ and "reason=camera init" in r.message]
    assert len(lines) == expected, lines
    if lines:
        assert "outcome=ok" in lines[0] and "held_s=1.1" in lines[0]


@pytest.mark.parametrize("operation", ["claim", "finish"])
def test_r4_label_preparation_failure_releases_native_resources(rig, monkeypatch, operation):
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    monkeypatch.setattr(backend, "_open_labels", lambda *a: (_ for _ in ()).throw(ValueError("label preparation")))
    cam = None
    if operation == "finish":
        cam = gb._new_camera(MODEL, PORT)
        cam.init()
    with pytest.raises(ValueError, match="label preparation"):
        if operation == "claim":
            backend._brief_camera(0, MODEL, PORT)
        else:
            backend._finish_identification(cam, PORT, 0)
    assert not backend._claimed_ports
    assert rig.script.exit_calls == (1 if operation == "finish" else 0)


@pytest.mark.parametrize("role,known,expected", [("right", True, "Camera 0 (right)"),
                                               ("left", False, "Camera 0")])
def test_r4_labels_use_known_roles_not_index_guesses(rig, role, known, expected):
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._port_map = {0: (MODEL, PORT)}
    if known:
        backend._serial_by_port[PORT] = "A"
    backend._roles_by_port[PORT] = (gb._hardware_id(MODEL, "A"), role)
    labels = backend._open_labels(0, PORT)
    assert labels[PORT] == expected
    assert all(isinstance(label, str) for label in labels.values())


@pytest.mark.parametrize("failed", [False, True])
def test_r4_role_diagnostics_use_memory_without_role_reads(rig, failed):
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._port_map = {0: (MODEL, PORT)}
    backend._serial_by_port[PORT] = "A"
    reads_while_held = []

    def role(key):
        reads_while_held.append(gb._INIT_LOCK.locked())
        return "left"

    backend._role_reader = role
    backend._roles_by_port[PORT] = (gb._hardware_id(MODEL, "A"), "left")
    rig.init_hook = lambda port: rig.clock.advance(1.1)
    if failed:
        rig.wedged[PORT] = -10
    try:
        if failed:
            with pytest.raises(rig.gp.GPhoto2Error):
                backend._open_claimed(0, MODEL, PORT, False, lambda port:
                    gb._init_camera(MODEL, port, rig.attachment(port), False))
        else:
            cam, _ = backend._open_claimed(0, MODEL, PORT, False, lambda port:
                gb._init_camera(MODEL, port, rig.attachment(port), False))
            cam.exit()
    finally:
        backend._release_port(0, PORT)
    assert not reads_while_held, reads_while_held


@pytest.mark.parametrize("error_type", [ValueError, KeyboardInterrupt])
def test_r5_slow_success_log_failure_closes_the_initialized_camera(rig, monkeypatch, error_type):
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._port_map = {0: (MODEL, PORT)}
    rig.init_hook = lambda port: rig.clock.advance(1.1)
    error = error_type("init diagnostic interrupted")
    original = gb._WAIT_LOG.info

    def info(message, *args, **kwargs):
        if "camera init" in args:
            raise error
        return original(message, *args, **kwargs)

    monkeypatch.setattr(gb._WAIT_LOG, "info", info)
    with pytest.raises(error_type) as caught:
        backend._brief_camera(0, MODEL, PORT)
    assert caught.value is error
    assert rig.script.exit_calls == 1
    assert not backend._claimed_ports and not gb._INIT_LOCK.locked()
