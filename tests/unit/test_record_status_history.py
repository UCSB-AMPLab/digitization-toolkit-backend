#!/usr/bin/env python3
"""record_status_changes: every change of a record's status is dated.

records.status says only where a record is now, so the time of an approval
was not stored anywhere. Each writer of the status now adds a row naming the
old status, the new one, who changed it and when. These tests go through the
endpoints, one per writer, and check the row each one leaves.

Run with: python -m pytest tests/unit/test_record_status_history.py
"""

import pytest

from tests.unit.test_capture_birth_and_retake import (
    _capture, _capture_dual, _install_capture_fakes, volume,  # noqa: F401
)


def _user(username, role):
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, username, role):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


def _collection(db_session, name="Tomo 1"):
    from app.models.project import Project
    from app.models.collection import Collection
    proj = db_session.query(Project).filter(Project.name == "P").first()
    if proj is None:
        proj = Project(name="P")
        db_session.add(proj)
        db_session.commit()
    col = Collection(name=name, project_id=proj.id)
    db_session.add(col)
    db_session.commit()
    db_session.refresh(col)
    return col


def _record(db_session, status="captured", col=None, name="f.jpg"):
    from app.models.record import Record, RecordImage
    rec = Record(title=name, status=status, capture_mode="single",
                 collection_id=col.id if col else None)
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)
    img = RecordImage(record_id=rec.id, filename=name, file_path=f"/{name}",
                      format="jpg", role="single", is_current=True)
    db_session.add(img)
    db_session.commit()
    db_session.refresh(rec)
    return rec, img


def _changes(db_session, rec):
    from app.models.record import RecordStatusChange
    db_session.expire_all()
    return [
        (c.from_status, c.to_status, c.changed_by)
        for c in db_session.query(RecordStatusChange)
        .filter(RecordStatusChange.record_id == rec.id)
        .order_by(RecordStatusChange.id)
    ]


# ---------------------------------------------------------------------------
# One row per writer
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_approval_is_dated(client, db_session):
    from app.models.record import RecordStatusChange
    rec, _ = _record(db_session, status="in_review")
    api = _client_as(client, "rev", "reviewer")

    resp = api.patch(f"/records/{rec.id}/status", json={"status": "approved"})
    assert resp.status_code == 200, resp.text

    assert _changes(db_session, rec) == [("in_review", "approved", "rev")]
    row = db_session.query(RecordStatusChange).filter_by(record_id=rec.id).one()
    assert row.changed_at is not None


@pytest.mark.unit
def test_undoing_an_approval_adds_a_row_and_keeps_the_first(client, db_session):
    rec, _ = _record(db_session, status="in_review")
    api = _client_as(client, "rev", "reviewer")

    assert api.patch(f"/records/{rec.id}/status", json={"status": "approved"}).status_code == 200
    assert api.patch(f"/records/{rec.id}/status", json={"status": "in_review"}).status_code == 200

    assert _changes(db_session, rec) == [
        ("in_review", "approved", "rev"),
        ("approved", "in_review", "rev"),
    ]


@pytest.mark.unit
def test_a_refused_or_unchanged_status_writes_nothing(client, db_session):
    rec, _ = _record(db_session, status="in_review")

    op = _client_as(client, "op", "operator")
    assert op.patch(f"/records/{rec.id}/status", json={"status": "approved"}).status_code == 403
    rev = _client_as(client, "rev", "reviewer")
    assert rev.patch(f"/records/{rec.id}/status", json={"status": "in_review"}).status_code == 200

    assert _changes(db_session, rec) == []


@pytest.mark.unit
def test_bulk_status_dates_each_record_it_moves(client, db_session):
    a, _ = _record(db_session, status="in_review", name="a.jpg")
    b, _ = _record(db_session, status="captured", name="b.jpg")
    api = _client_as(client, "rev", "reviewer")

    resp = api.post("/records/bulk-status",
                    json={"record_ids": [a.id, b.id], "status": "approved"})
    assert resp.status_code == 200, resp.text

    assert _changes(db_session, a) == [("in_review", "approved", "rev")]
    assert _changes(db_session, b) == []


@pytest.mark.unit
def test_rejection_is_dated(client, db_session):
    rec, _ = _record(db_session, status="in_review")
    api = _client_as(client, "rev", "reviewer")

    resp = api.post(f"/records/{rec.id}/reject", json={"predefined_reason": "blur"})
    assert resp.status_code == 200, resp.text

    assert _changes(db_session, rec) == [("in_review", "rejected", "rev")]


@pytest.mark.unit
def test_retaking_a_rejected_record_is_dated(client, db_session, test_project, volume,
                                              monkeypatch, tmp_path):
    _install_capture_fakes(monkeypatch, tmp_path)
    rec, _ = _record(db_session, status="rejected", col=volume)
    api = _client_as(client, "op", "operator")

    resp = _capture(api, test_project.name, collection_id=volume.id, record_id=rec.id)
    assert resp.status_code == 200, resp.text

    assert _changes(db_session, rec) == [("rejected", "in_review", "op")]


@pytest.mark.unit
def test_retaking_a_rejected_dual_record_is_dated(client, db_session, test_project, volume,
                                                   monkeypatch, tmp_path):
    _install_capture_fakes(monkeypatch, tmp_path)
    rec, _ = _record(db_session, status="rejected", col=volume)
    rec.capture_mode = "dual"
    db_session.commit()
    api = _client_as(client, "op", "operator")

    resp = _capture_dual(api, test_project.name, collection_id=volume.id, record_id=rec.id)
    assert resp.status_code == 200, resp.text

    assert _changes(db_session, rec) == [("rejected", "in_review", "op")]


@pytest.mark.unit
def test_undoing_a_rejection_is_dated(client, db_session):
    rec, _ = _record(db_session, status="rejected")
    api = _client_as(client, "rev", "reviewer")

    resp = api.patch(f"/records/{rec.id}/status", json={"status": "in_review"})
    assert resp.status_code == 200, resp.text

    assert _changes(db_session, rec) == [("rejected", "in_review", "rev")]


@pytest.mark.unit
def test_preparing_a_rejected_record_for_retake_is_dated(client, db_session):
    rec, _ = _record(db_session, status="rejected")
    api = _client_as(client, "op", "operator")

    resp = api.patch(f"/records/{rec.id}/status", json={"status": "captured"})
    assert resp.status_code == 200, resp.text

    assert _changes(db_session, rec) == [("rejected", "captured", "op")]


@pytest.mark.unit
def test_emptying_an_approved_record_is_dated(client, db_session):
    rec, img = _record(db_session, status="approved")
    api = _client_as(client, "adm", "admin")

    resp = api.delete(f"/records/images/{img.id}")
    assert resp.status_code == 200, resp.text

    assert _changes(db_session, rec) == [("approved", "in_review", "adm")]


@pytest.mark.unit
def test_promotion_dates_every_record_it_hands_over(client, db_session):
    col = _collection(db_session)
    recs = [_record(db_session, col=col, name=f"f{i}.jpg")[0] for i in range(3)]
    api = _client_as(client, "op", "operator")

    assert api.post(f"/collections/{col.id}/promote").status_code == 200

    for rec in recs:
        assert _changes(db_session, rec) == [("captured", "in_review", "op")]


@pytest.mark.unit
def test_take_back_dates_only_the_records_it_moved(client, db_session):
    """The approved record is excluded by the locked SELECT already, so this
    cannot tell the RETURNING list from the locked ids. The race RETURNING
    guards against needs two Postgres sessions, which SQLite cannot give."""
    col = _collection(db_session)
    waiting, _ = _record(db_session, status="in_review", col=col, name="w.jpg")
    approved, _ = _record(db_session, status="approved", col=col, name="a.jpg")
    api = _client_as(client, "op", "operator")

    resp = api.post(f"/collections/{col.id}/take-back")
    assert resp.status_code == 200, resp.text
    assert resp.json()["taken_back"] == 1

    assert _changes(db_session, waiting) == [("in_review", "captured", "op")]
    assert _changes(db_session, approved) == []


# ---------------------------------------------------------------------------
# Reading it back
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_collection_history_is_newest_first_and_scoped(client, db_session):
    col = _collection(db_session)
    other = _collection(db_session, name="Tomo 2")
    rec, _ = _record(db_session, status="in_review", col=col, name="mine.jpg")
    elsewhere, _ = _record(db_session, status="in_review", col=other, name="theirs.jpg")
    rev = _client_as(client, "rev", "reviewer")
    rev.patch(f"/records/{rec.id}/status", json={"status": "approved"})
    rev.patch(f"/records/{rec.id}/status", json={"status": "in_review"})
    rev.patch(f"/records/{elsewhere.id}/status", json={"status": "approved"})

    resp = rev.get(f"/collections/{col.id}/status-changes")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [(r["record_id"], r["from_status"], r["to_status"]) for r in rows] == [
        (rec.id, "approved", "in_review"),
        (rec.id, "in_review", "approved"),
    ]
    assert all(r["changed_by"] == "rev" and r["changed_at"] for r in rows)


@pytest.mark.unit
def test_collection_history_of_a_missing_collection_is_404(client, db_session):
    api = _client_as(client, "rev", "reviewer")
    assert api.get("/collections/999999/status-changes").status_code == 404


@pytest.mark.unit
def test_history_rows_refuse_an_unknown_status(db_session):
    from sqlalchemy.exc import IntegrityError
    from app.models.record import RecordStatusChange
    rec, _ = _record(db_session)
    db_session.add(RecordStatusChange(record_id=rec.id, from_status="captured",
                                      to_status="bogus", changed_by="x"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
@pytest.mark.parametrize("role", ["operator", "admin"])
def test_collection_history_is_readable_by_every_role(client, db_session, role):
    col = _collection(db_session)
    rec, _ = _record(db_session, status="in_review", col=col)
    _client_as(client, "rev", "reviewer").patch(
        f"/records/{rec.id}/status", json={"status": "approved"})

    resp = _client_as(client, "someone", role).get(f"/collections/{col.id}/status-changes")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1


@pytest.mark.unit
def test_a_moved_record_takes_its_history_with_it(client, db_session):
    a, b = _collection(db_session, "A"), _collection(db_session, "B")
    rec, _ = _record(db_session, status="rejected", col=a)
    api = _client_as(client, "op", "operator")
    assert api.patch(f"/records/{rec.id}/status", json={"status": "captured"}).status_code == 200
    assert api.patch(f"/records/{rec.id}", json={"collection_id": b.id}).status_code == 200

    assert api.get(f"/collections/{a.id}/status-changes").json() == []
    rows = api.get(f"/collections/{b.id}/status-changes").json()
    assert [(r["record_id"], r["to_status"]) for r in rows] == [(rec.id, "captured")]


@pytest.mark.unit
def test_deleting_a_record_keeps_its_history(client, db_session):
    """The rows stay, so the day's count keeps the work; record_id goes NULL."""
    from app.models.record import RecordStatusChange
    rec, _ = _record(db_session, status="in_review")
    api = _client_as(client, "adm", "admin")
    assert api.patch(f"/records/{rec.id}/status", json={"status": "approved"}).status_code == 200
    assert api.patch(f"/records/{rec.id}/status", json={"status": "in_review"}).status_code == 200
    rec_id = rec.id
    db_session.expire_all()

    resp = api.delete(f"/records/{rec_id}")
    assert resp.status_code in (200, 204), resp.text

    db_session.expire_all()
    rows = db_session.query(RecordStatusChange).order_by(RecordStatusChange.id).all()
    assert [(r.record_id, r.from_status, r.to_status, r.changed_by) for r in rows] == [
        (None, "in_review", "approved", "adm"),
        (None, "approved", "in_review", "adm"),
    ]


@pytest.mark.unit
def test_a_deleted_records_history_stays_listed_under_its_collection(client, db_session):
    col = _collection(db_session)
    other = _collection(db_session, name="Tomo 2")
    rec, _ = _record(db_session, status="in_review", col=col, name="gone.jpg")
    api = _client_as(client, "adm", "admin")
    assert api.patch(f"/records/{rec.id}/status", json={"status": "approved"}).status_code == 200
    db_session.expire_all()
    assert api.delete(f"/records/{rec.id}").status_code in (200, 204)

    rows = api.get(f"/collections/{col.id}/status-changes").json()
    assert [(r["record_id"], r["collection_id"], r["to_status"]) for r in rows] == [
        (None, col.id, "approved"),
    ]
    assert api.get(f"/collections/{other.id}/status-changes").json() == []
