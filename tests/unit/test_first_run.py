"""The first run of a unit, the unit's default language, each account's language and the unit's capture key (NEH-336).

- The first account may be created without the bootstrap token from the
  unit itself: a request whose socket peer is loopback (127.0.0.1, ::1),
  that carries no forwarding header and names a loopback host. Every other
  first registration needs the token, as before; once an account exists,
  only an administrator creates accounts. Two first registrations at once
  create one account.
- The kiosk's page (origin http://localhost) calls the backend's loopback
  address directly, so POST /auth/register and GET /auth/setup/status
  answer that origin and http://127.0.0.1 without credentials; no other
  route gains the allowance.
- `unit_settings.default_language` ('es' or 'en', default 'es'): read
  publicly through GET /auth/setup/status, written by an administrator
  through PUT /system/unit and by the registration that creates the first
  account.
- `users.language` (null until chosen): in GET /users/me, set by the
  signed-in user through PUT /users/me/language, any role.
- `unit_settings.capture_key` (null for the built-in default): one chord,
  shaped as the frontend's CaptureChord, in GET /system/unit and written by
  an administrator or an operator through PUT /system/unit/capture-key.

    python -m pytest tests/unit/test_first_run.py
"""

import importlib.util
import re
import threading
import time

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

REVISION = "c41302e2c9f0"
PARENT = "c8e2a4f6b0d1"
MIGRATION_FILE = f"{REVISION}_add_languages_and_capture_key.py"
TOKEN = "first-run-test-token"
PASSWORD = "first-run-pass"
LOOPBACK = ("127.0.0.1", "::1")
DEVICE = "192.168.1.50"
BRIDGE = "172.30.0.3"  # nginx, from the appliance's compose network
CHORD = {"key": "b", "ctrl": False, "alt": False, "meta": False, "shift": False}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def unit_token(monkeypatch):
    """A unit as it is set up in the field: production, with a bootstrap token."""
    from app.api import auth
    monkeypatch.setattr(auth.settings, "APP_ENV", "production")
    monkeypatch.setattr(auth.settings, "BOOTSTRAP_TOKEN", TOKEN)
    return TOKEN


@pytest.fixture
def peer(client):
    """A TestClient whose socket peer is the given address, on the client fixture's database."""
    from fastapi.testclient import TestClient
    from app.main import app

    def make(host, port=50000, asgi=None, base_url=None):
        if base_url is None:
            # Starlette's TestClient cannot take an IPv6 literal as its base URL.
            base_url = "http://127.0.0.1:8000" if host != "::1" else "http://localhost:8000"
        return TestClient(asgi or app, client=(host, port), base_url=base_url)

    return make


def _register(c, username="first-admin", headers=None, **extra):
    return c.post("/auth/register", json={"username": username, "password": PASSWORD, **extra},
                  headers=headers or {})


def _user_count(db):
    from app.models.user import User
    db.expire_all()
    return db.query(User).count()


def _account(db, role, username=None):
    """A user of `role` and the Authorization header of a current token for it."""
    from app.core.security import create_access_token, hash_password
    from app.models.user import User
    user = User(username=username or f"u-{role}", hashed_password=hash_password(PASSWORD), role=role,
                is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user, {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


def _unit_row(db):
    from app.models.unit_settings import UnitSettings
    db.expire_all()
    return db.get(UnitSettings, 1)


def _logs(db, action):
    from app.models.system_log import SystemLog
    db.expire_all()
    return db.query(SystemLog).filter(SystemLog.action == action).all()


# ---------------------------------------------------------------------------
# A. The first account from the unit itself
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("host", LOOPBACK)
def test_the_first_account_from_loopback_needs_no_token(peer, db_session, unit_token, host):
    resp = _register(peer(host))
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "admin"
    assert _user_count(db_session) == 1


@pytest.mark.unit
@pytest.mark.parametrize("host", [DEVICE, BRIDGE, "100.64.0.7", "testclient"])
def test_the_first_account_from_another_peer_without_token_is_refused_as_today(peer, db_session, unit_token,
                                                                                host):
    resp = _register(peer(host))
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Invalid or missing bootstrap token"
    assert _user_count(db_session) == 0


FORGED = [
    {"X-Forwarded-For": "127.0.0.1"},
    {"X-Real-IP": "127.0.0.1"},
    {"Forwarded": "for=127.0.0.1"},
    {"X-Forwarded-For": "::1"},
]


@pytest.mark.unit
@pytest.mark.parametrize("headers", FORGED)
def test_forged_forwarding_headers_from_another_peer_are_refused(peer, db_session, unit_token, headers):
    resp = _register(peer(DEVICE), headers=headers)
    assert resp.status_code == 401, resp.text
    assert _user_count(db_session) == 0


@pytest.mark.unit
@pytest.mark.parametrize("headers", FORGED)
def test_forged_headers_are_refused_even_where_uvicorn_trusts_every_proxy(peer, db_session, unit_token, headers):
    """uvicorn's ProxyHeadersMiddleware with FORWARDED_ALLOW_IPS=* rewrites the client from X-Forwarded-For."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    from app.main import app

    c = peer(DEVICE, asgi=ProxyHeadersMiddleware(app, trusted_hosts="*"))
    resp = _register(c, headers=headers)
    assert resp.status_code == 401, resp.text
    status = c.get("/auth/setup/status", headers=headers).json()
    assert status["local"] is False
    assert _user_count(db_session) == 0


@pytest.mark.unit
@pytest.mark.parametrize("headers", FORGED + [{"X-Forwarded-Proto": "http"}])
def test_a_loopback_peer_carrying_a_forwarding_header_is_not_local(peer, db_session, unit_token, headers):
    """A proxy on the unit itself connects from loopback and names its client in a header: not local."""
    resp = _register(peer("127.0.0.1"), headers=headers)
    assert resp.status_code == 401, resp.text
    assert _user_count(db_session) == 0


@pytest.mark.unit
@pytest.mark.parametrize("host_header", ["evil.example:8000", "captua.local:8000", "192.168.1.9:8000"])
def test_a_loopback_peer_naming_another_host_is_not_local(peer, db_session, unit_token, host_header):
    """A page of another site rebound to 127.0.0.1 sends its own host name: not local."""
    resp = _register(peer("127.0.0.1"), headers={"Host": host_header})
    assert resp.status_code == 401, resp.text
    assert _user_count(db_session) == 0


@pytest.mark.unit
@pytest.mark.parametrize("host_header", ["localhost:8000", "127.0.0.1:8000", "[::1]:8000", "localhost",
                                         "LOCALHOST:8000"])
@pytest.mark.parametrize("host", LOOPBACK)
def test_every_loopback_host_name_is_local(peer, db_session, unit_token, host, host_header):
    c = peer(host)
    assert c.get("/auth/setup/status", headers={"Host": host_header}).json()["local"] is True


@pytest.mark.unit
def test_loopback_with_an_account_already_follows_todays_rules(peer, db_session, unit_token):
    _account(db_session, "admin", "existing-admin")
    c = peer("127.0.0.1")
    resp = _register(c, "second")
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Not authenticated"
    resp = _register(c, "third", headers={"X-Bootstrap-Token": TOKEN})
    assert resp.status_code == 401, resp.text
    assert _user_count(db_session) == 1


@pytest.mark.unit
def test_loopback_with_an_account_and_an_operator_session_is_refused(peer, db_session, unit_token):
    _account(db_session, "admin", "existing-admin")
    _, headers = _account(db_session, "operator")
    resp = _register(peer("127.0.0.1"), "second", headers=headers)
    assert resp.status_code == 403, resp.text


@pytest.mark.unit
@pytest.mark.parametrize("host", [DEVICE, "127.0.0.1"])
def test_the_token_still_creates_the_first_account_from_anywhere(peer, db_session, unit_token, host):
    resp = _register(peer(host), headers={"X-Bootstrap-Token": TOKEN})
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "admin"


@pytest.mark.unit
def test_a_wrong_token_from_another_device_is_still_refused(peer, db_session, unit_token):
    resp = _register(peer(DEVICE), headers={"X-Bootstrap-Token": "wrong"})
    assert resp.status_code == 401


@pytest.mark.unit
def test_two_first_registrations_at_once_create_one_account(db_session, unit_token, monkeypatch):
    """Each request on its own session, the password hash slowed so both pass the first count.

    The unit's settings row exists, as start-up writes it (unit_mode.initialise)
    before any request: without it the two requests would wait for each
    other on inserting that row, and the test would not reach the guard.
    """
    from fastapi.testclient import TestClient
    from app.api import auth
    from app.main import app
    from app.models.unit_settings import UnitSettings

    db_session.add(UnitSettings(id=1, mode="offline"))
    db_session.commit()
    app.dependency_overrides.clear()
    real_hash = auth.hash_password

    def slow_hash(password):
        time.sleep(0.4)
        return real_hash(password)

    monkeypatch.setattr(auth, "hash_password", slow_hash)
    barrier = threading.Barrier(2)
    results = {}

    def go(name):
        c = TestClient(app, client=("127.0.0.1", 50000), base_url="http://127.0.0.1:8000")
        barrier.wait()
        results[name] = _register(c, name)

    threads = [threading.Thread(target=go, args=(n,)) for n in ("racer-a", "racer-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    statuses = sorted(r.status_code for r in results.values())
    assert statuses == [200, 401], {k: (r.status_code, r.text) for k, r in results.items()}
    assert _user_count(db_session) == 1


# ---------------------------------------------------------------------------
# A. GET /auth/setup/status
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_setup_status_says_whether_the_request_is_local_and_the_units_language(peer, db_session):
    assert peer("127.0.0.1").get("/auth/setup/status").json() == {
        "needs_setup": True, "local": True, "default_language": "es"}
    assert peer(BRIDGE).get("/auth/setup/status").json() == {
        "needs_setup": True, "local": False, "default_language": "es"}
    # Read without writing the unit's row.
    assert _unit_row(db_session) is None


@pytest.mark.unit
def test_setup_status_reads_the_stored_language(peer, db_session):
    from app.models.unit_settings import UnitSettings
    db_session.add(UnitSettings(id=1, mode="offline", default_language="en"))
    db_session.commit()
    _account(db_session, "admin")
    assert peer(DEVICE).get("/auth/setup/status").json() == {
        "needs_setup": False, "local": False, "default_language": "en"}


# ---------------------------------------------------------------------------
# A. CORS for the kiosk's page
# ---------------------------------------------------------------------------

def _preflight(c, path, origin, method, headers="content-type"):
    return c.options(path, headers={"Origin": origin, "Access-Control-Request-Method": method,
                                    "Access-Control-Request-Headers": headers})


@pytest.mark.unit
@pytest.mark.parametrize("origin", ["http://localhost", "http://127.0.0.1"])
def test_the_kiosk_page_may_post_the_first_account(peer, db_session, unit_token, origin):
    c = peer("127.0.0.1")
    pre = _preflight(c, "/auth/register", origin, "POST")
    assert pre.status_code == 200, pre.text
    assert pre.headers["access-control-allow-origin"] == origin
    assert "POST" in pre.headers["access-control-allow-methods"]
    assert "content-type" in pre.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in pre.headers
    resp = c.post("/auth/register", json={"username": "kiosk-admin", "password": PASSWORD},
                  headers={"Origin": origin})
    assert resp.status_code == 200, resp.text
    assert resp.headers["access-control-allow-origin"] == origin
    assert "access-control-allow-credentials" not in resp.headers


@pytest.mark.unit
@pytest.mark.parametrize("origin", ["http://localhost", "http://127.0.0.1"])
def test_the_kiosk_page_may_read_setup_status(peer, db_session, origin):
    c = peer("127.0.0.1")
    pre = _preflight(c, "/auth/setup/status", origin, "GET", headers="x-requested-with")
    assert pre.status_code == 200, pre.text
    assert pre.headers["access-control-allow-origin"] == origin
    assert "access-control-allow-credentials" not in pre.headers
    resp = c.get("/auth/setup/status", headers={"Origin": origin})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == origin
    assert "access-control-allow-credentials" not in resp.headers
    assert resp.json()["local"] is True


@pytest.mark.unit
def test_the_kiosk_allowance_is_for_one_method_per_route(peer, db_session):
    c = peer("127.0.0.1")
    assert _preflight(c, "/auth/register", "http://localhost", "PUT").status_code == 400
    assert _preflight(c, "/auth/setup/status", "http://localhost", "POST").status_code == 400
    bad = _preflight(c, "/auth/register", "http://localhost", "POST", headers="authorization")
    assert bad.status_code == 400
    assert "access-control-allow-origin" not in bad.headers


@pytest.mark.unit
@pytest.mark.parametrize("path, method", [("/auth/login", "POST"), ("/system/unit", "GET"),
                                          ("/users/me", "GET"), ("/system/unit/capture-key", "PUT"),
                                          ("/users/me/language", "PUT")])
def test_no_other_route_gains_the_kiosk_allowance(peer, db_session, path, method):
    c = peer("127.0.0.1")
    pre = _preflight(c, path, "http://localhost", method)
    assert pre.status_code == 400
    assert "access-control-allow-origin" not in pre.headers
    resp = c.request(method, path, headers={"Origin": "http://localhost"}, json={})
    assert "access-control-allow-origin" not in resp.headers


@pytest.mark.unit
def test_another_origin_gains_nothing_on_the_first_run_routes(peer, db_session):
    c = peer("127.0.0.1")
    pre = _preflight(c, "/auth/register", "http://evil.example", "POST")
    assert pre.status_code == 400
    resp = c.get("/auth/setup/status", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in resp.headers


@pytest.mark.unit
def test_a_configured_origin_keeps_its_credentialed_cors(peer, db_session):
    from app.core.config import settings
    origin = settings.CORS_ORIGINS[0]
    c = peer("127.0.0.1")
    pre = _preflight(c, "/auth/register", origin, "POST")
    assert pre.status_code == 200
    assert pre.headers["access-control-allow-credentials"] == "true"
    resp = c.get("/auth/setup/status", headers={"Origin": origin})
    assert resp.headers["access-control-allow-origin"] == origin
    assert resp.headers["access-control-allow-credentials"] == "true"


# ---------------------------------------------------------------------------
# B. The unit's default language
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_first_account_sets_the_units_language_and_takes_it(peer, db_session, unit_token):
    resp = _register(peer("127.0.0.1"), default_language="en")
    assert resp.status_code == 200, resp.text
    assert resp.json()["language"] == "en"
    assert _unit_row(db_session).default_language == "en"
    assert peer(DEVICE).get("/auth/setup/status").json()["default_language"] == "en"
    assert len(_logs(db_session, "unit_default_language_changed")) == 1


@pytest.mark.unit
def test_a_first_account_without_a_language_leaves_the_default(peer, db_session, unit_token):
    resp = _register(peer("127.0.0.1"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["language"] is None
    row = _unit_row(db_session)
    assert row is None or row.default_language == "es"


@pytest.mark.unit
def test_a_later_account_does_not_change_the_units_language(peer, db_session, unit_token):
    assert _register(peer("127.0.0.1"), default_language="en").status_code == 200
    token = peer("127.0.0.1").post("/auth/login", json={"username": "first-admin", "password": PASSWORD})
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
    resp = _register(peer(DEVICE), "second", headers=headers, default_language="es")
    assert resp.status_code == 200, resp.text
    assert resp.json()["language"] is None
    assert _unit_row(db_session).default_language == "en"


@pytest.mark.unit
def test_register_refuses_an_unknown_language(peer, db_session, unit_token):
    resp = _register(peer("127.0.0.1"), default_language="fr")
    assert resp.status_code == 422
    assert _user_count(db_session) == 0


@pytest.mark.unit
def test_an_admin_sets_the_units_language(client, db_session):
    _, admin = _account(db_session, "admin")
    resp = client.put("/system/unit", json={"defaultLanguage": "en"}, headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["defaultLanguage"] == "en"
    assert client.get("/system/unit", headers=admin).json()["defaultLanguage"] == "en"
    assert client.get("/auth/setup/status").json()["default_language"] == "en"
    assert _unit_row(db_session).updated_by == "u-admin"
    assert len(_logs(db_session, "unit_default_language_changed")) == 1


@pytest.mark.unit
@pytest.mark.parametrize("role", ["operator", "reviewer", "testing"])
def test_no_other_role_sets_the_units_language(client, db_session, role):
    _, headers = _account(db_session, role)
    resp = client.put("/system/unit", json={"defaultLanguage": "en"}, headers=headers)
    assert resp.status_code == 403
    assert client.get("/auth/setup/status").json()["default_language"] == "es"


@pytest.mark.unit
def test_the_units_language_is_es_or_en(client, db_session):
    _, admin = _account(db_session, "admin")
    assert client.put("/system/unit", json={"defaultLanguage": "fr"}, headers=admin).status_code == 422
    assert client.put("/system/unit", json={"defaultLanguage": None}, headers=admin).status_code == 200
    assert client.get("/system/unit", headers=admin).json()["defaultLanguage"] == "es"


# ---------------------------------------------------------------------------
# C. Each account's language
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("role", ["admin", "operator", "reviewer", "testing"])
def test_every_role_sets_its_own_language(client, db_session, role):
    user, headers = _account(db_session, role)
    assert client.get("/users/me", headers=headers).json()["language"] is None
    resp = client.put("/users/me/language", json={"language": "en"}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["language"] == "en"
    assert client.get("/users/me", headers=headers).json()["language"] == "en"
    resp = client.put("/users/me/language", json={"language": "es"}, headers=headers)
    assert resp.json()["language"] == "es"


@pytest.mark.unit
@pytest.mark.parametrize("body", [{"language": "fr"}, {"language": None}, {}, {"language": "en", "x": 1},
                                  {"language": "EN"}])
def test_a_language_is_es_or_en(client, db_session, body):
    _, headers = _account(db_session, "operator")
    assert client.put("/users/me/language", json=body, headers=headers).status_code == 422


@pytest.mark.unit
def test_setting_a_language_needs_a_session(client, db_session):
    assert client.put("/users/me/language", json={"language": "en"}).status_code == 401


@pytest.mark.unit
def test_setting_a_language_changes_only_the_callers_account(client, db_session):
    _, headers = _account(db_session, "operator")
    other, _ = _account(db_session, "reviewer")
    client.put("/users/me/language", json={"language": "en"}, headers=headers)
    db_session.refresh(other)
    assert other.language is None


@pytest.mark.unit
def test_accounts_an_admin_creates_start_without_a_language(client, db_session):
    _, admin = _account(db_session, "admin")
    resp = client.post("/auth/register", json={"username": "new-op", "password": PASSWORD}, headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["language"] is None


# ---------------------------------------------------------------------------
# D. The capture key
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("role", ["admin", "operator"])
def test_an_admin_or_operator_sets_and_clears_the_capture_key(client, db_session, role):
    _, headers = _account(db_session, role)
    assert client.get("/system/unit", headers=headers).json()["captureKey"] is None
    chord = {"key": "F5", "ctrl": True, "alt": False, "meta": False, "shift": True}
    resp = client.put("/system/unit/capture-key", json={"capture_key": chord}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["captureKey"] == chord
    assert client.get("/system/unit", headers=headers).json()["captureKey"] == chord
    assert len(_logs(db_session, "unit_capture_key_changed")) == 1
    resp = client.put("/system/unit/capture-key", json={"capture_key": None}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["captureKey"] is None
    assert _unit_row(db_session).capture_key is None


@pytest.mark.unit
def test_every_role_reads_the_capture_key_and_the_units_language(client, db_session):
    _, admin = _account(db_session, "admin")
    client.put("/system/unit/capture-key", json={"capture_key": CHORD}, headers=admin)
    client.put("/system/unit", json={"defaultLanguage": "en"}, headers=admin)
    for role in ("operator", "reviewer", "testing"):
        _, headers = _account(db_session, role)
        body = client.get("/system/unit", headers=headers).json()
        assert body["captureKey"] == CHORD and body["defaultLanguage"] == "en", role


@pytest.mark.unit
@pytest.mark.parametrize("role", ["reviewer", "testing"])
def test_reviewer_and_testing_do_not_set_the_capture_key(client, db_session, role):
    _, headers = _account(db_session, role)
    resp = client.put("/system/unit/capture-key", json={"capture_key": CHORD}, headers=headers)
    assert resp.status_code == 403
    assert _unit_row(db_session) is None or _unit_row(db_session).capture_key is None


@pytest.mark.unit
def test_the_capture_key_needs_a_session(client, db_session):
    assert client.put("/system/unit/capture-key", json={"capture_key": CHORD}).status_code == 401


MALFORMED = [
    {},
    {"capture_key": {"key": "b", "ctrl": False, "alt": False, "meta": False}},
    {"capture_key": {**CHORD, "code": "KeyB"}},
    {"capture_key": {**CHORD, "ctrl": "false"}},
    {"capture_key": {**CHORD, "ctrl": 0}},
    {"capture_key": {**CHORD, "key": ""}},
    {"capture_key": {**CHORD, "key": "x" * 33}},
    {"capture_key": {**CHORD, "key": 5}},
    {"capture_key": {**CHORD, "key": "Shift"}},
    {"capture_key": {**CHORD, "key": "Control"}},
    {"capture_key": {**CHORD, "key": "Dead"}},
    {"capture_key": {**CHORD, "key": "Unidentified"}},
    {"capture_key": {**CHORD, "key": "\n"}},
    {"capture_key": "b"},
    {"capture_key": [CHORD]},
    {"capture_key": CHORD, "other": 1},
]


@pytest.mark.unit
@pytest.mark.parametrize("body", MALFORMED)
def test_a_malformed_capture_key_is_refused(client, db_session, body):
    _, headers = _account(db_session, "admin")
    resp = client.put("/system/unit/capture-key", json=body, headers=headers)
    assert resp.status_code == 422, resp.text
    assert _unit_row(db_session) is None or _unit_row(db_session).capture_key is None


@pytest.mark.unit
@pytest.mark.parametrize("key", [" ", "b", "B", "Enter", "PageDown", "AudioVolumeUp", "ñ", "F12"])
def test_the_keys_a_pedal_or_keyboard_sends_are_taken(client, db_session, key):
    _, headers = _account(db_session, "operator")
    chord = {**CHORD, "key": key}
    resp = client.put("/system/unit/capture-key", json={"capture_key": chord}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["captureKey"] == chord


@pytest.mark.unit
def test_the_capture_key_is_not_in_the_admin_put(client, db_session):
    _, admin = _account(db_session, "admin")
    resp = client.put("/system/unit", json={"captureKey": CHORD}, headers=admin)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# The route table
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_new_routes_have_their_entries():
    from app.core.space_routes import ROUTES
    assert ROUTES[("PUT", "/users/me/language")].testing is True
    assert ROUTES[("PUT", "/system/unit/capture-key")].testing is False
    assert not ROUTES[("PUT", "/system/unit/capture-key")].public


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------

def _load_migration(backend_root):
    path = backend_root / "alembic" / "versions" / MIGRATION_FILE
    spec = importlib.util.spec_from_file_location("first_run_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_migration(engine, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def _before(engine):
    """unit_settings and users as the parent revision leaves them, with a row in each."""
    md = sa.MetaData()
    sa.Table("users", md, sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("username", sa.String(150), nullable=False, unique=True),
             sa.Column("role", sa.String(50), nullable=False),
             sa.Column("token_version", sa.Integer, nullable=False, server_default=sa.text("0")),
             sa.CheckConstraint("role IN ('admin','operator','reviewer','testing')", name="check_user_role"),
             sa.Index("uq_users_one_testing_account", "role", unique=True,
                      sqlite_where=sa.text("role = 'testing'"), postgresql_where=sa.text("role = 'testing'")))
    sa.Table("unit_settings", md, sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
             sa.Column("mode", sa.String(16), nullable=False),
             sa.CheckConstraint("id = 1", name="check_unit_settings_single_row"))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, username, role) VALUES (1, 'a', 'admin')"))
        conn.execute(sa.text("INSERT INTO unit_settings (id, mode) VALUES (1, 'offline')"))


@pytest.mark.unit
def test_the_migration_follows_the_testing_space_and_is_the_only_head(backend_root):
    mod = _load_migration(backend_root)
    assert mod.revision == REVISION
    revisions, downs = set(), set()
    for path in (backend_root / "alembic" / "versions").glob("*.py"):
        text = path.read_text()
        revisions.add(re.search(r"^revision(?:: str)? = '(\w+)'", text, re.M).group(1))
        downs.update(re.findall(r"'(\w+)'", re.search(r"^down_revision[^=]*= (.*)$", text, re.M).group(1)))
    # In the chain, and the chain has exactly one head, wherever other revisions are added.
    assert REVISION in revisions and mod.down_revision in revisions
    assert len(revisions - downs) == 1


@pytest.mark.unit
def test_the_migration_upgrades_and_downgrades_on_sqlite(backend_root, tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    try:
        mod = _load_migration(backend_root)
        _before(engine)
        _run_migration(engine, mod.upgrade)
        insp = sa.inspect(engine)
        unit = {c["name"]: c for c in insp.get_columns("unit_settings")}
        users = {c["name"]: c for c in insp.get_columns("users")}
        assert unit["default_language"]["nullable"] is False
        assert unit["capture_key"]["nullable"] is True
        assert users["language"]["nullable"] is True
        with engine.begin() as conn:
            row = conn.execute(sa.text("SELECT default_language, capture_key FROM unit_settings")).one()
            assert row[0] == "es" and row[1] is None
            assert conn.execute(sa.text("SELECT language FROM users")).scalar() is None
            conn.execute(sa.text("UPDATE users SET language = 'en'"))
            conn.execute(sa.text("UPDATE unit_settings SET default_language = 'en', capture_key = '{\"key\": \"b\"}'"))
            # The partial index survives the rebuild: several admins, one testing account.
            conn.execute(sa.text("INSERT INTO users (id, username, role) VALUES (2, 'b', 'admin')"))
            conn.execute(sa.text("INSERT INTO users (id, username, role) VALUES (3, 't', 'testing')"))
        for statement in ("UPDATE users SET language = 'fr'", "UPDATE unit_settings SET default_language = 'fr'",
                          "UPDATE unit_settings SET default_language = NULL",
                          "INSERT INTO users (id, username, role) VALUES (4, 'u', 'testing')",
                          "INSERT INTO users (id, username, role) VALUES (5, 'v', 'visitor')"):
            with pytest.raises(IntegrityError):
                with engine.begin() as conn:
                    conn.execute(sa.text(statement))
        names = {c["name"] for c in insp.get_check_constraints("users")}
        assert {"check_user_language", "check_user_role"} <= names
        names = {c["name"] for c in sa.inspect(engine).get_check_constraints("unit_settings")}
        assert {"check_unit_settings_default_language", "check_unit_settings_single_row"} <= names

        _run_migration(engine, mod.downgrade)
        insp = sa.inspect(engine)
        assert not {"default_language", "capture_key"} & {c["name"] for c in insp.get_columns("unit_settings")}
        assert "language" not in {c["name"] for c in insp.get_columns("users")}
        with engine.begin() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM users")).scalar() == 3
            assert conn.execute(sa.text("SELECT mode FROM unit_settings")).scalar() == "offline"
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(sa.text("INSERT INTO users (id, username, role) VALUES (6, 'w', 'testing')"))
    finally:
        engine.dispose()


@pytest.mark.unit
def test_the_models_match_the_migration(backend_root):
    from app.core.db import Base
    import app.models.unit_settings  # noqa: F401
    import app.models.user  # noqa: F401

    mod = _load_migration(backend_root)
    tables = Base.metadata.tables
    lang = tables["unit_settings"].c.default_language
    assert lang.nullable is False and lang.server_default is not None
    assert tables["unit_settings"].c.capture_key.nullable is True
    assert tables["users"].c.language.nullable is True

    def checks(table):
        return {c.name: str(c.sqltext) for c in tables[table].constraints if isinstance(c, sa.CheckConstraint)}

    assert checks("unit_settings")["check_unit_settings_default_language"] == mod.DEFAULT_LANGUAGE_CHECK
    assert checks("users")["check_user_language"] == mod.USER_LANGUAGE_CHECK
