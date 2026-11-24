"""The colour card migration (e7c3a9d1f5b2), on SQLite and, opt-in, on PostgreSQL.

As tests/unit/test_capture_requests_migration.py: SQLite always; PostgreSQL
only when DATABASE_HOST is set, in a schema created and dropped by the test.
The revision adds `unit_settings.colour_card_shots` and the tables
colour_card_sets, colour_card_shots and colour_card_objects
(app/models/colour_card.py).

    python -m pytest tests/unit/test_colour_card_migration.py
"""

import importlib.util
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

REVISION = "e7c3a9d1f5b2"
PARENT = "d2b4f6a8c0e3"
MIGRATION_FILE = f"{REVISION}_add_colour_card_shots.py"
TABLES = ("colour_card_sets", "colour_card_shots", "colour_card_objects")
SHA = "a" * 64


def _load(backend_root):
    path = backend_root / "alembic" / "versions" / MIGRATION_FILE
    spec = importlib.util.spec_from_file_location("colour_card_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _neighbours(engine):
    md = sa.MetaData()
    sa.Table("collections", md, sa.Column("id", sa.Integer, primary_key=True))
    sa.Table("unit_settings", md, sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("mode", sa.String(16), nullable=False))
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
        conn.execute(sa.text("INSERT INTO unit_settings (id, mode) VALUES (1, 'offline')"))

    _run(engine, mod.upgrade)

    insp = sa.inspect(engine)
    assert set(TABLES) <= set(insp.get_table_names())
    with engine.begin() as conn:
        # A unit that updates keeps the setting off.
        assert conn.execute(sa.text("SELECT colour_card_shots FROM unit_settings")).scalar() in (False, 0)

    set_insert = sa.text(
        "INSERT INTO colour_card_sets (collection_id, number, outcome, capture_mode, cameras, opened_at, "
        "completed_at) VALUES (:c, :n, :o, :m, '[]', CURRENT_TIMESTAMP, :done)")
    with engine.begin() as conn:
        conn.execute(set_insert, {"c": 1, "n": 1, "o": None, "m": "dual", "done": None})
        conn.execute(set_insert, {"c": 1, "n": 2, "o": "skipped", "m": "dual", "done": "2026-11-22 10:00:00"})
        conn.execute(set_insert, {"c": 2, "n": 1, "o": "taken", "m": "single", "done": "2026-11-22 10:00:00"})
    for bad in (
        {"c": 1, "n": 1, "o": None, "m": "dual", "done": None},              # number taken in the volume
        {"c": 1, "n": 3, "o": "lost", "m": "dual", "done": "2026-11-22"},    # unknown outcome
        {"c": 1, "n": 4, "o": "taken", "m": "dual", "done": None},           # taken with no time
        {"c": 1, "n": 5, "o": None, "m": "dual", "done": "2026-11-22"},      # a time and no outcome
        {"c": 1, "n": 6, "o": None, "m": "triple", "done": None},            # unknown mode
        {"c": 9, "n": 1, "o": None, "m": "dual", "done": None},              # no such volume
    ):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(set_insert, bad)

    shot_insert = sa.text(
        "INSERT INTO colour_card_shots (set_id, camera_index, capture_id, filename, file_path, sha256, captured_at) "
        "VALUES (:s, :i, :u, 'x-cal_01_c0.jpg', '/p/x-cal_01_c0.jpg', :h, CURRENT_TIMESTAMP)")
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(shot_insert, {"s": 1, "i": 0, "u": a, "h": SHA})
        conn.execute(shot_insert, {"s": 1, "i": 1, "u": b, "h": SHA})
    for bad in (
        {"s": 1, "i": 0, "u": str(uuid.uuid4()), "h": SHA},  # a second shot of the camera in the set
        {"s": 3, "i": 0, "u": a, "h": SHA},                  # another shot's capture id
        {"s": 9, "i": 0, "u": str(uuid.uuid4()), "h": SHA},  # no such set
        {"s": 3, "i": 0, "u": str(uuid.uuid4()), "h": None},  # no digest
    ):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(shot_insert, bad)

    obj_insert = sa.text(
        "INSERT INTO colour_card_objects (shot_id, kind, object_key, local_sha1, refused_at, refusal, created_at) "
        "VALUES (:s, :k, :key, :sha1, :rat, :r, CURRENT_TIMESTAMP)")
    with engine.begin() as conn:
        conn.execute(obj_insert, {"s": 1, "k": "shot", "key": "t/x/calibration/a.jpg", "sha1": "1" * 40,
                                  "rat": None, "r": None})
    for bad in (
        {"s": 2, "k": "shot", "key": "t/x/calibration/a.jpg", "sha1": "1" * 40, "rat": None, "r": None},
        {"s": 1, "k": "shot", "key": "t/x/calibration/b.jpg", "sha1": "1" * 40, "rat": None, "r": None},
        {"s": 2, "k": "page", "key": "t/x/calibration/c.jpg", "sha1": "1" * 40, "rat": None, "r": None},
        {"s": 2, "k": "shot", "key": "t/x/calibration/d.jpg", "sha1": "1" * 40, "rat": "2026-11-22", "r": "other"},
    ):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(obj_insert, bad)

    if postgresql:
        # A volume's sets and shots go with it; an object keeps its key with no shot.
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM collections WHERE id = 1"))
            assert conn.execute(sa.text("SELECT count(*) FROM colour_card_sets")).scalar() == 1
            assert conn.execute(sa.text("SELECT count(*) FROM colour_card_shots")).scalar() == 0
            assert conn.execute(sa.text("SELECT shot_id FROM colour_card_objects")).scalar() is None

    _run(engine, mod.downgrade)
    insp = sa.inspect(engine)
    assert not set(TABLES) & set(insp.get_table_names())
    assert "colour_card_shots" not in {c["name"] for c in insp.get_columns("unit_settings")}
    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM unit_settings")).scalar() == 1


@pytest.mark.unit
def test_the_migration_is_in_the_chain_and_there_is_one_head(backend_root):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    mod = _load(backend_root)
    assert mod.revision == REVISION and mod.down_revision == PARENT
    cfg = Config(str(backend_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_revision(mod.down_revision) is not None
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
def test_the_models_match_the_migration(backend_root, tmp_path):
    """The ORM tables and the migrated ones agree on columns, constraints, indexes and keys."""
    from app.core.db import Base
    import app.models.colour_card  # noqa: F401
    import app.models.unit_settings  # noqa: F401

    a = sa.create_engine(f"sqlite:///{tmp_path / 'orm.db'}")
    b = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    try:
        md = sa.MetaData()
        sa.Table("collections", md, sa.Column("id", sa.Integer, primary_key=True))
        md.create_all(a)
        for name in TABLES:
            Base.metadata.tables[name].to_metadata(md)
        md.create_all(a)
        _neighbours(b)
        _run(b, _load(backend_root).upgrade)

        def shape(engine, t):
            insp = sa.inspect(engine)
            return (
                sorted((c["name"], str(c["type"]), c["nullable"]) for c in insp.get_columns(t)),
                sorted(u["name"] for u in insp.get_unique_constraints(t)),
                sorted(c["name"] for c in insp.get_check_constraints(t)),
                sorted(i["name"] for i in insp.get_indexes(t)),
                sorted((f["name"], f["referred_table"], tuple(f["constrained_columns"]), f["options"].get("ondelete"))
                       for f in insp.get_foreign_keys(t)),
                insp.get_pk_constraint(t)["constrained_columns"],
            )
        def before_retirement(shape_):
            # The retirement of colour_card_objects comes with the next revision, c8e0a2f4b6d9
            # (tests/unit/test_offsite_retired_keys.py compares the model with the chain up to it).
            return tuple(sorted(x for x in part if "retired" not in str(x)) if isinstance(part, list) else part
                         for part in shape_)

        for name in TABLES:
            assert before_retirement(shape(a, name)) == shape(b, name), name
        col = Base.metadata.tables["unit_settings"].c.colour_card_shots
        mig = {c["name"]: c for c in sa.inspect(b).get_columns("unit_settings")}["colour_card_shots"]
        assert mig["nullable"] is False and col.nullable is False
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
