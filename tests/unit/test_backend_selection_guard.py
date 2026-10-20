#!/usr/bin/env python3
"""
Tests for the CAMERA_BACKEND guard (NEH-268).

An unrecognised value used to log "Unknown backend '<value>', defaulting to
subprocess" and return RpicamBackend -- a third backend that is neither the one
configured nor the one intended. On a DSLR rig that yields no cameras and
presents as a hardware fault, so a typo in one line of .env was
indistinguishable from a dead camera.

Two things have to hold, and the second is the one that was easy to miss: the
value is refused, AND it is refused at startup. The backend is built lazily on
the first capture, so a guard that lived only in the dispatch would let the
service start, answer /health, and fail at the moment someone tried to
photograph something.

This file is deliberately not named for the subsystem it tests.
tests/conftest.py adds @pytest.mark.camera to any test whose NODE ID contains
"camera", and CI runs `pytest -m "not camera"` -- so a file or a test named for
the camera backend would be silently deselected and prove nothing.

Run with: python -m pytest tests/unit/test_backend_selection_guard.py
"""

import pytest

from capture import service as capture_service

# The settings instance is patched through `capture_service.settings` rather
# than through `app.core.config.settings`, and the difference is not cosmetic.
# conftest.py's `set_camera_backend` fixture calls `importlib.reload(config)`,
# which rebinds `app.core.config.settings` to a NEW Settings object while every
# module that did `from app.core.config import settings` -- capture.service
# among them -- goes on holding the old one. Patching the name in config then
# changes an object nobody reads. The first version of this file did exactly
# that and all fourteen cases failed by reading the default.


@pytest.mark.unit
def test_the_registry_is_the_only_list_of_names():
    """The dispatch and the published tuple cannot disagree, by construction.

    The silent fallback survived because the names were enumerated in one place
    and "everything else" was handled in another. One mapping now feeds both.
    """
    assert capture_service.KNOWN_CAMERA_BACKENDS == tuple(
        sorted(capture_service._BACKEND_CONSTRUCTORS)
    )
    assert set(capture_service.KNOWN_CAMERA_BACKENDS) == {
        "picamera2", "subprocess", "gphoto2", "chdk"
    }
    # Four distinct classes: a mapping that pointed two names at one backend
    # would reintroduce the defect while passing a membership check.
    assert len(set(capture_service._BACKEND_CONSTRUCTORS.values())) == 4


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    ["picamera2", "subprocess", "gphoto2", "chdk"],
    # Explicit ids because the VALUES go into the node id, and "picamera2"
    # contains "camera" -- which conftest.py auto-marks and CI deselects. The
    # first version of this file lost exactly this case that way: 17 cases were
    # written, 16 ran, and only the deselected count showed it.
    ids=["pi_still_port", "subprocess", "gphoto2", "chdk"],
)
def test_every_known_value_is_accepted(monkeypatch, value):
    monkeypatch.setattr(capture_service.settings, "CAMERA_BACKEND", value)
    assert capture_service.assert_camera_backend_configured() == value


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [" gphoto2 ", "GPhoto2", "\tCHDK\n"],
    ids=["surrounding_spaces", "mixed_case", "tab_and_newline"],
)
def test_case_and_surrounding_whitespace_are_normalised(monkeypatch, value):
    """A trailing space in .env is a typo that should not stop a unit."""
    monkeypatch.setattr(capture_service.settings, "CAMERA_BACKEND", value)
    assert capture_service.assert_camera_backend_configured() == value.strip().lower()


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    ["gphoto", "gphoto3", "libgphoto2", "", "   ", "none"],
    ids=["near_miss", "wrong_version", "library_name", "empty", "whitespace", "none"],
)
def test_an_unrecognised_value_is_refused(monkeypatch, value):
    monkeypatch.setattr(capture_service.settings, "CAMERA_BACKEND", value)
    with pytest.raises(capture_service.UnknownCameraBackend) as exc:
        capture_service.assert_camera_backend_configured()
    message = str(exc.value)
    # The operator has to be able to see what they typed and what is valid.
    assert repr(value) in message
    for known in capture_service.KNOWN_CAMERA_BACKENDS:
        assert known in message


@pytest.mark.unit
def test_an_unrecognised_value_builds_nothing_at_all(monkeypatch):
    """The point of the issue: no fallback instance comes back.

    A test that only checked for a raised exception would still pass if the
    dispatch logged and returned RpicamBackend, so this asserts that no
    constructor in the registry is reached.
    """
    monkeypatch.setattr(capture_service.settings, "CAMERA_BACKEND", "rpicam")
    built = []
    for name, cls in capture_service._BACKEND_CONSTRUCTORS.items():
        monkeypatch.setitem(
            capture_service._BACKEND_CONSTRUCTORS,
            name,
            lambda *a, _n=name, **k: built.append(_n),
        )
    with pytest.raises(capture_service.UnknownCameraBackend):
        capture_service.get_camera_backend()
    assert built == [], f"a backend was constructed anyway: {built}"


@pytest.mark.unit
def test_the_application_lifespan_refuses_an_unrecognised_value(monkeypatch):
    """The wiring. Without this the guard above never runs on a real unit.

    Follows test_backend_shutdown.py's pattern for exercising the real
    lifespan with the rest of the startup work stubbed out.
    """
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
    monkeypatch.setattr(capture_service.settings, "CAMERA_BACKEND", "gphoto")

    served = []

    async def serve():
        async with app_main.lifespan(app_main.app):
            served.append(1)

    with pytest.raises(capture_service.UnknownCameraBackend):
        asyncio.run(serve())
    assert served == [], "the application started with a backend it does not have"


@pytest.mark.unit
def test_the_lifespan_still_starts_on_a_value_it_has(monkeypatch):
    """The guard must not be the reason a correctly configured unit fails."""
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
    monkeypatch.setattr(capture_service, "shutdown_backend", lambda: None)
    monkeypatch.setattr(capture_service.settings, "CAMERA_BACKEND", "gphoto2")

    served = []

    async def serve():
        async with app_main.lifespan(app_main.app):
            served.append(1)

    asyncio.run(serve())
    assert served == [1]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
