"""Recovery targets the failing body and bounds session startup."""
import logging
import threading
from collections import Counter
from types import SimpleNamespace

import pytest

import capture.backends.gphoto2_backend as gb
from tests.unit.test_gphoto2_capture_timeout import (
    _Clock, _Script, _config, _file_path, _make_backend, _make_fake_gp,
    MODEL, PORT, GP_EVENT_TIMEOUT, GP_EVENT_FILE_ADDED, GP_EVENT_UNKNOWN,
)


@pytest.fixture(autouse=True)
def recovery_state(monkeypatch):
    monkeypatch.setattr(gb, "_reset_attempts", {})
    monkeypatch.setattr(gb, "_recent_resets", {})


@pytest.fixture
def rig(monkeypatch):
    clock = _Clock()
    script = _Script(settings={"serialnumber": "A"}, clock=clock)
    fake = _make_fake_gp(script)
    original = fake.Camera
    state = SimpleNamespace(
        clock=clock, script=script, gp=fake, ports={PORT: ("1-1", 5, 123)},
        wedged={}, inits=Counter(), resets=[], closed=[], descriptors={},
        moved=None, init_hook=None, reset_error=None,
    )

    class Camera(original):
        def set_port_info(self, port):
            self.port = port

        @staticmethod
        def autodetect():
            return [(MODEL, p) for p in state.ports]

        def init(self):
            state.inits[self.port] += 1
            if state.init_hook:
                state.init_hook(self.port)
            if self.port in state.wedged:
                raise fake.GPhoto2Error(f"[{state.wedged[self.port]}] transport")
            super().init()

        def wait_for_event(self, ms):
            if not script.trigger_calls:
                return GP_EVENT_TIMEOUT, None
            return super().wait_for_event(ms)

    fake.Camera = Camera
    fake.PortInfoList.__getitem__ = lambda self, idx: idx
    fake.PortInfoList.lookup_path = lambda self, path: path
    monkeypatch.setattr(gb, "gp", fake)
    monkeypatch.setattr(gb, "_GP_AVAILABLE", True)
    monkeypatch.setattr(gb, "_monotonic", clock)
    monkeypatch.setattr(gb, "_usb_sleep", clock.advance, raising=False)
    monkeypatch.setattr(gb, "_recent_resets", {}, raising=False)
    monkeypatch.setattr(gb.GPhoto2Backend, "_load_pins", lambda self: None)
    monkeypatch.setattr(gb.GPhoto2Backend, "_save_pins", lambda self: None)

    def attachment(port):
        values = state.ports.get(port)
        return (values[0], values[1], values[2], 0x04a9, 0x32e1) if values else None

    monkeypatch.setattr(gb, "_usb_attachment", attachment, raising=False)
    monkeypatch.setattr(gb, "_port_at_socket", lambda sock: next(
        (p for p, a in state.ports.items() if a[0] == sock), None), raising=False)
    monkeypatch.setattr(gb, "_usb_open", lambda node, flags: node, raising=False)
    monkeypatch.setattr(gb, "_usb_close", state.closed.append, raising=False)

    def descriptor(fd, size):
        return state.descriptors.get(fd, bytes(8) + bytes.fromhex("a904e132") + bytes(6))

    monkeypatch.setattr(gb, "_usb_read", descriptor, raising=False)

    def reset(fd, request, arg):
        state.resets.append(fd)
        assert request == 0x5514
        if state.reset_error:
            raise state.reset_error
        if state.moved:
            old = state.ports.pop(PORT)
            state.ports[state.moved] = (old[0], 9, old[2] + 1)
        else:
            state.wedged.pop(PORT, None)

    monkeypatch.setattr(gb, "_usb_ioctl", reset, raising=False)
    state.attachment = attachment
    return state


def test_reset_recovers_only_the_failed_body(rig, caplog):
    rig.ports["usb:003,002"] = ("3-1", 2, 456)
    rig.wedged[PORT] = -10
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    with caplog.at_level(logging.INFO):
        rows = backend.list_devices()
    assert {r["serial"] for r in rows} == {"A"}
    assert rig.resets == ["/dev/bus/usb/001/005"]
    assert rig.inits[PORT] == 2
    assert rig.inits["usb:003,002"] == 1
    assert rig.clock.now - 1000 == 2
    assert any("USB reset" in r.message for r in caplog.records
               if r.name == gb.__name__)


@pytest.mark.parametrize("code", [-110, -53, -52, -105])
def test_non_transport_failures_never_reset(rig, code):
    rig.wedged[PORT] = code
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend.list_devices()
    assert not rig.resets


def test_reenumeration_follows_socket_and_claims_new_address(rig):
    rig.wedged[PORT] = -10
    rig.moved = "usb:001,009"
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    rows = backend.list_devices()
    assert len(rows) == 1
    assert rows[0]["serial"] == "A"
    assert rig.moved in rows[0]["location"]
    assert len(rig.resets) == 1
    assert rig.inits[rig.moved] <= 2
    assert not backend._claimed_ports


def test_stuck_reset_is_once_per_listing_and_cooldown_expires(rig):
    rig.wedged[PORT] = -10
    rig.reset_error = OSError("refused")
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend.list_devices()
    backend.list_devices()
    assert len(rig.resets) == 1
    rig.clock.advance(21)
    backend.list_devices()
    assert len(rig.resets) == 2


def test_replacement_at_old_address_is_not_reset(rig):
    expected = rig.attachment(PORT)
    rig.ports[PORT] = ("3-1", 5, 456)
    rig.wedged[PORT] = -10
    with pytest.raises(rig.gp.GPhoto2Error):
        gb._init_camera(MODEL, PORT, expected, True)
    assert not rig.resets


@pytest.mark.parametrize("descriptor", [b"", bytes(18), bytes(8) + bytes.fromhex("a904ffff") + bytes(6)])
def test_reset_descriptor_is_checked_on_the_reset_fd(rig, descriptor):
    rig.descriptors["/dev/bus/usb/001/005"] = descriptor
    rig.wedged[PORT] = -10
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend.list_devices()
    assert not rig.resets
    assert rig.closed


def test_preview_open_never_resets_or_sleeps(rig):
    rig.wedged[PORT] = -10
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._refresh_port_map()
    with pytest.raises(rig.gp.GPhoto2Error):
        backend._get_or_open_session(0, may_scan=False)
    assert not rig.resets
    assert rig.clock.now == 1000


def test_init_lock_serialises_bodies_and_releases_on_failure(rig, monkeypatch):
    other = "usb:003,002"
    rig.ports[other] = ("3-1", 2, 456)
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    queued = threading.Event()
    original_wait = gb._INIT_LOCK._condition.wait

    def wait(timeout=None):
        queued.set()
        return original_wait(timeout)

    monkeypatch.setattr(gb._INIT_LOCK._condition, "wait", wait)
    errors = []

    def hook(port):
        if port == PORT:
            entered.set()
            assert release.wait(2)
            raise rig.gp.GPhoto2Error("[-53] claimed")
        second_entered.set()

    rig.init_hook = hook

    def run(port):
        try:
            session = gb._PTPSession(port, MODEL, logging.getLogger("test"))
            session.close()
        except rig.gp.GPhoto2Error:
            pass
        except BaseException as exc:
            errors.append(exc)

    a = threading.Thread(target=run, args=(PORT,), daemon=True)
    b = threading.Thread(target=run, args=(other,), daemon=True)
    try:
        a.start()
        assert entered.wait(1)
        b.start()
        assert queued.wait(1), "second opener did not reach the gate wait"
        assert not second_entered.is_set(), "second native init preceded gate admission"
    finally:
        release.set()
        a.join(3)
        if b.ident is not None:
            b.join(3)
    assert not a.is_alive() and not b.is_alive()
    assert not errors
    assert second_entered.is_set()


def _session(monkeypatch, before=(), after=None, trigger_error=False):
    clock = _Clock()
    script = _Script(clock=clock, events=after)
    backend, fake = _make_backend(monkeypatch, script)
    monkeypatch.setattr(gb, "_monotonic", clock)
    monkeypatch.setattr(gb.time, "monotonic", clock)
    original = fake.Camera.wait_for_event
    pending = list(before)

    def wait(cam, ms):
        if not script.trigger_calls:
            if pending:
                entry = pending.pop(0)
                if isinstance(entry, Exception):
                    raise entry
                event, data, spent = entry
                clock.advance(spent)
                return event, data
            return GP_EVENT_TIMEOUT, None
        return original(cam, ms)

    monkeypatch.setattr(fake.Camera, "wait_for_event", wait)
    if trigger_error:
        script.triggers = [fake.GPhoto2Error("[-110] busy")]
    return backend, script, fake, clock


def test_startup_drains_backlog_before_configuration_and_only_once(monkeypatch, tmp_path, caplog):
    backend, script, fake, clock = _session(monkeypatch, before=[
        (GP_EVENT_UNKNOWN, None, .001) for _ in range(99)])
    order = []
    original_wait = fake.Camera.wait_for_event
    original_set = fake.Camera.set_config
    monkeypatch.setattr(fake.Camera, "wait_for_event", lambda c, ms: (
        order.append("wait"), original_wait(c, ms))[1])
    monkeypatch.setattr(fake.Camera, "set_config", lambda c, cfg: (
        order.append("set"), original_set(c, cfg))[1])
    with caplog.at_level(logging.INFO):
        backend.capture_image(tmp_path / "one.jpg", _config())
        before = len(order)
        backend.capture_image(tmp_path / "two.jpg", _config())
    assert order.index("wait") < order.index("set")
    assert order[before:].count("wait") == 1
    lines = [r.message for r in caplog.records if "startup events" in r.message]
    assert not lines
    assert clock.now < 1001


def test_incomplete_startup_drain_fails_before_shutter(monkeypatch, tmp_path):
    backend, script, _, _ = _session(monkeypatch,
        before=[(GP_EVENT_UNKNOWN, None, 6)])
    with pytest.raises(RuntimeError, match="startup|queue"):
        backend.capture_image(tmp_path / "page.jpg", _config())
    assert script.trigger_calls == 0
    assert not script.saved_to and not script.deleted


def test_startup_read_error_fails_before_shutter(monkeypatch, tmp_path):
    backend, script, fake, _ = _session(monkeypatch)
    monkeypatch.setattr(fake.Camera, "wait_for_event", lambda *a: (_ for _ in ()).throw(
        fake.GPhoto2Error("[-7] read")))
    with pytest.raises(RuntimeError):
        backend.capture_image(tmp_path / "page.jpg", _config())
    assert script.trigger_calls == 0
    assert not script.saved_to and not script.deleted


def test_long_healthy_post_trigger_traffic_is_accepted(monkeypatch, tmp_path):
    backend, script, _, _ = _session(monkeypatch, after=[
        (GP_EVENT_UNKNOWN, None, 3), (GP_EVENT_FILE_ADDED, _file_path(), 0)])
    backend.capture_image(tmp_path / "page.jpg", _config())
    assert len(script.saved_to) == 1


def test_waits_reach_the_journal_logger(monkeypatch, tmp_path, caplog):
    backend, _, _, _ = _session(monkeypatch, before=[(GP_EVENT_UNKNOWN, None, 1.001)])
    with caplog.at_level(logging.INFO):
        backend.capture_image(tmp_path / "page.jpg", _config())
        backend._close_session(0)
    lines = [r.message for r in caplog.records if r.name == gb.__name__]
    for reason in ("startup events",):
        assert any(reason in line and "duration=" in line for line in lines), (reason, lines)


def test_reset_descriptor_is_opened_before_the_failed_init(rig, monkeypatch):
    seen = []
    monkeypatch.setattr(gb, "_usb_open", lambda node, flags: (seen.append("fd"), node)[1])
    rig.init_hook = lambda port: seen.append("init")
    rig.wedged[PORT] = -10
    gb.GPhoto2Backend(logging.getLogger("test")).list_devices()
    assert seen[0:2] == ["fd", "init"]
    assert len(rig.closed) == 1


def test_reset_wait_does_not_hold_the_init_lock(rig, monkeypatch):
    acquired = []

    def settle(seconds):
        got = gb._INIT_LOCK.acquire(blocking=False)
        acquired.append(got)
        if got:
            gb._INIT_LOCK.release()
        rig.clock.advance(seconds)

    monkeypatch.setattr(gb, "_usb_sleep", settle)
    rig.wedged[PORT] = -10
    gb.GPhoto2Backend(logging.getLogger("test")).list_devices()
    assert acquired and all(acquired)


def test_delayed_reenumeration_after_failed_same_address_retry(rig, monkeypatch):
    calls = []

    def socket_port(socket):
        calls.append(socket)
        if len(calls) == 3:
            values = rig.ports.pop(PORT)
            rig.ports["usb:001,009"] = (values[0], 9, values[2] + 1)
            return "usb:001,009"
        return PORT

    rig.wedged[PORT] = -10
    # A reset can succeed while PTP stays wedged until the delayed disconnect.
    monkeypatch.setattr(gb, "_usb_ioctl", lambda *a: rig.resets.append(a[0]))
    monkeypatch.setattr(gb, "_port_at_socket", socket_port)
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    rows = backend.list_devices()
    assert len(rig.resets) == 1
    assert rig.inits[PORT] == 2
    assert rows[0]["serial"] == "A"
    assert "009" in rows[0]["location"]


def test_reenumerated_body_can_be_reset_again_after_cooldown(rig):
    rig.wedged[PORT] = -10
    rig.moved = "usb:001,009"
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend.list_devices()
    rig.wedged[rig.moved] = -10
    rig.moved = None
    # The reset fake clears PORT only, so this second reset remains wedged.
    backend.list_devices()
    assert len(rig.resets) == 1
    rig.clock.advance(21)
    backend.list_devices()
    assert len(rig.resets) == 2


def test_event_cap_fails_before_trigger_and_bounds_remembered_files(monkeypatch, tmp_path):
    monkeypatch.setattr(gb, "_OPEN_DRAIN_EVENTS", 3)
    backend, script, _, _ = _session(monkeypatch, before=[
        (GP_EVENT_FILE_ADDED, _file_path(name=f"OLD{n}.JPG"), 0) for n in range(4)])
    with pytest.raises(RuntimeError, match="startup"):
        backend.capture_image(tmp_path / "page.jpg", _config())
    assert script.trigger_calls == 0
    assert not script.saved_to
    assert not script.deleted
    assert not backend._claimed_ports


def test_unseen_delayed_file_after_success_is_an_explicit_protocol_limit(monkeypatch, tmp_path):
    # Neither the accepted trigger nor the path event carries a shared token.
    # This indistinguishable schedule pins the limit, not proof of ownership.
    backend, script, _, _ = _session(monkeypatch, after=[
        (GP_EVENT_FILE_ADDED, _file_path(name="UNSEEN_OLD.JPG"), 1)])
    backend.capture_image(tmp_path / "page.jpg", _config())
    assert script.deleted[0][1] == "UNSEEN_OLD.JPG"


def test_pinned_body_with_unreadable_serial_cannot_bypass_move_check(rig):
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend.list_devices()
    rig.script.settings.pop("serialnumber")
    with pytest.raises(gb.CameraIdentityError, match="identity"):
        backend._get_or_open_session(0)
    assert not backend._claimed_ports


def test_journal_handler_receives_wait_info(capsys):
    from app.core.logging_setup import configure_logging
    configure_logging(level="INFO")
    with gb._wait_log(PORT, "busy capture events"):
        pass
    assert "reason=busy capture events duration=" in capsys.readouterr().err


def test_sysfs_identity_tracks_socket_address_and_directory_lifetime(monkeypatch, tmp_path):
    monkeypatch.setattr(gb, "_SYSFS_USB_DEVICES", str(tmp_path))
    entry = tmp_path / "1-1"

    def make(dev):
        entry.mkdir()
        for field, value in {"busnum": "1", "devnum": str(dev), "idVendor": "04a9", "idProduct": "32e1"}.items():
            (entry / field).write_text(value)

    make(5)
    seen = gb._usb_attachment(PORT)
    assert seen == ("1-1", 5, entry.stat().st_ino, 0x04a9, 0x32e1)
    assert gb._port_at_socket("1-1") == PORT
    entry.rename(tmp_path / "departed")
    make(9)
    assert gb._usb_attachment(PORT) is None
    current = gb._usb_attachment("usb:001,009")
    assert current[0] == seen[0] and current[2] != seen[2]
    assert gb._port_at_socket("1-1") == "usb:001,009"


def test_usb_permission_failure_keeps_original_init_error(rig, monkeypatch):
    rig.wedged[PORT] = -10
    monkeypatch.setattr(gb, "_usb_open", lambda *a: (_ for _ in ()).throw(PermissionError("denied")))
    with pytest.raises(rig.gp.GPhoto2Error, match="-10"):
        gb._init_camera(MODEL, PORT, rig.attachment(PORT), True)
    assert not rig.resets


def test_same_listing_never_resets_twice_after_learning_other_body(rig, monkeypatch):
    other = "usb:003,002"
    rig.ports[other] = ("3-1", 2, 456)
    rig.wedged[PORT] = -10
    monkeypatch.setattr(gb, "_usb_ioctl", lambda *a: rig.resets.append(a[0]))
    backend = gb.GPhoto2Backend(logging.getLogger("test"))
    backend._pins = {0: "A", 1: "B"}
    original = rig.gp.Camera.get_config

    def config(cam):
        rig.script.settings["serialnumber"] = "A" if cam.port == PORT else "B"
        return original(cam)

    monkeypatch.setattr(rig.gp.Camera, "get_config", config)
    backend.list_devices()
    assert rig.resets == ["/dev/bus/usb/001/005"]


def test_slow_startup_event_aborts_before_trigger(monkeypatch, tmp_path):
    backend, script, _, clock = _session(monkeypatch,
        before=[(GP_EVENT_FILE_ADDED, _file_path(name="SLOW.CR2"), 6)])
    with pytest.raises(RuntimeError, match="startup"):
        backend.capture_image(tmp_path / "page.jpg", _config())
    assert not script.saved_to
    assert not script.deleted and script.trigger_calls == 0
    assert clock.now == 1006
