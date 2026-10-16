#!/usr/bin/env python3
"""
Tests for NEH-208 rejection scope: a dual-mode record's rejection must flag
BOTH images (a dual-camera pair can only ever be redone together), while a
single-mode record's rejection flags exactly the one image.
Run with: python -m pytest tests/unit/test_rejection_scope_resolution.py
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


@pytest.mark.unit
def test_dual_mode_rejection_flags_both_images(client, db_session):
    from app.models.record import Record, RecordImage

    rec = Record(title="r", status="in_review", capture_mode="dual")
    db_session.add(rec)
    db_session.commit()

    left = RecordImage(record_id=rec.id, filename="l.jpg", file_path="/tmp/l.jpg", format="jpg",
                        role="left", pair_id="p1")
    right = RecordImage(record_id=rec.id, filename="r.jpg", file_path="/tmp/r.jpg", format="jpg",
                         role="right", pair_id="p1")
    db_session.add_all([left, right])
    db_session.commit()

    api = _client_as(client, "rev", "reviewer")
    resp = api.post(f"/records/{rec.id}/reject", json={"predefined_reason": "shadow"})
    assert resp.status_code == 200, resp.text

    resp = api.get(f"/records/{rec.id}/rejections")
    assert resp.status_code == 200, resp.text
    rejections = resp.json()
    assert len(rejections) == 1
    flagged_ids = {i["id"] for i in rejections[0]["images"]}
    assert flagged_ids == {left.id, right.id}


@pytest.mark.unit
def test_single_mode_rejection_flags_exactly_one_image(client, db_session):
    from app.models.record import Record, RecordImage

    rec = Record(title="r", status="in_review", capture_mode="single")
    db_session.add(rec)
    db_session.commit()

    img = RecordImage(record_id=rec.id, filename="a.jpg", file_path="/tmp/a.jpg", format="jpg", role="single")
    db_session.add(img)
    db_session.commit()

    api = _client_as(client, "rev", "reviewer")
    resp = api.post(f"/records/{rec.id}/reject", json={"predefined_reason": "focus"})
    assert resp.status_code == 200, resp.text

    resp = api.get(f"/records/{rec.id}/rejections")
    assert resp.status_code == 200, resp.text
    rejections = resp.json()
    assert len(rejections) == 1
    assert [i["id"] for i in rejections[0]["images"]] == [img.id]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
