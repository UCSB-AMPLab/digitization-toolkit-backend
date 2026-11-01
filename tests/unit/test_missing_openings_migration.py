"""The migration for contract 1.9's placeholders: `missing_side` "both", what a placeholder expects, and closing one.

`records.missing_side` takes "both" beside "left" and "right". Five nullable
columns are added: `missing_expected`, `missing_note`, `missing_request_id`
(String(64)), `closed_reason` (String(32), held to four values) and
`closed_note`. `closed_reason` may be set only while `missing_side` is.
Downgrade narrows the side back and drops the columns, and refuses while any
record misses both sides, carries a closed reason or note, or carries what
a placeholder expects, its note or its request id, since dropping any of
them would lose what was recorded.

Run with: python -m pytest tests/unit/test_missing_openings_migration.py

Version: 0.3.3
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

BACKEND_ROOT = Path(__file__).resolve().parents[2]
VERSIONS = BACKEND_ROOT / "alembic" / "versions"
NEW_COLUMNS = ("missing_expected", "missing_note", "missing_request_id", "closed_reason", "closed_note")


def _load_migration():
    [path] = VERSIONS.glob("*_placeholders_and_closed_missing.py")
    spec = importlib.util.spec_from_file_location("placeholders_migration", path)
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
    """`records` as revision e9a1c3d5f7b2 leaves the columns this migration touches, and one row."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    md = sa.MetaData()
    sa.Table("records", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("status", sa.String(20), nullable=False),
             sa.Column("capture_mode", sa.String(10), nullable=False),
             sa.Column("missing_side", sa.String(5), nullable=True),
             sa.CheckConstraint("missing_side IS NULL OR missing_side IN ('left','right')",
                                name="check_record_missing_side"))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO records (id, status, capture_mode) VALUES (1, 'captured', 'dual')"))
    return engine


def _columns(engine):
    return {c["name"] for c in sa.inspect(engine).get_columns("records")}


def _set(engine, **values):
    sets = ", ".join(f"{k} = :{k}" for k in values)
    with engine.begin() as conn:
        conn.execute(sa.text(f"UPDATE records SET {sets} WHERE id = 1"), values)


@pytest.mark.unit
def test_it_follows_the_missing_side_revision():
    # The one-head check moved to the next revision's test (test_unit_mode).
    mod = _load_migration()
    assert mod.down_revision == "e9a1c3d5f7b2"


@pytest.mark.unit
def test_the_models_columns_and_checks_match_the_migration():
    from app.models.record import Record
    mod = _load_migration()
    t = Record.__table__

    def check(name):
        [c] = [c for c in t.constraints if getattr(c, "name", None) == name]
        return str(c.sqltext)

    assert check("check_record_missing_side") == mod.SIDE_CHECK
    assert check("check_record_closed_reason") == mod.CLOSED_REASON_CHECK
    assert check("check_record_closed_needs_missing") == mod.CLOSED_NEEDS_MISSING_CHECK
    for name in NEW_COLUMNS:
        assert t.c[name].nullable is True
    assert t.c.missing_request_id.type.length == 64
    assert t.c.closed_reason.type.length == 32
    assert isinstance(t.c.missing_expected.type, sa.Text)
    assert isinstance(t.c.missing_note.type, sa.Text)
    assert isinstance(t.c.closed_note.type, sa.Text)


@pytest.mark.unit
def test_upgrade_adds_the_columns_and_takes_both(tmp_path):
    mod = _load_migration()
    engine = _engine(tmp_path)
    _run(engine, mod.upgrade)

    assert set(NEW_COLUMNS) <= _columns(engine)
    for side in ("left", "right", "both", None):
        _set(engine, missing_side=side)
    for bad in ("BOTH", "", "none"):
        with pytest.raises(IntegrityError):
            _set(engine, missing_side=bad)


@pytest.mark.unit
def test_closed_reason_is_held_to_the_four_and_only_while_missing(tmp_path):
    mod = _load_migration()
    engine = _engine(tmp_path)
    _run(engine, mod.upgrade)

    _set(engine, missing_side="both")
    for reason in ("foliation_skip", "leaf_missing_from_volume", "captured_elsewhere", "other"):
        _set(engine, closed_reason=reason)
    with pytest.raises(IntegrityError):
        _set(engine, closed_reason="lost")
    # Set, it needs a missing side.
    with pytest.raises(IntegrityError):
        _set(engine, missing_side=None)
    _set(engine, closed_reason=None, missing_side=None)
    with pytest.raises(IntegrityError):
        _set(engine, closed_reason="other")


@pytest.mark.unit
def test_downgrade_drops_the_columns_when_nothing_would_be_lost(tmp_path):
    mod = _load_migration()
    engine = _engine(tmp_path)
    _run(engine, mod.upgrade)
    _set(engine, missing_side="left")
    _run(engine, mod.downgrade)

    assert not set(NEW_COLUMNS) & _columns(engine)
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT missing_side FROM records WHERE id = 1")).scalar() == "left"
    with pytest.raises(IntegrityError):
        _set(engine, missing_side="both")
    # And upgrades again.
    _run(engine, mod.upgrade)
    assert set(NEW_COLUMNS) <= _columns(engine)


@pytest.mark.unit
@pytest.mark.parametrize("values, word", [
    ({"missing_side": "both"}, "both"),
    ({"missing_side": "left", "closed_reason": "foliation_skip"}, "closed"),
    ({"missing_side": "right", "closed_note": "n"}, "closed"),
    ({"missing_side": "left", "missing_expected": "f. 12"}, "expected"),
    ({"missing_side": "left", "missing_note": "falta"}, "note"),
    ({"missing_side": None, "missing_request_id": "req-1"}, "request"),
])
def test_downgrade_refuses_while_a_row_would_lose_both_or_a_closed_value(tmp_path, values, word):
    mod = _load_migration()
    engine = _engine(tmp_path)
    _run(engine, mod.upgrade)
    _set(engine, **values)

    with pytest.raises(RuntimeError, match=word):
        _run(engine, mod.downgrade)
    # Nothing was half-applied: the columns and the wide check are still there.
    assert set(NEW_COLUMNS) <= _columns(engine)
    _set(engine, missing_side="both", closed_reason=None, closed_note=None, missing_expected=None,
         missing_note=None, missing_request_id=None)
