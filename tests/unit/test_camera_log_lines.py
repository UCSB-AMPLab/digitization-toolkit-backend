"""Each camera's recent libgphoto2 log lines, written out when a capture fails.

libgphoto2 has one process-wide log callback. The lines it delivers are
attributed to a camera by the thread that is inside that camera's lock when
the line arrives, so the fakes here call the registered callback from inside
the fake camera's methods, on the thread making the call, as libgphoto2 does.

Version: 0.4.0
"""

import logging
import os
import stat
import threading
import types

import pytest

import capture.backends.gphoto2_backend as gb
import tests.unit.test_gphoto2_capture_timeout as ct
from capture.backends import camera_log_lines as cll
from tests.unit.test_gphoto2_capture_timeout import (
    GP_EVENT_TIMEOUT,
    _Script,
    _config,
    _make_backend,
)

GP_LOG_DEBUG = 2
SERIAL = "0123456789AB"


# ----------------------------------------------------------------------
# The buffer
# ----------------------------------------------------------------------

def _texts(lines):
    return [line.split(" ", 1)[1] for line in lines]


def test_buffer_is_bounded_per_camera_and_lines_are_capped():
    rec = cll.CameraLogLines(lines=3, line_chars=12)
    rec.bind(0)
    for n in range(5):
        rec.record(GP_LOG_DEBUG, "ptp2", f"line {n} " + "x" * 40)
    rec.unbind(0)
    rec.bind(1)
    rec.record(GP_LOG_DEBUG, "ptp2", "oth a")
    rec.record(GP_LOG_DEBUG, "ptp2", "oth b")
    rec.unbind(1)
    # A line from a thread bound to no camera belongs to none.
    rec.record(GP_LOG_DEBUG, "ptp2", "unbound")

    zero = _texts(rec.lines(0))
    assert len(zero) == 3
    assert [t[:12] for t in zero] == ["ptp2: line 2", "ptp2: line 3", "ptp2: line 4"]
    assert all(len(t) <= 12 for t in zero)
    assert _texts(rec.lines(1)) == ["ptp2: oth a", "ptp2: oth b"]
    assert not any("unbound" in t for t in zero + _texts(rec.lines(1)))


def test_lines_go_to_the_camera_their_own_thread_is_bound_to():
    """Two bindings held at once on two threads, and an unbound third thread."""
    rec = cll.CameraLogLines(lines=10)
    both_bound = threading.Barrier(3)
    emitted = threading.Barrier(3)
    errors = []

    def camera_one():
        try:
            rec.bind(1)
            both_bound.wait(5)
            rec.record(GP_LOG_DEBUG, "usb", "from camera 1")
            emitted.wait(5)
            rec.unbind(1)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    def unbound():
        try:
            both_bound.wait(5)
            rec.record(GP_LOG_DEBUG, "usb", "from no camera")
            emitted.wait(5)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    rec.bind(0)
    threads = [threading.Thread(target=camera_one), threading.Thread(target=unbound)]
    for t in threads:
        t.start()
    both_bound.wait(5)
    # Camera 1's binding is active on its own thread while this one emits.
    rec.record(GP_LOG_DEBUG, "usb", "from camera 0")
    emitted.wait(5)
    for t in threads:
        t.join(5)
    rec.unbind(0)
    assert errors == []
    assert _texts(rec.lines(0)) == ["usb: from camera 0"]
    assert _texts(rec.lines(1)) == ["usb: from camera 1"]


PORT_A = "usb:001,005"
PORT_B = "usb:001,006"


def test_a_position_keeps_its_lines_while_the_same_body_is_learned():
    rec = cll.CameraLogLines(lines=10)
    rec.occupy(0, PORT_A, None)
    rec.bind(0)
    rec.record(GP_LOG_DEBUG, "ptp2", "opening")
    # The serial of the body on the same port is learned: the same body.
    rec.occupy(0, PORT_A, SERIAL)
    rec.record(GP_LOG_DEBUG, "ptp2", "opened")
    # The same serial on a new port is the same body, re-enumerated.
    rec.occupy(0, PORT_B, SERIAL)
    rec.record(GP_LOG_DEBUG, "ptp2", "moved")
    rec.unbind(0)
    assert _texts(rec.lines(0)) == ["ptp2: opening", "ptp2: opened", "ptp2: moved"]
    assert rec.occupant_serial(0) == SERIAL


def test_a_position_is_cleared_when_another_body_takes_it():
    rec = cll.CameraLogLines(lines=10)
    rec.occupy(0, PORT_A, SERIAL)
    rec.bind(0)
    rec.record(GP_LOG_DEBUG, "ptp2", "first body")
    rec.occupy(0, PORT_A, "OTHERSERIAL")
    rec.record(GP_LOG_DEBUG, "ptp2", "second body")
    rec.occupy(0, PORT_B, None)
    rec.record(GP_LOG_DEBUG, "ptp2", "third body")
    rec.unbind(0)
    assert _texts(rec.lines(0)) == ["ptp2: third body"]


def test_lines_are_not_attributed_inside_unattributed():
    rec = cll.CameraLogLines(lines=10)
    rec.bind(0)
    rec.record(GP_LOG_DEBUG, "ptp2", "before")
    with cll.unattributed():
        rec.record(GP_LOG_DEBUG, "gphoto2-abilities-list", "scanning every port")
        with cll.unattributed():
            rec.record(GP_LOG_DEBUG, "port", "nested")
        rec.record(GP_LOG_DEBUG, "port", "still inside")
    rec.record(GP_LOG_DEBUG, "ptp2", "after")
    rec.unbind(0)
    assert _texts(rec.lines(0)) == ["ptp2: before", "ptp2: after"]


def test_unattributed_is_per_thread():
    rec = cll.CameraLogLines(lines=10)
    inside = threading.Event()
    done = threading.Event()

    def scanner():
        with cll.unattributed():
            inside.set()
            done.wait(5)

    t = threading.Thread(target=scanner)
    t.start()
    assert inside.wait(5)
    rec.bind(0)
    rec.record(GP_LOG_DEBUG, "ptp2", "while another thread scans")
    rec.unbind(0)
    done.set()
    t.join(5)
    assert _texts(rec.lines(0)) == ["ptp2: while another thread scans"]


def test_install_registers_one_debug_callback_and_keeps_its_handle():
    calls = []
    handle = object()

    def gp_log_add_func(level, func, *rest):
        calls.append((level, func))
        return handle

    fake = types.SimpleNamespace(GP_LOG_DEBUG=GP_LOG_DEBUG, gp_log_add_func=gp_log_add_func)
    rec = cll.CameraLogLines()
    assert rec.install(fake) is True
    assert rec.install(fake) is True
    assert len(calls) == 1 and calls[0][0] == GP_LOG_DEBUG
    assert rec._handle is handle
    # libgphoto2 hands the callback (level, domain, string[, data]).
    rec.bind(0)
    calls[0][1](GP_LOG_DEBUG, "ptp2/usb", "ptp_usb_getresp", None)
    rec.unbind(0)
    assert _texts(rec.lines(0)) == ["ptp2/usb: ptp_usb_getresp"]


def test_install_reports_a_negative_status_and_retries_later():
    calls = []
    handle = object()
    results = [(-3, handle), (0, handle)]

    def gp_log_add_func(level, func, *rest):
        calls.append(level)
        return results[len(calls) - 1]

    fake = types.SimpleNamespace(GP_LOG_DEBUG=GP_LOG_DEBUG, gp_log_add_func=gp_log_add_func)
    rec = cll.CameraLogLines()
    assert rec.install(fake) is False
    assert rec.install(fake) is True
    assert rec.install(fake) is True
    assert len(calls) == 2
    assert rec._handle is handle


def test_install_without_a_log_api_is_a_no_op():
    rec = cll.CameraLogLines()
    assert rec.install(types.SimpleNamespace()) is False


# ----------------------------------------------------------------------
# The file and the directory cap
# ----------------------------------------------------------------------

def test_write_failure_writes_that_camera_only(tmp_path):
    rec = cll.CameraLogLines(lines=10)
    rec.occupy(0, PORT_A, SERIAL)
    rec.bind(0)
    rec.record(GP_LOG_DEBUG, "ptp2", "camera zero")
    rec.unbind(0)
    rec.bind(1)
    rec.record(GP_LOG_DEBUG, "ptp2", "camera one")
    rec.unbind(1)

    path = rec.write_failure(tmp_path, 0, SERIAL, "capture timed out", logging.getLogger("t"))
    assert path is not None and path.parent == tmp_path
    assert path.name.startswith("capture-failure-")
    assert "camera0" in path.name and "6789AB" in path.name
    text = path.read_text()
    assert "camera zero" in text and "camera one" not in text
    assert "capture timed out" in text
    assert stat.S_IMODE(path.stat().st_mode) == cll.FILE_MODE == 0o640


def _old_file(directory, n, size=10):
    path = directory / f"capture-failure-20260101T00000{n}000Z-camera0-unknown.log"
    path.write_bytes(b"x" * size)
    os.utime(path, (1_000_000 + n, 1_000_000 + n))
    return path


def test_directory_keeps_at_most_max_files_oldest_deleted_first(tmp_path):
    old = [_old_file(tmp_path, n) for n in range(5)]
    other = tmp_path / "unrelated.log"
    other.write_text("not ours")
    rec = cll.CameraLogLines()
    new = rec.write_failure(tmp_path, 0, None, "capture failed", logging.getLogger("t"),
                            max_files=3, max_bytes=10**9)
    left = sorted(p.name for p in tmp_path.glob("capture-failure-*.log"))
    assert len(left) == 3
    assert new.name in left
    assert old[4].name in left and old[3].name in left
    assert other.exists(), "files that are not failure logs are left alone"


def test_directory_total_size_is_capped_oldest_deleted_first(tmp_path):
    old = [_old_file(tmp_path, n, size=1000) for n in range(4)]
    rec = cll.CameraLogLines()
    new = rec.write_failure(tmp_path, 0, None, "capture failed", logging.getLogger("t"),
                            max_files=100, max_bytes=2500)
    total = sum(p.stat().st_size for p in tmp_path.glob("capture-failure-*.log"))
    assert total <= 2500
    assert new.exists()
    assert not old[0].exists() and not old[1].exists()


def test_write_error_returns_none_and_logs_one_line(tmp_path, caplog):
    blocker = tmp_path / "camera-logs"
    blocker.write_text("a file where the directory should be")
    rec = cll.CameraLogLines()
    with caplog.at_level(logging.WARNING, logger="t"):
        assert rec.write_failure(blocker, 0, None, "capture failed",
                                 logging.getLogger("t")) is None
    assert len([r for r in caplog.records if r.name == "t"]) == 1


def test_enospc_on_write_leaves_no_partial_file_and_the_cap_still_runs(
    tmp_path, monkeypatch, caplog
):
    import errno

    old = [_old_file(tmp_path, n) for n in range(4)]
    rec = cll.CameraLogLines(lines=10)
    rec.bind(0)
    for n in range(5):
        rec.record(GP_LOG_DEBUG, "ptp2", f"line {n}")
    rec.unbind(0)
    writes = []

    def full_disk(fd, data):
        writes.append(len(data))
        if len(writes) == 1:
            return os.write(fd, bytes(data[:10]))
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(cll, "_os_write", full_disk)
    with caplog.at_level(logging.WARNING, logger="t"):
        assert rec.write_failure(tmp_path, 0, None, "capture failed",
                                 logging.getLogger("t"), max_files=2,
                                 max_bytes=10**9) is None
    left = sorted(tmp_path.glob("capture-failure-*.log"))
    assert all(p in old for p in left), "the partial file is removed"
    assert len(left) == 2, "the cap runs even though the write failed"
    assert old[3] in left and old[2] in left
    assert len([r for r in caplog.records if r.name == "t"]) == 1


def test_a_short_write_is_completed(tmp_path, monkeypatch):
    rec = cll.CameraLogLines(lines=10)
    rec.bind(0)
    for n in range(20):
        rec.record(GP_LOG_DEBUG, "ptp2", f"line {n}")
    rec.unbind(0)
    calls = []

    def one_byte_at_a_time(fd, data):
        calls.append(1)
        return os.write(fd, bytes(data[:1]))

    monkeypatch.setattr(cll, "_os_write", one_byte_at_a_time)
    path = rec.write_failure(tmp_path, 0, None, "capture failed", logging.getLogger("t"))
    text = path.read_text()
    assert len(calls) == len(text.encode())
    assert "ptp2: line 19" in text and text.endswith("\n")


def test_the_setting_is_on_by_default():
    from app.core.config import Settings

    assert Settings.model_fields["CAMERA_LOG_LINES"].default is True


# ----------------------------------------------------------------------
# Through the gphoto2 backend
# ----------------------------------------------------------------------

def _logging_backend(monkeypatch, tmp_path, script, enabled=True):
    from app.core.config import settings

    monkeypatch.setattr(settings, "CAMERA_LOG_LINES", enabled)
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path / "logs"))
    rec = cll.CameraLogLines(lines=50)
    monkeypatch.setattr(cll, "RECORDER", rec)
    registered = []
    holder = {}

    def gp_log_add_func(level, func, *rest):
        registered.append(level)
        holder["func"] = func
        return object()

    def emit(text):
        func = holder.get("func")
        if func is not None:
            func(GP_LOG_DEBUG, "ptp2", text, None)

    # Called inside the fake trigger_capture, after its line is emitted.
    emit.during_trigger = []

    real_make_fake = ct._make_fake_gp

    def make_fake_gp(s):
        fake = real_make_fake(s)
        fake.GP_LOG_DEBUG = GP_LOG_DEBUG
        fake.gp_log_add_func = gp_log_add_func
        base_trigger = fake.Camera.trigger_capture

        def trigger_capture(self):
            emit("ptp_usb_sendreq InitiateCapture")
            for hook in emit.during_trigger:
                hook()
            return base_trigger(self)

        fake.Camera.trigger_capture = trigger_capture
        base_exit = fake.Camera.exit

        def exit_(self):
            emit("camera exit marker")
            return base_exit(self)

        fake.Camera.exit = exit_

        # The binding's destructor (gp_camera_unref) logs too.
        def del_(self):
            emit("camera destructor marker")

        fake.Camera.__del__ = del_

        # Bus-wide detection logs about every port, so about other cameras.
        base_autodetect = fake.Camera.autodetect

        def autodetect():
            emit("bus scan marker: autodetect")
            return base_autodetect()

        fake.Camera.autodetect = staticmethod(autodetect)
        for cls, what in ((fake.CameraAbilitiesList, "abilities"), (fake.PortInfoList, "ports")):
            def load(self, _what=what):
                emit(f"bus scan marker: {_what} load")
            cls.load = load
        return fake

    # _make_backend builds its fake through its own module's _make_fake_gp.
    monkeypatch.setattr(ct, "_make_fake_gp", make_fake_gp)
    backend, fake_gp = _make_backend(monkeypatch, script)
    return backend, rec, registered, emit


def _timeout_script():
    return _Script(
        settings={"shutterspeed": "1/125", "serialnumber": SERIAL},
        events=[(GP_EVENT_TIMEOUT, None, 0.05)],
    )


def test_capture_timeout_writes_the_failing_cameras_lines(monkeypatch, tmp_path):
    backend, rec, registered, emit = _logging_backend(monkeypatch, tmp_path, _timeout_script())
    monkeypatch.setattr(gb, "_CAPTURE_TIMEOUT_BASE_S", 0.3)
    assert registered == [GP_LOG_DEBUG]

    # Camera 1's lines arrive on another thread, inside camera 1's lock.
    def camera_one():
        with backend._get_camera_lock(1):
            emit("camera one line")

    t = threading.Thread(target=camera_one)
    t.start()
    t.join()

    with pytest.raises(RuntimeError, match="timed out|no image after"):
        backend.capture_image(tmp_path / "x.jpg", _config())

    files = list((tmp_path / "logs" / cll.DIR_NAME).glob("capture-failure-*.log"))
    assert len(files) == 1
    name = files[0].name
    assert "camera0" in name and "6789AB" in name
    text = files[0].read_text()
    assert "ptp_usb_sendreq InitiateCapture" in text
    assert "camera one line" not in text
    assert "capture timed out" in text
    # The failed capture's session is closed inside _PTPSession.capture
    # (_close_failed_capture) before this file is written; a close is
    # never kept for a camera.
    assert "camera exit marker" not in text


def test_a_write_error_does_not_raise_into_the_capture(monkeypatch, tmp_path, caplog):
    backend, rec, registered, emit = _logging_backend(monkeypatch, tmp_path, _timeout_script())
    monkeypatch.setattr(gb, "_CAPTURE_TIMEOUT_BASE_S", 0.3)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / cll.DIR_NAME).write_text("a file where the directory should be")

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="DSLR capture failed"):
            backend.capture_image(tmp_path / "x.jpg", _config())
    assert any("camera log lines" in r.getMessage() for r in caplog.records)


def test_switched_off_registers_nothing_and_writes_nothing(monkeypatch, tmp_path):
    backend, rec, registered, emit = _logging_backend(
        monkeypatch, tmp_path, _timeout_script(), enabled=False
    )
    monkeypatch.setattr(gb, "_CAPTURE_TIMEOUT_BASE_S", 0.3)
    assert registered == []
    with pytest.raises(RuntimeError):
        backend.capture_image(tmp_path / "x.jpg", _config())
    assert not (tmp_path / "logs" / cll.DIR_NAME).exists()


def test_without_a_libgphoto2_log_api_nothing_is_kept_or_written(monkeypatch, tmp_path):
    from app.core.config import settings

    monkeypatch.setattr(settings, "CAMERA_LOG_LINES", True)
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(cll, "RECORDER", cll.CameraLogLines())
    monkeypatch.setattr(gb, "_CAPTURE_TIMEOUT_BASE_S", 0.3)
    # The plain fake has no gp_log_add_func.
    backend, _ = _make_backend(monkeypatch, _timeout_script())
    assert backend._log_lines is None
    with pytest.raises(RuntimeError):
        backend.capture_image(tmp_path / "x.jpg", _config())
    assert not (tmp_path / "logs" / cll.DIR_NAME).exists()


def test_a_session_that_learns_its_serial_after_opening_still_writes_its_lines(
    monkeypatch, tmp_path
):
    # The body answers no serial at open.
    script = _Script(settings={"shutterspeed": "1/125"},
                     events=[(GP_EVENT_TIMEOUT, None, 0.05)])
    backend, rec, registered, emit = _logging_backend(monkeypatch, tmp_path, script)
    monkeypatch.setattr(gb, "_CAPTURE_TIMEOUT_BASE_S", 0.3)
    with backend._get_camera_lock(0):
        session = backend._get_or_open_session(0)
        emit("line before the serial was learned")
    # As _identify_unknown_ports does through an open session.
    session.serial = SERIAL
    backend._learn_serial(0, session.port, SERIAL)

    with pytest.raises(RuntimeError, match="timed out|no image after"):
        backend.capture_image(tmp_path / "x.jpg", _config())

    files = list((tmp_path / "logs" / cll.DIR_NAME).glob("capture-failure-*.log"))
    assert len(files) == 1
    assert "6789AB" in files[0].name
    text = files[0].read_text()
    assert "line before the serial was learned" in text
    assert "ptp_usb_sendreq InitiateCapture" in text


def test_a_body_moved_from_a_provisional_position_leaves_nothing_behind(
    monkeypatch, tmp_path
):
    backend, rec, registered, emit = _logging_backend(monkeypatch, tmp_path, _timeout_script())
    model = "Canon EOS Rebel T7"
    # SERIAL is pinned at 0 but its port is not yet known, so the body is
    # laid out on provisional position 1.
    backend._pins = {0: SERIAL}
    with backend._map_lock:
        backend._port_map = backend._build_port_map(
            backend._pins, backend._serial_by_port, [(model, PORT_A)]
        )
    assert backend._port_map == {1: (model, PORT_A)}
    with backend._get_camera_lock(1):
        emit("identification of the pinned body")
    backend._learn_serial(1, PORT_A, SERIAL)
    # The body goes to its pinned index; another body now holds position 1.
    with backend._map_lock:
        backend._port_map = backend._build_port_map(
            backend._pins, backend._serial_by_port, [(model, PORT_A), (model, PORT_B)]
        )
    assert backend._port_map == {0: (model, PORT_A), 1: (model, PORT_B)}

    backend._write_camera_log_lines(1, None, RuntimeError("x"))
    files = list((tmp_path / "logs" / cll.DIR_NAME).glob("capture-failure-*.log"))
    assert len(files) == 1
    assert "camera1-unknown" in files[0].name
    assert "identification of the pinned body" not in files[0].read_text()


def _failure_files(tmp_path):
    return list((tmp_path / "logs" / cll.DIR_NAME).glob("capture-failure-*.log"))


def test_a_map_published_during_a_capture_keeps_the_failing_cameras_lines(
    monkeypatch, tmp_path
):
    backend, rec, registered, emit = _logging_backend(monkeypatch, tmp_path, _timeout_script())
    monkeypatch.setattr(gb, "_CAPTURE_TIMEOUT_BASE_S", 0.3)

    def publish_without_camera_0():
        # Another thread's scan: the body has gone from the bus.
        def scan():
            with backend._map_lock:
                backend._port_map = backend._build_port_map(
                    backend._pins, backend._serial_by_port, []
                )
        t = threading.Thread(target=scan)
        t.start()
        t.join(5)

    emit.during_trigger.append(publish_without_camera_0)
    with pytest.raises(RuntimeError, match="timed out|no image after"):
        backend.capture_image(tmp_path / "x.jpg", _config())

    files = _failure_files(tmp_path)
    assert len(files) == 1
    assert "ptp_usb_sendreq InitiateCapture" in files[0].read_text()


def test_a_closed_session_keeps_its_lines_until_another_body_takes_the_position(
    monkeypatch, tmp_path
):
    backend, rec, registered, emit = _logging_backend(monkeypatch, tmp_path, _timeout_script())
    with backend._get_camera_lock(0):
        backend._get_or_open_session(0)
        emit("first session")
        backend._close_session(0)
    assert any("first session" in line for line in rec.lines(0))
    # Another body is published at position 0 (laid out with no pins, so
    # the open's pin on SERIAL does not hold position 0 for its body).
    with backend._map_lock:
        backend._port_map = backend._build_port_map(
            {}, {}, [("Canon EOS Rebel T7", PORT_B)]
        )
    assert backend._port_map == {0: ("Canon EOS Rebel T7", PORT_B)}
    assert not any("first session" in line for line in rec.lines(0))


def test_a_bus_scan_inside_a_capture_is_not_kept_for_the_camera(monkeypatch, tmp_path):
    backend, rec, registered, emit = _logging_backend(monkeypatch, tmp_path, _timeout_script())
    monkeypatch.setattr(gb, "_CAPTURE_TIMEOUT_BASE_S", 0.3)
    assert backend._port_map == {}, "the first capture scans under camera 0's lock"

    with pytest.raises(RuntimeError, match="timed out|no image after"):
        backend.capture_image(tmp_path / "x.jpg", _config())

    files = _failure_files(tmp_path)
    assert len(files) == 1
    text = files[0].read_text()
    assert "ptp_usb_sendreq InitiateCapture" in text
    assert "bus scan marker" not in text


def test_a_retired_bodys_close_is_not_kept_for_the_body_that_replaced_it(
    monkeypatch, tmp_path
):
    backend, rec, registered, emit = _logging_backend(monkeypatch, tmp_path, _timeout_script())
    model = "Canon EOS Rebel T7"
    with backend._get_camera_lock(0):
        backend._get_or_open_session(0)
    # The map republishes position 0 from body A to body B ...
    with backend._map_lock:
        backend._port_map = backend._build_port_map(
            {}, {PORT_B: "BODYB00000B2"}, [(model, PORT_B)]
        )
    assert backend._port_map == {0: (model, PORT_B)}
    # ... and reconciliation closes A's cached session under the lock.
    with backend._get_camera_lock(0):
        backend._close_session(0)
        emit("body B line")

    backend._write_camera_log_lines(0, None, RuntimeError("x"))
    files = _failure_files(tmp_path)
    assert len(files) == 1
    assert "0000B2" in files[0].name
    text = files[0].read_text()
    assert "body B line" in text
    assert "camera exit marker" not in text
    assert "camera destructor marker" not in text


def test_a_brief_identification_camera_is_not_destroyed_into_the_next_position(
    monkeypatch, tmp_path
):
    backend, rec, registered, emit = _logging_backend(monkeypatch, tmp_path, _timeout_script())
    model = "Canon EOS Rebel T7"
    with backend._map_lock:
        backend._port_map = backend._build_port_map(
            {}, {}, [(model, PORT_A), (model, PORT_B)]
        )
    assert backend._port_map == {0: (model, PORT_A), 1: (model, PORT_B)}
    monkeypatch.setattr(backend, "_open_claimed",
                        lambda index, model_, port, may_scan, factory: (factory(port), port))

    backend._identify_unknown_ports()

    assert not any("camera destructor marker" in line for line in rec.lines(1)), rec.lines(1)
    assert not any("camera destructor marker" in line for line in rec.lines(0)), rec.lines(0)


def test_a_listing_camera_is_not_destroyed_into_the_next_position(monkeypatch, tmp_path):
    backend, rec, registered, emit = _logging_backend(monkeypatch, tmp_path, _timeout_script())
    model = "Canon EOS Rebel T7"
    with backend._map_lock:
        backend._port_map = backend._build_port_map(
            {}, {}, [(model, PORT_A), (model, PORT_B)]
        )
    monkeypatch.setattr(backend, "_open_claimed",
                        lambda index, model_, port, may_scan, factory: (factory(port), port))

    backend._enumerate_port_map(dict(backend._port_map))

    assert not any("camera destructor marker" in line for line in rec.lines(1)), rec.lines(1)
    assert not any("camera destructor marker" in line for line in rec.lines(0)), rec.lines(0)


def _collect_under_another_binding(backend, fail):
    """Fail a camera's construction under camera 0's lock, then collect under camera 1's."""
    import gc

    was_enabled = gc.isenabled()
    gc.disable()
    try:
        with backend._get_camera_lock(0):
            try:
                fail()
            except Exception:
                pass
        with backend._get_camera_lock(1):
            gc.collect()
    finally:
        if was_enabled:
            gc.enable()


@pytest.mark.parametrize("step", ["init", "set_port_info"])
def test_a_camera_whose_construction_fails_is_released_unattributed(
    monkeypatch, tmp_path, step
):
    backend, rec, registered, emit = _logging_backend(monkeypatch, tmp_path, _timeout_script())
    fake_gp = gb.gp

    def raising(self, *args):
        # The binding's methods are native and leave no Python frame holding
        # the camera in the traceback; this fake's frame must not either.
        del self, args
        raise fake_gp.GPhoto2Error("[-7] I/O problem")

    monkeypatch.setattr(fake_gp.Camera, step, raising)
    _collect_under_another_binding(
        backend, lambda: gb._init_once(ct.MODEL, ct.PORT, None, may_reset=True)
    )
    for position in (0, 1):
        assert not any("camera destructor marker" in line for line in rec.lines(position)), (
            position, rec.lines(position))
