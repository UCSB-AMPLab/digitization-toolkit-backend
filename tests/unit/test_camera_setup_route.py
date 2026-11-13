"""GET /cameras/setup and the role fields on GET /cameras/devices.

The route reports whether the operator must set or confirm the camera roles,
and why. In this slice nothing writes a role, so the tests write the registry
file directly, the way a later writer will leave it.

Modelled on test_orientation_route.py: a fake backend stands in for the
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
            "role_guess": "left",
            "role_guess_source": "gphoto2_pin",
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
            "role_guess": "right",
            "role_guess_source": "gphoto2_pin",
        },
    ]


class _FakeBackend:
    def __init__(self, devices=None, error=None):
        self._devices = devices if devices is not None else []
        self._error = error

    def list_devices(self):
        if self._error is not None:
            raise self._error
        return [dict(d) for d in self._devices]


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


def _record(role=None, confirmed_at=None, port=None):
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
    if role is not None:
        record["role"] = role
    if confirmed_at is not None:
        record["role_confirmed_at"] = confirmed_at
    if port is not None:
        record["usb_port_at_confirmation"] = port
    return record


def _write_registry(cameras, confirmed_pair=None):
    from capture.camera_registry import CameraRegistry

    path = CameraRegistry().registry_path
    data = {"cameras": cameras, "version": "1.0"}
    if confirmed_pair is not None:
        data["confirmed_pair"] = confirmed_pair
    path.write_text(json.dumps(data))


CONFIRMED = "2026-11-12T10:00:00+00:00"


@pytest.mark.unit
def test_a_unit_with_no_roles_needs_setup_for_every_body(client, monkeypatch):
    _install(monkeypatch, _FakeBackend(_devices()))
    api = _client_as(client, "op", "operator")

    resp = api.get("/cameras/setup")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "needs_setup": True,
        "reasons": [{
            "code": "role_unconfirmed",
            "message": "Captua has no confirmed role for this camera.",
            "hardware_ids": [A, B],
        }],
        "confirmed_pair": None,
    }


@pytest.mark.unit
def test_a_confirmed_pair_needs_no_setup(client, monkeypatch):
    _install(monkeypatch, _FakeBackend(_devices()))
    _write_registry(
        {
            A: _record("left", CONFIRMED, "1-1.2"),
            B: _record("right", CONFIRMED, "1-1.3"),
        },
        confirmed_pair={"left": A, "right": B},
    )
    api = _client_as(client, "rev", "reviewer")

    resp = api.get("/cameras/setup")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "needs_setup": False,
        "reasons": [],
        "confirmed_pair": {"left": A, "right": B},
    }


@pytest.mark.unit
def test_every_reason_carries_an_english_message(client, monkeypatch):
    devices = _devices(port_a="1-1.4")
    devices.append({
        "index": 2,
        "model": "Canon EOS Rebel T7",
        "hardware_id": "canoneosrebelt7_idx2",
        "serial": None,
        "location": "USB usb:001,006",
        "port": "usb:001,006",
        "has_aperture_control": True,
        "supports_zoom": False,
    })
    _install(monkeypatch, _FakeBackend(devices))
    _write_registry(
        {
            A: _record("left", CONFIRMED, "1-1.2"),
            B: _record("left", CONFIRMED, "1-1.3"),
        },
        confirmed_pair={"left": A, "right": "canoneosrebelt7_3333333"},
    )
    api = _client_as(client, "op", "operator")

    resp = api.get("/cameras/setup")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["needs_setup"] is True
    codes = [reason["code"] for reason in body["reasons"]]
    assert codes == ["identity_missing", "role_conflict", "pair_changed", "port_changed"]
    for reason in body["reasons"]:
        assert set(reason) == {"code", "message", "hardware_ids"}
        assert reason["message"].endswith(".")
        assert reason["message"].count(".") == 1


@pytest.mark.unit
def test_setup_answers_401_without_a_user(client, monkeypatch):
    _install(monkeypatch, _FakeBackend(_devices()))

    resp = client.get("/cameras/setup")

    assert resp.status_code == 401


@pytest.mark.unit
def test_setup_answers_503_when_the_backend_cannot_list(client, monkeypatch):
    _install(monkeypatch, _FakeBackend(error=RuntimeError("bus gone")))
    api = _client_as(client, "op", "operator")

    resp = api.get("/cameras/setup")

    assert resp.status_code == 503


@pytest.mark.unit
def test_devices_report_the_role_fields(client, monkeypatch):
    _install(monkeypatch, _FakeBackend(_devices()))
    _write_registry({A: _record("left", CONFIRMED, "1-1.2"), B: _record("right")})
    api = _client_as(client, "op", "operator")

    resp = api.get("/cameras/devices")

    assert resp.status_code == 200, resp.text
    rows = {row["index"]: row for row in resp.json()}
    assert rows[0]["usb_port"] == "1-1.2"
    assert rows[0]["serial"] == "1111111"
    assert rows[0]["role"] == "left"
    assert rows[0]["role_confirmed"] is True
    assert rows[0]["usb_port_at_confirmation"] == "1-1.2"
    assert rows[0]["role_guess"] == "left"
    assert rows[0]["role_guess_source"] == "gphoto2_pin"
    assert rows[1]["role"] == "right"
    assert rows[1]["role_confirmed"] is False
    assert rows[1]["usb_port_at_confirmation"] is None


@pytest.mark.unit
def test_a_row_without_role_fields_still_validates(client, monkeypatch):
    """A backend that reports none of the new keys, as picamera2 does."""
    bare = [{"index": 0, "model": "imx519", "hardware_id": "imx519_88000"}]
    _install(monkeypatch, _FakeBackend(bare))
    api = _client_as(client, "op", "operator")

    resp = api.get("/cameras/devices")

    assert resp.status_code == 200, resp.text
    row = resp.json()[0]
    for key in ("usb_port", "serial", "role", "usb_port_at_confirmation",
                "role_guess", "role_guess_source"):
        assert row[key] is None
    assert row["role_confirmed"] is False


@pytest.mark.unit
def test_the_registry_reads_missing_role_keys_as_none(tmp_path):
    from capture.camera_registry import CameraRegistry

    path = tmp_path / "cameras.json"
    path.write_text(json.dumps({
        "cameras": {A: _record(), B: _record("right", CONFIRMED, "1-1.3")},
        "version": "1.0",
    }))
    registry = CameraRegistry(path)

    assert registry.get_role_record(A) == {
        "role": None, "role_confirmed_at": None, "usb_port_at_confirmation": None,
    }
    assert registry.get_role_record(B) == {
        "role": "right", "role_confirmed_at": CONFIRMED,
        "usb_port_at_confirmation": "1-1.3",
    }
    assert registry.get_role_record("unknown") == {
        "role": None, "role_confirmed_at": None, "usb_port_at_confirmation": None,
    }
    assert registry.get_confirmed_pair() is None


@pytest.mark.unit
@pytest.mark.parametrize("stored, expected", [
    ({"left": A, "right": B}, {"left": A, "right": B}),
    ({"left": A}, None),
    ({"left": A, "right": 3}, None),
    ("left", None),
    (None, None),
])
def test_the_registry_reads_only_a_whole_confirmed_pair(tmp_path, stored, expected):
    from capture.camera_registry import CameraRegistry

    path = tmp_path / "cameras.json"
    path.write_text(json.dumps({"cameras": {}, "version": "1.0", "confirmed_pair": stored}))

    assert CameraRegistry(path).get_confirmed_pair() == expected


@pytest.mark.unit
def test_reading_role_keys_writes_nothing(tmp_path):
    from capture.camera_registry import CameraRegistry

    path = tmp_path / "cameras.json"
    text = json.dumps({"cameras": {A: _record()}, "version": "1.0"})
    path.write_text(text)
    registry = CameraRegistry(path)

    registry.get_role_record(A)
    registry.get_confirmed_pair()

    assert path.read_text() == text
