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
def test_a_pending_sidecar_whose_master_row_is_gone_is_kept_and_logged(db_session, legacy, monkeypatch, caplog):
    import app.core.volume_layout as vl
    from app.models.record import RecordImage

    _fail_sidecars_once(vl, monkeypatch, db_session)
    db_session.delete(db_session.get(RecordImage, legacy["i1"].id))
    db_session.commit()
    old = (legacy["shared"] / "IMG_0001_preview.jpg").resolve()

    with caplog.at_level("WARNING"):
        report = vl.migrate_volume_layout(db_session)

    assert old.read_bytes() == b"preview of IMG_0001"
    assert [s["from"] for s in report["sidecars_pending"] if s["image_id"] == legacy["i1"].id] == [str(old)]
    assert str(old) in caplog.text


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
