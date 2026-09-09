#!/usr/bin/env python3
"""Constraint-violation responses must never echo SQL or bound parameters.

Guards the sanitized 409 helper and the schema-level single-parent rule that
keeps a record's project/collection conflict from reaching the database at all.
"""
import pytest
from sqlalchemy.exc import IntegrityError
from pydantic import ValidationError

from app.core.db_errors import integrity_conflict, constraint_name
from app.schemas.record import RecordCreate


class _SqliteOrig(Exception):
    def __str__(self):
        return "CHECK constraint failed: check_record_single_parent"


def _integrity_error():
    return IntegrityError(
        "INSERT INTO records (project_id, collection_id) VALUES (?, ?)",
        [1, 2],
        _SqliteOrig(),
    )


@pytest.mark.unit
def test_integrity_conflict_never_leaks_sql_or_params():
    exc = integrity_conflict(_integrity_error())
    assert exc.status_code == 409
    body = str(exc.detail)
    assert "INSERT" not in body
    assert "VALUES" not in body
    assert "records" not in body


@pytest.mark.unit
def test_integrity_conflict_reports_constraint_name():
    exc = integrity_conflict(_integrity_error())
    assert exc.detail["constraint"] == "check_record_single_parent"
    assert constraint_name(_integrity_error()) == "check_record_single_parent"


@pytest.mark.unit
def test_sqlite_unique_message_maps_to_canonical_name():
    class _UniqueOrig(Exception):
        def __str__(self):
            return "UNIQUE constraint failed: camera_settings.record_image_id"

    exc = integrity_conflict(IntegrityError("INSERT ...", [1], _UniqueOrig()))
    assert exc.status_code == 409
    assert exc.detail["constraint"] == "camera_settings_record_image_id_key"
    assert "INSERT" not in str(exc.detail)


@pytest.mark.unit
def test_record_create_rejects_two_parents_before_db():
    with pytest.raises(ValidationError):
        RecordCreate(title="x", capture_mode="single", project_id=1, collection_id=2)
