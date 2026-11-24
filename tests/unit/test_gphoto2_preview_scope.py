"""Preview policy spans connection checks; diagnostics use only in-memory labels."""
import logging
from types import SimpleNamespace

import pytest

import capture.backends.gphoto2_backend as gb
from capture import service
from capture.backends.errors import CameraNotConnectedError
from tests.unit.test_gphoto2_recovery import rig, recovery_state, _session
from tests.unit.test_gphoto2_capture_timeout import MODEL, PORT, GP_EVENT_FILE_ADDED, _file_path

OTHER = "usb:003,002"


def test_s1_service_connection_identification_obeys_preview_backoff(rig, monkeypatch):
    rig.ports[OTHER] = ("3-1", 2, 456)
    rig.wedged[PORT] = -10
    rig.init_hook = lambda port: rig.clock.advance(1.1) if port == PORT else None
    rig.reset_error = OSError("preview must not reset")
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._pins = {0: "A", 1: "B"}
    backend._serial_by_port = {OTHER: "A"}
    backend._refresh_port_map()
    assert 1 not in backend._port_map and backend._port_map[2][1] == PORT
    monkeypatch.setattr(service, "get_backend", lambda: backend)
    for _ in range(3):
        with pytest.raises(CameraNotConnectedError):
            service.capture_preview_frame(1)
        rig.clock.advance(2)
    assert rig.inits[PORT] == 1, rig.inits
    assert not rig.resets and not backend._claimed_ports
    until = gb._init_backoff["1-1"][1]
    rig.clock.advance(until - rig.clock.now)
    with pytest.raises(CameraNotConnectedError):
        service.capture_preview_frame(1)
    assert rig.inits[PORT] == 2 and not rig.resets
    backend.list_devices()
    assert rig.inits[PORT] == 3 and len(rig.resets) == 1


def test_s1_preview_fallback_cannot_reset_and_leaves_foreground_policy_intact(rig, monkeypatch, tmp_path):
    rig.wedged[PORT] = -10
    rig.init_hook = lambda port: rig.clock.advance(1.1)
    rig.reset_error = OSError("preview must not reset")
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._refresh_port_map()
    monkeypatch.setattr(service, "get_backend", lambda: backend)
    monkeypatch.setattr(service, "is_camera_connected", lambda index: True)
    monkeypatch.setattr(service, "_PREVIEW_TMP_DIR", tmp_path)
    monkeypatch.setattr(backend, "capture_preview", lambda index: (_ for _ in ()).throw(NotImplementedError()))
    with pytest.raises(RuntimeError):
        service.capture_preview_frame(0)
    assert rig.inits[PORT] == 1 and not rig.resets
    backend.list_devices()
    assert len(rig.resets) == 1 and not backend._claimed_ports


@pytest.mark.parametrize("index,serial,expected", [(2, None, "Camera 2"), (7, "A", "Camera 7")])
def test_l1_requested_index_does_not_require_an_identity(rig, index, serial, expected):
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._port_map = {index: (MODEL, PORT)}
    if serial:
        backend._serial_by_port[PORT] = serial
    assert str(backend._open_labels(index, PORT)[PORT]) == expected


@pytest.mark.parametrize("ending", ["cap", "read", "file", "move"])
def test_s3_operator_text_has_label_but_no_usb_or_camera_file_path(monkeypatch, caplog, ending):
    backend, script, gp, _ = _session(monkeypatch)
    backend._port_map = {0: (MODEL, PORT)}
    backend._serial_by_port[PORT] = "A"
    path = _file_path(name="PRIVATE_CAMERA_PATH.JPG")
    with caplog.at_level(logging.INFO, logger=gb.__name__):
        if ending == "move":
            def move(port):
                raise gb._PortMoved("usb:001,009", port)
            with pytest.raises(RuntimeError) as caught:
                backend._open_claimed(0, MODEL, PORT, False, move)
        elif ending == "file":
            session = backend._get_or_open_session(0)
            session._unsolicited.add((path.folder, path.name))
            with pytest.raises(gb.CaptureAttributionError) as caught:
                session._capture_file(path)
            backend._close_session(0)
        else:
            if ending == "read":
                monkeypatch.setattr(gp.Camera, "wait_for_event", lambda *a: (_ for _ in ()).throw(gp.GPhoto2Error("[-7] read")))
            else:
                monkeypatch.setattr(gb, "_OPEN_DRAIN_EVENTS", 0)
            with pytest.raises(gb.CaptureAttributionError) as caught:
                backend._get_or_open_session(0)
    text = str(caught.value)
    assert text.startswith("Camera 0"), text
    assert "usb:" not in text and path.folder not in text and path.name not in text
    if ending == "file":
        lines = [r.getMessage() for r in caplog.records if r.name == gb.__name__]
        assert any(PORT in s and path.folder in s and path.name in s for s in lines)


def test_s4_logging_formats_only_in_memory_data(rig, monkeypatch):
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._port_map = {0: (MODEL, PORT)}
    backend._serial_by_port[PORT] = "A"
    backend._roles_by_port = {PORT: (gb._hardware_id(MODEL, "A"), "right")}
    reads = []
    backend._role_reader = lambda key: (reads.append(True), "left")[1]
    labels = backend._open_labels(0, PORT)
    lines = []

    class Handler(logging.Handler):
        def emit(self, record):
            from pathlib import Path

            def forbidden(*args, **kwargs):
                reads.append(True)
                raise AssertionError("file I/O during log formatting")

            with monkeypatch.context() as guard:
                guard.setattr(Path, "read_text", forbidden)
                guard.setattr(Path, "stat", forbidden)
                lines.append(record.getMessage())

    handler = Handler()
    logger = gb._WAIT_LOG
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    token = gb._OPEN_LABELS.set(labels)
    try:
        with gb._wait_log(PORT, "startup events") as details:
            details.update(count=1, files=1)
    finally:
        gb._OPEN_LABELS.reset(token)
        logger.removeHandler(handler)
        logger.setLevel(level)
    assert not reads, "log formatting read the role from disk"
    assert len(lines) == 1 and "Camera 0 (right)" in lines[0]
    assert all(isinstance(label, str) for label in labels.values())


def test_s4_existing_registry_read_populates_and_invalidates_role_memory(rig):
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._port_map = {0: (MODEL, PORT)}
    backend._serial_by_port[PORT] = "A"
    entry = {"role": "right", "image_format": "JPEG"}
    backend._registry_factory = lambda: SimpleNamespace(get_camera_by_id=lambda key: entry)
    backend._role_reader = lambda key: (_ for _ in ()).throw(AssertionError("extra read"))
    hw_id = gb._hardware_id(MODEL, "A")
    backend._read_stored_format(hw_id)
    assert str(backend._open_labels(0, PORT)[PORT]) == "Camera 0 (right)"
    entry["role"] = None
    backend._read_stored_format(hw_id)
    assert str(backend._open_labels(0, PORT)[PORT]) == "Camera 0"
    backend._serial_by_port[PORT] = "B"
    assert str(backend._open_labels(0, PORT)[PORT]) == "Camera 0"


def test_s3_capture_response_keeps_the_safe_operator_text(monkeypatch, tmp_path):
    path = _file_path(name="PRIVATE_CAMERA_PATH.JPG")
    backend, _, _, _ = _session(monkeypatch, before=[(GP_EVENT_FILE_ADDED, path, 0)],
                               after=[(GP_EVENT_FILE_ADDED, path, 0)])
    backend._serial_by_port[PORT] = "A"
    with pytest.raises(RuntimeError) as caught:
        backend.capture_image(tmp_path / "page.jpg", SimpleNamespace(camera_index=0))
    text = str(caught.value)
    assert text.startswith("Camera 0"), text
    assert "usb:" not in text and path.folder not in text and path.name not in text


def test_s3_cached_native_error_details_stay_out_of_operator_text(rig):
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._port_map = {0: (MODEL, PORT)}
    backend._serial_by_port[PORT] = "A"
    gb._init_backoff["1-1"] = (rig.attachment(PORT), rig.clock.now + 20,
                               f"[-10] {PORT} /private/camera/file.jpg")
    with pytest.raises(RuntimeError) as caught:
        backend._open_claimed(0, MODEL, PORT, False, lambda port:
            gb._init_camera(MODEL, port, rig.attachment(port), False))
    assert "usb:" not in str(caught.value) and "/private/camera" not in str(caught.value)


def test_l1_claim_refusal_names_the_requested_index(rig):
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._port_map = {2: (MODEL, PORT)}
    backend._claim_port(7, PORT)
    try:
        with pytest.raises(RuntimeError) as caught:
            backend._brief_camera(2, MODEL, PORT)
        assert str(caught.value).startswith("Camera 2:"), str(caught.value)
    finally:
        backend._release_port(7, PORT)


def test_s2_timeout_names_an_unidentified_holder_as_the_other_camera(rig, monkeypatch):
    import threading
    rig.ports[OTHER] = ("3-1", 2, 456)
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._port_map = {2: (MODEL, PORT), 0: (MODEL, OTHER)}
    backend._serial_by_port[OTHER] = "A"
    backend._roles_by_port[OTHER] = (gb._hardware_id(MODEL, "A"), "left")
    entered, release = threading.Event(), threading.Event()
    errors = []

    def native(port):
        if port == PORT:
            entered.set()
            assert release.wait(2)

    def holder():
        try:
            cam, _ = backend._brief_camera(2, MODEL, PORT)
            cam.exit()
        except BaseException as exc:
            errors.append(exc)
        finally:
            backend._release_port(2, PORT)

    rig.init_hook = native
    monkeypatch.setattr(gb, "_INIT_LOCK_WAIT_S", .01)
    worker = threading.Thread(target=holder, daemon=True)
    worker.start()
    try:
        assert entered.wait(1)
        with pytest.raises(RuntimeError) as caught:
            backend._brief_camera(0, MODEL, OTHER)
        assert "Camera 0 (left) is waiting for the other camera" in str(caught.value)
        assert "Switch the other camera off and on" in str(caught.value)
        assert "Camera 2" not in str(caught.value)
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors and not backend._claimed_ports


@pytest.mark.parametrize("failure", ["identity", "reconnect"])
def test_l1_other_recovery_messages_name_the_requested_index(rig, monkeypatch, failure):
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._port_map = {2: (MODEL, PORT)}
    if failure == "identity":
        backend._pins[2] = "expected"
        rig.script.settings.pop("serialnumber", None)
        with pytest.raises(gb.CameraIdentityError) as caught:
            backend._open_identified_session(2, MODEL, PORT)
    else:
        monkeypatch.setattr(gb, "range", lambda *a: [], raising=False)
        with pytest.raises(RuntimeError) as caught:
            backend._open_claimed(2, MODEL, PORT, True, lambda port: None)
    assert str(caught.value).startswith("Camera 2:"), str(caught.value)
    assert "usb:" not in str(caught.value)
    assert not backend._claimed_ports
