"""Every capture event raises its volume's capture_counter by one.

Exchange contract 1.1 makes `<nnnn>` in a volume's file names count every
capture the volume has ever had, superseded ones included, and never reuse a
number. `collections.capture_counter` is that count. It is raised in the
same transaction that writes the images, so a capture whose commit fails
leaves it where it was:

- a single capture: one event;
- a dual capture: one event, two images;
- a retake through either endpoint: one event;
- an upload through POST /records/{id}/images: one event.

The stem lock and the delete refusal read the counter, so a stem stays fixed
and a volume stays undeletable once it has captured, even after its records
are deleted or moved out.

The hardware is stood in for as test_capture_birth_and_retake.py does.
"""

import capture.service as capture_service
import capture.project_manager as project_manager_module

import pytest


def _user(username, role):
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, username="op", role="admin"):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


def _install_capture_fakes(monkeypatch, tmp_path):
    def fake_config(camera_index, resolution="high", registry=None):
        return {"camera_index": camera_index, "rotate_deg": 0}, "hw"

    def fake_single(project_name, camera_config, check_camera=False,
                    include_resolution=False, collection_name=None, stem=None, filename_index=None, root=None):
        return str(tmp_path / "cap.jpg"), "cap-1", None

    def fake_dual(project_name, cam1_config, cam2_config, check_camera=False,
                  include_resolution=False, stagger_ms=0, collection_name=None, stem=None, filename_index=None, root=None, roles=None):
        return (str(tmp_path / "cap_c0.jpg"), str(tmp_path / "cap_c1.jpg"),
                "cap-2", "pair-1")

    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    monkeypatch.setattr(capture_service, "single_capture_image", fake_single)
    monkeypatch.setattr(capture_service, "dual_capture_image", fake_dual)
    monkeypatch.setattr(project_manager_module,
                        "default_camera_config_from_registry", fake_config)


def _volume(db_session, project, name="V", **kw):
    from app.models.collection import Collection
    # A volume needs a stem to capture; one per name keeps them unique.
    kw.setdefault("filename_stem", f"co-test-{name.lower()}")
    v = Collection(name=name, project_id=project.id, **kw)
    db_session.add(v)
    db_session.commit()
    return v


def _counter(db_session, volume_id):
    from app.models.collection import Collection
    db_session.expire_all()
    return db_session.get(Collection, volume_id).capture_counter


def _single(api, project, volume, **extra):
    payload = {"project_name": project.name, "camera_index": 0, "collection_id": volume.id}
    payload.update(extra)
    return api.post("/cameras/capture", json=payload)


def _dual(api, project, volume, **extra):
    payload = {"project_name": project.name, "collection_id": volume.id}
    payload.update(extra)
    return api.post("/cameras/capture/dual", json=payload)


@pytest.fixture
def upload_dir(monkeypatch, tmp_path):
    """Uploads and their thumbnails go under tmp_path, not the shared data dir.

    Patched on the settings object records.py holds: the projects-root
    fixture reloads app.core.config, so app.core.config.settings may be a
    different instance by the time a test runs.
    """
    import app.api.records as records_api
    monkeypatch.setattr(records_api.settings, "DTK_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def _upload(api, record_id):
    return api.post(f"/records/{record_id}/images",
                    files={"file": ("page.jpg", b"not really a jpeg", "image/jpeg")})


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_next_capture_number_returns_the_raised_value(db_session, test_project):
    from app.core.capture_counter import next_capture_number

    v = _volume(db_session, test_project, capture_counter=4)

    assert next_capture_number(db_session, v.id) == 5
    assert v.capture_counter == 5, "the loaded volume is kept in step"
    assert next_capture_number(db_session, v.id) == 6
    db_session.commit()
    assert _counter(db_session, v.id) == 6


@pytest.mark.unit
def test_next_capture_number_without_a_volume_is_none(db_session):
    from app.core.capture_counter import next_capture_number

    assert next_capture_number(db_session, None) is None


@pytest.mark.unit
def test_a_rolled_back_raise_leaves_the_counter(db_session, test_project):
    from app.core.capture_counter import next_capture_number

    v = _volume(db_session, test_project)
    next_capture_number(db_session, v.id)
    db_session.rollback()

    assert _counter(db_session, v.id) == 0


@pytest.mark.unit
def test_the_raise_is_one_atomic_update_returning_the_value(db_session):
    """One statement, so two concurrent captures cannot lose an increment."""
    import inspect
    from app.core import capture_counter

    src = inspect.getsource(capture_counter.next_capture_number)
    assert "capture_counter + 1" in src
    assert ".returning(" in src


# ---------------------------------------------------------------------------
# Each path raises it by one
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_single_capture_raises_it_by_one(client, db_session, test_project, monkeypatch, tmp_path):
    _install_capture_fakes(monkeypatch, tmp_path)
    api = _client_as(client)
    v = _volume(db_session, test_project)

    resp = _single(api, test_project, v)

    assert resp.status_code == 200 and resp.json()["success"], resp.text
    assert _counter(db_session, v.id) == 1


@pytest.mark.unit
def test_a_dual_capture_raises_it_by_one_for_two_images(client, db_session, test_project,
                                                        monkeypatch, tmp_path):
    _install_capture_fakes(monkeypatch, tmp_path)
    api = _client_as(client)
    v = _volume(db_session, test_project)

    resp = _dual(api, test_project, v)

    assert resp.status_code == 200 and resp.json()["success"], resp.text
    assert len(resp.json()["image_ids"]) == 2
    assert _counter(db_session, v.id) == 1


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["single", "dual"])
def test_a_retake_raises_it_by_one(client, db_session, test_project, monkeypatch, tmp_path, mode):
    _install_capture_fakes(monkeypatch, tmp_path)
    api = _client_as(client)
    v = _volume(db_session, test_project)
    take = _single if mode == "single" else _dual

    first = take(api, test_project, v)
    assert first.json()["success"], first.text
    retake = take(api, test_project, v, record_id=first.json()["record_id"])

    assert retake.status_code == 200 and retake.json()["success"], retake.text
    assert _counter(db_session, v.id) == 2


@pytest.mark.unit
def test_an_upload_raises_it_by_one(client, db_session, test_project, upload_dir):
    from app.models.record import Record

    api = _client_as(client)
    v = _volume(db_session, test_project)
    rec = Record(title="r", status="captured", capture_mode="single", collection_id=v.id)
    db_session.add(rec)
    db_session.commit()

    resp = _upload(api, rec.id)

    assert resp.status_code == 200, resp.text
    assert _counter(db_session, v.id) == 1


@pytest.mark.unit
def test_a_capture_outside_any_volume_is_refused_and_raises_nothing(client, db_session, test_project,
                                                                    monkeypatch, tmp_path):
    _install_capture_fakes(monkeypatch, tmp_path)
    api = _client_as(client)
    v = _volume(db_session, test_project)

    resp = api.post("/cameras/capture", json={"project_name": test_project.name, "camera_index": 0})

    assert resp.status_code == 409, resp.text
    assert _counter(db_session, v.id) == 0


# ---------------------------------------------------------------------------
# A failed commit raises nothing
# ---------------------------------------------------------------------------

def _failing_commit(monkeypatch, db_session):
    def boom():
        raise RuntimeError("commit failed")
    monkeypatch.setattr(db_session, "commit", boom)


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["single", "dual"])
def test_a_capture_whose_commit_fails_raises_nothing(client, db_session, test_project,
                                                     monkeypatch, tmp_path, mode):
    _install_capture_fakes(monkeypatch, tmp_path)
    api = _client_as(client)
    v = _volume(db_session, test_project)
    vid = v.id

    _failing_commit(monkeypatch, db_session)
    resp = (_single if mode == "single" else _dual)(api, test_project, v)
    monkeypatch.undo()

    assert resp.json()["success"] is False, resp.text
    assert _counter(db_session, vid) == 0


@pytest.mark.unit
def test_an_upload_whose_commit_fails_raises_nothing(client, db_session, test_project, monkeypatch,
                                                    upload_dir):
    from app.models.record import Record

    api = _client_as(client)
    v = _volume(db_session, test_project)
    rec = Record(title="r", status="captured", capture_mode="single", collection_id=v.id)
    db_session.add(rec)
    db_session.commit()
    vid, rid = v.id, rec.id

    real_commit = db_session.commit
    _failing_commit(monkeypatch, db_session)
    resp = _upload(api, rid)
    monkeypatch.setattr(db_session, "commit", real_commit)

    assert resp.status_code == 500, resp.text
    assert _counter(db_session, vid) == 0


# ---------------------------------------------------------------------------
# The verifier's flows, now refused
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_captured_stem_stays_fixed_after_the_record_is_deleted(client, db_session, test_project,
                                                                 monkeypatch, tmp_path):
    """Verifier 1a: a real capture, then the record deleted, then a stem change."""
    from app.models.collection import Collection
    from app.models.record import ImmutableFieldError, Record

    _install_capture_fakes(monkeypatch, tmp_path)
    api = _client_as(client)
    v = _volume(db_session, test_project, filename_stem="co-a-001")
    vid = v.id
    assert _single(api, test_project, v).json()["success"]

    for rec in db_session.query(Record).filter(Record.collection_id == vid).all():
        db_session.delete(rec)
    db_session.commit()

    v = db_session.get(Collection, vid)
    assert v.capture_counter == 1 and not v.has_images()
    with pytest.raises(ImmutableFieldError):
        v.filename_stem = "co-a-002"


@pytest.mark.unit
def test_move_records_then_delete_is_refused_after_a_real_capture(client, db_session, test_project,
                                                                  monkeypatch, tmp_path):
    """Verifier r10: capture into A, move its records to B, delete A."""
    from app.models.collection import Collection

    _install_capture_fakes(monkeypatch, tmp_path)
    api = _client_as(client)
    a = _volume(db_session, test_project, "A", filename_stem="co-flow-a")
    b = _volume(db_session, test_project, "B")
    aid, bid = a.id, b.id
    assert _single(api, test_project, a).json()["success"]

    moved = api.post(f"/collections/{aid}/move-records?target_collection_id={bid}")
    assert moved.status_code == 200, moved.text
    deleted = api.delete(f"/collections/{aid}")

    assert deleted.status_code == 409, deleted.text
    db_session.expire_all()
    assert db_session.get(Collection, aid) is not None
