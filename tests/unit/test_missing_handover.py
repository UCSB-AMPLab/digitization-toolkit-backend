"""A volume handed over again after its missing records change (contract 1.9 §10.4).

Inserting placeholders (POST /collections/{id}/records/missing-opening or a
Fisqua request), marking a page missing (POST /records/{id}/missing-page or
a Fisqua missing_page decision) and closing a missing record (POST
/records/{id}/close-missing) each hand the volume over again, in the same
transaction, when it has been handed over before (it has a Fisqua outbox
row) and afterwards holds no captured record other than open missing ones
and no record waiting for a retake. A page marked missing on a record in
review or approved returns it to captured, so it can be captured into; a
closed captured record in a volume handed over before goes to review. Both
moves are written to the record's status history.

Run with: python -m pytest tests/unit/test_missing_handover.py

Version: 0.3.3
"""

import pytest

from tests.unit.test_capture_manifest_export import TENANT, Volume, _as, tenant, vol  # noqa: F401
from tests.unit.test_fisqua_decisions import (  # noqa: F401
    Fisqua, _decide, _missing_page, _poll, _request, hold,
)
from tests.unit.test_fisqua_http import Server, _ok, _send
from tests.unit.test_fisqua_outbox import _handover, _rows, fisqua  # noqa: F401


def _states(db, vol):
    return [r.state for r in _rows(db, vol.col)]


def _placeholder(vol, status="captured", sequence=None):
    from app.models.record import Record
    rec = Record(title="p", collection_id=vol.col.id, status=status, capture_mode="dual",
                 sequence=sequence or len(vol.col.records) + 1, missing_side="both")
    vol.db.add(rec)
    vol.db.commit()
    vol.db.refresh(rec)
    vol.db.refresh(vol.col)
    return rec


def _one_sided(vol, status, role="left"):
    rec = vol.record(status=status, layout=(role,))
    rec.capture_mode = "dual"
    vol.db.commit()
    vol.db.refresh(rec)
    return rec


def _history(db, rec):
    from app.models.record import RecordStatusChange
    db.expire_all()
    return [(h.from_status, h.to_status, h.changed_by) for h in
            db.query(RecordStatusChange).filter(RecordStatusChange.record_id == rec.id)
            .order_by(RecordStatusChange.id)]


def _status(db, rec):
    from app.models.record import Record
    db.expire_all()
    return db.get(Record, rec.id).status


# ---------------------------------------------------------------------------
# Placeholders from the route
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_placeholders_in_a_volume_handed_over_before_hand_it_over_again(client, db_session, vol, fisqua):
    a = vol.record(status="captured")
    _handover(client, vol.col)
    assert _states(db_session, vol) == ["waiting_for_upload"]
    resp = _as(client, "operator").post(f"/collections/{vol.col.id}/records/missing-opening",
                                        json={"afterRecordId": a.id})
    assert resp.status_code == 200, resp.text
    assert _states(db_session, vol) == ["superseded", "waiting_for_upload"]


@pytest.mark.unit
def test_placeholders_in_a_volume_never_handed_over_hand_nothing_over(client, db_session, vol, fisqua):
    a = vol.record(status="captured")
    resp = _as(client, "operator").post(f"/collections/{vol.col.id}/records/missing-opening",
                                        json={"afterRecordId": a.id})
    assert resp.status_code == 200, resp.text
    assert _states(db_session, vol) == []


@pytest.mark.unit
def test_another_captured_record_holds_the_handover_back(client, db_session, vol, fisqua):
    a = vol.record(status="captured")
    _handover(client, vol.col)
    vol.record(status="captured")  # captured after the handover, not yet promoted
    resp = _as(client, "operator").post(f"/collections/{vol.col.id}/records/missing-opening",
                                        json={"afterRecordId": a.id})
    assert resp.status_code == 200, resp.text
    assert _states(db_session, vol) == ["waiting_for_upload"]


@pytest.mark.unit
def test_a_record_waiting_for_a_retake_holds_the_handover_back(client, db_session, vol, fisqua):
    a = vol.record(status="captured")
    _handover(client, vol.col)
    vol.record(status="rejected", rejection=("blur", None))
    resp = _as(client, "operator").post(f"/collections/{vol.col.id}/records/missing-opening",
                                        json={"afterRecordId": a.id})
    assert resp.status_code == 200, resp.text
    assert _states(db_session, vol) == ["waiting_for_upload"]


# ---------------------------------------------------------------------------
# A missing page from the route
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("status", ["in_review", "approved"])
def test_a_missing_page_on_a_reviewed_record_returns_it_to_captured_and_hands_over_again(
        client, db_session, vol, fisqua, status):
    rec = _one_sided(vol, "captured")
    _handover(client, vol.col)
    if status == "approved":
        assert _as(client, "reviewer").patch(f"/records/{rec.id}/status", json={"status": "approved"}).status_code == 200
    resp = _as(client, "operator").post(f"/records/{rec.id}/missing-page", json={"side": "right"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "captured"
    assert _history(db_session, rec)[-1] == (status, "captured", "operator")
    assert _states(db_session, vol) == ["superseded", "waiting_for_upload"]


@pytest.mark.unit
def test_a_missing_page_in_a_volume_never_handed_over_moves_the_status_and_hands_nothing_over(
        client, db_session, vol, fisqua):
    rec = _one_sided(vol, "in_review")
    resp = _as(client, "operator").post(f"/records/{rec.id}/missing-page", json={"side": "right"})
    assert resp.status_code == 200, resp.text
    assert _status(db_session, rec) == "captured"
    assert _history(db_session, rec)[-1] == ("in_review", "captured", "operator")
    assert _states(db_session, vol) == []


@pytest.mark.unit
def test_a_missing_page_on_a_rejected_record_leaves_it_rejected(client, db_session, vol, fisqua):
    rec = _one_sided(vol, "rejected")
    resp = _as(client, "operator").post(f"/records/{rec.id}/missing-page", json={"side": "right"})
    assert resp.status_code == 200, resp.text
    assert _status(db_session, rec) == "rejected"


# ---------------------------------------------------------------------------
# Closing
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_closing_in_a_volume_handed_over_before_sends_the_record_to_review_and_hands_over_again(
        client, db_session, vol, fisqua):
    vol.record(status="captured")
    _handover(client, vol.col)
    hole = _placeholder(vol)
    resp = _as(client, "operator").post(f"/records/{hole.id}/close-missing", json={"reason": "foliation_skip"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "in_review"
    assert _history(db_session, hole)[-1] == ("captured", "in_review", "operator")
    assert _states(db_session, vol) == ["superseded", "waiting_for_upload"]


@pytest.mark.unit
def test_closing_in_a_volume_never_handed_over_leaves_the_status_and_the_promotion_sends_it(
        client, db_session, vol, fisqua):
    vol.record(status="captured")
    hole = _placeholder(vol)
    resp = _as(client, "operator").post(f"/records/{hole.id}/close-missing", json={"reason": "foliation_skip"})
    assert resp.status_code == 200, resp.text
    assert _status(db_session, hole) == "captured"
    assert _states(db_session, vol) == []
    assert _handover(client, vol.col)["promoted"] == 2
    assert _status(db_session, hole) == "in_review"


@pytest.mark.unit
def test_a_rejected_record_closed_stays_rejected_and_holds_the_handover_back(client, db_session, vol, fisqua):
    vol.record(status="captured")
    _handover(client, vol.col)
    rec = _one_sided(vol, "rejected")
    rec.missing_side = "right"
    db_session.commit()
    resp = _as(client, "operator").post(f"/records/{rec.id}/close-missing", json={"reason": "foliation_skip"})
    assert resp.status_code == 200, resp.text
    assert _status(db_session, rec) == "rejected"
    assert _states(db_session, vol) == ["waiting_for_upload"]


# ---------------------------------------------------------------------------
# From Fisqua's decisions
# ---------------------------------------------------------------------------

@pytest.fixture
def delivered(client, db_session, vol, fisqua):
    pair = vol.record(status="captured", sequence=1)
    lone = _one_sided(vol, "captured")
    _handover(client, vol.col)
    _send(db_session, Server(_ok()))
    row = _rows(db_session, vol.col)[0]
    assert row.state == "delivered"
    fake = Fisqua(vol.col.filename_stem)
    fake.status = {"volumeId": "fq-v1", "manifestId": row.manifest_id, "state": "review_pending", "problems": []}
    return vol, (pair, lone), row, fake


@pytest.mark.unit
def test_a_missing_page_decision_returns_the_record_to_captured_and_hands_over_again(db_session, delivered):
    vol, (pair, lone), row, fake = delivered
    _decide(fake, row, [_missing_page(lone, "right")])
    _poll(db_session, fake)
    assert _status(db_session, lone) == "captured"
    assert _history(db_session, lone)[-1] == ("in_review", "captured", "Fisqua: Ana Gómez")
    assert _states(db_session, vol) == ["delivered", "waiting_for_upload"]


@pytest.mark.unit
def test_a_request_hands_the_volume_over_again(db_session, delivered):
    vol, (pair, lone), row, fake = delivered
    _decide(fake, row, [])
    fake.decisions["requests"] = [_request(pair.id)]
    _poll(db_session, fake)
    assert _states(db_session, vol) == ["delivered", "waiting_for_upload"]


@pytest.mark.unit
def test_a_set_with_no_missing_change_hands_nothing_over(db_session, delivered):
    from tests.unit.test_fisqua_decisions import _approve
    vol, (pair, lone), row, fake = delivered
    _decide(fake, row, [_approve(pair)])
    _poll(db_session, fake)
    assert _states(db_session, vol) == ["delivered"]
