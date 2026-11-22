"""A wrong current password on POST /auth/password-reset counts against the login throttle (NEH-395).

The own password change asks for the current password, so a stolen
session could otherwise guess it without limit. A wrong current password
is recorded in app.core.login_throttle exactly as a failed sign-in is
(record_failure with the account key `user:<username>` and the client's
IP key), so failed changes and failed sign-ins add up. While either key is
locked the route answers as POST /auth/login does, 429 "Too many failed
login attempts. Try again later." with Retry-After, without checking the
password; a correct current password clears both keys, as a successful
sign-in does.

    python -m pytest tests/unit/test_password_reset_throttle.py

Version: 0.3.0
"""

import pytest

from tests.unit.test_signout_ends_sessions import _version
from tests.unit.test_testing_space_admin import (
    PASSWORD,
    _bearer,
    _login,
    _ordinary_user,
    _real_auth,
    _token_for,
    fresh_throttle,  # noqa: F401  (autouse fixture)
    sends,  # noqa: F401  (autouse fixture)
)

NEW = "fake-new-pw-395"
WRONG = "fake-wrong-pw"


def _reset(client, token, old):
    _real_auth()
    client.cookies.clear()
    return client.post("/auth/password-reset", headers=_bearer(token),
                       json={"old_password": old, "new_password": NEW})


def _hash(db, user_id):
    from app.models.user import User
    db.expire_all()
    return db.get(User, user_id).hashed_password


@pytest.mark.unit
def test_a_wrong_current_password_is_still_a_401(client, db_session):
    _ordinary_user(db_session)
    token = _token_for(client, "ana", PASSWORD)
    resp = _reset(client, token, WRONG)
    assert resp.status_code == 401 and resp.json() == {"detail": "Old password incorrect"}


@pytest.mark.unit
def test_wrong_current_passwords_are_recorded_as_failed_sign_ins_of_that_account(client, db_session, monkeypatch):
    from app.api import auth as auth_mod
    _ordinary_user(db_session)
    token = _token_for(client, "ana", PASSWORD)
    calls = []
    real = auth_mod.login_throttle.record_failure
    monkeypatch.setattr(auth_mod.login_throttle, "record_failure", lambda *keys: calls.append(keys) or real(*keys))
    _reset(client, token, WRONG)
    _real_auth()
    client.post("/auth/login", json={"username": "ana", "password": WRONG})
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0][0] == "user:ana"


@pytest.mark.unit
def test_five_wrong_current_passwords_lock_sign_in_for_that_account(client, db_session):
    _ordinary_user(db_session)
    token = _token_for(client, "ana", PASSWORD)
    for _ in range(5):
        assert _reset(client, token, WRONG).status_code == 401
    resp = _login(client, "ana", PASSWORD)
    assert resp.status_code == 429, resp.text


@pytest.mark.unit
def test_failed_changes_and_failed_sign_ins_add_up(client, db_session):
    _ordinary_user(db_session)
    token = _token_for(client, "ana", PASSWORD)
    for _ in range(4):
        _reset(client, token, WRONG)
    assert _login(client, "ana", WRONG).status_code == 401  # the fifth failure
    assert _login(client, "ana", PASSWORD).status_code == 429


@pytest.mark.unit
def test_while_throttled_the_route_answers_as_login_does_and_does_not_check_the_password(
        client, db_session, monkeypatch):
    from app.api import auth as auth_mod
    user = _ordinary_user(db_session)
    token = _token_for(client, "ana", PASSWORD)
    for _ in range(5):
        _login(client, "ana", WRONG)
    login = _login(client, "ana", PASSWORD)
    assert login.status_code == 429

    checked = []
    real = auth_mod.verify_password
    monkeypatch.setattr(auth_mod, "verify_password", lambda *a: checked.append(a) or real(*a))
    before = _hash(db_session, user.id)
    resp = _reset(client, token, PASSWORD)

    assert resp.status_code == login.status_code == 429
    assert resp.json() == login.json() == {"detail": "Too many failed login attempts. Try again later."}
    assert resp.headers.get("Retry-After") and int(resp.headers["Retry-After"]) > 0
    assert checked == []
    assert _hash(db_session, user.id) == before
    assert _version(db_session, user.id) == 0


@pytest.mark.unit
def test_a_correct_current_password_clears_the_count(client, db_session):
    from app.api import auth as auth_mod
    _ordinary_user(db_session)
    token = _token_for(client, "ana", PASSWORD)
    for _ in range(4):
        _reset(client, token, WRONG)
    state = auth_mod.login_throttle._state
    assert len(state["user:ana"]["failures"]) == 4
    assert _reset(client, token, PASSWORD).status_code == 200
    assert "user:ana" not in state
    for _ in range(4):
        assert _login(client, "ana", WRONG).status_code == 401
    assert _login(client, "ana", NEW).status_code == 200


@pytest.mark.unit
def test_another_accounts_count_is_its_own(client, db_session, monkeypatch):
    """The account keys differ; the IP key is shared, as it is between sign-ins from one client."""
    from app.api import auth as auth_mod
    _ordinary_user(db_session)
    _ordinary_user(db_session, "luis", role="reviewer")
    token = _token_for(client, "ana", PASSWORD)
    _reset(client, token, WRONG)
    assert auth_mod.login_throttle._state.get("user:ana", {}).get("failures")
    assert "user:luis" not in auth_mod.login_throttle._state
