"""CameraRegistry.set_roles: the operator's confirmed roles, stored (NEH-342).

set_roles writes "left" on one body's record and "right" on the other's,
clears those roles from every other record, and stores the pair confirmed
together, all in one replace of cameras.json. Nothing reads a stored role
for indexes, pins or capture in this step.

Version: 0.1.0-beta
"""

import json
from datetime import datetime, timedelta

import pytest

import capture.camera_registry as camera_registry_module
from capture.camera_registry import CameraRegistry


A = "canoneosrebelt7_1111111"
B = "canoneosrebelt7_2222222"
C = "canoneosrebelt7_3333333"

PORTS = {"left": "1-1.2", "right": "1-1.3"}


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


def _registry(tmp_path, cameras, **top):
    path = tmp_path / "cameras.json"
    data = {"cameras": cameras, "version": "1.0"}
    data.update(top)
    path.write_text(json.dumps(data))
    return CameraRegistry(registry_path=path)


def _on_disk(registry):
    return json.loads(registry.registry_path.read_text())


def _count_replaces(monkeypatch):
    calls = []
    real = camera_registry_module.atomic_write

    def counting(path, write_fn):
        calls.append(str(path))
        return real(path, write_fn)

    monkeypatch.setattr(camera_registry_module, "atomic_write", counting)
    return calls


def _is_recent_utc(stamp):
    parsed = datetime.fromisoformat(stamp)
    assert parsed.utcoffset() == timedelta(0)
    return abs(datetime.now(parsed.tzinfo) - parsed) < timedelta(minutes=1)


@pytest.mark.unit
def test_set_roles_writes_both_roles_and_the_pair_in_one_replace(tmp_path, monkeypatch):
    registry = _registry(tmp_path, {A: _record(), B: _record()})
    replaces = _count_replaces(monkeypatch)

    registry.set_roles(A, B, ports=PORTS, backend="gphoto2", confirmed_by="op")

    assert replaces == [str(registry.registry_path)]
    data = _on_disk(registry)
    left, right = data["cameras"][A], data["cameras"][B]
    assert left["role"] == "left"
    assert right["role"] == "right"
    assert left["role_confirmed_by"] == right["role_confirmed_by"] == "op"
    assert left["usb_port_at_confirmation"] == "1-1.2"
    assert right["usb_port_at_confirmation"] == "1-1.3"
    assert _is_recent_utc(left["role_confirmed_at"])
    assert left["role_confirmed_at"] == right["role_confirmed_at"]
    pair = data["confirmed_pair"]
    assert pair == {
        "left": A,
        "right": B,
        "ports": {"left": "1-1.2", "right": "1-1.3"},
        "backend": "gphoto2",
        "confirmed_at": left["role_confirmed_at"],
        "confirmed_by": "op",
    }


@pytest.mark.unit
def test_set_roles_is_read_back_by_the_role_readers(tmp_path):
    registry = _registry(tmp_path, {A: _record(), B: _record()})

    registry.set_roles(A, B, ports=PORTS, backend="chdk", confirmed_by="op")

    fresh = CameraRegistry(registry_path=registry.registry_path)
    assert fresh.get_confirmed_pair() == {"left": A, "right": B}
    assert fresh.get_role_record(A)["role"] == "left"
    assert fresh.get_role_record(B)["role"] == "right"
    # The instance that wrote holds the state it wrote.
    assert registry.get_role_record(B)["usb_port_at_confirmation"] == "1-1.3"


@pytest.mark.unit
def test_a_port_may_be_unknown(tmp_path):
    registry = _registry(tmp_path, {A: _record(), B: _record()})

    registry.set_roles(A, B, ports={"left": None, "right": "1-1.3"},
                       backend="picamera2", confirmed_by="op")

    data = _on_disk(registry)
    assert data["cameras"][A]["usb_port_at_confirmation"] is None
    assert data["confirmed_pair"]["ports"] == {"left": None, "right": "1-1.3"}


@pytest.mark.unit
def test_set_roles_clears_the_roles_from_every_other_record(tmp_path):
    held = dict(role="left", role_confirmed_at="2026-11-01T00:00:00+00:00",
                role_confirmed_by="old", usb_port_at_confirmation="1-1.4")
    registry = _registry(tmp_path, {
        A: _record(),
        B: _record(),
        C: _record(orientation=90, zoom_step=3, **held),
        "imx519_88000": _record(role="right", role_confirmed_at="2026-11-01T00:00:00+00:00"),
    })

    registry.set_roles(A, B, ports=PORTS, backend="gphoto2", confirmed_by="op")

    cameras = _on_disk(registry)["cameras"]
    for other in (C, "imx519_88000"):
        for key in ("role", "role_confirmed_at", "role_confirmed_by", "usb_port_at_confirmation"):
            assert key not in cameras[other]
    # An absent body keeps the rest of its record.
    assert cameras[C]["orientation"] == 90
    assert cameras[C]["zoom_step"] == 3
    holders = [hw for hw, rec in cameras.items() if rec.get("role") in ("left", "right")]
    assert sorted(holders) == [A, B]


@pytest.mark.unit
def test_set_roles_leaves_unrelated_keys_untouched(tmp_path):
    calibration = {"focus": {"success": True, "lens_position": 2.5}}
    registry = _registry(
        tmp_path,
        {
            A: _record(orientation=270, zoom_step=2, calibration=calibration, label="L"),
            B: _record(orientation=90, machine_id="CAM-B"),
        },
        version="1.0",
        something_else={"kept": True},
    )
    before = _on_disk(registry)

    registry.set_roles(A, B, ports=PORTS, backend="gphoto2", confirmed_by="op")

    after = _on_disk(registry)
    role_keys = {"role", "role_confirmed_at", "role_confirmed_by", "usb_port_at_confirmation"}
    for hw_id in (A, B):
        kept = {k: v for k, v in after["cameras"][hw_id].items() if k not in role_keys}
        assert kept == before["cameras"][hw_id]
    assert after["something_else"] == {"kept": True}
    assert after["version"] == "1.0"


@pytest.mark.unit
def test_set_roles_exchanges_the_roles_of_two_bodies(tmp_path):
    registry = _registry(tmp_path, {A: _record(), B: _record()})
    registry.set_roles(A, B, ports=PORTS, backend="gphoto2", confirmed_by="op")

    registry.set_roles(B, A, ports={"left": "1-1.3", "right": "1-1.2"},
                       backend="gphoto2", confirmed_by="op2")

    data = _on_disk(registry)
    assert data["cameras"][B]["role"] == "left"
    assert data["cameras"][A]["role"] == "right"
    assert data["cameras"][B]["usb_port_at_confirmation"] == "1-1.3"
    assert data["cameras"][A]["role_confirmed_by"] == "op2"
    assert data["confirmed_pair"]["left"] == B
    assert data["confirmed_pair"]["right"] == A


@pytest.mark.unit
@pytest.mark.parametrize("left, right", [(A, A), ("", B), (A, ""), (None, B)])
def test_set_roles_refuses_the_same_or_an_empty_id_and_writes_nothing(tmp_path, left, right):
    registry = _registry(tmp_path, {A: _record(), B: _record()})
    text = registry.registry_path.read_text()

    with pytest.raises(ValueError):
        registry.set_roles(left, right, ports=PORTS, backend="gphoto2", confirmed_by="op")

    assert registry.registry_path.read_text() == text


@pytest.mark.unit
def test_set_roles_refuses_an_id_with_no_record_and_writes_nothing(tmp_path):
    registry = _registry(tmp_path, {A: _record(), C: _record(role="left", role_confirmed_at="x")})
    text = registry.registry_path.read_text()

    with pytest.raises(KeyError):
        registry.set_roles(A, B, ports=PORTS, backend="gphoto2", confirmed_by="op")

    assert registry.registry_path.read_text() == text
