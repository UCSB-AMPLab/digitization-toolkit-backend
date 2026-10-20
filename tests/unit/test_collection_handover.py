#!/usr/bin/env python3
"""NEH-260: handing a collection over for review, and taking it back.

An operator promotes a tomo when she has finished it, so the act is per
collection while the transition stays per record. Submitted state is derived
rather than stored: a collection has been handed over when none of its records
is `captured`.

Run with: python -m pytest tests/unit/test_collection_handover.py
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


def _record_with_image(db_session, col, status="captured", name="f.jpg"):
    from app.models.record import Record, RecordImage
    rec = Record(title=name, collection_id=col.id, status=status, capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)
    db_session.add(RecordImage(record_id=rec.id, filename=name, file_path=f"/{name}",
                               format="jpg", role="single", is_current=True))
    db_session.commit()
    db_session.refresh(rec)
    return rec


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_promote_moves_every_captured_record(client, db_session):
    col = _collection(db_session)
    recs = [_record_with_image(db_session, col, name=f"f{i}.jpg") for i in range(3)]
    api = _client_as(client, "op", "operator")

    resp = api.post(f"/collections/{col.id}/promote")
    assert resp.status_code == 200, resp.text
    assert resp.json()["promoted"] == 3
    for rec in recs:
        db_session.refresh(rec)
        assert rec.status == "in_review"


@pytest.mark.unit
def test_promote_leaves_records_a_reviewer_has_already_ruled_on(client, db_session):
    col = _collection(db_session)
    fresh = _record_with_image(db_session, col, name="fresh.jpg")
    approved = _record_with_image(db_session, col, status="approved", name="ok.jpg")
    rejected = _record_with_image(db_session, col, status="rejected", name="bad.jpg")
    api = _client_as(client, "op", "operator")

    resp = api.post(f"/collections/{col.id}/promote")
    assert resp.status_code == 200, resp.text
    assert resp.json()["promoted"] == 1
    for rec, expected in ((fresh, "in_review"), (approved, "approved"), (rejected, "rejected")):
        db_session.refresh(rec)
        assert rec.status == expected


@pytest.mark.unit
def test_promoting_an_already_handed_over_collection_is_a_no_op(client, db_session):
    col = _collection(db_session)
    _record_with_image(db_session, col, status="in_review")
    api = _client_as(client, "op", "operator")

    resp = api.post(f"/collections/{col.id}/promote")
    assert resp.status_code == 200, resp.text
    assert resp.json()["promoted"] == 0


@pytest.mark.unit
def test_promote_is_all_or_nothing(client, db_session):
    """One record that cannot go stops the handover and names itself.

    A partial promotion would leave the collection unsubmitted — submitted
    means no captured records — while reporting success.
    """
    col = _collection(db_session)
    ok = _record_with_image(db_session, col, name="ok.jpg")
    empty = _record_with_image(db_session, col, name="empty.jpg")
    # Strip the image rather than deleting through the API: the point is the
    # state, not the route to it.
    from app.models.record import RecordImage
    db_session.query(RecordImage).filter(RecordImage.record_id == empty.id).delete()
    db_session.commit()
    api = _client_as(client, "op", "operator")

    resp = api.post(f"/collections/{col.id}/promote")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["blocking_record_ids"] == [empty.id]
    db_session.refresh(ok)
    assert ok.status == "captured", "nothing may move when the handover is refused"


@pytest.mark.unit
def test_promote_is_closed_to_a_reviewer(client, db_session):
    col = _collection(db_session)
    _record_with_image(db_session, col)
    api = _client_as(client, "rev", "reviewer")

    resp = api.post(f"/collections/{col.id}/promote")
    assert resp.status_code == 403, resp.text


@pytest.mark.unit
def test_promote_unknown_collection_is_404(client, db_session):
    api = _client_as(client, "op", "operator")
    assert api.post("/collections/9999/promote").status_code == 404


# ---------------------------------------------------------------------------
# Take-back
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_take_back_returns_in_review_records_to_the_operator(client, db_session):
    col = _collection(db_session)
    rec = _record_with_image(db_session, col)
    api = _client_as(client, "op", "operator")

    assert api.post(f"/collections/{col.id}/promote").status_code == 200
    resp = api.post(f"/collections/{col.id}/take-back")
    assert resp.status_code == 200, resp.text
    assert resp.json()["taken_back"] == 1
    db_session.refresh(rec)
    assert rec.status == "captured"


@pytest.mark.unit
def test_take_back_leaves_approved_and_rejected_records_alone(client, db_session):
    col = _collection(db_session)
    approved = _record_with_image(db_session, col, status="approved", name="ok.jpg")
    rejected = _record_with_image(db_session, col, status="rejected", name="bad.jpg")
    in_review = _record_with_image(db_session, col, status="in_review", name="queued.jpg")
    api = _client_as(client, "op", "operator")

    resp = api.post(f"/collections/{col.id}/take-back")
    assert resp.status_code == 200, resp.text
    assert resp.json()["taken_back"] == 1
    for rec, expected in ((approved, "approved"), (rejected, "rejected"), (in_review, "captured")):
        db_session.refresh(rec)
        assert rec.status == expected


@pytest.mark.unit
def test_take_back_is_not_reachable_per_record(client, db_session):
    """in_review -> captured is deliberately absent from STATUS_TRANSITIONS."""
    col = _collection(db_session)
    rec = _record_with_image(db_session, col, status="in_review")
    api = _client_as(client, "op", "operator")

    resp = api.patch(f"/records/{rec.id}/status", json={"status": "captured"})
    assert resp.status_code == 422, resp.text
    db_session.refresh(rec)
    assert rec.status == "in_review"


@pytest.mark.unit
def test_take_back_is_not_reachable_through_the_bulk_endpoint(client, db_session):
    col = _collection(db_session)
    rec = _record_with_image(db_session, col, status="in_review")
    api = _client_as(client, "op", "operator")

    resp = api.post("/records/bulk-status",
                    json={"record_ids": [rec.id], "status": "captured"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
    db_session.refresh(rec)
    assert rec.status == "in_review"


@pytest.mark.unit
def test_take_back_is_closed_to_a_reviewer(client, db_session):
    col = _collection(db_session)
    _record_with_image(db_session, col, status="in_review")
    api = _client_as(client, "rev", "reviewer")

    assert api.post(f"/collections/{col.id}/take-back").status_code == 403


@pytest.mark.unit
def test_a_collection_can_be_promoted_again_after_a_take_back(client, db_session):
    col = _collection(db_session)
    rec = _record_with_image(db_session, col)
    api = _client_as(client, "op", "operator")

    assert api.post(f"/collections/{col.id}/promote").status_code == 200
    assert api.post(f"/collections/{col.id}/take-back").status_code == 200
    assert api.post(f"/collections/{col.id}/promote").status_code == 200
    db_session.refresh(rec)
    assert rec.status == "in_review"


# ---------------------------------------------------------------------------
# The export gate
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_export_refuses_a_collection_that_was_never_handed_over(client, db_session):
    """Work in progress is reported as that, not as having failed review."""
    col = _collection(db_session)
    captured = _record_with_image(db_session, col, name="wip.jpg")
    _record_with_image(db_session, col, status="approved", name="ok.jpg")
    api = _client_as(client, "op", "operator")

    resp = api.post(f"/collections/{col.id}/export")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["blocking_record_ids"] == [captured.id]
    assert "handed over" in detail["message"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
