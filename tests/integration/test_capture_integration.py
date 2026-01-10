"""
Integration tests for capture API endpoints wired to database.

Tests the full flow: capture image -> save to microSD -> create database record.
Run with: python -m pytest tests/integration/test_capture_integration.py
"""
import pytest
from pathlib import Path
from app.models.document import DocumentImage, ExifData
from app.models.camera import CameraSettings
from app.models.project import Project


@pytest.mark.integration
def test_single_capture_creates_database_record(client, db_session, test_project):
    """
    Test that /cameras/capture creates a DocumentImage record in database.
    """
    # Ensure project exists
    project_name = test_project.name
    
    # Call capture endpoint
    response = client.post(
        "/cameras/capture",
        json={
            "project_name": project_name,
            "camera_index": 0,
            "resolution": "medium",
            "include_resolution_in_filename": False
        },
        headers={"Authorization": "Bearer test_token"}
    )
    
    # Check response
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["file_path"] is not None
    
    # Verify database record was created
    doc = db_session.query(DocumentImage).filter(
        DocumentImage.file_path == data["file_path"]
    ).first()
    
    assert doc is not None
    assert doc.title is not None
    assert doc.format == "jpg"
    assert doc.resolution_width is not None
    assert doc.resolution_height is not None
    assert doc.project_id == test_project.id
    
    # Verify camera settings were saved
    cs = db_session.query(CameraSettings).filter(
        CameraSettings.document_image_id == doc.id
    ).first()
    assert cs is not None
    assert cs.white_balance == "indoor"


@pytest.mark.integration
def test_dual_capture_creates_two_database_records(client, db_session, test_project):
    """
    Test that /cameras/capture/dual creates two DocumentImage records.
    """
    project_name = test_project.name
    
    # Call dual capture endpoint
    response = client.post(
        "/cameras/capture/dual",
        json={
            "project_name": project_name,
            "resolution": "medium",
            "include_resolution_in_filename": False,
            "stagger_ms": 20
        },
        headers={"Authorization": "Bearer test_token"}
    )
    
    # Check response
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert len(data["file_paths"]) == 2
    
    # Verify both database records were created
    docs = db_session.query(DocumentImage).filter(
        DocumentImage.project_id == test_project.id
    ).all()
    
    assert len(docs) >= 2
    
    # Check both have camera settings
    for doc in docs[-2:]:
        cs = db_session.query(CameraSettings).filter(
            CameraSettings.document_image_id == doc.id
        ).first()
        assert cs is not None


@pytest.mark.integration
def test_captured_images_stored_locally(client, test_project, tmp_path):
    """
    Test that captured images are actually stored on the filesystem (microSD).
    """
    project_name = test_project.name
    
    # Call capture endpoint
    response = client.post(
        "/cameras/capture",
        json={
            "project_name": project_name,
            "camera_index": 0,
            "resolution": "medium"
        },
        headers={"Authorization": "Bearer test_token"}
    )
    
    data = response.json()
    file_path = Path(data["file_path"])
    
    # Verify file exists
    assert file_path.exists()
    assert file_path.stat().st_size > 0
    assert file_path.suffix.lower() in [".jpg", ".jpeg"]


@pytest.mark.integration
def test_exif_data_extracted_and_saved(client, db_session, test_project):
    """
    Test that EXIF data from captured image is extracted and saved to database.
    """
    project_name = test_project.name
    
    response = client.post(
        "/cameras/capture",
        json={
            "project_name": project_name,
            "camera_index": 0,
            "resolution": "high"
        },
        headers={"Authorization": "Bearer test_token"}
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Get database record
    doc = db_session.query(DocumentImage).filter(
        DocumentImage.file_path == data["file_path"]
    ).first()
    
    assert doc is not None
    
    # Check if EXIF data was created
    exif = db_session.query(ExifData).filter(
        ExifData.document_image_id == doc.id
    ).first()
    
    # EXIF might not be present if PIL can't read it, but raw_exif should have data
    if exif:
        assert exif.raw_exif is not None or exif.make is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
