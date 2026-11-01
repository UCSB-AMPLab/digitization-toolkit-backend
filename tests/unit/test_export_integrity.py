"""Export refuses to produce a misleading bag: a missing source file or a file
whose bytes no longer match its capture-time checksum both fail the export. 
The export runs as a background job, so these surface as a failed job with detail.

Version: 0.3.3
"""

import json
import time

import pytest


def _contributor():
    from app.models.user import User
    return User(username="op", email="op@example.com",
                hashed_password="x", role="operator", is_active=True)


@pytest.fixture
def export_client(client, db_session):
    from app.main import app
    import app.api.collections as collections
    app.dependency_overrides[collections.allow_read_only] = lambda: _contributor()
    yield client


def _approved_collection(db_session):
    from app.models.project import Project
    from app.models.collection import Collection
    from app.models.record import Record
    proj = Project(name="P")
    db_session.add(proj)
    db_session.commit()
    col = Collection(name="C", project_id=proj.id)
    db_session.add(col)
    db_session.commit()
    rec = Record(title="r", collection_id=col.id, status="approved", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)
    return col, rec


def _run_to_completion(client, collection_id, job_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/collections/{collection_id}/export/status/{job_id}")
        assert r.status_code == 200, r.text
        st = r.json()
        if st["state"] in ("done", "failed"):
            return st
        time.sleep(0.05)
    raise AssertionError("export job did not finish in time")


def test_export_rejects_missing_source_file(export_client, db_session):
    from app.models.record import RecordImage
    col, rec = _approved_collection(db_session)
    img = RecordImage(record_id=rec.id, filename="x.jpg", file_path="/nowhere/x.jpg",
                      format="jpg", role="single", is_current=True)
    db_session.add(img)
    db_session.commit()
    db_session.refresh(img)

    resp = export_client.post(f"/collections/{col.id}/export")
    assert resp.status_code == 202, resp.text
    st = _run_to_completion(export_client, col.id, resp.json()["job_id"])
    assert st["state"] == "failed"
    assert st["detail"]["reason"] == "missing_files"
    assert img.id in st["detail"]["missing_image_ids"]


def test_export_rejects_file_that_fails_capture_time_checksum(export_client, db_session, override_projects_root):
    from app.models.record import RecordImage
    projects_dir = override_projects_root
    proj_root = projects_dir / "P"
    (proj_root / "images").mkdir(parents=True)
    img_file = proj_root / "images" / "cap.jpg"
    img_file.write_bytes(b"the real captured bytes")
    (proj_root / "metadata").mkdir(parents=True)
    (proj_root / "metadata" / "manifest.jsonl").write_text(
        json.dumps({"capture_id": "cid1", "files": [
            {"role": "single", "relative_path": "images/cap.jpg", "sha256": "0" * 64}
        ]}) + "\n",
        encoding="utf-8",
    )

    col, rec = _approved_collection(db_session)
    img = RecordImage(record_id=rec.id, filename="cap.jpg", file_path=str(img_file),
                      format="jpg", role="single", capture_id="cid1", is_current=True)
    db_session.add(img)
    db_session.commit()
    db_session.refresh(img)

    resp = export_client.post(f"/collections/{col.id}/export")
    assert resp.status_code == 202, resp.text
    st = _run_to_completion(export_client, col.id, resp.json()["job_id"])
    assert st["state"] == "failed"
    assert st["detail"]["reason"] == "checksum_mismatch"
    assert img.id in st["detail"]["mismatched_image_ids"]


def test_export_produces_valid_bag_with_manifest(export_client, db_session, override_projects_root, monkeypatch, tmp_path):
    import hashlib, zipfile, bagit
    from app.core import config
    from app.models.record import RecordImage
    from capture.project_manager import secure_project_filename

    exports = tmp_path / "exp"
    exports.mkdir()
    monkeypatch.setattr(config.settings, "EXPORTS_ROOT", str(exports))

    # The export service resolves the manifest under the sanitised (lowercased)
    # project filename, not the raw project name, and the filesystem may be
    # case-sensitive (Linux/Docker/CI), unlike macOS.
    proot = override_projects_root / secure_project_filename("P")
    (proot / "images").mkdir(parents=True)
    (proot / "metadata").mkdir(parents=True)
    f = proot / "images" / "cap.jpg"
    f.write_bytes(b"captured bytes here")
    sha = hashlib.sha256(b"captured bytes here").hexdigest()
    (proot / "metadata" / "manifest.jsonl").write_text(
        json.dumps({"capture_id": "cid1", "files": [
            {"role": "single", "relative_path": "images/cap.jpg", "sha256": sha}]}) + "\n")

    col, rec = _approved_collection(db_session)
    db_session.add(RecordImage(record_id=rec.id, filename="cap.jpg", file_path=str(f),
                               format="jpg", role="single", capture_id="cid1", is_current=True))
    db_session.commit()

    resp = export_client.post(f"/collections/{col.id}/export")
    assert resp.status_code == 202, resp.text
    st = _run_to_completion(export_client, col.id, resp.json()["job_id"])
    assert st["state"] == "done", st
    assert "download_url" in st

    zname = st["zip_filename"]
    zpath = exports / zname
    ext = tmp_path / "ext"
    with zipfile.ZipFile(zpath) as zf:
        names = zf.namelist()
        zf.extractall(ext)
    assert any(n.endswith("data/metadata/manifest.jsonl") for n in names), names
    # The manually built archive validates as a real BagIt bag.
    bagit.Bag(str(ext / zname[:-4])).validate()


def test_prune_exports_keeps_newest(tmp_path):
    from app.core.export_service import _prune_exports
    import time as _t
    for i in range(5):
        (tmp_path / f"collection_7_2026010{i}T000000Z.zip").write_bytes(b"z")
        _t.sleep(0.01)
    (tmp_path / "collection_7_20260109T000000Z.zip.part").write_bytes(b"partial")
    _prune_exports(tmp_path, 7, keep=3)
    remaining = sorted(p.name for p in tmp_path.glob("collection_7_*.zip"))
    assert len(remaining) == 3
    assert remaining == ["collection_7_20260102T000000Z.zip",
                         "collection_7_20260103T000000Z.zip",
                         "collection_7_20260104T000000Z.zip"]
    assert list(tmp_path.glob("*.part")) == []


def test_exported_bag_metadata_carries_capture_mode_and_a_schema_version(
    export_client, db_session, override_projects_root, monkeypatch, tmp_path
):
    """NEH-259: the bag's metadata.json is what a receiving institution reads,
    so it carries capture_mode (single or dual) and no object_typology. The
    schema version tells a bag of this shape from one of an earlier shape;
    bags already exported are not rewritten."""
    import hashlib, zipfile
    from app.core import config

    from app.models.record import RecordImage
    from capture.project_manager import secure_project_filename

    exports = tmp_path / "exp"
    exports.mkdir()
    monkeypatch.setattr(config.settings, "EXPORTS_ROOT", str(exports))

    proot = override_projects_root / secure_project_filename("P")
    (proot / "images").mkdir(parents=True)
    (proot / "metadata").mkdir(parents=True)
    f = proot / "images" / "cap.jpg"
    f.write_bytes(b"captured bytes here")
    sha = hashlib.sha256(b"captured bytes here").hexdigest()
    (proot / "metadata" / "manifest.jsonl").write_text(
        json.dumps({"capture_id": "cid1", "files": [
            {"role": "single", "relative_path": "images/cap.jpg", "sha256": sha}]}) + "\n")

    col, rec = _approved_collection(db_session)
    db_session.add(RecordImage(record_id=rec.id, filename="cap.jpg", file_path=str(f),
                               format="jpg", role="single", capture_id="cid1", is_current=True))
    db_session.commit()

    resp = export_client.post(f"/collections/{col.id}/export")
    assert resp.status_code == 202, resp.text
    st = _run_to_completion(export_client, col.id, resp.json()["job_id"])
    assert st["state"] == "done", st

    zname = st["zip_filename"]
    with zipfile.ZipFile(exports / zname) as zf:
        meta_name = next(n for n in zf.namelist() if n.endswith("data/metadata.json"))
        meta = json.loads(zf.read(meta_name).decode("utf-8"))

    # The literal, not the constant -- see test_record_typology_dropped.py. Here it
    # also pins what a receiving institution reads out of the bag, and JSON round-trips
    # True as `true`, which no equality check against the constant would catch.
    assert type(meta["schema_version"]) is int
    assert meta["schema_version"] == 2
    assert len(meta["records"]) == 1
    entry = meta["records"][0]
    assert entry["capture_mode"] == "single"
    assert "object_typology" not in entry
    assert "material" not in entry
    # schema_version 2: every image carries rotate_deg (null for an image with none recorded).
    assert entry["images"] and all("rotate_deg" in i for i in entry["images"])


# ---------------------------------------------------------------------------
# An excluded record (Fisqua contract 1.6) is not a page of the volume: the
# export skips it, and it blocks nothing.
# ---------------------------------------------------------------------------

def _excluded(db_session, col, title="test shot", with_image=True):
    from app.models.record import Record, RecordImage
    rec = Record(title=title, collection_id=col.id, status="excluded", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)
    if with_image:
        # A file that is not on disk: the export would refuse it if it were planned.
        db_session.add(RecordImage(record_id=rec.id, filename="gone.jpg", file_path="/nowhere/gone.jpg",
                                   format="jpg", role="single", is_current=True))
        db_session.commit()
    return rec


def test_the_workflow_gate_skips_excluded_records():
    from types import SimpleNamespace
    from app.core.export_service import exported_records, workflow_blockers
    approved = SimpleNamespace(id=1, status="approved", images=[SimpleNamespace(is_current=True)])
    excluded = SimpleNamespace(id=2, status="excluded")
    review = SimpleNamespace(id=3, status="in_review")
    assert exported_records([approved, excluded]) == [approved]
    assert workflow_blockers(exported_records([approved, excluded])) is None
    assert workflow_blockers(exported_records([excluded, review]))[1] == [3]
    assert workflow_blockers(exported_records([excluded]))[0] == "Collection has no records to export."


def test_a_closed_missing_record_with_no_image_does_not_block_the_export():
    # Contract 1.9 §10.3: a closed missing record with no image never becomes a page.
    from types import SimpleNamespace
    from app.core.export_service import workflow_blockers
    image = SimpleNamespace(is_current=True)
    approved = SimpleNamespace(id=1, status="approved", missing_side=None, closed_reason=None, images=[image])
    closed_bare = SimpleNamespace(id=2, status="in_review", missing_side="both", closed_reason="foliation_skip",
                                  images=[SimpleNamespace(is_current=False)])
    closed_captured = SimpleNamespace(id=3, status="captured", missing_side="both",
                                      closed_reason="foliation_skip", images=[])
    closed_with_image = SimpleNamespace(id=4, status="in_review", missing_side="left",
                                        closed_reason="leaf_missing_from_volume", images=[image])
    open_bare = SimpleNamespace(id=5, status="captured", missing_side="both", closed_reason=None, images=[])
    assert workflow_blockers([approved, closed_bare]) is None
    assert workflow_blockers([approved, closed_captured]) is None
    assert workflow_blockers([approved, closed_with_image])[1] == [4]
    assert workflow_blockers([approved, open_bare])[1] == [5]


def test_a_volume_with_no_current_image_left_has_nothing_to_export():
    from types import SimpleNamespace
    from app.core.export_service import NOTHING_TO_EXPORT_MESSAGE, exported_records, workflow_blockers
    bare = SimpleNamespace(id=1, status="approved", missing_side=None, closed_reason=None,
                           images=[SimpleNamespace(is_current=False)])
    closed_bare = SimpleNamespace(id=2, status="in_review", missing_side="both", closed_reason="foliation_skip",
                                  images=[])
    assert workflow_blockers([bare]) == (NOTHING_TO_EXPORT_MESSAGE, [])
    assert workflow_blockers(exported_records([closed_bare])) == ("Collection has no records to export.", [])
    assert workflow_blockers([closed_bare]) == (NOTHING_TO_EXPORT_MESSAGE, [])


def test_exported_records_leave_out_a_closed_missing_record_with_no_image():
    from types import SimpleNamespace
    from app.core.export_service import exported_records
    image = SimpleNamespace(is_current=True)
    approved = SimpleNamespace(id=1, status="approved", missing_side=None, closed_reason=None, images=[image])
    closed_bare = SimpleNamespace(id=2, status="in_review", missing_side="both", closed_reason="foliation_skip",
                                  images=[SimpleNamespace(is_current=False)])
    closed_with_image = SimpleNamespace(id=3, status="approved", missing_side="left",
                                        closed_reason="leaf_missing_from_volume", images=[image])
    open_bare = SimpleNamespace(id=4, status="captured", missing_side="both", closed_reason=None, images=[])
    assert exported_records([approved, closed_bare, closed_with_image, open_bare]) == [
        approved, closed_with_image, open_bare]


def test_a_bag_with_a_closed_placeholder_builds_without_it_and_bags_a_closed_record_with_an_image(
    export_client, db_session, override_projects_root, monkeypatch, tmp_path
):
    import hashlib, zipfile, bagit
    from app.core import config
    from app.models.record import Record, RecordImage
    from capture.project_manager import secure_project_filename

    exports = tmp_path / "exp"
    exports.mkdir()
    monkeypatch.setattr(config.settings, "EXPORTS_ROOT", str(exports))
    proot = override_projects_root / secure_project_filename("P")
    (proot / "images").mkdir(parents=True)
    (proot / "metadata").mkdir(parents=True)
    lines = []
    paths = {}
    for name in ("cap.jpg", "half.jpg"):
        data = f"{name} bytes".encode()
        (proot / "images" / name).write_bytes(data)
        paths[name] = proot / "images" / name
        lines.append(json.dumps({"capture_id": f"cid-{name}", "files": [
            {"role": "single" if name == "cap.jpg" else "left", "relative_path": f"images/{name}",
             "sha256": hashlib.sha256(data).hexdigest()}]}))
    (proot / "metadata" / "manifest.jsonl").write_text("\n".join(lines) + "\n")

    col, rec = _approved_collection(db_session)
    rec.sequence = 1
    db_session.add(RecordImage(record_id=rec.id, filename="cap.jpg", file_path=str(paths["cap.jpg"]),
                               format="jpg", role="single", capture_id="cid-cap.jpg", is_current=True))
    hole = Record(title="placeholder hole", collection_id=col.id, status="in_review", capture_mode="dual",
                  sequence=2, missing_side="both", closed_reason="foliation_skip")
    half = Record(title="half", collection_id=col.id, status="approved", capture_mode="dual", sequence=3,
                  missing_side="right", closed_reason="leaf_missing_from_volume")
    db_session.add_all([hole, half])
    db_session.commit()
    db_session.add(RecordImage(record_id=half.id, filename="half.jpg", file_path=str(paths["half.jpg"]),
                               format="jpg", role="left", capture_id="cid-half.jpg", is_current=True))
    db_session.commit()

    resp = export_client.post(f"/collections/{col.id}/export")
    assert resp.status_code == 202, resp.text
    st = _run_to_completion(export_client, col.id, resp.json()["job_id"])
    assert st["state"] == "done", st

    zname = st["zip_filename"]
    ext = tmp_path / "ext"
    with zipfile.ZipFile(exports / zname) as zf:
        names = zf.namelist()
        meta = json.loads(zf.read(next(n for n in names if n.endswith("data/metadata.json"))))
        zf.extractall(ext)
    assert [r["id"] for r in meta["records"]] == [rec.id, half.id]
    assert "placeholder" not in json.dumps(meta)
    assert not any("placeholder" in n for n in names)
    bagit.Bag(str(ext / zname[:-4])).validate()


def test_an_excluded_record_neither_blocks_the_export_nor_enters_the_bag(
    export_client, db_session, override_projects_root, monkeypatch, tmp_path
):
    import hashlib, zipfile, bagit
    from app.core import config
    from app.models.record import RecordImage
    from capture.project_manager import secure_project_filename

    exports = tmp_path / "exp"
    exports.mkdir()
    monkeypatch.setattr(config.settings, "EXPORTS_ROOT", str(exports))
    proot = override_projects_root / secure_project_filename("P")
    (proot / "images").mkdir(parents=True)
    (proot / "metadata").mkdir(parents=True)
    f = proot / "images" / "cap.jpg"
    f.write_bytes(b"captured bytes here")
    sha = hashlib.sha256(b"captured bytes here").hexdigest()
    (proot / "metadata" / "manifest.jsonl").write_text(
        json.dumps({"capture_id": "cid1", "files": [
            {"role": "single", "relative_path": "images/cap.jpg", "sha256": sha}]}) + "\n")

    col, rec = _approved_collection(db_session)
    db_session.add(RecordImage(record_id=rec.id, filename="cap.jpg", file_path=str(f),
                               format="jpg", role="single", capture_id="cid1", is_current=True))
    db_session.commit()
    out = _excluded(db_session, col)
    _excluded(db_session, col, title="no image", with_image=False)

    resp = export_client.post(f"/collections/{col.id}/export")
    assert resp.status_code == 202, resp.text
    st = _run_to_completion(export_client, col.id, resp.json()["job_id"])
    assert st["state"] == "done", st

    zname = st["zip_filename"]
    ext = tmp_path / "ext"
    with zipfile.ZipFile(exports / zname) as zf:
        meta = json.loads(zf.read(next(n for n in zf.namelist() if n.endswith("data/metadata.json"))))
        zf.extractall(ext)
    assert [r["id"] for r in meta["records"]] == [rec.id]
    assert out.id not in [r["id"] for r in meta["records"]]
    bagit.Bag(str(ext / zname[:-4])).validate()


def test_a_collection_of_excluded_records_only_has_nothing_to_export(export_client, db_session):
    from app.models.project import Project
    from app.models.collection import Collection
    proj = Project(name="P")
    db_session.add(proj)
    db_session.commit()
    col = Collection(name="C", project_id=proj.id)
    db_session.add(col)
    db_session.commit()
    _excluded(db_session, col)
    resp = export_client.post(f"/collections/{col.id}/export")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["message"] == "Collection has no records to export."
