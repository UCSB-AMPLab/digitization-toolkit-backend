#!/usr/bin/env python3
"""NEH-260 follow-ups: the two ways round the handover rule.

An adversarial round found both. Neither needs a race.

  1. The ruling of 2026-10-17 is that taking a handover back applies to a whole
     collection and never to one record, so a reviewer cannot have a single item
     pulled out from under them. But moving a record between collections carried
     no status check, so three ordinary calls did it: move R into collection B,
     take B back, move R home. R returned as the operator's while its siblings
     stayed in the queue.
  2. Exporting is a request and then a worker. The request checked the workflow
     state; the worker reloaded the records and did not. A collection taken back
     in between still exported, and the bag's metadata described the work as
     `captured`.

Run with: python -m pytest tests/unit/test_handover_boundaries.py
"""

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


def _record(db_session, col, status="captured", name="f.jpg", with_image=True):
    from app.models.record import Record, RecordImage
    rec = Record(title=name, collection_id=col.id, status=status, capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)
    if with_image:
        db_session.add(RecordImage(record_id=rec.id, filename=name,
                                   file_path=f"/{name}", format="jpg",
                                   role="single", is_current=True))
        db_session.commit()
        db_session.refresh(rec)
    return rec


# ---------------------------------------------------------------------------
# Moving one record
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_captured_record_can_be_moved(client, db_session):
    a, b = _collection(db_session, "A"), _collection(db_session, "B")
    rec = _record(db_session, a)
    api = _client_as(client, "op", "operator")

    resp = api.patch(f"/records/{rec.id}", json={"collection_id": b.id})
    assert resp.status_code == 200, resp.text
    db_session.refresh(rec)
    assert rec.collection_id == b.id


@pytest.mark.unit
@pytest.mark.parametrize("status", ["in_review", "approved", "rejected"])
def test_a_handed_over_record_cannot_be_moved(client, db_session, status):
    a, b = _collection(db_session, "A"), _collection(db_session, "B")
    rec = _record(db_session, a, status=status)
    api = _client_as(client, "op", "operator")

    resp = api.patch(f"/records/{rec.id}", json={"collection_id": b.id})
    assert resp.status_code == 409, resp.text
    db_session.refresh(rec)
    assert rec.collection_id == a.id, "the move must not have happened"


@pytest.mark.unit
def test_the_three_call_sequence_no_longer_withdraws_one_record(client, db_session):
    """The reproduction, end to end: it has to fail at the first step now."""
    a, b = _collection(db_session, "A"), _collection(db_session, "B")
    r = _record(db_session, a, name="r.jpg")
    s = _record(db_session, a, name="s.jpg")
    api = _client_as(client, "op", "operator")

    assert api.post(f"/collections/{a.id}/promote").status_code == 200
    db_session.refresh(r)
    db_session.refresh(s)
    assert (r.status, s.status) == ("in_review", "in_review")

    # Step one of the old sequence.
    assert api.patch(f"/records/{r.id}", json={"collection_id": b.id}).status_code == 409
    db_session.refresh(r)
    db_session.refresh(s)
    assert (r.status, s.status) == ("in_review", "in_review")
    assert r.collection_id == a.id


@pytest.mark.unit
def test_cataloguing_a_record_under_review_is_still_allowed(client, db_session):
    """Only a move is refused. Retitling a queued record is ordinary work."""
    a = _collection(db_session, "A")
    rec = _record(db_session, a, status="in_review")
    api = _client_as(client, "op", "operator")

    resp = api.patch(f"/records/{rec.id}", json={"title": "Tomo 7, folio 12r"})
    assert resp.status_code == 200, resp.text
    db_session.refresh(rec)
    assert rec.title == "Tomo 7, folio 12r"


@pytest.mark.unit
def test_resending_the_same_parent_is_not_a_move(client, db_session):
    """A PATCH that changes nothing about the parent must not be refused."""
    a = _collection(db_session, "A")
    rec = _record(db_session, a, status="in_review")
    api = _client_as(client, "op", "operator")

    resp = api.patch(f"/records/{rec.id}", json={"collection_id": a.id})
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Moving a whole collection's records
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_move_records_refuses_a_handed_over_set(client, db_session):
    a, b = _collection(db_session, "A"), _collection(db_session, "B")
    captured = _record(db_session, a, name="wip.jpg")
    queued = _record(db_session, a, status="in_review", name="queued.jpg")
    api = _client_as(client, "op", "operator")

    resp = api.post(f"/collections/{a.id}/move-records?target_collection_id={b.id}")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["blocking_record_ids"] == [queued.id]
    for rec in (captured, queued):
        db_session.refresh(rec)
        assert rec.collection_id == a.id, "all or nothing"


# ---------------------------------------------------------------------------
# The export worker
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_workflow_gate_reports_handover_before_approval():
    from types import SimpleNamespace

    from app.core.export_service import workflow_blockers

    recs = [SimpleNamespace(id=1, status="captured"),
            SimpleNamespace(id=2, status="in_review")]
    message, ids = workflow_blockers(recs)
    assert "handed over" in message
    assert ids == [1]

    recs = [SimpleNamespace(id=2, status="in_review")]
    message, ids = workflow_blockers(recs)
    assert "not approved yet" in message
    assert ids == [2]

    assert workflow_blockers([SimpleNamespace(id=3, status="approved")]) is None


@pytest.mark.unit
def test_the_worker_refuses_a_collection_taken_back_after_the_request(db_session, tmp_path):
    """The gap the round exploited: request while approved, take back, let the
    worker run. It used to build a bag whose metadata said `captured`."""
    from app.core.export_jobs import ExportError, ExportJob
    from app.core.export_service import _do_export

    col = _collection(db_session, "A")
    rec = _record(db_session, col, status="approved", name="ok.jpg")

    # What the take-back does, after the request-time gate has already passed.
    rec.status = "captured"
    db_session.commit()

    job = ExportJob(col.id)
    with pytest.raises(ExportError) as excinfo:
        _do_export(db_session, job, col.id)
    assert "handed over" in str(excinfo.value)
    assert excinfo.value.detail["blocking_record_ids"] == [rec.id]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
