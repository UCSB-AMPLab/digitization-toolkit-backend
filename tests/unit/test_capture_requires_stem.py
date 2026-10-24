"""No capture into a volume without a filename_stem.

Exchange contract 1.1 §3 names every captured image after its volume's stem,
and Captua never makes a stem up. So the single capture, the dual capture, a
retake through either, and an upload through POST /records/{id}/images are
refused with a 409 when the volume has no stem, before any camera is touched
and before any file is written. With a stem, each goes through.

The camera test (POST /cameras/test-capture/{i}) takes no volume and stores
nothing (NEH-166), so it is not refused.

The hardware is stood in for as test_capture_counter_raised.py does.
"""

import inspect

import pytest

import capture.service as capture_service
import capture.project_manager as project_manager_module

from app.core.volume_stem import STEM_REQUIRED_DETAIL


def _user(username, role):
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, username="op", role="admin"):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


@pytest.fixture
def camera_calls(monkeypatch, tmp_path):
    """Fake cameras that note every call, so a test can say none was made."""
    calls = []

    def fake_connected(idx):
        calls.append(("connected", idx))
        return True

    def fake_config(camera_index, resolution="high", registry=None):
        calls.append(("config", camera_index))
        return {"camera_index": camera_index, "rotate_deg": 0}, "hw"

    def fake_single(project_name, camera_config, check_camera=False,
                    include_resolution=False, collection_name=None):
        calls.append(("single",))
        return str(tmp_path / "cap.jpg"), "cap-1", None

    def fake_dual(project_name, cam1_config, cam2_config, check_camera=False,
                  include_resolution=False, stagger_ms=0, collection_name=None):
        calls.append(("dual",))
        return (str(tmp_path / "cap_c0.jpg"), str(tmp_path / "cap_c1.jpg"),
                "cap-2", "pair-1")

    monkeypatch.setattr(capture_service, "is_camera_connected", fake_connected)
    monkeypatch.setattr(capture_service, "single_capture_image", fake_single)
    monkeypatch.setattr(capture_service, "dual_capture_image", fake_dual)
    monkeypatch.setattr(project_manager_module,
                        "default_camera_config_from_registry", fake_config)
    return calls


@pytest.fixture
def upload_dir(monkeypatch, tmp_path):
    """Uploads go under tmp_path; see test_capture_counter_raised.py."""
    import app.api.records as records_api
    monkeypatch.setattr(records_api.settings, "DTK_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def _volume(db_session, project, name, stem):
    from app.models.collection import Collection
    v = Collection(name=name, project_id=project.id, filename_stem=stem)
    db_session.add(v)
    db_session.commit()
    return v


def _record(db_session, volume, mode):
    from app.models.record import Record
    rec = Record(title="r", status="captured", capture_mode=mode, collection_id=volume.id)
    db_session.add(rec)
    db_session.commit()
    return rec


def _single(api, project, volume_id=None, **extra):
    payload = {"project_name": project.name, "camera_index": 0}
    if volume_id is not None:
        payload["collection_id"] = volume_id
    payload.update(extra)
    return api.post("/cameras/capture", json=payload)


def _dual(api, project, volume_id=None, **extra):
    payload = {"project_name": project.name}
    if volume_id is not None:
        payload["collection_id"] = volume_id
    payload.update(extra)
    return api.post("/cameras/capture/dual", json=payload)


def _capture(mode):
    return _single if mode == "single" else _dual


def _state(db_session, volume_id):
    """Records, images and capture_counter, to show a refusal wrote nothing."""
    from app.models.collection import Collection
    from app.models.record import Record, RecordImage
    db_session.expire_all()
    return (db_session.query(Record).count(),
            db_session.query(RecordImage).count(),
            db_session.get(Collection, volume_id).capture_counter)


def _assert_refused(resp):
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == STEM_REQUIRED_DETAIL


# ---------------------------------------------------------------------------
# A new capture
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("mode", ["single", "dual"])
def test_a_capture_into_a_volume_without_a_stem_is_refused(client, db_session, test_project,
                                                           camera_calls, mode):
    api = _client_as(client)
    v = _volume(db_session, test_project, "V", None)
    before = _state(db_session, v.id)

    resp = _capture(mode)(api, test_project, v.id)

    _assert_refused(resp)
    assert camera_calls == [], "a refused capture touches no camera"
    assert _state(db_session, v.id) == before


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["single", "dual"])
def test_a_capture_into_a_volume_with_a_stem_goes_through(client, db_session, test_project,
                                                          camera_calls, mode):
    api = _client_as(client)
    v = _volume(db_session, test_project, "V", "co-test-v")

    resp = _capture(mode)(api, test_project, v.id)

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"], resp.text
    assert (mode,) in camera_calls
    assert _state(db_session, v.id)[2] == 1


# ---------------------------------------------------------------------------
# A retake
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("mode", ["single", "dual"])
@pytest.mark.parametrize("names_volume", [True, False])
def test_a_retake_in_a_volume_without_a_stem_is_refused(client, db_session, test_project,
                                                        camera_calls, mode, names_volume):
    """Refused whether or not the request names the volume; the record's volume decides."""
    api = _client_as(client)
    v = _volume(db_session, test_project, "V", None)
    rec = _record(db_session, v, mode)
    before = _state(db_session, v.id)

    resp = _capture(mode)(api, test_project, v.id if names_volume else None, record_id=rec.id)

    _assert_refused(resp)
    assert camera_calls == []
    assert _state(db_session, v.id) == before


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["single", "dual"])
def test_a_retake_naming_a_stemmed_volume_for_a_record_in_a_stemless_one_is_refused(
        client, db_session, test_project, camera_calls, mode):
    api = _client_as(client)
    stemless = _volume(db_session, test_project, "A", None)
    stemmed = _volume(db_session, test_project, "B", "co-test-b")
    rec = _record(db_session, stemless, mode)

    resp = _capture(mode)(api, test_project, stemmed.id, record_id=rec.id)

    _assert_refused(resp)
    assert camera_calls == []


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["single", "dual"])
def test_a_retake_in_a_volume_with_a_stem_goes_through(client, db_session, test_project,
                                                       camera_calls, mode):
    api = _client_as(client)
    v = _volume(db_session, test_project, "V", "co-test-v")
    rec = _record(db_session, v, mode)

    resp = _capture(mode)(api, test_project, v.id, record_id=rec.id)

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"], resp.text
    assert resp.json()["record_id"] == rec.id
    assert _state(db_session, v.id)[2] == 1


# ---------------------------------------------------------------------------
# An upload
# ---------------------------------------------------------------------------

def _upload(api, record_id):
    return api.post(f"/records/{record_id}/images",
                    files={"file": ("page.jpg", b"not really a jpeg", "image/jpeg")})


@pytest.mark.unit
def test_an_upload_into_a_volume_without_a_stem_is_refused(client, db_session, test_project,
                                                          upload_dir):
    api = _client_as(client)
    v = _volume(db_session, test_project, "V", None)
    rec = _record(db_session, v, "single")
    before = _state(db_session, v.id)

    resp = _upload(api, rec.id)

    _assert_refused(resp)
    assert _state(db_session, v.id) == before
    uploads = upload_dir / "uploads"
    assert not uploads.exists() or not any(uploads.iterdir()), "a refused upload writes no file"


@pytest.mark.unit
def test_an_upload_into_a_volume_with_a_stem_goes_through(client, db_session, test_project,
                                                         upload_dir):
    api = _client_as(client)
    v = _volume(db_session, test_project, "V", "co-test-v")
    rec = _record(db_session, v, "single")

    resp = _upload(api, rec.id)

    assert resp.status_code == 200, resp.text
    assert _state(db_session, v.id)[2] == 1


# ---------------------------------------------------------------------------
# The camera test is not refused
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_camera_test_takes_no_volume_and_no_database():
    from app.api.cameras import test_capture

    assert set(inspect.signature(test_capture).parameters) == {
        "camera_index", "resolution", "current_user"}


@pytest.mark.unit
def test_the_camera_test_works_while_a_volume_has_no_stem(client, db_session, test_project,
                                                          monkeypatch):
    api = _client_as(client)
    _volume(db_session, test_project, "V", None)
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    monkeypatch.setattr(capture_service, "test_capture_bytes",
                        lambda idx, res: (b"\xff\xd8fake", 0.1))

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 200, resp.text
    assert resp.content == b"\xff\xd8fake"
