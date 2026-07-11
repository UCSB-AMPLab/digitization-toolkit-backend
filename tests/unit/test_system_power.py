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


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.mark.unit
def test_power_poweroff_success(power_client, monkeypatch):
    """When the helper is present: 200, Spanish message, audit logged, helper
    dispatched via subprocess in the background task (not synchronously),
    with stdin closed so sudo can never sit waiting for a password."""
    import subprocess as real_subprocess
    import app.api.system as system

    monkeypatch.setattr(system.shutil, "which", lambda name: "/usr/bin/sudo")
    monkeypatch.setattr(system.os.path, "exists", lambda p: True)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _Result(0)

    monkeypatch.setattr(system.subprocess, "run", fake_run)

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
    cmd, kwargs = calls[-1]
    assert cmd == ["sudo", system.HELPER, "poweroff"]
    assert kwargs.get("stdin") == real_subprocess.DEVNULL


@pytest.mark.unit
def test_power_reboot_success(power_client, monkeypatch):
    """Reboot path returns the reboot message and dispatches `reboot`."""
    import app.api.system as system

    monkeypatch.setattr(system.shutil, "which", lambda name: "/usr/bin/sudo")
    monkeypatch.setattr(system.os.path, "exists", lambda p: True)

    calls = []
    monkeypatch.setattr(system.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or _Result(0))

    import app.core.audit as audit
    monkeypatch.setattr(audit, "log_event", lambda db, **k: None)

    resp = power_client.post("/system/power", json={"action": "reboot"})

    assert resp.status_code == 200
    assert "se reiniciará" in resp.json()["message"]
    assert calls[-1] == ["sudo", system.HELPER, "reboot"]


@pytest.mark.unit
def test_power_helper_failure_is_logged(power_client, monkeypatch):
    """A non-zero exit from the helper is not silent: the background task logs
    the return code and stderr via logger.error (there is no client left to
    inform, so logging is the only diagnostic channel)."""
    import app.api.system as system

    monkeypatch.setattr(system.shutil, "which", lambda name: "/usr/bin/sudo")
    monkeypatch.setattr(system.os.path, "exists", lambda p: True)
    monkeypatch.setattr(
        system.subprocess, "run",
        lambda cmd, **k: _Result(1, stderr="sudo: not permitted by sudoers"),
    )

    import app.core.audit as audit
    monkeypatch.setattr(audit, "log_event", lambda db, **k: None)

    errors = []
    monkeypatch.setattr(system.logger, "error",
                        lambda msg, *args: errors.append(msg % args))

    resp = power_client.post("/system/power", json={"action": "poweroff"})

    # The endpoint itself still returns 200 — the failure happens post-response.
    assert resp.status_code == 200
    assert errors, "helper failure should have been logged"
    assert "poweroff" in errors[-1]
    assert "rc=1" in errors[-1]
    assert "not permitted by sudoers" in errors[-1]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
