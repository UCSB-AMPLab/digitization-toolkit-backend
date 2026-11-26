"""Applying an adoption: a hand-made volume's captures renamed and moved into the volume Fisqua assigned.

adopt_captures (app.core.adopt_apply) holds both volumes, locks the rows,
plans again under the locks, lists the bucket under the target's stem,
appends the source's relocation records, retires the source's offsite rows,
renames every file, writes the rows, commits, and appends the target's
manifest lines. These tests run it on real files and rows: the source volume
of tests/unit/test_adopt_manifest.py (a dual record of RAW masters with a
superseded pair and its retake, a dual record whose previews' digests were
backfilled, a JPEG master with its `.raw` buffer, and an uploaded JPEG),
and, where the bucket matters, the local rclone remote of
tests/unit/test_image_upload.py with the source's objects already in it.

Each way the adoption can stop is checked to leave the rows, the files and
the target's manifest as they were. The source's relocation records, once
appended, stay: each names its image and the sha256 it already had, so the
index reads them the same whether or not the files moved.

    python -m pytest tests/unit/test_adopt_apply.py

Version: 0.3.3
"""

import errno
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.unit.test_adopt_captures_plan import SRC, TGT, TENANT, _plan, _record, own_data_dir, tenant  # noqa: F401
from tests.unit.test_adopt_manifest import _lines, _sha, _source
from tests.unit.test_image_upload import remote  # noqa: F401

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def _empty_listing(tenant, stem):
    return []


def _adopt(db, src, tgt, **kw):
    from app.core.adopt_apply import adopt_captures

    kw.setdefault("list_target_prefix", _empty_listing)
    return adopt_captures(db, src.id, tgt.id, actor="admin", now=NOW, **kw)


def _manifest(root, stem) -> Path:
    return root / stem / "metadata" / "manifest.jsonl"


def _snapshot(db, root):
    """Every row of every table but the system log, and every file under the root but the two manifests, by bytes."""
    from app.core.db import Base

    db.expire_all()
    rows = {}
    for table in Base.metadata.sorted_tables:
        if table.name == "system_logs":
            continue
        rows[table.name] = sorted(tuple(str(v) for v in r) for r in db.execute(table.select()).fetchall())
    manifests = {_manifest(root, SRC), _manifest(root, TGT)}
    files = {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
             if p.is_file() and p not in manifests}
    return rows, files


def _relocations_only(root, before: bytes) -> list:
    """The lines the source's manifest gained since `before`, which must all be relocation records."""
    after = _manifest(root, SRC).read_bytes()
    assert after.startswith(before)
    added = [json.loads(line) for line in after[len(before):].decode().splitlines() if line.strip()]
    assert all(line["record_type"] == "relocation" for line in added)
    return added


def _journal():
    from app.core import config

    return config.settings.data_dir / "adopt-captures.journal"


def _audit(db, action):
    from app.models.system_log import SystemLog

    db.expire_all()
    return db.query(SystemLog).filter(SystemLog.action == action).all()


def _holds_released(*ids):
    from app.core.export_jobs import export_jobs

    with export_jobs.hold_for_reading(list(ids)):
        return True


def _setup(db, root):
    src, tgt, images = _source(db, root)
    plan = _plan(db, src, tgt)
    assert plan.refusals == [], plan.details
    return src, tgt, images, plan


# ---------------------------------------------------------------------------
# The adoption that runs
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_adoption_moves_every_file_and_row_and_the_upload_sends_them_again(remote, db_session, override_projects_root):
    from app.core.adopt_apply import adopt_captures
    from app.core.capture_manifest import build_capture_manifest
    from app.core.image_upload import pending_work, run_image_upload, volume_upload_state
    from app.core.integrity import run_integrity_check
    from app.core.offsite_retired import retired_keys
    from app.models.collection import Collection
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage

    root = override_projects_root
    src, tgt, images, plan = _setup(db_session, root)
    for row in db_session.query(OffsiteObject).all():
        folder = "captures" if row.kind == "capture" else "previews"
        remote.put(row.object_key, (root / SRC / folder / row.object_key.rsplit("/", 1)[1]).read_bytes())
    old_bucket = remote.files()
    bytes_of = {r.image_id: (r.old_path.read_bytes(), r.old_preview_path and r.old_preview_path.read_bytes(),
                             r.old_raw_buffer_path and r.old_raw_buffer_path.read_bytes()) for r in plan.renames}

    result = adopt_captures(db_session, src.id, tgt.id, actor="admin", now=NOW)

    assert result["records"] == 4 and result["images"] == len(plan.renames) == 8
    assert result["rows_retired"] == 8 + 6          # a capture row for each image, a preview row for each RAW master
    assert result["files_renamed"] == 8 + 6 + 1 + 8  # masters, previews, the buffer, the thumbnails
    assert result["target_lines_appended"] is True and result["counter_after"] == 5
    db_session.expire_all()
    for r in plan.renames:
        master, preview, buffer = bytes_of[r.image_id]
        assert r.new_path.read_bytes() == master and not r.old_path.exists()
        if r.new_preview_path is not None:
            assert r.new_preview_path.read_bytes() == preview and not r.old_preview_path.exists()
        if r.new_raw_buffer_path is not None:
            assert r.new_raw_buffer_path.read_bytes() == buffer and not r.old_raw_buffer_path.exists()
        img = db_session.get(RecordImage, r.image_id)
        assert (img.filename, img.file_path, img.pair_id) == (r.new_name, str(r.new_path), r.new_pair_id)
        assert img.thumbnail_path == str(r.new_thumbnail_path) and Path(img.thumbnail_path).is_file()
        assert db_session.get(Record, r.record_id).collection_id == tgt.id
    assert db_session.query(Record).filter(Record.collection_id == src.id).count() == 0
    assert sorted(r.sequence for r in db_session.query(Record).filter(Record.collection_id == tgt.id)) == [1, 2, 3, 4]
    assert db_session.get(Collection, tgt.id).capture_counter == 5
    retired = db_session.query(OffsiteObject).filter(OffsiteObject.retired_at.isnot(None)).all()
    assert len(retired) == 14 and all(r.record_image_id is None for r in retired)

    work = pending_work(db_session)
    raw = {r.image_id for r in plan.renames if r.new_preview_path is not None}
    assert sorted((w.image_id, w.kind) for w in work) == sorted(
        [(r.image_id, "capture") for r in plan.renames] + [(i, "preview") for i in raw])
    assert all(w.stem == TGT for w in work)

    status = run_image_upload(db_session)

    assert status["status"] == "done", status
    db_session.expire_all()
    for r in plan.renames:
        for kind, key in r.new_keys.items():
            row = db_session.query(OffsiteObject).filter(OffsiteObject.record_image_id == r.image_id,
                                                         OffsiteObject.kind == kind).one()
            assert row.object_key == key and row.verified_at is not None
    assert volume_upload_state(db_session, db_session.get(Collection, tgt.id))["received"] is True
    keys = retired_keys(db_session)
    assert len(keys) == 14 and all(k["deletable"] for k in keys)
    assert all(remote.files()[k] == v for k, v in old_bucket.items())

    report = run_integrity_check(db_session)
    assert report["missing_files"] == [] and report["manifest_mismatches"] == []
    assert report["manifest_missing_entry"] == []

    for rec in db_session.query(Record).filter(Record.collection_id == tgt.id):
        rec.status = "approved"
    db_session.commit()
    doc = build_capture_manifest(db_session, db_session.get(Collection, tgt.id))
    entries = [i for rec in doc["volumes"][0]["records"] for i in rec["images"]]
    assert len(entries) == 6
    for entry in entries:
        assert entry["filename"].startswith(f"{TGT}-img_") and entry["objectKey"].startswith(f"{TGT}/captures/")
        if "previewObjectKey" in entry:
            assert entry["previewObjectKey"].startswith(f"{TGT}/previews/{TGT}-img_")


@pytest.mark.unit
def test_an_adoption_applied_twice_is_refused_the_second_time(db_session, override_projects_root, tenant):
    from app.core.adopt_apply import AdoptRefused

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    _adopt(db_session, src, tgt)
    before = _snapshot(db_session, root)
    manifests = (_manifest(root, SRC).read_bytes(), _manifest(root, TGT).read_bytes())

    with pytest.raises(AdoptRefused) as refused:
        _adopt(db_session, src, tgt)

    assert refused.value.status_code == 409 and "target_has_records" in refused.value.codes
    assert _snapshot(db_session, root) == before
    assert (_manifest(root, SRC).read_bytes(), _manifest(root, TGT).read_bytes()) == manifests


@pytest.mark.unit
def test_the_adoption_is_logged(db_session, override_projects_root, tenant):
    from app.models.system_log import SystemLog

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)

    _adopt(db_session, src, tgt)

    [entry] = db_session.query(SystemLog).filter(SystemLog.action == "volume_captures_adopted").all()
    assert (entry.level, entry.category, entry.actor, entry.subject) == ("INFO", "system", "admin", TGT)
    assert f"source={src.id} ({SRC})" in entry.detail and "records=4 images=8" in entry.detail


# ---------------------------------------------------------------------------
# Every place it can stop, and what that leaves
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_refusal_under_the_locks_changes_nothing(db_session, override_projects_root, tenant):
    from app.core.adopt_apply import AdoptRefused

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    _record(db_session, tgt, "arrived meanwhile")
    before, manifest = _snapshot(db_session, root), _manifest(root, SRC).read_bytes()

    with pytest.raises(AdoptRefused) as refused:
        _adopt(db_session, src, tgt)

    assert (refused.value.status_code, refused.value.codes) == (409, ["target_has_records"])
    assert _snapshot(db_session, root) == before and _manifest(root, SRC).read_bytes() == manifest
    assert not _manifest(root, TGT).exists()


@pytest.mark.unit
def test_objects_already_under_the_targets_stem_refuse(db_session, override_projects_root, tenant):
    from app.core.adopt_apply import AdoptRefused

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    before, manifest = _snapshot(db_session, root), _manifest(root, SRC).read_bytes()
    stray = f"{TENANT}/{TGT}/captures/{TGT}-img_0001_c0.CR2"

    with pytest.raises(AdoptRefused) as refused:
        _adopt(db_session, src, tgt, list_target_prefix=lambda tenant, stem: [stray])

    assert (refused.value.status_code, refused.value.codes) == (409, ["target_has_bucket_objects"])
    assert refused.value.details == {"target_has_bucket_objects": [stray]}
    assert _snapshot(db_session, root) == before and _manifest(root, SRC).read_bytes() == manifest


@pytest.mark.unit
def test_a_listing_that_cannot_answer_refuses_with_503(db_session, override_projects_root, tenant):
    from app.core.adopt_apply import AdoptRefused
    from app.core.catalogue_backup import VerificationUnavailable

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    before, manifest = _snapshot(db_session, root), _manifest(root, SRC).read_bytes()

    def unavailable(tenant, stem):
        raise VerificationUnavailable("401 unauthorized")

    with pytest.raises(AdoptRefused) as refused:
        _adopt(db_session, src, tgt, list_target_prefix=unavailable)

    assert (refused.value.status_code, refused.value.codes) == (503, ["listing_unavailable"])
    assert _snapshot(db_session, root) == before and _manifest(root, SRC).read_bytes() == manifest


@pytest.mark.unit
def test_the_real_listing_of_an_empty_stem_lets_it_run_and_a_refused_listing_stops_it(remote, db_session, override_projects_root):
    from app.core.adopt_apply import AdoptRefused, adopt_captures

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    remote.set_modes("listing-refused")
    with pytest.raises(AdoptRefused) as refused:
        adopt_captures(db_session, src.id, tgt.id, actor="admin", now=NOW)
    assert refused.value.codes == ["listing_unavailable"]

    remote.set_modes()
    remote.put(f"{TENANT}/{TGT}/previews/x_preview.jpg", b"x")
    with pytest.raises(AdoptRefused) as refused:
        adopt_captures(db_session, src.id, tgt.id, actor="admin", now=NOW)
    assert refused.value.details == {"target_has_bucket_objects": [f"{TENANT}/{TGT}/previews/x_preview.jpg"]}

    (remote.root / TENANT / TGT / "previews" / "x_preview.jpg").unlink()
    assert adopt_captures(db_session, src.id, tgt.id, actor="admin", now=NOW)["images"] == 8


@pytest.mark.unit
def test_a_retirement_refused_changes_nothing_but_the_sources_relocations(db_session, override_projects_root, tenant, monkeypatch):
    from app.core import offsite_retired
    from app.core.adopt_apply import AdoptRefused

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    before, manifest = _snapshot(db_session, root), _manifest(root, SRC).read_bytes()

    def refused(db, image_ids, *, now):
        raise offsite_retired.RetireRefused([{"image_id": 1, "kind": "capture", "key": "k", "state": "pending"}])

    monkeypatch.setattr(offsite_retired, "retire_rows", refused)
    with pytest.raises(AdoptRefused) as exc:
        _adopt(db_session, src, tgt)

    assert (exc.value.status_code, exc.value.codes) == (409, ["offsite_rows_not_verified"])
    assert _snapshot(db_session, root) == before
    assert len(_relocations_only(root, manifest)) == len(plan.renames)
    assert not _manifest(root, TGT).exists()


def _failing_rename(monkeypatch, at):
    """Make the adoption's `at`-th rename (1-based) raise; return the list of calls made."""
    from app.core import adopt_apply

    calls = []
    real = adopt_apply._rename

    def rename(src, dst):
        calls.append((src, dst))
        if len(calls) == at:
            raise OSError(errno.EIO, "input/output error")
        real(src, dst)

    monkeypatch.setattr(adopt_apply, "_rename", rename)
    return calls


@pytest.mark.unit
@pytest.mark.parametrize("at", ["first", "middle", "last"])
def test_a_rename_that_fails_puts_every_file_back(db_session, override_projects_root, tenant, monkeypatch, at):
    from app.core.adopt_apply import AdoptFailed

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    before, manifest = _snapshot(db_session, root), _manifest(root, SRC).read_bytes()
    total = 23   # 8 masters, 6 previews, 1 buffer, 8 thumbnails
    calls = _failing_rename(monkeypatch, {"first": 1, "middle": 10, "last": total}[at])

    with pytest.raises(AdoptFailed) as failed:
        _adopt(db_session, src, tgt)

    assert len(calls) == {"first": 1, "middle": 10, "last": total}[at]
    assert failed.value.restored is True
    assert _snapshot(db_session, root) == before
    assert not _journal().exists() and _holds_released(src.id, tgt.id)
    [event] = _audit(db_session, "volume_captures_adoption_reverted")
    assert event.level == "WARN" and f"source={src.id}" in event.detail
    assert len(_relocations_only(root, manifest)) == len(plan.renames)
    assert not _manifest(root, TGT).exists()


@pytest.mark.unit
def test_a_cross_device_rename_stops_the_adoption_and_copies_nothing(db_session, override_projects_root, tenant, monkeypatch):
    from app.core import adopt_apply
    from app.core.adopt_apply import AdoptFailed

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    before = _snapshot(db_session, root)

    def cross_device(src_path, dst_path):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(adopt_apply, "_rename", cross_device)
    with pytest.raises(AdoptFailed):
        _adopt(db_session, src, tgt)

    assert _snapshot(db_session, root) == before
    assert not (root / TGT).exists() or not [p for p in (root / TGT).rglob("*") if p.is_file()]


@pytest.mark.unit
def test_a_destination_that_appears_before_its_rename_stops_the_adoption(db_session, override_projects_root, tenant, monkeypatch):
    from app.core import adopt_apply
    from app.core.adopt_apply import AdoptFailed

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    before_rows, before_files = _snapshot(db_session, root)
    real = adopt_apply._rename
    calls = []

    def rename_then_occupy(src_path, dst_path):
        real(src_path, dst_path)
        calls.append(dst_path)
        if len(calls) == 3:
            stray = plan.renames[-1].new_path
            stray.parent.mkdir(parents=True, exist_ok=True)
            stray.write_bytes(b"not ours")

    monkeypatch.setattr(adopt_apply, "_rename", rename_then_occupy)
    with pytest.raises(AdoptFailed) as failed:
        _adopt(db_session, src, tgt)

    assert "already exists" in str(failed.value)
    rows, files = _snapshot(db_session, root)
    stray_key = str(plan.renames[-1].new_path.relative_to(root))
    assert rows == before_rows
    assert files == {**before_files, stray_key: b"not ours"}


def _commit_raising(monkeypatch, db, *, land: bool):
    """Make the adoption's commit (the first while its journal exists: the rows') raise, after landing or not."""
    real = db.commit
    raised = []

    def commit():
        if _journal().exists() and not raised:
            raised.append(True)
            if land:
                real()
            raise RuntimeError("connection lost")
        real()

    monkeypatch.setattr(db, "commit", commit)


@pytest.mark.unit
def test_a_commit_that_raises_without_landing_puts_every_file_back(db_session, override_projects_root, tenant, monkeypatch):
    from app.core.adopt_apply import AdoptFailed

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    before, manifest = _snapshot(db_session, root), _manifest(root, SRC).read_bytes()
    _commit_raising(monkeypatch, db_session, land=False)

    with pytest.raises(AdoptFailed) as failed:
        _adopt(db_session, src, tgt)
    monkeypatch.undo()

    assert failed.value.restored is True
    assert _snapshot(db_session, root) == before
    assert len(_relocations_only(root, manifest)) == len(plan.renames)
    assert not _manifest(root, TGT).exists()
    assert not _journal().exists() and _audit(db_session, "volume_captures_adoption_reverted")


@pytest.mark.unit
def test_a_commit_that_raises_after_landing_goes_forward(db_session, override_projects_root, tenant, monkeypatch, caplog):
    from app.models.record import RecordImage

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    _commit_raising(monkeypatch, db_session, land=True)

    with caplog.at_level(logging.WARNING, logger="app.core.adopt_apply"):
        result = _adopt(db_session, src, tgt)
    monkeypatch.undo()

    assert result["target_lines_appended"] is True
    assert any(r.levelno == logging.WARNING and "landed" in r.getMessage() for r in caplog.records)
    db_session.expire_all()
    for r in plan.renames:
        assert r.new_path.is_file() and not r.old_path.exists()
        assert db_session.get(RecordImage, r.image_id).file_path == str(r.new_path)


@pytest.mark.unit
def test_a_commit_whose_outcome_is_unknown_leaves_the_files_and_logs_every_pair(db_session, override_projects_root, tenant, monkeypatch, caplog):
    from app.core import adopt_apply
    from app.core.adopt_apply import AdoptFailed

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    _commit_raising(monkeypatch, db_session, land=False)
    monkeypatch.setattr(adopt_apply, "_relocation_landed", lambda db, new_paths: None)

    with caplog.at_level(logging.ERROR, logger="app.core.adopt_apply"):
        with pytest.raises(AdoptFailed) as failed:
            _adopt(db_session, src, tgt)

    assert failed.value.restored is False
    for r in plan.renames:
        assert r.new_path.is_file() and not r.old_path.exists()
        assert any(str(r.old_path) in m and str(r.new_path) in m
                   for m in (rec.getMessage() for rec in caplog.records if rec.levelno == logging.ERROR))
    assert not _manifest(root, TGT).exists()
    assert _journal().exists()   # startup recovery decides by the rows which way to go
    [event] = _audit(db_session, "volume_captures_adoption_outcome_unknown")
    assert event.level == "ERR" and "adopt-captures.journal" in event.detail


@pytest.mark.unit
def test_target_lines_that_cannot_be_appended_are_logged_and_the_adoption_stands(db_session, override_projects_root, tenant, monkeypatch, caplog):
    from app.core import adopt_apply
    from app.core.integrity import build_fixity_index

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)

    def disk_full(volume_root, lines):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(adopt_apply, "_append_target_lines", disk_full)
    with caplog.at_level(logging.ERROR, logger="app.core.adopt_apply"):
        result = _adopt(db_session, src, tgt)

    assert result["target_lines_appended"] is False and result["target_lines_pending"] is True
    assert any(rec.levelno == logging.ERROR and plan.renames[0].capture_id in rec.getMessage()
               for rec in caplog.records)
    journal = json.loads(_journal().read_text())
    assert journal["phase"] == "committed" and len(journal["target_lines"]) == 7
    index = build_fixity_index(root.resolve())
    for r in plan.renames:
        assert index.expected(r.capture_id, r.image_id, r.new_path.resolve()) == _sha(r.new_path)


# ---------------------------------------------------------------------------
# The hold
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_upload_pass_during_the_adoption_skips_both_volumes(remote, db_session, override_projects_root, monkeypatch):
    """Between the commit and the release of the holds, the images are pending under the new keys; none is read."""
    from app.core import adopt_apply
    from app.core.db import SessionLocal
    from app.core.image_upload import pending_work, run_image_upload
    from app.models.offsite_object import OffsiteObject

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    seen = {}
    real = adopt_apply._append_target_lines

    def upload_first(volume_root, lines):
        other = SessionLocal()
        try:
            seen["work"] = pending_work(other)
            seen["run"] = run_image_upload(other)
            seen["rows"] = other.query(OffsiteObject).filter(OffsiteObject.retired_at.is_(None)).count()
        finally:
            other.close()
        real(volume_root, lines)

    monkeypatch.setattr(adopt_apply, "_append_target_lines", upload_first)
    _adopt(db_session, src, tgt, list_target_prefix=_empty_listing)

    assert len(seen["work"]) == 14
    assert seen["run"]["status"] == "done", seen["run"]
    assert len(seen["run"]["skipped"]) == 14
    assert {s["reason"] for s in seen["run"]["skipped"]} == {"files of the volume being changed"}
    assert seen["rows"] == 0
    assert remote.calls("copyto") == []


@pytest.mark.unit
def test_a_volume_whose_files_are_being_read_is_refused(db_session, override_projects_root, tenant):
    from app.core.adopt_apply import AdoptRefused
    from app.core.export_jobs import export_jobs

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    before = _snapshot(db_session, root)

    with export_jobs.hold_for_reading([tgt.id]):
        with pytest.raises(AdoptRefused) as refused:
            _adopt(db_session, src, tgt)

    assert (refused.value.status_code, refused.value.codes) == (409, ["volume_held"])
    assert _snapshot(db_session, root) == before


# ---------------------------------------------------------------------------
# R2: a record whose bytes are offsite only under a retired key stays where it is
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_record_offsite_only_under_retired_keys_is_still_stored_offsite(db_session, override_projects_root, tenant):
    from app.core.image_upload import records_stored_offsite
    from app.core.offsite_retired import retire_rows
    from app.models.record import RecordImage

    root = override_projects_root
    src, tgt, images, _ = _setup(db_session, root)
    rec_ids = sorted({img.record_id for img in images.values()})
    assert records_stored_offsite(db_session, rec_ids) == rec_ids

    retire_rows(db_session, [img.id for img in images.values()], now=NOW)
    db_session.commit()

    assert records_stored_offsite(db_session, rec_ids) == rec_ids
    db_session.delete(db_session.get(RecordImage, images[(5, 0)].id))
    db_session.commit()
    assert records_stored_offsite(db_session, rec_ids) == [r for r in rec_ids if r != images[(5, 0)].record_id]


@pytest.mark.unit
def test_after_an_adoption_the_records_cannot_be_moved_before_the_new_upload(client, db_session, override_projects_root, tenant):
    from tests.unit.test_offsite_free import _as

    root = override_projects_root
    src, tgt, images, _ = _setup(db_session, root)
    src_id = src.id
    _adopt(db_session, src, tgt)
    _as("admin")
    rec_id = images[(2, 0)].record_id

    r = client.post(f"/collections/{tgt.id}/move-records", params={"target_collection_id": src_id})
    assert r.status_code == 409, r.text
    assert "stored offsite" in r.json()["detail"]["message"]
    r = client.patch(f"/records/{rec_id}", json={"collection_id": src_id})
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# The review's fixes
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_record_added_between_the_record_locks_and_the_volume_lock_is_adopted_too(db_session, override_projects_root, tenant, monkeypatch):
    """F1: the record set is read again once the volume rows are held, and the locks taken again if it changed."""
    from app.core import volume_stem
    from app.core.db import SessionLocal
    from app.models.record import Record

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    real = volume_stem.locked_volume_query
    added = []

    def insert_at_the_first_lock(db, collection_id):
        if not added:
            other = SessionLocal()
            try:
                rec = Record(title="placeholder", collection_id=src.id, capture_mode="single", status="captured")
                other.add(rec)
                other.commit()
                added.append(rec.id)
            finally:
                other.close()
        return real(db, collection_id)

    monkeypatch.setattr(volume_stem, "locked_volume_query", insert_at_the_first_lock)
    result = _adopt(db_session, src, tgt)

    assert result["records"] == 5
    db_session.expire_all()
    assert db_session.query(Record).filter(Record.collection_id == src.id).count() == 0
    assert db_session.get(Record, added[0]).collection_id == tgt.id


@pytest.mark.unit
def test_a_source_that_still_has_a_record_after_the_move_is_refused_and_put_back(db_session, override_projects_root, tenant, monkeypatch):
    """F1: the source must hold no record when the rows are about to be committed."""
    from app.core import record_sequence
    from app.core.adopt_apply import AdoptRefused
    from app.models.record import Record

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    before = _snapshot(db_session, root)
    real = record_sequence.next_sequences

    def add_one(db, collection_id, count):
        db.add(Record(title="late", collection_id=src.id, capture_mode="single", status="captured"))
        return real(db, collection_id, count)

    monkeypatch.setattr(record_sequence, "next_sequences", add_one)
    with pytest.raises(AdoptRefused) as refused:
        _adopt(db_session, src, tgt)

    assert refused.value.codes == ["source_records_changed"]
    assert _snapshot(db_session, root) == before and not _journal().exists()
    assert _audit(db_session, "volume_captures_adoption_reverted")


@pytest.mark.unit
def test_the_bucket_is_listed_before_any_row_lock(db_session, override_projects_root, tenant, monkeypatch):
    """F2: no network call while row locks are held."""
    from app.core import volume_stem

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    order = []
    real = volume_stem.locked_volume_query

    def lock(db, collection_id):
        order.append("lock")
        return real(db, collection_id)

    def listing(tenant, stem):
        order.append("list")
        return []

    monkeypatch.setattr(volume_stem, "locked_volume_query", lock)
    _adopt(db_session, src, tgt, list_target_prefix=listing)

    assert order[0] == "list" and order.count("list") == 1 and "lock" in order


@pytest.mark.unit
def test_a_target_directory_that_cannot_be_made_stops_the_adoption_and_puts_everything_back(db_session, override_projects_root, tenant, monkeypatch):
    """F3: directory creation is inside the guarded section."""
    import capture.project_manager as project_manager
    from app.core.adopt_apply import AdoptFailed

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    before = _snapshot(db_session, root)

    def half_made(stem, root=None):
        (override_projects_root / stem / "captures").mkdir(parents=True)
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(project_manager, "ensure_volume_dirs", half_made)
    with pytest.raises(AdoptFailed) as failed:
        _adopt(db_session, src, tgt)

    assert failed.value.restored is True
    assert _snapshot(db_session, root) == before
    assert not (root / TGT).exists()
    assert not _journal().exists() and _holds_released(src.id, tgt.id)
    assert _audit(db_session, "volume_captures_adoption_reverted")


@pytest.mark.unit
def test_files_are_put_back_before_the_rollback_releases_the_locks(db_session, override_projects_root, tenant, monkeypatch):
    """F5: when the rollback runs, every file is already at its old name."""
    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    _failing_rename(monkeypatch, 10)
    real = db_session.rollback
    seen = []

    def rollback():
        seen.append(all(r.old_path.exists() for r in plan.renames))
        real()

    monkeypatch.setattr(db_session, "rollback", rollback)
    from app.core.adopt_apply import AdoptFailed

    with pytest.raises(AdoptFailed):
        _adopt(db_session, src, tgt)
    monkeypatch.undo()

    assert seen and seen[-1] is True
    assert seen[0] is True   # the first rollback after the renames began, too


@pytest.mark.unit
def test_files_are_put_back_before_the_rollback_when_the_commit_did_not_land(db_session, override_projects_root, tenant, monkeypatch):
    """F5, the commit branch: the locks are taken again and the files put back before the session lets go."""
    from app.core import adopt_apply
    from app.core.adopt_apply import AdoptFailed

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    _commit_raising(monkeypatch, db_session, land=False)
    real_undo = adopt_apply._undo
    order = []

    def undo(moved):
        order.append("undo")
        return real_undo(moved)

    real_rollback = db_session.rollback

    def rollback():
        order.append("rollback")
        real_rollback()

    monkeypatch.setattr(adopt_apply, "_undo", undo)
    monkeypatch.setattr(db_session, "rollback", rollback)
    with pytest.raises(AdoptFailed):
        _adopt(db_session, src, tgt)
    monkeypatch.undo()

    # The failed commit's own rollback, then the locks taken again, the files back, and the release.
    assert order[-2:] == ["undo", "rollback"]
    assert all(r.old_path.exists() for r in plan.renames)


@pytest.mark.unit
def test_an_undo_that_fails_is_reported_and_overwrites_nothing(db_session, override_projects_root, tenant, monkeypatch):
    import hashlib

    from app.core import adopt_apply
    from app.core.adopt_apply import AdoptFailed

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    _failing_rename(monkeypatch, 10)
    real_back = adopt_apply._rename_back
    stuck = []

    def back(new, old):
        if not stuck:
            stuck.append((old, new))
            raise OSError(errno.EACCES, "Permission denied")
        real_back(new, old)

    monkeypatch.setattr(adopt_apply, "_rename_back", back)
    before = sorted(hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*")
                    if p.is_file() and p.name != "manifest.jsonl")

    with pytest.raises(AdoptFailed) as failed:
        _adopt(db_session, src, tgt)

    assert failed.value.restored is False
    after = sorted(hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*")
                   if p.is_file() and p.name != "manifest.jsonl")
    assert after == before
    old, new = stuck[0]
    assert new.is_file() and not old.exists()
    assert _holds_released(src.id, tgt.id)
    assert _journal().exists()   # startup recovery puts the last file back
    [event] = _audit(db_session, "volume_captures_adoption_not_restored")
    assert event.level == "ERR" and "1 file" in event.detail
