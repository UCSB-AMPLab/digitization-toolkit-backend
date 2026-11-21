"""The rule every new password is held to: at least PASSWORD_MIN_LENGTH (8) characters (NEH-395).

The three routes that set a password from a request body hold the new
password to it: POST /auth/register (the one route that creates a user,
first-run setup included), PUT /auth/users/{user_id}/password and the
new password of POST /auth/password-reset. Length is counted in
characters, as sent: no trimming, and no other rule. A shorter password is
answered 422 {"detail": {"code": "password_too_short", "message",
"min_length": 8}}, and a password that cannot be encoded as UTF-8 (a lone
surrogate) 422 {"code": "password_invalid"}; neither body carries the
password, and nothing is written.

Passwords already stored are untouched: a user whose password is shorter
still signs in, and the current password POST /auth/password-reset asks
for is not held to the rule. The testing account keeps its own rule
(/system/testing: not empty, encodable).

    python -m pytest tests/unit/test_password_rule.py
"""

import pytest

from tests.unit.test_signout_ends_sessions import _version
from tests.unit.test_testing_space_admin import (
    PASSWORD,
    _account,
    _admin,
    _bearer,
    _login,
    _ordinary_user,
    _real_auth,
    _token_for,
    fresh_throttle,  # noqa: F401  (autouse fixture)
    sends,  # noqa: F401  (autouse fixture)
)

SEVEN = "fake-pw"            # 7 characters
EIGHT = "fake-pw8"           # 8 characters


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _raw(client, method, url, text, token=None):
    """Send `text` as the JSON body, as written: a lone surrogate reaches the backend as \\ud800."""
    _real_auth()
    client.cookies.clear()
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers.update(_bearer(token))
    return client.request(method, url, content=text.encode("ascii"), headers=headers)


def _hash(db, user_id):
    from app.models.user import User
    db.expire_all()
    return db.get(User, user_id).hashed_password


def _users(db):
    from app.models.user import User
    db.expire_all()
    return sorted(u.username for u in db.query(User).all())


def _too_short(resp, secret=None):
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": {"code": "password_too_short",
                                      "message": resp.json()["detail"]["message"],
                                      "min_length": 8}}
    assert resp.json()["detail"]["message"]
    if secret is not None:
        assert secret not in resp.text


def _invalid(resp):
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "password_invalid" and detail["message"]
    assert "ud800" not in resp.text.lower() and "\ud800" not in resp.text


class _World:
    """An administrator with a session, and an operator `ana` with a session of her own."""

    def __init__(self, client, db):
        self.admin = _ordinary_user(db, "jefa", role="admin")
        self.admin_token = _token_for(client, "jefa", PASSWORD)
        self.ana = _ordinary_user(db, "ana")
        self.ana_token = _token_for(client, "ana", PASSWORD)


@pytest.fixture
def world(client, db_session):
    return _World(client, db_session)


def _register(client, world, password):
    _real_auth()
    client.cookies.clear()
    return client.post("/auth/register", headers=_bearer(world.admin_token),
                       json={"username": "nuevo", "password": password})


def _set(client, world, password):
    _real_auth()
    client.cookies.clear()
    return client.put(f"/auth/users/{world.ana.id}/password", headers=_bearer(world.admin_token),
                      json={"new_password": password})


def _reset(client, world, password):
    _real_auth()
    client.cookies.clear()
    return client.post("/auth/password-reset", headers=_bearer(world.ana_token),
                       json={"old_password": PASSWORD, "new_password": password})


ROUTES = [pytest.param(_register, id="register"), pytest.param(_set, id="set"),
          pytest.param(_reset, id="reset")]


# ---------------------------------------------------------------------------
# One rule, in one place
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_minimum_is_8_and_defined_once():
    from app.schemas import user
    assert user.PASSWORD_MIN_LENGTH == 8


@pytest.mark.unit
@pytest.mark.parametrize("send", ROUTES)
def test_a_password_of_7_characters_is_refused_with_its_code_and_nothing_written(client, db_session, world, send):
    before = _hash(db_session, world.ana.id)
    users = _users(db_session)
    _too_short(send(client, world, SEVEN), SEVEN)
    assert _hash(db_session, world.ana.id) == before
    assert _version(db_session, world.ana.id) == 0
    assert _users(db_session) == users


@pytest.mark.unit
@pytest.mark.parametrize("send", ROUTES)
@pytest.mark.parametrize("value", ["", "x", "      ", SEVEN], ids=["empty", "one", "six-spaces", "seven"])
def test_every_password_under_8_characters_is_too_short(client, db_session, world, send, value):
    _too_short(send(client, world, value), value if len(value.strip()) > 2 else None)


@pytest.mark.unit
@pytest.mark.parametrize("send", ROUTES)
@pytest.mark.parametrize("value", [EIGHT, " " * 8, "  abcd  ", "ñ" * 8, "y" * 500],
                         ids=["eight", "eight-spaces", "spaces-kept", "eight-non-ascii", "long"])
def test_a_password_of_8_characters_or_more_is_accepted_as_sent(client, db_session, world, send, value):
    resp = send(client, world, value)
    assert resp.status_code == 200, resp.text


@pytest.mark.unit
@pytest.mark.parametrize("send", ROUTES)
def test_length_is_counted_in_characters_not_bytes(client, db_session, world, send):
    # Seven characters of four UTF-8 bytes each: 28 bytes, still too short.
    _too_short(send(client, world, "\U0001F600" * 7), "\U0001F600")


@pytest.mark.unit
def test_the_set_password_works_with_an_8_character_password_end_to_end(client, db_session, world):
    assert _set(client, world, " " * 8).status_code == 200
    assert _login(client, "ana", " " * 8).status_code == 200


# ---------------------------------------------------------------------------
# A lone surrogate is a 422, not a 500
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_lone_surrogate_is_refused_by_register(client, db_session, world):
    users = _users(db_session)
    _invalid(_raw(client, "POST", "/auth/register",
                  '{"username": "nuevo", "password": "abcdefgh\\ud800"}', world.admin_token))
    assert _users(db_session) == users


@pytest.mark.unit
def test_a_lone_surrogate_is_refused_by_the_set_password_route(client, db_session, world):
    before = _hash(db_session, world.ana.id)
    _invalid(_raw(client, "PUT", f"/auth/users/{world.ana.id}/password",
                  '{"new_password": "abcdefgh\\ud800"}', world.admin_token))
    assert _hash(db_session, world.ana.id) == before


@pytest.mark.unit
def test_a_lone_surrogate_is_refused_by_the_own_password_change(client, db_session, world):
    before = _hash(db_session, world.ana.id)
    _invalid(_raw(client, "POST", "/auth/password-reset",
                  '{"old_password": "%s", "new_password": "abcdefgh\\ud800"}' % PASSWORD, world.ana_token))
    assert _hash(db_session, world.ana.id) == before


# ---------------------------------------------------------------------------
# First-run setup goes through POST /auth/register
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_first_run_setup_refuses_a_short_password_and_creates_no_one(client, db_session):
    _real_auth()
    client.cookies.clear()
    resp = client.post("/auth/register", json={"username": "primera", "password": SEVEN})
    _too_short(resp, SEVEN)
    assert _users(db_session) == []


@pytest.mark.unit
def test_first_run_setup_accepts_8_characters(client, db_session):
    _real_auth()
    client.cookies.clear()
    resp = client.post("/auth/register", json={"username": "primera", "password": EIGHT})
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "admin"


# ---------------------------------------------------------------------------
# What stays as it is
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_user_with_a_shorter_stored_password_still_signs_in(client, db_session):
    _ordinary_user(db_session, "corto", password="pw")
    assert _login(client, "corto", "pw").status_code == 200
    assert _login(client, "corto", "pq").status_code == 401


@pytest.mark.unit
def test_the_current_password_of_the_own_change_is_not_held_to_the_rule(client, db_session):
    user = _ordinary_user(db_session, "corto", password="pw")
    token = _token_for(client, "corto", "pw")
    _real_auth()
    client.cookies.clear()
    resp = client.post("/auth/password-reset", headers=_bearer(token),
                       json={"old_password": "pw", "new_password": EIGHT})
    assert resp.status_code == 200, resp.text
    assert _login(client, "corto", EIGHT).status_code == 200
    assert _version(db_session, user.id) == 1


@pytest.mark.unit
def test_the_testing_account_keeps_its_own_rule(client, db_session):
    """/system/testing refuses an empty password (password_empty) and nothing else of its length."""
    resp = _admin(client).put("/system/testing", json={"enabled": True, "username": "pruebas", "password": "x"})
    assert resp.status_code == 200, resp.text
    assert _login(client, "pruebas", "x").status_code == 200
    resp = _admin(client).patch("/system/testing/account", json={"password": ""})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "password_empty"
    assert len(_account(db_session)) == 1
