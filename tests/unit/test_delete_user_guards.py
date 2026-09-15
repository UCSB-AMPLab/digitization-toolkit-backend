"""Critical NEH-57 guards on DELETE /auth/users/{id}: no self-delete, no
last-active-admin delete (both would strand a headless unit), and an audit trail."""

import pytest


def _make_user(db_session, username, role, is_active=True):
    from app.models.user import User
    from app.core.security import hash_password
    u = User(username=username, email=f"{username}@example.com",
             hashed_password=hash_password("x"), role=role, is_active=is_active)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture
def admin_client(client, db_session):
    """TestClient authenticated as a real admin row, with a second admin present
    so ordinary deletions aren't blocked by the last-admin guard."""
    from app.main import app
    from app.api.auth import get_current_user
    admin = _make_user(db_session, "admin_caller", "admin")
    _make_user(db_session, "admin_other", "admin")
    app.dependency_overrides[get_current_user] = lambda: admin
    yield client, admin
    # the `client` fixture clears dependency_overrides on teardown


def test_admin_cannot_delete_self(admin_client):
    client, admin = admin_client
    resp = client.delete(f"/auth/users/{admin.id}")
    assert resp.status_code == 400
    assert "own account" in resp.json()["detail"]


def test_delete_user_writes_audit_entry(admin_client, db_session):
    from app.models.system_log import SystemLog
    client, _ = admin_client
    victim = _make_user(db_session, "victim", "reviewer")
    assert client.delete(f"/auth/users/{victim.id}").status_code == 200
    logged = db_session.query(SystemLog).filter(
        SystemLog.action == "user_deleted", SystemLog.subject == "victim"
    ).first()
    assert logged is not None


def test_cannot_delete_last_active_admin(client, db_session):
    # Exactly one active admin in the DB; the caller is a stand-in admin that is
    # not persisted, so the DB holds no *other* active admin — this exercises the
    # last-admin guard's count without the self-guard short-circuiting first.
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User
    sole_admin = _make_user(db_session, "sole_admin", "admin")
    caller = User(id=99999, username="ghost_admin", email="g@example.com",
                  hashed_password="x", role="admin", is_active=True)
    app.dependency_overrides[get_current_user] = lambda: caller
    resp = client.delete(f"/auth/users/{sole_admin.id}")
    assert resp.status_code == 400
    assert "last active admin" in resp.json()["detail"]
