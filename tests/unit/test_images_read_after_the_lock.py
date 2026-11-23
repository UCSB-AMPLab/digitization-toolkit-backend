"""What a handler decides from a record's images it reads after the record's lock is held.

On PostgreSQL, under READ COMMITTED, a statement that waits for a row lock
re-reads the locked row once it has it, but rows it joins come from the
snapshot the statement started with. A locking select that also joins the
images would hand a retake that waited for another retake's commit the
images from before that commit. So the locking statement reads the record
row alone, and the images (and, where a path decides from them, the
rejections) are read by a separate statement issued after it, which also
replaces what the session already held.

SQLite takes no row lock, so these tests cannot show the interleaving; they
show the order of the statements and that the second read replaces what the
session held. tests/integration/test_pg_retake_reads_images_after_lock.py
shows the interleaving on a real PostgreSQL.
"""

import pytest
from sqlalchemy import event


class _Statements:
    """The SQL statements executed on an engine while it is installed, in order."""

    def __init__(self, engine):
        self.engine = engine
        self.sql = []

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        self.sql.append(" ".join(statement.split()).upper())

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._record)


def _record_with_image(db_session, volume_id, status="captured"):
    from app.models.record import Record, RecordImage
    rec = Record(title="folio", status=status, capture_mode="single", collection_id=volume_id)
    db_session.add(rec)
    db_session.commit()
    img = RecordImage(record_id=rec.id, filename="a.jpg", file_path="/a.jpg",
                      format="jpg", role="single", is_current=True)
    db_session.add(img)
    db_session.commit()
    return rec, img


@pytest.fixture
def volume(db_session, test_project):
    from app.models.collection import Collection
    v = Collection(name="V", project_id=test_project.id, filename_stem="co-test-v")
    db_session.add(v)
    db_session.commit()
    return v


def _from_records_alone(sql):
    return " FROM RECORDS" in sql and "RECORD_IMAGES" not in sql


@pytest.mark.unit
def test_the_locked_loader_reads_the_images_in_a_statement_after_the_lock(db_session, volume):
    from app.api.records import _locked_record

    rec, _ = _record_with_image(db_session, volume.id)
    db_session.expire_all()
    with _Statements(db_session.get_bind()) as seen:
        _locked_record(db_session, rec.id)

    # The first statement is the locking select: the record row, no images.
    assert seen.sql, "nothing was read"
    assert _from_records_alone(seen.sql[0]), seen.sql[0]
    later = [s for s in seen.sql[1:] if "FROM RECORD_IMAGES" in s]
    assert later, seen.sql


@pytest.mark.unit
def test_the_locked_loader_replaces_the_images_the_session_held(db_session, volume):
    """Another transaction's retake, committed before this lock, is what the handler sees."""
    from sqlalchemy.orm import Session
    from app.api.records import _locked_record
    from app.models.record import Record, RecordImage

    rec, old = _record_with_image(db_session, volume.id)
    # The session holds the record with its images, as a request that read it earlier does.
    held = db_session.get(Record, rec.id)
    assert [img.id for img in held.images if img.is_current] == [old.id]

    with Session(bind=db_session.get_bind()) as other:
        other.get(RecordImage, old.id).is_current = False
        new = RecordImage(record_id=rec.id, filename="b.jpg", file_path="/b.jpg",
                          format="jpg", role="single", is_current=True)
        other.add(new)
        other.commit()
        new_id = new.id

    locked = _locked_record(db_session, rec.id)
    assert sorted(img.id for img in locked.images if img.is_current) == [new_id]


@pytest.mark.unit
def test_without_images_the_loader_reads_none(db_session, volume):
    from app.api.records import _locked_record

    rec, _ = _record_with_image(db_session, volume.id)
    db_session.expire_all()
    with _Statements(db_session.get_bind()) as seen:
        _locked_record(db_session, rec.id, with_images=False)

    assert not [s for s in seen.sql if "FROM RECORD_IMAGES" in s], seen.sql


@pytest.mark.unit
def test_the_fisqua_decisions_lock_reads_images_and_rejections_after_it(db_session, volume):
    from sqlalchemy.orm import Session
    from app.core.fisqua_decisions import _lock_records
    from app.models.record import Record, RecordImage

    rec, old = _record_with_image(db_session, volume.id)
    held = db_session.get(Record, rec.id)
    assert len(held.images) == 1
    with Session(bind=db_session.get_bind()) as other:
        other.get(RecordImage, old.id).is_current = False
        other.add(RecordImage(record_id=rec.id, filename="b.jpg", file_path="/b.jpg",
                              format="jpg", role="single", is_current=True))
        other.commit()

    with _Statements(db_session.get_bind()) as seen:
        locked = _lock_records(db_session, [rec.id])

    assert _from_records_alone(seen.sql[0]), seen.sql[0]
    assert [s for s in seen.sql[1:] if "FROM RECORD_IMAGES" in s], seen.sql
    assert [s for s in seen.sql[1:] if "FROM RECORD_REJECTIONS" in s], seen.sql
    assert len([img for img in locked[rec.id].images if img.is_current]) == 1
    assert len(locked[rec.id].images) == 2


@pytest.mark.unit
def test_the_bulk_status_lock_reads_the_images_after_it(client, db_session, volume):
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User

    rec, _ = _record_with_image(db_session, volume.id, status="in_review")
    app.dependency_overrides[get_current_user] = lambda: User(
        username="rev", email="rev@example.com", hashed_password="x", role="admin", is_active=True)
    db_session.expire_all()
    with _Statements(db_session.get_bind()) as seen:
        resp = client.post("/records/bulk-status", json={"record_ids": [rec.id], "status": "approved"})
    assert resp.status_code == 200, resp.text

    locking = next(i for i, s in enumerate(seen.sql) if " FROM RECORDS" in s and "WHERE RECORDS.ID IN" in s)
    assert "RECORD_IMAGES" not in seen.sql[locking], seen.sql[locking]
    assert [s for s in seen.sql[locking + 1:] if "FROM RECORD_IMAGES" in s], seen.sql
