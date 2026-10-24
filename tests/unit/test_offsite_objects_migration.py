"""The offsite_objects migration (b8d2f4a6c1e3), on SQLite and, opt-in, on PostgreSQL.

SQLite always. PostgreSQL only when DATABASE_HOST is set and DATABASE_* reach
a server, in a schema of its own, created and dropped by the test, as in
tests/integration/test_pg_lock_order.py.

    python -m pytest tests/unit/test_offsite_objects_migration.py
"""

import importlib.util
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

MIGRATION_FILE = "b8d2f4a6c1e3_add_offsite_objects.py"
COLUMNS = {"id", "record_image_id", "kind", "object_key", "local_sha1", "remote_sha1",
           "uploaded_at", "verified_at", "refused_at", "refusal", "created_at"}


def _load(backend_root):
    path = backend_root / "alembic" / "versions" / MIGRATION_FILE
    spec = importlib.util.spec_from_file_location("offsite_objects_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _neighbours(engine):
    md = sa.MetaData()
    sa.Table("record_images", md, sa.Column("id", sa.Integer, primary_key=True))
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
        conn.execute(sa.text("INSERT INTO record_images (id) VALUES (1), (2)"))

    _run(engine, mod.upgrade)

    insp = sa.inspect(engine)
    assert {c["name"] for c in insp.get_columns("offsite_objects")} == COLUMNS
    uniques = {u["name"] for u in insp.get_unique_constraints("offsite_objects")}
    assert {"offsite_objects_object_key_key", "offsite_objects_image_kind_key"} <= uniques
    checks = {c["name"] for c in insp.get_check_constraints("offsite_objects")}
    assert {"check_offsite_object_kind", "check_offsite_object_state", "check_offsite_object_refusal"} <= checks
    assert {"ix_offsite_objects_id", "ix_offsite_objects_record_image_id"} <= {
        i["name"] for i in insp.get_indexes("offsite_objects")}

    insert = sa.text(
        "INSERT INTO offsite_objects (record_image_id, kind, object_key, local_sha1, verified_at, refused_at, refusal, created_at) "
        "VALUES (:i, :k, :key, :s, :v, :r, :why, CURRENT_TIMESTAMP)")
    with engine.begin() as conn:
        conn.execute(insert, dict(i=1, k="capture", key="t/s/captures/a", s="0" * 40, v=None, r=None, why=None))
        conn.execute(insert, dict(i=2, k="preview", key="t/s/previews/a", s="0" * 40, v=None,
                                  r="2026-10-23 00:00:00", why="fixity"))
    for bad in (
        dict(i=2, k="thumbnail", key="t/s/thumbnails/a", s="0" * 40, v=None, r=None, why=None),  # kind
        dict(i=2, k="capture", key="t/s/captures/a", s="0" * 40, v=None, r=None, why=None),      # key taken
        dict(i=1, k="capture", key="t/s/captures/b", s="0" * 40, v=None, r=None, why=None),      # image+kind taken
        dict(i=2, k="capture", key="t/s/captures/c", s="0" * 40,
             v="2026-10-23 00:00:00", r="2026-10-23 00:00:00", why="fixity"),                  # both states
        dict(i=2, k="capture", key="t/s/captures/d", s="0" * 40,
             v=None, r="2026-10-23 00:00:00", why=None),                                       # refused, no reason
        dict(i=2, k="capture", key="t/s/captures/e", s="0" * 40,
             v=None, r="2026-10-23 00:00:00", why="other"),                                    # unknown reason
        dict(i=2, k="capture", key="t/s/captures/f", s="0" * 40, v=None, r=None, why="fixity"),  # reason, not refused
    ):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert, bad)

    _run(engine, mod.downgrade)
    assert "offsite_objects" not in sa.inspect(engine).get_table_names()
    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM record_images")).scalar() == 2


@pytest.mark.unit
def test_the_migration_chains_onto_the_single_previous_head(backend_root):
    mod = _load(backend_root)
    assert mod.down_revision == "a7c1e9d2b4f6"
    siblings = [p.name for p in (backend_root / "alembic" / "versions").glob("*.py")
                if p.name != MIGRATION_FILE and "down_revision" in p.read_text()
                and "'a7c1e9d2b4f6'" in p.read_text().split("down_revision", 1)[1].splitlines()[0]]
    assert siblings == [], siblings


@pytest.mark.unit
def test_the_migration_upgrades_and_downgrades_on_sqlite(backend_root, tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    try:
        _exercise(engine, _load(backend_root))
    finally:
        engine.dispose()


@pytest.mark.unit
def test_the_model_matches_the_migration(backend_root, tmp_path):
    """The ORM table and the migrated one agree on columns, constraints and indexes."""
    from app.core.db import Base
    import app.models.offsite_object  # noqa: F401
    import app.models.record  # noqa: F401

    a = sa.create_engine(f"sqlite:///{tmp_path / 'orm.db'}")
    b = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    try:
        md = sa.MetaData()
        sa.Table("record_images", md, sa.Column("id", sa.Integer, primary_key=True))
        md.create_all(a)
        Base.metadata.tables["offsite_objects"].to_metadata(md).create(a)
        _neighbours(b)
        _run(b, _load(backend_root).upgrade)

        def shape(engine):
            insp = sa.inspect(engine)
            return (
                sorted((c["name"], str(c["type"]), c["nullable"]) for c in insp.get_columns("offsite_objects")),
                sorted(u["name"] for u in insp.get_unique_constraints("offsite_objects")),
                sorted(c["name"] for c in insp.get_check_constraints("offsite_objects")),
                sorted(i["name"] for i in insp.get_indexes("offsite_objects")),
                sorted((f["referred_table"], tuple(f["constrained_columns"]), f["options"].get("ondelete"))
                       for f in insp.get_foreign_keys("offsite_objects")),
            )
        assert shape(a) == shape(b)
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
