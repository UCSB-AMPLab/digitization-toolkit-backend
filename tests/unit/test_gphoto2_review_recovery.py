"""Recovery retains capture ownership across late files, reopenings, and retries."""
import logging
import threading

import pytest

import capture.backends.gphoto2_backend as gb

from tests.unit.test_gphoto2_recovery import rig, recovery_state, _session
from tests.unit.test_gphoto2_capture_timeout import (
    MODEL, PORT,
    GP_EVENT_UNKNOWN,
)


@pytest.mark.parametrize("pinned", [False, True])
def test_b_claim_refusal_is_not_a_device_row(rig, pinned):
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._refresh_port_map()
    if pinned:
        backend._pins = {0: "A"}
        backend._serial_by_port = {PORT: "A"}
    backend._claim_port(7, PORT)
    try:
        assert backend.list_devices() == []
        assert backend._claimed_ports == {PORT: 7}
        assert not rig.inits
    finally:
        backend._release_port(7, PORT)


def test_c_missing_serial_refuses_then_recovers_with_the_same_pin(rig):
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend.list_devices()
    rig.script.settings.pop("serialnumber")
    with pytest.raises(gb.CameraIdentityError, match="Switch the camera off and on"):
        backend._get_or_open_session(0)
    assert backend._pins == {0: "A"}
    assert not backend._claimed_ports
    rig.script.settings["serialnumber"] = "A"
    backend.rescan()
    session = backend._get_or_open_session(0)
    assert session.serial == "A"
    backend._close_session(0)


def test_e_init_lock_wait_is_bounded(rig, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    errors = []

    def holder():
        with gb._INIT_LOCK:
            entered.set()
            if not release.wait(2):
                errors.append("holder not released")

    worker = threading.Thread(target=holder, daemon=True)
    monkeypatch.setattr(gb, "_INIT_LOCK_WAIT_S", .02, raising=False)
    worker.start()
    try:
        assert entered.wait(1)
        with pytest.raises(RuntimeError, match="Wait and try again"):
            gb._init_camera(MODEL, PORT, rig.attachment(PORT), True)
        assert not rig.inits and not rig.resets
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors


def test_f_fast_locks_and_config_are_quiet_but_recovery_is_logged(monkeypatch, caplog):
    backend, _, _, clock = _session(monkeypatch, before=[(GP_EVENT_UNKNOWN, None, .001)])
    with caplog.at_level(logging.INFO):
        with backend._get_camera_lock(0):
            session = backend._get_or_open_session(0)
            session._get_config("flashmode")
            session._set_config("flashmode", "Off")
        with gb._wait_log(PORT, "slow config"):
            clock.advance(.251)
    lines = [r.message for r in caplog.records if r.name == gb.__name__ and r.levelno == logging.INFO]
    assert not any("reason=camera lock" in line or "reason=init lock" in line or "config flashmode" in line for line in lines)
    assert not any("reason=camera init" in line for line in lines)
    assert not any("reason=startup events" in line for line in lines)
    assert any("reason=slow config" in line for line in lines)
    backend._close_session(0)


def test_g_permanently_wedged_body_has_a_reset_cap(rig):
    rig.wedged[PORT] = -10
    rig.reset_error = OSError("reset refused")
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    for _ in range(6):
        backend.list_devices()
        rig.clock.advance(21)
    assert len(rig.resets) == 2
    # A genuine successful open lifts the consecutive-failure cap.
    rig.wedged.clear()
    backend.list_devices()
    rig.wedged[PORT] = -10
    rig.clock.advance(21)
    backend.list_devices()
    assert len(rig.resets) == 3
