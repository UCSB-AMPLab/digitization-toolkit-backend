#!/usr/bin/env python3
"""
Smoke test for GET /collections/{id}/hierarchy.

The endpoint returns a collection with its nested children and a per-collection
record count. This guards against a regression where the count query filtered a
column that does not exist, so the endpoint returned 500 on every call.
Run with: python -m pytest tests/unit/test_collections_hierarchy.py
"""

import pytest


def _reader():
    from app.models.user import User
    return User(username="reader", email="r@example.com",
                hashed_password="x", role="reviewer", is_active=True)


@pytest.fixture
def read_client(client, db_session):
    from app.main import app
    # collections.py binds its own allow_read_only RoleChecker instance;
    # override that exact object.
    import app.api.collections as collections
    app.dependency_overrides[collections.allow_read_only] = lambda: _reader()
    yield client


@pytest.mark.unit
def test_hierarchy_returns_200_with_record_count(read_client, db_session):
    from app.models.project import Project
    from app.models.collection import Collection
    from app.models.record import Record

    proj = Project(name="P")
    db_session.add(proj)
    db_session.commit()
    col = Collection(name="C", project_id=proj.id)
    db_session.add(col)
    db_session.commit()
    db_session.add(Collection(name="child", parent_collection_id=col.id))
    for i in range(3):
        db_session.add(Record(title=f"r{i}", collection_id=col.id, capture_mode="single"))
    db_session.commit()

    resp = read_client.get(f"/collections/{col.id}/hierarchy")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["record_count"] == 3
    assert len(body["child_collections"]) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
