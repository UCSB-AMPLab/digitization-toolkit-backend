"""Shared fixtures for the hardware-gated capture integration tests."""
import pytest


@pytest.fixture
def authed_client(client, db_session):
    """TestClient authenticated as a real contributor via a get_current_user override.

    The capture endpoints sit behind allow_contributor, which resolves the caller
    through get_current_user; verify_access_token rejects the literal
    "Bearer test_token" these tests used to send, giving a 401 before the endpoint
    ran. Overriding the dependency is the pattern used in
    tests/unit/test_delete_user_guards.py. The `client` fixture clears
    dependency_overrides on teardown.
    """
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User
    from app.core.security import hash_password

    user = User(
        username="capture_user",
        email="capture_user@example.com",
        hashed_password=hash_password("x"),
        role="contributor",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    app.dependency_overrides[get_current_user] = lambda: user
    yield client
