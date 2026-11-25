"""An unfinished adoption reported to administrators, and repaired from the unit.

GET /system/adoption (app.core.adopt_repair.report) says whether an
adoption's journal is in the data directory, which volumes it names, why
recovery stopped, and which files are at neither their old nor their new
path. POST /system/adoption/repair (app.core.adopt_repair.repair) fetches
each such file from the bucket through the read remote, verifies its
digests, places it where the records decide, and runs the startup recovery.

The volumes and the kill points are those of tests/unit/test_adopt_journal.py
and tests/unit/test_adopt_colour_card.py; the bucket is the local directory
remote of tests/unit/test_image_upload.py, read through
OFFSITE_READ_REMOTE as restore reads it, with every source object put in
it under its key before the adoption.

    python -m pytest tests/unit/test_adopt_repair.py

Version: 0.3.3
"""

import json
import os
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

from tests.unit.test_adopt_apply import NOW, _adopt, _audit, _journal, _setup, _snapshot
from tests.unit.test_adopt_captures_plan import SRC, TGT, own_data_dir, tenant  # noqa: F401
from tests.unit.test_adopt_colour_card import _with_shots
from tests.unit.test_adopt_journal import Killed, _inventory, _kill_after_renames, _kill_at_phase, _recover
from tests.unit.test_image_upload import remote  # noqa: F401
from tests.unit.test_offsite_free import _as, read_remote  # noqa: F401


@pytest.fixture
def kill():
    with pytest.MonkeyPatch.context() as patch:
        yield patch


def _fill_bucket(remote, db, root):
    """Every source object in the bucket under its key, with the bytes of its file."""
    from app.models.colour_card import ColourCardObject
    from app.models.offsite_object import OffsiteObject

    for row in db.query(OffsiteObject).all():
        folder = "captures" if row.kind == "capture" else "previews"
        remote.put(row.object_key, (root / SRC / folder / row.object_key.rsplit("/", 1)[1]).read_bytes())
    for row in db.query(ColourCardObject).all():
        remote.put(row.object_key, (root / SRC / "calibration" / row.object_key.rsplit("/", 1)[1]).read_bytes())


def _blocked(db, root, kill, *, committed=False, bucket=None):
    """An adoption killed mid-rename (or after its commit), its first master then lost, and a recovery that stopped.

    With `bucket`, every source object is put in it first.
    """
    src, tgt, _, plan = _setup(db, root)
    if bucket is not None:
        _fill_bucket(bucket, db, root)
    if committed:
        _kill_at_phase(kill, "committed")
    else:
        _kill_after_renames(kill, 1)
    with pytest.raises(Killed):
        _adopt(db, src, tgt)
    kill.undo()
    lost = plan.renames[0]
    lost.new_path.unlink()
    assert _recover()["outcome"] == "unfinished"
    return src, tgt, plan, lost


def _no_part_files(root: Path) -> bool:
    return not [p for p in root.rglob("*") if p.name.endswith(".part")]


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_no_journal_reports_nothing_unfinished(client, db_session, override_projects_root):
    _as("admin")

    r = client.get("/system/adoption")

    assert r.status_code == 200, r.text
    assert r.json() == {"unfinished": None, "running": None}


@pytest.mark.unit
@pytest.mark.parametrize("role", ["operator", "reviewer"])
def test_only_an_admin_sees_or_repairs_an_unfinished_adoption(client, db_session, override_projects_root, role):
    _as(role)

    assert client.get("/system/adoption").status_code == 403
    assert client.post("/system/adoption/repair").status_code == 403


@pytest.mark.unit
def test_a_blocked_adoption_with_a_missing_master_is_reported(client, db_session, override_projects_root, tenant,
                                                              read_remote, kill):
    root = override_projects_root
    src, tgt, plan, lost = _blocked(db_session, root, kill)
    journal = _journal().read_bytes()
    _as("admin")

    r = client.get("/system/adoption")

    assert r.status_code == 200, r.text
    body = r.json()["unfinished"]
    assert body["source"] == {"id": src.id, "name": src.name, "filename_stem": SRC}
    assert body["target"] == {"id": tgt.id, "name": tgt.name, "filename_stem": TGT}
    assert body["started_at"] == NOW.isoformat()
    assert body["state"] == "blocked" and body["reason"] == "file_at_neither_path"
    assert body["message"]
    assert body["missing"] == [{"kind": "master", "old_path": str(lost.old_path), "new_path": str(lost.new_path),
                                "name": lost.old_path.name, "image_id": lost.image_id, "repairable": True,
                                "why_not": None, "needs_storage": True}]
    assert body["repair_available"] is True
    assert _journal().read_bytes() == journal


@pytest.mark.unit
def test_a_missing_shot_is_reported_by_its_shot(client, db_session, override_projects_root, tenant, read_remote):
    from app.core import adopt_apply

    root = override_projects_root
    src, tgt, _, _, shots = _with_shots(db_session, root)
    real = adopt_apply._rename

    def rename(old, new):
        real(old, new)
        if "-cal_" in Path(new).name:
            raise Killed()

    with pytest.MonkeyPatch.context() as kill:
        kill.setattr(adopt_apply, "_rename", rename)
        with pytest.raises(Killed):
            _adopt(db_session, src, tgt)
    moved = [m for m in json.loads(_journal().read_text())["moves"] if m["kind"] == "colour_card_shot"][0]
    Path(moved["new"]).unlink()
    assert _recover()["outcome"] == "unfinished"
    _as("admin")

    body = client.get("/system/adoption").json()["unfinished"]

    assert body["state"] == "blocked"
    assert body["missing"] == [{"kind": "colour_card_shot", "old_path": moved["old"], "new_path": moved["new"],
                                "name": Path(moved["old"]).name, "shot_id": moved["shot_id"], "repairable": True,
                                "why_not": None, "needs_storage": True}]
    assert body["repair_available"] is True




@pytest.mark.unit
def test_a_missing_raw_buffer_cannot_be_repaired(client, db_session, override_projects_root, tenant, read_remote,
                                                 kill):
    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    _kill_after_renames(kill, 23)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    [buffer] = [m for m in json.loads(_journal().read_text())["moves"] if m["kind"] == "buffer"]
    Path(buffer["new"]).unlink()
    assert _recover()["outcome"] == "unfinished"
    state = (_snapshot(db_session, root), _journal().read_bytes())
    _as("admin")

    body = client.get("/system/adoption").json()["unfinished"]

    assert body["missing"] == [{"kind": "buffer", "old_path": buffer["old"], "new_path": buffer["new"],
                                "name": Path(buffer["old"]).name, "image_id": buffer["image_id"],
                                "repairable": False, "why_not": "never_uploaded", "needs_storage": False}]
    assert body["repair_available"] is False

    r = client.post("/system/adoption/repair")

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "repair_not_possible"
    assert [m["old_path"] for m in detail["missing"]] == [buffer["old"]]
    assert (_snapshot(db_session, root), _journal().read_bytes()) == state


@pytest.mark.unit
def test_a_running_adoption_is_reported_as_recovering_never_blocked(client, db_session, override_projects_root,
                                                                    tenant, kill):
    from app.core import adopt_apply
    from app.core.adopt_apply import adopt_captures
    from app.core.db import SessionLocal

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    src_id, tgt_id = src.id, tgt.id
    paused, go = threading.Event(), threading.Event()
    real = adopt_apply._rename

    def rename(a, b):
        real(a, b)
        if not paused.is_set():
            paused.set()
            go.wait(30)

    kill.setattr(adopt_apply, "_rename", rename)
    outcome = {}

    def run():
        session = SessionLocal()
        try:
            outcome["result"] = adopt_captures(session, src_id, tgt_id, actor="admin", now=NOW,
                                               list_target_prefix=lambda t, s: [])
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            outcome["result"] = exc
        finally:
            session.close()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert paused.wait(30)
        _as("admin")
        body = client.get("/system/adoption").json()["unfinished"]
        repaired = client.post("/system/adoption/repair")
    finally:
        go.set()
        thread.join(60)
    assert not thread.is_alive()

    assert body["state"] == "recovering" and body["repair_available"] is False
    assert (body["source"]["id"], body["target"]["id"]) == (src_id, tgt_id)
    assert repaired.status_code == 409 and repaired.json()["detail"]["code"] == "adoption_running"
    assert isinstance(outcome["result"], dict), outcome
    assert client.get("/system/adoption").json() == {"unfinished": None, "running": None}


@pytest.mark.unit
@pytest.mark.parametrize("case, code", [("file_at_both", "file_at_both_paths"),
                                        ("file_at_neither", "file_at_neither_path"),
                                        ("rows_disagree", "image_rows_contradict")])
def test_each_way_recovery_stops_has_its_own_code(client, db_session, override_projects_root, tenant, kill,
                                                   case, code):
    from app.models.record import RecordImage

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    _kill_after_renames(kill, 10)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    moved = plan.renames[0]
    if case == "file_at_both":
        moved.old_path.write_bytes(b"a copy put back by hand")
    elif case == "file_at_neither":
        moved.new_path.unlink()
    else:
        db_session.get(RecordImage, moved.image_id).file_path = str(moved.new_path)
        db_session.commit()

    assert _recover()["code"] == code
    _as("admin")
    body = client.get("/system/adoption").json()["unfinished"]
    assert (body["state"], body["reason"]) == ("blocked", code)


@pytest.mark.unit
def test_a_volume_shows_whether_a_journal_names_it_and_when_its_captures_were_adopted(
        client, db_session, override_projects_root, tenant, kill):
    root = override_projects_root
    src, tgt, _, _ = _blocked(db_session, root, kill, committed=True)
    _as("operator")

    one = client.get(f"/collections/{src.id}").json()
    listed = {c["id"]: c for c in client.get("/collections/").json()}

    assert one["adoption_unfinished"] is True and one["captures_adopted_at"] == NOW.replace(tzinfo=None).isoformat()
    assert listed[tgt.id]["adoption_unfinished"] is True and listed[tgt.id]["captures_adopted_at"] is None
    assert listed[src.id]["captures_adopted_at"] == one["captures_adopted_at"]


# ---------------------------------------------------------------------------
# The repair
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_master_lost_before_the_commit_is_fetched_to_its_old_path_and_the_adoption_undone(
        client, db_session, override_projects_root, read_remote, kill):
    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    _fill_bucket(read_remote, db_session, root)
    before, inventory = _snapshot(db_session, root), _inventory(root)
    _kill_after_renames(kill, 1)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    lost = plan.renames[0]
    lost.new_path.unlink()
    assert _recover()["outcome"] == "unfinished"
    _as("admin")

    r = client.post("/system/adoption/repair")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == "reverted"
    assert [f["path"] for f in body["fetched"]] == [str(lost.old_path)]
    assert lost.old_path.is_file() and not lost.new_path.exists()
    assert _inventory(root) == inventory
    assert _snapshot(db_session, root)[0] == before[0]
    assert not _journal().exists() and _no_part_files(root)
    [event] = _audit(db_session, "volume_captures_adoption_repaired")
    assert event.level == "WARN" and "reverted" in event.detail and lost.old_path.name in event.detail
    assert client.get("/system/adoption").json() == {"unfinished": None, "running": None}


@pytest.mark.unit
def test_a_master_lost_after_the_commit_is_fetched_to_its_new_path_and_the_adoption_finished(
        client, db_session, override_projects_root, read_remote, kill):
    from app.models.record import Record

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    _fill_bucket(read_remote, db_session, root)
    inventory = _inventory(root)
    _kill_at_phase(kill, "committed")
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    lost = plan.renames[0]
    lost.new_path.unlink()
    assert _recover()["outcome"] == "unfinished"
    _as("admin")

    r = client.post("/system/adoption/repair")

    assert r.status_code == 200, r.text
    assert r.json()["result"] == "finished"
    assert lost.new_path.is_file() and not lost.old_path.exists()
    assert _inventory(root) == inventory
    db_session.expire_all()
    assert {rec.collection_id for rec in db_session.query(Record).all()} == {tgt.id}
    assert not _journal().exists() and _no_part_files(root)


@pytest.mark.unit
def test_a_lost_shot_and_its_preview_are_fetched_and_the_adoption_undone(client, db_session, override_projects_root,
                                                                         read_remote):
    from app.core import adopt_apply

    root = override_projects_root
    src, tgt, _, _, shots = _with_shots(db_session, root)
    _fill_bucket(read_remote, db_session, root)
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
    moves = json.loads(_journal().read_text())["moves"]
    shot = [m for m in moves if m["kind"] == "colour_card_shot"][0]
    preview = [m for m in moves if m["kind"] == "colour_card_preview" and m["shot_id"] == shot["shot_id"]][0]
    Path(shot["new"]).unlink()
    Path(preview["old"]).unlink()
    assert _recover()["outcome"] == "unfinished"
    _as("admin")

    r = client.post("/system/adoption/repair")

    assert r.status_code == 200, r.text
    assert r.json()["result"] == "reverted"
    assert sorted(f["path"] for f in r.json()["fetched"]) == sorted([shot["old"], preview["old"]])
    assert _snapshot(db_session, root) == before
    assert _inventory(root) == inventory
    assert not _journal().exists() and _no_part_files(root)


@pytest.mark.unit
def test_a_lost_thumbnail_is_derived_again_from_its_master(client, db_session, override_projects_root, read_remote,
                                                          kill, monkeypatch):
    from app.core import thumbnail

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    _kill_after_renames(kill, 23)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    [thumb] = [m for m in json.loads(_journal().read_text())["moves"] if m["kind"] == "thumbnail"][:1]
    Path(thumb["new"]).unlink()
    assert _recover()["outcome"] == "unfinished"
    made = []

    def generate(source_path, dest_dir, *a, **kw):
        made.append(Path(source_path))
        out = Path(dest_dir) / "0123_thumb.jpg"
        out.write_bytes(b"derived from " + Path(source_path).read_bytes())
        return str(out)

    monkeypatch.setattr(thumbnail, "generate_thumbnail", generate)
    _as("admin")
    body = client.get("/system/adoption").json()["unfinished"]
    assert [(m["kind"], m["repairable"]) for m in body["missing"]] == [("thumbnail", True)]

    r = client.post("/system/adoption/repair")

    assert r.status_code == 200, r.text
    assert r.json()["result"] == "reverted"
    assert Path(thumb["old"]).is_file() and Path(thumb["old"]).read_bytes().startswith(b"derived from ")
    assert len(made) == 1 and not _journal().exists() and _no_part_files(root)


@pytest.mark.unit
def test_a_fetched_copy_with_another_digest_changes_nothing(client, db_session, override_projects_root, read_remote,
                                                           kill):
    from app.models.offsite_object import OffsiteObject

    root = override_projects_root
    src, tgt, plan, lost = _blocked(db_session, root, kill)
    key = db_session.query(OffsiteObject.object_key).filter(
        OffsiteObject.record_image_id == lost.image_id, OffsiteObject.kind == "capture").scalar()
    assert key
    read_remote.put(key, b"other bytes under the same key")
    state = (_snapshot(db_session, root), _journal().read_bytes())
    _as("admin")

    r = client.post("/system/adoption/repair")

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "repair_digest_mismatch"
    assert (_snapshot(db_session, root), _journal().read_bytes()) == state
    assert _no_part_files(root)


@pytest.mark.unit
def test_an_object_absent_from_the_bucket_changes_nothing(client, db_session, override_projects_root, read_remote,
                                                         kill):
    root = override_projects_root
    _blocked(db_session, root, kill)
    state = (_snapshot(db_session, root), _journal().read_bytes())
    _as("admin")

    r = client.post("/system/adoption/repair")

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "repair_object_missing"
    assert (_snapshot(db_session, root), _journal().read_bytes()) == state
    assert _no_part_files(root)


@pytest.mark.unit
def test_without_a_read_remote_the_repair_is_refused(client, db_session, override_projects_root, tenant, kill):
    root = override_projects_root
    _blocked(db_session, root, kill)
    state = (_snapshot(db_session, root), _journal().read_bytes())
    _as("admin")

    body = client.get("/system/adoption").json()["unfinished"]
    assert body["repair_available"] is False
    assert [(m["kind"], m["needs_storage"]) for m in body["missing"]] == [("master", True)]
    r = client.post("/system/adoption/repair")

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "repair_unavailable" and "access to the unit" in detail["message"]
    assert (_snapshot(db_session, root), _journal().read_bytes()) == state


@pytest.mark.unit
def test_two_repairs_at_once_one_acts(db_session, override_projects_root, read_remote, kill):
    from app.core import adopt_repair, offsite_free
    from app.core.adopt_repair import RepairRefused
    from app.core.db import SessionLocal

    root = override_projects_root
    _, _, plan, lost = _blocked(db_session, root, kill, bucket=read_remote)
    paused, go = threading.Event(), threading.Event()
    real = offsite_free._copy_result

    def copy(key, dest):
        paused.set()
        go.wait(30)
        return real(key, dest)

    kill.setattr(offsite_free, "_copy_result", copy)
    outcome = {}

    def first():
        session = SessionLocal()
        try:
            outcome["first"] = adopt_repair.repair(session, actor="admin")
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            outcome["first"] = exc
        finally:
            session.close()

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert paused.wait(30)
        second = SessionLocal()
        try:
            with pytest.raises(RepairRefused) as refused:
                adopt_repair.repair(second, actor="admin")
        finally:
            second.close()
    finally:
        go.set()
        thread.join(60)
    assert not thread.is_alive()

    assert (refused.value.status_code, refused.value.code) == (409, "repair_running")
    assert isinstance(outcome["first"], dict) and outcome["first"]["result"] == "reverted", outcome
    assert lost.old_path.is_file() and not _journal().exists()


@pytest.mark.unit
def test_a_second_repair_finds_nothing_to_repair(client, db_session, override_projects_root, read_remote, kill):
    root = override_projects_root
    _blocked(db_session, root, kill, bucket=read_remote)
    _as("admin")

    assert client.post("/system/adoption/repair").status_code == 200
    r = client.post("/system/adoption/repair")

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "no_unfinished_adoption"


@pytest.mark.unit
def test_with_nothing_missing_a_unit_without_a_read_remote_repairs_by_running_recovery(
        client, db_session, override_projects_root, tenant, kill):
    from app.core.offsite_free import restore_available

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    inventory = _inventory(root)
    _kill_at_phase(kill, "committed")
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    assert not restore_available() and _journal().exists()
    _as("admin")

    body = client.get("/system/adoption").json()["unfinished"]
    assert (body["state"], body["reason"], body["missing"]) == ("blocked", "recovery_not_run", [])
    assert body["repair_available"] is True

    r = client.post("/system/adoption/repair")

    assert r.status_code == 200, r.text
    assert (r.json()["result"], r.json()["fetched"], r.json()["derived"]) == ("finished", [], [])
    assert _inventory(root) == inventory and not _journal().exists()
    assert client.get("/system/adoption").json() == {"unfinished": None, "running": None}


@pytest.mark.unit
def test_a_derivable_thumbnail_is_repaired_on_a_unit_without_a_read_remote(client, db_session,
                                                                          override_projects_root, tenant, kill,
                                                                          monkeypatch):
    from app.core import thumbnail
    from app.core.offsite_free import restore_available

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    _kill_after_renames(kill, 23)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    thumb = [m for m in json.loads(_journal().read_text())["moves"] if m["kind"] == "thumbnail"][0]
    Path(thumb["new"]).unlink()
    assert _recover()["outcome"] == "unfinished"

    def generate(source_path, dest_dir, *a, **kw):
        out = Path(dest_dir) / "4567_thumb.jpg"
        out.write_bytes(b"derived from " + Path(source_path).read_bytes())
        return str(out)

    monkeypatch.setattr(thumbnail, "generate_thumbnail", generate)
    assert not restore_available()
    _as("admin")

    body = client.get("/system/adoption").json()["unfinished"]
    assert [(m["kind"], m["repairable"], m["needs_storage"]) for m in body["missing"]] == [("thumbnail", True, False)]
    assert body["repair_available"] is True

    r = client.post("/system/adoption/repair")

    assert r.status_code == 200, r.text
    assert r.json()["result"] == "reverted" and [d["path"] for d in r.json()["derived"]] == [thumb["old"]]
    assert Path(thumb["old"]).read_bytes().startswith(b"derived from ")
    assert not _journal().exists() and _no_part_files(root)


# ---------------------------------------------------------------------------
# Only verified bytes are placed, nothing is staged in a volume, and the work grows with the journal
# ---------------------------------------------------------------------------

def _volume_files(root: Path) -> set:
    """Every file under the projects root outside the repair's staging directory, by relative path."""
    from app.core.paths import ADOPTION_REPAIR_STAGING_DIR

    return {str(p.relative_to(root)) for p in root.rglob("*")
            if p.is_file() and p.relative_to(root).parts[0] != ADOPTION_REPAIR_STAGING_DIR}


@pytest.mark.unit
def test_a_file_at_both_paths_makes_the_journal_unrepairable_and_nothing_is_placed(
        client, db_session, override_projects_root, tenant, read_remote, kill):
    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    _fill_bucket(read_remote, db_session, root)
    _kill_at_phase(kill, "committed")
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    jpeg = next(r for r in plan.renames if r.old_path.suffix.lower() == ".jpg")
    jpeg.old_path.write_bytes(b"an unrelated file at the master's old name")
    thumb = next(m for m in json.loads(_journal().read_text())["moves"]
                 if m["kind"] == "thumbnail" and m["image_id"] == jpeg.image_id)
    Path(thumb["new"]).unlink()
    assert _recover()["outcome"] == "unfinished"
    state = (_snapshot(db_session, root), _journal().read_bytes())
    _as("admin")

    body = client.get("/system/adoption").json()["unfinished"]
    r = client.post("/system/adoption/repair")

    assert body["repair_available"] is False
    assert [(b["kind"], b["image_id"]) for b in body["at_both"]] == [("master", jpeg.image_id)]
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "repair_not_possible"
    assert [b["old_path"] for b in r.json()["detail"]["at_both"]] == [str(jpeg.old_path)]
    assert not Path(thumb["old"]).exists() and not Path(thumb["new"]).exists()
    assert (_snapshot(db_session, root), _journal().read_bytes()) == state


@pytest.mark.unit
def test_a_thumbnail_is_not_made_from_a_master_whose_bytes_are_not_its_own(
        client, db_session, override_projects_root, tenant, kill, monkeypatch):
    from app.core import thumbnail
    from app.core.adopt_repair import staging_dir

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    _kill_after_renames(kill, 23)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    jpeg = next(r for r in plan.renames if r.old_path.suffix.lower() == ".jpg")
    jpeg.new_path.write_bytes(b"not the bytes the records hold")
    thumb = next(m for m in json.loads(_journal().read_text())["moves"]
                 if m["kind"] == "thumbnail" and m["image_id"] == jpeg.image_id)
    Path(thumb["new"]).unlink()
    assert _recover()["outcome"] == "unfinished"
    made = []
    monkeypatch.setattr(thumbnail, "generate_thumbnail", lambda *a, **k: made.append(a) or None)
    state = (_snapshot(db_session, root), _journal().read_bytes())
    _as("admin")

    r = client.post("/system/adoption/repair")

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "repair_not_possible"
    assert [m["why_not"] for m in detail["missing"]] == ["thumbnail_source_unverified"]
    assert made == []
    assert (_snapshot(db_session, root), _journal().read_bytes()) == state
    assert not staging_dir().exists()


@pytest.mark.unit
def test_a_repair_killed_after_one_placement_leaves_only_placed_files_and_the_next_start_clears_its_staging(
        db_session, override_projects_root, read_remote, kill):
    from app.core import adopt_repair
    from app.core.adopt_repair import staging_dir
    from app.core.db import SessionLocal
    from tests.unit.test_adopt_captures_plan import _plan

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    _fill_bucket(read_remote, db_session, root)
    _kill_after_renames(kill, 23)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    first, second = plan.renames[0], plan.renames[1]
    first.new_path.unlink()
    second.new_path.unlink()
    assert _recover()["outcome"] == "unfinished"
    before = _volume_files(root)
    real = adopt_repair._place
    placed = []

    def place(staged, dest):
        if placed:
            raise Killed()
        real(staged, dest)
        placed.append(dest)

    kill.setattr(adopt_repair, "_place", place)
    session = SessionLocal()
    try:
        with pytest.raises(Killed):
            adopt_repair.repair(session, actor="admin")
    finally:
        session.close()
    kill.undo()

    [dest] = placed
    assert _volume_files(root) == before | {str(dest.relative_to(root))}
    assert dest in (first.old_path, second.old_path)
    assert [p.name.endswith(".part") for p in staging_dir().iterdir()] == [True]

    assert _recover()["outcome"] == "unfinished"
    assert not staging_dir().exists()
    session = SessionLocal()
    try:
        assert adopt_repair.repair(session, actor="admin")["result"] == "reverted"
    finally:
        session.close()
    db_session.expire_all()
    assert _plan(db_session, src, tgt).refusals == []


@pytest.mark.unit
def test_the_report_of_many_missing_thumbnails_reads_each_file_twice_and_the_rows_in_a_fixed_number_of_queries(
        db_session, override_projects_root, tenant, monkeypatch):
    import os

    from sqlalchemy import event

    from app.core import adopt_repair

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)

    def journal(n):
        moves = []
        for r in plan.renames:
            moves.append({"kind": "master", "image_id": r.image_id, "old": str(r.old_path), "new": str(r.new_path)})
            if r.old_preview_path is not None:
                moves.append({"kind": "preview", "image_id": r.image_id, "old": str(r.old_preview_path),
                              "new": str(r.new_preview_path)})
        for i in range(n):
            r = plan.renames[i % len(plan.renames)]
            moves.append({"kind": "thumbnail", "image_id": r.image_id,
                          "old": str(root / SRC / "thumbnails" / f"gone-{i}_thumb.jpg"),
                          "new": str(root / TGT / "thumbnails" / f"gone-{i}_thumb.jpg")})
        _journal().write_text(json.dumps({
            "version": 1, "run_id": f"many-{n}", "phase": "renaming", "source_id": src.id, "source_stem": SRC,
            "target_id": tgt.id, "target_stem": TGT, "actor": "admin", "started_at": NOW.isoformat(),
            "moves": moves,
            "images": [{"id": r.image_id, "record_id": r.record_id, "old_file_path": str(r.old_path),
                        "new_file_path": str(r.new_path)} for r in plan.renames],
            "record_ids": sorted({r.record_id for r in plan.renames}), "retired_row_ids": [], "created_dirs": [],
            "source_root": str(root / SRC), "target_root": str(root / TGT), "target_lines": []}))
        return moves

    engine = db_session.get_bind()
    real_lexists = os.path.lexists

    def measure(n):
        moves = journal(n)
        statements, looks = [], []

        def count(conn, cursor, statement, *a):
            statements.append(statement)

        def lexists(path):
            looks.append(path)
            return real_lexists(path)

        event.listen(engine, "before_cursor_execute", count)
        with monkeypatch.context() as m:
            m.setattr(os.path, "lexists", lexists)
            body = adopt_repair.report(db_session)["unfinished"]
        event.remove(engine, "before_cursor_execute", count)
        return body, len(statements), len(looks), len(moves)

    small, small_queries, small_looks, small_moves = measure(30)
    big, big_queries, big_looks, big_moves = measure(3000)

    assert len(big["missing"]) == 3000 and all(m["repairable"] for m in big["missing"])
    assert big_queries == small_queries <= 12, (small_queries, big_queries)
    assert big_looks == 2 * big_moves and small_looks == 2 * small_moves


# ---------------------------------------------------------------------------
# The storage stays where the adoption's files are
# ---------------------------------------------------------------------------

@pytest.fixture
def switches(monkeypatch):
    """Every write of the storage override, recorded instead of made."""
    from app.core import storage_override

    made = []
    monkeypatch.setattr(storage_override, "set_storage_override", lambda root: made.append(("set", root)))
    monkeypatch.setattr(storage_override, "clear_storage_override", lambda: made.append(("clear",)))
    return made


@pytest.mark.unit
def test_the_storage_is_not_switched_while_an_adoption_is_unfinished(client, db_session, override_projects_root,
                                                                     tenant, kill, tmp_path, switches):
    root = override_projects_root
    _blocked(db_session, root, kill)
    drive = tmp_path / "drive"
    drive.mkdir()
    _as("admin")

    activated = client.post("/system/storage/activate", json={"path": str(drive)})
    reset = client.delete("/system/storage/activate")

    assert activated.status_code == 409, activated.text
    assert activated.json()["detail"]["code"] == "adoption_unfinished"
    assert not (drive / "dtk-projects").exists()
    # The journal's files are on the default root, so reverting to it is the one switch allowed.
    assert reset.status_code == 200, reset.text
    assert switches == [("clear",)]


@pytest.mark.unit
def test_the_storage_is_not_switched_while_an_adoption_recovery_or_repair_runs(client, db_session,
                                                                             override_projects_root, tmp_path,
                                                                             switches):
    from app.core import adopt_journal

    drive = tmp_path / "drive"
    drive.mkdir()
    _as("admin")
    assert adopt_journal.ADOPTION_LOCK.acquire(timeout=5)
    try:
        activated = client.post("/system/storage/activate", json={"path": str(drive)})
        reset = client.delete("/system/storage/activate")
    finally:
        adopt_journal.ADOPTION_LOCK.release()

    assert activated.status_code == 409 and activated.json()["detail"]["code"] == "adoption_running"
    assert reset.status_code == 409 and reset.json()["detail"]["code"] == "adoption_running"
    assert switches == []


@pytest.mark.unit
def test_a_repair_whose_storage_setting_changes_midway_cleans_the_directory_it_used(
        db_session, override_projects_root, read_remote, kill, tmp_path):
    from app.core import adopt_repair, offsite_free, storage_override
    from app.core.adopt_repair import RepairRefused
    from app.core.db import SessionLocal
    from app.core.paths import ADOPTION_REPAIR_STAGING_DIR
    from app.models.offsite_object import OffsiteObject

    root = override_projects_root
    _, _, _, lost = _blocked(db_session, root, kill)
    key = db_session.query(OffsiteObject.object_key).filter(
        OffsiteObject.record_image_id == lost.image_id, OffsiteObject.kind == "capture").scalar()
    read_remote.put(key, b"bytes that are not the master's")
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / ADOPTION_REPAIR_STAGING_DIR).mkdir(parents=True)
    real = offsite_free._copy_result

    def copy(k, dest):
        result = real(k, dest)
        kill.setattr(storage_override, "get_storage_override_or_fallback", lambda: (str(elsewhere), False))
        return result

    kill.setattr(offsite_free, "_copy_result", copy)
    session = SessionLocal()
    try:
        with pytest.raises(RepairRefused) as refused:
            adopt_repair.repair(session, actor="admin")
    finally:
        session.close()
    kill.undo()

    assert refused.value.code == "repair_digest_mismatch"
    assert not (root / ADOPTION_REPAIR_STAGING_DIR).exists()
    assert (elsewhere / ADOPTION_REPAIR_STAGING_DIR).is_dir()


@pytest.mark.unit
def test_staging_on_another_filesystem_than_the_destinations_is_neither_offered_nor_made(
        client, db_session, override_projects_root, read_remote, kill, monkeypatch):
    from app.core import adopt_repair
    from app.core.paths import ADOPTION_REPAIR_STAGING_DIR

    root = override_projects_root
    _blocked(db_session, root, kill, bucket=read_remote)
    real = adopt_repair._device
    monkeypatch.setattr(adopt_repair, "_device",
                        lambda path: -1 if Path(path) == root / ADOPTION_REPAIR_STAGING_DIR or
                        Path(path) == root else real(path))
    state = (_snapshot(db_session, root), _journal().read_bytes())
    _as("admin")

    body = client.get("/system/adoption").json()["unfinished"]
    r = client.post("/system/adoption/repair")

    assert body["repair_available"] is False and body["staging"]["same_filesystem"] is False
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "repair_not_possible"
    assert r.json()["detail"]["reason"] == "staging_on_another_filesystem"
    assert (_snapshot(db_session, root), _journal().read_bytes()) == state
    assert not (root / ADOPTION_REPAIR_STAGING_DIR).exists()


@pytest.mark.unit
def test_the_next_start_sweeps_the_staging_directory_under_the_current_root_only(
        db_session, override_projects_root, tenant, kill, tmp_path, monkeypatch):
    from app.core import storage_override
    from app.core.paths import ADOPTION_REPAIR_STAGING_DIR

    root = override_projects_root
    _blocked(db_session, root, kill)
    journals = root / ADOPTION_REPAIR_STAGING_DIR
    journals.mkdir()
    (journals / "kept").write_text("")
    current = tmp_path / "current"
    swept = current / ADOPTION_REPAIR_STAGING_DIR
    swept.mkdir(parents=True)
    (swept / "abc-name.part").write_bytes(b"partial")
    monkeypatch.setattr(storage_override, "get_storage_override_or_fallback", lambda: (str(current), False))

    _recover()

    assert not swept.exists() and (journals / "kept").is_file()


# ---------------------------------------------------------------------------
# Nothing the journal says chooses a directory; the drive the adoption is on can be activated again
# ---------------------------------------------------------------------------

def _rewrite_journal(**fields):
    data = json.loads(_journal().read_text())
    data.update(fields)
    _journal().write_text(json.dumps(data))


@pytest.mark.unit
def test_no_drive_is_unmounted_while_an_adoption_runs_whichever_drive(client, db_session, override_projects_root,
                                                                     tmp_path, monkeypatch):
    from app.api import system
    from app.core import adopt_journal

    mounts = tmp_path / "mounts"
    drive = mounts / "sdb1"
    drive.mkdir(parents=True)
    monkeypatch.setattr(system, "_MOUNT_BASE", mounts)
    ran = []
    monkeypatch.setattr(system.subprocess, "run", lambda argv, **kw: ran.append(argv))
    _as("admin")
    assert adopt_journal.ADOPTION_LOCK.acquire(timeout=5)
    try:
        r = client.request("DELETE", "/system/storage/mount", json={"mountpoint": str(drive)})
    finally:
        adopt_journal.ADOPTION_LOCK.release()

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "adoption_running"
    assert ran == []


@pytest.fixture
def drive_root(tmp_path, monkeypatch):
    """A drive's `dtk-projects` as the active storage, through a real override file of the test's own."""
    from app.core import storage_override

    monkeypatch.setattr(storage_override, "_OVERRIDE_FILE", tmp_path / "storage-override.json")
    monkeypatch.setattr(storage_override, "_internal_device", lambda: -1)
    drive = tmp_path / "drive"
    root = drive / "dtk-projects"
    root.mkdir(parents=True)
    storage_override.set_storage_override(str(root))
    return drive, root


@pytest.mark.unit
def test_with_a_corrupt_override_the_drive_the_adoption_is_on_can_be_activated_and_the_repair_completes(
        client, db_session, override_projects_root, read_remote, kill, drive_root):
    from app.core import config, storage_override

    drive, root = drive_root
    assert config.settings.projects_dir == root
    src, tgt, _, plan = _setup(db_session, root)
    _fill_bucket(read_remote, db_session, root)
    _kill_at_phase(kill, "committed")
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    lost = plan.renames[0]
    lost.new_path.unlink()
    storage_override._OVERRIDE_FILE.write_text("{ torn")
    assert config.settings.projects_dir != root
    _as("admin")

    body = client.get("/system/adoption").json()["unfinished"]
    assert (body["state"], body["reason"], body["repair_available"]) == ("blocked", "storage_not_active", False)
    assert body["journal_root"] == str(root)
    refused = client.post("/system/adoption/repair")
    assert refused.status_code == 409 and refused.json()["detail"]["reason"] == "storage_not_active"
    other = override_projects_root / "elsewhere"
    other.mkdir()
    assert client.post("/system/storage/activate", json={"path": str(other)}).status_code == 409

    activated = client.post("/system/storage/activate", json={"path": str(drive)})

    assert activated.status_code == 200, activated.text
    assert config.settings.projects_dir == root
    assert client.get("/system/adoption").json()["unfinished"]["repair_available"] is True
    r = client.post("/system/adoption/repair")
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "finished" and lost.new_path.is_file() and not _journal().exists()


@pytest.mark.unit
def test_a_crafted_target_root_never_chooses_what_startup_deletes(db_session, override_projects_root, tenant, kill,
                                                                  tmp_path):
    from app.core.paths import ADOPTION_REPAIR_STAGING_DIR

    root = override_projects_root
    _blocked(db_session, root, kill)
    sentinel = tmp_path / "outside" / ADOPTION_REPAIR_STAGING_DIR
    sentinel.mkdir(parents=True)
    (sentinel / "keep").write_text("kept")
    _rewrite_journal(target_root=str(tmp_path / "outside" / "anything"))

    _recover()

    assert (sentinel / "keep").read_text() == "kept"
    assert _journal().exists()


@pytest.mark.unit
def test_a_journal_with_a_malformed_root_keeps_startup_and_the_routes_up(client, db_session, override_projects_root,
                                                                         tenant, kill):
    from app.core.adopt_journal import recover_adoption_at_startup

    root = override_projects_root
    _blocked(db_session, root, kill)
    _rewrite_journal(target_root=7)
    journal = _journal().read_bytes()

    recover_adoption_at_startup(db_session)

    assert _journal().read_bytes() == journal
    _as("admin")
    report = client.get("/system/adoption")
    assert report.status_code == 200, report.text
    body = report.json()["unfinished"]
    assert body["state"] == "blocked" and body["reason"] == "journal_unreadable"
    assert body["repair_available"] is False
    r = client.post("/system/adoption/repair")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "repair_not_possible"


# ---------------------------------------------------------------------------
# The volume routes never fail on the journal
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("ids", [{"source_id": [], "target_id": []}, {"source_id": "7", "target_id": {"a": 1}},
                                 "missing"])
def test_the_volume_list_and_detail_answer_whatever_ids_the_journal_holds(client, db_session,
                                                                          override_projects_root, tenant, kill,
                                                                          ids):
    root = override_projects_root
    src, tgt, _, _ = _blocked(db_session, root, kill)
    data = json.loads(_journal().read_text())
    if ids == "missing":
        del data["source_id"], data["target_id"]
    else:
        data.update(ids)
    _journal().write_text(json.dumps(data))
    _as("operator")

    listed = client.get("/collections/")
    one = client.get(f"/collections/{src.id}")

    assert listed.status_code == 200, listed.text
    assert not any(c["adoption_unfinished"] for c in listed.json())
    assert one.status_code == 200 and one.json()["adoption_unfinished"] is False


# ---------------------------------------------------------------------------
# The staging name belongs to the repair; one precheck decides availability
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("what", ["empty_directory", "full_directory", "file"])
def test_whatever_is_at_the_staging_name_is_cleared_and_the_repair_goes_on(db_session, override_projects_root,
                                                                         read_remote, kill, what):
    from app.core import adopt_repair
    from app.core.paths import ADOPTION_REPAIR_STAGING_DIR

    root = override_projects_root
    _blocked(db_session, root, kill, bucket=read_remote)
    name = root / ADOPTION_REPAIR_STAGING_DIR
    if what == "file":
        name.write_bytes(b"a file at the name")
    else:
        (name / "sub").mkdir(parents=True)
        if what == "full_directory":
            (name / "sub" / "left.part").write_bytes(b"partial")

    assert adopt_repair.repair(db_session, actor="admin")["result"] == "reverted"
    assert not os.path.lexists(name)


@pytest.mark.unit
def test_a_symlink_at_the_staging_name_or_inside_it_is_removed_and_its_target_kept(db_session,
                                                                                    override_projects_root,
                                                                                    tmp_path):
    from app.core.adopt_journal import recover_adoption_at_startup
    from app.core.paths import ADOPTION_REPAIR_STAGING_DIR

    root = override_projects_root
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    (sentinel / "keep").write_text("kept")
    name = root / ADOPTION_REPAIR_STAGING_DIR
    name.symlink_to(sentinel, target_is_directory=True)

    recover_adoption_at_startup(db_session)

    assert not os.path.lexists(name) and (sentinel / "keep").read_text() == "kept"
    name.mkdir()
    (name / "link").symlink_to(sentinel, target_is_directory=True)

    recover_adoption_at_startup(db_session)

    assert not os.path.lexists(name) and (sentinel / "keep").read_text() == "kept"


@pytest.mark.unit
@pytest.mark.parametrize("call, nth", [("unlink", 1), ("unlink", 2), ("rmdir", 1)])
def test_a_kill_at_any_point_of_a_cleanup_leaves_a_state_the_next_repair_clears(db_session, override_projects_root,
                                                                                read_remote, kill, call, nth):
    """The cleanup after a refused fetch removes the two staged copies (two unlinks) and then the directory (one
    rmdir); a kill at each of those calls leaves a state the next repair clears."""
    from app.core import adopt_repair, offsite_free
    from app.core.db import SessionLocal
    from app.core.paths import ADOPTION_REPAIR_STAGING_DIR

    root = override_projects_root
    staging = root / ADOPTION_REPAIR_STAGING_DIR
    src, tgt, _, plan = _setup(db_session, root)
    _fill_bucket(read_remote, db_session, root)
    _kill_after_renames(kill, 23)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    lost = plan.renames[:3]
    for r in lost:
        r.new_path.unlink()
    assert _recover()["outcome"] == "unfinished"
    real_copy = offsite_free._copy_result
    copies = []

    def copy(key, dest):
        copies.append(dest)
        if len(copies) == 3:
            return 3, "the copy found no object"   # the refusal whose cleanup is killed
        return real_copy(key, dest)

    seen = {"unlink": [], "rmdir": []}
    fired = []
    real = {"unlink": os.unlink, "rmdir": os.rmdir}

    def removal(name):
        def remove(path, *a, **kw):
            text = os.fspath(path)
            # Only the cleanup's own calls: a staged copy, or the staging directory itself.
            if (name == "unlink" and text.endswith(".part")) or \
                    (name == "rmdir" and Path(text).name == ADOPTION_REPAIR_STAGING_DIR):
                seen[name].append(text)
                if name == call and len(seen[name]) == nth:
                    fired.append(text)
                    raise Killed()
            return real[name](path, *a, **kw)
        return remove

    kill.setattr(offsite_free, "_copy_result", copy)
    kill.setattr(os, "unlink", removal("unlink"))
    kill.setattr(os, "rmdir", removal("rmdir"))
    session = SessionLocal()
    try:
        with pytest.raises(Killed):
            adopt_repair.repair(session, actor="admin")
    finally:
        session.close()
    kill.undo()

    assert len(fired) == 1, seen
    assert len(copies) == 3 and os.path.lexists(staging)
    session = SessionLocal()
    try:
        assert adopt_repair.repair(session, actor="admin")["result"] == "reverted"
    finally:
        session.close()
    assert not os.path.lexists(staging)


@contextmanager
def _refusal_state(code, db, root, kill, remote, monkeypatch, tmp_path):
    """Set up, for the duration, the one state in which the precheck answers `code`."""
    from app.core import adopt_journal, adopt_repair, storage_override
    from app.core.export_jobs import export_jobs
    from app.core.paths import ADOPTION_REPAIR_STAGING_DIR

    if code == "storage_unusable":
        monkeypatch.setattr(storage_override, "_OVERRIDE_FILE", tmp_path / "storage-override.json")
        monkeypatch.setattr(storage_override, "_internal_device", lambda: -1)
        drive = tmp_path / "drive"
        (drive / "dtk-projects").mkdir(parents=True)
        storage_override.set_storage_override(str(drive / "dtk-projects"))
        root = drive / "dtk-projects"
    src, tgt, _, plan = _setup(db, root)
    if remote is not None:
        _fill_bucket(remote, db, root)
    _kill_after_renames(kill, 1 if code == "file_at_both_paths" else 23)
    with pytest.raises(Killed):
        _adopt(db, src, tgt)
    kill.undo()
    moves = json.loads(_journal().read_text())["moves"]
    if code == "file_at_both_paths":
        plan.renames[0].old_path.write_bytes(b"a copy put back by hand")
    elif code == "files_not_repairable":
        Path(next(m for m in moves if m["kind"] == "buffer")["new"]).unlink()
    else:
        plan.renames[0].new_path.unlink()
    if code == "journal_unreadable":
        _rewrite_journal(target_root=7)
    elif code == "storage_not_active":
        _rewrite_journal(target_root=str(tmp_path / "another" / "root" / TGT))
    elif code == "storage_unusable":
        drive.rename(tmp_path / "drive-unplugged")
    elif code == "staging_on_another_filesystem":
        real = adopt_repair._device
        monkeypatch.setattr(adopt_repair, "_device",
                            lambda path: -1 if Path(path) in (root, root / ADOPTION_REPAIR_STAGING_DIR)
                            else real(path))
    with ExitStack() as stack:
        if code == "adoption_running":
            assert adopt_journal.ADOPTION_LOCK.acquire(timeout=5)
            stack.callback(adopt_journal.ADOPTION_LOCK.release)
        elif code == "repair_running":
            assert adopt_repair._REPAIR_LOCK.acquire(timeout=5)
            stack.callback(adopt_repair._REPAIR_LOCK.release)
        elif code == "volume_held":
            stack.enter_context(export_jobs.hold_for_reading([src.id]))
        yield


PRECHECK_CODES = ["repair_running", "adoption_running", "journal_unreadable", "storage_not_active",
                  "storage_unusable", "file_at_both_paths", "files_not_repairable", "no_read_remote",
                  "staging_on_another_filesystem", "volume_held"]


@pytest.mark.unit
@pytest.mark.parametrize("code", PRECHECK_CODES)
def test_the_report_and_the_repair_give_the_same_answer_for_every_refusal(db_session, override_projects_root,
                                                                          tenant, kill, remote, monkeypatch,
                                                                          tmp_path, code):
    from app.core import adopt_repair, config
    from app.core.adopt_repair import RepairRefused

    if code != "no_read_remote":
        monkeypatch.setattr(config.settings, "OFFSITE_READ_REMOTE", str(remote.root))
    with _refusal_state(code, db_session, override_projects_root, kill, remote, monkeypatch, tmp_path):
        body = adopt_repair.report(db_session)["unfinished"]
        with pytest.raises(RepairRefused) as refused:
            adopt_repair.repair(db_session, actor="admin")

    assert body["repair_available"] is False and body["repair_refusal"] == code, body
    assert refused.value.extra.get("reason") == code, refused.value.as_detail()


@pytest.mark.unit
def test_with_the_override_naming_an_unplugged_drive_no_repair_is_offered(db_session, override_projects_root,
                                                                         tenant, kill, read_remote, monkeypatch,
                                                                         tmp_path):
    from app.core import adopt_repair
    from app.core.adopt_repair import RepairRefused

    with _refusal_state("storage_unusable", db_session, override_projects_root, kill, read_remote, monkeypatch,
                        tmp_path):
        body = adopt_repair.report(db_session)["unfinished"]
        with pytest.raises(RepairRefused) as refused:
            adopt_repair.repair(db_session, actor="admin")

    assert body["repair_available"] is False
    assert (refused.value.code, refused.value.extra.get("reason")) == ("repair_not_possible", "storage_unusable")
