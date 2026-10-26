"""`/images/{id}/file` serves the master; `/images/{id}/preview` serves something a browser can show.

A RAW capture lands on the card as the master (`IMG_0001.CR2`) and a JPEG
sidecar the RAW backend writes beside it (`IMG_0001_preview.jpg`). A file
endpoint that swaps the sidecar in for a RAW master while still naming the
download after the master makes the download button and the collection ZIP
hand out a JPEG called `IMG_0001.CR2`, and with no sidecar it sends RAW bytes
to an `<img>` tag.

These tests pin the split: `/file` always sends the master's own bytes under
its own filename; `/preview` sends the master for a browser-displayable
format, the sidecar for a RAW master, and a 404 rather than RAW bytes when
there is no sidecar. Both endpoints confine what they send to the storage
roots, and on-demand thumbnail regeneration for a RAW master reads the
sidecar, as capture does.

Storage setup follows tests/unit/test_record_delete_sidecar.py: every file
lives under the per-test projects root from `override_projects_root`.

Version: 0.1.0-beta
"""

import io

import pytest


RAW_BYTES = b"raw-master-bytes-not-a-jpeg"
JPEG_MASTER_BYTES = b"\xff\xd8jpeg-master-bytes"
SIDECAR_BYTES = b"\xff\xd8sidecar-preview-bytes"


def _user(username, role):
    from app.models.user import User

    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, username, role):
    from app.main import app
    import app.api.records as records

    user = _user(username, role)
    for dep in (records.allow_contributor, records.allow_read_only):
        app.dependency_overrides[dep] = lambda u=user: u
    return client


@pytest.fixture
def capture_dir(override_projects_root):
    d = override_projects_root / "preview_project" / "images" / "main"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _record(db_session):
    from app.models.record import Record

    rec = Record(title="r", status="in_review", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)
    return rec


def _image(db_session, master, thumb=None):
    from app.models.record import RecordImage

    rec = _record(db_session)
    img = RecordImage(
        record_id=rec.id,
        filename=master.name,
        file_path=str(master),
        thumbnail_path=str(thumb) if thumb is not None else None,
        format=master.suffix.lstrip(".").lower(),
    )
    db_session.add(img)
    db_session.commit()
    db_session.refresh(img)
    return img


def _raw(capture_dir, stem="IMG_0001", sidecar=True):
    master = capture_dir / f"{stem}.CR2"
    master.write_bytes(RAW_BYTES)
    preview = capture_dir / f"{stem}_preview.jpg"
    if sidecar:
        preview.write_bytes(SIDECAR_BYTES)
    return master, preview


def _real_jpeg_bytes():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (120, 80, 40)).save(buf, format="JPEG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# GET /records/images/{id}/file
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_file_sends_the_raw_master_not_its_sidecar(client, db_session, capture_dir):
    master, _ = _raw(capture_dir, sidecar=True)
    img = _image(db_session, master)

    resp = _client_as(client, "rev", "reviewer").get(f"/records/images/{img.id}/file")

    assert resp.status_code == 200, resp.text
    assert resp.content == RAW_BYTES
    assert 'filename="IMG_0001.CR2"' in resp.headers["content-disposition"]
    assert resp.headers["content-type"] == "image/x-canon-cr2"


@pytest.mark.unit
def test_file_sends_the_raw_master_when_no_sidecar_exists(client, db_session, capture_dir):
    master, _ = _raw(capture_dir, sidecar=False)
    img = _image(db_session, master)

    resp = _client_as(client, "rev", "reviewer").get(f"/records/images/{img.id}/file")

    assert resp.status_code == 200, resp.text
    assert resp.content == RAW_BYTES
    assert 'filename="IMG_0001.CR2"' in resp.headers["content-disposition"]


@pytest.mark.unit
def test_file_sends_a_jpeg_master_as_jpeg(client, db_session, capture_dir):
    master = capture_dir / "IMG_0002.jpg"
    master.write_bytes(JPEG_MASTER_BYTES)
    img = _image(db_session, master)

    resp = _client_as(client, "rev", "reviewer").get(f"/records/images/{img.id}/file")

    assert resp.status_code == 200, resp.text
    assert resp.content == JPEG_MASTER_BYTES
    assert resp.headers["content-type"] == "image/jpeg"


@pytest.mark.unit
def test_file_refuses_a_master_outside_storage(client, db_session, tmp_path):
    from app.core.paths import resolve_within_storage

    outside = tmp_path / "outside"
    outside.mkdir()
    master = outside / "IMG_0003.jpg"
    master.write_bytes(JPEG_MASTER_BYTES)
    assert resolve_within_storage(str(master)) is None, "fixture is not outside storage"
    img = _image(db_session, master)

    resp = _client_as(client, "rev", "reviewer").get(f"/records/images/{img.id}/file")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /records/images/{id}/preview
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_preview_sends_the_sidecar_for_a_raw_master(client, db_session, capture_dir):
    master, _ = _raw(capture_dir, sidecar=True)
    img = _image(db_session, master)

    resp = _client_as(client, "rev", "reviewer").get(f"/records/images/{img.id}/preview")

    assert resp.status_code == 200, resp.text
    assert resp.content == SIDECAR_BYTES
    assert resp.headers["content-type"] == "image/jpeg"
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("inline")
    assert 'filename="IMG_0001_preview.jpg"' in disposition


@pytest.mark.unit
def test_preview_sends_a_jpeg_master_itself(client, db_session, capture_dir):
    master = capture_dir / "IMG_0004.jpg"
    master.write_bytes(JPEG_MASTER_BYTES)
    img = _image(db_session, master)

    resp = _client_as(client, "rev", "reviewer").get(f"/records/images/{img.id}/preview")

    assert resp.status_code == 200, resp.text
    assert resp.content == JPEG_MASTER_BYTES
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["content-disposition"].startswith("inline")


@pytest.mark.unit
def test_preview_is_404_for_a_raw_master_without_sidecar(client, db_session, capture_dir):
    master, _ = _raw(capture_dir, sidecar=False)
    img = _image(db_session, master)

    resp = _client_as(client, "rev", "reviewer").get(f"/records/images/{img.id}/preview")

    assert resp.status_code == 404
    assert resp.content != RAW_BYTES
    assert "preview" in resp.json()["detail"].lower()


@pytest.mark.unit
def test_preview_is_404_for_a_tiff_master(client, db_session, capture_dir):
    master = capture_dir / "IMG_0005.tif"
    master.write_bytes(b"II*\x00tiff-bytes")
    img = _image(db_session, master)

    resp = _client_as(client, "rev", "reviewer").get(f"/records/images/{img.id}/preview")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "No browser-displayable preview for .tif images"}


@pytest.mark.unit
def test_preview_refuses_a_master_outside_storage(client, db_session, tmp_path):
    from app.core.paths import resolve_within_storage

    outside = tmp_path / "outside"
    outside.mkdir()
    master = outside / "IMG_0006.CR2"
    master.write_bytes(RAW_BYTES)
    (outside / "IMG_0006_preview.jpg").write_bytes(SIDECAR_BYTES)
    assert resolve_within_storage(str(master)) is None, "fixture is not outside storage"
    img = _image(db_session, master)

    resp = _client_as(client, "rev", "reviewer").get(f"/records/images/{img.id}/preview")

    assert resp.status_code == 404
    assert resp.content != SIDECAR_BYTES
    assert resp.json() == {"detail": "File not found on disk"}


@pytest.mark.unit
def test_preview_refuses_a_sidecar_symlinked_outside_storage(client, db_session, capture_dir, tmp_path):
    """A master inside storage whose `_preview.jpg` is a symlink out of it gets no preview."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.jpg"
    secret.write_bytes(b"\xff\xd8not-for-serving")
    master = capture_dir / "IMG_0007.CR2"
    master.write_bytes(RAW_BYTES)
    (capture_dir / "IMG_0007_preview.jpg").symlink_to(secret)
    img = _image(db_session, master)

    resp = _client_as(client, "rev", "reviewer").get(f"/records/images/{img.id}/preview")

    assert resp.status_code == 404
    assert b"not-for-serving" not in resp.content
    assert resp.json() == {"detail": "No JPEG preview exists for this RAW image"}


@pytest.mark.unit
def test_preview_is_404_for_an_unknown_image(client, db_session):
    resp = _client_as(client, "rev", "reviewer").get("/records/images/999999/preview")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Image not found"}


# ---------------------------------------------------------------------------
# GET /records/images/{id}/thumbnail — on-demand regeneration
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_thumbnail_regeneration_reads_the_raw_sidecar(client, db_session, capture_dir, monkeypatch):
    """A RAW master with no thumbnail gets one built from its sidecar.

    The master holds bytes Pillow cannot open, so a 200 here also shows the
    master was not the source.
    """
    import app.api.records as records

    master = capture_dir / "IMG_0008.CR2"
    master.write_bytes(RAW_BYTES)
    sidecar = capture_dir / "IMG_0008_preview.jpg"
    sidecar.write_bytes(_real_jpeg_bytes())
    img = _image(db_session, master, thumb=None)

    sources = []
    real = records.generate_thumbnail

    def spy(source_path, dest_dir, *a, **kw):
        sources.append(source_path)
        return real(source_path, dest_dir, *a, **kw)

    monkeypatch.setattr(records, "generate_thumbnail", spy)

    resp = _client_as(client, "rev", "reviewer").get(f"/records/images/{img.id}/thumbnail")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/jpeg"
    assert sources == [sidecar.resolve()]
    db_session.refresh(img)
    assert img.thumbnail_path is not None


@pytest.mark.unit
def test_thumbnail_regeneration_reads_a_jpeg_master_directly(client, db_session, capture_dir, monkeypatch):
    import app.api.records as records

    master = capture_dir / "IMG_0009.jpg"
    master.write_bytes(_real_jpeg_bytes())
    # A stray file with the sidecar's name beside a JPEG master is not used.
    (capture_dir / "IMG_0009_preview.jpg").write_bytes(_real_jpeg_bytes())
    img = _image(db_session, master, thumb=None)

    sources = []
    real = records.generate_thumbnail

    def spy(source_path, dest_dir, *a, **kw):
        sources.append(source_path)
        return real(source_path, dest_dir, *a, **kw)

    monkeypatch.setattr(records, "generate_thumbnail", spy)

    resp = _client_as(client, "rev", "reviewer").get(f"/records/images/{img.id}/thumbnail")

    assert resp.status_code == 200, resp.text
    assert sources == [master.resolve()]
