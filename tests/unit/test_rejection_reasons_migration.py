"""The migration that adds framing, obstruction and other to the rejection reasons.

`record_rejections.predefined_reason` is held to the ruled list by
`check_record_rejection_reason`. Upgrade widens it to nine values; downgrade
narrows it back to six, and refuses while any rejection uses one of the three
new reasons, because rewriting an audit row's reason would falsify it.

Run with: python -m pytest tests/unit/test_rejection_reasons_migration.py
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

BACKEND_ROOT = Path(__file__).resolve().parents[2]
OLD = "predefined_reason IN ('blur','glare','shadow','focus','exposure','dirt')"


def _load_migration():
    [path] = (BACKEND_ROOT / "alembic" / "versions").glob("*_rejection_reasons_framing_obstruction_other.py")
    spec = importlib.util.spec_from_file_location("rejection_reasons_migration", path)
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
    """record_rejections as revision d7a1c3e5f9b2 leaves it, with one old-reason row."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    md = sa.MetaData()
    sa.Table("record_rejections", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("record_id", sa.Integer, nullable=False),
             sa.Column("predefined_reason", sa.String(20), nullable=False),
             sa.Column("comment", sa.Text, nullable=True),
             sa.CheckConstraint(OLD, name="check_record_rejection_reason"))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO record_rejections (id, record_id, predefined_reason) VALUES (1, 1, 'blur')"))
    return engine


def _insert(engine, rid, reason):
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO record_rejections (id, record_id, predefined_reason, comment) "
                             "VALUES (:id, 1, :r, 'n')"), {"id": rid, "r": reason})


@pytest.mark.unit
def test_it_follows_the_rotation_revision():
    mod = _load_migration()
    assert mod.down_revision == "d7a1c3e5f9b2"


@pytest.mark.unit
def test_before_the_upgrade_the_new_reasons_are_refused(tmp_path):
    engine = _engine(tmp_path)
    with pytest.raises(IntegrityError):
        _insert(engine, 2, "framing")


@pytest.mark.unit
def test_upgrade_accepts_the_three_new_reasons_and_keeps_refusing_others(tmp_path):
    mod = _load_migration()
    engine = _engine(tmp_path)
    _run(engine, mod.upgrade)

    for i, reason in enumerate(["framing", "obstruction", "other", "dirt"], start=2):
        _insert(engine, i, reason)
    with pytest.raises(IntegrityError):
        _insert(engine, 10, "bogus")
    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT predefined_reason FROM record_rejections ORDER BY id")).scalars().all() == [
            "blur", "framing", "obstruction", "other", "dirt"]


@pytest.mark.unit
def test_downgrade_narrows_the_list_again(tmp_path):
    mod = _load_migration()
    engine = _engine(tmp_path)
    _run(engine, mod.upgrade)
    _run(engine, mod.downgrade)

    _insert(engine, 2, "glare")
    with pytest.raises(IntegrityError):
        _insert(engine, 3, "framing")


@pytest.mark.unit
@pytest.mark.parametrize("reason", ["framing", "obstruction", "other"])
def test_downgrade_refuses_while_a_rejection_uses_a_new_reason(tmp_path, reason):
    mod = _load_migration()
    engine = _engine(tmp_path)
    _run(engine, mod.upgrade)
    _insert(engine, 2, reason)

    with pytest.raises(RuntimeError, match="1 rejection"):
        _run(engine, mod.downgrade)
    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT predefined_reason FROM record_rejections WHERE id = 2")).scalar() == reason
    # The constraint is still the wide one: nothing was half-applied.
    _insert(engine, 3, "other")


@pytest.mark.unit
def test_alembic_has_a_single_head():
    """Moved here from test_master_orientation.py: the newest revision pins the head."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    assert ScriptDirectory.from_config(cfg).get_heads() == ["a8c2e4f6b1d3"]
