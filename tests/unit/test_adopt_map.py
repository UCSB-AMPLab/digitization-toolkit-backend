"""Adoption of objects from a rename map (contract 1.1, kind "rename-map").

The bucket is a real rclone remote, as in tests/unit/test_image_upload.py:
the same wrapper in front of the real binary, here behind an alias remote so
that CAPTUA_OFFSITE_REMOTE names a bucket (`tb2:<bucket>`) as it does on a unit.
The maps are synthetic.

    python -m pytest tests/unit/test_adopt_map.py
"""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from tests.unit.test_image_upload import STEM, TENANT, Remote, _image, _rows, _volume, remote  # noqa: F401

BUCKET = "captua-test-masters"


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


@pytest.fixture
def masters(remote, tmp_path, monkeypatch):
    """CAPTUA_OFFSITE_REMOTE as `tb2:<BUCKET>`, an alias remote over a local directory."""
    from app.core import config

    buckets = tmp_path / "buckets"
    (buckets / BUCKET).mkdir(parents=True)
    conf = tmp_path / "rclone-alias.conf"
    conf.write_text(f"[tb2]\ntype = alias\nremote = {buckets}\n")
    monkeypatch.setattr(config.settings, "OFFSITE_REMOTE", f"tb2:{BUCKET}")
    monkeypatch.setattr(config.settings, "RCLONE_CONFIG", str(conf))
    return Remote(buckets / BUCKET, remote.ctl)


def _dest(n: int, kind: str = "capture", suffix: str = "cr2") -> str:
    if kind == "capture":
        return f"{STEM}/captures/{STEM}-img_{n:04d}_c0.{suffix}"
    return f"{STEM}/previews/{STEM}-img_{n:04d}_c0_preview.jpg"


def _entry(img, kind: str, dest: str, data: bytes, **over) -> dict:
    master = Path(img.file_path)
    name = master.name if kind == "capture" else f"{master.stem}_preview.jpg"
    entry = {
        "captureId": img.capture_id, "capturedAt": "2026-10-08T20:55:29+00:00", "ordinal": 1,
        "camera": 0, "role": "left", "imageId": img.id, "recordId": img.record_id,
        "superseded": None, "kind": kind, "originalFilename": name,
        "sourceKey": f"projects/p/tomo/images/main/{name}", "destinationKey": dest,
        "sha256": hashlib.sha256(data).hexdigest(), "sha1": _sha1(data), "bytes": len(data),
    }
    entry.update(over)
    return entry


def _map(vol, entries, **over) -> dict:
    data = {
        "contract": "fisqua-captua-exchange", "version": "1.1", "kind": "rename-map",
        "id": "00000000-0000-4000-8000-000000000001", "generatedAt": "2026-10-24T06:42:40Z",
        "generatedBy": {"system": "captua", "instance": TENANT, "systemVersion": "0.1.0-dev",
                        "catalogueRevision": "000000000000", "catalogueDumpedAt": "2026-10-23"},
        "volume": {"externalId": vol.filename_stem, "stem": vol.filename_stem,
                   "captuaCollectionId": vol.id, "counterAfter": len(entries)},
        "source": {"bucket": "captua-test-unit"},
        "destination": {"bucket": BUCKET, "root": f"{TENANT}/"},
        "entries": entries,
    }
    for name, value in over.items():
        data[name] = value
    return data


def _adopt(db, rename_map, apply=False):
    from app.core.image_upload import adopt_rename_map

    return adopt_rename_map(db, rename_map, apply=apply)


def _reasons(report):
    return {(r["image_id"], r["kind"], r["reason"]) for r in report["refused"]}


def _raw_volume(db, root, masters):
    """A volume with one RAW image (master and preview) on the card and in the bucket under contract names."""
    vol = _volume(db)
    img = _image(db, vol, root, "IMG_0001.CR2", b"raw one", preview=b"preview one")
    masters.put(f"{TENANT}/{_dest(1)}", b"raw one")
    masters.put(f"{TENANT}/{_dest(1, 'preview')}", b"preview one")
    entries = [_entry(img, "capture", _dest(1), b"raw one"),
               _entry(img, "preview", _dest(1, "preview"), b"preview one")]
    return vol, img, entries


@pytest.mark.unit
def test_a_present_file_is_adopted_under_the_maps_key(masters, db_session, override_projects_root):
    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    before = masters.files()

    report = _adopt(db_session, _map(vol, entries), apply=True)

    assert report["applied"] is True and report["refused"] == []
    assert {(a["image_id"], a["kind"], a["key"], a["local"]) for a in report["adopted"]} == {
        (img.id, "capture", f"{TENANT}/{_dest(1)}", "present"),
        (img.id, "preview", f"{TENANT}/{_dest(1, 'preview')}", "present"),
    }
    assert report["adopted_present"] == 2 and report["adopted_absent"] == 0
    rows = _rows(db_session)
    assert rows[(img.id, "capture")].object_key == f"{TENANT}/{_dest(1)}"
    for row in rows.values():
        assert row.verified_at is not None and row.uploaded_at is None
    assert rows[(img.id, "capture")].local_sha1 == _sha1(b"raw one")
    assert masters.calls("copyto") == [] and masters.files() == before


@pytest.mark.unit
def test_an_absent_file_is_adopted_and_mark_freed_then_marks_it(masters, db_session, override_projects_root):
    from app.core.offsite_free import mark_freed
    from app.models.record import RecordImage

    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    Path(img.file_path).unlink()  # the master was removed from the card; its preview is still there

    before = mark_freed(db_session, STEM)
    assert [(u["image_id"], u["reason"]) for u in before["unaccounted"]] == [(img.id, "no_verified_object")]

    report = _adopt(db_session, _map(vol, entries), apply=True)
    assert {(a["kind"], a["local"]) for a in report["adopted"]} == {("capture", "absent"), ("preview", "present")}
    assert report["adopted_absent"] == 1 and report["adopted_present"] == 1

    freed = mark_freed(db_session, STEM, apply=True)
    assert [(m["image_id"], m["kind"]) for m in freed["marked"]] == [(img.id, "capture")]
    assert freed["unaccounted"] == []
    db_session.expire_all()
    assert db_session.get(RecordImage, img.id).master_freed_at is not None


@pytest.mark.unit
def test_the_dry_run_writes_nothing(masters, db_session, override_projects_root):
    from app.models.system_log import SystemLog

    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    report = _adopt(db_session, _map(vol, entries))
    assert report["applied"] is False and len(report["adopted"]) == 2
    assert _rows(db_session) == {}
    assert db_session.query(SystemLog).filter(SystemLog.action == "image_upload_adopted").count() == 0


@pytest.mark.unit
def test_apply_logs_the_adoption(masters, db_session, override_projects_root):
    from app.models.system_log import SystemLog

    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    _adopt(db_session, _map(vol, entries), apply=True)
    events = db_session.query(SystemLog).filter(SystemLog.action == "image_upload_adopted").all()
    assert len(events) == 1 and events[0].subject == STEM
    assert "rename-map" in events[0].detail and "adopted=2" in events[0].detail


@pytest.mark.unit
def test_a_listing_with_another_sha1_or_no_object_records_nothing(masters, db_session, override_projects_root):
    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    masters.put(f"{TENANT}/{_dest(1)}", b"other bytes")
    (masters.root / TENANT / _dest(1, "preview")).unlink()

    report = _adopt(db_session, _map(vol, entries), apply=True)

    assert _reasons(report) == {(img.id, "capture", "bucket_sha1_differs"),
                                (img.id, "preview", "not_in_bucket")}
    assert report["adopted"] == [] and _rows(db_session) == {}


@pytest.mark.unit
def test_a_listed_size_that_differs_records_nothing(masters, db_session, override_projects_root):
    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    entries[0]["bytes"] = 999
    report = _adopt(db_session, _map(vol, entries), apply=True)
    assert _reasons(report) == {(img.id, "capture", "bucket_size_differs")}
    assert set(_rows(db_session)) == {(img.id, "preview")}


@pytest.mark.unit
def test_a_local_file_whose_sha1_differs_records_nothing(masters, db_session, override_projects_root):
    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    Path(img.file_path).write_bytes(b"changed on the card")

    report = _adopt(db_session, _map(vol, entries), apply=True)

    assert _reasons(report) == {(img.id, "capture", "local_sha1_differs")}
    assert set(_rows(db_session)) == {(img.id, "preview")}


@pytest.mark.unit
def test_a_file_name_that_differs_records_nothing(masters, db_session, override_projects_root):
    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    entries[0]["originalFilename"] = "IMG_0002.CR2"
    entries[1]["originalFilename"] = "IMG_0001.CR2"  # the master's name, not the preview's

    report = _adopt(db_session, _map(vol, entries), apply=True)

    assert _reasons(report) == {(img.id, "capture", "filename_differs"),
                                (img.id, "preview", "filename_differs")}
    assert _rows(db_session) == {}


@pytest.mark.unit
def test_an_image_of_another_record_or_volume_records_nothing(masters, db_session, override_projects_root):
    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    other_vol = _volume(db_session, name="Tomo 2", stem="xx-test-002")
    stranger = _image(db_session, other_vol, override_projects_root, "IMG_0009.jpg", b"jpeg nine")
    sibling = _image(db_session, vol, override_projects_root, "IMG_0003.jpg", b"jpeg three")
    for n, data in ((2, b"jpeg nine"), (3, b"jpeg three")):
        masters.put(f"{TENANT}/{_dest(n, suffix='jpg')}", data)
    wrong_record = _entry(sibling, "capture", _dest(3, suffix="jpg"), b"jpeg three", recordId=img.record_id)
    missing = _entry(sibling, "capture", _dest(4, suffix="jpg"), b"x", imageId=999999)
    entries += [_entry(stranger, "capture", _dest(2, suffix="jpg"), b"jpeg nine"), wrong_record, missing]

    report = _adopt(db_session, _map(vol, entries), apply=True)

    assert _reasons(report) == {(stranger.id, "capture", "not_in_volume"),
                                (sibling.id, "capture", "record_differs"),
                                (999999, "capture", "image_not_found")}
    assert set(_rows(db_session)) == {(img.id, "capture"), (img.id, "preview")}


@pytest.mark.unit
@pytest.mark.parametrize("dest", [
    "xx-test-002/captures/IMG_0001.CR2",    # another volume's stem
    "../xx-test-001/captures/IMG_0001.CR2",  # a climb out of the root
    "/xx-test-001/captures/IMG_0001.CR2",   # an absolute key
    "xx-test-001",                           # the stem itself, not under it
])
def test_a_key_outside_the_tenant_and_stem_records_nothing(masters, db_session, override_projects_root, dest):
    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    entries[0]["destinationKey"] = dest
    report = _adopt(db_session, _map(vol, entries), apply=True)
    assert _reasons(report) == {(img.id, "capture", "key_outside_volume")}
    assert set(_rows(db_session)) == {(img.id, "preview")}


@pytest.mark.unit
def test_a_root_without_its_trailing_slash_is_normalised(masters, db_session, override_projects_root):
    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    report = _adopt(db_session, _map(vol, entries, destination={"bucket": BUCKET, "root": TENANT}), apply=True)
    assert report["key_prefix"] == f"{TENANT}/{STEM}/" and len(report["adopted"]) == 2


@pytest.mark.unit
def test_a_bucket_that_is_not_the_configured_remotes_refuses_the_map(masters, db_session, override_projects_root):
    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    with pytest.raises(ValueError, match="bucket"):
        _adopt(db_session, _map(vol, entries, destination={"bucket": "another-bucket", "root": f"{TENANT}/"}),
               apply=True)
    assert _rows(db_session) == {} and masters.calls("lsjson") == []


@pytest.mark.unit
@pytest.mark.parametrize("remote_value", ["tb2:", "tb2:captua-test-masters/sub", "/some/local/dir"])
def test_a_remote_that_names_no_bucket_or_a_path_inside_one_refuses_the_map(
        masters, db_session, override_projects_root, monkeypatch, remote_value):
    from app.core import config

    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    monkeypatch.setattr(config.settings, "OFFSITE_REMOTE", remote_value)
    with pytest.raises(ValueError, match="CAPTUA_OFFSITE_REMOTE"):
        _adopt(db_session, _map(vol, entries), apply=True)
    assert _rows(db_session) == {}


@pytest.mark.unit
def test_a_root_under_another_tenant_refuses_the_map(masters, db_session, override_projects_root):
    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    with pytest.raises(ValueError, match="tenant"):
        _adopt(db_session, _map(vol, entries, destination={"bucket": BUCKET, "root": "ahr/"}), apply=True)
    assert _rows(db_session) == {}


@pytest.mark.unit
def test_an_entry_already_recorded_is_reported_and_left(masters, db_session, override_projects_root):
    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    _adopt(db_session, _map(vol, entries), apply=True)
    keys = {k: r.object_key for k, r in _rows(db_session).items()}

    report = _adopt(db_session, _map(vol, entries), apply=True)

    assert report["adopted"] == [] and report["refused"] == []
    assert {(a["image_id"], a["kind"]) for a in report["already_recorded"]} == {
        (img.id, "capture"), (img.id, "preview")}
    assert {k: r.object_key for k, r in _rows(db_session).items()} == keys


@pytest.mark.unit
def test_an_image_recorded_at_another_key_is_reported_and_left(masters, db_session, override_projects_root):
    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    other = copy.deepcopy(entries)
    other[0]["destinationKey"] = _dest(7)
    masters.put(f"{TENANT}/{_dest(7)}", b"raw one")
    _adopt(db_session, _map(vol, other[:1]), apply=True)

    report = _adopt(db_session, _map(vol, entries), apply=True)

    assert _reasons(report) == {(img.id, "capture", "key_differs")}
    assert _rows(db_session)[(img.id, "capture")].object_key == f"{TENANT}/{_dest(7)}"


@pytest.mark.unit
def test_a_key_another_image_holds_is_reported(masters, db_session, override_projects_root):
    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    twin = _image(db_session, vol, override_projects_root, "IMG_0002.CR2", b"raw one")
    _adopt(db_session, _map(vol, entries[:1]), apply=True)

    report = _adopt(db_session, _map(vol, [_entry(twin, "capture", _dest(1), b"raw one")]), apply=True)

    assert _reasons(report) == {(twin.id, "capture", "key_already_held")}
    assert (twin.id, "capture") not in _rows(db_session)


@pytest.mark.unit
def test_two_entries_for_one_object_or_one_key_are_both_refused(masters, db_session, override_projects_root):
    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    report = _adopt(db_session, _map(vol, entries + [copy.deepcopy(entries[0])]), apply=True)
    assert [r["reason"] for r in report["refused"]] == ["duplicate_entry", "duplicate_entry"]
    assert set(_rows(db_session)) == {(img.id, "preview")}


@pytest.mark.unit
@pytest.mark.parametrize("field,value", [
    ("contract", "something-else"), ("version", "1.0"), ("kind", "capture-manifest"),
])
def test_a_bad_contract_header_refuses_the_map(masters, db_session, override_projects_root, field, value):
    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    with pytest.raises(ValueError):
        _adopt(db_session, _map(vol, entries, **{field: value}), apply=True)
    assert _rows(db_session) == {}


@pytest.mark.unit
def test_a_volume_that_is_not_this_units_refuses_the_map(masters, db_session, override_projects_root):
    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    for volume in ({"stem": STEM, "captuaCollectionId": vol.id + 1},
                   {"stem": "xx-test-999", "captuaCollectionId": vol.id}):
        with pytest.raises(ValueError, match="volume"):
            _adopt(db_session, _map(vol, entries, volume=volume), apply=True)
    assert _rows(db_session) == {}


@pytest.mark.unit
def test_a_malformed_entry_is_refused_and_the_rest_adopted(masters, db_session, override_projects_root):
    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    entries[1]["sha1"] = "not-a-sha1"
    report = _adopt(db_session, _map(vol, entries), apply=True)
    assert _reasons(report) == {(img.id, "preview", "malformed_entry")}
    assert set(_rows(db_session)) == {(img.id, "capture")}


@pytest.mark.unit
@pytest.mark.parametrize("kind", [["preview"], {"preview": 1}])
def test_a_kind_that_is_not_a_string_is_a_malformed_entry(masters, db_session, override_projects_root, kind):
    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    entries[1]["kind"] = kind
    report = _adopt(db_session, _map(vol, entries), apply=True)
    assert [(r["image_id"], r["reason"]) for r in report["refused"]] == [(img.id, "malformed_entry")]
    assert set(_rows(db_session)) == {(img.id, "capture")}


@pytest.mark.unit
def test_the_adopt_map_command_reports_as_json(masters, db_session, override_projects_root, capsys, tmp_path):
    from app.core.image_upload import main

    vol, img, entries = _raw_volume(db_session, override_projects_root, masters)
    path = tmp_path / "rename-map.json"
    path.write_text(json.dumps(_map(vol, entries)))

    assert main(["adopt-map", str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is False and len(out["adopted"]) == 2
    assert _rows(db_session) == {}

    assert main(["adopt-map", str(path), "--apply"]) == 0
    assert len(_rows(db_session)) == 2

    path.write_text(json.dumps(_map(vol, entries, version="1.0")))
    assert main(["adopt-map", str(path)]) == 2
    assert capsys.readouterr().err.startswith("error: ")
    assert main(["adopt-map", str(tmp_path / "missing.json")]) == 2
