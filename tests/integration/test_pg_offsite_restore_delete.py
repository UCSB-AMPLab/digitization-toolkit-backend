"""A restore's placement and a record delete, concurrently, on PostgreSQL.

app.core.offsite_free._place puts a restored file in place and clears its
freed state in one transaction, under the record's row lock, after reading
the image again. delete_record takes the same lock before it chooses the
files to unlink, and unlinks them only after its commit. So whichever takes
the lock first decides:

(a) the placement first: the delete waits, then removes the placed file
    with its row;
(b) the delete first: the placement waits, finds no row, places nothing and
    reports the file as changed, and the staging copy is removed.

Either way no file is left on the card without a row. SQLite takes no row
locks, so only a real PostgreSQL shows the serialisation, and that neither
side deadlocks.

Opt-in, as tests/integration/test_pg_offsite_reservation.py: skipped unless
DATABASE_HOST is set and DATABASE_* reach a PostgreSQL server; the tables
are created in a schema of their own, dropped afterwards.
"""
import hashlib
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest


if "DATABASE_HOST" not in os.environ:
    pytest.skip("PostgreSQL lock tests are opt-in: set DATABASE_* to a PostgreSQL server",
                allow_module_level=True)

HOLD = 1.5      # seconds the first side sleeps with the record's lock held
SECOND = 0.5    # seconds before the second side starts
JOIN = 60

STEM = "co-test-v"
BYTES = b"the master's bytes"


@pytest.fixture
def pg(override_projects_root):
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import sessionmaker

    from app.core.db import Base, DATABASE_URL
    from app.models import (  # noqa: F401
        user, project, project_member, collection, record, camera, system_log, offsite_object,
    )
    from app.models.collection import Collection
    from app.models.offsite_object import OffsiteObject
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    schema = f"captua_placetest_{uuid.uuid4().hex[:12]}"
    admin = create_engine(DATABASE_URL)
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    except OperationalError as exc:
        admin.dispose()
        pytest.skip(f"DATABASE_* does not reach a PostgreSQL server: {exc.orig}")

    engine = create_engine(DATABASE_URL, connect_args={"options": f"-csearch_path={schema}"})
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    captures = override_projects_root / STEM / "captures"
    captures.mkdir(parents=True)
    target = captures / "f.jpg"
    key = f"t/{STEM}/captures/f.jpg"
    try:
        Base.metadata.create_all(engine)
        with Session() as db:
            p = Project(name="P")
            db.add(p)
            db.commit()
            v = Collection(name="V", project_id=p.id, filename_stem=STEM)
            db.add(v)
            db.commit()
            r = Record(title="r", status="captured", capture_mode="single", collection_id=v.id)
            db.add(r)
            db.commit()
            now = datetime.now(timezone.utc)
            i = RecordImage(record_id=r.id, filename="f.jpg", file_path=str(target), format="JPG",
                            master_freed_at=now, master_freed_bytes=len(BYTES))
            db.add(i)
            db.commit()
            db.add(OffsiteObject(record_image_id=i.id, kind="capture", object_key=key,
                                 local_sha1=hashlib.sha1(BYTES).hexdigest(), verified_at=now))
            db.commit()
            ids = {"volume": v.id, "record": r.id, "image": i.id}

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
            username="adm", email="adm@example.com", hashed_password="x", role="admin", is_active=True)
        yield {"Session": Session, "target": target, "captures": captures, "key": key, **ids}
    finally:
        from app.main import app
        app.dependency_overrides.clear()
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def fake_copy(monkeypatch):
    """The bucket's copy, written to the staging file without rclone."""
    import app.core.offsite_free as of

    def copy(key, dest):
        dest.write_bytes(BYTES)
        return None

    monkeypatch.setattr(of, "_copy_from_bucket", copy)


def _restore(pg, out):
    import app.core.offsite_free as of

    out["restore_start"] = time.monotonic()
    with pg["Session"]() as db:
        obj = of._objects(db, pg["volume"], freed=True)[0]
        entry, refusal = of._restore_one(db, obj)
    out["restore"] = "restored" if entry is not None else refusal["reason"]
    out["restore_done"] = time.monotonic()


def _delete(pg, out):
    from fastapi.testclient import TestClient
    from app.main import app

    r = TestClient(app).delete(f"/records/{pg['record']}")
    out["delete"] = r.status_code
    out["delete_done"] = time.monotonic()


def _both(first, second):
    threads = [threading.Thread(target=first, name="first"), threading.Thread(target=second, name="second")]
    threads[0].start()
    time.sleep(SECOND)
    threads[1].start()
    for t in threads:
        t.join(JOIN)
    assert not any(t.is_alive() for t in threads), "deadlock or a lock never released"


def _image_row_exists(pg):
    from app.models.record import RecordImage

    with pg["Session"]() as db:
        return db.get(RecordImage, pg["image"]) is not None


@pytest.mark.integration
def test_a_placement_holding_the_lock_makes_the_delete_remove_the_placed_file(pg, fake_copy, monkeypatch):
    import app.core.offsite_free as of

    real = of._fsync_dir

    def slow_under_the_lock(directory):
        # Called by _place after os.replace and before its commit: the
        # record's lock is held, and the file is already in place.
        real(directory)
        time.sleep(HOLD)

    monkeypatch.setattr(of, "_fsync_dir", slow_under_the_lock)
    out = {}
    _both(lambda: _restore(pg, out), lambda: _delete(pg, out))

    assert out["restore"] == "restored" and out["delete"] == 200, out
    assert out["delete_done"] > out["restore_done"], "the delete did not wait for the placement's commit"
    assert not pg["target"].exists(), "the placed file outlived its deleted row"
    assert not _image_row_exists(pg)
    assert list(pg["captures"].iterdir()) == []


@pytest.mark.integration
def test_a_delete_holding_the_lock_makes_the_placement_place_nothing(pg, fake_copy, monkeypatch):
    import app.api.records as records

    real = records._image_files_to_unlink

    def slow_under_the_lock(*args, **kwargs):
        # Called by delete_record with the record's lock held, before its commit.
        paths = real(*args, **kwargs)
        time.sleep(HOLD)
        return paths

    monkeypatch.setattr(records, "_image_files_to_unlink", slow_under_the_lock)
    out = {}
    _both(lambda: _delete(pg, out), lambda: _restore(pg, out))

    assert out["delete"] == 200 and out["restore"] == "changed", out
    # The placement started SECOND into the delete's HOLD and waited on the lock for the rest of it.
    assert out["restore_done"] - out["restore_start"] > HOLD - SECOND - 0.3, out
    assert not pg["target"].exists(), "the deleted image's master was written back to the card"
    assert not _image_row_exists(pg)
    assert list(pg["captures"].iterdir()) == [], "a staging copy was left behind"
