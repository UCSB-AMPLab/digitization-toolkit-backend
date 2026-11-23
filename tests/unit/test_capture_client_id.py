"""A client's id per capture: a repeated request is answered from what was saved, never photographed again.

A capture request may carry `client_capture_id`, a UUID the client mints.
When the capture is saved, the id is stored with the record and images it
saved, in the same transaction. A request repeating the id, in the same
kind of capture into the same volume and from the same space, is answered
200 from the stored capture with `replayed: true`, without reaching the
camera or changing anything. One that is not the same request (another
kind, volume, record, camera or any other field) is refused 409
`client_capture_id_in_use`, naming nothing of the stored capture; one whose
images are no longer all stored is refused 409 `capture_no_longer_stored`.
A capture that failed stored nothing, so its retry photographs. A retry
that arrives while the original is still running is refused at once, 409
`capture_in_progress`, and waits for nothing. Ids are the space's own: the
real work's and the testing space's never meet. GET
/cameras/capture-requests/{id} answers the stored capture, or
`in_progress`, in the caller's space, and 404 otherwise.

The camera is stood in for as tests/unit/test_testing_space_capture.py
does, and the unit is that file's: a real project and volume with one
real dual capture, one user per role, and the testing account.

    python -m pytest tests/unit/test_capture_client_id.py
"""

import threading
import uuid
from pathlib import Path

import pytest

from tests.unit.test_testing_space_capture import (  # noqa: F401  (fixtures)
    _testing_volume, backend, lookups, unit,
)


def _cid():
    return str(uuid.uuid4())


def _body(unit, cid, volume_id=None, **extra):
    body = {"project_name": unit.real_project.name,
            "collection_id": volume_id if volume_id is not None else unit.real_volume.id, **extra}
    if cid is not None:
        body["client_capture_id"] = cid
    return body


def _single(unit, cid, volume_id=None, caller="operator", **extra):
    return unit.post(caller, "/cameras/capture", _body(unit, cid, volume_id, camera_index=0, **extra))


def _dual(unit, cid, volume_id=None, caller="operator", **extra):
    return unit.post(caller, "/cameras/capture/dual", _body(unit, cid, volume_id, **extra))


def _lookup(unit, cid, caller="operator"):
    return unit.get(caller, f"/cameras/capture-requests/{cid}")


def _ok(resp):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True, resp.text
    return body


def _in_use(resp):
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "client_capture_id_in_use", resp.text
    # Nothing of the stored capture: no record, no image, no path.
    assert set(detail) == {"code", "message"}, resp.text
    assert "record_id" not in resp.text and "image_ids" not in resp.text and "file_path" not in resp.text


def _gone(resp):
    """The lookup of a stored capture whose record or an image has been deleted."""
    assert resp.status_code == 410, resp.text
    assert resp.json() == {"detail": {"code": "capture_no_longer_stored",
                                      "message": "An image of the capture saved with this id has been deleted."}}


def _abandoned(resp):
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "capture_abandoned", resp.text
    assert set(detail) == {"code", "message"}, resp.text


def _stored(unit):
    from app.models.capture_request import StoredCaptureRequest
    unit.db.expire_all()
    return unit.db.query(StoredCaptureRequest).all()


def _second_real_volume(unit):
    from app.models.collection import Collection
    v = Collection(name="Tomo real 2", project_id=unit.real_project.id, filename_stem="xx-real-2")
    unit.db.add(v)
    unit.db.commit()
    unit.db.refresh(v)
    return v


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_repeated_single_capture_is_answered_from_the_stored_one(unit):
    cid = _cid()
    first = _ok(_single(unit, cid))
    assert not first.get("replayed")
    shots = list(unit.backend.requested)
    before = unit.snapshot()

    again = _ok(_single(unit, cid))

    assert again["replayed"] is True
    assert again["record_id"] == first["record_id"]
    assert again["image_ids"] == first["image_ids"]
    assert again["file_path"] == first["file_path"]
    assert unit.backend.requested == shots, "a replay must not reach the camera"
    assert unit.snapshot() == before, "a replay changes no row, no counter, no manifest, no file"


@pytest.mark.unit
def test_a_repeated_dual_capture_is_answered_from_the_stored_one(unit):
    cid = _cid()
    first = _ok(_dual(unit, cid))
    shots = list(unit.backend.requested)
    before = unit.snapshot()

    again = _ok(_dual(unit, cid))

    assert again["replayed"] is True
    assert again["record_id"] == first["record_id"]
    assert again["image_ids"] == first["image_ids"]
    assert again["file_paths"] == first["file_paths"]
    assert unit.backend.requested == shots
    assert unit.snapshot() == before


@pytest.mark.unit
def test_a_replay_answers_the_images_it_saved_after_a_retake_superseded_them(unit):
    from app.models.record import RecordImage
    first_id, retake_id = _cid(), _cid()
    first = _ok(_dual(unit, first_id))
    retake = _ok(_dual(unit, retake_id, record_id=first["record_id"]))
    assert retake["record_id"] == first["record_id"] and retake["image_ids"] != first["image_ids"]
    unit.db.expire_all()
    assert all(unit.db.get(RecordImage, i).is_current is False for i in first["image_ids"])
    shots = list(unit.backend.requested)

    again = _ok(_dual(unit, first_id))
    assert again["replayed"] is True
    assert again["record_id"] == first["record_id"]
    assert again["image_ids"] == first["image_ids"]
    assert again["file_paths"] == first["file_paths"]
    again_retake = _ok(_dual(unit, retake_id, record_id=first["record_id"]))
    assert again_retake["image_ids"] == retake["image_ids"] and again_retake["replayed"] is True
    assert unit.backend.requested == shots
    assert len([r for r in _stored(unit) if r.record_id == first["record_id"]]) == 2


@pytest.mark.unit
def test_a_failed_capture_stores_nothing_and_its_retry_photographs(unit):
    cid = _cid()
    unit.backend.fail_after_write = {0}
    failed = _single(unit, cid)
    assert failed.status_code == 200 and failed.json()["success"] is False, failed.text
    assert _stored(unit) == []
    unit.backend.fail_after_write = set()
    shots = len(unit.backend.requested)

    retry = _ok(_single(unit, cid))
    assert not retry.get("replayed")
    assert len(unit.backend.requested) == shots + 1
    assert [r.client_capture_id for r in _stored(unit)] == [cid]


@pytest.mark.unit
def test_a_request_without_the_id_photographs_every_time(unit):
    shots = len(unit.backend.requested)
    a = _ok(_single(unit, None))
    b = _ok(_single(unit, None))
    assert a["record_id"] != b["record_id"]
    assert len(unit.backend.requested) == shots + 2
    assert not a.get("replayed") and not b.get("replayed")
    assert _stored(unit) == []


@pytest.mark.unit
def test_the_id_is_compared_in_lowercase(unit):
    cid = _cid()
    first = _ok(_single(unit, cid.upper()))
    assert [r.client_capture_id for r in _stored(unit)] == [cid]
    again = _ok(_single(unit, cid))
    assert again["replayed"] is True and again["record_id"] == first["record_id"]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

_GOOD = str(uuid.uuid4())


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "abc", "not-a-uuid-at-all-not-a-uuid-at-all!", _GOOD.replace("-", ""),
                                 "{" + _GOOD + "}", 12345, _GOOD + "\n", " " + _GOOD, _GOOD + " ", "\n" + _GOOD])
def test_a_malformed_id_is_refused_422_before_the_camera(unit, bad):
    shots = list(unit.backend.requested)
    resp = unit.post("operator", "/cameras/capture",
                     {**_body(unit, None, camera_index=0), "client_capture_id": bad})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "client_capture_id"], resp.text
    resp = unit.post("operator", "/cameras/capture/dual", {**_body(unit, None), "client_capture_id": bad})
    assert resp.status_code == 422, resp.text
    assert unit.backend.requested == shots
    assert _stored(unit) == []


@pytest.mark.unit
def test_the_id_of_a_single_capture_is_refused_on_a_dual_one(unit):
    cid = _cid()
    _ok(_single(unit, cid))
    shots = list(unit.backend.requested)
    before = unit.snapshot()
    _in_use(_dual(unit, cid))
    _ok(_single(unit, cid))
    assert unit.backend.requested == shots
    assert unit.snapshot() == before


@pytest.mark.unit
def test_the_id_of_a_capture_is_refused_in_another_volume(unit):
    other = _second_real_volume(unit)
    cid = _cid()
    _ok(_single(unit, cid))
    shots = list(unit.backend.requested)
    before = unit.snapshot()
    _in_use(_single(unit, cid, volume_id=other.id))
    assert unit.backend.requested == shots
    assert unit.snapshot() == before


# ---------------------------------------------------------------------------
# The lookup route
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("dual", [False, True], ids=["single", "dual"])
def test_the_lookup_answers_a_stored_capture_as_a_replay_does(unit, dual):
    cid = _cid()
    capture = _dual if dual else _single
    _ok(capture(unit, cid))
    replay = _ok(capture(unit, cid))
    shots = list(unit.backend.requested)

    for caller in ("operator", "admin"):
        looked = _lookup(unit, cid, caller=caller)
        assert looked.status_code == 200, looked.text
        assert looked.json() == {**replay, "in_progress": False}
    assert unit.backend.requested == shots


@pytest.mark.unit
def test_the_lookup_answers_404_for_an_unknown_id_and_422_for_a_malformed_one(unit):
    resp = _lookup(unit, _cid())
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "not_found"
    assert _lookup(unit, "not-a-uuid").status_code == 422
    good = _cid()
    for bad in (good + "%0A", "%20" + good, good + "%20", good.replace("-", "")):
        assert _lookup(unit, bad).status_code == 422, bad


@pytest.mark.unit
def test_the_lookup_takes_the_capture_routes_roles(unit):
    cid = _cid()
    _ok(_single(unit, cid))
    assert _lookup(unit, cid, caller="reviewer").status_code == 403


@pytest.mark.unit
def test_the_lookup_answers_only_in_the_captures_own_space(unit):
    project, volume = _testing_volume(unit)
    real_id, testing_id = _cid(), _cid()
    _ok(_single(unit, real_id))
    testing = _ok(unit.post("testing", "/cameras/capture", {
        "project_name": project["name"], "collection_id": volume["id"], "camera_index": 0,
        "client_capture_id": testing_id}))

    looked = _lookup(unit, testing_id, caller="testing")
    assert looked.status_code == 200 and looked.json()["record_id"] == testing["record_id"], looked.text
    for caller in ("operator", "admin"):
        resp = _lookup(unit, testing_id, caller=caller)
        assert resp.status_code == 404 and "record_id" not in resp.text, (caller, resp.text)
    assert _lookup(unit, real_id, caller="testing").status_code == 404


# ---------------------------------------------------------------------------
# C. A replay answers only the same request
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_same_id_with_another_record_to_retake_is_refused(unit):
    cid = _cid()
    a = _ok(_dual(unit, cid))
    b = _ok(_dual(unit, None))
    shots = list(unit.backend.requested)
    _in_use(_dual(unit, cid, record_id=b["record_id"]))
    assert unit.backend.requested == shots
    again = _ok(_dual(unit, cid))
    assert again["replayed"] is True and again["record_id"] == a["record_id"]


@pytest.mark.unit
def test_the_same_id_on_a_retake_and_on_a_new_capture_are_two_requests(unit):
    cid = _cid()
    first = _ok(_dual(unit, None))
    _ok(_dual(unit, cid, record_id=first["record_id"]))
    shots = list(unit.backend.requested)
    _in_use(_dual(unit, cid))
    assert unit.backend.requested == shots


@pytest.mark.unit
def test_the_same_id_with_another_insert_position_is_refused(unit):
    a = _ok(_dual(unit, None))
    b = _ok(_dual(unit, None))
    cid = _cid()
    _ok(_dual(unit, cid, afterRecordId=a["record_id"]))
    shots = list(unit.backend.requested)
    _in_use(_dual(unit, cid, afterRecordId=b["record_id"]))
    _in_use(_dual(unit, cid))
    assert unit.backend.requested == shots
    assert _ok(_dual(unit, cid, afterRecordId=a["record_id"]))["replayed"] is True


@pytest.mark.unit
def test_the_same_id_on_another_camera_is_refused(unit):
    cid = _cid()
    _ok(_single(unit, cid))
    shots = list(unit.backend.requested)
    resp = unit.post("operator", "/cameras/capture", {**_body(unit, cid), "camera_index": 1})
    _in_use(resp)
    assert unit.backend.requested == shots


@pytest.mark.unit
def test_the_same_id_with_the_pages_swapped_is_refused(unit):
    cid = _cid()
    _ok(_dual(unit, cid, left_camera_index=0))
    shots = list(unit.backend.requested)
    _in_use(_dual(unit, cid, left_camera_index=1))
    assert unit.backend.requested == shots


@pytest.mark.unit
@pytest.mark.parametrize("field,value", [("rotate_deg_cam0", 90), ("resolution", "high"), ("sequence", 7),
                                         ("record_title", "Otro"), ("stagger_ms", 50)])
def test_the_same_id_with_any_other_field_changed_is_refused(unit, field, value):
    cid = _cid()
    _ok(_dual(unit, cid))
    shots = list(unit.backend.requested)
    _in_use(_dual(unit, cid, **{field: value}))
    assert unit.backend.requested == shots


@pytest.mark.unit
def test_a_request_sending_the_defaults_is_the_same_request(unit):
    cid = _cid()
    first = _ok(_dual(unit, cid))
    again = _ok(_dual(unit, cid, left_camera_index=0, stagger_ms=20, resolution="medium"))
    assert again["replayed"] is True and again["record_id"] == first["record_id"]


@pytest.mark.unit
def test_a_stored_capture_whose_volume_was_deleted_is_not_a_request_naming_no_volume(unit):
    from app.models.capture_request import StoredCaptureRequest
    cid = _cid()
    _ok(_single(unit, cid))
    row = unit.db.query(StoredCaptureRequest).filter_by(client_capture_id=cid).one()
    row.collection_id = None  # what ON DELETE SET NULL leaves
    unit.db.commit()
    shots = list(unit.backend.requested)
    resp = unit.post("operator", "/cameras/capture",
                     {"project_name": unit.real_project.name, "camera_index": 0, "client_capture_id": cid})
    _in_use(resp)
    assert unit.backend.requested == shots


# ---------------------------------------------------------------------------
# D. A capture whose images are gone is not replayed
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("dual", [False, True], ids=["single", "dual"])
def test_a_capture_with_a_deleted_image_is_no_longer_stored(unit, dual):
    """Its lookup answers 410 in its own space; the other space sees 404, as for any id it does not hold."""
    cid = _cid()
    capture = _dual if dual else _single
    first = _ok(capture(unit, cid))
    resp = unit.client.delete(f"/records/images/{first['image_ids'][-1]}", headers=unit.headers["operator"])
    assert resp.status_code == 200, resp.text
    shots = list(unit.backend.requested)

    again = capture(unit, cid)
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["code"] == "capture_no_longer_stored"
    assert set(again.json()["detail"]) == {"code", "message"}
    assert "record_id" not in again.text and "image_ids" not in again.text
    looked = _lookup(unit, cid)
    _gone(looked)
    other = _lookup(unit, cid, caller="testing")
    assert other.status_code == 404 and other.json()["detail"]["code"] == "not_found", other.text
    assert unit.backend.requested == shots


def _foreign_keys_on(unit):
    """Enforce SQLite's foreign keys from here on, as PostgreSQL always does, so ON DELETE acts."""
    import sqlalchemy as sa
    engine = unit.db.get_bind()
    unit.db.commit()  # the session's connection goes back to the pool, which dispose empties
    engine.dispose()

    @sa.event.listens_for(engine, "connect")
    def _on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")


@pytest.mark.unit
@pytest.mark.parametrize("dual", [False, True], ids=["single", "dual"])
def test_a_capture_whose_record_was_deleted_is_no_longer_stored(unit, dual):
    """The stored id outlives its record: the same request is refused, never photographed again."""
    cid = _cid()
    capture = _dual if dual else _single
    first = _ok(capture(unit, cid))
    _foreign_keys_on(unit)
    resp = unit.client.delete(f"/records/{first['record_id']}", headers=unit.headers["operator"])
    assert resp.status_code == 200, resp.text
    [row] = [r for r in _stored(unit) if r.client_capture_id == cid]
    assert row.record_id is None
    shots = list(unit.backend.requested)

    again = capture(unit, cid)
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == {"code": "capture_no_longer_stored",
                                      "message": again.json()["detail"]["message"]}
    looked = _lookup(unit, cid)
    _gone(looked)
    assert unit.backend.requested == shots


@pytest.mark.unit
def test_a_capture_in_a_deleted_testing_volume_is_no_longer_stored(unit):
    """A testing volume's delete removes its records and images in bulk; the stored ids stay, and answer nothing."""
    project, volume = _testing_volume(unit)
    cid = _cid()
    _ok(_testing_single(unit, project, volume, cid))
    _foreign_keys_on(unit)
    resp = unit.client.delete(f"/collections/{volume['id']}", headers=unit.headers["testing"])
    assert resp.status_code == 204, resp.text
    [row] = [r for r in _stored(unit) if r.client_capture_id == cid]
    assert row.record_id is None and row.collection_id is None and row.space == "testing"
    shots = list(unit.backend.requested)

    again = _testing_single(unit, project, volume, cid)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "capture_no_longer_stored", again.text
    _gone(_lookup(unit, cid, caller="testing"))
    assert _lookup(unit, cid, caller="operator").status_code == 404
    assert unit.backend.requested == shots


@pytest.mark.unit
def test_a_capture_whose_image_id_was_taken_by_another_capture_is_no_longer_stored(unit):
    """SQLite reuses the highest rowid of record_images: a deleted image's id can name another record's image."""
    from app.models.record import RecordImage
    _foreign_keys_on(unit)
    cid = _cid()
    first = _ok(_single(unit, cid))
    [image_id] = first["image_ids"]
    resp = unit.client.delete(f"/records/images/{image_id}", headers=unit.headers["operator"])
    assert resp.status_code == 200, resp.text
    other = _ok(_single(unit, None))
    assert other["image_ids"] == [image_id], "the precondition: the id was given to the other capture's image"
    assert other["record_id"] != first["record_id"]
    unit.db.expire_all()
    assert unit.db.get(RecordImage, image_id).record_id == other["record_id"]
    shots = list(unit.backend.requested)

    again = _single(unit, cid)
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["code"] == "capture_no_longer_stored"
    assert str(other["file_path"]) not in again.text
    looked = _lookup(unit, cid)
    _gone(looked)
    assert unit.backend.requested == shots


@pytest.mark.unit
def test_a_capture_whose_images_were_freed_from_the_card_is_still_stored(unit):
    """A freed image keeps its row (app.core.offsite_free): the capture is still answered."""
    from datetime import datetime
    from app.models.record import RecordImage
    cid = _cid()
    first = _ok(_dual(unit, cid))
    for image_id in first["image_ids"]:
        img = unit.db.get(RecordImage, image_id)
        img.master_freed_at, img.master_freed_bytes = datetime(2026, 11, 22), 123
    unit.db.commit()
    again = _ok(_dual(unit, cid))
    assert again["replayed"] is True and again["image_ids"] == first["image_ids"]
    assert _lookup(unit, cid).json()["in_progress"] is False


# ---------------------------------------------------------------------------
# The lookup's order: the in-flight state is read before the table
# ---------------------------------------------------------------------------

def _commits_and_releases_inside_find(monkeypatch, unit, cid, *, after_select):
    """Patch find so the original commits its row and releases its claim during the lookup's select.

    `after_select`: the select runs first and sees no row, then the original
    commits and releases, as a lookup paused just after its select would
    see it. Otherwise the original commits and releases first, and the
    select runs after.
    """
    from app.core import capture_requests
    real = capture_requests.find
    done = []

    def commit_and_release():
        if not done:
            _row_saved_elsewhere(unit, cid, True)
            capture_requests.release(("real", cid))
            done.append(True)

    def find(db, client_capture_id, space):
        if after_select:
            row = real(db, client_capture_id, space)
            commit_and_release()
            return row
        commit_and_release()
        return real(db, client_capture_id, space)

    monkeypatch.setattr(capture_requests, "find", find)


@pytest.mark.unit
def test_a_lookup_paused_after_its_select_never_answers_404_for_a_capture_that_ends_stored(unit, monkeypatch):
    from app.core import capture_requests
    cid = _cid()
    assert capture_requests.claim(("real", cid))
    _commits_and_releases_inside_find(monkeypatch, unit, cid, after_select=True)

    first = _lookup(unit, cid)
    assert first.status_code == 200, first.text
    # The key was not in flight at the lookup's close, and the select after the close finds the row:
    # the key is opened again and the stored capture answered.
    assert first.json()["in_progress"] is False and first.json()["record_id"] == unit.real_record_id, first.text
    second = _lookup(unit, cid)
    assert second.status_code == 200 and second.json()["record_id"] == unit.real_record_id, second.text
    assert not capture_requests.closed(("real", cid))


@pytest.mark.unit
def test_a_lookup_paused_between_the_in_flight_read_and_the_select_answers_the_stored_capture(unit, monkeypatch):
    from app.core import capture_requests
    cid = _cid()
    assert capture_requests.claim(("real", cid))
    _commits_and_releases_inside_find(monkeypatch, unit, cid, after_select=False)

    looked = _lookup(unit, cid)
    assert looked.status_code == 200, looked.text
    assert looked.json()["in_progress"] is False and looked.json()["record_id"] == unit.real_record_id


@pytest.mark.unit
def test_a_request_claiming_right_after_the_original_released_sees_its_row(unit):
    from app.core import capture_requests
    cid = _cid()
    assert capture_requests.claim(("real", cid))
    images = _row_saved_elsewhere(unit, cid, True)
    capture_requests.release(("real", cid))
    shots = list(unit.backend.requested)

    again = _ok(_dual(unit, cid))
    assert again["replayed"] is True and again["image_ids"] == images
    assert unit.backend.requested == shots


@pytest.mark.unit
@pytest.mark.parametrize("dual", [False, True], ids=["single", "dual"])
def test_a_refusal_raised_after_the_capture_wrote_is_rolled_back_before_the_id_is_released(unit, monkeypatch, dual):
    """Every exit path ends its transaction before the claim goes: here a 507 raised after the counter was taken."""
    from fastapi import HTTPException
    from app.api import cameras
    from app.core import capture_requests

    def refuse(db, user, root):
        raise HTTPException(status_code=507, detail={"code": "storage_reserve", "message": "full"})

    monkeypatch.setattr(cameras, "_refuse_a_real_capture_below_the_storage_reserve", refuse)
    real_release, seen = capture_requests.release, []

    def release(key):
        seen.append(unit.db.in_transaction())
        real_release(key)

    monkeypatch.setattr(capture_requests, "release", release)
    cid = _cid()
    resp = (_dual if dual else _single)(unit, cid)
    assert resp.status_code == 507, resp.text
    assert seen == [False], "the id was released with the capture's transaction still open"
    assert not capture_requests.in_flight(("real", cid))


@pytest.mark.unit
def test_after_a_restart_mid_capture_the_lookup_answers_404_and_a_new_id_photographs(unit):
    """A process killed after the shutter and before the commit saved nothing: its transaction rolled back.

    Stood in for by a capture that writes its file and fails before the
    commit, and a registry emptied as a new process starts with it.
    """
    from app.core import capture_requests
    cid = _cid()
    unit.backend.fail_after_write = {0}
    killed = _single(unit, cid)
    assert killed.status_code == 200 and killed.json()["success"] is False, killed.text
    unit.backend.fail_after_write = set()
    with capture_requests._lock:
        capture_requests._in_flight.clear()
        for closed_here in capture_requests._closed.values():
            closed_here.clear()
    assert _lookup(unit, cid).status_code == 404
    shots = len(unit.backend.requested)
    _abandoned(_single(unit, cid))
    fresh = _ok(_single(unit, _cid()))
    assert not fresh.get("replayed") and len(unit.backend.requested) == shots + 1


# ---------------------------------------------------------------------------
# B. Ids are the space's own
# ---------------------------------------------------------------------------

def _testing_single(unit, project, volume, cid):
    return unit.post("testing", "/cameras/capture", {
        "project_name": project["name"], "collection_id": volume["id"], "camera_index": 0,
        "client_capture_id": cid})


@pytest.mark.unit
def test_each_space_stores_the_same_id_for_its_own_capture(unit):
    project, volume = _testing_volume(unit)
    cid = _cid()
    shots = len(unit.backend.requested)
    real = _ok(_single(unit, cid))
    testing = _ok(_testing_single(unit, project, volume, cid))
    assert not testing.get("replayed") and testing["record_id"] != real["record_id"]
    assert len(unit.backend.requested) == shots + 2
    assert sorted((r.space, r.client_capture_id) for r in _stored(unit)) == [("real", cid), ("testing", cid)]
    assert _ok(_single(unit, cid))["record_id"] == real["record_id"]
    assert _ok(_testing_single(unit, project, volume, cid))["record_id"] == testing["record_id"]
    assert _lookup(unit, cid, caller="testing").json()["record_id"] == testing["record_id"]
    assert _lookup(unit, cid, caller="operator").json()["record_id"] == real["record_id"]


@pytest.mark.unit
def test_the_testing_account_is_not_refused_by_a_real_capture_of_its_id(unit):
    project, volume = _testing_volume(unit)
    cid = _cid()
    _ok(_dual(unit, cid))
    testing = _ok(_testing_single(unit, project, volume, cid))
    assert not testing.get("replayed")


@pytest.mark.unit
def test_an_id_in_flight_in_one_space_neither_blocks_nor_shows_in_the_other(unit):
    from app.core import capture_requests
    project, volume = _testing_volume(unit)
    cid = _cid()
    assert capture_requests.claim(("real", cid))
    try:
        assert _ok(_testing_single(unit, project, volume, cid))
        assert _lookup(unit, cid, caller="testing").json()["in_progress"] is False
        assert _lookup(unit, cid, caller="operator").json() == {"in_progress": True}
    finally:
        capture_requests.release(("real", cid))
    cid = _cid()
    assert capture_requests.claim(("testing", cid))
    try:
        assert _ok(_single(unit, cid))
        assert _lookup(unit, cid, caller="operator").json()["in_progress"] is False
        assert _lookup(unit, cid, caller="testing").json() == {"in_progress": True}
    finally:
        capture_requests.release(("testing", cid))


@pytest.mark.unit
def test_a_testing_row_whose_volume_was_purged_is_never_read_as_real(unit):
    """The row's own space decides: a record left with no volume would read as real work through a join."""
    from app.core import capture_requests
    from app.models.capture_request import StoredCaptureRequest
    from app.models.record import Record
    cid = _cid()
    orphan = Record(title="left by a purge", capture_mode="single")  # no volume, no project: reads as real
    unit.db.add(orphan)
    unit.db.commit()
    unit.db.add(StoredCaptureRequest(client_capture_id=cid, space="testing", kind="single", collection_id=None,
                                     record_id=orphan.id, image_ids=[], request_fingerprint="0" * 64))
    unit.db.commit()

    assert capture_requests.find(unit.db, cid, "real") is None
    assert capture_requests.find(unit.db, cid, "testing").record_id == orphan.id
    real = _ok(_single(unit, cid))
    assert not real.get("replayed") and real["record_id"] != orphan.id
    assert _lookup(unit, cid, caller="operator").json()["record_id"] == real["record_id"]


# ---------------------------------------------------------------------------
# A. A retry while the original is still running is refused at once
# ---------------------------------------------------------------------------

@pytest.fixture
def own_sessions():
    """Each request on its own session, as in the application, so several can run at once."""
    from app.api.deps import get_db_dependency
    from app.core.db import SessionLocal
    from app.main import app

    def fresh():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db_dependency] = fresh
    yield


class _Held:
    """The first camera call blocks until released; every call is counted."""

    def __init__(self, unit, fail=False):
        self.original = unit.backend.capture_image
        self.entered, self.release, self.calls, self.fail = threading.Event(), threading.Event(), [], fail
        unit.backend.capture_image = self

    def __call__(self, output_path, camera_config, capture_output=False):
        self.calls.append(Path(output_path).name)
        if len(self.calls) == 1:
            self.entered.set()
            assert self.release.wait(30), "the test never released the original"
            if self.fail:
                raise RuntimeError("the camera failed")
        return self.original(output_path, camera_config, capture_output)


def _in_progress(resp):
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "capture_in_progress", resp.text
    assert set(resp.json()["detail"]) == {"code", "message"}


def _start(fn):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("resp", fn()))
    t.start()
    return t, out


@pytest.mark.unit
@pytest.mark.parametrize("dual", [False, True], ids=["single", "dual"])
def test_a_retry_while_the_original_runs_is_refused_and_then_replayed(unit, own_sessions, dual):
    cid = _cid()
    capture = _dual if dual else _single
    held = _Held(unit)
    original, out = _start(lambda: capture(unit, cid))
    assert held.entered.wait(60), "the original never reached the camera"

    _in_progress(capture(unit, cid))
    assert _lookup(unit, cid).json() == {"in_progress": True}
    held.release.set()
    original.join(120)
    first = _ok(out["resp"])

    assert len(held.calls) == (2 if dual else 1), "the retry must not photograph"
    again = _ok(capture(unit, cid))
    assert again["replayed"] is True and again["record_id"] == first["record_id"]
    assert _lookup(unit, cid).json() == {**again, "in_progress": False}
    assert len(held.calls) == (2 if dual else 1)


@pytest.mark.unit
def test_a_retry_after_a_failed_original_photographs(unit, own_sessions):
    cid = _cid()
    held = _Held(unit, fail=True)
    original, out = _start(lambda: _single(unit, cid))
    assert held.entered.wait(60)
    _in_progress(_single(unit, cid))
    held.release.set()
    original.join(120)
    assert out["resp"].status_code == 200 and out["resp"].json()["success"] is False

    retry = _ok(_single(unit, cid))
    assert not retry.get("replayed")
    assert len(held.calls) == 2
    assert [r.client_capture_id for r in _stored(unit)] == [cid]


@pytest.mark.unit
def test_a_failed_original_then_a_lookup_abandons_the_id(unit, own_sessions):
    cid = _cid()
    held = _Held(unit, fail=True)
    original, out = _start(lambda: _single(unit, cid))
    assert held.entered.wait(60)
    held.release.set()
    original.join(120)
    assert out["resp"].json()["success"] is False
    assert _lookup(unit, cid).status_code == 404
    _abandoned(_single(unit, cid))
    assert len(held.calls) == 1
    assert _stored(unit) == []


@pytest.mark.unit
def test_the_lookup_answers_each_of_its_four_states(unit, own_sessions):
    looked = _lookup(unit, _cid())
    assert looked.status_code == 404 and looked.json()["detail"]["code"] == "not_found"
    cid = _cid()
    held = _Held(unit)
    original, out = _start(lambda: _single(unit, cid))
    assert held.entered.wait(60)
    looked = _lookup(unit, cid)
    assert looked.status_code == 200 and looked.json() == {"in_progress": True}
    held.release.set()
    original.join(120)
    first = _ok(out["resp"])
    looked = _lookup(unit, cid)
    assert looked.status_code == 200
    assert looked.json()["in_progress"] is False and looked.json()["record_id"] == first["record_id"]
    assert looked.json()["replayed"] is True
    # Stored, and an image deleted since: 410, and the id stays open.
    from app.core import capture_requests
    resp = unit.client.delete(f"/records/images/{first['image_ids'][0]}", headers=unit.headers["operator"])
    assert resp.status_code == 200, resp.text
    _gone(_lookup(unit, cid))
    assert not capture_requests.closed(("real", cid))


@pytest.mark.unit
def test_thirty_retries_during_a_held_original_are_all_refused_without_waiting(unit, own_sessions):
    cid = _cid()
    held = _Held(unit)
    original, out = _start(lambda: _single(unit, cid))
    assert held.entered.wait(60)

    retries = [_start(lambda: _single(unit, cid)) for _ in range(30)]
    # Every retry answers while the original is still held: none waits for it.
    for t, _ in retries:
        t.join(120)
        assert not t.is_alive()
    assert not held.release.is_set() and original.is_alive()
    for _, r in retries:
        _in_progress(r["resp"])
    health = unit.client.get("/health")
    assert health.status_code == 200, health.text

    held.release.set()
    original.join(120)
    _ok(out["resp"])
    assert len(held.calls) == 1


# ---------------------------------------------------------------------------
# The unique constraint, the last backstop
# ---------------------------------------------------------------------------

def _row_saved_elsewhere(unit, cid, same_request):
    """A row for `cid` committed as another process would, behind the handler's check.

    Its fingerprint is that of _dual(unit, cid) when `same_request`, else that of another request.
    """
    from app.api.cameras import DualCaptureRequest
    from app.core import capture_requests
    from app.models.capture_request import StoredCaptureRequest
    from app.models.record import RecordImage
    images = [i.id for i in unit.db.query(RecordImage).filter(RecordImage.record_id == unit.real_record_id)]
    request = DualCaptureRequest(**_body(unit, cid, sequence=None if same_request else 3))
    unit.db.add(StoredCaptureRequest(client_capture_id=cid, space="real", kind="dual",
                                     collection_id=unit.real_volume.id, record_id=unit.real_record_id,
                                     image_ids=images,
                                     request_fingerprint=capture_requests.fingerprint("dual", request)))
    unit.db.commit()
    return images


def _check_misses_once(monkeypatch):
    from app.core import capture_requests
    real = capture_requests.find
    calls = []

    def find(db, client_capture_id, space):
        calls.append(client_capture_id)
        return None if len(calls) == 1 else real(db, client_capture_id, space)

    monkeypatch.setattr(capture_requests, "find", find)
    return calls


@pytest.mark.unit
def test_a_duplicate_found_at_commit_is_answered_from_the_stored_capture(unit, monkeypatch):
    cid = _cid()
    images = _row_saved_elsewhere(unit, cid, True)
    _check_misses_once(monkeypatch)
    records_before = unit.snapshot()[0]["records"]

    resp = _dual(unit, cid)

    body = _ok(resp)
    assert body["replayed"] is True
    assert body["record_id"] == unit.real_record_id and body["image_ids"] == images
    assert unit.snapshot()[0]["records"] == records_before, "the duplicate's record is rolled back"


@pytest.mark.unit
def test_a_duplicate_found_at_commit_that_does_not_match_is_refused(unit, monkeypatch):
    cid = _cid()
    _row_saved_elsewhere(unit, cid, False)
    _check_misses_once(monkeypatch)
    _in_use(_dual(unit, cid))


# ---------------------------------------------------------------------------
# An id the lookup has answered as not found never executes afterwards
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("dual", [False, True], ids=["single", "dual"])
def test_a_post_after_a_lookup_that_answered_not_found_is_abandoned(unit, dual):
    from app.core import capture_requests
    cid = _cid()
    looked = _lookup(unit, cid)
    assert looked.status_code == 404 and looked.json()["detail"]["code"] == "not_found"
    assert capture_requests.closed(("real", cid))
    shots = list(unit.backend.requested)
    before = unit.snapshot()

    _abandoned((_dual if dual else _single)(unit, cid))
    assert unit.backend.requested == shots, "an abandoned id must not reach the camera"
    assert unit.snapshot() == before and _stored(unit) == []
    assert _lookup(unit, cid).status_code == 404


@pytest.mark.unit
def test_a_new_id_after_an_abandoned_one_photographs(unit):
    cid = _cid()
    assert _lookup(unit, cid).status_code == 404
    _abandoned(_single(unit, cid))
    shots = len(unit.backend.requested)
    fresh = _ok(_single(unit, _cid()))
    assert not fresh.get("replayed") and len(unit.backend.requested) == shots + 1


@pytest.mark.unit
def test_a_lookup_of_a_stored_or_running_capture_closes_nothing(unit, own_sessions):
    from app.core import capture_requests
    stored_id = _cid()
    _ok(_single(unit, stored_id))
    assert _lookup(unit, stored_id).json()["in_progress"] is False
    assert not capture_requests.closed(("real", stored_id))
    assert _ok(_single(unit, stored_id))["replayed"] is True

    running_id = _cid()
    held = _Held(unit)
    original, out = _start(lambda: _single(unit, running_id))
    assert held.entered.wait(60)
    assert _lookup(unit, running_id).json() == {"in_progress": True}
    assert not capture_requests.closed(("real", running_id))
    held.release.set()
    original.join(120)
    _ok(out["resp"])
    assert _lookup(unit, running_id).json()["in_progress"] is False


@pytest.mark.unit
def test_a_malformed_id_closes_nothing(unit):
    from app.core import capture_requests
    assert _lookup(unit, "not-a-uuid").status_code == 422
    with capture_requests._lock:
        assert sum(len(c) for c in capture_requests._closed.values()) == 0


@pytest.mark.unit
def test_a_lookup_closes_only_its_own_spaces_id(unit):
    from app.core import capture_requests
    project, volume = _testing_volume(unit)
    # The testing account's lookup closes the testing key; real work still captures with the id.
    a = _cid()
    assert _lookup(unit, a, caller="testing").status_code == 404
    assert capture_requests.closed(("testing", a)) and not capture_requests.closed(("real", a))
    assert not _ok(_single(unit, a)).get("replayed")
    _abandoned(_testing_single(unit, project, volume, a))
    # And the other way round.
    b = _cid()
    assert _lookup(unit, b, caller="operator").status_code == 404
    assert capture_requests.closed(("real", b)) and not capture_requests.closed(("testing", b))
    assert not _ok(_testing_single(unit, project, volume, b)).get("replayed")
    _abandoned(_single(unit, b))


@pytest.mark.unit
def test_a_lookup_of_a_capture_that_lost_an_image_does_not_close_the_id(unit):
    """The row exists, so the 404 is not a not-found: the POST keeps answering capture_no_longer_stored."""
    from app.core import capture_requests
    cid = _cid()
    first = _ok(_dual(unit, cid))
    resp = unit.client.delete(f"/records/images/{first['image_ids'][0]}", headers=unit.headers["operator"])
    assert resp.status_code == 200, resp.text
    _gone(_lookup(unit, cid))
    assert not capture_requests.closed(("real", cid))
    shots = list(unit.backend.requested)
    again = _dual(unit, cid)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "capture_no_longer_stored", again.text
    assert unit.backend.requested == shots


@pytest.mark.unit
def test_the_closed_ids_are_bounded_and_the_oldest_goes_first(monkeypatch):
    from app.core import capture_requests
    assert capture_requests.CLOSED_LIMIT == 100_000
    monkeypatch.setattr(capture_requests, "CLOSED_LIMIT", 3)
    keys = [("real", _cid()) for _ in range(4)]
    for key in keys:
        capture_requests.close(key)
    assert [capture_requests.closed(k) for k in keys] == [False, True, True, True]
    # The evicted one can be claimed again; the others are refused at the claim.
    assert capture_requests.claim(keys[0]) == capture_requests.CLAIMED
    capture_requests.release(keys[0])
    assert all(capture_requests.claim(k) == capture_requests.CLOSED for k in keys[1:])
    # Closing a closed key again makes it the newest.
    capture_requests.close(keys[1])
    capture_requests.close(("real", _cid()))
    assert capture_requests.closed(keys[1]) and not capture_requests.closed(keys[2])


class _ObservedLock:
    """The registry's lock, with a hook: the first time the function `holder` holds it, `while_held` runs inside.

    The callers are told apart by the function that takes the lock (the
    handlers run on the server's own threads, not the test's): a caller in
    the function `arriver` sets `arrived` just before it asks for the lock,
    so the holder can wait until the other request is at the lock and let
    it in only after its own decision.
    """

    def __init__(self, inner, holder, arriver, while_held):
        self.inner, self.holder, self.arriver, self.while_held = inner, holder, arriver, while_held
        self.arrived, self.done = threading.Event(), False

    def __enter__(self):
        import sys
        caller = sys._getframe(1).f_code.co_name
        if caller == self.arriver:
            self.arrived.set()
        self.inner.acquire()
        if not self.done and caller == self.holder:
            self.done = True
            self.while_held(self)
        return self

    def __exit__(self, *exc):
        self.inner.release()
        return False


def _named(name, fn):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("resp", fn()), name=name)
    t.start()
    return t, out


@pytest.mark.unit
def test_the_claim_first_at_the_lock_means_the_lookup_answers_in_progress(unit, own_sessions, monkeypatch):
    """The POST holds the lock for its claim while the lookup arrives at it: the lookup sees the claim."""
    from app.core import capture_requests
    cid = _cid()
    lookups = {}

    def let_the_lookup_arrive(lock):
        lookups["t"], lookups["out"] = _named("arrive-lookup", lambda: _lookup(unit, cid))
        assert lock.arrived.wait(30), "the lookup never reached the lock"

    monkeypatch.setattr(capture_requests, "_lock",
                        _ObservedLock(capture_requests._lock, "claim", "lookup", let_the_lookup_arrive))
    held = _Held(unit)
    post, out = _named("post", lambda: _single(unit, cid))
    assert held.entered.wait(60), "the POST never reached the camera"
    lookups["t"].join(60)
    looked = lookups["out"]["resp"]
    assert looked.status_code == 200 and looked.json() == {"in_progress": True}, looked.text
    held.release.set()
    post.join(120)
    first = _ok(out["resp"])
    assert len(held.calls) == 1
    stored = _lookup(unit, cid)
    assert stored.json()["in_progress"] is False and stored.json()["record_id"] == first["record_id"]
    assert not capture_requests.closed(("real", cid))


@pytest.mark.unit
def test_the_lookup_first_at_the_lock_means_the_post_is_abandoned(unit, own_sessions, monkeypatch):
    """The lookup holds the lock for its decision while the POST arrives at it: the POST finds the id closed."""
    from app.core import capture_requests
    cid = _cid()
    posts = {}

    def let_the_post_arrive(lock):
        posts["t"], posts["out"] = _named("arrive-post", lambda: _single(unit, cid))
        assert lock.arrived.wait(30), "the POST never reached the lock"

    monkeypatch.setattr(capture_requests, "_lock",
                        _ObservedLock(capture_requests._lock, "lookup", "claim", let_the_post_arrive))
    shots = list(unit.backend.requested)
    lookup, out = _named("lookup", lambda: _lookup(unit, cid))
    lookup.join(60)
    assert out["resp"].status_code == 404, out["resp"].text
    posts["t"].join(60)
    _abandoned(posts["out"]["resp"])
    assert unit.backend.requested == shots
    assert _stored(unit) == []



@pytest.mark.unit
@pytest.mark.parametrize("one,other", [("testing", "real"), ("real", "testing")])
def test_lookups_in_one_space_never_evict_the_other_spaces_closed_ids(monkeypatch, one, other):
    from app.core import capture_requests
    monkeypatch.setattr(capture_requests, "CLOSED_LIMIT", 3)
    kept = (other, _cid())
    capture_requests.close(kept)
    flood = [(one, _cid()) for _ in range(10)]
    for key in flood:
        capture_requests.close(key)
    assert capture_requests.closed(kept), "the other space's flood evicted this space's closed id"
    assert capture_requests.claim(kept) == capture_requests.CLOSED
    # Within the flooding space the bound holds, oldest first.
    assert [capture_requests.closed(k) for k in flood] == [False] * 7 + [True] * 3


@pytest.mark.unit
def test_the_testing_accounts_lookups_never_evict_a_real_closed_id(unit, monkeypatch):
    from app.core import capture_requests
    monkeypatch.setattr(capture_requests, "CLOSED_LIMIT", 3)
    real = _cid()
    assert _lookup(unit, real).status_code == 404
    for _ in range(5):
        assert _lookup(unit, _cid(), caller="testing").status_code == 404
    shots = list(unit.backend.requested)
    _abandoned(_single(unit, real))
    assert unit.backend.requested == shots


# ---------------------------------------------------------------------------
# The lookup does no database I/O under the registry's lock
# ---------------------------------------------------------------------------

class _LookupSelects:
    """Patch find for the lookup's own calls only: the first can be made to miss, the second to wait.

    `second_started` is set when the lookup's second select begins; it
    waits for `go` (bounded) before reading. The capture routes' own finds
    are left alone.
    """

    def __init__(self, monkeypatch, first_misses=False, pause_second=True):
        import sys
        from app.core import capture_requests
        self.real, self.calls = capture_requests.find, 0
        self.second_started, self.go = threading.Event(), threading.Event()
        self.lock_free_during_second = None

        def find(db, client_capture_id, space):
            if sys._getframe(1).f_code.co_name != "lookup":
                return self.real(db, client_capture_id, space)
            self.calls += 1
            if self.calls == 1 and first_misses:
                return None
            if self.calls == 2 and pause_second:
                self.second_started.set()
                assert self.go.wait(20), "the test never let the lookup's second select go on"
            return self.real(db, client_capture_id, space)

        monkeypatch.setattr(capture_requests, "find", find)


@pytest.mark.unit
def test_the_registry_lock_is_free_while_the_lookups_second_select_runs(unit, own_sessions, monkeypatch):
    from app.core import capture_requests
    selects = _LookupSelects(monkeypatch)
    cid = _cid()
    lookup, out = _start(lambda: _lookup(unit, cid))
    assert selects.second_started.wait(30), "the lookup never reached its second select"

    other = ("real", _cid())
    claimed = {}
    t = threading.Thread(target=lambda: claimed.setdefault("state", capture_requests.claim(other)))
    t.start()
    t.join(5)
    alive = t.is_alive()
    selects.go.set()
    lookup.join(60)
    t.join(30)
    assert not alive, "a claim waited for the lookup's select: the registry lock was held across it"
    assert claimed["state"] == capture_requests.CLAIMED
    capture_requests.release(other)
    assert out["resp"].status_code == 404


@pytest.mark.unit
def test_a_post_claiming_after_the_lookups_close_is_abandoned_and_the_lookup_answers_404(unit, own_sessions,
                                                                                         monkeypatch):
    selects = _LookupSelects(monkeypatch)
    cid = _cid()
    shots = list(unit.backend.requested)
    lookup, out = _start(lambda: _lookup(unit, cid))
    assert selects.second_started.wait(30)
    _abandoned(_single(unit, cid))
    selects.go.set()
    lookup.join(60)
    assert out["resp"].status_code == 404 and out["resp"].json()["detail"]["code"] == "not_found"
    assert unit.backend.requested == shots and _stored(unit) == []


@pytest.mark.unit
def test_a_capture_committed_before_the_mark_is_found_by_the_second_select_and_nothing_is_closed(unit, own_sessions,
                                                                                          monkeypatch):
    """The lookup's first select missed a row committed just after it; the second, after the close, finds it."""
    from app.core import capture_requests
    cid = _cid()
    first = _ok(_dual(unit, cid))
    _LookupSelects(monkeypatch, first_misses=True, pause_second=False)
    looked = _lookup(unit, cid)
    assert looked.status_code == 200 and looked.json()["record_id"] == first["record_id"], looked.text
    assert looked.json()["in_progress"] is False
    assert not capture_requests.closed(("real", cid)), "the key must be opened again"
    assert _ok(_dual(unit, cid))["replayed"] is True


@pytest.mark.unit
def test_a_repeat_of_a_stored_id_while_a_mark_is_pending_is_refused_as_abandoned(unit, own_sessions,
                                                                                            monkeypatch):
    """The one imprecise case: the capture is stored, yet a second send in that window is refused.

    No client produces it: the screen never sends an id again, it asks the lookup.
    """
    from app.core import capture_requests
    cid = _cid()
    first = _ok(_dual(unit, cid))
    selects = _LookupSelects(monkeypatch, first_misses=True)
    shots = list(unit.backend.requested)
    lookup, out = _start(lambda: _lookup(unit, cid))
    assert selects.second_started.wait(30)
    _abandoned(_dual(unit, cid))
    selects.go.set()
    lookup.join(60)
    assert out["resp"].status_code == 200 and out["resp"].json()["record_id"] == first["record_id"]
    assert not capture_requests.closed(("real", cid))
    assert _ok(_dual(unit, cid))["replayed"] is True
    assert unit.backend.requested == shots


# ---------------------------------------------------------------------------
# A key is closed only by a "not found" that was actually answered
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_second_select_that_fails_leaves_the_key_open(unit, monkeypatch):
    """The database drops during the lookup's second select: the ask fails, and the stored capture is not abandoned."""
    import sys
    from app.core import capture_requests
    cid = _cid()
    first = _ok(_dual(unit, cid))
    real, calls = capture_requests.find, []

    def find(db, client_capture_id, space):
        if sys._getframe(1).f_code.co_name != "lookup":
            return real(db, client_capture_id, space)
        calls.append(client_capture_id)
        if len(calls) == 1:
            return None  # the first select misses
        raise RuntimeError("the database went away")

    monkeypatch.setattr(capture_requests, "find", find)
    with pytest.raises(RuntimeError):
        _lookup(unit, cid)
    assert len(calls) == 2
    assert not capture_requests.closed(("real", cid)), "a failed ask closed the key"
    monkeypatch.setattr(capture_requests, "find", real)

    looked = _lookup(unit, cid)
    assert looked.status_code == 200 and looked.json()["record_id"] == first["record_id"], looked.text
    shots = list(unit.backend.requested)
    again = _ok(_dual(unit, cid))
    assert again["replayed"] is True and again["record_id"] == first["record_id"]
    assert unit.backend.requested == shots


# ---------------------------------------------------------------------------
# An answered close is final; a pending mark is its own lookup's
# ---------------------------------------------------------------------------

class _ScriptedSelects:
    """The lookup's own selects, in the order they run, each one `real`, `miss`, `pause` or `pause_raise`.

    A `pause` sets `paused[n]` and waits (bounded) for `go[n]`, n being its
    position in the script; `pause_raise` then raises. Calls past the end of
    the script, and every find that is not the lookup's, read the table.
    """

    def __init__(self, monkeypatch, *script):
        import sys
        from app.core import capture_requests
        self.real, self.script, self.calls = capture_requests.find, list(script), 0
        self.paused = [threading.Event() for _ in script]
        self.go = [threading.Event() for _ in script]
        guard = threading.Lock()

        def find(db, client_capture_id, space):
            if sys._getframe(1).f_code.co_name != "lookup":
                return self.real(db, client_capture_id, space)
            with guard:
                n = self.calls
                self.calls += 1
            action = self.script[n] if n < len(self.script) else "real"
            if action == "miss":
                return None
            if action in ("pause", "pause_raise"):
                self.paused[n].set()
                assert self.go[n].wait(20), f"the test never let select {n} go on"
                if action == "pause_raise":
                    raise RuntimeError("the database went away")
            return self.real(db, client_capture_id, space)

        monkeypatch.setattr(capture_requests, "find", find)


def _start_catching(fn):
    """_start, keeping an exception raised in the thread as the result."""
    out = {}

    def run():
        try:
            out["resp"] = fn()
        except BaseException as exc:  # noqa: BLE001 - the test reads it
            out["error"] = exc

    t = threading.Thread(target=run)
    t.start()
    return t, out


def _no_marks():
    from app.core import capture_requests
    with capture_requests._lock:
        return not capture_requests._pending


@pytest.mark.unit
def test_a_failing_lookup_does_not_undo_another_lookups_answered_not_found(unit, own_sessions, monkeypatch):
    """The reviewer's schedule: A misses and is pending when B misses twice and answers 404; A's select then
    raises; a late POST is still refused, as B answered."""
    from app.core import capture_requests
    cid = _cid()
    # A: first select (real, no row), second select paused then raising; B: both real.
    selects = _ScriptedSelects(monkeypatch, "real", "pause_raise", "real", "real")
    a, a_out = _start_catching(lambda: _lookup(unit, cid))
    assert selects.paused[1].wait(30), "A never reached its second select"

    b = _lookup(unit, cid)
    assert b.status_code == 404 and b.json()["detail"]["code"] == "not_found", b.text
    selects.go[1].set()
    a.join(60)
    assert isinstance(a_out.get("error"), RuntimeError), a_out
    assert capture_requests.closed(("real", cid)), "A's failure undid B's answered close"
    assert _no_marks()

    shots = list(unit.backend.requested)
    _abandoned(_single(unit, cid))
    assert unit.backend.requested == shots and _stored(unit) == []


@pytest.mark.unit
def test_two_pending_lookups_of_a_stored_capture_both_find_it_and_close_nothing(unit, own_sessions, monkeypatch):
    """A row committed before either mark is seen by both second selects; nothing can commit one while a mark
    is pending, so one finding the row and the other not is not possible."""
    from app.core import capture_requests
    cid = _cid()
    first = _ok(_dual(unit, cid))
    # A: misses, then its second select waits; B: misses, then reads.
    selects = _ScriptedSelects(monkeypatch, "miss", "pause", "miss", "real")
    a, a_out = _start_catching(lambda: _lookup(unit, cid))
    assert selects.paused[1].wait(30)
    b = _lookup(unit, cid)
    assert b.status_code == 200 and b.json()["record_id"] == first["record_id"], b.text
    selects.go[1].set()
    a.join(60)
    assert a_out["resp"].status_code == 200 and a_out["resp"].json()["record_id"] == first["record_id"]
    assert not capture_requests.closed(("real", cid)) and _no_marks()
    assert _ok(_dual(unit, cid))["replayed"] is True


@pytest.mark.unit
def test_two_pending_lookups_of_an_unknown_id_both_answer_404_and_the_id_stays_closed(unit, own_sessions,
                                                                                       monkeypatch):
    from app.core import capture_requests
    cid = _cid()
    selects = _ScriptedSelects(monkeypatch, "real", "pause", "real", "real")
    a, a_out = _start_catching(lambda: _lookup(unit, cid))
    assert selects.paused[1].wait(30)
    assert _lookup(unit, cid).status_code == 404
    selects.go[1].set()
    a.join(60)
    assert a_out["resp"].status_code == 404
    assert capture_requests.closed(("real", cid)) and _no_marks()
    _abandoned(_single(unit, cid))


@pytest.mark.unit
def test_a_claim_while_a_mark_is_pending_is_refused_and_the_marks_owner_then_answers(unit, own_sessions,
                                                                                     monkeypatch):
    from app.core import capture_requests
    cid = _cid()
    selects = _ScriptedSelects(monkeypatch, "real", "pause")
    a, a_out = _start_catching(lambda: _lookup(unit, cid))
    assert selects.paused[1].wait(30)
    with capture_requests._lock:
        assert capture_requests._pending.get(("real", cid)), "the lookup holds no mark while its select runs"
    assert not capture_requests.closed(("real", cid)), "a pending mark is not an answered close"
    assert capture_requests.claim(("real", cid)) == capture_requests.CLOSED
    selects.go[1].set()
    a.join(60)
    assert a_out["resp"].status_code == 404
    assert capture_requests.closed(("real", cid)) and _no_marks()


@pytest.mark.unit
@pytest.mark.parametrize("ending", ["in_progress", "not_found", "stored_at_the_second_select", "raises", "gone"])
def test_the_lookups_mark_is_removed_on_every_exit_path(unit, own_sessions, monkeypatch, ending):
    from app.core import capture_requests
    cid = _cid()
    if ending == "in_progress":
        assert capture_requests.claim(("real", cid)) == capture_requests.CLAIMED
        assert _lookup(unit, cid).json() == {"in_progress": True}
        capture_requests.release(("real", cid))
    elif ending == "not_found":
        assert _lookup(unit, cid).status_code == 404
    elif ending == "stored_at_the_second_select":
        _ok(_single(unit, cid))
        _ScriptedSelects(monkeypatch, "miss", "real")
        assert _lookup(unit, cid).status_code == 200
    elif ending == "raises":
        _ScriptedSelects(monkeypatch, "real", "pause_raise").go[1].set()
        with pytest.raises(RuntimeError):
            _lookup(unit, cid)
        assert not capture_requests.closed(("real", cid))
    else:
        first = _ok(_single(unit, cid))
        unit.client.delete(f"/records/images/{first['image_ids'][0]}", headers=unit.headers["operator"])
        _ScriptedSelects(monkeypatch, "miss", "real")
        _gone(_lookup(unit, cid))
        assert not capture_requests.closed(("real", cid))
    assert _no_marks(), "a lookup left its mark behind"


@pytest.mark.unit
def test_no_key_leaves_the_closed_ids_except_by_eviction(unit, own_sessions, monkeypatch):
    """Every way out of the closed sets but the bound's popitem(last=False) is recorded, and none is taken."""
    from collections import OrderedDict
    from app.core import capture_requests

    removed = []

    class Guarded(OrderedDict):
        def pop(self, key, *default):
            removed.append(("pop", key))
            return super().pop(key, *default)

        def __delitem__(self, key):
            removed.append(("del", key))
            super().__delitem__(key)

        def popitem(self, last=True):
            if last:
                removed.append(("popitem-last", None))
            return super().popitem(last=last)

        def clear(self):
            removed.append(("clear", None))
            super().clear()

    monkeypatch.setattr(capture_requests, "_closed",
                        {capture_requests.REAL: Guarded(), capture_requests.TESTING: Guarded()})
    stored, unknown, failing = _cid(), _cid(), _cid()
    _ok(_dual(unit, stored))
    _lookup(unit, stored)
    assert _lookup(unit, unknown).status_code == 404
    _abandoned(_single(unit, unknown))
    selects = _ScriptedSelects(monkeypatch, "miss", "real", "real", "pause_raise")
    assert _lookup(unit, stored).status_code == 200
    selects.go[3].set()
    with pytest.raises(RuntimeError):
        _lookup(unit, failing)
    assert _lookup(unit, unknown).status_code == 404
    assert removed == [], removed
