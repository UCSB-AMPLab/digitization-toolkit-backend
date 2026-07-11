#!/usr/bin/env python3
"""
Unit tests for the /system/power endpoint (NEH-98).

These exercise the endpoint through the FastAPI TestClient with the auth and
DB dependencies overridden, and the privileged helper / subprocess mocked so
nothing is ever actually powered off. Run with:
    python -m pytest tests/unit/test_system_power.py
"""

import pytest


def _make_user(role="reviewer", username="operator1"):
    """Build a lightweight stand-in for an authenticated User row."""
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


@pytest.fixture
def power_client(client, db_session):
    """TestClient with get_current_user overridden to a plain authenticated user.

    Uses the `client` fixture (which already overrides the DB dependency) and
    additionally overrides get_current_user so no real token is needed.
    """
    from app.main import app
    from app.api.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _make_user()
    yield client
    # `client` fixture clears overrides on teardown


@pytest.mark.unit
def test_power_requires_authentication(client):
    """With no auth override and no token, the endpoint rejects the request."""
    resp = client.post("/system/power", json={"action": "poweroff"})
    assert resp.status_code == 401


@pytest.mark.unit
def test_power_rejects_invalid_action(power_client):
    """The Literal in the request model rejects anything but poweroff/reboot."""
    resp = power_client.post("/system/power", json={"action": "explode"})
    assert resp.status_code == 422


@pytest.mark.unit
def test_power_returns_501_when_helper_missing(power_client, monkeypatch):
    """On a host without the helper we refuse honestly with 501, not a fake 200."""
    import app.api.system as system
    monkeypatch.setattr(system.os.path, "exists", lambda p: False)

    resp = power_client.post("/system/power", json={"action": "poweroff"})
    assert resp.status_code == 501
    assert "no disponible" in resp.json()["detail"].lower()


@pytest.mark.unit
def test_power_poweroff_success(power_client, monkeypatch):
    """When the helper is present: 200, Spanish message, audit logged, helper
    dispatched via subprocess in the background task (not synchronously)."""
    import app.api.system as system

    monkeypatch.setattr(system.shutil, "which", lambda name: "/usr/bin/sudo")
    monkeypatch.setattr(system.os.path, "exists", lambda p: True)

    calls = []
    monkeypatch.setattr(system.subprocess, "run",
                        lambda *a, **k: calls.append(a[0]))

    logged = {}

    def fake_log_event(db, **kwargs):
        logged.update(kwargs)

    import app.core.audit as audit
    monkeypatch.setattr(audit, "log_event", fake_log_event)

    resp = power_client.post("/system/power", json={"action": "poweroff"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "poweroff"
    assert "se apagará" in body["message"]

    # Audit fired before the action, with the right category/action/actor.
    assert logged.get("category") == "system"
    assert logged.get("action") == "power_poweroff"
    assert logged.get("actor") == "operator1"

    # TestClient runs background tasks after the response; the helper was called.
    assert calls, "background task should have invoked the helper"
    assert calls[-1] == ["sudo", system.HELPER, "poweroff"]


@pytest.mark.unit
def test_power_reboot_success(power_client, monkeypatch):
    """Reboot path returns the reboot message and dispatches `reboot`."""
    import app.api.system as system

    monkeypatch.setattr(system.shutil, "which", lambda name: "/usr/bin/sudo")
    monkeypatch.setattr(system.os.path, "exists", lambda p: True)

    calls = []
    monkeypatch.setattr(system.subprocess, "run",
                        lambda *a, **k: calls.append(a[0]))

    import app.core.audit as audit
    monkeypatch.setattr(audit, "log_event", lambda db, **k: None)

    resp = power_client.post("/system/power", json={"action": "reboot"})

    assert resp.status_code == 200
    assert "se reiniciará" in resp.json()["message"]
    assert calls[-1] == ["sudo", system.HELPER, "reboot"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
