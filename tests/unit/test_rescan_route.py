"""POST /cameras/rescan: the operator's lever when a DSLR body drops off USB.

The route asks the process-wide backend singleton to re-detect hardware and
drop stale sessions (NEH-229), then returns the same enriched DeviceInfo list
GET /cameras/devices returns, so the frontend can reuse one shape.

Rescan mutates backend state, so it sits behind allow_contributor rather than
allow_read_only: a reviewer may look at the device list but may not reset the
hardware under someone else's capture run. A backend that raises is a 503 with
the reason attached - unlike enumeration, an empty list would read as "no
bodies attached" and hide the failure.
"""

import pytest

import capture.service as capture_service


DEVICES = [
    {
        "index": 0,
        "model": "Canon EOS Rebel T7",
        "hardware_id": "canoneosrebelt7_1111111",
        "serial": "1111111",
        "location": "USB usb:001,004",
        "port": "usb:001,004",
        "has_aperture_control": True,
        "supports_zoom": False,
    },
    {
        "index": 1,
        "model": "Canon EOS Rebel T7",
        "hardware_id": "canoneosrebelt7_2222222",
        "serial": "2222222",
        "location": "USB usb:001,009",
        "port": "usb:001,009",
        "has_aperture_control": True,
        "supports_zoom": False,
    },
]


class _FakeBackend:
    """Stand-in for the process singleton returned by get_backend()."""

    def __init__(self, devices=None, exc=None):
        self._devices = devices if devices is not None else []
        self._exc = exc
        self.calls = 0

    def rescan(self):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return [dict(d) for d in self._devices]

    def list_devices(self):
        raise AssertionError(
            "the rescan route must call rescan(), which re-detects hardware; "
            "list_devices() only reads the cached port map"
        )


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


def _install_backend(monkeypatch, backend):
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    return backend


@pytest.mark.unit
def test_rescan_returns_the_enriched_device_list_for_an_operator(client, monkeypatch):
    backend = _install_backend(monkeypatch, _FakeBackend(DEVICES))
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/rescan")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [d["hardware_id"] for d in body] == [
        "canoneosrebelt7_1111111",
        "canoneosrebelt7_2222222",
    ]
    assert [d["index"] for d in body] == [0, 1]
    assert [d["location"] for d in body] == ["USB usb:001,004", "USB usb:001,009"]
    assert all(d["model"] == "Canon EOS Rebel T7" for d in body)
    assert all(d["has_aperture_control"] is True for d in body)
    assert all(d["supports_zoom"] is False for d in body)
    assert all(d["calibrated"] is False for d in body)
    assert backend.calls == 1


@pytest.mark.unit
def test_rescan_is_forbidden_for_a_reviewer(client, monkeypatch):
    backend = _install_backend(monkeypatch, _FakeBackend(DEVICES))
    api = _client_as(client, "rev", "reviewer")

    resp = api.post("/cameras/rescan")

    assert resp.status_code == 403, resp.text
    assert backend.calls == 0, "a forbidden caller reached the hardware"


@pytest.mark.unit
def test_rescan_reports_a_backend_failure_as_503(client, monkeypatch):
    backend = _install_backend(
        monkeypatch, _FakeBackend(exc=RuntimeError("usb bus went away"))
    )
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/rescan")

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert "Camera rescan failed" in detail
    assert "usb bus went away" in detail
    assert backend.calls == 1
