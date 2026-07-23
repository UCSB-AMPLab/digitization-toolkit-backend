#!/usr/bin/env python3
"""
Minimal test script to validate API endpoints and routes.
This script tests the core functionality without requiring a running server.
Run with: python -m pytest tests/unit/test_api.py
"""

import sys
import os
import pytest

def test_imports():
    """Test that all modules can be imported without errors."""
    print("Testing imports...")
    try:
        from app.core.config import settings
        from app.core.security import (
            hash_password, verify_password,
            create_access_token, verify_access_token
        )
        from app.schemas.user import UserCreate, UserRead, PasswordReset
        from app.schemas.project import ProjectCreate, ProjectRead
        from app.schemas.record import RecordCreate, RecordRead, RecordUpdate
        from app.schemas.camera import CameraSettingsRead, CameraSettingsCreate
        from app.api.auth import router as auth_router, get_current_user
        from app.api.records import router as records_router
        from app.api.projects import router as projects_router
        from app.api.cameras import router as cameras_router
        
        # Try importing app.main (may fail due to database)
        try:
            from app.main import app
            print(" [OK] All imports successful")
        except ImportError as app_e:
            if "pq wrapper" in str(app_e) or "psycopg" in str(app_e):
                print(" [OK] Core imports successful (app.main import skipped - database not available on this platform)")
            else:
                raise
        
        # Try database imports (may fail on non-Linux)
        try:
            from app.core.db import Base, engine, init_db
            from app.models.user import User
            from app.models.project import Project
            from app.models.record import Record, RecordImage, ExifData
            from app.models.camera import CameraSettings
            print(" [OK] All imports successful (including database)")
        except ImportError as db_e:
            if "pq wrapper" in str(db_e) or "psycopg" in str(db_e):
                print(" [OK] Core imports successful (database imports skipped - not available on this platform)")
            else:
                raise
        
        return True
    except Exception as e:
        print(f"[ERROR] Import failed: {e}")
        return False


def test_password_hashing():
    """Test password hashing and verification."""
    print("\nTesting password hashing...")
    try:
        from app.core.security import hash_password, verify_password
        
        password = "test_password_123"
        hashed = hash_password(password)
        
        assert verify_password(password, hashed), "Password verification failed"
        assert not verify_password("wrong_password", hashed), "Wrong password should not verify"
        
        print(" [OK] Password hashing works correctly")
        return True
    except Exception as e:
        print(f"[ERROR] Password hashing test failed: {e}")
        return False


def test_token_generation():
    """Test token creation and verification."""
    print("\nTesting token generation and verification...")
    try:
        from app.core.security import create_access_token, verify_access_token
        import time
        
        token = create_access_token(subject="user_123")
        assert token, "Token should not be empty"
        
        payload = verify_access_token(token)
        assert payload is not None, "Token verification failed"
        assert payload.get("sub") == "user_123", "Subject mismatch"
        
        # Test expired token
        expired_token = create_access_token(subject="user_123", expires_seconds=0)
        time.sleep(1)
        expired_payload = verify_access_token(expired_token)
        assert expired_payload is None, "Expired token should not verify"
        
        print(" [OK] Token generation and verification works correctly")
        return True
    except Exception as e:
        print(f"[ERROR] Token test failed: {e}")
        return False


def test_schemas():
    """Test that Pydantic schemas validate correctly."""
    print("\nTesting Pydantic schemas...")
    try:
        from app.schemas.user import UserCreate, PasswordReset
        from app.schemas.project import ProjectCreate
        from app.schemas.record import RecordCreate, RecordUpdate
        
        # Test user creation
        user = UserCreate(username="testuser", email="test@example.com", password="pwd123")
        assert user.username == "testuser"
        
        # Test project creation
        project = ProjectCreate(name="Test Project", description="A test project")
        assert project.name == "Test Project"
        
        # Test record creation with typology
        doc = RecordCreate(
            title="Test Record",
            description="A test record",
            object_typology="book",
            author="John Doe",
            material="paper",
            date="2024-01-01"
        )
        assert doc.object_typology == "book"
        assert doc.author == "John Doe"
        
        # Test record update
        doc_update = RecordUpdate(
            title="Updated Title",
            object_typology="document",
            custom_attributes='{"custom": "value"}'
        )
        assert doc_update.title == "Updated Title"
        
        print(" [OK] Schema validation works correctly")
        return True
    except Exception as e:
        print(f"[ERROR] Schema test failed: {e}")
        return False


def test_routes():
    """Test that all routes are registered."""
    if sys.platform != 'linux':
        pytest.skip("Route registration tests require Linux/Raspberry Pi environment")

    print("\nTesting route registration...")
    try:
        from app.main import app

        # Read paths from the OpenAPI schema rather than iterating app.routes:
        # the entry types in app.routes are starlette internals that changed
        # across versions (in starlette 1.x, included routers appear as
        # _IncludedRouter objects with no .path attribute).
        routes = set(app.openapi()["paths"].keys())

        # Check auth routes
        assert "/auth/register" in routes, "Auth register route missing"
        assert "/auth/login" in routes, "Auth login route missing"
        assert "/auth/refresh" in routes, "Auth refresh route missing"
        assert "/auth/password-reset" in routes, "Auth password reset route missing"
        
        # Check records routes
        assert "/records/" in routes, "Records list route missing"
        assert "/records/{rec_id}" in routes, "Records get route missing"
        assert "/records/{rec_id}/images" in routes, "Record images route missing"
        assert "/records/images/{img_id}/file" in routes, "Record image file download route missing"
        
        # Check projects routes
        assert "/projects/" in routes, "Projects list route missing"
        assert "/projects/{project_id}" in routes, "Projects get route missing"
        assert "/projects/{project_id}/initialize" in routes, "Projects initialize route missing"
        assert "/projects/{project_id}/records" in routes, "Projects records route missing"
        
        # Check cameras routes
        assert "/cameras/" in routes, "Cameras list route missing"
        assert "/cameras/devices" in routes, "Cameras devices route missing"
        assert "/cameras/capture" in routes, "Cameras capture route missing"
        assert "/cameras/capture/dual" in routes, "Cameras dual capture route missing"
        assert "/cameras/calibrate" in routes, "Cameras calibrate route missing"
        assert "/cameras/calibrate/white-balance" in routes, "Cameras WB calibrate route missing"
        assert "/cameras/settings/{id}" in routes, "Cameras settings CRUD routes missing"
        
        # Check health route
        assert "/health" in routes, "Health check route missing"
        
        print(" [OK] All required routes are registered")
        return True
    except AssertionError as e:
        print(f"[ERROR] Route test failed: {e}")
        return False
    except Exception as e:
        print(f"[ERROR] Route test error: {e}")
        return False


def test_models():
    """Test that database models can be created."""
    if sys.platform != 'linux':
        pytest.skip("Model registration tests require Linux/Raspberry Pi environment")

    print("\nTesting model creation...")
    try:
        from app.core.db import Base, engine
        from app.models.user import User
        from app.models.project import Project
        from app.models.record import Record, RecordImage
        from app.models.camera import CameraSettings
        
        # Check that models are registered with Base
        table_names = {table.name for table in Base.metadata.tables.values()}
        
        assert "users" in table_names, "Users table not registered"
        assert "projects" in table_names, "Projects table not registered"
        assert "record_images" in table_names, "Record images table not registered"
        
        print(" [OK] All models are properly registered")
        return True
    except AssertionError as e:
        print(f"[ERROR] Model test failed: {e}")
        return False
    except Exception as e:
        print(f"[ERROR] Model test error: {e}")
        return False


def test_new_endpoints():
    """Test newly added endpoint schemas and models."""
    print("\nTesting new endpoint schemas...")
    try:
        # Test camera schemas
        from app.schemas.camera import CameraSettingsUpdate
        cam_update = CameraSettingsUpdate(iso=400, white_balance="daylight")
        assert cam_update.iso == 400
        assert cam_update.white_balance == "daylight"
        
        # Test project schemas
        from app.schemas.project import ProjectUpdate
        proj_update = ProjectUpdate(name="Updated Name", description="New description")
        assert proj_update.name == "Updated Name"
        
        # Partial updates should work
        partial_update = ProjectUpdate(description="Only description")
        assert partial_update.name is None
        assert partial_update.description == "Only description"
        
        print(" [OK] New endpoint schemas work correctly")
        return True
    except Exception as e:
        print(f"[ERROR] New endpoint test failed: {e}")
        return False


@pytest.mark.unit
def test_api_imports_pytest():
    """Pytest version of import test."""
    if sys.platform != 'linux':
        pytest.skip("Database imports not available on non-Linux platforms")
    assert test_imports()


@pytest.mark.unit
def test_api_password_pytest():
    """Pytest version of password test."""
    assert test_password_hashing()


@pytest.mark.unit
def test_api_tokens_pytest():
    """Pytest version of token test."""
    assert test_token_generation()


@pytest.mark.unit
def test_api_schemas_pytest():
    """Pytest version of schemas test."""
    assert test_schemas()


@pytest.mark.unit
def test_api_routes_pytest():
    """Pytest version of routes test."""
    assert test_routes()


@pytest.mark.unit
def test_api_models_pytest():
    """Pytest version of models test."""
    assert test_models()


@pytest.mark.unit
def test_api_endpoints_pytest():
    """Pytest version of endpoints test."""
    assert test_new_endpoints()


@pytest.mark.unit
def test_missing_credentials_returns_401_not_403(client):
    """A protected endpoint with no Authorization header must reject with 401
    ("session problem"), never 403 ("authorization answer"). NEH-167."""
    resp = client.get("/users/me")
    assert resp.status_code == 401


@pytest.mark.unit
def test_refresh_missing_credentials_returns_401(client):
    """/auth/refresh with no Authorization header must return 401, like every
    other protected endpoint. NEH-167."""
    resp = client.post("/auth/refresh")
    assert resp.status_code == 401


@pytest.mark.unit
def test_password_reset_missing_credentials_returns_401(client):
    """/auth/password-reset with no Authorization header must return 401. NEH-167."""
    resp = client.post(
        "/auth/password-reset",
        json={"old_password": "x", "new_password": "y"},
    )
    assert resp.status_code == 401


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
