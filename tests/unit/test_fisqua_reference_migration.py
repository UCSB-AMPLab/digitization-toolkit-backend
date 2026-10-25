"""The sent-reference migration (6df85ec6309b), on SQLite and, opt-in, on PostgreSQL.

    python -m pytest tests/unit/test_fisqua_reference_migration.py
"""

import importlib.util
import json
import os
import uuid

import pytest
import sqlalchemy as sa

REVISION = "6df85ec6309b"
MIGRATION_FILE = f"{REVISION}_add_fisqua_outbox_reference_code.py"


def _load(backend_root):
    path = backend_root / "alembic" / "versions" / MIGRATION_FILE
    spec = importlib.util.spec_from_file_location("fisqua_reference_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _neighbours(engine):
    md = sa.MetaData()
    sa.Table("fisqua_outbox", md, sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("manifest", sa.JSON, nullable=True))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO fisqua_outbox (id, manifest) VALUES (1, :m), (2, NULL), (3, :n)"),
                     {"m": json.dumps({"volumes": [{"externalId": "xx-test-001"}]}),
                      "n": json.dumps({"volumes": []})})


def _run(engine, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from app.core.db import sqlite_foreign_keys_off

    with engine.connect() as conn:
        with sqlite_foreign_keys_off(conn):
            with conn.begin():
                with Operations.context(MigrationContext.configure(conn)):
                    fn()


def _exercise(engine, mod):
    _neighbours(engine)
    _run(engine, mod.upgrade)
    with engine.begin() as conn:
        rows = conn.execute(sa.text("SELECT id, reference_code FROM fisqua_outbox ORDER BY id")).all()
    # Backfilled from each stored manifest's externalId; none where there is no manifest.
    assert rows == [(1, "xx-test-001"), (2, None), (3, None)]
    _run(engine, mod.downgrade)
    assert "reference_code" not in {c["name"] for c in sa.inspect(engine).get_columns("fisqua_outbox")}
    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM fisqua_outbox")).scalar() == 3


@pytest.mark.unit
def test_the_migration_chains_onto_the_decisions_migration(backend_root):
    mod = _load(backend_root)
    assert mod.revision == REVISION and mod.down_revision == "a5f0306a8f8c"


@pytest.mark.unit
def test_alembic_has_a_single_head(backend_root):
    """The newest revision pins the head (moved here from test_fisqua_decisions_migration.py)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(backend_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    assert ScriptDirectory.from_config(cfg).get_heads() == [REVISION]


@pytest.mark.unit
def test_the_migration_upgrades_backfills_and_downgrades_on_sqlite(backend_root, tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    try:
        _exercise(engine, _load(backend_root))
    finally:
        engine.dispose()


@pytest.mark.unit
def test_the_model_matches_the_migration(backend_root, tmp_path):
    from app.core.db import Base
    import app.models.fisqua  # noqa: F401

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    try:
        _neighbours(engine)
        _run(engine, _load(backend_root).upgrade)
        got = {c["name"]: (str(c["type"]), c["nullable"]) for c in sa.inspect(engine).get_columns("fisqua_outbox")}
        col = Base.metadata.tables["fisqua_outbox"].c["reference_code"]
        assert got["reference_code"] == (str(col.type.compile(engine.dialect)), True)
    finally:
        engine.dispose()


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
