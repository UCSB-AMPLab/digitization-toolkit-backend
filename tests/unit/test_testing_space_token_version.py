"""The testing account's session generation is raised in the database, not computed in Python.

A new password, and a change from active to inactive, raise the testing
account's token_version with one `UPDATE ... token_version = token_version + 1`
(app.api.auth.end_sessions) in the transaction that writes the password or the
flag. Two changes made at the same moment each count, and the ORM flush never
writes a version it read earlier back over the stored one.

    python -m pytest tests/unit/test_testing_space_token_version.py

Version: 0.3.0
"""

import re

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from tests.unit.test_capture_manifest_export import _as, tenant  # noqa: F401  (tenant is a fixture)
from tests.unit.test_testing_space_admin import PASSWORD, OTHER_PASSWORD, _switch_on
from tests.unit.test_testing_space_schema import _testing_user


def _statements(db, action):
    """The SQL statements `action` emits on `db`'s engine."""
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(re.sub(r"\s+", " ", statement))

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        action()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return seen


def _version_updates(statements):
    return [s for s in statements if s.startswith("UPDATE users") and "token_version" in s]


def _assert_one_increment(statements):
    updates = _version_updates(statements)
    assert len(updates) == 1, updates
    assert re.search(r"token_version\s*=\s*\(?users\.token_version\s*\+", updates[0]), updates[0]
    for statement in statements:
        if statement.startswith("UPDATE users"):
            assert not re.search(r"token_version\s*=\s*\?", statement), statement


@pytest.mark.unit
def test_a_new_password_raises_the_version_with_one_incrementing_update(client, db_session):
    _switch_on(client)
    statements = _statements(db_session, lambda: _as(client, "admin").patch(
        "/system/testing/account", json={"password": OTHER_PASSWORD}))
    _assert_one_increment(statements)


@pytest.mark.unit
def test_a_deactivation_raises_the_version_with_one_incrementing_update(client, db_session):
    _switch_on(client)
    statements = _statements(db_session, lambda: _as(client, "admin").put(
        "/system/testing", json={"enabled": False}))
    _assert_one_increment(statements)


@pytest.mark.unit
def test_the_password_and_the_raise_commit_together(client, db_session):
    from app.models.user import User
    _switch_on(client)
    db_session.expire_all()
    before = db_session.query(User).filter(User.role == "testing").one()
    old_hash, old_version = before.hashed_password, before.token_version
    resp = _as(client, "admin").patch("/system/testing/account", json={"password": OTHER_PASSWORD})
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    after = db_session.query(User).filter(User.role == "testing").one()
    assert after.hashed_password != old_hash and after.token_version == old_version + 1


@pytest.mark.unit
def test_two_interleaved_changes_each_count(db_session):
    from app.core import testing_space
    from app.models.user import User
    user = _testing_user(db_session)
    start = user.token_version or 0
    assert db_session.get(User, user.id).token_version == start  # N is now in this session's identity map

    other = sessionmaker(bind=db_session.get_bind())()
    try:
        other_user = other.get(User, user.id)
        testing_space._write_account(other, other_user, actor="admin", hashed_password="h-other")
        other.commit()
    finally:
        other.close()

    testing_space._write_account(db_session, user, actor="admin", hashed_password="h-here")
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(User, user.id).token_version == start + 2
