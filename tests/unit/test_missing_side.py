"""A dual opening with a page never photographed: `records.missing_side`.

AHR volumes digitised in 2022-24 with two cameras have openings where only
one camera fired. app.core.rebuild_volume writes such an opening as a dual
record with its one image and `missing_side` "left" or "right". The record
is pending (app.core.recapture_turn): it is listed in GET /records/to-retake
and its summary with its reason, and counts in the recapture turn. A dual
retake writes both images, supersedes the lone one and clears
`missing_side`. It does not block its volume's promotion: the promotion and
the status routes skip it, and it stays captured on the unit until it is
recaptured.

The hardware is stood in for as tests/unit/test_capture_birth_and_retake.py does.

Run with: python -m pytest tests/unit/test_missing_side.py

Version: 0.3.2
"""

import pytest

from tests.unit.test_recapture_turn import (  # noqa: F401
    COMPLETED, _capture, _capture_dual, _events, _prepared, _record, _rejected, _summary, _volume, fakes,
)
from tests.unit.test_record_status_history import _client_as

FULL_RESCAN = '{"fisqua": {"action": "full-rescan"}}'


def _missing(db_session, volume, sequence, side="left", status="captured"):
    """A dual record with one current image, on the side opposite `side`, as a rebuild writes one."""
    from app.models.record import Record, RecordImage
    present, cam = ("right", 1) if side == "left" else ("left", 0)
    rec = Record(title=f"folio {sequence}", status=status, capture_mode="dual", collection_id=volume.id,
                 sequence=sequence, missing_side=side)
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)
    name = f"{volume.filename_stem}-img_{sequence:04d}_c{cam}.jpg"
    db_session.add(RecordImage(record_id=rec.id, filename=name, file_path=f"/{name}", format="jpg",
                               role=present, is_current=True, pair_id=f"{volume.filename_stem}-img_{sequence:04d}"))
    db_session.commit()
    db_session.refresh(rec)
    return rec


def _fresh(db_session, model, id_):
    db_session.expire_all()
    return db_session.get(model, id_)


# ---------------------------------------------------------------------------
# Pending
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("side", ["left", "right"])
def test_a_missing_side_record_is_pending(db_session, test_project, side):
    from app.core import recapture_turn
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    _record(db_session, a, "captured", 1)
    rec = _missing(db_session, a, 2, side)

    assert recapture_turn.record_is_pending(rec) is True
    assert recapture_turn.volume_pending(db_session, a.id) == 1
    assert recapture_turn.pending_records(db_session) == 1
    ids = [rid for (rid,) in db_session.query(Record.id).filter(recapture_turn.pending_clause())]
    assert ids == [rec.id]


@pytest.mark.unit
def test_an_excluded_missing_side_record_is_not_pending(db_session, test_project):
    from app.core import recapture_turn
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rec = _missing(db_session, a, 1, status="excluded")

    assert recapture_turn.record_is_pending(rec) is False
    assert recapture_turn.volume_pending(db_session, a.id) == 0
    assert recapture_turn.pending_records(db_session) == 0
    assert db_session.query(Record.id).filter(recapture_turn.pending_clause()).all() == []


@pytest.mark.unit
def test_a_missing_side_record_keeps_the_turn_on_recaptures(client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    empty = _volume(db_session, test_project, "Tomo N", "co-test-n")
    _missing(db_session, a, 1)
    api = _client_as(client, "op", "operator")

    body = _summary(api)
    assert body["pendingRecords"] == 1
    assert body["turn"] == {"state": "recaptures", "unlockedBy": None}
    resp = _capture(api, test_project, empty)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "recapture_turn"


# ---------------------------------------------------------------------------
# What the unit reads: the record, to-retake and its summary
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_record_exposes_its_missing_side(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rec = _missing(db_session, a, 1, "right")
    plain = _record(db_session, a, "captured", 2)
    api = _client_as(client, "op", "operator")

    assert api.get(f"/records/{rec.id}").json()["missing_side"] == "right"
    assert api.get(f"/records/{plain.id}").json()["missing_side"] is None


@pytest.mark.unit
def test_to_retake_lists_every_pending_record_with_why(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rejected = _rejected(db_session, a, 3)
    prepared = _prepared(client, db_session, a, 2)
    missing = _missing(db_session, a, 1, "left")
    _record(db_session, a, "captured", 4, rejections=1)  # retaken: not pending
    api = _client_as(client, "op", "operator")

    rows = api.get("/records/to-retake")
    assert rows.status_code == 200, rows.text
    by_id = {r["recordId"]: r for r in rows.json()}
    assert set(by_id) == {rejected.id, prepared.id, missing.id}
    assert {rid: (r["pendingReason"], r["missingSide"]) for rid, r in by_id.items()} == {
        rejected.id: ("rejected", None),
        prepared.id: ("prepared", None),
        missing.id: ("missing_side", "left"),
    }
    # A missing side has no rejection behind it, so it comes after every rejected one.
    assert rows.json()[-1]["recordId"] == missing.id
    assert {k: by_id[missing.id][k] for k in ("reason", "comment", "rejectedBy", "rejectedAt")} == {
        "reason": None, "comment": None, "rejectedBy": None, "rejectedAt": None}
    assert (by_id[missing.id]["sequence"], by_id[missing.id]["collectionId"]) == (1, a.id)
    assert api.get("/records/to-retake/count").json() == {"count": 3}


@pytest.mark.unit
def test_an_excluded_missing_side_record_is_not_in_to_retake(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    _missing(db_session, a, 1, status="excluded")
    api = _client_as(client, "op", "operator")
    assert api.get("/records/to-retake").json() == []
    assert api.get("/records/to-retake/count").json() == {"count": 0}


@pytest.mark.unit
def test_the_summary_lists_it_among_the_pending_and_names_it_as_missing_a_side(
        client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rejected = _rejected(db_session, a, 3)
    missing = _missing(db_session, a, 1, "right")
    _record(db_session, a, "captured", 2)

    body = _summary(_client_as(client, "op", "operator"))

    assert body["pendingRecords"] == 2
    assert body["volumes"] == [{
        "collectionId": a.id, "projectId": test_project.id, "name": "Tomo A",
        "filenameStem": "co-test-a", "pending": 2, "total": 1, "excluded": 0,
        "firstPendingRecordId": missing.id,
        "pendingRecordIds": [missing.id, rejected.id],
        "missingSideRecordIds": [missing.id],
    }]


@pytest.mark.unit
def test_a_volume_whose_only_pending_record_misses_a_side_is_listed(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    missing = _missing(db_session, a, 1)
    [vol] = _summary(_client_as(client, "op", "operator"))["volumes"]
    assert (vol["pending"], vol["total"], vol["pendingRecordIds"], vol["missingSideRecordIds"]) == (
        1, 0, [missing.id], [missing.id])


# ---------------------------------------------------------------------------
# The recapture
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_dual_retake_writes_both_images_supersedes_the_lone_one_and_clears_the_side(
        client, db_session, test_project, fakes):
    from app.models.record import Record, RecordImage
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    b = _volume(db_session, test_project, "Tomo B", "co-test-b")
    rec = _missing(db_session, a, 1)
    [lone] = rec.images
    _rejected(db_session, b, 1)
    api = _client_as(client, "op", "operator")

    resp = _capture_dual(api, test_project, a, record_id=rec.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True and resp.json()["record_id"] == rec.id

    rec = _fresh(db_session, Record, rec.id)
    assert rec.missing_side is None and rec.status == "captured"
    current = sorted((i for i in rec.images if i.is_current), key=lambda i: i.role)
    assert [i.role for i in current] == ["left", "right"]
    assert sorted(i.id for i in current) == sorted(resp.json()["image_ids"])
    old = db_session.get(RecordImage, lone.id)
    assert old.is_current is False and old.superseded_at is not None

    # It was the volume's last pending record.
    events = _events(db_session, COMPLETED)
    assert len(events) == 1 and f"collection_id={a.id} " in events[0].detail
    body = _summary(api)
    assert body["turn"] == {"state": "new", "unlockedBy": {"collectionId": a.id, "name": "Tomo A"}}
    assert [v["collectionId"] for v in body["volumes"]] == [b.id]
    assert api.get(f"/records/{rec.id}").json()["missing_side"] is None


@pytest.mark.unit
def test_a_retake_that_leaves_another_pending_record_writes_no_event(client, db_session, test_project, fakes):
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    first = _missing(db_session, a, 1)
    _missing(db_session, a, 2, "right")
    api = _client_as(client, "op", "operator")

    assert _capture_dual(api, test_project, a, record_id=first.id).status_code == 200
    assert _fresh(db_session, Record, first.id).missing_side is None
    assert _events(db_session, COMPLETED) == []
    assert _summary(api)["volumes"][0]["pendingRecordIds"] != []


@pytest.mark.unit
def test_a_rescans_missing_side_retake_before_its_promotion_earns_no_turn(client, db_session, test_project, fakes):
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    a.custom_attributes = FULL_RESCAN
    db_session.commit()
    rec = _missing(db_session, a, 1)
    api = _client_as(client, "op", "operator")

    assert _capture_dual(api, test_project, a, record_id=rec.id).status_code == 200
    assert _fresh(db_session, Record, rec.id).missing_side is None
    assert _events(db_session, COMPLETED) == []


# ---------------------------------------------------------------------------
# The handover
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_promotion_skips_a_missing_side_record_and_promotes_the_rest(client, db_session, test_project):
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    one = _record(db_session, a, "captured", 1)
    missing = _missing(db_session, a, 2)
    three = _record(db_session, a, "captured", 3)

    resp = _client_as(client, "op", "operator").post(f"/collections/{a.id}/promote")
    assert resp.status_code == 200, resp.text
    assert resp.json()["promoted"] == 2
    assert [_fresh(db_session, Record, r.id).status for r in (one, missing, three)] == [
        "in_review", "captured", "in_review"]
    assert _fresh(db_session, Record, missing.id).missing_side == "left"


@pytest.mark.unit
def test_a_volume_whose_only_captured_record_misses_a_side_promotes_nothing(client, db_session, test_project):
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    missing = _missing(db_session, a, 1)
    resp = _client_as(client, "op", "operator").post(f"/collections/{a.id}/promote")
    assert resp.status_code == 200, resp.text
    assert resp.json()["promoted"] == 0
    assert _fresh(db_session, Record, missing.id).status == "captured"


@pytest.mark.unit
def test_a_rejected_record_beside_a_missing_side_one_still_refuses_the_promotion(
        client, db_session, test_project):
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    _rejected(db_session, a, 1)
    _missing(db_session, a, 2)
    captured = _record(db_session, a, "captured", 3)

    resp = _client_as(client, "op", "operator").post(f"/collections/{a.id}/promote")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "recaptures_pending"
    assert resp.json()["detail"]["pending"] == 1
    assert _fresh(db_session, Record, captured.id).status == "captured"


@pytest.mark.unit
def test_a_prepared_record_beside_a_missing_side_one_still_refuses_the_promotion(
        client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    _prepared(client, db_session, a, 1)
    _missing(db_session, a, 2)
    resp = _client_as(client, "op", "operator").post(f"/collections/{a.id}/promote")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["pending"] == 1


@pytest.mark.unit
def test_the_status_routes_skip_a_missing_side_record(client, db_session, test_project):
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    missing = _missing(db_session, a, 1)
    other = _record(db_session, a, "captured", 2)
    api = _client_as(client, "op", "operator")

    resp = api.patch(f"/records/{missing.id}/status", json={"status": "in_review"})
    assert resp.status_code == 422, resp.text
    assert _fresh(db_session, Record, missing.id).status == "captured"

    resp = api.post("/records/bulk-status", json={"record_ids": [missing.id, other.id], "status": "in_review"})
    assert resp.status_code == 200, resp.text
    assert [r["id"] for r in resp.json()] == [other.id]
    assert _fresh(db_session, Record, missing.id).status == "captured"


@pytest.mark.unit
def test_once_recaptured_it_goes_with_the_next_handover(client, db_session, test_project, fakes):
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    _record(db_session, a, "captured", 1)
    missing = _missing(db_session, a, 2)
    api = _client_as(client, "op", "operator")
    assert api.post(f"/collections/{a.id}/promote").json()["promoted"] == 1

    assert _capture_dual(api, test_project, a, record_id=missing.id).status_code == 200
    resp = api.post(f"/collections/{a.id}/promote")
    assert resp.status_code == 200, resp.text
    assert resp.json()["promoted"] == 1
    assert _fresh(db_session, Record, missing.id).status == "in_review"
