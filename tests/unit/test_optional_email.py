"""NEH-162: email is optional on account creation.

The appliance runs offline and nothing verifies an address, so the email
stays as a field on users but stops being required. Covers the
registration API (app/schemas/user.py, app/api/auth.py), the model
column (app/models/user.py), and the project-member listing/add
endpoints that flatten a user's email onto each row
(app/schemas/project_member.py, app/api/projects.py) — an email-less
admin or collaborator must not break either of those (R46-1).
"""

import pytest


def _register(client, username, password="pass1234", email=...):
    """POST /auth/register. `email` defaults to the sentinel `...` meaning
    "omit the key entirely"; pass None or "" to send those values explicitly."""
    payload = {"username": username, "password": password}
    if email is not ...:
        payload["email"] = email
    return client.post("/auth/register", json=payload)


def _login(client, username, password="pass1234"):
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _admin_auth_header(client, admin_username, admin_password="pass1234"):
    token = _login(client, admin_username, admin_password)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_register_without_email_returns_200_with_null_email(client):
    # /auth/register has no status_code=201 on its route decorator, so it
    # returns FastAPI's default 200 (pre-existing behavior, unrelated to
    # NEH-162; the brief for this test named 201, verified against the
    # running code to be 200).
    resp = _register(client, "u_no_email")
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] is None


def test_two_users_without_email_both_register(client):
    # First user bootstraps as admin (no auth required in dev with no
    # BOOTSTRAP_TOKEN configured); the second registration needs that
    # admin's token since bootstrap only applies once.
    first = _register(client, "u_first")
    assert first.status_code == 200, first.text
    assert first.json()["email"] is None

    headers = _admin_auth_header(client, "u_first")
    second = client.post(
        "/auth/register",
        json={"username": "u_second", "password": "pass1234"},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["email"] is None


def test_empty_string_email_registers_and_is_stored_as_null(client):
    resp = _register(client, "u_empty_email", email="")
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] is None


def test_duplicate_nonempty_email_is_still_409(client):
    first = _register(client, "u_dup_email_1", email="dup@example.com")
    assert first.status_code == 200, first.text

    headers = _admin_auth_header(client, "u_dup_email_1")
    second = client.post(
        "/auth/register",
        json={"username": "u_dup_email_2", "password": "pass1234", "email": "dup@example.com"},
        headers=headers,
    )
    assert second.status_code == 409
    assert "already exists" in second.json()["detail"]


def test_duplicate_username_with_no_email_is_409(client):
    first = _register(client, "u_dup_username")
    assert first.status_code == 200, first.text

    headers = _admin_auth_header(client, "u_dup_username")
    second = client.post(
        "/auth/register",
        json={"username": "u_dup_username", "password": "pass1234"},
        headers=headers,
    )
    assert second.status_code == 409
    assert "already exists" in second.json()["detail"]


def test_user_email_column_is_nullable():
    from app.models.user import User

    assert User.__table__.c.email.nullable is True


# ---------------------------------------------------------------------------
# Project members (R46-1) — ProjectMemberRead.email must tolerate None
# ---------------------------------------------------------------------------

def _make_user(db_session, username, role, email=None, is_active=True):
    from app.models.user import User
    from app.core.security import hash_password
    u = User(username=username, email=email, hashed_password=hash_password("x"),
              role=role, is_active=is_active)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def _make_project(db_session, created_by):
    from app.models.project import Project
    p = Project(name=f"project-{created_by}", created_by=created_by)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def emailless_admin_client(client, db_session):
    """TestClient authenticated as an admin row that has no email."""
    from app.main import app
    from app.api.auth import get_current_user
    admin = _make_user(db_session, "emailless_admin", "admin", email=None)
    app.dependency_overrides[get_current_user] = lambda: admin
    yield client, admin


def test_list_members_with_emailless_admin_returns_200_with_null_email(emailless_admin_client, db_session):
    client, admin = emailless_admin_client
    project = _make_project(db_session, admin.username)

    resp = client.get(f"/projects/{project.id}/members")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    admin_rows = [r for r in rows if r["user_id"] == admin.id]
    assert len(admin_rows) == 1
    assert admin_rows[0]["email"] is None
    assert admin_rows[0]["is_implicit"] is True


def test_add_emailless_collaborator_returns_200_and_lists(emailless_admin_client, db_session):
    client, admin = emailless_admin_client
    project = _make_project(db_session, admin.username)
    collaborator = _make_user(db_session, "emailless_operator", "operator", email=None)

    resp = client.post(
        f"/projects/{project.id}/members",
        json={"user_id": collaborator.id, "role": "operator"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["email"] is None

    listing = client.get(f"/projects/{project.id}/members")
    assert listing.status_code == 200, listing.text
    collaborator_rows = [r for r in listing.json() if r["user_id"] == collaborator.id]
    assert len(collaborator_rows) == 1
    assert collaborator_rows[0]["email"] is None


@pytest.mark.parametrize("bad", [123, ["a@b.co"], {"address": "a@b.co"}, True])
def test_a_non_string_email_is_a_422_not_a_server_error(client, bad):
    """R47-1: a "before" validator sees the raw payload value, so a number,
    a list, an object or a boolean must be refused as a format error rather
    than crash on .strip()."""
    resp = client.post("/auth/register", json={"username": "u_bad_email", "email": bad, "password": "Passw0rd!x"})
    assert resp.status_code == 422, resp.text
