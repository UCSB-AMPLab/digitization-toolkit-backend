"""An upload into a volume with a stem is stored in `<stem>/captures/`, its thumbnail in `<stem>/thumbnails/`.

The file keeps the uuid name uploads have always had (naming is step 3).
Other uploads still go to the data directory's `uploads/`.
"""

import io
import re
from pathlib import Path

import pytest

from tests.unit.test_volume_layout_by_stem import _as_admin, capture_settings, data_dir  # noqa: F401


def _jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format="JPEG")
    return buf.getvalue()


def _uploads_dir() -> Path:
    """The uploads directory of the settings app.api.records reads (bound at its import)."""
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
    assert re.fullmatch(r"[0-9a-f]{32}\.jpg", stored.name)
    assert stored.read_bytes() == _jpeg()
    assert Path(img.thumbnail_path).parent == root / "thumbnails"
    assert not list(_uploads_dir().glob(f"{stored.stem}*"))


@pytest.mark.unit
def test_an_upload_into_a_project_level_record_still_goes_to_uploads(client, db_session, capture_settings, data_dir):
    from app.models.project import Project
    from app.models.record import RecordImage

    p = Project(name="P")
    db_session.add(p)
    db_session.commit()
    rec = _record(db_session, project_id=p.id)

    resp = _as_admin(client).post(f"/records/{rec.id}/images",
                                  files={"file": ("page.jpg", _jpeg(), "image/jpeg")})

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    img = db_session.query(RecordImage).one()
    assert Path(img.file_path).parent == _uploads_dir()
    assert Path(img.thumbnail_path).parent == _uploads_dir().parent / "thumbnails"
