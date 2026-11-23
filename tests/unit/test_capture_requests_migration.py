"""The capture requests migration (d2b4f6a8c0e3), on SQLite and, opt-in, on PostgreSQL.

As tests/unit/test_fisqua_outbox_migration.py: SQLite always; PostgreSQL
only when DATABASE_HOST is set, in a schema created and dropped by the test.

    python -m pytest tests/unit/test_capture_requests_migration.py
"""

import importlib.util
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

REVISION = "d2b4f6a8c0e3"
MIGRATION_FILE = f"{REVISION}_add_capture_requests.py"
COLUMNS = {"id", "client_capture_id", "space", "kind", "collection_id", "record_id", "image_ids",
           "request_fingerprint", "created_at"}
FP = "f" * 64


def _load(backend_root):
    path = backend_root / "alembic" / "versions" / MIGRATION_FILE
    spec = importlib.util.spec_from_file_location("capture_requests_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _neighbours(engine):
    md = sa.MetaData()
    sa.Table("collections", md, sa.Column("id", sa.Integer, primary_key=True))
    sa.Table("records", md, sa.Column("id", sa.Integer, primary_key=True))
    md.create_all(engine)


def _run(engine, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def _exercise(engine, mod, postgresql=False):
    _neighbours(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO collections (id) VALUES (1), (2)"))
        conn.execute(sa.text("INSERT INTO records (id) VALUES (10), (11)"))

    _run(engine, mod.upgrade)

    insp = sa.inspect(engine)
    assert {c["name"] for c in insp.get_columns("capture_requests")} == COLUMNS
    assert {u["name"]: u["column_names"] for u in insp.get_unique_constraints("capture_requests")} == {
        "capture_requests_client_capture_id_space_key": ["client_capture_id", "space"]}
    assert {"check_capture_request_kind", "check_capture_request_space"} <= {
        c["name"] for c in insp.get_check_constraints("capture_requests")}
    if postgresql:
        lengths = {c["name"]: getattr(c["type"], "length", None) for c in insp.get_columns("capture_requests")}
        assert lengths["client_capture_id"] == 36 and lengths["request_fingerprint"] == 64
        assert lengths["space"] == 10 and lengths["kind"] == 10

    insert = sa.text(
        "INSERT INTO capture_requests (client_capture_id, space, kind, collection_id, record_id, image_ids, "
        "request_fingerprint, created_at) VALUES (:u, :s, :k, :c, :r, :i, :f, CURRENT_TIMESTAMP)")

    def row(**kw):
        return {"s": "real", "k": "single", "c": 1, "r": 10, "i": "[1]", "f": FP, **kw}

    a, b, c = (str(uuid.uuid4()) for _ in range(3))
    with engine.begin() as conn:
        conn.execute(insert, row(u=a))
        # The same id in the other space is another capture.
        conn.execute(insert, row(u=a, s="testing", r=11))
        # One record holds several: a retake carries its own id.
        conn.execute(insert, row(u=b, k="dual", i="[2, 3]"))
        conn.execute(insert, row(u=c, k="dual", c=None, r=11, i="[4, 5]"))
    for bad in (
        row(u=a, r=11),                       # id taken in its space
        row(u=str(uuid.uuid4()), k="triple"),  # unknown kind
        row(u=str(uuid.uuid4()), s="other"),   # unknown space
        row(u=str(uuid.uuid4()), s=None),      # no space
        row(u=str(uuid.uuid4()), r=99),        # no such record
        row(u=str(uuid.uuid4()), c=9),         # no such volume
        row(u=None),                           # no id
        row(u=str(uuid.uuid4()), i=None),      # no images
        row(u=str(uuid.uuid4()), f=None),      # no fingerprint
    ):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert, bad)
    if postgresql:
        from sqlalchemy.exc import DataError
        for too_long in (row(u=str(uuid.uuid4()) + "x"), row(u=str(uuid.uuid4()), f=FP + "f")):
            with pytest.raises(DataError), engine.begin() as conn:
                conn.execute(insert, too_long)

    # A stored id outlives its record and its volume: both are set null, the row stays.
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM records WHERE id = 11"))
        conn.execute(sa.text("DELETE FROM collections WHERE id = 1"))
        rows = conn.execute(sa.text(
            "SELECT client_capture_id, space, collection_id, record_id FROM capture_requests ORDER BY id")).all()
    assert [tuple(r) for r in rows] == [(a, "real", None, 10), (a, "testing", None, None),
                                        (b, "real", None, 10), (c, "real", None, None)]

    _run(engine, mod.downgrade)
    assert "capture_requests" not in sa.inspect(engine).get_table_names()
    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM records")).scalar() == 1


@pytest.mark.unit
def test_the_migration_is_in_the_chain_and_there_is_one_head(backend_root):
    """Its parent is a revision of the chain and the chain has one head, whichever revision a later merge adds."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    mod = _load(backend_root)
    assert mod.revision == REVISION
    cfg = Config(str(backend_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_revision(mod.down_revision) is not None
    assert REVISION in {r.revision for r in script.walk_revisions()}
    assert len(script.get_heads()) == 1


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
    """The ORM table and the migrated one agree on columns, constraints, indexes and keys."""
    from app.core.db import Base
    import app.models.capture_request  # noqa: F401

    a = sa.create_engine(f"sqlite:///{tmp_path / 'orm.db'}")
    b = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    try:
        md = sa.MetaData()
        sa.Table("collections", md, sa.Column("id", sa.Integer, primary_key=True))
        sa.Table("records", md, sa.Column("id", sa.Integer, primary_key=True))
        md.create_all(a)
        Base.metadata.tables["capture_requests"].to_metadata(md).create(a)
        _neighbours(b)
        _run(b, _load(backend_root).upgrade)

        def shape(engine):
            insp = sa.inspect(engine)
            t = "capture_requests"
            return (
                sorted((c["name"], str(c["type"]), c["nullable"]) for c in insp.get_columns(t)),
                sorted(u["name"] for u in insp.get_unique_constraints(t)),
                sorted(c["name"] for c in insp.get_check_constraints(t)),
                sorted(i["name"] for i in insp.get_indexes(t)),
                sorted((f["name"], f["referred_table"], tuple(f["constrained_columns"]), f["options"].get("ondelete"))
                       for f in insp.get_foreign_keys(t)),
                insp.get_pk_constraint(t)["constrained_columns"],
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
        _exercise(engine, _load(backend_root), postgresql=True)
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
