"""`python -m app.core.rebuild_volume <stem> <bundle> [--apply]`: a volume rebuilt from paired JPGs captured elsewhere.

The masters are already in the bucket under contract names and never come to
the card; the bundle carries each source image's facts (images.jsonl), the
records they make with names, ids, dates, roles and rotations (plan.json),
and a ready, upright thumbnail per image. The bucket is a real rclone local
remote behind the wrapper of tests/unit/test_image_upload.py, as in the
adopt-map and mark-freed tests.

The default bundle has five source images: 1 and 2 make the first opening, 3
and 4 the second, and 5 has no partner. Each odd image is the right-hand page
(camera 1, rotated 90), each even one the left-hand page (camera 0, rotated
270).

A volume whose earlier records were deleted to be rebuilt again
(`indexOffset`) is the default bundle numbered after the old records: the
old volume had OFFSET numbers, and the new files take OFFSET + 1 on.

    python -m pytest tests/unit/test_rebuild_volume.py

Version: 0.3.3
"""

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from tests.unit.test_image_upload import STEM, TENANT, _volume, remote  # noqa: F401

COUNTER_AFTER = 5  # numbers 4 and 5 were given elsewhere and are not in the bundle
# The EXIF Orientation of each source image, by volumeOrder: close to random,
# as the cameras wrote them; 2, 3 and 4 disagree with the plan's rotation.
EXIF = {1: 6, 2: 6, 3: None, 4: 1, 5: 8}


def _master(v: int) -> bytes:
    return f"jpg master {v} ".encode() * (10 + v)


def _name(n: int, cam: int = 0) -> str:
    return f"{STEM}-img_{n:04d}_c{cam}.jpg"


def _key(name: str) -> str:
    return f"{TENANT}/{STEM}/captures/{name}"


def _capture_id(n: int) -> str:
    return str(uuid.UUID(int=n, version=4))


def _pair_id(n: int) -> str:
    """A dual record's pair id: its filename index, as capture gives one (capture.service.dual_capture_image)."""
    return f"{STEM}-img_{n:04d}"


def _source(v: int) -> str:
    return f"rionegro/2023/tomo/IMG_{v:04d}.JPG"


# (ordinal, mode, [(volumeOrder, filename, role, rotateDeg)])
LAYOUT = (
    (1, "dual", [(2, _name(1, 0), "left", 270), (1, _name(1, 1), "right", 90)]),
    (2, "dual", [(4, _name(2, 0), "left", 270), (3, _name(2, 1), "right", 90)]),
    (3, "single", [(5, _name(3, 0), "single", 90)]),
)
FILES = [f for _, _, files in LAYOUT for f in files]  # in record order, left before right
NAMES = [name for _, name, _, _ in FILES]
KEYS = [_key(name) for name in NAMES]


def _bundle(tmp_path: Path, remote, *, put=True, plan_over=None, image_over=None, envelope_over=None) -> Path:
    """The default bundle (module header), and its masters in the bucket."""
    bundle = tmp_path / "bundle" / STEM
    (bundle / "thumbs").mkdir(parents=True)
    images, records = [], []
    for ordinal, mode, files in LAYOUT:
        records.append({
            "ordinal": ordinal, "captureId": _capture_id(ordinal),
            "pairId": _pair_id(ordinal) if mode == "dual" else None, "mode": mode,
            "capturedAt": f"2023-03-0{ordinal}T15:00:00+00:00", "capturedAtSource": "DateTimeOriginal",
            "images": [{"volumeOrder": v, "filename": name, "role": role, "rotateDeg": deg}
                       for v, name, role, deg in files],
        })
        for v, name, _, _ in files:
            data = _master(v)
            thumb = f"thumbs/{v:05d}.jpg"
            (bundle / thumb).write_bytes(b"thumbnail " + str(v).encode())
            line = {
                "volumeOrder": v, "sourceKey": _source(v), "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "sha1": hashlib.sha1(data).hexdigest(),
                "width": 4000, "height": 3000, "format": "JPEG",
                "exifCapturedAt": f"2023:03:0{ordinal} 10:00:00", "exifCapturedAtSource": "DateTimeOriginal",
                "thumbnail": thumb,
            }
            if EXIF[v] is not None:
                line["exifOrientation"] = EXIF[v]
            images.append(line)
            if put:
                remote.put(_key(name), data)
    images.sort(key=lambda i: i["volumeOrder"])
    for change in (image_over or []):
        change(images)
    for change in (plan_over or []):
        change(records)
    (bundle / "images.jsonl").write_text("".join(json.dumps(i) + "\n" for i in images))
    envelope = {"stem": STEM, "counterAfter": COUNTER_AFTER, "records": records}
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


def _thumbs(root: Path) -> list:
    d = root / STEM / "thumbnails"
    return sorted(d.iterdir()) if d.exists() else []


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
    assert (report["records"], report["images"]) == (3, 5)
    assert [(c["ordinal"], c["mode"], c["capture_id"]) for c in report["created"]] == [
        (1, "dual", _capture_id(1)), (2, "dual", _capture_id(2)), (3, "single", _capture_id(3))]
    assert [f["filename"] for c in report["created"] for f in c["files"]] == NAMES
    assert [f["key"] for c in report["created"] for f in c["files"]] == KEYS
    assert report["skipped"] == [] and report["counter_after"] == COUNTER_AFTER
    assert report["freed_bytes"] == sum(len(_master(v)) for v in range(1, 6))
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
    """An earlier run raised it and stopped before its first record."""
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
def test_a_counter_after_below_the_number_of_records_is_refused(remote, db_session, override_projects_root,
                                                                tmp_path):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote, envelope_over={"counterAfter": 2})
    _refused(db_session, bundle, override_projects_root, match="counterAfter")


# ---------------------------------------------------------------------------
# The bundle: each refusal
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_bundle_whose_two_files_name_different_images_is_refused(remote, db_session, override_projects_root,
                                                                    tmp_path):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote, image_over=[lambda images: images.pop()])
    refusal = _refused(db_session, bundle, override_projects_root, match="do not name the same images")
    assert refusal.problems == [{"volumeOrder": 5, "reason": "only_in_plan"}]


def _edit(fn):
    """A plan_over that applies `fn` to the records."""
    return [fn]


@pytest.mark.unit
@pytest.mark.parametrize("field,edit", [
    # the single's image is also named in the second pair
    ("volumeOrder", lambda r: r[2]["images"][0].update(volumeOrder=3)),
    # a name carries its ordinal, so the two repeat together
    ("filename", lambda r: r[2].update(ordinal=2) or r[2]["images"][0].update(filename=_name(2, 0))),
    ("captureId", lambda r: r[1].update(captureId=_capture_id(1))),
    # a pair id carries its ordinal too
    ("pairId", lambda r: (r[1].update(ordinal=1, pairId=_pair_id(1)),
                          [i.update(filename=_name(1, 1 if i["role"] == "right" else 0)) for i in r[1]["images"]])),
])
def test_a_repeated_volume_order_filename_capture_id_or_pair_id_is_refused(
        remote, db_session, override_projects_root, tmp_path, field, edit):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote, plan_over=_edit(edit))
    refusal = _refused(db_session, bundle, override_projects_root, match="repeat")
    assert {p["field"] for p in refusal.problems if "field" in p} >= {field}


def _record_refusal(db, bundle, root, reason, ordinal):
    refusal = _refused(db, bundle, root, match="plan.json")
    assert [(p.get("ordinal"), p["reason"]) for p in refusal.problems] == [(ordinal, reason)]


@pytest.mark.unit
@pytest.mark.parametrize("edit,reason,ordinal", [
    (lambda r: r[0]["images"][1].update(role="left"), "dual_roles_invalid", 1),
    (lambda r: r[0]["images"].pop(), "dual_roles_invalid", 1),
    (lambda r: r[0]["images"].append({"volumeOrder": 9, "filename": _name(1, 2), "role": "right",
                                      "rotateDeg": 90}), "dual_roles_invalid", 1),
    # left and right swapped between the files
    (lambda r: (r[0]["images"][0].update(filename=_name(1, 1)), r[0]["images"][1].update(filename=_name(1, 0))),
     "filename_not_its_ordinal", 1),
    (lambda r: r[1]["images"][0].update(filename=_name(1, 0)), "filename_not_its_ordinal", 2),
    (lambda r: r[0].update(pairId=None), "pair_id_invalid", 1),
    (lambda r: r[0].update(pairId=str(uuid.UUID(int=101, version=4))), "pair_id_invalid", 1),  # a UUID
    (lambda r: r[0].update(pairId=_pair_id(2)), "pair_id_invalid", 1),  # another record's index
    (lambda r: r[0].update(pairId=f"{STEM}-img_1"), "pair_id_invalid", 1),  # not zero-padded
    (lambda r: r[0].update(pairId=f"xx-test-002-img_0001"), "pair_id_invalid", 1),  # another stem's
    (lambda r: r[0].update(pairId=1), "pair_id_invalid", 1),
])
def test_a_dual_record_that_is_not_one_left_c0_and_one_right_c1_is_refused(
        remote, db_session, override_projects_root, tmp_path, edit, reason, ordinal):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote, plan_over=_edit(edit))
    _record_refusal(db_session, bundle, override_projects_root, reason, ordinal)


@pytest.mark.unit
@pytest.mark.parametrize("edit,reason", [
    (lambda r: r[2]["images"].append({"volumeOrder": 9, "filename": _name(3, 1), "role": "right",
                                      "rotateDeg": 90}), "single_images_invalid"),
    (lambda r: r[2]["images"][0].update(role="right"), "single_images_invalid"),
    (lambda r: r[2]["images"][0].update(filename=_name(3, 1)), "filename_not_its_ordinal"),
    (lambda r: r[2].update(pairId=_pair_id(3)), "pair_id_invalid"),
    (lambda r: r[2].update(mode="triple"), "mode_invalid"),
])
def test_a_single_record_that_is_not_one_single_c0_image_is_refused(
        remote, db_session, override_projects_root, tmp_path, edit, reason):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote, plan_over=_edit(edit))
    _record_refusal(db_session, bundle, override_projects_root, reason, 3)


@pytest.mark.unit
@pytest.mark.parametrize("rotate", [45, -90, 360, "90", None])
def test_a_rotation_capture_does_not_use_is_refused(remote, db_session, override_projects_root, tmp_path, rotate):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote, plan_over=_edit(lambda r: r[1]["images"][1].update(rotateDeg=rotate)))
    _record_refusal(db_session, bundle, override_projects_root, "rotate_deg_invalid", 2)


@pytest.mark.unit
@pytest.mark.parametrize("edit", [
    lambda r: r[2].update(ordinal=4),           # 1, 2, 4
    lambda r: r[2].update(ordinal=2),           # 1, 2, 2
    lambda r: [x.update(ordinal=x["ordinal"] + 1) for x in r],  # 2, 3, 4
])
def test_ordinals_that_are_not_1_to_n_are_refused(remote, db_session, override_projects_root, tmp_path, edit):
    """A record's file names and pair id carry its ordinal, so they are moved with it."""
    _volume(db_session)

    def renumber(records):
        edit(records)
        for rec in records:
            if rec["mode"] == "dual":
                rec["pairId"] = _pair_id(rec["ordinal"])
            for img in rec["images"]:
                cam = 1 if img["role"] == "right" else 0
                img["filename"] = _name(rec["ordinal"], cam)

    bundle = _bundle(tmp_path, remote, plan_over=[renumber])
    _refused(db_session, bundle, override_projects_root, match="ordinal")


@pytest.mark.unit
def test_a_repeated_source_key_is_refused(remote, db_session, override_projects_root, tmp_path):
    _volume(db_session)

    def repeat(images):
        images[2]["sourceKey"] = images[0]["sourceKey"]

    _refused(db_session, _bundle(tmp_path, remote, image_over=[repeat]), override_projects_root,
             match="sourceKey")


# ---------------------------------------------------------------------------
# The listing
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_listing_missing_one_key_of_a_pair_is_refused_naming_it(remote, db_session, override_projects_root,
                                                                  tmp_path):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    (remote.root / _key(_name(2, 1))).unlink()

    refusal = _refused(db_session, bundle, override_projects_root, match="bucket")

    assert [(p["key"], p["reason"], p["volumeOrder"]) for p in refusal.problems] == [
        (_key(_name(2, 1)), "not_in_bucket", 3)]


@pytest.mark.unit
def test_a_listing_with_another_sha1_or_size_is_refused_naming_each(remote, db_session, override_projects_root,
                                                                    tmp_path):
    _volume(db_session)

    def wrong_size(images):
        images[4]["bytes"] += 1

    bundle = _bundle(tmp_path, remote, image_over=[wrong_size])
    remote.put(_key(_name(1, 0)), b"other bytes")

    refusal = _refused(db_session, bundle, override_projects_root, match="bucket")

    assert sorted((p["key"], p["reason"]) for p in refusal.problems) == [
        (_key(_name(1, 0)), "bucket_sha1_differs"), (_key(_name(3, 0)), "bucket_size_differs")]


# ---------------------------------------------------------------------------
# A full rebuild
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_full_rebuild_writes_each_record_whole(remote, db_session, override_projects_root, tmp_path):
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
        (1, "dual", "captured", vol.id), (2, "dual", "captured", vol.id), (3, "single", "captured", vol.id)]
    assert [r.title for r in records] == ["P - Dual capture", "P - Dual capture", f"P - {Path(_name(3)).stem}"]

    for (ordinal, mode, files), rec in zip(LAYOUT, records):
        images = sorted(rec.images, key=lambda i: i.filename)
        assert [i.filename for i in images] == [name for _, name, _, _ in files]
        assert {i.capture_id for i in images} == {_capture_id(ordinal)}
        assert {i.pair_id for i in images} == {_pair_id(ordinal) if mode == "dual" else None}
        for img, (v, name, role, deg) in zip(images, files):
            data = _master(v)
            assert (img.role, img.rotate_deg) == (role, deg)
            assert img.file_path == str(root / STEM / "captures" / name)
            assert not Path(img.file_path).exists()
            assert (img.file_size, img.format, img.resolution_width, img.resolution_height) == (
                len(data), "jpg", 4000, 3000)
            assert img.is_current is True
            thumb = Path(img.thumbnail_path)
            assert thumb.parent == root / STEM / "thumbnails"
            assert thumb.name.endswith("_thumb.jpg") and len(thumb.name) == 32 + len("_thumb.jpg")
            assert thumb.read_bytes() == b"thumbnail " + str(v).encode()  # already upright: copied as it is
            assert img.master_freed_at is not None and img.master_freed_bytes == len(data)
            assert img.preview_freed_at is None and img.preview_freed_bytes is None

            row = db_session.query(OffsiteObject).filter(OffsiteObject.record_image_id == img.id).one()
            assert (row.kind, row.object_key, row.local_sha1) == (
                "capture", _key(name), hashlib.sha1(data).hexdigest())
            assert row.verified_at is not None and row.uploaded_at is None and row.refused_at is None
    # The right camera's images at 90, the left camera's at 270, whatever their EXIF said.
    assert sorted((i.role, i.rotate_deg) for i in db_session.query(RecordImage)) == [
        ("left", 270), ("left", 270), ("right", 90), ("right", 90), ("single", 90)]
    assert len(_thumbs(root)) == 5

    lines = _manifest_lines(root)
    assert [line["capture_id"] for line in lines] == [_capture_id(n) for n in (1, 2, 3)]
    for (ordinal, mode, files), line in zip(LAYOUT, lines):
        assert line["cameras"] == []
        assert line["pair_id"] == (_pair_id(ordinal) if mode == "dual" else None)
        assert line["timestamp_utc"] == f"2023-03-0{ordinal}T15:00:00+00:00"
        assert line["files"] == [{"role": role, "relative_path": f"captures/{name}", "bytes": len(_master(v)),
                                  "mimetype": "image/jpeg", "sha256": hashlib.sha256(_master(v)).hexdigest(),
                                  "rotate_deg": deg} for v, name, role, deg in files]
        assert line["provenance"] == {
            "rebuiltFrom": [{"role": role, "bucket": "zasqua-transition", "key": _source(v),
                             "exifOrientation": EXIF[v]} for v, _, role, _ in files],
            "capturedAtSource": "DateTimeOriginal"}

    freed = _freed_lines(root)
    assert [(f["key"], f["bytes"], f["method"]) for f in freed] == [
        (_key(name), len(_master(v)), "rebuild") for v, name, _, _ in FILES]

    assert db_session.get(Collection, vol.id).capture_counter == COUNTER_AFTER
    actions = [a for (a,) in db_session.query(SystemLog.action).all()]
    assert actions.count("volume_rebuilt") == 1
    assert remote.calls("copyto") == []

    # Handed over by the operator, as usual; the manifest to Fisqua then lists every image.
    for rec in records:
        rec.status = "in_review"
    db_session.commit()
    doc = build_capture_manifest(db_session, db_session.get(Collection, vol.id))
    listed = [(r["sequence"], i["captureId"], i["pairId"], i["filename"], i["role"], i["objectKey"], i["sha256"])
              for r in doc["volumes"][0]["records"] for i in r["images"]]
    assert listed == [
        (ordinal, _capture_id(ordinal), _pair_id(ordinal) if mode == "dual" else None, name, role,
         f"{STEM}/captures/{name}", hashlib.sha256(_master(v)).hexdigest())
        for ordinal, mode, files in LAYOUT for v, name, role, _ in files]
    assert db_session.query(RecordImage).count() == 5


@pytest.mark.unit
def test_an_exif_orientation_that_disagrees_with_the_plan_is_accepted_and_recorded(
        remote, db_session, override_projects_root, tmp_path):
    from app.models.record import RecordImage

    _volume(db_session)

    def odd_tags(images):
        images[0]["exifOrientation"] = 3   # planned at 90
        images[1]["exifOrientation"] = "6"  # a value that is not a number
        images[4]["exifOrientation"] = 2   # a mirrored one

    _rebuild(db_session, _bundle(tmp_path, remote, image_over=[odd_tags]), apply=True)

    rotations = {i.filename: i.rotate_deg for i in db_session.query(RecordImage)}
    assert rotations == {name: deg for _, name, _, deg in FILES}
    lines = _manifest_lines(override_projects_root)
    assert [s["exifOrientation"] for s in lines[0]["provenance"]["rebuiltFrom"]] == ["6", 3]
    assert [s["exifOrientation"] for s in lines[2]["provenance"]["rebuiltFrom"]] == [2]


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

def _interrupted(db, bundle, monkeypatch, *, on_call):
    """Run until the `on_call`th thumbnail copy, which fails (two copies per pair, one per single)."""
    from app.core import rebuild_volume

    real = rebuild_volume._copy_thumbnail
    calls = []

    def fail(src, dest_dir):
        calls.append(src)
        if len(calls) == on_call:
            raise OSError("card removed")
        return real(src, dest_dir)

    monkeypatch.setattr(rebuild_volume, "_copy_thumbnail", fail)
    with pytest.raises(OSError, match="card removed"):
        _rebuild(db, bundle, apply=True)
    monkeypatch.setattr(rebuild_volume, "_copy_thumbnail", real)


@pytest.mark.unit
@pytest.mark.parametrize("on_call", [3, 4], ids=["before-the-pair", "within-the-pair"])
def test_a_rerun_after_an_interruption_between_records_completes_the_rest(
        remote, db_session, override_projects_root, tmp_path, monkeypatch, on_call):
    """The second pair fails at its first or its second thumbnail and leaves nothing of itself."""
    from app.models.collection import Collection
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage

    vol = _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    root = override_projects_root

    _interrupted(db_session, bundle, monkeypatch, on_call=on_call)

    # The first pair is whole, the second left nothing, and the counter was raised before the first.
    db_session.expire_all()
    assert db_session.query(Record).count() == 1
    assert sorted(i.filename for i in db_session.query(RecordImage)) == NAMES[:2]
    assert db_session.query(OffsiteObject).count() == 2
    assert len(_thumbs(root)) == 2
    assert [line["capture_id"] for line in _manifest_lines(root)] == [_capture_id(1)]
    assert sorted(f["key"] for f in _freed_lines(root)) == sorted(KEYS[:2])
    assert db_session.get(Collection, vol.id).capture_counter == COUNTER_AFTER

    report = _rebuild(db_session, bundle, apply=True)

    assert [s["capture_id"] for s in report["skipped"]] == [_capture_id(1)]
    assert [c["capture_id"] for c in report["created"]] == [_capture_id(2), _capture_id(3)]
    db_session.expire_all()
    assert sorted(i.filename for i in db_session.query(RecordImage)) == sorted(NAMES)
    assert len(_thumbs(root)) == 5
    assert [line["capture_id"] for line in _manifest_lines(root)] == [_capture_id(n) for n in (1, 2, 3)]
    assert sorted(f["key"] for f in _freed_lines(root)) == sorted(KEYS)
    assert db_session.get(Collection, vol.id).capture_counter == COUNTER_AFTER


@pytest.mark.unit
def test_a_failed_commit_removes_both_thumbnails_and_a_rerun_adds_no_second_manifest_line(
        remote, db_session, override_projects_root, tmp_path, monkeypatch):
    from app.core import image_upload
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage

    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    root = override_projects_root

    real = image_upload._claim

    def refuse(db, *args, **kwargs):
        db.rollback()
        raise image_upload._ImageMoved()

    monkeypatch.setattr(image_upload, "_claim", refuse)
    with pytest.raises(RuntimeError, match="changed while"):
        _rebuild(db_session, bundle, apply=True)
    db_session.expire_all()
    assert (db_session.query(Record).count(), db_session.query(RecordImage).count(),
            db_session.query(OffsiteObject).count()) == (0, 0, 0)
    assert _thumbs(root) == []
    assert len(_manifest_lines(root)) == 1  # append-only: the line stays, as a capture's does

    monkeypatch.setattr(image_upload, "_claim", real)
    report = _rebuild(db_session, bundle, apply=True)
    assert [c["capture_id"] for c in report["created"]] == [_capture_id(n) for n in (1, 2, 3)]
    assert [line["capture_id"] for line in _manifest_lines(root)] == [_capture_id(n) for n in (1, 2, 3)]


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
    from app.models.collection import Collection
    from app.models.record import Record

    vol = _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    root = override_projects_root
    _interrupted(db_session, bundle, monkeypatch, on_call=3)

    index, captured_id = _capture_meanwhile(db_session, vol, root)
    assert index == f"{STEM}-img_{COUNTER_AFTER + 1:04d}"

    report = _rebuild(db_session, bundle, apply=True)

    assert [c["capture_id"] for c in report["created"]] == [_capture_id(2), _capture_id(3)]
    db_session.expire_all()
    order = [(r.id == captured_id, r.sequence)
             for r in db_session.query(Record).filter(Record.collection_id == vol.id).order_by(Record.sequence)]
    # The bundle's records keep their ordinals; the capture, taken after them, follows them.
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
    from app.models.record import Record

    vol = _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    _interrupted(db_session, bundle, monkeypatch, on_call=3)
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
    (remote.root / _key(_name(3))).unlink()

    assert rebuild_volume.main([STEM, str(bundle), "--apply"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and _key(_name(3)) in err and "not_in_bucket" in err


@pytest.mark.unit
def test_the_command_reports_a_refused_row_and_exits_2(remote, db_session, override_projects_root, tmp_path,
                                                       capsys, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from app.core import image_upload, rebuild_volume

    _volume(db_session)
    bundle = _bundle(tmp_path, remote)

    def refuse(db, *args, **kwargs):
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed: offsite_objects.object_key"))

    monkeypatch.setattr(image_upload, "_claim", refuse)
    assert rebuild_volume.main([STEM, str(bundle), "--apply"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "UNIQUE constraint failed" in err
    assert _thumbs(override_projects_root) == []


# ---------------------------------------------------------------------------
# Races, leftovers and the unit's other state
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_capture_between_the_checks_and_the_raise_refuses_the_run(remote, db_session, override_projects_root,
                                                                    tmp_path, monkeypatch):
    """The capture took planned number 1 before the counter was raised; no record is written over it."""
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
    assert [i.filename for i in db_session.query(RecordImage).all()] == [_name(1, 0)]  # the capture's alone
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
    _interrupted(db_session, bundle, monkeypatch, on_call=3)
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
def test_a_planned_master_already_on_the_card_is_refused(remote, db_session, override_projects_root, tmp_path):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    master = override_projects_root / STEM / "captures" / _name(2, 1)
    master.parent.mkdir(parents=True)
    master.write_bytes(_master(3))
    refusal = _refused(db_session, bundle, override_projects_root, match="on the card")
    assert [(p["volumeOrder"], p["reason"]) for p in refusal.problems] == [(3, "master_on_card")]


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
    db_session.add(OffsiteObject(record_image_id=img.id, kind="capture", object_key=_key(_name(1, 1)),
                                 local_sha1="0" * 40))
    db_session.commit()

    refusal = _refused(db_session, _bundle(tmp_path, remote), override_projects_root, match="already recorded")
    assert sorted((p["ordinal"], p["reason"]) for p in refusal.problems) == [
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
    assert sorted(f["key"] for f in freed) == sorted(KEYS)
    assert all(f["freed_at"].endswith("+00:00") and f["method"] == "rebuild" for f in freed)


@pytest.mark.unit
def test_a_rerun_recopies_a_missing_thumbnail(remote, db_session, override_projects_root, tmp_path):
    from app.models.record import RecordImage

    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    _rebuild(db_session, bundle, apply=True)
    img = db_session.query(RecordImage).filter(RecordImage.filename == _name(2, 1)).one()
    thumb = Path(img.thumbnail_path)
    thumb.unlink()

    report = _rebuild(db_session, bundle, apply=True)

    assert thumb.read_bytes() == b"thumbnail 3"
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


@pytest.mark.unit
def test_every_artifact_stays_under_the_root_read_at_the_start(remote, db_session, override_projects_root,
                                                               tmp_path, monkeypatch):
    """A storage switch during the run does not split the volume's files across two roots."""
    from capture import project_manager

    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    other = tmp_path / "other-drive"
    other.mkdir()
    real = project_manager._projects_root
    calls = []

    def switching():
        calls.append(1)
        return real() if len(calls) == 1 else other

    monkeypatch.setattr(project_manager, "_projects_root", switching)
    report = _rebuild(db_session, bundle, apply=True)

    assert len(report["created"]) == 3
    assert list(other.rglob("*")) == []
    first = override_projects_root / STEM
    assert len(_thumbs(override_projects_root)) == 5
    assert len(_manifest_lines(override_projects_root)) == 3
    assert len(_freed_lines(override_projects_root)) == 5
    assert report["manifest"].startswith(str(first))


# ---------------------------------------------------------------------------
# A done record is compared with the plan, never skipped on its capture id alone
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_rerun_refuses_a_done_record_the_corrected_plan_disagrees_with(remote, db_session,
                                                                         override_projects_root, tmp_path):
    from app.models.record import Record

    _volume(db_session)
    _rebuild(db_session, _bundle(tmp_path / "first", remote), apply=True)
    record_id = db_session.query(Record.id).filter(Record.sequence == 1).scalar()

    def turn(records):
        records[0]["images"][1]["rotateDeg"] = 270

    corrected = _bundle(tmp_path / "second", remote, plan_over=[turn])
    refusal = _refused(db_session, corrected, override_projects_root, match="differ from the plan")

    assert [(p["record_id"], p["ordinal"], p["fields"]) for p in refusal.problems] == [
        (record_id, 1, ["right.rotate_deg", "manifest.right.rotate_deg"])]


@pytest.mark.unit
@pytest.mark.parametrize("filename,change,fields", [
    (_name(1, 0), lambda img, row: setattr(img, "file_size", img.file_size + 1), ["left.file_size"]),
    (_name(1, 1), lambda img, row: setattr(img, "resolution_height", 1), ["right.resolution_height"]),
    (_name(1, 1), lambda img, row: setattr(img, "created_at", img.created_at.replace(year=2024)),
     ["right.created_at"]),
    (_name(1, 0), lambda img, row: setattr(img, "pair_id", str(uuid.uuid4())), ["left.pair_id"]),
    (_name(1, 0), lambda img, row: setattr(row, "local_sha1", "0" * 40), ["left.offsite.local_sha1"]),
    (_name(3, 0), lambda img, row: setattr(img, "rotate_deg", 0), ["single.rotate_deg"]),
])
def test_a_rerun_names_each_field_a_done_record_differs_in(remote, db_session, override_projects_root, tmp_path,
                                                           filename, change, fields):
    from app.models.offsite_object import OffsiteObject
    from app.models.record import RecordImage

    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    _rebuild(db_session, bundle, apply=True)
    img = db_session.query(RecordImage).filter(RecordImage.filename == filename).one()
    row = db_session.query(OffsiteObject).filter(OffsiteObject.record_image_id == img.id).one()
    change(img, row)
    db_session.commit()

    refusal = _refused(db_session, bundle, override_projects_root, match="differ from the plan")
    ordinal = int(filename.split("img_")[1][:4])
    assert [(p["ordinal"], p["fields"]) for p in refusal.problems] == [(ordinal, fields)]


@pytest.mark.unit
def test_a_rerun_refuses_a_done_pair_missing_an_image(remote, db_session, override_projects_root, tmp_path):
    from app.models.offsite_object import OffsiteObject
    from app.models.record import RecordImage

    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    _rebuild(db_session, bundle, apply=True)
    img = db_session.query(RecordImage).filter(RecordImage.filename == _name(2, 1)).one()
    db_session.query(OffsiteObject).filter(OffsiteObject.record_image_id == img.id).delete()
    db_session.delete(img)
    db_session.commit()

    refusal = _refused(db_session, bundle, override_projects_root, match="differ from the plan")
    assert [(p["ordinal"], p["fields"]) for p in refusal.problems] == [(2, ["images"])]


@pytest.mark.unit
@pytest.mark.parametrize("change,fields", [
    ({"capturedAt": "2023-03-01T16:00:00+00:00"},
     ["left.created_at", "right.created_at", "manifest.timestamp_utc"]),
    ({"capturedAtSource": "sheet-calibrated"}, ["manifest.provenance.capturedAtSource"]),
])
def test_a_rerun_compares_a_done_records_manifest_time_and_provenance(remote, db_session, override_projects_root,
                                                                      tmp_path, change, fields):
    _volume(db_session)
    _rebuild(db_session, _bundle(tmp_path / "first", remote), apply=True)

    def edit(records):
        records[0].update(change)

    refusal = _refused(db_session, _bundle(tmp_path / "second", remote, plan_over=[edit]),
                       override_projects_root, match="differ from the plan")
    assert [(p["ordinal"], p["fields"]) for p in refusal.problems] == [(1, fields)]


@pytest.mark.unit
@pytest.mark.parametrize("image_over,fields", [
    (lambda images: images[1].update(sourceKey="rionegro/2023/tomo/IMG_0002b.JPG"),
     ["manifest.provenance.left.key"]),
    (lambda images: images[0].update(exifOrientation=1), ["manifest.provenance.right.exifOrientation"]),
])
def test_a_rerun_compares_a_done_records_source_key_and_exif_orientation(
        remote, db_session, override_projects_root, tmp_path, image_over, fields):
    _volume(db_session)
    _rebuild(db_session, _bundle(tmp_path / "first", remote), apply=True)

    refusal = _refused(db_session, _bundle(tmp_path / "second", remote, image_over=[image_over]),
                       override_projects_root, match="differ from the plan")
    assert [(p["ordinal"], p["fields"]) for p in refusal.problems] == [(1, fields)]


@pytest.mark.unit
@pytest.mark.parametrize("line_over,fields", [
    (lambda line: line.update(timestamp_utc="2023-03-01T15:00:01+00:00"), ["manifest.timestamp_utc"]),
    (lambda line: line.update(pair_id=str(uuid.uuid4())), ["manifest.pair_id"]),
    (lambda line: line["files"].pop(), ["manifest.files"]),
    (lambda line: line["files"][1].update(sha256="0" * 64), ["manifest.right.sha256"]),
    (lambda line: line["provenance"]["rebuiltFrom"][0].update(bucket="elsewhere"),
     ["manifest.provenance.left.bucket"]),
    (lambda line: line.update(provenance=None),
     ["manifest.provenance.capturedAtSource", "manifest.provenance.left.bucket", "manifest.provenance.left.key",
      "manifest.provenance.left.exifOrientation", "manifest.provenance.right.bucket",
      "manifest.provenance.right.key", "manifest.provenance.right.exifOrientation"]),
])
def test_a_leftover_line_that_differs_is_refused(remote, db_session, override_projects_root, tmp_path,
                                                 line_over, fields):
    """A line a failed commit left is reused, not appended again, so it must be the plan's in every field."""
    from app.core.rebuild_volume import _capture_line, load_bundle
    from capture.manifestHandler import append_manifest_lines

    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    _, planned = load_bundle(str(bundle), STEM)
    line = json.loads(_capture_line(planned[0], "P"))
    line_over(line)
    append_manifest_lines(override_projects_root / STEM, [json.dumps(line)])

    refusal = _refused(db_session, bundle, override_projects_root, match="manifest")
    assert [(p["ordinal"], p["fields"]) for p in refusal.problems] == [(1, fields)]


@pytest.mark.unit
def test_a_leftover_line_that_is_the_plans_is_reused(remote, db_session, override_projects_root, tmp_path):
    from app.core.rebuild_volume import _capture_line, load_bundle
    from capture.manifestHandler import append_manifest_lines

    _volume(db_session)
    bundle = _bundle(tmp_path, remote)
    _, planned = load_bundle(str(bundle), STEM)
    append_manifest_lines(override_projects_root / STEM, [_capture_line(planned[0], "P")])

    assert len(_rebuild(db_session, bundle, apply=True)["created"]) == 3
    assert [line["capture_id"] for line in _manifest_lines(override_projects_root)] == [
        _capture_id(n) for n in (1, 2, 3)]


# ---------------------------------------------------------------------------
# The source bucket of each image
# ---------------------------------------------------------------------------

def _from_bucket(bucket, orders=(1, 2, 3, 4, 5)):
    """An image_over that names `bucket` as the sourceBucket of the images at `orders`."""
    def change(images):
        for i in images:
            if i["volumeOrder"] in orders:
                i["sourceBucket"] = bucket
    return change


@pytest.mark.unit
def test_a_line_without_a_source_bucket_names_zasqua_transition(remote, db_session, override_projects_root,
                                                                tmp_path):
    _volume(db_session)
    _rebuild(db_session, _bundle(tmp_path, remote), apply=True)

    lines = _manifest_lines(override_projects_root)
    assert [s["bucket"] for line in lines for s in line["provenance"]["rebuiltFrom"]] == ["zasqua-transition"] * 5


@pytest.mark.unit
def test_a_line_naming_zasqua_preservation_names_it_in_rebuilt_from(remote, db_session, override_projects_root,
                                                                    tmp_path):
    _volume(db_session)
    _rebuild(db_session, _bundle(tmp_path, remote, image_over=[_from_bucket("zasqua-preservation")]), apply=True)

    lines = _manifest_lines(override_projects_root)
    for (_, _, files), line in zip(LAYOUT, lines):
        assert line["provenance"]["rebuiltFrom"] == [
            {"role": role, "bucket": "zasqua-preservation", "key": _source(v), "exifOrientation": EXIF[v]}
            for v, _, role, _ in files]


@pytest.mark.unit
@pytest.mark.parametrize("bucket", ["zasqua-elsewhere", "Zasqua-Preservation", "", 7, None, ["zasqua-transition"]])
def test_a_source_bucket_outside_the_allowed_set_is_refused(remote, db_session, override_projects_root, tmp_path,
                                                            bucket):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote, image_over=[_from_bucket(bucket, orders=(3,))])
    refusal = _refused(db_session, bundle, override_projects_root, match="images.jsonl")
    assert refusal.problems == [{"volumeOrder": 3, "reason": "source_bucket_invalid"}]


@pytest.mark.unit
@pytest.mark.parametrize("first,second", [
    ([], [_from_bucket("zasqua-preservation", orders=(1, 2))]),
    ([_from_bucket("zasqua-preservation")], [_from_bucket("zasqua-transition", orders=(1, 2))]),
], ids=["transition-then-preservation", "preservation-then-transition"])
def test_a_rerun_compares_a_done_records_source_bucket(remote, db_session, override_projects_root, tmp_path,
                                                       first, second):
    _volume(db_session)
    _rebuild(db_session, _bundle(tmp_path / "first", remote, image_over=first), apply=True)

    # The second bundle is the first with record 1's two images moved to the other bucket.
    refusal = _refused(db_session, _bundle(tmp_path / "second", remote, image_over=first + second),
                       override_projects_root, match="differ from the plan")
    assert [(p["ordinal"], p["fields"]) for p in refusal.problems] == [
        (1, ["manifest.provenance.left.bucket", "manifest.provenance.right.bucket"])]


# ---------------------------------------------------------------------------
# An opening with a page never photographed: a dual record with `missingSide`
# and its one image, on the other side.
# ---------------------------------------------------------------------------

# The second opening's images: 3 is its right-hand page (_c1), 4 its left (_c0).
OTHER_SIDE = {"left": (3, "right"), "right": (4, "left")}


def _missing_side_over(side):
    """plan_over and image_over that leave the second opening with only the page opposite `side`."""
    keep, _ = OTHER_SIDE[side]
    drop = 4 if keep == 3 else 3

    def plan(records):
        records[1]["missingSide"] = side
        records[1]["images"] = [i for i in records[1]["images"] if i["volumeOrder"] == keep]

    def images(lines):
        lines[:] = [line for line in lines if line["volumeOrder"] != drop]

    return {"plan_over": [plan], "image_over": [images]}


@pytest.mark.unit
@pytest.mark.parametrize("side", ["left", "right"])
def test_a_dual_record_missing_a_side_is_written_with_its_one_image(
        remote, db_session, override_projects_root, tmp_path, side):
    from app.models.record import Record

    _volume(db_session)
    keep, role = OTHER_SIDE[side]
    name = _name(2, 1 if role == "right" else 0)
    bundle = _bundle(tmp_path, remote, **_missing_side_over(side))

    report = _rebuild(db_session, bundle, apply=True)

    assert (report["records"], report["images"]) == (3, 4)
    db_session.expire_all()
    records = db_session.query(Record).order_by(Record.sequence).all()
    assert [(r.capture_mode, r.missing_side) for r in records] == [("dual", None), ("dual", side), ("single", None)]
    [img] = records[1].images
    assert (img.filename, img.role, img.pair_id, img.capture_id, img.is_current) == (
        name, role, _pair_id(2), _capture_id(2), True)
    assert img.master_freed_bytes == len(_master(keep))

    lines = {line["capture_id"]: line for line in _manifest_lines(override_projects_root)}
    line = lines[_capture_id(2)]
    assert line["pair_id"] == _pair_id(2)
    assert [(f["role"], f["relative_path"]) for f in line["files"]] == [(role, f"captures/{name}")]
    assert line["provenance"]["missingSide"] == side
    assert [s["role"] for s in line["provenance"]["rebuiltFrom"]] == [role]
    assert "missingSide" not in lines[_capture_id(1)]["provenance"]
    assert "missingSide" not in lines[_capture_id(3)]["provenance"]

    # A rerun finds it whole.
    again = _rebuild(db_session, bundle, apply=True)
    assert again["created"] == [] and len(again["skipped"]) == 3


@pytest.mark.unit
@pytest.mark.parametrize("edit,reason,ordinal", [
    # both images kept
    (lambda r: r[1].update(missingSide="left"), "missing_side_images_invalid", 2),
    # no image
    (lambda r: r[1].update(missingSide="left", images=[]), "missing_side_images_invalid", 2),
    # the one image on the missing side
    (lambda r: r[1].update(missingSide="left", images=[i for i in r[1]["images"] if i["role"] == "left"]),
     "missing_side_images_invalid", 2),
    (lambda r: r[1].update(missingSide="right", images=[i for i in r[1]["images"] if i["role"] == "right"]),
     "missing_side_images_invalid", 2),
    # bad values
    (lambda r: r[1].update(missingSide="both", images=[i for i in r[1]["images"] if i["role"] == "right"]),
     "missing_side_invalid", 2),
    (lambda r: r[1].update(missingSide="", images=[i for i in r[1]["images"] if i["role"] == "right"]),
     "missing_side_invalid", 2),
    (lambda r: r[1].update(missingSide=0, images=[i for i in r[1]["images"] if i["role"] == "right"]),
     "missing_side_invalid", 2),
    # unhashable JSON values are refused, not raised
    (lambda r: r[1].update(missingSide=[], images=[i for i in r[1]["images"] if i["role"] == "right"]),
     "missing_side_invalid", 2),
    (lambda r: r[1].update(missingSide={}, images=[i for i in r[1]["images"] if i["role"] == "right"]),
     "missing_side_invalid", 2),
    # only a dual record can miss a side
    (lambda r: r[2].update(missingSide="right"), "missing_side_invalid", 3),
])
def test_a_missing_side_record_that_is_not_one_image_on_the_other_side_is_refused(
        remote, db_session, override_projects_root, tmp_path, edit, reason, ordinal):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote, plan_over=_edit(edit))
    _record_refusal(db_session, bundle, override_projects_root, reason, ordinal)


@pytest.mark.unit
def test_a_rerun_compares_a_done_records_missing_side(remote, db_session, override_projects_root, tmp_path):
    from app.models.record import Record

    _volume(db_session)
    bundle = _bundle(tmp_path, remote, **_missing_side_over("left"))
    _rebuild(db_session, bundle, apply=True)
    rec = db_session.query(Record).filter(Record.sequence == 2).one()
    rec.missing_side = None
    db_session.commit()

    refusal = _refused(db_session, bundle, override_projects_root, match="differ from the plan")
    assert [(p["ordinal"], p["fields"]) for p in refusal.problems] == [(2, ["missing_side"])]


# ---------------------------------------------------------------------------
# indexOffset: a volume rebuilt again, its new files numbered after the old ones
# ---------------------------------------------------------------------------

OFFSET = 7  # the numbers the deleted records' files carry, 1 to OFFSET


def _old_capture_id(n: int) -> str:
    return str(uuid.UUID(int=1000 + n, version=4))


def _camera(role: str) -> int:
    return 1 if role == "right" else 0


def _offset_bundle(tmp_path: Path, remote, k=OFFSET, *, plan_over=(), image_over=(), envelope_over=None):
    """The default bundle with `indexOffset` k: each record's files and pair id carry ordinal + k."""
    def shift(records):
        for rec in records:
            n = rec["ordinal"] + k
            if rec["mode"] == "dual":
                rec["pairId"] = _pair_id(n)
            for img in rec["images"]:
                img["filename"] = _name(n, _camera(img["role"]))

    envelope = {"indexOffset": k, "counterAfter": k + COUNTER_AFTER}
    envelope.update(envelope_over or {})
    bundle = _bundle(tmp_path, remote, put=False, plan_over=[shift, *plan_over], image_over=list(image_over),
                     envelope_over=envelope)
    for ordinal, _, files in LAYOUT:
        for v, _, role, _ in files:
            remote.put(_key(_name(ordinal + k, _camera(role))), _master(v))
    return bundle


def _emptied(db, vol, root: Path, *, k=OFFSET, keep=()):
    """A volume whose earlier records, numbered 1 to k, were deleted, keeping the volume row and its counter k.

    Each old record had one image, an offsite row at its key and a capture
    line in the manifest. The records are deleted as the API deletes one
    (the ORM cascade to their images); the offsite rows keep their keys with
    no image, as PostgreSQL's ON DELETE SET NULL leaves them (the test
    database is SQLite without foreign keys, so the update does it here).
    The ordinals in `keep` are not deleted.
    """
    from datetime import datetime, timezone

    from sqlalchemy import text

    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage
    from capture.manifestHandler import append_manifest_lines

    records, lines = [], []
    for n in range(1, k + 1):
        name = _name(n)
        rec = Record(title=f"P - {Path(name).stem}", collection_id=vol.id, capture_mode="single", sequence=n)
        db.add(rec)
        db.flush()
        img = RecordImage(record_id=rec.id, filename=name, file_path=str(root / STEM / "captures" / name),
                          format="jpg", capture_id=_old_capture_id(n), role="single")
        db.add(img)
        db.flush()
        db.add(OffsiteObject(record_image_id=img.id, kind="capture", object_key=_key(name), local_sha1="0" * 40,
                             verified_at=datetime.now(timezone.utc)))
        records.append(rec)
        lines.append(json.dumps({"capture_id": _old_capture_id(n), "project_name": "P", "pair_id": None,
                                 "cameras": [], "files": [{"role": "single", "relative_path": f"captures/{name}",
                                                           "sha256": "0" * 64, "rotate_deg": 0}]}))
    db.commit()
    append_manifest_lines(root / STEM, lines)
    _set_counter(db, vol, k)
    for n, rec in enumerate(records, start=1):
        if n not in keep:
            db.delete(rec)
    db.commit()
    db.execute(text("UPDATE offsite_objects SET record_image_id = NULL "
                    "WHERE record_image_id NOT IN (SELECT id FROM record_images)"))
    db.commit()


@pytest.mark.unit
def test_an_index_offset_names_the_files_after_the_old_numbers(remote, db_session, override_projects_root,
                                                              tmp_path):
    from app.core.capture_naming import take_filename_index
    from app.models.collection import Collection
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record

    vol = _volume(db_session)
    root = override_projects_root
    _emptied(db_session, vol, root)
    bundle = _offset_bundle(tmp_path, remote)

    report = _rebuild(db_session, bundle, apply=True)

    assert len(report["created"]) == 3 and report["counter_before"] == OFFSET
    db_session.expire_all()
    records = db_session.query(Record).filter(Record.collection_id == vol.id).order_by(Record.sequence).all()
    assert [r.sequence for r in records] == [1, 2, 3]
    for (ordinal, mode, files), rec in zip(LAYOUT, records):
        n = ordinal + OFFSET
        images = sorted(rec.images, key=lambda i: i.filename)
        assert [i.filename for i in images] == [_name(n, _camera(role)) for _, _, role, _ in files]
        assert {i.pair_id for i in images} == {_pair_id(n) if mode == "dual" else None}
        for img in images:
            row = db_session.query(OffsiteObject).filter(OffsiteObject.record_image_id == img.id).one()
            assert row.object_key == _key(img.filename)
    names = [_name(o + OFFSET, _camera(role)) for o, _, files in LAYOUT for _, _, role, _ in files]
    assert sorted(f["key"] for f in _freed_lines(root)) == sorted(_key(n) for n in names)
    lines = {line["capture_id"]: line for line in _manifest_lines(root)}
    # The deleted records' lines stay, as history.
    assert set(lines) == {_old_capture_id(n) for n in range(1, OFFSET + 1)} | {_capture_id(n) for n in (1, 2, 3)}
    assert [f["relative_path"] for n in (1, 2, 3) for f in lines[_capture_id(n)]["files"]] == [
        f"captures/{name}" for name in names]
    assert lines[_capture_id(1)]["pair_id"] == _pair_id(1 + OFFSET)
    assert db_session.get(Collection, vol.id).capture_counter == OFFSET + COUNTER_AFTER

    index = take_filename_index(db_session, vol.id, STEM, root / STEM / "captures")
    db_session.rollback()
    assert index == f"{STEM}-img_{OFFSET + COUNTER_AFTER + 1:04d}"


@pytest.mark.unit
@pytest.mark.parametrize("side", ["left", "right"])
def test_a_missing_side_and_a_single_record_under_an_offset_are_named_after_it(
        remote, db_session, override_projects_root, tmp_path, side):
    from app.models.record import Record

    vol = _volume(db_session)
    _emptied(db_session, vol, override_projects_root)
    keep, role = OTHER_SIDE[side]
    over = _missing_side_over(side)
    bundle = _offset_bundle(tmp_path, remote, plan_over=over["plan_over"], image_over=over["image_over"])

    _rebuild(db_session, bundle, apply=True)

    db_session.expire_all()
    records = db_session.query(Record).filter(Record.collection_id == vol.id).order_by(Record.sequence).all()
    [img] = records[1].images
    assert (records[1].sequence, records[1].missing_side) == (2, side)
    assert (img.filename, img.role, img.pair_id) == (_name(2 + OFFSET, _camera(role)), role, _pair_id(2 + OFFSET))
    [single] = records[2].images
    assert (records[2].sequence, single.filename, single.pair_id) == (3, _name(3 + OFFSET), None)


@pytest.mark.unit
def test_an_emptied_volume_with_its_counter_at_the_offset_is_accepted(remote, db_session, override_projects_root,
                                                                      tmp_path):
    vol = _volume(db_session)
    _emptied(db_session, vol, override_projects_root)
    report = _rebuild(db_session, _offset_bundle(tmp_path, remote), apply=False)
    assert report["counter_before"] == OFFSET and len(report["created"]) == 3


@pytest.mark.unit
def test_an_emptied_volume_with_its_counter_above_the_offset_is_refused(remote, db_session,
                                                                         override_projects_root, tmp_path):
    vol = _volume(db_session)
    _emptied(db_session, vol, override_projects_root)
    _set_counter(db_session, vol, OFFSET + 1)
    _refused(db_session, _offset_bundle(tmp_path, remote), override_projects_root, match="capture counter")


@pytest.mark.unit
def test_an_emptied_volume_with_a_record_left_is_refused(remote, db_session, override_projects_root, tmp_path):
    vol = _volume(db_session)
    _emptied(db_session, vol, override_projects_root, keep=(OFFSET,))
    refusal = _refused(db_session, _offset_bundle(tmp_path, remote), override_projects_root,
                       match="already has records")
    assert [p["filenames"] for p in refusal.problems] == [[_name(OFFSET)]]


@pytest.mark.unit
def test_an_old_manifest_line_whose_capture_id_an_image_still_holds_is_refused(
        remote, db_session, override_projects_root, tmp_path):
    from app.models.record import Record, RecordImage

    vol = _volume(db_session)
    _emptied(db_session, vol, override_projects_root)
    other = _volume(db_session, name="Tomo 2", stem="xx-test-002")
    rec = Record(title="t", collection_id=other.id, capture_mode="single", sequence=1)
    db_session.add(rec)
    db_session.flush()
    db_session.add(RecordImage(record_id=rec.id, filename="x.jpg", file_path="/nowhere/x.jpg", format="jpg",
                               capture_id=_old_capture_id(3), role="single"))
    db_session.commit()

    refusal = _refused(db_session, _offset_bundle(tmp_path, remote), override_projects_root, match="manifest")
    assert [(p["capture_id"], p["reason"]) for p in refusal.problems] == [
        (_old_capture_id(3), "manifest_capture_not_planned")]


@pytest.mark.unit
def test_an_old_manifest_line_numbered_above_the_offset_is_refused(remote, db_session, override_projects_root,
                                                                    tmp_path):
    """Only the deleted records' numbers, 1 to the offset, are history; the new ones are the plan's."""
    from capture.manifestHandler import append_manifest_lines

    vol = _volume(db_session)
    _emptied(db_session, vol, override_projects_root)
    append_manifest_lines(override_projects_root / STEM, [json.dumps({
        "capture_id": str(uuid.uuid4()), "files": [{"role": "single", "relative_path":
                                                    f"captures/{_name(OFFSET + 1)}", "sha256": "0" * 64}]})])
    _refused(db_session, _offset_bundle(tmp_path, remote), override_projects_root, match="manifest")


@pytest.mark.unit
def test_a_counter_after_below_the_offset_and_the_records_is_refused(remote, db_session, override_projects_root,
                                                                     tmp_path):
    vol = _volume(db_session)
    _emptied(db_session, vol, override_projects_root)
    bundle = _offset_bundle(tmp_path, remote, envelope_over={"counterAfter": OFFSET + 2})
    _refused(db_session, bundle, override_projects_root, match="counterAfter")


@pytest.mark.unit
@pytest.mark.parametrize("offset", [-1, 1.5, "7", True, None, 10000])
def test_an_index_offset_that_is_not_a_whole_number_in_range_is_refused(
        remote, db_session, override_projects_root, tmp_path, offset):
    _volume(db_session)
    bundle = _bundle(tmp_path, remote, envelope_over={"indexOffset": offset})
    refusal = _refused(db_session, bundle, override_projects_root, match="indexOffset")
    assert refusal.problems == [{"reason": "index_offset_invalid", "value": offset}]


@pytest.mark.unit
def test_an_offset_plan_whose_files_carry_the_ordinals_is_refused(remote, db_session, override_projects_root,
                                                                  tmp_path):
    """With an offset, a file numbered by its ordinal alone is another record's old name."""
    vol = _volume(db_session)
    _emptied(db_session, vol, override_projects_root)
    bundle = _bundle(tmp_path, remote, envelope_over={"indexOffset": OFFSET, "counterAfter": OFFSET + 5})
    refusal = _refused(db_session, bundle, override_projects_root, match="plan.json")
    assert {p["reason"] for p in refusal.problems} == {"pair_id_invalid", "filename_not_its_ordinal"}


@pytest.mark.unit
def test_a_rerun_after_an_offset_run_is_a_rerun(remote, db_session, override_projects_root, tmp_path):
    vol = _volume(db_session)
    _emptied(db_session, vol, override_projects_root)
    bundle = _offset_bundle(tmp_path, remote)
    _rebuild(db_session, bundle, apply=True)
    before = _snapshot(db_session, override_projects_root)

    report = _rebuild(db_session, bundle, apply=True)

    assert report["created"] == [] and len(report["skipped"]) == 3
    after = _snapshot(db_session, override_projects_root)
    assert {k: v for k, v in after.items() if k != "logs"} == {k: v for k, v in before.items() if k != "logs"}


@pytest.mark.unit
def test_a_rerun_after_an_interrupted_offset_run_completes_the_rest(remote, db_session, override_projects_root,
                                                                    tmp_path, monkeypatch):
    vol = _volume(db_session)
    _emptied(db_session, vol, override_projects_root)
    bundle = _offset_bundle(tmp_path, remote)
    _interrupted(db_session, bundle, monkeypatch, on_call=3)

    report = _rebuild(db_session, bundle, apply=True)

    assert [c["capture_id"] for c in report["created"]] == [_capture_id(2), _capture_id(3)]
    assert [s["capture_id"] for s in report["skipped"]] == [_capture_id(1)]
