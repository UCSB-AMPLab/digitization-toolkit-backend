"""The recapture turn: GET /records/to-retake/summary, the capture refusal, the two events, and the promotion refusal.

While records are rejected anywhere on the unit, starting a new volume (one
with no records) alternates with finishing recaptures. Finishing every
recapture of one volume writes `recapture_volume_completed` and opens the
turn for one new volume; the first capture into an empty volume writes
`new_volume_started` and the turn returns to recaptures. Turns do not
accumulate. An operator's capture that would start a new volume during the
recaptures turn is refused with a 409; an admin's is not. A full rescan
(fisqua.action "full-rescan") is rework, not a new volume: its first capture
is never refused for the turn and writes no `new_volume_started`, and its
first successful promotion writes `rescan_volume_completed`, which opens the
turn for one new volume as a finished recapture does, once per volume. A
volume with any rejected record cannot be promoted, by any role.

The hardware is stood in for as tests/unit/test_capture_birth_and_retake.py does.

Run with: python -m pytest tests/unit/test_recapture_turn.py

Version: 0.3.2
"""

from datetime import datetime

import pytest

from tests.unit.test_capture_birth_and_retake import _install_capture_fakes
from tests.unit.test_record_status_history import _client_as


COMPLETED = "recapture_volume_completed"
STARTED = "new_volume_started"
RESCAN_COMPLETED = "rescan_volume_completed"


def _volume(db_session, test_project, name, stem, custom_attributes=None):
    from app.models.collection import Collection
    v = Collection(name=name, project_id=test_project.id, filename_stem=stem,
                   custom_attributes=custom_attributes)
    db_session.add(v)
    db_session.commit()
    db_session.refresh(v)
    return v


def _record(db_session, volume, status, sequence, capture_mode="single", rejections=0):
    # sequence may be None: a record with no sequence sorts after every numbered one.
    from app.models.record import Record, RecordImage, RecordRejection
    rec = Record(title=f"folio {sequence}", status=status, capture_mode=capture_mode,
                 collection_id=volume.id, sequence=sequence)
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)
    db_session.add(RecordImage(record_id=rec.id, filename=f"{rec.id}.jpg", file_path=f"/{rec.id}.jpg",
                               format="jpg", role="single", is_current=True))
    for i in range(rejections):
        db_session.add(RecordRejection(record_id=rec.id, predefined_reason="blur",
                                       rejected_by="rev", rejected_at=datetime(2026, 10, 1, 12, i)))
    db_session.commit()
    db_session.refresh(rec)
    return rec


def _rejected(db_session, volume, sequence, capture_mode="single"):
    return _record(db_session, volume, "rejected", sequence, capture_mode, rejections=1)


def _summary(api):
    resp = api.get("/records/to-retake/summary")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _events(db_session, action):
    from app.models.system_log import SystemLog
    db_session.expire_all()
    return db_session.query(SystemLog).filter(SystemLog.action == action).order_by(SystemLog.id).all()


def _capture(api, test_project, volume, **extra):
    payload = {"project_name": test_project.name, "camera_index": 0, "collection_id": volume.id}
    payload.update(extra)
    return api.post("/cameras/capture", json=payload)


def _capture_dual(api, test_project, volume, **extra):
    payload = {"project_name": test_project.name, "collection_id": volume.id}
    payload.update(extra)
    return api.post("/cameras/capture/dual", json=payload)


def _records_in(db_session, volume):
    from app.models.record import Record
    db_session.expire_all()
    return db_session.query(Record).filter(Record.collection_id == volume.id).count()


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    _install_capture_fakes(monkeypatch, tmp_path)


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_summary_needs_authentication(client, db_session):
    assert client.get("/records/to-retake/summary").status_code == 401


@pytest.mark.unit
@pytest.mark.parametrize("role", ["admin", "operator", "reviewer"])
def test_every_role_can_read_the_summary(client, db_session, role):
    api = _client_as(client, "u", role)
    assert api.get("/records/to-retake/summary").status_code == 200


@pytest.mark.unit
def test_summary_shape_counts_and_order(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    b = _volume(db_session, test_project, "Tomo B", "co-test-b")
    c = _volume(db_session, test_project, "Tomo C", "co-test-c")
    d = _volume(db_session, test_project, "Tomo 0", "co-test-0")
    # A: two pending of three ever rejected; the lowest sequence is the first pending.
    _rejected(db_session, a, 7)
    first_a = _rejected(db_session, a, 3)
    _record(db_session, a, "captured", 1, rejections=2)   # retaken: counts in total only
    _record(db_session, a, "captured", 2)                 # never rejected
    # B: three pending, listed first.
    # Created out of sequence order, so the list's order is the sequence's, not the id's.
    b4 = _rejected(db_session, b, 4)
    first_b = _rejected(db_session, b, 1)
    b2 = _rejected(db_session, b, 2)
    # C: nothing pending, so not listed.
    _record(db_session, c, "in_review", 1, rejections=1)
    # D: two pending, the same as A, and before it by name.
    first_d = _rejected(db_session, d, 5)
    _rejected(db_session, d, 6)

    body = _summary(_client_as(client, "op", "operator"))

    assert body["pendingRecords"] == 7
    assert [v["collectionId"] for v in body["volumes"]] == [b.id, d.id, a.id]
    assert body["volumes"][0] == {
        "collectionId": b.id, "projectId": test_project.id, "name": "Tomo B",
        "filenameStem": "co-test-b", "pending": 3, "total": 3, "excluded": 0,
        "firstPendingRecordId": first_b.id,
        "pendingRecordIds": [first_b.id, b2.id, b4.id],
        "missingSideRecordIds": [],
    }
    by_id = {v["collectionId"]: v for v in body["volumes"]}
    assert (by_id[a.id]["pending"], by_id[a.id]["total"]) == (2, 3)
    assert by_id[a.id]["firstPendingRecordId"] == first_a.id
    assert by_id[d.id]["firstPendingRecordId"] == first_d.id
    assert body["turn"] == {"state": "recaptures", "unlockedBy": None}


@pytest.mark.unit
def test_summary_is_free_with_nothing_pending(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    _record(db_session, a, "captured", 1, rejections=1)
    body = _summary(_client_as(client, "op", "operator"))
    assert body == {"pendingRecords": 0, "volumes": [], "turn": {"state": "free", "unlockedBy": None}}


# ---------------------------------------------------------------------------
# Turn transitions through the capture endpoints
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_finishing_a_volume_opens_the_new_turn_and_writes_the_event_once(
        client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    b = _volume(db_session, test_project, "Tomo B", "co-test-b")
    r1 = _rejected(db_session, a, 1)
    r2 = _rejected(db_session, a, 2)
    _rejected(db_session, b, 1)
    api = _client_as(client, "op", "operator")

    assert _capture(api, test_project, a, record_id=r1.id).status_code == 200
    assert _events(db_session, COMPLETED) == [], "one of two still pending"
    assert _summary(api)["turn"]["state"] == "recaptures"

    assert _capture(api, test_project, a, record_id=r2.id).status_code == 200
    events = _events(db_session, COMPLETED)
    assert len(events) == 1
    assert str(a.id) in events[0].detail and "co-test-a" in events[0].detail

    body = _summary(api)
    assert body["turn"] == {"state": "new", "unlockedBy": {"collectionId": a.id, "name": "Tomo A"}}
    assert [v["collectionId"] for v in body["volumes"]] == [b.id]

    # Retaking a record of the finished volume again (now captured) writes nothing.
    assert _capture(api, test_project, a, record_id=r2.id).status_code == 200
    assert len(_events(db_session, COMPLETED)) == 1


@pytest.mark.unit
def test_a_dual_retake_that_finishes_a_volume_writes_the_event(
        client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    b = _volume(db_session, test_project, "Tomo B", "co-test-b")
    r = _rejected(db_session, a, 1, capture_mode="dual")
    _rejected(db_session, b, 1)
    api = _client_as(client, "op", "operator")

    assert _capture_dual(api, test_project, a, record_id=r.id).status_code == 200
    assert len(_events(db_session, COMPLETED)) == 1
    assert _summary(api)["turn"]["state"] == "new"


@pytest.mark.unit
def test_starting_a_new_volume_spends_the_turn(client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    b = _volume(db_session, test_project, "Tomo B", "co-test-b")
    empty1 = _volume(db_session, test_project, "Tomo N1", "co-test-n1")
    empty2 = _volume(db_session, test_project, "Tomo N2", "co-test-n2")
    r = _rejected(db_session, a, 1)
    _rejected(db_session, b, 1)
    api = _client_as(client, "op", "operator")

    assert _capture(api, test_project, a, record_id=r.id).status_code == 200
    assert _summary(api)["turn"]["state"] == "new"

    resp = _capture(api, test_project, empty1)
    assert resp.status_code == 200, resp.text
    started = _events(db_session, STARTED)
    assert len(started) == 1
    assert str(empty1.id) in started[0].detail and "co-test-n1" in started[0].detail
    assert _summary(api)["turn"] == {"state": "recaptures", "unlockedBy": None}

    # The new volume now has a record: capturing into it is never refused, and writes no second event.
    assert _capture(api, test_project, empty1).status_code == 200
    assert len(_events(db_session, STARTED)) == 1

    # A second new volume waits for the next completed volume.
    resp = _capture(api, test_project, empty2)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "recapture_turn"
    assert _records_in(db_session, empty2) == 0


@pytest.mark.unit
def test_turns_do_not_accumulate(client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    b = _volume(db_session, test_project, "Tomo B", "co-test-b")
    c = _volume(db_session, test_project, "Tomo C", "co-test-c")
    empty1 = _volume(db_session, test_project, "Tomo N1", "co-test-n1")
    empty2 = _volume(db_session, test_project, "Tomo N2", "co-test-n2")
    ra = _rejected(db_session, a, 1)
    rb = _rejected(db_session, b, 1)
    _rejected(db_session, c, 1)
    api = _client_as(client, "op", "operator")

    assert _capture(api, test_project, a, record_id=ra.id).status_code == 200
    assert _capture(api, test_project, b, record_id=rb.id).status_code == 200
    assert len(_events(db_session, COMPLETED)) == 2
    body = _summary(api)
    assert body["turn"] == {"state": "new", "unlockedBy": {"collectionId": b.id, "name": "Tomo B"}}

    assert _capture(api, test_project, empty1).status_code == 200
    assert _summary(api)["turn"]["state"] == "recaptures"
    resp = _capture(api, test_project, empty2)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "recapture_turn"


# ---------------------------------------------------------------------------
# The capture refusal
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("dual", [False, True])
def test_an_operator_cannot_start_a_new_volume_in_the_recaptures_turn(
        client, db_session, test_project, fakes, dual):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    empty = _volume(db_session, test_project, "Tomo N", "co-test-n")
    _rejected(db_session, a, 1)
    api = _client_as(client, "op", "operator")

    resp = (_capture_dual if dual else _capture)(api, test_project, empty)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "recapture_turn"
    assert isinstance(detail["message"], str) and detail["message"]
    assert _records_in(db_session, empty) == 0
    assert _events(db_session, STARTED) == []


@pytest.mark.unit
@pytest.mark.parametrize("dual", [False, True])
def test_an_admin_may_start_a_new_volume_in_the_recaptures_turn(
        client, db_session, test_project, fakes, dual):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    empty = _volume(db_session, test_project, "Tomo N", "co-test-n")
    _rejected(db_session, a, 1)
    api = _client_as(client, "boss", "admin")

    resp = (_capture_dual if dual else _capture)(api, test_project, empty)
    assert resp.status_code == 200, resp.text
    assert _records_in(db_session, empty) == 1
    assert len(_events(db_session, STARTED)) == 1
    assert _summary(api)["turn"]["state"] == "recaptures"


@pytest.mark.unit
def test_retakes_and_captures_into_started_volumes_are_never_refused(
        client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    started = _volume(db_session, test_project, "Tomo S", "co-test-s")
    r1 = _rejected(db_session, a, 1)
    _rejected(db_session, a, 2)
    captured = _record(db_session, a, "captured", 3)
    _record(db_session, started, "captured", 1)
    api = _client_as(client, "op", "operator")
    assert _summary(api)["turn"]["state"] == "recaptures"

    assert _capture(api, test_project, a, record_id=r1.id).status_code == 200
    assert _capture(api, test_project, a, record_id=captured.id).status_code == 200
    assert _capture(api, test_project, a).status_code == 200
    assert _capture(api, test_project, started).status_code == 200
    assert _capture_dual(api, test_project, started).status_code == 200
    assert _events(db_session, STARTED) == []
    assert _events(db_session, COMPLETED) == []


@pytest.mark.unit
def test_with_nothing_pending_a_new_volume_is_free_and_still_logged(
        client, db_session, test_project, fakes):
    empty = _volume(db_session, test_project, "Tomo N", "co-test-n")
    api = _client_as(client, "op", "operator")
    assert _capture(api, test_project, empty).status_code == 200
    assert len(_events(db_session, STARTED)) == 1
    assert _summary(api)["turn"]["state"] == "free"


# ---------------------------------------------------------------------------
# Full rescans: rework, outside the turn
# ---------------------------------------------------------------------------

def _with_action(action):
    import json
    return json.dumps({"fisqua": {"action": action, "notes": None}})


@pytest.mark.unit
@pytest.mark.parametrize("dual", [False, True])
def test_an_operator_may_start_a_full_rescan_in_the_recaptures_turn(
        client, db_session, test_project, fakes, dual):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rescan = _volume(db_session, test_project, "Tomo R", "co-test-r", _with_action("full-rescan"))
    _rejected(db_session, a, 1)
    api = _client_as(client, "op", "operator")
    before = _summary(api)["turn"]
    assert before == {"state": "recaptures", "unlockedBy": None}

    resp = (_capture_dual if dual else _capture)(api, test_project, rescan)
    assert resp.status_code == 200, resp.text
    assert _records_in(db_session, rescan) == 1
    assert _events(db_session, STARTED) == []
    assert _summary(api)["turn"] == before


@pytest.mark.unit
@pytest.mark.parametrize("custom_attributes", [
    _with_action("full-digitisation"),
    _with_action("spot-recapture"),
    _with_action("complete"),
    _with_action("Full-Rescan"),
    _with_action(None),
    '{"fisqua": {"notes": null}}',
    '{"fisqua": "full-rescan"}',
    '{"action": "full-rescan"}',
    '["full-rescan"]',
    "{not json",
    "",
    None,
], ids=["full-digitisation", "spot-recapture", "complete", "other-case", "null-action",
        "no-action", "fisqua-not-object", "action-outside-fisqua", "not-object", "unparsable",
        "empty", "none"])
def test_any_other_empty_volume_is_still_refused_in_the_recaptures_turn(
        client, db_session, test_project, fakes, custom_attributes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    empty = _volume(db_session, test_project, "Tomo N", "co-test-n", custom_attributes)
    _rejected(db_session, a, 1)
    api = _client_as(client, "op", "operator")

    resp = _capture(api, test_project, empty)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "recapture_turn"
    assert _records_in(db_session, empty) == 0
    assert _events(db_session, STARTED) == []


@pytest.mark.unit
def test_a_full_rescan_leaves_the_new_turn_open(client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    b = _volume(db_session, test_project, "Tomo B", "co-test-b")
    rescan = _volume(db_session, test_project, "Tomo R", "co-test-r", _with_action("full-rescan"))
    empty = _volume(db_session, test_project, "Tomo N", "co-test-n")
    r = _rejected(db_session, a, 1)
    _rejected(db_session, b, 1)
    api = _client_as(client, "op", "operator")

    assert _capture(api, test_project, a, record_id=r.id).status_code == 200
    opened = {"state": "new", "unlockedBy": {"collectionId": a.id, "name": "Tomo A"}}
    assert _summary(api)["turn"] == opened

    assert _capture(api, test_project, rescan).status_code == 200
    assert _events(db_session, STARTED) == []
    assert _summary(api)["turn"] == opened

    # The turn is still there for a new volume.
    assert _capture(api, test_project, empty).status_code == 200
    assert len(_events(db_session, STARTED)) == 1
    assert _summary(api)["turn"] == {"state": "recaptures", "unlockedBy": None}


@pytest.mark.unit
def test_an_admins_full_rescan_writes_no_start_either(client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rescan = _volume(db_session, test_project, "Tomo R", "co-test-r", _with_action("full-rescan"))
    _rejected(db_session, a, 1)
    api = _client_as(client, "boss", "admin")
    assert _capture(api, test_project, rescan).status_code == 200
    assert _events(db_session, STARTED) == []
    assert _summary(api)["turn"]["state"] == "recaptures"


@pytest.mark.unit
def test_a_full_rescan_with_nothing_pending_writes_no_start(client, db_session, test_project, fakes):
    rescan = _volume(db_session, test_project, "Tomo R", "co-test-r", _with_action("full-rescan"))
    api = _client_as(client, "op", "operator")
    assert _capture(api, test_project, rescan).status_code == 200
    assert _events(db_session, STARTED) == []
    assert _summary(api)["turn"]["state"] == "free"


@pytest.mark.unit
@pytest.mark.parametrize("custom_attributes,expected", [
    (_with_action("full-rescan"), True),
    ('{"fisqua": {"action": "full-rescan"}, "other": 1}', True),
    (_with_action("full-digitisation"), False),
    (_with_action("full-rescan "), False),
    ('{"fisqua": {"action": ["full-rescan"]}}', False),
    ('{"fisqua": "full-rescan"}', False),
    ('"full-rescan"', False),
    ("{not json", False),
    ("", False),
    (None, False),
])
def test_volume_is_full_rescan(custom_attributes, expected):
    from app.core.recapture_turn import volume_is_full_rescan
    from app.models.collection import Collection
    assert volume_is_full_rescan(Collection(name="x", custom_attributes=custom_attributes)) is expected


@pytest.mark.unit
def test_promoting_a_full_rescan_opens_the_new_turn(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rescan = _volume(db_session, test_project, "Tomo R", "co-test-r", _with_action("full-rescan"))
    _rejected(db_session, a, 1)
    _record(db_session, rescan, "captured", 1)
    _record(db_session, rescan, "captured", 2)
    api = _client_as(client, "op", "operator")
    assert _summary(api)["turn"] == {"state": "recaptures", "unlockedBy": None}

    resp = api.post(f"/collections/{rescan.id}/promote")
    assert resp.status_code == 200, resp.text
    assert resp.json()["promoted"] == 2
    events = _events(db_session, RESCAN_COMPLETED)
    assert len(events) == 1
    assert f"collection_id={rescan.id} " in events[0].detail and "co-test-r" in events[0].detail
    assert events[0].subject == "Tomo R"
    assert _events(db_session, COMPLETED) == []
    body = _summary(api)
    assert body["turn"] == {"state": "new", "unlockedBy": {"collectionId": rescan.id, "name": "Tomo R"}}
    assert [v["collectionId"] for v in body["volumes"]] == [a.id]


@pytest.mark.unit
def test_a_rescan_completion_is_spent_by_the_next_new_volume(client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rescan = _volume(db_session, test_project, "Tomo R", "co-test-r", _with_action("full-rescan"))
    empty1 = _volume(db_session, test_project, "Tomo N1", "co-test-n1")
    empty2 = _volume(db_session, test_project, "Tomo N2", "co-test-n2")
    _rejected(db_session, a, 1)
    _record(db_session, rescan, "captured", 1)
    api = _client_as(client, "op", "operator")

    assert api.post(f"/collections/{rescan.id}/promote").status_code == 200
    assert _summary(api)["turn"]["state"] == "new"
    assert _capture(api, test_project, empty1).status_code == 200
    assert _summary(api)["turn"] == {"state": "recaptures", "unlockedBy": None}
    resp = _capture(api, test_project, empty2)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "recapture_turn"


@pytest.mark.unit
def test_promoting_a_rescan_again_after_a_take_back_opens_no_second_turn(
        client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rescan = _volume(db_session, test_project, "Tomo R", "co-test-r", _with_action("full-rescan"))
    empty = _volume(db_session, test_project, "Tomo N", "co-test-n")
    _rejected(db_session, a, 1)
    _record(db_session, rescan, "captured", 1)
    api = _client_as(client, "op", "operator")

    assert api.post(f"/collections/{rescan.id}/promote").status_code == 200
    assert _capture(api, test_project, empty).status_code == 200
    assert _summary(api)["turn"]["state"] == "recaptures"

    resp = api.post(f"/collections/{rescan.id}/take-back")
    assert resp.status_code == 200, resp.text
    assert resp.json()["taken_back"] == 1
    resp = api.post(f"/collections/{rescan.id}/promote")
    assert resp.status_code == 200, resp.text
    assert resp.json()["promoted"] == 1
    assert len(_events(db_session, RESCAN_COMPLETED)) == 1
    assert _summary(api)["turn"] == {"state": "recaptures", "unlockedBy": None}


@pytest.mark.unit
def test_another_volumes_rescan_event_does_not_count_as_this_ones(client, db_session, test_project):
    from app.models.system_log import SystemLog
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rescan = _volume(db_session, test_project, "Tomo R", "co-test-r", _with_action("full-rescan"))
    _rejected(db_session, a, 1)
    _record(db_session, rescan, "captured", 1)
    # An event for a volume whose id begins with this one's digits.
    db_session.add(SystemLog(level="INFO", category="capture", action=RESCAN_COMPLETED,
                             subject="Other", detail=f"collection_id={rescan.id}0 filename_stem=co-other"))
    db_session.commit()
    api = _client_as(client, "op", "operator")

    assert api.post(f"/collections/{rescan.id}/promote").status_code == 200
    assert len(_events(db_session, RESCAN_COMPLETED)) == 2


@pytest.mark.unit
@pytest.mark.parametrize("custom_attributes", [
    _with_action("full-digitisation"), _with_action("spot-recapture"), None, "{not json"],
    ids=["full-digitisation", "spot-recapture", "none", "unparsable"])
def test_promoting_any_other_volume_records_nothing(client, db_session, test_project, custom_attributes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    other = _volume(db_session, test_project, "Tomo V", "co-test-v", custom_attributes)
    _rejected(db_session, a, 1)
    _record(db_session, other, "captured", 1)
    api = _client_as(client, "op", "operator")

    resp = api.post(f"/collections/{other.id}/promote")
    assert resp.status_code == 200, resp.text
    assert resp.json()["promoted"] == 1
    assert _events(db_session, RESCAN_COMPLETED) == []
    assert _summary(api)["turn"] == {"state": "recaptures", "unlockedBy": None}


@pytest.mark.unit
def test_a_refused_or_empty_rescan_promotion_records_nothing(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rescan = _volume(db_session, test_project, "Tomo R", "co-test-r", _with_action("full-rescan"))
    empty = _volume(db_session, test_project, "Tomo E", "co-test-e", _with_action("full-rescan"))
    _rejected(db_session, a, 1)
    _rejected(db_session, rescan, 1)
    _record(db_session, rescan, "captured", 2)
    api = _client_as(client, "op", "operator")

    resp = api.post(f"/collections/{rescan.id}/promote")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "recaptures_pending"
    resp = api.post(f"/collections/{empty.id}/promote")
    assert resp.status_code == 200, resp.text
    assert resp.json()["promoted"] == 0
    assert _events(db_session, RESCAN_COMPLETED) == []
    assert _summary(api)["turn"]["state"] == "recaptures"


@pytest.mark.unit
def test_a_rescans_retakes_before_its_promotion_earn_no_turn(client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rescan = _volume(db_session, test_project, "Tomo R", "co-test-r", _with_action("full-rescan"))
    empty1 = _volume(db_session, test_project, "Tomo N1", "co-test-n1")
    empty2 = _volume(db_session, test_project, "Tomo N2", "co-test-n2")
    _rejected(db_session, a, 1)
    _record(db_session, rescan, "captured", 1)
    r = _rejected(db_session, rescan, 2)
    api = _client_as(client, "op", "operator")

    assert _capture(api, test_project, rescan, record_id=r.id).status_code == 200
    assert _events(db_session, COMPLETED) == []
    assert _summary(api)["turn"] == {"state": "recaptures", "unlockedBy": None}

    assert api.post(f"/collections/{rescan.id}/promote").status_code == 200
    assert len(_events(db_session, RESCAN_COMPLETED)) == 1
    assert _summary(api)["turn"]["state"] == "new"
    # Exactly one turn: one new volume, then the second waits.
    assert _capture(api, test_project, empty1).status_code == 200
    resp = _capture(api, test_project, empty2)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "recapture_turn"


@pytest.mark.unit
def test_a_rescans_retakes_after_its_promotion_earn_turns(client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rescan = _volume(db_session, test_project, "Tomo R", "co-test-r", _with_action("full-rescan"))
    empty = _volume(db_session, test_project, "Tomo N", "co-test-n")
    _rejected(db_session, a, 1)
    rec = _record(db_session, rescan, "captured", 1)
    api = _client_as(client, "op", "operator")

    assert api.post(f"/collections/{rescan.id}/promote").status_code == 200
    assert _capture(api, test_project, empty).status_code == 200
    assert _summary(api)["turn"]["state"] == "recaptures"

    # A reviewer (or Fisqua) sends a folio back; its retake finishes the volume's recaptures.
    resp = _client_as(client, "rev", "reviewer").post(
        f"/records/{rec.id}/reject", json={"predefined_reason": "blur"})
    assert resp.status_code == 200, resp.text
    api = _client_as(client, "op", "operator")
    assert _capture(api, test_project, rescan, record_id=rec.id).status_code == 200
    events = _events(db_session, COMPLETED)
    assert len(events) == 1
    assert f"collection_id={rescan.id} " in events[0].detail
    assert _summary(api)["turn"] == {"state": "new", "unlockedBy": {"collectionId": rescan.id, "name": "Tomo R"}}


def _spy_locks(monkeypatch, collection_id):
    """Record, in order, each lock of this volume's row and each turn lock."""
    from app.core import recapture_turn, volume_stem
    taken = []
    real_volume, real_turn = volume_stem.locked_volume_query, recapture_turn.lock_turn

    def volume(db, cid):
        if cid == collection_id:
            taken.append("volume")
        return real_volume(db, cid)

    def turn(db):
        taken.append("turn")
        return real_turn(db)

    monkeypatch.setattr(volume_stem, "locked_volume_query", volume)
    monkeypatch.setattr(recapture_turn, "lock_turn", turn)
    return taken


@pytest.mark.unit
def test_a_rescan_promotion_locks_the_volume_before_the_turn(client, db_session, test_project, monkeypatch):
    # PostgreSQL only: the suite's SQLite emits neither lock, so the order is what can be tested.
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rescan = _volume(db_session, test_project, "Tomo R", "co-test-r", _with_action("full-rescan"))
    _rejected(db_session, a, 1)
    _record(db_session, rescan, "captured", 1)
    taken = _spy_locks(monkeypatch, rescan.id)

    assert _client_as(client, "op", "operator").post(f"/collections/{rescan.id}/promote").status_code == 200
    assert "turn" in taken
    assert "volume" in taken[:taken.index("turn")], taken


@pytest.mark.unit
def test_any_other_promotion_takes_neither_lock(client, db_session, test_project, monkeypatch):
    other = _volume(db_session, test_project, "Tomo V", "co-test-v", _with_action("full-digitisation"))
    _record(db_session, other, "captured", 1)
    taken = _spy_locks(monkeypatch, other.id)
    assert _client_as(client, "op", "operator").post(f"/collections/{other.id}/promote").status_code == 200
    assert taken == []


# The record status routes: PATCH /records/{id}/status and POST /records/bulk-status
# can send a rescan to review a record at a time, and award its turn the same way.

def _patch(api, rec, status):
    resp = api.patch(f"/records/{rec.id}/status", json={"status": status})
    assert resp.status_code == 200, resp.text
    return resp


def _bulk(api, recs, status):
    resp = api.post("/records/bulk-status", json={"record_ids": [r.id for r in recs], "status": status})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.unit
def test_a_rescan_sent_to_review_record_by_record_opens_the_turn_once(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rescan = _volume(db_session, test_project, "Tomo R", "co-test-r", _with_action("full-rescan"))
    _rejected(db_session, a, 1)
    r1 = _record(db_session, rescan, "captured", 1)
    r2 = _record(db_session, rescan, "captured", 2)
    api = _client_as(client, "op", "operator")

    _patch(api, r1, "in_review")
    assert _events(db_session, RESCAN_COMPLETED) == [], "one record still captured"
    assert _summary(api)["turn"]["state"] == "recaptures"

    _patch(api, r2, "in_review")
    events = _events(db_session, RESCAN_COMPLETED)
    assert len(events) == 1
    assert f"collection_id={rescan.id} " in events[0].detail
    assert _summary(api)["turn"] == {"state": "new", "unlockedBy": {"collectionId": rescan.id, "name": "Tomo R"}}

    # A reviewer's ruling afterwards writes no second event.
    _patch(_client_as(client, "rev", "reviewer"), r1, "approved")
    assert len(_events(db_session, RESCAN_COMPLETED)) == 1


@pytest.mark.unit
def test_a_rescan_sent_to_review_in_bulk_opens_the_turn_once(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rescan = _volume(db_session, test_project, "Tomo R", "co-test-r", _with_action("full-rescan"))
    _rejected(db_session, a, 1)
    recs = [_record(db_session, rescan, "captured", i) for i in (1, 2, 3)]
    api = _client_as(client, "op", "operator")

    assert len(_bulk(api, recs, "in_review")) == 3
    assert len(_events(db_session, RESCAN_COMPLETED)) == 1
    assert _summary(api)["turn"]["state"] == "new"

    # Taken back and sent again: no second event.
    assert api.post(f"/collections/{rescan.id}/take-back").json()["taken_back"] == 3
    assert len(_bulk(api, recs, "in_review")) == 3
    assert len(_events(db_session, RESCAN_COMPLETED)) == 1


@pytest.mark.unit
def test_a_bulk_move_awards_each_rescan_it_finishes(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    r1 = _volume(db_session, test_project, "Tomo R1", "co-test-r1", _with_action("full-rescan"))
    r2 = _volume(db_session, test_project, "Tomo R2", "co-test-r2", _with_action("full-rescan"))
    r3 = _volume(db_session, test_project, "Tomo R3", "co-test-r3", _with_action("full-rescan"))
    _rejected(db_session, a, 1)
    recs = [_record(db_session, r1, "captured", 1), _record(db_session, r2, "captured", 1),
            _record(db_session, r3, "captured", 1)]
    _record(db_session, r3, "captured", 2)  # r3 keeps a captured record
    api = _client_as(client, "op", "operator")

    _bulk(api, recs, "in_review")
    awarded = {e.detail.split(" ")[0] for e in _events(db_session, RESCAN_COMPLETED)}
    assert awarded == {f"collection_id={r1.id}", f"collection_id={r2.id}"}


@pytest.mark.unit
def test_a_partial_bulk_move_of_a_rescan_records_nothing(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rescan = _volume(db_session, test_project, "Tomo R", "co-test-r", _with_action("full-rescan"))
    _rejected(db_session, a, 1)
    recs = [_record(db_session, rescan, "captured", i) for i in (1, 2, 3)]
    api = _client_as(client, "op", "operator")

    _bulk(api, recs[:2], "in_review")
    assert _events(db_session, RESCAN_COMPLETED) == []
    assert _summary(api)["turn"]["state"] == "recaptures"


def _spy_lock_order(monkeypatch):
    """Record, in order, every volume row lock (by id) and every turn lock."""
    from app.core import recapture_turn, volume_stem
    taken = []
    real_volume, real_turn = volume_stem.locked_volume_query, recapture_turn.lock_turn

    def volume(db, cid):
        taken.append(("volume", cid))
        return real_volume(db, cid)

    def turn(db):
        taken.append("turn")
        return real_turn(db)

    monkeypatch.setattr(volume_stem, "locked_volume_query", volume)
    monkeypatch.setattr(recapture_turn, "lock_turn", turn)
    return taken


@pytest.mark.unit
def test_the_status_routes_lock_a_rescans_volume_before_the_turn(client, db_session, test_project, monkeypatch):
    rescan = _volume(db_session, test_project, "Tomo R", "co-test-r", _with_action("full-rescan"))
    rec = _record(db_session, rescan, "captured", 1)
    api = _client_as(client, "op", "operator")
    taken = _spy_lock_order(monkeypatch)
    _patch(api, rec, "in_review")
    assert taken == [("volume", rescan.id), "turn"], taken


@pytest.mark.unit
def test_a_bulk_move_locks_every_rescan_volume_in_id_order_before_the_turn(
        client, db_session, test_project, monkeypatch):
    r1 = _volume(db_session, test_project, "Tomo R1", "co-test-r1", _with_action("full-rescan"))
    other = _volume(db_session, test_project, "Tomo V", "co-test-v", _with_action("full-digitisation"))
    r2 = _volume(db_session, test_project, "Tomo R2", "co-test-r2", _with_action("full-rescan"))
    # Records created so the record ids run against the volume ids.
    recs = [_record(db_session, r2, "captured", 1), _record(db_session, other, "captured", 1),
            _record(db_session, r1, "captured", 1)]
    api = _client_as(client, "op", "operator")
    taken = _spy_lock_order(monkeypatch)
    _bulk(api, recs, "in_review")
    assert taken == [("volume", r1.id), ("volume", r2.id), "turn"], taken


@pytest.mark.unit
@pytest.mark.parametrize("bulk", [False, True])
def test_the_status_routes_take_neither_lock_for_other_volumes(
        client, db_session, test_project, monkeypatch, bulk):
    other = _volume(db_session, test_project, "Tomo V", "co-test-v", _with_action("full-digitisation"))
    plain = _volume(db_session, test_project, "Tomo P", "co-test-p")
    recs = [_record(db_session, other, "captured", 1), _record(db_session, plain, "captured", 1)]
    api = _client_as(client, "op", "operator")
    taken = _spy_lock_order(monkeypatch)
    if bulk:
        _bulk(api, recs, "in_review")
    else:
        for rec in recs:
            _patch(api, rec, "in_review")
    assert taken == []
    assert _events(db_session, RESCAN_COMPLETED) == []


# ---------------------------------------------------------------------------
# The promotion refusal
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("role", ["operator", "admin"])
def test_a_volume_with_rejected_records_cannot_be_promoted(client, db_session, test_project, role):
    from app.models.record import Record
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    _rejected(db_session, a, 1)
    _rejected(db_session, a, 2)
    captured = _record(db_session, a, "captured", 3)
    api = _client_as(client, "u", role)

    resp = api.post(f"/collections/{a.id}/promote")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "recaptures_pending"
    assert detail["pending"] == 2
    assert isinstance(detail["message"], str) and detail["message"]
    db_session.expire_all()
    assert db_session.get(Record, captured.id).status == "captured"


@pytest.mark.unit
def test_a_volume_with_only_rejected_records_left_is_refused_too(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    _rejected(db_session, a, 1)
    _record(db_session, a, "in_review", 2)
    resp = _client_as(client, "op", "operator").post(f"/collections/{a.id}/promote")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["pending"] == 1


@pytest.mark.unit
def test_a_volume_whose_retakes_are_done_can_be_promoted(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    _record(db_session, a, "captured", 1, rejections=1)
    resp = _client_as(client, "op", "operator").post(f"/collections/{a.id}/promote")
    assert resp.status_code == 200, resp.text
    assert resp.json()["promoted"] == 1


# ---------------------------------------------------------------------------
# A prepared record: rejected, then moved back to captured by PATCH status
# ("prepare for retake"), with the rejected image still current. It is still
# pending until a capture replaces the image.
# ---------------------------------------------------------------------------

def _prepared(client, db_session, volume, sequence):
    """A record rejected through the API and then prepared for retake, its rejected image still current."""
    rec = _record(db_session, volume, "in_review", sequence)
    resp = _client_as(client, "rev", "reviewer").post(
        f"/records/{rec.id}/reject", json={"predefined_reason": "blur"})
    assert resp.status_code == 200, resp.text
    resp = _client_as(client, "op", "operator").patch(
        f"/records/{rec.id}/status", json={"status": "captured"})
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    db_session.refresh(rec)
    assert rec.status == "captured"
    return rec


@pytest.mark.unit
def test_a_prepared_record_is_still_pending(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    _record(db_session, a, "captured", 1)
    prepared = _prepared(client, db_session, a, 2)
    api = _client_as(client, "op", "operator")

    body = _summary(api)
    assert body["pendingRecords"] == 1
    assert body["volumes"] == [{
        "collectionId": a.id, "projectId": test_project.id, "name": "Tomo A",
        "filenameStem": "co-test-a", "pending": 1, "total": 1, "excluded": 0,
        "firstPendingRecordId": prepared.id,
        "pendingRecordIds": [prepared.id],
        "missingSideRecordIds": [],
    }]
    assert body["turn"] == {"state": "recaptures", "unlockedBy": None}
    assert _events(db_session, COMPLETED) == [], "a status change alone never completes a volume"


@pytest.mark.unit
def test_the_first_pending_record_counts_prepared_ones(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    _rejected(db_session, a, 5)
    prepared = _prepared(client, db_session, a, 2)
    body = _summary(_client_as(client, "op", "operator"))
    assert body["volumes"][0]["pending"] == 2
    assert body["volumes"][0]["firstPendingRecordId"] == prepared.id


@pytest.mark.unit
def test_a_prepared_record_blocks_promotion(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    _record(db_session, a, "captured", 1)
    _prepared(client, db_session, a, 2)
    resp = _client_as(client, "op", "operator").post(f"/collections/{a.id}/promote")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "recaptures_pending"
    assert resp.json()["detail"]["pending"] == 1


@pytest.mark.unit
def test_a_prepared_record_keeps_a_new_volume_locked(client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    empty = _volume(db_session, test_project, "Tomo N", "co-test-n")
    _prepared(client, db_session, a, 1)
    resp = _capture(_client_as(client, "op", "operator"), test_project, empty)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "recapture_turn"


@pytest.mark.unit
def test_retaking_a_prepared_record_completes_the_volume_once(client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    b = _volume(db_session, test_project, "Tomo B", "co-test-b")
    prepared = _prepared(client, db_session, a, 1)
    _rejected(db_session, b, 1)
    api = _client_as(client, "op", "operator")

    assert _capture(api, test_project, a, record_id=prepared.id).status_code == 200
    assert len(_events(db_session, COMPLETED)) == 1
    body = _summary(api)
    assert body["turn"] == {"state": "new", "unlockedBy": {"collectionId": a.id, "name": "Tomo A"}}
    assert [v["collectionId"] for v in body["volumes"]] == [b.id]

    # The volume is at zero now: another retake of the same record writes no second event.
    assert _capture(api, test_project, a, record_id=prepared.id).status_code == 200
    assert len(_events(db_session, COMPLETED)) == 1
    assert _client_as(client, "op", "operator").post(f"/collections/{a.id}/promote").status_code == 200


# ---------------------------------------------------------------------------
# The summary reads each volume's counts and first pending record together
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_summary_reads_the_volumes_in_one_statement(client, db_session, test_project):
    """One statement gives every volume its counts and its first pending record,
    so a volume whose pending records are retaken between two queries cannot
    leave a listed volume without a first pending record. Counted as statements
    that read the records table: the volume listing and the unit's total."""
    from sqlalchemy import event as sa_event
    for i in range(3):
        v = _volume(db_session, test_project, f"Tomo {i}", f"co-test-{i}")
        _rejected(db_session, v, 1)
        _rejected(db_session, v, 2)
    api = _client_as(client, "op", "operator")
    engine = db_session.get_bind()
    seen = []

    def count(conn, cursor, statement, parameters, context, executemany):
        if ("FROM records" in statement or "JOIN records" in statement) and "system_logs" not in statement:
            seen.append(statement)

    sa_event.listen(engine, "before_cursor_execute", count)
    try:
        body = _summary(api)
    finally:
        sa_event.remove(engine, "before_cursor_execute", count)
    assert len(body["volumes"]) == 3
    assert len(seen) == 2, seen


# ---------------------------------------------------------------------------
# pendingRecordIds: every pending record of the volume, by sequence then id
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_pending_ids_include_prepared_records_in_sequence_order(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    r7 = _rejected(db_session, a, 7)
    prepared = _prepared(client, db_session, a, 3)
    _record(db_session, a, "captured", 1, rejections=1)   # retaken: not pending
    _record(db_session, a, "captured", 2)                 # never rejected
    r5 = _rejected(db_session, a, 5)
    tie_low = _rejected(db_session, a, 9)
    tie_high = _rejected(db_session, a, 9)                # same sequence: by id
    unsequenced = _rejected(db_session, a, None)          # no sequence: last

    vol = _summary(_client_as(client, "op", "operator"))["volumes"][0]
    assert vol["pendingRecordIds"] == [prepared.id, r5.id, r7.id, tie_low.id, tie_high.id, unsequenced.id]
    assert vol["firstPendingRecordId"] == vol["pendingRecordIds"][0]
    assert vol["pending"] == len(vol["pendingRecordIds"])


@pytest.mark.unit
def test_each_volume_lists_only_its_own_pending_ids(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    b = _volume(db_session, test_project, "Tomo B", "co-test-b")
    a1 = _rejected(db_session, a, 1)
    b1 = _rejected(db_session, b, 1)
    b2 = _prepared(client, db_session, b, 2)
    by_id = {v["collectionId"]: v for v in _summary(_client_as(client, "op", "operator"))["volumes"]}
    assert by_id[a.id]["pendingRecordIds"] == [a1.id]
    assert by_id[b.id]["pendingRecordIds"] == [b1.id, b2.id]


@pytest.mark.unit
def test_the_volume_and_its_ids_go_when_nothing_is_pending(client, db_session, test_project, fakes):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    b = _volume(db_session, test_project, "Tomo B", "co-test-b")
    prepared = _prepared(client, db_session, a, 1)
    r2 = _rejected(db_session, a, 2)
    _rejected(db_session, b, 1)
    api = _client_as(client, "op", "operator")

    assert _capture(api, test_project, a, record_id=prepared.id).status_code == 200
    by_id = {v["collectionId"]: v for v in _summary(api)["volumes"]}
    assert by_id[a.id]["pendingRecordIds"] == [r2.id]
    assert by_id[a.id]["firstPendingRecordId"] == r2.id

    assert _capture(api, test_project, a, record_id=r2.id).status_code == 200
    assert [v["collectionId"] for v in _summary(api)["volumes"]] == [b.id]


# ---------------------------------------------------------------------------
# An excluded record (Fisqua contract 1.6) is never pending: it is not a page
# of the volume, so nothing asks for its retake, even after a rejection.
# ---------------------------------------------------------------------------

def _excluded(db_session, volume, sequence, rejections=1):
    return _record(db_session, volume, "excluded", sequence, rejections=rejections)


@pytest.mark.unit
def test_an_excluded_record_is_not_pending(db_session, test_project):
    from app.core import recapture_turn
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    rec = _excluded(db_session, a, 1)
    # Its image still carries the rejection it had before it was excluded.
    from app.models.record import RecordRejection
    rej = db_session.query(RecordRejection).filter(RecordRejection.record_id == rec.id).first()
    for img in rec.images:
        img.rejection_id = rej.id
    db_session.commit()
    db_session.refresh(rec)
    assert recapture_turn.record_is_pending(rec) is False
    assert recapture_turn.volume_pending(db_session, a.id) == 0
    assert recapture_turn.pending_records(db_session) == 0


@pytest.mark.unit
def test_an_excluded_record_is_not_in_to_retake_or_its_count(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    _excluded(db_session, a, 1)
    waiting = _rejected(db_session, a, 2)
    api = _client_as(client, "op", "operator")
    rows = api.get("/records/to-retake")
    assert rows.status_code == 200, rows.text
    assert [r["recordId"] for r in rows.json()] == [waiting.id]
    assert api.get("/records/to-retake/count").json() == {"count": 1}


@pytest.mark.unit
def test_an_excluded_record_is_not_in_the_summary_or_its_pending_ids(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    b = _volume(db_session, test_project, "Tomo B", "co-test-b")
    _excluded(db_session, a, 1)
    waiting = _rejected(db_session, a, 2)
    _excluded(db_session, b, 1)

    body = _summary(_client_as(client, "op", "operator"))

    assert body["pendingRecords"] == 1
    [vol] = body["volumes"]
    assert vol["collectionId"] == a.id
    assert (vol["pending"], vol["firstPendingRecordId"], vol["pendingRecordIds"]) == (1, waiting.id, [waiting.id])


@pytest.mark.unit
def test_a_unit_with_only_excluded_records_is_free(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    _excluded(db_session, a, 1)
    body = _summary(_client_as(client, "op", "operator"))
    assert (body["pendingRecords"], body["volumes"]) == (0, [])
    assert body["turn"] == {"state": "free", "unlockedBy": None}


@pytest.mark.unit
def test_an_excluded_record_does_not_block_promotion(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    _excluded(db_session, a, 1)
    _record(db_session, a, "captured", 2)
    resp = _client_as(client, "op", "operator").post(f"/collections/{a.id}/promote")
    assert resp.status_code == 200, resp.text
    assert resp.json()["promoted"] == 1


@pytest.mark.unit
def test_the_summary_counts_a_volumes_exclusions_and_leaves_them_out_of_total(client, db_session, test_project):
    a = _volume(db_session, test_project, "Tomo A", "co-test-a")
    b = _volume(db_session, test_project, "Tomo B", "co-test-b")
    waiting = _rejected(db_session, a, 1)
    _excluded(db_session, a, 2, rejections=1)   # rejected before it was excluded
    _excluded(db_session, a, 3, rejections=0)
    _record(db_session, a, "in_review", 4, rejections=1)
    _excluded(db_session, b, 1)                 # exclusions alone do not list a volume

    body = _summary(_client_as(client, "op", "operator"))

    assert body["volumes"] == [{
        "collectionId": a.id, "projectId": test_project.id, "name": "Tomo A",
        "filenameStem": "co-test-a", "pending": 1, "total": 2, "excluded": 2,
        "firstPendingRecordId": waiting.id,
        "pendingRecordIds": [waiting.id],
        "missingSideRecordIds": [],
    }]


@pytest.mark.unit
@pytest.mark.parametrize("route", ["patch", "bulk", "promote"])
def test_a_rescans_row_is_locked_before_any_status_change(client, db_session, test_project, monkeypatch, route):
    # Two requests over one rescan must queue on its row before either writes
    # a history row (whose key share lock on the volume would otherwise have
    # to be upgraded by both). PostgreSQL only; here the order is what can be tested.
    from app.api import collections as collections_api, records as records_api
    rescan = _volume(db_session, test_project, "Tomo R", "co-test-r", _with_action("full-rescan"))
    recs = [_record(db_session, rescan, "captured", 1), _record(db_session, rescan, "captured", 2)]
    api = _client_as(client, "op", "operator")
    taken = _spy_lock_order(monkeypatch)
    for module in (records_api, collections_api):
        real = module._apply_status_change

        def spy(db, rec, status, user, _real=real):
            taken.append(("status", rec.id))
            return _real(db, rec, status, user)

        monkeypatch.setattr(module, "_apply_status_change", spy)
    if route == "patch":
        _patch(api, recs[0], "in_review")
    elif route == "bulk":
        _bulk(api, recs, "in_review")
    else:
        assert api.post(f"/collections/{rescan.id}/promote").status_code == 200
    first_status = next(i for i, t in enumerate(taken) if isinstance(t, tuple) and t[0] == "status")
    assert taken[0] == ("volume", rescan.id), taken
    assert first_status > 0 and taken.index("turn") > first_status, taken
