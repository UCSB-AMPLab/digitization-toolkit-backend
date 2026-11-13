"""Optical zoom on CHDK bodies: one stored step per body, applied in record mode.

Measured on 12 November 2026 on a Canon PowerShot A2500 under CHDK 1.5.1:
get_zoom_steps() returns 64, get_zoom() returns 0 at the widest, and
set_zoom(n) moves the lens and takes one to a few seconds. Spreads set the
zoom once per start with set_zoom(level) after the switch to record mode and
refused a level at or above get_zoom_steps().

On the same day a second Lua script sent while an earlier one was still
running, and commands sent to a body that had just timed out, knocked a
camera off USB. So the backend sends one script at a time under the body's
lock, gives set_zoom a 15 s timeout, and sends nothing more to a body in the
same operation after a timeout or an error.

Version: 0.1.0-beta
"""

import logging

import pytest

import capture.backends.chdk_backend as cb
import capture.service as capture_service
from capture.backends.errors import CaptureTimeoutError
from capture.camera import CameraConfig
from capture.camera_registry import CameraRegistry

from .chdk_fakes import Body, make_backend, make_pychdk


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"
JPEG = b"\xff\xd8\xff\xe0 page \xff\xd9"
GET_STEPS = "return get_zoom_steps()"
GET_ZOOM = "return get_zoom()"
HW_ID = "canon325b_AAA111"


def _config(index=0):
    return CameraConfig(camera_index=index)


def _registry(tmp_path):
    return CameraRegistry(registry_path=tmp_path / "cameras.json")


def _store(registry, hardware_id, step):
    """Write a zoom step into the registry the way a hand-edited file would."""
    registry.register_resolved(hardware_id, {"model": "A2500"}, 0)

    def change(cameras):
        cameras["cameras"][hardware_id]["zoom_step"] = step

    registry._mutate(change)


def _spy_timeouts(monkeypatch, backend):
    """Record the timeout the backend gives each Lua script it sends.

    The backend sends Lua itself, through _send_lua, so the timeout is the
    backend's own wait and never reaches the fake device.
    """
    sent = {}
    real = backend._send_lua

    def send(body, code, timeout=cb._LUA_TIMEOUT_S):
        sent[code] = timeout
        return real(body, code, timeout)

    monkeypatch.setattr(backend, "_send_lua", send)
    return sent


def _backend(monkeypatch, body, registry=None):
    fake = make_pychdk(body)
    backend = make_backend(monkeypatch, fake, registry=registry)
    backend.list_devices()
    return backend, fake


def _zoom_calls(body):
    return [c for c in body.lua_calls if c.startswith("set_zoom(")]


def _after(body, script):
    """The Lua calls made after the last call of `script`."""
    last = max(i for i, c in enumerate(body.lua_calls) if c == script)
    return body.lua_calls[last + 1:]


# --- reading the steps ----------------------------------------------------


@pytest.mark.unit
def test_the_steps_are_read_once_on_entering_record_mode(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64, zoom=5)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))

    backend.capture_image(tmp_path / "one.jpg", _config())
    backend.capture_image(tmp_path / "two.jpg", _config())

    assert body.lua_calls.count(GET_STEPS) == 1
    assert body.lua_calls.count(GET_ZOOM) == 1
    held = backend._body_at(0)
    assert held.zoom_steps == 64
    assert held.zoom_step == 5
    # The steps are read after record mode is confirmed.
    assert body.lua_calls.index(GET_STEPS) > body.lua_calls.index("return get_mode()")
    assert _zoom_calls(body) == []


@pytest.mark.unit
@pytest.mark.parametrize("answer", [None, 0, -3, True, "64", 64.0])
def test_a_body_that_answers_no_positive_integer_has_no_steps(
    monkeypatch, tmp_path, answer
):
    registry = _registry(tmp_path)
    _store(registry, HW_ID, 10)
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=answer)
    backend, _ = _backend(monkeypatch, body, registry)

    backend.capture_image(tmp_path / "one.jpg", _config())

    held = backend._body_at(0)
    assert held.zoom_steps is None
    assert held.zoom_step is None
    assert GET_ZOOM not in body.lua_calls
    assert _zoom_calls(body) == []
    assert len(body.shots) == 1


# --- the stored step on entering record mode ------------------------------


@pytest.mark.unit
def test_a_stored_step_is_applied_on_entering_record_mode(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    _store(registry, HW_ID, 20)
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64, zoom=0)
    backend, _ = _backend(monkeypatch, body, registry)
    timeouts = _spy_timeouts(monkeypatch, backend)

    backend.capture_image(tmp_path / "one.jpg", _config())

    assert body.zoom == 20
    assert _zoom_calls(body) == ["set_zoom(20)"]
    assert _after(body, "set_zoom(20)")[0] == GET_ZOOM, "the step was not confirmed"
    assert timeouts["set_zoom(20)"] == cb._SET_ZOOM_TIMEOUT_S == 15.0
    assert backend._body_at(0).zoom_step == 20
    assert len(body.shots) == 1

    backend.capture_image(tmp_path / "two.jpg", _config())
    assert _zoom_calls(body) == ["set_zoom(20)"], "the step was sent again"


@pytest.mark.unit
def test_a_stored_step_the_lens_is_already_at_is_not_sent(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    _store(registry, HW_ID, 20)
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64, zoom=20)
    backend, _ = _backend(monkeypatch, body, registry)

    backend.capture_image(tmp_path / "one.jpg", _config())

    assert _zoom_calls(body) == []
    assert backend._body_at(0).zoom_step == 20


@pytest.mark.unit
def test_a_body_with_no_stored_step_is_left_where_its_lens_is(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64, zoom=7)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))

    backend.capture_image(tmp_path / "one.jpg", _config())

    assert _zoom_calls(body) == []
    assert backend._body_at(0).zoom_step == 7


@pytest.mark.unit
@pytest.mark.parametrize("stored", [64, 100, -1, True, "20", 20.0])
def test_a_stored_step_out_of_range_is_ignored_with_a_warning(
    monkeypatch, tmp_path, caplog, stored
):
    registry = _registry(tmp_path)
    _store(registry, HW_ID, stored)
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64, zoom=3)
    backend, _ = _backend(monkeypatch, body, registry)
    caplog.set_level(logging.WARNING, logger="test-chdk")

    backend.capture_image(tmp_path / "one.jpg", _config())

    assert _zoom_calls(body) == []
    assert len(body.shots) == 1
    assert backend._body_at(0).zoom_step == 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("zoom step" in r.getMessage() for r in warnings)


@pytest.mark.unit
def test_a_stored_step_the_camera_does_not_reach_is_logged_and_the_capture_goes_on(
    monkeypatch, tmp_path, caplog
):
    registry = _registry(tmp_path)
    _store(registry, HW_ID, 20)
    body = Body(
        serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64, zoom=3,
        zoom_follows=False,
    )
    backend, _ = _backend(monkeypatch, body, registry)
    caplog.set_level(logging.WARNING, logger="test-chdk")

    backend.capture_image(tmp_path / "one.jpg", _config())

    assert len(body.shots) == 1
    assert body.closes == 0, "the body was dropped"
    held = backend._body_at(0)
    assert held.zoom_step == 3
    assert held.in_record_mode is True
    warnings = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert any(
        "usb:001,004" in m and "20" in m and "3" in m for m in warnings
    ), warnings
    # The stored step is left in the registry for the operator to change.
    assert _registry(tmp_path).get_camera_by_id(HW_ID)["zoom_step"] == 20


@pytest.mark.unit
def test_a_set_zoom_that_times_out_on_entering_record_mode_sends_nothing_more(
    monkeypatch, tmp_path
):
    registry = _registry(tmp_path)
    _store(registry, HW_ID, 20)
    body = Body(
        serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64,
        zoom_error=TimeoutError("Script did not complete within 15.0s"),
    )
    backend, _ = _backend(monkeypatch, body, registry)

    with pytest.raises(CaptureTimeoutError):
        backend.capture_image(tmp_path / "one.jpg", _config())

    assert body.lua_calls[-1] == "set_zoom(20)"
    assert body.shots == []
    assert body.closes == 1


@pytest.mark.unit
def test_an_ambiguous_body_does_not_read_a_stored_step(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    _store(registry, HW_ID, 20)
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, registry)
    held = backend._body_at(0)
    held.identity_clash = "another body answers to this id"

    backend._ensure_record_mode(held)

    assert _zoom_calls(body) == []
    assert held.zoom_step == 0


# --- setting a step on request ---------------------------------------------


@pytest.mark.unit
def test_set_zoom_step_moves_confirms_and_stores(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64, zoom=0)
    backend, _ = _backend(monkeypatch, body, registry)
    timeouts = _spy_timeouts(monkeypatch, backend)

    assert backend.set_zoom_step(0, 30) == 30

    assert body.zoom == 30
    assert backend._body_at(0).in_record_mode is True
    # The lens moved, so any focus lock is released.
    assert body.lua_calls[-3:] == ["set_zoom(30)", GET_ZOOM, "set_aflock(0)"]
    assert timeouts["set_zoom(30)"] == cb._SET_ZOOM_TIMEOUT_S
    assert backend._body_at(0).zoom_step == 30
    stored = _registry(tmp_path).get_camera_by_id(HW_ID)
    assert stored is not None, "an unregistered body was not registered"
    assert stored["zoom_step"] == 30


@pytest.mark.unit
def test_set_zoom_step_then_entering_record_mode_again_uses_the_stored_step(
    monkeypatch, tmp_path
):
    registry = _registry(tmp_path)
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, registry)
    backend.set_zoom_step(0, 12)

    backend._evict(backend._body_at(0), "test")
    body.mode_value = False
    body.zoom = 0
    backend.list_devices()
    backend.capture_image(tmp_path / "one.jpg", _config())

    assert body.zoom == 12
    assert _zoom_calls(body) == ["set_zoom(12)", "set_zoom(12)"]


@pytest.mark.unit
@pytest.mark.parametrize("step", [-1, 64, 1000, True, 2.0, "3", None])
def test_set_zoom_step_refuses_a_step_out_of_range(monkeypatch, tmp_path, step):
    registry = _registry(tmp_path)
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64, zoom=0)
    backend, _ = _backend(monkeypatch, body, registry)

    with pytest.raises(ValueError) as exc:
        backend.set_zoom_step(0, step)

    assert "0" in str(exc.value) and "63" in str(exc.value)
    assert _zoom_calls(body) == []
    assert _registry(tmp_path).get_camera_by_id(HW_ID) is None
    assert body.closes == 0


@pytest.mark.unit
def test_set_zoom_step_on_a_body_with_no_steps_fails(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=None)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))

    with pytest.raises(RuntimeError) as exc:
        backend.set_zoom_step(0, 3)

    assert "zoom steps" in str(exc.value)
    assert _zoom_calls(body) == []


@pytest.mark.unit
def test_a_camera_that_does_not_confirm_the_step_stores_nothing(
    monkeypatch, tmp_path
):
    registry = _registry(tmp_path)
    body = Body(
        serial="AAA111", card=EVEN_CARD, zoom_steps=64, zoom=4,
        zoom_follows=False,
    )
    backend, _ = _backend(monkeypatch, body, registry)

    with pytest.raises(RuntimeError) as exc:
        backend.set_zoom_step(0, 30)

    assert "30" in str(exc.value) and "4" in str(exc.value)
    assert backend._body_at(0).zoom_step == 4
    assert _registry(tmp_path).get_camera_by_id(HW_ID) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [TimeoutError("Script did not complete within 15.0s"),
     RuntimeError("Script did not start: busy")],
    ids=["timeout", "error"],
)
def test_a_set_zoom_that_fails_sends_nothing_further_and_stores_nothing(
    monkeypatch, tmp_path, error
):
    registry = _registry(tmp_path)
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64, zoom_error=error)
    backend, _ = _backend(monkeypatch, body, registry)

    with pytest.raises(RuntimeError):
        backend.set_zoom_step(0, 30)

    assert body.lua_calls[-1] == "set_zoom(30)"
    assert body.closes == 1, "the body was not dropped"
    assert backend._body_at(0) is None
    assert _registry(tmp_path).get_camera_by_id(HW_ID) is None


@pytest.mark.unit
def test_a_failed_switch_before_set_zoom_sends_no_zoom(monkeypatch, tmp_path):
    body = Body(
        serial="AAA111", card=EVEN_CARD, zoom_steps=64,
        mode_error=TimeoutError("Script still running after 5s"),
    )
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))

    with pytest.raises(RuntimeError):
        backend.set_zoom_step(0, 30)

    assert body.lua_calls == ["enter_alt()"]
    assert body.closes == 1


@pytest.mark.unit
def test_a_provisional_body_may_be_zoomed_but_nothing_is_stored(
    monkeypatch, tmp_path
):
    registry = _registry(tmp_path)
    body = Body(serial=None, card=b"EVEN\n", zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, registry)
    assert backend._body_at(0).hardware_id is None

    assert backend.set_zoom_step(0, 8) == 8

    assert body.zoom == 8
    assert _registry(tmp_path).list_cameras() == []


@pytest.mark.unit
def test_a_body_that_left_the_bus_during_set_zoom_is_retried_once(
    monkeypatch, tmp_path
):
    """A camera restarted with its cable in comes back at a new address."""
    old = Body(address=16, serial="AAA111", card=EVEN_CARD, zoom_steps=64,
               zoom_error=OSError(19, "No such device"))
    new = Body(address=17, serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    fake = make_pychdk(old)
    backend = make_backend(monkeypatch, fake, registry=_registry(tmp_path))
    backend.list_devices()
    fake.bodies = [new]

    assert backend.set_zoom_step(0, 30) == 30

    assert old.lua_calls[-1] == "set_zoom(30)", "the old body was sent more"
    assert new.zoom == 30
    assert _registry(tmp_path).get_camera_by_id(HW_ID)["zoom_step"] == 30


@pytest.mark.unit
def test_a_different_body_at_the_index_is_not_retried(monkeypatch, tmp_path):
    old = Body(address=16, serial="AAA111", card=EVEN_CARD, zoom_steps=64,
               zoom_error=OSError(19, "No such device"))
    other = Body(address=17, serial="BBB222", card=EVEN_CARD, zoom_steps=64)
    fake = make_pychdk(old)
    backend = make_backend(monkeypatch, fake, registry=_registry(tmp_path))
    backend.list_devices()
    fake.bodies = [other]

    with pytest.raises(cb.ZoomFailedError) as exc:
        backend.set_zoom_step(0, 30)

    assert "usb:001,016" in str(exc.value)
    assert _zoom_calls(other) == []
    assert _registry(tmp_path).list_cameras() == []


# --- what the backend reports ---------------------------------------------


@pytest.mark.unit
def test_device_rows_carry_the_steps_and_the_confirmed_step(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64, zoom=0)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))

    row = backend.list_devices()[0]
    assert row["zoom_steps"] is None
    assert row["zoom_step"] is None
    assert row["supports_zoom"] is False

    backend.set_zoom_step(0, 9)
    row = backend.list_devices()[0]
    assert row["zoom_steps"] == 64
    assert row["zoom_step"] == 9
    assert row["supports_zoom"] is False


@pytest.mark.unit
def test_only_the_chdk_backend_reports_stepped_zoom(monkeypatch):
    from capture.backends.base import CameraBackend
    from capture.backends.gphoto2_backend import GPhoto2Backend
    from capture.backends.subprocess_backend import RpicamBackend

    backend, _ = _backend(monkeypatch, Body(serial="AAA111", card=EVEN_CARD))
    assert backend.get_capabilities()["zoom_steps"] is True
    assert backend.get_capabilities()["zoom"] is False

    for cls in (GPhoto2Backend, RpicamBackend):
        assert cls.get_capabilities(None)["zoom_steps"] is False
    assert CameraBackend.get_capabilities(None)["zoom_steps"] is False


@pytest.mark.unit
def test_the_picamera2_backend_reports_no_stepped_zoom():
    from capture.backends import picamera2_backend

    assert picamera2_backend.Picamera2Backend.get_capabilities(None)["zoom_steps"] is False


# --- the registry ----------------------------------------------------------


@pytest.mark.unit
def test_the_registry_stores_one_step_per_hardware_id(tmp_path):
    registry = _registry(tmp_path)
    registry.register_resolved(HW_ID, {"model": "A2500"}, 0)

    registry.update_zoom_step(HW_ID, 17)

    stored = _registry(tmp_path).get_camera_by_id(HW_ID)
    assert stored["zoom_step"] == 17
    assert "zoom_step_updated_at" in stored


@pytest.mark.unit
@pytest.mark.parametrize("step", [-1, True, 2.5, "4", None])
def test_the_registry_refuses_a_step_that_is_not_a_non_negative_int(tmp_path, step):
    registry = _registry(tmp_path)
    registry.register_resolved(HW_ID, {"model": "A2500"}, 0)

    with pytest.raises(ValueError):
        registry.update_zoom_step(HW_ID, step)


@pytest.mark.unit
def test_the_registry_refuses_a_step_for_an_unknown_body(tmp_path):
    with pytest.raises(KeyError):
        _registry(tmp_path).update_zoom_step(HW_ID, 3)


# --- the route ---------------------------------------------------------------


def _user(username, role):
    from app.models.user import User

    return User(
        username=username, email=f"{username}@example.com",
        hashed_password="x", role=role, is_active=True,
    )


def _client_as(client, username, role):
    from app.main import app
    from app.api.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


def _install(monkeypatch, backend):
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)


@pytest.mark.unit
def test_the_route_answers_501_for_a_backend_without_stepped_zoom(
    client, monkeypatch
):
    class _Other:
        def get_backend_name(self):
            return "gphoto2"

        def get_capabilities(self):
            return {"zoom": False, "zoom_steps": False}

        def is_camera_connected(self, camera_index=0):
            return True

    _install(monkeypatch, _Other())
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/settings/0", json={"zoom_step": 3})

    assert resp.status_code == 501, resp.text
    assert "gphoto2" in resp.json()["detail"]


@pytest.mark.unit
def test_the_route_answers_422_with_the_range_for_a_step_out_of_range(
    client, monkeypatch, tmp_path
):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    _install(monkeypatch, backend)
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/settings/0", json={"zoom_step": 64})

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "0" in detail and "63" in detail
    assert _zoom_calls(body) == []


@pytest.mark.unit
def test_the_route_sets_the_step_and_reports_it(client, monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    _install(monkeypatch, backend)
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/settings/0", json={"zoom_step": 25})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"detail": "Zoom step applied", "zoom_step": 25}
    assert body.zoom == 25
    assert _registry(tmp_path).get_camera_by_id(HW_ID)["zoom_step"] == 25


@pytest.mark.unit
@pytest.mark.parametrize(
    "knobs",
    [
        {"zoom_steps": 64,
         "zoom_error": TimeoutError("Script did not complete within 15.0s")},
        {"zoom_steps": 64, "zoom_error": RuntimeError("Script did not start: busy")},
        {"zoom_steps": 64, "zoom_follows": False},
        {"zoom_steps": None},
    ],
    ids=["timeout", "wire-error", "not-confirmed", "no-steps"],
)
def test_the_route_answers_502_for_a_camera_failure(
    client, monkeypatch, tmp_path, knobs
):
    body = Body(serial="AAA111", card=EVEN_CARD, **knobs)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    _install(monkeypatch, backend)
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/settings/0", json={"zoom_step": 25})

    assert resp.status_code == 502, resp.text
    assert "usb:001,004" in resp.json()["detail"]


@pytest.mark.unit
def test_the_route_answers_404_for_an_index_with_no_camera(
    client, monkeypatch, tmp_path
):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    _install(monkeypatch, backend)
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/settings/1", json={"zoom_step": 25})

    assert resp.status_code == 404, resp.text
    assert "not connected" in resp.json()["detail"]


@pytest.mark.unit
@pytest.mark.parametrize("value", [True, 2.5, "3"], ids=["bool", "float", "string"])
def test_the_route_refuses_a_zoom_step_that_is_not_an_integer(
    client, monkeypatch, tmp_path, value
):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    _install(monkeypatch, backend)
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/settings/0", json={"zoom_step": value})

    assert resp.status_code == 422, resp.text
    assert body.lua_calls == [], "the camera was spoken to"


@pytest.mark.unit
def test_the_device_list_carries_the_zoom_fields(client, monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    backend.set_zoom_step(0, 6)
    _install(monkeypatch, backend)
    api = _client_as(client, "op", "operator")

    resp = api.get("/cameras/devices")

    assert resp.status_code == 200, resp.text
    row = resp.json()[0]
    assert row["zoom_steps"] == 64
    assert row["zoom_step"] == 6
    assert row["supports_zoom"] is False


@pytest.mark.unit
def test_the_device_list_carries_the_clock_offset(client, monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    backend.list_devices()
    backend._body_at(0).clock_offset_s = -0.5
    _install(monkeypatch, backend)
    api = _client_as(client, "op", "operator")

    resp = api.get("/cameras/devices")

    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["clock_offset_s"] == -0.5


@pytest.mark.unit
def test_the_device_list_answers_null_for_a_row_without_a_clock_offset(
    client, monkeypatch, tmp_path
):
    body = Body(serial="AAA111", card=EVEN_CARD, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body, _registry(tmp_path))
    listing = backend.list_devices

    def without_offset():
        rows = listing()
        for row in rows:
            row.pop("clock_offset_s", None)
        return rows

    monkeypatch.setattr(backend, "list_devices", without_offset)
    _install(monkeypatch, backend)
    api = _client_as(client, "op", "operator")

    resp = api.get("/cameras/devices")

    assert resp.status_code == 200, resp.text
    row = resp.json()[0]
    assert "clock_offset_s" in row
    assert row["clock_offset_s"] is None


@pytest.mark.unit
def test_the_capabilities_route_reports_stepped_zoom(client, monkeypatch):
    backend, _ = _backend(monkeypatch, Body(serial="AAA111", card=EVEN_CARD))
    _install(monkeypatch, backend)
    api = _client_as(client, "op", "operator")

    resp = api.get("/cameras/capabilities")

    assert resp.status_code == 200, resp.text
    assert resp.json()["zoom_steps"] is True
