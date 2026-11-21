"""Testing volumes expire, and the testing space is cleared (NEH-391).

A testing volume is kept for `unit_settings.testing_expiry_days` after its
last capture: `collections.testing_expires_at` is set when it is created
and at every capture or upload into it, and a capture into a sub-volume
sets it on every ancestor too. A change of the term shifts every testing
volume's date by the difference. Once a volume's date and every
descendant's are past, an hourly run removes it completely: its rows, its
directory and its manifest, which is removed and never retired. Testing
projects that hold nothing and were created more than the term ago go
too. An administrator clears the whole space at once (POST
/system/testing/clear). Nothing of real work is ever touched: the canary
test lists every file under the projects root and every row of every
table before and after.

The suite runs on SQLite, which takes no row locks. A capture holding a
volume is stood in for through testing_space._lock_free, the one place a
purge takes its locks, and the statement it sends to PostgreSQL is read
(FOR UPDATE SKIP LOCKED).

    python -m pytest tests/unit/test_testing_space_expiry.py

Version: 0.3.3
"""

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.unit.test_testing_space_capture import (  # noqa: F401  (backend, lookups and unit are fixtures)
    T, TP, backend, lookups, unit,
)

NOW = datetime(2026, 11, 20, 12, 0, 0)


@pytest.fixture
def clock(monkeypatch):
    """testing_space's clock, fixed; `clock.now` moves it."""
    from app.core import testing_space

    class Clock:
        now = NOW

    c = Clock()
    monkeypatch.setattr(testing_space, "_now", lambda: c.now)
    return c


def _volume_row(db, volume_id):
    from app.models.collection import Collection
    db.expire_all()
    return db.get(Collection, volume_id)


def _capture(unit, project, volume_id, dual=True):
    body = {"project_name": project["name"], "collection_id": volume_id}
    if not dual:
        body["camera_index"] = 0
    resp = unit.post("testing", "/cameras/capture/dual" if dual else "/cameras/capture", body)
    assert resp.status_code == 200 and resp.json()["success"], resp.text
    return resp.json()["record_id"]


def _set_expiry(db, volume_id, when):
    row = _volume_row(db, volume_id)
    row.testing_expires_at = when
    db.commit()


def _tree(unit):
    """A testing project, a volume and a sub-volume, both captured into."""
    project = unit.project("Pruebas")
    top = unit.volume(project_id=project["id"], name="Tomo")
    sub = unit.volume(parent_id=top["id"], name="Cuadernillo")
    _capture(unit, project, top["id"])
    _capture(unit, project, sub["id"], dual=False)
    return project, top, sub


# ---------------------------------------------------------------------------
# What the listings compare
# ---------------------------------------------------------------------------

def _files(root: Path):
    """Every file under the projects root with its bytes, and every directory."""
    files = {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
    dirs = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_dir()}
    return files, dirs


def _tables(db):
    from app.core.db import Base
    db.expire_all()
    return {name: sorted((dict(r._mapping) for r in db.execute(select(table)).all()), key=repr)
            for name, table in Base.metadata.tables.items()}


def _linked_tables():
    """The tables with a foreign-key path to collections or records, and the column each is read by."""
    from app.core.db import Base
    tables = Base.metadata.tables
    linked = {"collections": "id", "records": "id"}
    changed = True
    while changed:
        changed = False
        for name, table in tables.items():
            for fk in table.foreign_keys:
                if fk.column.table.name in linked and name not in linked and name != "projects":
                    linked[name] = fk.parent.name
                    changed = True
    return linked


def _ids(before_tables, volume_ids):
    """The ids, by table, of every row a purge of `volume_ids` removes, read from a listing."""
    volumes = set(volume_ids)
    records = {r["id"] for r in before_tables["records"] if r["collection_id"] in volumes}
    images = {r["id"] for r in before_tables["record_images"] if r["record_id"] in records}
    return volumes, records, images


def _belongs(table, row, volumes, records, images) -> bool:
    if table == "collections":
        return row["id"] in volumes
    if table == "records":
        return row["id"] in records
    if table == "record_images":
        return row["id"] in images
    if "record_image_id" in row:
        return row["record_image_id"] in images
    if table == "record_status_changes":
        return row["record_id"] in records or row["collection_id"] in volumes
    if "record_id" in row:
        return row["record_id"] in records
    if "collection_id" in row:
        return row["collection_id"] in volumes
    return False


def _canaries(root: Path):
    """Real material no row of the testing space points at: a real project's manifest and a retired real manifest."""
    real_manifest = root / "proyecto-real" / "metadata" / "manifest.jsonl"
    real_manifest.parent.mkdir(parents=True, exist_ok=True)
    real_manifest.write_text('{"capture_id": "real-canary"}\n')
    retired = root / ".retired-manifests" / "proyecto-viejo-20260101T000000Z-manifest.jsonl"
    retired.parent.mkdir(parents=True, exist_ok=True)
    retired.write_text('{"capture_id": "retired-canary"}\n')
    loose = root / "suelto.txt"
    loose.write_text("a file no row names")
    return [real_manifest, retired, loose]


# ---------------------------------------------------------------------------
# The purge never touches real work
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_purge_removes_its_volume_and_touches_nothing_else(unit, clock):
    from app.core import testing_space
    project, top, sub = _tree(unit)
    # Another testing volume, which stays.
    other = unit.volume(project_id=project["id"], name="Otro")
    _capture(unit, project, other["id"])
    canaries = _canaries(unit.root)
    files, dirs = _files(unit.root)
    tables = _tables(unit.db)
    assert any(name.startswith(top["filename_stem"] + "/") for name in files)
    assert f"{top['filename_stem']}/metadata/manifest.jsonl" in files

    assert testing_space.purge_volume(unit.db, _volume_row(unit.db, top["id"])) is True

    after_files, after_dirs = _files(unit.root)
    gone = (top["filename_stem"], sub["filename_stem"])
    assert after_files == {k: v for k, v in files.items() if k.split("/")[0] not in gone}
    assert after_dirs == {d for d in dirs if d.split("/")[0] not in gone}
    assert all(p.is_file() for p in canaries)
    # No manifest of the purged volumes is retired.
    assert sorted(p.name for p in (unit.root / ".retired-manifests").iterdir()) == [canaries[1].name]

    volumes, records, images = _ids(tables, [top["id"], sub["id"]])
    assert records and images
    after = _tables(unit.db)
    linked = _linked_tables()
    assert {"collections", "records", "record_images", "camera_settings", "exif_data", "offsite_objects",
            "record_annotations", "record_rejections", "record_status_changes", "fisqua_outbox",
            "fisqua_status"} <= set(linked)
    for name, rows in tables.items():
        if name == "system_logs":
            assert all(r in after[name] for r in rows)
            continue
        expected = [r for r in rows if not _belongs(name, r, volumes, records, images)]
        assert after[name] == expected, name
    for name in linked:
        for row in after[name]:
            assert not _belongs(name, row, volumes, records, images), (name, row)


@pytest.mark.unit
def test_a_purge_refuses_a_real_volume_and_changes_nothing(unit, clock):
    from app.core import testing_space
    _canaries(unit.root)
    files = _files(unit.root)
    tables = _tables(unit.db)
    with pytest.raises(testing_space.NotTestingWork):
        testing_space.purge_volume(unit.db, _volume_row(unit.db, unit.real_volume.id))
    unit.db.rollback()
    assert _files(unit.root) == files
    assert _tables(unit.db) == tables


@pytest.mark.unit
def test_the_refusal_is_the_first_statement_and_no_assert():
    """The real-volume refusal cannot be skipped: it is not an assert, which python -O drops."""
    import ast
    import inspect
    import textwrap
    from app.core import testing_space
    tree = ast.parse(textwrap.dedent(inspect.getsource(testing_space.purge_volume)))
    body = [n for n in tree.body[0].body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    first = body[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
    assert getattr(first.value.func, "id", None) == "refuse_real_work"
    assert not any(isinstance(n, ast.Assert) for n in ast.walk(tree))


@pytest.mark.unit
def test_refuse_real_work_reads_the_flag_and_the_namespace():
    from types import SimpleNamespace
    from app.core import testing_space
    for bad in (SimpleNamespace(testing=False, filename_stem=T + "000001"),
                SimpleNamespace(testing=True, filename_stem="xx-real-1"),
                SimpleNamespace(testing=True, filename_stem=None),
                None):
        with pytest.raises(testing_space.NotTestingWork):
            testing_space.refuse_real_work(bad)
    testing_space.refuse_real_work(SimpleNamespace(testing=True, filename_stem=T + "000001"))


@pytest.mark.unit
def test_a_crash_between_the_files_and_the_rows_leaves_a_closed_state_the_next_run_completes(
        unit, clock, monkeypatch):
    from app.core import testing_space
    project, top, sub = _tree(unit)
    record = _capture(unit, project, sub["id"], dual=False)
    tables = _tables(unit.db)
    real_delete = testing_space._delete_volume_rows

    def crash(*args, **kwargs):
        raise RuntimeError("power lost")

    monkeypatch.setattr(testing_space, "_delete_volume_rows", crash)
    with pytest.raises(RuntimeError):
        testing_space.purge_volume(unit.db, _volume_row(unit.db, top["id"]))
    unit.db.rollback()
    monkeypatch.setattr(testing_space, "_delete_volume_rows", real_delete)
    # The files went; every row is still there, each volume marked with the fixed past instant.
    assert not (unit.root / top["filename_stem"]).exists()
    assert not (unit.root / sub["filename_stem"]).exists()
    for v in (top, sub):
        assert _volume_row(unit.db, v["id"]).testing_expires_at == testing_space.REMOVING_AT
    after = _tables(unit.db)
    for name, rows in tables.items():
        if name in ("system_logs", "collections"):
            continue
        assert after[name] == rows, name
    # The state is closed: no capture, no upload, and no refresh or new term moves the mark.
    for dual in (True, False):
        resp = unit.post("testing", "/cameras/capture/dual" if dual else "/cameras/capture",
                         {"project_name": project["name"], "collection_id": sub["id"],
                          **({} if dual else {"camera_index": 0})})
        assert resp.status_code == 404 and resp.json()["detail"]["code"] == "not_found", resp.text
    resp = unit.client.post(f"/records/{record}/images", headers=unit.headers["testing"],
                            files={"file": ("p.jpg", b"\xff\xd8x", "image/jpeg")})
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "not_found", resp.text
    testing_space.refresh_expiry(unit.db, sub["id"])
    unit.db.commit()
    resp = unit.client.put("/system/testing", headers=unit.headers["admin"],
                           json={"enabled": True, "expiry_days": 60})
    assert resp.status_code == 200, resp.text
    for v in (top, sub):
        assert _volume_row(unit.db, v["id"]).testing_expires_at == testing_space.REMOVING_AT
    assert not (unit.root / sub["filename_stem"]).exists()
    # The next run completes it.
    assert testing_space.purge_expired(unit.db, NOW)["volumes"] == 2
    volumes, records, images = _ids(tables, [top["id"], sub["id"]])
    final = _tables(unit.db)
    for name in _linked_tables():
        assert not any(_belongs(name, r, volumes, records, images) for r in final[name]), name


@pytest.mark.unit
def test_a_purge_skips_a_volume_a_capture_holds(unit, clock, monkeypatch):
    from app.core import testing_space
    project, top, sub = _tree(unit)
    files = _files(unit.root)
    tables = _tables(unit.db)
    real = testing_space._lock_free

    def held(db, model, ids):
        return real(db, model, ids) - {sub["id"]}

    monkeypatch.setattr(testing_space, "_lock_free", held)
    assert testing_space.purge_volume(unit.db, _volume_row(unit.db, top["id"])) is False
    assert _files(unit.root) == files
    assert {k: v for k, v in _tables(unit.db).items() if k != "system_logs"} == {
        k: v for k, v in tables.items() if k != "system_logs"}


@pytest.mark.unit
def test_the_purge_locks_skip_what_a_capture_holds_on_postgresql(db_session):
    from sqlalchemy.dialects import postgresql
    from app.core import testing_space
    from app.models.collection import Collection
    sql = str(testing_space._lock_query(db_session, Collection, [1, 2]).statement.compile(
        dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE SKIP LOCKED" in sql, sql


# ---------------------------------------------------------------------------
# testing_expires_at
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_testing_volume_expires_a_term_after_its_creation(unit, clock):
    project = unit.project()
    top = unit.volume(project_id=project["id"])
    sub = unit.volume(parent_id=top["id"])
    for v in (top, sub):
        assert _volume_row(unit.db, v["id"]).testing_expires_at == NOW + timedelta(days=30)
    assert _volume_row(unit.db, unit.real_volume.id).testing_expires_at is None


@pytest.mark.unit
@pytest.mark.parametrize("how", ["single", "dual", "upload"])
def test_a_capture_into_a_sub_volume_refreshes_it_and_every_ancestor(unit, clock, how):
    project = unit.project()
    top = unit.volume(project_id=project["id"])
    mid = unit.volume(parent_id=top["id"])
    leaf = unit.volume(parent_id=mid["id"])
    sibling = unit.volume(parent_id=top["id"])
    record = _capture(unit, project, leaf["id"]) if how == "upload" else None
    clock.now = NOW + timedelta(days=3)
    if how == "upload":
        resp = unit.client.post(f"/records/{record}/images", headers=unit.headers["testing"],
                                files={"file": ("p.jpg", b"\xff\xd8x", "image/jpeg")})
        assert resp.status_code == 200, resp.text
    else:
        _capture(unit, project, leaf["id"], dual=how == "dual")
    for v in (top, mid, leaf):
        assert _volume_row(unit.db, v["id"]).testing_expires_at == clock.now + timedelta(days=30), v["id"]
    assert _volume_row(unit.db, sibling["id"]).testing_expires_at == NOW + timedelta(days=30)


@pytest.mark.unit
def test_a_real_capture_sets_no_expiry(unit, clock):
    unit.post("operator", "/cameras/capture/dual", {"project_name": unit.real_project.name,
                                                    "collection_id": unit.real_volume.id})
    assert _volume_row(unit.db, unit.real_volume.id).testing_expires_at is None


@pytest.mark.unit
@pytest.mark.parametrize("days,shift", [(40, 10), (25, -5), (30, 0)])
def test_a_new_term_shifts_every_testing_volumes_date_by_the_difference(unit, clock, days, shift):
    project = unit.project()
    a = unit.volume(project_id=project["id"])
    b = unit.volume(project_id=project["id"])
    _set_expiry(unit.db, b["id"], datetime(2027, 1, 1))
    unset = unit.volume(project_id=project["id"])
    _set_expiry(unit.db, unset["id"], None)
    resp = unit.client.put("/system/testing", headers=unit.headers["admin"],
                           json={"enabled": True, "expiry_days": days})
    assert resp.status_code == 200, resp.text
    assert _volume_row(unit.db, a["id"]).testing_expires_at == NOW + timedelta(days=30 + shift)
    assert _volume_row(unit.db, b["id"]).testing_expires_at == datetime(2027, 1, 1) + timedelta(days=shift)
    assert _volume_row(unit.db, unset["id"]).testing_expires_at is None
    assert _volume_row(unit.db, unit.real_volume.id).testing_expires_at is None


@pytest.mark.unit
def test_the_reads_carry_the_space_and_the_date(unit, clock):
    project = unit.project()
    vol = unit.volume(project_id=project["id"])
    assert vol["testing"] is True and vol["testing_expires_at"].startswith("2026-12-20T12:00:00")
    body = unit.get("testing", f"/collections/{vol['id']}").json()
    assert body["testing"] is True and body["testing_expires_at"].startswith("2026-12-20T12:00:00")
    real = unit.get("operator", f"/collections/{unit.real_volume.id}").json()
    assert real["testing"] is False and real["testing_expires_at"] is None
    assert project["testing"] is True
    assert unit.get("testing", f"/projects/{project['id']}").json()["testing"] is True
    assert unit.get("operator", f"/projects/{unit.real_project.id}").json()["testing"] is False


# ---------------------------------------------------------------------------
# purge_expired
# ---------------------------------------------------------------------------

def _age_project(db, project_id, days):
    from app.models.project import Project
    db.expire_all()
    p = db.get(Project, project_id)
    p.created_at = NOW - timedelta(days=days)
    db.commit()


@pytest.mark.unit
def test_purge_expired_removes_what_is_past_and_keeps_a_parent_with_a_live_child(unit, clock):
    from app.core import testing_space
    project = unit.project()
    gone = unit.volume(project_id=project["id"])
    live = unit.volume(project_id=project["id"])
    parent = unit.volume(project_id=project["id"])
    live_child = unit.volume(parent_id=parent["id"])
    kept_parent = unit.volume(project_id=project["id"])
    dead_child = unit.volume(parent_id=kept_parent["id"])
    undated = unit.volume(project_id=project["id"])
    _capture(unit, project, gone["id"])
    past, future = NOW - timedelta(seconds=1), NOW + timedelta(days=1)
    for v, when in ((gone, past), (live, future), (parent, past), (live_child, future),
                    (kept_parent, future), (dead_child, past), (undated, None)):
        _set_expiry(unit.db, v["id"], when)

    result = testing_space.purge_expired(unit.db, NOW)
    assert result == {"volumes": 2, "projects": 0, "skipped": 0}
    for v in (gone, dead_child):
        assert _volume_row(unit.db, v["id"]) is None
    for v in (live, parent, live_child, kept_parent, undated):
        assert _volume_row(unit.db, v["id"]) is not None
    assert not (unit.root / gone["filename_stem"]).exists()
    # Idempotent.
    assert testing_space.purge_expired(unit.db, NOW) == {"volumes": 0, "projects": 0, "skipped": 0}


@pytest.mark.unit
def test_purge_expired_removes_old_empty_testing_projects_and_their_directories(unit, clock):
    from app.core import testing_space
    from app.core.paths import project_dir_name
    from app.models.project import Project
    old = unit.project("Vieja")
    new = unit.project("Nueva")
    full = unit.project("Llena")
    unit.volume(project_id=full["id"])
    for p in (old, new, full):
        _age_project(unit.db, p["id"], 31 if p is not new else 29)
    stored = unit.db.get(Project, old["id"]).name
    directory = unit.root / project_dir_name(stored)
    (directory / "metadata").mkdir(parents=True)
    (directory / "metadata" / "manifest.jsonl").write_text("{}\n")
    _canaries(unit.root)
    retired_before = sorted(p.name for p in (unit.root / ".retired-manifests").iterdir())

    assert testing_space.purge_expired(unit.db, NOW)["projects"] == 1
    unit.db.expire_all()
    assert unit.db.get(Project, old["id"]) is None
    assert unit.db.get(Project, new["id"]) is not None and unit.db.get(Project, full["id"]) is not None
    assert unit.db.get(Project, unit.real_project.id) is not None
    assert not directory.exists()
    assert sorted(p.name for p in (unit.root / ".retired-manifests").iterdir()) == retired_before


@pytest.mark.unit
def test_purge_expired_runs_with_the_space_off(unit, clock):
    from app.core import testing_space
    from app.models.unit_settings import UnitSettings
    project = unit.project()
    vol = unit.volume(project_id=project["id"])
    _set_expiry(unit.db, vol["id"], NOW - timedelta(days=1))
    row = unit.db.get(UnitSettings, 1)
    row.testing_enabled = False
    unit.db.commit()
    assert testing_space.purge_expired(unit.db, NOW)["volumes"] == 1


@pytest.mark.unit
def test_purge_expired_skips_a_volume_a_capture_holds(unit, clock, monkeypatch):
    from app.core import testing_space
    project = unit.project()
    vol = unit.volume(project_id=project["id"])
    _set_expiry(unit.db, vol["id"], NOW - timedelta(days=1))
    real = testing_space._lock_free
    monkeypatch.setattr(testing_space, "_lock_free", lambda db, model, ids: real(db, model, ids) - {vol["id"]})
    assert testing_space.purge_expired(unit.db, NOW) == {"volumes": 0, "projects": 0, "skipped": 1}
    assert _volume_row(unit.db, vol["id"]) is not None


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_scheduler_purges_on_each_tick_and_a_second_tick_finds_nothing(unit, clock):
    from app.core import testing_space
    project = unit.project()
    vol = unit.volume(project_id=project["id"])
    _set_expiry(unit.db, vol["id"], NOW - timedelta(days=1))
    scheduler = testing_space.TestingExpiryScheduler()
    assert scheduler.tick() == {"volumes": 1, "projects": 0, "skipped": 0}
    assert scheduler.tick() == {"volumes": 0, "projects": 0, "skipped": 0}


@pytest.mark.unit
def test_a_failed_run_is_logged_and_raises_nothing(db_session, monkeypatch):
    from app.core import testing_space
    from app.models.system_log import SystemLog

    def boom(db, now):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(testing_space, "purge_expired", boom)
    assert testing_space.TestingExpiryScheduler().tick() is None
    db_session.expire_all()
    [row] = db_session.query(SystemLog).filter(SystemLog.action == "testing_purge_failed").all()
    assert row.level == "ERR" and row.category == "system"


@pytest.mark.unit
def test_one_run_at_a_time(unit, clock, monkeypatch):
    from app.core import testing_space
    monkeypatch.setattr(testing_space, "_purge_lock", _HeldLock())
    assert testing_space.TestingExpiryScheduler().tick() == {"status": "busy"}


@pytest.mark.unit
def test_the_scheduler_starts_and_stops_as_the_others_do(monkeypatch):
    from app.core import testing_space
    from app.core.scheduler_control import StoppableWorker
    scheduler = testing_space.TestingExpiryScheduler()
    assert isinstance(scheduler, StoppableWorker)
    monkeypatch.setattr(testing_space, "PURGE_START_DELAY_SECONDS", 3600)
    assert scheduler.start() is True and scheduler.is_running()
    assert scheduler.start() is True  # no second worker
    scheduler.stop()
    assert not scheduler.is_running()


@pytest.mark.unit
def test_the_application_starts_and_stops_the_scheduler():
    import inspect
    from app import main
    src = inspect.getsource(main.lifespan)
    assert "testing_expiry_scheduler.start()" in src and "testing_expiry_scheduler.stop()" in src


# ---------------------------------------------------------------------------
# POST /system/testing/clear
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_clear_removes_every_testing_volume_and_project_and_keeps_the_account(unit, clock):
    from app.models.system_log import SystemLog
    from app.models.user import User
    project, top, sub = _tree(unit)
    unit.project("Vacía")
    _canaries(unit.root)
    real_files = {k: v for k, v in _files(unit.root)[0].items() if not k.startswith(T)}
    resp = unit.post("admin", "/system/testing/clear")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"projects": 2, "volumes": 2, "skipped": 0}
    body = unit.get("admin", "/system/testing").json()
    assert body["projects"] == 0 and body["volumes"] == 0
    unit.db.expire_all()
    assert unit.db.get(User, unit.users["testing"].id) is not None
    assert _files(unit.root)[0] == real_files
    assert unit.db.query(SystemLog).filter(SystemLog.action == "testing_space_cleared").count() == 1


@pytest.mark.unit
def test_clear_skips_a_volume_a_capture_holds_and_keeps_its_project(unit, clock, monkeypatch):
    from app.core import testing_space
    project, top, sub = _tree(unit)
    other = unit.project("Otra")
    held_volume = unit.volume(project_id=other["id"])
    real = testing_space._lock_free
    monkeypatch.setattr(testing_space, "_lock_free",
                        lambda db, model, ids: real(db, model, ids) - {held_volume["id"]})
    resp = unit.post("admin", "/system/testing/clear")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"projects": 1, "volumes": 2, "skipped": 1}
    assert _volume_row(unit.db, held_volume["id"]) is not None
    body = unit.get("admin", "/system/testing").json()
    assert body["projects"] == 1 and body["volumes"] == 1


@pytest.mark.unit
@pytest.mark.parametrize("caller,status", [("operator", 403), ("reviewer", 403), ("testing", 403)])
def test_only_an_admin_clears(unit, clock, caller, status):
    project, top, sub = _tree(unit)
    assert unit.post(caller, "/system/testing/clear").status_code == status
    assert _volume_row(unit.db, top["id"]) is not None


@pytest.mark.unit
def test_clear_takes_no_body(unit, clock):
    resp = unit.client.post("/system/testing/clear", headers=unit.headers["admin"], json={"all": True})
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# The testing account's own deletes
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_deleting_a_testing_project_removes_its_manifest_and_retires_none(unit, clock):
    from app.core.paths import project_dir_name
    from app.models.project import Project
    project = unit.project("Borrar")
    directory = unit.root / project_dir_name(unit.db.get(Project, project["id"]).name)
    (directory / "metadata").mkdir(parents=True)
    (directory / "metadata" / "manifest.jsonl").write_text("{}\n")
    resp = unit.client.delete(f"/projects/{project['id']}", headers=unit.headers["testing"])
    assert resp.status_code == 200, resp.text
    assert not directory.exists()
    retired = unit.root / ".retired-manifests"
    assert not retired.exists() or not any(p.name.startswith(TP) for p in retired.iterdir())


@pytest.mark.unit
def test_deleting_a_real_project_still_retires_its_manifest(unit, clock):
    from app.core.paths import project_dir_name
    from app.models.project import Project
    p = Project(name="Para borrar")
    unit.db.add(p)
    unit.db.commit()
    directory = unit.root / project_dir_name(p.name)
    (directory / "metadata").mkdir(parents=True)
    (directory / "metadata" / "manifest.jsonl").write_text("{}\n")
    resp = unit.client.delete(f"/projects/{p.id}", headers=unit.headers["admin"])
    assert resp.status_code == 200, resp.text
    assert any(x.name.startswith(project_dir_name(p.name)) for x in (unit.root / ".retired-manifests").iterdir())


@pytest.mark.unit
def test_deleting_an_empty_testing_volume_and_a_testing_record_leaves_no_retired_manifest(unit, clock):
    project = unit.project()
    empty = unit.volume(project_id=project["id"])
    resp = unit.client.delete(f"/collections/{empty['id']}", headers=unit.headers["testing"])
    assert resp.status_code == 204, resp.text
    assert not (unit.root / empty["filename_stem"]).exists()
    vol = unit.volume(project_id=project["id"])
    rec = _capture(unit, project, vol["id"])
    resp = unit.client.delete(f"/records/{rec}", headers=unit.headers["testing"])
    assert resp.status_code == 200, resp.text
    assert not list((unit.root / vol["filename_stem"] / "captures").glob("*"))
    assert not (unit.root / ".retired-manifests").exists()


# ---------------------------------------------------------------------------
# The testing account deletes its own volumes; a testing record lives in a volume
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_testing_account_deletes_a_captured_volume_with_its_sub_volume_completely(unit, clock):
    project, top, sub = _tree(unit)
    canaries = _canaries(unit.root)
    files, dirs = _files(unit.root)
    tables = _tables(unit.db)
    resp = unit.client.delete(f"/collections/{top['id']}", headers=unit.headers["testing"])
    assert resp.status_code == 204, resp.text
    after_files, after_dirs = _files(unit.root)
    gone = (top["filename_stem"], sub["filename_stem"])
    assert after_files == {k: v for k, v in files.items() if k.split("/")[0] not in gone}
    assert after_dirs == {d for d in dirs if d.split("/")[0] not in gone}
    assert all(p.is_file() for p in canaries)
    volumes, records, images = _ids(tables, [top["id"], sub["id"]])
    assert records and images
    after = _tables(unit.db)
    for name, rows in tables.items():
        if name == "system_logs":
            continue
        assert after[name] == [r for r in rows if not _belongs(name, r, volumes, records, images)], name


@pytest.mark.unit
def test_deleting_a_testing_volume_a_capture_holds_is_a_409_and_removes_nothing(unit, clock, monkeypatch):
    from app.core import testing_space
    project, top, sub = _tree(unit)
    files = _files(unit.root)
    tables = _tables(unit.db)
    real = testing_space._lock_free
    monkeypatch.setattr(testing_space, "_lock_free", lambda db, model, ids: real(db, model, ids) - {sub["id"]})
    resp = unit.client.delete(f"/collections/{top['id']}", headers=unit.headers["testing"])
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "testing_volume_busy", resp.text
    assert _files(unit.root) == files
    assert {k: v for k, v in _tables(unit.db).items() if k != "system_logs"} == {
        k: v for k, v in tables.items() if k != "system_logs"}


@pytest.mark.unit
def test_a_real_captured_volume_is_still_refused_and_others_never_reach_a_testing_one(unit, clock):
    project, top, sub = _tree(unit)
    resp = unit.client.delete(f"/collections/{unit.real_volume.id}", headers=unit.headers["admin"])
    assert resp.status_code == 409, resp.text
    assert _volume_row(unit.db, unit.real_volume.id) is not None
    for caller in ("admin", "operator"):
        resp = unit.client.delete(f"/collections/{top['id']}", headers=unit.headers[caller])
        assert resp.status_code == 404 and resp.json()["detail"]["code"] == "not_found", resp.text
    assert _volume_row(unit.db, top["id"]) is not None


def _needs_volume(resp):
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "testing_record_needs_volume", resp.text


@pytest.mark.unit
def test_the_testing_accounts_record_needs_a_volume(unit, clock):
    project = unit.project()
    before = unit.snapshot()
    _needs_volume(unit.post("testing", "/records/", {"title": "x", "capture_mode": "single",
                                                     "project_id": project["id"]}))
    _needs_volume(unit.post("testing", "/records/", {"title": "x", "capture_mode": "single"}))
    assert unit.snapshot() == before


@pytest.mark.unit
def test_the_testing_account_no_longer_adds_a_record_to_a_project(unit, clock):
    project = unit.project()
    vol = unit.volume(project_id=project["id"])
    rec = _capture(unit, project, vol["id"])
    before = unit.snapshot()
    assert unit.post("testing", f"/projects/{project['id']}/add_record/{rec}").status_code == 403
    assert unit.snapshot() == before


@pytest.mark.unit
def test_the_testing_account_moves_no_record_out_of_its_volume(unit, clock):
    from app.models.record import Record
    project = unit.project()
    vol = unit.volume(project_id=project["id"])
    other = unit.volume(project_id=project["id"])
    resp = unit.post("testing", "/records/", {"title": "Hoja", "capture_mode": "single", "collection_id": vol["id"]})
    rec = resp.json()["id"]
    before = unit.snapshot()
    for body in ({"project_id": project["id"]}, {"collection_id": None}):
        resp = unit.client.patch(f"/records/{rec}", headers=unit.headers["testing"], json=body)
        _needs_volume(resp)
    assert unit.snapshot() == before
    resp = unit.client.patch(f"/records/{rec}", headers=unit.headers["testing"], json={"collection_id": other["id"]})
    assert resp.status_code == 200, resp.text
    unit.db.expire_all()
    assert unit.db.get(Record, rec).collection_id == other["id"]


def _orphan_record(unit, project_id, name="suelta.jpg"):
    """A testing project's record in no volume, with an image on disk in the project's directory, written directly."""
    from app.core.paths import project_dir_name
    from app.models.project import Project
    from app.models.record import Record, RecordImage
    stored = unit.db.get(Project, project_id).name
    path = unit.root / project_dir_name(stored) / "images" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"orphan")
    rec = Record(title="Suelta", project_id=project_id, capture_mode="single")
    unit.db.add(rec)
    unit.db.commit()
    img = RecordImage(record_id=rec.id, filename=name, file_path=str(path), format="JPG", is_current=True)
    unit.db.add(img)
    unit.db.commit()
    return rec.id, img.id, path


@pytest.mark.unit
def test_clear_removes_a_testing_projects_records_in_no_volume(unit, clock):
    from app.models.project import Project
    from app.models.record import Record, RecordImage
    project = unit.project("Huérfana")
    rec, img, path = _orphan_record(unit, project["id"])
    resp = unit.post("admin", "/system/testing/clear")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"projects": 1, "volumes": 0, "skipped": 0}
    unit.db.expire_all()
    assert unit.db.get(Project, project["id"]) is None
    assert unit.db.get(Record, rec) is None and unit.db.get(RecordImage, img) is None
    assert not path.exists() and not path.parent.parent.exists()


@pytest.mark.unit
def test_expiry_removes_an_old_testing_project_kept_only_by_records_in_no_volume(unit, clock):
    from app.core import testing_space
    from app.models.project import Project
    from app.models.record import Record
    project = unit.project("Huérfana")
    rec, img, path = _orphan_record(unit, project["id"])
    _age_project(unit.db, project["id"], 31)
    assert testing_space.purge_expired(unit.db, NOW)["projects"] == 1
    unit.db.expire_all()
    assert unit.db.get(Project, project["id"]) is None and unit.db.get(Record, rec) is None
    assert not path.exists()


# ---------------------------------------------------------------------------
# A volume with no date expires a term after its creation
# ---------------------------------------------------------------------------

def _created(db, volume_id, days_ago):
    row = _volume_row(db, volume_id)
    row.created_at = NOW - timedelta(days=days_ago)
    row.testing_expires_at = None
    db.commit()


@pytest.mark.unit
def test_a_volume_with_no_date_expires_a_term_after_its_creation(unit, clock):
    from app.core import testing_space
    project = unit.project()
    old = unit.volume(project_id=project["id"])
    young = unit.volume(project_id=project["id"])
    _created(unit.db, old["id"], 40)
    _created(unit.db, young["id"], 10)
    assert testing_space.purge_expired(unit.db, NOW) == {"volumes": 1, "projects": 0, "skipped": 0}
    assert _volume_row(unit.db, old["id"]) is None
    assert _volume_row(unit.db, young["id"]) is not None


@pytest.mark.unit
def test_an_aware_creation_time_is_read_as_utc(unit, clock):
    from datetime import timezone
    from app.core import testing_space
    from types import SimpleNamespace
    aware = SimpleNamespace(testing_expires_at=None,
                            created_at=datetime(2026, 10, 1, 7, 0, tzinfo=timezone(timedelta(hours=-5))))
    assert testing_space.effective_expiry(aware, timedelta(days=30)) == datetime(2026, 10, 31, 12, 0)


# ---------------------------------------------------------------------------
# Nothing decided before a lock is trusted after it
# ---------------------------------------------------------------------------

def _other_session():
    from app.core.db import SessionLocal
    return SessionLocal()


def _inject_child(root, parent_id, stem, title="Intrusa"):
    """A testing sub-volume written under `parent_id` from another session, with a record and an image on disk."""
    from app.models.collection import Collection
    from app.models.record import Record, RecordImage
    s = _other_session()
    try:
        parent = s.get(Collection, parent_id)
        child = Collection(name="Intruso", parent_collection_id=parent_id, project_id=None, testing=True,
                           kept_on_unit=True, filename_stem=stem,
                           testing_expires_at=parent.testing_expires_at)
        s.add(child)
        s.commit()
        path = root / stem / "captures" / f"{stem}-img_0001_c0.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"intrusa")
        rec = Record(title=title, collection_id=child.id, capture_mode="single", status="captured")
        s.add(rec)
        s.commit()
        img = RecordImage(record_id=rec.id, filename=path.name, file_path=str(path), format="JPG",
                          is_current=True)
        s.add(img)
        s.commit()
        return child.id, rec.id, img.id, path
    finally:
        s.close()


def _gone(db, volume_id, record_id, image_id, path):
    from app.models.collection import Collection
    from app.models.record import Record, RecordImage
    db.expire_all()
    return (db.get(Collection, volume_id) is None and db.get(Record, record_id) is None
            and db.get(RecordImage, image_id) is None and not path.exists())


@pytest.mark.unit
@pytest.mark.parametrize("stage", ["before_lock", "after_mark"])
def test_a_sub_volume_that_joins_the_tree_in_a_window_is_removed_with_it(unit, clock, monkeypatch, stage):
    from app.core import testing_space
    project, top, sub = _tree(unit)
    injected = {}

    def hook(at, db, ids):
        if at == stage and not injected:
            injected["child"] = _inject_child(unit.root, sub["id"], T + "900001")

    monkeypatch.setattr(testing_space, "_purge_hook", hook)
    assert testing_space.purge_volume(unit.db, _volume_row(unit.db, top["id"])) is True
    assert _gone(unit.db, *injected["child"])
    assert _volume_row(unit.db, top["id"]) is None and _volume_row(unit.db, sub["id"]) is None


def _mark(db, volume_id):
    from app.core import testing_space
    _set_expiry(db, volume_id, testing_space.REMOVING_AT)


@pytest.mark.unit
@pytest.mark.parametrize("marked", ["parent", "grandparent"])
def test_nothing_joins_a_marked_tree(unit, clock, marked):
    project, top, sub = _tree(unit)
    _mark(unit.db, top["id"] if marked == "grandparent" else sub["id"])
    loose = unit.volume(project_id=project["id"], name="Suelto")
    before = unit.snapshot()
    resp = unit.post("testing", "/collections/", {"name": "Nuevo", "parent_collection_id": sub["id"]})
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "not_found", resp.text
    resp = unit.client.patch(f"/collections/{loose['id']}", headers=unit.headers["testing"],
                             json={"parent_collection_id": sub["id"]})
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "not_found", resp.text
    assert unit.snapshot() == before


@pytest.mark.unit
def test_a_tree_not_marked_still_takes_new_and_moved_sub_volumes(unit, clock):
    project, top, sub = _tree(unit)
    loose = unit.volume(project_id=project["id"], name="Suelto")
    assert unit.post("testing", "/collections/", {"name": "Nuevo", "parent_collection_id": sub["id"]}).status_code == 201
    resp = unit.client.patch(f"/collections/{loose['id']}", headers=unit.headers["testing"],
                             json={"parent_collection_id": sub["id"]})
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# A term change never waits for a volume lock while it holds the settings row
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_shift_skips_locked_volumes_and_the_mark_on_both_dialects():
    from sqlalchemy.dialects import postgresql, sqlite
    from app.core import testing_space
    pg = str(testing_space._shift_statement(5, "postgresql").compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE SKIP LOCKED" in pg and "TESTING_EXPIRES_AT !=" in pg, pg
    lite = str(testing_space._shift_statement(5, "sqlite").compile(dialect=sqlite.dialect())).upper()
    assert "DATETIME(" in lite and "TESTING_EXPIRES_AT !=" in lite, lite


@pytest.mark.unit
def test_the_term_is_committed_before_the_shift_starts(unit, clock, monkeypatch):
    from app.core import testing_space, unit_mode
    project = unit.project()
    vol = unit.volume(project_id=project["id"])
    seen = []
    real = testing_space._shift_expiry

    def spy(db, days):
        s = _other_session()
        try:
            seen.append(unit_mode.testing_settings(s).expiry_days)
        finally:
            s.close()
        return real(db, days)

    monkeypatch.setattr(testing_space, "_shift_expiry", spy)
    resp = unit.client.put("/system/testing", headers=unit.headers["admin"], json={"enabled": True, "expiry_days": 45})
    assert resp.status_code == 200, resp.text
    assert seen == [45]
    assert _volume_row(unit.db, vol["id"]).testing_expires_at == NOW + timedelta(days=45)


# ---------------------------------------------------------------------------
# Expiry reads the date again under the locks
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_capture_between_the_selection_and_the_lock_keeps_the_volume(unit, clock, monkeypatch):
    from app.core import testing_space
    project = unit.project()
    vol = unit.volume(project_id=project["id"])
    _capture(unit, project, vol["id"])
    _set_expiry(unit.db, vol["id"], NOW - timedelta(days=1))
    files = _files(unit.root)

    def hook(at, db, ids):
        if at == "before_lock":
            s = _other_session()
            try:
                testing_space.refresh_expiry(s, vol["id"])
                s.commit()
            finally:
                s.close()

    monkeypatch.setattr(testing_space, "_purge_hook", hook)
    assert testing_space.purge_expired(unit.db, NOW) == {"volumes": 0, "projects": 0, "skipped": 0}
    row = _volume_row(unit.db, vol["id"])
    assert row is not None and row.testing_expires_at == NOW + timedelta(days=30)
    assert _files(unit.root) == files


@pytest.mark.unit
def test_a_term_extended_between_the_selection_and_the_lock_keeps_the_volume(unit, clock, monkeypatch):
    from app.core import testing_space
    from app.models.unit_settings import UnitSettings
    project = unit.project()
    vol = unit.volume(project_id=project["id"])
    _created(unit.db, vol["id"], 40)

    def hook(at, db, ids):
        if at == "before_lock":
            s = _other_session()
            try:
                s.get(UnitSettings, 1).testing_expiry_days = 60
                s.commit()
            finally:
                s.close()

    monkeypatch.setattr(testing_space, "_purge_hook", hook)
    assert testing_space.purge_expired(unit.db, NOW)["volumes"] == 0
    row = _volume_row(unit.db, vol["id"])
    assert row is not None and row.testing_expires_at is None


# ---------------------------------------------------------------------------
# Every projects root the unit knows
# ---------------------------------------------------------------------------

@pytest.fixture
def roots(unit, tmp_path, monkeypatch):
    """The default root (the unit's) and an override root B, switched with `use_b`, made unreadable with `lose_b`."""
    from app.core import storage_override

    class Roots:
        a = unit.root
        b = tmp_path / "drive_b"
        override = None
        lost = False

        def use_b(self):
            self.override = str(self.b)
            self.b.mkdir(exist_ok=True)

        def lose_b(self, lost=True):
            self.lost = lost

    r = Roots()
    monkeypatch.setattr(storage_override, "get_storage_override_or_fallback", lambda: (r.override, False))
    real_problem = storage_override.override_root_problem

    def problem(root):
        if r.override is not None and str(root).startswith(str(r.b)):
            return "The storage drive is not mounted." if r.lost else None
        return real_problem(root)

    monkeypatch.setattr(storage_override, "override_root_problem", problem)
    return r


def _all_files(unit, roots):
    from app.core import config
    out = {}
    for label, base in (("a", roots.a), ("b", roots.b), ("data", Path(config.settings.DATA_DIR))):
        if base.exists():
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    out[f"{label}/{p.relative_to(base)}"] = p.read_bytes()
    return out


@pytest.mark.unit
def test_a_purge_removes_the_files_on_a_root_that_is_no_longer_the_active_one(unit, clock, roots):
    from app.core import testing_space
    project, top, sub = _tree(unit)
    assert (roots.a / top["filename_stem"]).is_dir()
    roots.use_b()
    assert testing_space.purge_volume(unit.db, _volume_row(unit.db, top["id"])) is True
    assert not (roots.a / top["filename_stem"]).exists() and not (roots.a / sub["filename_stem"]).exists()
    assert _volume_row(unit.db, top["id"]) is None


@pytest.mark.unit
def test_every_root_and_the_data_directory_are_compared_with_real_canaries_inside_and_beside(unit, clock, roots):
    from app.core import testing_space
    from app.models.collection import Collection
    from app.models.record import Record, RecordImage
    project, top, sub = _tree(unit)
    # A real volume named and stemmed as close to the testing namespace as the rules allow.
    near = Collection(name="Captua testing", project_id=unit.real_project.id, filename_stem="captua-testing")
    unit.db.add(near)
    unit.db.commit()
    resp = unit.post("operator", "/cameras/capture/dual", {"project_name": unit.real_project.name,
                                                           "collection_id": near.id})
    assert resp.status_code == 200 and resp.json()["success"], resp.text
    # A real record whose image row's file lies inside the testing volume's directory, written directly.
    inside = roots.a / top["filename_stem"] / "captures" / "real-inside.jpg"
    inside.write_bytes(b"real inside")
    real_rec = Record(title="Real dentro", collection_id=unit.real_volume.id, capture_mode="single")
    unit.db.add(real_rec)
    unit.db.commit()
    real_img = RecordImage(record_id=real_rec.id, filename=inside.name, file_path=str(inside), format="JPG",
                           is_current=True)
    unit.db.add(real_img)
    unit.db.commit()
    real_ids = (real_rec.id, real_img.id, near.id)
    _canaries(roots.a)
    roots.use_b()
    _canaries(roots.b)
    before = _all_files(unit, roots)
    tables = _tables(unit.db)

    assert testing_space.purge_volume(unit.db, _volume_row(unit.db, top["id"])) is True

    gone = (f"a/{top['filename_stem']}/", f"a/{sub['filename_stem']}/")
    keep = f"a/{top['filename_stem']}/captures/real-inside.jpg"
    expected = {k: v for k, v in before.items() if not k.startswith(gone) or k == keep}
    assert _all_files(unit, roots) == expected
    assert sorted(p.name for p in (roots.a / top["filename_stem"]).rglob("*")) == ["captures", "real-inside.jpg"]
    unit.db.expire_all()
    assert unit.db.get(Record, real_ids[0]) is not None and unit.db.get(RecordImage, real_ids[1]) is not None
    assert unit.db.get(Collection, real_ids[2]) is not None
    volumes, records, images = _ids(tables, [top["id"], sub["id"]])
    after = _tables(unit.db)
    for name, rows in tables.items():
        if name == "system_logs":
            continue
        assert after[name] == [r for r in rows if not _belongs(name, r, volumes, records, images)], name


# ---------------------------------------------------------------------------
# One failure stops nothing after it
# ---------------------------------------------------------------------------

def _failures(db):
    from app.models.system_log import SystemLog
    db.expire_all()
    return [r.detail for r in db.query(SystemLog).filter(SystemLog.action == "testing_purge_item_failed")]


@pytest.mark.unit
@pytest.mark.parametrize("how", ["expiry", "clear"])
def test_a_project_that_fails_is_logged_and_the_next_one_is_removed(unit, clock, monkeypatch, how):
    from app.core import testing_space
    from app.models.project import Project
    first, second = unit.project("Primera"), unit.project("Segunda")
    for p in (first, second):
        _age_project(unit.db, p["id"], 31)
    real = testing_space._purge_project

    def flaky(db, project):
        if project.id == first["id"]:
            raise OSError("rmtree failed")
        return real(db, project)

    monkeypatch.setattr(testing_space, "_purge_project", flaky)
    if how == "expiry":
        result = testing_space.purge_expired(unit.db, NOW)
        assert result == {"volumes": 0, "projects": 1, "skipped": 1}
    else:
        resp = unit.post("admin", "/system/testing/clear")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"projects": 1, "volumes": 0, "skipped": 1}
        from app.models.system_log import SystemLog
        assert unit.db.query(SystemLog).filter(SystemLog.action == "testing_space_cleared").count() == 1
    unit.db.expire_all()
    assert unit.db.get(Project, first["id"]) is not None and unit.db.get(Project, second["id"]) is None
    [detail] = _failures(unit.db)
    assert f"project {first['id']}" in detail and "OSError" in detail


@pytest.mark.unit
@pytest.mark.parametrize("how", ["expiry", "clear"])
def test_a_volume_that_fails_is_logged_and_the_next_one_is_removed(unit, clock, monkeypatch, how):
    from app.core import testing_space
    project = unit.project()
    bad = unit.volume(project_id=project["id"], name="Mala")
    good = unit.volume(project_id=project["id"], name="Buena")
    for v in (bad, good):
        _set_expiry(unit.db, v["id"], NOW - timedelta(days=1))
    real = testing_space._remove_volume_files

    def flaky(db, *, stems, image_ids):
        if bad["filename_stem"] in stems:
            raise PermissionError("read-only")
        return real(db, stems=stems, image_ids=image_ids)

    monkeypatch.setattr(testing_space, "_remove_volume_files", flaky)
    if how == "expiry":
        assert testing_space.purge_expired(unit.db, NOW)["skipped"] == 1
    else:
        resp = unit.post("admin", "/system/testing/clear")
        assert resp.status_code == 200 and resp.json()["skipped"] == 1, resp.text
    assert _volume_row(unit.db, good["id"]) is None and _volume_row(unit.db, bad["id"]) is not None
    [detail] = _failures(unit.db)
    assert f"volume {bad['id']}" in detail and "PermissionError" in detail


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

@pytest.mark.unit
class _HeldLock:
    """A purge lock some other run holds: a non-blocking acquire fails, and a blocking one fails the test instead of waiting."""

    def acquire(self, blocking=True, timeout=-1):
        if blocking:
            raise AssertionError("waited on the purge lock")
        return False

    def release(self):
        raise AssertionError("released a lock it does not hold")

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        return False


@pytest.mark.unit
def test_clear_answers_at_once_while_a_scheduled_run_holds_the_lock(unit, clock, monkeypatch):
    from app.core import testing_space
    project, top, sub = _tree(unit)
    monkeypatch.setattr(testing_space, "_purge_lock", _HeldLock())
    resp = unit.post("admin", "/system/testing/clear")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "testing_purge_running", resp.text
    assert _volume_row(unit.db, top["id"]) is not None


@pytest.mark.unit
def test_the_last_guard_after_the_files_refuses_a_delete_that_would_cascade(unit, clock, monkeypatch):
    from app.core import testing_space
    from app.models.collection import Collection
    from app.models.record import Record
    project, top, sub = _tree(unit)
    ids = [top["id"], sub["id"]]
    injected = {}
    # (a) bypassed: the tree is always read as it was; (b) bypassed: the child is written directly,
    # once the files are gone, so only the guard after the files stands between it and the delete.
    monkeypatch.setattr(testing_space, "_subtree", lambda db, volume_id: list(ids))

    def hook(at, db, locked):
        if at == "after_files" and not injected:
            injected["child"] = _inject_child(unit.root, sub["id"], T + "900002")

    monkeypatch.setattr(testing_space, "_purge_hook", hook)
    assert testing_space.purge_volume(unit.db, _volume_row(unit.db, top["id"])) is False
    assert injected, "the purge did not reach the files"
    assert not (unit.root / top["filename_stem"]).exists()
    child, rec, img, path = injected["child"]
    unit.db.expire_all()
    assert unit.db.get(Collection, child) is not None and unit.db.get(Record, rec).collection_id == child
    assert path.exists()
    for v in ids:
        assert _volume_row(unit.db, v).testing_expires_at == testing_space.REMOVING_AT


@pytest.mark.unit
def test_an_unreadable_root_that_is_not_the_active_one_keeps_the_rows_until_it_returns(unit, clock, roots):
    """Captured on the default root A; B is then activated and A cannot be read: code that knows only the
    active root would find nothing on B and delete the rows."""
    import os
    from app.core import testing_space
    from app.models.system_log import SystemLog
    project, top, sub = _tree(unit)
    for v in (top, sub):
        _set_expiry(unit.db, v["id"], NOW - timedelta(days=1))
    roots.use_b()
    os.chmod(roots.a, 0)
    try:
        assert testing_space.purge_expired(unit.db, NOW) == {"volumes": 0, "projects": 0, "skipped": 2}
    finally:
        os.chmod(roots.a, 0o755)
    for v in (top, sub):
        assert _volume_row(unit.db, v["id"]).testing_expires_at == testing_space.REMOVING_AT
    assert (roots.a / top["filename_stem"]).is_dir()
    unit.db.expire_all()
    rows = unit.db.query(SystemLog).filter(SystemLog.action == "testing_purge_root_unreadable").all()
    assert len(rows) == 1 and str(roots.a) in rows[0].detail
    assert testing_space.purge_expired(unit.db, NOW)["volumes"] == 2
    assert not (roots.a / top["filename_stem"]).exists() and _volume_row(unit.db, top["id"]) is None


def _left_rows(db):
    from app.models.system_log import SystemLog
    db.expire_all()
    return [(r.level, r.detail) for r in db.query(SystemLog).filter(SystemLog.action == "testing_purge_file_left")]


def _testing_rows_gone(db, tables, volume_ids):
    volumes, records, images = _ids(tables, volume_ids)
    after = _tables(db)
    for name in _linked_tables():
        assert not any(_belongs(name, r, volumes, records, images) for r in after[name]), name


def _ordinary_twins(unit, volume, files):
    """The ordinary volume's directory with a file under each name the testing volume's rows use."""
    from app.models.record import Record, RecordImage
    real_dir = unit.root / unit.real_volume.filename_stem
    recs = [r for (r,) in unit.db.query(Record.id).filter(Record.collection_id == volume["id"]).all()]
    names = []
    for img in unit.db.query(RecordImage).filter(RecordImage.record_id.in_(recs)).all():
        twin = real_dir / "captures" / Path(img.file_path).name
        twin.write_bytes(b"ordinary twin")
        names.append(twin)
    manifest = real_dir / "metadata" / "manifest.jsonl"
    return real_dir, names, manifest


@pytest.mark.unit
@pytest.mark.parametrize("how", ["purge", "clear"])
def test_a_testing_directory_that_is_a_symlink_to_ordinary_work_is_not_followed(unit, clock, how):
    import os
    import shutil
    from app.core import testing_space
    project = unit.project()
    vol = unit.volume(project_id=project["id"])
    _capture(unit, project, vol["id"])
    tables = _tables(unit.db)
    real_dir, twins, manifest = _ordinary_twins(unit, vol, [])
    link = unit.root / vol["filename_stem"]
    shutil.rmtree(link)
    os.symlink(real_dir, link)
    before = {p: p.read_bytes() for p in real_dir.rglob("*") if p.is_file()}
    assert manifest in before
    if how == "purge":
        assert testing_space.purge_volume(unit.db, _volume_row(unit.db, vol["id"])) is True
    else:
        assert unit.post("admin", "/system/testing/clear").status_code == 200
    assert {p: p.read_bytes() for p in real_dir.rglob("*") if p.is_file()} == before
    assert link.is_symlink()
    assert _left_rows(unit.db)
    _testing_rows_gone(unit.db, tables, [vol["id"]])


@pytest.mark.unit
def test_a_testing_project_directory_that_is_a_symlink_is_not_followed_by_its_delete(unit, clock):
    import os
    from app.core.paths import project_dir_name
    from app.models.project import Project
    project = unit.project("Enlazada")
    ordinary = unit.root / "proyecto-ordinario"
    (ordinary / "metadata").mkdir(parents=True)
    (ordinary / "metadata" / "manifest.jsonl").write_text('{"ordinary": true}\n')
    link = unit.root / project_dir_name(unit.db.get(Project, project["id"]).name)
    os.symlink(ordinary, link)
    resp = unit.client.delete(f"/projects/{project['id']}", headers=unit.headers["testing"])
    assert resp.status_code == 200, resp.text
    assert (ordinary / "metadata" / "manifest.jsonl").read_text() == '{"ordinary": true}\n'
    assert link.is_symlink()
    assert _left_rows(unit.db)


@pytest.mark.unit
def test_a_symlink_in_a_middle_component_or_as_the_master_is_not_followed(unit, clock):
    import os
    import shutil
    from app.core import testing_space
    from app.models.record import Record, RecordImage
    project = unit.project()
    vol = unit.volume(project_id=project["id"])
    other = unit.volume(project_id=project["id"])
    _capture(unit, project, vol["id"])
    _capture(unit, project, other["id"], dual=False)
    tables = _tables(unit.db)
    real_dir = unit.root / unit.real_volume.filename_stem
    # The first volume's captures/ is a symlink to ordinary captures holding twins of its files.
    captures = unit.root / vol["filename_stem"] / "captures"
    recs = [r for (r,) in unit.db.query(Record.id).filter(Record.collection_id == vol["id"]).all()]
    twins = []
    for img in unit.db.query(RecordImage).filter(RecordImage.record_id.in_(recs)).all():
        twin = real_dir / "captures" / Path(img.file_path).name
        twin.write_bytes(b"ordinary twin")
        twins.append(twin)
    shutil.rmtree(captures)
    os.symlink(real_dir / "captures", captures)
    # The second volume's master is itself a symlink to an ordinary file.
    ordinary = unit.root / "ordinario.jpg"
    ordinary.write_bytes(b"ordinary file")
    rec2 = unit.db.query(Record.id).filter(Record.collection_id == other["id"]).first()[0]
    master = Path(unit.db.query(RecordImage).filter(RecordImage.record_id == rec2).first().file_path)
    master.unlink()
    os.symlink(ordinary, master)
    for v in (vol, other):
        assert testing_space.purge_volume(unit.db, _volume_row(unit.db, v["id"])) is True
    assert all(t.read_bytes() == b"ordinary twin" for t in twins)
    assert ordinary.read_bytes() == b"ordinary file"
    assert master.is_symlink()
    assert len(_left_rows(unit.db)) >= 2
    _testing_rows_gone(unit.db, tables, [vol["id"], other["id"]])


@pytest.mark.unit
def test_ordinary_files_in_a_testing_directory_and_a_stray_file_survive(unit, clock):
    from app.core import testing_space
    from app.models.record import Record, RecordImage
    project = unit.project()
    vol = unit.volume(project_id=project["id"])
    _capture(unit, project, vol["id"])
    tables = _tables(unit.db)
    base = unit.root / vol["filename_stem"]
    master = base / "captures" / "ordinario-img_0001_c0.jpg"
    raw_master = base / "captures" / "ordinario-img_0002_c0.cr2"
    written = {
        master: b"ordinary master",
        master.with_suffix(".raw"): b"ordinary raw buffer",
        raw_master: b"ordinary cr2",
        base / "previews" / "ordinario-img_0002_c0_preview.jpg": b"ordinary preview",
        base / "thumbnails" / "ordinario_thumb.jpg": b"ordinary thumb",
        base / "captures" / "desconocido.bin": b"stray",
    }
    for path, data in written.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    rec = Record(title="Ordinario", collection_id=unit.real_volume.id, capture_mode="single")
    unit.db.add(rec)
    unit.db.commit()
    for m, thumb in ((master, base / "thumbnails" / "ordinario_thumb.jpg"), (raw_master, None)):
        unit.db.add(RecordImage(record_id=rec.id, filename=m.name, file_path=str(m),
                                thumbnail_path=str(thumb) if thumb else None, format="JPG", is_current=True))
    unit.db.commit()
    assert testing_space.purge_volume(unit.db, _volume_row(unit.db, vol["id"])) is True
    assert all(p.read_bytes() == d for p, d in written.items())
    assert base.is_dir()
    [(level, detail)] = _left_rows(unit.db)
    assert level == "WARN" and str(base) in detail
    _testing_rows_gone(unit.db, tables, [vol["id"]])


@pytest.mark.unit
def test_clear_leaves_an_unmapped_directory_in_the_project_namespace_byte_for_byte(unit, clock, roots):
    archive = roots.b / (TP + "archive")
    roots.use_b()
    for rel, data in (("metadata/manifest.jsonl", b'{"old": 1}\n'), ("images/main/a.jpg", b"old a"),
                      ("notes.txt", b"old notes")):
        path = archive / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    before = {str(p.relative_to(archive)): p.read_bytes() for p in archive.rglob("*") if p.is_file()}
    project, top, sub = _tree(unit)
    assert unit.post("admin", "/system/testing/clear").status_code == 200
    assert {str(p.relative_to(archive)): p.read_bytes() for p in archive.rglob("*") if p.is_file()} == before


@pytest.mark.unit
def test_a_testing_image_outside_every_testing_directory_is_left_and_the_tree_removed(unit, clock):
    from app.core import testing_space
    from app.models.record import Record, RecordImage
    project = unit.project()
    vol = unit.volume(project_id=project["id"])
    rec = _capture(unit, project, vol["id"])
    tables = _tables(unit.db)
    outside = unit.root / "fuera" / "fuera.jpg"
    outside.parent.mkdir()
    outside.write_bytes(b"outside")
    img = unit.db.query(RecordImage).filter(RecordImage.record_id == rec).first()
    img.file_path = str(outside)
    unit.db.commit()
    assert testing_space.purge_volume(unit.db, _volume_row(unit.db, vol["id"])) is True
    assert outside.read_bytes() == b"outside"
    assert any(str(outside) in d for _, d in _left_rows(unit.db))
    _testing_rows_gone(unit.db, tables, [vol["id"]])


@pytest.mark.unit
def test_every_companion_of_a_testing_capture_is_gone_after_a_purge(unit, clock):
    """The enumeration is complete: dual, single, RAW (preview sidecar) and upload, each with its manifest line."""
    from app.core import testing_space
    from app.models.record import RecordImage
    project = unit.project()
    top = unit.volume(project_id=project["id"])
    sub = unit.volume(parent_id=top["id"])
    _capture(unit, project, top["id"])
    rec = _capture(unit, project, sub["id"], dual=False)
    unit.backend.raw = True
    _capture(unit, project, top["id"], dual=False)
    unit.backend.raw = False
    resp = unit.client.post(f"/records/{rec}/images", headers=unit.headers["testing"],
                            files={"file": ("p.jpg", b"\xff\xd8up", "image/jpeg")})
    assert resp.status_code == 200, resp.text
    # A multi-format capture's raw buffer beside a JPEG master (Picamera2Backend writes it there).
    jpeg = next(Path(i.file_path) for i in unit.db.query(RecordImage).all()
                if top["filename_stem"] in i.file_path and i.file_path.endswith(".jpg"))
    jpeg.with_suffix(".raw").write_bytes(b"raw buffer")
    stems = (top["filename_stem"], sub["filename_stem"])
    assert any((unit.root / stems[0] / "previews").iterdir())
    assert testing_space.purge_volume(unit.db, _volume_row(unit.db, top["id"])) is True
    for stem in stems:
        assert not os.path.lexists(unit.root / stem), sorted(str(p) for p in (unit.root / stem).rglob("*"))
    assert _left_rows(unit.db) == []


@pytest.mark.unit
def test_deleting_a_testing_project_removes_its_manifest_and_an_empty_directory(unit, clock):
    from app.core.paths import project_dir_name
    from app.models.project import Project
    gone = unit.project("Vacía")
    kept = unit.project("Con suelto")
    dirs = {}
    for p in (gone, kept):
        d = unit.root / project_dir_name(unit.db.get(Project, p["id"]).name)
        (d / "metadata").mkdir(parents=True)
        (d / "metadata" / "manifest.jsonl").write_text("{}\n")
        dirs[p["id"]] = d
    stray = dirs[kept["id"]] / "suelto.txt"
    stray.write_text("stray")
    for p in (gone, kept):
        resp = unit.client.delete(f"/projects/{p['id']}", headers=unit.headers["testing"])
        assert resp.status_code == 200, resp.text
    assert not dirs[gone["id"]].exists()
    assert stray.read_text() == "stray" and not (dirs[kept["id"]] / "metadata").exists()
    assert any(str(dirs[kept["id"]]) in d for _, d in _left_rows(unit.db))


# ---------------------------------------------------------------------------
# One rule for every testing deletion
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("route", ["record", "image"])
def test_a_testing_rows_delete_never_unlinks_an_ordinary_file(unit, clock, route):
    from app.models.record import Record, RecordImage
    project = unit.project()
    vol = unit.volume(project_id=project["id"])
    rec = _capture(unit, project, vol["id"], dual=False)
    ordinary = Path(unit.db.query(RecordImage).filter(RecordImage.record_id == unit.real_record_id)
                    .first().file_path)
    data = ordinary.read_bytes()
    img = unit.db.query(RecordImage).filter(RecordImage.record_id == rec).first()
    img_id = img.id
    img.file_path = str(ordinary)
    unit.db.commit()
    path = f"/records/{rec}" if route == "record" else f"/records/images/{img_id}"
    resp = unit.client.delete(path, headers=unit.headers["testing"])
    assert resp.status_code == 200, resp.text
    assert ordinary.read_bytes() == data
    unit.db.expire_all()
    assert unit.db.get(RecordImage, img_id) is None
    if route == "record":
        assert unit.db.get(Record, rec) is None
    left = _left_rows(unit.db)
    assert len(left) == 1 and str(ordinary) in left[0][1]


@pytest.mark.unit
@pytest.mark.parametrize("route", ["record", "image"])
def test_an_ordinary_rows_delete_never_reaches_the_testing_check(unit, clock, monkeypatch, route):
    from app.core import testing_space
    from app.models.record import RecordImage

    def refuse(*args, **kwargs):
        raise AssertionError("an ordinary delete reached the testing check")

    monkeypatch.setattr(testing_space, "_testing_place", refuse)
    img = unit.db.query(RecordImage).filter(RecordImage.record_id == unit.real_record_id).first()
    master = Path(img.file_path)
    path = f"/records/{unit.real_record_id}" if route == "record" else f"/records/images/{img.id}"
    resp = unit.client.delete(path, headers=unit.headers["operator"])
    assert resp.status_code == 200, resp.text
    assert not master.exists()
