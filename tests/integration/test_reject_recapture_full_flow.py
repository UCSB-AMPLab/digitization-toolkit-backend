#!/usr/bin/env python3
"""
Integration tests for the full NEH-208 reject -> recapture -> re-review flow,
covering both capture modes end to end against the real API + DB layer.

These don't go through the hardware-gated /cameras/capture(/dual) endpoints
(no mock capture backend exists in this repo — see
tests/integration/test_capture_integration.py, which skips outright on
non-Linux). Instead, "capture" and "recapture" are simulated the same way
cameras.py implements them: RecordImage rows are created directly, and
recapture calls the same _supersede_current_images helper cameras.py calls
before adding the replacement image(s). The mode-mismatch guard and the
actual capture-endpoint wiring are covered separately by the hardware-gated
test_camera_capture_recapture_hardware.py.
Run with: python -m pytest tests/integration/test_reject_recapture_full_flow.py
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


@pytest.mark.integration
def test_full_single_mode_reject_recapture_approve_flow(client, db_session):
    from app.models.record import Record, RecordImage
    from app.api.records import _supersede_current_images

    rec = Record(title="doc", status="in_review", capture_mode="single")
    db_session.add(rec)
    db_session.commit()

    old_img = RecordImage(record_id=rec.id, filename="a.jpg", file_path="/tmp/a.jpg", format="jpg", role="single")
    db_session.add(old_img)
    db_session.commit()

    api = _client_as(client, "u", "reviewer")

    # Reject with a mandatory reason.
    resp = api.post(f"/records/{rec.id}/reject", json={"predefined_reason": "blur", "comment": "shaky hand"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"

    # Recapture: supersede the old image, add the replacement, flip back to in_review
    # (exactly what cameras.py's trigger_capture does on a rejected record).
    db_session.refresh(rec)
    _supersede_current_images(rec)
    new_img = RecordImage(record_id=rec.id, filename="a2.jpg", file_path="/tmp/a2.jpg", format="jpg", role="single")
    db_session.add(new_img)
    rec.status = "in_review"
    db_session.commit()

    # Only the new image is "current".
    resp = api.get(f"/records/{rec.id}/images")
    assert resp.status_code == 200, resp.text
    assert [i["id"] for i in resp.json()] == [new_img.id]

    # The old image's rejection record is still queryable, with the old image attached.
    resp = api.get(f"/records/{rec.id}/rejections")
    assert resp.status_code == 200, resp.text
    rejections = resp.json()
    assert len(rejections) == 1
    assert rejections[0]["predefined_reason"] == "blur"
    assert [i["id"] for i in rejections[0]["images"]] == [old_img.id]
    assert rejections[0]["images"][0]["is_current"] is False

    # Back in in_review, so it can now be approved.
    resp = api.get(f"/records/{rec.id}")
    assert resp.json()["status"] == "in_review"

    resp = api.patch(f"/records/{rec.id}/status", json={"status": "approved"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"


@pytest.mark.integration
def test_full_dual_mode_reject_recapture_flow(client, db_session):
    from app.models.record import Record, RecordImage
    from app.api.records import _supersede_current_images

    rec = Record(title="book", status="in_review", capture_mode="dual")
    db_session.add(rec)
    db_session.commit()

    old_left = RecordImage(record_id=rec.id, filename="l.jpg", file_path="/tmp/l.jpg", format="jpg",
                            role="left", pair_id="p1")
    old_right = RecordImage(record_id=rec.id, filename="r.jpg", file_path="/tmp/r.jpg", format="jpg",
                             role="right", pair_id="p1")
    db_session.add_all([old_left, old_right])
    db_session.commit()

    api = _client_as(client, "u", "reviewer")

    # A defect on only one side still rejects the whole pair — physically,
    # dual capture can only ever be redone together.
    resp = api.post(f"/records/{rec.id}/reject", json={"predefined_reason": "exposure"})
    assert resp.status_code == 200, resp.text

    resp = api.get(f"/records/{rec.id}/rejections")
    flagged_ids = {i["id"] for i in resp.json()[0]["images"]}
    assert flagged_ids == {old_left.id, old_right.id}

    # Recapture both sides.
    db_session.refresh(rec)
    _supersede_current_images(rec)
    new_left = RecordImage(record_id=rec.id, filename="l2.jpg", file_path="/tmp/l2.jpg", format="jpg",
                            role="left", pair_id="p2")
    new_right = RecordImage(record_id=rec.id, filename="r2.jpg", file_path="/tmp/r2.jpg", format="jpg",
                             role="right", pair_id="p2")
    db_session.add_all([new_left, new_right])
    rec.status = "in_review"
    db_session.commit()

    resp = api.get(f"/records/{rec.id}/images")
    assert resp.status_code == 200, resp.text
    assert {i["id"] for i in resp.json()} == {new_left.id, new_right.id}

    # Old pair still fully queryable via the audit endpoint after recapture.
    resp = api.get(f"/records/{rec.id}/rejections")
    assert resp.status_code == 200, resp.text
    rejections = resp.json()
    assert len(rejections) == 1
    assert {i["id"] for i in rejections[0]["images"]} == {old_left.id, old_right.id}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
