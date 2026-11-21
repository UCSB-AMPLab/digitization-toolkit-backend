"""The testing space's admin routes (NEH-391): GET and PUT /system/testing, PATCH /system/testing/account.

An admin switches the unit's testing space on and off and manages its one
shared testing account through these routes only:

- GET reports whether the space is on, the account's username and whether
  it is active, the expiry setting, and the testing projects and volumes;
- PUT switches the space on (creating the account the first time, making
  it active again after) or off (the account made inactive, so its tokens
  are refused, and its material left as it is), and stores the expiry;
- PATCH renames the account or gives it a new password;
- nothing is sent to Fisqua, the password is never returned or logged,
  and every role but admin is refused, in every unit mode.

    python -m pytest tests/unit/test_testing_space_admin.py

Version: 0.3.3
"""

import logging

import pytest

from tests.unit.test_capture_manifest_export import _as, tenant  # noqa: F401  (tenant is a fixture)
from tests.unit.test_fisqua_outbox import fisqua  # noqa: F401  (fixture)
from tests.unit.test_testing_space_schema import T, _testing_project, _testing_user, _volume
from tests.unit.test_unit_mode import _logs, _project, _unit, link  # noqa: F401  (fixtures)

PASSWORD = "pw-for-tests-91"
OTHER_PASSWORD = "another-pw-92"


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_throttle(monkeypatch):
    """A login throttle of its own per test, so failed logins in one test never lock another."""
    from app.api import auth as auth_mod
    from app.core.login_throttle import LoginThrottle
    monkeypatch.setattr(auth_mod, "login_throttle", LoginThrottle())


@pytest.fixture(autouse=True)
def sends(monkeypatch):
    """Every way the unit's users reach Fisqua, replaced by a recorder; the test asserts it stays empty."""
    from app.api import auth as auth_mod
    from app.core import fisqua_queue
    calls = []
    monkeypatch.setattr(auth_mod, "notify_operators_changed", lambda *a, **k: calls.append(("notify", a, k)))
    monkeypatch.setattr(fisqua_queue, "notify_operators_changed",
                        lambda *a, **k: calls.append(("notify", a, k)))
    monkeypatch.setattr(fisqua_queue, "send_operators_with_mode",
                        lambda *a, **k: calls.append(("send_with_mode", a, k)) or (True, None))
    monkeypatch.setattr(fisqua_queue, "mark_operators_due", lambda *a, **k: calls.append(("due", a, k)))
    monkeypatch.setattr(fisqua_queue, "send_operators",
                        lambda *a, **k: calls.append(("send", a, k)) or (True, None))
    return calls


def _real_auth():
    """Drop the get_current_user override, so the next requests authenticate with real tokens."""
    from app.api.auth import get_current_user
    from app.main import app
    app.dependency_overrides.pop(get_current_user, None)


def _admin(client):
    return _as(client, "admin")


def _code(resp, status, code):
    assert resp.status_code == status, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == code, detail
    assert detail["message"]
    return detail


def _switch_on(client, username="pruebas", password=PASSWORD, **extra):
    body = {"enabled": True, "username": username, "password": password, **extra}
    resp = _admin(client).put("/system/testing", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _account(db):
    from app.models.user import User
    db.expire_all()
    return db.query(User).filter(User.role == "testing").all()


def _settings(db):
    from app.models.unit_settings import UnitSettings
    db.expire_all()
    return db.get(UnitSettings, 1)


def _login(client, username, password):
    _real_auth()
    return client.post("/auth/login", json={"username": username, "password": password})


def _no_password_in(body_or_text, *passwords):
    text = body_or_text if isinstance(body_or_text, str) else repr(body_or_text)
    for pw in passwords:
        assert pw not in text


SHAPE = {"enabled", "username", "account_active", "expiry_days", "reserve_gb", "free_gb", "projects", "volumes"}


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_get_on_a_fresh_unit(client, db_session):
    resp = _admin(client).get("/system/testing")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body.pop("free_gb"), float)
    assert body == {"enabled": False, "username": None, "account_active": False,
                    "expiry_days": 30, "reserve_gb": 20, "projects": 0, "volumes": 0}


@pytest.mark.unit
def test_get_counts_the_testing_projects_and_volumes_at_every_level(client, db_session):
    real = _project(db_session, "Real")
    _volume(db_session, real, name="Real tomo", stem="xx-real-1")
    p1 = _testing_project(db_session, "T1")
    _testing_project(db_session, "T2")
    top = _volume(db_session, p1, name="Top", stem=T + "a1")
    _volume(db_session, parent=top, name="Sub", stem=T + "a2")
    body = _admin(client).get("/system/testing").json()
    assert (body["projects"], body["volumes"]) == (2, 2)


# ---------------------------------------------------------------------------
# Switching on
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_switching_on_creates_the_testing_account(client, db_session):
    from app.core.security import verify_password
    body = _switch_on(client)
    assert set(body) == SHAPE
    assert body["enabled"] is True and body["username"] == "pruebas" and body["account_active"] is True
    [user] = _account(db_session)
    assert user.username == "pruebas" and user.is_active
    assert user.hashed_password != PASSWORD and verify_password(PASSWORD, user.hashed_password)
    settings = _settings(db_session)
    assert settings.testing_enabled is True and settings.testing_user_id == user.id


@pytest.mark.unit
def test_the_new_account_logs_in_through_the_login_route(client, db_session):
    _switch_on(client)
    resp = _login(client, "pruebas", PASSWORD)
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    me = client.get("/users/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["role"] == "testing"


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    {"enabled": True},
    {"enabled": True, "username": "pruebas"},
    {"enabled": True, "password": PASSWORD},
], ids=["neither", "no-password", "no-username"])
def test_switching_on_without_an_account_needs_username_and_password(client, db_session, body):
    _code(_admin(client).put("/system/testing", json=body), 422, "account_required")
    assert _account(db_session) == []
    settings = _settings(db_session)
    assert settings is None or not settings.testing_enabled


@pytest.mark.unit
def test_a_username_held_by_another_user_is_refused(client, db_session):
    from app.models.user import User
    db_session.add(User(username="ana", hashed_password="x", role="operator"))
    db_session.commit()
    _code(_admin(client).put("/system/testing", json={"enabled": True, "username": "ana",
                                                      "password": PASSWORD}), 409, "username_taken")
    assert _account(db_session) == []


@pytest.mark.unit
def test_a_concurrent_second_creation_is_a_409_not_a_500(client, db_session, monkeypatch):
    """The other creation committed after this one looked for the account: the partial unique index refuses it."""
    from app.core import testing_space
    _testing_user(db_session, "primera")
    monkeypatch.setattr(testing_space, "testing_account", lambda db: None)
    client.raise_server_exceptions = False
    resp = _admin(client).put("/system/testing", json={"enabled": True, "username": "segunda",
                                                       "password": PASSWORD})
    _code(resp, 409, "account_exists")
    assert [u.username for u in _account(db_session)] == ["primera"]


@pytest.mark.unit
@pytest.mark.parametrize("days,status", [
    (0, 422), (1, 200), (365, 200), (366, 422), (1.5, 422), ("30", 422), (True, 422), (None, 422),
], ids=["0", "1", "365", "366", "float", "string", "bool", "null"])
def test_expiry_days_is_an_integer_from_1_to_365(client, db_session, days, status):
    resp = _admin(client).put("/system/testing", json={"enabled": True, "username": "pruebas",
                                                       "password": PASSWORD, "expiry_days": days})
    assert resp.status_code == status, resp.text
    settings = _settings(db_session)
    if status == 200:
        assert resp.json()["expiry_days"] == days and settings.testing_expiry_days == days
    else:
        assert settings is None or settings.testing_expiry_days == 30
        assert _account(db_session) == []


@pytest.mark.unit
def test_expiry_days_alone_with_the_space_off_is_stored(client, db_session):
    resp = _admin(client).put("/system/testing", json={"enabled": False, "expiry_days": 7})
    assert resp.status_code == 200, resp.text
    assert resp.json()["expiry_days"] == 7 and _settings(db_session).testing_expiry_days == 7


@pytest.mark.unit
def test_expiry_days_shifts_existing_volumes_expiry_by_the_difference(client, db_session):
    from datetime import datetime
    from app.models.collection import Collection
    p = _testing_project(db_session, "T1")
    vol = _volume(db_session, p, name="Top", stem=T + "b1",
                  testing_expires_at=datetime(2030, 1, 31))
    _switch_on(client, expiry_days=3)
    db_session.expire_all()
    assert db_session.get(Collection, vol.id).testing_expires_at == datetime(2030, 1, 4)


@pytest.mark.unit
def test_unknown_fields_are_refused(client, db_session):
    resp = _admin(client).put("/system/testing", json={"enabled": True, "username": "pruebas",
                                                       "password": PASSWORD, "role": "admin"})
    assert resp.status_code == 422, resp.text
    assert _account(db_session) == []


# ---------------------------------------------------------------------------
# Switching off, and on again
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_switching_off_makes_the_account_inactive_and_leaves_the_material(client, db_session):
    from app.models.collection import Collection
    from app.models.project import Project
    _switch_on(client)
    p = _testing_project(db_session, "T1")
    vol = _volume(db_session, p, name="Top", stem=T + "c1")
    resp = _admin(client).put("/system/testing", json={"enabled": False})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["enabled"], body["username"], body["account_active"]) == (False, "pruebas", False)
    assert (body["projects"], body["volumes"]) == (1, 1)
    [user] = _account(db_session)
    assert not user.is_active
    assert _settings(db_session).testing_enabled is False
    assert db_session.get(Project, p.id) is not None and db_session.get(Collection, vol.id) is not None


@pytest.mark.unit
def test_after_switching_off_login_and_an_earlier_token_are_refused(client, db_session):
    _switch_on(client)
    token = _login(client, "pruebas", PASSWORD).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/users/me", headers=headers).status_code == 200
    _admin(client).put("/system/testing", json={"enabled": False})
    _real_auth()
    assert client.get("/users/me", headers=headers).status_code == 401
    assert client.post("/auth/refresh", headers=headers).status_code == 401
    assert _login(client, "pruebas", PASSWORD).status_code == 403


@pytest.mark.unit
def test_switching_on_again_reactivates_the_same_account(client, db_session):
    first = _switch_on(client)
    [user] = _account(db_session)
    _admin(client).put("/system/testing", json={"enabled": False})
    resp = _admin(client).put("/system/testing", json={"enabled": True})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {**first, "enabled": True, "account_active": True}
    [again] = _account(db_session)
    assert again.id == user.id and again.is_active
    assert _settings(db_session).testing_user_id == user.id
    assert _login(client, "pruebas", PASSWORD).status_code == 200


@pytest.mark.unit
def test_switching_on_again_applies_new_credentials(client, db_session):
    _switch_on(client)
    _admin(client).put("/system/testing", json={"enabled": False})
    resp = _admin(client).put("/system/testing", json={"enabled": True, "username": "nuevas",
                                                       "password": OTHER_PASSWORD})
    assert resp.status_code == 200 and resp.json()["username"] == "nuevas"
    assert len(_account(db_session)) == 1
    assert _login(client, "nuevas", OTHER_PASSWORD).status_code == 200
    assert _login(client, "nuevas", PASSWORD).status_code == 401


@pytest.mark.unit
@pytest.mark.parametrize("extra", [{"username": "x"}, {"password": "y"}, {"username": "x", "password": "y"}],
                         ids=["username", "password", "both"])
def test_credentials_with_the_space_off_are_refused_and_change_nothing(client, db_session, extra):
    from app.core.security import verify_password
    _switch_on(client)
    resp = _admin(client).put("/system/testing", json={"enabled": False, "expiry_days": 9, **extra})
    _code(resp, 422, "space_off")
    [user] = _account(db_session)
    assert user.username == "pruebas" and user.is_active and verify_password(PASSWORD, user.hashed_password)
    settings = _settings(db_session)
    assert settings.testing_enabled is True and settings.testing_expiry_days == 30


@pytest.mark.unit
def test_switching_off_with_no_account_is_allowed(client, db_session):
    resp = _admin(client).put("/system/testing", json={"enabled": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] is None and _account(db_session) == []


# ---------------------------------------------------------------------------
# PATCH /system/testing/account
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_patch_renames_the_account(client, db_session):
    _switch_on(client)
    resp = _admin(client).patch("/system/testing/account", json={"username": "otra"})
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == SHAPE and resp.json()["username"] == "otra"
    assert [u.username for u in _account(db_session)] == ["otra"]
    assert _login(client, "otra", PASSWORD).status_code == 200


@pytest.mark.unit
def test_patch_gives_a_new_password(client, db_session):
    _switch_on(client)
    resp = _admin(client).patch("/system/testing/account", json={"password": OTHER_PASSWORD})
    assert resp.status_code == 200, resp.text
    _no_password_in(resp.text, OTHER_PASSWORD)
    assert _login(client, "pruebas", PASSWORD).status_code == 401
    assert _login(client, "pruebas", OTHER_PASSWORD).status_code == 200


@pytest.mark.unit
def test_patch_leaves_the_active_flag_and_the_space_as_they_are(client, db_session):
    _switch_on(client)
    _admin(client).put("/system/testing", json={"enabled": False})
    resp = _admin(client).patch("/system/testing/account", json={"username": "otra"})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["enabled"], resp.json()["account_active"]) == (False, False)


@pytest.mark.unit
@pytest.mark.parametrize("body", [{}, {"username": None}, {"username": None, "password": None}],
                         ids=["empty", "null-username", "both-null"])
def test_patch_with_nothing_is_a_422(client, db_session, body):
    _switch_on(client)
    assert _admin(client).patch("/system/testing/account", json=body).status_code == 422
    assert [u.username for u in _account(db_session)] == ["pruebas"]


@pytest.mark.unit
def test_patch_with_no_account_is_a_404(client, db_session):
    _code(_admin(client).patch("/system/testing/account", json={"username": "x"}), 404, "no_testing_account")


@pytest.mark.unit
def test_patch_to_a_username_held_by_another_user_is_refused(client, db_session):
    from app.models.user import User
    _switch_on(client)
    db_session.add(User(username="ana", hashed_password="x", role="operator"))
    db_session.commit()
    _code(_admin(client).patch("/system/testing/account", json={"username": "ana"}), 409, "username_taken")
    assert [u.username for u in _account(db_session)] == ["pruebas"]


@pytest.mark.unit
def test_patch_to_its_own_username_is_not_a_conflict(client, db_session):
    _switch_on(client)
    assert _admin(client).patch("/system/testing/account", json={"username": "pruebas"}).status_code == 200


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

CALLS = [
    ("get", "/system/testing", None),
    ("put", "/system/testing", {"enabled": True, "username": "pruebas", "password": PASSWORD}),
    ("patch", "/system/testing/account", {"username": "otra"}),
]


@pytest.mark.unit
@pytest.mark.parametrize("role", ["operator", "reviewer", "testing"])
@pytest.mark.parametrize("method,path,body", CALLS, ids=["get", "put", "patch"])
def test_only_an_admin_reaches_the_routes(client, db_session, role, method, path, body):
    _testing_user(db_session, "existente")
    c = _as(client, role)
    resp = getattr(c, method)(path, json=body) if body is not None else getattr(c, method)(path)
    assert resp.status_code == 403, resp.text
    [user] = _account(db_session)
    assert user.username == "existente"
    settings = _settings(db_session)
    assert settings is None or not settings.testing_enabled


# ---------------------------------------------------------------------------
# Nothing to Fisqua, no password anywhere
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_no_route_sends_the_users_to_fisqua(client, db_session, sends):
    _switch_on(client)
    _admin(client).patch("/system/testing/account", json={"username": "otra", "password": OTHER_PASSWORD})
    _admin(client).put("/system/testing", json={"enabled": False})
    _admin(client).put("/system/testing", json={"enabled": True})
    assert sends == []


@pytest.mark.unit
def test_the_sender_recorder_sees_an_ordinary_user_change(client, db_session, sends):
    """The recorder is wired where the ordinary user routes send: a user creation reaches it."""
    _real_auth()
    from app.core.security import create_access_token
    from app.models.user import User
    admin = User(username="adm", hashed_password="x", role="admin")
    db_session.add(admin)
    db_session.commit()
    resp = client.post("/auth/register", json={"username": "nuevo", "password": "pw"},
                       headers={"Authorization": f"Bearer {create_access_token(str(admin.id))}"})
    assert resp.status_code == 200, resp.text
    assert len(sends) == 1


@pytest.mark.unit
def test_in_fisqua_mode_the_link_is_not_called(client, db_session, link):
    _unit(db_session, "fisqua")
    _switch_on(client)
    _admin(client).patch("/system/testing/account", json={"username": "otra"})
    _admin(client).put("/system/testing", json={"enabled": False})
    assert link.operators_sent == [] and link.descriptions_asked == [] and link.other_calls == []


@pytest.mark.unit
def test_the_password_is_never_returned_or_logged(client, db_session, caplog):
    from app.models.system_log import SystemLog
    caplog.set_level(logging.DEBUG)
    responses = [_admin(client).put("/system/testing", json={"enabled": True, "username": "pruebas",
                                                             "password": PASSWORD}),
                 _admin(client).patch("/system/testing/account", json={"password": OTHER_PASSWORD}),
                 _admin(client).get("/system/testing")]
    for resp in responses:
        assert resp.status_code == 200, resp.text
        _no_password_in(resp.text, PASSWORD, OTHER_PASSWORD)
        assert "password" not in resp.text
    _no_password_in(caplog.text, PASSWORD, OTHER_PASSWORD)
    db_session.expire_all()
    for row in db_session.query(SystemLog).all():
        _no_password_in(" ".join(str(v) for v in (row.actor, row.subject, row.detail)), PASSWORD, OTHER_PASSWORD)


@pytest.mark.unit
def test_the_changes_are_recorded_in_the_system_log(client, db_session):
    _switch_on(client)
    _admin(client).patch("/system/testing/account", json={"username": "otra", "password": OTHER_PASSWORD})
    _admin(client).put("/system/testing", json={"enabled": False, "expiry_days": 5})
    created = _logs(db_session, "testing_account_created")
    assert [(r.actor, r.subject) for r in created] == [("admin", "pruebas")]
    changed = _logs(db_session, "testing_account_changed")
    assert len(changed) == 1 and changed[0].subject == "otra" and "password" in changed[0].detail
    space = _logs(db_session, "testing_space_changed")
    assert [r.actor for r in space] == ["admin", "admin"]
    assert "enabled" in space[0].detail and "expiry" in space[1].detail


# ---------------------------------------------------------------------------
# Every mode
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("mode", ["offline", "backup", "fisqua"])
def test_the_routes_work_in_every_mode(client, db_session, mode, link):
    _unit(db_session, mode)
    body = _switch_on(client)
    assert body["enabled"] is True
    assert _admin(client).patch("/system/testing/account", json={"username": "otra"}).status_code == 200
    resp = _admin(client).put("/system/testing", json={"enabled": False})
    assert resp.status_code == 200 and resp.json()["account_active"] is False
    assert _admin(client).get("/system/testing").json()["username"] == "otra"
    assert _settings(db_session).mode == mode


# ---------------------------------------------------------------------------
# The account's username and password are never empty
# ---------------------------------------------------------------------------

BAD_CREDENTIALS = [
    {"username": "", "password": PASSWORD},
    {"username": "   ", "password": PASSWORD},
    {"username": "x" * 151, "password": PASSWORD},
]
BAD_IDS = ["empty-username", "blank-username", "long-username"]


@pytest.mark.unit
@pytest.mark.parametrize("creds", BAD_CREDENTIALS, ids=BAD_IDS)
def test_switching_on_with_an_empty_credential_is_a_validation_422(client, db_session, creds):
    resp = _admin(client).put("/system/testing", json={"enabled": True, **creds})
    assert resp.status_code == 422, resp.text
    assert isinstance(resp.json()["detail"], list), resp.text  # the validation body, not account_required
    assert _account(db_session) == []


@pytest.mark.unit
@pytest.mark.parametrize("creds", BAD_CREDENTIALS, ids=BAD_IDS)
def test_switching_on_again_with_an_empty_credential_is_a_422(client, db_session, creds):
    from app.core.security import verify_password
    _switch_on(client)
    resp = _admin(client).put("/system/testing", json={"enabled": True, **creds})
    assert resp.status_code == 422, resp.text
    [user] = _account(db_session)
    assert user.username == "pruebas" and verify_password(PASSWORD, user.hashed_password)


@pytest.mark.unit
@pytest.mark.parametrize("body", [{"username": ""}, {"username": "  "}, {"username": "x" * 151}],
                         ids=["empty-username", "blank-username", "long-username"])
def test_patch_with_an_empty_credential_is_a_422(client, db_session, body):
    from app.core.security import verify_password
    _switch_on(client)
    assert _admin(client).patch("/system/testing/account", json=body).status_code == 422
    [user] = _account(db_session)
    assert user.username == "pruebas" and verify_password(PASSWORD, user.hashed_password)


@pytest.mark.unit
def test_a_username_of_150_characters_is_accepted(client, db_session):
    assert _switch_on(client, username="x" * 150)["username"] == "x" * 150


@pytest.mark.unit
def test_the_username_is_stored_stripped_and_the_password_is_not(client, db_session):
    body = _switch_on(client, username="  pruebas  ", password=" pw ")
    assert body["username"] == "pruebas"
    assert [u.username for u in _account(db_session)] == ["pruebas"]
    assert _login(client, "pruebas", " pw ").status_code == 200
    assert _login(client, "pruebas", "pw").status_code == 401


@pytest.mark.unit
def test_patch_strips_the_username(client, db_session):
    _switch_on(client)
    resp = _admin(client).patch("/system/testing/account", json={"username": " otra "})
    assert resp.status_code == 200 and resp.json()["username"] == "otra"


@pytest.mark.unit
def test_the_uniqueness_check_reads_the_stripped_username(client, db_session):
    from app.models.user import User
    db_session.add(User(username="ana", hashed_password="x", role="operator"))
    db_session.commit()
    _code(_admin(client).put("/system/testing", json={"enabled": True, "username": " ana ",
                                                      "password": PASSWORD}), 409, "username_taken")
    _switch_on(client)
    _code(_admin(client).patch("/system/testing/account", json={"username": "ana  "}), 409, "username_taken")
    assert [u.username for u in _account(db_session)] == ["pruebas"]


@pytest.mark.unit
def test_the_expiry_bounds_have_one_definition(monkeypatch):
    """The schema reads the bounds from app.core.testing_space."""
    from app.api.system import TestingUpdate
    from app.core import testing_space
    meta = TestingUpdate.model_fields["expiry_days"].metadata
    ge = [m.ge for m in meta if getattr(m, "ge", None) is not None]
    le = [m.le for m in meta if getattr(m, "le", None) is not None]
    assert (ge, le) == ([testing_space.MIN_EXPIRY_DAYS], [testing_space.MAX_EXPIRY_DAYS])
    import inspect
    from app.api import system
    src = inspect.getsource(system.TestingUpdate)
    assert "MIN_EXPIRY_DAYS" in src and "MAX_EXPIRY_DAYS" in src


# ---------------------------------------------------------------------------
# An empty password: refused with a code, never echoed
# ---------------------------------------------------------------------------

def _password_empty(resp):
    import json
    _code(resp, 422, "password_empty")
    assert '"input"' not in resp.text and "input" not in json.dumps(resp.json()["detail"])


@pytest.mark.unit
def test_an_empty_password_switching_on_with_no_account_is_password_empty(client, db_session):
    _password_empty(_admin(client).put("/system/testing", json={"enabled": True, "username": "pruebas",
                                                                "password": ""}))
    assert _account(db_session) == []
    settings = _settings(db_session)
    assert settings is None or not settings.testing_enabled


@pytest.mark.unit
def test_an_empty_password_switching_on_again_is_password_empty(client, db_session):
    from app.core.security import verify_password
    _switch_on(client)
    _admin(client).put("/system/testing", json={"enabled": False})
    _password_empty(_admin(client).put("/system/testing", json={"enabled": True, "password": ""}))
    [user] = _account(db_session)
    assert not user.is_active and verify_password(PASSWORD, user.hashed_password)
    assert _settings(db_session).testing_enabled is False


@pytest.mark.unit
def test_an_empty_password_in_patch_is_password_empty(client, db_session):
    from app.core.security import verify_password
    _switch_on(client)
    _password_empty(_admin(client).patch("/system/testing/account", json={"username": "otra", "password": ""}))
    [user] = _account(db_session)
    assert user.username == "pruebas" and verify_password(PASSWORD, user.hashed_password)


@pytest.mark.unit
@pytest.mark.parametrize("schema", ["TestingUpdate", "TestingAccountUpdate"])
def test_the_password_carries_no_pydantic_constraint(schema):
    from typing import Optional
    from app.api import system
    field = getattr(system, schema).model_fields["password"]
    assert field.annotation == Optional[str] and field.metadata == []


# ---------------------------------------------------------------------------
# `enabled` is a strict boolean
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("value", ["yes", "on", "true", 1], ids=["yes", "on", "true-string", "1"])
def test_enabled_is_a_strict_boolean(client, db_session, value):
    resp = _admin(client).put("/system/testing", json={"enabled": value, "username": "pruebas",
                                                       "password": PASSWORD})
    assert resp.status_code == 422, resp.text
    assert _account(db_session) == []


# ---------------------------------------------------------------------------
# The unit's two settings routes leave each other's fields alone
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_put_unit_leaves_the_testing_settings_as_they_are(client, db_session):
    _unit(db_session, "offline")
    _switch_on(client, expiry_days=9)
    before = _settings(db_session)
    expected = (before.testing_enabled, before.testing_user_id, before.testing_expiry_days)
    resp = _admin(client).put("/system/unit", json={"projectsEnabled": True})
    assert resp.status_code == 200, resp.text
    after = _settings(db_session)
    assert after.projects_enabled is True
    assert (after.testing_enabled, after.testing_user_id, after.testing_expiry_days) == expected == (True, expected[1], 9)


@pytest.mark.unit
@pytest.mark.parametrize("mode,projects", [("offline", True), ("backup", False), ("fisqua", True)])
def test_put_testing_leaves_the_mode_and_projects_setting_as_they_are(client, db_session, link, mode, projects):
    _unit(db_session, mode, projects_enabled=projects)
    _switch_on(client, expiry_days=4)
    _admin(client).patch("/system/testing/account", json={"username": "otra"})
    _admin(client).put("/system/testing", json={"enabled": False})
    after = _settings(db_session)
    assert (after.mode, after.projects_enabled) == (mode, projects)


# ---------------------------------------------------------------------------
# Sessions: a switch-off and a new password end the account's sessions
# ---------------------------------------------------------------------------

def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _token_for(client, username, password):
    resp = _login(client, username, password)
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _works(client, token):
    """Whether `token` opens a protected route and renews: both or neither."""
    _real_auth()
    me = client.get("/users/me", headers=_bearer(token)).status_code
    refresh = client.post("/auth/refresh", headers=_bearer(token)).status_code
    assert (me, refresh) in ((200, 200), (401, 401)), (me, refresh)
    return me == 200


def _hand_made_token(user_id, **claims):
    """A token as earlier releases made it: `sub` and `exp`, signed with the same secret, no `ver`."""
    import base64
    import hashlib
    import hmac
    import json
    import time
    from app.core import security

    def b64(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    payload = {"sub": str(user_id), "exp": int(time.time()) + 600, **claims}
    enc = b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(security.SECRET_KEY.encode("utf-8"), enc.encode("utf-8"), hashlib.sha256).digest()
    return f"{enc}.{b64(sig)}"


def _ordinary_user(db, username="ana", role="operator", password=PASSWORD):
    from app.core.security import hash_password
    from app.models.user import User
    user = User(username=username, hashed_password=hash_password(password), role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.mark.unit
def test_a_token_carries_the_session_generation(client, db_session):
    from app.core.security import verify_access_token
    _switch_on(client)
    assert verify_access_token(_token_for(client, "pruebas", PASSWORD))["ver"] == 0
    _admin(client).patch("/system/testing/account", json={"password": OTHER_PASSWORD})
    assert verify_access_token(_token_for(client, "pruebas", OTHER_PASSWORD))["ver"] == 1


@pytest.mark.unit
def test_a_token_from_before_a_switch_off_stays_refused_after_switching_on_again(client, db_session):
    _switch_on(client)
    token = _token_for(client, "pruebas", PASSWORD)
    assert _works(client, token)
    _admin(client).put("/system/testing", json={"enabled": False})
    assert not _works(client, token)
    _admin(client).put("/system/testing", json={"enabled": True})
    [user] = _account(db_session)
    assert user.is_active
    assert not _works(client, token)
    assert _works(client, _token_for(client, "pruebas", PASSWORD))


@pytest.mark.unit
def test_a_renewed_token_from_before_a_switch_off_is_refused_too(client, db_session):
    _switch_on(client)
    token = _token_for(client, "pruebas", PASSWORD)
    _real_auth()
    renewed = client.post("/auth/refresh", headers=_bearer(token)).json()["access_token"]
    _admin(client).put("/system/testing", json={"enabled": False})
    _admin(client).put("/system/testing", json={"enabled": True})
    assert not _works(client, renewed)


@pytest.mark.unit
def test_a_new_password_ends_the_accounts_sessions(client, db_session):
    _switch_on(client)
    token = _token_for(client, "pruebas", PASSWORD)
    _admin(client).patch("/system/testing/account", json={"password": OTHER_PASSWORD})
    assert not _works(client, token)
    assert _works(client, _token_for(client, "pruebas", OTHER_PASSWORD))


@pytest.mark.unit
def test_a_new_password_switching_on_again_ends_the_earlier_sessions_once(client, db_session):
    _switch_on(client)
    _admin(client).put("/system/testing", json={"enabled": False})
    _admin(client).put("/system/testing", json={"enabled": True, "password": OTHER_PASSWORD})
    [user] = _account(db_session)
    assert user.token_version == 2  # one for the switch-off, one for the password


@pytest.mark.unit
def test_a_stale_token_is_refused_by_the_password_reset_route(client, db_session):
    _switch_on(client)
    token = _token_for(client, "pruebas", PASSWORD)
    _admin(client).patch("/system/testing/account", json={"password": OTHER_PASSWORD})
    _real_auth()
    resp = client.post("/auth/password-reset", headers=_bearer(token),
                       json={"old_password": OTHER_PASSWORD, "new_password": "x"})
    assert resp.status_code == 401 and resp.json()["detail"] == "Invalid or expired token"


@pytest.mark.unit
def test_a_stale_admin_token_is_refused_by_the_register_route(client, db_session):
    from app.models.user import User
    admin = _ordinary_user(db_session, "jefa", role="admin")
    token = _hand_made_token(admin.id)
    admin.token_version = 1
    db_session.commit()
    _real_auth()
    resp = client.post("/auth/register", json={"username": "nuevo", "password": "pw"}, headers=_bearer(token))
    assert resp.status_code == 401, resp.text
    assert db_session.query(User).filter(User.username == "nuevo").first() is None


@pytest.mark.unit
@pytest.mark.parametrize("change", [
    ("patch", "/system/testing/account", {"username": "otra"}),
    ("put", "/system/testing", {"enabled": True, "expiry_days": 12}),
    ("put", "/system/testing", {"enabled": True, "username": "otra"}),
    ("put", "/system/testing", {"enabled": True}),
], ids=["rename", "term", "rename-on-put", "on-again-while-on"])
def test_a_rename_or_a_term_change_leaves_the_sessions(client, db_session, change):
    _switch_on(client)
    token = _token_for(client, "pruebas", PASSWORD)
    method, path, body = change
    assert getattr(_admin(client), method)(path, json=body).status_code == 200
    assert _works(client, token)
    [user] = _account(db_session)
    assert user.token_version == 0


@pytest.mark.unit
def test_switching_off_an_already_inactive_account_does_not_count_again(client, db_session):
    _switch_on(client)
    _admin(client).put("/system/testing", json={"enabled": False})
    _admin(client).put("/system/testing", json={"enabled": False, "expiry_days": 3})
    [user] = _account(db_session)
    assert user.token_version == 1


@pytest.mark.unit
def test_a_token_without_the_claim_counts_as_generation_0(client, db_session):
    user = _ordinary_user(db_session)
    token = _hand_made_token(user.id)
    assert _works(client, token)
    user.token_version = 1
    db_session.commit()
    assert not _works(client, token)


@pytest.mark.unit
def test_a_token_with_a_malformed_claim_is_refused(client, db_session):
    user = _ordinary_user(db_session)
    assert not _works(client, _hand_made_token(user.id, ver="x"))


@pytest.mark.unit
def test_an_ordinary_users_sessions_are_untouched_by_the_testing_operations(client, db_session):
    user = _ordinary_user(db_session)
    old_style = _hand_made_token(user.id)
    token = _token_for(client, "ana", PASSWORD)
    _switch_on(client)
    _admin(client).patch("/system/testing/account", json={"username": "otra", "password": OTHER_PASSWORD})
    _admin(client).put("/system/testing", json={"enabled": False, "expiry_days": 5})
    _admin(client).put("/system/testing", json={"enabled": True, "password": PASSWORD})
    assert _works(client, token) and _works(client, old_style)
    db_session.expire_all()
    assert db_session.get(type(user), user.id).token_version == 0


# ---------------------------------------------------------------------------
# Login decides on one read: a change made during the password check wins
# ---------------------------------------------------------------------------

def _during_the_check(monkeypatch, change):
    """verify_password, as app.api.auth calls it, runs `change` in a second session and then answers True."""
    from app.api import auth as auth_mod
    from app.core.db import SessionLocal
    from app.core.security import verify_password as real_verify

    def verify(password, hashed):
        ok = real_verify(password, hashed)
        other = SessionLocal()
        try:
            change(other)
        finally:
            other.close()
        return ok

    monkeypatch.setattr(auth_mod, "verify_password", verify)


def _login_token(client, username, password):
    resp = _login(client, username, password)
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest.mark.unit
def test_a_login_straddling_a_password_change_gets_a_refused_token(client, db_session, monkeypatch):
    from app.core import testing_space
    _switch_on(client)
    _during_the_check(monkeypatch, lambda other: testing_space.change_account(
        other, actor="admin", password=OTHER_PASSWORD))
    token = _login_token(client, "pruebas", PASSWORD)
    monkeypatch.undo()
    assert not _works(client, token)


@pytest.mark.unit
def test_a_login_straddling_a_switch_off_gets_a_refused_token(client, db_session, monkeypatch):
    from app.core import testing_space
    _switch_on(client)
    _during_the_check(monkeypatch, lambda other: testing_space.change(other, actor="admin", enabled=False))
    token = _login_token(client, "pruebas", PASSWORD)
    monkeypatch.undo()
    assert not _works(client, token)
    _admin(client).put("/system/testing", json={"enabled": True})
    assert not _works(client, token)


@pytest.mark.unit
def test_an_ordinary_login_with_nothing_happening_works_as_before(client, db_session, monkeypatch):
    from app.core.security import verify_access_token
    user = _ordinary_user(db_session)
    _during_the_check(monkeypatch, lambda other: None)
    token = _login_token(client, "ana", PASSWORD)
    monkeypatch.undo()
    assert _works(client, token)
    assert verify_access_token(token)["ver"] == 0 and verify_access_token(token)["sub"] == str(user.id)


@pytest.mark.unit
def test_the_login_rehash_never_overwrites_a_password_changed_during_the_check(client, db_session, monkeypatch):
    from app.api import auth as auth_mod
    from app.core import testing_space
    from app.core.security import verify_password
    _switch_on(client)
    _during_the_check(monkeypatch, lambda other: testing_space.change_account(
        other, actor="admin", password=OTHER_PASSWORD))
    monkeypatch.setattr(auth_mod, "needs_rehash", lambda hashed: True)
    resp = _login(client, "pruebas", PASSWORD)
    assert resp.status_code == 401 and resp.json()["detail"] == "Invalid credentials"
    monkeypatch.undo()
    [user] = _account(db_session)
    assert verify_password(OTHER_PASSWORD, user.hashed_password)
    assert not verify_password(PASSWORD, user.hashed_password)


@pytest.mark.unit
def test_the_login_rehash_still_upgrades_an_unchanged_hash(client, db_session, monkeypatch):
    from app.api import auth as auth_mod
    from app.core.security import verify_password
    from app.models.user import User
    user = _ordinary_user(db_session)
    old_hash = user.hashed_password
    monkeypatch.setattr(auth_mod, "needs_rehash", lambda hashed: hashed == old_hash)
    assert _works(client, _login_token(client, "ana", PASSWORD))
    db_session.expire_all()
    stored = db_session.get(User, user.id).hashed_password
    assert stored != old_hash and verify_password(PASSWORD, stored)


# ---------------------------------------------------------------------------
# A password that cannot be encoded
# ---------------------------------------------------------------------------

LONE_SURROGATE = '"\\ud800"'


@pytest.mark.unit
def test_an_unencodable_password_switching_on_is_password_invalid(client, db_session):
    body = '{"enabled": true, "username": "pruebas", "password": %s}' % LONE_SURROGATE
    resp = _admin(client).put("/system/testing", content=body, headers={"Content-Type": "application/json"})
    detail = _code(resp, 422, "password_invalid")
    assert "\ud800" not in detail["message"] and "ud800" not in resp.text
    assert _account(db_session) == []


@pytest.mark.unit
def test_an_unencodable_password_in_patch_is_password_invalid(client, db_session):
    from app.core.security import verify_password
    _switch_on(client)
    body = '{"password": %s}' % LONE_SURROGATE
    resp = _admin(client).patch("/system/testing/account", content=body,
                                headers={"Content-Type": "application/json"})
    _code(resp, 422, "password_invalid")
    assert "ud800" not in resp.text
    [user] = _account(db_session)
    assert verify_password(PASSWORD, user.hashed_password) and user.token_version == 0
