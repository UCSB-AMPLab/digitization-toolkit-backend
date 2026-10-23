"""Writers refuse when the storage override names a root that is not usable.

A readable override file whose path does not exist, or whose path sits on the
root filesystem, is kept as the projects root (falling back to the internal
card would split projects across two disks). Nothing may then create the
projects tree there: with no drive mounted, `/var/lib/dtk/mounts/<x>/` is an
ordinary directory on the SD card, and `mkdir(parents=True)` under it would put
the project on the card while the operator believes it is on the drive (bench
D3, section 5c).

`writable_projects_dir()` is the check. Every path into the projects tree is
built through `capture.project_manager._projects_root()`, which calls it, so
captures, project initialisation, relocations, renames and deletes all refuse
with `StorageUnavailableError`, which the app answers with 503.

The root filesystem's device number comes from `_internal_device()`, patched
here because a test's tmp_path may or may not share a device with `/`.

Run with: python -m pytest tests/unit/test_storage_root_unusable.py
"""

import os

import pytest


@pytest.fixture
def override(monkeypatch):
    """Set the override path the way a readable override file does."""
    def set_to(path):
        monkeypatch.setattr("app.core.storage_override.get_storage_override",
                            lambda: str(path))
    return set_to


@pytest.fixture
def on_a_drive(monkeypatch):
    """Treat every directory as being on a filesystem other than the root one."""
    monkeypatch.setattr("app.core.storage_override._internal_device", lambda: -1)


@pytest.fixture
def on_the_card(monkeypatch, tmp_path):
    """Treat tmp_path's filesystem as the root filesystem."""
    dev = os.stat(tmp_path).st_dev
    monkeypatch.setattr("app.core.storage_override._internal_device", lambda: dev)


@pytest.fixture
def pm(override_projects_root, monkeypatch):
    import capture.project_manager as pm
    from app.core import config
    monkeypatch.setattr(pm, "settings", config.settings)
    return pm


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_no_override_uses_the_default_root(override_projects_root):
    from app.core.storage_override import writable_projects_dir
    assert writable_projects_dir() == override_projects_root


@pytest.mark.unit
def test_missing_override_root_is_refused_and_not_created(override, tmp_path):
    from app.core.storage_override import StorageUnavailableError, writable_projects_dir
    root = tmp_path / "mounts" / "usb" / "dtk-projects"
    override(root)
    with pytest.raises(StorageUnavailableError) as exc:
        writable_projects_dir()
    assert "not available" in str(exc.value)
    assert not (tmp_path / "mounts").exists()


@pytest.mark.unit
def test_override_root_on_the_root_filesystem_is_refused(override, on_the_card, tmp_path):
    from app.core.storage_override import StorageUnavailableError, writable_projects_dir
    root = tmp_path / "mounts" / "usb" / "dtk-projects"
    root.mkdir(parents=True)
    override(root)
    with pytest.raises(StorageUnavailableError) as exc:
        writable_projects_dir()
    assert "not on a mounted drive" in str(exc.value)


@pytest.mark.unit
def test_override_root_that_is_a_file_is_refused(override, on_a_drive, tmp_path):
    from app.core.storage_override import StorageUnavailableError, writable_projects_dir
    root = tmp_path / "dtk-projects"
    root.write_text("not a directory")
    override(root)
    with pytest.raises(StorageUnavailableError):
        writable_projects_dir()


@pytest.mark.unit
def test_override_root_on_another_filesystem_is_used(override, on_a_drive, tmp_path):
    from app.core.storage_override import writable_projects_dir
    root = tmp_path / "dtk-projects"
    root.mkdir()
    override(root)
    assert writable_projects_dir() == root


@pytest.mark.unit
def test_device_comparison_reads_the_root_filesystem(tmp_path):
    """Without patching, a directory's device is compared with that of `/`."""
    from app.core.storage_override import _same_device_as_internal
    assert _same_device_as_internal(os.stat("/")) is True
    assert _same_device_as_internal(os.stat(tmp_path)) == (
        os.stat(tmp_path).st_dev == os.stat("/").st_dev)


# ---------------------------------------------------------------------------
# The writers
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_path_helpers_refuse_and_create_nothing(pm, override, tmp_path):
    from app.core.storage_override import StorageUnavailableError
    override(tmp_path / "mounts" / "usb" / "dtk-projects")
    for call in (lambda: pm.image_output_dir("P", "C"),
                 lambda: pm.project_capture_root("P"),
                 lambda: pm.collection_capture_root("P", "C")):
        with pytest.raises(StorageUnavailableError):
            call()
    assert not (tmp_path / "mounts").exists()


@pytest.mark.unit
def test_capture_refuses_before_writing(pm, override, tmp_path, mock_camera_config, monkeypatch):
    import capture.service as service
    from app.core.storage_override import StorageUnavailableError
    called = []
    monkeypatch.setattr(service, "get_backend", lambda: called.append(1))
    override(tmp_path / "mounts" / "usb" / "dtk-projects")
    with pytest.raises(StorageUnavailableError):
        service.capture_image("P", mock_camera_config, check_camera=False, collection_name="C")
    assert called == []
    assert not (tmp_path / "mounts").exists()


@pytest.mark.unit
def test_camera_registry_refuses_and_creates_nothing(override, tmp_path):
    from capture.camera_registry import CameraRegistry
    from app.core.storage_override import StorageUnavailableError
    override(tmp_path / "mounts" / "usb" / "dtk-projects")
    with pytest.raises(StorageUnavailableError):
        CameraRegistry()
    assert not (tmp_path / "mounts").exists()


@pytest.mark.unit
def test_camera_registry_with_an_explicit_path_is_unaffected(override, tmp_path):
    from capture.camera_registry import CameraRegistry
    override(tmp_path / "mounts" / "usb" / "dtk-projects")
    reg = CameraRegistry(registry_path=tmp_path / "reg" / "cameras.json")
    assert reg.registry_path.parent.is_dir()


@pytest.mark.unit
def test_dslr_bindings_have_no_path_when_the_root_is_unusable(override, tmp_path):
    from capture.backends.gphoto2_backend import GPhoto2Backend
    override(tmp_path / "mounts" / "usb" / "dtk-projects")
    backend = GPhoto2Backend.__new__(GPhoto2Backend)
    assert backend._bindings_path() is None


def _admin_client(client):
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User
    app.dependency_overrides[get_current_user] = lambda: User(
        username="adm", email="a@example.com", hashed_password="x", role="admin", is_active=True)
    return client


@pytest.mark.unit
def test_collection_rename_answers_503_and_creates_nothing(client, db_session, pm, override, tmp_path):
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    proj = Project(name="P")
    db_session.add(proj)
    db_session.commit()
    col = Collection(name="C", project_id=proj.id)
    db_session.add(col)
    db_session.commit()
    upload = tmp_path / "upload.jpg"
    upload.write_bytes(b"x")
    rec = Record(title="r", collection_id=col.id, status="captured", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.add(RecordImage(record_id=rec.id, filename="upload.jpg", file_path=str(upload),
                               format="jpg", role="single", is_current=True))
    db_session.commit()

    override(tmp_path / "mounts" / "usb" / "dtk-projects")
    resp = _admin_client(client).patch(f"/collections/{col.id}", json={"name": "D"})
    assert resp.status_code == 503, resp.text
    assert "not available" in resp.json()["detail"]
    assert upload.exists()
    assert not (tmp_path / "mounts").exists()
    db_session.expire_all()
    assert db_session.get(Collection, col.id).name == "C"


@pytest.mark.unit
def test_storage_info_reports_unavailable_without_creating_the_root(client, db_session, override, tmp_path):
    override(tmp_path / "mounts" / "usb" / "dtk-projects")
    body = _admin_client(client).get("/system/storage").json()
    assert body["available"] is False
    assert body["is_override"] is True
    assert body["unavailable_reason"]
    assert not (tmp_path / "mounts").exists()


@pytest.mark.unit
def test_storage_info_reports_a_root_on_the_card_as_unavailable(client, db_session, override, on_the_card, tmp_path):
    root = tmp_path / "mounts" / "usb" / "dtk-projects"
    root.mkdir(parents=True)
    override(root)
    body = _admin_client(client).get("/system/storage").json()
    assert body["available"] is False
    assert "not on a mounted drive" in body["unavailable_reason"]


@pytest.mark.unit
def test_storage_info_for_a_usable_override_is_available(client, db_session, override, on_a_drive, tmp_path):
    root = tmp_path / "dtk-projects"
    root.mkdir()
    override(root)
    body = _admin_client(client).get("/system/storage").json()
    assert body["available"] is True
    assert body["unavailable_reason"] is None
