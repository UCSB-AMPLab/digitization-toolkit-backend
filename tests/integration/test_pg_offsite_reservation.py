"""The image upload's key reservation and the record moves, concurrently, on PostgreSQL.

app.core.image_upload._claim writes an object's first offsite_objects row
under the record's row lock, after reading the image again. Both move paths
take that lock before checking records_stored_offsite. So whichever commits
first decides: a reservation first makes the move answer 409; a move first
makes the reservation write nothing. SQLite takes no row locks, so only a
real PostgreSQL shows the serialisation, and that neither side deadlocks.

Opt-in, as tests/integration/test_pg_lock_order.py: skipped unless
DATABASE_HOST is set and DATABASE_* reach a PostgreSQL server; the tables
are created in a schema of their own, dropped afterwards.

Version: 0.1.0-beta
"""
import os
import threading
import time
import uuid

import pytest


if "DATABASE_HOST" not in os.environ:
    pytest.skip("PostgreSQL lock tests are opt-in: set DATABASE_* to a PostgreSQL server",
                allow_module_level=True)

HOLD = 1.5      # seconds the first side sleeps with the record's lock held
SECOND = 0.5    # seconds before the second side starts
JOIN = 60


@pytest.fixture
def pg(monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import sessionmaker

    from app.core.db import Base, DATABASE_URL
    from app.models import (  # noqa: F401
        user, project, project_member, collection, record, camera, system_log, offsite_object,
    )
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    schema = f"captua_reservetest_{uuid.uuid4().hex[:12]}"
    admin = create_engine(DATABASE_URL)
    try:
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
            w = Collection(name="W", project_id=p.id, filename_stem="co-test-w")
            db.add_all([v, w])
            db.commit()
            r = Record(title="r", status="captured", capture_mode="single", collection_id=v.id)
            db.add(r)
            db.commit()
            i = RecordImage(record_id=r.id, filename="f.jpg", file_path="/nonexistent/f.jpg", format="JPG")
            db.add(i)
            db.commit()
            ids = {"v": v.id, "w": w.id, "record": r.id, "image": i.id}

        from app.api.auth import get_current_user
        from app.api.deps import get_db_dependency
        from app.main import app
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
        yield {"Session": Session, **ids}
    finally:
        from app.main import app
        app.dependency_overrides.clear()
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _reserve(pg, out):
    import app.core.image_upload as iu

    with pg["Session"]() as db:
        state = iu._image_state(db, pg["image"])
        db.commit()
        item = iu.Work(pg["image"], "capture", "co-test-v", "/nonexistent/f.jpg")
        try:
            iu._claim(db, None, item, "t/co-test-v/captures/f.jpg", state, local_sha1="0" * 40)
            out["reserve"] = "reserved"
        except iu._ImageMoved:
            out["reserve"] = "moved"


def _move(pg, out, path):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    if path == "patch":
        r = client.patch(f"/records/{pg['record']}", json={"collection_id": pg["w"]})
    else:
        r = client.post(f"/collections/{pg['v']}/move-records", params={"target_collection_id": pg["w"]})
    out["move"] = r.status_code


def _both(first, second):
    threads = [threading.Thread(target=first, name="first"), threading.Thread(target=second, name="second")]
    threads[0].start()
    time.sleep(SECOND)
    threads[1].start()
    for t in threads:
        t.join(JOIN)
    assert not any(t.is_alive() for t in threads), "deadlock or a lock never released"


def _state(pg):
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record

    with pg["Session"]() as db:
        return (db.get(Record, pg["record"]).collection_id,
                db.query(OffsiteObject).count())


@pytest.mark.integration
@pytest.mark.parametrize("path", ["patch", "move-records"])
def test_a_reservation_holding_the_lock_makes_the_move_answer_409(pg, monkeypatch, path):
    import app.core.image_upload as iu

    real = iu._image_state
    calls = []

    def slow_under_the_lock(db, image_id):
        state = real(db, image_id)
        if threading.current_thread().name == "first":
            calls.append(1)
            if len(calls) == 2:  # the read inside _claim, with the record's lock held
                time.sleep(HOLD)
        return state

    monkeypatch.setattr(iu, "_image_state", slow_under_the_lock)
    out = {}
    _both(lambda: _reserve(pg, out), lambda: _move(pg, out, path))
    assert out == {"reserve": "reserved", "move": 409}, out
    assert _state(pg) == (pg["v"], 1)


@pytest.mark.integration
def test_a_move_holding_the_lock_makes_the_reservation_write_nothing(pg, monkeypatch):
    import app.core.image_upload as iu

    real = iu.records_stored_offsite

    def slow_under_the_lock(db, ids):
        found = real(db, ids)
        if threading.current_thread().name != "first":
            time.sleep(HOLD)
        return found

    monkeypatch.setattr(iu, "records_stored_offsite", slow_under_the_lock)
    out = {}
    _both(lambda: _move(pg, out, "patch"), lambda: _reserve(pg, out))
    assert out == {"move": 200, "reserve": "moved"}, out
    assert _state(pg) == (pg["w"], 0)
