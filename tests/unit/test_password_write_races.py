"""A password write by a user on their own account names the state it was authorised under (NEH-395).

POST /auth/password-reset checks the session and the current password,
then writes. If an administrator sets the user's password in between, that
change ends the user's sessions (token_version + 1), so the backend then
refuses the reset's session. The reset's one UPDATE carries
`token_version = <the version of the request's token>` in its WHERE clause:
when it changes no row the reset is answered as a dead session is (401
"Invalid or expired token"), writes nothing and counts nothing in the login
throttle, and the administrator's password stands.

The login's upgrade of a weak hash is written only over the hash it
verified (`hashed_password = <verified hash>` in its WHERE clause), so it
can never undo a password set meanwhile.

PUT /auth/users/{user_id}/password is unconditional on the target: an
administrator overrides whatever the user's password was.

The interleavings are made by running the administrator's route function
in a second database session from inside the first request's password
check, which is where the window lies. Both sessions use one engine, so the
statements recorded include the administrator's UPDATE; each test picks
the request's own by its WHERE clause.

    python -m pytest tests/unit/test_password_write_races.py
"""

import re

import pytest

from tests.unit.test_signout_ends_sessions import _Statements, _version
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

ADMIN_SET = "fake-admin-set-395"
USER_SET = "fake-user-set-395"


def _admin_sets_password_in_another_session(admin_id, user_id, new_password):
    """The administrator's route, run to its commit in a session of its own."""
    from app.api.auth import set_user_password
    from app.core.db import SessionLocal
    from app.models.user import User
    from app.schemas.user import PasswordSet
    other = SessionLocal()
    try:
        admin = other.get(User, admin_id)
        set_user_password(user_id=user_id, payload=PasswordSet(new_password=new_password),
                          current_user=admin, db=other)
    finally:
        other.close()


def _hash(db, user_id):
    from app.models.user import User
    db.expire_all()
    return db.get(User, user_id).hashed_password


@pytest.mark.unit
def test_a_reset_whose_session_ended_after_its_check_writes_nothing(client, db_session, monkeypatch):
    from app.api import auth as auth_mod
    from app.core.security import verify_password
    admin = _ordinary_user(db_session, "jefa", role="admin")
    user = _ordinary_user(db_session, "ana")
    token = _token_for(client, "ana", PASSWORD)

    def verify_then_admin_acts(password, hashed):
        ok = verify_password(password, hashed)
        _admin_sets_password_in_another_session(admin.id, user.id, ADMIN_SET)
        return ok

    monkeypatch.setattr(auth_mod, "verify_password", verify_then_admin_acts)
    _real_auth()
    client.cookies.clear()
    with _Statements(db_session) as stmts:
        resp = client.post("/auth/password-reset", headers=_bearer(token),
                           json={"old_password": PASSWORD, "new_password": USER_SET})
    monkeypatch.undo()

    assert resp.status_code == 401, resp.text
    assert resp.json() == {"detail": "Invalid or expired token"}
    assert _login(client, "ana", ADMIN_SET).status_code == 200
    assert _login(client, "ana", USER_SET).status_code == 401
    assert _version(db_session, user.id) == 1  # the administrator's raise only

    guarded = [s for s in stmts.seen
               if re.search(r"WHERE\s+users\.id\s*=\s*\?\s+AND\s+users\.token_version\s*=\s*\?", s)]
    assert len(guarded) == 1, stmts.seen
    print("\nreset UPDATE:", " ".join(guarded[0].split()))
    assert "hashed_password" in guarded[0]
    assert re.search(r"token_version\s*=\s*\(?\s*users\.token_version\s*\+", guarded[0]), guarded[0]


@pytest.mark.unit
def test_a_reset_refused_this_way_counts_nothing_in_the_throttle(client, db_session, monkeypatch):
    from app.api import auth as auth_mod
    from app.core.security import verify_password
    admin = _ordinary_user(db_session, "jefa", role="admin")
    user = _ordinary_user(db_session, "ana")
    token = _token_for(client, "ana", PASSWORD)
    # One failed sign-in on record before the reset.
    assert _login(client, "ana", "fake-wrong-pw").status_code == 401
    before = list(auth_mod.login_throttle._state["user:ana"]["failures"])

    def verify_then_admin_acts(password, hashed):
        ok = verify_password(password, hashed)
        _admin_sets_password_in_another_session(admin.id, user.id, ADMIN_SET)
        return ok

    monkeypatch.setattr(auth_mod, "verify_password", verify_then_admin_acts)
    _real_auth()
    client.cookies.clear()
    resp = client.post("/auth/password-reset", headers=_bearer(token),
                       json={"old_password": PASSWORD, "new_password": USER_SET})
    assert resp.status_code == 401
    assert auth_mod.login_throttle._state["user:ana"]["failures"] == before


@pytest.mark.unit
def test_an_undisturbed_reset_still_writes_and_raises_the_version(client, db_session):
    user = _ordinary_user(db_session, "ana")
    token = _token_for(client, "ana", PASSWORD)
    _real_auth()
    client.cookies.clear()
    resp = client.post("/auth/password-reset", headers=_bearer(token),
                       json={"old_password": PASSWORD, "new_password": USER_SET})
    assert resp.status_code == 200, resp.text
    assert _version(db_session, user.id) == 1
    assert _login(client, "ana", USER_SET).status_code == 200


@pytest.mark.unit
def test_a_login_rehash_never_undoes_a_password_set_meanwhile(client, db_session, monkeypatch):
    """A weak stored hash is upgraded at login only over the hash the login verified."""
    from app.api import auth as auth_mod
    from app.core import security
    from app.models.user import User
    admin = _ordinary_user(db_session, "jefa", role="admin")
    user = _ordinary_user(db_session, "ana")
    weak = "legacysalt$" + security._pbkdf2(PASSWORD, "legacysalt", security._LEGACY_ITERATIONS)
    db_session.get(User, user.id).hashed_password = weak
    db_session.commit()
    assert security.needs_rehash(weak)

    real = security.verify_password

    def verify_then_admin_acts(password, hashed):
        ok = real(password, hashed)
        _admin_sets_password_in_another_session(admin.id, user.id, ADMIN_SET)
        return ok

    monkeypatch.setattr(auth_mod, "verify_password", verify_then_admin_acts)
    _real_auth()
    client.cookies.clear()
    with _Statements(db_session) as stmts:
        client.post("/auth/login", json={"username": "ana", "password": PASSWORD})
    monkeypatch.undo()

    assert _login(client, "ana", ADMIN_SET).status_code == 200
    assert _hash(db_session, user.id) != weak
    guarded = [s for s in stmts.seen
               if re.search(r"WHERE\s+users\.id\s*=\s*\?\s+AND\s+users\.hashed_password\s*=\s*\?", s)]
    assert len(guarded) == 1, stmts.seen
    print("\nrehash UPDATE:", " ".join(guarded[0].split()))
