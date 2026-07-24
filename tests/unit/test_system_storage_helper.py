#!/usr/bin/env python3
"""
Unit tests for the NEH-69 refactor: storage mount/umount/activate now shell out
through the single privileged helper /usr/local/bin/dtk-system-helper instead of
raw sudo mount/umount/mkdir/chown.

These assert the exact argv passed to subprocess.run so a future regression that
reintroduces raw sudo calls (or the hardcoded pi:pi chown) is caught. They also
assert the backend never mkdirs/rmdirs under the mounts root - the root is
root-owned and the helper is its only writer. Run with:
    python -m pytest tests/unit/test_system_storage_helper.py
"""

import pytest


def _admin():
    from app.models.user import User
    return User(username="admin1", email="a@example.com",
                hashed_password="x", role="admin", is_active=True)


@pytest.fixture
def admin_client(client, db_session):
    from app.main import app
    # system.py binds its own allow_admin = RoleChecker(["admin"]) instance, so
    # override that exact object (not the one re-exported from app.api.auth).
    import app.api.system as system
    app.dependency_overrides[system.allow_admin] = lambda: _admin()
    yield client


@pytest.fixture
def dir_op_recorder(monkeypatch):
    """Record any Path.mkdir / Path.rmdir the endpoints attempt.

    The mounts root is root-owned and the helper is its only writer, so the
    endpoints must not touch the filesystem there at all.
    """
    from pathlib import Path
    ops = []
    monkeypatch.setattr(Path, "mkdir",
                        lambda self, **k: ops.append(("mkdir", str(self))))
    monkeypatch.setattr(Path, "rmdir",
                        lambda self: ops.append(("rmdir", str(self))))
    return ops


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _one_vfat_partition():
    return [{
        "name": "sda1", "path": "/dev/sda1", "size": "1G",
        "fstype": "vfat", "mountpoint": None, "label": "USB",
        "removable": True, "type": "part",
    }]


@pytest.mark.unit
def test_mount_uses_helper_and_does_not_mkdir(admin_client, monkeypatch, dir_op_recorder):
    import app.api.system as system

    monkeypatch.setattr(system, "_parse_lsblk", _one_vfat_partition)

    calls = []
    monkeypatch.setattr(system.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or _Result(0))
    import app.core.audit as audit
    monkeypatch.setattr(audit, "log_event", lambda *a, **k: None)

    resp = admin_client.post("/system/storage/mount", json={"device": "/dev/sda1"})
    assert resp.status_code == 200, resp.text

    cmd = calls[-1]
    # Routed through the helper; no raw `mount`, no -o uid/gid built here.
    assert cmd[:3] == ["sudo", system.HELPER, "mount"]
    assert cmd[3] == "/dev/sda1"
    assert "-o" not in cmd

    # Creating the mountpoint directory is the helper's job now.
    assert dir_op_recorder == []


@pytest.mark.unit
def test_mount_failure_returns_500_without_cleanup(admin_client, monkeypatch, dir_op_recorder):
    """When the helper fails, surface its stderr as a 500 and do NOT try to
    rmdir the mountpoint - the helper cleans up its own directory."""
    import app.api.system as system

    monkeypatch.setattr(system, "_parse_lsblk", _one_vfat_partition)
    monkeypatch.setattr(system.subprocess, "run",
                        lambda cmd, **k: _Result(32, stderr="mount: wrong fs type"))
    import app.core.audit as audit
    monkeypatch.setattr(audit, "log_event", lambda *a, **k: None)

    resp = admin_client.post("/system/storage/mount", json={"device": "/dev/sda1"})
    assert resp.status_code == 500
    assert "wrong fs type" in resp.json()["detail"]
    assert dir_op_recorder == []


@pytest.mark.unit
def test_unmount_uses_helper_and_does_not_rmdir(admin_client, monkeypatch, dir_op_recorder):
    import app.api.system as system
    import app.core.storage_override as so

    # The endpoint imports these lazily from app.core.storage_override.
    monkeypatch.setattr(so, "get_storage_override", lambda: None)

    target = str(system._MOUNT_BASE / "USB")

    calls = []
    monkeypatch.setattr(system.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or _Result(0))
    import app.core.audit as audit
    monkeypatch.setattr(audit, "log_event", lambda *a, **k: None)

    resp = admin_client.request("DELETE", "/system/storage/mount",
                                json={"mountpoint": target})
    assert resp.status_code == 200, resp.text

    cmd = calls[-1]
    # Routed through the helper, not a bare `sudo umount`.
    assert cmd == ["sudo", system.HELPER, "umount", target]

    # Removing the mountpoint directory after umount is the helper's job now.
    assert dir_op_recorder == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
