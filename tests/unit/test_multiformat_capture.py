"""A multi-format capture through the API: the JPEG is the image, the raw file its sidecar (NEH-261).

Picamera2Backend, asked for a raw stream, writes the JPEG and a `<stem>.raw`
beside it and reports both as a (jpeg_path, raw_path) tuple. The capture
service hands its callers the JPEG alone: the endpoints record one
RecordImage per camera, for the JPEG, and the raw file is a sidecar of that
JPEG, as `_preview.jpg` is of a RAW master. Deleting the image or its record
removes the raw sidecar with it.

The backend is stood in for as test_master_orientation.py does.

Run with: python -m pytest tests/unit/test_multiformat_capture.py
"""

import io
from pathlib import Path

import piexif
import pytest
from PIL import Image

import capture.project_manager as project_manager_module
import capture.service as capture_service


STEM = "co-test-multi"
ORIENTATION_TAG = 0x0112
RAW_BYTES = b"packed sensor bytes"


def _jpeg_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (8, 4), "white").save(buf, format="JPEG")
    return buf.getvalue()


def _user(username, role):
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, username="op", role="operator"):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


class _MultiFormatBackend:
    """Writes a JPEG and a `.raw` buffer beside it and reports both, as Picamera2Backend does with raw=True."""

    def capture_image(self, output_path, camera_config, capture_output=False):
        jpeg = Path(output_path)
        raw = Path(str(jpeg).rsplit(".", 1)[0] + ".raw")
        jpeg.write_bytes(_jpeg_bytes())
        raw.write_bytes(RAW_BYTES)
        return (str(jpeg), str(raw)), {"SensorTimestamp": 1}


@pytest.fixture
def backend(monkeypatch):
    b = _MultiFormatBackend()
    monkeypatch.setattr(capture_service, "get_backend", lambda: b)
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    monkeypatch.setattr(
        project_manager_module, "default_camera_config_from_registry",
        lambda camera_index, resolution="high", registry=None: (
            {"camera_index": camera_index, "rotate_deg": 90, "raw": True}, "hw"))
    return b


@pytest.fixture
def volume(db_session, test_project):
    from app.models.collection import Collection
    v = Collection(name="Tomo 1", project_id=test_project.id, filename_stem=STEM)
    db_session.add(v)
    db_session.commit()
    return v


def _post(api, path, payload):
    resp = api.post(path, json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"], resp.json()
    return resp.json()


def _images(db_session, record_id):
    from app.models.record import RecordImage
    db_session.expire_all()
    return (db_session.query(RecordImage).filter(RecordImage.record_id == record_id)
            .order_by(RecordImage.id).all())


def _raw_of(jpeg):
    return Path(jpeg).with_suffix(".raw")


def _orientation(path):
    return piexif.load(str(path))["0th"].get(ORIENTATION_TAG)


def _assert_jpeg_image(img):
    path = Path(img.file_path)
    assert path.suffix == ".jpg", img.file_path
    assert img.filename == path.name
    assert img.format == "jpg"
    assert img.file_size == path.stat().st_size > 0
    assert (img.resolution_width, img.resolution_height) == (8, 4)
    assert _orientation(path) == 6, "the rotation is recorded on the JPEG"
    assert _raw_of(path).read_bytes() == RAW_BYTES, "the raw sidecar is beside the JPEG, unchanged"


@pytest.mark.unit
def test_a_single_multiformat_capture_records_the_jpeg(
        client, db_session, test_project, volume, backend, override_projects_root):
    out = _post(_client_as(client), "/cameras/capture",
                {"project_name": test_project.name, "camera_index": 0, "collection_id": volume.id})

    assert Path(out["file_path"]).suffix == ".jpg"
    images = _images(db_session, out["record_id"])
    assert len(images) == 1, "the raw file is a sidecar, not a second image"
    assert images[0].file_path == out["file_path"]
    _assert_jpeg_image(images[0])


@pytest.mark.unit
def test_a_dual_multiformat_capture_records_each_jpeg(
        client, db_session, test_project, volume, backend, override_projects_root):
    out = _post(_client_as(client), "/cameras/capture/dual",
                {"project_name": test_project.name, "collection_id": volume.id})

    images = _images(db_session, out["record_id"])
    assert len(images) == 2
    for img in images:
        _assert_jpeg_image(img)


@pytest.mark.unit
def test_deleting_the_image_removes_its_raw_sidecar(
        client, db_session, test_project, volume, backend, override_projects_root):
    api = _client_as(client)
    out = _post(api, "/cameras/capture",
                {"project_name": test_project.name, "camera_index": 0, "collection_id": volume.id})
    jpeg = Path(out["file_path"])

    resp = api.delete(f"/records/images/{out['image_ids'][0]}")

    assert resp.status_code == 200, resp.text
    assert "not_removed" not in resp.json()
    assert not jpeg.exists()
    assert not _raw_of(jpeg).exists()


@pytest.mark.unit
def test_deleting_the_record_removes_every_raw_sidecar(
        client, db_session, test_project, volume, backend, override_projects_root):
    api = _client_as(client)
    out = _post(api, "/cameras/capture/dual",
                {"project_name": test_project.name, "collection_id": volume.id})
    jpegs = [Path(img.file_path) for img in _images(db_session, out["record_id"])]
    assert [j.suffix for j in jpegs] == [".jpg", ".jpg"]
    assert all(_raw_of(j).exists() for j in jpegs)

    resp = api.delete(f"/records/{out['record_id']}")

    assert resp.status_code == 200, resp.text
    for jpeg in jpegs:
        assert not jpeg.exists()
        assert not _raw_of(jpeg).exists()


@pytest.mark.unit
def test_a_sibling_master_that_is_the_raw_file_keeps_it(tmp_path, monkeypatch):
    """The raw sidecar is left when a surviving row's master is that very file, and a missing one is never listed."""
    import app.api.records as records

    monkeypatch.setattr(records, "resolve_within_storage", lambda p: Path(p) if p else None)
    jpeg, raw = tmp_path / "f_c0.jpg", tmp_path / "f_c0.raw"
    jpeg.write_bytes(_jpeg_bytes())
    raw.write_bytes(RAW_BYTES)

    alone = records._image_files_to_unlink(str(jpeg), None)
    kept = records._image_files_to_unlink(str(jpeg), None, [str(raw)])

    assert raw in alone
    assert raw not in kept
    raw.unlink()
    assert raw not in records._image_files_to_unlink(str(jpeg), None)


# ---------------------------------------------------------------------------
# A move carries the raw sidecar with its JPEG (NEH-261)
# ---------------------------------------------------------------------------

@pytest.fixture
def move_dirs(override_projects_root, db_session):
    # db_session imports every model, which RecordImage's relationships need to map.
    src = override_projects_root / "proj" / "images" / "a"
    dst = override_projects_root / "proj" / "images" / "b"
    src.mkdir(parents=True)
    return src, dst


def _jpeg_with_raw(directory, stem):
    jpeg = directory / f"{stem}.jpg"
    jpeg.write_bytes(_jpeg_bytes())
    _raw_of(jpeg).write_bytes(RAW_BYTES)
    return jpeg


def _row(master):
    from app.models.record import RecordImage
    return RecordImage(filename=master.name, file_path=str(master))


@pytest.mark.unit
def test_relocate_carries_the_raw_sidecar_of_a_jpeg_master(move_dirs):
    from app.core.storage_ops import relocate_image

    src_dir, dst_dir = move_dirs
    jpeg = _jpeg_with_raw(src_dir, "f_c0")
    img = _row(jpeg)
    moved = []

    assert relocate_image(img, dst_dir, moved=moved) is True

    new = Path(img.file_path)
    assert new == (dst_dir / "f_c0.jpg").resolve()
    assert _raw_of(new).read_bytes() == RAW_BYTES
    assert not jpeg.exists() and not _raw_of(jpeg).exists()
    assert (_raw_of(jpeg), _raw_of(dst_dir / "f_c0.jpg")) in [(Path(a), Path(b)) for a, b in moved]


@pytest.mark.unit
def test_relocate_renames_the_raw_sidecar_with_its_renamed_master(move_dirs):
    from app.core.storage_ops import relocate_image

    src_dir, dst_dir = move_dirs
    dst_dir.mkdir(parents=True)
    jpeg = _jpeg_with_raw(src_dir, "f_c0")
    occupant = dst_dir / "f_c0.jpg"
    occupant.write_bytes(b"someone else's jpeg")
    img = _row(jpeg)

    assert relocate_image(img, dst_dir) is True

    new = Path(img.file_path)
    assert new.name != "f_c0.jpg" and new.name.startswith("f_c0_")
    assert _raw_of(new).read_bytes() == RAW_BYTES, "the raw keeps the moved master's stem"
    assert not (dst_dir / "f_c0.raw").exists()
    assert occupant.read_bytes() == b"someone else's jpeg"


@pytest.mark.unit
def test_relocate_renames_when_only_the_raw_name_is_taken(move_dirs):
    """A stray `.raw` in the target is not overwritten by the one that moves."""
    from app.core.storage_ops import relocate_image

    src_dir, dst_dir = move_dirs
    dst_dir.mkdir(parents=True)
    jpeg = _jpeg_with_raw(src_dir, "f_c0")
    stray = dst_dir / "f_c0.raw"
    stray.write_bytes(b"stray raw")
    img = _row(jpeg)

    relocate_image(img, dst_dir)

    new = Path(img.file_path)
    assert new.name != "f_c0.jpg"
    assert _raw_of(new).read_bytes() == RAW_BYTES
    assert stray.read_bytes() == b"stray raw"


@pytest.mark.unit
def test_relocate_reports_a_raw_sidecar_that_could_not_follow(move_dirs, monkeypatch):
    import shutil
    from app.core.storage_ops import relocate_image

    src_dir, dst_dir = move_dirs
    jpeg = _jpeg_with_raw(src_dir, "f_c0")
    img = _row(jpeg)
    real_move = shutil.move

    def move(src, dst, *a, **kw):
        if str(src).endswith(".raw"):
            raise PermissionError(13, "Permission denied", src)
        return real_move(src, dst, *a, **kw)

    monkeypatch.setattr("app.core.storage_ops.shutil.move", move)
    not_moved = []

    assert relocate_image(img, dst_dir, not_moved) is True

    assert Path(img.file_path).exists()
    assert _raw_of(jpeg).read_bytes() == RAW_BYTES
    assert not_moved == ["f_c0.raw"]


@pytest.mark.unit
def test_undo_puts_a_carried_raw_sidecar_back(move_dirs):
    from app.core.storage_ops import relocate_image, undo_relocations

    src_dir, dst_dir = move_dirs
    jpeg = _jpeg_with_raw(src_dir, "f_c0")
    moved = []
    relocate_image(_row(jpeg), dst_dir, moved=moved)

    assert undo_relocations(moved) == []

    assert jpeg.exists() and _raw_of(jpeg).read_bytes() == RAW_BYTES
    assert not (dst_dir / "f_c0.raw").exists()


# ---------------------------------------------------------------------------
# The layout migration carries it too (NEH-261)
# ---------------------------------------------------------------------------

@pytest.fixture
def layout_data_dir(override_projects_root, tmp_path, monkeypatch):
    import capture.project_manager as pm
    from app.core import config

    monkeypatch.setattr(pm, "settings", config.settings)
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(config.settings, "DATA_DIR", str(d))
    return d


def _volume_with_old_files(db_session, root, stem, names):
    """A stemmed volume whose JPEG masters, each with a `.raw` beside it, are still in the name-based layout."""
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    proj = Project(name="Proj")
    db_session.add(proj)
    db_session.commit()
    vol = Collection(name="Tomo", project_id=proj.id, filename_stem=stem)
    db_session.add(vol)
    db_session.commit()
    old = root / "proj" / "tomo" / "images" / "main"
    old.mkdir(parents=True)
    images = []
    for name in names:
        rec = Record(title="r", collection_id=vol.id, status="captured", capture_mode="single")
        db_session.add(rec)
        db_session.commit()
        jpeg = _jpeg_with_raw(old, name)
        img = RecordImage(record_id=rec.id, filename=jpeg.name, file_path=str(jpeg), format="jpg", role="single")
        db_session.add(img)
        db_session.commit()
        images.append(img)
    return vol, old, images


@pytest.mark.unit
def test_the_layout_migration_carries_the_raw_sidecar(db_session, override_projects_root, layout_data_dir):
    from app.core.volume_layout import migrate_volume_layout

    root = override_projects_root
    _vol, old, images = _volume_with_old_files(db_session, root, "xx-raw-1", ["f_c0"])

    report = migrate_volume_layout(db_session)

    db_session.refresh(images[0])
    new = Path(images[0].file_path)
    assert new == (root / "xx-raw-1" / "captures" / "f_c0.jpg").resolve()
    assert _raw_of(new).read_bytes() == RAW_BYTES
    assert not _raw_of(old / "f_c0.jpg").exists()
    assert report["left_unreferenced"] == []
    assert report["skipped"] == []


@pytest.mark.unit
def test_the_layout_migration_renames_the_raw_with_a_renamed_master(
        db_session, override_projects_root, layout_data_dir):
    from app.core.volume_layout import migrate_volume_layout

    root = override_projects_root
    _vol, _old, images = _volume_with_old_files(db_session, root, "xx-raw-2", ["f_c0"])
    captures = root / "xx-raw-2" / "captures"
    captures.mkdir(parents=True)
    stray = captures / "f_c0.raw"
    stray.write_bytes(b"stray raw")

    migrate_volume_layout(db_session)

    db_session.refresh(images[0])
    new = Path(images[0].file_path)
    assert new.name != "f_c0.jpg" and new.name.startswith("f_c0_img")
    assert _raw_of(new).read_bytes() == RAW_BYTES, "the raw keeps the moved master's stem"
    assert stray.read_bytes() == b"stray raw"


@pytest.mark.unit
def test_a_raw_sidecar_in_captures_is_the_volumes_own(db_session, override_projects_root, layout_data_dir):
    """A JPEG master's `.raw` in its volume's `captures/` is not foreign to the volume."""
    from app.core.volume_layout import _foreign_entries
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    root = override_projects_root
    proj = Project(name="Proj")
    db_session.add(proj)
    db_session.commit()
    vol = Collection(name="Tomo", project_id=proj.id, filename_stem="xx-raw-3")
    db_session.add(vol)
    db_session.commit()
    captures = root / "xx-raw-3" / "captures"
    captures.mkdir(parents=True)
    jpeg = _jpeg_with_raw(captures, "xx-raw-3-img_0001_c0")
    rec = Record(title="r", collection_id=vol.id, status="captured", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.add(RecordImage(record_id=rec.id, filename=jpeg.name, file_path=str(jpeg), format="jpg"))
    db_session.commit()

    assert _foreign_entries(db_session, root / "xx-raw-3", vol.id) == []


@pytest.mark.unit
def test_a_raw_buffer_that_could_not_follow_is_retried_beside_its_renamed_master(
        db_session, override_projects_root, layout_data_dir, monkeypatch):
    import app.core.volume_layout as vl

    root = override_projects_root
    _vol, old, images = _volume_with_old_files(db_session, root, "xx-raw-4", ["f_c0"])
    real = vl._move

    def flaky(src, dst):
        if Path(dst).suffix == ".raw":
            raise OSError(5, "Input/output error")
        real(src, dst)

    monkeypatch.setattr(vl, "_move", flaky)
    first = vl.migrate_volume_layout(db_session)
    monkeypatch.setattr(vl, "_move", real)

    assert first["sidecars_not_moved"] == [{"image_id": images[0].id, "path": str((old / "f_c0.raw").resolve())}]
    assert [s.get("kind") for s in vl._read_pending_sidecars()] == ["raw"]

    # The master is renamed while the buffer is pending; the buffer follows the new name.
    db_session.refresh(images[0])
    moved = Path(images[0].file_path)
    renamed = moved.with_name("f_c0_renamed.jpg")
    moved.rename(renamed)
    images[0].file_path = str(renamed)
    db_session.commit()

    second = vl.migrate_volume_layout(db_session)

    assert _raw_of(renamed).read_bytes() == RAW_BYTES
    assert not (old / "f_c0.raw").exists()
    assert {"from": str((old / "f_c0.raw").resolve()), "to": str(_raw_of(renamed))} in second["sidecars_moved"]
    assert vl._read_pending_sidecars() == []


# ---------------------------------------------------------------------------
# The integrity scan and the fixity index follow a moved raw sidecar (NEH-261)
# ---------------------------------------------------------------------------

def _sha(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _captured_then_moved(client, db_session, test_project, volume, rename):
    """Capture a JPEG with its raw into `volume`, then move the image into a second volume's `captures/`.

    With `rename`, a file already at the master's name in the target makes
    relocate_image rename it, and the relocation record is written as
    relocate_images_and_commit writes it.
    """
    from app.core.config import settings
    from app.core.storage_ops import relocate_image, write_relocation_records
    from app.models.collection import Collection
    from app.models.record import RecordImage

    out = _post(_client_as(client), "/cameras/capture",
                {"project_name": test_project.name, "camera_index": 0, "collection_id": volume.id})
    img = db_session.get(RecordImage, out["image_ids"][0])
    raw_sha = _sha(_raw_of(img.file_path))

    other = Collection(name="Tomo 2", project_id=test_project.id, filename_stem="co-test-multi-b")
    db_session.add(other)
    db_session.commit()
    target = settings.projects_dir.resolve() / "co-test-multi-b" / "captures"
    target.mkdir(parents=True)
    if rename:
        (target / Path(img.file_path).name).write_bytes(_jpeg_bytes())

    relocations = []
    assert relocate_image(img, target, relocations=relocations) is True
    db_session.commit()
    write_relocation_records(relocations)
    assert bool(relocations) is rename
    return img, raw_sha


@pytest.mark.unit
@pytest.mark.parametrize("rename", [False, True], ids=["moved", "renamed"])
def test_a_moved_raw_sidecar_is_no_orphan_and_keeps_its_capture_digest(
        client, db_session, test_project, volume, backend, override_projects_root, rename):
    from app.core.integrity import build_fixity_index, run_integrity_check

    img, raw_sha = _captured_then_moved(client, db_session, test_project, volume, rename)
    new_raw = _raw_of(img.file_path)
    assert new_raw.read_bytes() == RAW_BYTES

    report = run_integrity_check(db_session, verify_hashes=False)
    index = build_fixity_index(override_projects_root.resolve())

    assert str(new_raw.resolve()) not in report["orphan_files"]
    assert index.expected(img.capture_id, None, new_raw.resolve()) == raw_sha


@pytest.mark.unit
def test_a_stray_raw_with_no_master_is_still_an_orphan(
        client, db_session, test_project, volume, backend, override_projects_root):
    from app.core.integrity import run_integrity_check

    out = _post(_client_as(client), "/cameras/capture",
                {"project_name": test_project.name, "camera_index": 0, "collection_id": volume.id})
    stray = Path(out["file_path"]).with_name("no-master_c0.raw")
    stray.write_bytes(b"stray raw")

    report = run_integrity_check(db_session, verify_hashes=False)

    assert str(_raw_of(out["file_path"]).resolve()) not in report["orphan_files"]
    assert report["orphan_files"] == [str(stray.resolve())]


# ---------------------------------------------------------------------------
# A `<stem>.raw` that is a row's own master is never claimed as a sidecar (NEH-261)
# ---------------------------------------------------------------------------

def _record_with_master(db_session, path, data, collection_id=None, project_id=None):
    from app.models.record import Record, RecordImage
    rec = Record(title="r", collection_id=collection_id, project_id=project_id,
                 status="captured", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    path.write_bytes(data)
    img = RecordImage(record_id=rec.id, filename=path.name, file_path=str(path),
                      format=path.suffix.lstrip(".").lower(), role="single")
    db_session.add(img)
    db_session.commit()
    return rec, img


@pytest.mark.unit
def test_the_migration_leaves_a_raw_that_is_another_rows_master(
        db_session, override_projects_root, layout_data_dir):
    from app.core.volume_layout import migrate_volume_layout
    from app.models.collection import Collection

    root = override_projects_root
    _vol, old, images = _volume_with_old_files(db_session, root, "xx-raw-5", ["f_c0"])
    raw_master = old / "f_c0.raw"
    other = Collection(name="Sin código", project_id=_vol.project_id)  # no stem: its files stay
    db_session.add(other)
    db_session.commit()
    _rec, raw_img = _record_with_master(db_session, raw_master, b"another row's RAW master",
                                        collection_id=other.id)

    report = migrate_volume_layout(db_session)

    db_session.refresh(images[0])
    db_session.refresh(raw_img)
    assert Path(images[0].file_path).parent == (root / "xx-raw-5" / "captures").resolve()
    assert raw_img.file_path == str(raw_master)
    assert raw_master.read_bytes() == b"another row's RAW master"
    assert not (root / "xx-raw-5" / "captures" / "f_c0.raw").exists()
    assert all(s["image_id"] != raw_img.id for s in report["skipped"] if s["reason"] == "missing")


@pytest.fixture
def two_records(db_session, override_projects_root):
    d = override_projects_root / "proj" / "images" / "main"
    d.mkdir(parents=True)
    return d


@pytest.mark.unit
@pytest.mark.parametrize("deleted", ["f_c0.CR2", "f_c0.jpg"])
def test_deleting_a_record_leaves_a_raw_that_is_another_records_master(
        client, db_session, test_project, two_records, deleted):
    d = two_records
    rec, _img = _record_with_master(db_session, d / deleted, b"master", project_id=test_project.id)
    raw_master = d / "f_c0.raw"
    _record_with_master(db_session, raw_master, b"another record's RAW master", project_id=test_project.id)

    resp = _client_as(client).delete(f"/records/{rec.id}")

    assert resp.status_code == 200, resp.text
    assert not (d / deleted).exists()
    assert raw_master.read_bytes() == b"another record's RAW master"


@pytest.mark.unit
def test_deleting_an_image_leaves_a_raw_that_is_another_records_master(
        client, db_session, test_project, two_records):
    d = two_records
    _rec, img = _record_with_master(db_session, d / "f_c0.jpg", b"jpeg", project_id=test_project.id)
    raw_master = d / "f_c0.raw"
    _record_with_master(db_session, raw_master, b"another record's RAW master", project_id=test_project.id)

    resp = _client_as(client).delete(f"/records/images/{img.id}")

    assert resp.status_code == 200, resp.text
    assert raw_master.read_bytes() == b"another record's RAW master"


@pytest.mark.unit
def test_relocate_leaves_a_raw_that_is_another_records_master(db_session, move_dirs):
    from app.core.storage_ops import relocate_image

    src_dir, dst_dir = move_dirs
    _rec, img = _record_with_master(db_session, src_dir / "f_c0.jpg", _jpeg_bytes())
    raw_master = src_dir / "f_c0.raw"
    _record_with_master(db_session, raw_master, b"another record's RAW master")

    assert relocate_image(img, dst_dir) is True

    assert raw_master.read_bytes() == b"another record's RAW master"
    assert not (dst_dir / "f_c0.raw").exists()


# ---------------------------------------------------------------------------
# Ownership is decided by file identity, not by path strings (NEH-261)
# ---------------------------------------------------------------------------

def _case_insensitive(directory):
    probe = directory / "case_probe_a"
    probe.write_bytes(b"x")
    try:
        return (directory / "CASE_PROBE_A").exists()
    finally:
        probe.unlink()


@pytest.mark.unit
def test_relocate_leaves_a_raw_symlinked_to_another_records_master(db_session, move_dirs):
    """`f_c0.raw` is a symlink to `g.raw`, which another record has as its master."""
    from app.core.storage_ops import relocate_image

    src_dir, dst_dir = move_dirs
    _rec, img = _record_with_master(db_session, src_dir / "f_c0.jpg", _jpeg_bytes())
    owned = src_dir / "g.raw"
    _record_with_master(db_session, owned, b"another record's RAW master")
    (src_dir / "f_c0.raw").symlink_to(owned)

    assert relocate_image(img, dst_dir) is True

    assert owned.read_bytes() == b"another record's RAW master"
    assert not (dst_dir / "g.raw").exists() and not (dst_dir / "f_c0.raw").exists()


@pytest.mark.unit
def test_on_a_case_insensitive_volume_another_rows_upper_case_raw_is_protected(db_session, move_dirs):
    """Where `F_C0.RAW` and `f_c0.raw` are one file, a row's `F_C0.RAW` master is not the JPEG's buffer."""
    from app.core.storage_ops import relocate_image

    src_dir, dst_dir = move_dirs
    if not _case_insensitive(src_dir):
        pytest.skip("the temporary filesystem is case-sensitive; the case-sensitive test covers it")
    _rec, img = _record_with_master(db_session, src_dir / "f_c0.jpg", _jpeg_bytes())
    owned = src_dir / "F_C0.RAW"
    _record_with_master(db_session, owned, b"another record's RAW master")

    assert relocate_image(img, dst_dir) is True

    assert owned.read_bytes() == b"another record's RAW master"
    assert [p.name for p in dst_dir.iterdir()] == ["f_c0.jpg"]


@pytest.mark.unit
def test_on_a_case_sensitive_volume_a_buffer_is_carried_beside_another_rows_upper_case_raw(db_session, move_dirs):
    """Where `f_c0.raw` and `F_C0.RAW` are two files, the JPEG's buffer moves and the other row's file stays."""
    from app.core.storage_ops import relocate_image

    src_dir, dst_dir = move_dirs
    if _case_insensitive(src_dir):
        pytest.skip("the temporary filesystem is case-insensitive (APFS default on the Mac mini); "
                    "f_c0.raw and F_C0.RAW cannot be two files here")
    _rec, img = _record_with_master(db_session, src_dir / "f_c0.jpg", _jpeg_bytes())
    (src_dir / "f_c0.raw").write_bytes(RAW_BYTES)
    owned = src_dir / "F_C0.RAW"
    _record_with_master(db_session, owned, b"another record's RAW master")

    assert relocate_image(img, dst_dir) is True

    assert (dst_dir / "f_c0.raw").read_bytes() == RAW_BYTES
    assert owned.read_bytes() == b"another record's RAW master"


@pytest.mark.unit
def test_a_file_name_with_like_wildcards_matches_only_itself(db_session, override_projects_root):
    from app.core.storage_ops import rows_master_paths_named

    d = override_projects_root / "proj" / "images" / "main"
    d.mkdir(parents=True)
    names = ["f%_c0.raw", "fXYc0.raw", "fAB_c0.raw", "f%\\c0.raw", "zf%_c0.raw"]
    for n in names:
        _record_with_master(db_session, d / n, n.encode())

    found = rows_master_paths_named(db_session, ["f%_c0.raw"])

    assert [p.name for p in found] == ["f%_c0.raw"]
    assert [p.name for p in rows_master_paths_named(db_session, ["F%_C0.RAW"])] == ["f%_c0.raw"]
