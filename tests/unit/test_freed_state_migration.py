"""The freed-state migration (c4e8f1a3b5d7), on SQLite and, opt-in, on PostgreSQL.

SQLite always. PostgreSQL only when DATABASE_HOST is set and DATABASE_* reach
a server, in a schema of its own, created and dropped by the test, as in
tests/unit/test_offsite_objects_migration.py.

    python -m pytest tests/unit/test_freed_state_migration.py

Version: 0.1.0-beta
"""

import importlib.util
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

MIGRATION_FILE = "c4e8f1a3b5d7_add_freed_state_to_record_images.py"
NEW_COLUMNS = {"master_freed_at", "master_freed_bytes", "preview_freed_at", "preview_freed_bytes"}
NEW_CHECKS = {"check_record_image_master_freed", "check_record_image_preview_freed"}


def _load(backend_root):
    path = backend_root / "alembic" / "versions" / MIGRATION_FILE
    spec = importlib.util.spec_from_file_location("freed_state_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _before(engine):
    """record_images as the previous head leaves it, in the columns this revision needs."""
    md = sa.MetaData()
    sa.Table("record_images", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("file_path", sa.String(512), nullable=False))
    md.create_all(engine)


def _run(engine, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def _exercise(engine, mod):
    _before(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO record_images (id, file_path) VALUES (1, '/a'), (2, '/b')"))

    _run(engine, mod.upgrade)

    insp = sa.inspect(engine)
    columns = {c["name"]: c for c in insp.get_columns("record_images")}
    assert NEW_COLUMNS <= set(columns)
    assert all(columns[c]["nullable"] for c in NEW_COLUMNS)
    assert NEW_CHECKS <= {c["name"] for c in insp.get_check_constraints("record_images")}
    with engine.begin() as conn:
        rows = conn.execute(sa.text(
            "SELECT id, file_path, master_freed_at, preview_freed_at FROM record_images ORDER BY id")).all()
    assert [tuple(r) for r in rows] == [(1, "/a", None, None), (2, "/b", None, None)]

    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE record_images SET master_freed_at = CURRENT_TIMESTAMP, "
                             "master_freed_bytes = 10 WHERE id = 1"))
    for bad in ("master_freed_at = CURRENT_TIMESTAMP", "master_freed_bytes = 5",
                "preview_freed_at = CURRENT_TIMESTAMP", "preview_freed_bytes = 5",
                "master_freed_at = CURRENT_TIMESTAMP, master_freed_bytes = -1"):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(f"UPDATE record_images SET {bad} WHERE id = 2"))

    _run(engine, mod.downgrade)
    insp = sa.inspect(engine)
    assert not NEW_COLUMNS & {c["name"] for c in insp.get_columns("record_images")}
    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM record_images")).scalar() == 2


@pytest.mark.unit
def test_the_migration_chains_onto_the_single_previous_head(backend_root):
    mod = _load(backend_root)
    assert mod.down_revision == "b8d2f4a6c1e3"
    siblings = [p.name for p in (backend_root / "alembic" / "versions").glob("*.py")
                if p.name != MIGRATION_FILE and "down_revision" in p.read_text()
                and "'b8d2f4a6c1e3'" in p.read_text().split("down_revision", 1)[1].splitlines()[0]]
    assert siblings == [], siblings


@pytest.mark.unit
def test_the_migration_upgrades_and_downgrades_on_sqlite(backend_root, tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    try:
        _exercise(engine, _load(backend_root))
    finally:
        engine.dispose()


@pytest.mark.unit
def test_the_model_declares_the_migrated_columns_and_checks():
    from app.core.db import Base
    import app.models.record  # noqa: F401

    table = Base.metadata.tables["record_images"]
    assert NEW_COLUMNS <= set(table.c.keys())
    assert all(table.c[c].nullable for c in NEW_COLUMNS)
    assert NEW_CHECKS <= {c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)}


@pytest.mark.unit
def test_the_migration_upgrades_and_downgrades_on_postgresql(backend_root):
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
        _exercise(engine, _load(backend_root))
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
