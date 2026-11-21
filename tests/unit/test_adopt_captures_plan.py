"""The plan for adopting a hand-made volume's captures into an empty volume Fisqua assigned.

plan_adopt_captures (app.core.adopt_captures) reads the catalogue, the disk,
the source's capture manifest and offsite_objects, and returns what an
adoption would do and every reason it may not run. It writes nothing: no
row, no file, no bucket call, and it flushes nothing pending in the session.
The fixtures are real rows in the test database and real files under the
test projects root, laid out as a stemmed volume keeps them (`captures/`,
`previews/`, `thumbnails/`, `metadata/manifest.jsonl`), with offsite_objects
rows written as a run of the upload leaves them. No bucket is involved,
since planning lists nothing.

The default source volume is in project "Fondo 10" and the target in "Fondo
13". The source has two dual records of RAW masters, each with its preview
and thumbnail, both cameras of an event sharing its number and capture id:
the first record holds a superseded pair (number 1) and its current retake
(number 2), the second a current pair (number 3). Each event has one capture
line in the manifest naming its masters and previews with their sha256, and
every capture and preview has a verified offsite row under the source's stem.

    python -m pytest tests/unit/test_adopt_captures_plan.py

Version: 0.3.3
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

SRC = "co-ahr-con-caj80-car008"
TGT = "co-ahr-con-caj080-car008"
TENANT = "captua-test"


@pytest.fixture(autouse=True)
def own_data_dir(tmp_path, override_projects_root):
    """A data directory of the test's own, so an adoption journal never outlives its test.

    After override_projects_root, which reloads app.core.config and so
    replaces the settings object this patches; with a MonkeyPatch of its
    own, so a test's monkeypatch.undo() leaves it in place.
    """
    from app.core import config

    data = tmp_path / "adopt-data"
    data.mkdir(exist_ok=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(config.settings, "DATA_DIR", str(data))
        yield data


@pytest.fixture
def tenant(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", TENANT)
    return TENANT


def _now():
    return datetime.now(timezone.utc)


def _project(db, name):
    from app.models.project import Project

    project = db.query(Project).filter(Project.name == name).one_or_none()
    if project is None:
        project = Project(name=name)
        db.add(project)
        db.commit()
    return project


def _volume(db, name, stem, external_id=None, project="Fondo 10", **fields):
    from app.models.collection import Collection

    vol = Collection(name=name, project_id=_project(db, project).id, filename_stem=stem,
                     external_id=external_id, **fields)
    db.add(vol)
    db.commit()
    return vol


def _record(db, vol, title, status="captured", mode="dual"):
    from app.models.record import Record

    rec = Record(title=title, collection_id=vol.id, capture_mode=mode, status=status)
    db.add(rec)
    db.commit()
    return rec


def _name(stem, n, cam=0, ext="CR2"):
    return f"{stem}-img_{n:04d}_c{cam}.{ext}"


def _manifest_line(root, stem, line):
    metadata = root / stem / "metadata"
    metadata.mkdir(parents=True, exist_ok=True)
    with open(metadata / "manifest.jsonl", "a") as fh:
        fh.write(json.dumps(line) + "\n")


def _image(db, rec, root, stem, name, data, *, capture_id=None, current=True, verified=True, thumbnail=True,
           preview=True, digest=True, preview_digest=True):
    """An image of `rec` with its files in `<root>/<stem>/` and, when `verified`, verified offsite rows.

    A RAW master gets a preview unless `preview` is false. Returns the image
    and the manifest entries of its files, each with its sha256, except the
    master's when `digest` is false and the preview's when `preview_digest` is.
    """
    from app.core.paths import is_raw_suffix, preview_sidecar_of
    from app.models.offsite_object import OffsiteObject
    from app.models.record import RecordImage

    master = root / stem / "captures" / name
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_bytes(data)
    files = [{"role": "single", "relative_path": f"captures/{name}"}]
    if digest:
        files[0]["sha256"] = hashlib.sha256(data).hexdigest()
    side = None
    if preview and is_raw_suffix(master):
        side = preview_sidecar_of(master)
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_bytes(b"preview " + data)
        files.append({"role": "single_preview", "relative_path": f"previews/{side.name}"})
        if preview_digest:
            files[-1]["sha256"] = hashlib.sha256(b"preview " + data).hexdigest()
    thumb = None
    if thumbnail:
        thumb = root / stem / "thumbnails" / f"{uuid.uuid4().hex}_thumb.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"thumb " + name.encode())
    img = RecordImage(record_id=rec.id, filename=name, file_path=str(master),
                      thumbnail_path=str(thumb) if thumb else None, capture_id=capture_id,
                      file_size=len(data), format=Path(name).suffix.lstrip(".").upper() or "X",
                      is_current=current, superseded_at=None if current else _now(),
                      pair_id=name.rsplit("_c", 1)[0])
    db.add(img)
    db.commit()
    if verified:
        db.add(OffsiteObject(record_image_id=img.id, kind="capture", object_key=f"{TENANT}/{stem}/captures/{name}",
                             local_sha1="0" * 40, uploaded_at=_now(), verified_at=_now()))
        if side is not None:
            db.add(OffsiteObject(record_image_id=img.id, kind="preview",
                                 object_key=f"{TENANT}/{stem}/previews/{side.name}",
                                 local_sha1="1" * 40, uploaded_at=_now(), verified_at=_now()))
        db.commit()
    return img, files


def _capture(db, rec, root, stem, number, *, cams=(0, 1), ext="CR2", current=True, line=True, **kw):
    """One capture event: an image per camera, sharing the number and a capture id, and its manifest line."""
    capture_id = str(uuid.uuid4())
    images, files = {}, []
    for cam in cams:
        img, f = _image(db, rec, root, stem, _name(stem, number, cam, ext), f"{ext} {number} c{cam}".encode(),
                        capture_id=capture_id, current=current, **kw)
        images[(number, cam)] = img
        files += f
    if line:
        _manifest_line(root, stem, {"capture_id": capture_id, "pair_id": f"{stem}-img_{number:04d}", "files": files})
    return images


def _default(db, root):
    """The source and target volumes of the module docstring, and the source's images by (number, camera)."""
    src = _volume(db, "Caja 80 carpeta 8", SRC, project="Fondo 10")
    tgt = _volume(db, "Caja 080 carpeta 008", TGT, external_id=TGT, project="Fondo 13")
    r1 = _record(db, src, "ff. 1-2")
    images = _capture(db, r1, root, SRC, 1, current=False)
    images.update(_capture(db, r1, root, SRC, 2))
    r2 = _record(db, src, "ff. 3-4")
    images.update(_capture(db, r2, root, SRC, 3))
    src.capture_counter = 3
    db.commit()
    return src, tgt, images


def _plan(db, src, tgt):
    from app.core.adopt_captures import plan_adopt_captures

    return plan_adopt_captures(db, src.id if hasattr(src, "id") else src, tgt.id if hasattr(tgt, "id") else tgt)


def _snapshot(db, root):
    """Every row of every table, and every file under the projects root with its size and mtime."""
    from app.core.db import Base

    db.expire_all()
    rows = {}
    for table in Base.metadata.sorted_tables:
        rows[table.name] = sorted(tuple(str(v) for v in r) for r in db.execute(table.select()).fetchall())
    files = sorted((str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
                   for p in root.rglob("*"))
    return rows, files


# ---------------------------------------------------------------------------
# The plan for a volume that may be adopted
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_dual_raw_volume_with_retakes_may_be_adopted_and_every_image_is_renamed(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, images = _default(db_session, root)

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == [], plan.details
    assert plan.may_adopt
    assert plan.source_stem == SRC and plan.target_stem == TGT
    assert [r.image_id for r in plan.renames] == sorted(img.id for img in images.values())
    assert plan.records == 2
    assert (plan.images_current, plan.images_superseded) == (4, 2)
    masters = sum(len(f"CR2 {n} c{c}".encode()) for n, c in images)
    assert plan.bytes == {"masters": masters, "previews": masters + 6 * len(b"preview "), "raw_buffers": 0}
    assert plan.counter_before == 0 and plan.counter_after == 3
    assert plan.uncovered == [] and plan.thumbnails_missing == []
    assert plan.capture_lines == 3 and plan.unaccounted_capture_lines == []


@pytest.mark.unit
def test_each_rename_swaps_the_stem_and_plans_both_keys(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, images = _default(db_session, root)

    by_id = {r.image_id: r for r in _plan(db_session, src, tgt).renames}

    for (n, cam), img in images.items():
        r = by_id[img.id]
        assert (r.old_name, r.new_name) == (_name(SRC, n, cam), _name(TGT, n, cam))
        assert r.old_path == root / SRC / "captures" / _name(SRC, n, cam)
        assert r.new_path == root / TGT / "captures" / _name(TGT, n, cam)
        assert r.is_current is (n != 1)
        assert r.old_pair_id == f"{SRC}-img_{n:04d}" and r.new_pair_id == f"{TGT}-img_{n:04d}"
        assert r.old_preview_path == root / SRC / "previews" / f"{SRC}-img_{n:04d}_c{cam}_preview.jpg"
        assert r.new_preview_path == root / TGT / "previews" / f"{TGT}-img_{n:04d}_c{cam}_preview.jpg"
        assert r.old_keys == {"capture": f"{TENANT}/{SRC}/captures/{_name(SRC, n, cam)}",
                              "preview": f"{TENANT}/{SRC}/previews/{SRC}-img_{n:04d}_c{cam}_preview.jpg"}
        assert r.new_keys == {"capture": f"{TENANT}/{TGT}/captures/{_name(TGT, n, cam)}",
                              "preview": f"{TENANT}/{TGT}/previews/{TGT}-img_{n:04d}_c{cam}_preview.jpg"}
        assert r.capture_id == img.capture_id and r.capture_line is True
    assert by_id[images[(2, 0)].id].capture_id == by_id[images[(2, 1)].id].capture_id


@pytest.mark.unit
def test_the_plan_carries_both_projects(db_session, override_projects_root, tenant):
    src, tgt, _ = _default(db_session, override_projects_root)

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == []
    assert plan.source_project_id == src.project_id and plan.target_project_id == tgt.project_id
    assert plan.source_project_id != plan.target_project_id
    assert plan.project_changes is True


@pytest.mark.unit
def test_thumbnails_keep_their_names_and_move_to_the_targets_thumbnails(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, images = _default(db_session, root)
    img = images[(1, 0)]

    r = {r.image_id: r for r in _plan(db_session, src, tgt).renames}[img.id]

    assert r.old_thumbnail_path == Path(img.thumbnail_path)
    assert r.new_thumbnail_path == root / TGT / "thumbnails" / Path(img.thumbnail_path).name


@pytest.mark.unit
def test_a_thumbnail_recorded_outside_the_sources_thumbnails_stays_where_it_is(db_session, override_projects_root, tenant):
    from app.models.record import RecordImage

    root = override_projects_root
    src, tgt, images = _default(db_session, root)
    img = images[(2, 1)]
    elsewhere = root / "derived" / Path(img.thumbnail_path).name
    elsewhere.parent.mkdir()
    Path(img.thumbnail_path).rename(elsewhere)
    db_session.get(RecordImage, img.id).thumbnail_path = str(elsewhere)
    db_session.commit()

    plan = _plan(db_session, src, tgt)
    r = {r.image_id: r for r in plan.renames}[img.id]

    assert plan.refusals == [], plan.details
    assert r.old_thumbnail_path == elsewhere and r.new_thumbnail_path == elsewhere


@pytest.mark.unit
def test_a_missing_thumbnail_is_reported_and_not_refused(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, images = _default(db_session, root)
    img = images[(3, 0)]
    Path(img.thumbnail_path).unlink()

    plan = _plan(db_session, src, tgt)
    r = {r.image_id: r for r in plan.renames}[img.id]

    assert plan.refusals == [], plan.details
    assert plan.thumbnails_missing == [{"image_id": img.id, "path": img.thumbnail_path}]
    assert r.old_thumbnail_path == Path(img.thumbnail_path) and r.new_thumbnail_path is None


@pytest.mark.unit
def test_a_raw_buffer_beside_a_jpeg_master_is_renamed_with_it(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, _ = _default(db_session, root)
    rec = _record(db_session, src, "f. 5", mode="single")
    img = _capture(db_session, rec, root, SRC, 4, cams=(0,), ext="jpg")[(4, 0)]
    (root / SRC / "captures" / f"{SRC}-img_0004_c0.raw").write_bytes(b"buffer")

    plan = _plan(db_session, src, tgt)
    r = {r.image_id: r for r in plan.renames}[img.id]

    assert plan.refusals == [], plan.details
    assert r.old_preview_path is None and r.new_keys == {"capture": f"{TENANT}/{TGT}/captures/{_name(TGT, 4, 0, 'jpg')}"}
    assert r.old_raw_buffer_path == root / SRC / "captures" / f"{SRC}-img_0004_c0.raw"
    assert r.new_raw_buffer_path == root / TGT / "captures" / f"{TGT}-img_0004_c0.raw"
    assert plan.bytes["raw_buffers"] == len(b"buffer")
    assert plan.uncovered == []


@pytest.mark.unit
def test_a_raw_buffer_is_found_by_listing_whatever_the_case_of_its_name(db_session, override_projects_root, tenant):
    """A buffer named `.RAW` is found on a case-sensitive disk too, and planned under the name it is listed with."""
    root = override_projects_root
    src, tgt, _ = _default(db_session, root)
    rec = _record(db_session, src, "f. 5", mode="single")
    jpg = _capture(db_session, rec, root, SRC, 4, cams=(0,), ext="jpg")[(4, 0)]
    buffer = root / SRC / "captures" / f"{SRC}-img_0004_c0.RAW"
    buffer.write_bytes(b"buffer")

    plan = _plan(db_session, src, tgt)
    r = {r.image_id: r for r in plan.renames}[jpg.id]

    assert plan.refusals == [], plan.details
    assert r.old_raw_buffer_path.name == buffer.name
    assert r.new_raw_buffer_path.name == f"{TGT}-img_0004_c0.RAW"
    assert plan.uncovered == []


@pytest.mark.unit
def test_a_preview_not_named_as_its_key_must_be_is_found_and_refused(db_session, override_projects_root, tenant):
    """A preview listed as `_preview.JPG` is found, planned under that name, and refused.

    The export takes a preview key only in the form `<stem>-img_<nnnn>_c<n>_preview.jpg`
    (app.core.capture_manifest), and the plan keeps the listed name.
    """
    root = override_projects_root
    src, tgt, images = _default(db_session, root)
    img = images[(3, 1)]
    lower = root / SRC / "previews" / f"{SRC}-img_0003_c1_preview.jpg"
    upper = lower.with_name(f"{SRC}-img_0003_c1_preview.JPG")
    lower.rename(upper)

    plan = _plan(db_session, src, tgt)
    r = {r.image_id: r for r in plan.renames}[img.id]

    assert plan.refusals == ["source_name_not_contract"]
    assert plan.details["source_name_not_contract"] == [{"image_id": img.id, "filename": upper.name}]
    assert r.old_preview_path.name == upper.name
    assert r.new_preview_path.name == f"{TGT}-img_0003_c1_preview.JPG"
    assert plan.uncovered == []


@pytest.mark.unit
def test_the_counter_after_is_the_highest_of_the_source_counter_and_the_numbers_renamed(db_session, override_projects_root, tenant):
    src, tgt, _ = _default(db_session, override_projects_root)
    src.capture_counter = 7  # numbers 4 to 7 were given and their files deleted
    db_session.commit()

    assert _plan(db_session, src, tgt).counter_after == 7


@pytest.mark.unit
def test_a_record_with_no_image_is_counted_and_has_no_rename(db_session, override_projects_root, tenant):
    src, tgt, _ = _default(db_session, override_projects_root)
    _record(db_session, src, "placeholder")

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == [] and plan.records == 3 and len(plan.renames) == 6


@pytest.mark.unit
def test_a_capture_line_no_image_accounts_for_is_reported_and_not_refused(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, _ = _default(db_session, root)
    stray = str(uuid.uuid4())
    _manifest_line(root, SRC, {"capture_id": stray, "files": [{"relative_path": "captures/gone.CR2"}]})
    _manifest_line(root, SRC, {"record_type": "relocation", "capture_id": stray, "from": "a", "to": "b"})

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == []
    assert plan.capture_lines == 4 and plan.unaccounted_capture_lines == [stray]


@pytest.mark.unit
def test_planning_changes_no_row_and_no_file(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, _ = _default(db_session, root)
    src_id, tgt_id = src.id, tgt.id
    before = _snapshot(db_session, root)

    _plan(db_session, src_id, tgt_id)
    db_session.commit()

    assert _snapshot(db_session, root) == before


@pytest.mark.unit
def test_planning_a_refused_adoption_changes_no_row_and_no_file(db_session, override_projects_root, tenant):
    from app.models.fisqua import FisquaOutbox

    root = override_projects_root
    src, tgt, images = _default(db_session, root)
    _record(db_session, tgt, "already here")
    db_session.add(FisquaOutbox(collection_id=src.id, manifest_id=str(uuid.uuid4()), state="waiting_for_upload"))
    db_session.commit()
    Path(images[(1, 0)].file_path).unlink()
    (root / SRC / "captures" / "stray.CR2").write_bytes(b"stray")
    src_id, tgt_id = src.id, tgt.id
    before = _snapshot(db_session, root)

    plan = _plan(db_session, src_id, tgt_id)
    db_session.commit()

    assert plan.refusals
    assert _snapshot(db_session, root) == before


@pytest.mark.unit
def test_planning_flushes_nothing_pending_in_the_session(db_session, override_projects_root, tenant):
    from sqlalchemy import inspect

    from app.models.record import Record

    src, tgt, _ = _default(db_session, override_projects_root)
    pending = Record(title="not yet", collection_id=tgt.id, capture_mode="single")
    db_session.add(pending)
    src.name = "renamed, not flushed"

    plan = _plan(db_session, src, tgt)

    assert inspect(pending).pending and pending in db_session.new
    assert src in db_session.dirty
    assert "target_has_records" not in plan.refusals
    db_session.rollback()


# ---------------------------------------------------------------------------
# Each refusal alone
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_source_or_target_that_does_not_exist_is_refused(db_session, override_projects_root, tenant):
    src, tgt, _ = _default(db_session, override_projects_root)

    assert _plan(db_session, 9999, tgt).refusals == ["source_not_found"]
    assert _plan(db_session, src, 9999).refusals == ["target_not_found"]
    assert _plan(db_session, 9998, 9999).refusals == ["source_not_found", "target_not_found"]


@pytest.mark.unit
def test_a_volume_adopted_into_itself_is_refused(db_session, override_projects_root, tenant):
    src, _, _ = _default(db_session, override_projects_root)

    assert _plan(db_session, src, src).refusals == ["same_volume"]


def _testing_volume(db, stem):
    from app.core.project_names import TESTING_PREFIX, TESTING_PROJECT_PREFIX
    from app.models.collection import Collection
    from app.models.project import Project

    name = TESTING_PROJECT_PREFIX + "Pruebas"
    project = db.query(Project).filter(Project.name == name).one_or_none()
    if project is None:
        project = Project(name=name, testing=True)
        db.add(project)
        db.commit()
    vol = Collection(name="Tomo de prueba", project_id=project.id, filename_stem=TESTING_PREFIX + stem)
    db.add(vol)
    db.commit()
    assert vol.testing
    return vol


@pytest.mark.unit
def test_a_testing_volume_is_neither_source_nor_target(db_session, override_projects_root, tenant):
    src, tgt, _ = _default(db_session, override_projects_root)
    testing = _testing_volume(db_session, "000901")

    plan = _plan(db_session, testing, tgt)
    assert plan.refusals == ["source_testing_volume"] and not plan.may_adopt and plan.renames == []
    assert _plan(db_session, src, testing).refusals == ["target_testing_volume"]
    other = _testing_volume(db_session, "000902")
    assert _plan(db_session, testing, other).refusals == ["source_testing_volume", "target_testing_volume"]


@pytest.mark.unit
def test_a_source_without_a_stem_is_refused(db_session, override_projects_root, tenant):
    src = _volume(db_session, "Sin raíz", None)
    tgt = _volume(db_session, "Asignado", TGT, external_id=TGT, project="Fondo 13")

    assert _plan(db_session, src, tgt).refusals == ["source_has_no_stem"]


@pytest.mark.unit
def test_a_target_without_a_stem_is_refused(db_session, override_projects_root, tenant):
    src, _, _ = _default(db_session, override_projects_root)
    tgt = _volume(db_session, "Sin raíz", None)

    assert _plan(db_session, src, tgt).refusals == ["target_has_no_stem", "target_not_assigned"]


@pytest.mark.unit
def test_a_target_without_a_reference_code_is_refused(db_session, override_projects_root, tenant):
    src, _, _ = _default(db_session, override_projects_root)
    tgt = _volume(db_session, "Hecho a mano también", "xx-unassigned-001")

    assert _plan(db_session, src, tgt).refusals == ["target_not_assigned"]


@pytest.mark.unit
def test_a_target_withdrawn_from_the_queue_is_refused(db_session, override_projects_root, tenant):
    src, tgt, _ = _default(db_session, override_projects_root)
    tgt.queue_hidden_at = _now()
    db_session.commit()

    assert _plan(db_session, src, tgt).refusals == ["target_withdrawn"]


@pytest.mark.unit
def test_a_source_with_records_and_no_image_is_refused(db_session, override_projects_root, tenant):
    src = _volume(db_session, "Solo placeholders", SRC)
    tgt = _volume(db_session, "Asignado", TGT, external_id=TGT, project="Fondo 13")
    _record(db_session, src, "missing opening")

    assert _plan(db_session, src, tgt).refusals == ["source_has_no_images"]


@pytest.mark.unit
def test_an_imageless_record_among_records_with_images_is_allowed(db_session, override_projects_root, tenant):
    src, tgt, _ = _default(db_session, override_projects_root)
    _record(db_session, src, "missing opening")

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == [] and plan.records == 3


@pytest.mark.unit
def test_a_source_with_a_sub_volume_is_refused(db_session, override_projects_root, tenant):
    from app.models.collection import Collection

    src, tgt, _ = _default(db_session, override_projects_root)
    child = Collection(name="Legajo", parent_collection_id=src.id)
    db_session.add(child)
    db_session.commit()

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["source_has_sub_volumes"]
    assert plan.details["source_has_sub_volumes"] == [child.id]


@pytest.mark.unit
@pytest.mark.parametrize("state", ["waiting_for_upload", "delivered", "withdrawn", "refused"])
def test_a_source_with_any_outbox_row_is_refused(db_session, override_projects_root, tenant, state):
    from app.models.fisqua import FisquaOutbox

    src, tgt, _ = _default(db_session, override_projects_root)
    fields = {"delivered_at": _now()} if state == "delivered" else {}
    if state == "refused":
        fields["refusal"] = "fisqua"
    db_session.add(FisquaOutbox(collection_id=src.id, manifest_id=str(uuid.uuid4()), state=state, **fields))
    db_session.commit()

    assert _plan(db_session, src, tgt).refusals == ["source_sent_to_fisqua"]


@pytest.mark.unit
def test_a_source_with_a_fisqua_status_is_refused(db_session, override_projects_root, tenant):
    from app.models.fisqua import FisquaStatus

    src, tgt, _ = _default(db_session, override_projects_root)
    db_session.add(FisquaStatus(collection_id=src.id, status_checked_at=_now()))
    db_session.commit()

    assert _plan(db_session, src, tgt).refusals == ["source_sent_to_fisqua"]


@pytest.mark.unit
@pytest.mark.parametrize("row", ["outbox", "status"])
def test_a_target_already_sent_to_fisqua_is_refused(db_session, override_projects_root, tenant, row):
    from app.models.fisqua import FisquaOutbox, FisquaStatus

    src, tgt, _ = _default(db_session, override_projects_root)
    if row == "outbox":
        db_session.add(FisquaOutbox(collection_id=tgt.id, manifest_id=str(uuid.uuid4()), state="waiting_for_upload"))
    else:
        db_session.add(FisquaStatus(collection_id=tgt.id, status_checked_at=_now()))
    db_session.commit()

    assert _plan(db_session, src, tgt).refusals == ["target_sent_to_fisqua"]


@pytest.mark.unit
def test_a_kept_source_or_target_is_refused(db_session, override_projects_root, tenant):
    src, tgt, _ = _default(db_session, override_projects_root)
    src.kept_on_unit = True
    db_session.commit()
    assert _plan(db_session, src, tgt).refusals == ["source_kept_on_unit"]

    src.kept_on_unit, tgt.kept_on_unit = False, True
    db_session.commit()
    assert _plan(db_session, src, tgt).refusals == ["target_kept_on_unit"]


@pytest.mark.unit
def test_a_target_with_a_record_is_refused(db_session, override_projects_root, tenant):
    src, tgt, _ = _default(db_session, override_projects_root)
    _record(db_session, tgt, "already here")

    assert _plan(db_session, src, tgt).refusals == ["target_has_records"]


@pytest.mark.unit
def test_a_target_with_an_image_is_refused(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, _ = _default(db_session, root)
    rec = _record(db_session, tgt, "already here")
    _image(db_session, rec, root, TGT, _name(TGT, 9), b"other", verified=False)

    assert _plan(db_session, src, tgt).refusals == ["target_has_records", "target_has_images"]


@pytest.mark.unit
def test_a_target_whose_counter_was_raised_is_refused(db_session, override_projects_root, tenant):
    src, tgt, _ = _default(db_session, override_projects_root)
    tgt.capture_counter = 1
    db_session.commit()

    assert _plan(db_session, src, tgt).refusals == ["target_counter_raised"]


@pytest.mark.unit
def test_a_target_with_an_offsite_key_under_its_stem_is_refused(db_session, override_projects_root, tenant):
    from app.models.offsite_object import OffsiteObject

    src, tgt, _ = _default(db_session, override_projects_root)
    db_session.add(OffsiteObject(record_image_id=None, kind="capture",
                                 object_key=f"earlier/{TGT}/captures/{_name(TGT, 50)}", local_sha1="2" * 40))
    db_session.commit()

    assert _plan(db_session, src, tgt).refusals == ["target_has_offsite_keys"]


@pytest.mark.unit
def test_no_configured_tenant_is_refused(db_session, override_projects_root, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", "")
    src, tgt, _ = _default(db_session, override_projects_root)

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["offsite_tenant_not_configured"]
    assert all(r.new_keys == {} for r in plan.renames)


@pytest.mark.unit
def test_a_projects_root_that_cannot_be_written_is_refused(db_session, override_projects_root, tenant, monkeypatch):
    from app.core import storage_override

    src, tgt, _ = _default(db_session, override_projects_root)

    def unusable(settings=None):
        raise storage_override.StorageUnavailableError("the drive is not mounted")

    monkeypatch.setattr(storage_override, "writable_projects_dir", unusable)

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["storage_unavailable"]
    assert plan.renames == []


@pytest.mark.unit
@pytest.mark.parametrize("status", ["in_review", "approved", "rejected", "excluded"])
def test_a_record_that_move_records_would_refuse_is_refused(db_session, override_projects_root, tenant, status):
    from app.models.record import Record

    src, tgt, images = _default(db_session, override_projects_root)
    rec = db_session.get(Record, images[(1, 0)].record_id)
    rec.status = status
    db_session.commit()

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["source_records_handed_over"]
    assert plan.details["source_records_handed_over"] == [rec.id]


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["master", "preview"])
def test_a_freed_file_is_refused(db_session, override_projects_root, tenant, kind):
    src, tgt, images = _default(db_session, override_projects_root)
    img = images[(3, 0)]
    setattr(img, f"{kind}_freed_at", _now())
    setattr(img, f"{kind}_freed_bytes", 9)
    db_session.commit()

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["source_files_freed"]
    assert plan.details["source_files_freed"] == [{"image_id": img.id, "kind": kind}]


@pytest.mark.unit
def test_a_missing_master_or_preview_is_refused(db_session, override_projects_root, tenant):
    from app.core.paths import preview_sidecar_of

    src, tgt, images = _default(db_session, override_projects_root)
    a, b = images[(1, 0)], images[(3, 1)]
    Path(a.file_path).unlink()
    preview_sidecar_of(Path(b.file_path)).unlink()

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["source_file_missing"]
    assert plan.details["source_file_missing"] == [
        {"image_id": a.id, "kind": "master", "path": a.file_path},
        {"image_id": b.id, "kind": "preview", "path": str(preview_sidecar_of(Path(b.file_path)))},
    ]


@pytest.mark.unit
def test_an_image_without_a_verified_capture_or_preview_row_is_refused(db_session, override_projects_root, tenant):
    from app.models.offsite_object import OffsiteObject

    src, tgt, images = _default(db_session, override_projects_root)
    a, b = images[(1, 0)], images[(3, 1)]
    db_session.query(OffsiteObject).filter(OffsiteObject.record_image_id == a.id,
                                           OffsiteObject.kind == "capture").update({"verified_at": None})
    db_session.query(OffsiteObject).filter(OffsiteObject.record_image_id == b.id,
                                           OffsiteObject.kind == "preview").delete()
    db_session.commit()

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["source_not_verified_offsite"]
    assert plan.details["source_not_verified_offsite"] == [
        {"image_id": a.id, "kind": "capture"}, {"image_id": b.id, "kind": "preview"}]


@pytest.mark.unit
def test_a_volume_the_upload_state_does_not_count_as_received_is_refused(db_session, override_projects_root, tenant, monkeypatch):
    from app.core import image_upload

    src, tgt, _ = _default(db_session, override_projects_root)
    real = image_upload.volume_upload_state
    monkeypatch.setattr(image_upload, "volume_upload_state",
                        lambda db, c: {**real(db, c), "received": False, "verified": 11})

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["source_not_verified_offsite"]
    assert plan.details["source_not_verified_offsite"] == [{"volume": src.id, "total": 12, "verified": 11}]


@pytest.mark.unit
def test_a_file_outside_the_sources_own_captures_is_refused(db_session, override_projects_root, tenant):
    from app.core.paths import preview_sidecar_of
    from app.models.record import RecordImage

    root = override_projects_root
    src, tgt, images = _default(db_session, root)
    img = images[(1, 0)]
    elsewhere = root / "fondo_10" / "caja" / "images" / "main" / img.filename
    elsewhere.parent.mkdir(parents=True)
    old_preview = preview_sidecar_of(Path(img.file_path))
    Path(img.file_path).rename(elsewhere)
    old_preview.rename(preview_sidecar_of(elsewhere))
    db_session.get(RecordImage, img.id).file_path = str(elsewhere)
    db_session.commit()

    plan = _plan(db_session, src, tgt)

    # Its capture line names `captures/<file>` under the volume, which is not where the file is.
    assert plan.refusals == ["source_file_outside_own_folder", "source_capture_line_missing"]
    assert plan.details["source_file_outside_own_folder"] == [{"image_id": img.id, "path": str(elsewhere)}]


@pytest.mark.unit
def test_an_image_without_a_capture_line_naming_its_file_is_refused(db_session, override_projects_root, tenant):
    from app.models.record import RecordImage

    root = override_projects_root
    src, tgt, images = _default(db_session, root)
    rec = _record(db_session, src, "f. 5")
    no_line = _capture(db_session, rec, root, SRC, 4, line=False)
    other = images[(2, 0)]
    db_session.get(RecordImage, other.id).capture_id = str(uuid.uuid4())
    db_session.commit()
    src.capture_counter = 4
    db_session.commit()

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["source_capture_line_missing"]
    assert plan.details["source_capture_line_missing"] == sorted(
        [{"image_id": other.id, "capture_id": db_session.get(RecordImage, other.id).capture_id}]
        + [{"image_id": i.id, "capture_id": i.capture_id} for i in no_line.values()],
        key=lambda d: d["image_id"])


@pytest.mark.unit
@pytest.mark.parametrize("name", [
    "f1.jpg",                                    # no contract name at all
    f"{SRC}-img_12_c0.jpg",                      # a number of two digits
    f"{SRC}-img_0004.jpg",                       # no camera
    f"co-ahr-con-caj80-car009-img_0004_c0.jpg",  # another volume's stem
    f"{SRC}-img_0004_c0",                        # no suffix
    f"{SRC}-img_0004_c0_high.jpg",               # a resolution in the name
    f"{SRC}-img_0000_c0.jpg",                    # number 0, which no capture is given
])
def test_a_file_name_that_is_not_the_contract_form_is_refused(db_session, override_projects_root, tenant, name):
    root = override_projects_root
    src, tgt, _ = _default(db_session, root)
    rec = _record(db_session, src, "odd", mode="single")
    capture_id = str(uuid.uuid4())
    img, files = _image(db_session, rec, root, SRC, name, b"odd", capture_id=capture_id)
    _manifest_line(root, SRC, {"capture_id": capture_id, "files": files})

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["source_name_not_contract"]
    assert plan.details["source_name_not_contract"] == [{"image_id": img.id, "filename": name}]
    assert img.id not in [r.image_id for r in plan.renames]


@pytest.mark.unit
def test_a_row_whose_filename_is_not_its_files_name_is_refused(db_session, override_projects_root, tenant):
    from app.models.record import RecordImage

    src, tgt, images = _default(db_session, override_projects_root)
    db_session.get(RecordImage, images[(1, 0)].id).filename = _name(SRC, 8)
    db_session.commit()

    assert _plan(db_session, src, tgt).refusals == ["source_name_not_contract"]


@pytest.mark.unit
@pytest.mark.parametrize("where", ["captures", "previews"])
def test_a_master_or_preview_with_no_row_is_refused(db_session, override_projects_root, tenant, where):
    root = override_projects_root
    src, tgt, _ = _default(db_session, root)
    name = _name(SRC, 9) if where == "captures" else f"{SRC}-img_0009_c0_preview.jpg"
    stray = root / SRC / where / name
    stray.write_bytes(b"no row")

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["source_has_unowned_files"]
    assert plan.uncovered == [{"path": str(stray), "kind": where}]
    assert plan.details["source_has_unowned_files"] == [{"path": str(stray), "kind": where}]


@pytest.mark.unit
def test_other_files_no_row_owns_are_reported_and_not_refused(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, _ = _default(db_session, root)
    thumb = root / SRC / "thumbnails" / "orphan_thumb.jpg"
    thumb.write_bytes(b"orphan")
    freed = root / SRC / "metadata" / "offsite-freed-20260101T000000Z.jsonl"
    freed.write_text("")
    note = root / SRC / "notes.txt"
    note.write_text("by hand")

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == [], plan.details
    assert plan.uncovered == [{"path": str(freed), "kind": "metadata"},
                              {"path": str(note), "kind": "other"},
                              {"path": str(thumb), "kind": "thumbnails"}]


@pytest.mark.unit
def test_a_new_name_taken_on_disk_is_refused(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, images = _default(db_session, root)
    taken = root / TGT / "captures" / _name(TGT, 1).upper()  # case-insensitive drives make these one file
    taken.parent.mkdir(parents=True)
    taken.write_bytes(b"stray")
    stray_preview = root / TGT / "previews" / f"{TGT}-img_0003_c0_preview.jpg"
    stray_preview.parent.mkdir(parents=True)
    stray_preview.write_bytes(b"stray")

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["new_name_collides"]
    assert plan.details["new_name_collides"] == [
        {"image_id": images[(1, 0)].id, "with": "file", "path": str(taken)},
        {"image_id": images[(3, 0)].id, "with": "file", "path": str(stray_preview)},
    ]


@pytest.mark.unit
def test_a_new_name_held_by_another_row_or_key_is_refused(db_session, override_projects_root, tenant):
    from app.models.offsite_object import OffsiteObject

    root = override_projects_root
    src, tgt, images = _default(db_session, root)
    other = _volume(db_session, "Otro", "xx-other-001")
    rec = _record(db_session, other, "elsewhere")
    stray, _ = _image(db_session, rec, root, "xx-other-001", _name(TGT, 1), b"stray", verified=False)
    key = f"{TENANT}/{TGT}/previews/{TGT}-img_0002_c1_preview.jpg"
    db_session.add(OffsiteObject(record_image_id=None, kind="preview", object_key=key, local_sha1="3" * 40))
    db_session.commit()

    plan = _plan(db_session, src, tgt)

    # The key under the target's stem also makes the target not empty.
    assert plan.refusals == ["target_has_offsite_keys", "new_name_collides"]
    assert plan.details["new_name_collides"] == [
        {"image_id": images[(1, 0)].id, "with": "record_image", "record_image_id": stray.id},
        {"image_id": images[(2, 1)].id, "with": "offsite_key", "key": key},
    ]


@pytest.mark.unit
def test_a_number_the_targets_manifest_names_is_refused(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, images = _default(db_session, root)
    _manifest_line(root, TGT, {"files": [{"relative_path": f"captures/{_name(TGT, 2)}"}]})

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["new_name_collides"]
    assert plan.details["new_name_collides"] == [
        {"image_id": images[(2, 0)].id, "with": "target_manifest", "number": 2},
        {"image_id": images[(2, 1)].id, "with": "target_manifest", "number": 2},
    ]


@pytest.mark.unit
def test_a_capture_line_without_the_masters_digest_is_refused(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, _ = _default(db_session, root)
    rec = _record(db_session, src, "f. 5")
    undigested = _capture(db_session, rec, root, SRC, 4, digest=False)
    src.capture_counter = 4
    db_session.commit()

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["source_digest_missing"]
    assert plan.details["source_digest_missing"] == [
        {"image_id": i.id, "path": i.file_path} for i in sorted(undigested.values(), key=lambda i: i.id)]


@pytest.mark.unit
def test_a_raw_preview_with_no_digest_in_the_sources_manifest_is_refused(db_session, override_projects_root, tenant):
    from app.core.paths import preview_sidecar_of

    root = override_projects_root
    src, tgt, _ = _default(db_session, root)
    rec = _record(db_session, src, "f. 5")
    undigested = _capture(db_session, rec, root, SRC, 4, preview_digest=False)
    src.capture_counter = 4
    db_session.commit()

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["source_preview_digest_missing"]
    assert plan.details["source_preview_digest_missing"] == [
        {"image_id": i.id, "path": str(preview_sidecar_of(Path(i.file_path)))}
        for i in sorted(undigested.values(), key=lambda i: i.id)]


@pytest.mark.unit
def test_a_raw_preview_whose_digest_was_backfilled_is_not_refused(db_session, override_projects_root, tenant):
    from app.core.paths import preview_sidecar_of

    root = override_projects_root
    src, tgt, _ = _default(db_session, root)
    rec = _record(db_session, src, "f. 5")
    undigested = _capture(db_session, rec, root, SRC, 4, preview_digest=False)
    src.capture_counter = 4
    db_session.commit()
    for i in undigested.values():
        preview = preview_sidecar_of(Path(i.file_path))
        _manifest_line(root, SRC, {"record_type": "preview_digest", "capture_id": i.capture_id, "image_id": i.id,
                                   "files": [{"role": "single_preview", "relative_path": f"previews/{preview.name}",
                                              "sha256": hashlib.sha256(preview.read_bytes()).hexdigest()}]})

    assert _plan(db_session, src, tgt).refusals == []


@pytest.mark.unit
def test_a_capture_line_naming_a_file_no_image_has_is_refused(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, images = _default(db_session, root)
    capture_id = images[(2, 0)].capture_id
    gone = f"captures/{_name(SRC, 2, 2)}"   # a third camera's file, deleted with its row
    _manifest_line(root, SRC, {"capture_id": capture_id, "files": [{"role": "single", "relative_path": gone,
                                                                   "sha256": "4" * 64}]})

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["source_manifest_path_unmapped"]
    assert plan.details["source_manifest_path_unmapped"] == [{"capture_id": capture_id, "path": gone}]


@pytest.mark.unit
def test_a_capture_line_of_a_capture_id_no_image_has_is_not_checked_for_its_paths(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, _ = _default(db_session, root)
    _manifest_line(root, SRC, {"capture_id": str(uuid.uuid4()), "files": [{"relative_path": "captures/gone.CR2"}]})

    assert _plan(db_session, src, tgt).refusals == []


@pytest.mark.unit
def test_two_images_sharing_one_thumbnail_are_refused(db_session, override_projects_root, tenant):
    from app.models.record import RecordImage

    src, tgt, images = _default(db_session, override_projects_root)
    a, b = images[(2, 0)], images[(3, 1)]
    db_session.get(RecordImage, b.id).thumbnail_path = a.thumbnail_path
    db_session.commit()

    plan = _plan(db_session, src, tgt)

    # b's own thumbnail is now one no image owns, which is reported, not refused.
    assert plan.refusals == ["source_thumbnail_shared"]
    assert plan.details["source_thumbnail_shared"] == [{"path": a.thumbnail_path, "image_ids": [a.id, b.id]}]


@pytest.mark.unit
def test_an_unfinished_adoption_refuses_every_adoption(db_session, override_projects_root, tenant, own_data_dir):
    src, tgt, _ = _default(db_session, override_projects_root)
    (own_data_dir / "adopt-captures.journal").write_text(json.dumps({"version": 1, "source_id": 98,
                                                                       "target_id": 99}))

    assert _plan(db_session, src, tgt).refusals == ["adoption_unfinished"]
    (own_data_dir / "adopt-captures.journal").write_text("not json")
    assert _plan(db_session, src, tgt).refusals == ["adoption_unfinished"]


def _outside(tmp_path, name):
    outside = tmp_path / name
    outside.mkdir()
    return outside


@pytest.mark.unit
def test_a_source_previews_directory_that_leads_outside_the_storage_is_refused(db_session, override_projects_root, tenant, tmp_path):
    root = override_projects_root
    src, tgt, images = _default(db_session, root)
    outside = _outside(tmp_path, "elsewhere")
    previews = root / SRC / "previews"
    for p in previews.iterdir():
        p.rename(outside / p.name)
    previews.rmdir()
    previews.symlink_to(outside, target_is_directory=True)

    plan = _plan(db_session, src, tgt)

    assert "source_outside_storage" in plan.refusals
    paths = {d["path"] for d in plan.details["source_outside_storage"]}
    assert str(previews) in paths
    assert str(previews / f"{SRC}-img_0001_c0_preview.jpg") in paths


@pytest.mark.unit
def test_a_source_thumbnail_outside_the_storage_is_refused(db_session, override_projects_root, tenant, tmp_path):
    from app.models.record import RecordImage

    root = override_projects_root
    src, tgt, images = _default(db_session, root)
    outside = _outside(tmp_path, "elsewhere")
    img = images[(2, 0)]
    moved = outside / Path(img.thumbnail_path).name
    Path(img.thumbnail_path).rename(moved)
    db_session.get(RecordImage, img.id).thumbnail_path = str(moved)
    db_session.commit()

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["source_outside_storage"]
    assert plan.details["source_outside_storage"] == [{"path": str(moved)}]


@pytest.mark.unit
def test_a_buffer_reached_through_a_source_captures_directory_outside_the_storage_is_refused(db_session, override_projects_root, tenant, tmp_path):
    root = override_projects_root
    src, tgt, _ = _default(db_session, root)
    rec = _record(db_session, src, "f. 5", mode="single")
    _capture(db_session, rec, root, SRC, 4, cams=(0,), ext="jpg")
    (root / SRC / "captures" / f"{SRC}-img_0004_c0.raw").write_bytes(b"buffer")
    outside = _outside(tmp_path, "elsewhere")
    captures = root / SRC / "captures"
    for p in captures.iterdir():
        p.rename(outside / p.name)
    captures.rmdir()
    captures.symlink_to(outside, target_is_directory=True)

    plan = _plan(db_session, src, tgt)

    assert "source_outside_storage" in plan.refusals
    assert str(captures / f"{SRC}-img_0004_c0.raw") in {d["path"] for d in plan.details["source_outside_storage"]}


# ---------------------------------------------------------------------------
# Several refusals together, and their order
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_refusal_codes_are_in_this_order():
    from app.core.adopt_captures import REFUSAL_CODES

    assert REFUSAL_CODES == (
        "adoption_unfinished", "source_not_found", "target_not_found", "same_volume",
        "source_testing_volume", "target_testing_volume", "source_has_no_stem", "target_has_no_stem", "target_not_assigned", "target_withdrawn",
        "source_sent_to_fisqua", "target_sent_to_fisqua", "source_kept_on_unit", "target_kept_on_unit",
        "source_has_sub_volumes", "source_has_no_images", "target_has_records", "target_has_images", "target_counter_raised",
        "target_has_offsite_keys",
        "offsite_tenant_not_configured", "storage_unavailable", "source_outside_storage",
        "target_outside_storage",
        "source_records_handed_over", "source_files_freed", "source_file_outside_own_folder",
        "source_file_missing", "source_not_verified_offsite", "source_capture_line_missing",
        "source_manifest_path_unmapped", "source_digest_missing", "source_preview_digest_missing",
        "source_name_not_contract", "source_thumbnail_shared", "source_has_unowned_files", "new_name_collides",
    )


@pytest.mark.unit
def test_every_refusal_that_applies_is_given_in_the_fixed_order(db_session, override_projects_root, monkeypatch):
    from app.core import config
    from app.core.adopt_captures import REFUSAL_CODES
    from app.models.collection import Collection
    from app.models.fisqua import FisquaStatus
    from app.models.record import Record

    root = override_projects_root
    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", "")
    src, _, images = _default(db_session, root)
    tgt = _volume(db_session, "Sin código", "xx-unassigned-001", kept_on_unit=True, capture_counter=4,
                  queue_hidden_at=_now())
    _record(db_session, tgt, "already here")
    src.kept_on_unit = True
    db_session.add(Collection(name="Legajo", parent_collection_id=src.id))
    db_session.add(FisquaStatus(collection_id=src.id, status_checked_at=_now()))
    db_session.get(Record, images[(1, 0)].record_id).status = "in_review"
    images[(3, 0)].master_freed_at, images[(3, 0)].master_freed_bytes = _now(), 9
    db_session.commit()
    Path(images[(2, 0)].file_path).unlink()
    (root / SRC / "previews" / "stray_preview.jpg").write_bytes(b"stray")
    _manifest_line(root, SRC, {"capture_id": images[(1, 0)].capture_id,
                               "files": [{"relative_path": f"captures/{_name(SRC, 1, 2)}"}]})
    _capture(db_session, db_session.get(Record, images[(3, 0)].record_id), root, SRC, 4, digest=False,
             preview_digest=False)
    db_session.add(FisquaStatus(collection_id=tgt.id, status_checked_at=_now()))
    images[(1, 1)].thumbnail_path = images[(1, 0)].thumbnail_path
    db_session.commit()
    (config.settings.data_dir / "adopt-captures.journal").write_text("{}")

    plan = _plan(db_session, src, tgt)

    expected = ["adoption_unfinished", "target_not_assigned", "target_withdrawn", "source_sent_to_fisqua", "target_sent_to_fisqua",
                "source_kept_on_unit", "target_kept_on_unit", "source_has_sub_volumes", "target_has_records",
                "target_counter_raised", "offsite_tenant_not_configured", "source_records_handed_over",
                "source_files_freed", "source_file_missing", "source_manifest_path_unmapped",
                "source_digest_missing", "source_preview_digest_missing", "source_thumbnail_shared",
                "source_has_unowned_files"]
    assert plan.refusals == expected
    assert plan.refusals == [c for c in REFUSAL_CODES if c in expected]
    assert not plan.may_adopt
