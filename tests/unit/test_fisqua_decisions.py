"""QC decisions from Fisqua (contract 1.2 §9.5), against a fake server.

A status entry carries `decisionsId`; when it differs from the last one
applied, the unit reads `GET /api/capture/v1/volumes/<ref>/decisions` and
applies the set if it is for the volume's current delivered manifest:
approved -> approved, rejected -> rejected with a rejection, both through
the undo transitions over a local decision, and rotation corrections per
image. Records taken back, gone from the volume, or with a decision the unit
cannot apply are skipped and reported.

    python -m pytest tests/unit/test_fisqua_decisions.py
"""

import json
import logging

import httpx
import pytest

from tests.unit.test_capture_manifest_export import TENANT, Volume, _as, tenant, vol  # noqa: F401
from tests.unit.test_fisqua_http import Server, _ok, _send, _transport, hold  # noqa: F401
from tests.unit.test_fisqua_outbox import TOKEN, _handover, _rows, _take_back, fisqua  # noqa: F401

STATUS_PATH = "/api/capture/v1/volumes/status"


class Fisqua:
    """A fake Fisqua that answers status and decisions requests by path."""

    def __init__(self, ref):
        self.ref = ref
        self.status = {}
        self.decisions = None
        self.decisions_answer = None
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path == STATUS_PATH:
            return httpx.Response(200, json={"volumes": [{"ref": self.ref, **self.status}]})
        if request.url.path == f"/api/capture/v1/volumes/{self.ref}/decisions":
            if self.decisions_answer is not None:
                return self.decisions_answer
            return httpx.Response(200, json=self.decisions)
        return httpx.Response(500)

    def decision_calls(self):
        return [r for r in self.requests if r.url.path.endswith("/decisions")]


def _poll(db, fake):
    from app.core.fisqua_outbox import poll_fisqua_status
    return poll_fisqua_status(db, transport=_transport(fake))


@pytest.fixture
def handed(client, db_session, vol, fisqua):
    """A volume of two records, handed over and delivered; Fisqua reviewing it."""
    recs = [vol.record(status="captured"), vol.record(status="captured")]
    _handover(client, vol.col)
    _send(db_session, Server(_ok()))
    row = _rows(db_session, vol.col)[0]
    assert row.state == "delivered"
    fake = Fisqua(vol.col.filename_stem)
    fake.status = {"volumeId": "fq-v1", "manifestId": row.manifest_id, "state": "review_pending", "problems": []}
    return vol, recs, row, fake


def _decide(fake, row, records, decisions_id="d-1"):
    fake.status.update({"decisionsId": decisions_id, "decidedAt": "2026-10-25T10:00:00Z",
                        "state": "retakes_requested" if any(r["decision"] == "rejected" for r in records)
                        else "review_pending"})
    fake.decisions = {"decisionsId": decisions_id, "decidedAt": "2026-10-25T10:00:00Z",
                      "manifestId": row.manifest_id, "records": records}


def _approve(rec, **extra):
    return {"captuaRecordId": rec.id, "decision": "approved", "decidedBy": "Ana Gómez",
            "decidedAt": "2026-10-25T09:00:00Z", "rotations": [], **extra}


def _reject(rec, reason="blur", note="img_0003_c1 blurred", **extra):
    return {"captuaRecordId": rec.id, "decision": "rejected", "reason": reason, "note": note,
            "decidedBy": "Ana Gómez", "decidedAt": "2026-10-25T09:00:00Z", "rotations": [], **extra}


def _refresh(db, rec):
    db.expire_all()
    from app.models.record import Record
    return db.get(Record, rec.id)


def _history(db, rec):
    from app.models.record import RecordStatusChange
    return [(h.from_status, h.to_status, h.changed_by) for h in
            db.query(RecordStatusChange).filter(RecordStatusChange.record_id == rec.id)
            .order_by(RecordStatusChange.id)]


def _decisions_state(client, col):
    return _as(client, "reviewer").get(f"/collections/{col.id}/offsite").json()["fisqua"]["decisions"]


# ---------------------------------------------------------------------------
# Approve and reject
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_approval_approves_the_record_and_names_the_reviewer(db_session, handed):
    vol, (a, b), row, fake = handed
    _decide(fake, row, [_approve(a)])
    _poll(db_session, fake)
    assert _refresh(db_session, a).status == "approved"
    assert _refresh(db_session, b).status == "in_review"
    assert _history(db_session, a)[-1] == ("in_review", "approved", "Fisqua: Ana Gómez")
    [call] = fake.decision_calls()
    assert call.headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.unit
def test_a_rejection_rejects_the_record_with_its_reason_and_note(db_session, handed):
    from app.models.record import RecordRejection
    vol, (a, b), row, fake = handed
    _decide(fake, row, [_reject(a, reason="glare", note="reflejo en el folio")])
    _poll(db_session, fake)
    rec = _refresh(db_session, a)
    assert rec.status == "rejected"
    [rej] = db_session.query(RecordRejection).filter(RecordRejection.record_id == a.id).all()
    assert (rej.predefined_reason, rej.comment, rej.rejected_by) == ("glare", "reflejo en el folio", "Fisqua: Ana Gómez")
    assert all(img.rejection_id == rej.id for img in rec.images if img.is_current)
    assert _history(db_session, a)[-1] == ("in_review", "rejected", "Fisqua: Ana Gómez")


@pytest.mark.unit
def test_a_rejection_overrides_a_local_approval_through_in_review(client, db_session, handed):
    vol, (a, b), row, fake = handed
    resp = _as(client, "reviewer").patch(f"/records/{a.id}/status", json={"status": "approved"})
    assert resp.status_code == 200, resp.text
    _decide(fake, row, [_reject(a)])
    _poll(db_session, fake)
    assert _refresh(db_session, a).status == "rejected"
    assert [h[:2] for h in _history(db_session, a)][-3:] == [
        ("in_review", "approved"), ("approved", "in_review"), ("in_review", "rejected")]


@pytest.mark.unit
def test_an_approval_overrides_a_local_rejection_through_in_review(client, db_session, handed):
    vol, (a, b), row, fake = handed
    resp = _as(client, "reviewer").post(f"/records/{a.id}/reject",
                                        json={"predefined_reason": "dirt", "comment": "polvo"})
    assert resp.status_code == 200, resp.text
    _decide(fake, row, [_approve(a)])
    _poll(db_session, fake)
    assert _refresh(db_session, a).status == "approved"
    assert [h[:2] for h in _history(db_session, a)][-3:] == [
        ("in_review", "rejected"), ("rejected", "in_review"), ("in_review", "approved")]


@pytest.mark.unit
def test_a_local_rejection_is_replaced_by_fisquas(client, db_session, handed):
    from app.models.record import RecordRejection
    vol, (a, b), row, fake = handed
    _as(client, "reviewer").post(f"/records/{a.id}/reject", json={"predefined_reason": "dirt"})
    _decide(fake, row, [_reject(a, reason="focus", note="desenfocado")])
    _poll(db_session, fake)
    assert _refresh(db_session, a).status == "rejected"
    latest = (db_session.query(RecordRejection).filter(RecordRejection.record_id == a.id)
              .order_by(RecordRejection.id.desc()).first())
    assert (latest.predefined_reason, latest.rejected_by) == ("focus", "Fisqua: Ana Gómez")


# ---------------------------------------------------------------------------
# Rotations
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_rotation_correction_is_recorded_on_the_image_and_shown(client, db_session, handed):
    vol, (a, b), row, fake = handed
    left = next(i for i in a.images if i.role == "left")
    right = next(i for i in a.images if i.role == "right")
    before = {p.name: p.read_bytes() for p in (vol.root / "captures").iterdir()}
    _decide(fake, row, [_approve(a, rotations=[{"captureId": left.capture_id, "role": "left", "quarterTurns": 3}])])
    _poll(db_session, fake)
    images = {i.role: i for i in _refresh(db_session, a).images}
    assert images["left"].rotation_correction_quarter_turns == 3
    assert images["right"].rotation_correction_quarter_turns is None
    assert {p.name: p.read_bytes() for p in (vol.root / "captures").iterdir()} == before  # no file touched
    body = _as(client, "reviewer").get(f"/records/{a.id}").json()
    shown = {i["role"]: i["rotation_correction_quarter_turns"] for i in body["images"]}
    assert shown == {"left": 3, "right": None}
    assert right.id


@pytest.mark.unit
def test_a_later_set_without_the_correction_clears_it(db_session, handed):
    vol, (a, b), row, fake = handed
    left = next(i for i in a.images if i.role == "left")
    _decide(fake, row, [_approve(a, rotations=[{"captureId": left.capture_id, "role": "left", "quarterTurns": 1}])])
    _poll(db_session, fake)
    _decide(fake, row, [_approve(a)], decisions_id="d-2")
    _poll(db_session, fake)
    images = {i.role: i for i in _refresh(db_session, a).images}
    assert images["left"].rotation_correction_quarter_turns is None


@pytest.mark.unit
@pytest.mark.parametrize("bad", [
    {"role": "left", "quarterTurns": 4}, {"role": "left", "quarterTurns": 0}, {"role": "top", "quarterTurns": 1},
    {"role": "left", "quarterTurns": "1"}, {"captureId": "no-such-capture", "role": "left", "quarterTurns": 1},
])
def test_an_unusable_rotation_is_skipped_and_reported(client, db_session, handed, bad):
    vol, (a, b), row, fake = handed
    left = next(i for i in a.images if i.role == "left")
    rotation = {"captureId": left.capture_id, **bad}
    _decide(fake, row, [_approve(a, rotations=[rotation])])
    _poll(db_session, fake)
    assert _refresh(db_session, a).status == "approved"
    assert all(i.rotation_correction_quarter_turns is None for i in _refresh(db_session, a).images)
    state = _decisions_state(client, vol.col)
    assert state["counts"]["rotations_skipped"] == 1


# ---------------------------------------------------------------------------
# Only the current handover, only once
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_set_for_another_manifest_is_stale_and_not_applied(client, db_session, handed):
    vol, (a, b), row, fake = handed
    _decide(fake, row, [_approve(a)])
    fake.decisions["manifestId"] = "an-earlier-manifest"
    _poll(db_session, fake)
    assert _refresh(db_session, a).status == "in_review"
    state = _decisions_state(client, vol.col)
    assert state["stale"] is True and state["decisions_id"] is None
    # Not asked for again while Fisqua offers the same set.
    _poll(db_session, fake)
    assert len(fake.decision_calls()) == 1


@pytest.mark.unit
def test_the_current_manifest_is_the_newest_delivered_one(client, db_session, handed):
    vol, (a, b), row, fake = handed
    c = vol.record(status="captured")
    _handover(client, vol.col)
    _send(db_session, Server(_ok()))
    newest = _rows(db_session, vol.col)[-1]
    assert newest.state == "delivered" and newest.manifest_id != row.manifest_id
    fake.status["manifestId"] = newest.manifest_id
    _decide(fake, row, [_approve(a)])  # for the first manifest
    _poll(db_session, fake)
    assert _refresh(db_session, a).status == "in_review"
    _decide(fake, newest, [_approve(c)], decisions_id="d-2")
    _poll(db_session, fake)
    assert _refresh(db_session, c).status == "approved"


@pytest.mark.unit
def test_the_same_decisions_id_is_applied_once(client, db_session, handed):
    vol, (a, b), row, fake = handed
    _decide(fake, row, [_reject(a)])
    _poll(db_session, fake)
    history = _history(db_session, a)
    _poll(db_session, fake)
    assert len(fake.decision_calls()) == 1
    assert _history(db_session, a) == history
    # Applied again directly, the same id still changes nothing.
    from app.core.fisqua_decisions import apply_decisions
    report = apply_decisions(db_session, vol.col.id, fake.decisions)
    assert report["status"] == "already_applied"
    assert _history(db_session, a) == history


@pytest.mark.unit
def test_a_new_set_that_repeats_a_rejection_adds_no_second_rejection(db_session, handed):
    from app.models.record import RecordRejection
    vol, (a, b), row, fake = handed
    _decide(fake, row, [_reject(a)])
    _poll(db_session, fake)
    _decide(fake, row, [_reject(a), _approve(b)], decisions_id="d-2")
    _poll(db_session, fake)
    assert db_session.query(RecordRejection).filter(RecordRejection.record_id == a.id).count() == 1
    assert _refresh(db_session, b).status == "approved"


# ---------------------------------------------------------------------------
# Skipped and reported
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_other_without_a_note_is_skipped(client, db_session, handed):
    vol, (a, b), row, fake = handed
    _decide(fake, row, [_reject(a, reason="other", note=""), _approve(b)])
    _poll(db_session, fake)
    assert _refresh(db_session, a).status == "in_review"
    assert _refresh(db_session, b).status == "approved"
    state = _decisions_state(client, vol.col)
    assert state["skipped"] == [{"captua_record_id": a.id, "reason": "other_without_note"}]
    assert state["counts"] == {"approved": 1, "rejected": 0, "unchanged": 0, "skipped": 1,
                               "rotations": 0, "rotations_skipped": 0}


@pytest.mark.unit
@pytest.mark.parametrize("decision,why", [
    ({"decision": "rejected", "reason": "smudge", "note": "x"}, "invalid_reason"),
    ({"decision": "rejected", "note": "x"}, "invalid_reason"),
    ({"decision": "maybe"}, "invalid_decision"),
])
def test_an_invalid_decision_is_skipped(client, db_session, handed, decision, why):
    vol, (a, b), row, fake = handed
    _decide(fake, row, [{"captuaRecordId": a.id, "decidedBy": "Ana", "rotations": [], **decision}])
    _poll(db_session, fake)
    assert _refresh(db_session, a).status == "in_review"
    assert _decisions_state(client, vol.col)["skipped"] == [{"captua_record_id": a.id, "reason": why}]


@pytest.mark.unit
@pytest.mark.parametrize("reason", ["framing", "obstruction", "other"])
def test_the_three_reasons_added_in_1_2_are_stored(db_session, handed, reason):
    from app.models.record import RecordRejection
    vol, (a, b), row, fake = handed
    _decide(fake, row, [_reject(a, reason=reason, note="the reason, in words")])
    _poll(db_session, fake)
    assert _refresh(db_session, a).status == "rejected"
    [rej] = db_session.query(RecordRejection).filter(RecordRejection.record_id == a.id).all()
    assert (rej.predefined_reason, rej.comment) == (reason, "the reason, in words")


@pytest.mark.unit
@pytest.mark.parametrize("note", ["", "   ", None])
def test_other_without_a_note_is_skipped_as_the_reject_endpoint_refuses_it(client, db_session, handed, note):
    vol, (a, b), row, fake = handed
    resp = _as(client, "reviewer").post(f"/records/{a.id}/reject",
                                        json={"predefined_reason": "other", "comment": note})
    assert resp.status_code == 422  # the endpoint's own check
    _decide(fake, row, [_reject(a, reason="other", note=note)])
    _poll(db_session, fake)
    assert _refresh(db_session, a).status == "in_review"
    assert _decisions_state(client, vol.col)["skipped"] == [{"captua_record_id": a.id, "reason": "other_without_note"}]


@pytest.mark.unit
def test_the_decisions_path_uses_the_units_reason_list():
    import app.core.fisqua_contract as fc
    assert not hasattr(fc, "REJECTION_REASONS")


@pytest.mark.unit
def test_a_record_taken_back_is_skipped(client, db_session, handed):
    vol, (a, b), row, fake = handed
    _take_back(client, vol.col)
    _decide(fake, row, [_approve(a)])
    _poll(db_session, fake)
    assert _refresh(db_session, a).status == "captured"
    assert _decisions_state(client, vol.col)["skipped"] == [{"captua_record_id": a.id, "reason": "captured"}]


@pytest.mark.unit
@pytest.mark.parametrize("record_id", [999999, "not-a-number", None])
def test_a_record_not_in_the_volume_is_skipped(client, db_session, handed, override_projects_root, record_id):
    vol, (a, b), row, fake = handed
    if record_id == 999999:
        other = Volume(db_session, override_projects_root, stem="xx-test-002", name="Otro", external_id="xx-test-002")
        record_id = other.record(status="in_review").id
    _decide(fake, row, [{"captuaRecordId": record_id, "decision": "approved", "decidedBy": "Ana", "rotations": []}])
    _poll(db_session, fake)
    [skip] = _decisions_state(client, vol.col)["skipped"]
    assert skip["reason"] == "not_in_volume"


@pytest.mark.unit
def test_a_numeric_string_record_id_is_matched(db_session, handed):
    vol, (a, b), row, fake = handed
    _decide(fake, row, [{**_approve(a), "captuaRecordId": str(a.id)}])
    _poll(db_session, fake)
    assert _refresh(db_session, a).status == "approved"


# ---------------------------------------------------------------------------
# What the volume page and the log show
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_offsite_state_shows_the_applied_set(client, db_session, handed):
    vol, (a, b), row, fake = handed
    left = next(i for i in a.images if i.role == "left")
    _decide(fake, row, [_approve(a, rotations=[{"captureId": left.capture_id, "role": "left", "quarterTurns": 1}]),
                        _reject(b)])
    _poll(db_session, fake)
    state = _decisions_state(client, vol.col)
    assert state["decisions_id"] == "d-1"
    assert state["decided_at"].startswith("2026-10-25T10:00:00")
    assert state["applied_at"] is not None
    assert state["stale"] is False
    assert state["counts"] == {"approved": 1, "rejected": 1, "unchanged": 0, "skipped": 0,
                               "rotations": 1, "rotations_skipped": 0}
    assert state["skipped"] == []


@pytest.mark.unit
def test_no_decisions_yet_shows_none(client, db_session, handed):
    vol, (a, b), row, fake = handed
    _poll(db_session, fake)
    assert _decisions_state(client, vol.col) is None
    assert fake.decision_calls() == []


@pytest.mark.unit
def test_applying_a_set_is_logged_with_its_counts(db_session, handed):
    from app.models.system_log import SystemLog
    vol, (a, b), row, fake = handed
    _decide(fake, row, [_approve(a), _reject(b)])
    _poll(db_session, fake)
    [entry] = db_session.query(SystemLog).filter(SystemLog.action == "fisqua_decisions_applied").all()
    assert entry.subject == vol.col.filename_stem
    assert "d-1" in entry.detail and "approved=1" in entry.detail and "rejected=1" in entry.detail


# ---------------------------------------------------------------------------
# Transport rules, hold, states
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_retakes_requested_is_not_final(db_session, handed):
    from app.core import fisqua_contract as fc
    from app.core.fisqua_outbox import volumes_to_poll
    vol, (a, b), row, fake = handed
    assert "retakes_requested" in fc.STATES and "retakes_requested" not in fc.FINAL_STATES
    _decide(fake, row, [_reject(a)])
    _poll(db_session, fake)
    assert vol.col.filename_stem in volumes_to_poll(db_session)


@pytest.mark.unit
def test_the_hold_blocks_the_fetch(db_session, handed, hold):
    vol, (a, b), row, fake = handed
    _decide(fake, row, [_approve(a)])
    hold.touch()
    assert _poll(db_session, fake)["status"] == "held"
    assert fake.requests == []
    assert _refresh(db_session, a).status == "in_review"
    hold.unlink()
    _poll(db_session, fake)
    assert _refresh(db_session, a).status == "approved"


@pytest.mark.unit
@pytest.mark.parametrize("code", [401, 403, 404])
def test_a_configuration_error_on_the_fetch_stops_the_unit(db_session, handed, code):
    from app.core.fisqua_outbox import config_error
    vol, (a, b), row, fake = handed
    _decide(fake, row, [_approve(a)])
    fake.decisions_answer = httpx.Response(code)
    assert _poll(db_session, fake)["status"] == "config_error"
    assert config_error()["status_code"] == code
    assert _refresh(db_session, a).status == "in_review"


@pytest.mark.unit
def test_a_transient_error_on_the_fetch_is_tried_at_the_next_tick(db_session, handed):
    vol, (a, b), row, fake = handed
    _decide(fake, row, [_approve(a)])
    fake.decisions_answer = httpx.Response(503)
    _poll(db_session, fake)
    assert _refresh(db_session, a).status == "in_review"
    fake.decisions_answer = None
    _poll(db_session, fake)
    assert _refresh(db_session, a).status == "approved"


@pytest.mark.unit
def test_the_ref_is_quoted_in_the_path(db_session, handed):
    from app.core.fisqua_http import HttpFisquaTransport
    seen = []
    t = HttpFisquaTransport("https://fisqua.test", TOKEN, client=httpx.Client(
        transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={}))))
    t.get_decisions("a b/c")
    assert seen[0].url.raw_path == b"/api/capture/v1/volumes/a%20b%2Fc/decisions"


@pytest.mark.unit
def test_the_token_never_reaches_the_applied_report(client, db_session, handed, caplog):
    vol, (a, b), row, fake = handed
    caplog.set_level(logging.DEBUG)
    _decide(fake, row, [_reject(a, note=f"bad {TOKEN}"), {"captuaRecordId": TOKEN, "decision": "approved"}])
    _poll(db_session, fake)
    text = json.dumps(_as(client, "admin").get(f"/collections/{vol.col.id}/offsite").json())
    assert TOKEN not in text and TOKEN not in caplog.text
    from app.models.record import RecordRejection
    assert all(TOKEN not in (r.comment or "") for r in db_session.query(RecordRejection).all())
