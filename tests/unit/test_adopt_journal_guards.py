"""What is refused while an adoption's journal names a volume (app.core.adopt_journal.journal_names_volume).

A volume an unfinished adoption names has its files at one of two names
until startup recovery decides which; nothing may free, restore, move,
delete or add its files meanwhile, or recovery would find a file at
neither name. Each path that could is refused with the code
`adoption_unfinished`. The journal here is written by hand, naming the two
volumes of tests/unit/test_adopt_apply.py.

    python -m pytest tests/unit/test_adopt_journal_guards.py

Version: 0.3.3
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from tests.unit.test_adopt_apply import _journal, _setup
from tests.unit.test_adopt_captures_plan import SRC, _record, own_data_dir, tenant  # noqa: F401
from tests.unit.test_image_upload import remote  # noqa: F401
from tests.unit.test_offsite_free import _as


@pytest.fixture
def journaled(db_session, override_projects_root, tenant):
    src, tgt, images, plan = _setup(db_session, override_projects_root)
    _journal().write_text(json.dumps({"version": 1, "run_id": "r", "source_id": src.id, "target_id": tgt.id}))
    return src, tgt, images


@pytest.mark.unit
def test_the_one_helper_answers_for_the_two_volumes_only(db_session, journaled):
    from app.core.adopt_journal import journal_names_volume
    from tests.unit.test_adopt_captures_plan import _volume

    src, tgt, _ = journaled
    other = _volume(db_session, "Otro", "xx-other-001")
    assert journal_names_volume(src.id) and journal_names_volume(tgt.id)
    assert not journal_names_volume(other.id)
    _journal().write_text("torn")
    assert journal_names_volume(other.id)


@pytest.mark.unit
def test_freeing_its_dry_run_and_restoring_are_refused(db_session, journaled, monkeypatch):
    from app.core import offsite_free

    from app.core import unit_mode

    # The unit's mode is not what is tested.
    monkeypatch.setattr(offsite_free, "_refuse_offline", lambda db: None)
    monkeypatch.setattr(unit_mode, "offsite_on", lambda db: True)

    src, tgt, _ = journaled
    for call in (offsite_free.plan_free, offsite_free._prepare_free, offsite_free._prepare_restore):
        with pytest.raises(offsite_free.FreeRefused) as refused:
            call(db_session, src.id)
        assert refused.value.detail["code"] == "adoption_unfinished"
    with pytest.raises(offsite_free.FreeRefused):
        offsite_free._free_work(db_session, src.id, SRC, "admin")
    with pytest.raises(ValueError, match="adoption"):
        offsite_free.mark_freed(db_session, SRC)


@pytest.mark.unit
def test_a_rebuild_of_the_volume_is_refused(db_session, journaled, tmp_path):
    from app.core.rebuild_volume import RebuildRefused, rebuild_volume

    with pytest.raises(RebuildRefused, match="adoption"):
        rebuild_volume(db_session, SRC, str(tmp_path))


@pytest.mark.unit
def test_the_routes_that_change_its_files_are_refused(client, db_session, journaled):
    from tests.unit.test_adopt_captures_plan import _volume

    src, tgt, images = journaled
    other = _volume(db_session, "Otro", "xx-other-001")
    rec_id, img_id = images[(2, 0)].record_id, images[(2, 0)].id
    placeholder = _record(db_session, src, "placeholder")
    _as("admin")

    responses = {
        "move-records": client.post(f"/collections/{src.id}/move-records", params={"target_collection_id": other.id}),
        "move-records into": client.post(f"/collections/{other.id}/move-records",
                                         params={"target_collection_id": tgt.id}),
        "record delete": client.delete(f"/records/{placeholder.id}"),
        "image delete": client.delete(f"/records/images/{img_id}"),
        "image upload": client.post(f"/records/{rec_id}/images", files={"file": ("x.jpg", b"jpeg", "image/jpeg")}),
        "volume delete": client.delete(f"/collections/{tgt.id}"),
        "export": client.post(f"/collections/{src.id}/export"),
        "record move": client.patch(f"/records/{placeholder.id}", json={"collection_id": other.id}),
    }

    for name, r in responses.items():
        assert r.status_code == 409, (name, r.status_code, r.text)
        assert r.json()["detail"]["code"] == "adoption_unfinished", (name, r.text)


# ---------------------------------------------------------------------------
# A journal that appears after the early check: the action is refused where it can no longer be overtaken
# ---------------------------------------------------------------------------

def _write_journal(src_id, tgt_id):
    _journal().write_text(json.dumps({"version": 1, "run_id": "kept", "phase": "committed",
                                      "source_id": src_id, "target_id": tgt_id}))


@pytest.mark.unit
@pytest.mark.parametrize("named", ["source", "target"])
def test_move_records_reads_the_journal_again_under_its_locks(client, db_session, override_projects_root, tenant,
                                                              monkeypatch, named):
    """The journal is written once move-records holds its record locks and the target's row; it refuses, nothing moves."""
    from app.api import collections as collections_api
    from app.models.record import Record
    from tests.unit.test_adopt_captures_plan import _volume

    a = _volume(db_session, "Uno", "xx-move-001")
    b = _volume(db_session, "Dos", "xx-move-002")
    rec = _record(db_session, a, "r")
    rec_id = rec.id
    real = collections_api.next_sequences

    def locked_then_adopted(db, collection_id, n):
        out = real(db, collection_id, n)
        _write_journal(a.id if named == "source" else -1, b.id if named == "target" else -1)
        return out

    monkeypatch.setattr(collections_api, "next_sequences", locked_then_adopted)
    _as("admin")

    r = client.post(f"/collections/{a.id}/move-records", params={"target_collection_id": b.id})

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "adoption_unfinished"
    db_session.expire_all()
    assert db_session.get(Record, rec_id).collection_id == a.id


@pytest.mark.unit
def test_an_export_is_not_started_while_a_journal_names_the_volume(db_session, override_projects_root, tenant):
    """export_jobs.start reads the journal under its own lock, where the job is started."""
    from app.core.export_jobs import ExportBlockedError, export_jobs
    from tests.unit.test_adopt_captures_plan import _volume

    vol = _volume(db_session, "Uno", "xx-export-001")
    _write_journal(vol.id, -1)
    ran = []

    with pytest.raises(ExportBlockedError):
        export_jobs.start(vol.id, lambda job: ran.append(job))

    assert ran == [] and export_jobs.active_for(vol.id) is None


@pytest.mark.unit
def test_an_export_request_that_passed_the_early_check_is_refused_when_the_job_would_start(
        client, db_session, journaled, monkeypatch):
    """The early check passes (the adoption has not begun); the adoption returns with its journal kept; 409."""
    from app.core import adopt_journal

    from app.models.record import Record

    src, tgt, _ = journaled
    _journal().unlink()
    for rec in db_session.query(Record).filter(Record.collection_id == src.id):
        rec.status = "approved"
    db_session.commit()
    real = adopt_journal.refuse_if_journaled

    def check_then_adoption(ids):
        real(ids)
        _write_journal(src.id, tgt.id)

    monkeypatch.setattr(adopt_journal, "refuse_if_journaled", check_then_adoption)
    _as("admin")

    r = client.post(f"/collections/{src.id}/export")

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "adoption_unfinished"


@pytest.mark.unit
def test_a_rebuild_reads_the_journal_again_before_it_changes_anything(remote, db_session, override_projects_root,
                                                                      tmp_path, monkeypatch):
    """The journal appears after the rebuild's first check: nothing is raised or written."""
    from app.core import rebuild_volume as rv
    from app.models.collection import Collection
    from app.models.record import Record
    from tests.unit.test_rebuild_volume import STEM, _bundle, _volume as _stem_volume

    vol = _stem_volume(db_session)
    vol_id = vol.id
    bundle = _bundle(tmp_path, remote)
    real = rv.load_bundle

    def loaded_then_adopted(bundle_dir, stem):
        out = real(bundle_dir, stem)
        _write_journal(-1, vol_id)
        return out

    monkeypatch.setattr(rv, "load_bundle", loaded_then_adopted)

    with pytest.raises(rv.RebuildRefused, match="adoption"):
        rv.rebuild_volume(db_session, STEM, str(bundle), apply=True)

    db_session.expire_all()
    assert (db_session.get(Collection, vol_id).capture_counter or 0) == 0
    assert db_session.query(Record).filter(Record.collection_id == vol_id).count() == 0


@pytest.mark.unit
def test_a_free_that_passed_its_check_frees_nothing_while_a_journal_with_unchanged_rows_names_the_source(
        remote, db_session, override_projects_root, tenant, monkeypatch):
    """The free's checks pass; an adoption renames some files and is killed, its rows rolled back and its journal
    kept; the free resumes. Nothing is freed, every file is on disk at one of its names, and recovery reverts."""
    import hashlib

    from app.core import adopt_apply, offsite_free, unit_mode
    from app.core.adopt_journal import recover_adoption_at_startup
    from app.core.db import SessionLocal
    from app.models.offsite_object import OffsiteObject
    from tests.unit.test_adopt_apply import _adopt

    class Killed(BaseException):
        pass

    monkeypatch.setattr(offsite_free, "_refuse_offline", lambda db: None)
    monkeypatch.setattr(unit_mode, "offsite_on", lambda db: True)
    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    for row in db_session.query(OffsiteObject).all():
        folder = "captures" if row.kind == "capture" else "previews"
        remote.put(row.object_key, (root / SRC / folder / row.object_key.rsplit("/", 1)[1]).read_bytes())
    src_id = src.id
    inventory = sorted(hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*")
                       if p.is_file() and p.name != "manifest.jsonl")
    real_listings, real_rename = offsite_free._listings, adopt_apply._rename
    renamed = []

    def rename(old, new):
        real_rename(old, new)
        renamed.append(new)
        if len(renamed) == 3:
            raise Killed()

    def listed_then_killed_adoption(keys):
        out = real_listings(keys)
        with pytest.MonkeyPatch.context() as kill:
            kill.setattr(adopt_apply, "_rename", rename)
            with pytest.raises(Killed):
                _adopt(db_session, src, tgt)
        assert _journal().exists()
        return out

    monkeypatch.setattr(offsite_free, "_listings", listed_then_killed_adoption)

    with pytest.raises(offsite_free.FreeRefused) as refused:
        offsite_free._free_work(db_session, src_id, SRC, "admin")

    assert refused.value.detail["code"] == "adoption_unfinished"
    db_session.expire_all()
    from app.models.record import RecordImage
    assert db_session.query(RecordImage).filter(RecordImage.master_freed_at.isnot(None)).count() == 0
    assert db_session.query(RecordImage).filter(RecordImage.preview_freed_at.isnot(None)).count() == 0
    for r in plan.renames:
        assert r.old_path.is_file() or r.new_path.is_file()
    fresh = SessionLocal()
    try:
        report = recover_adoption_at_startup(fresh)
    finally:
        fresh.close()
    assert report["outcome"] == "reverted", report
    assert sorted(hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*")
                  if p.is_file() and p.name != "manifest.jsonl") == inventory


@pytest.mark.unit
def test_a_restore_that_passed_its_check_writes_nothing_back_while_a_journal_names_the_volume(
        db_session, override_projects_root, tenant, monkeypatch):
    """Under the record's lock, where a restore clears a freed state: refused while a journal names the volume."""
    from app.core import offsite_free
    from app.models.record import RecordImage

    root = override_projects_root
    src, tgt, images, plan = _setup(db_session, root)
    img = images[(2, 0)]
    img.master_freed_at, img.master_freed_bytes = datetime(2026, 11, 19), 1
    db_session.commit()
    obj = offsite_free._Object(img.id, img.record_id, "capture", img.file_path, Path(img.file_path),
                               "k", "0" * 40)
    _journal().write_text(json.dumps({"version": 1, "run_id": "kept", "source_id": src.id, "target_id": tgt.id}))

    with pytest.raises(offsite_free.FreeRefused) as refused:
        offsite_free._set_freed(db_session, obj, at=None, size=None)

    assert refused.value.detail["code"] == "adoption_unfinished"
    db_session.expire_all()
    assert db_session.get(RecordImage, img.id).master_freed_at is not None


@pytest.mark.unit
def test_a_free_cut_short_by_the_guard_logs_what_it_freed(remote, db_session, override_projects_root, tenant,
                                                         monkeypatch):
    """The first file is freed; a journal then names the volume; the run is refused at the next file: its summary
    is logged at WARN with the one file freed and the manifest's name."""
    from app.core import offsite_free, unit_mode
    from app.models.offsite_object import OffsiteObject
    from app.models.system_log import SystemLog

    monkeypatch.setattr(offsite_free, "_refuse_offline", lambda db: None)
    monkeypatch.setattr(unit_mode, "offsite_on", lambda db: True)
    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    for row in db_session.query(OffsiteObject).all():
        folder = "captures" if row.kind == "capture" else "previews"
        remote.put(row.object_key, (root / SRC / folder / row.object_key.rsplit("/", 1)[1]).read_bytes())
    src_id, tgt_id = src.id, tgt.id
    real = offsite_free._set_freed
    calls = []

    def freed_then_adoption(db, obj, *, at, size):
        out = real(db, obj, at=at, size=size)
        calls.append(obj)
        if len(calls) == 1:
            _write_journal(src_id, tgt_id)
        return out

    monkeypatch.setattr(offsite_free, "_set_freed", freed_then_adoption)

    with pytest.raises(offsite_free.FreeRefused):
        offsite_free._free_work(db_session, src_id, SRC, "admin")

    db_session.expire_all()
    [entry] = db_session.query(SystemLog).filter(SystemLog.action == offsite_free.FREED_ACTION).all()
    assert entry.level == "WARN"
    assert "files=1 " in entry.detail and "manifest=offsite-freed-" in entry.detail and "stopped" in entry.detail
