"""The HTTPS transport to Fisqua (contract 1.2), against a fake server (httpx.MockTransport).

Each answer Fisqua can give a manifest is mapped to a delivery state;
Retry-After is a floor on the backoff; a 401 or 403 stops the unit until the
token changes; status is asked for in batches of at most 100 refs; and the
token never reaches a log, a row or a response.

    python -m pytest tests/unit/test_fisqua_http.py
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from tests.unit.test_capture_manifest_export import TENANT, _as, tenant, vol  # noqa: F401
from tests.unit.test_fisqua_outbox import OTHER_TOKEN, TOKEN, _handover, _rows, fisqua  # noqa: F401

BASE = "https://fisqua.test"


class Server:
    """A fake Fisqua: answers each request from a script and keeps what it received."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if self.answers else httpx.Response(500)
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(request)
        return answer


def _transport(server):
    """The transport get_transport would build, with the configured token, over the fake server."""
    from app.core import config
    from app.core.fisqua_http import HttpFisquaTransport
    return HttpFisquaTransport(BASE, config.settings.FISQUA_TOKEN.get_secret_value(),
                               client=httpx.Client(transport=httpx.MockTransport(server)))


def _send(db, server, now=None):
    from app.core.fisqua_outbox import run_fisqua_outbox
    return run_fisqua_outbox(db, transport=_transport(server), now=now)


def _poll(db, server, now=None):
    from app.core.fisqua_outbox import poll_fisqua_status
    return poll_fisqua_status(db, transport=_transport(server), now=now)


def _ok(state="received", volume_id="fq-v1"):
    """Fisqua's 200 to a POST, naming the manifest id it was sent, as the contract has it."""
    def answer(request):
        sent = json.loads(request.content)["id"] if request.content else None
        return httpx.Response(200, json={"state": state, "manifestId": sent, "volumeId": volume_id})
    return answer


@pytest.fixture
def queued(client, db_session, vol, fisqua):
    vol.record(status="captured")
    _handover(client, vol.col)
    return vol


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_manifest_is_posted_with_the_bearer_token(db_session, queued):
    server = Server(_ok())
    _send(db_session, server)
    [req] = server.requests
    assert req.method == "POST"
    assert str(req.url) == f"{BASE}/api/capture/v1/manifests"
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    row = _rows(db_session, queued.col)[0]
    assert json.loads(req.content) == row.manifest
    assert TOKEN not in str(req.url)


@pytest.mark.unit
def test_the_client_verifies_tls_follows_no_redirect_and_has_timeouts():
    from app.core.fisqua_http import HttpFisquaTransport
    t = HttpFisquaTransport(BASE, TOKEN, timeout=42.0)
    client = t._client
    assert client.follow_redirects is False
    assert client.timeout.read == 42.0 and client.timeout.connect == 10.0
    # httpx keeps the verify setting on the transport's SSL context.
    import ssl
    ctx = client._transport._pool._ssl_context
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    t.close()
    assert TOKEN not in repr(t)


# ---------------------------------------------------------------------------
# Answers to a manifest
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_200_is_delivered_with_the_volume_id_and_state(db_session, queued):
    from app.models.fisqua import FisquaStatus
    _send(db_session, Server(_ok(volume_id="fq-v7", state="registered")))
    row = _rows(db_session, queued.col)[0]
    assert row.state == "delivered" and row.external_id == "fq-v7"
    assert row.response["volumeId"] == "fq-v7"
    db_session.expire_all()
    assert db_session.get(FisquaStatus, queued.col.id).status == "registered"


@pytest.mark.unit
@pytest.mark.parametrize("answered", ["another-manifest-id", None])
def test_a_2xx_for_another_manifest_id_is_retried_and_logged(db_session, queued, caplog, answered):
    body = {"state": "received", "volumeId": "fq-v1"}
    if answered is not None:
        body["manifestId"] = answered
    with caplog.at_level(logging.WARNING):
        _send(db_session, Server(httpx.Response(200, json=body)))
    row = _rows(db_session, queued.col)[0]
    assert row.state == "failed"
    assert "manifestId" in row.last_error
    assert row.manifest_id in caplog.text


@pytest.mark.unit
def test_a_2xx_for_the_manifest_sent_is_delivered(db_session, queued):
    server = Server(lambda req: httpx.Response(200, json={
        "state": "received", "volumeId": "fq-v1", "manifestId": json.loads(req.content)["id"]}))
    _send(db_session, server)
    assert _rows(db_session, queued.col)[0].state == "delivered"


@pytest.mark.unit
def test_200_without_a_volume_id_is_retried(db_session, queued):
    _send(db_session, Server(httpx.Response(200, json={"state": "received"})))
    row = _rows(db_session, queued.col)[0]
    assert row.state == "failed" and "volumeId" in row.last_error


@pytest.mark.unit
def test_422_is_refused_with_fisquas_problems(db_session, queued):
    problems = [{"rule": "run-started", "message": "A run has started on the older manifest.", "recordIds": [1]}]
    server = Server(httpx.Response(422, json={"problems": problems}))
    _send(db_session, server)
    _send(db_session, server, now=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1))
    row = _rows(db_session, queued.col)[0]
    assert row.state == "refused" and row.refusal == "fisqua"
    assert row.problems == problems
    assert len(server.requests) == 1  # final


@pytest.mark.unit
def test_409_id_reused_is_refused(db_session, queued):
    _send(db_session, Server(httpx.Response(409, json={"problems": [{"rule": "id-reused", "message": "taken"}]})))
    row = _rows(db_session, queued.col)[0]
    assert row.state == "refused" and row.problems[0]["rule"] == "id-reused"


@pytest.mark.unit
def test_409_with_any_problems_is_refused(db_session, queued):
    """The supersede refusal once a run has started on the older manifest comes as a 409 with problems."""
    problems = [{"rule": "run-started", "message": "A run has started on the volume's manifest."}]
    _send(db_session, Server(httpx.Response(409, json={"problems": problems})))
    row = _rows(db_session, queued.col)[0]
    assert row.state == "refused" and row.refusal == "fisqua" and row.problems == problems


@pytest.mark.unit
@pytest.mark.parametrize("body", [{"rule": "id-reused"}, {"problems": []}, None])
def test_409_without_problems_is_retried(db_session, queued, body):
    answer = httpx.Response(409, json=body) if body is not None else httpx.Response(409)
    _send(db_session, Server(answer))
    assert _rows(db_session, queued.col)[0].state == "failed"


@pytest.mark.unit
def test_400_with_problems_is_refused(db_session, queued):
    problems = [{"rule": "malformed", "message": "volumes[0].records is not a list"}]
    _send(db_session, Server(httpx.Response(400, json={"problems": problems})))
    row = _rows(db_session, queued.col)[0]
    assert row.state == "refused" and row.problems == problems


@pytest.mark.unit
@pytest.mark.parametrize("body", [{"error": "bad"}, {"problems": []}, None])
def test_400_without_problems_is_retried(db_session, queued, body):
    answer = httpx.Response(400, json=body) if body is not None else httpx.Response(400)
    _send(db_session, Server(answer))
    assert _rows(db_session, queued.col)[0].state == "failed"


@pytest.mark.unit
def test_422_reference_code_is_refused(db_session, queued):
    problems = [{"rule": "reference-code", "message": "The volume has no reference code."}]
    _send(db_session, Server(httpx.Response(422, json={"problems": problems})))
    row = _rows(db_session, queued.col)[0]
    assert row.state == "refused" and row.problems[0]["rule"] == "reference-code"


@pytest.mark.unit
def test_413_is_refused(db_session, queued):
    _send(db_session, Server(httpx.Response(413)))
    row = _rows(db_session, queued.col)[0]
    assert row.state == "refused" and row.problems[0]["rule"] == "payload-too-large"


@pytest.mark.unit
@pytest.mark.parametrize("answer", [
    httpx.Response(429), httpx.Response(500), httpx.Response(502), httpx.Response(503),
    httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.Response(418),
])
def test_transient_answers_are_retried_with_backoff(db_session, queued, monkeypatch, answer):
    from app.core import config
    monkeypatch.setattr(config.settings, "FISQUA_RETRY_BASE_SECONDS", 60)
    now = datetime(2026, 10, 24, 12, 0, 0)
    _send(db_session, Server(answer), now=now)
    row = _rows(db_session, queued.col)[0]
    assert row.state == "failed" and row.attempts == 1
    assert row.next_attempt_at == now + timedelta(seconds=60)


@pytest.mark.unit
@pytest.mark.parametrize("code", [301, 302, 307, 308])
def test_a_redirect_is_not_followed_and_stops_the_unit(db_session, queued, code):
    from app.core.fisqua_outbox import config_error
    server = Server(httpx.Response(code, headers={"Location": "https://elsewhere.test/x"}))
    assert _send(db_session, server)["status"] == "config_error"
    assert len(server.requests) == 1
    assert config_error()["status_code"] == code
    assert _rows(db_session, queued.col)[0].state == "ready"


@pytest.mark.unit
def test_retry_after_in_seconds_is_a_floor(db_session, queued, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config.settings, "FISQUA_RETRY_BASE_SECONDS", 60)
    now = datetime(2026, 10, 24, 12, 0, 0)
    _send(db_session, Server(httpx.Response(429, headers={"Retry-After": "900"})), now=now)
    assert _rows(db_session, queued.col)[0].next_attempt_at == now + timedelta(seconds=900)


@pytest.mark.unit
def test_a_shorter_retry_after_does_not_shorten_the_backoff(db_session, queued, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config.settings, "FISQUA_RETRY_BASE_SECONDS", 600)
    now = datetime(2026, 10, 24, 12, 0, 0)
    _send(db_session, Server(httpx.Response(503, headers={"Retry-After": "5"})), now=now)
    assert _rows(db_session, queued.col)[0].next_attempt_at == now + timedelta(seconds=600)


@pytest.mark.unit
def test_retry_after_as_an_http_date():
    from app.core.fisqua_http import retry_after_seconds
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    later = format_datetime(now + timedelta(hours=2), usegmt=True)
    assert retry_after_seconds(later, now) == 7200
    assert retry_after_seconds(format_datetime(now - timedelta(hours=1), usegmt=True), now) == 0
    assert retry_after_seconds("120", now) == 120
    assert retry_after_seconds("soon", now) is None
    assert retry_after_seconds(None, now) is None


@pytest.mark.unit
def test_a_repeated_id_after_a_crash_is_sent_again_and_takes_the_original_answer(db_session, queued):
    from app.models.fisqua import FisquaOutbox
    _send(db_session, Server(httpx.ConnectError("down")))
    db_session.query(FisquaOutbox).update({"state": "sending"})
    db_session.commit()
    server = Server(_ok(volume_id="fq-v1"))
    _send(db_session, server)
    row = _rows(db_session, queued.col)[0]
    assert row.state == "delivered"
    assert json.loads(server.requests[0].content)["id"] == row.manifest_id


# ---------------------------------------------------------------------------
# 401 and 403: the unit stops until the token changes
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("code", [401, 403, 404, 302])
def test_a_configuration_error_stops_the_unit_until_the_token_changes(client, db_session, queued, monkeypatch, code, caplog):
    from pydantic import SecretStr

    from app.core import config
    from app.core.fisqua_outbox import config_error
    caplog.set_level(logging.DEBUG)
    server = Server(httpx.Response(code, json={"error": "bad token"}))
    result = _send(db_session, server)
    assert result["status"] == "config_error"
    row = _rows(db_session, queued.col)[0]
    assert row.state == "ready" and row.attempts == 0
    stop = config_error()
    assert stop["status_code"] == code and stop["at"] is not None
    assert len(stop["config_fingerprint"]) == 8
    assert stop["reason"]

    # Nothing is sent or polled while the token is the one refused, across a restart too.
    server = Server(_ok())
    assert _send(db_session, server)["status"] == "config_error"
    assert _poll(db_session, server)["status"] == "config_error"
    assert server.requests == []

    # The volume page and the system panel show it.
    body = _as(client, "reviewer").get(f"/collections/{queued.col.id}/offsite").json()
    assert body["fisqua"]["config_error"]["status_code"] == code
    system = _as(client, "reviewer").get("/system/catalogue-backup").json()
    assert system["fisqua"]["config_error"]["status_code"] == code
    assert system["fisqua"]["configured"] is True

    # A new token resumes sending.
    monkeypatch.setattr(config.settings, "FISQUA_TOKEN", SecretStr(OTHER_TOKEN))
    assert config_error() is None
    server = Server(_ok())
    _send(db_session, server)
    assert _rows(db_session, queued.col)[0].state == "delivered"
    assert server.requests[0].headers["Authorization"] == f"Bearer {OTHER_TOKEN}"

    import hashlib
    full_hash = hashlib.sha256(TOKEN.encode()).hexdigest()
    assert TOKEN not in str(stop) and full_hash not in str(stop)
    assert TOKEN not in caplog.text and full_hash not in caplog.text


@pytest.mark.unit
@pytest.mark.parametrize("code", [401, 403, 404, 307])
def test_a_configuration_error_on_the_poll_also_stops_the_unit(db_session, queued, code):
    from app.core.fisqua_outbox import config_error
    _send(db_session, Server(_ok()))
    assert _poll(db_session, Server(httpx.Response(code)))["status"] == "config_error"
    assert config_error()["status_code"] == code
    server = Server(_ok())
    assert _send(db_session, server)["status"] == "config_error"
    assert server.requests == []


@pytest.mark.unit
@pytest.mark.parametrize("setting,value", [("FISQUA_ENV", "production"),
                                           ("FISQUA_BASE_URL", "https://fisqua.dev.test")])
def test_a_configuration_error_clears_when_the_environment_or_base_url_changes(
        db_session, queued, monkeypatch, setting, value):
    from app.core import config
    from app.core.fisqua_outbox import config_error
    _send(db_session, Server(httpx.Response(404)))
    assert config_error() is not None
    monkeypatch.setattr(config.settings, setting, value)
    assert config_error() is None
    _send(db_session, Server(_ok()))
    assert _rows(db_session, queued.col)[0].state == "delivered"


@pytest.mark.unit
def test_the_fingerprint_is_of_env_base_url_and_token_together(fisqua, monkeypatch):
    from app.core import config
    from app.core.fisqua_outbox import config_fingerprint
    first = config_fingerprint()
    monkeypatch.setattr(config.settings, "FISQUA_BASE_URL", "https://x.test")
    second = config_fingerprint()
    monkeypatch.setattr(config.settings, "FISQUA_ENV", "production")
    third = config_fingerprint()
    assert len({first, second, third}) == 3
    assert all(len(f) == 8 for f in (first, second, third))


@pytest.mark.unit
def test_the_auth_file_holds_no_token_or_full_hash(db_session, queued):
    import hashlib

    from app.core import config
    from app.core.fisqua_outbox import CONFIG_ERROR_FILE
    _send(db_session, Server(httpx.Response(401)))
    text = (config.settings.data_dir / CONFIG_ERROR_FILE).read_text()
    assert TOKEN not in text and hashlib.sha256(TOKEN.encode()).hexdigest() not in text


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_status_is_asked_for_by_ref_and_read(db_session, queued):
    from app.models.fisqua import FisquaStatus
    _send(db_session, Server(_ok()))
    row = _rows(db_session, queued.col)[0]
    ref = queued.col.external_id
    server = Server(httpx.Response(200, json=[{
        "ref": ref, "volumeId": "fq-v1", "manifestId": row.manifest_id, "state": "running",
        "enteredAt": "2026-10-24T10:00:00Z", "problems": []}]))
    _poll(db_session, server)
    [req] = server.requests
    assert req.method == "GET"
    assert req.url.path == "/api/capture/v1/volumes/status"
    assert req.url.params["refs"] == ref
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    db_session.expire_all()
    st = db_session.get(FisquaStatus, queued.col.id)
    assert st.status == "running" and st.status_at == datetime(2026, 10, 24, 10, 0, 0)
    assert st.problems is None


@pytest.mark.unit
def test_status_also_reads_an_object_holding_the_volumes(db_session, queued):
    from app.models.fisqua import FisquaStatus
    _send(db_session, Server(_ok()))
    row = _rows(db_session, queued.col)[0]
    _poll(db_session, Server(httpx.Response(200, json={"volumes": [
        {"ref": queued.col.external_id, "manifestId": row.manifest_id, "state": "published", "problems": []}]})))
    db_session.expire_all()
    assert db_session.get(FisquaStatus, queued.col.id).status == "published"


@pytest.mark.unit
def test_unknown_is_not_final_and_is_shown(client, db_session, queued):
    from app.models.fisqua import FisquaStatus
    _send(db_session, Server(_ok()))
    row = _rows(db_session, queued.col)[0]
    ref = queued.col.external_id
    unknown = httpx.Response(200, json={"volumes": [{"ref": ref, "state": "unknown", "problems": []}]})
    server = Server(unknown, unknown)
    _poll(db_session, server)
    _poll(db_session, server)
    assert len(server.requests) == 2  # polled again
    db_session.expire_all()
    assert db_session.get(FisquaStatus, queued.col.id).status == "unknown"
    body = _as(client, "reviewer").get(f"/collections/{queued.col.id}/offsite").json()
    assert body["fisqua"]["status"]["status"] == "unknown"
    assert row.manifest_id  # the row's manifest is untouched by an unknown answer
    assert _rows(db_session, queued.col)[0].state == "delivered"


@pytest.mark.unit
def test_more_than_100_refs_are_asked_for_in_batches(db_session, override_projects_root, fisqua):
    import uuid

    from tests.unit.test_capture_manifest_export import Volume
    from app.core.fisqua_outbox import _now
    from app.models.fisqua import FisquaOutbox
    vols = [Volume(db_session, override_projects_root, stem=f"xx-test-{n:03d}", name=f"T{n}",
                   external_id=f"xx-test-{n:03d}") for n in range(1, 206)]
    for v in vols:
        db_session.add(FisquaOutbox(collection_id=v.col.id, manifest_id=str(uuid.uuid4()), stem=v.stem,
                                    state="delivered", attempts=1, delivered_at=_now(), created_at=_now()))
    db_session.commit()
    server = Server(*(lambda req: httpx.Response(200, json=[
        {"ref": r, "state": "queued", "manifestId": None, "problems": []}
        for r in req.url.params["refs"].split(",")]) for _ in range(3)))
    result = _poll(db_session, server)
    sizes = [len(r.url.params["refs"].split(",")) for r in server.requests]
    assert sizes == [100, 100, 5]
    assert result["answered"] == 205


@pytest.mark.unit
def test_the_transport_refuses_more_than_100_refs(fisqua):
    t = _transport(Server())
    with pytest.raises(ValueError):
        t.get_status([f"r{n}" for n in range(101)])


@pytest.mark.unit
def test_a_transient_status_error_keeps_the_last_answer(db_session, queued):
    _send(db_session, Server(_ok()))
    assert _poll(db_session, Server(httpx.Response(503)))["status"] == "error"


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_token_echoed_by_fisqua_never_reaches_rows_logs_or_the_api(client, db_session, queued, caplog):
    from app.models.fisqua import FisquaStatus
    from app.models.system_log import SystemLog
    caplog.set_level(logging.DEBUG)
    echo = {"seen": f"Bearer {TOKEN}", "nested": [{"k": TOKEN}]}
    server = Server(httpx.Response(503, json=echo),
                    httpx.Response(422, json={"problems": [{"rule": "x", "message": f"bad {TOKEN}"}], **echo}))
    _send(db_session, server)
    _send(db_session, server, now=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1))
    queued.record(status="captured")
    _handover(client, queued.col)
    _send(db_session, Server(lambda req: httpx.Response(200, json={
        "volumeId": f"v-{TOKEN}", "state": TOKEN, "manifestId": json.loads(req.content)["id"], **echo})))
    mid = _rows(db_session, queued.col)[-1].manifest_id
    _poll(db_session, Server(httpx.Response(200, json=[{"ref": queued.col.external_id, "manifestId": mid,
                                                        "state": f"s {TOKEN}", "problems": [{"m": TOKEN}]}])))
    texts = [caplog.text,
             _as(client, "admin").get(f"/collections/{queued.col.id}/offsite").text,
             _as(client, "admin").get("/system/catalogue-backup").text]
    for row in _rows(db_session, queued.col):
        texts += [str(row.response), str(row.problems), str(row.last_error), str(row.external_id)]
    db_session.expire_all()
    st = db_session.get(FisquaStatus, queued.col.id)
    texts += [str(st.status), str(st.problems)]
    texts += [str(r.detail) for r in db_session.query(SystemLog).all()]
    for text in texts:
        assert TOKEN not in text
