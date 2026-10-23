"""Follow-ups to the volume layout migration: old journals, pending sidecars, adoption, fixity after renames.

Uses the fixtures of test_volume_dir_by_id: the name-based `legacy` layout
under the per-test projects root, and a writable data directory.
"""

import json
import os
from pathlib import Path

import pytest

from tests.unit.test_volume_dir_by_id import capture_settings, data_dir, legacy  # noqa: F401


# ---------------------------------------------------------------------------
# A journal written by the first version of the migration
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_v1_journal_left_by_the_earlier_commits_is_completed(db_session, legacy, data_dir):
    """2d1116b wrote version-1 entries, with no "adopt" and no "capture_id". The crash came after the first move."""
    from app.core.volume_layout import JOURNAL_NAME, migrate_volume_layout_at_startup
    from app.models.record import RecordImage

    i2 = legacy["i2"]
    src = Path(i2.file_path).resolve()
    dst = (legacy["root"] / "proj" / f"collection-{legacy['a2'].id}" / "images" / "main" / src.name).resolve()
    entry = {"image_id": i2.id, "record_id": i2.record_id, "collection_id": legacy["a2"].id,
             "file_path": i2.file_path, "filename": i2.filename, "from": str(src), "to": str(dst),
             "sidecar_from": None, "sidecar_to": None, "thumb_from": None, "thumb_to": None, "thumb_clear": None}
    (data_dir / JOURNAL_NAME).write_text(json.dumps({"version": 1, "root": str(legacy["root"].resolve()),
                                                     "collection_id": legacy["a2"].id, "entries": [entry]}))
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)  # the move happened, the commit did not

    migrate_volume_layout_at_startup(db_session)

    db_session.expire_all()
    row = db_session.get(RecordImage, i2.id)
    assert Path(row.file_path) == dst
    assert dst.read_bytes() == b"bytes of IMG_0002.jpg"
    assert not (data_dir / JOURNAL_NAME).exists()


# ---------------------------------------------------------------------------
# A pending sidecar whose master was renamed after the sidecar failed to follow
# ---------------------------------------------------------------------------

def _fail_sidecars_once(vl, monkeypatch, db):
    """Run the migration at startup with every sidecar move failing, then restore the real move."""
    real = vl._move

    def flaky(src, dst):
        if Path(dst).name.endswith("_preview.jpg"):
            raise OSError(5, "Input/output error")
        real(src, dst)

    monkeypatch.setattr(vl, "_move", flaky)
    vl.migrate_volume_layout_at_startup(db)
    monkeypatch.setattr(vl, "_move", real)
    return real


@pytest.mark.unit
def test_a_pending_sidecar_whose_master_is_renumbered_meanwhile_is_not_silently_dropped(db_session, legacy, monkeypatch):
    """The backend serves while a sidecar is pending, and a renumber renames its master."""
    import app.core.volume_layout as vl
    from app.core.paths import preview_sidecar_of
    from app.core.storage_ops import renumber_collection_images
    from app.models.record import RecordImage

    _fail_sidecars_once(vl, monkeypatch, db_session)
    renumber_collection_images(db_session, legacy["a1"])
    db_session.commit()

    report = vl.migrate_volume_layout(db_session)

    db_session.expire_all()
    master = Path(db_session.get(RecordImage, legacy["i1"].id).file_path)
    assert master.name != "IMG_0001.CR2"
    sidecar = preview_sidecar_of(master)
    assert sidecar.read_bytes() == b"preview of IMG_0001"
    assert not (legacy["shared"] / "IMG_0001_preview.jpg").exists()
    assert {"from": str((legacy["shared"] / "IMG_0001_preview.jpg").resolve()), "to": str(sidecar)} in report["sidecars_moved"]
    assert all(s["image_id"] != legacy["i1"].id for s in vl._read_pending_sidecars())


@pytest.mark.unit
def test_a_pending_sidecar_whose_new_name_is_taken_is_kept_and_logged_with_both_paths(
        db_session, legacy, monkeypatch, caplog):
    import app.core.volume_layout as vl
    from app.core.paths import preview_sidecar_of
    from app.core.storage_ops import renumber_collection_images
    from app.models.record import RecordImage

    _fail_sidecars_once(vl, monkeypatch, db_session)
    renumber_collection_images(db_session, legacy["a1"])
    db_session.commit()
    db_session.expire_all()
    master = Path(db_session.get(RecordImage, legacy["i1"].id).file_path)
    taken = preview_sidecar_of(master)
    taken.write_bytes(b"someone else's preview")
    old = (legacy["shared"] / "IMG_0001_preview.jpg").resolve()

    with caplog.at_level("WARNING"):
        report = vl.migrate_volume_layout(db_session)

    assert taken.read_bytes() == b"someone else's preview"
    assert old.read_bytes() == b"preview of IMG_0001"
    assert [s["from"] for s in report["sidecars_pending"] if s["image_id"] == legacy["i1"].id] == [str(old)]
    assert any(s["image_id"] == legacy["i1"].id for s in vl._read_pending_sidecars())
    assert str(old) in caplog.text and str(taken) in caplog.text


@pytest.mark.unit
def test_a_pending_sidecar_whose_master_row_is_gone_is_reported_once_as_unreferenced_and_dropped(
        db_session, legacy, monkeypatch, caplog, override_projects_root):
    """The entry is dropped and the file left where it is, reported in that run as a file no row references.

    A row found out of place in the same run (a restored old-layout row) makes the
    run plan moves too, and the sidecar must still be in that run's report.
    """
    import app.core.volume_layout as vl
    from app.models.collection import Collection
    from app.models.record import Record, RecordImage

    _fail_sidecars_once(vl, monkeypatch, db_session)
    db_session.delete(db_session.get(RecordImage, legacy["i1"].id))
    db_session.commit()
    c = Collection(name="Restored", project_id=legacy["proj"].id)
    db_session.add(c)
    db_session.commit()
    f = override_projects_root / "proj" / "restored" / "images" / "main" / "R_0001.jpg"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"restored")
    rec = Record(title="r", collection_id=c.id, status="captured", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.add(RecordImage(record_id=rec.id, filename=f.name, file_path=str(f), format="jpg", role="single"))
    db_session.commit()
    old = (legacy["shared"] / "IMG_0001_preview.jpg").resolve()

    with caplog.at_level("WARNING"):
        report = vl.migrate_volume_layout(db_session)

    assert report["moved"]  # the restored row was planned and moved in the same run
    assert old.read_bytes() == b"preview of IMG_0001"
    assert str(old) in report["left_unreferenced"]
    assert all(s["image_id"] != legacy["i1"].id for s in report["sidecars_pending"])
    assert all(s["image_id"] != legacy["i1"].id for s in vl._read_pending_sidecars())
    assert sum(str(old) in r.getMessage() and "row is gone" in r.getMessage() for r in caplog.records) == 1

    caplog.clear()
    with caplog.at_level("WARNING"):
        again = vl.migrate_volume_layout(db_session)
    assert "row is gone" not in caplog.text
    assert old.read_bytes() == b"preview of IMG_0001"
    assert all(s["image_id"] != legacy["i1"].id for s in again["sidecars_pending"])


# ---------------------------------------------------------------------------
# Adoption only by a checksum the system holds
# ---------------------------------------------------------------------------

def _project_and_collection(db, name="Proj", cname="Low"):
    from app.models.collection import Collection
    from app.models.project import Project

    p = Project(name=name)
    db.add(p)
    db.commit()
    c = Collection(name=cname, project_id=p.id)
    db.add(c)
    db.commit()
    return p, c


def _image(db, cid, path, data=None, capture_id=None):
    from app.models.record import Record, RecordImage

    if data is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    rec = Record(title="r", collection_id=cid, status="captured", capture_mode="single")
    db.add(rec)
    db.commit()
    img = RecordImage(record_id=rec.id, filename=path.name, file_path=str(path), format=path.suffix.lstrip(".").lower(),
                      role="single", capture_id=capture_id)
    db.add(img)
    db.commit()
    return img


def _manifest(project_dir: Path, capture_id: str, rel: str, data: bytes) -> None:
    import hashlib

    (project_dir / "metadata").mkdir(parents=True, exist_ok=True)
    with open(project_dir / "metadata" / "manifest.jsonl", "a") as f:
        f.write(json.dumps({"capture_id": capture_id,
                            "files": [{"relative_path": rel, "sha256": hashlib.sha256(data).hexdigest()}]}) + "\n")


@pytest.mark.unit
def test_a_row_whose_file_was_already_lost_is_not_pointed_at_another_file_of_the_same_name(
        db_session, capture_settings, data_dir, override_projects_root):
    """No checksum is held for the row (no capture id), so a file of its name is not taken for it."""
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    root = override_projects_root
    _p, c = _project_and_collection(db_session)
    lost = _image(db_session, c.id, root / "proj" / "low" / "images" / "main" / "fondo_0001.jpg")  # no file
    other = root / "proj" / f"collection-{c.id}" / "images" / "main" / "fondo_0001.jpg"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"a different page, left by hand or by another collection")
    stored = lost.file_path

    report = migrate_volume_layout(db_session)

    db_session.expire_all()
    assert db_session.get(RecordImage, lost.id).file_path == stored
    assert report["adopted"] == []
    assert {"image_id": lost.id, "path": str(Path(stored).resolve()), "reason": "missing"} in report["skipped"]
    assert str(other.resolve()) in report["left_unreferenced"]
    assert other.read_bytes() == b"a different page, left by hand or by another collection"


@pytest.mark.unit
def test_a_file_whose_bytes_differ_from_the_capture_checksum_is_not_adopted(
        db_session, capture_settings, data_dir, override_projects_root):
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    root = override_projects_root
    _p, c = _project_and_collection(db_session)
    lost = _image(db_session, c.id, root / "proj" / "low" / "images" / "main" / "fondo_0001.jpg", capture_id="cap-1")
    _manifest(root / "proj", "cap-1", "low/images/main/fondo_0001.jpg", b"the captured page")
    other = root / "proj" / f"collection-{c.id}" / "images" / "main" / "fondo_0001.jpg"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"another page")

    report = migrate_volume_layout(db_session)

    db_session.expire_all()
    assert db_session.get(RecordImage, lost.id).file_path == lost.file_path
    assert report["adopted"] == []
    assert str(other.resolve()) in report["left_unreferenced"]


@pytest.mark.unit
def test_a_file_whose_bytes_match_the_capture_checksum_is_adopted(
        db_session, capture_settings, data_dir, override_projects_root):
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    root = override_projects_root
    _p, c = _project_and_collection(db_session)
    lost = _image(db_session, c.id, root / "proj" / "low" / "images" / "main" / "fondo_0001.jpg", capture_id="cap-1")
    _manifest(root / "proj", "cap-1", "low/images/main/fondo_0001.jpg", b"the captured page")
    moved = root / "proj" / f"collection-{c.id}" / "images" / "main" / "fondo_0001.jpg"
    moved.parent.mkdir(parents=True)
    moved.write_bytes(b"the captured page")

    report = migrate_volume_layout(db_session)

    db_session.expire_all()
    assert Path(db_session.get(RecordImage, lost.id).file_path) == moved.resolve()
    assert [a["image_id"] for a in report["adopted"]] == [lost.id]
    assert str(moved.resolve()) not in report["left_unreferenced"]


@pytest.mark.unit
def test_a_renamed_master_is_adopted_by_the_checksum_its_relocation_record_carries(
        db_session, capture_settings, data_dir, override_projects_root):
    """The migration gave the master `<stem>_img<id>` and wrote a relocation record; then the database was restored."""
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    root = override_projects_root
    _p, c = _project_and_collection(db_session)
    src = root / "proj" / "low" / "images" / "main" / "fondo_0001.jpg"
    img = _image(db_session, c.id, src, b"the captured page", capture_id="cap-1")
    _manifest(root / "proj", "cap-1", "low/images/main/fondo_0001.jpg", b"the captured page")
    target = root / "proj" / f"collection-{c.id}" / "images" / "main"
    target.mkdir(parents=True)
    (target / "fondo_0001.jpg").write_bytes(b"unrelated")
    before = (img.file_path, img.filename)
    migrate_volume_layout(db_session)
    db_session.expire_all()
    row = db_session.get(RecordImage, img.id)
    renamed = Path(row.file_path)
    assert renamed.name == f"fondo_0001_img{img.id}.jpg"
    row.file_path, row.filename = before
    db_session.commit()

    report = migrate_volume_layout(db_session)

    db_session.expire_all()
    assert Path(db_session.get(RecordImage, img.id).file_path) == renamed
    assert [a["image_id"] for a in report["adopted"]] == [img.id]
    assert (target / "fondo_0001.jpg").read_bytes() == b"unrelated"


# ---------------------------------------------------------------------------
# Fixity after a rename made after the migration
# ---------------------------------------------------------------------------

def _manifest_lines(project_dir: Path) -> list:
    return [json.loads(l) for l in (project_dir / "metadata" / "manifest.jsonl").read_text().splitlines() if l.strip()]


def _relocations(project_dir: Path) -> list:
    return [l for l in _manifest_lines(project_dir) if l.get("record_type") == "relocation"]


def _captured_i2(db_session, legacy):
    """IMG_0002 with a capture record in the project manifest, then migrated into its id-based directory."""
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    img = db_session.get(RecordImage, legacy["i2"].id)
    img.capture_id = "cap-0002"
    db_session.commit()
    _manifest_dir = legacy["root"] / "proj"
    (_manifest_dir / "metadata" / "manifest.jsonl").write_text("")
    _manifest(_manifest_dir, "cap-0002", "fondo_a/images/main/IMG_0002.jpg", Path(img.file_path).read_bytes())
    migrate_volume_layout(db_session)
    db_session.expire_all()


@pytest.mark.unit
def test_fixity_follows_a_master_renamed_by_a_renumber_after_the_migration(db_session, legacy):
    from app.core.integrity import verify_images_against_manifest
    from app.core.storage_ops import renumber_collection_images
    from app.models.record import RecordImage

    _captured_i2(db_session, legacy)
    old = Path(db_session.get(RecordImage, legacy["i2"].id).file_path)
    renumber_collection_images(db_session, legacy["a2"])
    db_session.commit()
    db_session.expire_all()
    img = db_session.get(RecordImage, legacy["i2"].id)
    new = Path(img.file_path)
    assert new.name != "IMG_0002.jpg"

    proj = legacy["root"] / "proj"
    assert [(r["capture_id"], r["from"], r["to"]) for r in _relocations(proj)] == [
        ("cap-0002", os.path.relpath(old, proj.resolve()), os.path.relpath(new, proj.resolve()))]
    assert verify_images_against_manifest([img]) == []
    new.write_bytes(b"bit rot")
    assert [m["record_image_id"] for m in verify_images_against_manifest([img])] == [img.id]


@pytest.mark.unit
def test_a_renumber_whose_commit_is_rolled_back_writes_no_relocation_record(db_session, legacy):
    from app.core.storage_ops import renumber_collection_images, restore_original_names

    _captured_i2(db_session, legacy)
    applied: list = []
    renumber_collection_images(db_session, legacy["a2"], applied)
    db_session.rollback()
    assert restore_original_names(applied) == []
    db_session.commit()  # a later commit on the same session carries nothing from the rolled-back renumber

    assert _relocations(legacy["root"] / "proj") == []


@pytest.mark.unit
def test_a_renumber_unwound_on_disk_writes_no_relocation_record(db_session, legacy, monkeypatch):
    import app.core.storage_ops as so

    _captured_i2(db_session, legacy)
    real = os.replace
    calls = {"n": 0}

    def fail_in_phase_two(src, dst):
        if not Path(dst).name.startswith(".renumber_"):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(5, "Input/output error")
        return real(src, dst)

    monkeypatch.setattr(so.os, "replace", fail_in_phase_two)
    with pytest.raises(OSError):
        so.renumber_collection_images(db_session, legacy["a2"])
    monkeypatch.setattr(so.os, "replace", real)
    db_session.commit()

    assert _relocations(legacy["root"] / "proj") == []


def _clash_setup(db_session, capture_settings_root):
    """An image with a capture record in collection A, to be moved into collection B, where its name is taken."""
    root = capture_settings_root
    _p, a = _project_and_collection(db_session, cname="A")
    from app.models.collection import Collection

    b = Collection(name="B", project_id=_p.id)
    db_session.add(b)
    db_session.commit()
    src = root / "proj" / f"collection-{a.id}" / "images" / "main" / "folio_0001.jpg"
    img = _image(db_session, a.id, src, b"the captured page", capture_id="cap-1")
    _manifest(root / "proj", "cap-1", f"collection-{a.id}/images/main/folio_0001.jpg", b"the captured page")
    target = root / "proj" / f"collection-{b.id}" / "images" / "main"
    target.mkdir(parents=True)
    (target / "folio_0001.jpg").write_bytes(b"another page")
    return img, src, target


@pytest.mark.unit
def test_fixity_follows_a_master_renamed_on_a_clash_when_it_is_moved(
        db_session, capture_settings, data_dir, override_projects_root):
    from app.core.integrity import verify_images_against_manifest
    from app.core.storage_ops import relocate_images_and_commit
    from app.models.record import RecordImage

    img, src, target = _clash_setup(db_session, override_projects_root)

    relocate_images_and_commit(db_session, [(img, target)], [], "test move")

    db_session.expire_all()
    img = db_session.get(RecordImage, img.id)
    new = Path(img.file_path)
    assert new.parent == target and new.name != "folio_0001.jpg"
    assert (target / "folio_0001.jpg").read_bytes() == b"another page"
    proj = (override_projects_root / "proj").resolve()
    assert [(r["capture_id"], r["from"], r["to"]) for r in _relocations(proj)] == [
        ("cap-1", os.path.relpath(src.resolve(), proj), os.path.relpath(new.resolve(), proj))]
    assert verify_images_against_manifest([img]) == []
    new.write_bytes(b"bit rot")
    assert [m["record_image_id"] for m in verify_images_against_manifest([img])] == [img.id]


@pytest.mark.unit
def test_a_clash_rename_whose_commit_fails_and_is_unwound_writes_no_relocation_record(
        db_session, capture_settings, data_dir, override_projects_root, monkeypatch):
    from fastapi import HTTPException
    from app.core.storage_ops import relocate_images_and_commit

    img, src, target = _clash_setup(db_session, override_projects_root)

    def failing_commit():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db_session, "commit", failing_commit)
    with pytest.raises(HTTPException):
        relocate_images_and_commit(db_session, [(img, target)], [], "test move")
    monkeypatch.undo()

    assert src.read_bytes() == b"the captured page"
    assert _relocations(override_projects_root / "proj") == []


@pytest.mark.unit
def test_a_clash_rename_whose_commit_raised_but_landed_writes_its_relocation_record(
        db_session, capture_settings, data_dir, override_projects_root, monkeypatch):
    from app.core.storage_ops import relocate_images_and_commit

    img, src, target = _clash_setup(db_session, override_projects_root)
    real_commit = db_session.commit

    def commit_then_raise():
        real_commit()
        raise RuntimeError("connection lost after COMMIT")

    monkeypatch.setattr(db_session, "commit", commit_then_raise)
    relocate_images_and_commit(db_session, [(img, target)], [], "test move")
    monkeypatch.undo()

    assert not src.exists()
    assert [r["capture_id"] for r in _relocations(override_projects_root / "proj")] == ["cap-1"]


@pytest.mark.unit
def test_relocation_records_of_later_renames_stay_out_of_the_bag_manifest(db_session, legacy):
    from app.core.export_service import _filtered_manifest_blob
    from app.core.storage_ops import renumber_collection_images
    from app.models.collection import Collection
    from app.models.record import Record

    _captured_i2(db_session, legacy)
    renumber_collection_images(db_session, legacy["a2"])
    db_session.commit()
    assert _relocations(legacy["root"] / "proj")

    collection = db_session.get(Collection, legacy["a2"].id)
    records = db_session.query(Record).filter(Record.collection_id == collection.id).all()
    blob = _filtered_manifest_blob(db_session, collection, records)

    kept = [json.loads(l) for l in blob.decode().splitlines() if l.strip()]
    assert [k.get("capture_id") for k in kept] == ["cap-0002"]
    assert all(k.get("record_type") != "relocation" for k in kept)
