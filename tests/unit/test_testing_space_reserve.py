"""The reserve for real work (NEH-391): testing captures stop before the storage is full.

`unit_settings.testing_reserve_gb` (1 to 10000, default 20) is the free
space, in GB of 10^9 bytes, kept for real work on the file system that holds
the active projects root. While free space is below it, every route that
writes files for testing material refuses the testing account with a 507
`testing_reserve`, before any camera is touched, any number taken or any
file written: the single and dual captures, the image upload, and volume
creation. Free space that cannot be read refuses too. The test capture
writes only to a temporary directory it removes, so it is not refused. Real
work is never refused by the reserve.

GET /system/testing reports `reserve_gb` and `free_gb`; PUT /system/testing
takes `reserve_gb` as it takes `expiry_days`.

    python -m pytest tests/unit/test_testing_space_reserve.py

Version: 0.3.0
"""

import shutil
from collections import namedtuple

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from tests.unit.test_capture_manifest_export import _as
from tests.unit.test_testing_space_capture import (  # noqa: F401  (backend, lookups and unit are fixtures)
    TP, backend, lookups, unit,
)
from tests.unit.test_testing_space_schema import (  # noqa: F401  (migration_root is a fixture)
    _before, _load_migration, _load_migration_from_package, _run_migration, migration_root,
)

GB = 10 ** 9
PASSWORD = "pw-for-tests-91"
Usage = namedtuple("Usage", "total used free")


class Disk:
    """shutil.disk_usage stood in for: the free bytes it answers, or OSError, and the paths it was asked about."""

    def __init__(self, free=100 * GB):
        self.free = free
        self.asked = []

    def __call__(self, path):
        self.asked.append(str(path))
        if self.free is None:
            raise OSError("unreadable")
        return Usage(total=10 ** 13, used=10 ** 13 - self.free, free=self.free)


@pytest.fixture
def disk(monkeypatch):
    d = Disk()
    monkeypatch.setattr(shutil, "disk_usage", d)
    return d


def _settings_row(db):
    from app.models.unit_settings import UnitSettings
    db.expire_all()
    return db.get(UnitSettings, 1)


def _set_reserve(db, gb):
    row = _settings_row(db)
    row.testing_reserve_gb = gb
    db.commit()


def _refused(resp, free_gb, reserve_gb):
    assert resp.status_code == 507, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "testing_reserve"
    assert detail["free_gb"] == free_gb and detail["reserve_gb"] == reserve_gb
    # The message names no figure.
    assert detail["message"] and not any(ch.isdigit() for ch in detail["message"])
    return detail


# ---------------------------------------------------------------------------
# The column
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_migration_adds_the_reserve_with_its_default_and_bounds(backend_root, tmp_path, migration_root):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    mod = _load_migration(backend_root)
    try:
        _before(engine)
        _run_migration(engine, mod.upgrade)
        column = {c["name"]: c for c in sa.inspect(engine).get_columns("unit_settings")}["testing_reserve_gb"]
        assert column["nullable"] is False
        with engine.begin() as conn:
            assert conn.execute(sa.text("SELECT testing_reserve_gb FROM unit_settings")).scalar() == 20
        for value in (0, 10001):
            with pytest.raises(IntegrityError):
                with engine.begin() as conn:
                    conn.execute(sa.text(f"UPDATE unit_settings SET testing_reserve_gb = {value}"))
        for value in (1, 10000):
            with engine.begin() as conn:
                conn.execute(sa.text(f"UPDATE unit_settings SET testing_reserve_gb = {value}"))
        _run_migration(engine, mod.downgrade)
        assert "testing_reserve_gb" not in {c["name"] for c in sa.inspect(engine).get_columns("unit_settings")}
    finally:
        engine.dispose()


@pytest.mark.unit
def test_the_model_matches_the_migration(db_session):
    from app.core.db import Base
    from app.models.unit_settings import UnitSettings
    column = Base.metadata.tables["unit_settings"].c.testing_reserve_gb
    assert column.nullable is False and str(column.server_default.arg) == "20"
    checks = {c.name: str(c.sqltext) for c in Base.metadata.tables["unit_settings"].constraints
              if isinstance(c, sa.CheckConstraint)}
    mod = _load_migration_from_package()
    assert checks["check_unit_settings_testing_reserve_gb"] == mod.RESERVE_CHECK
    row = UnitSettings(id=1, mode="offline")
    db_session.add(row)
    db_session.commit()
    assert _settings_row(db_session).testing_reserve_gb == 20
    row.testing_reserve_gb = 10001
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------------
# The admin routes
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_get_reports_the_reserve_and_the_free_space_on_the_active_root(client, db_session, disk):
    from app.core.config import settings
    disk.free = 12_345_678_901
    resp = _as(client, "admin").get("/system/testing")
    assert resp.status_code == 200, resp.text
    assert resp.json()["reserve_gb"] == 20 and resp.json()["free_gb"] == 12.3
    assert disk.asked and set(disk.asked) == {str(settings.projects_dir)}
    # The same reading as GET /system/storage.
    storage = _as(client, "admin").get("/system/storage").json()
    assert storage["free_bytes"] == disk.free


@pytest.mark.unit
def test_get_reports_no_free_space_when_it_cannot_be_read(client, db_session, disk):
    disk.free = None
    resp = _as(client, "admin").get("/system/testing")
    assert resp.status_code == 200, resp.text
    assert resp.json()["free_gb"] is None


@pytest.mark.unit
@pytest.mark.parametrize("enabled", [True, False], ids=["switching-on", "space-off"])
@pytest.mark.parametrize("gb,status", [
    (0, 422), (1, 200), (10000, 200), (10001, 422), (1.5, 422), ("20", 422), (True, 422), (None, 422),
], ids=["0", "1", "10000", "10001", "float", "string", "bool", "null"])
def test_reserve_gb_is_an_integer_from_1_to_10000(client, db_session, disk, enabled, gb, status):
    body = {"enabled": enabled, "reserve_gb": gb}
    if enabled:
        body.update(username="pruebas", password=PASSWORD)
    resp = _as(client, "admin").put("/system/testing", json=body)
    assert resp.status_code == status, resp.text
    row = _settings_row(db_session)
    if status == 200:
        assert resp.json()["reserve_gb"] == gb and row.testing_reserve_gb == gb
    else:
        assert row is None or row.testing_reserve_gb == 20


@pytest.mark.unit
def test_reserve_gb_absent_leaves_the_stored_value_and_a_change_is_logged(client, db_session, disk):
    from app.models.system_log import SystemLog
    assert _as(client, "admin").put("/system/testing", json={"enabled": False, "reserve_gb": 55}).status_code == 200
    assert _as(client, "admin").put("/system/testing", json={"enabled": False, "expiry_days": 9}).status_code == 200
    assert _settings_row(db_session).testing_reserve_gb == 55
    details = [r.detail for r in db_session.query(SystemLog).filter(SystemLog.action == "testing_space_changed")]
    assert any("reserve_gb 20 -> 55" in d for d in details), details


@pytest.mark.unit
def test_the_reserve_bounds_have_one_definition():
    from app.api.system import TestingUpdate
    from app.core import testing_space
    meta = TestingUpdate.model_fields["reserve_gb"].metadata
    ge = [m.ge for m in meta if getattr(m, "ge", None) is not None]
    le = [m.le for m in meta if getattr(m, "le", None) is not None]
    assert (ge, le) == ([testing_space.MIN_RESERVE_GB], [testing_space.MAX_RESERVE_GB]) == ([1], [10000])


# ---------------------------------------------------------------------------
# The routes that write files for testing material
# ---------------------------------------------------------------------------

def _testing_volume(unit):
    project = unit.project()
    return project, unit.volume(project_id=project["id"])


def _testing_record(unit, volume):
    resp = unit.post("testing", "/records/", {"title": "Hoja", "capture_mode": "single",
                                              "collection_id": volume["id"]})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _route(unit, name, project, volume, record_id):
    if name == "capture":
        return unit.post("testing", "/cameras/capture", {"project_name": project["name"],
                                                         "collection_id": volume["id"], "camera_index": 0})
    if name == "dual":
        return unit.post("testing", "/cameras/capture/dual", {"project_name": project["name"],
                                                              "collection_id": volume["id"]})
    if name == "upload":
        return unit.client.post(f"/records/{record_id}/images", headers=unit.headers["testing"],
                                files={"file": ("p.jpg", b"\xff\xd8upload", "image/jpeg")})
    if name == "volume":
        return unit.post("testing", "/collections/", {"name": "Otro", "project_id": project["id"]})
    raise AssertionError(name)


def _ok(resp, name):
    if name == "volume":
        assert resp.status_code == 201, resp.text
    else:
        assert resp.status_code == 200, resp.text
        if name != "upload":
            assert resp.json()["success"], resp.text


ROUTES = ["capture", "dual", "upload", "volume"]


@pytest.mark.unit
@pytest.mark.parametrize("name", ROUTES)
def test_below_the_reserve_a_testing_write_is_refused_before_anything_is_touched(unit, disk, name):
    project, volume = _testing_volume(unit)
    record_id = _testing_record(unit, volume)
    _set_reserve(unit.db, 30)
    disk.free = 30 * GB - 1
    requested = list(unit.backend.requested)
    before = unit.snapshot()
    # Rounded down: what is shown never claims more than there is.
    _refused(_route(unit, name, project, volume, record_id), 29.9, 30)
    assert unit.backend.requested == requested
    assert unit.snapshot() == before


@pytest.mark.unit
@pytest.mark.parametrize("name", ROUTES)
@pytest.mark.parametrize("free", [30 * GB, 30 * GB + 1, 500 * GB], ids=["at", "just-above", "above"])
def test_at_or_above_the_reserve_a_testing_write_goes_ahead(unit, disk, name, free):
    project, volume = _testing_volume(unit)
    record_id = _testing_record(unit, volume)
    _set_reserve(unit.db, 30)
    disk.free = free
    _ok(_route(unit, name, project, volume, record_id), name)


@pytest.mark.unit
@pytest.mark.parametrize("name", ROUTES)
def test_free_space_that_cannot_be_read_refuses_a_testing_write(unit, disk, name):
    project, volume = _testing_volume(unit)
    record_id = _testing_record(unit, volume)
    disk.free = None
    before = unit.snapshot()
    _refused(_route(unit, name, project, volume, record_id), None, 20)
    assert unit.snapshot() == before


@pytest.mark.unit
@pytest.mark.parametrize("free", [0, None], ids=["full", "unreadable"])
def test_real_work_is_never_refused_by_the_reserve(unit, disk, free, monkeypatch):
    # Real work has a reserve of its own (CAPTUA_STORAGE_RESERVE_GB), set to none here.
    from app.core.config import settings
    monkeypatch.setattr(settings, "STORAGE_RESERVE_GB", 0)
    disk.free = free
    for path, body in (("/cameras/capture/dual", {"project_name": unit.real_project.name,
                                                  "collection_id": unit.real_volume.id}),
                       ("/cameras/capture", {"project_name": unit.real_project.name,
                                             "collection_id": unit.real_volume.id, "camera_index": 0})):
        for caller in ("operator", "admin"):
            resp = unit.post(caller, path, body)
            assert resp.status_code == 200 and resp.json()["success"], resp.text
    resp = unit.client.post(f"/records/{unit.real_record_id}/images", headers=unit.headers["operator"],
                            files={"file": ("p.jpg", b"\xff\xd8upload", "image/jpeg")})
    assert resp.status_code == 200, resp.text
    resp = unit.post("admin", "/collections/", {"name": "Real 2", "project_id": unit.real_project.id,
                                                "filename_stem": "xx-real-2"})
    assert resp.status_code == 201, resp.text


@pytest.mark.unit
def test_the_test_capture_is_not_refused(unit, disk):
    disk.free = 0
    resp = unit.client.post("/cameras/test-capture/0", headers=unit.headers["testing"])
    assert resp.status_code == 200, resp.text


@pytest.mark.unit
def test_the_function_reads_the_stored_reserve(db_session, disk):
    from fastapi import HTTPException
    from app.core import testing_space
    from tests.unit.test_unit_mode import _unit
    _unit(db_session, "offline")
    _set_reserve(db_session, 5)
    disk.free = 5 * GB
    testing_space.refuse_below_reserve(db_session)
    disk.free = 5 * GB - 1
    with pytest.raises(HTTPException) as refused:
        testing_space.refuse_below_reserve(db_session)
    assert refused.value.status_code == 507 and refused.value.detail["reserve_gb"] == 5


@pytest.mark.unit
def test_free_gb_is_rounded_down_so_the_shown_figure_agrees_with_the_refusal(client, db_session, disk):
    from fastapi import HTTPException
    from app.core import testing_space
    from tests.unit.test_unit_mode import _unit
    _unit(db_session, "offline")
    disk.free = 20 * GB - 1
    assert _as(client, "admin").get("/system/testing").json()["free_gb"] == 19.9
    with pytest.raises(HTTPException) as refused:
        testing_space.refuse_below_reserve(db_session)
    assert refused.value.detail["free_gb"] == 19.9 and refused.value.detail["reserve_gb"] == 20
    disk.free = 20 * GB
    assert _as(client, "admin").get("/system/testing").json()["free_gb"] == 20.0
    testing_space.refuse_below_reserve(db_session)
