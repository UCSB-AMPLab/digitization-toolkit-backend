"""The testing space's schema, role and invariants (NEH-391).

A unit may hold a testing space beside its real work: testing projects and
testing volumes, and one shared testing account with a role of its own.
This file covers what holds whatever routes later reach them:

- the migration: `projects.testing`, `collections.testing` (always kept on
  the unit), `collections.testing_expires_at`, the `testing` role with at
  most one account holding it, and the unit's testing settings;
- the models: `testing` is fixed at insert and follows the volume's project
  or parent; a testing volume stays kept;
- the role: refused by every role allowlist, never given or taken away
  through PATCH /auth/users/{id}/role, and the account is not deactivated,
  deleted or given a new password through the ordinary user routes;
- the kept machinery: a testing volume is never released and never counted
  among the kept volumes;
- moves: nothing crosses between testing and real work (`cross_space`);
- the default project and the list import never pick a testing project;
- the operators sent to Fisqua never include the testing account.

    python -m pytest tests/unit/test_testing_space_schema.py

Version: 0.3.0
"""

import importlib.util
import os
import re
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from tests.unit.test_capture_manifest_export import _as, tenant  # noqa: F401  (tenant is a fixture)
from tests.unit.test_fisqua_outbox import fisqua  # noqa: F401  (fixture)
from tests.unit.test_unit_mode import _logs, _project, _unit, link, recipient  # noqa: F401  (fixtures)

REVISION = "c8e2a4f6b0d1"
# app.core.project_names.TESTING_PREFIX, spelled out so the tests fail if it changes.
T = "captua-testing-"
# app.core.project_names.TESTING_PROJECT_PREFIX: a testing project's stored name begins with it.
TP = "captua-testing--"
MIGRATION_FILE = f"{REVISION}_add_testing_space.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detail(resp, status, code):
    assert resp.status_code == status, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == code, detail
    assert detail["message"]
    return detail


def _testing_project(db, name="Pruebas"):
    """A testing project whose display name is `name`, stored with the prefix."""
    from app.models.project import Project
    p = Project(name=TP + name, testing=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _volume(db, project=None, parent=None, name="Tomo", stem=None, **extra):
    from app.models.collection import Collection
    col = Collection(name=name, filename_stem=stem,
                     project_id=project.id if project is not None else None,
                     parent_collection_id=parent.id if parent is not None else None, **extra)
    db.add(col)
    db.commit()
    db.refresh(col)
    return col


def _testing_volume(db, project=None, name="Prueba", stem=T + "t1"):
    project = project or _testing_project(db, f"P {name}")
    return _volume(db, project, name=name, stem=stem)


def _record(db, collection_id=None, project_id=None, title="r1"):
    from app.models.record import Record
    rec = Record(title=title, collection_id=collection_id, project_id=project_id, status="captured",
                 capture_mode="single", sequence=1)
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def _testing_user(db, username="pruebas", password="secret-pw"):
    from app.core.security import hash_password
    from app.models.user import User
    user = User(username=username, hashed_password=hash_password(password), role="testing", is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------

def _load_migration(backend_root):
    path = backend_root / "alembic" / "versions" / MIGRATION_FILE
    spec = importlib.util.spec_from_file_location("testing_space_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_migration(engine, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def _before(engine):
    """The tables this revision changes, as the previous head leaves them, in the columns it needs."""
    md = sa.MetaData()
    sa.Table("users", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("username", sa.String(150), nullable=False),
             sa.Column("role", sa.String(50), nullable=False),
             sa.CheckConstraint("role IN ('admin','operator','reviewer')", name="check_user_role"))
    sa.Table("projects", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("name", sa.String(255), nullable=False))
    sa.Table("collections", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("name", sa.String(255), nullable=False),
             sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=True),
             sa.Column("kept_on_unit", sa.Boolean, nullable=False, server_default=sa.false()))
    sa.Table("unit_settings", md,
             sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
             sa.Column("mode", sa.String(16), nullable=False))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, username, role) VALUES (1, 'a', 'admin')"))
        conn.execute(sa.text("INSERT INTO projects (id, name) VALUES (1, 'p')"))
        conn.execute(sa.text("INSERT INTO collections (id, name, project_id) VALUES (1, 'c', 1)"))
        conn.execute(sa.text("INSERT INTO unit_settings (id, mode) VALUES (1, 'backup')"))


def _exercise(engine, mod):
    _before(engine)
    _run_migration(engine, mod.upgrade)

    insp = sa.inspect(engine)
    for table, name in (("projects", "testing"), ("collections", "testing"),
                        ("unit_settings", "testing_enabled"), ("unit_settings", "testing_expiry_days")):
        column = {c["name"]: c for c in insp.get_columns(table)}[name]
        assert column["nullable"] is False, (table, name)
    assert {c["name"]: c for c in insp.get_columns("collections")}["testing_expires_at"]["nullable"] is True
    assert {c["name"]: c for c in insp.get_columns("unit_settings")}["testing_user_id"]["nullable"] is True
    assert {c["name"]: c for c in insp.get_columns("users")}["token_version"]["nullable"] is False
    [fk] = [f for f in insp.get_foreign_keys("unit_settings") if f["constrained_columns"] == ["testing_user_id"]]
    assert fk["referred_table"] == "users" and fk["options"].get("ondelete") == "SET NULL"

    with engine.begin() as conn:
        # Every existing row is real work, and the space is off.
        assert conn.execute(sa.text("SELECT testing FROM projects")).scalar() in (0, False)
        assert conn.execute(sa.text("SELECT testing FROM collections")).scalar() in (0, False)
        row = conn.execute(sa.text(
            "SELECT testing_enabled, testing_user_id, testing_expiry_days FROM unit_settings")).one()
        assert row[0] in (0, False) and row[1] is None and row[2] == 30
        # Every existing account starts at session generation 0.
        assert conn.execute(sa.text("SELECT token_version FROM users")).scalar() == 0
        conn.execute(sa.text("INSERT INTO projects (id, name, testing, capture_format) VALUES (2, 't', TRUE, 'JPEG')"))
        conn.execute(sa.text(
            "INSERT INTO collections (id, name, project_id, testing, kept_on_unit) VALUES (2, 't', 2, TRUE, TRUE)"))
        conn.execute(sa.text("INSERT INTO users (id, username, role) VALUES (2, 't', 'testing')"))
    # A testing volume not kept on the unit.
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE collections SET kept_on_unit = FALSE WHERE id = 2"))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO collections (id, name, project_id, testing) VALUES (3, 'x', 2, TRUE)"))
    # A second testing account.
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO users (id, username, role) VALUES (3, 'u', 'testing')"))
    # Any number of accounts in the other roles.
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, username, role) VALUES (4, 'b', 'admin')"))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO users (id, username, role) VALUES (5, 'v', 'visitor')"))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE unit_settings SET testing_expiry_days = 0"))

    # Downgrade refuses while anything of the testing space exists.
    with pytest.raises(RuntimeError, match="testing"):
        _run_migration(engine, mod.downgrade)
    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM collections WHERE testing")).scalar() == 1
        conn.execute(sa.text("DELETE FROM collections WHERE id = 2"))
        conn.execute(sa.text("DELETE FROM projects WHERE id = 2"))
        conn.execute(sa.text("DELETE FROM users WHERE id = 2"))

    _run_migration(engine, mod.downgrade)
    insp = sa.inspect(engine)
    assert "testing" not in {c["name"] for c in insp.get_columns("projects")}
    assert not {"testing", "testing_expires_at"} & {c["name"] for c in insp.get_columns("collections")}
    assert not {"testing_enabled", "testing_user_id", "testing_expiry_days"} & {
        c["name"] for c in insp.get_columns("unit_settings")}
    assert "token_version" not in {c["name"] for c in insp.get_columns("users")}
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO users (id, username, role) VALUES (6, 'w', 'testing')"))
    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM users")).scalar() == 2


@pytest.mark.unit
def test_the_migration_follows_kept_on_unit_and_is_the_only_head(backend_root):
    mod = _load_migration(backend_root)
    assert mod.revision == REVISION and mod.down_revision == "b6d8f0a2c4e7"
    revisions, downs = set(), set()
    for path in (backend_root / "alembic" / "versions").glob("*.py"):
        text = path.read_text()
        revisions.add(re.search(r"^revision(?:: str)? = '(\w+)'", text, re.M).group(1))
        downs.update(re.findall(r"'(\w+)'", re.search(r"^down_revision[^=]*= (.*)$", text, re.M).group(1)))
    # The head is now the languages and capture key revision (test_first_run.py), which follows this one.
    assert REVISION in downs
    assert len(revisions - downs) == 1


@pytest.mark.unit
def test_the_migration_upgrades_and_downgrades_on_sqlite(backend_root, tmp_path, migration_root):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    try:
        _exercise(engine, _load_migration(backend_root))
    finally:
        engine.dispose()


@pytest.mark.unit
def test_the_migration_upgrades_and_downgrades_on_postgresql(backend_root, migration_root):
    if "DATABASE_HOST" not in os.environ:
        pytest.skip("opt-in: set DATABASE_* to a PostgreSQL server")
    from sqlalchemy.exc import OperationalError

    from app.core.db import DATABASE_URL

    schema = f"captua_migtest_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(DATABASE_URL)
    try:
        with admin.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    except OperationalError as exc:
        admin.dispose()
        pytest.skip(f"DATABASE_* does not reach a PostgreSQL server: {exc.orig}")
    engine = sa.create_engine(DATABASE_URL, connect_args={"options": f"-csearch_path={schema}"})
    try:
        _exercise(engine, _load_migration(backend_root))
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.unit
def test_the_models_match_the_migration():
    from app.core.db import Base
    import app.models.collection  # noqa: F401
    import app.models.project  # noqa: F401
    import app.models.unit_settings  # noqa: F401
    import app.models.user  # noqa: F401

    tables = Base.metadata.tables
    for table, name in (("projects", "testing"), ("collections", "testing"),
                        ("unit_settings", "testing_enabled"), ("unit_settings", "testing_expiry_days")):
        column = tables[table].c[name]
        assert column.nullable is False and column.server_default is not None, (table, name)
    assert tables["collections"].c.testing_expires_at.nullable is True
    version = tables["users"].c.token_version
    assert version.nullable is False and version.server_default is not None
    assert str(version.server_default.arg) == "0"
    [fk] = tables["unit_settings"].c.testing_user_id.foreign_keys
    assert fk.column.table.name == "users" and fk.ondelete == "SET NULL"

    def checks(table):
        return {c.name: str(c.sqltext) for c in tables[table].constraints if isinstance(c, sa.CheckConstraint)}

    mod_checks = _load_migration_from_package()
    assert checks("collections")["check_collection_testing_kept"] == mod_checks.TESTING_KEPT_CHECK
    assert checks("users")["check_user_role"] == mod_checks.NEW_ROLE_CHECK
    assert checks("unit_settings")["check_unit_settings_testing_expiry_days"] == mod_checks.EXPIRY_CHECK
    [index] = [i for i in tables["users"].indexes if i.name == mod_checks.ONE_TESTING_ACCOUNT_INDEX]
    assert index.unique and [c.name for c in index.columns] == ["role"]
    for dialect in ("postgresql", "sqlite"):
        assert str(index.dialect_options[dialect]["where"]) == mod_checks.ONE_TESTING_ACCOUNT_WHERE


def _load_migration_from_package():
    from pathlib import Path
    return _load_migration(Path(__file__).resolve().parents[2])


# ---------------------------------------------------------------------------
# The models: `testing` fixed at insert, following the project or the parent
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_project_and_a_volume_are_real_work_unless_made_testing(db_session):
    p = _project(db_session, "Real")
    col = _volume(db_session, p, stem="xx-test-r1")
    assert p.testing is False and col.testing is False and col.kept_on_unit is False


@pytest.mark.unit
def test_a_volume_in_a_testing_project_is_testing_and_kept(db_session):
    p = _testing_project(db_session)
    col = _volume(db_session, p, stem=T + "t1")
    sub = _volume(db_session, parent=col, name="Sub", stem=T + "t2")
    assert (col.testing, col.kept_on_unit) == (True, True)
    assert (sub.testing, sub.kept_on_unit) == (True, True)


@pytest.mark.unit
def test_a_testing_volume_given_kept_false_at_insert_is_created_kept(db_session):
    p = _testing_project(db_session)
    col = _volume(db_session, p, stem=T + "t1", kept_on_unit=False)
    assert col.kept_on_unit is True


@pytest.mark.unit
@pytest.mark.parametrize("project_testing, asked", [(True, False), (False, True)])
def test_a_volume_whose_testing_differs_from_its_projects_is_refused(db_session, project_testing, asked):
    from app.models.collection import Collection
    from app.models.project import TestingSpaceError
    p = _testing_project(db_session) if project_testing else _project(db_session, "Real")
    db_session.add(Collection(name="X", project_id=p.id, testing=asked, kept_on_unit=True))
    with pytest.raises(TestingSpaceError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
@pytest.mark.parametrize("parent_testing", [True, False])
def test_a_sub_volume_whose_testing_differs_from_its_parents_is_refused(db_session, parent_testing):
    from app.models.collection import Collection
    from app.models.project import TestingSpaceError
    parent = _testing_volume(db_session) if parent_testing else _volume(
        db_session, _project(db_session, "Real"), stem="xx-test-r1")
    db_session.add(Collection(name="X", parent_collection_id=parent.id, testing=not parent_testing,
                              kept_on_unit=True))
    with pytest.raises(TestingSpaceError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
def test_a_new_volume_under_a_new_testing_project_follows_it_in_one_flush(db_session):
    from app.models.collection import Collection
    from app.models.project import Project
    p = Project(name=TP + "Both", testing=True)
    col = Collection(name="C", project=p)
    sub = Collection(name="S", parent_collection=col)
    db_session.add_all([p, col, sub])
    db_session.commit()
    assert (col.testing, col.kept_on_unit, sub.testing, sub.kept_on_unit) == (True, True, True, True)


@pytest.mark.unit
@pytest.mark.parametrize("start", [True, False])
def test_testing_never_changes_after_insert(db_session, start):
    from app.models.project import TestingSpaceError
    p = _testing_project(db_session) if start else _project(db_session, "Real")
    col = _volume(db_session, p, stem=(T + "c1") if start else "xx-test-c1")
    with pytest.raises(TestingSpaceError):
        p.testing = not start
    with pytest.raises(TestingSpaceError):
        col.testing = not start
    p.testing = start  # the same value changes nothing
    col.testing = start
    db_session.commit()


@pytest.mark.unit
def test_a_testing_volume_cannot_stop_being_kept(db_session):
    from app.models.project import TestingSpaceError
    col = _testing_volume(db_session)
    with pytest.raises(TestingSpaceError):
        col.kept_on_unit = False
    col.kept_on_unit = True
    db_session.commit()


@pytest.mark.unit
def test_a_real_volume_moved_under_a_testing_one_through_the_orm_is_refused(db_session):
    from app.models.project import TestingSpaceError
    testing = _testing_volume(db_session)
    real = _volume(db_session, _project(db_session, "Real"), stem="xx-test-r1")
    real.parent_collection_id, real.project_id = testing.id, None
    with pytest.raises(TestingSpaceError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
def test_the_database_refuses_a_testing_volume_not_kept(db_session):
    col = _testing_volume(db_session)
    with pytest.raises(IntegrityError):
        db_session.execute(sa.text("UPDATE collections SET kept_on_unit = 0 WHERE id = :i"), {"i": col.id})
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
def test_at_most_one_account_holds_the_testing_role(db_session):
    from app.models.user import User
    _testing_user(db_session)
    db_session.add(User(username="otra", hashed_password="x", role="testing"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
def test_the_unit_settings_carry_the_testing_space_off_by_default(db_session):
    from app.models.unit_settings import UnitSettings
    row = _unit(db_session, "backup")
    db_session.refresh(row)
    assert (row.testing_enabled, row.testing_user_id, row.testing_expiry_days) == (False, None, 30)
    user = _testing_user(db_session)
    row.testing_user_id = user.id
    db_session.commit()
    row.testing_expiry_days = 0
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    assert db_session.get(UnitSettings, 1).testing_user_id == user.id


# ---------------------------------------------------------------------------
# The role
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_testing_role_is_valid_but_never_given_through_the_role_update():
    from pydantic import ValidationError

    from app.schemas.user import UserCreate, UserRoleUpdate, VALID_ROLES
    assert "testing" in VALID_ROLES
    with pytest.raises(ValidationError):
        UserRoleUpdate(role="testing")
    with pytest.raises(ValidationError):
        UserCreate(username="x", password="fake-pw-8", role="testing")


@pytest.mark.unit
def test_the_testing_account_is_refused_by_every_role_allowlist(client, db_session):
    """Outside the routes app.core.space_routes admits it to (tests/unit/test_testing_space_access.py)."""
    _project(db_session, "Real")
    for path in ("/records/to-retake", "/system/logs", "/auth/users"):
        assert _as(client, "testing").get(path).status_code == 403, path


@pytest.mark.unit
def test_no_account_is_made_the_testing_account_through_the_role_route(client, db_session):
    from app.models.user import User
    user = User(username="op", hashed_password="x", role="operator")
    db_session.add(user)
    db_session.commit()
    resp = _as(client, "admin").patch(f"/auth/users/{user.id}/role", json={"role": "testing"})
    assert resp.status_code == 422, resp.text
    db_session.expire_all()
    assert db_session.get(User, user.id).role == "operator"


@pytest.mark.unit
@pytest.mark.parametrize("call", [
    lambda c, uid: c.patch(f"/auth/users/{uid}/role", json={"role": "operator"}),
    lambda c, uid: c.patch(f"/auth/users/{uid}/active", params={"is_active": False}),
    lambda c, uid: c.delete(f"/auth/users/{uid}"),
], ids=["role", "active", "delete"])
def test_the_testing_account_is_managed_only_through_the_testing_settings(client, db_session, call):
    from app.models.user import User
    user = _testing_user(db_session)
    _detail(call(_as(client, "admin"), user.id), 409, "testing_account")
    db_session.expire_all()
    stored = db_session.get(User, user.id)
    assert stored is not None and (stored.role, stored.is_active) == ("testing", True)


@pytest.mark.unit
def test_the_testing_account_cannot_change_its_own_password(client, db_session):
    from app.core.security import create_access_token, verify_password
    from app.models.user import User
    user = _testing_user(db_session)
    resp = client.post("/auth/password-reset",
                       json={"old_password": "secret-pw", "new_password": "another-pw"},
                       headers={"Authorization": f"Bearer {create_access_token(str(user.id))}"})
    _detail(resp, 403, "testing_account")
    db_session.expire_all()
    assert verify_password("secret-pw", db_session.get(User, user.id).hashed_password)


@pytest.mark.unit
def test_the_operators_sent_to_fisqua_never_include_the_testing_account(db_session):
    from app.core.fisqua_queue import operators_body
    from app.core import fisqua_contract as fc
    from app.models.user import User
    db_session.add(User(username="op", hashed_password="x", role="operator"))
    db_session.commit()
    _testing_user(db_session)
    names = [entry["username"] for entry in operators_body(db_session, mode="fisqua")[fc.F_OPERATORS]]
    assert names == ["op"]


# ---------------------------------------------------------------------------
# The kept machinery: a testing volume is never released, never counted
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("mode", ["offline", "backup"])
def test_a_testing_volume_is_never_released(client, db_session, mode):
    _unit(db_session, mode)
    col = _testing_volume(db_session)
    _detail(_as(client, "admin").post(f"/collections/{col.id}/release-from-unit"), 409, "testing_volume")
    db_session.expire_all()
    assert db_session.get(type(col), col.id).kept_on_unit is True
    assert _logs(db_session, "volume_released_from_unit") == []


@pytest.mark.unit
def test_keeping_a_testing_volume_is_refused_as_a_testing_volume(client, db_session):
    _unit(db_session, "backup")
    col = _testing_volume(db_session)
    _detail(_as(client, "admin").post(f"/collections/{col.id}/keep-on-unit"), 409, "testing_volume")
    assert _logs(db_session, "volume_kept_on_unit") == []


@pytest.mark.unit
def test_testing_volumes_alone_need_no_kept_volumes_decision(client, db_session, link):
    from app.core import unit_mode
    _unit(db_session, "offline")
    col = _testing_volume(db_session)
    resp = _as(client, "admin").put("/system/unit", json={"mode": "backup"})
    assert resp.status_code == 200, resp.text
    assert unit_mode.current(db_session).mode == "backup"
    db_session.expire_all()
    assert db_session.get(type(col), col.id).kept_on_unit is True


@pytest.mark.unit
def test_including_the_kept_volumes_leaves_the_testing_volumes_kept(client, db_session, link):
    from app.models.collection import Collection
    _unit(db_session, "offline")
    testing = _testing_volume(db_session)
    kept = _volume(db_session, _project(db_session, "Real"), stem="xx-test-k1", kept_on_unit=True)
    resp = _as(client, "admin").put("/system/unit", json={"mode": "backup", "keptVolumes": "include"})
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert db_session.get(Collection, testing.id).kept_on_unit is True
    assert db_session.get(Collection, kept.id).kept_on_unit is False
    [log] = _logs(db_session, "kept_volumes_released")
    assert log.detail.startswith("1 volume(s)") and str(testing.id) not in log.detail.split("ids ")[1].split(", ")


@pytest.mark.unit
def test_the_kept_volumes_listed_and_counted_leave_out_the_testing_volumes(client, db_session):
    from app.core.image_upload import unit_upload_summary
    from app.core.kept_volumes import kept_volumes
    _unit(db_session, "offline")
    _testing_volume(db_session)
    kept = _volume(db_session, _project(db_session, "Real"), stem="xx-test-k1", kept_on_unit=True)
    assert [v["id"] for v in kept_volumes(db_session)] == [kept.id]
    assert [v["id"] for v in _as(client, "admin").get("/system/unit").json()["keptVolumes"]] == [kept.id]
    summary = unit_upload_summary(db_session)
    assert summary["kept_volumes"] == 1 and summary["volumes"] == 0


@pytest.mark.unit
def test_the_kept_files_count_leaves_out_the_testing_volumes(db_session, monkeypatch):
    from app.core import image_upload
    testing = _testing_volume(db_session)
    kept = _volume(db_session, _project(db_session, "Real"), stem="xx-test-k1", kept_on_unit=True)

    def counts(db, collection_ids=None):
        return {cid: {"total": n, "verified": 0, "pending": n, "refused": 0, "last_verified_at": None,
                      "refusals": [], "freed_files": 0, "freed_bytes": 0, "received": False,
                      "kept_on_unit": True}
                for cid, n in ((testing.id, 5), (kept.id, 2))}

    monkeypatch.setattr(image_upload, "_object_counts", counts)
    assert image_upload.unit_upload_summary(db_session)["kept_files"] == 2


# ---------------------------------------------------------------------------
# Moves never cross between testing and real work
# ---------------------------------------------------------------------------

def _pair(db, from_testing):
    """(source volume, target volume) on either side of the line."""
    testing = _testing_volume(db, stem=T + "t1")
    real = _volume(db, _project(db, "Real"), stem="xx-test-r1", kept_on_unit=True)
    return (testing, real) if from_testing else (real, testing)


@pytest.mark.unit
@pytest.mark.parametrize("from_testing", [True, False])
def test_move_records_across_the_testing_line_is_refused(client, db_session, from_testing):
    from app.models.record import Record
    src, dst = _pair(db_session, from_testing)
    rec = _record(db_session, src.id)
    resp = _as(client, "operator").post(f"/collections/{src.id}/move-records",
                                        params={"target_collection_id": dst.id})
    _detail(resp, 409, "cross_space")
    db_session.expire_all()
    assert db_session.get(Record, rec.id).collection_id == src.id


@pytest.mark.unit
def test_move_records_between_two_testing_volumes_goes_ahead(client, db_session):
    p = _testing_project(db_session)
    src = _volume(db_session, p, name="A", stem=T + "t1")
    dst = _volume(db_session, p, name="B", stem=T + "t2")
    _record(db_session, src.id)
    resp = _as(client, "operator").post(f"/collections/{src.id}/move-records",
                                        params={"target_collection_id": dst.id})
    assert resp.status_code == 200, resp.text


@pytest.mark.unit
@pytest.mark.parametrize("from_testing", [True, False])
def test_patching_a_records_volume_across_the_testing_line_is_refused(client, db_session, from_testing):
    from app.models.record import Record
    src, dst = _pair(db_session, from_testing)
    rec = _record(db_session, src.id)
    _detail(_as(client, "operator").patch(f"/records/{rec.id}", json={"collection_id": dst.id}),
            409, "cross_space")
    db_session.expire_all()
    assert db_session.get(Record, rec.id).collection_id == src.id


@pytest.mark.unit
@pytest.mark.parametrize("from_testing", [True, False])
def test_patching_a_loose_records_project_across_the_testing_line_is_refused(client, db_session, from_testing):
    from app.models.record import Record
    testing, real = _testing_project(db_session), _project(db_session, "Real")
    src, dst = (testing, real) if from_testing else (real, testing)
    rec = _record(db_session, project_id=src.id)
    _detail(_as(client, "operator").patch(f"/records/{rec.id}", json={"project_id": dst.id}),
            409, "cross_space")
    db_session.expire_all()
    assert db_session.get(Record, rec.id).project_id == src.id


@pytest.mark.unit
@pytest.mark.parametrize("from_testing", [True, False])
def test_add_record_to_a_project_across_the_testing_line_is_refused(client, db_session, from_testing):
    from app.models.record import Record
    testing, real = _testing_project(db_session), _project(db_session, "Real")
    src, dst = (testing, real) if from_testing else (real, testing)
    rec = _record(db_session, project_id=src.id)
    _detail(_as(client, "operator").post(f"/projects/{dst.id}/add_record/{rec.id}"), 409, "cross_space")
    db_session.expire_all()
    assert db_session.get(Record, rec.id).project_id == src.id


@pytest.mark.unit
def test_add_record_out_of_a_testing_volume_into_its_real_project_is_refused(client, db_session):
    testing = _testing_volume(db_session)
    real = _project(db_session, "Real")
    rec = _record(db_session, testing.id)
    _detail(_as(client, "operator").post(f"/projects/{real.id}/add_record/{rec.id}"), 409, "cross_space")


@pytest.mark.unit
@pytest.mark.parametrize("from_testing", [True, False])
def test_reparenting_a_volume_across_the_testing_line_is_refused(client, db_session, from_testing):
    from app.models.collection import Collection
    moving, parent = _pair(db_session, from_testing)
    _detail(_as(client, "operator").patch(f"/collections/{moving.id}",
                                          json={"parent_collection_id": parent.id}), 409, "cross_space")
    db_session.expire_all()
    assert db_session.get(Collection, moving.id).parent_collection_id is None


@pytest.mark.unit
@pytest.mark.parametrize("from_testing", [True, False])
def test_moving_a_projects_volumes_across_the_testing_line_is_refused(client, db_session, from_testing):
    from app.models.collection import Collection
    testing, real = _testing_project(db_session), _project(db_session, "Real")
    src, dst = (testing, real) if from_testing else (real, testing)
    col = _volume(db_session, src, stem=(T + "m1") if from_testing else "xx-test-m1")
    _detail(_as(client, "operator").post(f"/projects/{src.id}/move-collections",
                                         json={"target_project_id": dst.id}), 409, "cross_space")
    db_session.expire_all()
    assert db_session.get(Collection, col.id).project_id == src.id


@pytest.mark.unit
def test_moving_a_projects_volumes_between_two_real_projects_goes_ahead(client, db_session):
    a, b = _project(db_session, "A"), _project(db_session, "B")
    _volume(db_session, a, stem="xx-test-m1")
    resp = _as(client, "operator").post(f"/projects/{a.id}/move-collections", json={"target_project_id": b.id})
    assert resp.status_code == 200, resp.text


@pytest.mark.unit
def test_the_cross_space_helper_reads_the_volume_before_the_project(db_session):
    from app.core import testing_space
    testing = _testing_volume(db_session)
    real = _project(db_session, "Real")
    # A volume's space is its own, whatever project id is passed beside it.
    assert testing_space.is_testing(db_session, project_id=real.id, collection_id=testing.id) is True
    assert testing_space.is_testing(db_session, project_id=real.id) is False
    assert testing_space.is_testing(db_session) is False
    testing_space.refuse_cross_space(db_session, (real.id, None), (None, None))
    with pytest.raises(testing_space.SpaceRefused) as exc:
        testing_space.refuse_cross_space(db_session, (real.id, None), (None, testing.id))
    assert (exc.value.status, exc.value.code) == (409, "cross_space")


# ---------------------------------------------------------------------------
# The default project and the list import never pick a testing project
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_default_project_is_never_the_only_project_when_it_is_testing(db_session, override_projects_root,
                                                                         tmp_path, monkeypatch):
    from app.core import list_import, unit_mode
    monkeypatch.setattr(list_import, "_take_project_lock", lambda: object())
    monkeypatch.setattr(list_import, "_release_project_lock", lambda fd: None)
    _unit(db_session, "backup")
    testing = _testing_project(db_session)
    project, created = unit_mode.default_project(db_session, "boss")
    assert project.id != testing.id and project.testing is False and created is True


@pytest.mark.unit
def test_a_stored_default_project_that_is_testing_is_not_used(db_session, monkeypatch):
    from app.core import list_import, unit_mode
    monkeypatch.setattr(list_import, "_take_project_lock", lambda: object())
    monkeypatch.setattr(list_import, "_release_project_lock", lambda fd: None)
    testing = _testing_project(db_session)
    _unit(db_session, "backup", default_project_id=testing.id)
    real = _project(db_session, "Real")
    project, _ = unit_mode.default_project(db_session, "boss")
    assert project.id == real.id
    assert unit_mode.stored_default_project(db_session).id == real.id


@pytest.mark.unit
def test_the_list_import_never_takes_a_testing_project_by_its_name(db_session, monkeypatch):
    """A list named as a testing project's display name makes a real project of that name beside it."""
    from app.core import list_import
    monkeypatch.setattr(list_import, "_take_project_lock", lambda: object())
    monkeypatch.setattr(list_import, "_release_project_lock", lambda fd: None)
    testing = _testing_project(db_session, "Basket")
    assert list_import._find_project(db_session, "basket") is None
    assert list_import._find_project(db_session, TP + "Basket") is None
    project, created = list_import._project_for(db_session, "Basket", "boss", True)
    assert created and (project.name, project.testing) == ("Basket", False) and project.id != testing.id


# ---------------------------------------------------------------------------
# Leaving a place for no place is a move too
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_removing_a_record_from_a_testing_project_is_refused(client, db_session):
    from app.models.record import Record
    testing = _testing_project(db_session)
    rec = _record(db_session, project_id=testing.id)
    _detail(_as(client, "operator").post(f"/projects/{testing.id}/remove_record/{rec.id}"), 409, "cross_space")
    db_session.expire_all()
    stored = db_session.get(Record, rec.id)
    assert (stored.project_id, stored.collection_id) == (testing.id, None)


@pytest.mark.unit
def test_removing_a_record_from_a_real_project_still_works(client, db_session):
    from app.models.record import Record
    real = _project(db_session, "Real")
    rec = _record(db_session, project_id=real.id)
    assert _as(client, "operator").post(f"/projects/{real.id}/remove_record/{rec.id}").status_code == 200
    db_session.expire_all()
    assert db_session.get(Record, rec.id).project_id is None


@pytest.mark.unit
def test_patching_a_record_out_of_a_testing_project_to_no_place_is_refused(client, db_session):
    testing = _testing_project(db_session)
    rec = _record(db_session, project_id=testing.id)
    _detail(_as(client, "operator").patch(f"/records/{rec.id}", json={"project_id": None}), 409, "cross_space")


# ---------------------------------------------------------------------------
# POST /collections/ decides the new volume's space before any Fisqua lookup
# ---------------------------------------------------------------------------

def _no_lookup(monkeypatch):
    from app.core import unit_mode
    asked = []
    monkeypatch.setattr(unit_mode, "check_reference_codes", lambda db, codes: asked.append(list(codes)))
    return asked


@pytest.mark.unit
def test_a_volume_posted_into_a_testing_project_in_fisqua_mode_is_not_looked_up(client, db_session, link,
                                                                               monkeypatch):
    # Projects are shown only in offline mode (UnitState.projects_shown), so a
    # top-level volume posted in fisqua mode goes into the default project.
    # The seam shows them here to reach the route's order of decisions.
    from app.core import unit_mode
    from app.models.collection import Collection
    _unit(db_session, "fisqua", projects_enabled=True)
    monkeypatch.setattr(unit_mode.UnitState, "projects_shown", property(lambda self: True))
    asked = _no_lookup(monkeypatch)
    testing = _testing_project(db_session)
    resp = _as(client, "admin").post("/collections/", json={
        "name": "T", "project_id": testing.id, "filename_stem": T + "t9"})
    assert resp.status_code == 201, resp.text
    assert asked == []
    col = db_session.get(Collection, resp.json()["id"])
    assert (col.testing, col.kept_on_unit) == (True, True)


@pytest.mark.unit
def test_a_sub_volume_of_a_testing_volume_in_fisqua_mode_is_not_looked_up(client, db_session, link, monkeypatch):
    from app.models.collection import Collection
    _unit(db_session, "fisqua")
    asked = _no_lookup(monkeypatch)
    parent = _testing_volume(db_session)
    resp = _as(client, "admin").post("/collections/", json={
        "name": "S", "parent_collection_id": parent.id, "filename_stem": T + "t8"})
    assert resp.status_code == 201, resp.text
    assert asked == []
    col = db_session.get(Collection, resp.json()["id"])
    assert (col.testing, col.kept_on_unit) == (True, True)


# ---------------------------------------------------------------------------
# The testing account is no project's member
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_testing_account_is_not_added_to_a_project(client, db_session):
    from app.models.project_member import ProjectMember
    user = _testing_user(db_session)
    real = _project(db_session, "Real")
    resp = _as(client, "admin").post(f"/projects/{real.id}/members", json={"user_id": user.id, "role": "operator"})
    _detail(resp, 409, "testing_account")
    assert db_session.query(ProjectMember).count() == 0


# ---------------------------------------------------------------------------
# The default project's name is real work's; the testing space has its own namespace
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_real_project_named_default_beside_testing_projects_still_works(client, db_session):
    _testing_project(db_session, "Pruebas A")
    _testing_project(db_session, "Pruebas B")
    resp = _as(client, "admin").post("/projects/", json={"name": "Default"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Default"
    real = _project(db_session, "Otro")
    renamed = _as(client, "admin").put(f"/projects/{real.id}", json={"name": "Por defecto"})
    assert renamed.status_code == 200, renamed.text


@pytest.mark.unit
def test_the_default_project_is_real_with_testing_projects_present(db_session, monkeypatch):
    from app.core import list_import, unit_mode
    monkeypatch.setattr(list_import, "_take_project_lock", lambda: object())
    monkeypatch.setattr(list_import, "_release_project_lock", lambda fd: None)
    _unit(db_session, "backup")
    _testing_project(db_session, "Pruebas A")
    _testing_project(db_session, "Pruebas B")
    project, created = unit_mode.default_project(db_session, "boss")
    assert (project.name, project.testing, created) == ("default", False, True)
    again, created = unit_mode.default_project(db_session, "boss")
    assert (again.id, created) == (project.id, False)


# ---------------------------------------------------------------------------
# The queue: how far a list name refused for a testing project reaches
# ---------------------------------------------------------------------------

def _queue_body(list_name, codes):
    from tests.unit.test_fisqua_queue import _qitem
    return {"listName": list_name, "items": [_qitem(code) for code in codes]}


@pytest.mark.unit
def test_in_fisqua_mode_a_queue_list_named_as_a_testing_project_is_imported(db_session, link):
    from datetime import datetime, timezone

    from app.core.fisqua_queue import apply_queue
    _unit(db_session, "fisqua")
    _testing_project(db_session, "Lista")
    ack, report, _ = apply_queue(db_session, _queue_body("Lista", ["xx-test-901", "xx-test-902"]), "r1",
                                 datetime.now(timezone.utc))
    assert ack["skipped"] == [] and report["created"] == 2
    assert report["listName"] == "Lista" and report["projectName"] == "default"


@pytest.mark.unit
def test_with_projects_shown_a_list_named_as_a_testing_project_goes_into_a_real_one(db_session, link,
                                                                                 monkeypatch):
    # Projects are shown only in offline mode, where the sync does not run;
    # the seam shows them to reach the list's own project.
    from datetime import datetime, timezone

    from app.core import list_import, unit_mode
    from app.core.fisqua_queue import apply_queue
    from app.models.project import Project
    monkeypatch.setattr(list_import, "_take_project_lock", lambda: object())
    monkeypatch.setattr(list_import, "_release_project_lock", lambda fd: None)
    monkeypatch.setattr(unit_mode.UnitState, "projects_shown", property(lambda self: True))
    _unit(db_session, "fisqua")
    testing = _testing_project(db_session, "Lista")
    now = datetime.now(timezone.utc)
    ack, report, _ = apply_queue(db_session, _queue_body("lista", ["xx-test-901", "xx-test-902"]), "r1", now)
    assert ack["skipped"] == [] and report["created"] == 2
    project = db_session.get(Project, report["projectId"])
    assert (project.name, project.testing) == ("lista", False) and project.id != testing.id


# ---------------------------------------------------------------------------
# The testing space's namespace: the prefix, the two model rules, the display name
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_prefix_is_defined_once():
    from pathlib import Path

    from app.core.project_names import TESTING_PREFIX
    assert TESTING_PREFIX == T
    app_dir = Path(__file__).resolve().parents[2] / "app"
    holders = sorted(str(f.relative_to(app_dir)) for f in app_dir.rglob("*.py") if T in f.read_text())
    assert holders == ["core/project_names.py"]


@pytest.mark.unit
def test_the_prefix_leaves_room_for_a_stem_of_twelve_characters():
    from app.models.collection import FILENAME_STEM_MAX_LENGTH, FILENAME_STEM_PATTERN
    assert FILENAME_STEM_MAX_LENGTH - len(T) == 12
    assert re.match(FILENAME_STEM_PATTERN, T + "a")
    assert re.match(FILENAME_STEM_PATTERN, T + "a" * 12) and len(T + "a" * 12) == FILENAME_STEM_MAX_LENGTH


@pytest.mark.unit
@pytest.mark.parametrize("name", ["Pruebas", "captua-testing", "Captua-Testing-x", " " + TP + "x", T + "x"])
def test_a_testing_project_outside_the_namespace_is_refused(db_session, name):
    from app.models.project import Project, TestingSpaceError
    db_session.add(Project(name=name, testing=True))
    with pytest.raises(TestingSpaceError) as exc:
        db_session.commit()
    db_session.rollback()
    assert exc.value.code == "testing_name_required"


@pytest.mark.unit
def test_a_testing_project_renamed_out_of_the_namespace_is_refused(db_session):
    from app.models.project import TestingSpaceError
    testing = _testing_project(db_session)
    testing.name = "Pruebas"
    with pytest.raises(TestingSpaceError) as exc:
        db_session.commit()
    db_session.rollback()
    assert exc.value.code == "testing_name_required"


RESERVED_REAL_NAMES = [T + "x", "Captua-Testing-x", "cáptua-testing-x", ".captua-testing-x", "CAPTUA-TESTING-"]


@pytest.mark.unit
@pytest.mark.parametrize("name", RESERVED_REAL_NAMES)
def test_a_real_project_in_the_namespace_is_refused_on_insert_and_on_rename(db_session, name):
    from app.models.project import Project, TestingSpaceError
    db_session.add(Project(name=name))
    with pytest.raises(TestingSpaceError) as exc:
        db_session.commit()
    db_session.rollback()
    assert exc.value.code == "name_reserved"
    real = _project(db_session, "Real")
    real.name = name
    with pytest.raises(TestingSpaceError) as exc:
        db_session.commit()
    db_session.rollback()
    assert exc.value.code == "name_reserved"


@pytest.mark.unit
@pytest.mark.parametrize("name", ["captua-testingx", "captua testing x", "x" + T, "Captua"])
def test_a_real_project_beside_the_namespace_is_allowed(db_session, name):
    assert _project(db_session, name).name == name


@pytest.mark.unit
@pytest.mark.parametrize("field", ["filename_stem", "external_id"])
def test_a_testing_volumes_codes_begin_with_the_prefix(db_session, field):
    from app.models.collection import Collection
    from app.models.project import TestingSpaceError
    p = _testing_project(db_session)
    db_session.add(Collection(name="X", project_id=p.id, **{field: "xx-test-t1"}))
    with pytest.raises(TestingSpaceError) as exc:
        db_session.commit()
    db_session.rollback()
    assert exc.value.code == "testing_name_required"
    col = _volume(db_session, p, stem=None)
    setattr(col, field, "xx-test-t2")
    with pytest.raises(TestingSpaceError) as exc:
        db_session.commit()
    db_session.rollback()
    assert exc.value.code == "testing_name_required"
    both = _volume(db_session, p, name="Y", stem=T + "t3", external_id=T + "t3")
    assert (both.testing, both.filename_stem, both.external_id) == (True, T + "t3", T + "t3")


@pytest.mark.unit
@pytest.mark.parametrize("field", ["filename_stem", "external_id"])
def test_a_real_volumes_codes_never_begin_with_the_prefix(db_session, field):
    from app.models.collection import Collection
    from app.models.project import TestingSpaceError
    p = _project(db_session, "Real")
    db_session.add(Collection(name="X", project_id=p.id, **{field: T + "r1"}))
    with pytest.raises(TestingSpaceError) as exc:
        db_session.commit()
    db_session.rollback()
    assert exc.value.code == "name_reserved"
    col = _volume(db_session, p, stem=None)
    setattr(col, field, T + "r2")
    with pytest.raises(TestingSpaceError) as exc:
        db_session.commit()
    db_session.rollback()
    assert exc.value.code == "name_reserved"


@pytest.mark.unit
def test_a_sub_volume_of_a_testing_volume_follows_its_namespace_in_one_flush(db_session):
    from app.models.collection import Collection
    from app.models.project import TestingSpaceError
    parent = _testing_volume(db_session)
    db_session.add(Collection(name="S", parent_collection=parent, filename_stem="xx-test-s1"))
    with pytest.raises(TestingSpaceError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
def test_the_display_name_and_the_stored_name_are_each_others_inverse(db_session):
    from app.core import testing_space
    from app.core.project_names import display_project_name
    from app.schemas.project import ProjectRead
    testing = _testing_project(db_session, "Pruebas")
    real = _project(db_session, "Real")
    assert (testing.name, testing.display_name) == (TP + "Pruebas", "Pruebas")
    assert (real.name, real.display_name) == ("Real", "Real")
    assert ProjectRead.model_validate(testing).name == "Pruebas"
    assert ProjectRead.model_validate(real).name == "Real"
    for sent in ("Pruebas", T + "x", " Défault ", ""):
        assert display_project_name(testing_space.stored_project_name(True, sent)) == sent
        assert testing_space.stored_project_name(False, sent) == sent


# ---------------------------------------------------------------------------
# Real work never looks a testing project up by name
# ---------------------------------------------------------------------------

def _envelope(name):
    return {"contract": "captua-digitisation-list", "kind": "digitisation-list", "version": "1.0",
            "id": str(uuid.uuid4()), "list": {"name": name}, "items": []}


@pytest.mark.unit
@pytest.mark.parametrize("name", [T + "Lista", "Captua-Testing-Lista", "cáptua-testing-lista"])
def test_a_list_named_in_the_testing_namespace_is_refused(name):
    from app.core import list_import
    document = _envelope(name)
    document["contract"], document["kind"] = list_import.CONTRACT_NAME, list_import.LIST_KIND
    with pytest.raises(list_import.ListRefused, match="testing space"):
        list_import.check_envelope(document)


@pytest.mark.unit
@pytest.mark.parametrize("name", ["日本", "..."])
def test_a_list_whose_name_gives_no_directory_is_refused(name):
    from app.core import list_import
    document = _envelope(name)
    document["contract"], document["kind"] = list_import.CONTRACT_NAME, list_import.LIST_KIND
    with pytest.raises(list_import.ListRefused, match="directory"):
        list_import.check_envelope(document)


@pytest.mark.unit
def test_the_default_name_reservation_is_gone():
    from app.core import list_import, project_names, testing_space
    import app.models.project as project_model
    for module, name in ((list_import, "_refuse_a_testing_projects_name"), (list_import, "_project_named"),
                         (list_import, "TESTING_PROJECT_NAME_MESSAGE"), (project_names, "is_default_projects_name"),
                         (project_names, "DEFAULT_PROJECT_NAME"), (testing_space, "refuse_reserved_name"),
                         (project_model, "_a_testing_project_never_has_the_default_projects_name")):
        assert not hasattr(module, name), name


@pytest.mark.unit
@pytest.mark.parametrize("name", ["default", "Défault", "Default "])
def test_a_testing_project_with_a_default_display_name_leaves_the_default_project_alone(
        db_session, monkeypatch, name):
    from app.core import list_import, unit_mode
    monkeypatch.setattr(list_import, "_take_project_lock", lambda: object())
    monkeypatch.setattr(list_import, "_release_project_lock", lambda fd: None)
    _unit(db_session, "backup")
    testing = _testing_project(db_session, name)
    project, created = unit_mode.default_project(db_session, "boss")
    assert (project.name, project.testing, created) == ("default", False, True) and project.id != testing.id
    assert list_import._find_project(db_session, name) is None or list_import._find_project(
        db_session, name).id == project.id


# ---------------------------------------------------------------------------
# The migration refuses real rows already in the testing namespace
# ---------------------------------------------------------------------------

def _before_with_codes(engine):
    _before(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE collections ADD COLUMN filename_stem VARCHAR(27)"))
        conn.execute(sa.text("ALTER TABLE collections ADD COLUMN external_id VARCHAR(255)"))


@pytest.mark.unit
@pytest.mark.parametrize("insert, named", [
    ("INSERT INTO projects (id, name) VALUES (7, 'captua-testing-viejo')", "project 7"),
    ("INSERT INTO projects (id, name) VALUES (7, 'Cáptua-Testing-Viejo')", "project 7"),
    ("INSERT INTO collections (id, name, project_id, filename_stem) VALUES (7, 'v', 1, 'captua-testing-t1')",
     "volume 7"),
    ("INSERT INTO collections (id, name, project_id, external_id) VALUES (7, 'v', 1, 'CAPTUA-TESTING-T1')",
     "volume 7"),
])
def test_the_migration_refuses_rows_in_the_testing_namespace(backend_root, tmp_path, migration_root, insert, named):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    try:
        _before_with_codes(engine)
        with engine.begin() as conn:
            conn.execute(sa.text(insert))
        with pytest.raises(RuntimeError, match=named):
            _run_migration(engine, _load_migration(backend_root).upgrade)
        assert "testing" not in {c["name"] for c in sa.inspect(engine).get_columns("projects")}
    finally:
        engine.dispose()


@pytest.mark.unit
def test_the_migration_upgrades_with_names_beside_the_namespace(backend_root, tmp_path, migration_root):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    try:
        _before_with_codes(engine)
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO projects (id, name) VALUES (7, 'captua testing')"))
            conn.execute(sa.text(
                "INSERT INTO collections (id, name, project_id, filename_stem) VALUES (7, 'v', 1, 'captua-t1')"))
        _run_migration(engine, _load_migration(backend_root).upgrade)
        assert "testing" in {c["name"] for c in sa.inspect(engine).get_columns("projects")}
    finally:
        engine.dispose()


@pytest.mark.unit
def test_a_queue_list_named_in_the_testing_namespace_is_refused(db_session, link, monkeypatch):
    from datetime import datetime, timezone

    from app.core import list_import, unit_mode
    from app.core.fisqua_queue import apply_queue
    from app.models.project import Project
    monkeypatch.setattr(list_import, "_take_project_lock", lambda: object())
    monkeypatch.setattr(list_import, "_release_project_lock", lambda fd: None)
    monkeypatch.setattr(unit_mode.UnitState, "projects_shown", property(lambda self: True))
    _unit(db_session, "fisqua")
    from app.core.fisqua_queue import QueueUnreadable
    with pytest.raises(QueueUnreadable, match="testing space"):
        apply_queue(db_session, _queue_body(T + "Cola", ["xx-test-905"]), "r1", datetime.now(timezone.utc))
    assert db_session.query(Project).count() == 0


# ---------------------------------------------------------------------------
# A list or queue item in the testing namespace is refused as an item
# ---------------------------------------------------------------------------

RESERVED_CODES = ["captua-testing-1", "CAPTUA-TESTING-1", "ｃａｐｔｕａ-ｔｅｓｔｉｎｇ-1"]


@pytest.fixture
def imports_root(tmp_path, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config.settings, "DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data" / "imports"


@pytest.mark.unit
@pytest.mark.parametrize("code", RESERVED_CODES)
def test_a_list_item_in_the_testing_namespace_is_refused_and_the_rest_imported(db_session, imports_root, code):
    from app.core import list_import
    from tests.unit.test_list_import import _item, _list_with, _reasons, _volumes
    doc = _list_with([_item(code="xx-test-901", stem="xx-test-901", sequence=1),
                      _item(code=code, stem=code, sequence=2),
                      _item(code="xx-test-903", stem="xx-test-903", sequence=3)])
    report = list_import.import_list(db_session, doc, "ad", True)
    assert report["created"] == 2
    assert _reasons(report) == [(2, code, "reference_code_reserved")]
    assert set(_volumes(db_session)) == {"xx-test-901", "xx-test-903"}


@pytest.mark.unit
def test_a_testing_namespace_refusal_at_the_flush_is_one_items_refusal(db_session, imports_root, monkeypatch):
    """Whatever check lets an item through, the model's refusal at the flush rejects that item only."""
    from app.core import list_import
    from tests.unit.test_list_import import _item, _list_with, _reasons, _volumes
    monkeypatch.setattr(list_import, "_in_reserved_namespace", lambda value: False)
    doc = _list_with([_item(code="xx-test-901", stem="xx-test-901", sequence=1),
                      _item(code=T + "1", stem=T + "1", sequence=2),
                      _item(code="xx-test-903", stem="xx-test-903", sequence=3)])
    report = list_import.import_list(db_session, doc, "ad", True)
    assert report["created"] == 2
    assert _reasons(report) == [(2, T + "1", "reference_code_reserved")]
    assert set(_volumes(db_session)) == {"xx-test-901", "xx-test-903"}


@pytest.mark.unit
@pytest.mark.parametrize("code", RESERVED_CODES)
def test_a_queue_item_in_the_testing_namespace_is_skipped_and_the_rest_created(db_session, link, code):
    from datetime import datetime, timezone

    from app.core.fisqua_queue import apply_queue
    from app.models.collection import Collection
    from tests.unit.test_fisqua_queue import _qitem
    _unit(db_session, "fisqua")
    body = {"listName": "Cola", "items": [_qitem("xx-test-901", sequence=1), _qitem(code, sequence=2),
                                          _qitem("xx-test-903", sequence=3)]}
    reserved_id = body["items"][1]["itemId"]
    for revision in ("r1", "r2"):
        ack, report, _ = apply_queue(db_session, body, revision, datetime.now(timezone.utc))
        assert [(s["itemId"], s["reason"]) for s in ack["skipped"]] == [(reserved_id, "reference_code_reserved")]
    db_session.expire_all()
    assert sorted(c.external_id for c in db_session.query(Collection).all()) == ["xx-test-901", "xx-test-903"]


# ---------------------------------------------------------------------------
# The migration refuses directories and retired manifests in the testing namespace
# ---------------------------------------------------------------------------

@pytest.fixture
def migration_root(tmp_path, monkeypatch):
    """The projects root the migration reads: a temporary one, with no storage override."""
    from app.core import config, storage_override
    root = tmp_path / "projects"
    monkeypatch.setattr(config.settings, "PROJECTS_ROOT", str(root))
    monkeypatch.setattr(storage_override, "get_storage_override_or_fallback", lambda: (None, False))
    return root


@pytest.mark.unit
@pytest.mark.parametrize("make, named", [
    (lambda root: (root / "captua-testing-viejo" / "metadata").mkdir(parents=True), "captua-testing-viejo"),
    (lambda root: ((root / ".retired-manifests").mkdir(parents=True),
                   (root / ".retired-manifests" / "captua-testing-viejo-20250101T000000Z-manifest.jsonl")
                   .write_text("{}\n")), "captua-testing-viejo-20250101T000000Z-manifest.jsonl"),
])
def test_the_migration_refuses_a_projects_root_holding_the_testing_namespace(backend_root, tmp_path,
                                                                             migration_root, make, named):
    make(migration_root)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    try:
        _before(engine)
        with pytest.raises(RuntimeError, match=re.escape(named)):
            _run_migration(engine, _load_migration(backend_root).upgrade)
        assert "testing" not in {c["name"] for c in sa.inspect(engine).get_columns("projects")}
    finally:
        engine.dispose()


@pytest.mark.unit
@pytest.mark.parametrize("exists", [False, True])
def test_the_migration_upgrades_with_no_projects_root_or_a_clean_one(backend_root, tmp_path, migration_root, exists):
    if exists:
        (migration_root / "proyecto_real" / "metadata").mkdir(parents=True)
        (migration_root / ".retired-manifests").mkdir()
        (migration_root / ".retired-manifests" / "viejo-20250101T000000Z-manifest.jsonl").write_text("{}\n")
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    try:
        _before(engine)
        _run_migration(engine, _load_migration(backend_root).upgrade)
        assert "testing" in {c["name"] for c in sa.inspect(engine).get_columns("projects")}
    finally:
        engine.dispose()


@pytest.mark.unit
def test_the_migration_refuses_a_projects_root_it_cannot_read(backend_root, tmp_path, migration_root, monkeypatch):
    from pathlib import Path
    migration_root.mkdir()
    real_iterdir = Path.iterdir

    def refuse(self):
        if self == migration_root:
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", refuse)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    try:
        _before(engine)
        with pytest.raises(RuntimeError, match="cannot be read"):
            _run_migration(engine, _load_migration(backend_root).upgrade)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# A real volume's name is never in the testing namespace; a testing volume's is free
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("name", [T + "tomo", "Captua-Testing-Tomo", "ｃａｐｔｕａ-testing-tomo"])
def test_a_real_volumes_name_is_never_in_the_testing_namespace(db_session, name):
    from app.models.collection import Collection
    from app.models.project import TestingSpaceError
    p = _project(db_session, "Real")
    db_session.add(Collection(name=name, project_id=p.id))
    with pytest.raises(TestingSpaceError) as exc:
        db_session.commit()
    db_session.rollback()
    assert (exc.value.code, exc.value.field) == ("name_reserved", "collections.name")
    col = _volume(db_session, p, name="Tomo")
    col.name = name
    with pytest.raises(TestingSpaceError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
def test_a_testing_volumes_name_is_free(db_session):
    p = _testing_project(db_session)
    assert _volume(db_session, p, name="Tomo cualquiera").name == "Tomo cualquiera"
    assert _volume(db_session, p, name=T + "tomo").name == T + "tomo"


@pytest.mark.unit
def test_a_list_item_titled_in_the_testing_namespace_is_refused_as_an_item(db_session, imports_root):
    from app.core import list_import
    from tests.unit.test_list_import import _item, _list_with, _reasons, _volumes
    doc = _list_with([_item(code="xx-test-901", stem="xx-test-901", sequence=1),
                      _item(code="xx-test-902", stem="xx-test-902", sequence=2, title="Captua-Testing-Tomo"),
                      _item(code="xx-test-903", stem="xx-test-903", sequence=3)])
    report = list_import.import_list(db_session, doc, "ad", True)
    assert report["created"] == 2 and _reasons(report) == [(2, "xx-test-902", "title_reserved")]
    assert set(_volumes(db_session)) == {"xx-test-901", "xx-test-903"}


@pytest.mark.unit
def test_the_migration_refuses_a_volume_named_in_the_testing_namespace(backend_root, tmp_path, migration_root):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    try:
        _before(engine)
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO collections (id, name, project_id) VALUES (7, 'Captua-Testing-v', 1)"))
        with pytest.raises(RuntimeError, match="volume 7"):
            _run_migration(engine, _load_migration(backend_root).upgrade)
    finally:
        engine.dispose()
