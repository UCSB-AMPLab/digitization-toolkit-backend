"""Who set the unit's mode and when (NEH-373): unit_settings.mode_set_by and mode_set_at.

The two columns change only when the stored mode does: a switch through
PUT /system/unit records the admin and the time, the start-up row records
the time with no username (the unit itself), and every other setting
(projects, language, colour card shots, capture key, a PUT that repeats the
current mode) leaves them as they are. A row written before the revision
that adds them (a3c5e7f9b1d4) holds NULL in both, and GET /system/unit
answers null for each: nobody knows who set that mode.

    python -m pytest tests/unit/test_unit_mode_set_by.py

Version: 0.4.0
"""

import importlib.util
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from tests.unit.test_capture_manifest_export import TENANT, _as, tenant  # noqa: F401  (fixtures)
from tests.unit.test_fisqua_outbox import fisqua  # noqa: F401  (fixture)
from tests.unit.test_unit_mode import _result, _unit, link, recipient  # noqa: F401  (fixtures)

REVISION = "a3c5e7f9b1d4"
PARENT = "b7d9f1a3c5e8"
MIGRATION_FILE = f"{REVISION}_add_unit_mode_set_by.py"


def _at(body):
    """modeSetAt parsed; it must carry a timezone."""
    value = datetime.fromisoformat(body["modeSetAt"])
    assert value.tzinfo is not None, body["modeSetAt"]
    return value


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


# ---------------------------------------------------------------------------
# Setting the mode
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("start, target", [("fisqua", "offline"), ("offline", "backup"), ("backup", "fisqua")])
def test_setting_each_mode_records_the_admin_and_the_time(client, db_session, link, start, target):
    _unit(db_session, start)
    link.operators_answers = [_result("delivered")]
    before = _now()
    resp = _as(client, "admin", "boss").put("/system/unit", json={"mode": target})
    after = _now() + timedelta(seconds=1)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == target
    assert body["modeSetBy"] == "boss"
    assert before <= _at(body) <= after
    assert _as(client, "reviewer").get("/system/unit").json()["modeSetBy"] == "boss"


@pytest.mark.unit
def test_a_later_switch_replaces_who_and_when(client, db_session, link):
    from app.models.unit_settings import UnitSettings
    _unit(db_session, "backup")
    _as(client, "admin", "first").put("/system/unit", json={"mode": "offline"})
    db_session.expire_all()
    row = db_session.get(UnitSettings, 1)
    row.mode_set_at = datetime(2026, 1, 1, 9, 0, 0)
    db_session.commit()
    body = _as(client, "admin", "second").put("/system/unit", json={"mode": "backup"}).json()
    assert body["modeSetBy"] == "second"
    assert _at(body) > datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)


@pytest.mark.unit
def test_the_start_up_row_records_the_time_and_no_username(db_session, fisqua):
    from app.core import unit_mode
    before = _now()
    assert unit_mode.initialise() == "fisqua"
    after = _now() + timedelta(seconds=1)
    body = unit_mode.as_api(db_session)
    assert body["modeSetBy"] is None
    assert before <= _at(body) <= after


@pytest.mark.unit
def test_a_refused_switch_leaves_both_as_they_were(client, db_session):
    _unit(db_session, "offline")
    resp = _as(client, "admin", "boss").put("/system/unit", json={"mode": "backup"})
    assert resp.status_code == 409, resp.text
    body = _as(client, "reviewer").get("/system/unit").json()
    assert body["modeSetBy"] is None and body["modeSetAt"] is None


# ---------------------------------------------------------------------------
# Changing anything else
# ---------------------------------------------------------------------------

def _set_by_first(client, db_session):
    """The mode switched to offline by `first`, its time moved back to a known value; that value."""
    from app.models.unit_settings import UnitSettings
    _unit(db_session, "backup")
    assert _as(client, "admin", "first").put("/system/unit", json={"mode": "offline"}).status_code == 200
    db_session.expire_all()
    row = db_session.get(UnitSettings, 1)
    row.mode_set_at = datetime(2026, 1, 1, 9, 0, 0)
    db_session.commit()
    body = _as(client, "reviewer").get("/system/unit").json()
    assert body["modeSetBy"] == "first"
    return body["modeSetAt"]


@pytest.mark.unit
@pytest.mark.parametrize("change", [
    {"projectsEnabled": True},
    {"mode": "offline", "projectsEnabled": True},  # the mode it already has
    {"mode": "offline"},
    {},
    {"defaultLanguage": "en"},
    {"colourCardShots": True},
])
def test_changing_anything_but_the_mode_leaves_both_unchanged(client, db_session, link, change):
    at = _set_by_first(client, db_session)
    resp = _as(client, "admin", "second").put("/system/unit", json=change)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["modeSetBy"], body["modeSetAt"]) == ("first", at)
    assert body["mode"] == "offline"


@pytest.mark.unit
def test_the_projects_setting_leaves_both_unchanged_but_moves_updated_by(client, db_session, link):
    from app.models.unit_settings import UnitSettings
    at = _set_by_first(client, db_session)
    _as(client, "admin", "second").put("/system/unit", json={"projectsEnabled": True})
    db_session.expire_all()
    row = db_session.get(UnitSettings, 1)
    assert row.updated_by == "second"
    assert (row.mode_set_by, row.mode_set_at) == ("first", datetime(2026, 1, 1, 9, 0, 0))
    assert at.startswith("2026-01-01T09:00:00")


@pytest.mark.unit
def test_the_capture_key_leaves_both_unchanged(client, db_session, link):
    at = _set_by_first(client, db_session)
    chord = {"key": "F8", "ctrl": False, "alt": False, "meta": False, "shift": False}
    resp = _as(client, "operator", "op").put("/system/unit/capture-key", json={"capture_key": chord})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["modeSetBy"], resp.json()["modeSetAt"]) == ("first", at)


@pytest.mark.unit
def test_a_switch_committed_during_the_read_never_names_its_admin_for_the_old_mode(
        client, db_session, link, monkeypatch):
    """The mode and who set it come from one read: a switch committed after it changes neither."""
    from app.core import unit_mode
    at = _set_by_first(client, db_session)
    original = unit_mode._row
    reads = {"n": 0, "switching": False}

    def row_then_switch(db, lock=False):
        # The switch is committed just before the second read of the row in as_api.
        if not reads["switching"]:
            reads["n"] += 1
            if reads["n"] == 2:
                reads["switching"] = True
                try:
                    unit_mode.change(db, actor="second", mode="backup")
                finally:
                    reads["switching"] = False
        return original(db, lock)

    monkeypatch.setattr(unit_mode, "_row", row_then_switch)
    body = unit_mode.as_api(db_session)
    monkeypatch.setattr(unit_mode, "_row", original)
    assert reads["n"] >= 2, "the switch never ran inside as_api"
    assert unit_mode.current(db_session).mode == "backup"
    answer = (body["mode"], body["modeSetBy"])
    assert answer in {("offline", "first"), ("backup", "second")}, answer
    if body["modeSetBy"] == "first":
        assert body["modeSetAt"] == at


@pytest.mark.unit
def test_the_testing_account_is_not_told_who_set_the_mode(client, db_session, link):
    at = _set_by_first(client, db_session)
    body = _as(client, "testing", "tester").get("/system/unit")
    assert body.status_code == 200, body.text
    assert body.json()["modeSetBy"] is None
    assert body.json()["modeSetAt"] == at
    assert _as(client, "reviewer").get("/system/unit").json()["modeSetBy"] == "first"


# ---------------------------------------------------------------------------
# A row from before the revision
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_row_written_before_the_revision_answers_null_for_both(client, db_session):
    _unit(db_session, "backup", projects_enabled=True)
    body = _as(client, "reviewer").get("/system/unit").json()
    assert body["mode"] == "backup"
    assert body["modeSetBy"] is None and body["modeSetAt"] is None


@pytest.mark.unit
def test_with_no_row_yet_both_are_null(db_session):
    from app.core import unit_mode
    body = unit_mode.as_api(db_session)
    assert body["modeSetBy"] is None and body["modeSetAt"] is None


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------

def _load(backend_root):
    path = backend_root / "alembic" / "versions" / MIGRATION_FILE
    spec = importlib.util.spec_from_file_location("unit_mode_set_by_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(engine, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def _unit_settings_before(engine):
    """unit_settings with the columns the revision's parent leaves it, and the unit's one row."""
    md = sa.MetaData()
    sa.Table("unit_settings", md,
             sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
             sa.Column("mode", sa.String(16), nullable=False),
             sa.Column("updated_at", sa.DateTime, nullable=False),
             sa.Column("updated_by", sa.String(150), nullable=True))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO unit_settings (id, mode, updated_at, updated_by) "
                             "VALUES (1, 'backup', CURRENT_TIMESTAMP, 'boss')"))


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
def test_the_migration_adds_two_null_columns_and_drops_them(backend_root, tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    try:
        mod = _load(backend_root)
        _unit_settings_before(engine)
        _run(engine, mod.upgrade)
        cols = {c["name"]: c for c in sa.inspect(engine).get_columns("unit_settings")}
        assert cols["mode_set_by"]["nullable"] is True and cols["mode_set_at"]["nullable"] is True
        with engine.begin() as conn:
            # No backfill: nobody knows who set the existing row's mode.
            row = conn.execute(sa.text("SELECT mode, updated_by, mode_set_by, mode_set_at FROM unit_settings")).one()
            assert tuple(row) == ("backup", "boss", None, None)
            conn.execute(sa.text("UPDATE unit_settings SET mode_set_by = 'boss', mode_set_at = CURRENT_TIMESTAMP"))

        _run(engine, mod.downgrade)
        cols = {c["name"] for c in sa.inspect(engine).get_columns("unit_settings")}
        assert "mode_set_by" not in cols and "mode_set_at" not in cols
        with engine.begin() as conn:
            assert tuple(conn.execute(sa.text("SELECT id, mode FROM unit_settings")).one()) == (1, "backup")
    finally:
        engine.dispose()


@pytest.mark.unit
def test_the_model_matches_the_migration(backend_root, tmp_path):
    from app.models.unit_settings import UnitSettings

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    try:
        _unit_settings_before(engine)
        _run(engine, _load(backend_root).upgrade)
        mig = {c["name"]: c for c in sa.inspect(engine).get_columns("unit_settings")}
        for name in ("mode_set_by", "mode_set_at"):
            col = UnitSettings.__table__.c[name]
            assert col.nullable is True
            assert str(col.type) == str(mig[name]["type"]), name
        assert str(UnitSettings.__table__.c.mode_set_by.type) == str(UnitSettings.__table__.c.updated_by.type)
        assert str(UnitSettings.__table__.c.mode_set_at.type) == str(UnitSettings.__table__.c.updated_at.type)
    finally:
        engine.dispose()
