"""An image upload writes a record to its volume's capture manifest.

POST /records/{id}/images takes a number from the volume's counter and names
its file `<stem>-img_<nnnn>_c0.<ext>`. take_filename_index passes over any
number the volume's metadata/manifest.jsonl names, so the upload appends a
record there naming its file: an `upload` record, in the shape of a capture
record (`files` with `relative_path`, `bytes`, `mimetype`, `sha256`). After
the upload's image is deleted and the catalogue restored from before it, the
number is not given again.

The consumers of the manifest read the record as they read a capture record
when it carries the image's capture id, and skip it when it carries none:
the fixity index (app.core.integrity), the export bag's capture records
(capture_record_lines) and the startup repair of a volume's manifest
(app.core.volume_layout).
"""

import hashlib
import io
import json
import logging
from pathlib import Path

import pytest

from tests.unit.test_volume_layout_by_stem import _as_admin, capture_settings, data_dir  # noqa: F401

STEM = "xx-test-upl"


def _jpeg(colour="white") -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), colour).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _records_data_dir(data_dir, monkeypatch):
    from app.api import records

    monkeypatch.setattr(records.settings, "DTK_DATA_DIR", str(data_dir))


@pytest.fixture
def volume(db_session):
    from app.models.collection import Collection
    from app.models.project import Project

    p = Project(name="P")
    db_session.add(p)
    db_session.commit()
    v = Collection(name="Tomo", project_id=p.id, filename_stem=STEM)
    db_session.add(v)
    db_session.commit()
    return v


def _record(db, volume):
    from app.models.record import Record

    rec = Record(title="r", status="captured", capture_mode="single", collection_id=volume.id)
    db.add(rec)
    db.commit()
    return rec


def _upload(api, rec, name="page.jpg", body=None, capture_id=None):
    url = f"/records/{rec.id}/images" + (f"?capture_id={capture_id}" if capture_id else "")
    return api.post(url, files={"file": (name, body if body is not None else _jpeg(), "image/jpeg")})


def _manifest(root) -> list:
    m = root / STEM / "metadata" / "manifest.jsonl"
    return [json.loads(l) for l in m.read_text().splitlines() if l.strip()] if m.exists() else []


@pytest.mark.unit
def test_a_restored_catalogue_does_not_reuse_the_number_of_a_deleted_upload(
        client, db_session, capture_settings, override_projects_root, volume):
    """Fails without the upload record: nothing else remembers the deleted upload's number."""
    from sqlalchemy import text
    from app.models.record import RecordImage

    api = _as_admin(client)
    rec = _record(db_session, volume)
    assert _upload(api, rec).json()["filename"] == f"{STEM}-img_0001_c0.jpg"
    second = _upload(api, rec).json()
    assert second["filename"] == f"{STEM}-img_0002_c0.jpg"
    assert api.delete(f"/records/images/{second['id']}").status_code == 200
    assert not (override_projects_root / STEM / "captures" / second["filename"]).exists()
    # The catalogue restored from a backup taken after the first upload.
    db_session.execute(text("UPDATE collections SET capture_counter = 1 WHERE id = :i"), {"i": volume.id})
    db_session.commit()

    third = _upload(api, rec)

    assert third.status_code == 200, third.text
    assert third.json()["filename"] == f"{STEM}-img_0003_c0.jpg"
    db_session.expire_all()
    names = [i.filename for i in db_session.query(RecordImage).all()]
    assert len(names) == len(set(names))


@pytest.mark.unit
def test_an_upload_appends_a_record_naming_its_file(client, db_session, capture_settings,
                                                     override_projects_root, volume):
    body = _jpeg()
    resp = _upload(_as_admin(client), _record(db_session, volume), body=body)

    assert resp.status_code == 200, resp.text
    [rec] = _manifest(override_projects_root)
    assert rec["record_type"] == "upload"
    assert rec["capture_id"] is None
    assert rec["files"] == [{
        "role": "single",
        "relative_path": f"captures/{STEM}-img_0001_c0.jpg",
        "bytes": len(body),
        "mimetype": "image/jpeg",
        "sha256": hashlib.sha256(body).hexdigest(),
    }]
    assert rec["timestamp_utc"]


@pytest.mark.unit
def test_the_record_starts_on_a_line_of_its_own_after_a_torn_one(client, db_session, capture_settings,
                                                                  override_projects_root, volume):
    meta = override_projects_root / STEM / "metadata"
    meta.mkdir(parents=True)
    (meta / "manifest.jsonl").write_bytes(b'{"capture_id": "torn", "fi')

    assert _upload(_as_admin(client), _record(db_session, volume)).status_code == 200

    lines = (meta / "manifest.jsonl").read_text().splitlines()
    assert lines[0] == '{"capture_id": "torn", "fi'
    assert json.loads(lines[1])["record_type"] == "upload"


@pytest.mark.unit
def test_the_upload_logs_the_name_the_client_sent(client, db_session, capture_settings,
                                                  override_projects_root, volume, caplog):
    with caplog.at_level(logging.INFO, logger="app.api.records"):
        resp = _upload(_as_admin(client), _record(db_session, volume), name="Scan 0042.JPG")

    assert resp.status_code == 200, resp.text
    assert any("'Scan 0042.JPG'" in r.getMessage() and f"{STEM}-img_0001_c0.jpg" in r.getMessage()
               and r.levelno == logging.INFO for r in caplog.records)


@pytest.mark.unit
def test_an_upload_with_a_capture_id_is_verified_against_its_record(client, db_session, capture_settings,
                                                                    override_projects_root, volume):
    from app.core.integrity import build_fixity_index, verify_images_against_manifest
    from app.models.record import RecordImage

    body = _jpeg()
    resp = _upload(_as_admin(client), _record(db_session, volume), body=body, capture_id="upl-1")
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    img = db_session.get(RecordImage, resp.json()["id"])

    index = build_fixity_index(override_projects_root.resolve())
    assert "upl-1" in index.captured
    assert index.expected("upl-1", img.id, Path(img.file_path).resolve()) == hashlib.sha256(body).hexdigest()
    assert verify_images_against_manifest([img]) == []

    Path(img.file_path).write_bytes(_jpeg("black"))
    assert [m["reason"] for m in verify_images_against_manifest([img])] == ["mismatch"]


@pytest.mark.unit
def test_an_upload_without_a_capture_id_adds_nothing_to_the_fixity_index(client, db_session, capture_settings,
                                                                         override_projects_root, volume):
    from app.core.integrity import build_fixity_index

    assert _upload(_as_admin(client), _record(db_session, volume)).status_code == 200

    index = build_fixity_index(override_projects_root.resolve())
    assert index.captured == set()
    assert index.by_path == {}


@pytest.mark.unit
def test_the_export_bag_carries_the_upload_record_of_its_capture_id(client, db_session, capture_settings,
                                                                    override_projects_root, volume):
    from app.core.integrity import capture_record_lines

    api = _as_admin(client)
    rec = _record(db_session, volume)
    assert _upload(api, rec, capture_id="upl-1").status_code == 200
    assert _upload(api, rec).status_code == 200

    lines = capture_record_lines(override_projects_root.resolve(), {"upl-1", "other"})

    assert [json.loads(l)["capture_id"] for l in lines] == ["upl-1"]
    assert json.loads(lines[0])["record_type"] == "upload"


@pytest.mark.unit
def test_the_startup_repair_finds_an_uploads_record_in_its_volumes_manifest(client, db_session, capture_settings,
                                                                            override_projects_root, volume):
    """Without the record, an upload's capture id was reported at every startup as having none."""
    from app.core.volume_layout import migrate_volume_layout

    api = _as_admin(client)
    rec = _record(db_session, volume)
    assert _upload(api, rec, capture_id="upl-1").status_code == 200
    assert _upload(api, rec).status_code == 200
    before = (override_projects_root / STEM / "metadata" / "manifest.jsonl").read_bytes()

    report = migrate_volume_layout(db_session)

    assert report["captures_without_record"] == {}
    assert report["manifests_repaired"] == {}
    assert (override_projects_root / STEM / "metadata" / "manifest.jsonl").read_bytes() == before
    assert sorted(p.name for p in (override_projects_root / STEM / "captures").iterdir()) == [
        f"{STEM}-img_0001_c0.jpg", f"{STEM}-img_0002_c0.jpg"]


@pytest.mark.unit
def test_a_failed_commit_still_reserves_the_uploads_number(client, db_session, capture_settings,
                                                          override_projects_root, volume, monkeypatch):
    """The file and its thumbnail are removed as before; the manifest line stays, so the number is not reused."""
    api = _as_admin(client)
    rec = _record(db_session, volume)
    real_commit = db_session.commit
    calls = {"n": 0}

    def failing_commit():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk full at commit")
        return real_commit()

    monkeypatch.setattr(db_session, "commit", failing_commit)
    assert _upload(api, rec).status_code == 500
    monkeypatch.setattr(db_session, "commit", real_commit)
    assert not any((override_projects_root / STEM / "captures").iterdir())
    assert [r["files"][0]["relative_path"] for r in _manifest(override_projects_root)] == [
        f"captures/{STEM}-img_0001_c0.jpg"]

    resp = _upload(api, rec)

    assert resp.json()["filename"] == f"{STEM}-img_0002_c0.jpg"
