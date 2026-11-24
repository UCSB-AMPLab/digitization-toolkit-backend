"""Requested cameras keep the request index; other cameras need a matching pin."""
import logging
import threading

import pytest

import capture.backends.gphoto2_backend as gb
from tests.unit.test_gphoto2_recovery import rig, recovery_state, _session
from tests.unit.test_gphoto2_capture_timeout import MODEL, PORT, _file_path, _config, GP_EVENT_FILE_ADDED

OTHER = "usb:003,002"


@pytest.mark.parametrize("failure", ["pause", "claim", "move", "drain_cap", "drain_read", "identity", "reconnect", "file"])
def test_l1_first_open_messages_name_the_requested_index(monkeypatch, tmp_path, caplog, failure):
    path = _file_path(name="STARTUP.JPG")
    backend, script, gp, clock = _session(monkeypatch,
        before=[(GP_EVENT_FILE_ADDED, path, 0)] if failure == "file" else (),
        after=[(GP_EVENT_FILE_ADDED, path, 0)] if failure == "file" else None)
    backend._port_map = {0: (MODEL, PORT)}
    assert not backend._serial_by_port
    held = False
    with caplog.at_level(logging.WARNING, logger=gb.__name__):
        try:
            if failure == "pause":
                gb._init_backoff[PORT] = (None, gb._monotonic() + 20, "[-10] transport")
                with pytest.raises(RuntimeError) as caught:
                    backend.capture_preview(0)
            elif failure == "claim":
                backend._claim_port(7, PORT)
                held = True
                with pytest.raises(RuntimeError) as caught:
                    backend._brief_camera(0, MODEL, PORT)
            elif failure == "move":
                with pytest.raises(RuntimeError) as caught:
                    backend._open_claimed(0, MODEL, PORT, False, lambda port:
                        (_ for _ in ()).throw(gb._PortMoved("usb:001,009", port)))
            elif failure == "identity":
                backend._pins[0] = "expected"
                with pytest.raises(gb.CameraIdentityError) as caught:
                    backend._get_or_open_session(0)
            elif failure == "reconnect":
                monkeypatch.setattr(gb, "range", lambda *args: [], raising=False)
                with pytest.raises(RuntimeError) as caught:
                    backend._open_claimed(0, MODEL, PORT, True, lambda port: None)
            else:
                if failure == "drain_cap":
                    monkeypatch.setattr(gb, "_OPEN_DRAIN_EVENTS", 0)
                elif failure == "drain_read":
                    monkeypatch.setattr(gp.Camera, "wait_for_event", lambda *a:
                        (_ for _ in ()).throw(gp.GPhoto2Error("[-7] read")))
                with pytest.raises(RuntimeError) as caught:
                    backend.capture_image(tmp_path / "page.jpg", _config())
            text = str(caught.value)
            assert text.startswith("Camera 0:"), text
            assert "other camera" not in text and "usb:" not in text
        finally:
            if held:
                backend._release_port(7, PORT)
    assert not backend._claimed_ports


@pytest.mark.parametrize("pinned,serial,expected", [
    (False, None, "the other camera"),
    (False, "A", "the other camera"),
    (True, "A", "Camera 7 (left)"),
])
def test_l2_timeout_names_only_a_pinned_identified_holder(rig, monkeypatch, pinned, serial, expected):
    rig.ports[OTHER] = ("3-1", 2, 456)
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._port_map = {7: (MODEL, PORT), 0: (MODEL, OTHER)}
    if serial:
        backend._serial_by_port[PORT] = serial
    if pinned:
        backend._pins[7] = "A"
    backend._serial_by_port[OTHER] = "B"
    backend._pins[0] = "B"
    backend._roles_by_port[PORT] = (gb._hardware_id(MODEL, "A"), "left")
    backend._roles_by_port[OTHER] = (gb._hardware_id(MODEL, "B"), "right")
    entered, release = threading.Event(), threading.Event()
    errors = []

    def native(port):
        if port == PORT:
            entered.set()
            assert release.wait(2)

    def opener():
        try:
            cam, _ = backend._brief_camera(7, MODEL, PORT)
            cam.exit()
        except BaseException as exc:
            errors.append(exc)
        finally:
            backend._release_port(7, PORT)

    rig.init_hook = native
    monkeypatch.setattr(gb, "_INIT_LOCK_WAIT_S", .01)
    worker = threading.Thread(target=opener, daemon=True)
    worker.start()
    try:
        assert entered.wait(1)
        with pytest.raises(RuntimeError) as caught:
            backend._brief_camera(0, MODEL, OTHER)
        text = str(caught.value)
        assert f"Camera 0 (right) is waiting for {expected}" in text, text
        assert f"Switch {expected} off and on" in text, text
        if not pinned:
            assert "Camera 7" not in text
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors and not backend._claimed_ports


def test_l1_waiter_after_restart_keeps_its_request_index(rig, monkeypatch):
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._port_map = {0: (MODEL, PORT), 7: (MODEL, OTHER)}
    monkeypatch.setattr(gb._INIT_LOCK, "acquire_for_port", lambda *a: (False, ("the other camera", OTHER)))
    with pytest.raises(RuntimeError) as caught:
        backend._brief_camera(0, MODEL, PORT)
    assert str(caught.value).startswith("Camera 0 is waiting for the other camera"), str(caught.value)


def test_l3_startup_file_journal_matches_the_refusal_label(monkeypatch, caplog):
    path = _file_path(name="STARTUP.JPG")
    backend, _, _, _ = _session(monkeypatch, before=[(GP_EVENT_FILE_ADDED, path, 0)])
    with caplog.at_level(logging.WARNING, logger=gb.__name__):
        session = backend._get_or_open_session(0)
        with pytest.raises(gb.CaptureAttributionError):
            session._capture_file(path)
    lines = [r.getMessage() for r in caplog.records if r.name == gb.__name__ and "startup file=" in r.getMessage()]
    assert len(lines) == 2
    assert all(s.startswith("[gphoto2] Camera 0:") and s.endswith(f"({PORT})") for s in lines), lines
    backend._close_session(0)


def test_l2_new_address_does_not_inherit_the_old_holder_identity(rig):
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._port_map = {0: (MODEL, PORT)}
    backend._pins[0] = "A"
    backend._serial_by_port[PORT] = "A"
    backend._roles_by_port[PORT] = (gb._hardware_id(MODEL, "A"), "left")
    moved = "usb:001,009"
    assert backend._open_labels(0, moved, PORT)[moved] == "Camera 0 (left)"
    assert backend._open_labels(0, moved, PORT, True)[moved] == "the other camera"
    backend._port_map = {0: (MODEL, moved)}
    backend._serial_by_port[moved] = "A"
    backend._roles_by_port[moved] = (gb._hardware_id(MODEL, "A"), "left")
    assert backend._open_labels(0, moved, PORT, True)[moved] == "Camera 0 (left)"
