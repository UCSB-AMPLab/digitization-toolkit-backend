"""Freed files of a stemmed volume still in the name-based layout, and the startup layout migration.

Rionegro's CR2 masters were removed by hand while the volume's files were
still in `<project>/<name>/images/main/`, and `mark-freed` records them as
freed. The migration moves nothing for an absent freed file: it points the
row at the name it would have moved the file to in `<stem>/`, by the same
naming and collision rules, and that name stays reserved. The volume is then
wholly in its own folder, so a restore writes the bytes there.

    python -m pytest tests/unit/test_offsite_free_layout.py
"""

import hashlib
from pathlib import Path

import pytest

from tests.unit.test_image_upload import STEM, TENANT, _run, _volume, remote  # noqa: F401
from tests.unit.test_offsite_free import _image_row, _restore, read_remote  # noqa: F401

OLD = ("p", "tomo", "images", "main")


def _old_layout_image(db, vol, root, name, data, preview=None):
    """A record of `vol` whose master (and RAW preview, beside it) are in the name-based layout."""
    from app.models.record import Record, RecordImage

    master = root.joinpath(*OLD, name)
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_bytes(data)
    if preview is not None:
        master.with_name(f"{master.stem}_preview.jpg").write_bytes(preview)
    rec = Record(title=name, collection_id=vol.id, capture_mode="single")
    db.add(rec)
    db.commit()
    img = RecordImage(record_id=rec.id, filename=name, file_path=str(master),
                      format=Path(name).suffix.lstrip(".").upper())
    db.add(img)
    db.commit()
    return img, master


def _hand_freed(db, root):
    """A received old-layout volume whose CR2 master was removed by hand and marked freed; its preview is kept."""
    from app.core.offsite_free import mark_freed

    vol = _volume(db)
    img, master = _old_layout_image(db, vol, root, "f1.CR2", b"raw one", preview=b"preview one")
    assert _run(db)["uploaded"] == 2
    master.unlink()
    report = mark_freed(db, STEM, apply=True)
    assert [(m["image_id"], m["kind"]) for m in report["marked"]] == [(img.id, "capture")]
    return vol, img, master


def _migrate(db):
    from app.core.volume_layout import migrate_volume_layout

    db.expire_all()
    return migrate_volume_layout(db)


@pytest.mark.unit
def test_the_migration_points_a_freed_master_into_its_folder_and_restore_puts_the_bytes_there(
        read_remote, db_session, override_projects_root):
    vol, img, old = _hand_freed(db_session, override_projects_root)
    new = override_projects_root / STEM / "captures" / "f1.CR2"

    report = _migrate(db_session)

    assert [(m["image_id"], m["to"]) for m in report["freed_paths_moved"]] == [(img.id, str(new.resolve()))]
    row = _image_row(db_session, img.id)
    assert row.file_path == str(new.resolve()) and row.master_freed_at is not None
    assert not new.exists(), "the migration created a file for a freed master"
    # The kept preview moved as usual.
    assert (override_projects_root / STEM / "previews" / "f1_preview.jpg").read_bytes() == b"preview one"
    assert not old.with_name("f1_preview.jpg").exists()
    assert not [s for s in report["skipped"] if s["image_id"] == img.id]

    result = _restore(db_session, vol)

    assert result["refused"] == [], result
    assert new.read_bytes() == b"raw one"
    assert _image_row(db_session, img.id).master_freed_at is None
    assert _migrate(db_session)["freed_paths_moved"] == []


@pytest.mark.unit
def test_a_freed_masters_destination_taken_by_a_stray_gets_the_clash_name(
        read_remote, db_session, override_projects_root):
    vol, img, old = _hand_freed(db_session, override_projects_root)
    captures = override_projects_root / STEM / "captures"
    captures.mkdir(parents=True)
    stray = captures / "f1.CR2"
    stray.write_bytes(b"not this image")

    report = _migrate(db_session)

    renamed = captures / f"f1_img{img.id}.CR2"
    assert [(m["image_id"], m["to"]) for m in report["freed_paths_moved"]] == [(img.id, str(renamed.resolve()))]
    assert _image_row(db_session, img.id).file_path == str(renamed.resolve())
    assert not renamed.exists()
    assert stray.read_bytes() == b"not this image"
    assert (override_projects_root / STEM / "previews" / f"f1_img{img.id}_preview.jpg").read_bytes() == b"preview one"

    _restore(db_session, vol)

    assert renamed.read_bytes() == b"raw one"
    assert stray.read_bytes() == b"not this image"


@pytest.mark.unit
def test_a_freed_masters_reserved_name_is_not_given_to_a_later_file(remote, db_session, override_projects_root):
    vol, img, old = _hand_freed(db_session, override_projects_root)
    _migrate(db_session)
    reserved = override_projects_root / STEM / "captures" / "f1.CR2"
    assert _image_row(db_session, img.id).file_path == str(reserved.resolve())
    # Another record of the volume, still in the old layout, with the same name.
    other, other_old = _old_layout_image(db_session, vol, override_projects_root, "f1.CR2", b"another page")

    report = _migrate(db_session)

    moved = {m["image_id"]: m["to"] for m in report["moved"]}
    assert moved[other.id] != str(reserved.resolve())
    assert Path(moved[other.id]).read_bytes() == b"another page"
    assert not reserved.exists()


@pytest.mark.unit
def test_the_dry_run_lists_the_freed_paths_it_would_point(remote, db_session, override_projects_root):
    from app.core.volume_layout import migrate_volume_layout

    vol, img, old = _hand_freed(db_session, override_projects_root)

    report = migrate_volume_layout(db_session, dry_run=True)

    assert [m["image_id"] for m in report["freed_paths_moved"]] == [img.id]
    assert _image_row(db_session, img.id).file_path == str(old)


@pytest.mark.unit
def test_a_freed_masters_name_taken_after_planning_gets_the_next_name_as_a_move_would(
        remote, db_session, override_projects_root, monkeypatch):
    import app.core.volume_layout as vl

    vol, img, old = _hand_freed(db_session, override_projects_root)
    late = override_projects_root / STEM / "captures" / "f1.CR2"
    real = vl._plan

    def plan_then_a_file_appears(*args, **kwargs):
        out = real(*args, **kwargs)
        late.parent.mkdir(parents=True, exist_ok=True)
        late.write_bytes(b"written after planning")
        return out

    monkeypatch.setattr(vl, "_plan", plan_then_a_file_appears)

    report = _migrate(db_session)

    renamed = override_projects_root / STEM / "captures" / f"f1_img{img.id}.CR2"
    assert _image_row(db_session, img.id).file_path == str(renamed.resolve())
    assert not renamed.exists()
    assert late.read_bytes() == b"written after planning"
    assert str(late.resolve()) in report["left_unreferenced"]
    assert (override_projects_root / STEM / "previews" / f"f1_img{img.id}_preview.jpg").read_bytes() == b"preview one"
