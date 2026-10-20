#!/usr/bin/env python3
"""While an export is assembling a bag, the collection it describes holds still.

The gap this closes: a bag's metadata.json asserts the collection's title,
description, type and archival metadata, its project, and for every record the
sequence, title, description, author, date, status and capture mode, with the
filenames and dimensions of its current images. The version before this one
compared a snapshot of (status, current image ids) at the gate against the same
tuple once the payload was planned -- two of those fields -- and spent the whole
assembly, integrity checks included, before noticing a change.

The ruling of 2026-10-20 is to serialise. The rule is not written into the
sixteen routes that can change a bag field, because the last round of this work
defeated a sixteen-places rule in two calls through the two places nobody had
edited (see assert_movable in app/api/records.py). It is one before_flush
listener on SessionLocal, which no route can be written without, plus an early
refusal in the five routes that change the filesystem before they commit.

So the tests here are about the three model classes and the five routes, not
about an enumeration of handlers.

Run with: python -m pytest tests/unit/test_export_serialisation.py
"""

import pytest

from app.core.export_jobs import ExportJob, export_jobs


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _user(username, role):
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, username, role):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


def _project(db_session, name="P"):
    from app.models.project import Project
    proj = db_session.query(Project).filter(Project.name == name).first()
    if proj is None:
        proj = Project(name=name)
        db_session.add(proj)
        db_session.commit()
        db_session.refresh(proj)
    return proj


def _collection(db_session, name):
    from app.models.collection import Collection
    col = Collection(name=name, project_id=_project(db_session).id)
    db_session.add(col)
    db_session.commit()
    db_session.refresh(col)
    return col


def _record(db_session, col, status="approved", title="f.jpg"):
    from app.models.record import Record, RecordImage
    rec = Record(title=title, collection_id=col.id, status=status,
                 capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)
    img = RecordImage(record_id=rec.id, filename=title, file_path=f"/x/{title}",
                      format="jpeg", is_current=True, sequence=0)
    db_session.add(img)
    db_session.commit()
    db_session.refresh(rec)
    return rec


@pytest.fixture
def hold(monkeypatch):
    """Put a collection in the state a running export leaves it in.

    The registry's own active_for() does the deciding, so this exercises the
    real predicate rather than a stand-in for it.
    """
    jobs: dict = {}
    active: dict = {}
    monkeypatch.setattr(export_jobs, "_jobs", jobs)
    monkeypatch.setattr(export_jobs, "_active_by_collection", active)

    def _hold(collection_id, state="running"):
        job = ExportJob(collection_id)
        job.state = state
        jobs[job.id] = job
        active[collection_id] = job.id
        return job

    return _hold


def _refused(excinfo):
    detail = excinfo.value.detail
    assert excinfo.value.status_code == 409, detail
    assert "being exported" in str(detail), detail


# ---------------------------------------------------------------------------
# the wiring
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_listener_is_registered_on_the_session_factory():
    """Without this the rest of the file passes against nothing.

    The guard is one listener, so if it is not attached there is no rule at
    all -- and the tests rebind SessionLocal rather than replacing it, which is
    what makes a listener registered at import time apply here too.
    """
    from sqlalchemy import event
    from app.core.db import SessionLocal

    assert event.contains(SessionLocal, "before_flush",
                          _registered_listener()), \
        "no before_flush listener on SessionLocal: nothing is guarded"


def _registered_listener():
    from app.core import db as core_db
    return core_db._refuse_writes_to_an_exporting_collection


# ---------------------------------------------------------------------------
# the three model classes, which is what makes route enumeration unnecessary
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_record_status_change_is_refused(db_session, hold):
    from fastapi import HTTPException
    col = _collection(db_session, "held-status")
    rec = _record(db_session, col)
    hold(col.id)

    rec.status = "rejected"
    with pytest.raises(HTTPException) as excinfo:
        db_session.commit()
    _refused(excinfo)


@pytest.mark.unit
def test_a_record_metadata_change_is_refused(db_session, hold):
    """The snapshot this replaced did not look at titles, and the bag does."""
    from fastapi import HTTPException
    col = _collection(db_session, "held-title")
    rec = _record(db_session, col)
    hold(col.id)

    rec.title = "a different title"
    with pytest.raises(HTTPException) as excinfo:
        db_session.commit()
    _refused(excinfo)


@pytest.mark.unit
def test_a_reorder_is_refused(db_session, hold):
    """Sequence is in the metadata and sets the order of the records array."""
    from fastapi import HTTPException
    col = _collection(db_session, "held-order")
    first = _record(db_session, col, title="a.jpg")
    second = _record(db_session, col, title="b.jpg")
    hold(col.id)

    first.sequence, second.sequence = 1, 0
    with pytest.raises(HTTPException) as excinfo:
        db_session.commit()
    _refused(excinfo)


@pytest.mark.unit
def test_moving_a_record_out_of_a_held_collection_is_refused(db_session, hold):
    from fastapi import HTTPException
    held_col = _collection(db_session, "held-source")
    other = _collection(db_session, "free-target")
    rec = _record(db_session, held_col)
    hold(held_col.id)

    rec.collection_id = other.id
    with pytest.raises(HTTPException) as excinfo:
        db_session.commit()
    _refused(excinfo)


@pytest.mark.unit
def test_moving_a_record_into_a_held_collection_is_refused(db_session, hold):
    """The other end of a move, which a check on the old parent alone misses."""
    from fastapi import HTTPException
    free = _collection(db_session, "free-source")
    held_col = _collection(db_session, "held-target")
    _record(db_session, held_col, title="sibling.jpg")
    rec = _record(db_session, free, title="incomer.jpg")
    hold(held_col.id)

    rec.collection_id = held_col.id
    with pytest.raises(HTTPException) as excinfo:
        db_session.commit()
    _refused(excinfo)


@pytest.mark.unit
def test_deleting_a_record_is_refused(db_session, hold):
    from fastapi import HTTPException
    col = _collection(db_session, "held-delete")
    rec = _record(db_session, col)
    hold(col.id)

    db_session.delete(rec)
    with pytest.raises(HTTPException) as excinfo:
        db_session.commit()
    _refused(excinfo)


@pytest.mark.unit
def test_a_retake_is_refused(db_session, hold):
    """A new current image changes the filenames the bag lists."""
    from fastapi import HTTPException
    from app.models.record import RecordImage
    col = _collection(db_session, "held-retake")
    rec = _record(db_session, col)
    hold(col.id)

    db_session.add(RecordImage(record_id=rec.id, filename="retake.jpg",
                               file_path="/x/retake.jpg", format="jpeg",
                               is_current=True, sequence=1))
    with pytest.raises(HTTPException) as excinfo:
        db_session.commit()
    _refused(excinfo)


@pytest.mark.unit
def test_an_image_change_is_refused_through_its_record(db_session, hold):
    """An image carries no collection_id, so the guard resolves it upwards."""
    from fastapi import HTTPException
    from app.models.record import RecordImage
    col = _collection(db_session, "held-image")
    rec = _record(db_session, col)
    img = db_session.query(RecordImage).filter(
        RecordImage.record_id == rec.id).first()
    hold(col.id)

    img.sequence = 7
    with pytest.raises(HTTPException) as excinfo:
        db_session.commit()
    _refused(excinfo)


@pytest.mark.unit
def test_a_collection_metadata_change_is_refused(db_session, hold):
    """The bag names the collection's own title and archival metadata."""
    from fastapi import HTTPException
    col = _collection(db_session, "held-collection")
    _record(db_session, col)
    hold(col.id)

    col.description = "edited while the bag was being written"
    with pytest.raises(HTTPException) as excinfo:
        db_session.commit()
    _refused(excinfo)


# ---------------------------------------------------------------------------
# what must NOT be refused
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_unheld_collection_is_untouched_by_the_guard(db_session, hold):
    """The guard is per collection, not a global pause on the appliance."""
    held_col = _collection(db_session, "busy")
    free = _collection(db_session, "quiet")
    _record(db_session, held_col)
    rec = _record(db_session, free)
    hold(held_col.id)

    rec.title = "edited freely"
    db_session.commit()
    db_session.refresh(rec)
    assert rec.title == "edited freely"


@pytest.mark.unit
def test_a_finished_export_releases_the_collection(db_session, hold):
    """`done` and `failed` are not holds; only queued and running are."""
    col = _collection(db_session, "finished")
    rec = _record(db_session, col)
    hold(col.id, state="done")

    rec.title = "edited after the export finished"
    db_session.commit()
    db_session.refresh(rec)
    assert rec.title == "edited after the export finished"


@pytest.mark.unit
def test_a_queued_export_already_holds_the_collection(db_session, hold):
    """The registry registers the job before the thread runs, so `queued`
    counts: otherwise the window between start() and the first read is open."""
    from fastapi import HTTPException
    col = _collection(db_session, "queued")
    rec = _record(db_session, col)
    hold(col.id, state="queued")

    rec.title = "edited while queued"
    with pytest.raises(HTTPException) as excinfo:
        db_session.commit()
    _refused(excinfo)


@pytest.mark.unit
def test_an_annotation_is_deliberately_left_open(db_session, hold):
    """Nothing in the bag reads annotations, so cataloguing carries on."""
    from app.models.record import RecordAnnotation
    col = _collection(db_session, "held-annotations")
    rec = _record(db_session, col)
    hold(col.id)

    db_session.add(RecordAnnotation(record_id=rec.id, note="a note",
                                    created_by="someone"))
    db_session.commit()
    assert db_session.query(RecordAnnotation).filter(
        RecordAnnotation.record_id == rec.id).count() == 1


@pytest.mark.unit
def test_the_export_worker_itself_is_not_blocked(db_session, hold):
    """The worker's session reads and never writes, so the guard cannot
    deadlock the thing it protects. Asserted against the module rather than
    trusted: a db.add() added to the worker later would break this."""
    import inspect
    from app.core import export_service

    source = inspect.getsource(export_service)
    for forbidden in ("db.add(", "db.commit(", "db.delete(", "db.flush("):
        assert forbidden not in source, (
            f"export_service now writes ({forbidden}), so its own session would "
            f"hit the guard that holds its collection"
        )


# ---------------------------------------------------------------------------
# the five routes that change the filesystem before they commit
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_renumbering_is_refused_before_a_single_file_is_renamed(
        client, db_session, hold, monkeypatch):
    from app.api import collections as collections_api
    col = _collection(db_session, "held-renumber")
    _record(db_session, col)
    hold(col.id)

    renamed = []
    monkeypatch.setattr(collections_api, "renumber_collection_images",
                        lambda db, collection: renamed.append(collection.id))

    response = _client_as(client, "op", "admin").post(
        f"/collections/{col.id}/images/renumber")

    assert response.status_code == 409, response.text
    assert "being exported" in response.text
    assert renamed == [], "files were renamed before the refusal"


@pytest.mark.unit
def test_deleting_a_collection_is_refused_before_remove_tree(
        client, db_session, hold, monkeypatch):
    from app.api import collections as collections_api
    col = _collection(db_session, "held-rmtree")
    hold(col.id)

    removed = []
    monkeypatch.setattr(collections_api, "remove_tree",
                        lambda path: removed.append(path))

    response = _client_as(client, "op", "admin").delete(f"/collections/{col.id}")

    assert response.status_code == 409, response.text
    assert removed == [], "the collection directory was removed before the refusal"


@pytest.mark.unit
def test_deleting_a_record_is_refused_before_its_masters_are_unlinked(
        client, db_session, hold, monkeypatch):
    from app.api import records as records_api
    col = _collection(db_session, "held-recdel")
    rec = _record(db_session, col, status="captured")
    hold(col.id)

    unlinked = []
    monkeypatch.setattr(records_api, "_unlink_image_file_and_sidecar",
                        lambda *args, **kwargs: unlinked.append(args))

    response = _client_as(client, "op", "admin").delete(f"/records/{rec.id}")

    assert response.status_code == 409, response.text
    assert unlinked == [], "masters were unlinked before the refusal"


@pytest.mark.unit
def test_deleting_an_image_is_refused_before_its_master_is_unlinked(
        client, db_session, hold, monkeypatch):
    from app.api import records as records_api
    from app.models.record import RecordImage
    col = _collection(db_session, "held-imgdel")
    rec = _record(db_session, col, status="captured")
    img = db_session.query(RecordImage).filter(
        RecordImage.record_id == rec.id).first()
    hold(col.id)

    unlinked = []
    monkeypatch.setattr(records_api, "_unlink_image_file_and_sidecar",
                        lambda *args, **kwargs: unlinked.append(args))

    response = _client_as(client, "op", "admin").delete(f"/records/images/{img.id}")

    assert response.status_code == 409, response.text
    assert unlinked == [], "the master was unlinked before the refusal"


# ---------------------------------------------------------------------------
# the registry's own half: one export per collection
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_second_export_does_not_stack_on_the_first():
    """A retry has to return the running job, not double the disk pressure."""
    import threading

    registry_jobs, registry_active = export_jobs._jobs, export_jobs._active_by_collection
    export_jobs._jobs, export_jobs._active_by_collection = {}, {}
    release = threading.Event()
    try:
        first = export_jobs.start(4242, lambda job: release.wait(10))
        second = export_jobs.start(4242, lambda job: release.wait(10))
        assert second is first, "a second export was started for one collection"
        assert export_jobs.active_for(4242) is first
    finally:
        release.set()
        export_jobs._jobs, export_jobs._active_by_collection = registry_jobs, registry_active


@pytest.mark.unit
def test_the_hold_is_taken_by_start_before_the_work_begins(db_session):
    """The real path, not the fixture: if the registration happened inside the
    thread instead, a write arriving first would slip past the guard."""
    import threading
    from fastapi import HTTPException

    col = _collection(db_session, "real-hold")
    rec = _record(db_session, col)

    registry_jobs, registry_active = export_jobs._jobs, export_jobs._active_by_collection
    export_jobs._jobs, export_jobs._active_by_collection = {}, {}
    release = threading.Event()
    try:
        export_jobs.start(col.id, lambda job: release.wait(10))
        rec.title = "edited during a real export"
        with pytest.raises(HTTPException) as excinfo:
            db_session.commit()
        _refused(excinfo)
    finally:
        release.set()
        export_jobs._jobs, export_jobs._active_by_collection = registry_jobs, registry_active
