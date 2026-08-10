"""Export refuses to produce a misleading bag: a missing source file or a file
whose bytes no longer match its capture-time checksum both hard-fail the export."""

import json

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


def test_export_rejects_missing_source_file(export_client, db_session):
    from app.models.record import RecordImage
    col, rec = _approved_collection(db_session)
    img = RecordImage(record_id=rec.id, filename="x.jpg", file_path="/nowhere/x.jpg",
                      format="jpg", role="single", is_current=True)
    db_session.add(img)
    db_session.commit()
    db_session.refresh(img)

    resp = export_client.post(f"/collections/{col.id}/export")
    assert resp.status_code == 422, resp.text
    assert img.id in resp.json()["detail"]["missing_image_ids"]


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
    assert resp.status_code == 422, resp.text
    assert img.id in resp.json()["detail"]["mismatched_image_ids"]
