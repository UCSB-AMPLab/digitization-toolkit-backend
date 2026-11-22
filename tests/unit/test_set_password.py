"""An administrator sets a new password for another user: PUT /auth/users/{user_id}/password (NEH-395).

A person who forgets their password is given a new one by an
administrator. The route takes {"new_password": str}, held to the rules
POST /auth/register holds a password to, and sets the hash and raises the
user's token_version in one UPDATE, so every session of that user ends and
no other user's does. It answers the user as the other user routes do and
writes one system-log row naming who set whose password.

It refuses, each with {code, message} and nothing written:

- a user id with no row: 404 `user_not_found`;
- the testing account: the refusal the other user routes give it, 409
  `testing_account` (its password is set through /system/testing only);
- the administrator's own account: 409 `use_own_password_change`, since an
  administrator changes their own password through POST /auth/password-reset,
  which asks for the current one.

Operators, reviewers and the testing account are refused as on every other
administrator route. No response and no log line carries the password or
its hash.

    python -m pytest tests/unit/test_set_password.py

Version: 0.3.0
"""

import logging

import pytest

from tests.unit.test_signout_ends_sessions import INCREMENT, _Statements, _version
from tests.unit.test_testing_space_admin import (
    OTHER_PASSWORD,
    PASSWORD,
    _account,
    _bearer,
    _login,
    _ordinary_user,
    _real_auth,
    _switch_on,
    _token_for,
    _works,
    fresh_throttle,  # noqa: F401  (autouse fixture)
    sends,  # noqa: F401  (autouse fixture)
)
from tests.unit.test_unit_mode import _logs

NEW_PASSWORD = "fake-new-pw-395"
USER_SHAPE = {"id", "username", "email", "role", "is_active", "created_at"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set(client, token, user_id, body=None):
    """PUT /auth/users/{user_id}/password with a bearer token; the body defaults to NEW_PASSWORD."""
    _real_auth()
    client.cookies.clear()
    if body is None:
        body = {"new_password": NEW_PASSWORD}
    return client.put(f"/auth/users/{user_id}/password", headers=_bearer(token), json=body)


def _admin_session(client, db, username="jefa"):
    admin = _ordinary_user(db, username, role="admin")
    return admin, _token_for(client, username, PASSWORD)


def _hash(db, user_id):
    from app.models.user import User
    db.expire_all()
    return db.get(User, user_id).hashed_password


def _code(resp, status, code):
    assert resp.status_code == status, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == code, detail
    assert detail["message"]
    return detail


def _users_writes(stmts):
    return [s for s in stmts.seen if s.lstrip().upper().startswith("UPDATE") and "users" in s.lower()]


def _no_row_written(db, stmts, before_hash, user_id, version=0):
    assert _users_writes(stmts) == [], _users_writes(stmts)
    assert _hash(db, user_id) == before_hash
    assert _version(db, user_id) == version
    assert _logs(db, "user_password_set") == []


# ---------------------------------------------------------------------------
# An administrator sets an operator's, a reviewer's and another administrator's password
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("role", ["operator", "reviewer", "admin"])
def test_an_administrator_sets_another_users_password(client, db_session, role):
    _, admin_token = _admin_session(client, db_session)
    target = _ordinary_user(db_session, "ana", role=role)
    bystander = _ordinary_user(db_session, "luis", role="reviewer")
    first = _token_for(client, "ana", PASSWORD)
    second = _token_for(client, "ana", PASSWORD)
    theirs = _token_for(client, "luis", PASSWORD)

    resp = _set(client, admin_token, target.id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == USER_SHAPE
    assert (body["id"], body["username"], body["role"]) == (target.id, "ana", role)
    assert _login(client, "ana", PASSWORD).status_code == 401
    assert _works(client, _token_for(client, "ana", NEW_PASSWORD))
    assert not _works(client, first)
    assert not _works(client, second)
    assert _works(client, theirs)
    assert _works(client, admin_token)
    assert _version(db_session, target.id) == 1
    assert _version(db_session, bystander.id) == 0


@pytest.mark.unit
def test_an_inactive_users_password_is_set_and_they_stay_inactive(client, db_session):
    from app.models.user import User
    _, admin_token = _admin_session(client, db_session)
    target = _ordinary_user(db_session, "ana")
    target.is_active = False
    db_session.commit()
    resp = _set(client, admin_token, target.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is False
    db_session.expire_all()
    assert db_session.get(User, target.id).is_active is False


# ---------------------------------------------------------------------------
# Refusals, each with nothing written
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_user_id_with_no_row_is_user_not_found(client, db_session):
    admin, admin_token = _admin_session(client, db_session)
    before = _hash(db_session, admin.id)
    with _Statements(db_session) as stmts:
        resp = _set(client, admin_token, 987654)
    _code(resp, 404, "user_not_found")
    _no_row_written(db_session, stmts, before, admin.id)


@pytest.mark.unit
def test_the_testing_account_as_target_is_refused_as_the_other_user_routes_refuse_it(client, db_session):
    _, admin_token = _admin_session(client, db_session)
    _switch_on(client)
    [account] = _account(db_session)
    before = account.hashed_password
    testing_token = _token_for(client, "pruebas", PASSWORD)

    _real_auth()
    client.cookies.clear()
    role_resp = client.patch(f"/auth/users/{account.id}/role", headers=_bearer(admin_token),
                             json={"role": "operator"})
    with _Statements(db_session) as stmts:
        resp = _set(client, admin_token, account.id)

    expected = role_resp.json()["detail"]
    assert (resp.status_code, resp.json()["detail"]) == (role_resp.status_code, expected)
    _code(resp, 409, "testing_account")
    _no_row_written(db_session, stmts, before, account.id)
    assert _works(client, testing_token)
    assert _login(client, "pruebas", PASSWORD).status_code == 200


@pytest.mark.unit
def test_the_administrators_own_account_is_use_own_password_change(client, db_session):
    admin, admin_token = _admin_session(client, db_session)
    before = _hash(db_session, admin.id)
    with _Statements(db_session) as stmts:
        resp = _set(client, admin_token, admin.id)
    _code(resp, 409, "use_own_password_change")
    _no_row_written(db_session, stmts, before, admin.id)
    assert _works(client, admin_token)
    assert _login(client, "jefa", PASSWORD).status_code == 200


@pytest.mark.unit
@pytest.mark.parametrize("role", ["operator", "reviewer"])
def test_an_operator_or_a_reviewer_is_refused_as_on_the_other_admin_routes(client, db_session, role):
    _ordinary_user(db_session, "caller", role=role)
    target = _ordinary_user(db_session, "ana", role="reviewer")
    token = _token_for(client, "caller", PASSWORD)
    before = _hash(db_session, target.id)

    _real_auth()
    client.cookies.clear()
    other = client.get("/auth/users", headers=_bearer(token))
    with _Statements(db_session) as stmts:
        resp = _set(client, token, target.id)

    assert other.status_code == 403
    assert (resp.status_code, resp.json()) == (other.status_code, other.json())
    _no_row_written(db_session, stmts, before, target.id)


@pytest.mark.unit
def test_the_testing_account_as_caller_is_refused_as_on_the_other_admin_routes(client, db_session):
    _switch_on(client)
    target = _ordinary_user(db_session, "ana")
    token = _token_for(client, "pruebas", PASSWORD)
    before = _hash(db_session, target.id)

    _real_auth()
    client.cookies.clear()
    other = client.get("/auth/users", headers=_bearer(token))
    with _Statements(db_session) as stmts:
        resp = _set(client, token, target.id)

    assert other.status_code == 403
    assert (resp.status_code, resp.json()) == (other.status_code, other.json())
    _no_row_written(db_session, stmts, before, target.id)


@pytest.mark.unit
def test_a_deactivated_administrator_with_a_current_cookie_is_refused(client, db_session):
    """Deactivation raises the version through its route; here only the flag is cleared, so the cookie stays current."""
    from app.models.user import User
    from tests.unit.test_signout_ends_sessions import _cookie_session, _with_cookie
    _ordinary_user(db_session, "otra", role="admin")
    target = _ordinary_user(db_session, "ana")
    cookie = _cookie_session(client, "otra")
    before = _hash(db_session, target.id)
    db_session.query(User).filter(User.username == "otra").update({User.is_active: False})
    db_session.commit()
    _real_auth()
    client.cookies.clear()
    resp = client.put(f"/auth/users/{target.id}/password", headers=_with_cookie(cookie),
                      json={"new_password": NEW_PASSWORD})
    assert resp.status_code == 401, resp.text
    assert _hash(db_session, target.id) == before
    assert _version(db_session, target.id) == 0


@pytest.mark.unit
def test_without_a_session_the_route_is_a_401(client, db_session):
    target = _ordinary_user(db_session, "ana")
    _real_auth()
    client.cookies.clear()
    resp = client.put(f"/auth/users/{target.id}/password", json={"new_password": NEW_PASSWORD})
    assert resp.status_code == 401
    assert _version(db_session, target.id) == 0


@pytest.mark.unit
def test_a_stale_administrator_token_is_refused(client, db_session):
    admin, admin_token = _admin_session(client, db_session)
    target = _ordinary_user(db_session, "ana")
    from app.api import auth as auth_mod
    auth_mod.end_sessions(db_session, admin.id)
    db_session.commit()
    resp = _set(client, admin_token, target.id)
    assert resp.status_code == 401
    assert _version(db_session, target.id) == 0


# ---------------------------------------------------------------------------
# One UPDATE sets the hash and raises the session generation
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_change_is_one_update_that_sets_the_hash_and_increments_token_version(client, db_session):
    _, admin_token = _admin_session(client, db_session)
    target = _ordinary_user(db_session, "ana")
    with _Statements(db_session) as stmts:
        resp = _set(client, admin_token, target.id)
    assert resp.status_code == 200, resp.text
    writes = _users_writes(stmts)
    assert len(writes) == 1, writes
    assert "hashed_password" in writes[0], writes[0]
    assert INCREMENT.search(writes[0]), writes[0]


# ---------------------------------------------------------------------------
# The password rules are registration's
# ---------------------------------------------------------------------------

# Bodies each route is sent (the password value, or the whole body for a
# missing field), and the status every one of the routes answers.
RULE_CASES = [
    pytest.param({"new_password": "x"}, {"password": "x"}, 422, id="one-character"),
    pytest.param({"new_password": ""}, {"password": ""}, 422, id="empty"),
    pytest.param({"new_password": " " * 3}, {"password": " " * 3}, 422, id="spaces"),
    pytest.param({"new_password": "fake-pw"}, {"password": "fake-pw"}, 422, id="seven"),
    pytest.param({"new_password": "fake-pw8"}, {"password": "fake-pw8"}, 200, id="eight"),
    pytest.param({"new_password": "y" * 500}, {"password": "y" * 500}, 200, id="long"),
    pytest.param({"new_password": 12345}, {"password": 12345}, 422, id="a-number"),
    pytest.param({"new_password": None}, {"password": None}, 422, id="null"),
    pytest.param({"new_password": ["x"]}, {"password": ["x"]}, 422, id="a-list"),
    pytest.param({}, {}, 422, id="missing"),
]


@pytest.mark.unit
@pytest.mark.parametrize("set_body,register_part,status", RULE_CASES)
def test_a_password_is_accepted_or_refused_as_registration_accepts_or_refuses_it(
        client, db_session, set_body, register_part, status):
    _, admin_token = _admin_session(client, db_session)
    target = _ordinary_user(db_session, "ana")
    before = _hash(db_session, target.id)

    _real_auth()
    client.cookies.clear()
    register = client.post("/auth/register", headers=_bearer(admin_token),
                           json={"username": "nuevo", **register_part})
    resp = _set(client, admin_token, target.id, body=set_body)

    assert register.status_code == status, register.text
    assert resp.status_code == status, resp.text
    if resp.status_code == 422:
        assert _hash(db_session, target.id) == before
        assert _version(db_session, target.id) == 0


# ---------------------------------------------------------------------------
# The system log, and no password or hash anywhere
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_one_system_log_row_names_who_set_whose_password(client, db_session):
    _, admin_token = _admin_session(client, db_session)
    target = _ordinary_user(db_session, "ana")
    assert _set(client, admin_token, target.id).status_code == 200
    rows = _logs(db_session, "user_password_set")
    assert [(r.level, r.category, r.actor, r.subject) for r in rows] == [("INFO", "access", "jefa", "ana")]
    text = " ".join(str(v) for v in (rows[0].actor, rows[0].subject, rows[0].detail))
    assert NEW_PASSWORD not in text and PASSWORD not in text


@pytest.mark.unit
def test_no_response_or_log_line_carries_the_password_or_its_hash(client, db_session, caplog):
    from app.models.system_log import SystemLog
    caplog.set_level(logging.DEBUG)
    admin, admin_token = _admin_session(client, db_session)
    target = _ordinary_user(db_session, "ana")
    responses = [
        _set(client, admin_token, target.id),
        _set(client, admin_token, target.id, body={"new_password": OTHER_PASSWORD}),
        _set(client, admin_token, admin.id, body={"new_password": NEW_PASSWORD}),
        _set(client, admin_token, 987654, body={"new_password": NEW_PASSWORD}),
        _set(client, admin_token, target.id, body={"new_password": [NEW_PASSWORD]}),
    ]
    stored = _hash(db_session, target.id)
    digest = stored.split("$")[-1]
    for resp in responses:
        for secret in (NEW_PASSWORD, OTHER_PASSWORD, stored, digest):
            assert secret not in resp.text
        assert "hashed_password" not in resp.text
    for secret in (NEW_PASSWORD, OTHER_PASSWORD, stored, digest):
        assert secret not in caplog.text
    db_session.expire_all()
    for row in db_session.query(SystemLog).all():
        text = " ".join(str(v) for v in (row.actor, row.subject, row.detail))
        for secret in (NEW_PASSWORD, OTHER_PASSWORD, stored, digest):
            assert secret not in text


@pytest.mark.unit
def test_nothing_is_sent_to_fisqua(client, db_session, sends):
    _, admin_token = _admin_session(client, db_session)
    target = _ordinary_user(db_session, "ana")
    assert _set(client, admin_token, target.id).status_code == 200
    assert sends == []


# ---------------------------------------------------------------------------
# The self-service route: the rules, the current password, every session ended
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("set_body,register_part,status", RULE_CASES)
def test_the_own_password_change_holds_the_new_password_to_registrations_rules(
        client, db_session, set_body, register_part, status):
    _, admin_token = _admin_session(client, db_session)
    _ordinary_user(db_session, "ana")
    token = _token_for(client, "ana", PASSWORD)

    _real_auth()
    client.cookies.clear()
    register = client.post("/auth/register", headers=_bearer(admin_token),
                           json={"username": "nuevo", **register_part})
    body = {"old_password": PASSWORD, **set_body}
    resp = client.post("/auth/password-reset", headers=_bearer(token), json=body)

    assert register.status_code == status, register.text
    assert resp.status_code == status, resp.text


@pytest.mark.unit
def test_the_own_password_change_needs_the_current_password(client, db_session):
    user = _ordinary_user(db_session, "ana")
    token = _token_for(client, "ana", PASSWORD)
    _real_auth()
    client.cookies.clear()
    missing = client.post("/auth/password-reset", headers=_bearer(token), json={"new_password": NEW_PASSWORD})
    wrong = client.post("/auth/password-reset", headers=_bearer(token),
                        json={"old_password": "fake-wrong-pw", "new_password": NEW_PASSWORD})
    assert missing.status_code == 422
    assert wrong.status_code == 401 and wrong.json() == {"detail": "Old password incorrect"}
    assert _version(db_session, user.id) == 0
    assert _login(client, "ana", PASSWORD).status_code == 200


@pytest.mark.unit
def test_an_administrator_changes_their_own_password_with_the_current_one(client, db_session):
    admin, admin_token = _admin_session(client, db_session)
    other_screen = _token_for(client, "jefa", PASSWORD)
    _real_auth()
    client.cookies.clear()
    resp = client.post("/auth/password-reset", headers=_bearer(admin_token),
                       json={"old_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"detail": "password updated successfully"}
    assert not _works(client, admin_token)
    assert not _works(client, other_screen)
    assert _version(db_session, admin.id) == 1
    assert _works(client, _token_for(client, "jefa", NEW_PASSWORD))
