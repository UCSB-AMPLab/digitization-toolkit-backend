"""A file already in `<stem>/` that forces a row's master to be renamed `<stem>_img<id>` is reported on that run.

The file is no row's, so it is listed in `left_unreferenced` (files no row
references, left where they are) and logged at WARNING with the image id it
displaced. Otherwise the next run finds it foreign and skips the volume as
`stem_dir_occupied` with no earlier word about it.

Version: 0.1.0-beta
"""

import pytest

from tests.unit.test_volume_layout_by_stem import (  # noqa: F401
    STEMS, capture_settings, data_dir, legacy,
)


@pytest.mark.unit
@pytest.mark.parametrize("name", ["IMG_0002.jpg", "img_0002.JPG"])
def test_a_file_that_forces_a_rename_is_reported_on_that_run(db_session, legacy, caplog, name):
    from app.core.volume_layout import migrate_volume_layout

    target = legacy["root"] / STEMS["a2"] / "captures"
    target.mkdir(parents=True)
    occupant = target / name
    occupant.write_bytes(b"other bytes")

    with caplog.at_level("WARNING", logger="app.core.volume_layout"):
        report = migrate_volume_layout(db_session)

    moved = {m["image_id"]: m["to"] for m in report["moved"]}
    assert moved[legacy["i2"].id].endswith(f"IMG_0002_img{legacy['i2'].id}.jpg")
    assert str(occupant.resolve()) in report["left_unreferenced"]
    assert occupant.read_bytes() == b"other bytes"
    assert any(str(occupant.resolve()) in r.getMessage() and f"image {legacy['i2'].id}" in r.getMessage()
               for r in caplog.records)
    # The next run names the same file as the reason the volume is skipped.
    again = migrate_volume_layout(db_session)
    assert again["moved"] == []


@pytest.mark.unit
def test_a_preview_that_forces_a_rename_is_reported_on_that_run(db_session, legacy, caplog):
    from app.core.volume_layout import migrate_volume_layout

    previews = legacy["root"] / STEMS["a1"] / "previews"
    previews.mkdir(parents=True)
    occupant = previews / "IMG_0001_preview.jpg"
    occupant.write_bytes(b"another preview")

    with caplog.at_level("WARNING", logger="app.core.volume_layout"):
        report = migrate_volume_layout(db_session)

    assert str(occupant.resolve()) in report["left_unreferenced"]
    assert any(str(occupant.resolve()) in r.getMessage() and f"image {legacy['i1'].id}" in r.getMessage()
               for r in caplog.records)


@pytest.mark.unit
def test_a_clash_between_two_rows_reports_nothing(db_session, legacy):
    """A name taken by another row's planned move is that row's file, not a stray one."""
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import Record, RecordImage

    second = legacy["root"] / "proj" / "tomo_1" / "images" / "main" / "IMG_0002.jpg"
    second.write_bytes(b"a second IMG_0002 in another directory")
    rec = Record(title="r", collection_id=legacy["a2"].id, status="captured", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.add(RecordImage(record_id=rec.id, filename=second.name, file_path=str(second), format="jpg"))
    db_session.commit()

    report = migrate_volume_layout(db_session)

    target = (legacy["root"] / STEMS["a2"] / "captures").resolve()
    assert not [p for p in report["left_unreferenced"] if p.startswith(str(target))]


@pytest.mark.unit
def test_a_file_named_like_the_master_with_another_suffix_skips_the_volume_naming_it(db_session, legacy):
    """Not a clash the run renames for: it is foreign already, and the skip names it."""
    from app.core.volume_layout import migrate_volume_layout

    target = legacy["root"] / STEMS["a2"] / "captures"
    target.mkdir(parents=True)
    (target / "IMG_0002.tif").write_bytes(b"other bytes")

    report = migrate_volume_layout(db_session)

    assert {tuple(s["foreign"]) for s in report["skipped"] if s["reason"] == "stem_dir_occupied"} == {
        ("captures/IMG_0002.tif",)}


@pytest.mark.unit
def test_a_file_that_takes_the_name_after_planning_is_reported(db_session, legacy, monkeypatch, caplog):
    import app.core.volume_layout as vl

    real_plan = vl._plan
    planted = []

    def plan_then_plant(*a, **k):
        out = real_plan(*a, **k)
        for b in out[0]:
            for e in b["entries"]:
                if e["image_id"] == legacy["i2"].id:
                    dst = vl.Path(e["to"])
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(b"arrived after planning")
                    planted.append(dst.resolve())
        return out

    monkeypatch.setattr(vl, "_plan", plan_then_plant)
    with caplog.at_level("WARNING", logger="app.core.volume_layout"):
        report = vl.migrate_volume_layout(db_session)

    assert planted and str(planted[0]) in report["left_unreferenced"]
    assert planted[0].read_bytes() == b"arrived after planning"
    assert f"image {legacy['i2'].id}" in caplog.text


@pytest.mark.unit
def test_a_file_that_forces_the_second_rename_is_reported(db_session, legacy, caplog):
    from app.core.volume_layout import migrate_volume_layout

    target = legacy["root"] / STEMS["a2"] / "captures"
    target.mkdir(parents=True)
    iid = legacy["i2"].id
    first = target / "IMG_0002.jpg"
    second = target / f"IMG_0002_img{iid}.jpg"
    first.write_bytes(b"stray one")
    second.write_bytes(b"stray two")
    with caplog.at_level("WARNING", logger="app.core.volume_layout"):
        report = migrate_volume_layout(db_session)
    moved = {m["image_id"]: m["to"] for m in report["moved"]}
    left = report["left_unreferenced"]
    assert moved[iid].endswith(f"IMG_0002_img{iid}_2.jpg")
    assert str(first.resolve()) in left and str(second.resolve()) in left
    assert str(second.resolve()) in caplog.text


@pytest.mark.unit
def test_a_preview_at_a_passed_over_name_is_reported(db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout

    iid = legacy["i1"].id
    previews = legacy["root"] / STEMS["a1"] / "previews"
    previews.mkdir(parents=True)
    first = previews / "IMG_0001_preview.jpg"
    second = previews / f"IMG_0001_img{iid}_preview.jpg"
    first.write_bytes(b"stray one")
    second.write_bytes(b"stray two")

    report = migrate_volume_layout(db_session)

    assert str(first.resolve()) in report["left_unreferenced"]
    assert str(second.resolve()) in report["left_unreferenced"]


# ---------------------------------------------------------------------------
# Names taken after planning
# ---------------------------------------------------------------------------

def _plant_after_planning(vl, monkeypatch, image_id, plant):
    """Wrap _plan so that, once it has planned `image_id`'s move, `plant(planned destination)` runs."""
    real_plan = vl._plan

    def plan_then_plant(*a, **k):
        out = real_plan(*a, **k)
        for b in out[0]:
            for e in b["entries"]:
                if e["image_id"] == image_id:
                    plant(vl.Path(e["to"]))
        return out

    monkeypatch.setattr(vl, "_plan", plan_then_plant)


@pytest.mark.unit
def test_a_late_occupant_that_a_row_references_is_not_reported_as_unreferenced(db_session, legacy, monkeypatch):
    import app.core.volume_layout as vl
    from app.models.record import Record, RecordImage

    planted = []

    def plant(dst):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"another row's master")
        rec = Record(title="r", collection_id=legacy["a2"].id, status="captured", capture_mode="single")
        db_session.add(rec)
        db_session.flush()
        db_session.add(RecordImage(record_id=rec.id, filename=dst.name, file_path=str(dst), format="jpg"))
        db_session.flush()
        planted.append(dst.resolve())

    _plant_after_planning(vl, monkeypatch, legacy["i2"].id, plant)

    report = vl.migrate_volume_layout(db_session)

    assert planted
    assert str(planted[0]) not in report["left_unreferenced"]
    moved = {m["image_id"]: m["to"] for m in report["moved"]}
    assert moved[legacy["i2"].id].endswith(f"IMG_0002_img{legacy['i2'].id}.jpg")


@pytest.mark.unit
def test_every_name_taken_after_planning_is_reported_previews_included(db_session, legacy, monkeypatch):
    """IMG_0001.CR2 and IMG_0001_img<id>.CR2 in captures/, and the preview of _img<id>_2, appear after planning."""
    import app.core.volume_layout as vl

    iid = legacy["i1"].id
    planted = []

    def plant(dst):
        captures = dst.parent
        previews = captures.parent / "previews"
        captures.mkdir(parents=True, exist_ok=True)
        previews.mkdir(parents=True, exist_ok=True)
        for f in (dst, captures / f"IMG_0001_img{iid}.CR2", previews / f"IMG_0001_img{iid}_2_preview.jpg"):
            f.write_bytes(b"arrived after planning")
            planted.append(f.resolve())

    _plant_after_planning(vl, monkeypatch, iid, plant)

    report = vl.migrate_volume_layout(db_session)

    moved = {m["image_id"]: m["to"] for m in report["moved"]}
    assert moved[iid].endswith(f"IMG_0001_img{iid}_3.CR2")
    for f in planted:
        assert str(f) in report["left_unreferenced"], f
        assert f.read_bytes() == b"arrived after planning"


@pytest.mark.unit
def test_a_directory_at_the_planned_name_after_planning_counts_as_taken(db_session, legacy, monkeypatch):
    """A directory, not only a file, takes the name: the master moves to the next free one, not reported missing."""
    import app.core.volume_layout as vl
    from app.models.record import RecordImage

    iid = legacy["i1"].id
    planted = []

    def plant(dst):
        dst.mkdir(parents=True)
        planted.append(dst)

    _plant_after_planning(vl, monkeypatch, iid, plant)

    report = vl.migrate_volume_layout(db_session)

    moved = {m["image_id"]: m["to"] for m in report["moved"]}
    assert moved[iid].endswith(f"IMG_0001_img{iid}.CR2")
    assert not [s for s in report["skipped"] if s["image_id"] == iid]
    db_session.expire_all()
    master = vl.Path(db_session.get(RecordImage, iid).file_path)
    assert master.name == f"IMG_0001_img{iid}.CR2" and master.read_bytes() == b"bytes of IMG_0001.CR2"
    assert planted[0].is_dir()
    # left_unreferenced lists files; the directory is not one.
    assert str(planted[0].resolve()) not in report["left_unreferenced"]


@pytest.mark.unit
def test_a_master_moved_earlier_in_the_batch_is_never_reported_as_unreferenced(db_session, legacy, monkeypatch):
    """IMG_0002 is pushed by a late file into IMG_0002_img<id>.jpg, the name another row of the batch was planned to take.

    Production sessions do not autoflush and the batch commits at its end, so
    the earlier move is not yet in the database when the later entry meets it.
    """
    from sqlalchemy.orm import sessionmaker
    import app.core.volume_layout as vl
    from app.models.record import Record, RecordImage

    i2 = legacy["i2"].id
    other = legacy["root"] / "proj" / "tomo_1" / "images" / "main" / f"IMG_0002_img{i2}.jpg"
    other.write_bytes(b"a page whose name is a clash name")
    rec = Record(title="r", collection_id=legacy["a2"].id, status="captured", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    row = RecordImage(record_id=rec.id, filename=other.name, file_path=str(other), format="jpg", role="single")
    db_session.add(row)
    db_session.commit()

    def plant(dst):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"arrived after planning")

    _plant_after_planning(vl, monkeypatch, i2, plant)
    session = sessionmaker(bind=db_session.get_bind(), autoflush=False)()
    try:
        report = vl.migrate_volume_layout(session)
    finally:
        session.close()

    moved = {m["image_id"]: vl.Path(m["to"]).resolve() for m in report["moved"]}
    assert moved[i2].name == f"IMG_0002_img{i2}.jpg"
    assert str(moved[i2]) not in report["left_unreferenced"]
    assert str(moved[row.id]) not in report["left_unreferenced"]
    late = (legacy["root"] / STEMS["a2"] / "captures" / "IMG_0002.jpg").resolve()
    assert str(late) in report["left_unreferenced"]
