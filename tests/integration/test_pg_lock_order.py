"""Two concurrent requests on one record, through the real app code, on PostgreSQL.

Every path that takes both locks takes the record's row first and then its
volume's (app/core/volume_stem.py). An upload that read its record unlocked
locked the volume at the stem check and then inserted the image, whose
foreign-key check takes FOR KEY SHARE on the record: volume, then record. A
retake locks the record, then the volume, and the two deadlocked
("deadlock detected ... while locking tuple ... in relation records").
tests/unit/test_capture_requires_stem.py checks the order in the source, but
SQLite takes no row locks, so only a real PostgreSQL shows the deadlock.

Opt-in: skipped unless DATABASE_HOST is set in the environment and
DATABASE_* reach a PostgreSQL server. The tables are created in a schema of
their own, named for this run and dropped afterwards, so nothing already in
that database is read or changed. The test needs the right to create a schema.

Each request runs in its own thread with its own TestClient and its own
session. The upload sleeps after taking its locks, while saving the file, so
the second request arrives while they are held. TestClient runs endpoints on
threads of its own, so the stand-in for the old unlocked read decides by the
function calling it, never by thread name.

Run with, for example:
  DATABASE_HOST=localhost DATABASE_PORT=5432 DATABASE_USER=... DATABASE_PASSWORD=... \
  DATABASE_NAME=... python -m pytest tests/integration/test_pg_lock_order.py
"""
import io
import os
import sys
import threading
import time
import uuid

import pytest


if "DATABASE_HOST" not in os.environ:
    pytest.skip("PostgreSQL lock tests are opt-in: set DATABASE_* to a PostgreSQL server",
                allow_module_level=True)


UPLOAD_HOLDS_LOCKS_FOR = 1.5   # seconds the upload sleeps with its locks held
SECOND_REQUEST_AFTER = 0.5     # seconds before the second request starts
JOIN_TIMEOUT = 60


@pytest.fixture
def pg(monkeypatch, tmp_path):
    """A throwaway schema on the PostgreSQL that DATABASE_* names, with one record in a stemmed volume."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import sessionmaker

    from app.core.db import Base, DATABASE_URL
    from app.models import (  # noqa: F401
        user, project, project_member, collection, record, camera, system_log,
    )
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record

    schema = f"captua_locktest_{uuid.uuid4().hex[:12]}"
    admin = create_engine(DATABASE_URL)
    try:
        # app.core.db builds a postgresql+psycopg URL from DATABASE_*, so a
        # server that answers is a PostgreSQL.
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    except OperationalError as exc:
        admin.dispose()
        pytest.skip(f"DATABASE_* does not reach a PostgreSQL server: {exc.orig}")

    engine = create_engine(DATABASE_URL, connect_args={"options": f"-csearch_path={schema}"})
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    try:
        Base.metadata.create_all(engine)
        with Session() as db:
            p = Project(name="P")
            db.add(p)
            db.commit()
            v = Collection(name="V", project_id=p.id, filename_stem="co-test-v")
            db.add(v)
            db.commit()
            r = Record(title="r", status="captured", capture_mode="single", collection_id=v.id)
            db.add(r)
            db.commit()
            ids = {"volume": v.id, "record": r.id}

        _install_app(monkeypatch, tmp_path, Session)
        yield {"Session": Session, **ids}
    finally:
        from app.main import app
        app.dependency_overrides.clear()
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _install_app(monkeypatch, tmp_path, Session):
    """Point the app at the schema, stand in for the camera, and slow the upload's save."""
    import capture.service as capture_service
    import capture.project_manager as project_manager_module
    import app.api.records as records_api
    from app.main import app
    from app.api.auth import get_current_user
    from app.api.deps import get_db_dependency
    from app.models.user import User

    def session_per_request():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db_dependency] = session_per_request
    app.dependency_overrides[get_current_user] = lambda: User(
        username="op", email="op@example.com", hashed_password="x", role="admin", is_active=True)

    monkeypatch.setattr(records_api.settings, "DTK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    monkeypatch.setattr(capture_service, "single_capture_image",
                        lambda project_name, camera_config, check_camera=False,
                        include_resolution=False, collection_name=None, stem=None:
                        (str(tmp_path / "cap.jpg"), "cap-1", None))
    monkeypatch.setattr(project_manager_module, "default_camera_config_from_registry",
                        lambda camera_index, resolution="high", registry=None:
                        ({"camera_index": camera_index, "rotate_deg": 0}, "hw"))

    real_save = records_api._save_upload_capped

    def slow_save(src, dst, max_bytes):
        time.sleep(UPLOAD_HOLDS_LOCKS_FOR)
        return real_save(src, dst, max_bytes)

    monkeypatch.setattr(records_api, "_save_upload_capped", slow_save)


def _upload(record_id):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    return client.post(f"/records/{record_id}/images?role=single",
                       files={"file": ("a.jpg", io.BytesIO(b"\xff\xd8\xff\xd9"), "image/jpeg")})


def _retake(volume_id, record_id):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    return client.post("/cameras/capture", json={
        "project_name": "P", "camera_index": 0, "collection_id": volume_id, "record_id": record_id})


def _run_together(first, second):
    """Start first, then second after SECOND_REQUEST_AFTER; return both responses."""
    results = {}

    def run(name, fn, delay):
        time.sleep(delay)
        results[name] = fn()

    threads = [threading.Thread(target=run, args=("first", first, 0)),
               threading.Thread(target=run, args=("second", second, SECOND_REQUEST_AFTER))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN_TIMEOUT)
    assert not any(t.is_alive() for t in threads), "a request did not finish"
    return results["first"], results["second"]


def _images_and_counter(pg):
    from app.models.collection import Collection
    from app.models.record import RecordImage

    with pg["Session"]() as db:
        return (db.query(RecordImage).filter(RecordImage.record_id == pg["record"]).count(),
                db.get(Collection, pg["volume"]).capture_counter)


@pytest.mark.integration
def test_an_upload_and_a_retake_on_one_record_both_land(pg):
    upload, retake = _run_together(lambda: _upload(pg["record"]),
                                   lambda: _retake(pg["volume"], pg["record"]))

    assert upload.status_code == 200, upload.text
    assert retake.status_code == 200, retake.text
    assert retake.json()["success"] is True, retake.text
    assert _images_and_counter(pg) == (2, 2)


@pytest.mark.integration
def test_two_uploads_on_one_record_both_land(pg):
    first, second = _run_together(lambda: _upload(pg["record"]), lambda: _upload(pg["record"]))

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert _images_and_counter(pg) == (2, 2)


@pytest.mark.integration
def test_the_old_unlocked_read_deadlocks_so_this_test_can_see_it(pg, monkeypatch):
    """Control: with the upload reading its record unlocked again, the pair fails."""
    import app.api.records as records_api
    from app.models.record import Record

    real_locked = records_api._locked_record

    def unlocked_for_the_upload(db, rec_id, with_images=True):
        if sys._getframe(1).f_code.co_name == "add_image_to_record":
            return db.query(Record).filter(Record.id == rec_id).first()
        return real_locked(db, rec_id, with_images)

    monkeypatch.setattr(records_api, "_locked_record", unlocked_for_the_upload)

    upload, retake = _run_together(lambda: _upload(pg["record"]),
                                   lambda: _retake(pg["volume"], pg["record"]))

    assert (upload.status_code, retake.status_code) != (200, 200), (upload.text, retake.text)
