#!/usr/bin/env python3
"""
Tests for the CAMERA_BACKEND guard (NEH-268).

An unrecognised value is refused, never mapped to a fallback. A fallback to
RpicamBackend is a third backend, neither the one configured nor the one
intended: on a DSLR rig it yields no cameras and presents as a hardware fault,
so a typo in one line of .env would be indistinguishable from a dead camera.

Two things have to hold: the value is refused, AND it is refused at startup.
The backend is built lazily on the first capture, so a guard that lived only in
the dispatch would let the service start, answer /health, and fail at the
moment someone tried to photograph something.

CI runs `pytest -m "not camera"`. The backend's root conftest.py adds
@pytest.mark.camera only to the hardware files in HARDWARE_TEST_FILES, so
these tests run in CI whatever their names say.

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
# changes an object nobody reads, and every case here would read the default.


@pytest.mark.unit
def test_the_registry_is_the_only_list_of_names():
    """The dispatch and the published tuple cannot disagree, by construction.

    Names enumerated in one place and "everything else" handled in another
    leave room for a silent fallback. One mapping feeds both.
    """
    assert capture_service.KNOWN_CAMERA_BACKENDS == tuple(
        sorted(capture_service._BACKEND_CONSTRUCTORS)
    )
    assert set(capture_service.KNOWN_CAMERA_BACKENDS) == {
        "picamera2", "subprocess", "gphoto2", "chdk"
    }
    # Four distinct classes: a mapping that pointed two names at one backend
    # would be a silent fallback that passes a membership check.
    assert len(set(capture_service._BACKEND_CONSTRUCTORS.values())) == 4


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    ["picamera2", "subprocess", "gphoto2", "chdk"],
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
    # only the registry leaves a hole: a construction written directly above the
    # dispatch, such as `RpicamBackend(subprocess_logger)`, never touches the
    # mapping, so the recorder would stay empty and this test would pass with
    # the forbidden fallback being built.
    for name in list(capture_service._BACKEND_CONSTRUCTORS):
        monkeypatch.setitem(
            capture_service._BACKEND_CONSTRUCTORS,
            name,
            lambda *a, _n=name, **k: built.append(_n),
        )
    # Instrument the CLASS OBJECTS, not the names that point at them. Patching
    # names cannot be shown to cover every alias: the registry mapping and
    # capture.service miss a local import from a defining module, and those
    # together miss capture/__init__.py's re-export. Replacing __init__ on a
    # class records a construction reached through any name bound to it.
    #
    # Collected from every source rather than assumed to be one object:
    # capture/backends/__init__.py does NOT always re-export the defining
    # module's class -- it can supply a stand-in that raises when the optional
    # dependency is absent, which is how picamera2 behaves off a Pi.
    import importlib

    candidates = list(capture_service._BACKEND_CONSTRUCTORS.values())
    for cls_name, module_name in (
        ("Picamera2Backend", "capture.backends.picamera2_backend"),
        ("RpicamBackend", "capture.backends.subprocess_backend"),
        ("GPhoto2Backend", "capture.backends.gphoto2_backend"),
        ("ChdkBackend", "capture.backends.chdk_backend"),
    ):
        for holder in (module_name, "capture.backends", "capture"):
            try:
                module = importlib.import_module(holder)
            except Exception:
                continue
            found = getattr(module, cls_name, None)
            if isinstance(found, type):
                candidates.append(found)

    seen_ids = set()
    for cls in candidates:
        if id(cls) in seen_ids or not isinstance(cls, type):
            continue
        seen_ids.add(id(cls))
        monkeypatch.setattr(
            cls, "__init__",
            lambda self, *a, _n=cls.__name__, **k: built.append(_n),
        )
    assert len(seen_ids) >= 4, f"only {len(seen_ids)} backend classes instrumented"

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
    # registration is dropped: with `FastAPI()` given no lifespan=, every
    # other case in this file stays green while the protection is gone.
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


# The real device-row shape, which the registry reads by key:
# `device["hardware_id"]` and `device.get("port")`. A row missing either raises
# into _hardware_id_from_backend's `except Exception`, which returns
# (None, {"error": ...}) -- so a fake without them fails even on an unpadded
# "gphoto2" and looks like a normalisation bug.
_FAKE_DEVICE = {
    "index": 0,
    "model": "Canon EOS Rebel T7",
    "hardware_id": "canoneosrebelt7_1111111",
    "serial": "1111111",
    "location": "USB usb:001,004",
    "port": "usb:001,004",
    "has_aperture_control": True,
    "supports_zoom": False,
}


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    ["gphoto2", " gphoto2 ", "GPhoto2", "\tGPHOTO2\n"],
    ids=["plain", "padded", "mixed_case", "tabs"],
)
def test_the_registry_dispatches_on_the_normalised_value(monkeypatch, value):
    """A value the guard accepts must route to the same backend downstream.

    If the guard accepts surrounding whitespace and camera_registry.py
    normalises differently, `" gphoto2 "` passes startup and then selects the
    picamera2 path, so a DSLR rig produces no hardware id and detects nothing.

    This goes through the registry's real dispatch rather than asserting on its
    source: the suite already enumerates a fake backend this way, and a source
    assertion passes a mutation which adds a raw-value equality check beside
    the normalised one.
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
