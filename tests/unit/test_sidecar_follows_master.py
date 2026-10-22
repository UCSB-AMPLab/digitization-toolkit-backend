"""A RAW master's `_preview.jpg` sidecar follows it wherever it is moved or renamed.

NEH-267: `relocate_image` and `renumber_collection_images` carry
`<stem>_preview.jpg` to the name the master's final path derives, because the
preview endpoint and deletes look for the sidecar beside the master's current
path. NEH-264: the name rule is `preview_sidecar_of` in app/core/paths.py.
NEH-263: `delete_thumbnail` raises when the unlink fails. The integrity scan
counts a sidecar of a known RAW master as known, not orphaned.

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
