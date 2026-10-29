"""`python -m app.core.rebuild_volume <stem> <bundle> [--apply]`: a volume rebuilt from JPGs captured elsewhere.

The masters are already in the bucket under contract names and never come to
the card; the bundle carries each image's facts (images.jsonl), its name,
capture id, date and rotation (plan.json), and a ready thumbnail. The bucket
is a real rclone local remote behind the wrapper of
tests/unit/test_image_upload.py, as in the adopt-map and mark-freed tests.

    python -m pytest tests/unit/test_rebuild_volume.py

Version: 0.2.0
"""

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from tests.unit.test_image_upload import STEM, TENANT, _volume, remote  # noqa: F401

COUNTER_AFTER = 5  # numbers 4 and 5 were given elsewhere and are not in the bundle


def _master(n: int) -> bytes:
    return f"jpg master {n} ".encode() * (10 + n)


def _name(n: int) -> str:
    return f"{STEM}-img_{n:04d}_c0.jpg"


def _key(n: int) -> str:
    return f"{TENANT}/{STEM}/captures/{_name(n)}"


def _capture_id(n: int) -> str:
    return str(uuid.UUID(int=n, version=4))


def _bundle(tmp_path: Path, remote, count: int = 3, *, put=True, plan_over=None, image_over=None,
            envelope_over=None) -> Path:
    """A bundle of `count` images, ordinals 1..count, and their masters in the bucket."""
    bundle = tmp_path / "bundle" / STEM
    (bundle / "thumbs").mkdir(parents=True)
    images, plan = [], []
    for n in range(1, count + 1):
        data = _master(n)
        thumb = f"thumbs/{n:05d}.jpg"
        (bundle / thumb).write_bytes(b"thumbnail " + str(n).encode())
        images.append({
            "volumeOrder": n, "sourceKey": f"rionegro/2023/tomo/IMG_{n:04d}.JPG", "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "sha1": hashlib.sha1(data).hexdigest(),
            "width": 4000, "height": 3000, "format": "JPEG",
            "exifCapturedAt": f"2023:03:0{n} 10:00:00", "exifCapturedAtSource": "DateTimeOriginal",
            "exifOrientation": 6 if n == 2 else 1, "thumbnail": thumb,
        })
        plan.append({
            "volumeOrder": n, "ordinal": n, "filename": _name(n), "captureId": _capture_id(n),
            "capturedAt": f"2023-03-0{n}T15:00:00+00:00", "capturedAtSource": "DateTimeOriginal",
            "rotateDeg": 90 if n == 2 else 0,
        })
        if put:
            remote.put(_key(n), data)
    for change in (image_over or []):
        change(images)
    for change in (plan_over or []):
        change(plan)
    (bundle / "images.jsonl").write_text("".join(json.dumps(i) + "\n" for i in images))
    envelope = {"stem": STEM, "counterAfter": COUNTER_AFTER, "images": plan}
    envelope.update(envelope_over or {})
    (bundle / "plan.json").write_text(json.dumps(envelope))
    return bundle


def _rebuild(db, bundle, apply=False, stem=STEM):
    from app.core.rebuild_volume import rebuild_volume

    return rebuild_volume(db, stem, str(bundle), apply=apply)


def _snapshot(db, root: Path):
    """Everything a run could write: rows, the counter, logs, and files under the volume's directory."""
    from app.models.collection import Collection
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage
    from app.models.system_log import SystemLog

    db.expire_all()
    vol = db.query(Collection).filter(Collection.filename_stem == STEM).one_or_none()
    files = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    return {
        "records": db.query(Record).count(), "images": db.query(RecordImage).count(),
        "offsite": db.query(OffsiteObject).count(), "logs": db.query(SystemLog).count(),
        "counter": vol.capture_counter if vol else None, "files": files,
    }


def _manifest_lines(root: Path):
    path = root / STEM / "metadata" / "manifest.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _freed_lines(root: Path):
    out = []
    for path in sorted((root / STEM / "metadata").glob("offsite-freed-*.jsonl")):
        out.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    return out


# ---------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_dry_run_reports_and_writes_nothing(remote, db_session, override_projects_root, tmp_path):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    before = _snapshot(db_session, override_projects_root)

    report = _rebuild(db_session, bundle, apply=False)

    assert report["applied"] is False
    assert [c["filename"] for c in report["created"]] == [_name(1), _name(2), _name(3)]
    assert [c["key"] for c in report["created"]] == [_key(1), _key(2), _key(3)]
    assert report["skipped"] == [] and report["counter_after"] == COUNTER_AFTER
    assert _snapshot(db_session, override_projects_root) == before
    assert remote.calls("copyto") == []


# ---------------------------------------------------------------------------
# Preconditions: each refuses and changes nothing
# ---------------------------------------------------------------------------

def _refused(db, bundle, root, *, match, stem=STEM):
    from app.core.rebuild_volume import RebuildRefused

    before = _snapshot(db, root)
    with pytest.raises(RebuildRefused, match=match) as caught:
        _rebuild(db, bundle, apply=True, stem=stem)
    assert _snapshot(db, root) == before
    return caught.value


@pytest.mark.unit
def test_an_unknown_stem_is_refused(remote, db_session, override_projects_root, tmp_path):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote, envelope_over={"stem": "xx-test-999"})
    _refused(db_session, bundle, override_projects_root, match="no volume has the file name stem",
             stem="xx-test-999")


@pytest.mark.unit
def test_a_plan_for_another_stem_is_refused(remote, db_session, override_projects_root, tmp_path):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote, envelope_over={"stem": "xx-test-002"})
    _refused(db_session, bundle, override_projects_root, match="plan.json is for the stem")


@pytest.mark.unit
def test_a_volume_with_a_record_of_its_own_is_refused(remote, db_session, override_projects_root, tmp_path):
    from app.models.record import Record

    vol = _volume(db_session)
    db_session.add(Record(title="t", collection_id=vol.id, capture_mode="single", sequence=1))
    db_session.commit()
    bundle = _bundle(tmp_path, remote)
    refusal = _refused(db_session, bundle, override_projects_root, match="already has records")
    assert refusal.problems


def _set_counter(db, vol, n):
    from sqlalchemy import text

    db.execute(text("UPDATE collections SET capture_counter = :n WHERE id = :i"), {"n": n, "i": vol.id})
    db.commit()


@pytest.mark.unit
@pytest.mark.parametrize("counter", [1, COUNTER_AFTER - 1])
def test_a_volume_whose_counter_is_between_0_and_counter_after_is_refused(
        remote, db_session, override_projects_root, tmp_path, counter):
    vol = _volume(db_session)
    _set_counter(db_session, vol, counter)
    bundle = _bundle(tmp_path, remote)
    _refused(db_session, bundle, override_projects_root, match="capture counter")


@pytest.mark.unit
def test_a_counter_already_at_counter_after_is_accepted(remote, db_session, override_projects_root, tmp_path):
    """An earlier run raised it and stopped before its first image."""
    from app.models.collection import Collection

    vol = _volume(db_session)
    _set_counter(db_session, vol, COUNTER_AFTER)
    report = _rebuild(db_session, _bundle(tmp_path, remote), apply=True)
    assert len(report["created"]) == 3
    db_session.expire_all()
    assert db_session.get(Collection, vol.id).capture_counter == COUNTER_AFTER


@pytest.mark.unit
@pytest.mark.parametrize("setting", ["OFFSITE_REMOTE", "OFFSITE_TENANT"])
def test_a_unit_without_offsite_is_refused(remote, db_session, override_projects_root, tmp_path,
                                           monkeypatch, setting):
    from app.core import config

    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    monkeypatch.setattr(config.settings, setting, "")
    _refused(db_session, bundle, override_projects_root, match="offsite")


@pytest.mark.unit
def test_a_counter_after_below_the_highest_ordinal_is_refused(remote, db_session, override_projects_root,
                                                              tmp_path):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote, envelope_over={"counterAfter": 2})
    _refused(db_session, bundle, override_projects_root, match="counterAfter")


@pytest.mark.unit
def test_a_bundle_whose_two_files_name_different_images_is_refused(remote, db_session, override_projects_root,
                                                                    tmp_path):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote, image_over=[lambda images: images.pop()])
    _refused(db_session, bundle, override_projects_root, match="do not name the same images")


@pytest.mark.unit
@pytest.mark.parametrize("change", [
    {"captureId": _capture_id(1)},
    {"ordinal": 1, "filename": _name(1)},  # a name carries its ordinal, so the two repeat together
])
def test_a_repeated_ordinal_filename_or_capture_id_is_refused(remote, db_session, override_projects_root,
                                                              tmp_path, change):
    _volume(db_session)

    def repeat(plan):
        plan[1].update(change)

    bundle = _bundle(tmp_path, remote, plan_over=[repeat])
    _refused(db_session, bundle, override_projects_root, match="repeat")


@pytest.mark.unit
def test_a_filename_that_does_not_carry_its_ordinal_is_refused(remote, db_session, override_projects_root,
                                                               tmp_path):
    _volume(db_session)

    def misname(plan):
        plan[2]["filename"] = _name(7)

    bundle = _bundle(tmp_path, remote, plan_over=[misname])
    _refused(db_session, bundle, override_projects_root, match="filename")


@pytest.mark.unit
def test_a_listing_missing_one_key_is_refused_naming_it(remote, db_session, override_projects_root, tmp_path):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    (remote.root / _key(2)).unlink()

    refusal = _refused(db_session, bundle, override_projects_root, match="bucket")

    assert [(p["key"], p["reason"]) for p in refusal.problems] == [(_key(2), "not_in_bucket")]


@pytest.mark.unit
def test_a_listing_with_another_sha1_or_size_is_refused_naming_each(remote, db_session, override_projects_root,
                                                                    tmp_path):
    _volume(db_session)

    def wrong_size(images):
        images[2]["bytes"] += 1

    bundle = _bundle(tmp_path, remote, image_over=[wrong_size])
    remote.put(_key(1), b"other bytes")

    refusal = _refused(db_session, bundle, override_projects_root, match="bucket")

    assert sorted((p["key"], p["reason"]) for p in refusal.problems) == [
        (_key(1), "bucket_sha1_differs"), (_key(3), "bucket_size_differs")]


# ---------------------------------------------------------------------------
# A full rebuild
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_full_rebuild_writes_each_image_whole(remote, db_session, override_projects_root, tmp_path):
    from app.core.capture_manifest import build_capture_manifest
    from app.models.collection import Collection
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage
    from app.models.system_log import SystemLog

    vol = _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    root = override_projects_root

    report = _rebuild(db_session, bundle, apply=True)

    assert report["applied"] is True
    assert len(report["created"]) == 3 and report["skipped"] == []
    db_session.expire_all()
    records = db_session.query(Record).order_by(Record.sequence).all()
    assert [(r.sequence, r.capture_mode, r.status, r.collection_id) for r in records] == [
        (1, "single", "captured", vol.id), (2, "single", "captured", vol.id), (3, "single", "captured", vol.id)]
    assert [r.title for r in records] == [f"P - {Path(_name(n)).stem}" for n in (1, 2, 3)]

    for n, rec in enumerate(records, start=1):
        (img,) = rec.images
        data = _master(n)
        assert img.filename == _name(n)
        assert img.file_path == str(root / STEM / "captures" / _name(n))
        assert not Path(img.file_path).exists()
        assert img.capture_id == _capture_id(n) and img.pair_id is None and img.role == "single"
        assert img.rotate_deg == (90 if n == 2 else 0)
        assert (img.file_size, img.format, img.resolution_width, img.resolution_height) == (
            len(data), "jpg", 4000, 3000)
        assert img.is_current is True
        thumb = Path(img.thumbnail_path)
        assert thumb.parent == root / STEM / "thumbnails"
        assert thumb.name.endswith("_thumb.jpg") and len(thumb.name) == 32 + len("_thumb.jpg")
        assert thumb.read_bytes() == b"thumbnail " + str(n).encode()
        assert img.master_freed_at is not None and img.master_freed_bytes == len(data)
        assert img.preview_freed_at is None and img.preview_freed_bytes is None

        row = db_session.query(OffsiteObject).filter(OffsiteObject.record_image_id == img.id).one()
        assert (row.kind, row.object_key, row.local_sha1) == ("capture", _key(n), hashlib.sha1(data).hexdigest())
        assert row.verified_at is not None and row.uploaded_at is None and row.refused_at is None

    lines = _manifest_lines(root)
    assert [line["capture_id"] for line in lines] == [_capture_id(n) for n in (1, 2, 3)]
    for n, line in enumerate(lines, start=1):
        data = _master(n)
        assert line["cameras"] == [] and line["pair_id"] is None
        assert line["timestamp_utc"] == f"2023-03-0{n}T15:00:00+00:00"
        assert line["files"] == [{"role": "single", "relative_path": f"captures/{_name(n)}", "bytes": len(data),
                                  "mimetype": "image/jpeg", "sha256": hashlib.sha256(data).hexdigest(),
                                  "rotate_deg": 90 if n == 2 else 0}]
        assert line["provenance"] == {"rebuiltFrom": {"bucket": "zasqua-transition",
                                                      "key": f"rionegro/2023/tomo/IMG_{n:04d}.JPG",
                                                      "capturedAtSource": "DateTimeOriginal"}}

    freed = _freed_lines(root)
    assert [(f["key"], f["bytes"], f["method"]) for f in freed] == [
        (_key(n), len(_master(n)), "rebuild") for n in (1, 2, 3)]

    assert db_session.get(Collection, vol.id).capture_counter == COUNTER_AFTER
    actions = [a for (a,) in db_session.query(SystemLog.action).all()]
    assert actions.count("volume_rebuilt") == 1
    assert remote.calls("copyto") == []

    # Handed over by the operator, as usual; the manifest to Fisqua then lists every image.
    for rec in records:
        rec.status = "in_review"
    db_session.commit()
    doc = build_capture_manifest(db_session, db_session.get(Collection, vol.id))
    images = [i for r in doc["volumes"][0]["records"] for i in r["images"]]
    assert [(i["captureId"], i["filename"], i["objectKey"], i["sha256"], i["role"], i["pairId"])
            for i in images] == [
        (_capture_id(n), _name(n), f"{STEM}/captures/{_name(n)}", hashlib.sha256(_master(n)).hexdigest(),
         "single", None) for n in (1, 2, 3)]
    assert db_session.query(RecordImage).count() == 3


@pytest.mark.unit
def test_the_capture_after_a_rebuild_takes_the_number_after_counter_after(
        remote, db_session, override_projects_root, tmp_path):
    from app.core.capture_naming import take_filename_index

    vol = _volume(db_session)
    _rebuild(db_session, _bundle(tmp_path, remote), apply=True)

    index = take_filename_index(db_session, vol.id, STEM, override_projects_root / STEM / "captures")
    db_session.rollback()
    assert index == f"{STEM}-img_{COUNTER_AFTER + 1:04d}"


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_rerun_after_an_interruption_completes_the_rest(remote, db_session, override_projects_root, tmp_path,
                                                          monkeypatch):
    from app.core import rebuild_volume
    from app.models.collection import Collection
    from app.models.record import RecordImage

    vol = _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    root = override_projects_root

    real = rebuild_volume._copy_thumbnail
    calls = []

    def fail_on_second(src, dest_dir):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("card removed")
        return real(src, dest_dir)

    monkeypatch.setattr(rebuild_volume, "_copy_thumbnail", fail_on_second)
    with pytest.raises(OSError, match="card removed"):
        _rebuild(db_session, bundle, apply=True)

    # The first image is whole, the second left nothing, and the counter was raised before the first.
    db_session.expire_all()
    assert [i.capture_id for i in db_session.query(RecordImage).all()] == [_capture_id(1)]
    assert len(list((root / STEM / "thumbnails").iterdir())) == 1
    assert [line["capture_id"] for line in _manifest_lines(root)] == [_capture_id(1)]
    assert db_session.get(Collection, vol.id).capture_counter == COUNTER_AFTER

    monkeypatch.setattr(rebuild_volume, "_copy_thumbnail", real)
    report = _rebuild(db_session, bundle, apply=True)

    assert [s["capture_id"] for s in report["skipped"]] == [_capture_id(1)]
    assert [c["capture_id"] for c in report["created"]] == [_capture_id(2), _capture_id(3)]
    db_session.expire_all()
    assert sorted(i.capture_id for i in db_session.query(RecordImage).all()) == [_capture_id(n) for n in (1, 2, 3)]
    assert len(list((root / STEM / "thumbnails").iterdir())) == 3
    assert [line["capture_id"] for line in _manifest_lines(root)] == [_capture_id(n) for n in (1, 2, 3)]
    assert sorted(f["key"] for f in _freed_lines(root)) == [_key(n) for n in (1, 2, 3)]
    assert db_session.get(Collection, vol.id).capture_counter == COUNTER_AFTER


@pytest.mark.unit
def test_a_failed_commit_removes_the_images_thumbnail_and_a_rerun_adds_no_second_manifest_line(
        remote, db_session, override_projects_root, tmp_path, monkeypatch):
    from app.core import image_upload
    from app.models.record import RecordImage

    _volume(db_session)
    bundle = _bundle(tmp_path, remote, count=1)
    root = override_projects_root

    real = image_upload._claim

    def refuse(db, *args, **kwargs):
        db.rollback()
        raise image_upload._ImageMoved()

    monkeypatch.setattr(image_upload, "_claim", refuse)
    with pytest.raises(RuntimeError, match="changed while"):
        _rebuild(db_session, bundle, apply=True)
    db_session.expire_all()
    assert db_session.query(RecordImage).count() == 0
    assert list((root / STEM / "thumbnails").iterdir()) == []
    assert len(_manifest_lines(root)) == 1  # append-only: the line stays, as a capture's does

    monkeypatch.setattr(image_upload, "_claim", real)
    report = _rebuild(db_session, bundle, apply=True)
    assert [c["capture_id"] for c in report["created"]] == [_capture_id(1)]
    assert [line["capture_id"] for line in _manifest_lines(root)] == [_capture_id(1)]


def _capture_meanwhile(db, vol, root: Path):
    """What a single capture into the volume writes, as trigger_capture does: number, file, manifest line, rows."""
    from app.core.capture_naming import take_filename_index
    from app.core.record_sequence import next_sequences
    from app.models.record import Record, RecordImage
    from capture.manifestHandler import append_manifest_lines

    captures = root / STEM / "captures"
    captures.mkdir(parents=True, exist_ok=True)
    index = take_filename_index(db, vol.id, STEM, captures)
    name = f"{index}_c0.jpg"
    (captures / name).write_bytes(b"a capture meanwhile")
    capture_id = str(uuid.uuid4())
    append_manifest_lines(root / STEM, [json.dumps({
        "capture_id": capture_id, "project_name": "P", "pair_id": None, "cameras": [],
        "files": [{"role": "single", "relative_path": f"captures/{name}", "bytes": 19, "mimetype": "image/jpeg",
                   "sha256": hashlib.sha256(b"a capture meanwhile").hexdigest(), "rotate_deg": 0}]})])
    rec = Record(title=f"P - {index}_c0", collection_id=vol.id, capture_mode="single",
                 sequence=next_sequences(db, vol.id, 1)[0])
    db.add(rec)
    db.flush()
    db.add(RecordImage(record_id=rec.id, filename=name, file_path=str(captures / name), format="jpg",
                       capture_id=capture_id, role="single"))
    db.commit()
    return index, rec.id


@pytest.mark.unit
def test_a_capture_during_a_run_takes_the_number_after_counter_after_and_the_rerun_completes(
        remote, db_session, override_projects_root, tmp_path, monkeypatch):
    from app.core import rebuild_volume
    from app.models.collection import Collection
    from app.models.record import Record

    vol = _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    root = override_projects_root
    real = rebuild_volume._copy_thumbnail
    calls = []

    def fail_on_second(src, dest_dir):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("card removed")
        return real(src, dest_dir)

    monkeypatch.setattr(rebuild_volume, "_copy_thumbnail", fail_on_second)
    with pytest.raises(OSError):
        _rebuild(db_session, bundle, apply=True)
    monkeypatch.setattr(rebuild_volume, "_copy_thumbnail", real)

    index, captured_id = _capture_meanwhile(db_session, vol, root)
    assert index == f"{STEM}-img_{COUNTER_AFTER + 1:04d}"

    report = _rebuild(db_session, bundle, apply=True)

    assert [c["capture_id"] for c in report["created"]] == [_capture_id(2), _capture_id(3)]
    db_session.expire_all()
    order = [(r.id == captured_id, r.sequence)
             for r in db_session.query(Record).filter(Record.collection_id == vol.id).order_by(Record.sequence)]
    # The bundle's pages keep their volume order; the capture, taken after them, follows them.
    assert order == [(False, 1), (False, 2), (False, 3), (True, 4)]
    assert report["resequenced"] == [{"record_id": captured_id, "from": 2, "to": 4}]
    assert db_session.get(Collection, vol.id).capture_counter == COUNTER_AFTER + 1


@pytest.mark.unit
def test_a_rerun_refuses_a_capture_numbered_within_the_plan(remote, db_session, override_projects_root, tmp_path):
    """A record carrying a number at or below counterAfter was not taken after the raise."""
    vol = _volume(db_session)
    _set_counter(db_session, vol, COUNTER_AFTER - 1)  # as before a rebuild, by hand
    _capture_meanwhile(db_session, vol, override_projects_root)  # takes counterAfter
    _refused(db_session, _bundle(tmp_path, remote), override_projects_root, match="already has records")


@pytest.mark.unit
def test_a_rerun_of_a_finished_rebuild_changes_nothing(remote, db_session, override_projects_root, tmp_path):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    _rebuild(db_session, bundle, apply=True)
    before = _snapshot(db_session, override_projects_root)

    report = _rebuild(db_session, bundle, apply=True)

    assert report["created"] == [] and len(report["skipped"]) == 3
    after = _snapshot(db_session, override_projects_root)
    assert {k: v for k, v in after.items() if k != "logs"} == {k: v for k, v in before.items() if k != "logs"}


@pytest.mark.unit
def test_a_rerun_refuses_a_record_that_is_not_the_earlier_runs(remote, db_session, override_projects_root,
                                                               tmp_path, monkeypatch):
    from app.core import rebuild_volume
    from app.models.record import Record

    vol = _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    real = rebuild_volume._copy_thumbnail
    calls = []

    def fail_on_second(src, dest_dir):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("card removed")
        return real(src, dest_dir)

    monkeypatch.setattr(rebuild_volume, "_copy_thumbnail", fail_on_second)
    with pytest.raises(OSError):
        _rebuild(db_session, bundle, apply=True)
    monkeypatch.setattr(rebuild_volume, "_copy_thumbnail", real)
    db_session.add(Record(title="a capture meanwhile", collection_id=vol.id, capture_mode="single", sequence=9))
    db_session.commit()

    _refused(db_session, bundle, override_projects_root, match="already has records")


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_command_reports_as_json(remote, db_session, override_projects_root, tmp_path, capsys):
    from app.core import rebuild_volume

    _volume(db_session)
    bundle = _bundle(tmp_path, remote)

    assert rebuild_volume.main([STEM, str(bundle)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is False and len(report["created"]) == 3


@pytest.mark.unit
def test_the_command_prints_each_problem_and_exits_2_when_refused(remote, db_session, override_projects_root,
                                                                  tmp_path, capsys):
    from app.core import rebuild_volume

    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    (remote.root / _key(3)).unlink()

    assert rebuild_volume.main([STEM, str(bundle), "--apply"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and _key(3) in err and "not_in_bucket" in err


# ---------------------------------------------------------------------------
# Review follow-ups
# ---------------------------------------------------------------------------

def _interrupted(db, bundle, monkeypatch, after=1):
    """Run until the thumbnail of image `after` + 1 fails to copy."""
    from app.core import rebuild_volume

    real = rebuild_volume._copy_thumbnail
    calls = []

    def fail(src, dest_dir):
        calls.append(src)
        if len(calls) == after + 1:
            raise OSError("card removed")
        return real(src, dest_dir)

    monkeypatch.setattr(rebuild_volume, "_copy_thumbnail", fail)
    with pytest.raises(OSError):
        _rebuild(db, bundle, apply=True)
    monkeypatch.setattr(rebuild_volume, "_copy_thumbnail", real)


@pytest.mark.unit
def test_a_capture_between_the_checks_and_the_raise_refuses_the_run(remote, db_session, override_projects_root,
                                                                    tmp_path, monkeypatch):
    """The capture took planned number 1 before the counter was raised; no image is written over it."""
    from app.core import capture_counter
    from app.core.rebuild_volume import RebuildRefused
    from app.models.offsite_object import OffsiteObject
    from app.models.record import RecordImage

    vol = _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    real = capture_counter.raise_capture_counter
    taken = []

    def capture_first(db, stem, n, *, apply=False):
        taken.append(_capture_meanwhile(db, vol, override_projects_root)[0])
        return real(db, stem, n, apply=apply)

    monkeypatch.setattr(capture_counter, "raise_capture_counter", capture_first)
    with pytest.raises(RebuildRefused, match="capture counter .* changed"):
        _rebuild(db_session, bundle, apply=True)

    assert taken == [f"{STEM}-img_0001"]
    db_session.expire_all()
    assert [i.filename for i in db_session.query(RecordImage).all()] == [_name(1)]  # the capture's alone
    assert db_session.query(OffsiteObject).count() == 0
    assert not (override_projects_root / STEM / "thumbnails").exists()
    assert [line["files"][0]["sha256"] for line in _manifest_lines(override_projects_root)] == [
        hashlib.sha256(b"a capture meanwhile").hexdigest()]


@pytest.mark.unit
def test_a_counter_above_counter_after_with_no_other_records_completes(remote, db_session, override_projects_root,
                                                                       tmp_path, monkeypatch):
    """A capture taken during the run and then deleted leaves only its manifest line, numbered above counterAfter."""
    from app.models.collection import Collection
    from app.models.record import Record, RecordImage

    vol = _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    _interrupted(db_session, bundle, monkeypatch)
    _, record_id = _capture_meanwhile(db_session, vol, override_projects_root)
    db_session.query(RecordImage).filter(RecordImage.record_id == record_id).delete()
    db_session.query(Record).filter(Record.id == record_id).delete()
    db_session.commit()

    report = _rebuild(db_session, bundle, apply=True)

    assert [c["capture_id"] for c in report["created"]] == [_capture_id(2), _capture_id(3)]
    db_session.expire_all()
    assert db_session.get(Collection, vol.id).capture_counter == COUNTER_AFTER + 1


@pytest.mark.unit
def test_a_counter_above_counter_after_with_an_unplanned_line_within_the_plan_is_refused(
        remote, db_session, override_projects_root, tmp_path):
    from capture.manifestHandler import append_manifest_lines

    vol = _volume(db_session)
    _set_counter(db_session, vol, COUNTER_AFTER + 1)
    append_manifest_lines(override_projects_root / STEM, [json.dumps({
        "capture_id": str(uuid.uuid4()), "files": [{"role": "single", "relative_path": f"captures/{_name(2)}",
                                                    "sha256": "0" * 64}]})])
    _refused(db_session, _bundle(tmp_path, remote), override_projects_root, match="manifest")


@pytest.mark.unit
@pytest.mark.parametrize("orientation,rotate", [(None, 90), (1, 90), (6, 0), (3, 270), (8, 90), (2, 0), ("6", 90)])
def test_a_rotation_that_disagrees_with_exif_or_an_unsupported_orientation_is_refused(
        remote, db_session, override_projects_root, tmp_path, orientation, rotate):
    _volume(db_session)

    def set_exif(images):
        images[0]["exifOrientation"] = orientation

    def set_rotate(plan):
        plan[0]["rotateDeg"] = rotate

    bundle = _bundle(tmp_path, remote, image_over=[set_exif], plan_over=[set_rotate])
    refusal = _refused(db_session, bundle, override_projects_root, match="orientation")
    assert [p["volumeOrder"] for p in refusal.problems] == [1]


@pytest.mark.unit
@pytest.mark.parametrize("orientation,rotate", [(None, 0), (1, 0), (6, 90), (3, 180), (8, 270)])
def test_a_rotation_that_agrees_with_exif_is_accepted(remote, db_session, override_projects_root, tmp_path,
                                                       orientation, rotate):
    _volume(db_session)

    def set_exif(images):
        images[0]["exifOrientation"] = orientation
        images[1]["exifOrientation"] = 6  # the bundle's second image is planned at 90

    def set_rotate(plan):
        plan[0]["rotateDeg"] = rotate

    bundle = _bundle(tmp_path, remote, image_over=[set_exif], plan_over=[set_rotate])
    assert len(_rebuild(db_session, bundle)["created"]) == 3


@pytest.mark.unit
def test_the_orientation_map_is_captures_own():
    from app.core.rebuild_volume import DEG_FOR_EXIF_ORIENTATION
    from capture.service import _EXIF_ORIENTATION_FOR_DEG

    assert {v: k for k, v in _EXIF_ORIENTATION_FOR_DEG.items()} | {1: 0} == DEG_FOR_EXIF_ORIENTATION


@pytest.mark.unit
def test_a_repeated_source_key_is_refused(remote, db_session, override_projects_root, tmp_path):
    _volume(db_session)

    def repeat(images):
        images[2]["sourceKey"] = images[0]["sourceKey"]

    _refused(db_session, _bundle(tmp_path, remote, image_over=[repeat]), override_projects_root,
             match="sourceKey")


@pytest.mark.unit
def test_a_planned_master_already_on_the_card_is_refused(remote, db_session, override_projects_root, tmp_path):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    master = override_projects_root / STEM / "captures" / _name(2)
    master.parent.mkdir(parents=True)
    master.write_bytes(_master(2))
    refusal = _refused(db_session, bundle, override_projects_root, match="on the card")
    assert [(p["volumeOrder"], p["reason"]) for p in refusal.problems] == [(2, "master_on_card")]


@pytest.mark.unit
def test_a_planned_key_or_capture_id_recorded_elsewhere_is_refused(remote, db_session, override_projects_root,
                                                                   tmp_path):
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage

    _volume(db_session)
    other = _volume(db_session, name="Tomo 2", stem="xx-test-002")
    rec = Record(title="t", collection_id=other.id, capture_mode="single", sequence=1)
    db_session.add(rec)
    db_session.commit()
    img = RecordImage(record_id=rec.id, filename="x.jpg", file_path="/nowhere/x.jpg", format="jpg",
                      capture_id=_capture_id(3))
    db_session.add(img)
    db_session.commit()
    db_session.add(OffsiteObject(record_image_id=img.id, kind="capture", object_key=_key(1), local_sha1="0" * 40))
    db_session.commit()

    refusal = _refused(db_session, _bundle(tmp_path, remote), override_projects_root, match="already recorded")
    assert sorted((p["volumeOrder"], p["reason"]) for p in refusal.problems) == [
        (1, "key_already_held"), (3, "capture_id_in_use")]


@pytest.mark.unit
def test_a_manifest_holding_another_capture_is_refused(remote, db_session, override_projects_root, tmp_path):
    from capture.manifestHandler import append_manifest_lines

    _volume(db_session)
    append_manifest_lines(override_projects_root / STEM, [json.dumps({
        "capture_id": str(uuid.uuid4()), "files": [{"role": "single", "relative_path": "captures/other.jpg"}]})])
    _refused(db_session, _bundle(tmp_path, remote), override_projects_root, match="manifest")


@pytest.mark.unit
def test_an_unanswered_listing_is_refused(remote, db_session, override_projects_root, tmp_path):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    remote.set_modes("listing-refused")
    _refused(db_session, bundle, override_projects_root, match="listing did not answer")


@pytest.mark.unit
def test_a_rerun_writes_a_lost_freed_line_with_its_offset(remote, db_session, override_projects_root, tmp_path):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    _rebuild(db_session, bundle, apply=True)
    for path in (override_projects_root / STEM / "metadata").glob("offsite-freed-*.jsonl"):
        path.unlink()

    _rebuild(db_session, bundle, apply=True)

    freed = _freed_lines(override_projects_root)
    assert sorted(f["key"] for f in freed) == [_key(n) for n in (1, 2, 3)]
    assert all(f["freed_at"].endswith("+00:00") and f["method"] == "rebuild" for f in freed)


@pytest.mark.unit
def test_a_rerun_recopies_a_missing_thumbnail(remote, db_session, override_projects_root, tmp_path):
    from app.models.record import RecordImage

    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    _rebuild(db_session, bundle, apply=True)
    img = db_session.query(RecordImage).filter(RecordImage.capture_id == _capture_id(2)).one()
    thumb = Path(img.thumbnail_path)
    thumb.unlink()

    report = _rebuild(db_session, bundle, apply=True)

    assert thumb.read_bytes() == b"thumbnail 2"
    assert report["thumbnails_recopied"] == [str(thumb)]


@pytest.mark.unit
def test_a_second_run_while_one_holds_the_volume_is_refused(remote, db_session, override_projects_root, tmp_path):
    import fcntl

    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    lock = override_projects_root / STEM / "metadata" / ".rebuild.lock"
    lock.parent.mkdir(parents=True)
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        from app.core.rebuild_volume import RebuildRefused
        with pytest.raises(RebuildRefused, match="another rebuild"):
            _rebuild(db_session, bundle, apply=True)
    assert _snapshot(db_session, override_projects_root)["records"] == 0


@pytest.mark.unit
def test_the_command_reports_a_refused_row_and_exits_2(remote, db_session, override_projects_root, tmp_path,
                                                       capsys, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from app.core import image_upload, rebuild_volume

    _volume(db_session)
    bundle = _bundle(tmp_path, remote, count=1)

    def refuse(db, *args, **kwargs):
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed: offsite_objects.object_key"))

    monkeypatch.setattr(image_upload, "_claim", refuse)
    assert rebuild_volume.main([STEM, str(bundle), "--apply"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "UNIQUE constraint failed" in err
    assert list((override_projects_root / STEM / "thumbnails").iterdir()) == []


@pytest.mark.unit
def test_a_lock_file_a_killed_run_left_is_not_foreign_to_the_layout_pass(remote, db_session, override_projects_root,
                                                                         tmp_path):
    from app.core.rebuild_volume import LOCK_NAME
    from app.core.volume_layout import _foreign_entries

    vol = _volume(db_session)
    _rebuild(db_session, _bundle(tmp_path, remote), apply=True)
    metadata = override_projects_root / STEM / "metadata"
    (metadata / LOCK_NAME).write_text("")
    (metadata / "other.lock").write_text("")

    assert _foreign_entries(db_session, override_projects_root / STEM, vol.id) == ["metadata/other.lock"]
