#!/usr/bin/env python3
"""
Tests for NEH-109/NEH-110/NEH-111/NEH-112: schema-hygiene batch —
FK indexes on records, UNIQUE on exif_data.record_image_id, ondelete
CASCADE on the record_images children, and CHECK constraints on the
enum-like status/role/level/category columns.

SQLite (via db_session's create_all) enforces model-declared CHECK and
UNIQUE constraints, so those are covered directly here. FK ondelete
CASCADE is a Postgres-side behavior verified separately against the
scratch database, not here.

Run with: python -m pytest tests/unit/test_schema_constraints.py
"""

import pytest
from sqlalchemy.exc import IntegrityError


# ==============================================================================
# (1) CHECK constraints - invalid values raise IntegrityError
# ==============================================================================

@pytest.mark.unit
def test_record_bogus_status_raises_integrity_error(db_session):
    from app.models.record import Record

    r = Record(title="r", status="bogus")
    db_session.add(r)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
def test_user_bogus_role_raises_integrity_error(db_session):
    from app.models.user import User

    u = User(username="u1", email="u1@example.com", hashed_password="x", role="contributor")
    db_session.add(u)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
def test_project_member_bogus_role_raises_integrity_error(db_session):
    from app.models.project import Project
    from app.models.user import User
    from app.models.project_member import ProjectMember

    proj = Project(name="P")
    user = User(username="u2", email="u2@example.com", hashed_password="x", role="operator")
    db_session.add_all([proj, user])
    db_session.commit()

    pm = ProjectMember(project_id=proj.id, user_id=user.id, role="admin")
    db_session.add(pm)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
def test_system_log_bogus_level_and_category_raise_integrity_error(db_session):
    from app.models.system_log import SystemLog

    bad_level = SystemLog(level="DEBUG", category="access", action="login_success")
    db_session.add(bad_level)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    bad_category = SystemLog(level="INFO", category="bogus", action="login_success")
    db_session.add(bad_category)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ==============================================================================
# (2) UNIQUE constraint on exif_data.record_image_id
# ==============================================================================

@pytest.mark.unit
def test_second_exif_data_row_for_same_record_image_raises_integrity_error(db_session):
    from app.models.project import Project
    from app.models.record import Record, RecordImage, ExifData

    proj = Project(name="P")
    db_session.add(proj)
    db_session.commit()

    rec = Record(title="r", project_id=proj.id, capture_mode="single")
    db_session.add(rec)
    db_session.commit()

    img = RecordImage(
        record_id=rec.id,
        filename="a.jpg",
        file_path="/tmp/a.jpg",
        format="jpg",
    )
    db_session.add(img)
    db_session.commit()

    exif1 = ExifData(record_image_id=img.id, make="Canon")
    db_session.add(exif1)
    db_session.commit()

    exif2 = ExifData(record_image_id=img.id, make="Nikon")
    db_session.add(exif2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ==============================================================================
# (3) Happy path - one valid row per constrained table
# ==============================================================================

@pytest.mark.unit
def test_valid_rows_commit_fine(db_session):
    from app.models.project import Project
    from app.models.user import User
    from app.models.project_member import ProjectMember
    from app.models.system_log import SystemLog
    from app.models.record import Record

    proj = Project(name="P")
    user = User(username="u3", email="u3@example.com", hashed_password="x", role="operator")
    db_session.add_all([proj, user])
    db_session.commit()

    pm = ProjectMember(project_id=proj.id, user_id=user.id, role="operator")
    log = SystemLog(level="ERR", category="capture", action="capture_failed")
    rec = Record(title="r", project_id=proj.id, status="approved", capture_mode="single")
    db_session.add_all([pm, log, rec])
    db_session.commit()


# ==============================================================================
# (4) API-level: schema tighten on RecordBase.status
# ==============================================================================

def _contributor():
    from app.models.user import User
    return User(username="contributor", email="contributor@example.com",
                hashed_password="x", role="operator", is_active=True)


@pytest.fixture
def contributor_client(client, db_session):
    from app.main import app
    # POST /records/ is behind allow_contributor, not allow_read_only;
    # override that exact RoleChecker instance bound in app.api.records.
    import app.api.records as records
    app.dependency_overrides[records.allow_contributor] = lambda: _contributor()
    yield client


@pytest.mark.unit
def test_create_record_with_bogus_status_returns_422(contributor_client, db_session):
    # RecordCreate ignores `status` on the create path (Record() is built
    # without it in create_record), so this guards an otherwise-unused
    # field - belt and suspenders alongside the DB-level CHECK constraint.
    resp = contributor_client.post("/records/", json={"title": "r", "status": "bogus"})
    assert resp.status_code == 422, resp.text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
