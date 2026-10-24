#!/usr/bin/env python3
"""
Tests for the status state machine reachable through PATCH /{id}/status and
POST /records/bulk-status.

Five transitions are in the table: promotion (captured -> in_review),
approval, the two undo resets, and prepare-for-retake (rejected -> captured).
Rejection has its own endpoint (see test_record_reject_endpoint.py), taking a
handover back is per collection (see test_collection_handover.py), and
leaving `captured` also has to satisfy the replacement-image invariant (see
test_promotion_invariant.py).

Run with: python -m pytest tests/unit/test_record_status_transitions.py
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


def _make_record(db_session, status="in_review", capture_mode="single"):
    from app.models.record import Record

    rec = Record(title="r", status=status, capture_mode=capture_mode)
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)
    return rec


def _with_image(db_session, rec):
    """Leaving `captured` needs a current image carrying no rejection_id."""
    from app.models.record import RecordImage

    db_session.add(RecordImage(record_id=rec.id, filename="f.jpg",
                               file_path="/f.jpg", format="jpg",
                               role="single", is_current=True))
    db_session.commit()
    db_session.refresh(rec)
    return rec


@pytest.mark.unit
def test_in_review_to_approved_allowed_for_reviewer(client, db_session):
    rec = _make_record(db_session, status="in_review")
    api = _client_as(client, "rev", "reviewer")

    resp = api.patch(f"/records/{rec.id}/status", json={"status": "approved"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"


@pytest.mark.unit
def test_in_review_to_approved_forbidden_for_operator(client, db_session):
    rec = _make_record(db_session, status="in_review")
    api = _client_as(client, "op", "operator")

    resp = api.patch(f"/records/{rec.id}/status", json={"status": "approved"})
    assert resp.status_code == 403, resp.text


@pytest.mark.unit
def test_approved_to_rejected_no_longer_allowed(client, db_session):
    """approved is terminal (NEH-208) — the old 'flag for rework' walk-back is gone."""
    rec = _make_record(db_session, status="approved")
    api = _client_as(client, "rev", "reviewer")

    resp = api.patch(f"/records/{rec.id}/status", json={"status": "rejected"})
    assert resp.status_code == 422, resp.text


@pytest.mark.unit
def test_rejected_is_not_reachable_via_generic_status_endpoint(client, db_session):
    """Rejection must go through POST /{id}/reject with a mandatory reason, not this endpoint."""
    rec = _make_record(db_session, status="in_review")
    api = _client_as(client, "rev", "reviewer")

    resp = api.patch(f"/records/{rec.id}/status", json={"status": "rejected"})
    assert resp.status_code == 422, resp.text


@pytest.mark.unit
def test_captured_to_in_review_allowed_for_operator(client, db_session):
    """Promotion. The operator hands her own work over (NEH-260)."""
    rec = _with_image(db_session, _make_record(db_session, status="captured"))
    api = _client_as(client, "op", "operator")

    resp = api.patch(f"/records/{rec.id}/status", json={"status": "in_review"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "in_review"


@pytest.mark.unit
def test_captured_to_in_review_forbidden_for_reviewer(client, db_session):
    rec = _with_image(db_session, _make_record(db_session, status="captured"))
    api = _client_as(client, "rev", "reviewer")

    resp = api.patch(f"/records/{rec.id}/status", json={"status": "in_review"})
    assert resp.status_code == 403, resp.text


@pytest.mark.unit
def test_rejected_to_captured_allowed_for_operator(client, db_session):
    """Prepare for retake: a rejected record returns to her worklist (NEH-260)."""
    rec = _make_record(db_session, status="rejected")
    api = _client_as(client, "op", "operator")

    resp = api.patch(f"/records/{rec.id}/status", json={"status": "captured"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "captured"


@pytest.mark.unit
def test_rejected_to_captured_forbidden_for_reviewer(client, db_session):
    rec = _make_record(db_session, status="rejected")
    api = _client_as(client, "rev", "reviewer")

    resp = api.patch(f"/records/{rec.id}/status", json={"status": "captured"})
    assert resp.status_code == 403, resp.text


@pytest.mark.unit
def test_in_review_to_captured_is_not_in_the_table(client, db_session):
    """Taking a handover back is per collection, never per record (NEH-260).

    The value validates — `captured` is a real status again — so the refusal
    has to come from the transition table rather than from the schema.
    """
    rec = _make_record(db_session, status="in_review")
    api = _client_as(client, "op", "operator")

    resp = api.patch(f"/records/{rec.id}/status", json={"status": "captured"})
    assert resp.status_code == 422, resp.text
    assert "not allowed" in resp.json()["detail"], resp.text


@pytest.mark.unit
def test_bulk_status_update_skips_invalid_transitions_and_updates_valid_ones(client, db_session):
    rec_ok = _make_record(db_session, status="in_review")
    rec_blocked = _make_record(db_session, status="rejected")
    api = _client_as(client, "rev", "reviewer")

    resp = api.post("/records/bulk-status", json={
        "record_ids": [rec_ok.id, rec_blocked.id],
        "status": "approved",
    })
    assert resp.status_code == 200, resp.text
    updated_ids = {r["id"] for r in resp.json()}
    assert rec_ok.id in updated_ids
    assert rec_blocked.id not in updated_ids


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
