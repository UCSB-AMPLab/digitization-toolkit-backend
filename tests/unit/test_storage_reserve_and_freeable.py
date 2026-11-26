"""Freeing space by operators, the list of what can be freed, and the storage reserve (NEH-380).

Operators free; administrators restore. The dry run, the free and the job
status admit the administrator and the operator; restore stays the
administrator's, and an operator cannot read a restore job. The reviewer and
the testing account are refused all of them.

GET /system/storage/freeable lists, for the administrator and the operator,
every real volume freeing is on offer for (every object verified offsite and
some verified file still on the card; never a testing volume, never one
Captua keeps on the unit), largest first, with the files and bytes freeing
would remove, from the database only, in a fixed number of queries.

CAPTUA_STORAGE_RESERVE_GB (default 2, 0 for none) is the free space Captua
keeps for its own work on the file system that holds the active projects
root. GET /system/storage reports it, with the free space left above it.
Below it, a capture or an image upload by any caller but the testing
account is refused with a 507 `storage_reserve` before the camera is
touched or anything written; the testing account keeps its own reserve
(`testing_reserve`), which sits above this one.

    python -m pytest tests/unit/test_storage_reserve_and_freeable.py

Version: 0.3.0
"""

from datetime import datetime, timezone

import pytest

from tests.unit.test_image_upload import _run, remote  # noqa: F401  (remote is a fixture)
from tests.unit.test_offsite_free import _as, _free, _paths, _received, read_remote  # noqa: F401
from tests.unit.test_testing_space_capture import (  # noqa: F401  (backend, lookups and unit are fixtures)
    TP, backend, lookups, unit,
)
from tests.unit.test_testing_space_reserve import GB, Disk, _set_reserve, disk  # noqa: F401  (disk is a fixture)


def _wait(client, vol, job_id, timeout=30):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/collections/{vol.id}/offsite/jobs/{job_id}").json()
        if body["state"] in ("done", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


# ---------------------------------------------------------------------------
# Part 1: operators free, administrators restore
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_operator_may_plan_free_and_read_the_job(remote, client, db_session, override_projects_root):
    from app.models.system_log import SystemLog

    _as("operator")
    vol, _, _ = _received(db_session, override_projects_root)

    plan = client.get(f"/collections/{vol.id}/offsite/free")
    assert plan.status_code == 200, plan.text
    assert plan.json()["would_free"]["files"] == 3

    started = client.post(f"/collections/{vol.id}/offsite/free")
    assert started.status_code == 202, started.text
    body = _wait(client, vol, started.json()["job_id"])
    assert body["state"] == "done" and body["result"]["freed"]["files"] == 3, body
    assert not _paths(override_projects_root, "f2.jpg")[0].exists()

    db_session.expire_all()
    logged = db_session.query(SystemLog).filter(SystemLog.action == "volume_freed").all()
    assert [r.actor for r in logged] == ["operator"]


@pytest.mark.unit
def test_an_operator_is_refused_restore_and_a_restore_jobs_status(read_remote, client, db_session,
                                                                    override_projects_root):
    vol, _, _ = _received(db_session, override_projects_root)
    _free(db_session, vol)

    _as("operator")
    assert client.post(f"/collections/{vol.id}/offsite/restore").status_code == 403
    assert not _paths(override_projects_root, "f2.jpg")[0].exists()

    _as("admin")
    started = client.post(f"/collections/{vol.id}/offsite/restore")
    assert started.status_code == 202, started.text
    job_id = started.json()["job_id"]
    assert _wait(client, vol, job_id)["state"] == "done"

    _as("operator")
    refused = client.get(f"/collections/{vol.id}/offsite/jobs/{job_id}")
    assert refused.status_code == 403, refused.text
    assert "result" not in refused.json()


@pytest.mark.unit
def test_the_reviewer_is_refused_every_offsite_route(remote, client, db_session, override_projects_root):
    vol, _, _ = _received(db_session, override_projects_root)
    _as("reviewer")
    assert client.get(f"/collections/{vol.id}/offsite/free").status_code == 403
    assert client.post(f"/collections/{vol.id}/offsite/free").status_code == 403
    assert client.post(f"/collections/{vol.id}/offsite/restore").status_code == 403
    assert client.get(f"/collections/{vol.id}/offsite/jobs/no-such-job").status_code == 403
    assert client.get("/system/storage/freeable").status_code == 403
    assert _paths(override_projects_root, "f2.jpg")[0].exists()


@pytest.mark.unit
def test_the_testing_account_is_refused_every_offsite_route_and_the_list(unit):
    vol = unit.real_volume.id
    for method, path in (("GET", f"/collections/{vol}/offsite/free"),
                         ("POST", f"/collections/{vol}/offsite/free"),
                         ("POST", f"/collections/{vol}/offsite/restore"),
                         ("GET", f"/collections/{vol}/offsite/jobs/no-such-job"),
                         ("GET", "/system/storage/freeable")):
        resp = unit.client.request(method, path, headers=unit.headers["testing"])
        assert resp.status_code == 403, (method, path, resp.text)


# ---------------------------------------------------------------------------
# Part 2: the list of what can be freed
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _project(db, name="P", testing=False):
    from app.models.project import Project

    project = db.query(Project).filter(Project.name == name).one_or_none()
    if project is None:
        project = Project(name=name, testing=testing)
        db.add(project)
        db.commit()
    return project


def _vol(db, name, stem, *, kept=False, testing=False):
    from app.models.collection import Collection

    project = _project(db, TP + "Pruebas", testing=True) if testing else _project(db)
    vol = Collection(name=name, project_id=project.id, filename_stem=stem, kept_on_unit=kept,
                     testing=testing)
    db.add(vol)
    db.commit()
    return vol


def _img(db, vol, name, size, *, verified=True, preview=None, freed=False, preview_freed=False):
    """A record with one image and its offsite rows. `preview` is the preview row's state: None for none,
    True for verified, False for pending. Nothing is written to disk: the list reads the database only."""
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage

    rec = Record(title=name, collection_id=vol.id, capture_mode="single")
    db.add(rec)
    db.commit()
    stem = vol.filename_stem or "none"
    img = RecordImage(record_id=rec.id, filename=name, file_path=f"/nowhere/{stem}/captures/{name}",
                      format=name.rsplit(".", 1)[1].upper(), file_size=size,
                      master_freed_at=_NOW if freed else None, master_freed_bytes=size if freed else None,
                      preview_freed_at=_NOW if preview_freed else None,
                      preview_freed_bytes=7 if preview_freed else None)
    db.add(img)
    db.commit()
    db.add(OffsiteObject(record_image_id=img.id, kind="capture", object_key=f"t/{stem}/captures/{name}",
                         local_sha1="a" * 40, verified_at=_NOW if verified else None))
    if preview is not None:
        db.add(OffsiteObject(record_image_id=img.id, kind="preview",
                             object_key=f"t/{stem}/previews/{name}_preview.jpg",
                             local_sha1="b" * 40, verified_at=_NOW if preview else None))
    db.commit()
    return img


def _world(db):
    """Volumes of every kind the list must include or leave out."""
    a = _vol(db, "Tomo A", "xx-a")
    _img(db, a, "a1.jpg", 100)
    _img(db, a, "a2.jpg", 300)
    b = _vol(db, "Tomo B", "xx-b")
    _img(db, b, "b1.CR2", 1000, preview=True)
    kept = _vol(db, "Guardado", "xx-kept", kept=True)
    _img(db, kept, "k1.jpg", 5000)
    testing = _vol(db, "captua-testing-000001", "captua-testing-000001", kept=True, testing=True)
    _img(db, testing, "t1.jpg", 6000)
    pending = _vol(db, "Pendiente", "xx-pending")
    _img(db, pending, "p1.jpg", 7000)
    _img(db, pending, "p2.jpg", 7000, verified=False)
    freed = _vol(db, "Liberado", "xx-freed")
    _img(db, freed, "f1.jpg", 8000, freed=True)
    _img(db, freed, "f2.CR2", 8000, preview=True, freed=True, preview_freed=True)
    half = _vol(db, "Medio", "xx-half")
    _img(db, half, "h1.jpg", 9000, freed=True)
    _img(db, half, "h2.jpg", 50)
    no_stem = _vol(db, "Sin raíz", None)
    _img(db, no_stem, "n1.jpg", 9999)
    return a, b, half


@pytest.mark.unit
def test_the_list_holds_only_the_volumes_freeing_is_on_offer_for_largest_first(db_session):
    from app.core.offsite_free import freeable_volumes

    a, b, half = _world(db_session)

    listed = freeable_volumes(db_session)

    assert listed["volumes"] == [
        {"collection_id": b.id, "project_id": b.project_id, "name": "Tomo B", "filename_stem": "xx-b",
         "files": 2, "bytes": 1000, "unmeasured_files": 1},
        {"collection_id": a.id, "project_id": a.project_id, "name": "Tomo A", "filename_stem": "xx-a",
         "files": 2, "bytes": 400, "unmeasured_files": 0},
        {"collection_id": half.id, "project_id": half.project_id, "name": "Medio", "filename_stem": "xx-half",
         "files": 1, "bytes": 50, "unmeasured_files": 0},
    ]
    assert listed["total"] == {"volumes": 3, "files": 5, "bytes": 1450, "unmeasured_files": 1}


@pytest.mark.unit
def test_a_master_with_no_recorded_size_is_counted_as_unmeasured(db_session):
    from app.core.offsite_free import freeable_volumes

    vol = _vol(db_session, "Tomo", "xx-c")
    _img(db_session, vol, "c1.jpg", None)
    _img(db_session, vol, "c2.jpg", 20)

    row, = freeable_volumes(db_session)["volumes"]
    assert (row["files"], row["bytes"], row["unmeasured_files"]) == (2, 20, 1)


@pytest.mark.unit
def test_the_list_is_empty_on_a_unit_with_nothing_to_free(db_session):
    from app.core.offsite_free import freeable_volumes

    listed = freeable_volumes(db_session)
    assert (listed["volumes"], listed["total"]) == ([], {"volumes": 0, "files": 0, "bytes": 0, "unmeasured_files": 0})


class _Statements:
    """Every SQL statement the engine runs while it is listening."""

    def __init__(self, engine):
        self.engine = engine
        self.seen = []

    def _on(self, conn, cursor, statement, parameters, context, executemany):
        self.seen.append(" ".join(statement.split()))

    def __enter__(self):
        from sqlalchemy import event
        event.listen(self.engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc):
        from sqlalchemy import event
        event.remove(self.engine, "before_cursor_execute", self._on)


@pytest.mark.unit
def test_the_list_takes_the_same_few_queries_however_many_volumes_and_objects(db_session, tmp_path):
    from app.core.offsite_free import freeable_volumes

    _world(db_session)
    engine = db_session.get_bind()
    with _Statements(engine) as small:
        freeable_volumes(db_session)
    selects = [s for s in small.seen if s.upper().startswith("SELECT")]

    for n in range(30):
        vol = _vol(db_session, f"Más {n}", f"xx-more-{n}")
        for i in range(25):
            _img(db_session, vol, f"m{n}-{i}.CR2", 10 + i, preview=True)
    with _Statements(engine) as large:
        listed = freeable_volumes(db_session)

    assert len(listed["volumes"]) == 33
    assert len(large.seen) == len(small.seen)
    # _object_counts (images, their offsite rows, the volumes), the aggregate, and the unit mode.
    assert len(selects) <= 5, selects
    (tmp_path / "statements.sql").write_text(";\n\n".join(large.seen) + ";\n")
    print("\n-- the statements freeable_volumes runs --\n" + ";\n\n".join(large.seen) + ";")


@pytest.mark.unit
def test_the_route_answers_the_administrator_and_the_operator(client, db_session):
    from tests.unit.test_unit_mode import _unit

    a, b, half = _world(db_session)
    for mode, allows in (("backup", True), ("offline", False)):
        _unit(db_session, mode)
        for role in ("admin", "operator"):
            _as(role)
            resp = client.get("/system/storage/freeable")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert [v["collection_id"] for v in body["volumes"]] == [b.id, a.id, half.id]
            assert body["total"]["bytes"] == 1450
            assert body["mode_allows"] is allows


# ---------------------------------------------------------------------------
# Part 3: the reserve in the storage figures
# ---------------------------------------------------------------------------

@pytest.fixture
def reserve(monkeypatch):
    from app.core.config import settings

    def set_to(gb):
        monkeypatch.setattr(settings, "STORAGE_RESERVE_GB", gb)
    return set_to


@pytest.mark.unit
def test_the_reserve_setting_defaults_to_2_gb_and_takes_0(monkeypatch):
    from pydantic import ValidationError
    from app.core.config import Settings

    assert Settings(_env_file=()).STORAGE_RESERVE_GB == 2
    monkeypatch.setenv("CAPTUA_STORAGE_RESERVE_GB", "0")
    assert Settings(_env_file=()).STORAGE_RESERVE_GB == 0
    monkeypatch.setenv("CAPTUA_STORAGE_RESERVE_GB", "15")
    assert Settings(_env_file=()).STORAGE_RESERVE_GB == 15
    monkeypatch.setenv("CAPTUA_STORAGE_RESERVE_GB", "-1")
    with pytest.raises(ValidationError):
        Settings(_env_file=())


@pytest.mark.unit
@pytest.mark.parametrize("gb,free,usable", [
    (2, 10 * GB, 8 * GB), (2, 2 * GB, 0), (2, GB, 0), (0, GB, GB), (5, 6 * GB, GB),
])
def test_storage_reports_the_reserve_and_the_free_space_above_it(client, db_session, disk, reserve, gb, free, usable):
    reserve(gb)
    disk.free = free
    for role in ("admin", "operator", "reviewer"):
        _as(role)
        body = client.get("/system/storage").json()
        assert body["free_bytes"] == free and body["used_bytes"] == 10 ** 13 - free
        assert body["reserve_bytes"] == gb * GB
        assert body["usable_free_bytes"] == usable


@pytest.mark.unit
def test_storage_that_cannot_be_read_reports_the_reserve_and_no_usable_space(client, db_session, disk, reserve):
    reserve(3)
    disk.free = None
    _as("admin")
    body = client.get("/system/storage").json()
    assert body["available"] is False
    assert (body["reserve_bytes"], body["usable_free_bytes"]) == (3 * GB, 0)


# ---------------------------------------------------------------------------
# Part 4: captures refused before the reserve is used
# ---------------------------------------------------------------------------

def _route(unit, caller, name):
    project, volume = unit.real_project.name, unit.real_volume.id
    if name == "capture":
        return unit.post(caller, "/cameras/capture", {"project_name": project, "collection_id": volume,
                                                       "camera_index": 0})
    if name == "dual":
        return unit.post(caller, "/cameras/capture/dual", {"project_name": project, "collection_id": volume})
    if name == "upload":
        return unit.client.post(f"/records/{unit.real_record_id}/images", headers=unit.headers[caller],
                                files={"file": ("p.jpg", b"\xff\xd8upload", "image/jpeg")})
    raise AssertionError(name)


def _refused(resp, free_gb, reserve_gb):
    assert resp.status_code == 507, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "storage_reserve", detail
    assert (detail["free_gb"], detail["reserve_gb"]) == (free_gb, reserve_gb)
    assert detail["message"] and not any(ch.isdigit() for ch in detail["message"])


ROUTES = ["capture", "dual", "upload"]
REAL_CALLERS = ["admin", "operator"]


@pytest.mark.unit
@pytest.mark.parametrize("name", ROUTES)
@pytest.mark.parametrize("caller", REAL_CALLERS)
def test_below_the_reserve_a_real_capture_is_refused_before_anything_is_touched(unit, disk, reserve, name, caller):
    reserve(2)
    disk.free = 2 * GB - 1
    requested = list(unit.backend.requested)
    before = unit.snapshot()
    _refused(_route(unit, caller, name), 1.9, 2)
    assert unit.backend.requested == requested
    assert unit.snapshot() == before


@pytest.mark.unit
@pytest.mark.parametrize("name", ROUTES)
@pytest.mark.parametrize("caller", REAL_CALLERS)
@pytest.mark.parametrize("free", [2 * GB, 2 * GB + 1, 500 * GB], ids=["at", "just-above", "above"])
def test_at_or_above_the_reserve_a_real_capture_goes_ahead(unit, disk, reserve, name, caller, free):
    reserve(2)
    disk.free = free
    resp = _route(unit, caller, name)
    assert resp.status_code == 200, resp.text
    if name != "upload":
        assert resp.json()["success"], resp.text


@pytest.mark.unit
@pytest.mark.parametrize("name", ROUTES)
def test_free_space_that_cannot_be_read_does_not_refuse_a_real_capture(unit, disk, reserve, name):
    reserve(2)
    disk.free = None
    requested = list(unit.backend.requested)
    resp = _route(unit, "operator", name)
    assert resp.status_code == 200, resp.text
    if name != "upload":
        assert resp.json()["success"], resp.text
        # The camera was asked: nothing stopped the capture.
        assert len(unit.backend.requested) > len(requested)


@pytest.mark.unit
@pytest.mark.parametrize("name", ROUTES)
def test_free_space_that_cannot_be_read_still_refuses_the_testing_account(unit, disk, reserve, name):
    project, volume, record_id = _testing_target(unit)
    reserve(2)
    disk.free = None
    requested = list(unit.backend.requested)
    before = unit.snapshot()
    resp = _testing_route(unit, name, project, volume, record_id)
    assert resp.status_code == 507 and resp.json()["detail"]["code"] == "testing_reserve", resp.text
    assert resp.json()["detail"]["free_gb"] is None
    # The camera was not asked.
    assert unit.backend.requested == requested
    assert unit.snapshot() == before


@pytest.mark.unit
@pytest.mark.parametrize("name", ROUTES)
@pytest.mark.parametrize("free", [0, None], ids=["full", "unreadable"])
def test_a_reserve_of_0_refuses_nothing(unit, disk, reserve, name, free):
    reserve(0)
    disk.free = free
    resp = _route(unit, "operator", name)
    assert resp.status_code == 200, resp.text


@pytest.mark.unit
def test_creating_a_volume_is_not_refused_below_the_reserve(unit, disk, reserve):
    """Regression guard: the storage reserve refuses captures and uploads only, never a new volume."""
    reserve(2)
    disk.free = 0
    resp = unit.post("admin", "/collections/", {"name": "Real 2", "project_id": unit.real_project.id,
                                                "filename_stem": "xx-real-2"})
    assert resp.status_code == 201, resp.text


def _testing_target(unit):
    project = unit.project()
    volume = unit.volume(project_id=project["id"])
    resp = unit.post("testing", "/records/", {"title": "Hoja", "capture_mode": "single",
                                              "collection_id": volume["id"]})
    assert resp.status_code == 200, resp.text
    return project, volume, resp.json()["id"]


def _testing_route(unit, name, project, volume, record_id):
    if name == "capture":
        return unit.post("testing", "/cameras/capture", {"project_name": project["name"],
                                                         "collection_id": volume["id"], "camera_index": 0})
    if name == "dual":
        return unit.post("testing", "/cameras/capture/dual", {"project_name": project["name"],
                                                              "collection_id": volume["id"]})
    return unit.client.post(f"/records/{record_id}/images", headers=unit.headers["testing"],
                            files={"file": ("p.jpg", b"\xff\xd8upload", "image/jpeg")})


@pytest.mark.unit
@pytest.mark.parametrize("name", ROUTES)
def test_the_testing_account_is_refused_below_the_larger_of_the_two_reserves(unit, disk, reserve, name):
    project, volume, record_id = _testing_target(unit)
    _set_reserve(unit.db, 1)
    reserve(2)

    # Above the testing reserve but below the storage reserve: refused with its own code and the larger figure.
    disk.free = GB + GB // 2
    requested = list(unit.backend.requested)
    resp = _testing_route(unit, name, project, volume, record_id)
    assert resp.status_code == 507, resp.text
    detail = resp.json()["detail"]
    assert (detail["code"], detail["reserve_gb"], detail["free_gb"]) == ("testing_reserve", 2, 1.5)
    assert unit.backend.requested == requested

    # At the larger figure: goes ahead.
    disk.free = 2 * GB
    resp = _testing_route(unit, name, project, volume, record_id)
    assert resp.status_code == 200, resp.text

    # A testing reserve above the storage reserve is the larger figure.
    _set_reserve(unit.db, 30)
    disk.free = 29 * GB
    resp = _testing_route(unit, name, project, volume, record_id)
    assert resp.status_code == 507 and resp.json()["detail"]["reserve_gb"] == 30, resp.text


@pytest.mark.unit
@pytest.mark.parametrize("name", ROUTES)
def test_an_unusable_drive_is_answered_as_before_not_by_the_reserve(unit, reserve, monkeypatch, name):
    """Regression guard: with an override drive that is not usable, each route answers as it did before the
    reserve: the upload with the 503 of StorageUnavailableError, a capture with success false and the drive's
    problem, the camera not asked either way."""
    from app.core import storage_override

    reserve(2)
    problem = "The storage drive set for projects is not available."
    monkeypatch.setattr(storage_override, "get_storage_override_or_fallback", lambda: ("/nowhere/drive", False))
    monkeypatch.setattr(storage_override, "override_root_problem", lambda root: problem)
    requested = list(unit.backend.requested)
    before = unit.snapshot()
    resp = _route(unit, "operator", name)
    if name == "upload":
        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"] == problem
    else:
        assert resp.status_code == 200, resp.text
        assert (resp.json()["success"], resp.json()["error"]) == (False, problem)
    assert unit.backend.requested == requested
    assert unit.snapshot() == before


# The refusals a capture had before the reserve come first: below the reserve, each still answers as it did.

def _no_stem_volume(unit):
    from app.models.collection import Collection
    vol = Collection(name="Sin raíz", project_id=unit.real_project.id, filename_stem=None)
    unit.db.add(vol)
    unit.db.commit()
    return vol.id


@pytest.mark.unit
@pytest.mark.parametrize("dual", [False, True], ids=["capture", "dual"])
@pytest.mark.parametrize("case,status", [("missing-record", 404), ("no-stem", 409), ("wrong-mode", 422)])
def test_below_the_reserve_the_earlier_refusals_still_come_first(unit, disk, reserve, dual, case, status):
    reserve(2)
    disk.free = 0
    path = "/cameras/capture/dual" if dual else "/cameras/capture"
    body = {"project_name": unit.real_project.name, "collection_id": unit.real_volume.id}
    if not dual:
        body["camera_index"] = 0
    if case == "missing-record":
        body["record_id"] = 999999
    elif case == "no-stem":
        body["collection_id"] = _no_stem_volume(unit)
    elif dual:
        # afterRecordId inserts a record; it cannot be combined with record_id.
        body.update(record_id=unit.real_record_id, afterRecordId=unit.real_record_id)
    else:
        # The real record was captured with two cameras.
        body["record_id"] = unit.real_record_id
    requested = list(unit.backend.requested)
    resp = unit.post("operator", path, body)
    assert resp.status_code == status, resp.text
    assert unit.backend.requested == requested


# ---------------------------------------------------------------------------
# The list agrees with the free: the volume's own folder, and no probe of excluded volumes
# ---------------------------------------------------------------------------

def _img_at(db, vol, path, size):
    """A JPEG image at `path` with its capture verified offsite."""
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage

    rec = Record(title=str(path), collection_id=vol.id, capture_mode="single")
    db.add(rec)
    db.commit()
    img = RecordImage(record_id=rec.id, filename=path.name, file_path=str(path), format="JPG", file_size=size)
    db.add(img)
    db.commit()
    db.add(OffsiteObject(record_image_id=img.id, kind="capture", object_key=f"t/{vol.filename_stem}/{path.name}",
                         local_sha1="c" * 40, verified_at=_NOW))
    db.commit()
    return img


@pytest.mark.unit
def test_a_volume_with_an_image_outside_its_own_folder_is_not_listed(db_session, override_projects_root):
    from app.core.offsite_free import FreeRefused, NOT_IN_OWN_FOLDER_MESSAGE, freeable_volumes, plan_free

    root = override_projects_root
    own = _vol(db_session, "Propio", "xx-own")
    _img_at(db_session, own, root / "xx-own" / "captures" / "o1.jpg", 10)
    legacy = _vol(db_session, "Antiguo", "xx-legacy")
    _img_at(db_session, legacy, root / "xx-legacy" / "captures" / "l1.jpg", 10)
    _img_at(db_session, legacy, root / "P" / "antiguo" / "images" / "main" / "l2.jpg", 20)
    # A path that begins with the stem but is another folder is outside the volume's own folder too.
    prefix = _vol(db_session, "Prefijo", "xx-pre")
    _img_at(db_session, prefix, root / "xx-prefix" / "captures" / "p1.jpg", 10)

    assert [v["name"] for v in freeable_volumes(db_session)["volumes"]] == ["Propio"]
    # The free refuses both for the same reason.
    for vol in (legacy, prefix):
        with pytest.raises(FreeRefused) as refused:
            plan_free(db_session, vol.id)
        assert refused.value.message == NOT_IN_OWN_FOLDER_MESSAGE


@pytest.mark.unit
def test_the_list_probes_no_file_of_a_testing_or_kept_volume(db_session, monkeypatch):
    import app.core.image_upload as image_upload
    from app.core.offsite_free import freeable_volumes

    kept = _vol(db_session, "Guardado", "xx-kept")
    kept.kept_on_unit = True
    db_session.commit()
    testing = _vol(db_session, "captua-testing-000002", "captua-testing-000002", kept=True, testing=True)
    real = _vol(db_session, "Real", "xx-real")
    for vol, name in ((kept, "k1.CR2"), (testing, "t1.CR2"), (real, "r1.CR2")):
        _img(db_session, vol, name, 100)  # RAW, no preview row: the shared rule looks for its sidecar

    probed = []
    real_local_file = image_upload.local_file

    def recording(kind, file_path):
        probed.append(file_path)
        return real_local_file(kind, file_path)

    monkeypatch.setattr(image_upload, "local_file", recording)
    listed = freeable_volumes(db_session)

    # The list reads the database only: no file is looked for, and a RAW image
    # with no preview row keeps its volume off the list.
    assert listed["volumes"] == []
    assert probed == [], probed


@pytest.mark.unit
def test_restorable_is_whether_a_read_remote_is_configured(db_session, monkeypatch):
    from app.core import config
    from app.core.offsite_free import freeable_volumes, restore_available

    monkeypatch.setattr(config.settings, "OFFSITE_READ_REMOTE", "")
    assert restore_available() is False
    assert freeable_volumes(db_session)["restorable"] is False
    monkeypatch.setattr(config.settings, "OFFSITE_READ_REMOTE", "   ")
    assert restore_available() is False
    monkeypatch.setattr(config.settings, "OFFSITE_READ_REMOTE", "b2read:bucket")
    assert restore_available() is True
    assert freeable_volumes(db_session)["restorable"] is True


@pytest.mark.unit
def test_the_plan_and_the_free_say_whether_the_files_can_be_restored(remote, client, db_session,
                                                                    override_projects_root, monkeypatch):
    from app.core import config
    from app.core.offsite_free import FREED_FILE_MESSAGE, FREED_FILE_NOT_RESTORABLE_MESSAGE

    _as("operator")
    vol, raw, jpg = _received(db_session, override_projects_root)
    monkeypatch.setattr(config.settings, "OFFSITE_READ_REMOTE", "")
    assert client.get(f"/collections/{vol.id}/offsite/free").json()["restorable"] is False
    monkeypatch.setattr(config.settings, "OFFSITE_READ_REMOTE", str(remote.root))
    assert client.get(f"/collections/{vol.id}/offsite/free").json()["restorable"] is True

    monkeypatch.setattr(config.settings, "OFFSITE_READ_REMOTE", "")
    started = client.post(f"/collections/{vol.id}/offsite/free")
    assert started.status_code == 202, started.text
    body = _wait(client, vol, started.json()["job_id"])
    assert body["state"] == "done" and body["result"]["restorable"] is False, body

    # The freed file's 410 says the truth either way.
    resp = client.get(f"/records/images/{jpg.id}/file")
    assert resp.status_code == 410
    detail = resp.json()["detail"]
    assert (detail["message"], detail["restorable"]) == (FREED_FILE_NOT_RESTORABLE_MESSAGE, False)
    assert "restore" not in detail["message"].lower() or "cannot" in detail["message"].lower()
    monkeypatch.setattr(config.settings, "OFFSITE_READ_REMOTE", str(remote.root))
    detail = client.get(f"/records/images/{jpg.id}/file").json()["detail"]
    assert (detail["message"], detail["restorable"]) == (FREED_FILE_MESSAGE, True)


# ---------------------------------------------------------------------------
# The export refusal for freed files says whether a restore can bring them back
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("read_remote_set", [True, False], ids=["restorable", "not-restorable"])
def test_the_export_refusal_for_freed_files_carries_restorable(remote, client, db_session, override_projects_root,
                                                              monkeypatch, read_remote_set):
    from app.core import config
    from app.core.export_jobs import ExportError, ExportJob
    from app.core.export_service import _do_export
    from app.core.offsite_free import FREED_EXPORT_MESSAGE
    from tests.unit.test_offsite_free_routes import _approve

    _as("admin")
    vol, _, _ = _received(db_session, override_projects_root)
    _approve(db_session, vol)
    _free(db_session, vol)
    monkeypatch.setattr(config.settings, "OFFSITE_READ_REMOTE", str(remote.root) if read_remote_set else "")

    r = client.post(f"/collections/{vol.id}/export")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    # The text is unchanged: the interface matches it.
    assert detail["message"] == FREED_EXPORT_MESSAGE.format(n=2)
    assert detail["restorable"] is read_remote_set

    with pytest.raises(ExportError) as exc:
        _do_export(db_session, ExportJob(vol.id), vol.id)
    assert str(exc.value) == FREED_EXPORT_MESSAGE.format(n=2)
    assert exc.value.detail["reason"] == "freed_files"
    assert exc.value.detail["restorable"] is read_remote_set


@pytest.mark.unit
def test_the_volume_offsite_state_says_whether_a_restore_is_possible(client, db_session, monkeypatch):
    from app.core import config

    vol = _vol(db_session, "Tomo", "xx-state")
    _as("reviewer")
    monkeypatch.setattr(config.settings, "OFFSITE_READ_REMOTE", "")
    assert client.get(f"/collections/{vol.id}/offsite").json()["restorable"] is False
    monkeypatch.setattr(config.settings, "OFFSITE_READ_REMOTE", "b2read:bucket")
    assert client.get(f"/collections/{vol.id}/offsite").json()["restorable"] is True


# ---------------------------------------------------------------------------
# Round 4: the reserve is measured on the destination; one layout predicate
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("name", ROUTES)
def test_the_reserve_is_measured_on_the_destination_not_the_active_root(unit, reserve, monkeypatch, name):
    """The destination's drive (A) has 1 GB free; the active root is read as another drive (B) with 50 GB,
    as after an administrator activates B once the destination is retained. The check measures A."""
    import shutil
    from collections import namedtuple
    from app.core import storage_override

    Usage = namedtuple("Usage", "total used free")
    asked = []

    def usage(path):
        asked.append(str(path))
        return Usage(total=10 ** 12, used=10 ** 12 - GB, free=GB)

    reserve(2)
    monkeypatch.setattr(shutil, "disk_usage", usage)
    monkeypatch.setattr(storage_override, "projects_root_usage",
                        lambda settings=None: (Usage(total=10 ** 12, used=0, free=50 * GB), None))
    requested = list(unit.backend.requested)
    resp = _route(unit, "operator", name)
    _refused(resp, 1.0, 2)
    assert unit.backend.requested == requested
    root = str(unit.root)
    assert asked and all(p.startswith(root) for p in asked), asked


@pytest.mark.unit
def test_a_non_canonical_root_and_its_resolved_spelling_are_one_root(db_session, override_projects_root, monkeypatch):
    """Configured `<root>/cache/..`: an image stored under that spelling in `<stem>/` is in its own folder,
    as the free reads it; one outside `<stem>/` under both spellings is not. The list and the free agree."""
    from app.core import storage_override
    from app.core.offsite_free import FreeRefused, NOT_IN_OWN_FOLDER_MESSAGE, _stem_volume, freeable_volumes

    root = override_projects_root
    (root / "cache").mkdir()
    spelled = f"{root}/cache/.."
    monkeypatch.setattr(storage_override, "get_storage_override_or_fallback", lambda: (spelled, False))
    from pathlib import Path as _P

    own = _vol(db_session, "Propio", "xx-own")
    _img_at(db_session, own, _P(f"{spelled}/xx-own/captures/o1.jpg"), 10)
    resolved_own = _vol(db_session, "Propio resuelto", "xx-own2")
    _img_at(db_session, resolved_own, root / "xx-own2" / "captures" / "o2.jpg", 10)
    legacy = _vol(db_session, "Antiguo", "xx-legacy")
    _img_at(db_session, legacy, _P(f"{spelled}/P/antiguo/images/main/l1.jpg"), 20)

    listed = {v["name"] for v in freeable_volumes(db_session)["volumes"]}
    assert listed == {"Propio", "Propio resuelto"}
    for vol in (own, resolved_own):
        _stem_volume(db_session, vol.id)  # the free's layout gate passes
    with pytest.raises(FreeRefused) as refused:
        _stem_volume(db_session, legacy.id)
    assert refused.value.message == NOT_IN_OWN_FOLDER_MESSAGE


# ---------------------------------------------------------------------------
# Round 5: the list reads the database only; it reports the free jobs in progress
# ---------------------------------------------------------------------------

def _raw_without_preview_row(db, root, vol, name, *, sidecar: bool):
    """A current RAW image under `<root>/<stem>/captures/`, capture verified, no preview row; its sidecar on disk or not."""
    img = _img_at(db, vol, root / vol.filename_stem / "captures" / name, 100)
    if sidecar:
        from app.core.paths import preview_sidecar_of
        side = preview_sidecar_of(root / vol.filename_stem / "captures" / name)
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_bytes(b"preview")
    return img


@pytest.mark.unit
@pytest.mark.parametrize("sidecar", [False, True], ids=["no-sidecar", "sidecar-on-disk"])
def test_a_volume_with_a_raw_image_and_no_preview_row_is_not_listed(db_session, override_projects_root, sidecar):
    from app.core.offsite_free import freeable_volumes

    root = override_projects_root
    jpg = _vol(db_session, "Con JPEG", "xx-jpg")
    _img_at(db_session, jpg, root / "xx-jpg" / "captures" / "j1.jpg", 10)
    raw = _vol(db_session, "Con RAW", "xx-raw")
    _raw_without_preview_row(db_session, root, raw, "r1.CR2", sidecar=sidecar)

    assert [v["name"] for v in freeable_volumes(db_session)["volumes"]] == ["Con JPEG"]


@pytest.mark.unit
def test_the_list_route_touches_no_file_under_the_projects_root(client, db_session, override_projects_root, monkeypatch):
    import builtins
    import os
    import pathlib

    root = str(override_projects_root.resolve())
    raw = _vol(db_session, "Con RAW", "xx-raw")
    _raw_without_preview_row(db_session, override_projects_root, raw, "r1.CR2", sidecar=True)
    other = _vol(db_session, "Otro RAW", "xx-raw2")
    _raw_without_preview_row(db_session, override_projects_root, other, "r2.CR2", sidecar=False)
    jpg = _vol(db_session, "Con JPEG", "xx-jpg")
    _img_at(db_session, jpg, override_projects_root / "xx-jpg" / "captures" / "j1.jpg", 10)

    touched = []

    # Below the root: a file or folder of a volume. The root itself is read once per
    # request by Path.resolve, to spell it as resolved (the layout predicate).
    def under_root(p):
        try:
            path = os.path.abspath(os.fspath(p))
        except TypeError:
            return False
        return path.startswith(root + os.sep) or path.startswith(str(override_projects_root) + os.sep)

    def spy(name, real):
        def wrapper(target, *a, **k):
            if under_root(target):
                touched.append((name, os.fspath(target)))
            return real(target, *a, **k)
        return wrapper

    real_path_stat = pathlib.Path.stat
    monkeypatch.setattr(pathlib.Path, "stat", lambda self, *a, **k: (touched.append(("Path.stat", str(self)))
                                                                    if under_root(self) else None) or real_path_stat(self, *a, **k))
    monkeypatch.setattr(os, "stat", spy("os.stat", os.stat))
    monkeypatch.setattr(os.path, "exists", spy("os.path.exists", os.path.exists))
    monkeypatch.setattr(os.path, "isfile", spy("os.path.isfile", os.path.isfile))
    monkeypatch.setattr(builtins, "open", spy("open", builtins.open))

    _as("operator")
    first = client.get("/system/storage/freeable")
    second = client.get("/system/storage/freeable")
    assert first.status_code == 200, first.text
    assert touched == [], touched
    assert [v["name"] for v in first.json()["volumes"]] == ["Con JPEG"]
    assert first.json() == second.json()


@pytest.mark.unit
def test_the_list_reports_the_free_jobs_in_progress_and_no_restore_job(client, db_session):
    from app.core.offsite_free import offsite_jobs

    a = _vol(db_session, "Tomo A", "xx-ja")
    b = _vol(db_session, "Tomo B", "xx-jb")
    c = _vol(db_session, "Tomo C", "xx-jc")
    running = offsite_jobs.reserve(a.id, "free")
    running.state = "running"
    queued = offsite_jobs.reserve(b.id, "free")
    restore = offsite_jobs.reserve(c.id, "restore")
    done = offsite_jobs.reserve(_vol(db_session, "Tomo D", "xx-jd").id, "free")
    done.state = "done"
    done.result = {"freed": {"files": 1, "bytes": 1}}
    try:
        _as("operator")
        jobs = client.get("/system/storage/freeable").json()["jobs"]
        assert sorted(jobs, key=lambda j: j["collection_id"]) == [
            {"collection_id": a.id, "job_id": running.id, "state": "running"},
            {"collection_id": b.id, "job_id": queued.id, "state": "queued"},
        ]
    finally:
        for job in (running, queued, restore, done):
            offsite_jobs.discard(job)
