"""The capture backend has to be closed when the application stops.

The CHDK and DSLR backends hold PTP claims on the bodies for the life of the
process, and their cleanup is what waits for whatever is running and releases
them. Nothing in production called it: the lifespan had startup work and a
bare yield, so on a restart the sessions were dropped rather than closed, and
the lifecycle the backend documents was a promise nothing kept.
"""

import pytest

import capture.service as capture_service


class _Backend:
    def __init__(self, fails=False):
        self.cleanups = 0
        self._fails = fails

    def get_backend_name(self):
        return "fake"

    def cleanup(self):
        self.cleanups += 1
        if self._fails:
            raise RuntimeError("the bus went away mid-shutdown")


@pytest.fixture(autouse=True)
def no_leftover_backend(monkeypatch):
    """Never touch the real process singleton from here."""
    monkeypatch.setattr(capture_service, "_backend", None)


@pytest.mark.unit
def test_shutdown_closes_the_backend_that_was_built(monkeypatch):
    backend = _Backend()
    monkeypatch.setattr(capture_service, "_backend", backend)

    capture_service.shutdown_backend()

    assert backend.cleanups == 1
    assert capture_service._backend is None, "a closed backend was left in place"


@pytest.mark.unit
def test_shutdown_does_not_build_a_backend_just_to_close_it(monkeypatch):
    built = []
    monkeypatch.setattr(
        capture_service, "get_camera_backend", lambda: built.append(1)
    )

    capture_service.shutdown_backend()

    assert built == [], "shutdown opened the cameras in order to close them"


@pytest.mark.unit
def test_a_failing_cleanup_does_not_fail_the_shutdown(monkeypatch):
    backend = _Backend(fails=True)
    monkeypatch.setattr(capture_service, "_backend", backend)

    capture_service.shutdown_backend()

    assert backend.cleanups == 1
    assert capture_service._backend is None


@pytest.mark.unit
def test_the_next_call_builds_a_fresh_backend(monkeypatch):
    monkeypatch.setattr(capture_service, "_backend", _Backend())
    replacement = _Backend()
    monkeypatch.setattr(capture_service, "get_camera_backend", lambda: replacement)

    capture_service.shutdown_backend()

    assert capture_service.get_backend() is replacement


@pytest.mark.unit
def test_the_application_lifespan_closes_the_backend_on_the_way_out(monkeypatch):
    """The wiring itself: without this the method above is never called."""
    import asyncio

    import app.core.db as core_db
    import app.core.storage_ops as storage_ops
    from app import main as app_main

    monkeypatch.setattr(app_main, "init_db", lambda: None)
    monkeypatch.setattr(app_main, "assert_schema_at_head", lambda: None)
    monkeypatch.setattr(
        core_db, "SessionLocal", lambda: type("S", (), {"close": lambda self: None})()
    )
    monkeypatch.setattr(storage_ops, "reconcile_pending_rename", lambda db: None)

    closed = []
    monkeypatch.setattr(
        capture_service, "shutdown_backend", lambda: closed.append(1)
    )

    async def serve():
        async with app_main.lifespan(app_main.app):
            assert closed == [], "the backend was closed before serving"

    asyncio.run(serve())

    assert closed == [1], "the lifespan never closed the camera backend"
