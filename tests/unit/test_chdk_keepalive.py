"""The idle keep-alive: an idle CHDK body is sent one read per interval.

On 14 November 2026 an A2500 under CHDK, switched on in playback mode with
Canon's "Auto Power Down" on and sent nothing over USB, left USB five minutes
after power-on, twice. The same body sent one trivial Lua read a minute was
still answering after twelve minutes. So while the backend runs, every open
body that has had no traffic for _KEEPALIVE_INTERVAL_S is sent one read, which
switches no mode, sends no enter_alt() and changes no setting.

Version: 0.1.0-beta
"""

import logging
import threading

import pytest

import capture.backends.chdk_backend as cb
from capture.camera import CameraConfig

from .chdk_fakes import (
    Body, BusyLock, ScriptedStop, TransportError, make_backend, make_pychdk,
)


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"
ODD_CARD = b"ODD\nid=bbbbbbbbbbbb\n"
JPEG = b"\xff\xd8\xff\xe0 a page \xff\xd9"
LOGGER = "test-chdk"


def _backend(monkeypatch, *bodies):
    fake = make_pychdk(*bodies)
    backend = make_backend(monkeypatch, fake, logger_name=LOGGER)
    backend.list_devices()
    return backend, fake


def _reads(body):
    return body.lua_calls.count(cb._KEEPALIVE_LUA)


def _keepalive_warnings(caplog):
    return [
        record for record in caplog.records
        if record.name == LOGGER
        and record.levelno == logging.WARNING
        and "keep-alive" in record.getMessage()
    ]


@pytest.mark.unit
def test_the_interval_is_a_minute():
    assert cb._KEEPALIVE_INTERVAL_S == 60.0


@pytest.mark.unit
def test_the_backend_starts_one_keepalive_thread(monkeypatch):
    fake = make_pychdk(Body(card=EVEN_CARD))
    backend = make_backend(monkeypatch, fake)

    assert len(fake.threads) == 1
    assert fake.threads[0].target == backend._keepalive_loop


@pytest.mark.unit
def test_an_idle_open_body_gets_one_read_per_interval(monkeypatch):
    body = Body(card=EVEN_CARD)
    backend, fake = _backend(monkeypatch, body)

    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()
    assert _reads(body) == 1

    # The read is traffic: a tick straight after it sends nothing.
    backend._keepalive_tick()
    assert _reads(body) == 1

    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()
    assert _reads(body) == 2
    assert backend._body_at(0) is not None


@pytest.mark.unit
def test_a_body_opened_less_than_an_interval_ago_gets_none(monkeypatch):
    body = Body(card=EVEN_CARD)
    backend, fake = _backend(monkeypatch, body)

    fake.advance(cb._KEEPALIVE_INTERVAL_S - 1)
    backend._keepalive_tick()

    assert _reads(body) == 0


@pytest.mark.unit
def test_a_capture_counts_as_traffic(monkeypatch, tmp_path):
    body = Body(card=EVEN_CARD, image=JPEG)
    backend, fake = _backend(monkeypatch, body)

    fake.advance(cb._KEEPALIVE_INTERVAL_S * 3)
    backend.capture_image(tmp_path / "one.jpg", CameraConfig(camera_index=0))
    fake.advance(cb._KEEPALIVE_INTERVAL_S - 1)
    backend._keepalive_tick()

    assert _reads(body) == 0
    fake.advance(1)
    backend._keepalive_tick()
    assert _reads(body) == 1


@pytest.mark.unit
def test_a_zoom_request_counts_as_traffic(monkeypatch):
    body = Body(card=EVEN_CARD, zoom_steps=64)
    backend, fake = _backend(monkeypatch, body)

    fake.advance(cb._KEEPALIVE_INTERVAL_S * 3)
    backend.set_zoom_step(0, 10)
    fake.advance(cb._KEEPALIVE_INTERVAL_S - 1)
    backend._keepalive_tick()

    assert _reads(body) == 0


@pytest.mark.unit
def test_the_read_switches_no_mode_and_sends_no_enter_alt(monkeypatch):
    body = Body(card=EVEN_CARD, zoom_steps=64)
    backend, fake = _backend(monkeypatch, body)
    opened = backend._body_at(0)

    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()

    assert body.lua_calls == [cb._KEEPALIVE_LUA]
    assert [s["script"] for s in body.scripts] == [cb._KEEPALIVE_LUA]
    assert body.scripts[0]["flags"] == cb._SCRIPT_NOKILL
    assert body.mode_switches == []
    assert body.mode_value is False
    assert body.half_presses == 0
    assert body.shots == []
    assert body.frames_served == 0
    assert body.uploads == []
    assert opened.in_record_mode is False
    assert opened.alt_asserted_at is None
    # The answer was read: nothing is left for the next script.
    assert body.messages == []


@pytest.mark.unit
def test_the_read_does_not_touch_a_body_in_record_mode(monkeypatch, tmp_path):
    body = Body(card=EVEN_CARD, image=JPEG)
    backend, fake = _backend(monkeypatch, body)
    backend.capture_image(tmp_path / "one.jpg", CameraConfig(camera_index=0))
    before = list(body.lua_calls)
    switches = list(body.mode_switches)
    alt_at = backend._body_at(0).alt_asserted_at

    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()

    assert body.lua_calls == before + [cb._KEEPALIVE_LUA]
    assert body.mode_switches == switches
    assert backend._body_at(0).in_record_mode is True
    assert backend._body_at(0).alt_asserted_at == alt_at


@pytest.mark.unit
def test_a_body_whose_lock_is_held_is_skipped_without_waiting(monkeypatch):
    busy = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD)
    idle = Body(bus=1, address=5, serial="BBB222", card=ODD_CARD)
    backend, fake = _backend(monkeypatch, busy, idle)
    held = BusyLock()
    backend._body_at(0).lock = held

    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()

    assert held.attempts == [False]
    assert _reads(busy) == 0
    assert _reads(idle) == 1
    assert backend._body_at(0) is not None


@pytest.mark.unit
def test_a_failing_read_closes_the_body_once_and_is_not_retried(
    monkeypatch, caplog
):
    failing = Body(
        bus=1, address=4, serial="AAA111", card=EVEN_CARD,
        lua_error=TransportError("pipe error"),
    )
    healthy = Body(bus=1, address=5, serial="BBB222", card=ODD_CARD)
    backend, fake = _backend(monkeypatch, failing, healthy)
    lists = fake.list_calls
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()

    assert len(failing.scripts) == 1
    assert failing.closes == 1
    assert failing.key not in backend._bodies
    assert failing.key in backend._evicted
    assert _reads(healthy) == 1
    warnings = _keepalive_warnings(caplog)
    assert len(warnings) == 1
    assert "usb:001,004" in warnings[0].getMessage()

    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()

    assert len(failing.scripts) == 1
    assert failing.opens == 1
    assert fake.list_calls == lists
    assert len(_keepalive_warnings(caplog)) == 1
    assert _reads(healthy) == 2


@pytest.mark.unit
def test_a_read_that_times_out_sends_nothing_more(monkeypatch, caplog):
    body = Body(card=EVEN_CARD, script_reads={"get_mode": 100_000})
    backend, fake = _backend(monkeypatch, body)
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()

    assert [s["script"] for s in body.scripts] == [cb._KEEPALIVE_LUA]
    assert body.closes == 1
    assert backend._body_at(0) is None
    assert len(_keepalive_warnings(caplog)) == 1
    status_reads = body.status_reads

    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()

    assert body.status_reads == status_reads
    assert len(body.scripts) == 1


@pytest.mark.unit
def test_the_tick_does_not_enumerate_when_every_attached_body_is_open(
    monkeypatch,
):
    body = Body(card=EVEN_CARD)
    backend, fake = _backend(monkeypatch, body)
    lists = fake.list_calls
    indices = dict(backend._indices)

    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()

    assert fake.list_calls == lists
    assert backend._indices == indices
    assert body.opens == 1


@pytest.mark.unit
def test_nothing_is_sent_once_stop_is_asked_for(monkeypatch):
    body = Body(card=EVEN_CARD)
    backend, fake = _backend(monkeypatch, body)

    backend._keepalive_stop.set()
    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()

    assert _reads(body) == 0


@pytest.mark.unit
def test_the_loop_ticks_once_per_interval_until_stopped(monkeypatch):
    fake = make_pychdk(Body(card=EVEN_CARD))
    backend = make_backend(monkeypatch, fake)
    ticks = []
    monkeypatch.setattr(backend, "_keepalive_tick", lambda: ticks.append(1))
    backend._keepalive_stop = ScriptedStop(ticks=3)

    backend._keepalive_loop()

    assert len(ticks) == 3
    assert backend._keepalive_stop.waits == [cb._KEEPALIVE_INTERVAL_S] * 4


@pytest.mark.unit
def test_an_exception_in_one_tick_does_not_end_the_loop(monkeypatch, caplog):
    fake = make_pychdk(Body(card=EVEN_CARD))
    backend = make_backend(monkeypatch, fake, logger_name=LOGGER)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    ticks = []

    def tick():
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("the map changed under the tick")

    monkeypatch.setattr(backend, "_keepalive_tick", tick)
    backend._keepalive_stop = ScriptedStop(ticks=2)

    backend._keepalive_loop()

    assert len(ticks) == 2
    assert any(
        "the map changed under the tick" in record.getMessage()
        for record in caplog.records if record.name == LOGGER
    )


@pytest.mark.unit
def test_cleanup_stops_and_joins_the_thread(monkeypatch):
    fake = make_pychdk(Body(card=EVEN_CARD))
    backend = make_backend(monkeypatch, fake)
    backend.list_devices()

    backend.cleanup()

    assert backend._keepalive_stop.is_set()
    assert fake.threads[0].joins == [cb._KEEPALIVE_JOIN_S]


@pytest.mark.unit
@pytest.mark.real_chdk_thread
def test_cleanup_ends_a_real_keepalive_thread(monkeypatch):
    real_start = cb._start_thread
    fake = make_pychdk(Body(card=EVEN_CARD))
    backend = make_backend(monkeypatch, fake)
    # Built again with the real start, so a real thread runs; it is waiting
    # on its stop event, which cleanup sets.
    monkeypatch.setattr(cb, "_start_thread", real_start)
    backend = cb.ChdkBackend(logging.getLogger(LOGGER))
    backend._registry_factory = lambda: None
    thread = backend._keepalive_thread
    assert thread.is_alive()
    assert thread.daemon

    backend.cleanup()

    assert not thread.is_alive()


# --- attached bodies that were never opened ------------------------------

@pytest.mark.unit
def test_an_unopened_body_is_opened_once_every_open_body_is_idle(
    monkeypatch, caplog
):
    first = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD)
    backend, fake = _backend(monkeypatch, first)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    late = Body(bus=1, address=5, serial="BBB222", card=ODD_CARD)
    fake.bodies.append(late)
    lists = fake.list_calls

    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()

    assert fake.list_calls == lists + 1
    assert late.opens == 1
    assert late.key in backend._bodies
    assert backend._body_at(1) is not None
    assert any(
        record.levelno == logging.INFO
        and "usb:001,005" in record.getMessage()
        and "keep-alive" in record.getMessage()
        for record in caplog.records if record.name == LOGGER
    )

    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()
    assert fake.list_calls == lists + 1
    assert _reads(late) == 1


@pytest.mark.unit
def test_no_scan_while_an_open_body_is_locked_and_the_rest_are_read(monkeypatch):
    busy = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD)
    idle = Body(bus=1, address=5, serial="BBB222", card=ODD_CARD)
    backend, fake = _backend(monkeypatch, busy, idle)
    late = Body(bus=1, address=6, serial="CCC333", card=EVEN_CARD)
    fake.bodies.append(late)
    held = BusyLock()
    backend._body_at(0).lock = held
    lists = fake.list_calls

    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()

    assert fake.list_calls == lists
    assert late.open_attempts == 0
    assert late.key not in backend._keepalive_tried
    assert all(blocking is False for blocking in held.attempts)
    assert _reads(idle) == 1
    assert _reads(busy) == 0

    # The lock is free again: the next tick scans.
    backend._body_at(0).lock = cb.threading.RLock()
    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()
    assert fake.list_calls == lists + 1
    assert late.opens == 1


@pytest.mark.unit
def test_a_body_a_capture_evicted_is_not_reopened_by_the_keepalive(
    monkeypatch, tmp_path
):
    body = Body(card=EVEN_CARD, shoot_error=TransportError("pipe error"))
    backend, fake = _backend(monkeypatch, body)
    lists = fake.list_calls

    with pytest.raises(RuntimeError):
        backend.capture_image(tmp_path / "one.jpg", CameraConfig(camera_index=0))
    assert body.key not in backend._bodies
    assert body.key in backend._keepalive_tried

    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()
    assert fake.list_calls == lists
    assert body.opens == 1

    # The operator's own listing still opens it.
    backend.list_devices()
    assert body.opens == 2


@pytest.mark.unit
def test_an_unopened_body_is_opened_when_no_body_is_open(monkeypatch):
    fake = make_pychdk()
    backend = make_backend(monkeypatch, fake, logger_name=LOGGER)
    body = Body(card=EVEN_CARD)
    fake.bodies.append(body)

    backend._keepalive_tick()

    assert fake.list_calls == 1
    assert body.key in backend._bodies


@pytest.mark.unit
def test_no_scan_while_an_open_body_has_recent_traffic(monkeypatch, tmp_path):
    first = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend, fake = _backend(monkeypatch, first)
    late = Body(bus=1, address=5, serial="BBB222", card=ODD_CARD)
    fake.bodies.append(late)
    lists = fake.list_calls

    fake.advance(cb._KEEPALIVE_INTERVAL_S * 3)
    backend.capture_image(tmp_path / "one.jpg", CameraConfig(camera_index=0))
    fake.advance(cb._KEEPALIVE_INTERVAL_S - 1)
    backend._keepalive_tick()

    assert fake.list_calls == lists
    assert late.open_attempts == 0

    # Not marked as tried: once the scanner is idle, it is opened.
    fake.advance(1)
    backend._keepalive_tick()
    assert fake.list_calls == lists + 1
    assert late.opens == 1


@pytest.mark.unit
def test_a_body_that_failed_to_open_is_not_tried_again(monkeypatch):
    fake = make_pychdk()
    backend = make_backend(monkeypatch, fake, logger_name=LOGGER)
    body = Body(card=EVEN_CARD, open_error=TransportError("timed out"))
    fake.bodies.append(body)

    backend._keepalive_tick()
    assert body.open_attempts == 1
    assert fake.list_calls == 1

    for _ in range(3):
        fake.advance(cb._KEEPALIVE_INTERVAL_S)
        backend._keepalive_tick()

    assert body.open_attempts == 1
    assert fake.list_calls == 1


@pytest.mark.unit
def test_a_body_back_at_a_new_device_number_is_tried_again(monkeypatch):
    fake = make_pychdk()
    backend = make_backend(monkeypatch, fake, logger_name=LOGGER)
    body = Body(address=4, card=EVEN_CARD, open_error=TransportError("timed out"))
    fake.bodies.append(body)
    backend._keepalive_tick()
    assert body.open_attempts == 1

    # It leaves the bus and comes back at another device number.
    body.address = 9
    body.open_error = None
    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()

    assert body.open_attempts == 2
    assert fake.list_calls == 2
    assert (1, 9) in backend._bodies


@pytest.mark.unit
def test_the_tried_set_forgets_devices_that_left_the_bus(monkeypatch):
    fake = make_pychdk()
    backend = make_backend(monkeypatch, fake, logger_name=LOGGER)
    body = Body(card=EVEN_CARD, open_error=TransportError("timed out"))
    fake.bodies.append(body)
    backend._keepalive_tick()
    assert backend._keepalive_tried == {body.key}

    fake.bodies.remove(body)
    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()

    assert backend._keepalive_tried == set()


@pytest.mark.unit
def test_the_presence_scan_sends_nothing_to_open_bodies(monkeypatch, tmp_path):
    first = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend, fake = _backend(monkeypatch, first)
    fake.bodies.append(Body(bus=1, address=5, serial="BBB222", card=ODD_CARD))
    backend.capture_image(tmp_path / "one.jpg", CameraConfig(camera_index=0))
    lists = fake.list_calls
    before = (
        list(first.scripts), first.version_calls, first.status_reads,
        first.opens, first.frames_served,
    )

    # The open body has just been used, so nothing is read and nothing
    # enumerated: what is left is the presence scan alone.
    backend._keepalive_tick()

    assert fake.presence_calls == 1
    assert fake.list_calls == lists
    assert (
        list(first.scripts), first.version_calls, first.status_reads,
        first.opens, first.frames_served,
    ) == before


@pytest.mark.unit
def test_a_failing_presence_scan_does_not_stop_the_reads(monkeypatch, caplog):
    body = Body(card=EVEN_CARD)
    backend, fake = _backend(monkeypatch, body)
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    def broken():
        raise OSError("libusb is not answering")

    backend._presence_scan = broken
    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()

    assert _reads(body) == 1
    assert any(
        "libusb is not answering" in record.getMessage()
        for record in caplog.records if record.name == LOGGER
    )


@pytest.mark.unit
def test_no_scan_once_stop_is_asked_for(monkeypatch):
    fake = make_pychdk()
    backend = make_backend(monkeypatch, fake, logger_name=LOGGER)
    fake.bodies.append(Body(card=EVEN_CARD))
    backend._keepalive_stop.set()

    backend._keepalive_tick()

    assert fake.list_calls == 0


# --- start-up ------------------------------------------------------------

class _StartupBackend:
    def __init__(self, fails=False):
        self.listings = 0
        self.fails = fails
        self.listed = threading.Event()

    def list_devices(self):
        self.listings += 1
        self.listed.set()
        if self.fails:
            raise RuntimeError("no camera answered")
        return []


def _stub_startup(monkeypatch, backend_name, get_backend):
    """Stub everything in the lifespan but the camera start-up."""
    import app.core.db as core_db
    import app.core.device_id as device_id
    import app.core.storage_ops as storage_ops
    import app.core.unit_mode as unit_mode
    from app import main as app_main
    from app.core import restart_latch
    from capture import service as capture_service

    restart_latch._reset_for_tests()
    monkeypatch.setattr(app_main, "init_db", lambda: None)
    monkeypatch.setattr(app_main, "assert_schema_at_head", lambda: None)
    monkeypatch.setattr(
        core_db, "SessionLocal",
        lambda: type("S", (), {"close": lambda self: None})(),
    )
    monkeypatch.setattr(storage_ops, "reconcile_pending_rename", lambda db: None)
    monkeypatch.setattr(unit_mode, "initialise", lambda: None)
    monkeypatch.setattr(device_id, "initialise", lambda: None)
    monkeypatch.setattr(capture_service, "shutdown_backend", lambda: None)
    monkeypatch.setattr(capture_service.settings, "CAMERA_BACKEND", backend_name)
    monkeypatch.setattr(capture_service, "get_backend", get_backend)
    return app_main


def _serve(app_main, reached=None):
    """Run the lifespan; with `reached`, stay in it until that event is set."""
    import asyncio

    from app.core import restart_latch

    served = []

    async def serve():
        async with app_main.app.router.lifespan_context(app_main.app):
            served.append(1)
            if reached is not None:
                assert reached.wait(5), "the start-up scan never got there"

    try:
        asyncio.run(serve())
    finally:
        restart_latch._reset_for_tests()
    return served


@pytest.mark.unit
def test_startup_builds_the_chdk_backend_and_enumerates_once(monkeypatch):
    backend = _StartupBackend()
    built = []

    def get_backend():
        built.append(1)
        return backend

    app_main = _stub_startup(monkeypatch, "chdk", get_backend)

    assert _serve(app_main, backend.listed) == [1]
    assert built == [1]
    assert backend.listings == 1


@pytest.mark.unit
@pytest.mark.parametrize("name", ["gphoto2", "picamera2", "subprocess"])
def test_startup_does_nothing_for_other_backends(monkeypatch, name):
    built = []
    app_main = _stub_startup(
        monkeypatch, name, lambda: built.append(1) or _StartupBackend()
    )

    assert _serve(app_main) == [1]
    assert built == []


@pytest.mark.unit
@pytest.mark.parametrize("where", ["building", "enumerating"])
def test_a_startup_failure_is_a_warning_and_start_up_goes_on(
    monkeypatch, caplog, where
):
    asked = threading.Event()

    def get_backend():
        asked.set()
        if where == "building":
            raise RuntimeError("pychdk is not installed")
        return _StartupBackend(fails=True)

    app_main = _stub_startup(monkeypatch, "chdk", get_backend)
    caplog.set_level(logging.DEBUG)

    assert _serve(app_main, asked) == [1]
    warnings = [
        record for record in caplog.records
        if record.levelno == logging.WARNING
        and "start-up" in record.getMessage()
    ]
    assert len(warnings) == 1


@pytest.mark.unit
def test_no_backend_is_built_once_shutdown_has_begun(monkeypatch):
    from app import main as app_main
    from app.core import restart_latch

    built = []
    _stub_startup(monkeypatch, "chdk", lambda: built.append(1))
    restart_latch.begin_shutdown()
    try:
        thread = app_main._start_camera_startup_scan("chdk")
        thread.join(5)
    finally:
        restart_latch._reset_for_tests()

    assert not thread.is_alive()
    assert built == []


@pytest.mark.unit
def test_shutdown_waits_for_a_scan_in_progress_before_closing(monkeypatch):
    from app.core import restart_latch
    from capture import service as capture_service

    order = []
    entered = threading.Event()

    class Slow:
        def list_devices(self):
            entered.set()
            # Still enumerating when the shutdown begins.
            for _ in range(500):
                if restart_latch.shutting_down():
                    break
                threading.Event().wait(0.01)
            order.append("listed")
            return []

    app_main = _stub_startup(monkeypatch, "chdk", lambda: Slow())
    monkeypatch.setattr(
        capture_service, "shutdown_backend", lambda: order.append("closed")
    )

    assert _serve(app_main, entered) == [1]
    assert order == ["listed", "closed"]


@pytest.mark.unit
def test_a_backend_built_outside_make_backend_starts_no_real_thread(monkeypatch):
    """tests/conftest.py replaces _start_thread for every test not marked."""
    monkeypatch.setattr(cb, "pychdk", make_pychdk(Body()))
    monkeypatch.setattr(cb, "_PYCHDK_AVAILABLE", True)
    before = {t.name for t in threading.enumerate()}

    backend = cb.ChdkBackend(logging.getLogger(LOGGER))

    assert not isinstance(backend._keepalive_thread, threading.Thread)
    assert "chdk-keepalive" not in (
        {t.name for t in threading.enumerate()} - before
    )


# --- the rule: no tried, unopened address is opened by the keep-alive ------

@pytest.mark.unit
def test_a_new_body_is_opened_without_reopening_a_failed_one(monkeypatch):
    fake = make_pychdk()
    backend = make_backend(monkeypatch, fake, logger_name=LOGGER)
    failed = Body(
        bus=1, address=4, serial="AAA111", card=EVEN_CARD,
        open_error=TransportError("timed out"),
    )
    fake.bodies.append(failed)
    backend._keepalive_tick()
    assert failed.open_attempts == 1
    failed.open_error = None

    late = Body(bus=1, address=5, serial="BBB222", card=ODD_CARD)
    fake.bodies.append(late)
    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()

    assert late.key in backend._bodies
    assert failed.open_attempts == 1
    assert failed.key not in backend._bodies

    # The operator's own listing still opens it.
    backend.list_devices()
    assert failed.open_attempts == 2
    assert failed.key in backend._bodies


@pytest.mark.unit
def test_a_tick_opens_no_tried_unopened_address(monkeypatch, tmp_path):
    healthy = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD)
    evicted = Body(
        bus=1, address=5, serial="BBB222", card=ODD_CARD,
        shoot_error=TransportError("pipe error"),
    )
    relapsing = Body(bus=1, address=7, serial="DDD444", card=None)
    backend, fake = _backend(monkeypatch, healthy, evicted, relapsing)
    # One that was evicted, reopened by a listing, and stops answering at
    # the next enumeration's check.
    backend._evict(backend._bodies[relapsing.key], "test")
    backend.list_devices()
    assert relapsing.key in backend._bodies
    # One body evicted by a failed capture and still attached.
    assert backend._body_at(1) is backend._bodies[evicted.key]
    with pytest.raises(RuntimeError):
        backend.capture_image(tmp_path / "one.jpg", CameraConfig(camera_index=1))
    assert evicted.key not in backend._bodies
    # One that never opened.
    unopenable = Body(
        bus=1, address=6, serial="CCC333", card=EVEN_CARD,
        open_error=TransportError("timed out"),
    )
    fake.bodies.append(unopenable)
    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()
    assert unopenable.open_attempts == 1
    assert evicted.key not in backend._bodies
    relapsing.version_error = TransportError("gone quiet")
    # And one that is new.
    new = Body(bus=1, address=8, serial="EEE555", card=EVEN_CARD)
    fake.bodies.append(new)
    for body in (evicted, unopenable):
        body.open_error = None
    attempts = {
        body.key: body.open_attempts
        for body in (evicted, unopenable, relapsing)
    }
    tried = set(backend._keepalive_tried)
    assert {evicted.key, unopenable.key, relapsing.key} <= tried

    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()

    assert new.key in backend._bodies
    assert healthy.key in backend._bodies
    for body in (evicted, unopenable, relapsing):
        assert body.open_attempts == attempts[body.key], body.key
        assert body.key not in backend._bodies, body.key


# --- the positive form: the keep-alive opens only what it chose ------------

@pytest.mark.unit
def test_a_body_evicted_after_the_tick_chose_is_left_closed(
    monkeypatch, tmp_path
):
    open_body = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD)
    backend, fake = _backend(monkeypatch, open_body)
    new = Body(bus=1, address=5, serial="BBB222", card=ODD_CARD)
    fake.bodies.append(new)
    scan = fake.list_devices

    def evict_then_scan():
        # A request fails on the open body after the tick has chosen the
        # addresses to open and before the enumeration opens anything.
        body = backend._bodies.get(open_body.key)
        if body is not None:
            backend._evict(body, "a capture failed")
        return scan()

    monkeypatch.setattr(fake, "list_devices", evict_then_scan)
    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()

    assert new.key in backend._bodies
    assert open_body.key not in backend._bodies
    assert open_body.open_attempts == 1


@pytest.mark.unit
def test_a_chosen_address_opened_and_evicted_meanwhile_is_left_closed(
    monkeypatch,
):
    open_body = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD)
    backend, fake = _backend(monkeypatch, open_body)
    new = Body(bus=1, address=5, serial="BBB222", card=ODD_CARD)
    fake.bodies.append(new)
    scan = fake.list_devices
    nested = []

    def listing_and_eviction_then_scan():
        # Between the tick's choice and its open: an operator's listing
        # opens the new body, and a failed request evicts it.
        if not nested:
            nested.append(1)
            backend.list_devices()
            backend._evict(backend._bodies[new.key], "a preview failed")
        return scan()

    monkeypatch.setattr(fake, "list_devices", listing_and_eviction_then_scan)
    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()

    assert new.open_attempts == 1
    assert new.key not in backend._bodies


@pytest.mark.unit
def test_an_enumeration_given_only_opens_nothing_outside_it(monkeypatch):
    fake = make_pychdk()
    backend = make_backend(monkeypatch, fake, logger_name=LOGGER)
    chosen = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD)
    other = Body(bus=1, address=5, serial="BBB222", card=ODD_CARD)
    third = Body(bus=1, address=6, serial="CCC333", card=None)
    fake.bodies.extend([chosen, other, third])

    backend._enumerate(reread=False, only=frozenset({chosen.key}))

    assert set(backend._bodies) == {chosen.key}
    assert other.open_attempts == 0
    assert third.open_attempts == 0


@pytest.mark.unit
def test_a_body_that_stops_answering_is_not_reopened_on_later_ticks(monkeypatch):
    quiet = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD)
    backend, fake = _backend(monkeypatch, quiet)
    fake.bodies.append(Body(bus=1, address=5, serial="BBB222", card=ODD_CARD))
    quiet.version_error = TransportError("gone quiet")

    # The tick's enumeration finds it not answering and closes it.
    fake.advance(cb._KEEPALIVE_INTERVAL_S)
    backend._keepalive_tick()
    assert quiet.key not in backend._bodies
    quiet.version_error = None

    for _ in range(2):
        fake.advance(cb._KEEPALIVE_INTERVAL_S)
        backend._keepalive_tick()

    assert quiet.open_attempts == 1
    assert quiet.key not in backend._bodies
