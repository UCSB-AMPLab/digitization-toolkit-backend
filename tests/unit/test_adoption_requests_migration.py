"""The adoption requests migration (b7d9f1a3c5e8), on SQLite.

As tests/unit/test_capture_requests_migration.py: the revision is loaded
from its file and run against a bare engine, then downgraded.

    python -m pytest tests/unit/test_adoption_requests_migration.py

Version: 0.3.3
"""

import importlib.util
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

REVISION = "b7d9f1a3c5e8"
MIGRATION_FILE = f"{REVISION}_add_adoption_requests.py"
COLUMNS = {"id", "request_id", "source_id", "target_id", "started_at", "ended_at", "outcome", "status", "body"}


def _load(backend_root):
    path = backend_root / "alembic" / "versions" / MIGRATION_FILE
    spec = importlib.util.spec_from_file_location("adoption_requests_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(engine, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


@pytest.mark.unit
def test_the_migration_is_in_the_chain_and_there_is_one_head(backend_root):
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
    mod = _load(backend_root)

    _run(engine, mod.upgrade)

    insp = sa.inspect(engine)
    assert {c["name"] for c in insp.get_columns("adoption_requests")} == COLUMNS
    assert {u["name"]: u["column_names"] for u in insp.get_unique_constraints("adoption_requests")} == {
        "adoption_requests_request_id_key": ["request_id"]}
    assert "check_adoption_request_outcome" in {c["name"] for c in insp.get_check_constraints("adoption_requests")}

    insert = sa.text("INSERT INTO adoption_requests (request_id, source_id, target_id, started_at, outcome) "
                     "VALUES (:u, :s, :t, CURRENT_TIMESTAMP, :o)")
    a = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(insert, {"u": a, "s": 1, "t": 2, "o": None})
        # The volumes are plain ids: a request naming a volume that does not exist is recorded too.
        conn.execute(insert, {"u": str(uuid.uuid4()), "s": 998, "t": 999, "o": "refused"})
        # A closed id's row names no volume.
        conn.execute(insert, {"u": str(uuid.uuid4()), "s": None, "t": None, "o": "closed"})
    for bad in ({"u": a, "s": 1, "t": 2, "o": None},                    # id taken
                {"u": str(uuid.uuid4()), "s": 1, "t": 2, "o": "done"},  # unknown outcome
                {"u": None, "s": 1, "t": 2, "o": None}):                # no id
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert, bad)

    _run(engine, mod.downgrade)
    assert "adoption_requests" not in sa.inspect(engine).get_table_names()
