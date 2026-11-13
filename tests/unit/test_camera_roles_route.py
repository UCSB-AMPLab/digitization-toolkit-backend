"""PUT /cameras/roles: the operator sets which page each camera photographs (NEH-342).

The route checks that both bodies are connected and identifiable, registers
a body the registry does not know, and stores both roles and the pair in one
write. It answers with the device rows and the setup report as they stand
after the write. Nothing reads a stored role for indexes, pins or capture in
this step.

Modelled on test_camera_setup_route.py: a fake backend stands in for the
process singleton, and the registry is a real CameraRegistry on the client
fixture's projects root.

Version: 0.1.0-beta
"""

import json

import pytest

import capture.service as capture_service


A = "canoneosrebelt7_1111111"
B = "canoneosrebelt7_2222222"


def _devices(port_a="1-1.2", port_b="1-1.3"):
    return [
        {
            "index": 0,
            "model": "Canon EOS Rebel T7",
            "hardware_id": A,
            "serial": "1111111",
            "location": "USB usb:001,004",
            "port": "usb:001,004",
            "has_aperture_control": True,
            "supports_zoom": False,
            "usb_port": port_a,
        },
        {
            "index": 1,
            "model": "Canon EOS Rebel T7",
            "hardware_id": B,
            "serial": "2222222",
            "location": "USB usb:001,005",
            "port": "usb:001,005",
            "has_aperture_control": True,
            "supports_zoom": False,
            "usb_port": port_b,
        },
    ]


class _FakeBackend:
    """Lists devices and records every other attribute the route reaches for."""

    def __init__(self, devices=None, error=None, name="gphoto2"):
        self._devices = devices if devices is not None else []
        self._error = error
        self._name = name
        self.calls = []

    def list_devices(self):
        self.calls.append("list_devices")
        if self._error is not None:
            raise self._error
        return [dict(d) for d in self._devices]

    def get_backend_name(self):
        self.calls.append("get_backend_name")
        return self._name

    def __getattr__(self, name):
        # Any other backend method (rescan, assign_side, bindings, capture)
        # is recorded and refused, so a test can tell the route used none.
        def refused(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"the roles route called backend.{name}")
        return refused


def _user(username, role):
    from app.models.user import User

    return User(
        username=username,
        email=f"{username}@example.com",
        hashed_password="x",
        role=role,
        is_active=True,
    )


def _client_as(client, username, role):
    from app.main import app
    from app.api.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


def _install(monkeypatch, backend):
    from app.core.config import settings

    monkeypatch.setattr(settings, "CAMERA_BACKEND", "gphoto2")
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    return backend


def _record(**extra):
    record = {
        "model": "Canon EOS Rebel T7",
        "serial": None,
        "location": None,
        "machine_id": None,
        "label": None,
        "last_seen_index": 0,
        "first_registered_at": "2026-11-01T00:00:00+00:00",
        "last_seen_at": "2026-11-01T00:00:00+00:00",
        "calibration": {},
        "orientation": None,
    }
    record.update(extra)
    return record


def _registry_path():
    from capture.camera_registry import CameraRegistry

    return CameraRegistry().registry_path


def _write_registry(cameras, confirmed_pair=None):
    data = {"cameras": cameras, "version": "1.0"}
    if confirmed_pair is not None:
        data["confirmed_pair"] = confirmed_pair
    _registry_path().write_text(json.dumps(data))


def _read_registry():
    return json.loads(_registry_path().read_text())


def _body(left=A, right=B):
    return {"left": {"hardware_id": left}, "right": {"hardware_id": right}}


@pytest.mark.unit
def test_an_operator_sets_the_roles_and_gets_the_new_state(client, monkeypatch):
    _install(monkeypatch, _FakeBackend(_devices()))
    _write_registry({A: _record(orientation=270), B: _record()})
    api = _client_as(client, "op", "operator")

    resp = api.put("/cameras/roles", json=_body())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"devices", "setup"}
    rows = {row["hardware_id"]: row for row in body["devices"]}
    assert rows[A]["role"] == "left"
    assert rows[A]["role_confirmed"] is True
    assert rows[A]["usb_port_at_confirmation"] == "1-1.2"
    assert rows[A]["orientation"] == 270
    assert rows[B]["role"] == "right"
    assert rows[B]["usb_port_at_confirmation"] == "1-1.3"
    assert body["setup"] == {
        "needs_setup": False,
        "reasons": [],
        "confirmed_pair": {"left": A, "right": B},
    }
    pair = _read_registry()["confirmed_pair"]
    assert pair["backend"] == "gphoto2"
    assert pair["confirmed_by"] == "op"
    assert pair["ports"] == {"left": "1-1.2", "right": "1-1.3"}
    assert _read_registry()["cameras"][A]["role_confirmed_by"] == "op"


@pytest.mark.unit
def test_the_next_reads_reflect_the_roles(client, monkeypatch):
    _install(monkeypatch, _FakeBackend(_devices()))
    api = _client_as(client, "op", "operator")
    before = api.get("/cameras/setup").json()
    assert [r["code"] for r in before["reasons"]] == ["role_unconfirmed"]

    assert api.put("/cameras/roles", json=_body()).status_code == 200

    setup = api.get("/cameras/setup")
    assert setup.status_code == 200, setup.text
    codes = [r["code"] for r in setup.json()["reasons"]]
    assert "role_unconfirmed" not in codes
    assert "pair_changed" not in codes
    assert setup.json()["needs_setup"] is False
    devices = {row["hardware_id"]: row for row in api.get("/cameras/devices").json()}
    assert devices[A]["role"] == "left" and devices[A]["role_confirmed"] is True
    assert devices[B]["role"] == "right" and devices[B]["role_confirmed"] is True
    assert devices[A]["usb_port_at_confirmation"] == "1-1.2"
    assert devices[B]["usb_port_at_confirmation"] == "1-1.3"


@pytest.mark.unit
def test_an_unknown_body_is_registered_before_the_write(client, monkeypatch):
    _install(monkeypatch, _FakeBackend(_devices()))
    _write_registry({A: _record()})
    api = _client_as(client, "op", "operator")

    resp = api.put("/cameras/roles", json=_body())

    assert resp.status_code == 200, resp.text
    record = _read_registry()["cameras"][B]
    assert record["model"] == "Canon EOS Rebel T7"
    assert record["serial"] == "2222222"
    assert record["location"] == "USB usb:001,005"
    assert record["last_seen_index"] == 1
    assert record["orientation"] is None
    assert record["role"] == "right"


@pytest.mark.unit
def test_two_bodies_exchange_their_roles_in_one_request(client, monkeypatch):
    _install(monkeypatch, _FakeBackend(_devices()))
    api = _client_as(client, "op", "operator")
    assert api.put("/cameras/roles", json=_body(A, B)).status_code == 200

    resp = api.put("/cameras/roles", json=_body(B, A))

    assert resp.status_code == 200, resp.text
    rows = {row["hardware_id"]: row for row in resp.json()["devices"]}
    assert rows[B]["role"] == "left"
    assert rows[A]["role"] == "right"
    assert resp.json()["setup"]["needs_setup"] is False
    assert resp.json()["setup"]["confirmed_pair"] == {"left": B, "right": A}
    cameras = _read_registry()["cameras"]
    assert cameras[B]["usb_port_at_confirmation"] == "1-1.3"
    assert cameras[A]["usb_port_at_confirmation"] == "1-1.2"


@pytest.mark.unit
def test_a_body_that_held_a_role_and_is_absent_loses_it(client, monkeypatch):
    C = "canoneosrebelt7_3333333"
    _install(monkeypatch, _FakeBackend(_devices()))
    _write_registry({
        C: _record(role="left", role_confirmed_at="2026-11-01T00:00:00+00:00",
                   usb_port_at_confirmation="1-1.2", orientation=180),
    }, confirmed_pair={"left": C, "right": B})
    api = _client_as(client, "op", "operator")

    resp = api.put("/cameras/roles", json=_body())

    assert resp.status_code == 200, resp.text
    record = _read_registry()["cameras"][C]
    assert "role" not in record
    assert record["orientation"] == 180


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    _body(A, A),
    _body("", B),
    _body(A, ""),
    {"left": {"hardware_id": A}},
    {"left": {}, "right": {"hardware_id": B}},
])
def test_the_same_or_an_empty_id_is_422(client, monkeypatch, body):
    backend = _install(monkeypatch, _FakeBackend(_devices()))
    api = _client_as(client, "op", "operator")

    resp = api.put("/cameras/roles", json=body)

    assert resp.status_code == 422, resp.text
    assert not _registry_path().exists() or "confirmed_pair" not in _read_registry()


@pytest.mark.unit
def test_a_body_that_is_not_connected_is_409(client, monkeypatch):
    _install(monkeypatch, _FakeBackend(_devices()))
    api = _client_as(client, "op", "operator")

    resp = api.put("/cameras/roles", json=_body(A, "canoneosrebelt7_9999999"))

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert set(detail) == {"code", "message"}
    assert detail["code"] == "camera_not_connected"
    assert detail["message"].endswith(".") and detail["message"].count(".") == 1
    assert not _registry_path().exists() or "confirmed_pair" not in _read_registry()


def _unusable(kind):
    devices = _devices()
    if kind == "provisional":
        devices[1]["provisional"] = True
    elif kind == "placeholder":
        devices[1]["hardware_id"] = "canoneosrebelt7_idx1"
    elif kind == "ambiguous":
        devices[1]["identity_ambiguous"] = True
    return devices


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["provisional", "placeholder", "ambiguous"])
def test_a_body_without_a_usable_identity_is_409(client, monkeypatch, kind):
    devices = _unusable(kind)
    _install(monkeypatch, _FakeBackend(devices))
    api = _client_as(client, "op", "operator")

    resp = api.put("/cameras/roles", json=_body(A, devices[1]["hardware_id"]))

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert set(detail) == {"code", "message"}
    assert detail["code"] == "camera_identity_unusable"
    assert detail["message"].endswith(".") and detail["message"].count(".") == 1
    assert not _registry_path().exists() or not _read_registry()["cameras"]


@pytest.mark.unit
def test_a_reviewer_is_refused(client, monkeypatch):
    _install(monkeypatch, _FakeBackend(_devices()))
    api = _client_as(client, "rev", "reviewer")

    resp = api.put("/cameras/roles", json=_body())

    assert resp.status_code == 403, resp.text
    assert not _registry_path().exists() or not _read_registry()["cameras"]


@pytest.mark.unit
def test_no_user_is_401(client, monkeypatch):
    _install(monkeypatch, _FakeBackend(_devices()))

    resp = client.put("/cameras/roles", json=_body())

    assert resp.status_code == 401


@pytest.mark.unit
def test_a_backend_that_cannot_list_is_503(client, monkeypatch):
    _install(monkeypatch, _FakeBackend(error=RuntimeError("bus gone")))
    api = _client_as(client, "op", "operator")

    resp = api.put("/cameras/roles", json=_body())

    assert resp.status_code == 503, resp.text


@pytest.mark.unit
def test_a_registry_that_cannot_be_written_is_503(client, monkeypatch):
    from app.core.storage_override import StorageUnavailableError
    from capture.camera_registry import CameraRegistry

    _install(monkeypatch, _FakeBackend(_devices()))
    _write_registry({A: _record(), B: _record()})
    text = _registry_path().read_text()

    def refuse(self):
        raise StorageUnavailableError("the storage root is not usable")

    monkeypatch.setattr(CameraRegistry, "_refuse_write", refuse)
    api = _client_as(client, "op", "operator")

    resp = api.put("/cameras/roles", json=_body())

    assert resp.status_code == 503, resp.text
    assert resp.json() == {"detail": "the storage root is not usable"}
    assert _registry_path().read_text() == text


@pytest.mark.unit
def test_a_role_write_changes_no_index_and_calls_nothing_else_on_the_backend(client, monkeypatch):
    backend = _install(monkeypatch, _FakeBackend(_devices()))
    api = _client_as(client, "op", "operator")
    before = [(row["index"], row["hardware_id"]) for row in api.get("/cameras/devices").json()]

    resp = api.put("/cameras/roles", json=_body(B, A))

    assert resp.status_code == 200, resp.text
    after = [(row["index"], row["hardware_id"]) for row in api.get("/cameras/devices").json()]
    assert after == before == [(0, A), (1, B)]
    assert [(row["index"], row["hardware_id"]) for row in resp.json()["devices"]] == before
    assert set(backend.calls) <= {"list_devices", "get_backend_name"}
