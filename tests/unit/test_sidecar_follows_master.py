"""A RAW master's `_preview.jpg` sidecar follows it wherever it is moved or renamed.

NEH-267: `relocate_image` and `renumber_collection_images` carry
`<stem>_preview.jpg` to the name the master's final path derives, because the
preview endpoint and deletes look for the sidecar beside the master's current
path. NEH-264: the name rule is `preview_sidecar_of` in app/core/paths.py.
NEH-263: `delete_thumbnail` raises when the unlink fails. The integrity scan
counts a sidecar as known only while its RAW master is known and on disk.

Files are real, under the per-test projects root from `override_projects_root`.
"""

import pathlib
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# The helper (NEH-264)
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("name, sidecar", [
    ("IMG_0001.CR2", "IMG_0001_preview.jpg"),
    ("IMG_0001.cr2", "IMG_0001_preview.jpg"),
    ("folio.JPG", "folio_preview.jpg"),
    ("a.b.dng", "a.b_preview.jpg"),
])
def test_preview_sidecar_of_names_the_sidecar_beside_the_master(name, sidecar):
    from app.core.paths import preview_sidecar_of

    master = Path("/some/dir") / name
    assert preview_sidecar_of(master) == Path("/some/dir") / sidecar


@pytest.mark.unit
@pytest.mark.parametrize("name, raw", [
    ("x.CR2", True), ("x.cr2", True), ("x.NEF", True), ("x.dng", True), ("x.raw", True),
    ("x.jpg", False), ("x.JPEG", False), ("x.tif", False), ("x", False),
])
def test_is_raw_suffix_ignores_case(name, raw):
    from app.core.paths import is_raw_suffix

    assert is_raw_suffix(Path(name)) is raw


# ---------------------------------------------------------------------------
# relocate_image (NEH-267)
# ---------------------------------------------------------------------------

@pytest.fixture
def dirs(override_projects_root, db_session):
    # db_session imports every model, which RecordImage's relationships need to map.
    src = override_projects_root / "proj" / "images" / "a"
    dst = override_projects_root / "proj" / "images" / "b"
    src.mkdir(parents=True)
    return src, dst


def _row(master, thumb=None):
    from app.models.record import RecordImage

    return RecordImage(filename=master.name, file_path=str(master),
                       thumbnail_path=str(thumb) if thumb else None)


@pytest.mark.unit
def test_relocate_moves_a_raw_masters_sidecar(dirs):
    from app.core.storage_ops import relocate_image

    src_dir, dst_dir = dirs
    master = src_dir / "IMG_0001.CR2"
    master.write_bytes(b"raw")
    sidecar = src_dir / "IMG_0001_preview.jpg"
    sidecar.write_bytes(b"preview")
    img = _row(master)

    assert relocate_image(img, dst_dir) is True

    new_master = Path(img.file_path)
    assert new_master == (dst_dir / "IMG_0001.CR2").resolve()
    assert new_master.read_bytes() == b"raw"
    assert (dst_dir / "IMG_0001_preview.jpg").read_bytes() == b"preview"
    assert not master.exists() and not sidecar.exists()


@pytest.mark.unit
def test_relocate_names_the_sidecar_from_the_renamed_master_on_collision(dirs):
    from app.core.storage_ops import relocate_image

    src_dir, dst_dir = dirs
    dst_dir.mkdir(parents=True)
    master = src_dir / "IMG_0002.CR2"
    master.write_bytes(b"raw")
    (src_dir / "IMG_0002_preview.jpg").write_bytes(b"preview")
    occupant = dst_dir / "IMG_0002.CR2"
    occupant.write_bytes(b"someone else's raw")
    occupant_sidecar = dst_dir / "IMG_0002_preview.jpg"
    occupant_sidecar.write_bytes(b"someone else's preview")
    img = _row(master)

    assert relocate_image(img, dst_dir) is True

    new_master = Path(img.file_path)
    assert new_master.parent == dst_dir.resolve()
    assert new_master.name != "IMG_0002.CR2"
    assert new_master.name.startswith("IMG_0002_") and new_master.suffix == ".CR2"
    assert new_master.read_bytes() == b"raw"
    new_sidecar = new_master.with_name(new_master.stem + "_preview.jpg")
    assert new_sidecar.read_bytes() == b"preview"
    assert occupant.read_bytes() == b"someone else's raw"
    assert occupant_sidecar.read_bytes() == b"someone else's preview"


@pytest.mark.unit
def test_relocate_renames_when_only_the_sidecar_name_is_taken(dirs):
    """A stray `_preview.jpg` in the target is not overwritten by the one that moves."""
    from app.core.storage_ops import relocate_image

    src_dir, dst_dir = dirs
    dst_dir.mkdir(parents=True)
    master = src_dir / "IMG_0003.CR2"
    master.write_bytes(b"raw")
    (src_dir / "IMG_0003_preview.jpg").write_bytes(b"preview")
    stray = dst_dir / "IMG_0003_preview.jpg"
    stray.write_bytes(b"stray preview")
    img = _row(master)

    assert relocate_image(img, dst_dir) is True

    new_master = Path(img.file_path)
    assert new_master.name != "IMG_0003.CR2"
    assert new_master.with_name(new_master.stem + "_preview.jpg").read_bytes() == b"preview"
    assert stray.read_bytes() == b"stray preview"


@pytest.mark.unit
def test_relocate_leaves_a_jpeg_masters_preview_named_file_alone(dirs):
    """Only a RAW master carries a sidecar; `folio.jpg` and `folio.CR2` name the same one."""
    from app.core.storage_ops import relocate_image

    src_dir, dst_dir = dirs
    master = src_dir / "folio.jpg"
    master.write_bytes(b"jpeg")
    preview = src_dir / "folio_preview.jpg"
    preview.write_bytes(b"belongs to folio.CR2")
    img = _row(master)

    assert relocate_image(img, dst_dir) is True

    assert Path(img.file_path) == (dst_dir / "folio.jpg").resolve()
    assert preview.read_bytes() == b"belongs to folio.CR2"
    assert not (dst_dir / "folio_preview.jpg").exists()


@pytest.mark.unit
def test_relocate_of_a_raw_master_without_a_sidecar_moves_only_the_master(dirs):
    from app.core.storage_ops import relocate_image

    src_dir, dst_dir = dirs
    master = src_dir / "IMG_0004.CR2"
    master.write_bytes(b"raw")
    img = _row(master)

    assert relocate_image(img, dst_dir) is True
    assert sorted(p.name for p in dst_dir.iterdir()) == ["IMG_0004.CR2"]


@pytest.mark.unit
def test_relocate_keeps_the_master_moved_when_the_sidecar_cannot_follow(dirs, monkeypatch, caplog):
    import logging
    import shutil
    from app.core.storage_ops import relocate_image

    src_dir, dst_dir = dirs
    master = src_dir / "IMG_0005.CR2"
    master.write_bytes(b"raw")
    sidecar = src_dir / "IMG_0005_preview.jpg"
    sidecar.write_bytes(b"preview")
    img = _row(master)

    real_move = shutil.move

    def move(src, dst, *a, **kw):
        if src.endswith("_preview.jpg"):
            raise PermissionError(13, "Permission denied", src)
        return real_move(src, dst, *a, **kw)

    monkeypatch.setattr("app.core.storage_ops.shutil.move", move)
    with caplog.at_level(logging.WARNING, logger="app.core.storage_ops"):
        assert relocate_image(img, dst_dir) is True

    assert Path(img.file_path).read_bytes() == b"raw"
    assert sidecar.exists()
    assert any(str(sidecar) in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


# ---------------------------------------------------------------------------
# renumber_collection_images (NEH-267)
# ---------------------------------------------------------------------------

def _collection_with_images(db_session, image_dir, names):
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    project = Project(name="proj")
    db_session.add(project)
    db_session.commit()
    coll = Collection(name="Caja 1", project_id=project.id)
    db_session.add(coll)
    db_session.commit()
    for seq, name in enumerate(names, start=1):
        rec = Record(title=name, collection_id=coll.id,
                     sequence=seq, status="in_review", capture_mode="single")
        db_session.add(rec)
        db_session.commit()
        db_session.add(RecordImage(record_id=rec.id, filename=name,
                                   file_path=str(image_dir / name), role="single",
                                   format=Path(name).suffix.lstrip(".").lower()))
    db_session.commit()
    db_session.refresh(coll)
    return coll


@pytest.mark.unit
def test_renumber_renames_each_raw_masters_sidecar(db_session, override_projects_root):
    from app.core.storage_ops import renumber_collection_images
    from app.models.record import RecordImage

    image_dir = override_projects_root / "proj" / "images" / "caja_1"
    image_dir.mkdir(parents=True)
    # Display order puts IMG_0002 first, so it becomes caja_1_0001 and takes the
    # sidecar name IMG_0001's sidecar is about to vacate.
    (image_dir / "IMG_0002.CR2").write_bytes(b"raw two")
    (image_dir / "IMG_0002_preview.jpg").write_bytes(b"preview two")
    (image_dir / "IMG_0001.CR2").write_bytes(b"raw one")
    (image_dir / "IMG_0001_preview.jpg").write_bytes(b"preview one")
    (image_dir / "IMG_0003.jpg").write_bytes(b"jpeg three")
    coll = _collection_with_images(db_session, image_dir, ["IMG_0002.CR2", "IMG_0001.CR2", "IMG_0003.jpg"])

    result = renumber_collection_images(db_session, coll)
    db_session.commit()

    assert result["renumbered"] == 3
    on_disk = {p.name: p.read_bytes() for p in image_dir.iterdir()}
    assert on_disk == {
        "caja_1_0001.CR2": b"raw two",
        "caja_1_0001_preview.jpg": b"preview two",
        "caja_1_0002.CR2": b"raw one",
        "caja_1_0002_preview.jpg": b"preview one",
        "caja_1_0003.jpg": b"jpeg three",
    }
    names = sorted(i.filename for i in db_session.query(RecordImage).all())
    assert names == ["caja_1_0001.CR2", "caja_1_0002.CR2", "caja_1_0003.jpg"]


@pytest.mark.unit
def test_renumber_refuses_to_overwrite_a_preview_no_image_owns(db_session, override_projects_root):
    from app.core.storage_ops import renumber_collection_images

    image_dir = override_projects_root / "proj" / "images" / "caja_1"
    image_dir.mkdir(parents=True)
    (image_dir / "IMG_0001.CR2").write_bytes(b"raw")
    (image_dir / "IMG_0001_preview.jpg").write_bytes(b"preview")
    stray = image_dir / "caja_1_0001_preview.jpg"
    stray.write_bytes(b"stray")
    coll = _collection_with_images(db_session, image_dir, ["IMG_0001.CR2"])

    with pytest.raises(ValueError, match="caja_1_0001_preview.jpg"):
        renumber_collection_images(db_session, coll)

    assert sorted(p.name for p in image_dir.iterdir()) == [
        "IMG_0001.CR2", "IMG_0001_preview.jpg", "caja_1_0001_preview.jpg",
    ]
    assert stray.read_bytes() == b"stray"


# ---------------------------------------------------------------------------
# delete_thumbnail (NEH-263)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_delete_thumbnail_raises_when_the_unlink_fails(tmp_path, monkeypatch):
    from app.core.thumbnail import delete_thumbnail

    thumb = tmp_path / "t_thumb.jpg"
    thumb.write_bytes(b"t")
    real_unlink = pathlib.Path.unlink

    def unlink(self, *a, **kw):
        if self == thumb:
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "unlink", unlink)
    with pytest.raises(PermissionError):
        delete_thumbnail(str(thumb))
    assert thumb.exists()


@pytest.mark.unit
def test_delete_thumbnail_removes_the_file_and_ignores_empty_or_missing(tmp_path):
    from app.core.thumbnail import delete_thumbnail

    thumb = tmp_path / "t_thumb.jpg"
    thumb.write_bytes(b"t")
    delete_thumbnail(str(thumb))
    assert not thumb.exists()
    delete_thumbnail(str(thumb))  # already gone
    delete_thumbnail(None)
    delete_thumbnail("")


# ---------------------------------------------------------------------------
# Integrity scan
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_integrity_does_not_report_a_known_raw_masters_sidecar_as_orphan(db_session, override_projects_root):
    from app.core.integrity import run_integrity_check
    from app.models.record import Record, RecordImage

    image_dir = override_projects_root / "proj" / "images"
    image_dir.mkdir(parents=True)
    master = image_dir / "IMG_0001.CR2"
    master.write_bytes(b"raw")
    sidecar = image_dir / "IMG_0001_preview.jpg"
    sidecar.write_bytes(b"preview")
    lost_sidecar = image_dir / "IMG_0099_preview.jpg"  # its master and row are gone
    lost_sidecar.write_bytes(b"orphan preview")
    jpeg = image_dir / "folio.jpg"
    jpeg.write_bytes(b"jpeg")
    jpeg_preview = image_dir / "folio_preview.jpg"  # a JPEG master has no sidecar
    jpeg_preview.write_bytes(b"orphan preview")

    rec = Record(title="r", status="rejected", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    for m in (master, jpeg):
        db_session.add(RecordImage(record_id=rec.id, filename=m.name, file_path=str(m),
                                   format=m.suffix.lstrip(".").lower()))
    db_session.commit()

    report = run_integrity_check(db_session, verify_hashes=False)

    orphans = set(report["orphan_files"])
    assert str(sidecar.resolve()) not in orphans
    assert str(lost_sidecar.resolve()) in orphans
    assert str(jpeg_preview.resolve()) in orphans
    assert report["summary"]["orphan_files"] == 2


@pytest.mark.unit
@pytest.mark.parametrize("suffix", [".NEF", ".arw", ".raf"])
def test_integrity_scans_every_raw_suffix_for_orphans(db_session, override_projects_root, suffix):
    """The orphan scan's RAW members are RAW_SUFFIXES, so a stray .NEF is reported like a stray .CR2."""
    from app.core.integrity import run_integrity_check

    image_dir = override_projects_root / "proj" / "images"
    image_dir.mkdir(parents=True)
    stray = image_dir / f"IMG_0001{suffix}"
    stray.write_bytes(b"raw with no row")

    report = run_integrity_check(db_session, verify_hashes=False)

    assert report["orphan_files"] == [str(stray.resolve())]


# ---------------------------------------------------------------------------
# Review follow-ups
# ---------------------------------------------------------------------------

import hashlib


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.unit
def test_integrity_reports_the_sidecar_of_a_deleted_master_still_in_the_manifest(db_session, override_projects_root):
    """The manifest keeps a deleted capture's line, so a manifest path does not vouch for its sidecar."""
    import json
    from app.core.integrity import run_integrity_check
    from app.models.record import Record, RecordImage

    proj_dir = override_projects_root / "proj"
    (proj_dir / "images").mkdir(parents=True)
    (proj_dir / "metadata").mkdir(parents=True)
    master = proj_dir / "images" / "IMG_0001.CR2"
    master.write_bytes(b"raw")
    sidecar = proj_dir / "images" / "IMG_0001_preview.jpg"
    sidecar.write_bytes(b"preview")
    (proj_dir / "metadata" / "manifest.jsonl").write_text(json.dumps({
        "capture_id": "c1",
        "files": [{"sha256": _digest(master), "relative_path": "images/IMG_0001.CR2"}],
    }), encoding="utf-8")
    rec = Record(title="r", status="rejected", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    img = RecordImage(record_id=rec.id, filename=master.name, file_path=str(master),
                      capture_id="c1", format="cr2")
    db_session.add(img)
    db_session.commit()

    db_session.delete(img)
    db_session.commit()
    master.unlink()

    report = run_integrity_check(db_session, verify_hashes=False)

    assert report["orphan_files"] == [str(sidecar.resolve())]


def _fail_nth_phase2_replace(monkeypatch, n):
    """Raise on the nth os.replace from a `.renumber_` temp to a final name, once."""
    import os
    real_replace = os.replace
    state = {"count": 0, "fired": False}

    def replace(src, dst):
        if not state["fired"] and Path(src).name.startswith(".renumber_") and not Path(dst).name.startswith(".renumber_"):
            state["count"] += 1
            if state["count"] == n:
                state["fired"] = True
                raise PermissionError(13, "Permission denied", str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr("app.core.storage_ops.os.replace", replace)


@pytest.mark.unit
@pytest.mark.parametrize("fail_at", [1, 2, 3, 4, 5, 6])
def test_renumber_failure_restores_every_original_name(db_session, override_projects_root, monkeypatch, fail_at):
    """X 0003->0001, Y 0001->0002, Z 0002->0003: each original name is another file's final name.

    Six phase-2 renames (three masters, then three sidecars); the failure is
    injected at each in turn.
    """
    from app.core.storage_ops import renumber_collection_images

    image_dir = override_projects_root / "proj" / "images" / "caja_1"
    image_dir.mkdir(parents=True)
    order = ["caja_1_0003.CR2", "caja_1_0001.CR2", "caja_1_0002.CR2"]
    for name in order:
        (image_dir / name).write_bytes(f"raw {name}".encode())
        (image_dir / (Path(name).stem + "_preview.jpg")).write_bytes(f"preview {name}".encode())
    before = {p.name: _digest(p) for p in image_dir.iterdir()}
    coll = _collection_with_images(db_session, image_dir, order)

    _fail_nth_phase2_replace(monkeypatch, fail_at)
    with pytest.raises(OSError):
        renumber_collection_images(db_session, coll)

    after = {p.name: _digest(p) for p in image_dir.iterdir()}
    assert after == before


@pytest.mark.unit
def test_restore_original_names_never_overwrites_a_taken_name(tmp_path):
    from app.core.storage_ops import restore_original_names

    a = tmp_path / "a.jpg"
    moved = tmp_path / "moved.jpg"
    moved.write_bytes(b"mine")
    a.write_bytes(b"someone else")

    failed = restore_original_names([(a, moved)])

    assert failed == [a]
    assert a.read_bytes() == b"someone else"
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".renumber_")]
    assert len(leftovers) == 1 and leftovers[0].read_bytes() == b"mine"


@pytest.mark.unit
def test_renumber_refuses_a_stray_preview_at_a_sidecarless_raw_masters_new_name(db_session, override_projects_root):
    from app.core.storage_ops import renumber_collection_images

    image_dir = override_projects_root / "proj" / "images" / "caja_1"
    image_dir.mkdir(parents=True)
    (image_dir / "IMG_0001.CR2").write_bytes(b"raw")
    (image_dir / "caja_1_0001_preview.jpg").write_bytes(b"stray")
    coll = _collection_with_images(db_session, image_dir, ["IMG_0001.CR2"])

    with pytest.raises(ValueError, match="caja_1_0001_preview.jpg"):
        renumber_collection_images(db_session, coll)
    assert (image_dir / "IMG_0001.CR2").exists()


@pytest.mark.unit
def test_relocate_reports_a_sidecar_that_could_not_follow(dirs, monkeypatch):
    import shutil
    from app.core.storage_ops import relocate_image

    src_dir, dst_dir = dirs
    master = src_dir / "IMG_0010.CR2"
    master.write_bytes(b"raw")
    (src_dir / "IMG_0010_preview.jpg").write_bytes(b"preview")
    img = _row(master)
    real_move = shutil.move

    def move(src, dst, *a, **kw):
        if src.endswith("_preview.jpg"):
            raise PermissionError(13, "Permission denied", src)
        return real_move(src, dst, *a, **kw)

    monkeypatch.setattr("app.core.storage_ops.shutil.move", move)
    not_moved = []
    assert relocate_image(img, dst_dir, not_moved) is True
    assert not_moved == ["IMG_0010_preview.jpg"]


@pytest.mark.unit
def test_relocate_renames_a_sidecarless_raw_master_beside_a_stray_preview(dirs):
    from app.core.storage_ops import relocate_image

    src_dir, dst_dir = dirs
    dst_dir.mkdir(parents=True)
    master = src_dir / "IMG_0011.CR2"
    master.write_bytes(b"raw")
    stray = dst_dir / "IMG_0011_preview.jpg"
    stray.write_bytes(b"stray")
    img = _row(master)

    assert relocate_image(img, dst_dir) is True
    new_master = Path(img.file_path)
    assert new_master.name != "IMG_0011.CR2"
    assert img.filename == new_master.name
    assert not new_master.with_name(new_master.stem + "_preview.jpg").exists()
    assert stray.read_bytes() == b"stray"


@pytest.mark.unit
def test_relocate_renames_beside_an_unrelated_master_of_the_same_stem(dirs):
    from app.core.storage_ops import relocate_image

    src_dir, dst_dir = dirs
    dst_dir.mkdir(parents=True)
    master = src_dir / "folio.jpg"
    master.write_bytes(b"jpeg")
    (dst_dir / "folio.CR2").write_bytes(b"another record's raw")
    img = _row(master)

    assert relocate_image(img, dst_dir) is True
    assert Path(img.file_path).stem != "folio"


@pytest.mark.unit
def test_relocate_keeps_a_jpeg_and_raw_pair_together(db_session, dirs):
    """The pair's CR2, moved first, is a sibling of the jpg, not a clash."""
    from app.core.storage_ops import relocate_image
    from app.models.record import Record, RecordImage

    src_dir, dst_dir = dirs
    raw = src_dir / "folio.CR2"
    raw.write_bytes(b"raw")
    (src_dir / "folio_preview.jpg").write_bytes(b"preview")
    jpeg = src_dir / "folio.jpg"
    jpeg.write_bytes(b"jpeg")
    rec = Record(title="r", status="rejected", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    raw_img = RecordImage(record_id=rec.id, filename=raw.name, file_path=str(raw), format="cr2")
    jpeg_img = RecordImage(record_id=rec.id, filename=jpeg.name, file_path=str(jpeg), format="jpg")
    db_session.add_all([raw_img, jpeg_img])
    db_session.commit()
    db_session.refresh(rec)

    relocate_image(raw_img, dst_dir)
    relocate_image(jpeg_img, dst_dir)

    assert sorted(p.name for p in dst_dir.iterdir()) == ["folio.CR2", "folio.jpg", "folio_preview.jpg"]


@pytest.mark.unit
def test_relocate_keeps_an_uploads_original_filename_on_a_uuid_rename(dirs):
    from app.core.storage_ops import relocate_image

    src_dir, dst_dir = dirs
    dst_dir.mkdir(parents=True)
    stored = src_dir / "3f2a9c.jpg"
    stored.write_bytes(b"upload")
    (dst_dir / "3f2a9c.jpg").write_bytes(b"occupant")
    img = _row(stored)
    img.filename = "carta de 1810.jpg"

    assert relocate_image(img, dst_dir) is True
    assert Path(img.file_path).name != "3f2a9c.jpg"
    assert img.filename == "carta de 1810.jpg"


# Endpoints -----------------------------------------------------------------

def _as_operator(client):
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User

    app.dependency_overrides[get_current_user] = lambda: User(
        username="op", email="op@example.com", hashed_password="x", role="operator", is_active=True)
    return client


@pytest.fixture
def capture_settings(override_projects_root, monkeypatch):
    """Point capture.project_manager at this test's projects root.

    It binds `settings` at import, so the reload `override_projects_root`
    does is not seen there; without this the endpoints' target directories
    land under whichever test imported it first.
    """
    import capture.project_manager as pm
    from app.core import config

    monkeypatch.setattr(pm, "settings", config.settings)


def _two_collections_with_a_raw_capture(db_session):
    from capture.project_manager import image_output_dir
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    project = Project(name="proj")
    db_session.add(project)
    db_session.commit()
    a = Collection(name="A", project_id=project.id)
    b = Collection(name="B", project_id=project.id)
    db_session.add_all([a, b])
    db_session.commit()
    src_dir = image_output_dir("proj", a.id)
    src_dir.mkdir(parents=True)
    master = src_dir / "IMG_0020.CR2"
    master.write_bytes(b"raw")
    sidecar = src_dir / "IMG_0020_preview.jpg"
    sidecar.write_bytes(b"preview")
    rec = Record(title="r", collection_id=a.id, status="captured", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.add(RecordImage(record_id=rec.id, filename=master.name, file_path=str(master), format="cr2"))
    db_session.commit()
    return a, b, sidecar


@pytest.mark.unit
def test_move_records_lists_a_sidecar_left_behind(client, db_session, monkeypatch, capture_settings):
    import shutil

    a, b, sidecar = _two_collections_with_a_raw_capture(db_session)
    real_move = shutil.move

    def move(src, dst, *args, **kw):
        if src.endswith("_preview.jpg"):
            raise PermissionError(13, "Permission denied", src)
        return real_move(src, dst, *args, **kw)

    monkeypatch.setattr("app.core.storage_ops.shutil.move", move)
    resp = _as_operator(client).post(f"/collections/{a.id}/move-records?target_collection_id={b.id}")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"moved": 1, "target_collection_id": b.id, "not_moved": [sidecar.name]}
    assert sidecar.exists()


@pytest.mark.unit
def test_move_records_has_no_not_moved_key_when_everything_moved(client, db_session, capture_settings):
    from capture.project_manager import image_output_dir

    a, b, sidecar = _two_collections_with_a_raw_capture(db_session)
    resp = _as_operator(client).post(f"/collections/{a.id}/move-records?target_collection_id={b.id}")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"moved": 1, "target_collection_id": b.id}
    assert (image_output_dir("proj", b.id) / sidecar.name).exists()


@pytest.mark.unit
def test_reparent_into_another_project_lists_a_sidecar_left_behind(client, db_session, monkeypatch, capture_settings):
    import shutil
    from app.models.collection import Collection
    from app.models.project import Project

    a, _, sidecar = _two_collections_with_a_raw_capture(db_session)
    other = Project(name="proj2")
    db_session.add(other)
    db_session.commit()
    c = Collection(name="C", project_id=other.id)
    db_session.add(c)
    db_session.commit()
    real_move = shutil.move

    def move(src, dst, *args, **kw):
        if src.endswith("_preview.jpg"):
            raise PermissionError(13, "Permission denied", src)
        return real_move(src, dst, *args, **kw)

    monkeypatch.setattr("app.core.storage_ops.shutil.move", move)
    resp = _as_operator(client).patch(f"/collections/{a.id}", json={"parent_collection_id": c.id})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parent_collection_id"] == c.id
    assert body["not_moved"] == [sidecar.name]


@pytest.mark.unit
def test_renumber_whose_commit_fails_puts_the_files_back(client, db_session, monkeypatch, capture_settings):
    from sqlalchemy.exc import OperationalError
    from capture.project_manager import image_output_dir
    from app.models.record import RecordImage

    a, _, sidecar = _two_collections_with_a_raw_capture(db_session)
    image_dir = image_output_dir("proj", a.id)
    before = {p.name: _digest(p) for p in image_dir.iterdir()}

    def failing_commit():
        raise OperationalError("COMMIT", {}, Exception("disk I/O error"))

    monkeypatch.setattr(db_session, "commit", failing_commit)
    resp = _as_operator(client).post(f"/collections/{a.id}/images/renumber")

    assert resp.status_code == 500, resp.text
    assert "back under their old names" in resp.json()["detail"]
    assert {p.name: _digest(p) for p in image_dir.iterdir()} == before
    db_session.rollback()
    assert db_session.query(RecordImage).one().filename == "IMG_0020.CR2"


# Two collections of one name share a directory --------------------------------

def _same_named_collections(db_session, image_dir):
    """Collections A and B, both named "Tomo 1", whose masters share `image_dir`."""
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    project = Project(name="proj")
    db_session.add(project)
    db_session.commit()
    colls = []
    for _ in range(2):
        c = Collection(name="Tomo 1", project_id=project.id)
        db_session.add(c)
        db_session.commit()
        colls.append(c)

    def add(coll, name, seq):
        rec = Record(title=name, collection_id=coll.id, sequence=seq,
                     status="in_review", capture_mode="single")
        db_session.add(rec)
        db_session.commit()
        db_session.add(RecordImage(record_id=rec.id, filename=name, file_path=str(image_dir / name),
                                   role="single", format=Path(name).suffix.lstrip(".").lower()))
        db_session.commit()

    return colls, add


@pytest.mark.unit
def test_renumber_refuses_to_overwrite_a_same_named_collections_master(db_session, override_projects_root):
    from app.core.storage_ops import renumber_collection_images

    image_dir = override_projects_root / "proj" / "tomo_1" / "images" / "main"
    image_dir.mkdir(parents=True)
    (a, b), add = _same_named_collections(db_session, image_dir)
    (image_dir / "tomo_1_0001.jpg").write_bytes(b"B's renumbered master")
    add(b, "tomo_1_0001.jpg", 1)
    (image_dir / "IMG_0001.jpg").write_bytes(b"A's master")
    add(a, "IMG_0001.jpg", 1)
    before = {p.name: _digest(p) for p in image_dir.iterdir()}

    db_session.refresh(a)
    with pytest.raises(ValueError, match="tomo_1_0001.jpg"):
        renumber_collection_images(db_session, a)

    assert {p.name: _digest(p) for p in image_dir.iterdir()} == before


@pytest.mark.unit
def test_renumber_refuses_a_new_name_whose_stem_another_collection_holds(db_session, override_projects_root):
    from app.core.storage_ops import renumber_collection_images

    image_dir = override_projects_root / "proj" / "tomo_1" / "images" / "main"
    image_dir.mkdir(parents=True)
    (a, b), add = _same_named_collections(db_session, image_dir)
    (image_dir / "tomo_1_0001.tif").write_bytes(b"B's tiff")
    add(b, "tomo_1_0001.tif", 1)
    (image_dir / "IMG_0001.jpg").write_bytes(b"A's master")
    add(a, "IMG_0001.jpg", 1)
    before = {p.name: _digest(p) for p in image_dir.iterdir()}

    db_session.refresh(a)
    with pytest.raises(ValueError, match="tomo_1_0001.tif"):
        renumber_collection_images(db_session, a)

    assert {p.name: _digest(p) for p in image_dir.iterdir()} == before


@pytest.mark.unit
def test_renumber_of_one_collection_in_a_shared_directory_still_runs(db_session, override_projects_root):
    """Names the other collection does not hold are free to take."""
    from app.core.storage_ops import renumber_collection_images

    image_dir = override_projects_root / "proj" / "tomo_1" / "images" / "main"
    image_dir.mkdir(parents=True)
    (a, b), add = _same_named_collections(db_session, image_dir)
    (image_dir / "other_0001.jpg").write_bytes(b"B's master")
    add(b, "other_0001.jpg", 1)
    (image_dir / "IMG_0001.jpg").write_bytes(b"A's master")
    add(a, "IMG_0001.jpg", 1)

    db_session.refresh(a)
    renumber_collection_images(db_session, a)

    assert (image_dir / "tomo_1_0001.jpg").read_bytes() == b"A's master"
    assert (image_dir / "other_0001.jpg").read_bytes() == b"B's master"


# The endpoint's 500 for an on-disk failure -------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("unwind_fails", [False, True])
def test_renumber_500_says_whether_every_file_went_back(client, db_session, monkeypatch, capture_settings, unwind_fails):
    import os
    from capture.project_manager import image_output_dir

    a, _, sidecar = _two_collections_with_a_raw_capture(db_session)
    image_dir = image_output_dir("proj", a.id)
    real_replace = os.replace
    state = {"phase2": 0}

    def replace(src, dst):
        src, dst = Path(src), Path(dst)
        if src.name.startswith(".renumber_") and not dst.name.startswith(".renumber_"):
            state["phase2"] += 1
            # Fail the second phase-2 rename (the sidecar's); when asked, also
            # fail every step of the unwind that moves the master back.
            if state["phase2"] == 2:
                raise PermissionError(13, "Permission denied", str(dst))
        if unwind_fails and src.name.endswith(".CR2") and src.name.startswith("a_0001"):
            raise PermissionError(13, "Permission denied", str(src))
        return real_replace(src, dst)

    monkeypatch.setattr("app.core.storage_ops.os.replace", replace)
    resp = _as_operator(client).post(f"/collections/{a.id}/images/renumber")

    assert resp.status_code == 500, resp.text
    detail = resp.json()["detail"]
    if unwind_fails:
        assert detail.endswith("Some files could not be put back under their old names")
        assert (image_dir / "a_0001.CR2").exists()
    else:
        assert detail.endswith("The files are back under their old names")
        assert sorted(p.name for p in image_dir.iterdir()) == ["IMG_0020.CR2", sidecar.name]
