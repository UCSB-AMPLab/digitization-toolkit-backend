"""A migrated volume carries its own fixity, and deleting a project never deletes a capture manifest.

When the startup migration moves a volume into `<stem>/`, it copies the
volume's capture and relocation records from the other manifests into
`<stem>/metadata/manifest.jsonl`, each marked `copied_from` and with its paths
rebased onto `<stem>/` so they name the same files. The project manifest is
left as it is. The fixity index reads both and gets the same checksums from
each, so nothing it answers changes; the bag takes one record per capture.

`DELETE /projects/{id}` moves the project's `metadata/manifest.jsonl` to
`<projects root>/.retired-manifests/<project dir>-<UTC timestamp>-manifest.jsonl`
before it removes the tree, and the index keeps reading it there.
"""

import hashlib
import json
from pathlib import Path

import pytest

from tests.unit.test_volume_layout_by_stem import (  # noqa: F401
    STEMS, _as_admin, _capture_records, capture_settings, data_dir, legacy,
)


def _lines(path: Path) -> list:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def _vol_manifest(L, key) -> Path:
    return L["root"] / STEMS[key] / "metadata" / "manifest.jsonl"


def _index_answers(images):
    from app.core import config
    from app.core.integrity import build_fixity_index

    index = build_fixity_index(config.settings.projects_dir.resolve())
    return {i.id: index.expected(i.capture_id, i.id, Path(i.file_path).resolve()) for i in images}


# ---------------------------------------------------------------------------
# (a) The migration copies a volume's records into its own manifest
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_each_migrated_volume_gets_copies_of_its_own_capture_records(db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout

    manifest = _capture_records(db_session, legacy)

    report = migrate_volume_layout(db_session)

    assert (legacy["root"] / "proj" / "metadata" / "manifest.jsonl").read_bytes() == manifest
    copies = {key: _lines(_vol_manifest(legacy, key)) for key in ("a1", "a2", "tomo")}
    assert sorted(l["capture_id"] for l in copies["a1"]) == ["cap-i1"]
    assert sorted(l["capture_id"] for l in copies["a2"]) == ["cap-i2", "cap-i3a", "cap-i3b"]
    assert sorted(l["capture_id"] for l in copies["tomo"]) == ["cap-i4"]
    for line in copies["a2"]:
        assert line["copied_from"] == "proj/metadata/manifest.jsonl"
    i2 = next(l for l in copies["a2"] if l["capture_id"] == "cap-i2")
    # Rebased onto <stem>/, naming the file where its capture record put it.
    assert i2["files"][0]["relative_path"] == "../proj/fondo_a/images/main/IMG_0002.jpg"
    assert i2["files"][0]["sha256"] == hashlib.sha256(b"bytes of IMG_0002.jpg").hexdigest()
    assert report["manifest_records_copied"] == 5


@pytest.mark.unit
def test_the_copies_change_no_answer_of_the_fixity_index(db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    _capture_records(db_session, legacy)
    migrate_volume_layout(db_session)
    db_session.expire_all()
    images = [db_session.get(RecordImage, legacy[k].id) for k in ("i1", "i2", "i3a", "i3b", "i4")]
    with_copies = _index_answers(images)
    for key in ("a1", "a2", "tomo"):
        _vol_manifest(legacy, key).unlink()

    assert _index_answers(images) == with_copies
    assert all(with_copies.values())


@pytest.mark.unit
def test_a_migrated_volume_keeps_its_fixity_without_the_project_manifest(db_session, legacy):
    from app.core.integrity import verify_images_against_manifest
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    _capture_records(db_session, legacy)
    migrate_volume_layout(db_session)
    (legacy["root"] / "proj" / "metadata" / "manifest.jsonl").unlink()
    db_session.expire_all()
    images = [db_session.get(RecordImage, legacy[k].id) for k in ("i1", "i2", "i3a", "i3b", "i4")]

    assert verify_images_against_manifest(images) == []
    Path(images[1].file_path).write_bytes(b"bit rot")
    assert [(m["record_image_id"], m["reason"]) for m in verify_images_against_manifest(images)] == [
        (images[1].id, "mismatch")]


@pytest.mark.unit
def test_a_relocation_record_of_an_earlier_rename_is_copied_too(db_session, legacy):
    """A renumber before stems renamed the master; its relocation record names the image and its sha256."""
    import os
    from app.core.integrity import verify_images_against_manifest
    from app.core.storage_ops import renumber_collection_images
    from app.core.volume_layout import migrate_volume_layout
    from app.models.collection import Collection
    from app.models.record import RecordImage

    _capture_records(db_session, legacy)
    renumber_collection_images(db_session, db_session.get(Collection, legacy["a2"].id))
    db_session.commit()
    migrate_volume_layout(db_session)
    (legacy["root"] / "proj" / "metadata" / "manifest.jsonl").unlink()
    db_session.expire_all()
    img = db_session.get(RecordImage, legacy["i2"].id)
    assert Path(img.file_path).name != "IMG_0002.jpg"

    relocations = [l for l in _lines(_vol_manifest(legacy, "a2")) if l.get("record_type") == "relocation"]
    assert legacy["i2"].id in [r["image_id"] for r in relocations]
    assert verify_images_against_manifest([img]) == []
    Path(img.file_path).write_bytes(b"bit rot")
    assert [m["record_image_id"] for m in verify_images_against_manifest([img])] == [img.id]


@pytest.mark.unit
def test_a_replayed_batch_and_a_second_run_copy_nothing_twice(db_session, legacy, monkeypatch):
    import app.core.volume_layout as vl

    _capture_records(db_session, legacy)
    real_move = vl._move
    count = {"n": 0}

    class Crash(BaseException):
        pass

    def crashing(src, dst):
        count["n"] += 1
        if count["n"] == 2:
            raise Crash()
        real_move(src, dst)

    monkeypatch.setattr(vl, "_move", crashing)
    with pytest.raises(Crash):
        vl.migrate_volume_layout(db_session)
    db_session.rollback()
    monkeypatch.setattr(vl, "_move", real_move)
    vl.migrate_volume_layout(db_session)
    vl.migrate_volume_layout(db_session)

    for key in ("a1", "a2", "tomo"):
        raw = _vol_manifest(legacy, key).read_text().splitlines()
        assert len(raw) == len(set(raw)), key


@pytest.mark.unit
def test_a_dry_run_copies_nothing(db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout

    _capture_records(db_session, legacy)

    report = migrate_volume_layout(db_session, dry_run=True)

    assert not any(_vol_manifest(legacy, k).exists() for k in ("a1", "a2", "tomo"))
    assert report["manifest_records_copied"] == 5


@pytest.mark.unit
def test_the_bag_takes_one_record_per_capture(db_session, legacy):
    from app.core.export_service import _filtered_manifest_blob
    from app.core.volume_layout import migrate_volume_layout
    from app.models.collection import Collection
    from app.models.record import Record

    manifest = _capture_records(db_session, legacy)
    migrate_volume_layout(db_session)
    collection = db_session.get(Collection, legacy["a2"].id)
    records = db_session.query(Record).filter(Record.collection_id == collection.id).all()

    lines = _filtered_manifest_blob(db_session, collection, records).decode().splitlines()

    assert sorted(json.loads(l)["capture_id"] for l in lines) == ["cap-i2", "cap-i3a", "cap-i3b"]
    # The capture-time record itself, not its copy.
    originals = set(manifest.decode().splitlines())
    assert all(l in originals for l in lines)


# ---------------------------------------------------------------------------
# (b) Deleting a project retires its manifest
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_deleting_a_project_retires_its_manifest_and_the_index_keeps_reading_it(
        client, db_session, legacy, override_projects_root):
    """The volumes move to another project, the empty project is deleted, and their fixity holds."""
    from app.core.integrity import verify_images_against_manifest
    from app.core.volume_layout import migrate_volume_layout
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    manifest = _capture_records(db_session, legacy)
    migrate_volume_layout(db_session)
    for key in ("a1", "a2", "tomo"):
        _vol_manifest(legacy, key).unlink()  # only the project manifest holds the checksums now
    other = Project(name="Otro")
    db_session.add(other)
    db_session.commit()
    api = _as_admin(client)
    assert api.post(f"/projects/{legacy['proj'].id}/move-collections",
                    json={"target_project_id": other.id}).status_code == 200
    for r in db_session.query(Record).filter(Record.project_id == legacy["proj"].id).all():
        for i in r.images:
            db_session.delete(i)
        db_session.delete(r)
    db_session.commit()

    resp = api.delete(f"/projects/{legacy['proj'].id}")

    assert resp.status_code == 200, resp.text
    assert not (override_projects_root / "proj").exists()
    retired = list((override_projects_root / ".retired-manifests").iterdir())
    assert len(retired) == 1
    assert retired[0].name.startswith("proj-") and retired[0].name.endswith("Z-manifest.jsonl")
    assert retired[0].read_bytes() == manifest
    db_session.expire_all()
    images = [db_session.get(RecordImage, legacy[k].id) for k in ("i1", "i2", "i3a", "i3b", "i4")]
    assert verify_images_against_manifest(images) == []
    Path(images[0].file_path).write_bytes(b"bit rot")
    assert [m["record_image_id"] for m in verify_images_against_manifest(images)] == [images[0].id]


@pytest.mark.unit
def test_deleting_a_project_with_no_manifest_retires_nothing(client, db_session, capture_settings,
                                                            override_projects_root):
    from app.models.project import Project

    p = Project(name="Vacio")
    db_session.add(p)
    db_session.commit()
    (override_projects_root / "vacio" / "packages").mkdir(parents=True)

    resp = _as_admin(client).delete(f"/projects/{p.id}")

    assert resp.status_code == 200, resp.text
    assert not (override_projects_root / "vacio").exists()
    assert not (override_projects_root / ".retired-manifests").exists()


@pytest.mark.unit
def test_two_retired_manifests_of_one_name_in_one_second_do_not_overwrite_each_other(
        capture_settings, override_projects_root, monkeypatch):
    import app.core.storage_ops as so

    monkeypatch.setattr(so, "_utc_stamp", lambda: "20261023T120000Z")
    for n in (1, 2):
        m = override_projects_root / "p" / "metadata" / "manifest.jsonl"
        m.parent.mkdir(parents=True, exist_ok=True)
        m.write_text(f'{{"n": {n}}}\n')
        so.retire_project_manifest(override_projects_root / "p")

    names = sorted(p.name for p in (override_projects_root / ".retired-manifests").iterdir())
    assert names == ["p-20261023T120000Z-2-manifest.jsonl", "p-20261023T120000Z-manifest.jsonl"]
