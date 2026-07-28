#!/usr/bin/env python3
"""
Tests for NEH-208's narrowed status state machine.

Only in_review -> approved remains reachable through PATCH /{id}/status and
POST /records/bulk-status. Rejection has its own endpoint (see
test_record_reject_endpoint.py); approved is terminal; "captured" is not a
valid status anywhere anymore.
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
def test_captured_is_not_a_valid_status_value(client, db_session):
    rec = _make_record(db_session, status="in_review")
    api = _client_as(client, "rev", "reviewer")

    resp = api.patch(f"/records/{rec.id}/status", json={"status": "captured"})
    assert resp.status_code == 422, resp.text


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
