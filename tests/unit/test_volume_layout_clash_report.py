"""A file already in `<stem>/` that forces a row's master to be renamed `<stem>_img<id>` is reported on that run.

The file is no row's, so it is listed in `left_unreferenced` (files no row
references, left where they are) and logged at WARNING with the image id it
displaced. Otherwise the next run finds it foreign and skips the volume as
`stem_dir_occupied` with no earlier word about it. The staged case of the
Rionegro bench rehearsal.
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
