"""The migration that adds `records.missing_side`: the page of a dual opening never photographed.

A nullable String(5), "left", "right" or null, held to those values by
`check_record_missing_side`. Upgrade adds the column and the check; downgrade
drops both. The placeholders revision after it widens the check to "both".

Run with: python -m pytest tests/unit/test_missing_side_migration.py

Version: 0.3.3
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

BACKEND_ROOT = Path(__file__).resolve().parents[2]
VERSIONS = BACKEND_ROOT / "alembic" / "versions"


def _load_migration():
    [path] = VERSIONS.glob("*_add_record_missing_side.py")
    spec = importlib.util.spec_from_file_location("record_missing_side_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(engine, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def _engine(tmp_path):
    """`records` with the columns the migration touches around, and one row."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    md = sa.MetaData()
    sa.Table("records", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("status", sa.String(20), nullable=False),
             sa.Column("capture_mode", sa.String(10), nullable=False))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO records (id, status, capture_mode) VALUES (1, 'captured', 'dual')"))
    return engine


def _columns(engine):
    return {c["name"] for c in sa.inspect(engine).get_columns("records")}


def _set(engine, value):
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE records SET missing_side = :v WHERE id = 1"), {"v": value})


@pytest.mark.unit
def test_it_follows_the_queue_fields_revision():
    # The head is now the placeholders revision (test_missing_openings_migration.py).
    assert _load_migration().down_revision == "b3d5f7a9c1e2"


@pytest.mark.unit
def test_the_models_column_matches_the_migration():
    # The check was widened to "both" by the placeholders revision, which
    # test_missing_openings_migration.py compares with the model.
    from app.models.record import Record
    col = Record.__table__.c.missing_side
    assert col.nullable is True and col.type.length == 5


@pytest.mark.unit
def test_upgrade_adds_the_column_null_and_holds_it_to_left_or_right(tmp_path):
    mod = _load_migration()
    engine = _engine(tmp_path)
    _run(engine, mod.upgrade)

    assert "missing_side" in _columns(engine)
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT missing_side FROM records WHERE id = 1")).scalar() is None
    _set(engine, "left")
    _set(engine, "right")
    _set(engine, None)
    for bad in ("both", "LEFT", ""):
        with pytest.raises(IntegrityError):
            _set(engine, bad)


@pytest.mark.unit
def test_downgrade_drops_the_column(tmp_path):
    mod = _load_migration()
    engine = _engine(tmp_path)
    _run(engine, mod.upgrade)
    _set(engine, "left")
    _run(engine, mod.downgrade)

    assert "missing_side" not in _columns(engine)
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM records")).scalar() == 1
    # And upgrades again.
    _run(engine, mod.upgrade)
    assert "missing_side" in _columns(engine)
