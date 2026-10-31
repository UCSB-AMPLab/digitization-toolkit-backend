"""Openings and pages never captured (contract 1.9): placeholders, missing pages, closing, capturing into them.

A placeholder is a dual record with no image, `missing_side` "both",
status captured, in its volume with a sequence. POST
/collections/{id}/records/missing-opening inserts placeholders right after a
record of the volume, shifting the sequence of every later record; POST
/records/{id}/missing-page marks a dual record as missing a page it has no
image for; POST /records/{id}/close-missing closes a missing record without
capturing it. A missing record is pending until it is closed or captured
into. A dual capture into a placeholder fills it in place; a dual capture
with afterRecordId inserts its new record after that record.

The hardware is stood in for as tests/unit/test_capture_birth_and_retake.py does.

Run with: python -m pytest tests/unit/test_missing_openings.py

Version: 0.3.3
"""

import pytest

import capture.service as capture_service

from tests.unit.test_missing_side import _fresh, _missing
from tests.unit.test_recapture_turn import (  # noqa: F401
    COMPLETED, _capture, _capture_dual, _events, _record, _rejected, _summary, _volume, fakes,
)
from tests.unit.test_record_status_history import _client_as


def _mark(api, volume, after, **body):
    return api.post(f"/collections/{volume.id}/records/missing-opening", json={"afterRecordId": after, **body})


def _order(db_session, volume):
    """[(record id, sequence)] of the volume, in sequence order."""
    from app.models.record import Record
    db_session.expire_all()
    return [(r.id, r.sequence) for r in db_session.query(Record)
            .filter(Record.collection_id == volume.id).order_by(Record.sequence, Record.id)]


def _placeholder(db_session, volume, sequence, closed=None, request_id=None):
    """A placeholder as the route writes one: dual, no image, missing both, captured."""
    from app.models.record import Record
    rec = Record(title="P - Dual capture", status="captured", capture_mode="dual", collection_id=volume.id,
                 sequence=sequence, missing_side="both", closed_reason=closed, missing_request_id=request_id,
                 missing_expected="f. 12", missing_note="falta")
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)
    return rec


def _dual_only(db_session, volume, sequence, role="left"):
    """A dual record with one current image, `role`, and no missing side recorded."""
    from app.models.record import Record, RecordImage
    rec = Record(title=f"folio {sequence}", status="captured", capture_mode="dual",
                 collection_id=volume.id, sequence=sequence)
    db_session.add(rec)
    db_session.commit()
    db_session.add(RecordImage(record_id=rec.id, filename=f"{rec.id}.jpg", file_path=f"/{rec.id}.jpg",
                               format="jpg", role=role, is_current=True))
    db_session.commit()
    db_session.refresh(rec)
    return rec


# ---------------------------------------------------------------------------
# Ruling 2 and 4: POST /collections/{id}/records/missing-opening
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_marking_inserts_placeholders_right_after_the_record_and_shifts_the_rest(
        client, db_session, test_project):
    from app.models.record import Record, RecordImage
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    one, two, three = (_record(db_session, a, "captured", n) for n in (1, 2, 3))
    api = _client_as(client, "op", "operator")

    resp = _mark(api, a, one.id, count=2, expected="ff. 3r-4v", note="faltan dos", requestId="req-1")
    assert resp.status_code == 200, resp.text
    made = resp.json()
    assert [r["sequence"] for r in made] == [2, 3]
    for r in made:
        assert (r["status"], r["capture_mode"], r["missing_side"], r["images"]) == ("captured", "dual", "both", [])
        assert (r["missing_expected"], r["missing_note"], r["missing_request_id"]) == ("ff. 3r-4v", "faltan dos", "req-1")
        assert (r["closed_reason"], r["closed_note"]) == (None, None)
        assert r["collection_id"] == a.id and r["created_by"] == "op"
        assert r["title"] == f"{test_project.name} - Dual capture"
    ids = [r["id"] for r in made]
    assert _order(db_session, a) == [(one.id, 1), (ids[0], 2), (ids[1], 3), (two.id, 4), (three.id, 5)]
    assert db_session.query(RecordImage).filter(RecordImage.record_id.in_(ids)).count() == 0
    assert db_session.query(Record).filter(Record.collection_id == a.id).count() == 5


@pytest.mark.unit
def test_with_no_count_one_placeholder_is_made(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    one, two = _record(db_session, a, "captured", 1), _record(db_session, a, "captured", 2)
    resp = _mark(_client_as(client, "op", "admin"), a, one.id, count=None)
    assert resp.status_code == 200, resp.text
    [made] = resp.json()
    assert (made["missing_expected"], made["missing_note"], made["missing_request_id"]) == (None, None, None)
    assert _order(db_session, a) == [(one.id, 1), (made["id"], 2), (two.id, 3)]


@pytest.mark.unit
def test_after_the_last_record_goes_at_the_end(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    one, two = _record(db_session, a, "captured", 1), _record(db_session, a, "captured", 2)
    [made] = _mark(_client_as(client, "op", "operator"), a, two.id).json()
    assert _order(db_session, a) == [(one.id, 1), (two.id, 2), (made["id"], 3)]


@pytest.mark.unit
@pytest.mark.parametrize("status", ["excluded", "approved", "in_review", "rejected"])
def test_after_a_record_of_any_status(client, db_session, test_project, status):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    one = _record(db_session, a, status, 1)
    two = _record(db_session, a, "approved", 2)
    resp = _mark(_client_as(client, "op", "operator"), a, one.id)
    assert resp.status_code == 200, resp.text
    assert _order(db_session, a) == [(one.id, 1), (resp.json()[0]["id"], 2), (two.id, 3)]


@pytest.mark.unit
def test_gaps_in_the_sequence_are_kept(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    one, two = _record(db_session, a, "captured", 1), _record(db_session, a, "captured", 7)
    [made] = _mark(_client_as(client, "op", "operator"), a, one.id).json()
    assert _order(db_session, a) == [(one.id, 1), (made["id"], 2), (two.id, 8)]


@pytest.mark.unit
def test_after_a_record_with_no_sequence_the_volume_is_numbered_first(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    one = _record(db_session, a, "captured", 1)
    loose = _record(db_session, a, "captured", None)
    [made] = _mark(_client_as(client, "op", "operator"), a, loose.id).json()
    assert _order(db_session, a) == [(one.id, 1), (loose.id, 2), (made["id"], 3)]


@pytest.mark.unit
def test_marking_is_logged_with_the_volume_the_record_the_count_the_request_and_the_user(
        client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    one = _record(db_session, a, "captured", 1)
    _mark(_client_as(client, "op", "operator"), a, one.id, count=3, requestId="req-9")
    [event] = _events(db_session, "missing_openings_marked")
    assert event.actor == "op"
    assert f"volume {a.id}" in event.detail
    assert f"after {one.id}" in event.detail
    assert "count=3" in event.detail
    assert "requestId=req-9" in event.detail


@pytest.mark.unit
def test_unknown_volume_or_record_is_404_and_another_volumes_record_is_422(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    b = _volume(db_session, test_project, "Tomo B", "co-test-b")
    one = _record(db_session, a, "captured", 1)
    theirs = _record(db_session, b, "captured", 1)
    api = _client_as(client, "op", "operator")

    assert _mark(api, a, 999999).status_code == 404
    assert api.post("/collections/999999/records/missing-opening", json={"afterRecordId": one.id}).status_code == 404
    assert _mark(api, a, theirs.id).status_code == 422
    assert _order(db_session, a) == [(one.id, 1)]
    assert _order(db_session, b) == [(theirs.id, 1)]


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    {"count": 0}, {"count": 51}, {"count": -1}, {"expected": "x" * 2001}, {"note": "x" * 2001},
    {"requestId": "x" * 65},
])
def test_a_bad_body_is_422_and_changes_nothing(client, db_session, test_project, body):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    one = _record(db_session, a, "captured", 1)
    assert _mark(_client_as(client, "op", "operator"), a, one.id, **body).status_code == 422
    assert _order(db_session, a) == [(one.id, 1)]


@pytest.mark.unit
def test_fifty_is_the_most(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    one = _record(db_session, a, "captured", 1)
    resp = _mark(_client_as(client, "op", "operator"), a, one.id, count=50)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 50


@pytest.mark.unit
def test_a_reviewer_cannot_mark(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    one = _record(db_session, a, "captured", 1)
    assert _mark(_client_as(client, "rev", "reviewer"), a, one.id).status_code == 403


@pytest.mark.unit
def test_marking_inserts_through_the_record_sequence_helper(client, db_session, test_project, monkeypatch):
    from app.core import record_sequence
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    one = _record(db_session, a, "captured", 1)
    calls = []
    real = record_sequence.lock_volume_records

    def spy(db, collection_id):
        calls.append(collection_id)
        return real(db, collection_id)

    monkeypatch.setattr(record_sequence, "lock_volume_records", spy)
    assert _mark(_client_as(client, "op", "operator"), a, one.id).status_code == 200
    assert calls == [a.id]


# ---------------------------------------------------------------------------
# Ruling 3: pending while open, not once closed
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_open_placeholder_is_pending_and_a_closed_one_is_not(db_session, test_project):
    from app.core import recapture_turn
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    open_ = _placeholder(db_session, a, 1)
    closed = _placeholder(db_session, a, 2, closed="foliation_skip")
    side_closed = _missing(db_session, a, 3, "left")
    side_closed.closed_reason = "leaf_missing_from_volume"
    db_session.commit()

    assert recapture_turn.pending_reason(open_) == recapture_turn.MISSING_SIDE
    assert recapture_turn.pending_reason(closed) is None
    assert recapture_turn.pending_reason(side_closed) is None
    ids = [rid for (rid,) in db_session.query(Record.id).filter(recapture_turn.pending_clause())]
    assert ids == [open_.id]
    reasons = dict(db_session.query(Record.id, recapture_turn.pending_reason_clause()).all())
    assert reasons == {open_.id: "missing_side", closed.id: None, side_closed.id: None}
    assert recapture_turn.volume_pending(db_session, a.id) == 1


@pytest.mark.unit
def test_a_closed_record_is_promoted_and_an_open_one_stays(client, db_session, test_project):
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    one = _record(db_session, a, "captured", 1)
    open_ = _placeholder(db_session, a, 2)
    closed = _placeholder(db_session, a, 3, closed="captured_elsewhere")
    side_closed = _missing(db_session, a, 4, "right")
    side_closed.closed_reason = "other"
    side_closed.closed_note = "en otro tomo"
    db_session.commit()

    resp = _client_as(client, "op", "operator").post(f"/collections/{a.id}/promote")
    assert resp.status_code == 200, resp.text
    assert resp.json()["promoted"] == 3
    assert [_fresh(db_session, Record, r.id).status for r in (one, open_, closed, side_closed)] == [
        "in_review", "captured", "in_review", "in_review"]


@pytest.mark.unit
def test_the_status_routes_promote_a_closed_record_with_no_image(client, db_session, test_project):
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    closed = _placeholder(db_session, a, 1, closed="foliation_skip")
    open_ = _placeholder(db_session, a, 2)
    api = _client_as(client, "op", "operator")
    assert api.patch(f"/records/{closed.id}/status", json={"status": "in_review"}).status_code == 200
    assert api.patch(f"/records/{open_.id}/status", json={"status": "in_review"}).status_code == 422
    assert _fresh(db_session, Record, open_.id).status == "captured"


# ---------------------------------------------------------------------------
# Ruling 5: POST /records/{id}/missing-page
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_dual_record_without_a_right_image_is_marked_missing_right(client, db_session, test_project):
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rec = _dual_only(db_session, a, 1, "left")
    resp = _client_as(client, "op", "operator").post(f"/records/{rec.id}/missing-page",
                                                     json={"side": "right", "note": "hoja arrancada"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["missing_side"] == "right"
    rec = _fresh(db_session, Record, rec.id)
    assert (rec.missing_side, rec.missing_note) == ("right", "hoja arrancada")


@pytest.mark.unit
def test_marking_a_page_it_has_an_image_for_is_409(client, db_session, test_project):
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rec = _dual_only(db_session, a, 1, "left")
    resp = _client_as(client, "op", "operator").post(f"/records/{rec.id}/missing-page", json={"side": "left"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "missing_page_has_image"
    assert _fresh(db_session, Record, rec.id).missing_side is None


@pytest.mark.unit
def test_marking_a_single_record_is_409(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rec = _record(db_session, a, "captured", 1)
    resp = _client_as(client, "op", "operator").post(f"/records/{rec.id}/missing-page", json={"side": "left"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "missing_page_not_dual"


@pytest.mark.unit
def test_marking_the_side_already_missing_is_idempotent(client, db_session, test_project):
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rec = _missing(db_session, a, 1, "left")
    resp = _client_as(client, "op", "operator").post(f"/records/{rec.id}/missing-page", json={"side": "left"})
    assert resp.status_code == 200, resp.text
    assert _fresh(db_session, Record, rec.id).missing_side == "left"


@pytest.mark.unit
def test_missing_page_refusals(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rec = _dual_only(db_session, a, 1, "left")
    api = _client_as(client, "op", "operator")
    assert api.post("/records/999999/missing-page", json={"side": "right"}).status_code == 404
    assert api.post(f"/records/{rec.id}/missing-page", json={"side": "both"}).status_code == 422
    assert api.post(f"/records/{rec.id}/missing-page", json={"side": "right", "note": "x" * 2001}).status_code == 422
    assert _client_as(client, "rev", "reviewer").post(
        f"/records/{rec.id}/missing-page", json={"side": "right"}).status_code == 403


# ---------------------------------------------------------------------------
# Ruling 6: POST /records/{id}/close-missing
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("reason", ["foliation_skip", "leaf_missing_from_volume", "captured_elsewhere"])
def test_closing_a_placeholder_records_why_and_it_is_no_longer_pending(client, db_session, test_project, reason):
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rec = _placeholder(db_session, a, 1)
    api = _client_as(client, "op", "operator")

    resp = api.post(f"/records/{rec.id}/close-missing", json={"reason": reason})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["closed_reason"], resp.json()["closed_note"]) == (reason, None)
    rec = _fresh(db_session, Record, rec.id)
    assert (rec.closed_reason, rec.missing_side, rec.status, rec.sequence) == (reason, "both", "captured", 1)
    assert api.get("/records/to-retake").json() == []
    assert api.get("/records/to-retake/count").json() == {"count": 0}
    [event] = _events(db_session, "missing_record_closed")
    assert event.actor == "op" and f"record {rec.id}" in event.detail and reason in event.detail


@pytest.mark.unit
def test_other_needs_a_note(client, db_session, test_project):
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rec = _placeholder(db_session, a, 1)
    api = _client_as(client, "op", "operator")
    assert api.post(f"/records/{rec.id}/close-missing", json={"reason": "other"}).status_code == 422
    assert api.post(f"/records/{rec.id}/close-missing", json={"reason": "other", "note": "  "}).status_code == 422
    assert api.post(f"/records/{rec.id}/close-missing",
                    json={"reason": "other", "note": "x" * 2001}).status_code == 422
    assert _fresh(db_session, Record, rec.id).closed_reason is None
    resp = api.post(f"/records/{rec.id}/close-missing", json={"reason": "other", "note": "foliado doble"})
    assert resp.status_code == 200, resp.text
    assert _fresh(db_session, Record, rec.id).closed_note == "foliado doble"


@pytest.mark.unit
def test_closing_refusals(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    plain = _record(db_session, a, "captured", 1)
    closed = _placeholder(db_session, a, 2, closed="foliation_skip")
    side = _missing(db_session, a, 3, "left")
    api = _client_as(client, "op", "operator")

    resp = api.post(f"/records/{plain.id}/close-missing", json={"reason": "foliation_skip"})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "not_missing"
    resp = api.post(f"/records/{closed.id}/close-missing", json={"reason": "captured_elsewhere"})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "already_closed"
    assert api.post(f"/records/{side.id}/close-missing", json={"reason": "lost"}).status_code == 422
    assert api.post("/records/999999/close-missing", json={"reason": "foliation_skip"}).status_code == 404
    assert _client_as(client, "rev", "reviewer").post(
        f"/records/{side.id}/close-missing", json={"reason": "foliation_skip"}).status_code == 403
    # A record missing one side closes like a placeholder.
    api = _client_as(client, "op", "operator")
    assert api.post(f"/records/{side.id}/close-missing", json={"reason": "leaf_missing_from_volume"}).status_code == 200


# ---------------------------------------------------------------------------
# Ruling 7: capturing into a placeholder
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_dual_capture_fills_the_placeholder_in_place(client, db_session, test_project, fakes, monkeypatch):
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    a.capture_counter = 7
    db_session.commit()
    one = _record(db_session, a, "captured", 1)
    rec = _placeholder(db_session, a, 2, request_id="req-1")
    three = _record(db_session, a, "captured", 3)
    seen = []
    real = capture_service.dual_capture_image

    def spy(*args, **kwargs):
        seen.append(kwargs["filename_index"])
        return real(*args, **kwargs)

    monkeypatch.setattr(capture_service, "dual_capture_image", spy)
    api = _client_as(client, "op", "operator")

    resp = _capture_dual(api, test_project, a, record_id=rec.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["record_id"] == rec.id
    rec = _fresh(db_session, Record, rec.id)
    assert (rec.missing_side, rec.missing_expected, rec.missing_note) == (None, None, None)
    assert rec.missing_request_id == "req-1"
    assert (rec.sequence, rec.status) == (2, "captured")
    assert sorted(i.role for i in rec.images if i.is_current) == ["left", "right"]
    assert seen == ["co-test-a-img_0008"]
    assert _order(db_session, a) == [(one.id, 1), (rec.id, 2), (three.id, 3)]
    # It was the volume's last open one: a completed recapture, as a retake.
    [event] = _events(db_session, COMPLETED)
    assert f"collection_id={a.id} " in event.detail


@pytest.mark.unit
def test_capturing_into_a_closed_record_clears_the_closing(client, db_session, test_project, fakes):
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rec = _placeholder(db_session, a, 1, closed="foliation_skip")
    rec.closed_note = "n"
    db_session.commit()
    resp = _capture_dual(_client_as(client, "op", "operator"), test_project, a, record_id=rec.id)
    assert resp.status_code == 200, resp.text
    rec = _fresh(db_session, Record, rec.id)
    assert (rec.missing_side, rec.closed_reason, rec.closed_note) == (None, None, None)


@pytest.mark.unit
def test_a_single_capture_into_a_placeholder_is_409(client, db_session, test_project, fakes):
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rec = _placeholder(db_session, a, 1)
    resp = _capture(_client_as(client, "op", "operator"), test_project, a, record_id=rec.id)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "placeholder_needs_dual"
    assert _fresh(db_session, Record, rec.id).missing_side == "both"


@pytest.mark.unit
def test_filling_a_placeholder_is_never_refused_for_the_turn(client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    b = _volume(db_session, test_project, "Tomo B", "co-test-b")
    _rejected(db_session, b, 1)
    rec = _placeholder(db_session, a, 1)
    api = _client_as(client, "op", "operator")
    assert _summary(api)["turn"]["state"] == "recaptures"
    assert _capture_dual(api, test_project, a, record_id=rec.id).status_code == 200


# ---------------------------------------------------------------------------
# Ruling 8: a dual capture after a record
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_dual_capture_after_a_record_is_inserted_there(client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    one, two = _record(db_session, a, "captured", 1), _record(db_session, a, "in_review", 2)
    resp = _capture_dual(_client_as(client, "op", "operator"), test_project, a, afterRecordId=one.id)
    assert resp.status_code == 200, resp.text
    new = resp.json()["record_id"]
    assert _order(db_session, a) == [(one.id, 1), (new, 2), (two.id, 3)]


@pytest.mark.unit
def test_after_a_record_and_a_recapture_together_is_422(client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    one = _record(db_session, a, "captured", 1)
    rec = _placeholder(db_session, a, 2)
    resp = _capture_dual(_client_as(client, "op", "operator"), test_project, a,
                         afterRecordId=one.id, record_id=rec.id)
    assert resp.status_code == 422, resp.text
    assert _order(db_session, a) == [(one.id, 1), (rec.id, 2)]


@pytest.mark.unit
def test_after_an_unknown_or_another_volumes_record(client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    b = _volume(db_session, test_project, "Tomo B", "co-test-b")
    one = _record(db_session, a, "captured", 1)
    theirs = _record(db_session, b, "captured", 1)
    api = _client_as(client, "op", "operator")
    assert _capture_dual(api, test_project, a, afterRecordId=999999).status_code == 404
    assert _capture_dual(api, test_project, a, afterRecordId=theirs.id).status_code == 422
    assert _order(db_session, a) == [(one.id, 1)]


@pytest.mark.unit
def test_a_capture_after_a_record_is_a_capture_into_a_started_volume(client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    b = _volume(db_session, test_project, "Tomo B", "co-test-b")
    _rejected(db_session, b, 1)
    one = _record(db_session, a, "captured", 1)
    api = _client_as(client, "op", "operator")
    assert _summary(api)["turn"]["state"] == "recaptures"
    assert _capture_dual(api, test_project, a, afterRecordId=one.id).status_code == 200
    assert _events(db_session, "new_volume_started") == []


# ---------------------------------------------------------------------------
# Ruling 11: GET /records/to-retake
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_to_retake_carries_what_a_placeholder_expects_and_orders_by_volume_then_sequence(
        client, db_session, test_project):
    from app.models.collection import Collection
    a = _volume(db_session, test_project, "Tomo A", "co-test-b")
    b = _volume(db_session, test_project, "Tomo B", "co-test-a")
    loose = Collection(name="Sin raíz", project_id=test_project.id)
    db_session.add(loose)
    db_session.commit()
    a2 = _rejected(db_session, a, 2)
    a1 = _placeholder(db_session, a, 1, request_id="req-1")
    b5 = _missing(db_session, b, 5, "right")
    b3 = _rejected(db_session, b, 3)
    l1 = _rejected(db_session, loose, 1)
    _placeholder(db_session, a, 4, closed="foliation_skip")  # closed: not listed
    api = _client_as(client, "op", "operator")

    rows = api.get("/records/to-retake").json()
    # By stem (NULLs last), then sequence.
    assert [r["recordId"] for r in rows] == [b3.id, b5.id, a1.id, a2.id, l1.id]
    by_id = {r["recordId"]: r for r in rows}
    assert (by_id[a1.id]["missingExpected"], by_id[a1.id]["missingNote"], by_id[a1.id]["requestId"]) == (
        "f. 12", "falta", "req-1")
    assert by_id[a1.id]["missingSide"] == "both"
    assert (by_id[b3.id]["missingExpected"], by_id[b3.id]["missingNote"], by_id[b3.id]["requestId"]) == (
        None, None, None)
    assert api.get("/records/to-retake/count").json() == {"count": 5}


# ---------------------------------------------------------------------------
# Closing and the recapture turn
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_closing_the_volumes_last_pending_record_completes_its_recaptures(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    b = _volume(db_session, test_project, "Tomo B", "co-test-b")
    _record(db_session, a, "captured", 1)
    rec = _placeholder(db_session, a, 2)
    _rejected(db_session, b, 1)
    api = _client_as(client, "op", "operator")

    resp = api.post(f"/records/{rec.id}/close-missing", json={"reason": "foliation_skip"})
    assert resp.status_code == 200, resp.text
    [event] = _events(db_session, COMPLETED)
    assert f"collection_id={a.id} " in event.detail and event.actor == "op"
    assert _summary(api)["turn"] == {"state": "new", "unlockedBy": {"collectionId": a.id, "name": "Tomo A"}}


@pytest.mark.unit
def test_closing_with_other_pending_records_in_the_volume_writes_nothing(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rec = _placeholder(db_session, a, 1)
    _placeholder(db_session, a, 2)
    _rejected(db_session, a, 3)
    resp = _client_as(client, "op", "operator").post(f"/records/{rec.id}/close-missing",
                                                     json={"reason": "foliation_skip"})
    assert resp.status_code == 200, resp.text
    assert _events(db_session, COMPLETED) == []


@pytest.mark.unit
def test_closing_a_record_that_was_not_pending_writes_nothing(client, db_session, test_project):
    # An excluded missing record is not pending, so its volume had nothing pending before.
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rec = _missing(db_session, a, 1, "left", status="excluded")
    _record(db_session, a, "captured", 2)
    api = _client_as(client, "op", "operator")
    assert api.post(f"/records/{rec.id}/close-missing", json={"reason": "foliation_skip"}).status_code == 200
    assert _events(db_session, COMPLETED) == []


@pytest.mark.unit
def test_closing_a_full_rescans_last_pending_record_before_its_promotion_earns_no_turn(
        client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a", '{"fisqua": {"action": "full-rescan"}}')
    rec = _placeholder(db_session, a, 1)
    api = _client_as(client, "op", "operator")
    assert api.post(f"/records/{rec.id}/close-missing", json={"reason": "foliation_skip"}).status_code == 200
    assert _events(db_session, COMPLETED) == []


@pytest.mark.unit
def test_closing_takes_the_record_then_the_volume_then_the_turn(client, db_session, test_project, monkeypatch):
    from app.api import records as records_api
    from app.core import recapture_turn
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rec = _placeholder(db_session, a, 1)
    order = []
    real_record, real_volume, real_turn = (records_api._locked_record, records_api.locked_volume_query,
                                           recapture_turn.lock_turn)
    monkeypatch.setattr(records_api, "_locked_record",
                        lambda db, rid, *a, **k: (order.append("record"), real_record(db, rid, *a, **k))[1])
    monkeypatch.setattr(records_api, "locked_volume_query",
                        lambda db, cid: (order.append("volume"), real_volume(db, cid))[1])
    monkeypatch.setattr(recapture_turn, "lock_turn", lambda db: (order.append("turn"), real_turn(db))[1])
    assert _client_as(client, "op", "operator").post(
        f"/records/{rec.id}/close-missing", json={"reason": "foliation_skip"}).status_code == 200
    assert order == ["record", "volume", "turn"]
