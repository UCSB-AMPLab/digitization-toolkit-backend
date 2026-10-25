"""A body's rotation is recorded beside each image, and a RAW master is left as the camera wrote it.

The per-body rotation (NEH-71) is held in degrees clockwise (0, 90, 180,
270): the registry's `orientation`, applied as CameraConfig.rotate_deg. A
JPEG, master or a RAW master's preview, carries it as the EXIF Orientation
tag, written losslessly. A RAW master (`.cr2`) is a preservation master and
is never rewritten: piexif can only insert into a JPEG, and on a CR2 it
raises. The rotation is
recorded, for every captured image, as `rotate_deg`:

* in each `files` entry of the capture record in the volume's manifest
  (old records simply lack it),
* on RecordImage, and so in the API,
* in each image of the BagIt export's metadata.json (schema_version 2).

Run with: python -m pytest tests/unit/test_master_orientation.py
"""

import hashlib
import importlib.util
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import piexif
import pytest
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.exc import IntegrityError

import capture.project_manager as project_manager_module
import capture.service as capture_service


STEM = "co-test-orient"
ORIENTATION_TAG = 0x0112


def _jpeg_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (8, 4), "white").save(buf, format="JPEG")
    return buf.getvalue()


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _user(username, role):
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, username="op", role="operator"):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


class _FakeBackend:
    """Writes a master where capture asks: a real JPEG, or a `.cr2` (not a JPEG) and a real JPEG sidecar."""

    def __init__(self):
        self.raw = False
        self.written = {}  # name -> sha256 of the bytes the camera wrote

    def capture_image(self, output_path, camera_config, capture_output=False):
        from app.core.paths import preview_sidecar_of

        out = Path(output_path)
        if self.raw:
            out = out.with_suffix(".cr2")
            out.write_bytes(b"II*\x00\x10\x00\x00\x00CR\x02\x00 raw sensor bytes of " + out.name.encode())
            preview_sidecar_of(out).write_bytes(_jpeg_bytes())
        else:
            out.write_bytes(_jpeg_bytes())
        self.written[out.name] = _sha(out)
        return out, None


@pytest.fixture
def rotations():
    return {0: 90, 1: 270}


@pytest.fixture
def backend(monkeypatch, rotations):
    b = _FakeBackend()
    monkeypatch.setattr(capture_service, "get_backend", lambda: b)
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    monkeypatch.setattr(
        project_manager_module, "default_camera_config_from_registry",
        lambda camera_index, resolution="high", registry=None: (
            {"camera_index": camera_index, "rotate_deg": rotations[camera_index]}, "hw"))
    return b


@pytest.fixture
def volume(db_session, test_project):
    from app.models.collection import Collection
    v = Collection(name="Tomo 7", project_id=test_project.id, filename_stem=STEM)
    db_session.add(v)
    db_session.commit()
    return v


def _post(api, path, payload):
    resp = api.post(path, json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"], resp.json()
    return resp.json()


def _manifest(root):
    lines = (root / STEM / "metadata" / "manifest.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _images(db_session, record_id):
    from app.models.record import RecordImage
    db_session.expire_all()
    return (db_session.query(RecordImage).filter(RecordImage.record_id == record_id)
            .order_by(RecordImage.id).all())


def _orientation(path):
    return piexif.load(str(path))["0th"].get(ORIENTATION_TAG)


def _exif_warnings(caplog):
    return [r for r in caplog.records if "EXIF orientation" in r.getMessage()]


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_cr2_capture_is_left_untouched_and_its_rotation_recorded(
        client, db_session, test_project, volume, backend, override_projects_root, caplog):
    from app.core.paths import preview_sidecar_of

    backend.raw = True
    caplog.set_level(logging.WARNING, logger="capture_service")
    out = _post(_client_as(client), "/cameras/capture",
                {"project_name": test_project.name, "camera_index": 0, "collection_id": volume.id})

    master = Path(out["file_path"])
    assert master.suffix == ".cr2"
    assert _sha(master) == backend.written[master.name], "the RAW master keeps the camera's bytes"
    assert _exif_warnings(caplog) == [], "a RAW master is skipped, not failed"
    assert _orientation(preview_sidecar_of(master)) == 6, "the preview still carries the rotation"

    record = _manifest(override_projects_root)[-1]
    assert {f["role"]: f["rotate_deg"] for f in record["files"]} == {"single": 90, "single_preview": 90}
    assert _images(db_session, out["record_id"])[0].rotate_deg == 90


@pytest.mark.unit
def test_a_jpeg_master_still_gets_its_exif_orientation(
        client, db_session, test_project, volume, backend, override_projects_root, caplog):
    caplog.set_level(logging.WARNING, logger="capture_service")
    out = _post(_client_as(client), "/cameras/capture",
                {"project_name": test_project.name, "camera_index": 1, "collection_id": volume.id})

    assert _orientation(out["file_path"]) == 8  # 270 deg clockwise
    assert _exif_warnings(caplog) == []
    record = _manifest(override_projects_root)[-1]
    assert [(f["role"], f["rotate_deg"]) for f in record["files"]] == [("single", 270)]
    assert _images(db_session, out["record_id"])[0].rotate_deg == 270


@pytest.mark.unit
def test_no_rotation_is_recorded_as_zero(
        client, db_session, test_project, volume, backend, override_projects_root, rotations):
    rotations[0] = 0
    out = _post(_client_as(client), "/cameras/capture",
                {"project_name": test_project.name, "camera_index": 0, "collection_id": volume.id})

    assert _orientation(out["file_path"]) is None
    assert [f["rotate_deg"] for f in _manifest(override_projects_root)[-1]["files"]] == [0]
    assert _images(db_session, out["record_id"])[0].rotate_deg == 0


@pytest.mark.unit
def test_a_dual_capture_records_each_bodys_rotation(
        client, db_session, test_project, volume, backend, override_projects_root):
    backend.raw = True
    out = _post(_client_as(client), "/cameras/capture/dual",
                {"project_name": test_project.name, "collection_id": volume.id})

    record = _manifest(override_projects_root)[-1]
    assert {f["role"]: f["rotate_deg"] for f in record["files"]} == {
        "left": 90, "left_preview": 90, "right": 270, "right_preview": 270}
    assert {i.role: i.rotate_deg for i in _images(db_session, out["record_id"])} == {"left": 90, "right": 270}
    for name, sha in backend.written.items():
        assert _sha(override_projects_root / STEM / "captures" / name) == sha


# ---------------------------------------------------------------------------
# _apply_rotation
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("suffix", [".cr2", ".CR2", ".raw", ".dng"])
def test_a_raw_master_is_not_rewritten_and_not_warned_about(tmp_path, caplog, suffix):
    raw = tmp_path / f"img{suffix}"
    raw.write_bytes(b"II*\x00 not a jpeg")
    before = _sha(raw)
    caplog.set_level(logging.DEBUG, logger="capture_service")

    capture_service._apply_rotation(raw, 90)

    assert _sha(raw) == before
    assert _exif_warnings(caplog) == []


@pytest.mark.unit
def test_a_jpeg_that_cannot_take_the_tag_is_warned_about_with_the_exception_type(tmp_path, caplog):
    bad = tmp_path / "img.jpg"
    bad.write_bytes(b"not a jpeg at all")
    caplog.set_level(logging.WARNING, logger="capture_service")

    capture_service._apply_rotation(bad, 90)

    [warning] = _exif_warnings(caplog)
    message = warning.getMessage()
    assert "img.jpg" in message
    assert message.rstrip().split(": ", 1)[1].strip(), "the warning is never empty"
    assert any(name in message for name in ("InvalidImageDataError", "ValueError", "Error"))


# ---------------------------------------------------------------------------
# The record and its readers
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_upload_records_carry_no_rotation(tmp_path):
    """An upload is not a capture: nothing decided its rotation, so the field is absent."""
    from capture.manifestHandler import append_upload_record

    append_upload_record(tmp_path, capture_id="c", relative_path="captures/x.jpg",
                         size=1, mimetype="image/jpeg", sha256="0" * 64)
    [entry] = [json.loads(l) for l in (tmp_path / "metadata" / "manifest.jsonl").read_text().splitlines()]
    assert all("rotate_deg" not in f for f in entry["files"])


@pytest.mark.unit
def test_old_manifest_lines_without_the_field_still_index(tmp_path):
    from app.core.integrity import build_fixity_index

    vol = tmp_path / "Project" / STEM
    (vol / "metadata").mkdir(parents=True)
    old = {"capture_id": "old", "files": [{"role": "single", "relative_path": "captures/a.cr2",
                                           "bytes": 1, "mimetype": "image/cr2", "sha256": "a" * 64}]}
    new = {"capture_id": "new", "files": [{"role": "single", "relative_path": "captures/b.cr2",
                                           "bytes": 1, "mimetype": "image/cr2", "sha256": "b" * 64,
                                           "rotate_deg": 90}]}
    (vol / "metadata" / "manifest.jsonl").write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n")

    index = build_fixity_index(tmp_path)

    assert {"old", "new"} <= index.captured
    assert index.by_name["old"]["a.cr2"] == "a" * 64
    assert index.by_name["new"]["b.cr2"] == "b" * 64


@pytest.mark.unit
def test_metadata_json_carries_the_rotation_under_schema_version_2():
    from app.core import export_service

    def image(i, rotate_deg):
        return SimpleNamespace(id=i, filename=f"{i}.cr2", role="single", sequence=None, format="cr2",
                               resolution_width=None, resolution_height=None, file_size=1,
                               rotate_deg=rotate_deg, is_current=True)

    rec = SimpleNamespace(id=1, sequence=1, title="t", description=None, author=None, date=None,
                          status="approved", capture_mode="single", created_by="op", created_at=None,
                          images=[image(1, 90), image(2, None)])
    coll = SimpleNamespace(id=1, name="v", description=None, collection_type=None,
                           archival_metadata=None, created_by="op", created_at=None)

    payload = export_service._metadata_payload(coll, None, [rec], "20261024T000000Z")

    assert payload["schema_version"] == export_service.METADATA_SCHEMA_VERSION == 2
    assert [i["rotate_deg"] for i in payload["records"][0]["images"]] == [90, None], \
        "an image from before the field, or an upload, has null"


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------

def _load_migration():
    backend_root = Path(__file__).resolve().parents[2]
    [path] = (backend_root / "alembic" / "versions").glob("*_record_rotation_on_record_images.py")
    spec = importlib.util.spec_from_file_location("rotation_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(engine, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


@pytest.mark.unit
def test_the_migration_adds_a_nullable_checked_column_and_drops_it(tmp_path):
    mod = _load_migration()
    assert mod.down_revision == "c4e8f1a3b5d7"
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    md = sa.MetaData()
    sa.Table("record_images", md, sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("file_path", sa.String(512), nullable=False))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO record_images (id, file_path) VALUES (1, '/a'), (2, '/b')"))

    _run(engine, mod.upgrade)

    columns = {c["name"]: c for c in sa.inspect(engine).get_columns("record_images")}
    assert columns["rotate_deg"]["nullable"]
    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT rotate_deg FROM record_images")).scalars().all() == [None, None]
        conn.execute(sa.text("UPDATE record_images SET rotate_deg = 270 WHERE id = 1"))
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(sa.text("UPDATE record_images SET rotate_deg = 45 WHERE id = 2"))

    _run(engine, mod.downgrade)
    assert "rotate_deg" not in {c["name"] for c in sa.inspect(engine).get_columns("record_images")}
