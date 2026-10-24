"""A RAW master's preview carries a capture-time sha256, recorded at capture or backfilled.

Fisqua's pipeline reads the preview of a RAW capture, and the media runner
checks its sha256 when it pulls it (exchange contract 1.1, §4), so the
digest has to be in the capture manifest as the master's is.

* At capture, the capture record lists the preview beside its master, as one
  more file: `{"role": "<role>_preview", "relative_path", "bytes",
  "mimetype": "image/jpeg", "sha256"}`. The preview is extracted and rotated
  before the record is written, so the digest is of the final bytes and the
  record stays one line.
* For previews captured before that, a `preview_digest` record per preview
  is appended by the backfill (tests/unit/test_preview_digest_backfill.py).
* The fixity index reads both. A `preview_digest` record is not a capture
  record: the export bag, the startup repair and the integrity check's
  "unresolved" test do not count it as one, and a preview's manifest entry
  does not keep the preview of a deleted master off the orphan list.

    python -m pytest tests/unit/test_preview_digests.py
"""

import hashlib
import io
import json
from pathlib import Path

import pytest

import capture.project_manager as project_manager_module
import capture.service as capture_service

STEM = "co-test-previews"
TENANT = "captua-test"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _jpeg(text: str) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 6), (len(text) % 255, 10, 20)).save(buf, format="JPEG")
    return buf.getvalue()


def _lines(path: Path) -> list:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# At capture
# ---------------------------------------------------------------------------

def _user(username, role):
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, role="operator"):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _user(role, role)
    return client


class _RawBackend:
    """Writes a `.cr2` and, unless `no_preview`, a real JPEG sidecar where preview_sidecar_of names it."""

    def __init__(self):
        self.raw = True
        self.no_preview = False

    def capture_image(self, output_path, camera_config, capture_output=False):
        from app.core.paths import preview_sidecar_of

        out = Path(output_path)
        if not self.raw:
            out.write_bytes(b"jpeg bytes of " + out.name.encode())
            return out, None
        out = out.with_suffix(".cr2")
        out.write_bytes(b"raw bytes of " + out.name.encode())
        if not self.no_preview:
            preview_sidecar_of(out).write_bytes(_jpeg(out.name))
        return out, None


@pytest.fixture
def rotate():
    return {"deg": 0}


@pytest.fixture
def backend(monkeypatch, rotate):
    b = _RawBackend()
    monkeypatch.setattr(capture_service, "get_backend", lambda: b)
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    monkeypatch.setattr(
        project_manager_module, "default_camera_config_from_registry",
        lambda camera_index, resolution="high", registry=None: (
            {"camera_index": camera_index, "rotate_deg": rotate["deg"]}, "hw"))
    return b


@pytest.fixture
def volume(db_session, test_project):
    from app.models.collection import Collection
    v = Collection(name="Tomo 1", project_id=test_project.id, filename_stem=STEM)
    db_session.add(v)
    db_session.commit()
    return v


def _capture(client, project, volume, dual):
    api = _client_as(client)
    payload = {"project_name": project.name, "collection_id": volume.id}
    if not dual:
        payload["camera_index"] = 0
    resp = api.post("/cameras/capture/dual" if dual else "/cameras/capture", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"], resp.json()
    return resp.json()


@pytest.mark.unit
def test_a_dual_raw_capture_records_each_preview_beside_its_master(
        client, db_session, test_project, volume, backend, override_projects_root):
    _capture(client, test_project, volume, dual=True)
    vol = override_projects_root / STEM
    [record] = _lines(vol / "metadata" / "manifest.jsonl")
    by_path = {f["relative_path"]: f for f in record["files"]}
    assert sorted(by_path) == sorted([
        f"captures/{STEM}-img_0001_c0.cr2", f"previews/{STEM}-img_0001_c0_preview.jpg",
        f"captures/{STEM}-img_0001_c1.cr2", f"previews/{STEM}-img_0001_c1_preview.jpg"])
    for cam, role in ((0, "left"), (1, "right")):
        preview = vol / "previews" / f"{STEM}-img_0001_c{cam}_preview.jpg"
        entry = by_path[f"previews/{preview.name}"]
        assert entry == {"role": f"{role}_preview", "relative_path": f"previews/{preview.name}",
                         "bytes": preview.stat().st_size, "mimetype": "image/jpeg",
                         "sha256": _sha256(preview.read_bytes())}


@pytest.mark.unit
def test_a_single_raw_capture_records_its_preview(
        client, db_session, test_project, volume, backend, override_projects_root):
    _capture(client, test_project, volume, dual=False)
    [record] = _lines(override_projects_root / STEM / "metadata" / "manifest.jsonl")
    assert [f["role"] for f in record["files"]] == ["single", "single_preview"]


@pytest.mark.unit
def test_the_recorded_digest_is_of_the_rotated_preview(
        client, db_session, test_project, volume, backend, rotate, override_projects_root):
    rotate["deg"] = 90
    _capture(client, test_project, volume, dual=False)
    vol = override_projects_root / STEM
    preview = vol / "previews" / f"{STEM}-img_0001_c0_preview.jpg"
    [record] = _lines(vol / "metadata" / "manifest.jsonl")
    [entry] = [f for f in record["files"] if f["role"] == "single_preview"]
    assert preview.read_bytes() != _jpeg(f"{STEM}-img_0001_c0.cr2")  # the rotation rewrote it
    assert entry["sha256"] == _sha256(preview.read_bytes())


@pytest.mark.unit
def test_a_raw_capture_whose_preview_was_not_extracted_records_the_master_only(
        client, db_session, test_project, volume, backend, override_projects_root):
    backend.no_preview = True
    _capture(client, test_project, volume, dual=False)
    [record] = _lines(override_projects_root / STEM / "metadata" / "manifest.jsonl")
    assert [f["role"] for f in record["files"]] == ["single"]


@pytest.mark.unit
def test_a_jpeg_master_never_records_a_preview(tmp_path):
    from capture.camera import CameraConfig
    from capture.manifestHandler import generate_manifest_record

    master = tmp_path / "folio.jpg"
    master.write_bytes(b"jpeg")
    (tmp_path / "folio_preview.jpg").write_bytes(b"not a sidecar of a JPEG")
    record = generate_manifest_record("p", [master], [CameraConfig(camera_index=0)], project_root=tmp_path)
    assert [f.role for f in record.files] == ["single"]


# ---------------------------------------------------------------------------
# The fixity index and its readers
# ---------------------------------------------------------------------------

def _volume_dir(root: Path) -> Path:
    vol = root / STEM
    for sub in ("captures", "previews", "metadata"):
        (vol / sub).mkdir(parents=True, exist_ok=True)
    return vol


def _write(path: Path, data: bytes) -> bytes:
    path.write_bytes(data)
    return data


def _append(vol: Path, entry: dict) -> None:
    with (vol / "metadata" / "manifest.jsonl").open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


@pytest.mark.unit
def test_the_index_reads_a_preview_from_its_capture_record(override_projects_root):
    from app.core.integrity import build_fixity_index

    vol = _volume_dir(override_projects_root)
    raw = _write(vol / "captures" / "a.cr2", b"raw")
    prev = _write(vol / "previews" / "a_preview.jpg", b"prev")
    _append(vol, {"capture_id": "c1", "files": [
        {"role": "single", "relative_path": "captures/a.cr2", "sha256": _sha256(raw)},
        {"role": "single_preview", "relative_path": "previews/a_preview.jpg", "sha256": _sha256(prev)}]})
    index = build_fixity_index(override_projects_root.resolve())
    assert index.expected("c1", None, (vol / "previews" / "a_preview.jpg").resolve()) == _sha256(prev)
    assert index.expected("c1", 5, (vol / "captures" / "a.cr2").resolve()) == _sha256(raw)


@pytest.mark.unit
def test_the_index_reads_a_backfilled_preview_digest_without_counting_it_a_capture(override_projects_root):
    from app.core.integrity import build_fixity_index

    vol = _volume_dir(override_projects_root)
    prev = _write(vol / "previews" / "a_preview.jpg", b"prev")
    _append(vol, {"record_type": "preview_digest", "capture_id": "c1", "image_id": 5,
                  "files": [{"role": "single_preview", "relative_path": "previews/a_preview.jpg",
                             "bytes": 4, "mimetype": "image/jpeg", "sha256": _sha256(prev)}],
                  "computed_at": "2026-10-24T00:00:00+00:00", "backfilled": True})
    index = build_fixity_index(override_projects_root.resolve())
    assert index.expected("c1", None, (vol / "previews" / "a_preview.jpg").resolve()) == _sha256(prev)
    assert "c1" not in index.captured
    # Never the master's digest: a preview_digest names no master.
    assert ("c1", 5) not in index.by_image
    assert index.expected("c1", 5, (vol / "captures" / "a.cr2").resolve()) is None


@pytest.mark.unit
def test_the_bag_carries_capture_records_and_not_preview_digests(override_projects_root):
    from app.core.integrity import capture_record_lines

    vol = _volume_dir(override_projects_root)
    capture = {"capture_id": "c1", "files": [
        {"role": "single", "relative_path": "captures/a.cr2", "sha256": "1" * 64},
        {"role": "single_preview", "relative_path": "previews/a_preview.jpg", "sha256": "2" * 64}]}
    digest = {"record_type": "preview_digest", "capture_id": "c1", "image_id": 5,
              "files": [{"role": "single_preview", "relative_path": "previews/a_preview.jpg",
                         "sha256": "2" * 64}], "computed_at": "x", "backfilled": True}
    _append(vol, capture)
    _append(vol, digest)
    lines = [json.loads(l) for l in capture_record_lines(override_projects_root.resolve(), {"c1"})]
    assert lines == [capture]


@pytest.mark.unit
def test_a_bag_export_of_a_volume_with_both_kinds_of_preview_record(client, db_session, override_projects_root,
                                                                     tmp_path, monkeypatch):
    """The whole export, through the API: the bag is built and its manifest holds the capture record only."""
    import time
    import zipfile
    from app.core import config
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    monkeypatch.setattr(config.settings, "EXPORTS_ROOT", str(tmp_path / "exp"))
    (tmp_path / "exp").mkdir()
    vol = _volume_dir(override_projects_root)
    p = Project(name="P")
    db_session.add(p)
    db_session.commit()
    col = Collection(name="Tomo", project_id=p.id, filename_stem=STEM)
    db_session.add(col)
    db_session.commit()
    rec = Record(title="r", collection_id=col.id, status="approved", sequence=1, capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    raw = _write(vol / "captures" / f"{STEM}-img_0001_c0.cr2", b"raw")
    prev = _write(vol / "previews" / f"{STEM}-img_0001_c0_preview.jpg", b"prev")
    img = RecordImage(record_id=rec.id, capture_id="c1", role="single", filename=f"{STEM}-img_0001_c0.cr2",
                      file_path=str(vol / "captures" / f"{STEM}-img_0001_c0.cr2"), format="cr2")
    db_session.add(img)
    db_session.commit()
    _append(vol, {"capture_id": "c1", "files": [
        {"role": "single", "relative_path": f"captures/{STEM}-img_0001_c0.cr2", "sha256": _sha256(raw)},
        {"role": "single_preview", "relative_path": f"previews/{STEM}-img_0001_c0_preview.jpg",
         "sha256": _sha256(prev)}]})
    _append(vol, {"record_type": "preview_digest", "capture_id": "c1", "image_id": img.id,
                  "files": [{"role": "single_preview",
                             "relative_path": f"previews/{STEM}-img_0001_c0_preview.jpg",
                             "sha256": _sha256(prev)}], "computed_at": "x", "backfilled": True})

    api = _client_as(client, "admin")
    resp = api.post(f"/collections/{col.id}/export")
    assert resp.status_code == 202, resp.text
    status_url = resp.json()["status_url"]
    for _ in range(200):
        body = api.get(status_url).json()
        if body["state"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert body["state"] == "done", body
    [zpath] = sorted(config.settings.exports_dir.glob(f"collection_{col.id}_*.zip"))
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        [manifest] = [n for n in names if n.endswith("manifest.jsonl")]
        records = [json.loads(l) for l in z.read(manifest).decode().splitlines() if l.strip()]
    assert [r.get("record_type") for r in records] == [None]
    assert records[0]["capture_id"] == "c1"


@pytest.mark.unit
def test_integrity_reports_the_preview_of_a_deleted_master_even_when_the_manifest_lists_it(
        db_session, override_projects_root):
    from app.core.integrity import run_integrity_check

    vol = _volume_dir(override_projects_root)
    raw = _write(vol / "captures" / "a.cr2", b"raw")
    prev = _write(vol / "previews" / "a_preview.jpg", b"prev")
    other = _write(vol / "previews" / "b_preview.jpg", b"prev b")
    _append(vol, {"capture_id": "c1", "files": [
        {"role": "single", "relative_path": "captures/a.cr2", "sha256": _sha256(raw)},
        {"role": "single_preview", "relative_path": "previews/a_preview.jpg", "sha256": _sha256(prev)}]})
    _append(vol, {"record_type": "preview_digest", "capture_id": "c2", "image_id": 9,
                  "files": [{"role": "single_preview", "relative_path": "previews/b_preview.jpg",
                             "sha256": _sha256(other)}], "computed_at": "x", "backfilled": True})
    (vol / "captures" / "a.cr2").unlink()  # its master is gone; the manifest keeps its line

    report = run_integrity_check(db_session, verify_hashes=False)

    assert sorted(report["orphan_files"]) == sorted([
        str((vol / "previews" / "a_preview.jpg").resolve()),
        str((vol / "previews" / "b_preview.jpg").resolve())])


@pytest.mark.unit
def test_integrity_hash_check_is_unchanged_by_preview_records(db_session, override_projects_root):
    from app.core.integrity import run_integrity_check
    from app.models.record import Record, RecordImage

    vol = _volume_dir(override_projects_root)
    raw = _write(vol / "captures" / "a.cr2", b"raw")
    prev = _write(vol / "previews" / "a_preview.jpg", b"prev")
    _append(vol, {"capture_id": "c1", "files": [
        {"role": "single", "relative_path": "captures/a.cr2", "sha256": _sha256(raw)},
        {"role": "single_preview", "relative_path": "previews/a_preview.jpg", "sha256": _sha256(prev)}]})
    _append(vol, {"record_type": "preview_digest", "capture_id": "c1", "image_id": 1,
                  "files": [{"role": "single_preview", "relative_path": "previews/a_preview.jpg",
                             "sha256": _sha256(prev)}], "computed_at": "x", "backfilled": True})
    # A capture known only by a preview_digest record: its image is not "unresolved".
    _append(vol, {"record_type": "preview_digest", "capture_id": "c2", "image_id": 2,
                  "files": [{"role": "single_preview", "relative_path": "previews/b_preview.jpg",
                             "sha256": "3" * 64}], "computed_at": "x", "backfilled": True})
    b = _write(vol / "captures" / "b.cr2", b"raw b")
    rec = Record(title="r", status="approved", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    for cid, name in (("c1", "a.cr2"), ("c2", "b.cr2")):
        db_session.add(RecordImage(record_id=rec.id, capture_id=cid, filename=name,
                                   file_path=str(vol / "captures" / name), format="cr2"))
    db_session.commit()

    report = run_integrity_check(db_session, verify_hashes=True)

    assert report["orphan_files"] == []
    assert report["manifest_mismatches"] == []


@pytest.mark.unit
def test_the_startup_repair_does_not_take_a_preview_digest_for_a_capture_record(db_session, legacy):
    from tests.unit.test_volume_layout_by_stem import STEMS, _capture_records
    import app.core.volume_layout as vl

    _capture_records(db_session, legacy)
    vl.migrate_volume_layout_at_startup(db_session)
    own = legacy["root"] / STEMS["a2"] / "metadata" / "manifest.jsonl"
    ids = sorted({json.loads(l)["capture_id"] for l in own.read_text().splitlines()
                  if json.loads(l).get("record_type") != "relocation"})
    own.write_text("".join(json.dumps({"record_type": "preview_digest", "capture_id": cid, "image_id": 1,
                                       "files": [], "computed_at": "x", "backfilled": True}) + "\n"
                           for cid in ids))

    report = vl.migrate_volume_layout(db_session)

    captures = sorted(r["capture_id"] for r in _lines(own)
                      if r.get("record_type") not in ("relocation", "preview_digest"))
    assert captures == ids
    assert report["manifests_repaired"] == {legacy["a2"].id: len(ids)}


from tests.unit.test_volume_layout_by_stem import capture_settings, data_dir, legacy  # noqa: E402,F401


# ---------------------------------------------------------------------------
# A preview renamed with its master keeps its digest
#
# storage_ops.relocate_image and the layout migration rename a RAW master on
# a name clash and move its preview to the name preview_sidecar_of derives
# from the new name; only the master gets a relocation record. The index
# follows that record to the preview: the preview of `from` is the preview of
# `to`.
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_index_follows_a_masters_relocation_record_to_its_preview(override_projects_root):
    from app.core.integrity import build_fixity_index

    vol = _volume_dir(override_projects_root)
    raw = _write(vol / "captures" / "a_x1.cr2", b"raw")
    prev = _write(vol / "previews" / "a_x1_preview.jpg", b"prev")
    _append(vol, {"capture_id": "c1", "files": [
        {"role": "single", "relative_path": "captures/a.cr2", "sha256": _sha256(raw)},
        {"role": "single_preview", "relative_path": "previews/a_preview.jpg", "sha256": _sha256(prev)}]})
    _append(vol, {"record_type": "relocation", "capture_id": "c1", "image_id": 7, "sha256": _sha256(raw),
                  "from": "captures/a.cr2", "to": "captures/a_x1.cr2", "relocated_at": "x"})
    index = build_fixity_index(override_projects_root.resolve())
    path = (vol / "previews" / "a_x1_preview.jpg").resolve()
    assert index.expected("c1", None, path) == _sha256(prev)
    assert str(path) in index.previews


@pytest.mark.unit
def test_the_index_follows_a_chain_of_relocations_to_the_preview(override_projects_root):
    from app.core.integrity import build_fixity_index

    vol = _volume_dir(override_projects_root)
    _append(vol, {"capture_id": "c1", "files": [
        {"role": "single_preview", "relative_path": "previews/a_preview.jpg", "sha256": "5" * 64}]})
    # Out of order on purpose: the second rename's record comes first.
    _append(vol, {"record_type": "relocation", "capture_id": "c1", "image_id": 7, "sha256": "4" * 64,
                  "from": "captures/a_x1.cr2", "to": "captures/a_x2.cr2"})
    _append(vol, {"record_type": "relocation", "capture_id": "c1", "image_id": 7, "sha256": "4" * 64,
                  "from": "captures/a.cr2", "to": "captures/a_x1.cr2"})
    index = build_fixity_index(override_projects_root.resolve())
    assert index.expected("c1", None, (vol / "previews" / "a_x2_preview.jpg").resolve()) == "5" * 64


@pytest.mark.unit
def test_a_jpeg_masters_relocation_gives_no_preview(override_projects_root):
    from app.core.integrity import build_fixity_index

    vol = _volume_dir(override_projects_root)
    _append(vol, {"capture_id": "c1", "files": [
        {"role": "single_preview", "relative_path": "previews/a_preview.jpg", "sha256": "5" * 64}]})
    _append(vol, {"record_type": "relocation", "capture_id": "c1", "image_id": 7, "sha256": "4" * 64,
                  "from": "captures/a.jpg", "to": "captures/a_x1.jpg"})
    index = build_fixity_index(override_projects_root.resolve())
    assert index.expected("c1", None, (vol / "previews" / "a_x1_preview.jpg").resolve()) is None


def _stemmed_raw(db, root, stem, name="IMG_0001.cr2", capture_id="cap-r1"):
    """A volume with one RAW image whose capture record lists its preview, as capture now writes it."""
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    vol = root / stem
    for sub in ("captures", "previews", "metadata"):
        (vol / sub).mkdir(parents=True, exist_ok=True)
    p = db.query(Project).filter(Project.name == "P").one_or_none()
    if p is None:
        p = Project(name="P")
        db.add(p)
        db.commit()
    col = Collection(name=stem, project_id=p.id, filename_stem=stem)
    db.add(col)
    db.commit()
    if name is None:
        return col, None, None
    raw = _write(vol / "captures" / name, b"raw " + name.encode())
    prev = _write(vol / "previews" / f"{Path(name).stem}_preview.jpg", b"preview " + name.encode())
    rec = Record(title="r", collection_id=col.id, status="approved", sequence=1, capture_mode="single")
    db.add(rec)
    db.commit()
    img = RecordImage(record_id=rec.id, capture_id=capture_id, role="single", filename=name,
                      file_path=str(vol / "captures" / name), format="cr2")
    db.add(img)
    db.commit()
    _append(vol, {"capture_id": capture_id, "files": [
        {"role": "single", "relative_path": f"captures/{name}", "sha256": _sha256(raw)},
        {"role": "single_preview", "relative_path": f"previews/{Path(name).stem}_preview.jpg",
         "sha256": _sha256(prev)}]})
    return col, img, prev


@pytest.mark.unit
def test_a_preview_renamed_by_relocate_image_keeps_its_digest(db_session, override_projects_root):
    from app.core.integrity import build_fixity_index
    from app.core.paths import preview_sidecar_of
    from app.core.preview_digests import backfill_preview_digests
    from app.core.storage_ops import relocate_image, write_relocation_records

    src_col, img, prev = _stemmed_raw(db_session, override_projects_root, "co-test-src")
    dst_col, _, _ = _stemmed_raw(db_session, override_projects_root, "co-test-dst", name=None)
    target = override_projects_root / "co-test-dst" / "captures"
    _write(target / "IMG_0001.cr2", b"another master of the same name")  # forces the rename

    relocations = []
    assert relocate_image(img, target, relocations=relocations)
    img.record.collection_id = dst_col.id
    db_session.commit()
    write_relocation_records(relocations)

    master = Path(img.file_path)
    assert master.name != "IMG_0001.cr2"
    moved_preview = preview_sidecar_of(master)
    assert moved_preview.read_bytes() == prev
    index = build_fixity_index(override_projects_root.resolve())
    assert index.expected(img.capture_id, None, moved_preview.resolve()) == _sha256(prev)

    report = backfill_preview_digests(db_session, "co-test-dst", apply=True)
    assert report["already_recorded"] == 1 and report["backfilled"] == []


@pytest.mark.unit
def test_a_preview_renamed_by_the_layout_migration_keeps_its_digest(db_session, legacy):
    from tests.unit.test_volume_layout_by_stem import STEMS, _capture_records
    import app.core.volume_layout as vl
    from app.core.integrity import build_fixity_index
    from app.core.paths import preview_sidecar_of
    from app.core.preview_digests import backfill_preview_digests
    from app.models.record import RecordImage

    _capture_records(db_session, legacy)
    # The RAW capture's record lists its preview, as capture now writes it.
    manifest = legacy["root"] / "proj" / "metadata" / "manifest.jsonl"
    preview = legacy["root"] / "proj" / "fondo_a" / "images" / "main" / "IMG_0001_preview.jpg"
    records = _lines(manifest)
    for r in records:
        if r["capture_id"] == "cap-i1":
            r["files"].append({"role": "single_preview", "relative_path": "fondo_a/images/main/IMG_0001_preview.jpg",
                               "sha256": _sha256(preview.read_bytes())})
    manifest.write_text("".join(json.dumps(r) + "\n" for r in records))
    digest = _sha256(preview.read_bytes())
    occupant = legacy["root"] / STEMS["a1"] / "captures" / "IMG_0001.CR2"
    occupant.parent.mkdir(parents=True)
    occupant.write_bytes(b"another master of the same name")  # forces the rename

    vl.migrate_volume_layout(db_session)

    img = db_session.get(RecordImage, legacy["i1"].id)
    master = Path(img.file_path)
    assert master.parent == legacy["root"] / STEMS["a1"] / "captures" and master.name != "IMG_0001.CR2"
    moved_preview = preview_sidecar_of(master)
    assert moved_preview.is_file()
    index = build_fixity_index(legacy["root"].resolve())
    assert index.expected("cap-i1", None, moved_preview.resolve()) == digest

    report = backfill_preview_digests(db_session, STEMS["a1"], apply=True)
    assert report["already_recorded"] == 1 and report["backfilled"] == []
