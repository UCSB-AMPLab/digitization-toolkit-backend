"""The testing account creates and captures (NEH-391): projects, volumes, records and pictures of its own.

The testing account creates testing projects and testing volumes and
captures into them, in every unit mode, through the real handlers; only the
camera is stood in for (tests/unit/test_capture_contract_naming.py's fake
backend, which writes a master where the capture service asks).

A. Create: a testing project (stored with the prefix, shown without it), a
   top-level volume and a sub-volume, each given its stem from the reserved
   series `captua-testing-<id, six digits>`; in each mode and with projects
   hidden; with no Fisqua lookup; `project_id` required, a stem or a
   reference code refused, `kept_on_unit` ignored. Administrators and
   operators create no testing material.
B. Capture: single and dual captures, a retake and a test capture by the
   testing account write only into the testing volume's directory and
   manifest; the same requests naming real work, and an operator's capture
   into a testing volume, are refused and write nothing.
C. The recapture turn is real work's alone.
D. The camera writes stay refused to the testing account and change nothing.
E. The whole story, from switching the space on to the zip, and what the
   other roles see of it.

    python -m pytest tests/unit/test_testing_space_capture.py

Version: 0.3.0
"""

import json
from pathlib import Path

import pytest

import capture.project_manager as project_manager_module
import capture.service as capture_service
from tests.unit.test_capture_contract_naming import _FakeBackend

T = "captua-testing-"
# app.core.project_names.TESTING_PROJECT_PREFIX: a testing project's stored name begins with it.
TP = T + "-"
CALLERS = ("testing", "operator", "reviewer", "admin")
MODES = [
    pytest.param("offline", True, id="offline-projects-shown"),
    pytest.param("offline", False, id="offline-projects-hidden"),
    pytest.param("backup", False, id="backup"),
    pytest.param("fisqua", False, id="fisqua"),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _unit(db, mode, projects_enabled=False):
    from app.models.unit_settings import UnitSettings
    row = db.get(UnitSettings, 1)
    if row is None:
        row = UnitSettings(id=1, mode=mode, projects_enabled=projects_enabled)
        db.add(row)
    else:
        row.mode, row.projects_enabled = mode, projects_enabled
    db.commit()


class Unit:
    """A unit with one user per role, a real project and volume with one real capture, and a camera stood in for."""

    def __init__(self, db, client, root, backend):
        from app.core.security import create_access_token
        from app.models.collection import Collection
        from app.models.project import Project
        from app.models.user import User

        self.db, self.client, self.root, self.backend = db, client, root, backend
        self.headers, self.users = {}, {}
        for role in CALLERS:
            user = User(username=f"u-{role}", hashed_password="x", role=role, is_active=True)
            db.add(user)
            db.commit()
            db.refresh(user)
            self.users[role] = user
            self.headers[role] = {"Authorization": f"Bearer {create_access_token(str(user.id))}"}
        self.real_project = Project(name="Proyecto real")
        db.add(self.real_project)
        db.commit()
        self.real_volume = Collection(name="Tomo real", project_id=self.real_project.id, filename_stem="xx-real-1")
        db.add(self.real_volume)
        db.commit()
        db.refresh(self.real_project)
        db.refresh(self.real_volume)
        resp = self.post("operator", "/cameras/capture/dual",
                         {"project_name": self.real_project.name, "collection_id": self.real_volume.id})
        assert resp.status_code == 200 and resp.json()["success"], resp.text
        self.real_record_id = resp.json()["record_id"]

    def post(self, caller, path, body=None, **kwargs):
        return self.client.post(path, headers=self.headers[caller], json=body, **kwargs)

    def get(self, caller, path, **kwargs):
        return self.client.get(path, headers=self.headers[caller], **kwargs)

    def project(self, name="Pruebas"):
        resp = self.post("testing", "/projects/", {"name": name})
        assert resp.status_code == 200, resp.text
        return resp.json()

    def volume(self, project_id=None, parent_id=None, name="Tomo de prueba", **extra):
        body = {"name": name, **extra}
        if project_id is not None:
            body["project_id"] = project_id
        if parent_id is not None:
            body["parent_collection_id"] = parent_id
        resp = self.post("testing", "/collections/", body)
        assert resp.status_code == 201, resp.text
        return resp.json()

    def snapshot(self):
        """Every row of the material's and the log's tables, and every file under the projects root."""
        from sqlalchemy import select
        from app.models.camera import CameraSettings
        from app.models.collection import Collection
        from app.models.project import Project
        from app.models.record import Record, RecordImage, RecordStatusChange
        from app.models.system_log import SystemLog
        self.db.expire_all()
        rows = {m.__tablename__: sorted(tuple(r) for r in self.db.execute(select(m.__table__)).all())
                for m in (Project, Collection, Record, RecordImage, RecordStatusChange, CameraSettings)}
        rows["system_logs"] = sorted(
            (r.id, r.action) for r in self.db.query(SystemLog).filter(SystemLog.category != "access").all())
        files = sorted((str(p.relative_to(self.root)), p.read_bytes()) for p in self.root.rglob("*") if p.is_file())
        return rows, files


@pytest.fixture
def backend(monkeypatch):
    b = _FakeBackend()
    monkeypatch.setattr(capture_service, "get_backend", lambda: b)
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    monkeypatch.setattr(
        project_manager_module, "default_camera_config_from_registry",
        lambda camera_index, resolution="high", registry=None: (
            {"camera_index": camera_index, "rotate_deg": 0}, "hw"))
    return b


@pytest.fixture
def lookups(monkeypatch):
    """Every Fisqua reference-code lookup, recorded instead of sent."""
    from app.core import unit_mode
    calls = []
    monkeypatch.setattr(unit_mode, "check_reference_codes", lambda db, codes: calls.append(list(codes)))
    return calls


@pytest.fixture
def unit(client, db_session, override_projects_root, backend, lookups):
    _unit(db_session, "offline", True)
    return Unit(db_session, client, override_projects_root, backend)


def _code(resp, status, code):
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"]["code"] == code, resp.text


def _not_found(resp):
    _code(resp, 404, "not_found")


def _series(volume_id):
    return f"{T}{volume_id:06d}"


def _row(db, model, row_id):
    db.expire_all()
    return db.get(model, row_id)


# ---------------------------------------------------------------------------
# A. Projects and volumes
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("mode,projects_enabled", MODES)
def test_the_testing_account_creates_a_project_a_volume_and_a_sub_volume(unit, lookups, mode, projects_enabled):
    from app.core.paths import project_dir_name
    from app.models.collection import Collection
    from app.models.project import Project
    _unit(unit.db, mode, projects_enabled)

    project = unit.project("Pruebas de cámara")
    assert project["name"] == "Pruebas de cámara"
    row = _row(unit.db, Project, project["id"])
    assert row.testing is True and row.name == TP + "Pruebas de cámara"
    assert project_dir_name(row.name).startswith(TP)

    top = unit.volume(project_id=project["id"])
    sub = unit.volume(parent_id=top["id"], name="Cuadernillo")
    for body in (top, sub):
        vol = _row(unit.db, Collection, body["id"])
        assert vol.testing is True and vol.kept_on_unit is True
        assert vol.filename_stem == _series(vol.id) == body["filename_stem"]
        assert vol.external_id is None
    assert _row(unit.db, Collection, top["id"]).project_id == project["id"]
    assert _row(unit.db, Collection, sub["id"]).parent_collection_id == top["id"]
    assert lookups == []


@pytest.mark.unit
@pytest.mark.parametrize("mode,projects_enabled", MODES)
def test_a_top_level_testing_volume_needs_a_project(unit, mode, projects_enabled):
    _unit(unit.db, mode, projects_enabled)
    before = unit.snapshot()
    resp = unit.post("testing", "/collections/", {"name": "Sin proyecto"})
    assert resp.status_code == 400, resp.text
    assert unit.snapshot() == before


@pytest.mark.unit
@pytest.mark.parametrize("field", ["filename_stem", "external_id"])
@pytest.mark.parametrize("mode,projects_enabled", MODES)
def test_the_testing_account_sends_no_stem_or_reference_code(unit, field, mode, projects_enabled):
    _unit(unit.db, mode, projects_enabled)
    project = unit.project()
    before = unit.snapshot()
    resp = unit.post("testing", "/collections/", {"name": "Tomo", "project_id": project["id"],
                                                  field: T + "elegido-1"})
    _code(resp, 409, "testing_volume_codes")
    assert unit.snapshot() == before


@pytest.mark.unit
@pytest.mark.parametrize("kept", [False, True])
@pytest.mark.parametrize("mode,projects_enabled", MODES)
def test_kept_on_unit_from_the_testing_account_is_ignored(unit, kept, mode, projects_enabled):
    from app.models.collection import Collection
    _unit(unit.db, mode, projects_enabled)
    project = unit.project()
    body = unit.volume(project_id=project["id"], kept_on_unit=kept)
    assert _row(unit.db, Collection, body["id"]).kept_on_unit is True


@pytest.mark.unit
def test_a_testing_projects_display_name_has_a_limit(unit):
    before = unit.snapshot()
    resp = unit.post("testing", "/projects/", {"name": "x" * 240})
    assert resp.status_code == 422, resp.text
    assert unit.snapshot() == before
    assert unit.project("y" * 239)["name"] == "y" * 239


@pytest.mark.unit
def test_a_testing_project_name_is_unique_in_its_space(unit):
    unit.project("Doble")
    before = unit.snapshot()
    assert unit.post("testing", "/projects/", {"name": "Doble"}).status_code == 409
    assert unit.snapshot() == before


@pytest.mark.unit
def test_administrators_and_operators_create_no_testing_material(unit):
    from app.models.project import Project
    project = unit.project()
    volume = unit.volume(project_id=project["id"])
    before = unit.snapshot()
    _code(unit.post("admin", "/projects/", {"name": T + "Pruebas"}), 409, "name_reserved")
    _code(unit.post("admin", "/projects/", {"name": TP + "Pruebas"}), 409, "name_reserved")
    assert unit.post("operator", "/projects/", {"name": "Otro"}).status_code == 403
    for caller in ("admin", "operator"):
        _not_found(unit.post(caller, "/collections/", {"name": "Tomo", "project_id": project["id"]}))
        _not_found(unit.post(caller, "/collections/", {"name": "Tomo", "parent_collection_id": volume["id"]}))
    assert unit.snapshot() == before
    # An administrator's project is real, whatever else the body says.
    resp = unit.post("admin", "/projects/", {"name": "Real", "testing": True})
    assert resp.status_code == 200, resp.text
    assert _row(unit.db, Project, resp.json()["id"]).testing is False


@pytest.mark.unit
def test_the_testing_account_initialises_no_project(unit):
    project = unit.project()
    before = unit.snapshot()
    assert unit.post("testing", f"/projects/{project['id']}/initialize", {}).status_code == 403
    assert unit.snapshot() == before


@pytest.mark.unit
@pytest.mark.parametrize("mode,projects_enabled", MODES)
def test_the_unit_shows_projects_to_the_testing_account(unit, mode, projects_enabled):
    _unit(unit.db, mode, projects_enabled)
    body = unit.get("testing", "/system/unit").json()
    assert body["projectsShown"] is True
    assert body["mode"] == mode and body["projectsEnabled"] is projects_enabled
    assert body["defaultProjectId"] is None and body["keptVolumes"] == [] and body["prerequisites"] == []
    operator = unit.get("operator", "/system/unit").json()
    assert operator["projectsShown"] is (mode == "offline" and projects_enabled)


# ---------------------------------------------------------------------------
# B. Records and capture
# ---------------------------------------------------------------------------

def _testing_volume(unit):
    project = unit.project()
    return project, unit.volume(project_id=project["id"])


def _manifest_lines(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def _manifests(root):
    return sorted(root.rglob("metadata/manifest.jsonl"))


def _capture(unit, caller, project_name, volume_id, dual=True, **extra):
    body = {"project_name": project_name, "collection_id": volume_id, **extra}
    path = "/cameras/capture/dual" if dual else "/cameras/capture"
    if not dual:
        body.setdefault("camera_index", 0)
    return unit.post(caller, path, body)


@pytest.mark.unit
@pytest.mark.parametrize("dual", [False, True], ids=["single", "dual"])
@pytest.mark.parametrize("mode,projects_enabled", MODES)
def test_a_testing_capture_writes_into_its_volume_only(unit, dual, mode, projects_enabled):
    from app.models.record import Record, RecordImage
    _unit(unit.db, mode, projects_enabled)
    project, volume = _testing_volume(unit)
    stem = volume["filename_stem"]
    real_manifests = {p: p.read_bytes() for p in _manifests(unit.root)}
    assert real_manifests

    resp = _capture(unit, "testing", project["name"], volume["id"], dual=dual)
    assert resp.status_code == 200 and resp.json()["success"], resp.text
    record = _row(unit.db, Record, resp.json()["record_id"])
    assert record.collection_id == volume["id"] and record.status == "captured"
    assert record.title.startswith(project["name"] + " - ") and T not in record.title.split(" - ")[0]
    images = unit.db.query(RecordImage).filter(RecordImage.record_id == record.id).all()
    assert len(images) == (2 if dual else 1)
    for img in images:
        path = Path(img.file_path)
        assert path.parent == unit.root / stem / "captures" and path.name.startswith(stem + "-img_0001_c")
        assert path.is_file()
    own = unit.root / stem / "metadata" / "manifest.jsonl"
    assert [line["capture_id"] for line in _manifest_lines(own)] == [images[0].capture_id]
    for manifest in _manifests(unit.root):
        if manifest != own:
            assert manifest.read_bytes() == real_manifests[manifest], manifest


@pytest.mark.unit
def test_the_testing_account_retakes_its_record(unit):
    from app.models.record import Record, RecordImage
    project, volume = _testing_volume(unit)
    first = _capture(unit, "testing", project["name"], volume["id"]).json()
    unit.db.get(Record, first["record_id"]).status = "rejected"
    unit.db.commit()
    resp = _capture(unit, "testing", project["name"], volume["id"], record_id=first["record_id"])
    assert resp.status_code == 200 and resp.json()["success"], resp.text
    unit.db.expire_all()
    assert unit.db.get(Record, first["record_id"]).status == "captured"
    current = (unit.db.query(RecordImage)
               .filter(RecordImage.record_id == first["record_id"], RecordImage.is_current.is_(True)).all())
    assert sorted(img.id for img in current) == sorted(resp.json()["image_ids"])
    assert all(Path(img.file_path).name.startswith(volume["filename_stem"] + "-img_0002") for img in current)


@pytest.mark.unit
def test_a_test_capture_saves_nothing(unit):
    project, volume = _testing_volume(unit)
    before = unit.snapshot()
    resp = unit.client.post("/cameras/test-capture/0", headers=unit.headers["testing"])
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg", resp.text
    assert unit.snapshot() == before


@pytest.mark.unit
@pytest.mark.parametrize("dual", [False, True], ids=["single", "dual"])
def test_a_testing_capture_naming_real_work_is_refused(unit, dual):
    project, volume = _testing_volume(unit)
    before = unit.snapshot()
    # A real volume.
    _not_found(_capture(unit, "testing", project["name"], unit.real_volume.id, dual=dual))
    # A real record to retake.
    _not_found(_capture(unit, "testing", project["name"], volume["id"], dual=dual, record_id=unit.real_record_id))
    # A real project's name, as stored, and as it would be stored for a testing project.
    _not_found(_capture(unit, "testing", unit.real_project.name, volume["id"], dual=dual))
    _not_found(_capture(unit, "testing", "No existe", volume["id"], dual=dual))
    if dual:
        _not_found(_capture(unit, "testing", project["name"], volume["id"], afterRecordId=unit.real_record_id))
    assert unit.snapshot() == before
    assert unit.backend.requested == ["xx-real-1-img_0001_c0.jpg", "xx-real-1-img_0001_c1.jpg"]


@pytest.mark.unit
@pytest.mark.parametrize("caller", ["operator", "admin"])
@pytest.mark.parametrize("dual", [False, True], ids=["single", "dual"])
def test_an_operators_capture_into_a_testing_volume_is_refused(unit, caller, dual):
    project, volume = _testing_volume(unit)
    before = unit.snapshot()
    _not_found(_capture(unit, caller, unit.real_project.name, volume["id"], dual=dual))
    _not_found(_capture(unit, caller, TP + project["name"], unit.real_volume.id, dual=dual))
    assert unit.snapshot() == before


@pytest.mark.unit
def test_the_testing_account_creates_records_in_its_space_only(unit):
    from app.models.record import Record
    project, volume = _testing_volume(unit)
    resp = unit.post("testing", "/records/", {"title": "Hoja", "capture_mode": "single",
                                              "collection_id": volume["id"]})
    assert resp.status_code == 200, resp.text
    assert _row(unit.db, Record, resp.json()["id"]).collection_id == volume["id"]
    before = unit.snapshot()
    # A testing record lives in a volume: one with no volume is refused, in the account's own project too.
    for extra in ({}, {"project_id": project["id"]}, {"project_id": 999999}):
        _code(unit.post("testing", "/records/", {"title": "x", "capture_mode": "single", **extra}),
              400, "testing_record_needs_volume")
    # A real project is not found, by the space rule, before the handler.
    _not_found(unit.post("testing", "/records/", {"title": "x", "capture_mode": "single",
                                                  "project_id": unit.real_project.id}))
    _not_found(unit.post("testing", "/records/", {"title": "x", "capture_mode": "single",
                                                  "collection_id": unit.real_volume.id}))
    _not_found(unit.post("operator", "/records/", {"title": "x", "capture_mode": "single",
                                                   "collection_id": volume["id"]}))
    assert unit.snapshot() == before


@pytest.mark.unit
def test_the_testing_account_uploads_and_marks_missing_pages_in_its_records(unit):
    from app.models.record import Record, RecordImage
    project, volume = _testing_volume(unit)
    stem = volume["filename_stem"]
    rec = _capture(unit, "testing", project["name"], volume["id"]).json()["record_id"]
    resp = unit.client.post(f"/records/{rec}/images", headers=unit.headers["testing"],
                            files={"file": ("p.jpg", b"\xff\xd8upload", "image/jpeg")})
    assert resp.status_code == 200, resp.text
    img = _row(unit.db, RecordImage, resp.json()["id"])
    assert Path(img.file_path).parent == unit.root / stem / "captures"
    resp = unit.post("testing", f"/records/{rec}/missing-page", {"side": "left"})
    assert resp.status_code == 200, resp.text
    resp = unit.post("testing", f"/records/{rec}/close-missing", {"reason": "foliation_skip"})
    assert resp.status_code == 200, resp.text
    resp = unit.post("testing", f"/collections/{volume['id']}/records/missing-opening",
                     {"afterRecordId": rec, "count": 1})
    assert resp.status_code in (200, 201), resp.text
    assert _row(unit.db, Record, resp.json()[0]["id"]).collection_id == volume["id"]
    before = unit.snapshot()
    _not_found(unit.post("testing", f"/records/{unit.real_record_id}/missing-page", {"side": "left"}))
    _not_found(unit.client.post(f"/records/{unit.real_record_id}/images", headers=unit.headers["testing"],
                                files={"file": ("p.jpg", b"\xff\xd8upload", "image/jpeg")}))
    _not_found(unit.post("testing", f"/collections/{unit.real_volume.id}/records/missing-opening",
                         {"afterRecordId": unit.real_record_id, "count": 1}))
    assert unit.snapshot() == before


@pytest.mark.unit
def test_the_testing_account_moves_records_and_volumes_between_its_projects_only(unit):
    from app.models.collection import Collection
    from app.models.record import Record
    one, volume = _testing_volume(unit)
    two = unit.project("Otras pruebas")
    other = unit.volume(project_id=two["id"])
    # A testing record lives in a volume: it moves between the account's volumes, never into a project alone.
    rec = unit.post("testing", "/records/", {"title": "Hoja", "capture_mode": "single",
                                             "collection_id": volume["id"]}).json()["id"]
    assert unit.client.patch(f"/records/{rec}", headers=unit.headers["testing"],
                             json={"collection_id": other["id"]}).status_code == 200
    assert _row(unit.db, Record, rec).collection_id == other["id"]
    assert unit.post("testing", f"/projects/{two['id']}/add_record/{rec}").status_code == 403
    resp = unit.post("testing", f"/projects/{one['id']}/move-collections", {"target_project_id": two["id"]})
    assert resp.status_code == 200, resp.text
    assert _row(unit.db, Collection, volume["id"]).project_id == two["id"]
    before = unit.snapshot()
    _not_found(unit.post("testing", f"/projects/{two['id']}/move-collections",
                         {"target_project_id": unit.real_project.id}))
    assert unit.snapshot() == before


@pytest.mark.unit
@pytest.mark.parametrize("mode,projects_enabled", [pytest.param("offline", True, id="offline"),
                                                   pytest.param("fisqua", False, id="fisqua")])
@pytest.mark.parametrize("caller", ["admin", "operator"])
def test_a_full_volume_body_naming_testing_work_from_another_role_writes_nothing(unit, lookups, caller, mode,
                                                                                 projects_enabled):
    project, volume = _testing_volume(unit)
    _unit(unit.db, mode, projects_enabled)
    before = unit.snapshot()
    for place in ({"project_id": project["id"]}, {"parent_collection_id": volume["id"]}):
        body = {"name": "Tomo completo", "filename_stem": "xx-completo-1", "external_id": "xx-completo-1",
                "description": "d", "kept_on_unit": False, **place}
        _not_found(unit.post(caller, "/collections/", body))
    assert unit.snapshot() == before
    assert lookups == []


@pytest.mark.unit
def test_an_operators_testing_project_id_with_projects_hidden_is_not_found(unit):
    """With projects hidden, POST /collections/ ignores an operator's project_id and uses the default project.

    A testing project_id is refused before the handler (app.api.space.enforce_space), so the operator learns
    nothing of it and no volume is put in the default project in its place.
    """
    project = unit.project()
    _unit(unit.db, "offline", False)
    before = unit.snapshot()
    _not_found(unit.post("operator", "/collections/", {"name": "Tomo", "filename_stem": "xx-oculto-1",
                                                       "project_id": project["id"]}))
    assert unit.snapshot() == before


def _volume_tree(root, stem):
    """Every path under a volume's directory, with the stem written as <stem>: directories end with /."""
    base = root / stem
    return sorted(str(p.relative_to(base)).replace(stem, "<stem>") + ("/" if p.is_dir() else "")
                  for p in base.rglob("*"))


@pytest.mark.unit
def test_a_dual_capture_whose_second_camera_fails_leaves_the_same_in_either_space(unit):
    from app.models.collection import Collection
    project, volume = _testing_volume(unit)
    stem = volume["filename_stem"]
    fresh = Collection(name="Nuevo real", project_id=unit.real_project.id, filename_stem="xx-real-2")
    unit.db.add(fresh)
    unit.db.commit()
    rows_before, files_before = unit.snapshot()
    unit.backend.fail_after_write = {1}
    for caller, name, vid in (("testing", project["name"], volume["id"]),
                              ("operator", unit.real_project.name, fresh.id)):
        resp = _capture(unit, caller, name, vid)
        assert resp.status_code == 200 and resp.json()["success"] is False, resp.text
        assert "cam1" in resp.json()["error"]
    rows_after, files_after = unit.snapshot()
    # No row of any kind, and no file anywhere: each failure leaves only its volume's empty directories.
    assert rows_after == rows_before
    assert files_after == files_before
    tree = ["captures/", "metadata/", "previews/", "thumbnails/"]
    assert _volume_tree(unit.root, stem) == tree
    assert _volume_tree(unit.root, "xx-real-2") == tree
    assert unit.db.get(Collection, volume["id"]).capture_counter == 0
    assert unit.db.get(Collection, fresh.id).capture_counter == 0
    # The numbers given to the failed events are given again.
    unit.backend.fail_after_write = set()
    real = _capture(unit, "operator", unit.real_project.name, fresh.id).json()
    assert real["success"] and [Path(p).name for p in real["file_paths"]] == [
        "xx-real-2-img_0001_c0.jpg", "xx-real-2-img_0001_c1.jpg"]
    testing = _capture(unit, "testing", project["name"], volume["id"]).json()
    assert testing["success"] and [Path(p).name for p in testing["file_paths"]] == [
        f"{stem}-img_0001_c0.jpg", f"{stem}-img_0001_c1.jpg"]
    after = _volume_tree(unit.root, stem)
    assert {"captures/<stem>-img_0001_c0.jpg", "captures/<stem>-img_0001_c1.jpg", "metadata/manifest.jsonl"} <= set(after)
    assert [p for p in after if p.startswith("captures/")] == [
        "captures/", "captures/<stem>-img_0001_c0.jpg", "captures/<stem>-img_0001_c1.jpg"]


# ---------------------------------------------------------------------------
# Locks and the busy state, shared with real work
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_failed_testing_capture_leaves_nothing_that_blocks_real_work(unit):
    from app.core import unit_activity
    from app.models.collection import Collection
    project, volume = _testing_volume(unit)
    unit.backend.fail_after_write = {0, 1}
    resp = _capture(unit, "testing", project["name"], volume["id"])
    assert resp.status_code == 200 and resp.json()["success"] is False, resp.text
    assert not unit_activity.in_progress(unit_activity.CAMERA)
    unit.backend.fail_after_write = set()
    resp = _capture(unit, "operator", unit.real_project.name, unit.real_volume.id)
    assert resp.status_code == 200 and resp.json()["success"], resp.text
    fresh = Collection(name="Nuevo real", project_id=unit.real_project.id, filename_stem="xx-real-2")
    unit.db.add(fresh)
    unit.db.commit()
    resp = _capture(unit, "operator", unit.real_project.name, fresh.id)
    assert resp.status_code == 200 and resp.json()["success"], resp.text
    assert not unit_activity.in_progress(unit_activity.CAMERA)


# ---------------------------------------------------------------------------
# C. The recapture turn is real work's alone
# ---------------------------------------------------------------------------

STARTED, COMPLETED = "new_volume_started", "recapture_volume_completed"


def _events(db, *actions):
    from app.models.system_log import SystemLog
    db.expire_all()
    return [(e.action, e.detail) for e in
            db.query(SystemLog).filter(SystemLog.action.in_(actions)).order_by(SystemLog.id).all()]


def _real_pending(unit):
    from app.models.record import Record
    unit.db.get(Record, unit.real_record_id).status = "rejected"
    unit.db.commit()


def _summary(unit, caller="operator", **params):
    resp = unit.get(caller, "/records/to-retake/summary", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.unit
def test_a_testing_volume_is_started_in_the_recaptures_turn_and_writes_no_event(unit):
    _real_pending(unit)
    events = _events(unit.db, STARTED, COMPLETED)
    assert _summary(unit)["turn"]["state"] == "recaptures"
    project, volume = _testing_volume(unit)
    for dual in (True, False):
        resp = _capture(unit, "testing", project["name"], volume["id"], dual=dual)
        assert resp.status_code == 200 and resp.json()["success"], resp.text
        other = unit.volume(project_id=project["id"], name=f"Otro {dual}")
        resp = _capture(unit, "testing", project["name"], other["id"], dual=dual)
        assert resp.status_code == 200 and resp.json()["success"], resp.text
    assert _events(unit.db, STARTED, COMPLETED) == events
    assert _summary(unit)["turn"]["state"] == "recaptures"


@pytest.mark.unit
def test_testing_records_never_count_for_the_turn(unit):
    from app.models.collection import Collection
    from app.models.record import Record
    project, volume = _testing_volume(unit)
    rec = _capture(unit, "testing", project["name"], volume["id"]).json()["record_id"]
    before = {caller: _summary(unit, caller) for caller in ("operator", "reviewer", "admin")}
    assert before["operator"]["turn"] == {"state": "free", "unlockedBy": None}
    unit.db.get(Record, rec).status = "rejected"
    unit.db.commit()
    for caller in ("operator", "reviewer", "admin"):
        assert _summary(unit, caller) == before[caller], caller
    # The administrator's testing summary lists the testing retake, and the turn is still real work's.
    testing = _summary(unit, "admin", space="testing")
    assert [v["collectionId"] for v in testing["volumes"]] == [volume["id"]]
    assert testing["turn"] == before["admin"]["turn"]
    # An operator starts a real volume while real work allows it.
    fresh = Collection(name="Nuevo real", project_id=unit.real_project.id, filename_stem="xx-real-2")
    unit.db.add(fresh)
    unit.db.commit()
    resp = _capture(unit, "operator", unit.real_project.name, fresh.id)
    assert resp.status_code == 200 and resp.json()["success"], resp.text
    # With real work pending too, the counts and the turn are real work's only.
    _real_pending(unit)
    summary = _summary(unit)
    assert summary["pendingRecords"] == 1 and summary["turn"]["state"] == "recaptures"


@pytest.mark.unit
@pytest.mark.parametrize("action", ["new_volume_started", "recapture_volume_completed", "rescan_volume_completed"])
def test_record_event_writes_nothing_for_a_testing_volume(unit, action):
    from app.core import recapture_turn
    from app.models.collection import Collection
    project, volume = _testing_volume(unit)
    before = _events(unit.db, action)
    recapture_turn.record_event(unit.db, action, unit.db.get(Collection, volume["id"]), "u-testing")
    unit.db.commit()
    assert _events(unit.db, action) == before
    # The same call for a real volume writes one: the test can fail.
    recapture_turn.record_event(unit.db, action, unit.db.get(Collection, unit.real_volume.id), "u-operator")
    unit.db.commit()
    assert len(_events(unit.db, action)) == len(before) + 1


TURN_ACTIONS = ("new_volume_started", "recapture_volume_completed", "rescan_volume_completed")
TURN_CONSTANTS = ("STARTED_ACTION", "COMPLETED_ACTION", "RESCAN_COMPLETED_ACTION", "_COMPLETION_ACTIONS")


@pytest.mark.unit
def test_record_event_is_the_one_writer_of_the_turn_events():
    """Read from the source with ast, over every module under app/:

    * the three action names appear as string literals in app/core/recapture_turn.py only, where the
      constants are defined;
    * outside that module, a turn constant is never the value of an `action=` keyword (a SystemLog row or
      log_event) and never an argument of a call other than record_event, has_event and the readers;
    * inside it, the only SystemLog(...) call is in record_event.
    """
    import ast
    app_dir = Path(__file__).resolve().parents[2] / "app"
    literal_holders, misused, systemlog_calls = set(), [], []
    readers = {"record_event", "has_event", "_newest", "in_"}
    for path in sorted(app_dir.rglob("*.py")):
        tree = ast.parse(path.read_text())
        rel = str(path.relative_to(app_dir))
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in TURN_ACTIONS:
                literal_holders.add(rel)

            def names_turn_action(value):
                return (isinstance(value, ast.Constant) and value.value in TURN_ACTIONS) or (
                    isinstance(value, ast.Attribute) and value.attr in TURN_CONSTANTS) or (
                    isinstance(value, ast.Name) and value.id in TURN_CONSTANTS)

            if isinstance(node, ast.Call):
                func = node.func
                fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if fname == "SystemLog" and rel == "core/recapture_turn.py":
                    owner = node
                    while owner in parents and not isinstance(owner, ast.FunctionDef):
                        owner = parents[owner]
                    systemlog_calls.append(getattr(owner, "name", None))
                if rel == "core/recapture_turn.py":
                    continue
                for kw in node.keywords:
                    if kw.arg == "action" and names_turn_action(kw.value):
                        misused.append(f"{rel}:{node.lineno} action={ast.unparse(kw.value)}")
                if fname not in readers and any(names_turn_action(arg) for arg in node.args):
                    misused.append(f"{rel}:{node.lineno} {ast.unparse(node)[:80]}")
    assert literal_holders == {"core/recapture_turn.py"}
    assert misused == []
    assert systemlog_calls == ["record_event"]


@pytest.mark.unit
def test_completing_testing_retakes_writes_no_completion_event(unit):
    from app.models.record import Record
    _real_pending(unit)
    project, volume = _testing_volume(unit)
    rec = _capture(unit, "testing", project["name"], volume["id"]).json()["record_id"]
    unit.db.get(Record, rec).status = "rejected"
    unit.db.commit()
    events = _events(unit.db, STARTED, COMPLETED)
    resp = _capture(unit, "testing", project["name"], volume["id"], record_id=rec)
    assert resp.status_code == 200 and resp.json()["success"], resp.text
    assert _row(unit.db, Record, rec).status == "captured"
    # A missing page closed by the account: the record is not pending, and no event is written either.
    assert unit.post("testing", f"/records/{rec}/missing-page", {"side": "right"}).status_code == 200
    assert unit.post("testing", f"/records/{rec}/close-missing", {"reason": "foliation_skip"}).status_code == 200
    assert _events(unit.db, STARTED, COMPLETED) == events


# ---------------------------------------------------------------------------
# D. Camera writes stay refused
# ---------------------------------------------------------------------------

CAMERA_WRITES = [
    ("PUT", "/cameras/roles", None, {"left": {"hardware_id": "a"}, "right": {"hardware_id": "b"}}),
    ("POST", "/cameras/rescan", None, None),
    ("POST", "/cameras/side/{camera_index}", "/cameras/side/0", {"side": "odd"}),
    ("POST", "/cameras/focus/{camera_index}", "/cameras/focus/0", {"lens_position": 2.0}),
    ("POST", "/cameras/focus-lock/{camera_index}", "/cameras/focus-lock/0", None),
    ("DELETE", "/cameras/focus-lock/{camera_index}", "/cameras/focus-lock/0", None),
    ("POST", "/cameras/settings/{camera_index}", "/cameras/settings/0", {"zoom_step": 1}),
    ("POST", "/cameras/calibrate", None, {"camera_index": 0}),
    ("POST", "/cameras/calibrate/white-balance", None, {"camera_index": 0}),
    ("POST", "/cameras/calibrate/white-balance/manual", None, {"camera_index": 0, "awb_gains": [1.0, 1.0]}),
    ("PUT", "/cameras/dslr/{camera_index}/settings", "/cameras/dslr/0/settings", {"iso": "400"}),
    ("PUT", "/cameras/{camera_index}/orientation", "/cameras/0/orientation",
     {"orientation": 180, "hardware_id": "hw"}),
    ("DELETE", "/cameras/preview/tmp", None, None),
    ("POST", "/cameras/", None, "image"),
    ("PUT", "/cameras/settings/{id}", "settings", {"iso": 100}),
    ("DELETE", "/cameras/settings/{id}", "settings", None),
]


@pytest.mark.unit
def test_the_camera_write_table_is_the_routes_the_testing_account_does_not_reach():
    from app.core.space_routes import ROUTES
    for method, template, _url, _body in CAMERA_WRITES:
        assert ROUTES[(method, template)].testing is False, (method, template)


@pytest.mark.unit
@pytest.mark.parametrize("method,template,url,body", CAMERA_WRITES, ids=[f"{m} {t}" for m, t, _u, _b in CAMERA_WRITES])
def test_the_camera_writes_are_refused_to_the_testing_account(unit, method, template, url, body):
    from app.models.camera import CameraSettings
    from app.models.record import RecordImage
    project, volume = _testing_volume(unit)
    rec = _capture(unit, "testing", project["name"], volume["id"]).json()["record_id"]
    image = unit.db.query(RecordImage).filter(RecordImage.record_id == rec).first()
    settings = unit.db.query(CameraSettings).filter(CameraSettings.record_image_id == image.id).first()
    # The unit's stored camera configuration, which no refused request may change.
    (unit.root / "cameras.json").write_text(json.dumps({"cameras": {"hw": {"orientation": 90}}}))
    (unit.root / "camera-bindings.json").write_text(json.dumps({"version": 1, "pins": {"0": "s0"}}))
    if body == "image":
        body = {"record_image_id": image.id}
    if url == "settings":
        url = template.replace("{id}", str(settings.id))
    before = unit.snapshot()
    resp = unit.client.request(method, url or template, headers=unit.headers["testing"],
                               **({"json": body} if body is not None else {}))
    assert resp.status_code == 403, resp.text
    assert unit.snapshot() == before


# ---------------------------------------------------------------------------
# E. The whole story
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_testing_account_works_from_switch_on_to_its_pictures(unit, monkeypatch):
    from app.api import auth as auth_mod
    from app.core import fisqua_queue
    from app.core.login_throttle import LoginThrottle
    from app.models.record import RecordImage
    monkeypatch.setattr(auth_mod, "login_throttle", LoginThrottle())
    monkeypatch.setattr(auth_mod, "notify_operators_changed", lambda *a, **k: None)
    monkeypatch.setattr(fisqua_queue, "notify_operators_changed", lambda *a, **k: None)
    client = unit.client

    resp = client.put("/system/testing", headers=unit.headers["admin"],
                      json={"enabled": True, "username": "pruebas", "password": "pw-de-prueba-91"})
    assert resp.status_code == 200, resp.text
    login = client.post("/auth/login", json={"username": "pruebas", "password": "pw-de-prueba-91"})
    assert login.status_code == 200, login.text
    h = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.post("/projects/", headers=h, json={"name": "Banco de pruebas"})
    assert resp.status_code == 200 and resp.json()["name"] == "Banco de pruebas", resp.text
    project = resp.json()
    resp = client.post("/collections/", headers=h, json={"name": "Tomo de ensayo", "project_id": project["id"]})
    assert resp.status_code == 201, resp.text
    volume = resp.json()
    records = []
    for _ in range(2):
        resp = client.post("/cameras/capture/dual", headers=h,
                           json={"project_name": "Banco de pruebas", "collection_id": volume["id"]})
        assert resp.status_code == 200 and resp.json()["success"], resp.text
        records.append(resp.json()["record_id"])
    listed = client.get("/records/", headers=h, params={"collection_id": volume["id"]})
    assert listed.status_code == 200 and sorted(r["id"] for r in listed.json()) == sorted(records)
    assert all(len(r["images"]) == 2 for r in listed.json()), listed.json()
    assert [p["name"] for p in client.get("/projects/", headers=h).json()] == ["Banco de pruebas"]
    assert [v["id"] for v in client.get("/collections/", headers=h).json()] == [volume["id"]]

    # One picture of the first pair, and its preview, read as the account.
    image = (unit.db.query(RecordImage).filter(RecordImage.record_id == records[0])
             .order_by(RecordImage.id).first())
    master = client.get(f"/records/images/{image.id}/file", headers=h)
    assert master.status_code == 200 and master.content == Path(image.file_path).read_bytes(), master.text
    preview = client.get(f"/records/images/{image.id}/preview", headers=h)
    assert preview.status_code == 200 and preview.content, preview.text
    # The export is not the testing account's: no route takes its records to approved.
    assert client.post(f"/collections/{volume['id']}/export", headers=h).status_code == 403

    for caller in ("operator", "reviewer"):
        assert project["id"] not in {p["id"] for p in unit.get(caller, "/projects/").json()}
        assert volume["id"] not in {v["id"] for v in unit.get(caller, "/collections/").json()}
        assert not set(records) & {r["id"] for r in unit.get(caller, "/records/").json()}
    assert project["id"] not in {p["id"] for p in unit.get("admin", "/projects/").json()}
    assert project["id"] in {p["id"] for p in unit.get("admin", "/projects/", params={"space": "testing"}).json()}
    assert volume["id"] in {v["id"] for v in unit.get("admin", "/collections/", params={"space": "testing"}).json()}
    assert set(records) <= {r["id"] for r in unit.get("admin", "/records/", params={"space": "testing"}).json()}


# ---------------------------------------------------------------------------
# F. Testing project directories and testing volume stems are two disjoint series
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_project_prefix_is_the_testing_prefix_and_a_hyphen():
    from app.core.project_names import TESTING_PREFIX, TESTING_PROJECT_PREFIX
    assert TESTING_PROJECT_PREFIX == TESTING_PREFIX + "-" == TP


@pytest.mark.unit
def test_no_stem_the_pattern_admits_begins_with_the_project_prefix():
    """The pattern admits no two hyphens in a row, and the project prefix ends in two.

    So every string that begins with the project prefix holds "--", and the
    pattern refuses every string that holds "--": no stem begins with the
    prefix, and no testing project's directory (which begins with it) is a stem.
    """
    import re
    from app.core.paths import project_dir_name
    from app.core.testing_space import series_stem
    from app.models.collection import FILENAME_STEM_PATTERN
    assert TP.endswith("--") and "--" not in T
    for value in ("a--b", "a--", "--a", "x-y--z", TP, TP + "x", TP + "000500", TP + "a-b"):
        assert re.fullmatch(FILENAME_STEM_PATTERN, value) is None, value
    for stem in ("a", "a-b", "xx-real-1", T + "000500", T + "a-b-c"):
        assert re.fullmatch(FILENAME_STEM_PATTERN, stem) and not stem.startswith(TP), stem
    for volume_id in (1, 500, 999999, 10 ** 7):
        stem = series_stem(volume_id)
        assert re.fullmatch(FILENAME_STEM_PATTERN, stem) and not stem.startswith(TP)
        assert project_dir_name(TP + f"{volume_id:06d}") != stem


def _force_next_volume_id(db, next_id):
    """The next volume the route inserts takes `next_id`: a volume with the id before it, put in directly."""
    from app.models.collection import Collection
    from app.models.project import Project
    holder = Project(name="Proyecto de relleno")
    db.add(holder)
    db.commit()
    db.add(Collection(id=next_id - 1, name="Relleno", project_id=holder.id, filename_stem="xx-relleno-1"))
    db.commit()


@pytest.mark.unit
def test_a_project_named_like_a_volumes_number_leaves_that_volume_its_stem(unit):
    from app.core.paths import project_dir_name
    from app.models.project import Project
    project = unit.project("000500")
    stored = _row(unit.db, Project, project["id"]).name
    assert stored == TP + "000500" and project_dir_name(stored) == TP + "000500"
    _force_next_volume_id(unit.db, 500)
    volume = unit.volume(project_id=project["id"])
    assert volume["id"] == 500 and volume["filename_stem"] == T + "000500"


@pytest.mark.unit
def test_a_volumes_stem_leaves_a_project_its_number_as_a_name(unit):
    first = unit.project("Primero")
    _force_next_volume_id(unit.db, 500)
    volume = unit.volume(project_id=first["id"])
    assert volume["id"] == 500 and volume["filename_stem"] == T + "000500"
    assert unit.project("000500")["name"] == "000500"


DIRECTORIES = [
    ("-x", TP + "-x"),
    (".x", TP + ".x"),
    (" x", TP + "_x"),
    ("_x", TP + "_x"),
    ("x--y", TP + "x--y"),
]


@pytest.mark.unit
@pytest.mark.parametrize("display,directory", DIRECTORIES, ids=[repr(d) for d, _ in DIRECTORIES])
def test_a_testing_projects_directory_keeps_the_two_hyphens(unit, display, directory):
    from app.core.paths import project_dir_name
    from app.models.project import Project
    project = unit.project(display)
    assert project["name"] == display
    stored = _row(unit.db, Project, project["id"]).name
    assert stored == TP + display and project_dir_name(stored) == directory


@pytest.mark.unit
def test_two_display_names_that_fold_to_one_directory_are_refused_the_second_time(unit):
    unit.project(" x")
    _code(unit.post("testing", "/projects/", {"name": "_x"}), 409, "project_directory_taken")


@pytest.mark.unit
@pytest.mark.parametrize("display", ["", "...", "中文", "́"])
def test_a_display_name_that_folds_to_nothing_is_refused(unit, display):
    before = unit.snapshot()
    _code(unit.post("testing", "/projects/", {"name": display}), 422, "project_name_invalid")
    assert unit.snapshot() == before


@pytest.mark.unit
def test_a_testing_projects_display_name_takes_the_longer_prefix_into_account(unit):
    from app.core.testing_space import display_name_max_length
    from app.models.project import Project
    longest = Project.__table__.c.name.type.length - len(TP)
    assert display_name_max_length() == longest == 239
    assert unit.project("z" * longest)["name"] == "z" * longest
    assert unit.post("testing", "/projects/", {"name": "w" * (longest + 1)}).status_code == 422
