"""Writers refuse when the storage override names a root that is not usable.

A readable override file whose path does not exist, or whose path sits on the
root filesystem, is kept as the projects root (falling back to the internal
card would split projects across two disks). Nothing may then create the
projects tree there: with no drive mounted, `/var/lib/dtk/mounts/<x>/` is an
ordinary directory on the SD card, and `mkdir(parents=True)` under it would put
the project on the card while the operator believes it is on the drive (bench
D3, section 5c).

`writable_projects_dir()` is the check for writers that build a path into the
tree; they all do it through `capture.project_manager._projects_root()`:
captures, project initialisation, relocations (collection rename or re-parent,
move-records, move-collections), project rename, and the path computation of
collection and project deletes. Writers that work beside an existing file use
`refuse_writes_under_unusable_root()`: thumbnail regeneration.
Both raise `StorageUnavailableError`, which the app answers with 503. A
renumber writes no file, so it is not refused.
POST /system/storage/activate refuses a target the same test would refuse.
Record and image deletes are not checked: they create nothing. Reading the
camera registry is never refused; under such a root it reads as no file, and
only its writes refuse.

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
def test_camera_registry_reads_a_missing_root_as_no_file_and_creates_nothing(override, tmp_path):
    from capture.camera_registry import CameraRegistry
    override(tmp_path / "mounts" / "usb" / "dtk-projects")
    reg = CameraRegistry()
    assert reg.cameras["cameras"] == {}
    assert not (tmp_path / "mounts").exists()


@pytest.mark.unit
def test_camera_registry_ignores_a_file_under_a_root_on_the_card_and_refuses_writes(
        override, on_the_card, tmp_path):
    import json
    from capture.camera_registry import CameraRegistry
    from app.core.storage_override import StorageUnavailableError
    root = tmp_path / "mounts" / "usb" / "dtk-projects"
    root.mkdir(parents=True)
    (root / "cameras.json").write_text(json.dumps(
        {"version": "1.0", "cameras": {"hw1": {"current_index": 0}}}))
    override(root)
    reg = CameraRegistry()
    assert reg.cameras["cameras"] == {}
    with pytest.raises(StorageUnavailableError):
        reg.update_orientation("hw1", 90)
    assert json.loads((root / "cameras.json").read_text())["cameras"] == {"hw1": {"current_index": 0}}


@pytest.mark.unit
def test_test_capture_is_not_blocked_by_reading_the_registry(override, tmp_path, monkeypatch):
    import capture.service as svc
    override(tmp_path / "mounts" / "usb" / "dtk-projects")
    monkeypatch.setattr(svc, "is_camera_connected", lambda i: True)

    class FakeBackend:
        def capture_image(self, *a, **k):
            raise RuntimeError("reached the backend")

    monkeypatch.setattr(svc, "get_backend", lambda: FakeBackend())
    with pytest.raises(RuntimeError, match="reached the backend"):
        svc.test_capture_bytes(0)
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


def _jpeg(path):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), (200, 10, 10)).save(path, "JPEG")


@pytest.fixture
def card_root(override, on_the_card, tmp_path):
    """An override root that exists but is on the root filesystem, holding a file
    from before (an empty mount point that something earlier wrote under)."""
    root = tmp_path / "mounts" / "usb" / "dtk-projects"
    root.mkdir(parents=True)
    override(root)
    return root


def _record_under(db_session, root):
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage
    p = Project(name="P")
    db_session.add(p)
    db_session.commit()
    c = Collection(name="C", project_id=p.id)
    db_session.add(c)
    db_session.commit()
    f = root / "p" / "c" / "images" / "main" / "a.jpg"
    _jpeg(f)
    r = Record(title="r", collection_id=c.id, status="captured", capture_mode="single")
    db_session.add(r)
    db_session.commit()
    img = RecordImage(record_id=r.id, filename=f.name, file_path=str(f),
                      format="jpg", role="single", is_current=True)
    db_session.add(img)
    db_session.commit()
    return c, img


def _entries(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.unit
def test_thumbnail_is_not_generated_under_a_refused_root(client, db_session, pm, card_root):
    c, img = _record_under(db_session, card_root)
    before = _entries(card_root)
    resp = _admin_client(client).get(f"/records/images/{img.id}/thumbnail")
    assert resp.status_code == 503, resp.text
    assert _entries(card_root) == before


@pytest.mark.unit
def test_thumbnail_of_a_file_outside_the_override_root_is_still_generated(
        client, db_session, pm, override, tmp_path, monkeypatch):
    from app.core import config
    c, img = _record_under(db_session, tmp_path / "elsewhere")
    monkeypatch.setattr(config.settings, "DTK_DATA_DIR", str(tmp_path / "elsewhere"))
    override(tmp_path / "mounts" / "usb" / "dtk-projects")
    resp = _admin_client(client).get(f"/records/images/{img.id}/thumbnail")
    assert resp.status_code == 200, resp.text


@pytest.mark.unit
def test_renumber_under_a_refused_root_goes_ahead_and_writes_nothing_there(client, db_session, pm, card_root):
    c, img = _record_under(db_session, card_root)
    before = _entries(card_root)
    resp = _admin_client(client).post(f"/collections/{c.id}/images/renumber")
    assert resp.status_code == 200, resp.text
    assert _entries(card_root) == before


@pytest.mark.unit
def test_activate_refuses_a_target_on_the_root_filesystem(client, db_session, on_the_card, tmp_path, monkeypatch):
    import app.core.storage_override as so
    stored = []
    monkeypatch.setattr(so, "set_storage_override", stored.append)
    target = tmp_path / "mounts" / "empty-mountpoint"
    target.mkdir(parents=True)
    resp = _admin_client(client).post("/system/storage/activate", json={"path": str(target)})
    assert resp.status_code == 400, resp.text
    assert "not on a mounted drive" in resp.json()["detail"]
    assert not (target / "dtk-projects").exists()
    assert stored == []


@pytest.mark.unit
def test_activate_accepts_a_target_on_another_filesystem(client, db_session, on_a_drive, tmp_path, monkeypatch):
    import app.core.storage_override as so
    stored = []
    monkeypatch.setattr(so, "set_storage_override", stored.append)
    target = tmp_path / "drive"
    target.mkdir()
    resp = _admin_client(client).post("/system/storage/activate", json={"path": str(target)})
    assert resp.status_code == 200, resp.text
    assert stored == [str(target / "dtk-projects")]


@pytest.fixture
def linked_card_root(override, on_the_card, tmp_path):
    """The override names a path through a symlink to a directory on the root filesystem."""
    real = tmp_path / "real-mount" / "dtk-projects"
    real.mkdir(parents=True)
    (tmp_path / "media-usb").symlink_to(tmp_path / "real-mount")
    root = tmp_path / "media-usb" / "dtk-projects"
    override(root)
    return root, real


@pytest.mark.unit
def test_thumbnail_is_refused_under_a_symlinked_refused_root(client, db_session, pm, linked_card_root):
    root, real = linked_card_root
    c, img = _record_under(db_session, root)
    before = _entries(real)
    resp = _admin_client(client).get(f"/records/images/{img.id}/thumbnail")
    assert resp.status_code == 503, resp.text
    assert _entries(real) == before


def _bare_gphoto2_backend():
    import logging
    import threading
    from capture.backends.gphoto2_backend import GPhoto2Backend
    b = object.__new__(GPhoto2Backend)
    b._pins = {}
    b._seeded_serials = set()
    b.logger = logging.getLogger("test-gphoto2")
    b._save_lock = threading.Lock()
    b._map_lock = threading.Lock()
    return b


@pytest.mark.unit
def test_bindings_saved_after_the_drive_returns_keep_the_pins_on_disk(
        override, on_the_card, tmp_path, monkeypatch):
    """A backend built while the root was refused never read the bindings
    file. Its first save once the drive is back merges: its own pins win, and
    the pins on disk for other indices and other bodies stay."""
    import json
    root = tmp_path / "dtk-projects"
    root.mkdir()
    f = root / "camera-bindings.json"
    f.write_text(json.dumps({"version": 1, "pins": {"0": "SERIAL-L", "1": "SERIAL-R", "2": "SERIAL-X"}}))
    override(root)
    b = _bare_gphoto2_backend()
    b._load_pins()
    assert b._pins == {}

    monkeypatch.setattr("app.core.storage_override._internal_device", lambda: -1)  # drive back
    # Index 0 is now a new body; index 3 holds a body that was at index 2.
    b._pins = {0: "SERIAL-NEW", 3: "SERIAL-X"}
    b._save_pins()
    assert json.loads(f.read_text())["pins"] == {"0": "SERIAL-NEW", "1": "SERIAL-R", "3": "SERIAL-X"}
    assert b._pins == {0: "SERIAL-NEW", 3: "SERIAL-X"}


@pytest.mark.unit
def test_bindings_saved_by_a_backend_that_read_the_file_are_not_merged(override, on_a_drive, tmp_path):
    """Behaviour is unchanged for a backend that seeded from the file: its pins
    are written as they stand, so a pin it dropped stays dropped."""
    import json
    root = tmp_path / "dtk-projects"
    root.mkdir()
    f = root / "camera-bindings.json"
    f.write_text(json.dumps({"version": 1, "pins": {"0": "SERIAL-L", "1": "SERIAL-R"}}))
    override(root)
    b = _bare_gphoto2_backend()
    b._load_pins()
    assert b._pins == {0: "SERIAL-L", 1: "SERIAL-R"}
    b._pins = {0: "SERIAL-L"}
    b._save_pins()
    assert json.loads(f.read_text())["pins"] == {"0": "SERIAL-L"}
