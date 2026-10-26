"""The volume layout migration: version-1 journals, pending sidecars, adoption, fixity after renames.

Uses the fixtures of test_volume_layout_by_stem: the name-based `legacy`
layout under the per-test projects root, and a writable data directory.

Version: 0.1.0-beta
"""

import json
import os
from pathlib import Path

import pytest

from tests.unit.test_volume_layout_by_stem import STEMS, capture_settings, data_dir, legacy  # noqa: F401


# ---------------------------------------------------------------------------
# A version-1 journal
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_v1_journal_left_by_the_earlier_commits_is_completed(db_session, legacy, data_dir):
    """Version-1 entries have no "adopt" and no "capture_id". The crash came after the first move."""
    from app.core.volume_layout import JOURNAL_NAME, migrate_volume_layout_at_startup
    from app.models.record import RecordImage

    i2 = legacy["i2"]
    src = Path(i2.file_path).resolve()
    dst = (legacy["root"] / STEMS["a2"] / "captures" / src.name).resolve()
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
    """The backend serves while a sidecar is pending, and a file-renaming renumber renamed its master."""
    import app.core.volume_layout as vl
    from app.core.paths import preview_sidecar_of
    from app.models.record import RecordImage
    from tests.unit.earlier_renumber import rename_as_an_earlier_renumber

    _fail_sidecars_once(vl, monkeypatch, db_session)
    rename_as_an_earlier_renumber(db_session, legacy["a1"])

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
    from app.models.record import RecordImage
    from tests.unit.earlier_renumber import rename_as_an_earlier_renumber

    _fail_sidecars_once(vl, monkeypatch, db_session)
    rename_as_an_earlier_renumber(db_session, legacy["a1"])
    db_session.expire_all()
    master = Path(db_session.get(RecordImage, legacy["i1"].id).file_path)
    taken = preview_sidecar_of(master)
    taken.parent.mkdir(parents=True, exist_ok=True)
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
    c = Collection(name="Restored", project_id=legacy["proj"].id, filename_stem="xx-test-restored")
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

def _project_and_collection(db, name="Proj", cname="Low", stem="xx-test-low"):
    from app.models.collection import Collection
    from app.models.project import Project

    p = Project(name=name)
    db.add(p)
    db.commit()
    c = Collection(name=cname, project_id=p.id, filename_stem=stem)
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
    other = root / "xx-test-low" / "captures" / "fondo_0001.jpg"
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
    other = root / "xx-test-low" / "captures" / "fondo_0001.jpg"
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
    moved = root / "xx-test-low" / "captures" / "fondo_0001.jpg"
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
    target = root / "xx-test-low" / "captures"
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

def _manifest_lines(top: Path) -> list:
    """The lines of the manifest under `top` (a project's directory or a volume's `<stem>/`); none if it has none."""
    path = top / "metadata" / "manifest.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _relocations(top: Path) -> list:
    return [l for l in _manifest_lines(top) if l.get("record_type") == "relocation"]


def _a2_volume(legacy) -> Path:
    """Fondo A (the second)'s `<stem>/`, whose manifest takes the relocation records of its later renames."""
    return legacy["root"] / STEMS["a2"]


def _captured_i2(db_session, legacy):
    """IMG_0002 with a capture record in the project manifest, then migrated into its volume's stem layout."""
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
def test_fixity_follows_a_master_renamed_by_a_file_renaming_renumber_after_the_migration(db_session, legacy):
    """The relocation record such a renumber wrote stays in the volume's manifest, and the index reads it."""
    from app.core.integrity import verify_images_against_manifest
    from app.models.record import RecordImage
    from tests.unit.earlier_renumber import rename_as_an_earlier_renumber

    _captured_i2(db_session, legacy)
    old = Path(db_session.get(RecordImage, legacy["i2"].id).file_path)
    rename_as_an_earlier_renumber(db_session, legacy["a2"])
    db_session.expire_all()
    img = db_session.get(RecordImage, legacy["i2"].id)
    new = Path(img.file_path)
    assert new.name != "IMG_0002.jpg"

    vol = _a2_volume(legacy)
    assert [(r["capture_id"], r["from"], r["to"]) for r in _relocations(vol)] == [
        ("cap-0002", os.path.relpath(old, vol.resolve()), os.path.relpath(new, vol.resolve()))]
    assert _relocations(legacy["root"] / "proj") == []
    assert verify_images_against_manifest([img]) == []
    new.write_bytes(b"bit rot")
    assert [m["record_image_id"] for m in verify_images_against_manifest([img])] == [img.id]


def _clash_setup(db_session, capture_settings_root):
    """An image with a capture record in volume A, to be moved into volume B, where its name is taken.

    Both have stems, so the capture record is in A's own manifest and the
    relocation record goes in B's.
    """
    root = capture_settings_root
    _p, a = _project_and_collection(db_session, cname="A", stem="xx-test-a")
    from app.models.collection import Collection

    b = Collection(name="B", project_id=_p.id, filename_stem="xx-test-b")
    db_session.add(b)
    db_session.commit()
    src = root / "xx-test-a" / "captures" / "folio_0001.jpg"
    img = _image(db_session, a.id, src, b"the captured page", capture_id="cap-1")
    _manifest(root / "xx-test-a", "cap-1", "captures/folio_0001.jpg", b"the captured page")
    target = root / "xx-test-b" / "captures"
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
    vol = (override_projects_root / "xx-test-b").resolve()
    assert [(r["capture_id"], r["from"], r["to"]) for r in _relocations(vol)] == [
        ("cap-1", os.path.relpath(src.resolve(), vol), os.path.relpath(new.resolve(), vol))]
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
    assert _relocations(override_projects_root / "xx-test-b") == []


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
    assert [r["capture_id"] for r in _relocations(override_projects_root / "xx-test-b")] == ["cap-1"]


@pytest.mark.unit
def test_relocation_records_of_later_renames_stay_out_of_the_bag_manifest(db_session, legacy):
    from app.core.export_service import _filtered_manifest_blob
    from app.models.collection import Collection
    from app.models.record import Record
    from tests.unit.earlier_renumber import rename_as_an_earlier_renumber

    _captured_i2(db_session, legacy)
    rename_as_an_earlier_renumber(db_session, legacy["a2"])
    assert _relocations(_a2_volume(legacy))

    collection = db_session.get(Collection, legacy["a2"].id)
    records = db_session.query(Record).filter(Record.collection_id == collection.id).all()
    blob = _filtered_manifest_blob(db_session, collection, records)

    kept = [json.loads(l) for l in blob.decode().splitlines() if l.strip()]
    assert [k.get("capture_id") for k in kept] == ["cap-0002"]
    assert all(k.get("record_type") != "relocation" for k in kept)


# ---------------------------------------------------------------------------
# Relocations applied in the order they happened, across the manifests of several volumes
# ---------------------------------------------------------------------------

def _glob_in_order(monkeypatch, reverse: bool):
    """Make the manifest glob return the manifests sorted by path, forwards or backwards."""
    import pathlib

    real = pathlib.Path.glob

    def ordered(self, pattern, *a, **kw):
        found = list(real(self, pattern, *a, **kw))
        return iter(sorted(found, reverse=reverse)) if pattern.endswith("manifest.jsonl") else iter(found)

    monkeypatch.setattr(pathlib.Path, "glob", ordered)


@pytest.mark.unit
@pytest.mark.parametrize("reverse", [False, True])
def test_a_relocation_chain_split_across_volume_manifests_resolves_to_the_current_path(
        db_session, capture_settings, data_dir, override_projects_root, monkeypatch, reverse):
    """Captured in volume A, moved into volume B (another project) and renamed there by a file-renaming renumber, then moved to C on a clash."""
    from app.core.integrity import verify_images_against_manifest
    from app.core.storage_ops import relocate_images_and_commit
    from tests.unit.earlier_renumber import rename_as_an_earlier_renumber
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    root = override_projects_root
    pa, a = _project_and_collection(db_session, name="PA", cname="A", stem="xx-test-pa-a")
    pb = Project(name="PB")
    db_session.add(pb)
    db_session.commit()
    b = Collection(name="B", project_id=pb.id, filename_stem="xx-test-pb-b")
    c = Collection(name="C", project_id=pa.id, filename_stem="xx-test-pa-c")
    db_session.add_all([b, c])
    db_session.commit()
    src = root / "xx-test-pa-a" / "captures" / "folio_0001.jpg"
    img = _image(db_session, a.id, src, b"the captured page", capture_id="cap-1")
    _manifest(root / "xx-test-pa-a", "cap-1", "captures/folio_0001.jpg", b"the captured page")

    rec = db_session.get(Record, img.record_id)
    rec.collection_id = b.id
    relocate_images_and_commit(db_session, [(img, root / "xx-test-pb-b" / "captures")], [], "move")
    rename_as_an_earlier_renumber(db_session, db_session.get(Collection, b.id))
    c_dir = root / "xx-test-pa-c" / "captures"
    c_dir.mkdir(parents=True)
    (c_dir / "b_0001.jpg").write_bytes(b"another page")
    db_session.expire_all()
    img = db_session.get(RecordImage, img.id)
    db_session.get(Record, img.record_id).collection_id = c.id
    relocate_images_and_commit(db_session, [(img, c_dir)], [], "move back")
    db_session.expire_all()
    img = db_session.get(RecordImage, img.id)
    current = Path(img.file_path)
    assert current.parent == c_dir.resolve() or current.parent == c_dir
    assert current.name.startswith("b_0001_")
    assert len(_relocations(root / "xx-test-pb-b")) == 1 and len(_relocations(root / "xx-test-pa-c")) == 1
    assert _relocations(root / "xx-test-pa-a") == []

    _glob_in_order(monkeypatch, reverse)
    assert verify_images_against_manifest([img]) == []
    current.write_bytes(b"bit rot")
    assert [m["record_image_id"] for m in verify_images_against_manifest([img])] == [img.id]


# ---------------------------------------------------------------------------
# Relocation records that name their image; chains only for records that do not
# ---------------------------------------------------------------------------

def _chain_setup(legacy):
    """A captured file at C, its third name; its capture record names A. Returns (A, B, C) relative to proj, and C."""
    import hashlib

    root = legacy["root"]
    a = "fondo_a/images/main/CHAIN.jpg"
    b = "tomo_9/images/main/CHAIN_img9.jpg"
    c = "tomo_9/images/main/fondo_a_0001.jpg"
    f = root / "proj" / c
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"captured bytes")
    sha = hashlib.sha256(b"captured bytes").hexdigest()
    with open(root / "proj" / "metadata" / "manifest.jsonl", "a") as m:
        m.write(json.dumps({"capture_id": "cap-chain", "files": [{"relative_path": a, "sha256": sha}]}) + "\n")
    f.write_bytes(b"bit rot")
    return a, b, c, f, sha


def _chain_checked(f: Path) -> bool:
    from app.core import config
    from app.core.integrity import _build_manifest_index

    by_path, _ = _build_manifest_index(config.settings.projects_dir.resolve())
    return str(f.resolve()) in by_path.get("cap-chain", {})


def _reloc(frm, to, **extra) -> str:
    return json.dumps({"record_type": "relocation", "capture_id": "cap-chain", "from": frm, "to": to, **extra}) + "\n"


@pytest.mark.unit
def test_a_chain_survives_the_clock_going_back_between_its_two_renames(db_session, legacy):
    """A unit with no real-time clock stamps the second rename earlier than the first; line order decides."""
    a, b, c, f, _ = _chain_setup(legacy)
    with open(legacy["root"] / "proj" / "metadata" / "manifest.jsonl", "a") as m:
        m.write(_reloc(a, b, relocated_at="2026-10-20T10:00:00+00:00"))
        m.write(_reloc(b, c, relocated_at="2026-01-01T00:00:05+00:00"))
    assert _chain_checked(f)


@pytest.mark.unit
@pytest.mark.parametrize("reverse", [False, True])
def test_a_chain_across_two_manifests_with_equal_timestamps(db_session, legacy, monkeypatch, reverse):
    """The second rename is recorded in another manifest; whichever manifest is read first, the chain completes."""
    root = legacy["root"]
    a, b, c, f, _ = _chain_setup(legacy)
    ts = "2026-10-20T10:00:00+00:00"
    with open(root / "proj" / "metadata" / "manifest.jsonl", "a") as m:
        m.write(_reloc(a, b, relocated_at=ts))
    other = root / "aaa" / "metadata" / "manifest.jsonl"
    other.parent.mkdir(parents=True)
    other.write_text(_reloc("../proj/" + b, "../proj/" + c, relocated_at=ts))
    _glob_in_order(monkeypatch, reverse)
    assert _chain_checked(f)


@pytest.mark.unit
def test_a_hand_written_record_for_the_later_rename_completes_the_chain(db_session, legacy):
    """A record written by hand (no timestamp) that names the image and its checksum resolves it on its own."""
    from app.core.integrity import verify_images_against_manifest
    from app.models.record import Record, RecordImage

    a, b, c, f, sha = _chain_setup(legacy)
    rec = Record(title="r", collection_id=legacy["a2"].id, status="captured", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    img = RecordImage(record_id=rec.id, filename=f.name, file_path=str(f), format="jpg", role="single",
                      capture_id="cap-chain")
    db_session.add(img)
    db_session.commit()
    with open(legacy["root"] / "proj" / "metadata" / "manifest.jsonl", "a") as m:
        m.write(_reloc(a, b, relocated_at="2026-10-20T10:00:00+00:00"))
        m.write(_reloc(b, c, image_id=img.id, sha256=sha))

    assert [m["record_image_id"] for m in verify_images_against_manifest([img])] == [img.id]
    f.write_bytes(b"captured bytes")
    assert verify_images_against_manifest([img]) == []


@pytest.mark.unit
def test_two_file_renaming_renumbers_that_reuse_names_still_resolve_each_image(
        db_session, capture_settings, data_dir, override_projects_root):
    """Two files of one capture (a dual pair); the second renumber swapped their names.

    A chain by path cannot express a swap: whichever record is applied first,
    the second reads the checksum the first just wrote.
    """
    from app.core.integrity import run_integrity_check, verify_images_against_manifest
    from app.models.collection import Collection
    from app.models.record import Record, RecordImage
    from tests.unit.earlier_renumber import rename_as_an_earlier_renumber

    root = override_projects_root
    _p, c = _project_and_collection(db_session, cname="Tomo", stem="xx-test-tomo")
    d = root / "xx-test-tomo" / "captures"
    left = _image(db_session, c.id, d / "L.jpg", b"left page", capture_id="cap-pair")
    right = _image(db_session, c.id, d / "R.jpg", b"right page", capture_id="cap-pair")
    for f, data in (("L.jpg", b"left page"), ("R.jpg", b"right page")):
        _manifest(root / "xx-test-tomo", "cap-pair", f"captures/{f}", data)
    rl, rr = db_session.get(Record, left.record_id), db_session.get(Record, right.record_id)
    rl.sequence, rr.sequence = 1, 2
    db_session.commit()

    rename_as_an_earlier_renumber(db_session, db_session.get(Collection, c.id))
    first = {i.id: Path(db_session.get(RecordImage, i.id).file_path).name for i in (left, right)}
    rl, rr = db_session.get(Record, left.record_id), db_session.get(Record, right.record_id)
    rl.sequence, rr.sequence = 2, 1
    db_session.commit()
    rename_as_an_earlier_renumber(db_session, db_session.get(Collection, c.id))
    db_session.expire_all()
    imgs = [db_session.get(RecordImage, i.id) for i in (left, right)]
    second = {i.id: Path(i.file_path).name for i in imgs}
    assert second == {left.id: first[right.id], right.id: first[left.id]}  # the names were swapped

    assert verify_images_against_manifest(imgs) == []
    result = run_integrity_check(db_session, verify_hashes=True)
    assert result["manifest_mismatches"] == []
    assert not {i.id for i in imgs} & {m["record_image_id"] for m in result["manifest_missing_entry"]}
    for img in imgs:
        Path(img.file_path).write_bytes(b"bit rot " + str(img.id).encode())
    assert sorted(m["record_image_id"] for m in verify_images_against_manifest(imgs)) == sorted(i.id for i in imgs)


@pytest.mark.unit
def test_every_written_relocation_record_names_its_image_and_checksum(db_session, legacy, renamed_capture_i2):
    """The migration's clash rename writes image_id and the capture sha256."""
    import hashlib
    from app.core.volume_layout import migrate_volume_layout

    sha = renamed_capture_i2
    migrate_volume_layout(db_session)

    records = [r for r in _relocations(_a2_volume(legacy)) if r["capture_id"] == "cap-0002"]
    assert len(records) == 1
    assert all(r["image_id"] == legacy["i2"].id and r["sha256"] == sha for r in records)
    assert hashlib.sha256(b"bytes of IMG_0002.jpg").hexdigest() == sha


@pytest.fixture
def renamed_capture_i2(db_session, legacy):
    """IMG_0002 with a capture record, and its name taken in its target so the migration renames it."""
    import hashlib
    from app.models.record import RecordImage

    img = db_session.get(RecordImage, legacy["i2"].id)
    img.capture_id = "cap-0002"
    db_session.commit()
    proj = legacy["root"] / "proj"
    (proj / "metadata" / "manifest.jsonl").write_text("")
    _manifest(proj, "cap-0002", "fondo_a/images/main/IMG_0002.jpg", b"bytes of IMG_0002.jpg")
    target = _a2_volume(legacy) / "captures"
    target.mkdir(parents=True)
    (target / "IMG_0002.jpg").write_bytes(b"unrelated")
    return hashlib.sha256(b"bytes of IMG_0002.jpg").hexdigest()


# ---------------------------------------------------------------------------
# The export fixity gate refuses a captured master it cannot check
# ---------------------------------------------------------------------------

def _renumber_with_failed_record(db, legacy, monkeypatch):
    """A file-renaming renumber renamed captured IMG_0002, its relocation record was not written, and the file rots."""
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage
    from tests.unit.earlier_renumber import rename_as_an_earlier_renumber

    migrate_volume_layout(db)
    img = db.get(RecordImage, legacy["i2"].id)
    img.capture_id = "cap-0002"
    db.commit()
    (legacy["root"] / "proj" / "metadata" / "manifest.jsonl").write_text("")
    # A capture made after the migration: its record is in the volume's own manifest.
    vol = _a2_volume(legacy)
    _manifest(vol, "cap-0002", os.path.relpath(Path(img.file_path), vol.resolve()), Path(img.file_path).read_bytes())

    rename_as_an_earlier_renumber(db, legacy["a2"], write_records=False)
    db.expire_all()
    img = db.get(RecordImage, legacy["i2"].id)
    Path(img.file_path).write_bytes(b"bit rot")
    return img


@pytest.mark.unit
def test_the_export_fixity_gate_does_not_pass_a_renamed_master_whose_record_was_not_written(db_session, legacy, monkeypatch):
    from app.core.integrity import verify_images_against_manifest

    img = _renumber_with_failed_record(db_session, legacy, monkeypatch)

    found = verify_images_against_manifest([img])
    assert [(m["record_image_id"], m["reason"]) for m in found] == [(img.id, "unresolved")]
    assert found[0]["file_path"] == img.file_path


@pytest.mark.unit
def test_the_export_refuses_a_captured_master_with_no_resolvable_checksum_naming_it(db_session, legacy, monkeypatch):
    from app.core.export_jobs import ExportError
    from app.core.export_service import _fixity_gate

    img = _renumber_with_failed_record(db_session, legacy, monkeypatch)

    with pytest.raises(ExportError) as err:
        _fixity_gate([img])
    assert err.value.detail["reason"] == "checksum_unresolved"
    assert err.value.detail["unresolved_image_ids"] == [img.id]
    assert err.value.detail["unresolved"] == [{"record_image_id": img.id, "capture_id": "cap-0002",
                                               "file_path": img.file_path}]
    assert str(img.id) in str(err.value)


@pytest.mark.unit
def test_the_integrity_scan_reports_a_renamed_master_whose_record_was_not_written(db_session, legacy, monkeypatch):
    from app.core.integrity import run_integrity_check

    img = _renumber_with_failed_record(db_session, legacy, monkeypatch)
    result = run_integrity_check(db_session, verify_hashes=True)
    assert img.id in [m["record_image_id"] for m in result["manifest_missing_entry"]]


@pytest.mark.unit
def test_images_with_no_capture_record_still_pass_the_gate_unchecked(db_session, capture_settings, data_dir,
                                                                    override_projects_root):
    """An upload has no capture id unless the client sent one; one naming a capture no manifest holds is not checked either."""
    from app.core import config
    from app.core.export_service import _fixity_gate
    from app.core.integrity import verify_images_against_manifest

    root = override_projects_root
    _p, c = _project_and_collection(db_session)
    uploads = config.settings.data_dir / "uploads"
    plain = _image(db_session, c.id, uploads / "upload_1.jpg", b"uploaded")
    claimed = _image(db_session, c.id, uploads / "upload_2.jpg", b"uploaded", capture_id="cap-nowhere")
    _manifest(root / "proj", "cap-other", "x/a.jpg", b"page")

    assert verify_images_against_manifest([plain, claimed]) == []
    _fixity_gate([plain, claimed])


# ---------------------------------------------------------------------------
# A commit whose outcome is unknown still records the renames
# ---------------------------------------------------------------------------

class _Interrupt(BaseException):
    pass


def _unknown_outcome_move(db_session, root, monkeypatch, lands: bool, interrupt: bool = False):
    """Move a captured master onto a clashing name, with a commit whose outcome cannot be determined."""
    import app.core.storage_ops as so

    img, src, target = _clash_setup(db_session, root)
    real_commit = db_session.commit

    def commit():
        if lands:
            real_commit()
        raise (_Interrupt() if interrupt else RuntimeError("connection lost"))

    monkeypatch.setattr(db_session, "commit", commit)
    monkeypatch.setattr(so, "_relocation_landed", lambda db, new_paths: None)
    with pytest.raises(_Interrupt if interrupt else so.HTTPException):
        so.relocate_images_and_commit(db_session, [(img, target)], [], "test move")
    monkeypatch.setattr(db_session, "commit", real_commit)
    records = _relocations(root / "xx-test-b")
    return img, src, records


def _scan_resolves(db_session, img_id) -> None:
    from app.core.integrity import run_integrity_check
    from app.models.record import RecordImage

    db_session.expire_all()
    img = db_session.get(RecordImage, img_id)
    result = run_integrity_check(db_session, verify_hashes=True)
    assert img_id not in [m["record_image_id"] for m in result["manifest_missing_entry"]]
    assert result["manifest_mismatches"] == []
    Path(img.file_path).write_bytes(b"bit rot")
    result = run_integrity_check(db_session, verify_hashes=True)
    assert [m["record_image_id"] for m in result["manifest_mismatches"]] == [img_id]


@pytest.mark.unit
def test_an_unknown_outcome_commit_that_landed_writes_the_record_and_the_scan_resolves_the_file(
        db_session, capture_settings, data_dir, override_projects_root, monkeypatch):
    img, src, records = _unknown_outcome_move(db_session, override_projects_root, monkeypatch, lands=True)

    assert [(r["image_id"], r["capture_id"]) for r in records] == [(img.id, "cap-1")]
    _scan_resolves(db_session, img.id)


@pytest.mark.unit
@pytest.mark.parametrize("interrupt", [False, True])
def test_an_unknown_outcome_commit_that_did_not_land_writes_the_record_and_the_file_resolves_once_the_row_is_fixed(
        db_session, capture_settings, data_dir, override_projects_root, monkeypatch, interrupt):
    """The row still names the old path; recovery by hand points it at the logged new path."""
    from app.models.record import RecordImage

    img, src, records = _unknown_outcome_move(db_session, override_projects_root, monkeypatch, lands=False,
                                              interrupt=interrupt)
    assert [(r["image_id"], r["capture_id"]) for r in records] == [(img.id, "cap-1")]
    db_session.rollback()
    db_session.expire_all()
    row = db_session.get(RecordImage, img.id)
    assert Path(row.file_path) == src and not src.exists()
    new = (override_projects_root / "xx-test-b").resolve() / records[0]["to"]
    assert new.is_file()

    row.file_path = str(new)  # the recovery by hand the ERROR log asks for
    db_session.commit()
    _scan_resolves(db_session, img.id)
