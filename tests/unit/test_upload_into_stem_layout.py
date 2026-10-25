"""An upload into a volume with a stem is stored in `<stem>/captures/`, its thumbnail in `<stem>/thumbnails/`.

The file is named as a single capture is, `<stem>-img_<nnnn>_c0.<ext>`
(contract 1.1, section 3); test_capture_contract_naming.py covers the rules.
Any other upload is refused with 409, on the same terms as a capture, before
anything is written.
"""

import io
from pathlib import Path

import pytest

from tests.unit.test_volume_layout_by_stem import _as_admin, capture_settings, data_dir  # noqa: F401


def _jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _records_data_dir(data_dir, monkeypatch):
    """app.api.records binds settings at its import; point its data directory at this test's too."""
    from app.api import records

    monkeypatch.setattr(records.settings, "DTK_DATA_DIR", str(data_dir))


def _uploads_dir() -> Path:
    from app.api import records

    return records.settings.data_dir / "uploads"


def _record(db, **parent):
    from app.models.record import Record

    rec = Record(title="r", status="captured", capture_mode="single", **parent)
    db.add(rec)
    db.commit()
    return rec


@pytest.mark.unit
def test_an_upload_into_a_stemmed_volume_goes_into_its_captures(client, db_session, capture_settings, data_dir,
                                                               override_projects_root):
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import RecordImage

    p = Project(name="P")
    db_session.add(p)
    db_session.commit()
    vol = Collection(name="Tomo", project_id=p.id, filename_stem="xx-test-001")
    db_session.add(vol)
    db_session.commit()
    rec = _record(db_session, collection_id=vol.id)

    resp = _as_admin(client).post(f"/records/{rec.id}/images",
                                  files={"file": ("page.jpg", _jpeg(), "image/jpeg")})

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    img = db_session.query(RecordImage).one()
    stored = Path(img.file_path)
    root = override_projects_root / "xx-test-001"
    assert stored.parent == root / "captures"
    assert stored.name == "xx-test-001-img_0001_c0.jpg"
    assert img.filename == stored.name
    assert stored.read_bytes() == _jpeg()
    assert Path(img.thumbnail_path).parent == root / "thumbnails"
    assert not list(_uploads_dir().glob(f"{stored.stem}*"))


@pytest.mark.unit
def test_an_upload_into_a_project_level_record_is_refused_and_writes_nothing(client, db_session, capture_settings,
                                                                           data_dir, override_projects_root):
    """An upload outside a stemmed volume is refused before anything is written."""
    from app.models.project import Project
    from app.models.record import RecordImage

    p = Project(name="P")
    db_session.add(p)
    db_session.commit()
    rec = _record(db_session, project_id=p.id)

    resp = _as_admin(client).post(f"/records/{rec.id}/images",
                                  files={"file": ("page.jpg", _jpeg(), "image/jpeg")})

    assert resp.status_code == 409, resp.text
    assert db_session.query(RecordImage).count() == 0
    assert not _uploads_dir().exists() or list(_uploads_dir().iterdir()) == []
    assert [p for p in override_projects_root.rglob("*") if p.is_file()] == []


@pytest.mark.unit
def test_the_projects_root_is_read_from_the_live_settings(client, db_session, override_projects_root):
    """Without patching capture.project_manager, a write lands under the root the test configured.

    The projects root is read from app.core.config.settings at each call, not
    from the object bound when capture.project_manager was imported, which a
    test that reloads the config leaves behind.
    """
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import RecordImage

    p = Project(name="P")
    db_session.add(p)
    db_session.commit()
    vol = Collection(name="Tomo", project_id=p.id, filename_stem="xx-test-live")
    db_session.add(vol)
    db_session.commit()
    rec = _record(db_session, collection_id=vol.id)

    resp = _as_admin(client).post(f"/records/{rec.id}/images",
                                  files={"file": ("page.jpg", _jpeg(), "image/jpeg")})

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    stored = Path(db_session.query(RecordImage).one().file_path)
    assert stored.parent == override_projects_root / "xx-test-live" / "captures"
