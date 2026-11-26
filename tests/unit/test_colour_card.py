"""Colour card shots of a volume (NEH-340): the routes, the files, the sets and what a volume says of them.

The camera is stood in for as tests/unit/test_testing_space_capture.py
does, and the unit is that file's (a real project and volume with one dual
capture, one user per role, the testing account), with a backend that
also answers the settings reads of a CHDK or a gphoto2 driver.

    python -m pytest tests/unit/test_colour_card.py
"""

import json
import re
import threading
import uuid

import pytest

import capture.service as capture_service
from tests.unit.test_capture_contract_naming import _FakeBackend
from tests.unit.test_testing_space_capture import (  # noqa: F401  (fixtures)
    _testing_volume, backend, lookups, unit,
)


class _SettingsBackend(_FakeBackend):
    """The fake camera, with a driver's settings reads: CHDK's held zoom step, or gphoto2's PTP settings."""

    def __init__(self, name="chdk", raw=False):
        super().__init__(raw=raw)
        self.name = name
        self.zoom = {0: 3, 1: 3}
        self.exposure = {0: {"iso": "100", "shutter_speed": "1/60", "aperture": "8"},
                         1: {"iso": "100", "shutter_speed": "1/60", "aperture": "8"}}
        self.unreadable = set()
        self.reads = []

    def get_backend_name(self):
        return self.name

    def held_zoom(self, index):
        if index in self.unreadable:
            return None
        return {"zoom_step": self.zoom[index]}

    def capture_image_format(self, index):
        return "RAW+JPEG" if self.raw else "JPEG"

    def get_dslr_settings(self, index):
        self.reads.append(index)
        if index in self.unreadable:
            raise RuntimeError("no answer")
        return {**self.exposure[index], "image_format": "JPEG", "focus_mode": None, "flash_mode": None,
                "stored_image_format": None}


@pytest.fixture
def card(unit, monkeypatch):
    """The unit with the colour card setting on and a backend that reads settings."""
    from app.core import unit_mode
    b = _SettingsBackend()
    monkeypatch.setattr(capture_service, "get_backend", lambda: b)
    unit.backend = b
    unit_mode.set_colour_card_shots(unit.db, True, actor="u-admin")
    return unit


def _volume(unit, stem="xx-card-1", name="Tomo con carta"):
    from app.models.collection import Collection
    v = Collection(name=name, project_id=unit.real_project.id, filename_stem=stem)
    unit.db.add(v)
    unit.db.commit()
    unit.db.refresh(v)
    return v


def _ok(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()


def _code(resp, status, code):
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"]["code"] == code, resp.text
    return resp.json()["detail"]


def _state(unit, vid, caller="operator", **params):
    return _ok(unit.get(caller, f"/collections/{vid}/colour-card", params=params))


def _open(unit, vid, caller="operator", **body):
    return unit.post(caller, f"/collections/{vid}/colour-card/sets", body or None)


def _shot(unit, vid, sid, index, caller="operator"):
    return unit.post(caller, f"/collections/{vid}/colour-card/sets/{sid}/shots", {"camera_index": index})


def _complete(unit, vid, sid, caller="operator"):
    return unit.post(caller, f"/collections/{vid}/colour-card/sets/{sid}/complete")


def _take_set(unit, vid):
    s = _ok(_open(unit, vid))
    for cam in s["cameras"]:
        _ok(_shot(unit, vid, s["id"], cam["camera_index"]))
    return _ok(_complete(unit, vid, s["id"]))


def _records_and_images(unit):
    from app.models.record import Record, RecordImage
    unit.db.expire_all()
    return unit.db.query(Record).count(), unit.db.query(RecordImage).count()


def _manifest_lines(unit, stem):
    path = unit.root / stem / "metadata" / "manifest.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


# ---------------------------------------------------------------------------
# Whether a volume is asked
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_with_the_setting_off_nothing_is_asked_and_nothing_is_taken(unit):
    v = _volume(unit)
    assert _state(unit, v.id) == {"enabled": False, "needed": False, "reason": None, "status": "none", "sets": []}
    for resp in (_open(unit, v.id), unit.post("operator", f"/collections/{v.id}/colour-card/skip")):
        _code(resp, 409, "colour_card_not_enabled")
    assert _ok(unit.get("operator", f"/collections/{v.id}"))["colour_card"] == {"enabled": False}
    listed = {c["id"]: c for c in _ok(unit.get("operator", "/collections/"))}
    assert listed[v.id]["colour_card"] == {"enabled": False}


@pytest.mark.unit
def test_a_new_volume_is_asked_at_the_start_and_one_begun_without_once(card):
    v = _volume(card)
    assert _state(card, v.id) == {"enabled": True, "needed": True, "reason": "start", "status": "none", "sets": []}
    begun = _state(card, card.real_volume.id)
    assert (begun["needed"], begun["reason"]) == (True, "begun_without")
    _take_set(card, card.real_volume.id)
    again = _state(card, card.real_volume.id)
    assert (again["needed"], again["reason"]) == (False, None)


@pytest.mark.unit
def test_a_testing_volume_is_never_asked(card):
    vid = _testing_volume(card)[1]["id"]
    body = _ok(card.get("testing", f"/collections/{vid}/colour-card"))
    assert body["enabled"] is False and body["needed"] is False and body["sets"] == []
    _code(card.post("testing", f"/collections/{vid}/colour-card/sets"), 409, "colour_card_not_enabled")
    _code(card.post("testing", f"/collections/{vid}/colour-card/skip"), 409, "colour_card_not_enabled")
    assert _ok(card.get("testing", f"/collections/{vid}"))["colour_card"] == {"enabled": False}


# ---------------------------------------------------------------------------
# Taking a set
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_set_is_opened_with_each_cameras_settings_and_its_number(card, monkeypatch):
    v = _volume(card)
    s = _ok(_open(card, v.id))
    assert s["number"] == 1 and s["outcome"] is None and s["capture_mode"] == "dual" and s["shots"] == []
    assert [(c["camera_index"], c["settings"]) for c in s["cameras"]] == [
        (0, {"zoom_step": 3, "image_format": "JPEG"}), (1, {"zoom_step": 3, "image_format": "JPEG"})]
    # Opening again answers the open set.
    assert _ok(_open(card, v.id))["id"] == s["id"]
    single = _volume(card, "xx-card-2", "Otro")
    one = _ok(_open(card, single.id, capture_mode="single", camera_index=1))
    assert [c["camera_index"] for c in one["cameras"]] == [1] and one["cameras"][0]["role"] == "single"


@pytest.mark.unit
def test_each_camera_takes_its_shot_into_the_calibration_folder(card):
    v = _volume(card)
    before = _records_and_images(card)
    s = _ok(_open(card, v.id))
    a = _ok(_shot(card, v.id, s["id"], 0))
    b = _ok(_shot(card, v.id, s["id"], 1))
    folder = card.root / v.filename_stem / "calibration"
    assert sorted(p.name for p in folder.iterdir()) == ["xx-card-1-cal_01_c0.jpg", "xx-card-1-cal_01_c1.jpg"]
    assert a["filename"] == "xx-card-1-cal_01_c0.jpg" and b["filename"] == "xx-card-1-cal_01_c1.jpg"
    for shot in (a, b):
        assert uuid.UUID(shot["capture_id"]).version == 4
        import hashlib
        assert shot["sha256"] == hashlib.sha256((folder / shot["filename"]).read_bytes()).hexdigest()
        assert shot["url"] == f"/collections/{v.id}/colour-card/shots/{shot['id']}/preview"
        assert shot["settings"]["driver"] == "chdk" and shot["settings"]["camera"]["zoom_step"] == 3
    assert a["capture_id"] != b["capture_id"]
    # No record, no image, no page number taken.
    assert _records_and_images(card) == before
    card.db.expire_all()
    from app.models.collection import Collection
    assert card.db.get(Collection, v.id).capture_counter == 0
    assert not (card.root / v.filename_stem / "captures").exists() or not any(
        (card.root / v.filename_stem / "captures").iterdir())
    lines = [line for line in _manifest_lines(card, v.filename_stem) if line.get("record_type") == "calibration"]
    assert [(line["capture_id"], line["files"][0]["relative_path"]) for line in lines] == [
        (a["capture_id"], "calibration/xx-card-1-cal_01_c0.jpg"),
        (b["capture_id"], "calibration/xx-card-1-cal_01_c1.jpg")]
    assert lines[0]["files"][0]["sha256"] == a["sha256"]


@pytest.mark.unit
def test_a_shot_taken_again_replaces_the_first(card):
    from app.models.colour_card import ColourCardShot
    v = _volume(card)
    s = _ok(_open(card, v.id))
    first = _ok(_shot(card, v.id, s["id"], 0))
    again = _ok(_shot(card, v.id, s["id"], 0))
    assert again["id"] == first["id"] and again["capture_id"] != first["capture_id"]
    assert again["sha256"] != first["sha256"]
    folder = card.root / v.filename_stem / "calibration"
    assert [p.name for p in folder.iterdir()] == ["xx-card-1-cal_01_c0.jpg"]
    card.db.expire_all()
    assert card.db.query(ColourCardShot).count() == 1


@pytest.mark.unit
def test_a_raw_shots_preview_is_kept_beside_it(card):
    card.backend.raw = True
    v = _volume(card)
    s = _ok(_open(card, v.id))
    shot = _ok(_shot(card, v.id, s["id"], 0))
    folder = card.root / v.filename_stem / "calibration"
    assert sorted(p.name for p in folder.iterdir()) == ["xx-card-1-cal_01_c0.cr2", "xx-card-1-cal_01_c0_preview.jpg"]
    assert shot["preview_filename"] == "xx-card-1-cal_01_c0_preview.jpg" and shot["preview_sha256"]
    previews = card.root / v.filename_stem / "previews"
    assert not previews.exists() or not any(previews.iterdir())
    resp = card.get("reviewer", shot["url"])
    assert resp.status_code == 200 and resp.content == (folder / shot["preview_filename"]).read_bytes()


@pytest.mark.unit
def test_a_set_is_finished_only_with_every_cameras_shot(card):
    v = _volume(card)
    s = _ok(_open(card, v.id))
    _ok(_shot(card, v.id, s["id"], 0))
    detail = _code(_complete(card, v.id, s["id"]), 409, "colour_card_shots_missing")
    assert detail["missing_camera_indexes"] == [1]
    _ok(_shot(card, v.id, s["id"], 1))
    done = _ok(_complete(card, v.id, s["id"]))
    assert done["outcome"] == "taken" and done["completed_by"] == "u-operator"
    _code(_shot(card, v.id, s["id"], 0), 409, "colour_card_set_closed")
    _code(_complete(card, v.id, s["id"]), 409, "colour_card_set_closed")
    # The next set has the next number.
    assert _ok(_open(card, v.id))["number"] == 2


@pytest.mark.unit
def test_the_shot_preview_is_served_inline_to_every_reading_role(card):
    v = _volume(card)
    s = _ok(_open(card, v.id))
    shot = _ok(_shot(card, v.id, s["id"], 0))
    for caller in ("admin", "operator", "reviewer"):
        resp = card.get(caller, shot["url"])
        assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"
        assert resp.headers["content-disposition"].startswith("inline")
    other = _volume(card, "xx-card-2", "Otro")
    _code(card.get("operator", f"/collections/{other.id}/colour-card/shots/{shot['id']}/preview"), 404,
          "colour_card_shot_not_found")


# ---------------------------------------------------------------------------
# Status, skipping and settings changes
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_volume_whose_set_came_first_is_present_and_one_begun_without_is_skipped(card):
    v = _volume(card)
    _take_set(card, v.id)
    assert _state(card, v.id)["status"] == "present"
    resp = card.post("operator", "/cameras/capture/dual",
                     {"project_name": card.real_project.name, "collection_id": v.id})
    assert resp.status_code == 200 and resp.json()["success"], resp.text
    assert _state(card, v.id)["status"] == "present"
    assert _ok(card.get("operator", f"/collections/{v.id}"))["colour_card"] == {
        "enabled": True, "status": "present", "sets": 1}
    _take_set(card, card.real_volume.id)
    assert _state(card, card.real_volume.id)["status"] == "skipped"


@pytest.mark.unit
def test_a_skipped_request_is_recorded_with_its_settings_and_asks_no_more(card):
    v = _volume(card)
    skipped = _ok(card.post("operator", f"/collections/{v.id}/colour-card/skip"))
    assert skipped["outcome"] == "skipped" and skipped["cameras"][0]["settings"] == {
        "zoom_step": 3, "image_format": "JPEG"}
    state = _state(card, v.id)
    assert (state["needed"], state["status"]) == (False, "skipped")
    listed = {c["id"]: c for c in _ok(card.get("operator", "/collections/"))}
    assert listed[v.id]["colour_card"] == {"enabled": True, "status": "skipped", "sets": 0}
    # A later change of zoom is compared with the skipped set's settings.
    card.backend.zoom[1] = 5
    state = _state(card, v.id)
    assert (state["needed"], state["reason"]) == (True, "settings_changed")


@pytest.mark.unit
def test_skipping_an_open_set_removes_its_shots(card):
    v = _volume(card)
    s = _ok(_open(card, v.id))
    _ok(_shot(card, v.id, s["id"], 0))
    skipped = _ok(card.post("operator", f"/collections/{v.id}/colour-card/skip"))
    assert skipped["id"] == s["id"] and skipped["outcome"] == "skipped" and skipped["shots"] == []
    assert list((card.root / v.filename_stem / "calibration").iterdir()) == []


@pytest.mark.unit
def test_a_changed_zoom_asks_again_and_an_unreadable_camera_is_unknown(card):
    v = _volume(card)
    _take_set(card, v.id)
    card.backend.zoom[0] = 4
    assert (_state(card, v.id)["needed"], _state(card, v.id)["reason"]) == (True, "settings_changed")
    card.backend.zoom[0] = 3
    assert _state(card, v.id)["needed"] is False
    card.backend.unreadable.add(1)
    assert (_state(card, v.id)["needed"], _state(card, v.id)["reason"]) == (None, None)


@pytest.mark.unit
def test_a_dslrs_exposure_is_read_only_when_asked(card):
    card.backend.name = "gphoto2"
    v = _volume(card)
    _take_set(card, v.id)
    card.backend.reads.clear()
    assert _state(card, v.id)["needed"] is None
    assert card.backend.reads == [], "without read_cameras no camera is read"
    assert _state(card, v.id, read_cameras=1)["needed"] is False
    card.backend.exposure[1]["shutter_speed"] = "1/30"
    state = _state(card, v.id, read_cameras=1)
    assert (state["needed"], state["reason"]) == (True, "settings_changed")


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_volume_without_a_stem_is_refused(card):
    v = _volume(card, stem=None, name="Sin raíz")
    _code(_open(card, v.id), 409, "stem_required")
    _code(card.post("operator", f"/collections/{v.id}/colour-card/skip"), 409, "stem_required")


@pytest.mark.unit
def test_a_shot_below_the_storage_reserve_is_refused(card, monkeypatch):
    from fastapi import HTTPException
    from app.core import storage_override

    def refuse(root):
        raise HTTPException(status_code=507, detail={"code": "storage_reserve", "message": "full"})
    v = _volume(card)
    s = _ok(_open(card, v.id))
    monkeypatch.setattr(storage_override, "refuse_below_storage_reserve", refuse)
    shots = list(card.backend.requested)
    _code(_shot(card, v.id, s["id"], 0), 507, "storage_reserve")
    assert card.backend.requested == shots, "a refused shot never reaches the camera"
    assert not (card.root / v.filename_stem / "calibration").exists()


@pytest.mark.unit
def test_a_camera_already_taking_a_shot_is_busy(card):
    from app.core import colour_card
    v = _volume(card)
    s = _ok(_open(card, v.id))
    guard = colour_card._camera_guard(0)
    assert guard.acquire(timeout=5)
    try:
        _code(_shot(card, v.id, s["id"], 0), 409, "camera_busy")
    finally:
        guard.release()
    _ok(_shot(card, v.id, s["id"], 0))


@pytest.mark.unit
def test_a_camera_outside_the_set_and_a_set_of_another_volume_are_refused(card, monkeypatch):
    v = _volume(card)
    other = _volume(card, "xx-card-2", "Otro")
    s = _ok(_open(card, v.id))
    _code(_shot(card, v.id, s["id"], 2), 422, "colour_card_camera_not_in_set")
    _code(_shot(card, other.id, s["id"], 0), 404, "colour_card_set_not_found")
    _code(_complete(card, other.id, s["id"]), 404, "colour_card_set_not_found")
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: False)
    _code(_shot(card, v.id, s["id"], 0), 409, "camera_not_connected")


@pytest.mark.unit
def test_a_failed_shot_leaves_nothing_behind(card):
    v = _volume(card)
    s = _ok(_open(card, v.id))
    card.backend.fail_after_write.add(0)
    _code(_shot(card, v.id, s["id"], 0), 502, "colour_card_capture_failed")
    assert list((card.root / v.filename_stem / "calibration").iterdir()) == []
    assert _state(card, v.id)["sets"][0]["shots"] == []


@pytest.mark.unit
@pytest.mark.parametrize("path,body", [
    ("/colour-card/sets", None), ("/colour-card/skip", None),
    ("/colour-card/sets/1/shots", {"camera_index": 0}), ("/colour-card/sets/1/complete", None),
])
def test_a_reviewer_takes_no_shot(card, path, body):
    v = _volume(card)
    assert card.post("reviewer", f"/collections/{v.id}{path}", body).status_code == 403
    assert card.get("reviewer", f"/collections/{v.id}/colour-card").status_code == 403


# ---------------------------------------------------------------------------
# Deleting a volume
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_deleting_a_volume_deletes_its_sets(card):
    """A volume with a shot reserves its stem and is not deleted (below); one whose sets hold none is, with them."""
    from app.models.colour_card import ColourCardSet
    v = _volume(card)
    s = _ok(_open(card, v.id))
    _ok(_shot(card, v.id, s["id"], 0))
    _ok(card.post("operator", f"/collections/{v.id}/colour-card/skip"))
    _ok(card.post("operator", f"/collections/{v.id}/colour-card/skip"))
    resp = card.client.delete(f"/collections/{v.id}", headers=card.headers["operator"])
    assert resp.status_code == 204, resp.text
    card.db.expire_all()
    assert card.db.query(ColourCardSet).count() == 0
    folder = card.root / v.filename_stem / "calibration"
    assert not folder.exists() or list(folder.iterdir()) == []


# ---------------------------------------------------------------------------
# Shots fix the volume's stem, as captures do
# ---------------------------------------------------------------------------

def _patch_stem(unit, vid, stem):
    return unit.client.patch(f"/collections/{vid}", headers=unit.headers["admin"], json={"filename_stem": stem})


@pytest.mark.unit
def test_a_shot_fixes_the_volumes_stem(card):
    v = _volume(card)
    s = _ok(_open(card, v.id))
    assert _patch_stem(card, v.id, "xx-card-9").status_code == 200, "an open set with no shot fixes nothing"
    _ok(_shot(card, v.id, s["id"], 0))
    resp = _patch_stem(card, v.id, "xx-card-8")
    assert resp.status_code == 409, resp.text
    card.db.expire_all()
    from app.models.collection import Collection
    assert card.db.get(Collection, v.id).filename_stem == "xx-card-9"
    assert card.db.get(Collection, v.id).reserves_its_stem() is True


@pytest.mark.unit
def test_a_volume_whose_only_shots_were_skipped_away_reserves_its_stem_no_more(card):
    v = _volume(card)
    s = _ok(_open(card, v.id))
    _ok(_shot(card, v.id, s["id"], 0))
    _ok(card.post("operator", f"/collections/{v.id}/colour-card/skip"))
    assert _patch_stem(card, v.id, "xx-card-7").status_code == 200


@pytest.mark.unit
def test_a_volume_with_taken_shots_is_not_deleted(card):
    v = _volume(card)
    _take_set(card, v.id)
    resp = card.client.delete(f"/collections/{v.id}", headers=card.headers["operator"])
    assert resp.status_code == 409, resp.text
    assert len(list((card.root / v.filename_stem / "calibration").iterdir())) == 2


# ---------------------------------------------------------------------------
# Taking a shot again: the earlier one goes only once the new one is saved
# ---------------------------------------------------------------------------

def _shot_state(card, v):
    import hashlib
    from app.models.colour_card import ColourCardShot
    card.db.expire_all()
    shot = card.db.query(ColourCardShot).one()
    folder = card.root / v.filename_stem / "calibration"
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
    return (shot.capture_id, shot.sha256, shot.preview_sha256), files


class _Boom(Exception):
    pass


def _fail_hash(monkeypatch):
    import capture.utils
    real = capture.utils.compute_sha256
    calls = []

    def hash_(path):
        calls.append(path)
        raise _Boom("hash")
    monkeypatch.setattr(capture.utils, "compute_sha256", hash_)
    return real


def _fail_second_replace(monkeypatch):
    from app.core import colour_card
    real = colour_card.os.replace
    n = []

    def replace(a, b):
        n.append(1)
        if len(n) == 2:
            raise OSError("disk")
        return real(a, b)
    monkeypatch.setattr(colour_card.os, "replace", replace)


def _fail_manifest(monkeypatch):
    import capture.manifestHandler
    monkeypatch.setattr(capture.manifestHandler, "append_calibration_record",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("manifest")))


def _fail_commit(monkeypatch, card):
    real = card.db.commit
    from app.models.colour_card import ColourCardShot

    def commit():
        if any(isinstance(o, ColourCardShot) for o in card.db.dirty):
            raise _Boom("commit")
        return real()
    monkeypatch.setattr(card.db, "commit", commit)


@pytest.mark.unit
@pytest.mark.parametrize("step", ["hash", "rename", "manifest", "commit"])
def test_a_retake_that_fails_leaves_the_earlier_shot_as_it_was(card, monkeypatch, step):
    card.backend.raw = True
    v = _volume(card)
    s = _ok(_open(card, v.id))
    _ok(_shot(card, v.id, s["id"], 0))
    before = _shot_state(card, v)
    if step == "hash":
        _fail_hash(monkeypatch)
    elif step == "rename":
        _fail_second_replace(monkeypatch)
    elif step == "manifest":
        _fail_manifest(monkeypatch)
    else:
        _fail_commit(monkeypatch, card)
    lines = len(_manifest_lines(card, v.filename_stem))
    _code(_shot(card, v.id, s["id"], 0), 502, "colour_card_capture_failed")
    monkeypatch.undo()
    assert _shot_state(card, v) == before, "the row, the files and their digests are the earlier shot's"
    if step != "commit":
        assert len(_manifest_lines(card, v.filename_stem)) == lines


# ---------------------------------------------------------------------------
# The volume list touches no volume's files; it reads the adoption journal once
# ---------------------------------------------------------------------------

def _list_queries(card, monkeypatch):
    """(queries the list's colour_card objects take, the list's body), with every file look refused.

    Path.exists and Path.is_file are refused for the whole request, so no
    volume's file is looked at. The list's one other read of the disk is
    the adoption journal (app.core.adopt_journal.journaled_volume_ids),
    opened once for every volume it lists.
    """
    import pathlib
    from sqlalchemy import event
    from app.core import colour_card
    engine = card.db.get_bind()
    statements, inside = [], []

    def count(conn, cursor, statement, *a):
        if inside:
            statements.append(statement)
    real = colour_card.summaries

    def counted(db, volumes):
        inside.append(1)
        try:
            return real(db, volumes)
        finally:
            inside.clear()
    event.listen(engine, "before_cursor_execute", count)

    def no_file(*a, **k):
        raise AssertionError("the volume list touched the file system")
    with monkeypatch.context() as m:
        m.setattr(colour_card, "summaries", counted)
        m.setattr(pathlib.Path, "exists", no_file)
        m.setattr(pathlib.Path, "is_file", no_file)
        resp = card.get("operator", "/collections/")
    event.remove(engine, "before_cursor_execute", count)
    assert resp.status_code == 200, resp.text
    assert statements, "the list's colour_card objects were not built through colour_card.summaries"
    return len(statements), resp.json()


@pytest.mark.unit
def test_the_list_takes_as_many_queries_for_fifty_volumes_as_for_one_and_reads_no_file(card, monkeypatch):
    v = _volume(card)
    _take_set(card, v.id)
    one, _ = _list_queries(card, monkeypatch)
    for n in range(49):
        w = _volume(card, f"xx-card-n{n}", f"Tomo {n}")
        _take_set(card, w.id) if n % 2 else card.post("operator", f"/collections/{w.id}/colour-card/skip")
    fifty, body = _list_queries(card, monkeypatch)
    assert one == fifty
    assert {c["colour_card"]["status"] for c in body if c["colour_card"].get("enabled")} >= {"present", "skipped"}


@pytest.mark.unit
def test_the_lists_status_is_from_rows_only(card, monkeypatch):
    from app.models.colour_card import ColourCardShot
    v = _volume(card)
    _take_set(card, v.id)
    shot = card.db.query(ColourCardShot).first()
    (card.root / v.filename_stem / "calibration" / shot.filename).unlink()
    _count, body = _list_queries(card, monkeypatch)
    assert {c["id"]: c["colour_card"] for c in body}[v.id] == {"enabled": True, "status": "present", "sets": 1}
    assert _state(card, v.id)["status"] == "skipped"


# ---------------------------------------------------------------------------
# The subprocess driver takes one capture at a time per camera
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_subprocess_driver_takes_one_capture_at_a_time_per_camera(tmp_path, monkeypatch):
    """A page capture held inside rpicam-still makes a shot on the same camera wait, as the other drivers' locks do."""
    import logging
    import subprocess
    from capture.backends import subprocess_backend
    from capture.camera import CameraConfig
    driver = subprocess_backend.RpicamBackend(logging.getLogger("test"))
    inside, release, order = threading.Event(), threading.Event(), []

    def run(command, **kw):
        out = command[command.index("-o") + 1]
        order.append(("start", out))
        if out.endswith("page.jpg"):
            inside.set()
            assert release.wait(10)
        order.append(("end", out))
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(subprocess_backend.subprocess, "run", run)
    page = threading.Thread(target=driver.capture_image, args=(tmp_path / "page.jpg", CameraConfig(camera_index=0)))
    page.start()
    assert inside.wait(10)
    shot = threading.Thread(target=driver.capture_image, args=(tmp_path / "shot.jpg", CameraConfig(camera_index=0)))
    shot.start()
    shot.join(0.3)
    assert shot.is_alive(), "the shot waits while the page capture holds the camera"
    other = threading.Thread(target=driver.capture_image, args=(tmp_path / "other.jpg", CameraConfig(camera_index=1)))
    other.start()
    other.join(5)
    assert not other.is_alive(), "another camera does not wait"
    release.set()
    page.join(5)
    shot.join(5)
    assert not page.is_alive() and not shot.is_alive()
    assert ("end", str(tmp_path / "page.jpg")) in order and order.index(("end", str(tmp_path / "page.jpg"))) < \
        order.index(("start", str(tmp_path / "shot.jpg")))


@pytest.mark.unit
def test_a_raw_shot_without_a_preview_says_it_cannot_be_shown(card):
    card.backend.raw = True
    real = card.backend.capture_image

    def no_preview(output_path, camera_config, capture_output=False):
        from app.core.paths import preview_sidecar_of
        out, meta = real(output_path, camera_config, capture_output)
        preview_sidecar_of(out).unlink()
        return out, meta
    card.backend.capture_image = no_preview
    v = _volume(card)
    s = _ok(_open(card, v.id))
    shot = _ok(_shot(card, v.id, s["id"], 0))
    assert shot["preview"] is False and shot["preview_filename"] is None
    card.backend.capture_image = real
    jpeg_volume = _volume(card, "xx-card-2", "Otro")
    card.backend.raw = False
    t = _ok(_open(card, jpeg_volume.id))
    assert _ok(_shot(card, jpeg_volume.id, t["id"], 0))["preview"] is True
    assert _state(card, v.id)["sets"][0]["shots"][0]["preview"] is False
