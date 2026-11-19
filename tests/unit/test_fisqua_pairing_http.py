"""The pairing client (contract 1.11 §12), against a fake Fisqua (httpx.MockTransport).

Each answer of the codes and collect routes is read as the contract has
it; a 404 that is not JSON is `cannot_pair` on the codes route only; any
other answer that is not a JSON object, and any connection error, is
network trouble; redirects are not followed; the collect secret, the code
and the payload reach no log, repr or error text.

    python -m pytest tests/unit/test_fisqua_pairing_http.py

Version: 0.3.3
"""

import json
import logging

import httpx
import pytest
from pydantic import SecretStr

BASE = "https://fisqua.test"
SECRET = "5ec2e7" + "ab" * 29
CODE = "7KQM2X"
TOKEN = "70ce" + "0f" * 30
WRITE_APP_KEY = "K005WriteAppKeySecretValue"
READ_APP_KEY = "K005ReadAppKeySecretValue"


def payload(**changes):
    body = {"tenant": "ahr", "environment": "production", "token": TOKEN, "bucket": "captua-masters",
            "writeKey": {"keyId": "005writekeyid", "applicationKey": WRITE_APP_KEY},
            "readKey": {"keyId": "005readkeyid", "applicationKey": READ_APP_KEY},
            "ageRecipient": "age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq"}
    body.update(changes)
    return body


class Server:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        answer = self.answers.pop(0) if self.answers else httpx.Response(500)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _client(server):
    from app.core.fisqua_pairing_http import FisquaPairingClient

    return FisquaPairingClient(BASE, client=httpx.Client(transport=httpx.MockTransport(server)))


def _code_201(**changes):
    body = {"code": CODE, "collectSecret": SECRET, "expiresInSeconds": 120, "pollSeconds": 5}
    body.update(changes)
    return httpx.Response(201, json=body)


# ---------------------------------------------------------------------------
# The codes route
# ---------------------------------------------------------------------------

def test_codes_201_parsed():
    server = Server(_code_201())
    answer = _client(server).request_code("Sala 1", "abcdefgh", None)
    assert answer.outcome == "code"
    assert (answer.code, answer.collect_secret, answer.expires_in_seconds, answer.poll_seconds) == (
        CODE, SECRET, 120, 5)
    request = server.requests[0]
    assert request.method == "POST"
    assert str(request.url) == BASE + "/api/capture/v1/pairing/codes"
    assert json.loads(request.content) == {"unitName": "Sala 1", "deviceId": "abcdefgh"}
    assert "authorization" not in request.headers


def test_codes_send_current_tenant_when_given():
    server = Server(_code_201())
    _client(server).request_code("Sala 1", "abcdefgh", "ahr")
    assert json.loads(server.requests[0].content) == {"unitName": "Sala 1", "deviceId": "abcdefgh",
                                                      "currentTenant": "ahr"}


@pytest.mark.parametrize("changes", [{"code": ""}, {"collectSecret": "XYZ"}, {"collectSecret": None},
                                     {"collectSecret": SECRET + "\n"},
                                     {"pollSeconds": "5"}, {"expiresInSeconds": None}])
def test_codes_201_malformed_is_network_trouble(changes):
    answer = _client(Server(_code_201(**changes))).request_code("Sala 1", "abcdefgh", None)
    assert answer.outcome == "network"
    assert answer.collect_secret is None


def test_non_json_404_is_cannot_pair():
    server = Server(httpx.Response(404, text="<html>Not Found</html>", headers={"Content-Type": "text/html"}))
    assert _client(server).request_code("Sala 1", "abcdefgh", None).outcome == "cannot_pair"


def test_json_array_404_is_cannot_pair():
    """The contract's test is a body that is not a JSON object."""
    server = Server(httpx.Response(404, json=["not", "an", "object"]))
    assert _client(server).request_code("Sala 1", "abcdefgh", None).outcome == "cannot_pair"


def test_json_404_on_codes_is_unexpected():
    server = Server(httpx.Response(404, json={"state": "unknown"}))
    assert _client(server).request_code("Sala 1", "abcdefgh", None).outcome == "unexpected"


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="<html>Sign in to the Wi-Fi</html>"),
    httpx.Response(302, headers={"Location": "http://portal.example/login"}),
    httpx.Response(502, text="Bad gateway"),
    httpx.Response(503, text="Service Unavailable"),
    httpx.Response(201, json=["a"]),
])
def test_other_non_json_is_network(response):
    assert _client(Server(response)).request_code("Sala 1", "abcdefgh", None).outcome == "network"


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"),
                                 httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]")])
def test_connection_and_tls_errors_are_network(exc):
    assert _client(Server(exc)).request_code("Sala 1", "abcdefgh", None).outcome == "network"


@pytest.mark.parametrize("status,rule", [(429, "rate-limited"), (503, "busy")])
def test_503_busy_and_429_carry_retry_after(status, rule):
    response = httpx.Response(status, json={"rule": rule}, headers={"Retry-After": "17"})
    for call in ("codes", "collect"):
        client = _client(Server(response))
        answer = (client.request_code("Sala 1", "abcdefgh", None) if call == "codes"
                  else client.collect(CODE, SECRET))
        assert answer.outcome == "busy"
        assert answer.retry_after == 17.0


def test_busy_without_retry_after():
    answer = _client(Server(httpx.Response(503, json={"rule": "busy"}))).collect(CODE, SECRET)
    assert (answer.outcome, answer.retry_after) == ("busy", None)


def test_405():
    for call in ("codes", "collect"):
        client = _client(Server(httpx.Response(405, json={"rule": "method"})))
        answer = (client.request_code("Sala 1", "abcdefgh", None) if call == "codes"
                  else client.collect(CODE, SECRET))
        assert answer.outcome == "method"


@pytest.mark.parametrize("rule", ["unit-name", "device-id", "current-tenant"])
def test_codes_400_names_the_rule(rule):
    answer = _client(Server(httpx.Response(400, json={"rule": rule}))).request_code("x", "abcdefgh", None)
    assert (answer.outcome, answer.rule) == ("refused", rule)


def test_json_5xx_is_network_and_other_json_4xx_unexpected():
    assert _client(Server(httpx.Response(500, json={"error": "x"}))).collect(CODE, SECRET).outcome == "network"
    assert _client(Server(httpx.Response(418, json={"rule": "teapot"}))).collect(CODE, SECRET).outcome == "unexpected"


# ---------------------------------------------------------------------------
# The collect route
# ---------------------------------------------------------------------------

def test_collect_each_state():
    server = Server(
        httpx.Response(202, json={"state": "waiting"}),
        httpx.Response(200, json=payload()),
        httpx.Response(410, json={"state": "expired"}),
        httpx.Response(410, json={"state": "collected"}),
        httpx.Response(404, json={"state": "unknown"}),
        httpx.Response(404, text="Not Found"),
        httpx.Response(410, json={"state": "other"}),
    )
    client = _client(server)
    outcomes = [client.collect(CODE, SECRET) for _ in range(7)]
    assert [a.outcome for a in outcomes] == ["waiting", "payload", "expired", "collected", "unknown",
                                             "network", "unexpected"]
    assert outcomes[1].payload == payload()
    request = server.requests[0]
    assert request.method == "POST"
    assert str(request.url) == BASE + "/api/capture/v1/pairing/collect"
    assert json.loads(request.content) == {"code": CODE, "collectSecret": SECRET}
    assert "authorization" not in request.headers


def test_no_redirects_followed():
    server = Server(httpx.Response(307, headers={"Location": "https://elsewhere.test/api/capture/v1/pairing/collect"}),
                    httpx.Response(200, json=payload()))
    answer = _client(server).collect(CODE, SECRET)
    assert answer.outcome == "network"
    assert len(server.requests) == 1


def test_default_client_verifies_tls_and_follows_no_redirects():
    from app.core.fisqua_pairing_http import FisquaPairingClient

    client = FisquaPairingClient(BASE)
    try:
        assert client._client.follow_redirects is False
        # httpx keeps the verifying SSL context on the transport's pool.
        pool = client._client._transport._pool
        import ssl
        assert pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
    finally:
        client.close()


def test_secret_never_in_logs(caplog):
    caplog.set_level(logging.DEBUG)
    server = Server(_code_201(), httpx.Response(202, json={"state": "waiting"}),
                    httpx.ConnectError(f"boom {SECRET}"), httpx.Response(200, json=payload()),
                    httpx.Response(418, json={"code": CODE, "collectSecret": SECRET}))
    client = _client(server)
    answers = [client.request_code("Sala 1", "abcdefgh", None)]
    answers += [client.collect(CODE, SECRET) for _ in range(4)]
    text = caplog.text + " ".join(repr(a) + str(a) + str(a.error) for a in answers) + repr(client)
    for secret in (SECRET, CODE, TOKEN, WRITE_APP_KEY, READ_APP_KEY):
        assert secret not in text


# ---------------------------------------------------------------------------
# Which host
# ---------------------------------------------------------------------------

@pytest.fixture
def settings(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "FISQUA_ENV", "")
    monkeypatch.setattr(config.settings, "FISQUA_TOKEN", SecretStr(""))
    monkeypatch.setattr(config.settings, "FISQUA_PAIRING_BASE_URL", "")
    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", "")
    return config.settings


def test_never_linked_unit_asks_production(settings):
    from app.core.fisqua_pairing_http import pairing_target

    target = pairing_target()
    assert (target.base_url, target.environment, target.current_tenant) == (
        "https://fisqua.org", "production", None)


def test_never_linked_unit_asks_staging_when_the_setting_says_so(settings, monkeypatch):
    from app.core.fisqua_pairing_http import pairing_target

    monkeypatch.setattr(settings, "FISQUA_ENV", "Staging")
    target = pairing_target()
    assert (target.base_url, target.environment, target.current_tenant) == (
        "https://staging.fisqua.org", "staging", None)


def test_current_tenant_sent_when_linked(settings, monkeypatch):
    from app.core.fisqua_pairing_http import pairing_target

    monkeypatch.setattr(settings, "FISQUA_ENV", "staging")
    monkeypatch.setattr(settings, "FISQUA_TOKEN", SecretStr("ab" * 32))
    monkeypatch.setattr(settings, "OFFSITE_TENANT", "ahr")
    target = pairing_target()
    assert (target.base_url, target.environment, target.current_tenant) == (
        "https://staging.fisqua.org", "staging", "ahr")


def test_tenant_without_a_link_is_not_sent(settings, monkeypatch):
    """A unit with only an offsite tenant (backup mode, no token) is not linked to a workspace."""
    from app.core.fisqua_pairing_http import pairing_target

    monkeypatch.setattr(settings, "OFFSITE_TENANT", "ahr")
    assert pairing_target().current_tenant is None


@pytest.mark.parametrize("value,usable", [("https://pairing.test/", "https://pairing.test"),
                                          ("http://127.0.0.1:8787", "http://127.0.0.1:8787"),
                                          ("http://localhost:8787", "http://localhost:8787"),
                                          ("http://fisqua.example", None),
                                          ("ftp://fisqua.example", None)])
def test_override_under_the_https_or_loopback_rule(settings, monkeypatch, value, usable):
    from app.core.fisqua_pairing_http import pairing_target

    monkeypatch.setattr(settings, "FISQUA_PAIRING_BASE_URL", value)
    target = pairing_target()
    assert target.base_url == (usable or "https://fisqua.org")
    assert target.environment == "production"


# ---------------------------------------------------------------------------
# The payload's shape
# ---------------------------------------------------------------------------

def test_valid_payload_passes():
    from app.core.fisqua_pairing_http import validate_payload

    assert validate_payload(payload()) is None


@pytest.mark.parametrize("changes,field", [
    ({"tenant": None}, "tenant"),
    ({"bucket": 5}, "bucket"),
    ({"token": "AB" * 32}, "token"),
    ({"token": "ab" * 31}, "token"),
    ({"ageRecipient": "age1 x"}, "ageRecipient"),
    ({"tenant": "ahr\nFISQUA_TOKEN=00"}, "tenant"),
    ({"writeKey": {"keyId": "005", "applicationKey": "bad key"}}, "writeKey.applicationKey"),
    ({"readKey": {"keyId": "005"}}, "readKey.applicationKey"),
    ({"readKey": "005:key"}, "readKey"),
    ({"environment": ""}, "environment"),
    ({"bucket": "captua-masters\n"}, "bucket"),
    ({"token": "ab" * 32 + "\n"}, "token"),
    ({"writeKey": {"keyId": "005\n", "applicationKey": "k"}}, "writeKey.keyId"),
])
def test_invalid_payload_names_the_field_only(changes, field):
    from app.core.fisqua_pairing_http import validate_payload

    body = payload(**changes)
    assert validate_payload(body) == field


def test_missing_field_is_named():
    from app.core.fisqua_pairing_http import validate_payload

    body = payload()
    del body["ageRecipient"]
    assert validate_payload(body) == "ageRecipient"
    assert validate_payload(["not", "an", "object"]) == "payload"
