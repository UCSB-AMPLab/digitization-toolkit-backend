"""The /network routes (NEH-253): availability, roles, refusals, the join's
state and forgetting a saved network.

The command layer is tests.unit.test_network_core's FakeNM, and the join's
thread body runs synchronously in the request.

    python -m pytest tests/unit/test_network_api.py

Version: 0.3.3
"""

import pytest

from tests.unit.test_network_core import (  # noqa: F401  (nm is a fixture)
    ANON, EDUROAM_UUID, IDENTITY, NEW_UUID, OLD_UUID, PASSWORD, UP_NEW, JOURNAL, PERMISSIONS,
    WIFI_LIST_CACHED, WIFI_LIST_RESCAN, WIFI_LIST_SCAN, _join_nm, _permissions_with, fixture, journal_for, nm,
)


def _user(username, role):
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _as(client, role):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _user(role, role)
    return client


@pytest.fixture
def api(client, nm, monkeypatch):
    """The client, nmcli present, audit calls recorded, the join thread run inline."""
    from app.api import network as routes
    from app.core import network
    monkeypatch.setattr(network.shutil, "which", lambda name: f"/usr/bin/{name}")
    audits = []
    monkeypatch.setattr(routes, "log_event", lambda db, **kw: audits.append(kw))
    started = []

    def inline(target, kwargs):
        started.append(kwargs)
        if not getattr(api_flags, "hold", False):
            target(**kwargs)
    monkeypatch.setattr(routes, "_start", inline)
    client.audits = audits
    client.started = started
    return client


class _Flags:
    hold = False


api_flags = _Flags()


@pytest.fixture(autouse=True)
def _reset_flags():
    api_flags.hold = False
    yield
    api_flags.hold = False


ENTERPRISE = {"ssid": "eduroam", "security": "enterprise", "identity": IDENTITY, "password": PASSWORD,
              "method": "peap", "anonymousIdentity": ANON, "serverName": "radius.example.edu"}

ENDPOINTS = [
    ("get", "/network/status", None),
    ("get", "/network/wifi", None),
    ("get", "/network/wifi/join", None),
    ("post", "/network/wifi/join", {"ssid": "Library Guest", "security": "open"}),
    ("delete", f"/network/wifi/saved/{EDUROAM_UUID}", None),
]


def _call(client, method, path, body):
    if body is None:
        return getattr(client, method)(path)
    return getattr(client, method)(path, json=body)


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("method, path, body", ENDPOINTS)
def test_every_endpoint_answers_501_without_nmcli(api, monkeypatch, method, path, body):
    from app.core import network
    monkeypatch.setattr(network.shutil, "which", lambda name: None)
    resp = _call(_as(api, "admin"), method, path, body)
    assert resp.status_code == 501
    assert resp.json()["detail"] == "Network settings are not available on this device."


@pytest.mark.unit
@pytest.mark.parametrize("path", ["/network/status", "/network/wifi"])
@pytest.mark.parametrize("failing", ["nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status", PERMISSIONS])
def test_a_failed_read_answers_503(api, nm, path, failing):
    nm.on("nmcli -t -f GENERAL.HWADDR", stdout="")
    nm.on(failing, rc=8, stderr="Error: NetworkManager is not running.")
    resp = _as(api, "reviewer").get(path)
    assert resp.status_code == 503
    assert resp.json()["detail"]["problems"] == [{"code": "network_manager_unavailable"}]
    assert resp.json()["detail"]["message"]


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

ROLE_CASES = [
    ("get", "/network/status", None, {"admin", "operator", "reviewer"}),
    ("get", "/network/wifi", None, {"admin", "operator", "reviewer"}),
    ("get", "/network/wifi/join", None, {"admin", "operator", "reviewer"}),
    ("post", "/network/wifi/join", {"ssid": "Library Guest", "security": "open"}, {"admin", "operator"}),
    ("delete", f"/network/wifi/saved/{EDUROAM_UUID}", None, {"admin", "operator"}),
]


@pytest.mark.unit
@pytest.mark.parametrize("method, path, body, allowed", ROLE_CASES)
@pytest.mark.parametrize("role", ["admin", "operator", "reviewer"])
def test_roles(api, nm, method, path, body, allowed, role):
    from app.core import network
    _join_nm(nm)
    nm.on("nmcli -t -f GENERAL.HWADDR", stdout="")
    network._reset_join_state()
    resp = _call(_as(api, role), method, path, body)
    if role in allowed:
        assert resp.status_code in (200, 202, 204), resp.text
    else:
        assert resp.status_code == 403


@pytest.mark.unit
@pytest.mark.parametrize("method, path, body", ENDPOINTS)
def test_unauthenticated_is_401(api, method, path, body):
    assert _call(api, method, path, body).status_code == 401


# ---------------------------------------------------------------------------
# Status and scan
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_status_and_scan_bodies(api, nm):
    nm.on("nmcli -t -f GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,IP6.ADDRESS,IP6.DNS device show eth0", stdout="")
    status = _as(api, "reviewer").get("/network/status").json()
    assert set(status) == {"hostname", "wifiDevice", "permissions", "interfaces", "problems"}
    assert status["permissions"] == {"scan": True, "control": True, "modify": True}
    assert [i["device"] for i in status["interfaces"]] == ["wlan0", "eth0"]
    wifi = api.get("/network/wifi").json()
    assert wifi["networks"][0]["ssid"] == "Captua Lab"
    assert {s["uuid"] for s in wifi["saved"]} == {OLD_UUID, EDUROAM_UUID}


@pytest.mark.unit
def test_scan_without_wifi_device_is_409(api, nm):
    nm.on("nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status", stdout="eth0:ethernet:connected:x\n")
    resp = _as(api, "reviewer").get("/network/wifi")
    assert resp.status_code == 409
    assert resp.json()["detail"]["problems"][0]["code"] == "no_wifi_device"


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("body, code", [
    ({"ssid": "", "security": "open"}, "ssid_invalid"),
    ({"ssid": "x" * 33, "security": "open"}, "ssid_invalid"),
    ({"ssid": "Home", "security": "password"}, "password_required"),
    ({"ssid": "Home", "security": "password", "password": "short"}, "password_length"),
    ({**ENTERPRISE, "identity": ""}, "identity_required"),
    ({**ENTERPRISE, "serverName": None}, "server_name_required"),
    ({**ENTERPRISE, "serverName": "radius"}, "server_name_invalid"),
    ({**ENTERPRISE, "innerMethod": "pap"}, "inner_method_invalid"),
    ({**ENTERPRISE, "method": None}, "method_required"),
    ({k: v for k, v in ENTERPRISE.items() if k != "method"}, "method_required"),
    ({"ssid": "Home\nNet", "security": "open"}, "ssid_invalid"),
    ({**ENTERPRISE, "anonymousIdentity": "anon\r@example.edu"}, "anonymous_identity_invalid"),
    ({**ENTERPRISE, "identity": "a\nb"}, "identity_invalid"),
    ({"ssid": "Home", "security": "password", "password": "\tTab-lead-pass"}, "password_invalid"),
])
def test_join_refusals(api, nm, body, code):
    resp = _as(api, "admin").post("/network/wifi/join", json=body)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert code in [p["code"] for p in detail["problems"]]
    assert all(p["message"] for p in detail["problems"]) and detail["message"]
    assert not nm.ran("nmcli connection add") and not api.started and not api.audits


@pytest.mark.unit
def test_join_without_wifi_device_is_409(api, nm):
    nm.on("nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status", stdout="eth0:ethernet:connected:x\n")
    resp = _as(api, "admin").post("/network/wifi/join", json={"ssid": "Home", "security": "open"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["problems"][0]["code"] == "no_wifi_device"


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    {"ssid": "Home", "security": "open", "bssid": "x"},
    {"ssid": "Home", "security": "wep"},
    {**ENTERPRISE, "method": "tls"},
    {**ENTERPRISE, "innerMethod": "gtc"},
    {"ssid": 5, "security": "open"},
])
def test_join_shape_errors_are_422(api, body):
    assert _as(api, "admin").post("/network/wifi/join", json=body).status_code == 422


@pytest.mark.unit
def test_join_answers_202_then_joined(api, nm):
    from app.core import network
    _join_nm(nm)
    api_flags.hold = True
    resp = _as(api, "operator").post("/network/wifi/join", json=ENTERPRISE)
    assert resp.status_code == 202
    assert resp.json()["state"] == "joining" and resp.json()["ssid"] == "eduroam"
    assert api.get("/network/wifi/join").json()["state"] == "joining"
    assert api.audits == [dict(level="INFO", category="system", action="network_join", actor="operator",
                               subject="eduroam", detail="security=enterprise")]
    network.run_join(**api.started[0])
    state = api.get("/network/wifi/join").json()
    assert state["state"] == "joined" and state["problem"] is None and state["finishedAt"]
    for text in (resp.text, repr(api.audits), repr(api.started[0].get("ssid"))):
        assert PASSWORD not in text and IDENTITY not in text


@pytest.mark.unit
def test_join_failure_state(api, nm):
    _join_nm(nm, up_rc=4, up_waits=0)
    nm.on(JOURNAL, stdout=journal_for("journal-server-name-mismatch.txt", ssid="eduroam"))
    resp = _as(api, "admin").post("/network/wifi/join", json={**ENTERPRISE, "serverName": "other.example.org"})
    assert resp.status_code == 202
    state = api.get("/network/wifi/join").json()
    assert state["state"] == "failed"
    assert state["problem"]["code"] == "server_name_mismatch"
    assert state["problem"]["server"]["subject"] == "/CN=radius.test.example.edu"
    assert state["restored"] is True
    for text in (resp.text, repr(state)):
        assert PASSWORD not in text and IDENTITY not in text and ANON not in text


@pytest.mark.unit
def test_a_second_join_during_a_join_is_409(api, nm):
    _join_nm(nm)
    api_flags.hold = True
    assert _as(api, "admin").post("/network/wifi/join", json=ENTERPRISE).status_code == 202
    resp = api.post("/network/wifi/join", json={"ssid": "Library Guest", "security": "open"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["problems"][0]["code"] == "join_in_progress"
    assert len(api.started) == 1


@pytest.mark.unit
def test_join_state_starts_idle(api):
    assert _as(api, "reviewer").get("/network/wifi/join").json() == {
        "state": "idle", "ssid": None, "startedAt": None, "finishedAt": None, "problem": None, "restored": None,
    }


# ---------------------------------------------------------------------------
# Forget
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_forget(api, nm):
    nm.on("nmcli connection delete uuid", rc=0)
    admin = _as(api, "admin")
    assert admin.delete("/network/wifi/saved/not-a-uuid").status_code == 422
    assert admin.delete("/network/wifi/saved/5f0e7a1c-3b2d-4e5f-8a9b-0c1d2e3f4a5b").status_code == 404
    resp = admin.delete(f"/network/wifi/saved/{OLD_UUID}")
    assert resp.status_code == 409
    assert resp.json()["detail"]["problems"][0]["code"] == "connection_active"
    assert not nm.ran("nmcli connection delete")
    assert admin.delete(f"/network/wifi/saved/{OLD_UUID}?force=true").status_code == 204
    assert api.audits[-1] == dict(level="INFO", category="system", action="network_forget", actor="admin",
                                  subject="Captua Lab")
    assert [c[-1] for c in nm.ran("nmcli connection delete")] == [OLD_UUID]


# ---------------------------------------------------------------------------
# Rescan, permissions and polkit refusals
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_wifi_rescan_true_asks_for_a_fresh_scan(api, nm):
    nm.on(WIFI_LIST_RESCAN, stdout=fixture("nmcli-wifi-list.txt"))
    reviewer = _as(api, "reviewer")
    assert reviewer.get("/network/wifi").status_code == 200
    assert nm.ran(WIFI_LIST_SCAN) and not nm.ran(WIFI_LIST_RESCAN)
    resp = reviewer.get("/network/wifi?rescan=true")
    assert resp.status_code == 200 and resp.json()["rescanned"] is True
    assert len(nm.ran(WIFI_LIST_RESCAN)) == 1
    assert reviewer.get("/network/wifi?rescan=maybe").status_code == 422


@pytest.mark.unit
@pytest.mark.parametrize("query", ["", "?rescan=true"])
def test_scan_without_permission_answers_the_cached_list(api, nm, query):
    nm.on(PERMISSIONS, stdout=_permissions_with(scan="no"))
    resp = _as(api, "reviewer").get(f"/network/wifi{query}")
    assert resp.status_code == 200
    assert resp.json()["rescanned"] is False and resp.json()["networks"]
    assert nm.ran(WIFI_LIST_CACHED) and not nm.ran(WIFI_LIST_SCAN) and not nm.ran(WIFI_LIST_RESCAN)


@pytest.mark.unit
@pytest.mark.parametrize("values", [{"control": "no"}, {"modify": "auth"}])
def test_join_without_permission_is_409_before_anything_runs(api, nm, values):
    nm.on(PERMISSIONS, stdout=_permissions_with(**values))
    resp = _as(api, "admin").post("/network/wifi/join", json=ENTERPRISE)
    assert resp.status_code == 409
    assert resp.json()["detail"]["problems"][0]["code"] == "not_permitted"
    assert not nm.ran("nmcli connection add") and not api.started and not api.audits
    assert api.get("/network/wifi/join").json()["state"] == "idle"


@pytest.mark.unit
def test_join_refuses_a_password_with_a_line_break(api, nm):
    resp = _as(api, "admin").post("/network/wifi/join", json={**ENTERPRISE, "password": "a\n802-1x.identity:x"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["problems"][0]["code"] == "password_invalid"
    assert not nm.ran("nmcli connection add") and not api.started


@pytest.mark.unit
def test_forget_refused_by_polkit_is_409(api, nm):
    nm.on("nmcli connection delete uuid", rc=7, stderr=fixture("nmcli-delete-denied.txt"))
    resp = _as(api, "admin").delete(f"/network/wifi/saved/{EDUROAM_UUID}")
    assert resp.status_code == 409
    assert resp.json()["detail"]["problems"][0]["code"] == "not_permitted"
    assert not api.audits


@pytest.mark.unit
def test_forget_other_failure_is_503(api, nm):
    nm.on("nmcli connection delete uuid", rc=10, stderr="Error: unknown connection")
    resp = _as(api, "admin").delete(f"/network/wifi/saved/{EDUROAM_UUID}")
    assert resp.status_code == 503
    assert resp.json()["detail"]["problems"] == [{"code": "network_manager_unavailable"}]



# ---------------------------------------------------------------------------
# Validation errors carry nothing from the request
# ---------------------------------------------------------------------------

LEAKY_BODIES = [
    {k: v for k, v in ENTERPRISE.items() if k != "method"} | {"method": "tls"},
    {**ENTERPRISE, "unknownField": PASSWORD},
    {**ENTERPRISE, "password": [PASSWORD]},
    {**ENTERPRISE, "password": {"value": PASSWORD}},
    {**ENTERPRISE, "security": PASSWORD},
    {**ENTERPRISE, "method": IDENTITY},
    {**ENTERPRISE, "identity": 12345, "password": 678},
]


@pytest.mark.unit
@pytest.mark.parametrize("body", LEAKY_BODIES)
def test_join_422_carries_no_submitted_value(api, nm, caplog, body):
    import logging
    caplog.set_level(logging.DEBUG)
    resp = _as(api, "admin").post("/network/wifi/join", json=body)
    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert errors and all(set(e) == {"loc", "type", "msg"} for e in errors)
    for text in (resp.text, caplog.text):
        assert PASSWORD not in text and IDENTITY not in text and ANON not in text
        assert "radius.example.edu" not in text
    assert not nm.ran("nmcli connection add") and not api.started and not api.audits


@pytest.mark.unit
def test_join_422_for_a_body_that_is_not_json(api):
    resp = _as(api, "admin").post("/network/wifi/join", content=f'{{"password": "{PASSWORD}"',
                                  headers={"Content-Type": "application/json"})
    assert resp.status_code == 422
    assert PASSWORD not in resp.text
    assert all(set(e) == {"loc", "type", "msg"} for e in resp.json()["detail"])


@pytest.mark.unit
def test_enterprise_without_method_is_a_409_that_carries_no_secret(api, nm, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    body = {k: v for k, v in ENTERPRISE.items() if k != "method"}
    resp = _as(api, "admin").post("/network/wifi/join", json=body)
    assert resp.status_code == 409
    assert [p["code"] for p in resp.json()["detail"]["problems"]] == ["method_required"]
    for text in (resp.text, caplog.text):
        assert PASSWORD not in text and IDENTITY not in text


@pytest.mark.unit
def test_query_and_path_422s_carry_no_submitted_value(api):
    admin = _as(api, "admin")
    resp = admin.delete("/network/wifi/saved/s3cret-not-a-uuid")
    assert resp.status_code == 422 and "s3cret-not-a-uuid" not in resp.text
    resp = admin.get("/network/wifi?rescan=s3cret-flag")
    assert resp.status_code == 422 and "s3cret-flag" not in resp.text


@pytest.mark.unit
def test_other_routers_keep_the_default_422(client):
    """The sanitising is scoped to /network: elsewhere FastAPI's body stands."""
    resp = client.post("/auth/login", json={"username": 5})
    assert resp.status_code == 422
    assert any("input" in e for e in resp.json()["detail"])


# ---------------------------------------------------------------------------
# CSRF: a cookie session's state change needs X-Requested-With
# (as tests/unit/test_session_cookie.py exercises it)
# ---------------------------------------------------------------------------

@pytest.fixture
def cookie_admin(api, db_session):
    from app.core.security import hash_password
    from app.models.user import User
    user = User(username="net_cookie_admin", email="nca@example.com",
                hashed_password=hash_password("correct-horse-battery"), role="admin", is_active=True)
    db_session.add(user)
    db_session.commit()
    resp = api.post("/auth/login", json={"username": "net_cookie_admin", "password": "correct-horse-battery"})
    assert resp.status_code == 200, resp.text
    return api


@pytest.mark.unit
def test_cookie_join_without_csrf_header_is_refused(cookie_admin, nm):
    from app.api.auth import CSRF_HEADER
    _join_nm(nm)
    resp = cookie_admin.post("/network/wifi/join", json={"ssid": "Library Guest", "security": "open"})
    assert resp.status_code == 403 and CSRF_HEADER in resp.json()["detail"]
    assert not cookie_admin.started and not nm.ran("nmcli connection add")
    resp = cookie_admin.post("/network/wifi/join", json={"ssid": "Library Guest", "security": "open"},
                             headers={CSRF_HEADER: "captua"})
    assert resp.status_code == 202, resp.text


@pytest.mark.unit
def test_cookie_forget_without_csrf_header_is_refused(cookie_admin, nm):
    from app.api.auth import CSRF_HEADER
    nm.on("nmcli connection delete uuid", rc=0)
    resp = cookie_admin.delete(f"/network/wifi/saved/{EDUROAM_UUID}")
    assert resp.status_code == 403 and CSRF_HEADER in resp.json()["detail"]
    assert not nm.ran("nmcli connection delete")
    resp = cookie_admin.delete(f"/network/wifi/saved/{EDUROAM_UUID}", headers={CSRF_HEADER: "captua"})
    assert resp.status_code == 204, resp.text


@pytest.mark.unit
def test_undecodable_output_answers_503(api, nm):
    nm.on("nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status", undecodable=True)
    resp = _as(api, "reviewer").get("/network/status")
    assert resp.status_code == 503
    assert resp.json()["detail"]["problems"] == [{"code": "network_manager_unavailable"}]



@pytest.mark.unit
def test_unknown_field_name_is_not_echoed(api, nm):
    resp = _as(api, "admin").post("/network/wifi/join",
                                  json={"ssid": "Home", "security": "open", "s3cret-field-name": 1})
    assert resp.status_code == 422
    assert "s3cret-field-name" not in resp.text
    assert {"loc": ["body"], "type": "extra_forbidden", "msg": "Extra inputs are not permitted"} in resp.json()["detail"]
