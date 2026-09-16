"""
Hardware-gated integration tests for recapture through the real
/cameras/capture and /cameras/capture/dual endpoints.

Mirrors the skip pattern in test_capture_integration.py: these require real
camera hardware (Linux/Raspberry Pi) and skip when none is connected. They are
not the primary coverage for recapture — the non-hardware-gated
test_reject_recapture_full_flow.py carries the required assertions on a dev
machine. This file verifies the part only the real endpoints can exercise: the
capture-mode mismatch guard, and that a recapture leaves the original file on
disk untouched.
Run with: python -m pytest tests/integration/test_camera_capture_recapture_hardware.py
"""
import pytest
import sys

# Skip on non-Linux platforms
if sys.platform != 'linux':
    pytest.skip("Integration tests require Linux/Raspberry Pi environment", allow_module_level=True)


from pathlib import Path

from app.models.record import Record, RecordImage


@pytest.mark.integration
def test_single_capture_recapture_flips_rejected_back_to_in_review(authed_client, db_session, test_project, skip_if_no_camera):
    project_name = test_project.name

    response = authed_client.post(
        "/cameras/capture",
        json={
            "project_name": project_name,
            "camera_index": 0,
            "resolution": "medium",
            "include_resolution_in_filename": False
        },
    )
    assert response.status_code == 200
    record_id = response.json()["record_id"]
    first_image_path = Path(response.json()["file_path"])

    record = db_session.query(Record).filter(Record.id == record_id).first()
    record.status = "rejected"
    db_session.commit()

    response = authed_client.post(
        "/cameras/capture",
        json={
            "project_name": project_name,
            "camera_index": 0,
            "resolution": "medium",
            "include_resolution_in_filename": False,
            "record_id": record_id,
        },
    )
    assert response.status_code == 200, response.text

    db_session.refresh(record)
    assert record.status == "in_review"

    # The original file is preserved on disk — a rejected capture is never deleted.
    assert first_image_path.exists()

    images = db_session.query(RecordImage).filter(RecordImage.record_id == record_id).all()
    old_images = [i for i in images if str(i.file_path) == str(first_image_path)]
    assert old_images and old_images[0].is_current is False


@pytest.mark.integration
def test_dual_capture_recapture_supersedes_both_sides(authed_client, db_session, test_project, skip_if_no_camera):
    project_name = test_project.name

    response = authed_client.post(
        "/cameras/capture/dual",
        json={
            "project_name": project_name,
            "resolution": "medium",
            "include_resolution_in_filename": False,
            "stagger_ms": 20
        },
    )
    assert response.status_code == 200
    record_id = response.json()["record_id"]

    record = db_session.query(Record).filter(Record.id == record_id).first()
    assert record.capture_mode == "dual"
    record.status = "rejected"
    db_session.commit()

    old_images = db_session.query(RecordImage).filter(RecordImage.record_id == record_id).all()
    assert len(old_images) == 2

    response = authed_client.post(
        "/cameras/capture/dual",
        json={
            "project_name": project_name,
            "resolution": "medium",
            "include_resolution_in_filename": False,
            "stagger_ms": 20,
            "record_id": record_id,
        },
    )
    assert response.status_code == 200, response.text

    db_session.refresh(record)
    assert record.status == "in_review"

    for img in old_images:
        db_session.refresh(img)
        assert img.is_current is False

    current_images = db_session.query(RecordImage).filter(
        RecordImage.record_id == record_id, RecordImage.is_current.is_(True)
    ).all()
    assert len(current_images) == 2


@pytest.mark.integration
def test_single_capture_recapture_rejects_mode_mismatch(authed_client, db_session, test_project, skip_if_no_camera):
    """A dual-mode record can't be recaptured through the single-camera endpoint."""
    project_name = test_project.name

    rec = Record(title="dual doc", status="rejected", capture_mode="dual", project_id=test_project.id)
    db_session.add(rec)
    db_session.commit()

    response = authed_client.post(
        "/cameras/capture",
        json={
            "project_name": project_name,
            "camera_index": 0,
            "resolution": "medium",
            "record_id": rec.id,
        },
    )
    assert response.status_code == 422, response.text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
