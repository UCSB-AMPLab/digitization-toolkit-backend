"""A volume's files are never merged into a `<stem>/` that already holds something else.

The startup migration skips a volume whose `<stem>/` holds anything but the
four directories and, in them, files of that volume's rows (its masters, their
previews, its thumbnails, the names a restored row's moved file may have, and
a manifest of its own captures), reporting it as `stem_dir_occupied` with the
foreign entries. Setting a stem is refused with 409 when `<stem>/` exists on
disk with a file in it and the volume has no image there.

The first test is the verifier's (round 4).
"""

import json

import pytest

from tests.unit.test_volume_layout_by_stem import (  # noqa: F401
    STEMS, _as_admin, _capture_records, capture_settings, data_dir, legacy,
)


@pytest.mark.unit
def test_a_stray_directory_named_like_the_stem_is_not_merged_into(db_session, legacy):
    """<stem>/ already holds someone else's tree: a deleted project's leftovers, without images/ or packages/."""
    from app.core.volume_layout import migrate_volume_layout

    stray = legacy["root"] / STEMS["a1"] / "fondo_z" / "images" / "main"
    stray.mkdir(parents=True)
    (stray / "z.jpg").write_bytes(b"someone else")
    (legacy["root"] / STEMS["a1"] / "metadata").mkdir()
    (legacy["root"] / STEMS["a1"] / "metadata" / "manifest.jsonl").write_text('{"capture_id": "cap-z"}\n')

    report = migrate_volume_layout(db_session)

    moved_a1 = [m for m in report["moved"] if m["collection_id"] == legacy["a1"].id]
    skipped = [s for s in report["skipped"] if s["image_id"] == legacy["i1"].id]
    assert moved_a1 == []
    assert [s["reason"] for s in skipped] == ["stem_dir_occupied"]
    assert sorted(skipped[0]["foreign"]) == ["fondo_z", "metadata/manifest.jsonl"]
    assert (legacy["shared"] / "IMG_0001.CR2").exists()


@pytest.mark.unit
@pytest.mark.parametrize("rel", ["captures/someone.jpg", "previews/someone_preview.jpg",
                                 "thumbnails/someone_thumb.jpg", "notes.txt", "captures/sub/x.jpg"])
def test_a_single_foreign_file_stops_the_move(db_session, legacy, rel):
    from app.core.volume_layout import migrate_volume_layout

    f = legacy["root"] / STEMS["a2"] / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"foreign")

    report = migrate_volume_layout(db_session)

    assert not [m for m in report["moved"] if m["collection_id"] == legacy["a2"].id]
    foreign = {tuple(s["foreign"]) for s in report["skipped"] if s["reason"] == "stem_dir_occupied"}
    assert foreign == {(rel.split("/sub")[0] + "/sub",) if "/sub/" in rel else (rel,)}
    # The other volumes migrate as before.
    assert [m for m in report["moved"] if m["collection_id"] == legacy["a1"].id]


@pytest.mark.unit
def test_the_volumes_own_files_and_manifest_do_not_stop_the_move(db_session, legacy, monkeypatch):
    """Captures made since the stem was set, and a batch's own copies, are the volume's."""
    import app.core.volume_layout as vl

    _capture_records(db_session, legacy)

    class Crash(BaseException):
        pass

    real = vl._move
    n = {"i": 0}

    def crash_once(src, dst):
        n["i"] += 1
        if n["i"] == 1:
            raise Crash()
        real(src, dst)

    monkeypatch.setattr(vl, "_move", crash_once)
    with pytest.raises(Crash):
        vl.migrate_volume_layout(db_session)  # the a1 copy is written, then the crash
    db_session.rollback()
    monkeypatch.setattr(vl, "_move", real)
    vl.clear_layout_journal()  # as if the journal were gone: the plan sees the copy in <stem>/metadata

    report = vl.migrate_volume_layout(db_session)

    assert not [s for s in report["skipped"] if s["reason"] == "stem_dir_occupied"]
    assert [m for m in report["moved"] if m["collection_id"] == legacy["a1"].id]


# ---------------------------------------------------------------------------
# Setting a stem whose directory exists
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_setting_a_stem_whose_directory_holds_files_is_refused(client, db_session, legacy):
    from app.models.collection import Collection

    stray = legacy["root"] / "xx-test-b" / "captures" / "old.jpg"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"left by someone")

    resp = _as_admin(client).patch(f"/collections/{legacy['u'].id}", json={"filename_stem": "xx-test-b"})

    assert resp.status_code == 409, resp.text
    assert "xx-test-b" in resp.json()["detail"]["message"]
    db_session.expire_all()
    assert db_session.get(Collection, legacy["u"].id).filename_stem is None


@pytest.mark.unit
def test_creating_a_volume_whose_stem_directory_holds_files_is_refused(client, db_session, legacy):
    (legacy["root"] / "xx-test-new" / "metadata").mkdir(parents=True)
    (legacy["root"] / "xx-test-new" / "metadata" / "manifest.jsonl").write_text("{}\n")

    resp = _as_admin(client).post("/collections/", json={"name": "N", "project_id": legacy["proj"].id,
                                                         "filename_stem": "xx-test-new"})

    assert resp.status_code == 409, resp.text


@pytest.mark.unit
def test_a_writer_outside_the_routes_cannot_take_an_occupied_stem_directory(db_session, legacy):
    from app.models.collection import Collection, StemDirectoryCollisionError

    (legacy["root"] / "xx-test-new" / "x.txt").parent.mkdir(parents=True)
    (legacy["root"] / "xx-test-new" / "x.txt").write_text("x")
    db_session.add(Collection(name="N", project_id=legacy["proj"].id, filename_stem="xx-test-new"))

    with pytest.raises(StemDirectoryCollisionError):
        db_session.flush()
    db_session.rollback()


@pytest.mark.unit
def test_an_empty_stem_directory_is_free(client, db_session, legacy):
    for d in ("captures", "previews", "thumbnails", "metadata"):
        (legacy["root"] / "xx-test-b" / d).mkdir(parents=True)

    resp = _as_admin(client).patch(f"/collections/{legacy['u'].id}", json={"filename_stem": "xx-test-b"})

    assert resp.status_code == 200, resp.text
