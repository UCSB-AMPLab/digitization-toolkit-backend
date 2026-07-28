#!/usr/bin/env python3
"""
Tests for DELETE /records/annotations/{annotation_id} (NEH-164).

Operators/reviewers may only delete annotations they created; admins may
delete any annotation. Legacy annotations with no created_by recorded can
still be deleted by anyone, since ownership can't be enforced on them.
Run with: python -m pytest tests/unit/test_annotation_delete_creator_check.py
"""

import pytest


def _user(username, role):
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, username, role):
    from app.main import app
    import app.api.records as records
    # records.py binds its own allow_read_only RoleChecker instance;
    # override that exact object (see tests/unit/test_collections_hierarchy.py).
    app.dependency_overrides[records.allow_read_only] = lambda: _user(username, role)
    return client


def _make_annotation(db_session, created_by):
    from app.models.record import Record, RecordAnnotation

    rec = Record(title="r", created_by="someone", capture_mode="single")
    db_session.add(rec)
    db_session.commit()

    annotation = RecordAnnotation(record_id=rec.id, note="n", created_by=created_by)
    db_session.add(annotation)
    db_session.commit()
    db_session.refresh(annotation)
    return annotation


@pytest.mark.unit
def test_creator_can_delete_own_annotation(client, db_session):
    annotation = _make_annotation(db_session, created_by="alice")
    c = _client_as(client, "alice", "operator")

    resp = c.delete(f"/records/annotations/{annotation.id}")
    assert resp.status_code == 200, resp.text


@pytest.mark.unit
def test_non_creator_non_admin_gets_403(client, db_session):
    annotation = _make_annotation(db_session, created_by="alice")
    c = _client_as(client, "bob", "reviewer")

    resp = c.delete(f"/records/annotations/{annotation.id}")
    assert resp.status_code == 403, resp.text

    from app.models.record import RecordAnnotation
    assert db_session.query(RecordAnnotation).filter_by(id=annotation.id).first() is not None


@pytest.mark.unit
def test_admin_can_delete_any_annotation(client, db_session):
    annotation = _make_annotation(db_session, created_by="alice")
    c = _client_as(client, "root", "admin")

    resp = c.delete(f"/records/annotations/{annotation.id}")
    assert resp.status_code == 200, resp.text


@pytest.mark.unit
def test_legacy_annotation_with_no_creator_can_be_deleted_by_anyone(client, db_session):
    annotation = _make_annotation(db_session, created_by=None)
    c = _client_as(client, "bob", "reviewer")

    resp = c.delete(f"/records/annotations/{annotation.id}")
    assert resp.status_code == 200, resp.text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
