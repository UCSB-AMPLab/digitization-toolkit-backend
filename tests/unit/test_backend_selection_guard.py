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
# than through `app.core.capture_service.settings`, and the difference is not cosmetic.
# conftest.py's `set_camera_backend` fixture calls `importlib.reload(config)`,
# which rebinds `app.core.capture_service.settings` to a NEW Settings object while every
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
    # Both the registry values AND the names in the module namespace. Patching
    # only the registry left a hole: a construction written directly above the
    # dispatch -- RpicamBackend(subprocess_logger) -- never touches the mapping,
    # so the recorder stayed empty and this test passed with the forbidden
    # fallback being built.
    for name in list(capture_service._BACKEND_CONSTRUCTORS):
        monkeypatch.setitem(
            capture_service._BACKEND_CONSTRUCTORS,
            name,
            lambda *a, _n=name, **k: built.append(_n),
        )
    # Three namespaces, because two were not enough. The registry mapping and
    # the names in capture.service both miss a constructor imported locally
    # from its own module at the point of use, which a review reproduced.
    import importlib

    defining_modules = {
        "Picamera2Backend": "capture.backends.picamera2_backend",
        "RpicamBackend": "capture.backends.subprocess_backend",
        "GPhoto2Backend": "capture.backends.gphoto2_backend",
        "ChdkBackend": "capture.backends.chdk_backend",
    }
    for cls_name, module_name in defining_modules.items():
        assert hasattr(capture_service, cls_name), cls_name
        monkeypatch.setattr(
            capture_service,
            cls_name,
            lambda *a, _n=cls_name, **k: built.append(_n),
        )
        module = importlib.import_module(module_name)
        assert hasattr(module, cls_name), f"{module_name}.{cls_name}"
        monkeypatch.setattr(
            module,
            cls_name,
            lambda *a, _n=cls_name, **k: built.append(_n),
        )
        package = importlib.import_module("capture.backends")
        if hasattr(package, cls_name):
            monkeypatch.setattr(
                package,
                cls_name,
                lambda *a, _n=cls_name, **k: built.append(_n),
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

    # Entered through the app's REGISTERED lifespan, not by calling the
    # function. Calling app_main.lifespan directly passes even if the
    # registration is dropped -- a mutation to `FastAPI()` with no lifespan=
    # left all 17 cases in this file green while the protection was gone.
    async def serve():
        async with app_main.app.router.lifespan_context(app_main.app):
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
        async with app_main.app.router.lifespan_context(app_main.app):
            served.append(1)

    asyncio.run(serve())
    assert served == [1]


class _FakeBackend:
    """Enough of a backend for the registry to enumerate, with no hardware.

    The same shape tests/unit/test_registry_hw_id_gphoto2.py uses: the registry
    asks capture_service.get_backend() for devices, so a fake there exercises
    the real dispatch without a camera attached.
    """

    def __init__(self, devices):
        self._devices = devices
        self.calls = 0

    def list_devices(self):
        self.calls += 1
        return [dict(d) for d in self._devices]


_FAKE_DEVICE = {
    "index": 0,
    "id": "usb:001,004",
    "model": "Canon EOS Rebel T7",
    "serial": "1111111",
    "location": "USB usb:001,004",
}


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    ["gphoto2", " gphoto2 ", "GPhoto2", "\tGPHOTO2\n"],
    ids=["plain", "padded", "mixed_case", "tabs"],
)
def test_the_registry_dispatches_on_the_normalised_value(monkeypatch, value):
    """A value the guard accepts must route to the same backend downstream.

    Accepting surrounding whitespace in the guard while camera_registry.py
    normalised differently meant `" gphoto2 "` passed startup and then selected
    the picamera2 path, so a DSLR rig produced no hardware id and detected
    nothing.

    This goes through the registry's real dispatch rather than asserting on its
    source. An earlier version of this test read the source instead, on the
    grounds that both registry sites lead into hardware detection -- a review
    pointed out that the suite already enumerates a fake backend this way, and
    that a source assertion passes a mutation which adds a raw-value equality
    check beside the normalised one.
    """
    from app.core.config import settings
    from capture.camera_registry import CameraRegistry

    backend = _FakeBackend([_FAKE_DEVICE])
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    monkeypatch.setattr(settings, "CAMERA_BACKEND", value)

    hw_id, info = CameraRegistry.get_camera_hardware_id(0)

    assert hw_id == "canoneosrebelt7_1111111", (
        f"CAMERA_BACKEND={value!r} did not reach the gphoto2 path"
    )
    assert info["id"] == "usb:001,004"
    assert backend.calls == 1


@pytest.mark.unit
def test_detection_also_dispatches_on_the_normalised_value(monkeypatch, tmp_path):
    """The registry's other reader of the setting, through the same fake."""
    from app.core.config import settings
    from capture.camera_registry import CameraRegistry

    backend = _FakeBackend([_FAKE_DEVICE])
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    monkeypatch.setattr(settings, "CAMERA_BACKEND", " GPhoto2 ")

    registry = CameraRegistry(registry_path=tmp_path / "registry.json")
    detected = registry.detect_cameras()

    assert set(detected) == {0}, (
        "a padded CAMERA_BACKEND did not reach the gphoto2 detection path"
    )
    hw_id, _info = detected[0]
    assert hw_id == "canoneosrebelt7_1111111"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
