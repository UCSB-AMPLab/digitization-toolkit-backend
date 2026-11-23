"""NEH-388: a retake that names the images it replaces.

POST /cameras/capture and /cameras/capture/dual take `expected_image_ids`
with a `record_id`: the ids of the images the retake replaces. Once the
record's lock is held, and before any camera is called, they are compared,
as a set, with the record's current images; on a difference the retake is
refused with 409 `record_changed`, and nothing is captured or superseded.
Without the field a retake is as before; with it and no `record_id`, 422.

The hardware is stood in for as test_capture_birth_and_retake.py does.
"""

import capture.service as capture_service
import capture.project_manager as project_manager_module

import pytest


def _user(username, role):
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, username, role):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


def _install_capture_fakes(monkeypatch, tmp_path):
    """The camera fakes; returns the list of the camera calls made."""
    calls = []

    def fake_config(camera_index, resolution="high", registry=None):
        return {"camera_index": camera_index, "rotate_deg": 0}, "hw"

    def fake_single(project_name, camera_config, check_camera=False,
                    include_resolution=False, collection_name=None, stem=None, filename_index=None, root=None):
        calls.append("single")
        return str(tmp_path / "cap.jpg"), "cap-1", None

    def fake_dual(project_name, cam1_config, cam2_config, check_camera=False,
                  include_resolution=False, stagger_ms=0, collection_name=None, stem=None, filename_index=None, root=None, roles=None):
        calls.append("dual")
        return (str(tmp_path / "cap_c0.jpg"), str(tmp_path / "cap_c1.jpg"),
                "cap-2", "pair-1")

    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    monkeypatch.setattr(capture_service, "single_capture_image", fake_single)
    monkeypatch.setattr(capture_service, "dual_capture_image", fake_dual)
    monkeypatch.setattr(project_manager_module,
                        "default_camera_config_from_registry", fake_config)
    return calls


@pytest.fixture
def volume(db_session, test_project):
    from app.models.collection import Collection
    v = Collection(name="V", project_id=test_project.id, filename_stem="co-test-v")
    db_session.add(v)
    db_session.commit()
    return v


def _record(db_session, volume, capture_mode):
    """A captured record with its current images: one in single mode, a pair in dual."""
    from app.models.record import Record, RecordImage
    rec = Record(title="folio", status="captured", capture_mode=capture_mode,
                 collection_id=volume.id)
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)
    roles = ["single"] if capture_mode == "single" else ["left", "right"]
    images = []
    for role in roles:
        img = RecordImage(record_id=rec.id, filename=f"old-{role}.jpg", file_path=f"/old-{role}.jpg",
                          format="jpg", role=role, is_current=True)
        db_session.add(img)
        images.append(img)
    db_session.commit()
    for img in images:
        db_session.refresh(img)
    return rec, [img.id for img in images]


def _retake(api, project_name, mode, **extra):
    path = "/cameras/capture" if mode == "single" else "/cameras/capture/dual"
    payload = {"project_name": project_name}
    if mode == "single":
        payload["camera_index"] = 0
    payload.update(extra)
    return api.post(path, json=payload)


def _current_ids(db_session, rec_id):
    from app.models.record import RecordImage
    db_session.expire_all()
    return sorted(img.id for img in db_session.query(RecordImage)
                  .filter(RecordImage.record_id == rec_id, RecordImage.is_current.is_(True)))


MODES = ["single", "dual"]


@pytest.mark.unit
@pytest.mark.parametrize("mode", MODES)
def test_the_current_images_named_retakes(client, db_session, test_project, volume, monkeypatch, tmp_path, mode):
    calls = _install_capture_fakes(monkeypatch, tmp_path)
    rec, ids = _record(db_session, volume, mode)
    api = _client_as(client, "op", "operator")

    resp = _retake(api, test_project.name, mode, collection_id=volume.id, record_id=rec.id,
                   expected_image_ids=list(reversed(ids)))

    assert resp.status_code == 200, resp.text
    assert calls == [mode]
    assert set(_current_ids(db_session, rec.id)).isdisjoint(ids)


@pytest.mark.unit
@pytest.mark.parametrize("mode", MODES)
def test_other_images_named_is_a_409_and_nothing_is_captured(client, db_session, test_project, volume,
                                                             monkeypatch, tmp_path, mode):
    calls = _install_capture_fakes(monkeypatch, tmp_path)
    rec, ids = _record(db_session, volume, mode)
    api = _client_as(client, "op", "operator")

    for expected in ([ids[0] + 1000], ids[:-1] if len(ids) > 1 else [], ids + [ids[0] + 1000]):
        resp = _retake(api, test_project.name, mode, collection_id=volume.id, record_id=rec.id,
                       expected_image_ids=expected)
        assert resp.status_code == 409, (expected, resp.text)
        assert resp.json()["detail"]["code"] == "record_changed"

    assert calls == []
    assert _current_ids(db_session, rec.id) == sorted(ids)


@pytest.mark.unit
@pytest.mark.parametrize("mode", MODES)
def test_a_retake_committed_before_the_lock_is_a_409(client, db_session, test_project, volume,
                                                     monkeypatch, tmp_path, mode):
    """Another screen's retake commits while this request waits for the record's lock."""
    import app.api.records as records_module
    from app.models.record import RecordImage

    calls = _install_capture_fakes(monkeypatch, tmp_path)
    rec, ids = _record(db_session, volume, mode)
    api = _client_as(client, "op", "operator")
    real_locked_record = records_module._locked_record

    def after_another_retake(db, rec_id, with_images=True):
        for img in db.query(RecordImage).filter(RecordImage.record_id == rec_id):
            img.is_current = False
        db.add(RecordImage(record_id=rec_id, filename="new.jpg", file_path="/new.jpg",
                           format="jpg", role="single" if mode == "single" else "left", is_current=True))
        db.commit()
        return real_locked_record(db, rec_id, with_images)

    monkeypatch.setattr(records_module, "_locked_record", after_another_retake)

    resp = _retake(api, test_project.name, mode, collection_id=volume.id, record_id=rec.id,
                   expected_image_ids=ids)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "record_changed"
    assert calls == []


@pytest.mark.unit
@pytest.mark.parametrize("mode", MODES)
def test_without_the_field_a_retake_is_as_before(client, db_session, test_project, volume,
                                                 monkeypatch, tmp_path, mode):
    calls = _install_capture_fakes(monkeypatch, tmp_path)
    rec, ids = _record(db_session, volume, mode)
    api = _client_as(client, "op", "operator")

    resp = _retake(api, test_project.name, mode, collection_id=volume.id, record_id=rec.id)

    assert resp.status_code == 200, resp.text
    assert calls == [mode]


@pytest.mark.unit
@pytest.mark.parametrize("mode", MODES)
def test_the_field_without_a_record_is_a_422(client, db_session, test_project, volume,
                                             monkeypatch, tmp_path, mode):
    calls = _install_capture_fakes(monkeypatch, tmp_path)
    api = _client_as(client, "op", "operator")

    resp = _retake(api, test_project.name, mode, collection_id=volume.id, expected_image_ids=[1])

    assert resp.status_code == 422, resp.text
    assert calls == []


@pytest.mark.unit
@pytest.mark.parametrize("mode", MODES)
def test_the_field_is_bounded(client, db_session, test_project, volume, monkeypatch, tmp_path, mode):
    calls = _install_capture_fakes(monkeypatch, tmp_path)
    rec, ids = _record(db_session, volume, mode)
    api = _client_as(client, "op", "operator")

    resp = _retake(api, test_project.name, mode, collection_id=volume.id, record_id=rec.id,
                   expected_image_ids=list(range(1, 66)))

    assert resp.status_code == 422, resp.text
    assert calls == []
