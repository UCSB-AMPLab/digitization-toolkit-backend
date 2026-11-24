"""An adoption of a source volume that has colour card shots, and the capture requests of its records.

A source volume's colour card sets, shots, their files in `calibration/`
and their offsite objects (colour_card_objects) are adopted with its pages:
each shot's file and a RAW shot's preview renamed to the target's stem
keeping the set number and the camera (`<target stem>-cal_<nn>_c<n>.<ext>`),
the sets moved to the target, the old objects' keys recorded as retired and
the shots uploaded again under the target's keys, as pages are. The checks
that refuse an adoption (every file on the unit, every object verified
offsite, no new name taken) cover the shots. The target's capture manifest
gets the shots' `calibration` lines under their new paths, and the target's
Fisqua manifest (version 1.12) carries the shots in `calibration`.

The source is that of tests/unit/test_adopt_apply.py, with a taken set of
two RAW shots (cameras 0 and 1), each with its preview, a `calibration`
line in the source's manifest written by the codebase's own writer, and a
verified colour_card_objects row for each shot and preview.

A capture request (app.core.capture_requests) names its record and images
by id. An adoption keeps both ids, so a request replayed after it is
answered from what was stored, with the image's path in the target.

    python -m pytest tests/unit/test_adopt_colour_card.py

Version: 0.3.3
"""

import errno
import hashlib
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from tests.unit.test_adopt_apply import _adopt, _journal, _manifest, _setup, _snapshot
from tests.unit.test_adopt_captures_plan import SRC, TGT, TENANT, _plan, own_data_dir, tenant  # noqa: F401
from tests.unit.test_image_upload import remote  # noqa: F401

OPENED = datetime(2026, 11, 19, 9, 0)


def _shots(db, vol, root, *, verified=True):
    """A taken set (number 1) in `vol` with a RAW shot from cameras 0 and 1, each with its preview, on disk."""
    from app.core.paths import CALIBRATION_DIR
    from app.models.colour_card import ColourCardObject, ColourCardSet, ColourCardShot
    from capture.manifestHandler import append_calibration_record

    card_set = ColourCardSet(collection_id=vol.id, number=1, outcome="taken", capture_mode="dual", cameras=[],
                             opened_at=OPENED, completed_at=OPENED, after_image_id=0)
    db.add(card_set)
    db.commit()
    folder = root / vol.filename_stem / CALIBRATION_DIR
    folder.mkdir(parents=True, exist_ok=True)
    shots = []
    for cam in (0, 1):
        name = f"{vol.filename_stem}-cal_01_c{cam}.CR2"
        pname = f"{vol.filename_stem}-cal_01_c{cam}_preview.jpg"
        data, pdata = f"shot {cam}".encode(), f"preview {cam}".encode()
        (folder / name).write_bytes(data)
        (folder / pname).write_bytes(pdata)
        shot = ColourCardShot(set_id=card_set.id, camera_index=cam, capture_id=str(uuid.uuid4()), filename=name,
                              file_path=str(folder / name), file_size=len(data),
                              sha256=hashlib.sha256(data).hexdigest(), preview_filename=pname,
                              preview_path=str(folder / pname), preview_sha256=hashlib.sha256(pdata).hexdigest(),
                              captured_at=OPENED)
        db.add(shot)
        db.commit()
        append_calibration_record(root / vol.filename_stem, {
            "capture_id": shot.capture_id, "timestamp_utc": "2026-11-19T09:00:00+00:00", "set_number": 1,
            "camera_index": cam, "hardware_id": None, "role": None,
            "files": [{"role": "calibration", "relative_path": f"{CALIBRATION_DIR}/{name}", "bytes": len(data),
                       "mimetype": "application/octet-stream", "sha256": shot.sha256, "rotate_deg": 0},
                      {"role": "calibration_preview", "relative_path": f"{CALIBRATION_DIR}/{pname}",
                       "bytes": len(pdata), "mimetype": "image/jpeg", "sha256": shot.preview_sha256,
                       "rotate_deg": 0}],
            "cameras": [], "settings": {}})
        if verified:
            for kind, file_name, body in (("shot", name, data), ("preview", pname, pdata)):
                db.add(ColourCardObject(shot_id=shot.id, kind=kind,
                                        object_key=f"{TENANT}/{vol.filename_stem}/calibration/{file_name}",
                                        local_sha1=hashlib.sha1(body).hexdigest(), uploaded_at=OPENED,
                                        verified_at=OPENED))
            db.commit()
        shots.append(shot)
    return card_set, shots


def _with_shots(db, root, **kw):
    src, tgt, images, _ = _setup(db, root)
    card_set, shots = _shots(db, src, root, **kw)
    return src, tgt, images, card_set, shots


def _shot_renames(plan) -> dict:
    return {r.shot_id: r for r in plan.shot_renames}


# ---------------------------------------------------------------------------
# (a) The plan covers the shots
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_plan_renames_each_shot_into_the_targets_calibration_folder(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, _, _, shots = _with_shots(db_session, root)

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == [], plan.details
    renames = _shot_renames(plan)
    assert sorted(renames) == sorted(s.id for s in shots)
    for shot in shots:
        r = renames[shot.id]
        cam = shot.camera_index
        assert (r.old_name, r.new_name) == (shot.filename, f"{TGT}-cal_01_c{cam}.CR2")
        assert r.old_path == Path(shot.file_path) and r.new_path == root / TGT / "calibration" / r.new_name
        assert r.new_preview_path == root / TGT / "calibration" / f"{TGT}-cal_01_c{cam}_preview.jpg"
        assert r.new_keys == {"shot": f"{TENANT}/{TGT}/calibration/{r.new_name}",
                              "preview": f"{TENANT}/{TGT}/calibration/{r.new_preview_path.name}"}
    assert not [e for e in plan.uncovered if "calibration" in e["path"]], plan.uncovered


@pytest.mark.unit
def test_a_shot_file_missing_from_disk_refuses(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, _, _, shots = _with_shots(db_session, root)
    Path(shots[1].preview_path).unlink()

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["source_file_missing"]
    assert plan.details["source_file_missing"] == [{"shot_id": shots[1].id, "kind": "colour_card_preview",
                                                    "path": shots[1].preview_path}]


@pytest.mark.unit
def test_a_shot_not_verified_offsite_refuses(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, _, _, shots = _with_shots(db_session, root, verified=False)

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["source_not_verified_offsite"]
    assert {(d.get("shot_id"), d["kind"]) for d in plan.details["source_not_verified_offsite"]} == {
        (s.id, kind) for s in shots for kind in ("shot", "preview")}


@pytest.mark.unit
def test_a_file_at_a_shots_new_name_refuses(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, _, _, shots = _with_shots(db_session, root)
    taken = root / TGT / "calibration" / f"{TGT}-cal_01_c0.CR2"
    taken.parent.mkdir(parents=True, exist_ok=True)
    taken.write_bytes(b"already here")

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["new_name_collides"]
    assert {"shot_id": shots[0].id, "with": "file", "path": str(taken)} in plan.details["new_name_collides"]


# ---------------------------------------------------------------------------
# (a) The adoption moves them, and puts them back on failure
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_adoption_moves_the_shots_and_retires_their_old_keys(db_session, override_projects_root, tenant):
    from app.core.integrity import read_manifest_lines
    from app.core.offsite_retired import retired_keys
    from app.models.colour_card import ColourCardObject, ColourCardSet, ColourCardShot

    root = override_projects_root
    src, tgt, _, card_set, shots = _with_shots(db_session, root)
    old_keys = sorted(o.object_key for o in db_session.query(ColourCardObject))
    old_paths = {s.id: (Path(s.file_path), Path(s.preview_path)) for s in shots}
    bytes_of = {s.id: (Path(s.file_path).read_bytes(), Path(s.preview_path).read_bytes()) for s in shots}

    _adopt(db_session, src, tgt)

    db_session.expire_all()
    assert db_session.get(ColourCardSet, card_set.id).collection_id == tgt.id
    for shot_id, (old_master, old_preview) in old_paths.items():
        shot = db_session.get(ColourCardShot, shot_id)
        name = f"{TGT}-cal_01_c{shot.camera_index}.CR2"
        new_master = root / TGT / "calibration" / name
        new_preview = new_master.with_name(f"{new_master.stem}_preview.jpg")
        assert (shot.filename, shot.file_path) == (name, str(new_master))
        assert (shot.preview_filename, shot.preview_path) == (new_preview.name, str(new_preview))
        assert (new_master.read_bytes(), new_preview.read_bytes()) == bytes_of[shot_id]
        assert not old_master.exists() and not old_preview.exists()
    old = db_session.query(ColourCardObject).filter(ColourCardObject.object_key.in_(old_keys)).all()
    assert len(old) == 4 and all(o.shot_id is None and o.retired_at is not None for o in old)
    assert old_keys == sorted(k["key"] for k in retired_keys(db_session) if k["kind"].startswith("colour_card_"))
    lines = [l for l in read_manifest_lines(_manifest(root, TGT))[0] if '"calibration"' in l]
    assert len(lines) == 2 and all(f"calibration/{TGT}-cal_01_c" in l for l in lines)


@pytest.mark.unit
def test_a_shot_rename_that_fails_puts_every_file_and_row_back(db_session, override_projects_root, tenant, monkeypatch):
    from app.core import adopt_apply
    from app.core.adopt_apply import AdoptFailed

    root = override_projects_root
    src, tgt, _, _, _ = _with_shots(db_session, root)
    before = _snapshot(db_session, root)
    real = adopt_apply._rename

    def rename(old, new):
        if "-cal_" in Path(new).name:
            raise OSError(errno.EIO, "I/O error")
        real(old, new)

    monkeypatch.setattr(adopt_apply, "_rename", rename)

    with pytest.raises(AdoptFailed) as failed:
        _adopt(db_session, src, tgt)

    assert failed.value.restored is True
    assert _snapshot(db_session, root) == before
    assert not _journal().exists()


# ---------------------------------------------------------------------------
# (d) The target's Fisqua manifest carries the adopted shots
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_targets_fisqua_manifest_carries_the_adopted_shots(remote, db_session, override_projects_root):
    from app.core import unit_mode
    from app.core.capture_manifest import VERSION, build_capture_manifest
    from app.core.image_upload import run_image_upload
    from app.core.offsite_retired import retired_keys
    from app.models.collection import Collection
    from app.models.colour_card import ColourCardObject
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record

    root = override_projects_root
    src, tgt, _, _, shots = _with_shots(db_session, root)
    for row in db_session.query(OffsiteObject).all():
        folder = "captures" if row.kind == "capture" else "previews"
        remote.put(row.object_key, (root / SRC / folder / row.object_key.rsplit("/", 1)[1]).read_bytes())
    for row in db_session.query(ColourCardObject).all():
        remote.put(row.object_key, (root / SRC / "calibration" / row.object_key.rsplit("/", 1)[1]).read_bytes())
    unit_mode.set_colour_card_shots(db_session, True, actor="admin")

    _adopt(db_session, src, tgt)
    status = run_image_upload(db_session)
    assert status["status"] == "done", status
    for rec in db_session.query(Record).filter(Record.collection_id == tgt.id):
        rec.status = "approved"
    db_session.commit()

    shot_keys = [k for k in retired_keys(db_session) if k["kind"].startswith("colour_card_")]
    assert len(shot_keys) == 4 and all(k["deletable"] for k in shot_keys)
    assert all(k["current_key"].startswith(f"{TENANT}/{TGT}/calibration/{TGT}-cal_01_c") for k in shot_keys)

    doc = build_capture_manifest(db_session, db_session.get(Collection, tgt.id))

    assert doc["version"] == VERSION == "1.12"
    volume = doc["volumes"][0]
    assert volume["calibrationStatus"] == "present"
    assert sorted(e["filename"] for e in volume["calibration"]) == [f"{TGT}-cal_01_c0.CR2", f"{TGT}-cal_01_c1.CR2"]
    for entry in volume["calibration"]:
        assert entry["objectKey"] == f"{TGT}/calibration/{entry['filename']}"
        assert entry["previewObjectKey"] == f"{TGT}/calibration/{Path(entry['filename']).stem}_preview.jpg"


# ---------------------------------------------------------------------------
# (c) A capture request replayed after the adoption
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_capture_request_replayed_after_an_adoption_answers_the_stored_capture(db_session, override_projects_root,
                                                                                  tenant):
    from app.core import capture_requests
    from app.models.capture_request import StoredCaptureRequest

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    r = plan.renames[0]
    request_id, fingerprint = str(uuid.uuid4()), "f" * 64
    db_session.add(StoredCaptureRequest(client_capture_id=request_id, space="real", kind="single",
                                        collection_id=src.id, record_id=r.record_id, image_ids=[r.image_id],
                                        request_fingerprint=fingerprint))
    db_session.commit()

    _adopt(db_session, src, tgt)
    db_session.expire_all()

    body = capture_requests.answer(db_session, request_id, "real", fingerprint)
    assert body == {"success": True, "record_id": r.record_id, "image_ids": [r.image_id], "replayed": True,
                    "file_path": str(r.new_path)}
    assert capture_requests.lookup(db_session, request_id, "real")["file_path"] == str(r.new_path)


# ---------------------------------------------------------------------------
# Open and skipped sets
# ---------------------------------------------------------------------------

def _set(db, vol, number, outcome):
    from app.models.colour_card import ColourCardSet

    card_set = ColourCardSet(collection_id=vol.id, number=number, outcome=outcome, capture_mode="dual", cameras=[],
                             opened_at=OPENED, completed_at=None if outcome is None else OPENED, after_image_id=0)
    db.add(card_set)
    db.commit()
    return card_set


@pytest.mark.unit
def test_a_source_with_an_open_set_is_refused(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, _, _, _ = _with_shots(db_session, root)
    open_set = _set(db_session, src, 2, None)

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["source_has_open_colour_card_set"]
    assert plan.details["source_has_open_colour_card_set"] == [open_set.id]


@pytest.mark.unit
def test_a_skipped_set_moves_to_the_target_with_the_others(db_session, override_projects_root, tenant):
    from app.models.colour_card import ColourCardSet

    root = override_projects_root
    src, tgt, _, taken, _ = _with_shots(db_session, root)
    skipped = _set(db_session, src, 2, "skipped")

    _adopt(db_session, src, tgt)

    db_session.expire_all()
    assert {s.id for s in db_session.query(ColourCardSet).filter(ColourCardSet.collection_id == tgt.id)} == {
        taken.id, skipped.id}
    assert db_session.query(ColourCardSet).filter(ColourCardSet.collection_id == src.id).count() == 0


@pytest.mark.unit
def test_a_source_with_only_a_skipped_set_moves_it(db_session, override_projects_root, tenant):
    from app.models.colour_card import ColourCardSet

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    skipped = _set(db_session, src, 1, "skipped")

    _adopt(db_session, src, tgt)

    db_session.expire_all()
    assert db_session.get(ColourCardSet, skipped.id).collection_id == tgt.id


# ---------------------------------------------------------------------------
# The source's manifest records where its shots went
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_sources_manifest_records_where_each_shot_went(db_session, override_projects_root, tenant):
    import json

    root = override_projects_root
    src, tgt, _, _, shots = _with_shots(db_session, root)
    before = _manifest(root, SRC).read_bytes()

    _adopt(db_session, src, tgt)

    added = [json.loads(l) for l in _manifest(root, SRC).read_bytes()[len(before):].decode().splitlines() if l]
    by_capture = {l["capture_id"]: l for l in added}
    for shot in shots:
        line = by_capture[shot.capture_id]
        assert line["record_type"] == "relocation" and line["shot_id"] == shot.id
        assert line["sha256"] == shot.sha256 and "image_id" not in line
        assert line["from"] == f"calibration/{SRC}-cal_01_c{shot.camera_index}.CR2"
        assert line["to"] == f"../{TGT}/calibration/{TGT}-cal_01_c{shot.camera_index}.CR2"


# ---------------------------------------------------------------------------
# Keys under a stem, and the source deleted afterwards
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_shot_key_under_the_targets_stem_refuses(db_session, override_projects_root, tenant):
    from app.models.colour_card import ColourCardObject

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    db_session.add(ColourCardObject(shot_id=None, kind="shot", object_key=f"{TENANT}/{TGT}/calibration/x.jpg",
                                    local_sha1="3" * 40))
    db_session.commit()

    assert _plan(db_session, src, tgt).refusals == ["target_has_offsite_keys"]


@pytest.mark.unit
def test_a_source_whose_shots_were_adopted_away_is_deletable_and_its_stem_stays_held(client, db_session,
                                                                                     override_projects_root, tenant):
    from app.models.collection import Collection, offsite_keys_under_stem
    from tests.unit.test_offsite_free import _as

    root = override_projects_root
    src, tgt, _, _, shots = _with_shots(db_session, root)
    _adopt(db_session, src, tgt)
    src_id = src.id
    new_paths = [Path(p) for s in shots for p in (s.file_path, s.preview_path)]
    db_session.expire_all()
    new_paths = [root / TGT / "calibration" / p.name.replace(SRC, TGT) for p in new_paths]
    _as("admin")

    r = client.delete(f"/collections/{src_id}")

    assert r.status_code == 204, r.text
    db_session.expire_all()
    assert db_session.get(Collection, src_id) is None
    assert all(p.is_file() for p in new_paths)
    assert offsite_keys_under_stem(db_session, SRC)


@pytest.mark.unit
def test_a_source_with_a_shot_key_not_retired_is_not_adopted_away(db_session, override_projects_root, tenant):
    from app.models.collection import Collection
    from app.models.colour_card import ColourCardObject

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    _adopt(db_session, src, tgt)
    db_session.add(ColourCardObject(shot_id=None, kind="shot", object_key=f"{TENANT}/{SRC}/calibration/y.jpg",
                                    local_sha1="4" * 40, verified_at=OPENED))
    db_session.commit()

    assert db_session.get(Collection, src.id).deletable_after_adoption() is False


# ---------------------------------------------------------------------------
# GET /system/offsite/retired-keys lists the shots' keys with the pages'
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_retired_keys_route_lists_shot_keys_with_their_own_kinds(client, db_session, override_projects_root,
                                                                       tenant):
    from tests.unit.test_offsite_free import _as

    root = override_projects_root
    src, tgt, _, _, shots = _with_shots(db_session, root)
    _adopt(db_session, src, tgt)
    _as("admin")

    body = client.get("/system/offsite/retired-keys").json()

    kinds = {}
    for k in body["keys"]:
        kinds.setdefault(k["kind"], []).append(k)
    assert sorted(kinds) == ["capture", "colour_card_preview", "colour_card_shot", "preview"]
    assert sorted(k["shot_id"] for k in kinds["colour_card_shot"]) == sorted(s.id for s in shots)
    assert all(k["volume_id"] == tgt.id and k["volume_stem"] == TGT for k in kinds["colour_card_shot"])
    assert body["totals"]["keys"] == 14 + 4


# ---------------------------------------------------------------------------
# A kill, then the next start: everything put back or everything moved
# ---------------------------------------------------------------------------

class Killed(BaseException):
    """The process dies here."""


def _inventory(root: Path) -> list:
    return sorted(hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*")
                  if p.is_file() and p.name != "manifest.jsonl")


def _recover():
    from app.core.adopt_journal import recover_adoption_at_startup
    from app.core.db import SessionLocal

    fresh = SessionLocal()
    try:
        return recover_adoption_at_startup(fresh)
    finally:
        fresh.close()


@pytest.mark.unit
def test_a_kill_after_a_shot_rename_is_undone_at_the_next_start(db_session, override_projects_root, tenant):
    from app.core import adopt_apply

    root = override_projects_root
    src, tgt, _, _, _ = _with_shots(db_session, root)
    before, inventory = _snapshot(db_session, root), _inventory(root)
    real = adopt_apply._rename

    def rename(old, new):
        real(old, new)
        if "-cal_" in Path(new).name:
            raise Killed()

    with pytest.MonkeyPatch.context() as kill:
        kill.setattr(adopt_apply, "_rename", rename)
        with pytest.raises(Killed):
            _adopt(db_session, src, tgt)
    assert _journal().exists()

    report = _recover()

    assert report["outcome"] == "reverted", report
    assert _snapshot(db_session, root) == before
    assert _inventory(root) == inventory
    assert not _journal().exists()


@pytest.mark.unit
def test_a_kill_after_the_rows_are_committed_is_finished_at_the_next_start(db_session, override_projects_root, tenant):
    from app.core import adopt_journal
    from app.core.integrity import read_manifest_lines
    from app.models.colour_card import ColourCardSet, ColourCardShot

    root = override_projects_root
    src, tgt, _, card_set, shots = _with_shots(db_session, root)
    inventory = _inventory(root)

    def update(payload):
        raise Killed()

    with pytest.MonkeyPatch.context() as kill:
        kill.setattr(adopt_journal, "update_journal", update)
        with pytest.raises(Killed):
            _adopt(db_session, src, tgt)
    assert _journal().exists()

    report = _recover()

    assert report["outcome"] == "completed", report
    db_session.expire_all()
    assert db_session.get(ColourCardSet, card_set.id).collection_id == tgt.id
    for shot in shots:
        row = db_session.get(ColourCardShot, shot.id)
        assert Path(row.file_path).is_file() and Path(row.preview_path).is_file()
        assert Path(row.file_path).parent == root / TGT / "calibration"
    assert not list((root / SRC / "calibration").glob("*"))
    assert _inventory(root) == inventory
    lines = [l for l in read_manifest_lines(_manifest(root, TGT))[0] if '"calibration"' in l]
    assert len(lines) == 2
    assert not _journal().exists()


# ---------------------------------------------------------------------------
# No shot is taken in a volume an unfinished adoption names
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("which", ["source", "target"])
def test_colour_card_changes_are_refused_while_a_journal_names_the_volume(db_session, override_projects_root, tenant,
                                                                           which):
    from fastapi import HTTPException

    from app.core import colour_card
    from app.models.user import User

    root = override_projects_root
    src, tgt, _, card_set, _ = _with_shots(db_session, root)
    _journal().write_text('{"version": 1, "run_id": "x", "source_id": %d, "target_id": %d}' % (src.id, tgt.id))
    vol = src if which == "source" else tgt
    user = User(username="op", role="operator")

    for call in (lambda: colour_card.open_set(db_session, vol.id, user),
                 lambda: colour_card.skip(db_session, vol.id, user),
                 lambda: colour_card.complete(db_session, vol.id, card_set.id, user)):
        with pytest.raises(HTTPException) as refused:
            call()
        assert refused.value.status_code == 409 and refused.value.detail["code"] == "adoption_unfinished"


# ---------------------------------------------------------------------------
# The routes show the shots
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_dry_run_and_the_result_show_the_sets_and_shots(client, db_session, override_projects_root, tenant,
                                                            monkeypatch):
    from app.core import adopt_apply
    from tests.unit.test_offsite_free import _as

    monkeypatch.setattr(adopt_apply, "bucket_keys_under_stem", lambda tenant, stem: [])
    root = override_projects_root
    src, tgt, _, card_set, shots = _with_shots(db_session, root)
    _as("admin")

    plan = client.get(f"/collections/{src.id}/adopt-into/{tgt.id}").json()

    assert plan["colour_card_set_ids"] == [card_set.id]
    assert [r["new_name"] for r in plan["shot_renames"]] == [f"{TGT}-cal_01_c0.CR2", f"{TGT}-cal_01_c1.CR2"]
    assert plan["shot_renames"][0]["new_keys"]["shot"] == f"{TENANT}/{TGT}/calibration/{TGT}-cal_01_c0.CR2"
    assert plan["shot_bytes"] > 0

    done = client.post(f"/collections/{src.id}/adopt-into/{tgt.id}")

    assert done.status_code == 200, done.text
    assert (done.json()["colour_card_sets"], done.json()["colour_card_shots"]) == (1, 2)


# ---------------------------------------------------------------------------
# A kept target whose adopted shots are offsite only under retired keys
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_target_whose_shots_are_offsite_only_under_retired_keys_cannot_be_kept(client, db_session,
                                                                                  override_projects_root, tenant):
    """Adopt; delete the target's records before the new uploads run; keep the target: already_left_unit.

    The pages' retired rows lose their image with the records (retired_image_id is set null), so only the
    shots' retired rows, linked by retired_shot_id, say that the volume's files are offsite.
    """
    from app.core import kept_volumes
    from app.core.image_upload import colour_card_work
    from app.models.record import Record
    from tests.unit.test_offsite_free import _as

    root = override_projects_root
    src, tgt, _, _, shots = _with_shots(db_session, root)
    _adopt(db_session, src, tgt)
    _as("admin")
    for rec_id in [r.id for r in db_session.query(Record).filter(Record.collection_id == tgt.id)]:
        assert client.delete(f"/records/{rec_id}").status_code in (200, 204)
    db_session.expire_all()
    assert len(colour_card_work(db_session)) == 4

    with pytest.raises(kept_volumes.KeepRefused) as refused:
        kept_volumes.keep(db_session, tgt.id, "admin")

    assert refused.value.code == kept_volumes.ALREADY_LEFT_UNIT
    db_session.expire_all()
    assert len(colour_card_work(db_session)) == 4


# ---------------------------------------------------------------------------
# The unfinished-adoption guard of a colour card change is read under the volume's lock
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_journal_written_while_a_colour_card_change_waited_for_the_lock_refuses_it(db_session,
                                                                                      override_projects_root,
                                                                                      tenant, monkeypatch):
    """The journal appears between the request's first check and its lock, as when it waited for an adoption."""
    from fastapi import HTTPException

    from app.core import colour_card, volume_stem
    from app.models.colour_card import ColourCardSet
    from app.models.user import User

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    monkeypatch.setattr(colour_card, "_refuse_unless_asked", lambda db, volume: None)
    real = volume_stem.locked_volume_query

    def lock_after_the_adoption(db, collection_id):
        _journal().write_text('{"version": 1, "run_id": "x", "source_id": %d, "target_id": %d}'
                              % (src.id, tgt.id))
        return real(db, collection_id)

    monkeypatch.setattr(volume_stem, "locked_volume_query", lock_after_the_adoption)

    with pytest.raises(HTTPException) as refused:
        colour_card.open_set(db_session, tgt.id, User(username="op", role="operator"))

    assert refused.value.status_code == 409 and refused.value.detail["code"] == "adoption_unfinished"
    db_session.expire_all()
    assert db_session.query(ColourCardSet).filter(ColourCardSet.collection_id == tgt.id).count() == 0


# ---------------------------------------------------------------------------
# A volume whose captures were adopted away is closed
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_adopted_away_source_takes_no_colour_card_set(db_session, override_projects_root, tenant):
    """Its next set would be number 1 again, under a file name whose old key a retired row holds."""
    from fastapi import HTTPException

    from app.core import colour_card
    from app.models.colour_card import ColourCardSet
    from app.models.user import User

    root = override_projects_root
    src, tgt, _, _, _ = _with_shots(db_session, root)
    _adopt(db_session, src, tgt)
    user = User(username="op", role="operator")

    for call in (lambda: colour_card.open_set(db_session, src.id, user),
                 lambda: colour_card.skip(db_session, src.id, user)):
        with pytest.raises(HTTPException) as refused:
            call()
        assert refused.value.status_code == 409 and refused.value.detail["code"] == "captures_adopted_away"
    db_session.expire_all()
    assert db_session.query(ColourCardSet).filter(ColourCardSet.collection_id == src.id).count() == 0


@pytest.mark.unit
def test_an_adopted_away_source_takes_no_page_capture(db_session, override_projects_root, tenant):
    from fastapi import HTTPException

    from app.api.cameras import _refuse_capture_without_volume_or_stem

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    _adopt(db_session, src, tgt)

    with pytest.raises(HTTPException) as refused:
        _refuse_capture_without_volume_or_stem(db_session, src.id, None)

    assert refused.value.status_code == 409 and refused.value.detail["code"] == "captures_adopted_away"


@pytest.mark.unit
def test_a_source_with_a_set_of_its_own_is_not_adopted_away_and_keeps_its_shot(client, db_session,
                                                                              override_projects_root, tenant):
    """A set and shot written into the emptied source by another path: it is not deletable, and the shot stays."""
    from app.models.collection import Collection
    from tests.unit.test_offsite_free import _as

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    _adopt(db_session, src, tgt)
    _, shots = _shots(db_session, src, root, verified=False)
    _as("admin")

    db_session.expire_all()
    assert db_session.get(Collection, src.id).deletable_after_adoption() is False
    r = client.delete(f"/collections/{src.id}")
    assert r.status_code == 409, r.text
    assert all(Path(s.file_path).is_file() for s in shots)


# ---------------------------------------------------------------------------
# Closure is recorded by the adoption (collections.captures_adopted_at), not derived from keys
# ---------------------------------------------------------------------------

def _adopted_after_a_deleted_record(client, db_session, root):
    """The reviewer's sequence: a verified page record of the source deleted, then its pages and shots adopted.

    The deleted record's offsite row stays detached and not retired, under the source's stem.
    """
    from app.models.record import Record
    from tests.unit.test_offsite_free import _as

    src, tgt, images, card_set, shots = _with_shots(db_session, root)
    _as("admin")
    gone = images[(3, 0)].record_id
    assert client.delete(f"/records/{gone}").status_code in (200, 204)
    db_session.expire_all()
    _adopt(db_session, src, tgt)
    db_session.expire_all()
    assert db_session.query(Record).filter(Record.collection_id == src.id).count() == 0
    return src, tgt


@pytest.mark.unit
def test_a_source_closed_by_its_adoption_takes_no_set_even_with_a_key_it_never_adopted(
        client, db_session, override_projects_root, tenant):
    from fastapi import HTTPException

    from app.api.cameras import _refuse_capture_without_volume_or_stem
    from app.core import colour_card
    from app.models.collection import Collection
    from app.models.user import User

    src, tgt = _adopted_after_a_deleted_record(client, db_session, override_projects_root)

    with pytest.raises(HTTPException) as refused:
        colour_card.open_set(db_session, src.id, User(username="op", role="operator"))
    assert refused.value.status_code == 409 and refused.value.detail["code"] == "captures_adopted_away"
    with pytest.raises(HTTPException) as refused:
        _refuse_capture_without_volume_or_stem(db_session, src.id, None)
    assert refused.value.status_code == 409 and refused.value.detail["code"] == "captures_adopted_away"
    # Its delete is still governed by its keys: the deleted record's key is not retired.
    assert client.delete(f"/collections/{src.id}").status_code == 409
    db_session.expire_all()
    assert db_session.get(Collection, src.id).captures_adopted_at is not None


@pytest.mark.unit
def test_no_record_is_created_moved_into_or_given_an_image_in_a_closed_source(client, db_session,
                                                                             override_projects_root, tenant):
    from app.models.record import Record
    from tests.unit.test_adopt_captures_plan import _volume
    from tests.unit.test_offsite_free import _as

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    _adopt(db_session, src, tgt)
    other = _volume(db_session, "Otro", "xx-other-001")
    loose = Record(title="loose", collection_id=other.id, capture_mode="single", status="captured")
    db_session.add(loose)
    db_session.commit()
    # A record put into the closed source by no route (a repair by hand), to reach the image upload.
    planted = Record(title="planted", collection_id=src.id, capture_mode="single", status="captured")
    db_session.add(planted)
    db_session.commit()
    _as("admin")

    responses = {
        "create": client.post("/records/", json={"title": "new", "collection_id": src.id, "capture_mode": "single"}),
        "move-records": client.post(f"/collections/{other.id}/move-records", params={"target_collection_id": src.id}),
        "record move": client.patch(f"/records/{loose.id}", json={"collection_id": src.id}),
        "image upload": client.post(f"/records/{planted.id}/images",
                                    files={"file": ("x.jpg", b"jpeg", "image/jpeg")}),
    }

    for name, r in responses.items():
        assert r.status_code == 409, (name, r.status_code, r.text)
        assert r.json()["detail"]["code"] == "captures_adopted_away", (name, r.text)
    db_session.expire_all()
    assert db_session.get(Record, loose.id).collection_id == other.id


@pytest.mark.unit
def test_an_undone_adoption_leaves_the_source_open(db_session, override_projects_root, tenant, monkeypatch):
    from app.core import adopt_apply
    from app.core.adopt_apply import AdoptFailed
    from app.models.collection import Collection

    root = override_projects_root
    src, tgt, _, _, _ = _with_shots(db_session, root)
    real = adopt_apply._rename

    def rename(old, new):
        if "-cal_" in Path(new).name:
            raise OSError(errno.EIO, "I/O error")
        real(old, new)

    monkeypatch.setattr(adopt_apply, "_rename", rename)
    with pytest.raises(AdoptFailed):
        _adopt(db_session, src, tgt)

    db_session.expire_all()
    assert getattr(db_session.get(Collection, src.id), "captures_adopted_at", "no such column") is None
