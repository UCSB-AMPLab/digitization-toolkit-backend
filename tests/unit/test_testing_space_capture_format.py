"""The capture format of a testing project (NEH-391).

A testing project carries a capture format, `projects.capture_format`:
"JPEG" unless the testing account chooses "RAW+JPEG". An ordinary project
carries none. Every capture into a testing volume takes its project's
format as the capture's own, on every backend, and leaves the format the
unit stores for the camera body as it was.

A. The column: the CHECK in the model and the migration.
B. The project routes: the default, a choice, the old name RAW, a format
   the cameras do not offer (`capture_format_unavailable`), a format sent
   for an ordinary project (`capture_format_not_testing`), and the read.
C. The capture routes, with a camera stood in for that stores a format per
   body (as CHDK does): a testing capture takes the project's format, an
   ordinary one the stored format.
D. A gphoto2 DSLR, through the routes, against tests/unit/test_gphoto2_stored_format.py's
   fake libgphoto2: the widget writes, the files and cameras.json.

    python -m pytest tests/unit/test_testing_space_capture_format.py

Version: 0.3.3
"""

import types
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

import capture.project_manager as project_manager_module
import capture.service as capture_service
from tests.unit.test_capture_contract_naming import _FakeBackend
from tests.unit.test_gphoto2_stored_format import (  # noqa: F401  (rig and registry are fixtures)
    HW_A, PORT_A, SERIAL_A, _store, registry, rig,
)

T = "captua-testing-"
TP = T + "-"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _FormatBackend(_FakeBackend):
    """A camera that stores a format per body, as CHDK does, and records each capture's configuration."""

    resolves_capture_format = False

    def __init__(self, stored=None, offered=("JPEG", "RAW+JPEG")):
        super().__init__()
        self.stored = stored
        self.offered = list(offered)
        self.configs = []

    def capture_image_format(self, camera_index):
        return self.stored

    def get_capabilities(self):
        return {"dslr_settings": False, "image_formats": list(self.offered)}

    def get_backend_name(self):
        return "fake"

    def capture_image(self, output_path, camera_config, capture_output=False):
        self.configs.append(camera_config)
        return super().capture_image(output_path, camera_config, capture_output)


def _set_unit(db):
    from app.models.unit_settings import UnitSettings
    row = db.get(UnitSettings, 1)
    if row is None:
        db.add(UnitSettings(id=1, mode="offline", projects_enabled=True))
    else:
        row.mode, row.projects_enabled = "offline", True
    db.commit()


class Unit:
    """One user per role, a real project and volume, and the testing account's routes."""

    def __init__(self, db, client):
        from app.core.security import create_access_token
        from app.models.collection import Collection
        from app.models.project import Project
        from app.models.user import User

        _set_unit(db)
        self.db, self.client, self.headers = db, client, {}
        for role in ("testing", "operator", "reviewer", "admin"):
            user = User(username=f"u-{role}", hashed_password="x", role=role, is_active=True)
            db.add(user)
            db.commit()
            db.refresh(user)
            self.headers[role] = {"Authorization": f"Bearer {create_access_token(str(user.id))}"}
        self.real_project = Project(name="Proyecto real")
        db.add(self.real_project)
        db.commit()
        self.real_volume = Collection(name="Tomo real", project_id=self.real_project.id, filename_stem="xx-real-1")
        db.add(self.real_volume)
        db.commit()
        db.refresh(self.real_project)
        db.refresh(self.real_volume)

    def post(self, caller, path, body=None):
        return self.client.post(path, headers=self.headers[caller], json=body)

    def put(self, caller, path, body=None):
        return self.client.put(path, headers=self.headers[caller], json=body)

    def get(self, caller, path):
        return self.client.get(path, headers=self.headers[caller])

    def project(self, name="Pruebas", **extra):
        resp = self.post("testing", "/projects/", {"name": name, **extra})
        assert resp.status_code == 200, resp.text
        return resp.json()

    def volume(self, project_id=None, parent_id=None, name="Tomo de prueba"):
        body = {"name": name}
        if project_id is not None:
            body["project_id"] = project_id
        if parent_id is not None:
            body["parent_collection_id"] = parent_id
        resp = self.post("testing", "/collections/", body)
        assert resp.status_code == 201, resp.text
        return resp.json()

    def capture(self, caller, project_name, volume_id, dual=False):
        path = "/cameras/capture/dual" if dual else "/cameras/capture"
        body = {"project_name": project_name, "collection_id": volume_id}
        if not dual:
            body["camera_index"] = 0
        resp = self.post(caller, path, body)
        assert resp.status_code == 200 and resp.json()["success"], resp.text
        return resp.json()

    def stored_project(self, project_id):
        from app.models.project import Project
        self.db.expire_all()
        return self.db.get(Project, project_id)

    def project_rows(self):
        self.db.expire_all()
        return sorted(tuple(r) for r in self.db.execute(sa.text("SELECT * FROM projects")).all())


def _install(monkeypatch, backend):
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    monkeypatch.setattr(capture_service, "_backend", backend)
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    monkeypatch.setattr(
        project_manager_module, "default_camera_config_from_registry",
        lambda camera_index, resolution="high", registry=None: (
            {"camera_index": camera_index, "rotate_deg": 0}, "hw"))


@pytest.fixture
def camera(monkeypatch):
    b = _FormatBackend(stored="RAW+JPEG")
    _install(monkeypatch, b)
    return b


@pytest.fixture
def unit(client, db_session, override_projects_root, camera):
    return Unit(db_session, client)


def _code(resp, status, code):
    assert resp.status_code == status, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == code, resp.text
    assert detail["message"], resp.text


# ---------------------------------------------------------------------------
# A. The column
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_model_check_is_the_migrations():
    from app.core.db import Base
    import app.models.project  # noqa: F401
    from tests.unit.test_testing_space_schema import _load_migration_from_package

    mod = _load_migration_from_package()
    table = Base.metadata.tables["projects"]
    assert table.c.capture_format.nullable is True
    checks = {c.name: str(c.sqltext) for c in table.constraints if isinstance(c, sa.CheckConstraint)}
    assert checks["check_project_capture_format"] == mod.CAPTURE_FORMAT_CHECK
    assert mod.CAPTURE_FORMAT_CHECK == (
        "(testing AND capture_format IS NOT NULL AND capture_format IN ('JPEG','RAW+JPEG')) OR (NOT testing AND capture_format IS NULL)")


@pytest.mark.unit
def test_a_testing_project_made_through_the_model_takes_jpeg_and_a_real_one_none(db_session):
    from app.models.project import Project
    testing = Project(name=TP + "Pruebas", testing=True)
    real = Project(name="Real")
    db_session.add_all([testing, real])
    db_session.commit()
    db_session.refresh(testing)
    db_session.refresh(real)
    assert (testing.capture_format, real.capture_format) == ("JPEG", None)


@pytest.mark.unit
@pytest.mark.parametrize("sql", [
    "INSERT INTO projects (id, name, created_at, testing, capture_format) VALUES (50, 'r', '2026-01-01', FALSE, 'JPEG')",
    "INSERT INTO projects (id, name, created_at, testing, capture_format) VALUES (50, 'x', '2026-01-01', TRUE, NULL)",
    "INSERT INTO projects (id, name, created_at, testing, capture_format) VALUES (50, 'x', '2026-01-01', TRUE, 'RAW')",
], ids=["real-with-format", "testing-without", "testing-old-name"])
def test_the_database_refuses_a_format_outside_the_rule(db_session, sql):
    with pytest.raises(IntegrityError):
        db_session.execute(sa.text(sql))
        db_session.flush()
    db_session.rollback()


@pytest.mark.unit
def test_the_migration_adds_the_column_and_its_check_and_drops_them(backend_root, tmp_path, monkeypatch):
    from app.core import config, storage_override
    from tests.unit.test_testing_space_schema import _before, _load_migration, _run_migration

    monkeypatch.setattr(config.settings, "PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setattr(storage_override, "get_storage_override_or_fallback", lambda: (None, False))
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    try:
        mod = _load_migration(backend_root)
        _before(engine)
        _run_migration(engine, mod.upgrade)
        column = {c["name"]: c for c in sa.inspect(engine).get_columns("projects")}["capture_format"]
        assert column["nullable"] is True
        with engine.begin() as conn:
            assert conn.execute(sa.text("SELECT capture_format FROM projects WHERE id = 1")).scalar() is None
            conn.execute(sa.text(
                "INSERT INTO projects (id, name, testing, capture_format) VALUES (2, 't', TRUE, 'RAW+JPEG')"))
        for sql in ("INSERT INTO projects (id, name, testing) VALUES (3, 'u', TRUE)",
                    "UPDATE projects SET capture_format = 'JPEG' WHERE id = 1"):
            with pytest.raises(IntegrityError):
                with engine.begin() as conn:
                    conn.execute(sa.text(sql))
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM projects WHERE id = 2"))
        _run_migration(engine, mod.downgrade)
        assert "capture_format" not in {c["name"] for c in sa.inspect(engine).get_columns("projects")}
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# B. The project routes
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_testing_project_is_jpeg_unless_chosen(unit):
    project = unit.project()
    assert project["capture_format"] == "JPEG"
    assert unit.stored_project(project["id"]).capture_format == "JPEG"
    assert unit.project("Otra", capture_format=None)["capture_format"] == "JPEG"


@pytest.mark.unit
@pytest.mark.parametrize("sent", ["RAW+JPEG", "RAW"])
def test_the_testing_account_chooses_raw_plus_jpeg(unit, sent):
    project = unit.project(capture_format=sent)
    assert project["capture_format"] == "RAW+JPEG"
    assert unit.stored_project(project["id"]).capture_format == "RAW+JPEG"
    assert unit.get("testing", f"/projects/{project['id']}").json()["capture_format"] == "RAW+JPEG"


@pytest.mark.unit
@pytest.mark.parametrize("offered", [[], ["JPEG"]])
def test_a_format_the_cameras_do_not_offer_is_refused_and_nothing_is_written(unit, camera, offered):
    camera.offered = offered
    before = unit.project_rows()
    _code(unit.post("testing", "/projects/", {"name": "Pruebas", "capture_format": "RAW+JPEG"}),
          422, "capture_format_unavailable")
    assert unit.project_rows() == before
    assert unit.project(capture_format="JPEG")["capture_format"] == "JPEG"


@pytest.mark.unit
def test_with_capabilities_that_cannot_be_read_only_jpeg_is_taken(unit, monkeypatch):
    def unavailable():
        raise RuntimeError("no camera backend")
    monkeypatch.setattr(capture_service, "get_backend", unavailable)
    before = unit.project_rows()
    _code(unit.post("testing", "/projects/", {"name": "Pruebas", "capture_format": "RAW+JPEG"}),
          422, "capture_format_unavailable")
    assert unit.project_rows() == before
    assert unit.project()["capture_format"] == "JPEG"
    assert unit.project("Otra", capture_format="JPEG")["capture_format"] == "JPEG"


@pytest.mark.unit
@pytest.mark.parametrize("sent", ["TIFF", "jpeg", ""])
def test_a_format_that_names_none_is_refused(unit, sent):
    _code(unit.post("testing", "/projects/", {"name": "Pruebas", "capture_format": sent}),
          422, "capture_format_unavailable")


@pytest.mark.unit
@pytest.mark.parametrize("sent", ["JPEG", "RAW+JPEG", "TIFF"])
def test_an_admin_creates_no_project_with_a_format(unit, sent):
    before = unit.project_rows()
    _code(unit.post("admin", "/projects/", {"name": "Real nuevo", "capture_format": sent}),
          422, "capture_format_not_testing")
    assert unit.project_rows() == before
    resp = unit.post("admin", "/projects/", {"name": "Real nuevo", "capture_format": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["capture_format"] is None
    assert unit.stored_project(resp.json()["id"]).capture_format is None


@pytest.mark.unit
def test_the_testing_account_changes_its_projects_format(unit, camera):
    project = unit.project()
    resp = unit.put("testing", f"/projects/{project['id']}", {"capture_format": "RAW"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["capture_format"] == "RAW+JPEG"
    assert unit.stored_project(project["id"]).capture_format == "RAW+JPEG"

    # Left out or null: the format stays.
    resp = unit.put("testing", f"/projects/{project['id']}", {"description": "d", "capture_format": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["capture_format"] == "RAW+JPEG"

    resp = unit.put("testing", f"/projects/{project['id']}", {"name": "Renombrado", "capture_format": "JPEG"})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["name"], resp.json()["capture_format"]) == ("Renombrado", "JPEG")

    camera.offered = ["JPEG"]
    before = unit.project_rows()
    _code(unit.put("testing", f"/projects/{project['id']}", {"name": "Otro", "capture_format": "RAW+JPEG"}),
          422, "capture_format_unavailable")
    assert unit.project_rows() == before


@pytest.mark.unit
@pytest.mark.parametrize("caller", ["admin", "operator"])
@pytest.mark.parametrize("sent", ["JPEG", "RAW+JPEG"])
def test_a_format_for_an_ordinary_project_is_refused_and_nothing_is_written(unit, override_projects_root,
                                                                            caller, sent):
    before = unit.project_rows()
    files = sorted(str(p) for p in override_projects_root.rglob("*"))
    _code(unit.put(caller, f"/projects/{unit.real_project.id}", {"name": "Otro nombre", "capture_format": sent}),
          422, "capture_format_not_testing")
    assert unit.project_rows() == before
    assert sorted(str(p) for p in override_projects_root.rglob("*")) == files
    resp = unit.put(caller, f"/projects/{unit.real_project.id}", {"description": "d", "capture_format": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["capture_format"] is None


@pytest.mark.unit
def test_the_format_is_read_wherever_the_project_is(unit):
    project = unit.project(capture_format="RAW+JPEG")
    [listed] = unit.get("testing", "/projects/").json()
    assert listed["capture_format"] == "RAW+JPEG"
    admin = unit.get("admin", f"/projects/{project['id']}")
    assert admin.status_code == 200 and admin.json()["capture_format"] == "RAW+JPEG"
    [real] = unit.get("operator", "/projects/").json()
    assert real["capture_format"] is None


# ---------------------------------------------------------------------------
# C. The capture routes
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("project_format", ["JPEG", "RAW+JPEG"])
@pytest.mark.parametrize("stored", ["RAW+JPEG", "JPEG", None])
def test_a_testing_capture_takes_its_projects_format(unit, camera, project_format, stored):
    camera.stored = stored
    project = unit.project(capture_format=project_format)
    volume = unit.volume(project_id=project["id"])

    unit.capture("testing", project["name"], volume["id"])
    unit.capture("testing", project["name"], volume["id"], dual=True)

    assert [c.image_format for c in camera.configs] == [project_format] * 3


@pytest.mark.unit
def test_a_testing_sub_volume_takes_the_format_of_its_volumes_project(unit, camera):
    camera.stored = "JPEG"
    project = unit.project(capture_format="RAW+JPEG")
    top = unit.volume(project_id=project["id"])
    sub = unit.volume(parent_id=top["id"], name="Cuadernillo")

    unit.capture("testing", project["name"], sub["id"])

    assert [c.image_format for c in camera.configs] == ["RAW+JPEG"]


@pytest.mark.unit
def test_a_retake_in_a_testing_volume_takes_its_projects_format(unit, camera):
    project = unit.project(capture_format="JPEG")
    volume = unit.volume(project_id=project["id"])
    first = unit.capture("testing", project["name"], volume["id"])
    resp = unit.post("testing", "/cameras/capture", {"project_name": project["name"], "collection_id": volume["id"],
                                                     "camera_index": 0, "record_id": first["record_id"]})
    assert resp.status_code == 200 and resp.json()["success"], resp.text
    assert [c.image_format for c in camera.configs] == ["JPEG", "JPEG"]


@pytest.mark.unit
def test_a_testing_volume_whose_project_cannot_be_found_captures_jpeg(unit, camera, monkeypatch):
    from app.core import storage_ops
    project = unit.project(capture_format="RAW+JPEG")
    volume = unit.volume(project_id=project["id"])
    monkeypatch.setattr(storage_ops, "resolve_project", lambda db, collection: None)
    from app.api import cameras
    assert cameras._testing_capture_format(unit.db, volume["id"]) == "JPEG"


@pytest.mark.unit
def test_an_ordinary_capture_takes_the_stored_format(unit, camera):
    unit.project(capture_format="JPEG")
    unit.capture("operator", unit.real_project.name, unit.real_volume.id)
    camera.stored = None
    unit.capture("operator", unit.real_project.name, unit.real_volume.id, dual=True)
    assert [c.image_format for c in camera.configs] == ["RAW+JPEG", None, None]


# ---------------------------------------------------------------------------
# D. A gphoto2 DSLR through the routes
# ---------------------------------------------------------------------------

@pytest.fixture
def dslr(rig, registry, monkeypatch):
    """A gphoto2 backend on the fake libgphoto2; every call of the adoption path is recorded with what the camera held."""
    import logging

    import capture.backends.gphoto2_backend as gb
    from capture.camera_registry import CameraRegistry

    log, adoptions = [], []
    backend = gb.GPhoto2Backend(logging.getLogger(DSLR_LOGGER))
    backend._registry_factory = lambda: CameraRegistry(registry_path=registry.registry_path)
    backend._system_log = lambda **row: log.append(row)
    adopt = backend._adopt_format

    def recorded_adoption(camera_index, session, hardware_id):
        adoptions.append(getattr(session, "opened_imageformat", None))
        return adopt(camera_index, session, hardware_id)
    monkeypatch.setattr(backend, "_adopt_format", recorded_adoption)
    _install(monkeypatch, backend)
    yield types.SimpleNamespace(backend=backend, log=log, adoptions=adoptions)
    backend.cleanup()


DSLR_LOGGER = "test-testing-capture-format"


def _registry_bytes(registry):
    path = registry.registry_path
    return path.read_bytes() if path.exists() else None


def _mismatches(dslr):
    return [row for row in dslr.log if row.get("action") == "capture_format_mismatch"]


def _testing_volumes(unit, *formats):
    """One testing project and volume for each format, as (project name, volume id)."""
    made = []
    for n, image_format in enumerate(formats):
        project = unit.project(f"Pruebas {n}", capture_format=image_format)
        made.append((project["name"], unit.volume(project_id=project["id"])["id"]))
    return made


def _suffix(capture):
    return Path(capture["file_path"]).suffix


@pytest.mark.unit
@pytest.mark.parametrize("stored,held,project_format,writes,suffixes", [
    pytest.param("RAW+JPEG", "RAW", "JPEG", ["L", "RAW"], [".jpg", ".cr2"], id="testing-jpeg-on-raw-body"),
    pytest.param("JPEG", "L", "RAW+JPEG", ["RAW", "L"], [".cr2", ".jpg"], id="testing-raw-on-jpeg-body"),
])
def test_a_testing_capture_on_a_dslr_leaves_the_bodys_format_to_the_next_capture(
    client, db_session, override_projects_root, rig, registry, dslr, stored, held, project_format, writes, suffixes,
):
    rig.add(PORT_A, SERIAL_A, imageformat=held)
    _store(registry, HW_A, stored)
    unit = Unit(db_session, client)
    [(name, volume_id)] = _testing_volumes(unit, project_format)
    registry_before = _registry_bytes(registry)

    testing = unit.capture("testing", name, volume_id)
    ordinary = unit.capture("operator", unit.real_project.name, unit.real_volume.id)

    assert rig.formats_written(PORT_A) == writes
    assert [_suffix(testing), _suffix(ordinary)] == suffixes
    assert _registry_bytes(registry) == registry_before
    assert _mismatches(dslr) == []
    assert dslr.adoptions == []


def _own_suffix(held):
    return ".cr2" if held in ("RAW", "RAW + L") else ".jpg"


def _assert_nothing_applied(unit, rig, registry, dslr, caplog, held, registry_before):
    """Testing captures in each format, then an ordinary one: all in the camera's own format, nothing written."""
    import logging

    caplog.set_level(logging.INFO, logger=DSLR_LOGGER)
    captures = [unit.capture("testing", name, volume_id)
                for name, volume_id in _testing_volumes(unit, "JPEG", "RAW+JPEG")]
    captures.append(unit.capture("operator", unit.real_project.name, unit.real_volume.id))

    assert rig.formats_written(PORT_A) == []
    assert [_suffix(c) for c in captures] == [_own_suffix(held)] * 3
    assert rig.bodies[PORT_A]["imageformat"] == held
    assert _mismatches(dslr) == []
    assert _registry_bytes(registry) == registry_before
    not_applied = [r.getMessage() for r in caplog.records
                   if r.name == DSLR_LOGGER and r.levelno == logging.INFO and "not applied" in r.getMessage()]
    assert len(not_applied) == 2, not_applied
    assert "JPEG" in not_applied[0] and "RAW+JPEG" in not_applied[1]
    assert all("Camera 0" in m for m in not_applied)


@pytest.mark.unit
def test_with_nothing_stored_and_a_value_captua_does_not_set_no_testing_format_is_written(
    client, db_session, override_projects_root, rig, registry, dslr, caplog,
):
    rig.add(PORT_A, SERIAL_A, imageformat="RAW + L")
    unit = Unit(db_session, client)
    _assert_nothing_applied(unit, rig, registry, dslr, caplog, "RAW + L", _registry_bytes(registry))
    # The open looked at the camera's own value and adopted nothing.
    assert dslr.adoptions == ["RAW + L"]


@pytest.mark.unit
@pytest.mark.parametrize("held", ["RAW + L", "L", "RAW"])
def test_on_a_body_with_no_serial_no_testing_format_is_written(
    client, db_session, override_projects_root, rig, registry, dslr, caplog, held,
):
    rig.add(PORT_A, None, imageformat=held)
    unit = Unit(db_session, client)
    _assert_nothing_applied(unit, rig, registry, dslr, caplog, held, _registry_bytes(registry))
    assert dslr.adoptions == []


@pytest.mark.unit
@pytest.mark.parametrize("held", ["RAW + L", "L", "RAW"])
def test_with_a_registry_that_cannot_be_read_no_testing_format_is_written(
    client, db_session, override_projects_root, rig, registry, dslr, caplog, held,
):
    rig.add(PORT_A, SERIAL_A, imageformat=held)

    def unreadable():
        raise OSError("cameras.json is not readable")
    dslr.backend._registry_factory = unreadable
    unit = Unit(db_session, client)
    _assert_nothing_applied(unit, rig, registry, dslr, caplog, held, _registry_bytes(registry))
    assert dslr.adoptions == []


@pytest.mark.unit
def test_a_reopen_after_a_testing_capture_adopts_only_the_cameras_own_value(
    client, db_session, override_projects_root, rig, registry, dslr,
):
    from tests.unit.test_gphoto2_stored_format import _stored

    rig.add(PORT_A, SERIAL_A, imageformat="RAW + L")
    unit = Unit(db_session, client)
    [(name, volume_id)] = _testing_volumes(unit, "JPEG")
    unit.capture("testing", name, volume_id)
    dslr.backend._close_session(0)

    ordinary = unit.capture("operator", unit.real_project.name, unit.real_volume.id)

    assert dslr.adoptions == ["RAW + L", "RAW + L"]
    assert _stored(HW_A, registry.registry_path) is None
    assert rig.formats_written(PORT_A) == []
    assert _suffix(ordinary) == ".cr2"


@pytest.mark.unit
@pytest.mark.parametrize("held,adopted", [("L", "JPEG"), ("RAW", "RAW+JPEG")])
def test_a_serial_learnt_after_a_testing_capture_reads_the_cameras_own_value(
    client, db_session, override_projects_root, rig, registry, dslr, held, adopted,
):
    from tests.unit.test_gphoto2_stored_format import _stored

    rig.add(PORT_A, SERIAL_A, imageformat=held)
    rig.unreadable.add((PORT_A, "serialnumber"))
    unit = Unit(db_session, client)
    other = "RAW+JPEG" if adopted == "JPEG" else "JPEG"
    [(name, volume_id)] = _testing_volumes(unit, other)
    testing = unit.capture("testing", name, volume_id)
    assert dslr.backend._sessions[0].serial == ""

    rig.unreadable.discard((PORT_A, "serialnumber"))
    dslr.backend.list_devices()

    assert dslr.adoptions == [held]
    assert _stored(HW_A, registry.registry_path) == adopted
    assert rig.formats_written(PORT_A) == []
    assert _suffix(testing) == _own_suffix(held)
