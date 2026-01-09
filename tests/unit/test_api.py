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
        from app.core.db import Base, engine, init_db
        from app.core.config import settings
        from app.core.security import (
            hash_password, verify_password,
            create_access_token, verify_access_token
        )
        from app.models.user import User
        from app.models.project import Project
        from app.models.document import DocumentImage, ExifData
        from app.models.camera import CameraSettings
        from app.schemas.user import UserCreate, UserRead, PasswordReset
        from app.schemas.project import ProjectCreate, ProjectRead
        from app.schemas.document import DocumentCreate, DocumentRead, DocumentUpdate
        from app.schemas.camera import CameraSettingsRead, CameraSettingsCreate
        from app.api.auth import router as auth_router, get_current_user
        from app.api.documents import router as documents_router
        from app.api.projects import router as projects_router
        from app.api.cameras import router as cameras_router
        from app.main import app
        print("✓ All imports successful")
        return True
    except Exception as e:
        print(f"✗ Import failed: {e}")
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
        
        print("✓ Password hashing works correctly")
        return True
    except Exception as e:
        print(f"✗ Password hashing test failed: {e}")
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
        
        print("✓ Token generation and verification works correctly")
        return True
    except Exception as e:
        print(f"✗ Token test failed: {e}")
        return False


def test_schemas():
    """Test that Pydantic schemas validate correctly."""
    print("\nTesting Pydantic schemas...")
    try:
        from app.schemas.user import UserCreate, PasswordReset
        from app.schemas.project import ProjectCreate
        from app.schemas.document import DocumentCreate, DocumentUpdate
        
        # Test user creation
        user = UserCreate(username="testuser", email="test@example.com", password="pwd123")
        assert user.username == "testuser"
        
        # Test project creation
        project = ProjectCreate(name="Test Project", description="A test project")
        assert project.name == "Test Project"
        
        # Test document creation with typology
        doc = DocumentCreate(
            filename="test.jpg",
            file_path="/path/to/test.jpg",
            format="jpeg",
            object_typology="book",
            author="John Doe",
            material="paper",
            date="2024-01-01"
        )
        assert doc.object_typology == "book"
        assert doc.author == "John Doe"
        
        # Test document update
        doc_update = DocumentUpdate(
            title="Updated Title",
            object_typology="document",
            custom_attributes='{"custom": "value"}'
        )
        assert doc_update.title == "Updated Title"
        
        print("✓ Schema validation works correctly")
        return True
    except Exception as e:
        print(f"✗ Schema test failed: {e}")
        return False


def test_routes():
    """Test that all routes are registered."""
    print("\nTesting route registration...")
    try:
        from app.main import app
        
        routes = {route.path: route.methods for route in app.routes}
        
        # Check auth routes
        assert "/auth/register" in routes, "Auth register route missing"
        assert "/auth/login" in routes, "Auth login route missing"
        assert "/auth/refresh" in routes, "Auth refresh route missing"
        assert "/auth/password-reset" in routes, "Auth password reset route missing"
        
        # Check documents routes
        assert "/documents/" in routes, "Documents list route missing"
        assert "/documents/{doc_id}" in routes, "Documents get route missing"
        assert "/documents/upload" in routes, "Documents upload route missing"
        assert "/documents/{doc_id}/file" in routes, "Documents file download route missing"
        
        # Check projects routes
        assert "/projects/" in routes, "Projects list route missing"
        assert "/projects/{project_id}" in routes, "Projects get route missing"
        assert "/projects/{project_id}/initialize" in routes, "Projects initialize route missing"
        assert "/projects/{project_id}/documents" in routes, "Projects documents route missing"
        
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
        
        print("✓ All required routes are registered")
        return True
    except AssertionError as e:
        print(f"✗ Route test failed: {e}")
        return False
    except Exception as e:
        print(f"✗ Route test error: {e}")
        return False


def test_models():
    """Test that database models can be created."""
    print("\nTesting model creation...")
    try:
        from app.core.db import Base, engine
        from app.models.user import User
        from app.models.project import Project
        from app.models.document import DocumentImage
        
        # Check that models are registered with Base
        table_names = {table.name for table in Base.metadata.tables.values()}
        
        assert "users" in table_names, "Users table not registered"
        assert "projects" in table_names, "Projects table not registered"
        assert "document_images" in table_names, "Document images table not registered"
        
        print("✓ All models are properly registered")
        return True
    except AssertionError as e:
        print(f"✗ Model test failed: {e}")
        return False
    except Exception as e:
        print(f"✗ Model test error: {e}")
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
        
        print("✓ New endpoint schemas work correctly")
        return True
    except Exception as e:
        print(f"✗ New endpoint test failed: {e}")
        return False


@pytest.mark.unit
def test_api_imports_pytest():
    """Pytest version of import test."""
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
