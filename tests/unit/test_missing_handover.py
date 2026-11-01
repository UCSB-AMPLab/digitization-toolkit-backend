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


# ---------------------------------------------------------------------------
# A missing page on a side that has an image: the image is superseded
# ---------------------------------------------------------------------------

def _manifest_entry(client, vol, rec):
    resp = _as(client, "operator").get(f"/collections/{vol.col.id}/capture-manifest")
    assert resp.status_code == 200, resp.text
    [entry] = [r for r in resp.json()["volumes"][0]["records"] if r["captuaRecordId"] == rec.id]
    return entry


def _current_roles(db, rec):
    from app.models.record import Record
    db.expire_all()
    return sorted(i.role for i in db.get(Record, rec.id).images if i.is_current)


def _superseded(db, rec, role):
    from app.models.record import Record
    db.expire_all()
    [img] = [i for i in db.get(Record, rec.id).images if i.role == role]
    return img.is_current is False and img.superseded_at is not None


@pytest.mark.unit
def test_the_route_supersedes_the_sides_image_and_the_manifest_lists_only_the_other(client, db_session, vol, fisqua):
    other = vol.record(status="captured")
    pair = vol.record(status="captured")
    _handover(client, vol.col)
    resp = _as(client, "operator").post(f"/records/{pair.id}/missing-page", json={"side": "right"})
    assert resp.status_code == 200, resp.text
    assert _superseded(db_session, pair, "right")
    assert _current_roles(db_session, pair) == ["left"]
    assert _status(db_session, pair) == "captured"
    assert _states(db_session, vol) == ["superseded", "waiting_for_upload"]
    entry = _manifest_entry(client, vol, pair)
    assert (entry["status"], entry["missingSide"]) == ("missing", "right")
    assert [i["role"] for i in entry["images"]] == ["left"]
    assert _manifest_entry(client, vol, other)["status"] == "in_review"


@pytest.mark.unit
def test_the_decision_supersedes_the_sides_image_and_hands_over_again(client, db_session, delivered):
    vol, (pair, lone), row, fake = delivered
    _decide(fake, row, [_missing_page(pair, "left")])
    _poll(db_session, fake)
    assert _superseded(db_session, pair, "left")
    assert _current_roles(db_session, pair) == ["right"]
    assert _status(db_session, pair) == "captured"
    assert _states(db_session, vol) == ["delivered", "waiting_for_upload"]
    entry = _manifest_entry(client, vol, pair)
    assert (entry["status"], entry["missingSide"], [i["role"] for i in entry["images"]]) == (
        "missing", "left", ["right"])


@pytest.mark.unit
def test_a_page_marked_on_a_record_missing_the_other_side_leaves_both_and_no_image(client, db_session, delivered):
    from app.models.record import Record
    vol, (pair, lone), row, fake = delivered
    _decide(fake, row, [_missing_page(lone, "right")])
    _poll(db_session, fake)
    # lone (only a left image) now misses right; marking left as well leaves it with nothing.
    resp = _as(client, "operator").post(f"/records/{lone.id}/missing-page", json={"side": "left"})
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert db_session.get(Record, lone.id).missing_side == "both"
    assert _current_roles(db_session, lone) == []


@pytest.mark.unit
def test_a_single_record_is_skipped_and_refused(client, db_session, vol, fisqua):
    single = vol.record(status="captured", layout=("single",))
    vol.record(status="captured")
    _handover(client, vol.col)
    _send(db_session, Server(_ok()))
    row = _rows(db_session, vol.col)[0]
    fake = Fisqua(vol.col.filename_stem)
    fake.status = {"volumeId": "fq-v1", "manifestId": row.manifest_id, "state": "review_pending", "problems": []}
    _decide(fake, row, [_missing_page(single, "left")])
    _poll(db_session, fake)
    state = _as(client, "reviewer").get(f"/collections/{vol.col.id}/offsite").json()["fisqua"]["decisions"]
    assert state["skipped"] == [{"captua_record_id": single.id, "reason": "missing_page_single_record"}]
    resp = _as(client, "operator").post(f"/records/{single.id}/missing-page", json={"side": "left"})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "missing_page_single_record"
    assert _current_roles(db_session, single) == ["single"]
    assert _states(db_session, vol) == ["delivered"]


@pytest.mark.unit
def test_a_side_already_marked_on_a_rejected_record_is_unchanged_and_hands_nothing_over(client, db_session, vol, fisqua):
    vol.record(status="captured")
    rec = _one_sided(vol, "captured")
    rec.missing_side = "right"
    db_session.commit()
    _handover(client, vol.col)
    _send(db_session, Server(_ok()))
    row = _rows(db_session, vol.col)[0]
    assert row.state == "delivered"
    rec.status = "rejected"
    db_session.commit()
    fake = Fisqua(vol.col.filename_stem)
    fake.status = {"volumeId": "fq-v1", "manifestId": row.manifest_id, "state": "review_pending", "problems": []}
    _decide(fake, row, [_missing_page(rec, "right")])
    _poll(db_session, fake)
    counts = _as(client, "reviewer").get(f"/collections/{vol.col.id}/offsite").json()["fisqua"]["decisions"]["counts"]
    assert (counts["missing_pages"], counts["unchanged"], counts["skipped"]) == (0, 1, 0)
    assert _status(db_session, rec) == "rejected"
    assert _current_roles(db_session, rec) == ["left"]
    assert _states(db_session, vol) == ["delivered"]


# ---------------------------------------------------------------------------
# The volume lock, and closing a record that waits for a retake
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_handing_over_again_takes_the_volumes_row_first(client, db_session, vol, fisqua, monkeypatch):
    from app.core import fisqua_outbox, volume_stem
    a = vol.record(status="captured")
    _handover(client, vol.col)
    calls = []
    real = volume_stem.locked_volume_query
    real_handed = fisqua_outbox.handed_over_before

    def spy(db, cid):
        calls.append(("volume", cid))
        return real(db, cid)

    def spy_handed(db, cid):
        calls.append(("check", cid))
        return real_handed(db, cid)

    monkeypatch.setattr(volume_stem, "locked_volume_query", spy)
    monkeypatch.setattr(fisqua_outbox, "handed_over_before", spy_handed)
    calls.clear()
    fisqua_outbox.hand_over_again_after_missing_change(db_session, vol.col.id)
    assert calls[:2] == [("volume", vol.col.id), ("check", vol.col.id)]
    db_session.rollback()
    assert a.id


@pytest.mark.unit
def test_closing_a_record_waiting_for_a_retake_closes_it_and_leaves_its_status(client, db_session, vol, fisqua):
    from app.models.record import Record, RecordRejection
    vol.record(status="captured")
    _handover(client, vol.col)
    rec = _one_sided(vol, "captured")
    rec.missing_side = "right"
    rejection = RecordRejection(record_id=rec.id, predefined_reason="blur")
    db_session.add(rejection)
    db_session.commit()
    for img in rec.images:
        img.rejection_id = rejection.id
    db_session.commit()
    resp = _as(client, "operator").post(f"/records/{rec.id}/close-missing", json={"reason": "foliation_skip"})
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    rec = db_session.get(Record, rec.id)
    assert (rec.closed_reason, rec.status) == ("foliation_skip", "captured")
    assert _states(db_session, vol) == ["waiting_for_upload"]


# ---------------------------------------------------------------------------
# Take-back's lock order; a decision that reopens a closed record
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_take_back_locks_the_volume_before_history_and_the_outbox(client, db_session, vol, fisqua, monkeypatch):
    import app.api.collections as collections_api
    from app.core import fisqua_outbox
    vol.record(status="captured")
    _handover(client, vol.col)
    order = []
    real_volume = collections_api.locked_volume_query
    real_history = collections_api.record_bulk_status_changes
    real_withdraw = fisqua_outbox.withdraw_unsent
    monkeypatch.setattr(collections_api, "locked_volume_query",
                        lambda db, cid: (order.append("volume"), real_volume(db, cid))[1])
    monkeypatch.setattr(collections_api, "record_bulk_status_changes",
                        lambda *a, **k: (order.append("history"), real_history(*a, **k))[1])
    monkeypatch.setattr(fisqua_outbox, "withdraw_unsent",
                        lambda *a, **k: (order.append("outbox"), real_withdraw(*a, **k))[1])
    resp = _as(client, "operator").post(f"/collections/{vol.col.id}/take-back")
    assert resp.status_code == 200, resp.text
    assert resp.json()["taken_back"] == 1
    assert order == ["volume", "history", "outbox"]


@pytest.mark.unit
def test_a_decision_that_reopens_a_closed_record_reports_and_logs_it(client, db_session, vol, fisqua):
    from app.models.system_log import SystemLog
    vol.record(status="captured")
    rec = _one_sided(vol, "captured", role="right")
    rec.missing_side, rec.closed_reason, rec.closed_note = "left", "other", "hoja suelta"
    db_session.commit()
    _handover(client, vol.col)
    assert _status(db_session, rec) == "in_review"
    _send(db_session, Server(_ok()))
    row = _rows(db_session, vol.col)[0]
    fake = Fisqua(vol.col.filename_stem)
    fake.status = {"volumeId": "fq-v1", "manifestId": row.manifest_id, "state": "review_pending", "problems": []}
    _decide(fake, row, [_missing_page(rec, "right")])
    _poll(db_session, fake)

    state = _as(client, "reviewer").get(f"/collections/{vol.col.id}/offsite").json()["fisqua"]["decisions"]
    assert state["reopened"] == [{"recordId": rec.id, "reason": "other", "note": "hoja suelta"}]
    assert state["counts"]["reopened"] == 1
    db_session.expire_all()
    [event] = db_session.query(SystemLog).filter(SystemLog.action == "missing_record_reopened").all()
    assert event.actor == "Fisqua: Ana Gómez" and f"record {rec.id}" in event.detail and "other" in event.detail
    [applied] = db_session.query(SystemLog).filter(SystemLog.action == "fisqua_decisions_applied").all()
    assert "reopened=1" in applied.detail


@pytest.mark.unit
def test_promotion_locks_the_volume_before_history_and_the_outbox(client, db_session, vol, fisqua, monkeypatch):
    from app.api import records as records_api
    from app.core import fisqua_outbox, volume_stem
    vol.record(status="captured")
    vol.record(status="captured")
    order = []
    real_volume = volume_stem.locked_volume_query
    real_status = records_api.change_record_status
    real_enqueue = fisqua_outbox.enqueue_handover
    monkeypatch.setattr(volume_stem, "locked_volume_query",
                        lambda db, cid: (order.append("volume"), real_volume(db, cid))[1])
    monkeypatch.setattr(records_api, "change_record_status",
                        lambda *a, **k: (order.append("history"), real_status(*a, **k))[1])
    monkeypatch.setattr(fisqua_outbox, "enqueue_handover",
                        lambda *a, **k: (order.append("outbox"), real_enqueue(*a, **k))[1])
    assert _handover(client, vol.col)["promoted"] == 2
    assert order == ["volume", "history", "history", "outbox"]
