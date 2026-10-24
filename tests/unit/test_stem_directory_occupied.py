"""A volume's files are never merged into a `<stem>/` that already holds something else.

The startup migration skips a volume whose `<stem>/` holds anything but the
four directories and, in them, files of that volume's rows (its masters, their
previews, its thumbnails, the names a restored row's moved file may have, and
a manifest of its own captures), reporting it as `stem_dir_occupied` with the
foreign entries. Setting a stem is refused with 409 when `<stem>/` exists on
disk with a file in it and the volume has no image there.

`metadata/manifest.jsonl` is never foreign. It is append-only, so it keeps
the records of captures deleted since, of records moved out, and of copies
naming other volumes' directories; and a stray one merged into does no harm,
because the index, the fixity gate and the bag select records by the capture
ids of the volume's rows.

The first test is the verifier's (round 4); the deleted-capture and
moved-out tests are the verifier's (round 5); the two-hop and merged-manifest
tests are the verifier's (round 6).
"""

import hashlib
import io
import json
from pathlib import Path

import pytest

from tests.unit.test_volume_layout_by_stem import (  # noqa: F401
    STEMS, _approve, _as_admin, _capture_records, capture_settings, data_dir, legacy,
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
    # The manifest's record names no path, so it is not foreign by itself; the tree beside it is.
    assert sorted(skipped[0]["foreign"]) == ["fondo_z"]
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


# ---------------------------------------------------------------------------
# The volume's own manifest
# ---------------------------------------------------------------------------

def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format="JPEG")
    return buf.getvalue()


def _new_layout_capture(db, root, collection_id, stem, name, cid):
    from app.models.record import Record, RecordImage

    vol = root / stem
    for d in ("captures", "previews", "thumbnails", "metadata"):
        (vol / d).mkdir(parents=True, exist_ok=True)
    f = vol / "captures" / name
    f.write_bytes(_jpeg())
    with (vol / "metadata" / "manifest.jsonl").open("a") as m:
        m.write(json.dumps({"capture_id": cid, "files": [{"relative_path": f"captures/{name}", "sha256": _sha(f)}]}) + "\n")
    rec = Record(title="n", collection_id=collection_id, status="captured", capture_mode="single")
    db.add(rec)
    db.commit()
    img = RecordImage(record_id=rec.id, filename=name, file_path=str(f), format="jpg", role="single", capture_id=cid)
    db.add(img)
    db.commit()
    return img



@pytest.mark.unit
def test_a_deleted_new_layout_capture_does_not_strand_the_volume_in_the_old_layout(client, db_session, legacy):
    """Stem set while running; one capture into <stem>/, deleted as a bad shot; then the startup."""
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    r = _as_admin(client).patch(f"/collections/{legacy['u'].id}", json={"filename_stem": "xx-test-u"})
    assert r.status_code == 200, r.text
    img = _new_layout_capture(db_session, legacy["root"], legacy["u"].id, "xx-test-u", "BAD_c0.jpg", "cap-bad")
    r = _as_admin(client).delete(f"/records/images/{img.id}")
    assert r.status_code in (200, 204), r.text

    report = migrate_volume_layout(db_session)

    skipped = [(s["reason"], s.get("foreign")) for s in report["skipped"] if s["image_id"] == legacy["i9"].id]
    db_session.expire_all()
    fp = db_session.get(RecordImage, legacy["i9"].id).file_path
    assert skipped == []
    assert Path(fp).parent == (legacy["root"] / "xx-test-u" / "captures").resolve()


@pytest.mark.unit
def test_a_volume_whose_records_moved_out_still_takes_a_reassigned_record_at_the_next_start(client, db_session, legacy):
    """a2 migrated with its capture records; its records moved to a1; a record of u reassigned to a2; restart."""
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    _capture_records(db_session, legacy)
    migrate_volume_layout(db_session)
    r = _as_admin(client).post(f"/collections/{legacy['a2'].id}/move-records",
                               params={"target_collection_id": legacy["a1"].id})
    assert r.status_code == 200, r.text
    rec9 = db_session.get(RecordImage, legacy["i9"].id).record_id
    r = _as_admin(client).patch(f"/records/{rec9}", json={"collection_id": legacy["a2"].id})
    assert r.status_code == 200, r.text

    report = migrate_volume_layout(db_session)

    skipped = [(s["reason"], s.get("foreign")) for s in report["skipped"] if s["image_id"] == legacy["i9"].id]
    db_session.expire_all()
    fp = db_session.get(RecordImage, legacy["i9"].id).file_path
    assert skipped == []
    assert Path(fp).parent == (legacy["root"] / STEMS["a2"] / "captures").resolve()


def _a1_manifest(L, lines):
    meta = L["root"] / STEMS["a1"] / "metadata"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "manifest.jsonl").write_text("".join(l + "\n" for l in lines))


@pytest.mark.unit
def test_a_manifest_whose_records_point_into_another_volumes_directory_is_not_foreign(db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout

    _a1_manifest(legacy, [json.dumps({"capture_id": "cap-z", "files": [
        {"relative_path": f"../{STEMS['a2']}/captures/z.jpg", "sha256": "0" * 64}]})])

    report = migrate_volume_layout(db_session)

    assert [m for m in report["moved"] if m["collection_id"] == legacy["a1"].id]
    assert not [s for s in report["skipped"] if s["image_id"] == legacy["i1"].id]


@pytest.mark.unit
def test_a_relocation_record_pointing_into_another_volumes_directory_is_not_foreign(db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout

    _a1_manifest(legacy, [json.dumps({"record_type": "relocation", "capture_id": "cap-z", "image_id": 999,
                                      "sha256": "0" * 64, "from": f"../{STEMS['tomo']}/captures/a.jpg",
                                      "to": f"../{STEMS['tomo']}/captures/b.jpg"})])

    report = migrate_volume_layout(db_session)

    assert [m for m in report["moved"] if m["collection_id"] == legacy["a1"].id]
    assert not [s for s in report["skipped"] if s["image_id"] == legacy["i1"].id]


@pytest.mark.unit
def test_a_record_of_this_volume_pointing_into_another_volumes_directory_is_not_foreign(db_session, legacy):
    """A copy of a record moved in from another volume names that volume's directory; its capture id is ours."""
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    img = db_session.get(RecordImage, legacy["i1"].id)
    img.capture_id = "cap-ours"
    db_session.commit()
    _a1_manifest(legacy, [json.dumps({"capture_id": "cap-ours", "copied_from": f"{STEMS['a2']}/metadata/manifest.jsonl",
                                      "files": [{"relative_path": f"../{STEMS['a2']}/captures/IMG_0001.CR2",
                                                 "sha256": "0" * 64}]})])

    report = migrate_volume_layout(db_session)

    assert [m for m in report["moved"] if m["collection_id"] == legacy["a1"].id]
    assert not [s for s in report["skipped"] if s["reason"] == "stem_dir_occupied"]


@pytest.mark.unit
def test_records_of_no_volume_pointing_at_a_project_or_at_this_stem_are_not_foreign(db_session, legacy):
    """Deleted captures, and copies rebased onto the project tree they came from."""
    from app.core.volume_layout import migrate_volume_layout

    _a1_manifest(legacy, [
        json.dumps({"capture_id": "cap-gone", "files": [{"relative_path": "captures/GONE.jpg", "sha256": "0" * 64}]}),
        json.dumps({"capture_id": "cap-old", "copied_from": "proj/metadata/manifest.jsonl",
                    "files": [{"relative_path": "../proj/fondo_a/images/main/OLD.jpg", "sha256": "0" * 64}]}),
        json.dumps({"capture_id": "cap-nopath", "files": [{"sha256": "0" * 64}]}),
        json.dumps({"capture_id": "cap-badpath", "files": [{"relative_path": 7}]}),
    ])

    report = migrate_volume_layout(db_session)

    assert [m for m in report["moved"] if m["collection_id"] == legacy["a1"].id]
    assert not [s for s in report["skipped"] if s["reason"] == "stem_dir_occupied"]


@pytest.mark.unit
def test_a_manifest_whose_only_odd_records_are_unparsable_lines_is_not_foreign(db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout

    _a1_manifest(legacy, ['{"capture_id": "cap-z", "files": [{"relative_path": "../xx-te', "not json at all", "[1, 2"])

    report = migrate_volume_layout(db_session)

    assert [m for m in report["moved"] if m["collection_id"] == legacy["a1"].id]
    assert not [s for s in report["skipped"] if s["reason"] == "stem_dir_occupied"]


def _capture_into(db, root, collection_id, stem, name, cid):
    from app.models.record import Record, RecordImage

    vol = root / stem
    for d in ("captures", "previews", "thumbnails", "metadata"):
        (vol / d).mkdir(parents=True, exist_ok=True)
    f = vol / "captures" / name
    f.write_bytes(_jpeg())
    with (vol / "metadata" / "manifest.jsonl").open("a") as m:
        m.write(json.dumps({"capture_id": cid, "files": [{"relative_path": f"captures/{name}", "sha256": _sha(f)}]}) + "\n")
    rec = Record(title="n", collection_id=collection_id, status="captured", capture_mode="single")
    db.add(rec)
    db.commit()
    img = RecordImage(record_id=rec.id, filename=name, file_path=str(f), format="jpg", role="single", capture_id=cid)
    db.add(img)
    db.commit()
    return img


@pytest.mark.unit
def test_a_record_that_passed_through_a_volume_twice_does_not_strand_it(client, db_session, legacy):
    """A capture in a2; its record moved to a1 (a1's repair copies it, naming a2's <stem>/); then moved on to tomo;
    then a record of u reassigned into a1; restart."""
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    _capture_records(db_session, legacy)
    migrate_volume_layout(db_session)
    _capture_into(db_session, legacy["root"], legacy["a2"].id, STEMS["a2"], "NEW_c0.jpg", "cap-new")
    c = _as_admin(client)
    assert c.post(f"/collections/{legacy['a2'].id}/move-records", params={"target_collection_id": legacy["a1"].id}).status_code == 200
    r1 = migrate_volume_layout(db_session)  # a restart: a1's repair copies cap-new's record
    a1_manifest = (legacy["root"] / STEMS["a1"] / "metadata" / "manifest.jsonl").read_text()
    assert c.post(f"/collections/{legacy['a1'].id}/move-records", params={"target_collection_id": legacy["tomo"].id}).status_code == 200
    rec9 = db_session.get(RecordImage, legacy["i9"].id).record_id
    assert c.patch(f"/records/{rec9}", json={"collection_id": legacy["a1"].id}).status_code == 200

    report = migrate_volume_layout(db_session)

    skipped = [(s["reason"], s.get("foreign")) for s in report["skipped"] if s["image_id"] == legacy["i9"].id]
    db_session.expire_all()
    fp = db_session.get(RecordImage, legacy["i9"].id).file_path
    assert skipped == []
    assert Path(fp).parent == (legacy["root"] / STEMS["a1"] / "captures").resolve()


@pytest.mark.unit
def test_a_stray_manifest_merged_into_does_no_harm(client, db_session, legacy):
    """<stem>/metadata/manifest.jsonl from elsewhere (another unit's drive, a restored copy): stem-relative paths,
    capture ids no row holds. Not evidence, so the volume is moved in. Fixity, bag and integrity afterwards."""
    from app.core.export_jobs import ExportJob
    from app.core.export_service import _do_export
    from app.core.integrity import run_integrity_check, verify_images_against_manifest
    from app.core import config
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import Record, RecordImage
    import zipfile

    _capture_records(db_session, legacy)
    meta = legacy["root"] / STEMS["a2"] / "metadata"
    meta.mkdir(parents=True)
    (meta / "manifest.jsonl").write_text("\n".join(json.dumps(
        {"capture_id": f"other-unit-{n}", "files": [{"relative_path": f"captures/IMG_000{n}.jpg", "sha256": "f" * 64}]})
        for n in (2, 3)) + "\n")

    report = migrate_volume_layout(db_session)
    moved = sorted(m["image_id"] for m in report["moved"] if m["collection_id"] == legacy["a2"].id)
    db_session.expire_all()
    imgs = db_session.query(RecordImage).join(Record).filter(Record.collection_id == legacy["a2"].id).all()
    gate = verify_images_against_manifest(imgs)
    result = run_integrity_check(db_session, verify_hashes=True)
    _approve(db_session, legacy["a2"].id)
    job = ExportJob(legacy["a2"].id)
    _do_export(db_session, job, legacy["a2"].id)
    with zipfile.ZipFile(config.settings.exports_dir / job.zip_filename) as z:
        names = z.namelist()
        bag = [json.loads(l)["capture_id"] for l in z.read(next(n for n in names if n.endswith("data/metadata/manifest.jsonl"))).decode().splitlines()]
    assert moved == sorted(legacy[k].id for k in ("i2", "i3a", "i3b"))
    assert gate == [] and result["manifest_mismatches"] == []
    assert sorted(bag) == ["cap-i2", "cap-i3a", "cap-i3b"]


@pytest.mark.unit
def test_a_stray_tree_with_an_unowned_file_in_captures_still_skips(db_session, legacy):
    """A stray manifest is not foreign, but the stray tree's capture beside it is."""
    from app.core.volume_layout import migrate_volume_layout

    vol = legacy["root"] / STEMS["a2"]
    (vol / "metadata").mkdir(parents=True)
    (vol / "metadata" / "manifest.jsonl").write_text(json.dumps(
        {"capture_id": "other-unit-9", "files": [{"relative_path": "captures/OTHER_0009.jpg", "sha256": "f" * 64}]}) + "\n")
    (vol / "captures").mkdir()
    (vol / "captures" / "OTHER_0009.jpg").write_bytes(b"another unit's page")

    report = migrate_volume_layout(db_session)

    assert not [m for m in report["moved"] if m["collection_id"] == legacy["a2"].id]
    assert {tuple(s["foreign"]) for s in report["skipped"] if s["reason"] == "stem_dir_occupied"} == {
        ("captures/OTHER_0009.jpg",)}
