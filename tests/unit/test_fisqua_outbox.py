"""The Fisqua outbox: a manifest per handover, delivered once the volume is received.

What is tested here does not depend on Fisqua's side, which is not agreed
yet: the transport is a fake (FakeTransport), the only one besides
NullTransport. A handover enqueues a row when Fisqua is configured; the
worker step promotes it to `ready` once every object of the volume is
verified in the bucket, building the manifest then; and `ready` rows are
handed to the transport, with backoff on errors.

    python -m pytest tests/unit/test_fisqua_outbox.py
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.unit.test_capture_manifest_export import TENANT, Volume, _as, tenant, vol  # noqa: F401

TOKEN = "fisqua-secret-token-8c1f2e"
URL = "https://fisqua.example.org"


@pytest.fixture
def fisqua(monkeypatch, tenant):
    from pydantic import SecretStr

    from app.core import config
    monkeypatch.setattr(config.settings, "FISQUA_URL", URL)
    monkeypatch.setattr(config.settings, "FISQUA_TOKEN", SecretStr(TOKEN))
    monkeypatch.setattr(config.settings, "OFFSITE_REMOTE", "b2unit:bucket")
    return URL


class FakeTransport:
    """Answers each send and each status poll from a script; raises what the script holds as an exception."""

    def __init__(self, sends=(), statuses=()):
        self.sends = list(sends)
        self.statuses = list(statuses)
        self.sent = []
        self.polled = []

    def send_manifest(self, manifest):
        from app.core.fisqua_outbox import TransportResult
        self.sent.append(manifest)
        answer = self.sends.pop(0) if self.sends else TransportResult(outcome="error", error="no answer")
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get_status(self, external_id):
        from app.core.fisqua_outbox import TransportResult
        self.polled.append(external_id)
        answer = self.statuses.pop(0) if self.statuses else TransportResult(outcome="error", error="no answer")
        if isinstance(answer, Exception):
            raise answer
        return answer


def _delivered(external_id="fq-1", response=None):
    from app.core.fisqua_outbox import TransportResult
    return TransportResult(outcome="delivered", external_id=external_id,
                           response=response if response is not None else {"accepted": True})


def _rows(db, col):
    from app.models.fisqua import FisquaOutbox
    db.expire_all()
    return db.query(FisquaOutbox).filter(FisquaOutbox.collection_id == col.id).order_by(FisquaOutbox.id).all()


def _handover(client, col):
    resp = _as(client, "operator").post(f"/collections/{col.id}/promote")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _take_back(client, col):
    resp = _as(client, "operator").post(f"/collections/{col.id}/take-back")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _run(db, transport, now=None):
    from app.core.fisqua_outbox import run_fisqua_outbox
    return run_fisqua_outbox(db, transport=transport, now=now)


# ---------------------------------------------------------------------------
# Enqueue at handover
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_handover_enqueues_a_row_waiting_for_upload(client, db_session, vol, fisqua):
    vol.record(status="captured")
    assert _handover(client, vol.col)["promoted"] == 1

    rows = _rows(db_session, vol.col)
    assert len(rows) == 1
    row = rows[0]
    assert row.state == "waiting_for_upload"
    assert row.stem == vol.stem
    assert uuid.UUID(row.manifest_id).version == 4
    assert row.attempts == 0
    assert row.manifest is None and row.delivered_at is None


@pytest.mark.unit
def test_no_row_without_fisqua_configured(client, db_session, vol):
    vol.record(status="captured")
    assert _handover(client, vol.col)["promoted"] == 1
    assert _rows(db_session, vol.col) == []


@pytest.mark.unit
@pytest.mark.parametrize("url,token", [(URL, ""), ("", TOKEN), ("  ", "  ")])
def test_a_half_configured_fisqua_is_not_configured(client, db_session, vol, monkeypatch, url, token):
    from pydantic import SecretStr

    from app.core import config
    monkeypatch.setattr(config.settings, "FISQUA_URL", url)
    monkeypatch.setattr(config.settings, "FISQUA_TOKEN", SecretStr(token))
    vol.record(status="captured")
    _handover(client, vol.col)
    assert _rows(db_session, vol.col) == []


@pytest.mark.unit
def test_a_handover_that_promotes_nothing_enqueues_nothing(client, db_session, vol, fisqua):
    vol.record(status="in_review")
    assert _handover(client, vol.col)["promoted"] == 0
    assert _rows(db_session, vol.col) == []


@pytest.mark.unit
def test_a_refused_handover_enqueues_nothing(client, db_session, vol, fisqua):
    from app.models.record import RecordImage
    rec = vol.record(status="captured")
    db_session.query(RecordImage).filter(RecordImage.record_id == rec.id).delete()
    db_session.commit()
    resp = _as(client, "operator").post(f"/collections/{vol.col.id}/promote")
    assert resp.status_code == 422
    assert _rows(db_session, vol.col) == []


@pytest.mark.unit
def test_take_back_withdraws_the_unsent_row_and_a_new_handover_adds_another(client, db_session, vol, fisqua):
    vol.record(status="captured")
    _handover(client, vol.col)
    first = _rows(db_session, vol.col)[0].manifest_id

    _take_back(client, vol.col)
    rows = _rows(db_session, vol.col)
    assert [r.state for r in rows] == ["withdrawn"]

    _handover(client, vol.col)
    rows = _rows(db_session, vol.col)
    assert [r.state for r in rows] == ["withdrawn", "waiting_for_upload"]
    assert rows[1].manifest_id != first


@pytest.mark.unit
def test_a_take_back_while_unconfigured_still_withdraws_the_queued_row(client, db_session, vol, fisqua, monkeypatch):
    from pydantic import SecretStr

    from app.core import config
    vol.record(status="captured")
    _handover(client, vol.col)
    _run(db_session, FakeTransport())  # promoted, then an error: failed
    assert _rows(db_session, vol.col)[0].state == "failed"

    monkeypatch.setattr(config.settings, "FISQUA_URL", "")
    monkeypatch.setattr(config.settings, "FISQUA_TOKEN", SecretStr(""))
    _take_back(client, vol.col)
    monkeypatch.setattr(config.settings, "FISQUA_URL", URL)
    monkeypatch.setattr(config.settings, "FISQUA_TOKEN", SecretStr(TOKEN))

    transport = FakeTransport(sends=[_delivered()])
    _run(db_session, transport, now=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1))
    assert transport.sent == []
    assert [r.state for r in _rows(db_session, vol.col)] == ["withdrawn"]


@pytest.mark.unit
def test_a_handover_while_unconfigured_still_supersedes_the_queued_row(client, db_session, vol, fisqua, monkeypatch):
    from app.core import config
    vol.record(status="captured")
    _handover(client, vol.col)
    _take_back(client, vol.col)
    _handover(client, vol.col)  # configured: withdrawn, waiting_for_upload

    monkeypatch.setattr(config.settings, "FISQUA_URL", "")
    vol.record(status="captured")
    _handover(client, vol.col)  # unconfigured: no new row, the queued one is superseded
    monkeypatch.setattr(config.settings, "FISQUA_URL", URL)

    transport = FakeTransport(sends=[_delivered()])
    _run(db_session, transport)
    assert transport.sent == []
    assert [r.state for r in _rows(db_session, vol.col)] == ["withdrawn", "superseded"]


@pytest.mark.unit
def test_a_new_handover_supersedes_an_unsent_row_and_leaves_a_delivered_one(client, db_session, vol, fisqua):
    vol.record(status="captured")
    _handover(client, vol.col)
    _run(db_session, FakeTransport(sends=[_delivered()]))
    assert _rows(db_session, vol.col)[0].state == "delivered"

    # A new folio makes the volume unsubmitted; handing it over again
    # enqueues a second manifest, and a third handover supersedes the second.
    vol.record(status="captured")
    _handover(client, vol.col)
    vol.record(status="captured")
    _handover(client, vol.col)
    rows = _rows(db_session, vol.col)
    assert [r.state for r in rows] == ["delivered", "superseded", "waiting_for_upload"]


# ---------------------------------------------------------------------------
# Promotion to ready
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_promotion_waits_until_every_object_is_verified(client, db_session, vol, fisqua):
    rec = vol.record(status="captured", upload=False)
    _handover(client, vol.col)
    transport = FakeTransport(sends=[_delivered()])

    _run(db_session, transport)
    assert _rows(db_session, vol.col)[0].state == "waiting_for_upload"
    assert transport.sent == []

    data = {img.id: f"{img.filename} bytes".encode() for img in rec.images}
    for img in rec.images:
        vol.offsite(img, "capture", f"{TENANT}/{vol.stem}/captures/{img.filename}", data[img.id])
    _run(db_session, transport)
    row = _rows(db_session, vol.col)[0]
    assert row.state == "delivered"
    assert len(transport.sent) == 1


@pytest.mark.unit
def test_the_manifest_is_built_at_promotion_with_the_rows_id(client, db_session, vol, fisqua):
    vol.record(status="captured")
    _handover(client, vol.col)
    transport = FakeTransport(sends=[_delivered()])
    _run(db_session, transport)

    row = _rows(db_session, vol.col)[0]
    sent = transport.sent[0]
    assert sent["id"] == row.manifest_id
    assert sent["kind"] == "capture-manifest"
    assert sent["volumes"][0]["stem"] == vol.stem
    assert row.manifest == sent


@pytest.mark.unit
def test_a_non_exportable_volume_is_refused_locally_with_its_problems(client, db_session, vol, fisqua):
    # A JPEG capture with no recorded sha256 is verified offsite but cannot be exported.
    vol.record(status="captured", record_digest=False)
    _handover(client, vol.col)
    transport = FakeTransport()
    _run(db_session, transport)

    row = _rows(db_session, vol.col)[0]
    assert row.state == "refused"
    assert row.refusal == "not_exportable"
    assert "sha256_missing" in {p["rule"] for p in row.problems}
    assert transport.sent == []


@pytest.mark.unit
def test_a_volume_without_a_stem_is_refused_rather_than_left_waiting(client, db_session, fisqua):
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage
    proj = Project(name="P")
    db_session.add(proj)
    db_session.commit()
    col = Collection(name="Tomo", project_id=proj.id)
    db_session.add(col)
    db_session.commit()
    rec = Record(title="r", collection_id=col.id, status="captured", capture_mode="single", sequence=1)
    db_session.add(rec)
    db_session.commit()
    db_session.add(RecordImage(record_id=rec.id, filename="f.jpg", file_path="/f.jpg", format="jpg",
                               role="single", is_current=True))
    db_session.commit()
    _handover(client, col)
    _run(db_session, FakeTransport())

    row = _rows(db_session, col)[0]
    assert row.stem is None
    assert row.state == "refused" and row.refusal == "not_exportable"
    assert "no_stem" in {p["rule"] for p in row.problems}


@pytest.mark.unit
def test_promotion_waits_while_the_volume_is_held_for_file_changes(client, db_session, vol, fisqua):
    from app.core.export_jobs import export_jobs
    vol.record(status="captured")
    _handover(client, vol.col)
    transport = FakeTransport(sends=[_delivered()])
    with export_jobs.hold_for_file_changes([vol.col.id]):
        _run(db_session, transport)
    assert _rows(db_session, vol.col)[0].state == "waiting_for_upload"
    _run(db_session, transport)
    assert _rows(db_session, vol.col)[0].state == "delivered"


@pytest.mark.unit
def test_the_build_holds_the_volume_for_reading(client, db_session, vol, fisqua, monkeypatch):
    """While the manifest is built, a request that would move the volume's files is refused."""
    from fastapi import HTTPException

    from app.core import capture_manifest
    from app.core.export_jobs import export_jobs
    vol.record(status="captured")
    _handover(client, vol.col)
    seen = []
    real = capture_manifest.build_capture_manifest

    def spy(db, collection, **kw):
        with pytest.raises(HTTPException) as exc:
            with export_jobs.hold_for_file_changes([collection.id]):
                pass
        seen.append(exc.value.status_code)
        return real(db, collection, **kw)

    monkeypatch.setattr(capture_manifest, "build_capture_manifest", spy)
    _run(db_session, FakeTransport(sends=[_delivered()]))
    assert seen == [409]


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_delivered_records_the_answer_and_the_external_id(client, db_session, vol, fisqua):
    vol.record(status="captured")
    _handover(client, vol.col)
    _run(db_session, FakeTransport(sends=[_delivered("fq-77", {"volume": "fq-77", "state": "received"})]))

    row = _rows(db_session, vol.col)[0]
    assert row.state == "delivered"
    assert row.delivered_at is not None
    assert row.external_id == "fq-77"
    assert row.response == {"volume": "fq-77", "state": "received"}
    assert row.attempts == 1
    assert row.last_error is None


@pytest.mark.unit
def test_refused_by_fisqua_records_its_problems(client, db_session, vol, fisqua):
    from app.core.fisqua_outbox import TransportResult
    vol.record(status="captured")
    _handover(client, vol.col)
    answer = {"error": "stem unknown"}
    transport = FakeTransport(sends=[TransportResult(outcome="refused", response=answer,
                                                     problems=[{"rule": "stem_unknown"}])])
    _run(db_session, transport)
    _run(db_session, transport)  # a refusal is final: not sent again

    row = _rows(db_session, vol.col)[0]
    assert row.state == "refused" and row.refusal == "fisqua"
    assert row.response == answer
    assert row.problems == [{"rule": "stem_unknown"}]
    assert len(transport.sent) == 1


@pytest.mark.unit
def test_a_transport_error_fails_the_row_and_retries_after_a_backoff(client, db_session, vol, fisqua, monkeypatch):
    from app.core import config
    from app.core.fisqua_outbox import TransportResult
    monkeypatch.setattr(config.settings, "FISQUA_RETRY_BASE_SECONDS", 60)
    monkeypatch.setattr(config.settings, "FISQUA_RETRY_MAX_SECONDS", 300)
    vol.record(status="captured")
    _handover(client, vol.col)
    transport = FakeTransport(sends=[
        TransportResult(outcome="error", error="connection refused"),
        ConnectionError("network unreachable"),
        TransportResult(outcome="error", error="503"),
        TransportResult(outcome="error", error="503"),
        _delivered(),
    ])
    t0 = datetime(2026, 10, 24, 12, 0, 0)

    _run(db_session, transport, now=t0)
    row = _rows(db_session, vol.col)[0]
    assert row.state == "failed" and row.attempts == 1
    assert row.last_error == "connection refused"
    assert row.next_attempt_at == t0 + timedelta(seconds=60)

    _run(db_session, transport, now=t0 + timedelta(seconds=30))  # too early: not sent
    assert len(transport.sent) == 1

    _run(db_session, transport, now=t0 + timedelta(seconds=60))
    row = _rows(db_session, vol.col)[0]
    assert row.attempts == 2 and "network unreachable" in row.last_error
    assert row.next_attempt_at == t0 + timedelta(seconds=60 + 120)

    t = t0 + timedelta(seconds=180)
    _run(db_session, transport, now=t)
    assert _rows(db_session, vol.col)[0].next_attempt_at == t + timedelta(seconds=240)
    t += timedelta(seconds=240)
    _run(db_session, transport, now=t)
    row = _rows(db_session, vol.col)[0]
    assert row.attempts == 4
    assert row.next_attempt_at == t + timedelta(seconds=300)  # capped

    _run(db_session, transport, now=t + timedelta(seconds=300))
    row = _rows(db_session, vol.col)[0]
    assert row.state == "delivered" and row.attempts == 5
    assert row.next_attempt_at is None


@pytest.mark.unit
def test_the_retry_delay_doubles_and_is_capped(monkeypatch):
    from app.core import config
    from app.core.fisqua_outbox import retry_delay
    monkeypatch.setattr(config.settings, "FISQUA_RETRY_BASE_SECONDS", 60)
    monkeypatch.setattr(config.settings, "FISQUA_RETRY_MAX_SECONDS", 3600)
    assert [retry_delay(n).total_seconds() for n in (1, 2, 3, 6, 7, 50)] == [60, 120, 240, 1920, 3600, 3600]


@pytest.mark.unit
def test_last_error_is_truncated(client, db_session, vol, fisqua):
    from app.core.fisqua_outbox import LAST_ERROR_LIMIT, TransportResult
    vol.record(status="captured")
    _handover(client, vol.col)
    _run(db_session, FakeTransport(sends=[TransportResult(outcome="error", error="x" * 10_000)]))
    assert len(_rows(db_session, vol.col)[0].last_error) == LAST_ERROR_LIMIT


@pytest.mark.unit
def test_a_row_left_sending_by_a_crash_is_retried(client, db_session, vol, fisqua):
    vol.record(status="captured")
    _handover(client, vol.col)
    _run(db_session, FakeTransport())  # an error: ready -> failed
    row = _rows(db_session, vol.col)[0]
    row.state, row.next_attempt_at = "sending", None
    db_session.commit()

    transport = FakeTransport(sends=[_delivered()])
    _run(db_session, transport)
    row = _rows(db_session, vol.col)[0]
    assert row.state == "delivered"
    assert len(transport.sent) == 1


@pytest.mark.unit
def test_the_null_transport_leaves_ready_rows_ready(client, db_session, vol, fisqua):
    from app.core.fisqua_outbox import NullTransport, run_fisqua_outbox
    vol.record(status="captured")
    _handover(client, vol.col)
    result = run_fisqua_outbox(db_session, transport=NullTransport())
    row = _rows(db_session, vol.col)[0]
    assert row.state == "ready" and row.attempts == 0
    assert row.manifest is not None
    assert result["status"] == "no_transport"


@pytest.mark.unit
def test_the_default_transport_is_the_null_one(fisqua):
    from app.core.fisqua_outbox import NullTransport, get_transport
    assert isinstance(get_transport(), NullTransport)


@pytest.mark.unit
def test_not_configured_the_step_does_nothing(client, db_session, vol, fisqua, monkeypatch):
    from app.core import config
    vol.record(status="captured")
    _handover(client, vol.col)
    monkeypatch.setattr(config.settings, "FISQUA_URL", "")
    transport = FakeTransport(sends=[_delivered()])
    assert _run(db_session, transport)["status"] == "disabled"
    assert _rows(db_session, vol.col)[0].state == "waiting_for_upload"


@pytest.mark.unit
def test_the_upload_schedule_runs_the_step_after_each_upload_run(db_session, monkeypatch):
    from app.core import fisqua_outbox, image_upload
    calls = []
    monkeypatch.setattr(image_upload, "run_image_upload", lambda db, stop=None: calls.append("upload") or {"status": "done"})
    monkeypatch.setattr(fisqua_outbox, "run_fisqua_outbox", lambda db, stop=None: calls.append("fisqua") or {"status": "done"})
    scheduler = image_upload.ImageUploadScheduler()
    assert scheduler.tick() == {"status": "done"}
    assert calls == ["upload", "fisqua"]
    assert scheduler.last_fisqua_result == {"status": "done"}


@pytest.mark.unit
def test_the_step_runs_even_when_the_upload_run_raised(db_session, monkeypatch):
    from app.core import fisqua_outbox, image_upload
    calls = []

    def boom(db, stop=None):
        raise RuntimeError("upload broke")
    monkeypatch.setattr(image_upload, "run_image_upload", boom)
    monkeypatch.setattr(fisqua_outbox, "run_fisqua_outbox", lambda db, stop=None: calls.append("fisqua") or {"status": "done"})
    image_upload.ImageUploadScheduler().tick()
    assert calls == ["fisqua"]


# ---------------------------------------------------------------------------
# Status polling
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_latest_delivered_manifest_is_polled_and_its_status_kept(client, db_session, vol, fisqua):
    from app.core.fisqua_outbox import TransportResult
    from app.models.fisqua import FisquaStatus
    vol.record(status="captured")
    _handover(client, vol.col)
    at = datetime(2026, 10, 24, 9, 30, 0)
    transport = FakeTransport(
        sends=[_delivered("fq-9")],
        statuses=[TransportResult(outcome="status", status="en_revision", status_at=at,
                                  problems=[{"page": 3, "issue": "blur"}], response={"state": "en_revision"})])
    now = datetime(2026, 10, 24, 12, 0, 0)
    _run(db_session, transport, now=now)  # delivers, then polls
    assert transport.polled == ["fq-9"]

    db_session.expire_all()
    st = db_session.get(FisquaStatus, vol.col.id)
    assert st.status == "en_revision"
    assert st.status_at == at
    assert st.status_checked_at == now
    assert st.problems == [{"page": 3, "issue": "blur"}]
    assert st.manifest_id == _rows(db_session, vol.col)[0].manifest_id


@pytest.mark.unit
def test_a_poll_error_keeps_the_last_status(client, db_session, vol, fisqua):
    from app.core.fisqua_outbox import TransportResult
    from app.models.fisqua import FisquaStatus
    vol.record(status="captured")
    _handover(client, vol.col)
    transport = FakeTransport(sends=[_delivered("fq-9")],
                              statuses=[TransportResult(outcome="status", status="recibido"),
                                        TransportResult(outcome="error", error="timeout")])
    t1 = datetime(2026, 10, 24, 12, 0, 0)
    _run(db_session, transport, now=t1)
    _run(db_session, transport, now=t1 + timedelta(hours=1))
    db_session.expire_all()
    st = db_session.get(FisquaStatus, vol.col.id)
    assert st.status == "recibido" and st.status_checked_at == t1


@pytest.mark.unit
def test_a_delivery_with_no_external_id_is_not_polled(client, db_session, vol, fisqua):
    vol.record(status="captured")
    _handover(client, vol.col)
    transport = FakeTransport(sends=[_delivered(external_id=None)])
    _run(db_session, transport)
    assert transport.polled == []


# ---------------------------------------------------------------------------
# The offsite state response
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_offsite_state_has_a_null_fisqua_when_not_configured(client, db_session, vol):
    resp = _as(client, "reviewer").get(f"/collections/{vol.col.id}/offsite")
    assert resp.status_code == 200, resp.text
    assert resp.json()["fisqua"] is None


@pytest.mark.unit
def test_offsite_state_carries_the_outbox_and_the_status(client, db_session, vol, fisqua):
    from app.core.fisqua_outbox import TransportResult
    vol.record(status="captured")
    _handover(client, vol.col)
    body = _as(client, "reviewer").get(f"/collections/{vol.col.id}/offsite").json()
    assert body["fisqua"]["status"] is None
    [entry] = body["fisqua"]["outbox"]
    assert entry["state"] == "waiting_for_upload"

    _run(db_session, FakeTransport(sends=[_delivered("fq-1")],
                                   statuses=[TransportResult(outcome="status", status="recibido")]))
    body = _as(client, "reviewer").get(f"/collections/{vol.col.id}/offsite").json()
    fq = body["fisqua"]
    [entry] = fq["outbox"]
    assert entry["state"] == "delivered"
    assert entry["manifest_id"] == _rows(db_session, vol.col)[0].manifest_id
    assert set(entry) == {"manifest_id", "stem", "state", "created_at", "attempts", "next_attempt_at",
                          "last_attempt_at", "last_error", "refusal", "problems", "response",
                          "external_id", "delivered_at"}
    assert fq["status"]["status"] == "recibido"
    assert set(fq["status"]) == {"status", "status_at", "status_checked_at", "problems", "manifest_id"}


@pytest.mark.unit
def test_offsite_state_lists_the_newest_manifest_first(client, db_session, vol, fisqua):
    vol.record(status="captured")
    _handover(client, vol.col)
    _take_back(client, vol.col)
    _handover(client, vol.col)
    body = _as(client, "reviewer").get(f"/collections/{vol.col.id}/offsite").json()
    assert [e["state"] for e in body["fisqua"]["outbox"]] == ["waiting_for_upload", "withdrawn"]


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_token_never_reaches_logs_rows_or_responses(client, db_session, vol, fisqua, caplog):
    from app.core.fisqua_outbox import TransportResult
    from app.models.system_log import SystemLog
    caplog.set_level(logging.DEBUG)
    vol.record(status="captured")
    _handover(client, vol.col)
    # A transport whose answers echo the request, token included, at any depth.
    echoed = {"headers": {"Authorization": f"Bearer {TOKEN}"},
              "trace": [f"sent {TOKEN}", {"nested": [TOKEN, 3, None]}]}
    transport = FakeTransport(
        sends=[TransportResult(outcome="error", error=f"401 for Authorization: Bearer {TOKEN}",
                               response=echoed),
               RuntimeError(f"token {TOKEN} rejected"),
               TransportResult(outcome="refused", response=echoed, error=f"bad {TOKEN}",
                               problems=[{"rule": "auth", "detail": f"token {TOKEN} unknown"}])])
    _run(db_session, transport)
    row = _rows(db_session, vol.col)[0]
    assert TOKEN not in row.last_error
    assert TOKEN not in str(row.response)
    assert row.response["trace"][1]["nested"][1:] == [3, None]
    later = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    _run(db_session, transport, now=later)
    row = _rows(db_session, vol.col)[0]
    assert TOKEN not in (row.last_error or "")
    _run(db_session, transport, now=later + timedelta(days=1))
    row = _rows(db_session, vol.col)[0]
    assert row.state == "refused" and row.refusal == "fisqua"
    for value in (row.response, row.problems, row.last_error):
        assert TOKEN not in str(value)

    # A delivery and a status answer that echo it too.
    vol.record(status="captured")
    _handover(client, vol.col)
    _run(db_session, FakeTransport(
        sends=[TransportResult(outcome="delivered", external_id="fq-2", response=echoed)],
        statuses=[TransportResult(outcome="status", status=f"ok {TOKEN}",
                                  problems=[{"x": TOKEN}], response=echoed)]))
    from app.models.fisqua import FisquaStatus
    db_session.expire_all()
    st = db_session.get(FisquaStatus, vol.col.id)
    assert st is not None
    for value in (st.status, st.problems, _rows(db_session, vol.col)[-1].response):
        assert TOKEN not in str(value)

    texts = [caplog.text]
    texts += [_as(client, "admin").get(f"/collections/{vol.col.id}/offsite").text]
    texts += [_as(client, "admin").get("/system/storage").text]
    texts += [str(r.detail) for r in db_session.query(SystemLog).all()]
    from app.core import config
    texts += [repr(config.settings), str(config.settings.model_dump())]
    for text in texts:
        assert TOKEN not in text
