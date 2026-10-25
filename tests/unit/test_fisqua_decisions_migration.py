"""The QC decisions migration (a5f0306a8f8c), on SQLite and, opt-in, on PostgreSQL.

    python -m pytest tests/unit/test_fisqua_decisions_migration.py
"""

import importlib.util
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

REVISION = "a5f0306a8f8c"
MIGRATION_FILE = f"{REVISION}_add_fisqua_decisions.py"
STATUS_COLUMNS = {"decisions_id", "decisions_decided_at", "decisions_applied_at", "decisions_manifest_id",
                  "decisions_report", "decisions_stale_id"}


def _load(backend_root):
    path = backend_root / "alembic" / "versions" / MIGRATION_FILE
    spec = importlib.util.spec_from_file_location("fisqua_decisions_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _neighbours(engine):
    md = sa.MetaData()
    sa.Table("record_images", md, sa.Column("id", sa.Integer, primary_key=True))
    sa.Table("fisqua_status", md, sa.Column("collection_id", sa.Integer, primary_key=True),
             sa.Column("status_checked_at", sa.DateTime, nullable=False))
    md.create_all(engine)


def _run(engine, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def _exercise(engine, mod):
    _neighbours(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO record_images (id) VALUES (1)"))
        conn.execute(sa.text("INSERT INTO fisqua_status (collection_id, status_checked_at) VALUES (1, CURRENT_TIMESTAMP)"))
    _run(engine, mod.upgrade)

    insp = sa.inspect(engine)
    assert "rotation_correction_quarter_turns" in {c["name"] for c in insp.get_columns("record_images")}
    assert STATUS_COLUMNS <= {c["name"] for c in insp.get_columns("fisqua_status")}
    for good in (None, 1, 2, 3):
        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE record_images SET rotation_correction_quarter_turns = :q WHERE id = 1"),
                         {"q": good})
    for bad in (0, 4, -1):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(sa.text("UPDATE record_images SET rotation_correction_quarter_turns = :q WHERE id = 1"),
                         {"q": bad})

    _run(engine, mod.downgrade)
    insp = sa.inspect(engine)
    assert "rotation_correction_quarter_turns" not in {c["name"] for c in insp.get_columns("record_images")}
    assert not STATUS_COLUMNS & {c["name"] for c in insp.get_columns("fisqua_status")}
    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM fisqua_status")).scalar() == 1


@pytest.mark.unit
def test_the_migration_chains_onto_the_fisqua_outbox(backend_root):
    mod = _load(backend_root)
    assert mod.revision == REVISION and mod.down_revision == "9b7087bd0bc5"


@pytest.mark.unit
def test_alembic_has_a_single_head(backend_root):
    """The newest revision pins the head (moved here from test_fisqua_outbox_migration.py)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(backend_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    assert ScriptDirectory.from_config(cfg).get_heads() == [REVISION]


@pytest.mark.unit
def test_the_migration_upgrades_and_downgrades_on_sqlite(backend_root, tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    try:
        _exercise(engine, _load(backend_root))
    finally:
        engine.dispose()


@pytest.mark.unit
def test_the_models_match_the_migration(backend_root, tmp_path):
    """The added columns and check agree between the ORM and the migrated tables."""
    from app.core.db import Base
    import app.models.fisqua  # noqa: F401
    import app.models.record  # noqa: F401

    b = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    try:
        _neighbours(b)
        _run(b, _load(backend_root).upgrade)
        insp = sa.inspect(b)
        migrated = {c["name"]: (str(c["type"]), c["nullable"]) for c in insp.get_columns("fisqua_status")}
        for name in STATUS_COLUMNS:
            col = Base.metadata.tables["fisqua_status"].c[name]
            assert migrated[name] == (str(col.type.compile(b.dialect)), col.nullable), name
        col = Base.metadata.tables["record_images"].c["rotation_correction_quarter_turns"]
        got = {c["name"]: (str(c["type"]), c["nullable"]) for c in insp.get_columns("record_images")}
        assert got["rotation_correction_quarter_turns"] == (str(col.type.compile(b.dialect)), True)
        checks = {c["name"] for c in insp.get_check_constraints("record_images")}
        assert "check_record_image_rotation_correction" in checks
        orm_checks = {c.name for c in Base.metadata.tables["record_images"].constraints
                      if isinstance(c, sa.CheckConstraint)}
        assert "check_record_image_rotation_correction" in orm_checks
    finally:
        b.dispose()


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
