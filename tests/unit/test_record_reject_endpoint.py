#!/usr/bin/env python3
"""
Tests for POST /records/{id}/reject and GET /records/{id}/rejections (NEH-208).

Rejection requires a mandatory predefined_reason from a fixed 6-value list,
plus an optional free-text comment; it's only valid from 'in_review'; and
only reviewer/admin may perform it.
Run with: python -m pytest tests/unit/test_record_reject_endpoint.py
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
def test_reject_requires_predefined_reason(client, db_session):
    rec = _make_record(db_session)
    api = _client_as(client, "rev", "reviewer")

    resp = api.post(f"/records/{rec.id}/reject", json={})
    assert resp.status_code == 422, resp.text


@pytest.mark.unit
def test_reject_rejects_unknown_reason(client, db_session):
    rec = _make_record(db_session)
    api = _client_as(client, "rev", "reviewer")

    resp = api.post(f"/records/{rec.id}/reject", json={"predefined_reason": "bogus"})
    assert resp.status_code == 422, resp.text


@pytest.mark.unit
def test_reject_accepts_valid_reason_and_optional_comment(client, db_session):
    rec = _make_record(db_session)
    api = _client_as(client, "rev", "reviewer")

    resp = api.post(f"/records/{rec.id}/reject", json={"predefined_reason": "blur", "comment": "too fuzzy"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"


@pytest.mark.unit
def test_reject_only_valid_from_in_review(client, db_session):
    rec = _make_record(db_session, status="approved")
    api = _client_as(client, "rev", "reviewer")

    resp = api.post(f"/records/{rec.id}/reject", json={"predefined_reason": "blur"})
    assert resp.status_code == 422, resp.text


@pytest.mark.unit
def test_operator_cannot_reject(client, db_session):
    rec = _make_record(db_session)
    api = _client_as(client, "op", "operator")

    resp = api.post(f"/records/{rec.id}/reject", json={"predefined_reason": "blur"})
    assert resp.status_code == 403, resp.text


@pytest.mark.unit
def test_rejection_appears_in_rejections_history_and_image_drops_from_images_list(client, db_session):
    from app.models.record import RecordImage

    rec = _make_record(db_session)
    img = RecordImage(record_id=rec.id, filename="a.jpg", file_path="/tmp/a.jpg", format="jpg")
    db_session.add(img)
    db_session.commit()

    api = _client_as(client, "rev", "reviewer")
    resp = api.post(f"/records/{rec.id}/reject", json={"predefined_reason": "glare", "comment": "window reflection"})
    assert resp.status_code == 200, resp.text

    resp = api.get(f"/records/{rec.id}/rejections")
    assert resp.status_code == 200, resp.text
    rejections = resp.json()
    assert len(rejections) == 1
    assert rejections[0]["predefined_reason"] == "glare"
    assert rejections[0]["comment"] == "window reflection"
    assert [i["id"] for i in rejections[0]["images"]] == [img.id]

    # The rejected image is still "current" until a recapture supersedes it,
    # so it still appears in the default images listing.
    resp = api.get(f"/records/{rec.id}/images")
    assert resp.status_code == 200, resp.text
    assert [i["id"] for i in resp.json()] == [img.id]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
