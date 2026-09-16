"""Export refuses to produce a misleading bag: a missing source file or a file
whose bytes no longer match its capture-time checksum both fail the export. 
The export runs as a background job, so these surface as a failed job with detail."""

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
    # newest kept
    assert remaining == ["collection_7_20260102T000000Z.zip",
                         "collection_7_20260103T000000Z.zip",
                         "collection_7_20260104T000000Z.zip"]
    # stale .part cleaned
    assert list(tmp_path.glob("*.part")) == []
