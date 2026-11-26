"""A retake that waited for the record's lock sees the images committed meanwhile, on PostgreSQL.

Under READ COMMITTED, a statement that waits for a row lock re-reads the
locked row once it has it, but rows it joins come from the snapshot it
started with. The record's loader (app.api.records._locked_record) therefore
locks the record row alone and reads the images in a second statement, issued
once the lock is held. Here one session holds the record's lock while a
second starts the retake path's locking read and waits; the first then
commits a retake of its own (the old image superseded, a new one current).
The second must see the new image: through the loader, and through
POST /cameras/capture, whose `expected_image_ids` name the old image and
which must answer 409 record_changed without calling the camera, leaving one
current image. SQLite takes no row lock, so only a real PostgreSQL shows this
(tests/unit/test_images_read_after_the_lock.py checks the statements' order).

Opt-in, as tests/integration/test_pg_lock_order.py: skipped unless
DATABASE_HOST is set and DATABASE_* reach a PostgreSQL server; the tables are
created in a schema of their own, dropped afterwards. The waiting session is
detected in pg_locks (a lock not granted in this database), never by time.
"""
import os
import threading
import time
import uuid

import pytest


if "DATABASE_HOST" not in os.environ:
    pytest.skip("PostgreSQL lock tests are opt-in: set DATABASE_* to a PostgreSQL server",
                allow_module_level=True)


# Bounds on the waits below, which wait on conditions: a test that
# passes never reaches them.
WAIT_LIMIT_S = 60
POLL_S = 0.05
# On every connection the tests open, the teardown's DROP included, so that
# nothing waits for a lock or runs without limit, whatever fails first.
PG_LIMITS = "-clock_timeout=30s -cstatement_timeout=30s"


@pytest.fixture
def pg(monkeypatch, tmp_path):
    """A throwaway schema with one captured single record in a stemmed volume, its image current."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import sessionmaker

    from app.core.db import Base, DATABASE_URL, init_db
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    schema = f"captua_retaketest_{uuid.uuid4().hex[:12]}"
    admin = create_engine(DATABASE_URL, connect_args={"options": PG_LIMITS})
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    except OperationalError as exc:
        admin.dispose()
        pytest.skip(f"DATABASE_* does not reach a PostgreSQL server: {exc.orig}")

    engine = create_engine(DATABASE_URL,
                           connect_args={"options": f"-csearch_path={schema} {PG_LIMITS}"})
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    try:
        init_db()
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
            a = RecordImage(record_id=r.id, filename="a.jpg", file_path="/a.jpg",
                            format="jpg", role="single", is_current=True)
            db.add(a)
            db.commit()
            ids = {"volume": v.id, "record": r.id, "image": a.id}

        calls, request_pids = _install_app(monkeypatch, tmp_path, Session)
        yield {"Session": Session, "engine": engine, "calls": calls,
               "request_pids": request_pids, **ids}
    finally:
        from app.main import app
        app.dependency_overrides.clear()
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _install_app(monkeypatch, tmp_path, Session):
    """Point the app at the schema and stand in for the camera.

    Returns the camera calls made, and the backend pid of each request's
    session, read when the request opens it, before anything is locked.
    """
    import capture.service as capture_service
    import capture.project_manager as project_manager_module
    from app.main import app
    from app.api.auth import get_current_user
    from app.api.deps import get_db_dependency
    from app.models.user import User

    from sqlalchemy import text

    calls = []
    request_pids = []

    def session_per_request():
        db = Session()
        request_pids.append(db.execute(text("SELECT pg_backend_pid()")).scalar())
        try:
            yield db
        finally:
            db.close()

    def fake_single(project_name, camera_config, check_camera=False,
                    include_resolution=False, collection_name=None, **_kw):
        calls.append("single")
        return str(tmp_path / "cap.jpg"), "cap-1", None

    app.dependency_overrides[get_db_dependency] = session_per_request
    app.dependency_overrides[get_current_user] = lambda: User(
        username="op", email="op@example.com", hashed_password="x", role="admin", is_active=True)
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    monkeypatch.setattr(capture_service, "single_capture_image", fake_single)
    # The cameras' pages are taken as set: tests/unit/test_capture_refusals_set_up.py covers that refusal.
    # Its camera listing is noted as a camera call, so "no camera touched" covers it too.
    from app.api import cameras as cameras_api
    monkeypatch.setattr(cameras_api, "_refuse_a_capture_while_camera_setup_is_needed",
                        lambda: calls.append("camera_list"))
    monkeypatch.setattr(project_manager_module, "default_camera_config_from_registry",
                        lambda camera_index, resolution="high", registry=None:
                        ({"camera_index": camera_index, "rotate_deg": 0}, "hw"))
    return calls, request_pids


def _until(condition, what):
    """Wait until condition() is true; fail, naming `what`, past WAIT_LIMIT_S."""
    deadline = time.monotonic() + WAIT_LIMIT_S
    while not condition():
        assert time.monotonic() < deadline, f"gave up waiting: {what}"
        time.sleep(POLL_S)


def _pid(db):
    """The backend pid of the connection a session holds."""
    from sqlalchemy import text
    return db.execute(text("SELECT pg_backend_pid()")).scalar()


def _blocked_by(engine, waiter_pid, holder_pid):
    """Whether the waiter's backend waits for a lock, and the holder's backend is what blocks it."""
    from sqlalchemy import text
    with engine.connect() as conn:
        waiting = conn.execute(text(
            "SELECT count(*) FROM pg_locks WHERE pid = :pid AND NOT granted"), {"pid": waiter_pid}).scalar()
        blockers = conn.execute(text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid}).scalar()
    return waiting > 0 and holder_pid in (blockers or [])


def _hold_the_record_lock(pg):
    """A session holding the record's row lock, as another retake does while it captures."""
    from app.models.record import Record
    holder = pg["Session"]()
    holder.query(Record).filter(Record.id == pg["record"]).with_for_update(of=Record).one()
    return holder


def _commit_another_retake(holder, pg):
    """The holder's retake commits: the old image superseded, a new one current. Returns its id."""
    from app.models.record import RecordImage
    holder.get(RecordImage, pg["image"]).is_current = False
    new = RecordImage(record_id=pg["record"], filename="b.jpg", file_path="/b.jpg",
                      format="jpg", role="single", is_current=True)
    holder.add(new)
    holder.commit()
    new_id = new.id
    holder.close()
    return new_id


def _in_a_thread(fn):
    """Run fn in a thread; returns (thread, done event, results dict)."""
    done = threading.Event()
    out = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as exc:  # reported by the test, not lost in the thread
            out["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, done, out


def _joined(thread):
    """Join the worker within WAIT_LIMIT_S; fail if it is still alive."""
    thread.join(WAIT_LIMIT_S)
    assert not thread.is_alive(), "the worker thread did not finish"


def _release(holder):
    """End the holder's transaction and give its connection back, whatever state it is in."""
    holder.rollback()
    holder.close()


def _current_ids(pg):
    from app.models.record import RecordImage
    with pg["Session"]() as db:
        return sorted(img.id for img in db.query(RecordImage).filter(
            RecordImage.record_id == pg["record"], RecordImage.is_current.is_(True)))


@pytest.mark.integration
def test_pg_the_locked_loader_sees_the_images_committed_while_it_waited(pg):
    from app.api.records import _locked_record

    holder = _hold_the_record_lock(pg)
    waiter = pg["Session"]()
    thread = None
    try:
        holder_pid, waiter_pid = _pid(holder), _pid(waiter)
        thread, done, out = _in_a_thread(
            lambda: sorted(img.id for img in _locked_record(waiter, pg["record"]).images if img.is_current))
        _until(lambda: _blocked_by(pg["engine"], waiter_pid, holder_pid) or done.is_set(),
               "the loader waiting for the holder's lock")
        assert not done.is_set(), f"the loader did not wait for the lock: {out}"

        new_id = _commit_another_retake(holder, pg)
        _until(done.is_set, "the loader to return once the lock was released")
        assert "error" not in out, out.get("error")
        assert out["value"] == [new_id]
    finally:
        _release(holder)
        if thread is not None:
            _joined(thread)
        waiter.rollback()
        waiter.close()


@pytest.mark.integration
def test_pg_a_retake_that_waited_for_another_retake_is_refused(pg):
    from fastapi.testclient import TestClient
    from app.main import app

    holder = _hold_the_record_lock(pg)
    thread = None
    try:
        holder_pid = _pid(holder)
        client = TestClient(app, raise_server_exceptions=False)
        thread, done, out = _in_a_thread(lambda: client.post("/cameras/capture", json={
            "project_name": "P", "camera_index": 0, "collection_id": pg["volume"],
            "record_id": pg["record"], "expected_image_ids": [pg["image"]]}))
        # The request's session is opened, and its pid recorded, before it locks anything.
        _until(lambda: pg["request_pids"] or done.is_set(), "the retake to open its session")
        assert not done.is_set(), f"the retake answered before it opened a session: {out}"
        waiter_pid = pg["request_pids"][0]
        _until(lambda: _blocked_by(pg["engine"], waiter_pid, holder_pid) or done.is_set(),
               "the retake waiting for the holder's lock")
        assert not done.is_set(), f"the retake did not wait for the lock: {out}"

        new_id = _commit_another_retake(holder, pg)
        _until(done.is_set, "the retake to answer once the lock was released")
        assert "error" not in out, out.get("error")
        resp = out["value"]
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "record_changed", resp.text
        assert pg["calls"] == []
        assert _current_ids(pg) == [new_id]
    finally:
        _release(holder)
        if thread is not None:
            _joined(thread)
