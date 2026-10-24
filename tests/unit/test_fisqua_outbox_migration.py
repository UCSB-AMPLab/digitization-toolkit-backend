"""The Fisqua outbox migration (9b7087bd0bc5), on SQLite and, opt-in, on PostgreSQL.

As tests/unit/test_offsite_objects_migration.py: SQLite always; PostgreSQL
only when DATABASE_HOST is set, in a schema created and dropped by the test.

    python -m pytest tests/unit/test_fisqua_outbox_migration.py
"""

import importlib.util
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

REVISION = "9b7087bd0bc5"
MIGRATION_FILE = f"{REVISION}_add_fisqua_outbox.py"
OUTBOX_COLUMNS = {"id", "collection_id", "manifest_id", "stem", "created_at", "state", "attempts",
                  "next_attempt_at", "last_attempt_at", "last_error", "refusal", "problems", "manifest",
                  "response", "external_id", "delivered_at"}
STATUS_COLUMNS = {"collection_id", "manifest_id", "status", "status_at", "status_checked_at", "problems"}


def _load(backend_root):
    path = backend_root / "alembic" / "versions" / MIGRATION_FILE
    spec = importlib.util.spec_from_file_location("fisqua_outbox_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _neighbours(engine):
    md = sa.MetaData()
    sa.Table("collections", md, sa.Column("id", sa.Integer, primary_key=True))
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
        conn.execute(sa.text("INSERT INTO collections (id) VALUES (1), (2)"))

    _run(engine, mod.upgrade)

    insp = sa.inspect(engine)
    assert {c["name"] for c in insp.get_columns("fisqua_outbox")} == OUTBOX_COLUMNS
    assert {c["name"] for c in insp.get_columns("fisqua_status")} == STATUS_COLUMNS
    assert "fisqua_outbox_manifest_id_key" in {u["name"] for u in insp.get_unique_constraints("fisqua_outbox")}
    checks = {c["name"] for c in insp.get_check_constraints("fisqua_outbox")}
    assert {"check_fisqua_outbox_state", "check_fisqua_outbox_refusal",
            "check_fisqua_outbox_delivered", "check_fisqua_outbox_attempts"} <= checks

    insert = sa.text(
        "INSERT INTO fisqua_outbox (collection_id, manifest_id, stem, created_at, state, attempts, refusal, delivered_at) "
        "VALUES (:c, :m, 'xx-001', CURRENT_TIMESTAMP, :s, :a, :r, :d)")
    with engine.begin() as conn:
        conn.execute(insert, dict(c=1, m="m1", s="waiting_for_upload", a=0, r=None, d=None))
        conn.execute(insert, dict(c=1, m="m2", s="delivered", a=1, r=None, d="2026-10-24 00:00:00"))
        conn.execute(insert, dict(c=2, m="m3", s="refused", a=0, r="not_exportable", d=None))
    for bad in (
        dict(c=1, m="m1", s="ready", a=0, r=None, d=None),                    # manifest id taken
        dict(c=1, m="b1", s="lost", a=0, r=None, d=None),                     # unknown state
        dict(c=1, m="b2", s="refused", a=0, r=None, d=None),                  # refused, no reason
        dict(c=1, m="b3", s="refused", a=0, r="other", d=None),               # unknown reason
        dict(c=1, m="b4", s="ready", a=0, r="fisqua", d=None),                # reason, not refused
        dict(c=1, m="b5", s="delivered", a=1, r=None, d=None),                # delivered, no time
        dict(c=1, m="b6", s="failed", a=1, r=None, d="2026-10-24 00:00:00"),  # time, not delivered
        dict(c=1, m="b7", s="ready", a=-1, r=None, d=None),                   # negative attempts
        dict(c=9, m="b8", s="ready", a=0, r=None, d=None),                    # no such volume
    ):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert, bad)

    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO fisqua_status (collection_id, status, status_checked_at) "
                             "VALUES (1, 'recibido', CURRENT_TIMESTAMP)"))
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO fisqua_status (collection_id, status, status_checked_at) "
                             "VALUES (1, 'otro', CURRENT_TIMESTAMP)"))

    _run(engine, mod.downgrade)
    names = sa.inspect(engine).get_table_names()
    assert "fisqua_outbox" not in names and "fisqua_status" not in names
    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM collections")).scalar() == 2


@pytest.mark.unit
def test_the_migration_chains_onto_the_single_previous_head(backend_root):
    mod = _load(backend_root)
    assert mod.revision == REVISION
    assert mod.down_revision == "a8c2e4f6b1d3"
    siblings = [p.name for p in (backend_root / "alembic" / "versions").glob("*.py")
                if p.name != MIGRATION_FILE and "down_revision" in p.read_text()
                and "'a8c2e4f6b1d3'" in p.read_text().split("down_revision", 1)[1].splitlines()[0]]
    assert siblings == [], siblings


@pytest.mark.unit
def test_alembic_has_a_single_head(backend_root):
    """The newest revision pins the head (moved here from test_rejection_reasons_migration.py)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(backend_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    assert ScriptDirectory.from_config(cfg).get_heads() == [REVISION]


@pytest.mark.unit
def test_the_migration_upgrades_and_downgrades_on_sqlite(backend_root, tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")

    @sa.event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
    try:
        _exercise(engine, _load(backend_root))
    finally:
        engine.dispose()


@pytest.mark.unit
def test_the_model_matches_the_migration(backend_root, tmp_path):
    """The ORM tables and the migrated ones agree on columns, constraints, indexes and keys."""
    from app.core.db import Base
    import app.models.fisqua  # noqa: F401

    a = sa.create_engine(f"sqlite:///{tmp_path / 'orm.db'}")
    b = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    try:
        md = sa.MetaData()
        sa.Table("collections", md, sa.Column("id", sa.Integer, primary_key=True))
        md.create_all(a)
        for name in ("fisqua_outbox", "fisqua_status"):
            Base.metadata.tables[name].to_metadata(md).create(a)
        _neighbours(b)
        _run(b, _load(backend_root).upgrade)

        def shape(engine, table):
            insp = sa.inspect(engine)
            return (
                sorted((c["name"], str(c["type"]), c["nullable"]) for c in insp.get_columns(table)),
                sorted(u["name"] for u in insp.get_unique_constraints(table)),
                sorted(c["name"] for c in insp.get_check_constraints(table)),
                sorted(i["name"] for i in insp.get_indexes(table)),
                sorted((f["referred_table"], tuple(f["constrained_columns"]), f["options"].get("ondelete"))
                       for f in insp.get_foreign_keys(table)),
                insp.get_pk_constraint(table)["constrained_columns"],
            )
        for name in ("fisqua_outbox", "fisqua_status"):
            assert shape(a, name) == shape(b, name), name
    finally:
        a.dispose()
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
