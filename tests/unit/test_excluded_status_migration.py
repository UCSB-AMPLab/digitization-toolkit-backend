"""The migration that adds `excluded` to the record statuses (Fisqua contract 1.6).

`records.status` is held to the status list by `check_record_status`, and
`record_status_changes.from_status` / `to_status` by
`check_record_status_change_from` / `_to`. Upgrade widens all three to five
values and adds `record_status_changes.note`, where an exclusion's note is
kept; downgrade narrows the lists back to four and drops the column, and
refuses while any record or history row uses `excluded`, since rewriting
either would falsify it.

Run with: python -m pytest tests/unit/test_excluded_status_migration.py

Version: 0.2.1
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

BACKEND_ROOT = Path(__file__).resolve().parents[2]
OLD = "('captured','in_review','rejected','approved')"


def _load_migration():
    [path] = (BACKEND_ROOT / "alembic" / "versions").glob("*_record_status_excluded.py")
    spec = importlib.util.spec_from_file_location("record_status_excluded_migration", path)
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
    """records and record_status_changes as revision 6df85ec6309b leaves their status CHECKs, both empty."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    md = sa.MetaData()
    sa.Table("records", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("status", sa.String(20), nullable=False),
             sa.CheckConstraint(f"status IN {OLD}", name="check_record_status"))
    sa.Table("record_status_changes", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("record_id", sa.Integer, nullable=True),
             sa.Column("from_status", sa.String(20), nullable=False),
             sa.Column("to_status", sa.String(20), nullable=False),
             sa.CheckConstraint(f"from_status IN {OLD}", name="check_record_status_change_from"),
             sa.CheckConstraint(f"to_status IN {OLD}", name="check_record_status_change_to"))
    md.create_all(engine)
    return engine


def _record(engine, rid, status):
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO records (id, status) VALUES (:id, :s)"), {"id": rid, "s": status})


def _change(engine, cid, from_status, to_status):
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO record_status_changes (id, record_id, from_status, to_status) "
                             "VALUES (:id, 1, :f, :t)"), {"id": cid, "f": from_status, "t": to_status})


@pytest.mark.unit
def test_it_follows_the_reference_code_revision():
    assert _load_migration().down_revision == "6df85ec6309b"


@pytest.mark.unit
def test_the_models_checks_match_the_migration():
    from app.models.record import Record, RecordStatusChange
    mod = _load_migration()

    def check(table, name):
        [c] = [c for c in table.constraints if getattr(c, "name", None) == name]
        return str(c.sqltext)

    assert check(Record.__table__, "check_record_status") == f"status IN {mod.NEW}"
    assert check(RecordStatusChange.__table__, "check_record_status_change_from") == f"from_status IN {mod.NEW}"
    assert check(RecordStatusChange.__table__, "check_record_status_change_to") == f"to_status IN {mod.NEW}"


@pytest.mark.unit
def test_before_the_upgrade_excluded_is_refused(tmp_path):
    engine = _engine(tmp_path)
    with pytest.raises(IntegrityError):
        _record(engine, 1, "excluded")
    with pytest.raises(IntegrityError):
        _change(engine, 1, "in_review", "excluded")


@pytest.mark.unit
def test_upgrade_on_empty_tables_accepts_excluded_and_keeps_refusing_others(tmp_path):
    mod = _load_migration()
    engine = _engine(tmp_path)
    _run(engine, mod.upgrade)

    _record(engine, 1, "excluded")
    _record(engine, 2, "approved")
    _change(engine, 1, "in_review", "excluded")
    _change(engine, 2, "excluded", "approved")
    with pytest.raises(IntegrityError):
        _record(engine, 3, "bogus")
    with pytest.raises(IntegrityError):
        _change(engine, 3, "bogus", "approved")
    with pytest.raises(IntegrityError):
        _change(engine, 4, "approved", "bogus")


@pytest.mark.unit
def test_downgrade_on_empty_tables_narrows_the_lists_again(tmp_path):
    mod = _load_migration()
    engine = _engine(tmp_path)
    _run(engine, mod.upgrade)
    _run(engine, mod.downgrade)

    _record(engine, 1, "approved")
    _change(engine, 1, "in_review", "approved")
    with pytest.raises(IntegrityError):
        _record(engine, 2, "excluded")
    with pytest.raises(IntegrityError):
        _change(engine, 2, "excluded", "approved")
    with pytest.raises(IntegrityError):
        _change(engine, 3, "in_review", "excluded")


@pytest.mark.unit
@pytest.mark.parametrize("row", [
    ("record", "excluded", None),
    ("change", "in_review", "excluded"),
    ("change", "excluded", "approved"),
])
def test_downgrade_refuses_while_a_row_uses_excluded(tmp_path, row):
    mod = _load_migration()
    engine = _engine(tmp_path)
    _run(engine, mod.upgrade)
    kind, a, b = row
    if kind == "record":
        _record(engine, 1, a)
    else:
        _change(engine, 1, a, b)

    with pytest.raises(RuntimeError, match="excluded"):
        _run(engine, mod.downgrade)
    # The constraints are still the wide ones: nothing was half-applied.
    _record(engine, 5, "excluded")
    _change(engine, 5, "in_review", "excluded")


def _columns(engine, table):
    return [c["name"] for c in sa.inspect(engine).get_columns(table)]


@pytest.mark.unit
def test_upgrade_adds_the_note_column_and_downgrade_drops_it(tmp_path):
    mod = _load_migration()
    engine = _engine(tmp_path)
    assert "note" not in _columns(engine, "record_status_changes")
    _run(engine, mod.upgrade)
    assert "note" in _columns(engine, "record_status_changes")
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO record_status_changes (id, record_id, from_status, to_status, note) "
                             "VALUES (1, 1, 'in_review', 'approved', NULL)"))
        conn.execute(sa.text("INSERT INTO record_status_changes (id, record_id, from_status, to_status, note) "
                             "VALUES (2, 1, 'approved', 'in_review', :n)"), {"n": "n" * 2000})
        conn.execute(sa.text("DELETE FROM record_status_changes WHERE id = 2"))
    _run(engine, mod.downgrade)
    assert "note" not in _columns(engine, "record_status_changes")
    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM record_status_changes")).scalar() == 1


@pytest.mark.unit
def test_the_model_has_the_note_column():
    from app.models.record import RecordStatusChange
    col = RecordStatusChange.__table__.c.note
    assert col.nullable is True
