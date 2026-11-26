"""The manifest lines of an adoption, read back through the readers that use them.

adopt_manifest_lines (app.core.adopt_manifest) takes an adoption's plan and
the source's manifest lines and returns the lines for the target's manifest
and those for the source's. These tests check those lines with the code
that reads manifests, not with their own shapes: the fixity index
(app.core.integrity), the hash check and the integrity report, the Fisqua
capture-manifest export (app.core.capture_manifest), the startup repair of
a volume's manifest (app.core.volume_layout), the numbers a manifest
reserves (app.core.capture_naming) and the export bag's capture records.

The source volume is written by the codebase's own writers: captures through
capture.manifestHandler.generate_manifest_record and append_manifest_record,
an upload through append_upload_record, and digests for previews captured
without one through app.core.preview_digests. It holds a dual record of RAW
masters with a superseded pair (number 1) and its retake (number 2), a dual
record of RAW masters whose previews' digests were backfilled (number 3), a
JPEG master with its `.raw` buffer (number 4) and an uploaded JPEG (number
5). Every capture and preview has a verified offsite row with its real SHA1.
The adoption itself is simulated as the apply step will run it: the files
are renamed and moved as the plan says, the rows changed (the record's
volume; the image's filename, file_path, thumbnail_path and pair_id), the
offsite rows given the new keys as the upload under new keys leaves them,
and the lines appended with append_manifest_lines.

    python -m pytest tests/unit/test_adopt_manifest.py

Version: 0.3.3
"""

import copy
import hashlib
import json
import uuid
from pathlib import Path

import pytest

from tests.unit.test_adopt_captures_plan import (  # noqa: F401
    SRC, TGT, TENANT, _now, _plan, _record, _volume, tenant,
)

ADOPTED_AT = "2026-11-19T12:00:00+00:00"


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _offsite(db, img, kind, key, data):
    from app.models.offsite_object import OffsiteObject

    db.add(OffsiteObject(record_image_id=img.id, kind=kind, object_key=key, local_sha1=hashlib.sha1(data).hexdigest(),
                         uploaded_at=_now(), verified_at=_now()))


def _row(db, rec, master: Path, capture_id, pair_id, role, current, root):
    from app.models.record import RecordImage

    thumb = _write(root / SRC / "thumbnails" / f"{uuid.uuid4().hex}_thumb.jpg", b"thumb " + master.name.encode())
    img = RecordImage(record_id=rec.id, filename=master.name, file_path=str(master), thumbnail_path=str(thumb),
                      capture_id=capture_id, file_size=master.stat().st_size,
                      format=master.suffix.lstrip(".").upper(), role=role, is_current=current,
                      superseded_at=None if current else _now(), pair_id=pair_id)
    db.add(img)
    db.commit()
    return img


def _shoot(db, rec, root, number, *, cams=(0, 1), ext="CR2", current=True, preview_digest=True, buffer=False):
    """One capture event written as capture writes it; returns its images by camera."""
    from capture.camera import CameraConfig
    from capture.manifestHandler import append_manifest_record, generate_manifest_record

    from app.core.paths import preview_sidecar_of

    vol = root / SRC
    pair_id = f"{SRC}-img_{number:04d}"
    masters, paths = [], []
    for cam in cams:
        master = _write(vol / "captures" / f"{pair_id}_c{cam}.{ext}", f"{ext} {number} c{cam}".encode())
        masters.append(master)
        if buffer:
            paths.append((master, _write(master.with_suffix(".raw"), f"buffer {number} c{cam}".encode())))
        else:
            paths.append(master)
        if ext == "CR2" and preview_digest:
            _write(preview_sidecar_of(master), b"preview " + master.read_bytes())
    roles = ["left", "right"] if len(cams) == 2 else ["single"]
    record = generate_manifest_record("Fondo 10", paths, [CameraConfig(camera_index=c) for c in cams],
                                      pair_id=pair_id if len(cams) == 2 else None, roles=roles, project_root=vol)
    append_manifest_record(vol, record)
    if ext == "CR2" and not preview_digest:
        for master in masters:   # extracted after the record was written, so it names no preview
            _write(preview_sidecar_of(master), b"preview " + master.read_bytes())
    images = {}
    for cam, master, role in zip(cams, masters, roles):
        img = _row(db, rec, master, record.capture_id, pair_id, role, current, root)
        _offsite(db, img, "capture", f"{TENANT}/{SRC}/captures/{master.name}", master.read_bytes())
        if ext == "CR2":
            preview = preview_sidecar_of(master)
            _offsite(db, img, "preview", f"{TENANT}/{SRC}/previews/{preview.name}", preview.read_bytes())
        db.commit()
        images[(number, cam)] = img
    return images


def _upload(db, rec, root, number):
    """An image uploaded into the volume, recorded as POST /records/{id}/images records it."""
    from capture.manifestHandler import append_upload_record

    master = _write(root / SRC / "captures" / f"{SRC}-img_{number:04d}_c0.jpg", b"uploaded jpeg")
    capture_id = str(uuid.uuid4())
    append_upload_record(root / SRC, capture_id=capture_id, relative_path=f"captures/{master.name}",
                         size=master.stat().st_size, mimetype="image/jpeg",
                         sha256=hashlib.sha256(master.read_bytes()).hexdigest())
    img = _row(db, rec, master, capture_id, f"{SRC}-img_{number:04d}", "single", True, root)
    _offsite(db, img, "capture", f"{TENANT}/{SRC}/captures/{master.name}", master.read_bytes())
    db.commit()
    return {(number, 0): img}


def _source(db, root):
    """The source and target volumes of the module docstring, and the source's images by (number, camera)."""
    from app.core.preview_digests import backfill_preview_digests

    src = _volume(db, "Caja 80 carpeta 8", SRC, project="Fondo 10")
    tgt = _volume(db, "Caja 080 carpeta 008", TGT, external_id=TGT, project="Fondo 13")
    r1 = _record(db, src, "ff. 1-2")
    images = _shoot(db, r1, root, 1, current=False)
    images.update(_shoot(db, r1, root, 2))
    r2 = _record(db, src, "ff. 3-4")
    images.update(_shoot(db, r2, root, 3, preview_digest=False))
    r3 = _record(db, src, "f. 5", mode="single")
    images.update(_shoot(db, r3, root, 4, cams=(0,), ext="jpg", buffer=True))
    r4 = _record(db, src, "f. 6", mode="single")
    images.update(_upload(db, r4, root, 5))
    src.capture_counter = 5
    db.commit()
    report = backfill_preview_digests(db, SRC, apply=True)
    assert len(report["backfilled"]) == 2, report
    return src, tgt, images


def _lines(manifest: Path) -> list:
    from app.core.integrity import read_manifest_lines

    out = []
    for line in read_manifest_lines(manifest)[0]:
        if line.strip():
            out.append(json.loads(line))
    return out


def _build(db, root):
    """The source, the target, the source's images, a plan that may adopt, and the two lists of lines."""
    from app.core.adopt_manifest import adopt_manifest_lines

    src, tgt, images = _source(db, root)
    plan = _plan(db, src, tgt)
    assert plan.refusals == [], plan.details
    target_lines, source_lines = adopt_manifest_lines(plan, _lines(root / SRC / "metadata" / "manifest.jsonl"),
                                                      adopted_at=ADOPTED_AT)
    return src, tgt, images, plan, target_lines, source_lines


def _adopt(db, root, plan, tgt, target_lines, source_lines, *, write=("target", "source")):
    """Do what the apply step will: rename and move the files, change the rows, append the lines."""
    from capture.manifestHandler import append_manifest_lines

    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage

    for r in plan.renames:
        for old, new in ((r.old_path, r.new_path), (r.old_preview_path, r.new_preview_path),
                         (r.old_raw_buffer_path, r.new_raw_buffer_path),
                         (r.old_thumbnail_path, r.new_thumbnail_path)):
            if old is not None and new is not None and old != new:
                new.parent.mkdir(parents=True, exist_ok=True)
                old.rename(new)
        img = db.get(RecordImage, r.image_id)
        img.filename, img.file_path, img.pair_id = r.new_name, str(r.new_path), r.new_pair_id
        if r.new_thumbnail_path is not None:
            img.thumbnail_path = str(r.new_thumbnail_path)
        db.get(Record, r.record_id).collection_id = tgt.id
        for kind, key in r.new_keys.items():
            db.query(OffsiteObject).filter(OffsiteObject.record_image_id == r.image_id,
                                           OffsiteObject.kind == kind).update({"object_key": key})
    db.commit()
    if "target" in write:
        append_manifest_lines(root / TGT, [json.dumps(line, ensure_ascii=False) for line in target_lines])
    if "source" in write:
        append_manifest_lines(root / SRC, [json.dumps(line, ensure_ascii=False) for line in source_lines])


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# The lines
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_target_gets_each_capture_line_and_preview_digest_with_the_new_paths(db_session, override_projects_root, tenant):
    root = override_projects_root
    _, _, images, plan, target_lines, _ = _build(db_session, root)
    source = _lines(root / SRC / "metadata" / "manifest.jsonl")

    captures = [line for line in source if line.get("record_type") != "preview_digest"]
    digests = [line for line in source if line.get("record_type") == "preview_digest"]
    assert len(target_lines) == len(captures) + len(digests) == 5 + 2
    for original, copied in zip(source, target_lines):
        assert copied["copied_from"] == f"{SRC}/metadata/manifest.jsonl"
        assert {k: v for k, v in copied.items() if k not in ("files", "pair_id", "copied_from")} == \
            {k: v for k, v in original.items() if k not in ("files", "pair_id")}
        for f_old, f_new in zip(original["files"], copied["files"]):
            assert f_new["relative_path"] == f_old["relative_path"].replace(f"{SRC}-img_", f"{TGT}-img_")
            assert {k: v for k, v in f_new.items() if k != "relative_path"} == \
                {k: v for k, v in f_old.items() if k != "relative_path"}
        if original.get("pair_id"):
            assert copied["pair_id"] == original["pair_id"].replace(SRC, TGT)
    assert [f["relative_path"] for f in target_lines[0]["files"]] == [
        f"captures/{TGT}-img_0001_c0.CR2", f"previews/{TGT}-img_0001_c0_preview.jpg",
        f"captures/{TGT}-img_0001_c1.CR2", f"previews/{TGT}-img_0001_c1_preview.jpg"]


@pytest.mark.unit
def test_the_source_gets_one_relocation_record_per_master_moved(db_session, override_projects_root, tenant):
    root = override_projects_root
    _, _, images, plan, _, source_lines = _build(db_session, root)

    assert source_lines == [
        {"record_type": "relocation", "capture_id": r.capture_id, "image_id": r.image_id,
         "sha256": _sha(r.old_path), "from": f"captures/{r.old_name}", "to": f"../{TGT}/captures/{r.new_name}",
         "relocated_at": ADOPTED_AT}
        for r in plan.renames]


@pytest.mark.unit
def test_lines_of_capture_ids_no_image_has_and_earlier_relocations_are_not_copied(db_session, override_projects_root, tenant):
    from app.core.adopt_manifest import adopt_manifest_lines

    root = override_projects_root
    _, _, images, plan, _, _ = _build(db_session, root)
    source = _lines(root / SRC / "metadata" / "manifest.jsonl")
    owned = images[(2, 0)].capture_id
    extra = [
        {"capture_id": "gone", "files": [{"role": "single", "relative_path": "captures/gone.CR2", "sha256": "5" * 64}]},
        {"record_type": "relocation", "capture_id": "gone", "from": "captures/a.CR2", "to": "captures/b.CR2"},
        {"record_type": "relocation", "capture_id": owned, "from": "captures/x.CR2", "to": "captures/y.CR2"},
        "not an object",
    ]

    target_lines, _ = adopt_manifest_lines(plan, source + extra, adopted_at=ADOPTED_AT)

    assert len(target_lines) == 7
    assert all(line["capture_id"] != "gone" and line.get("record_type") != "relocation" for line in target_lines)


@pytest.mark.unit
def test_the_lines_are_a_pure_function_of_the_plan_and_the_source_lines(db_session, override_projects_root, tenant):
    from app.core.adopt_manifest import adopt_manifest_lines

    root = override_projects_root
    _, _, _, plan, target_lines, source_lines = _build(db_session, root)
    source = _lines(root / SRC / "metadata" / "manifest.jsonl")
    before_source, before_renames = copy.deepcopy(source), copy.deepcopy(plan.renames)

    again = adopt_manifest_lines(plan, source, adopted_at=ADOPTED_AT)

    assert again == (target_lines, source_lines)
    assert source == before_source and plan.renames == before_renames
    json.dumps(again)
    again[0][0]["files"][0]["relative_path"] = "changed"
    assert source == before_source


@pytest.mark.unit
def test_a_plan_with_a_refusal_has_no_lines(db_session, override_projects_root, tenant):
    from app.core.adopt_manifest import adopt_manifest_lines

    root = override_projects_root
    src, tgt, _ = _source(db_session, root)
    _record(db_session, tgt, "already here")
    plan = _plan(db_session, src, tgt)

    with pytest.raises(ValueError):
        adopt_manifest_lines(plan, _lines(root / SRC / "metadata" / "manifest.jsonl"), adopted_at=ADOPTED_AT)


# ---------------------------------------------------------------------------
# The readers, after the adoption
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("write", [("target",), ("source",), ("target", "source")])
def test_the_index_gives_every_master_and_preview_its_digest_at_its_new_path(db_session, override_projects_root, tenant, write):
    """The target's lines alone, the source's alone, and both, each give every file its capture-time digest."""
    from app.core.integrity import build_fixity_index

    root = override_projects_root
    _, tgt, _, plan, target_lines, source_lines = _build(db_session, root)
    shas = {r.image_id: (_sha(r.old_path), r.old_preview_path and _sha(r.old_preview_path),
                         r.old_raw_buffer_path and _sha(r.old_raw_buffer_path)) for r in plan.renames}
    _adopt(db_session, root, plan, tgt, target_lines, source_lines, write=write)

    index = build_fixity_index(root.resolve())

    for r in plan.renames:
        master, preview, buffer = shas[r.image_id]
        new = r.new_path.resolve()
        assert index.expected(r.capture_id, r.image_id, new) == master == _sha(new)
        if "target" in write:
            assert index.expected(r.capture_id, None, new) == master
            assert index.by_path[r.capture_id][str(new)] == master
        if r.new_preview_path is not None:
            p = r.new_preview_path.resolve()
            assert index.by_path[r.capture_id][str(p)] == preview == _sha(p)
            assert str(p) in index.previews
        if r.new_raw_buffer_path is not None:
            b = r.new_raw_buffer_path.resolve()
            assert index.expected(r.capture_id, None, b) == buffer == _sha(b)


@pytest.mark.unit
def test_the_hash_check_and_the_integrity_report_find_nothing_wrong(db_session, override_projects_root, tenant):
    """The target's images check clean; the empty source adds nothing, as the report reads rows, not manifests."""
    from app.core.integrity import run_integrity_check, verify_images_against_manifest
    from app.models.record import Record, RecordImage

    root = override_projects_root
    src, tgt, _, plan, target_lines, source_lines = _build(db_session, root)
    _adopt(db_session, root, plan, tgt, target_lines, source_lines)
    adopted = (db_session.query(RecordImage).join(Record, RecordImage.record_id == Record.id)
               .filter(Record.collection_id == tgt.id).all())
    assert len(adopted) == len(plan.renames)
    assert db_session.query(Record).filter(Record.collection_id == src.id).count() == 0

    assert verify_images_against_manifest(adopted) == []
    report = run_integrity_check(db_session)
    assert report["missing_files"] == [] and report["manifest_mismatches"] == []
    assert report["manifest_missing_entry"] == []
    assert [p for p in report["orphan_files"] if str(root.resolve()) in p] == []
    assert report["summary"]["hashes_checked"] >= len(adopted)


@pytest.mark.unit
def test_the_fisqua_export_finds_every_master_and_preview_digest(db_session, override_projects_root, tenant):
    from app.core import capture_manifest
    from app.core.paths import preview_sidecar_of
    from app.core.integrity import build_fixity_index
    from app.models.record import RecordImage

    root = override_projects_root
    _, tgt, _, plan, target_lines, source_lines = _build(db_session, root)
    _adopt(db_session, root, plan, tgt, target_lines, source_lines)

    index = build_fixity_index(root.resolve())
    for r in plan.renames:
        img = db_session.get(RecordImage, r.image_id)
        master = capture_manifest._resolved(img.file_path)
        assert capture_manifest._digest(index, img.capture_id, img.id, master, r.new_name) == _sha(master)
        if r.new_preview_path is not None:
            preview = preview_sidecar_of(master)
            assert capture_manifest._digest(index, img.capture_id, None, preview,
                                            r.new_keys["preview"].rsplit("/", 1)[-1]) == _sha(preview)
    try:
        capture_manifest.build_capture_manifest(db_session, tgt)
        rules = set()
    except capture_manifest.ManifestRefused as refused:
        rules = {p.rule for p in refused.problems}
    assert rules.isdisjoint({"sha256_missing", "preview_sha256_missing", "key_outside", "filename_form",
                             "raw_without_preview", "preview_not_uploaded", "not_uploaded"}), rules


@pytest.mark.unit
def test_the_startup_repair_appends_nothing_to_the_target(db_session, override_projects_root, tenant):
    from app.core.volume_layout import migrate_volume_layout

    root = override_projects_root
    _, tgt, _, plan, target_lines, source_lines = _build(db_session, root)
    _adopt(db_session, root, plan, tgt, target_lines, source_lines)
    manifests = {stem: (root / stem / "metadata" / "manifest.jsonl").read_bytes() for stem in (SRC, TGT)}

    report = migrate_volume_layout(db_session)

    assert report["out_of_place"] == 0
    assert report["manifests_repaired"] == {} and report["captures_without_record"] == {}
    assert {stem: (root / stem / "metadata" / "manifest.jsonl").read_bytes() for stem in (SRC, TGT)} == manifests


@pytest.mark.unit
def test_without_the_targets_lines_the_startup_repair_copies_the_sources_under_their_old_names(db_session, override_projects_root, tenant):
    """When only the source's lines were written, the repair copies the source's records into the target.

    _records_to_copy rebases each line onto the target's root, so the copies
    name the old files (`../<source stem>/captures/<old name>`); the digests
    at the new paths come from the source's relocation records.
    """
    from app.core.capture_naming import manifest_capture_numbers
    from app.core.integrity import build_fixity_index
    from app.core.volume_layout import migrate_volume_layout

    root = override_projects_root
    _, tgt, _, plan, target_lines, source_lines = _build(db_session, root)
    _adopt(db_session, root, plan, tgt, target_lines, source_lines, write=("source",))

    report = migrate_volume_layout(db_session)

    copied = _lines(root / TGT / "metadata" / "manifest.jsonl")
    assert report["manifests_repaired"] == {tgt.id: len(copied)}
    captures = [line for line in copied if line.get("record_type") not in ("relocation", "preview_digest")]
    assert len(captures) == 5
    assert all(f["relative_path"].startswith(f"../{SRC}/") for line in captures for f in line["files"])
    assert manifest_capture_numbers(root / TGT / "metadata" / "manifest.jsonl", TGT) == {1, 2, 3, 4, 5}
    index = build_fixity_index(root.resolve())
    for r in plan.renames:
        assert index.expected(r.capture_id, r.image_id, r.new_path.resolve()) == _sha(r.new_path)


@pytest.mark.unit
def test_the_targets_manifest_reserves_exactly_the_adopted_numbers(db_session, override_projects_root, tenant):
    from app.core.capture_naming import manifest_capture_numbers

    root = override_projects_root
    _, tgt, _, plan, target_lines, source_lines = _build(db_session, root)
    _adopt(db_session, root, plan, tgt, target_lines, source_lines)

    assert manifest_capture_numbers(root / TGT / "metadata" / "manifest.jsonl", TGT) == {1, 2, 3, 4, 5}
    assert manifest_capture_numbers(root / SRC / "metadata" / "manifest.jsonl", SRC) == {1, 2, 3, 4, 5}


@pytest.mark.unit
def test_without_a_volume_the_capture_records_are_the_sources_own(db_session, override_projects_root, tenant):
    """Asked for capture ids alone, capture_record_lines prefers the capture-time lines to `copied_from` copies."""
    from app.core.integrity import capture_record_lines

    root = override_projects_root
    _, tgt, _, plan, target_lines, source_lines = _build(db_session, root)
    source = [line for line in _lines(root / SRC / "metadata" / "manifest.jsonl")
              if line.get("record_type") != "preview_digest"]
    _adopt(db_session, root, plan, tgt, target_lines, source_lines)

    lines = [json.loads(line) for line in capture_record_lines(root.resolve(), {r.capture_id for r in plan.renames})]

    assert lines == source


@pytest.mark.unit
def test_the_bag_takes_one_capture_record_per_capture_with_the_new_paths(db_session, override_projects_root, tenant):
    """The export bag asks for the volume's own lines first, so the adopted copies are the ones it takes."""
    from app.core.export_service import _filtered_manifest_blob
    from app.models.record import Record

    root = override_projects_root
    _, tgt, _, plan, target_lines, source_lines = _build(db_session, root)
    _adopt(db_session, root, plan, tgt, target_lines, source_lines)
    db_session.expire_all()
    records = db_session.query(Record).filter(Record.collection_id == tgt.id).all()

    lines = [json.loads(line) for line in _filtered_manifest_blob(db_session, tgt, records).decode().splitlines()]

    current = {r.capture_id for r in plan.renames if r.is_current}
    assert sorted(line["capture_id"] for line in lines) == sorted(current)
    paths = [f["relative_path"] for line in lines for f in line["files"]]
    assert paths and all(f"{TGT}-img_" in p for p in paths) and not any(f"{SRC}-img_" in p for p in paths)
    assert all(line["copied_from"] == f"{SRC}/metadata/manifest.jsonl" for line in lines)
