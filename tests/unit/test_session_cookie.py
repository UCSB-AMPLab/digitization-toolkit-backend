"""The browser keeps its session in an httpOnly cookie, never in a URL (NEH-61).

Any XSS bug can read a token kept in localStorage, and access logs, history
and referrers record a token placed in a URL. Login sets an httpOnly,
SameSite=Strict, Path=/ cookie that holds the signed token, and the backend
reads it on every route. API clients and the e2e scripts authenticate with
`Authorization: Bearer`; the backend reads no `?token=` query parameter.

A request authenticated by the cookie and using a state-changing method must
also carry an `X-Requested-With` header. A cross-site page can add a custom
header only through a CORS preflight, which the origin allowlist refuses, so
the header check backs up SameSite=Strict on browsers or paths where the
browser does not apply that attribute.

Version: 0.1.0
"""

import pytest

from app.api.auth import SESSION_COOKIE_NAME, CSRF_HEADER
from app.core.config import settings


PASSWORD = "correct-horse-battery"


@pytest.fixture
def admin(db_session):
    from app.models.user import User
    from app.core.security import hash_password

    user = User(username="cookie_admin", email="ca@example.com",
                hashed_password=hash_password(PASSWORD), role="admin", is_active=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _login(client, username="cookie_admin"):
    resp = client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp


def _set_cookie_header(resp):
    headers = [v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie"]
    matching = [h for h in headers if h.startswith(f"{SESSION_COOKIE_NAME}=")]
    assert len(matching) == 1, headers
    return matching[0]


def _attrs(header):
    """Cookie attributes as a lower-cased dict; flag attributes map to True."""
    parts = [p.strip() for p in header.split(";")]
    out = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.strip().lower()] = v.strip()
        else:
            out[p.lower()] = True
    return out


# ---------------------------------------------------------------------------
# Login sets the cookie
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_login_sets_an_httponly_strict_root_cookie_with_the_token_lifetime(client, admin):
    resp = _login(client)
    attrs = _attrs(_set_cookie_header(resp))

    assert attrs.get("httponly") is True
    assert attrs.get("samesite", "").lower() == "strict"
    assert attrs.get("path") == "/"
    assert int(attrs["max-age"]) == settings.ACCESS_TOKEN_EXPIRE_SECONDS


@pytest.mark.unit
def test_login_cookie_is_not_secure_over_plain_http(client, admin):
    # The appliance serves plain HTTP on the LAN; the browser would never send
    # a Secure cookie back, and nobody could stay logged in.
    attrs = _attrs(_set_cookie_header(_login(client)))
    assert "secure" not in attrs


@pytest.mark.unit
def test_login_cookie_is_secure_when_the_proxy_reports_https(client, admin):
    resp = client.post(
        "/auth/login",
        json={"username": "cookie_admin", "password": PASSWORD},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert resp.status_code == 200, resp.text
    assert _attrs(_set_cookie_header(resp)).get("secure") is True


@pytest.mark.unit
def test_login_without_the_browser_header_returns_the_token_for_api_clients(client, admin):
    body = _login(client).json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


@pytest.mark.unit
def test_browser_login_sets_the_cookie_and_keeps_the_token_out_of_the_body(client, admin):
    # The browser app sends X-Requested-With on every request and keeps its
    # session in the cookie, so the backend leaves the token out of the body.
    resp = client.post(
        "/auth/login",
        json={"username": "cookie_admin", "password": PASSWORD},
        headers={CSRF_HEADER: "XMLHttpRequest"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"] is None
    assert resp.json()["token_type"] == "cookie"
    assert _attrs(_set_cookie_header(resp)).get("httponly") is True
    assert client.get("/users/me").status_code == 200


@pytest.mark.unit
def test_failed_login_sets_no_cookie(client, admin):
    resp = client.post("/auth/login", json={"username": "cookie_admin", "password": "wrong"})
    assert resp.status_code == 401
    assert SESSION_COOKIE_NAME not in resp.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# The backend accepts the cookie on every kind of route
# ---------------------------------------------------------------------------

# GET routes the browser reaches through fetch, <img src> or a download link.
# The backend answers 404 for a missing resource only after authentication
# has passed; with no credentials it answers 401 on every one of them.
COOKIE_GET_ROUTES = [
    "/users/me",
    "/records/images/999999/thumbnail",
    "/records/images/999999/file",
    "/records/images/999999/preview",
    "/collections/999999/export/download",
    "/records/",
]


@pytest.mark.unit
@pytest.mark.parametrize("path", COOKIE_GET_ROUTES)
def test_cookie_authenticates_get_routes(client, admin, path):
    _login(client)
    resp = client.get(path)
    assert resp.status_code != 401, (path, resp.status_code, resp.text)


@pytest.mark.unit
@pytest.mark.parametrize("path", COOKIE_GET_ROUTES)
def test_without_credentials_get_routes_answer_401(client, admin, path):
    resp = client.get(path)
    assert resp.status_code == 401, (path, resp.status_code, resp.text)


@pytest.mark.unit
def test_cookie_returns_the_logged_in_user(client, admin):
    _login(client)
    resp = client.get("/users/me")
    assert resp.status_code == 200
    assert resp.json()["username"] == "cookie_admin"
    assert resp.json()["role"] == "admin"


# ---------------------------------------------------------------------------
# The backend ignores ?token= and accepts the bearer header
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("path", COOKIE_GET_ROUTES)
def test_token_query_parameter_does_not_authenticate(client, admin, path):
    token = _login(client).json()["access_token"]
    client.cookies.clear()
    resp = client.get(path, params={"token": token})
    assert resp.status_code == 401, (path, resp.status_code, resp.text)


@pytest.mark.unit
@pytest.mark.parametrize("path", COOKIE_GET_ROUTES)
def test_bearer_header_still_authenticates(client, admin, path):
    token = _login(client).json()["access_token"]
    client.cookies.clear()
    resp = client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code != 401, (path, resp.status_code, resp.text)


@pytest.mark.unit
def test_bearer_state_changing_request_needs_no_csrf_header(client, admin):
    # A browser never attaches an Authorization header on its own, so a
    # bearer request cannot be forged cross-site.
    token = _login(client).json()["access_token"]
    client.cookies.clear()
    resp = client.delete("/records/images/999999", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# CSRF: cookie-authenticated state changes need X-Requested-With
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_cookie_state_changing_request_without_csrf_header_is_refused(client, admin):
    _login(client)
    resp = client.delete("/records/images/999999")
    assert resp.status_code == 403, resp.text
    assert CSRF_HEADER in resp.json()["detail"]


@pytest.mark.unit
def test_cookie_state_changing_request_with_csrf_header_is_accepted(client, admin):
    _login(client)
    resp = client.delete("/records/images/999999", headers={CSRF_HEADER: "captua"})
    assert resp.status_code == 404, resp.text


@pytest.mark.unit
def test_cookie_get_needs_no_csrf_header(client, admin):
    _login(client)
    assert client.get("/users/me").status_code == 200


@pytest.mark.unit
def test_cookie_password_reset_without_csrf_header_is_refused(client, admin):
    _login(client)
    resp = client.post("/auth/password-reset",
                       json={"old_password": PASSWORD, "new_password": "another-long-pass"})
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Refresh: the backend keeps the 8-hour lifetime and, for a cookie session,
# sets a new cookie without giving the token to page scripts
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_cookie_refresh_renews_the_cookie_and_keeps_the_token_out_of_the_body(client, admin):
    _login(client)
    resp = client.post("/auth/refresh", headers={CSRF_HEADER: "captua"})
    assert resp.status_code == 200, resp.text
    attrs = _attrs(_set_cookie_header(resp))
    assert attrs.get("httponly") is True
    assert int(attrs["max-age"]) == settings.ACCESS_TOKEN_EXPIRE_SECONDS
    assert resp.json()["access_token"] is None


@pytest.mark.unit
def test_bearer_refresh_returns_a_new_token(client, admin):
    token = _login(client).json()["access_token"]
    client.cookies.clear()
    resp = client.post("/auth/refresh", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_logout_clears_the_cookie(client, admin):
    _login(client)
    assert client.get("/users/me").status_code == 200

    resp = client.post("/auth/logout", headers={CSRF_HEADER: "XMLHttpRequest"})
    assert resp.status_code == 204
    attrs = _attrs(_set_cookie_header(resp))
    assert int(attrs["max-age"]) == 0
    assert attrs.get("path") == "/"

    assert client.get("/users/me").status_code == 401


@pytest.mark.unit
def test_logout_without_a_session_still_succeeds(client):
    assert client.post("/auth/logout", headers={CSRF_HEADER: "XMLHttpRequest"}).status_code == 204


@pytest.mark.unit
def test_logout_without_the_csrf_header_is_refused_and_keeps_the_cookie(client, admin):
    # A cross-site page must not be able to log the operator out.
    _login(client)
    resp = client.post("/auth/logout")
    assert resp.status_code == 403, resp.text
    assert CSRF_HEADER in resp.json()["detail"]
    assert SESSION_COOKIE_NAME not in resp.headers.get("set-cookie", "")
    assert client.get("/users/me").status_code == 200


# ---------------------------------------------------------------------------
# CORS: a page from the dev server (5173) calls the backend (8000) with credentials
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_cors_preflight_allows_credentials_and_the_csrf_header_for_the_dev_origin(client):
    origin = "http://localhost:5173"
    assert origin in settings.CORS_ORIGINS
    resp = client.options(
        "/records/images/1",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "content-type,x-requested-with",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["access-control-allow-origin"] == origin
    assert resp.headers["access-control-allow-credentials"] == "true"
    allowed = resp.headers["access-control-allow-headers"].lower()
    assert "x-requested-with" in allowed


@pytest.mark.unit
def test_cors_preflight_refuses_an_unlisted_origin(client):
    resp = client.options(
        "/records/images/1",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "x-requested-with",
        },
    )
    assert "access-control-allow-origin" not in resp.headers


# ---------------------------------------------------------------------------
# CORS_ORIGINS must be an explicit list
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_cors_wildcard_is_refused_at_startup():
    from types import SimpleNamespace
    from app.core.config import _guard_cors_origins

    with pytest.raises(RuntimeError):
        _guard_cors_origins(SimpleNamespace(CORS_ORIGINS=["http://localhost:5173", "*"]))
    _guard_cors_origins(SimpleNamespace(CORS_ORIGINS=["http://localhost:5173"]))


# ---------------------------------------------------------------------------
# The backend sets Secure from the request scheme when no proxy header is present
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_login_cookie_is_secure_when_the_request_itself_is_https(db_session, admin):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.deps import get_db_dependency

    app.dependency_overrides[get_db_dependency] = lambda: db_session
    try:
        https_client = TestClient(app, base_url="https://testserver")
        resp = https_client.post("/auth/login", json={"username": "cookie_admin", "password": PASSWORD})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    assert _attrs(_set_cookie_header(resp)).get("secure") is True


@pytest.mark.unit
def test_proxy_header_wins_over_the_request_scheme(client, admin):
    # nginx terminates the connection and reports the scheme it saw.
    resp = client.post(
        "/auth/login",
        json={"username": "cookie_admin", "password": PASSWORD},
        headers={"X-Forwarded-Proto": "http"},
    )
    assert "secure" not in _attrs(_set_cookie_header(resp))


# ---------------------------------------------------------------------------
# The backend prefers the Authorization header over the cookie
# ---------------------------------------------------------------------------

@pytest.fixture
def reviewer(db_session):
    from app.models.user import User
    from app.core.security import hash_password

    user = User(username="cookie_reviewer", email="cr@example.com",
                hashed_password=hash_password(PASSWORD), role="reviewer", is_active=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.mark.unit
def test_bearer_header_wins_over_the_cookie(client, admin, reviewer):
    from app.core.security import create_access_token

    _login(client)  # the client now holds the admin's cookie
    reviewer_token = create_access_token(subject=str(reviewer.id))
    resp = client.get("/users/me", headers={"Authorization": f"Bearer {reviewer_token}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] == "cookie_reviewer"


@pytest.mark.unit
def test_an_invalid_bearer_header_does_not_fall_back_to_the_cookie(client, admin):
    _login(client)
    resp = client.get("/users/me", headers={"Authorization": "Bearer not-a-token"})
    assert resp.status_code == 401, resp.text


@pytest.mark.unit
def test_bearer_post_with_a_cookie_present_needs_no_csrf_header(client, admin):
    token = _login(client).json()["access_token"]
    resp = client.delete("/records/images/999999", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# A real image fetched with the cookie, as the browser does for <img src>
# ---------------------------------------------------------------------------

JPEG_BYTES = b"\xff\xd8cookie-session-jpeg"


@pytest.fixture
def jpeg_image(db_session, override_projects_root):
    from app.models.record import Record, RecordImage

    d = override_projects_root / "cookie_project" / "images" / "main"
    d.mkdir(parents=True, exist_ok=True)
    master = d / "IMG_0001.jpg"
    master.write_bytes(JPEG_BYTES)
    rec = Record(title="r", status="in_review", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    img = RecordImage(record_id=rec.id, filename=master.name, file_path=str(master), format="jpg")
    db_session.add(img)
    db_session.commit()
    db_session.refresh(img)
    return img


@pytest.mark.unit
@pytest.mark.parametrize("suffix", ["file", "preview"])
def test_cookie_get_serves_an_image_file(client, admin, jpeg_image, suffix):
    _login(client)
    resp = client.get(f"/records/images/{jpeg_image.id}/{suffix}")
    assert resp.status_code == 200, resp.text
    assert resp.content == JPEG_BYTES


@pytest.mark.unit
@pytest.mark.parametrize("suffix", ["file", "preview"])
def test_token_query_parameter_does_not_serve_an_image_file(client, admin, jpeg_image, suffix):
    token = _login(client).json()["access_token"]
    client.cookies.clear()
    resp = client.get(f"/records/images/{jpeg_image.id}/{suffix}", params={"token": token})
    assert resp.status_code == 401, resp.text


@pytest.mark.unit
def test_token_query_parameter_does_not_authenticate_a_json_route(client, admin):
    token = _login(client).json()["access_token"]
    client.cookies.clear()
    resp = client.get("/records/", params={"token": token})
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Cookie POSTs and the CSRF header
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_cookie_post_without_csrf_header_is_refused(client, admin):
    _login(client)
    resp = client.post("/auth/refresh")
    assert resp.status_code == 403, resp.text
    assert CSRF_HEADER in resp.json()["detail"]


@pytest.mark.unit
def test_cookie_register_with_csrf_header_creates_a_user(client, admin):
    _login(client)
    payload = {"username": "made_by_cookie", "password": "another-long-pass"}
    assert client.post("/auth/register", json=payload).status_code == 403
    resp = client.post("/auth/register", json=payload, headers={CSRF_HEADER: "XMLHttpRequest"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] == "made_by_cookie"


@pytest.mark.unit
def test_cookie_password_reset_with_csrf_header_succeeds(client, admin):
    _login(client)
    resp = client.post("/auth/password-reset",
                       json={"old_password": PASSWORD, "new_password": "another-long-pass"},
                       headers={CSRF_HEADER: "XMLHttpRequest"})
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# The backend refuses a deactivated user's cookie on the next request
# ---------------------------------------------------------------------------

def _deactivate(db_session, user):
    user.is_active = False
    db_session.add(user)
    db_session.commit()


@pytest.mark.unit
def test_deactivated_user_with_a_valid_cookie_gets_401(client, db_session, admin):
    _login(client)
    assert client.get("/users/me").status_code == 200
    _deactivate(db_session, admin)
    assert client.get("/users/me").status_code == 401


@pytest.mark.unit
def test_deactivated_user_cannot_refresh_a_cookie_session(client, db_session, admin):
    _login(client)
    _deactivate(db_session, admin)
    resp = client.post("/auth/refresh", headers={CSRF_HEADER: "XMLHttpRequest"})
    assert resp.status_code == 401, resp.text


@pytest.mark.unit
def test_deactivated_user_cannot_change_the_password_with_a_cookie(client, db_session, admin):
    _login(client)
    _deactivate(db_session, admin)
    resp = client.post("/auth/password-reset",
                       json={"old_password": PASSWORD, "new_password": "another-long-pass"},
                       headers={CSRF_HEADER: "XMLHttpRequest"})
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# A browser login: the backend sets the cookie with every attribute
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_browser_login_cookie_carries_the_token_and_every_attribute(client, admin):
    from app.core.security import verify_access_token

    resp = client.post("/auth/login", json={"username": "cookie_admin", "password": PASSWORD},
                       headers={CSRF_HEADER: "XMLHttpRequest"})
    header = _set_cookie_header(resp)
    value = header.split(";", 1)[0].split("=", 1)[1]
    assert verify_access_token(value)["sub"] == str(admin.id)
    attrs = _attrs(header)
    assert attrs.get("httponly") is True
    assert attrs.get("samesite", "").lower() == "strict"
    assert attrs.get("path") == "/"
    assert int(attrs["max-age"]) == settings.ACCESS_TOKEN_EXPIRE_SECONDS
    assert "secure" not in attrs
