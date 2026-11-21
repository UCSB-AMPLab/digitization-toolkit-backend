"""Signing out, a new password and a deactivation end a user's sessions on the backend (NEH-382).

A unit is shared by operators, reviewers and administrators, often on one
screen. When a person signs out, POST /auth/logout raises that person's
token_version, so every token issued to them before is refused by each
route that reads a token to a user: GET /users/me (get_current_user),
POST /auth/refresh, POST /auth/password-reset and the caller check of
POST /auth/register. An ordinary user signed in on two screens is signed
out of both. The testing account, which several people share at once, is
the exception: its sign-out only clears the cookie.

A user's own new password, and an admin deactivating a user, end that
user's sessions in the same way; making the user active again does not
lower the generation. Every raise is one UPDATE that adds one in the
database.

    python -m pytest tests/unit/test_signout_ends_sessions.py

Version: 0.3.3
"""

import re

import pytest

from tests.unit.test_capture_manifest_export import _as
from tests.unit.test_testing_space_admin import (
    OTHER_PASSWORD,
    PASSWORD,
    _account,
    _bearer,
    _hand_made_token,
    _login,
    _ordinary_user,
    _real_auth,
    _switch_on,
    _token_for,
    _works,
    fresh_throttle,  # noqa: F401  (autouse fixture)
    sends,  # noqa: F401  (autouse fixture)
)

CSRF = {"X-Requested-With": "XMLHttpRequest"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _logout(client, token=None, cookie=None):
    """POST /auth/logout with the CSRF header and either a bearer token or a cookie (or neither)."""
    _real_auth()
    client.cookies.clear()
    headers = dict(CSRF)
    if token is not None:
        headers.update(_bearer(token))
    if cookie is not None:
        headers["Cookie"] = f"dtk_session={cookie}"
    return client.post("/auth/logout", headers=headers)


def _version(db, user_id):
    from app.models.user import User
    db.expire_all()
    return db.get(User, user_id).token_version


def _cookie_session(client, username, password=PASSWORD):
    """Sign in as the browser app does and return the cookie's value."""
    _real_auth()
    client.cookies.clear()
    resp = client.post("/auth/login", json={"username": username, "password": password}, headers=CSRF)
    assert resp.status_code == 200, resp.text
    value = resp.cookies.get("dtk_session")
    assert value
    client.cookies.clear()
    return value


def _with_cookie(cookie):
    return {**CSRF, "Cookie": f"dtk_session={cookie}"}


class _Statements:
    """Every SQL statement sent on the test database's engine while active."""

    def __init__(self, db):
        self.engine = db.get_bind()
        self.seen = []

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        self.seen.append(statement)

    def __enter__(self):
        from sqlalchemy import event
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc):
        from sqlalchemy import event
        event.remove(self.engine, "before_cursor_execute", self._record)

    def token_version_writes(self):
        return [s for s in self.seen if s.lstrip().upper().startswith("UPDATE") and "token_version" in s]


INCREMENT = re.compile(r"token_version\s*=\s*\(?\s*(users\.)?token_version\s*\+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Sign-out ends the user's sessions at every token reader
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_after_signing_out_the_token_neither_opens_a_route_nor_renews(client, db_session):
    _ordinary_user(db_session)
    token = _token_for(client, "ana", PASSWORD)
    assert _works(client, token)
    assert _logout(client, token).status_code == 204
    assert not _works(client, token)


@pytest.mark.unit
def test_after_signing_out_the_password_reset_route_refuses_the_token(client, db_session):
    _ordinary_user(db_session)
    token = _token_for(client, "ana", PASSWORD)
    _logout(client, token)
    _real_auth()
    client.cookies.clear()
    resp = client.post("/auth/password-reset", headers=_bearer(token),
                       json={"old_password": PASSWORD, "new_password": OTHER_PASSWORD})
    assert resp.status_code == 401 and resp.json()["detail"] == "Invalid or expired token"


@pytest.mark.unit
def test_after_signing_out_the_register_route_refuses_the_admins_token(client, db_session):
    from app.models.user import User
    _ordinary_user(db_session, "jefa", role="admin")
    token = _token_for(client, "jefa", PASSWORD)
    _logout(client, token)
    _real_auth()
    client.cookies.clear()
    resp = client.post("/auth/register", json={"username": "nuevo", "password": "pw"}, headers=_bearer(token))
    assert resp.status_code == 401, resp.text
    assert db_session.query(User).filter(User.username == "nuevo").first() is None


@pytest.mark.unit
def test_a_copy_of_the_cookie_is_refused_after_signing_out(client, db_session):
    _ordinary_user(db_session)
    cookie = _cookie_session(client, "ana")
    assert client.get("/users/me", headers=_with_cookie(cookie)).status_code == 200
    assert _logout(client, cookie=cookie).status_code == 204
    assert client.get("/users/me", headers=_with_cookie(cookie)).status_code == 401
    assert client.post("/auth/refresh", headers=_with_cookie(cookie)).status_code == 401


@pytest.mark.unit
def test_a_second_session_opened_before_the_sign_out_is_refused_after_it(client, db_session):
    _ordinary_user(db_session)
    first = _token_for(client, "ana", PASSWORD)
    second = _cookie_session(client, "ana")
    _logout(client, first)
    assert not _works(client, first)
    assert client.get("/users/me", headers=_with_cookie(second)).status_code == 401
    assert client.post("/auth/refresh", headers=_with_cookie(second)).status_code == 401


@pytest.mark.unit
def test_another_users_session_is_untouched(client, db_session):
    ana = _ordinary_user(db_session)
    luis = _ordinary_user(db_session, "luis", role="reviewer")
    mine = _token_for(client, "ana", PASSWORD)
    theirs = _token_for(client, "luis", PASSWORD)
    _logout(client, mine)
    assert _works(client, theirs)
    assert _version(db_session, luis.id) == 0
    assert _version(db_session, ana.id) == 1


@pytest.mark.unit
def test_signing_in_again_gives_a_working_session(client, db_session):
    from app.core.security import verify_access_token
    _ordinary_user(db_session)
    old = _token_for(client, "ana", PASSWORD)
    _logout(client, old)
    new = _token_for(client, "ana", PASSWORD)
    assert verify_access_token(new)["ver"] == 1
    assert _works(client, new)
    assert not _works(client, old)


@pytest.mark.unit
def test_the_sign_out_answers_as_before_and_clears_the_cookie(client, db_session):
    _ordinary_user(db_session)
    cookie = _cookie_session(client, "ana")
    resp = _logout(client, cookie=cookie)
    assert resp.status_code == 204
    assert resp.content == b""
    set_cookie = resp.headers.get("set-cookie", "")
    assert "dtk_session=" in set_cookie and "max-age=0" in set_cookie.lower()


# ---------------------------------------------------------------------------
# Sign-out with a token that is already refused answers as before
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_signing_out_with_a_stale_token_answers_204_and_changes_nothing(client, db_session):
    user = _ordinary_user(db_session)
    stale = _hand_made_token(user.id, ver=0)
    user.token_version = 3
    db_session.commit()
    current = _token_for(client, "ana", PASSWORD)
    resp = _logout(client, stale)
    assert resp.status_code == 204
    assert "max-age=0" in resp.headers.get("set-cookie", "").lower()
    assert _version(db_session, user.id) == 3
    assert _works(client, current)


@pytest.mark.unit
def test_signing_out_with_an_expired_token_answers_204_and_changes_nothing(client, db_session):
    import time
    user = _ordinary_user(db_session)
    expired = _hand_made_token(user.id, ver=0, exp=int(time.time()) - 10)
    assert _logout(client, expired).status_code == 204
    assert _version(db_session, user.id) == 0


@pytest.mark.unit
@pytest.mark.parametrize("token", ["not-a-token", "a.b"], ids=["garbage", "bad-signature"])
def test_signing_out_with_an_unreadable_token_answers_204(client, db_session, token):
    assert _logout(client, token).status_code == 204
    assert _logout(client, cookie=token).status_code == 204


@pytest.mark.unit
def test_signing_out_with_a_token_of_a_deleted_user_answers_204(client, db_session):
    assert _logout(client, _hand_made_token(987654, ver=0)).status_code == 204


@pytest.mark.unit
def test_signing_out_without_the_csrf_header_is_still_refused_and_ends_nothing(client, db_session):
    user = _ordinary_user(db_session)
    token = _token_for(client, "ana", PASSWORD)
    _real_auth()
    client.cookies.clear()
    resp = client.post("/auth/logout", headers=_bearer(token))
    assert resp.status_code == 403
    assert _version(db_session, user.id) == 0
    assert _works(client, token)


# ---------------------------------------------------------------------------
# The testing account: one person's sign-out leaves the others signed in
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_testing_sign_out_leaves_the_other_testing_sessions(client, db_session):
    _switch_on(client)
    first = _token_for(client, "pruebas", PASSWORD)
    second = _token_for(client, "pruebas", PASSWORD)
    resp = _logout(client, first)
    assert resp.status_code == 204
    assert "max-age=0" in resp.headers.get("set-cookie", "").lower()
    assert _works(client, second)
    [account] = _account(db_session)
    assert account.token_version == 0


# ---------------------------------------------------------------------------
# A new password and a deactivation end the user's sessions
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_changing_ones_own_password_ends_the_earlier_sessions(client, db_session):
    user = _ordinary_user(db_session)
    other_screen = _token_for(client, "ana", PASSWORD)
    this_screen = _token_for(client, "ana", PASSWORD)
    _real_auth()
    client.cookies.clear()
    resp = client.post("/auth/password-reset", headers=_bearer(this_screen),
                       json={"old_password": PASSWORD, "new_password": OTHER_PASSWORD})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"detail": "password updated successfully"}
    assert not _works(client, other_screen)
    assert not _works(client, this_screen)
    assert _version(db_session, user.id) == 1
    assert _login(client, "ana", PASSWORD).status_code == 401
    assert _works(client, _token_for(client, "ana", OTHER_PASSWORD))


@pytest.mark.unit
def test_a_wrong_old_password_ends_nothing(client, db_session):
    user = _ordinary_user(db_session)
    token = _token_for(client, "ana", PASSWORD)
    _real_auth()
    client.cookies.clear()
    resp = client.post("/auth/password-reset", headers=_bearer(token),
                       json={"old_password": "wrong", "new_password": OTHER_PASSWORD})
    assert resp.status_code == 401
    assert _version(db_session, user.id) == 0
    assert _works(client, token)


@pytest.mark.unit
def test_deactivating_a_user_ends_their_sessions_and_reactivating_does_not_lower_it(client, db_session):
    user = _ordinary_user(db_session)
    token = _token_for(client, "ana", PASSWORD)
    resp = _as(client, "admin").patch(f"/auth/users/{user.id}/active", params={"is_active": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is False
    assert _version(db_session, user.id) == 1
    resp = _as(client, "admin").patch(f"/auth/users/{user.id}/active", params={"is_active": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is True
    assert _version(db_session, user.id) == 1
    assert not _works(client, token)
    assert _works(client, _token_for(client, "ana", PASSWORD))


@pytest.mark.unit
def test_deactivating_an_inactive_user_does_not_count_again(client, db_session):
    user = _ordinary_user(db_session)
    _as(client, "admin").patch(f"/auth/users/{user.id}/active", params={"is_active": False})
    _as(client, "admin").patch(f"/auth/users/{user.id}/active", params={"is_active": False})
    assert _version(db_session, user.id) == 1


@pytest.mark.unit
def test_another_users_sessions_survive_a_deactivation(client, db_session):
    user = _ordinary_user(db_session)
    other = _ordinary_user(db_session, "luis", role="reviewer")
    theirs = _token_for(client, "luis", PASSWORD)
    _as(client, "admin").patch(f"/auth/users/{user.id}/active", params={"is_active": False})
    assert _works(client, theirs)
    assert _version(db_session, other.id) == 0


# ---------------------------------------------------------------------------
# The raise is one UPDATE that adds one in the database
# ---------------------------------------------------------------------------

def _assert_one_increment(stmts):
    writes = stmts.token_version_writes()
    assert len(writes) == 1, writes
    assert INCREMENT.search(writes[0]), writes[0]


@pytest.mark.unit
def test_the_sign_out_raise_is_one_incrementing_update(client, db_session):
    _ordinary_user(db_session)
    token = _token_for(client, "ana", PASSWORD)
    with _Statements(db_session) as stmts:
        _logout(client, token)
    _assert_one_increment(stmts)


@pytest.mark.unit
def test_the_password_change_raise_is_one_incrementing_update(client, db_session):
    _ordinary_user(db_session)
    token = _token_for(client, "ana", PASSWORD)
    _real_auth()
    client.cookies.clear()
    with _Statements(db_session) as stmts:
        client.post("/auth/password-reset", headers=_bearer(token),
                    json={"old_password": PASSWORD, "new_password": OTHER_PASSWORD})
    _assert_one_increment(stmts)


@pytest.mark.unit
def test_the_deactivation_raise_is_one_incrementing_update(client, db_session):
    user = _ordinary_user(db_session)
    with _Statements(db_session) as stmts:
        _as(client, "admin").patch(f"/auth/users/{user.id}/active", params={"is_active": False})
    _assert_one_increment(stmts)


@pytest.mark.unit
def test_two_interleaved_sign_outs_both_count(client, db_session):
    """Two sessions of one user sign out at once: the second sees the first's raise, so both count."""
    from app.core.db import SessionLocal
    from app.models.user import User
    user = _ordinary_user(db_session)
    first = _token_for(client, "ana", PASSWORD)
    second = _token_for(client, "ana", PASSWORD)
    # db_session holds the row as read before either sign-out (version 0).
    assert db_session.get(User, user.id).token_version == 0
    other = SessionLocal()
    try:
        _logout_in(other, first)
    finally:
        other.close()
    # The route's session still holds version 0 in memory; the second token
    # was current when it was read, as in a request that read the user
    # before the first sign-out committed.
    from app.api import auth as auth_mod
    auth_mod.end_sessions(db_session, user.id)
    db_session.commit()
    assert _version(db_session, user.id) == 2
    assert not _works(client, second)


def _logout_in(session, token):
    """Raise the token's user's generation through end_sessions in `session`, as a concurrent sign-out does."""
    from app.api import auth as auth_mod
    from app.core.security import verify_access_token
    auth_mod.end_sessions(session, int(verify_access_token(token)["sub"]))
    session.commit()


# ---------------------------------------------------------------------------
# A database failure during sign-out still clears the cookie
# ---------------------------------------------------------------------------

def _db_failure(*args, **kwargs):
    from sqlalchemy.exc import OperationalError
    raise OperationalError("UPDATE users", {}, Exception("database is locked"))


def _auth_errors(caplog):
    return [r for r in caplog.records if r.name == "app.api.auth" and r.levelname == "ERROR"]


@pytest.mark.unit
@pytest.mark.parametrize("failing", ["end_sessions", "_user_of_token"], ids=["update", "lookup"])
def test_a_database_failure_still_answers_204_and_clears_the_cookie(client, db_session, monkeypatch,
                                                                    caplog, failing):
    import logging
    from app.api import auth as auth_mod
    user = _ordinary_user(db_session)
    token = _token_for(client, "ana", PASSWORD)
    monkeypatch.setattr(auth_mod, failing, _db_failure)
    with caplog.at_level(logging.ERROR, logger="app.api.auth"):
        resp = _logout(client, token)
    assert resp.status_code == 204
    set_cookie = resp.headers.get("set-cookie", "")
    assert "dtk_session=" in set_cookie and "max-age=0" in set_cookie.lower()
    errors = _auth_errors(caplog)
    assert len(errors) == 1, [r.getMessage() for r in errors]
    assert str(user.id) in errors[0].getMessage()
    assert token not in caplog.text
    monkeypatch.undo()
    assert _version(db_session, user.id) == 0
    assert _works(client, token)


@pytest.mark.unit
def test_a_failure_that_is_not_a_database_error_is_not_swallowed(client, db_session, monkeypatch):
    from app.api import auth as auth_mod
    user = _ordinary_user(db_session)
    token = _token_for(client, "ana", PASSWORD)

    def boom(*args, **kwargs):
        raise RuntimeError("not a database error")

    monkeypatch.setattr(auth_mod, "end_sessions", boom)
    with pytest.raises(RuntimeError):
        _logout(client, token)
    monkeypatch.undo()
    assert _version(db_session, user.id) == 0
