"""No capture outside a volume, and none into a volume without a filename_stem.

Exchange contract 1.1 §3 names every captured image after its volume's stem,
withdraws the timestamp names used when there was none, and Captua never
makes a stem up. So the single capture, the dual capture, a retake through
either, and an upload through POST /records/{id}/images are refused with a
409 when they would go into no volume (a record attached straight to a
project) or into a volume that has no stem, before any camera is touched and
before any file is written. In a volume with a stem, each goes through.

The camera test (POST /cameras/test-capture/{i}) takes no volume and stores
nothing (NEH-166), so it is not refused.

The hardware is stood in for as test_capture_counter_raised.py does.

Version: 0.1.0-beta
"""

import inspect

import pytest

import capture.service as capture_service
import capture.project_manager as project_manager_module

from app.core.volume_stem import RETAKE_VOLUME_DETAIL, STEM_REQUIRED_DETAIL, VOLUME_REQUIRED_DETAIL


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
                    include_resolution=False, collection_name=None, stem=None, filename_index=None, root=None):
        calls.append(("single",))
        return str(tmp_path / "cap.jpg"), "cap-1", None

    def fake_dual(project_name, cam1_config, cam2_config, check_camera=False,
                  include_resolution=False, stagger_ms=0, collection_name=None, stem=None, filename_index=None, root=None, roles=None):
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
    monkeypatch.setattr(records_api.settings, "DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def _volume(db_session, project, name, stem):
    from app.models.collection import Collection
    v = Collection(name=name, project_id=project.id, filename_stem=stem)
    db_session.add(v)
    db_session.commit()
    return v


def _record(db_session, volume, mode, project=None):
    """A captured record in volume, or attached straight to project when volume is None."""
    from app.models.record import Record
    rec = Record(title="r", status="captured", capture_mode=mode,
                 collection_id=volume.id if volume is not None else None,
                 project_id=project.id if volume is None else None)
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


def _assert_refused(resp, detail=STEM_REQUIRED_DETAIL):
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == detail


def _counts(db_session):
    from app.models.record import Record, RecordImage
    db_session.expire_all()
    return db_session.query(Record).count(), db_session.query(RecordImage).count()


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
def test_a_retake_in_a_volume_without_a_stem_is_refused(client, db_session, test_project,
                                                        camera_calls, mode):
    api = _client_as(client)
    v = _volume(db_session, test_project, "V", None)
    rec = _record(db_session, v, mode)
    before = _state(db_session, v.id)

    resp = _capture(mode)(api, test_project, v.id, record_id=rec.id)

    _assert_refused(resp)
    assert camera_calls == []
    assert _state(db_session, v.id) == before


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["single", "dual"])
@pytest.mark.parametrize("record_stem", ["co-test-a", None], ids=["stemmed", "stemless"])
@pytest.mark.parametrize("status", ["captured", "rejected", "approved"])
def test_a_retake_into_another_volume_is_refused(client, db_session, test_project,
                                                 camera_calls, mode, record_stem, status):
    """The file would land in the named volume while the record and its counter are another's."""
    api = _client_as(client)
    own = _volume(db_session, test_project, "A", record_stem)
    other = _volume(db_session, test_project, "B", "co-test-b")
    rec = _record(db_session, own, mode)
    rec.status = status
    db_session.commit()
    before_own, before_other = _state(db_session, own.id), _state(db_session, other.id)

    resp = _capture(mode)(api, test_project, other.id, record_id=rec.id)

    _assert_refused(resp, RETAKE_VOLUME_DETAIL)
    assert camera_calls == [], "a retake into another volume touches no camera"
    assert _state(db_session, own.id) == before_own
    assert _state(db_session, other.id) == before_other


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


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["single", "dual"])
def test_a_retake_of_a_missing_record_is_a_404_before_any_camera(client, db_session, test_project,
                                                                 camera_calls, mode):
    api = _client_as(client)
    v = _volume(db_session, test_project, "V", "co-test-v")
    before = _state(db_session, v.id)

    resp = _capture(mode)(api, test_project, v.id, record_id=999)

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Record 999 not found"
    assert camera_calls == [], "a retake of a missing record touches no camera"
    assert _state(db_session, v.id) == before


@pytest.mark.unit
@pytest.mark.parametrize("mode,other", [("single", "dual"), ("dual", "single")])
@pytest.mark.parametrize("status", ["captured", "rejected"])
def test_a_retake_in_the_wrong_capture_mode_is_a_422_before_any_camera(
        client, db_session, test_project, camera_calls, mode, other, status):
    """A record is retaken only in the mode it was captured in."""
    api = _client_as(client)
    v = _volume(db_session, test_project, "V", "co-test-v")
    rec = _record(db_session, v, other)
    rec.status = status
    db_session.commit()
    before = _state(db_session, v.id)

    resp = _capture(mode)(api, test_project, v.id, record_id=rec.id)

    assert resp.status_code == 422, resp.text
    assert camera_calls == [], "a retake in the wrong mode touches no camera"
    assert _state(db_session, v.id) == before


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["single", "dual"])
def test_a_capture_into_an_unknown_volume_is_a_404_before_any_camera(client, db_session, test_project,
                                                                     camera_calls, mode):
    api = _client_as(client)
    before = _counts(db_session)

    resp = _capture(mode)(api, test_project, 999)

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Collection 999 not found"
    assert camera_calls == [], "a capture into an unknown volume touches no camera"
    assert _counts(db_session) == before


# ---------------------------------------------------------------------------
# No capture outside a volume
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("mode", ["single", "dual"])
@pytest.mark.parametrize("volume_id", [None, 0], ids=["omitted", "zero"])
def test_a_capture_into_no_volume_is_refused(client, db_session, test_project,
                                             camera_calls, mode, volume_id):
    """0 counts as none: the capture screen sends it when its URL names no volume."""
    api = _client_as(client)
    before = _counts(db_session)

    resp = _capture(mode)(api, test_project, volume_id)

    _assert_refused(resp, VOLUME_REQUIRED_DETAIL)
    assert camera_calls == [], "a refused capture touches no camera"
    assert _counts(db_session) == before


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["single", "dual"])
def test_a_retake_that_names_no_volume_is_refused(client, db_session, test_project,
                                                  camera_calls, mode):
    """The request's volume decides where the file is written, so it must name one."""
    api = _client_as(client)
    v = _volume(db_session, test_project, "V", "co-test-v")
    rec = _record(db_session, v, mode)
    before = _state(db_session, v.id)

    resp = _capture(mode)(api, test_project, None, record_id=rec.id)

    _assert_refused(resp, VOLUME_REQUIRED_DETAIL)
    assert camera_calls == []
    assert _state(db_session, v.id) == before


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["single", "dual"])
def test_a_retake_of_a_record_in_no_volume_is_refused(client, db_session, test_project,
                                                      camera_calls, mode):
    api = _client_as(client)
    v = _volume(db_session, test_project, "V", "co-test-v")
    rec = _record(db_session, None, mode, project=test_project)
    before = _counts(db_session)

    resp = _capture(mode)(api, test_project, v.id, record_id=rec.id)

    _assert_refused(resp, VOLUME_REQUIRED_DETAIL)
    assert camera_calls == []
    assert _counts(db_session) == before


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
def test_an_upload_into_no_volume_is_refused(client, db_session, test_project, upload_dir):
    api = _client_as(client)
    rec = _record(db_session, None, "single", project=test_project)
    before = _counts(db_session)

    resp = _upload(api, rec.id)

    _assert_refused(resp, VOLUME_REQUIRED_DETAIL)
    assert _counts(db_session) == before
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


# ---------------------------------------------------------------------------
# The stem check holds the volume row
# ---------------------------------------------------------------------------
#
# A PATCH clearing a stem (app/api/collections.py) takes the volume row under
# SELECT ... FOR UPDATE. The stem check takes the same row the same way, so a
# PATCH cannot land between the check and the capture's commit. The suite
# runs on SQLite, which emits no lock clause, so these tests read the SQL
# that would reach PostgreSQL and the source of each path, as
# test_workflow_row_locking.py does for the record row.

def _postgres_sql(query) -> str:
    from sqlalchemy.dialects import postgresql
    return str(query.statement.compile(dialect=postgresql.dialect())).upper()


@pytest.mark.unit
def test_the_volume_loader_locks_the_volume_row(db_session):
    from app.core.volume_stem import locked_volume_query

    sql = _postgres_sql(locked_volume_query(db_session, 1))
    assert "FROM COLLECTIONS" in sql, sql
    assert sql.rstrip().endswith("FOR UPDATE"), sql


@pytest.mark.unit
def test_sqlite_emits_no_volume_lock(db_session):
    """Stated as a test so the limitation is not rediscovered as a surprise."""
    from sqlalchemy.dialects import sqlite
    from app.core.volume_stem import locked_volume_query

    sql = str(locked_volume_query(db_session, 1).statement.compile(dialect=sqlite.dialect())).upper()
    assert "FOR UPDATE" not in sql, sql


@pytest.mark.unit
def test_every_stem_check_loads_the_volume_through_the_locked_loader():
    from app.api import cameras as cameras_api
    from app.core import volume_stem

    for fn in (cameras_api._refuse_capture_without_volume_or_stem, volume_stem.volume_lacks_stem):
        src = inspect.getsource(fn)
        assert "locked_volume_query(db, " in src, f"{fn.__name__} does not lock the volume row"
        assert "db.query(Collection" not in src, f"{fn.__name__} loads the volume without the lock"

    from app.api import records as records_api
    src = inspect.getsource(records_api.add_image_to_record)
    assert "volume_lacks_stem(db, rec.collection_id)" in src, "the upload does not run the locked stem check"


# ---------------------------------------------------------------------------
# One lock order: the record, then its volume
# ---------------------------------------------------------------------------
#
# A path that takes both locks takes the record's row first and the volume's
# second. An upload that locks the volume and then inserts an image, whose
# foreign-key check takes FOR KEY SHARE on the record, deadlocks on
# PostgreSQL 16 against a retake that holds the record and wants the volume.
# The locked loaders' SQL is checked above and in test_workflow_row_locking.py;
# here, the order in each path's source.

def _before(src: str, first: str, second: str) -> bool:
    assert first in src, f"{first!r} not in the source"
    assert second in src, f"{second!r} not in the source"
    return src.index(first) < src.index(second)


@pytest.mark.unit
def test_the_upload_locks_the_record_before_the_volume():
    from app.api import records as records_api

    src = inspect.getsource(records_api.add_image_to_record)
    assert "db.query(Record).filter(Record.id == rec_id)" not in src, "the upload loads its record unlocked"
    assert _before(src, "_locked_record(db, rec_id", "volume_lacks_stem(db, rec.collection_id)")


@pytest.mark.unit
@pytest.mark.parametrize("name", ["trigger_capture", "trigger_dual_capture"])
def test_a_retake_locks_the_record_before_the_volume(name):
    from app.api import cameras as cameras_api

    src = inspect.getsource(getattr(cameras_api, name))
    assert _before(src, "_locked_record(db, request.record_id)",
                   "_refuse_capture_without_volume_or_stem(db, request.collection_id, retake_record)")
