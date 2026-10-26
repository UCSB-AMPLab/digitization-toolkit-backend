"""The digitisation list import (exchange contract 1.1, artefact 1).

POST /imports/digitisation-list takes a list file and creates one volume per
item, in a project named after the basket; items that cannot be created are
reported as rejects and the rest are still created. The fixtures are the two
synthetic lists in tests/fixtures/ (tenant captua-test, codes xx-test-...).

Version: 0.1.0-beta
"""

import json
from pathlib import Path

import pytest


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
VALID = FIXTURES / "digitisation-list_captua-test_prueba-valida_2026-10-23.json"
REJECTS = FIXTURES / "digitisation-list_captua-test_prueba-rechazos_2026-10-23.json"
VALID_ID = "00000000-0000-4000-8000-000000000001"
REJECTS_ID = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def reports_root(tmp_path, monkeypatch):
    from app.core import config

    root = tmp_path / "data"
    monkeypatch.setattr(config.settings, "DATA_DIR", str(root))
    return root / "imports"


def _as(username, role):
    from app.main import app
    import app.api.imports as imports
    from app.models.user import User

    user = User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)
    for dep in (imports.allow_contributor, imports.allow_read_only):
        app.dependency_overrides[dep] = lambda u=user: u


def _post(client, payload, name="list.json"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return client.post("/imports/digitisation-list",
                       files={"file": (name, body, "application/json")})


def _post_fixture(client, path):
    return _post(client, path.read_bytes(), path.name)


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _volumes(db_session):
    from app.models.collection import Collection

    db_session.expire_all()
    return {c.external_id: c for c in db_session.query(Collection).all()}


def _reasons(report):
    return [(r["position"], r["referenceCode"], r["reason"]) for r in report["rejects"]]


# ---------------------------------------------------------------------------
# The valid list
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_valid_list_creates_four_volumes_in_a_project_named_after_the_basket(client, db_session, reports_root):
    from app.models.project import Project
    from app.models.record import Record

    _as("op", "admin")
    resp = _post_fixture(client, VALID)
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["created"] == 4
    assert report["skipped"] == 0
    assert report["rejects"] == []
    assert report["listId"] == VALID_ID
    assert report["projectCreated"] is True
    assert report["reportStored"] is True

    project = db_session.query(Project).filter(Project.name == "Prueba válida").one()
    assert report["projectId"] == project.id
    vols = _volumes(db_session)
    assert set(vols) == {"xx-test-001", "xx-test-002", "xx-test-003", "xx-test-largo-0000000000004"}
    assert all(v.project_id == project.id and v.parent_collection_id is None for v in vols.values())
    assert db_session.query(Record).count() == 0
    assert sorted(v["captuaCollectionId"] for v in report["volumes"]) == sorted(v.id for v in vols.values())


@pytest.mark.unit
def test_valid_list_stores_the_mapped_fields(client, db_session, reports_root):
    from datetime import date

    _as("op", "admin")
    assert _post_fixture(client, VALID).status_code == 200
    vols = _volumes(db_session)

    v1 = vols["xx-test-001"]
    assert v1.filename_stem == "xx-test-001"
    assert v1.name == "Volumen de prueba uno"
    assert v1.date == "1790–1795"
    assert v1.date_start == date(1790, 1, 1)
    assert v1.date_end == date(1795, 12, 31)
    assert v1.sequence == 1
    assert v1.created_by == "op"
    assert v1.capture_counter == 0

    v2 = vols["xx-test-002"]
    assert v2.name == "Volumen de prueba dos, con tildes y eñe (á, é, í, ó, ú, ü, ñ)"
    assert v2.date == "ca. 1801"
    assert v2.date_start == date(1801, 1, 1)
    assert v2.date_end is None
    assert v2.sequence == 2

    v3 = vols["xx-test-003"]
    assert v3.date == "s. f."
    assert v3.date_start is None and v3.date_end is None

    v4 = vols["xx-test-largo-0000000000004"]
    assert v4.filename_stem == "xx-test-largo-0000000000004"
    assert len(v4.filename_stem) == 27


@pytest.mark.unit
def test_custom_attributes_fisqua_keeps_every_unmapped_field(client, db_session, reports_root):
    _as("op", "admin")
    assert _post_fixture(client, VALID).status_code == 200
    vols = _volumes(db_session)
    items = {item["referenceCode"]: item for item in _load(VALID)["items"]}

    for code, volume in vols.items():
        attrs = json.loads(volume.custom_attributes)
        assert set(attrs) == {"fisqua"}
        expected = {k: v for k, v in items[code].items()
                    if k not in {"referenceCode", "filenameStem", "title", "dates", "sequence"}}
        assert attrs["fisqua"] == expected

    f1 = json.loads(vols["xx-test-001"].custom_attributes)["fisqua"]
    assert f1["typology"] == "Libro de prueba"
    assert f1["extent"] == "120 folios"
    assert f1["expectedImages"] == 240
    assert f1["notes"] == "Volumen con fechas completas."
    assert f1["containerPath"] == [
        {"level": "fonds", "title": "Fondo de prueba", "referenceCode": "xx-test"},
        {"level": "volume", "title": "Volumen de prueba uno", "referenceCode": "xx-test-001"},
    ]
    f3 = json.loads(vols["xx-test-003"].custom_attributes)["fisqua"]
    assert f3["fieldNotInContract"].startswith("Campo que no está en el contrato")
    assert f3["containerPath"] == []
    assert f3["extent"] is None and f3["expectedImages"] is None and f3["notes"] is None
    # Non-ASCII text is stored as itself, not as \u escapes.
    assert "está" in vols["xx-test-003"].custom_attributes


# ---------------------------------------------------------------------------
# The rejects list
# ---------------------------------------------------------------------------

FRESH_REJECTS = [
    (2, "xx-test-101", "reference_code_duplicated_in_list"),
    (3, None, "missing_reference_code"),
    (4, "xx-test-104", "missing_filename_stem"),
    (5, "xx-test-largo-00000000000105", "filename_stem_too_long"),
    (6, "xx-test-106", "filename_stem_not_a_slug"),
    (7, "xx-test-107", "filename_stem_differs_from_reference_code"),
    (8, "xx-test-108", "end_date_before_start"),
    (10, " xx-test-110", "reference_code_padded"),
]


@pytest.mark.unit
def test_rejects_list_on_a_fresh_unit_creates_items_1_and_9(client, db_session, reports_root):
    _as("op", "admin")
    resp = _post_fixture(client, REJECTS)
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["created"] == 2
    assert report["skipped"] == 8
    assert [v["position"] for v in report["volumes"]] == [1, 9]
    assert _reasons(report) == FRESH_REJECTS
    assert all(r["message"] for r in report["rejects"])

    vols = _volumes(db_session)
    assert set(vols) == {"xx-test-101", "xx-test-001"}
    assert all(v.filename_stem == v.external_id for v in vols.values())
    assert vols["xx-test-001"].name.startswith("Rechazo si antes")


@pytest.mark.unit
def test_rejects_list_after_the_valid_list_creates_one(client, db_session, reports_root):
    _as("op", "admin")
    assert _post_fixture(client, VALID).json()["created"] == 4
    report = _post_fixture(client, REJECTS).json()
    assert report["created"] == 1
    assert report["skipped"] == 9
    assert [v["referenceCode"] for v in report["volumes"]] == ["xx-test-101"]
    assert _reasons(report) == FRESH_REJECTS[:7] + [
        (9, "xx-test-001", "reference_code_on_unit"),
    ] + FRESH_REJECTS[7:]
    vols = _volumes(db_session)
    assert len(vols) == 5
    # The valid list's xx-test-001 is untouched by item 9.
    assert vols["xx-test-001"].name == "Volumen de prueba uno"


@pytest.mark.unit
@pytest.mark.parametrize("code, stem", [
    ("xx-test-980", "xx-test-981"),
    ("xx-test-980", "xx-test-980-b"),
    ("XX-TEST-980", "xx-test-980"),
])
def test_a_stem_that_differs_from_the_reference_code_is_rejected(client, db_session, reports_root, code, stem):
    """The stem is the volume's reference code, unchanged (contract 1.1 §8)."""
    _as("op", "admin")
    report = _post(client, _list_with([_item(code=code, stem=stem)])).json()
    assert report["created"] == 0
    assert _reasons(report) == [(1, code, "filename_stem_differs_from_reference_code")]


@pytest.mark.unit
def test_a_bad_stem_format_is_reported_before_the_equality_check(client, db_session, reports_root):
    _as("op", "admin")
    report = _post(client, _list_with([
        _item(code="xx-test-990", stem="XX Test 990"),
        _item(code="xx-test-largo-00000000000991", stem="xx-test-largo-00000000000991"),
    ])).json()
    assert [r["reason"] for r in report["rejects"]] == ["filename_stem_not_a_slug", "filename_stem_too_long"]


@pytest.mark.unit
def test_a_stem_held_by_a_volume_without_a_reference_code_is_rejected(client, db_session, reports_root):
    """A volume created on the device may carry the stem with no external_id."""
    from app.models.collection import Collection
    from app.models.project import Project

    p = Project(name="Local")
    db_session.add(p)
    db_session.commit()
    db_session.add(Collection(name="local", project_id=p.id, filename_stem="xx-test-901"))
    db_session.commit()

    _as("op", "admin")
    report = _post(client, _list_with([_item()])).json()
    assert _reasons(report) == [(1, "xx-test-901", "filename_stem_on_unit")]


@pytest.mark.unit
def test_reimporting_the_valid_list_creates_nothing_twice(client, db_session, reports_root):
    from app.models.project import Project

    _as("op", "admin")
    first = _post_fixture(client, VALID).json()
    second = _post_fixture(client, VALID).json()
    assert second["created"] == 0
    assert second["skipped"] == 4
    assert _reasons(second) == [
        (1, "xx-test-001", "reference_code_on_unit"),
        (2, "xx-test-002", "reference_code_on_unit"),
        (3, "xx-test-003", "reference_code_on_unit"),
        (4, "xx-test-largo-0000000000004", "reference_code_on_unit"),
    ]
    # The existing project of that name is reused.
    assert second["projectCreated"] is False
    assert second["projectId"] == first["projectId"]
    assert db_session.query(Project).filter(Project.name == "Prueba válida").count() == 1
    assert len(_volumes(db_session)) == 4


# ---------------------------------------------------------------------------
# Further item checks
# ---------------------------------------------------------------------------

def _list_with(items, **envelope):
    doc = _load(VALID)
    doc["items"] = items
    doc.update(envelope)
    return doc


def _item(code="xx-test-901", stem="xx-test-901", **kw):
    item = {"referenceCode": code, "filenameStem": stem, "title": "T", "sequence": 1,
            "dates": {"display": None, "start": None, "end": None}}
    item.update(kw)
    return item


@pytest.mark.unit
@pytest.mark.parametrize("item, reason", [
    ("not an object", "item_not_an_object"),
    (_item(code="   "), "missing_reference_code"),
    (_item(code=42), "invalid_reference_code"),
    (_item(stem=""), "missing_filename_stem"),
    (_item(stem="xx--test"), "filename_stem_not_a_slug"),
    (_item(stem=7), "filename_stem_not_a_slug"),
    (_item(title=None), "missing_title"),
    (_item(title="  "), "missing_title"),
    (_item(title="x" * 256), "invalid_title"),
    (_item(dates={"display": "x" * 51, "start": None, "end": None}), "invalid_dates"),
    (_item(dates={"display": None, "start": "1790", "end": None}), "invalid_dates"),
    (_item(dates={"display": None, "start": "1790-02-30", "end": None}), "invalid_dates"),
    (_item(dates="1790"), "invalid_dates"),
    (_item(sequence="1"), "invalid_sequence"),
    (_item(sequence=True), "invalid_sequence"),
])
def test_an_item_that_cannot_be_stored_is_rejected_and_the_rest_created(client, db_session, reports_root, item, reason):
    _as("op", "admin")
    good = _item(code="xx-test-950", stem="xx-test-950")
    report = _post(client, _list_with([item, good])).json()
    assert report["created"] == 1
    assert [(r["position"], r["reason"]) for r in report["rejects"]] == [(1, reason)]
    assert set(_volumes(db_session)) == {"xx-test-950"}


@pytest.mark.unit
def test_missing_dates_and_sequence_are_stored_as_null(client, db_session, reports_root):
    _as("op", "admin")
    item = {"referenceCode": "xx-test-960", "filenameStem": "xx-test-960", "title": "T"}
    report = _post(client, _list_with([item])).json()
    assert report["created"] == 1
    v = _volumes(db_session)["xx-test-960"]
    assert v.date is None and v.date_start is None and v.date_end is None and v.sequence is None
    assert json.loads(v.custom_attributes) == {"fisqua": {}}


@pytest.mark.unit
@pytest.mark.parametrize("code", [" xx-test-970", "xx-test-970 ", "\txx-test-970\n"])
def test_a_padded_reference_code_is_rejected_not_stripped(client, db_session, reports_root, code):
    """Codes are exchanged as Fisqua canonicalises them, and Captua never modifies one."""
    _as("op", "admin")
    report = _post(client, _list_with([_item(code=code, stem="xx-test-970")])).json()
    assert report["created"] == 0
    assert _reasons(report) == [(1, code, "reference_code_padded")]
    assert _volumes(db_session) == {}


@pytest.mark.unit
def test_an_empty_items_list_creates_the_project_and_no_volumes(client, db_session, reports_root):
    _as("op", "admin")
    report = _post(client, _list_with([])).json()
    assert report["created"] == 0 and report["skipped"] == 0
    assert _volumes(db_session) == {}


@pytest.mark.unit
def test_a_commit_refused_by_the_database_is_a_reject_not_an_error(client, db_session, reports_root):
    """A duplicate that reaches the insert past the checks is reported by its constraint.

    A listener adds a second volume with the same stem to the flush that
    inserts item 1, as a concurrent writer would between the check and the
    insert; the unique constraint refuses the flush.
    """
    from sqlalchemy import event
    from app.models.collection import Collection
    from app.models.project import Project

    other = Project(name="Elsewhere")
    db_session.add(other)
    db_session.commit()
    other_id = other.id
    fired = []

    def race(session, flush_context, instances):
        if fired:
            return
        if any(isinstance(o, Collection) and o.filename_stem == "xx-test-901" for o in session.new):
            fired.append(True)
            session.add(Collection(name="racer", project_id=other_id, filename_stem="xx-test-901"))

    event.listen(db_session, "before_flush", race)
    try:
        _as("op", "admin")
        resp = _post(client, _list_with([_item(), _item(code="xx-test-902", stem="xx-test-902")]))
    finally:
        event.remove(db_session, "before_flush", race)
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert fired
    assert [(r["position"], r["reason"]) for r in report["rejects"]] == [(1, "filename_stem_on_unit")]
    assert report["created"] == 1
    assert set(_volumes(db_session)) == {"xx-test-902"}


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("change", [
    {"contract": "something-else"},
    {"contract": None},
    {"kind": "capture-manifest"},
    {"kind": "qc-decisions"},
    {"version": "2.0"},
    {"version": "0.9"},
    {"version": 1.1},
    {"id": "not-a-uuid"},
    {"id": None},
    {"list": {"tenant": "captua-test"}},
    {"list": {"name": "  "}},
    {"items": {"0": {}}},
])
def test_an_envelope_this_unit_does_not_read_is_refused_and_nothing_written(client, db_session, reports_root, change):
    from app.models.project import Project

    _as("op", "admin")
    doc = _load(VALID)
    doc.update(change)
    resp = _post(client, doc)
    assert resp.status_code == 422, resp.text
    assert db_session.query(Project).count() == 0
    assert _volumes(db_session) == {}
    assert not reports_root.exists()


@pytest.mark.unit
def test_a_missing_contract_field_is_refused(client, db_session, reports_root):
    _as("op", "admin")
    doc = _load(VALID)
    del doc["kind"]
    assert _post(client, doc).status_code == 422


@pytest.mark.unit
def test_a_later_minor_version_and_unknown_fields_are_accepted(client, db_session, reports_root):
    _as("op", "admin")
    doc = _load(VALID)
    doc["version"] = "1.7"
    doc["somethingNew"] = {"a": 1}
    doc["list"]["alsoNew"] = True
    report = _post(client, doc).json()
    assert report["created"] == 4


@pytest.mark.unit
@pytest.mark.parametrize("body", [b"{not json", "{}".encode("utf-16"), b"[1, 2]"])
def test_a_file_that_is_not_a_json_object_is_refused(client, db_session, reports_root, body):
    _as("op", "admin")
    assert _post(client, body).status_code in (400, 422)
    assert _volumes(db_session) == {}


@pytest.mark.unit
def test_an_oversized_file_is_refused(client, db_session, reports_root, monkeypatch):
    import app.api.imports as imports

    monkeypatch.setattr(imports, "MAX_LIST_BYTES", 100)
    _as("op", "admin")
    assert _post_fixture(client, VALID).status_code == 413
    assert _volumes(db_session) == {}


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

def _existing_project(db_session, name):
    from app.models.project import Project

    p = Project(name=name)
    db_session.add(p)
    db_session.commit()
    return p.id


@pytest.mark.unit
@pytest.mark.parametrize("role, allowed", [("admin", True), ("operator", True), ("reviewer", False)])
def test_import_into_an_existing_project_is_admin_or_operator(client, db_session, reports_root, role, allowed):
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User

    project_id = _existing_project(db_session, "Prueba válida")
    user = User(username="u", email="u@example.com", hashed_password="x", role=role, is_active=True)
    app.dependency_overrides[get_current_user] = lambda: user
    resp = _post_fixture(client, VALID)
    if allowed:
        assert resp.status_code == 200, resp.text
        assert resp.json()["projectId"] == project_id
        assert resp.json()["projectCreated"] is False
        assert resp.json()["created"] == 4
    else:
        assert resp.status_code == 403
        assert _volumes(db_session) == {}


@pytest.mark.unit
def test_an_operator_cannot_import_a_list_that_creates_a_project(client, db_session, reports_root):
    from app.models.project import Project
    from app.models.system_log import SystemLog

    _existing_project(db_session, "Another project")
    _as("op", "operator")
    resp = _post_fixture(client, VALID)
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"]
    assert "project" in resp.json()["detail"]
    assert [p.name for p in db_session.query(Project).all()] == ["Another project"]
    assert _volumes(db_session) == {}
    assert not reports_root.exists()
    assert db_session.query(SystemLog).filter(SystemLog.action == "digitisation_list_imported").count() == 0


@pytest.mark.unit
def test_an_admin_import_creates_the_project_and_an_operator_can_then_reimport(client, db_session, reports_root):
    _as("admin", "admin")
    assert _post_fixture(client, VALID).json()["projectCreated"] is True
    _as("op", "operator")
    resp = _post_fixture(client, REJECTS)
    assert resp.status_code == 403  # a different basket, so a new project
    resp = _post_fixture(client, VALID)
    assert resp.status_code == 200
    assert resp.json()["created"] == 0 and resp.json()["skipped"] == 4


@pytest.mark.unit
def test_an_envelope_refusal_comes_before_the_project_check(client, db_session, reports_root):
    _as("op", "operator")
    doc = _load(VALID)
    doc["kind"] = "qc-decisions"
    assert _post(client, doc).status_code == 422


@pytest.mark.unit
def test_import_requires_authentication(client, db_session, reports_root):
    assert _post_fixture(client, VALID).status_code == 401


# ---------------------------------------------------------------------------
# The stored report
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_every_run_is_stored_and_the_latest_can_be_fetched_again(client, db_session, reports_root):
    _as("op", "admin")
    first = _post_fixture(client, VALID).json()
    second = _post_fixture(client, VALID).json()

    runs = sorted((reports_root / VALID_ID).glob("import-report_*.json"))
    assert len(runs) == 2
    stored = [json.loads(p.read_text(encoding="utf-8")) for p in runs]
    assert [s["created"] for s in stored] == [first["created"], second["created"]] == [4, 0]
    assert "reportStored" not in stored[0]

    resp = client.get(f"/imports/digitisation-list/{VALID_ID}/report")
    assert resp.status_code == 200
    assert f'filename="import-report_{VALID_ID}.json"' in resp.headers["content-disposition"]
    body = resp.json()
    assert body["created"] == 0 and body["skipped"] == 4
    assert body["rejects"] == second["rejects"]


@pytest.mark.unit
def test_fetching_a_report_that_does_not_exist(client, db_session, reports_root):
    _as("op", "admin")
    assert client.get(f"/imports/digitisation-list/{REJECTS_ID}/report").status_code == 404
    assert client.get("/imports/digitisation-list/..%2F..%2Fetc/report").status_code in (404, 422)
    assert client.get("/imports/digitisation-list/not-a-uuid/report").status_code == 422


@pytest.mark.unit
def test_a_report_that_cannot_be_stored_still_returns_and_keeps_the_volumes(client, db_session, reports_root, monkeypatch):
    import app.core.list_import as li

    def fail(report):
        raise OSError("disk full")

    monkeypatch.setattr(li, "store_report", fail)
    _as("op", "admin")
    resp = _post_fixture(client, VALID)
    assert resp.status_code == 200
    assert resp.json()["reportStored"] is False
    assert resp.json()["created"] == 4
    assert len(_volumes(db_session)) == 4


@pytest.mark.unit
def test_the_import_is_logged(client, db_session, reports_root):
    from app.models.system_log import SystemLog

    _as("op", "admin")
    _post_fixture(client, REJECTS)
    entry = db_session.query(SystemLog).filter(SystemLog.action == "digitisation_list_imported").one()
    assert entry.actor == "op"
    assert entry.subject == "Prueba de rechazos"
    assert entry.detail == f"list {REJECTS_ID}: 2 created, 8 skipped"


# ---------------------------------------------------------------------------
# One bad item never stops the others
# ---------------------------------------------------------------------------

@pytest.fixture
def client500(db_session):
    """A client that returns a 500 instead of raising, so a crash shows as a status."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.deps import get_db_dependency

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db_dependency] = override_get_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _numbered(n, start=1):
    return [_item(code=f"xx-test-{i:05d}", stem=f"xx-test-{i:05d}") for i in range(start, start + n)]


@pytest.mark.unit
def test_a_database_failure_on_one_item_does_not_stop_the_rest(client500, db_session, reports_root, monkeypatch):
    """An OperationalError on item 2's commit: items 1 and 3 are created and the report is written."""
    from sqlalchemy.exc import OperationalError

    real_commit = db_session.commit
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 3:  # 1 = the project, 2 = item 1, 3 = item 2
            raise OperationalError("INSERT", {}, Exception("disk I/O error"))
        return real_commit()

    monkeypatch.setattr(db_session, "commit", flaky)
    _as("op", "admin")
    resp = _post(client500, _list_with(_numbered(3)))
    monkeypatch.setattr(db_session, "commit", real_commit)
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert _reasons(report) == [(2, "xx-test-00002", "refused_by_database")]
    assert report["created"] == 2
    assert set(_volumes(db_session)) == {"xx-test-00001", "xx-test-00003"}
    assert report["reportStored"] is True
    assert len(list(reports_root.rglob("import-report_*.json"))) == 1


@pytest.mark.unit
@pytest.mark.parametrize("sequence, ok", [
    (2**63, False), (2**31, False), (-(2**31) - 1, False),
    (2**31 - 1, True), (-(2**31), True), (0, True),
])
def test_sequence_is_bounded_to_int4(client500, db_session, reports_root, sequence, ok):
    _as("op", "admin")
    items = [_item(code="xx-test-800", stem="xx-test-800"),
             _item(code="xx-test-801", stem="xx-test-801", sequence=sequence),
             _item(code="xx-test-802", stem="xx-test-802")]
    resp = _post(client500, _list_with(items))
    assert resp.status_code == 200, resp.text
    vols = _volumes(db_session)
    if ok:
        assert set(vols) == {"xx-test-800", "xx-test-801", "xx-test-802"}
        assert vols["xx-test-801"].sequence == sequence
    else:
        assert _reasons(resp.json()) == [(2, "xx-test-801", "invalid_sequence")]
        assert set(vols) == {"xx-test-800", "xx-test-802"}


# ---------------------------------------------------------------------------
# Malformed JSON is a 400, never a 500
# ---------------------------------------------------------------------------

def _single(code="xx-test-991"):
    return json.dumps(_list_with([_item(code=code, stem=code)]))


@pytest.mark.unit
@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nan_and_infinity_are_refused(client500, db_session, reports_root, constant):
    _as("op", "admin")
    body = _single().replace('"sequence": 1', f'"sequence": 1, "expectedImages": {constant}')
    resp = _post(client500, body.encode())
    assert resp.status_code == 400, resp.text
    assert constant in resp.json()["detail"]
    assert _volumes(db_session) == {}


@pytest.mark.unit
def test_a_number_beyond_the_parsers_digit_limit_is_a_400(client500, db_session, reports_root):
    _as("op", "admin")
    body = _single().replace('"sequence": 1', '"sequence": 1, "extent": ' + "9" * 5000)
    resp = _post(client500, body.encode())
    assert resp.status_code == 400
    assert resp.json()["detail"] == "The file contains a number too large to read."
    assert _volumes(db_session) == {}


@pytest.mark.unit
def test_deep_nesting_is_a_400(client500, db_session, reports_root):
    _as("op", "admin")
    body = _single().replace('"sequence": 1', '"sequence": 1, "extent": ' + "[" * 100000 + "]" * 100000)
    assert _post(client500, body.encode()).status_code == 400
    assert _volumes(db_session) == {}


@pytest.mark.unit
def test_a_duplicate_envelope_key_is_refused_naming_the_key(client500, db_session, reports_root):
    _as("op", "admin")
    text = VALID.read_text(encoding="utf-8")
    body = text.replace('"version": "1.1"', '"version": "2.0", "version": "1.1"', 1)
    resp = _post(client500, body.encode())
    assert resp.status_code == 400, resp.text
    assert "'version'" in resp.json()["detail"]
    assert _volumes(db_session) == {}


@pytest.mark.unit
@pytest.mark.parametrize("item, key", [
    ('{"referenceCode": "xx-test-bad", "referenceCode": "xx-test-990", '
     '"filenameStem": "xx-test-990", "title": "T"}', "referenceCode"),
    ('{"referenceCode": "xx-test-990", "filenameStem": "xx-test-990", "title": "T", '
     '"dates": {"start": null, "start": "1800-01-01"}}', "start"),
])
def test_a_duplicate_key_inside_an_item_is_refused(client500, db_session, reports_root, item, key):
    _as("op", "admin")
    body = json.dumps(_list_with([])).replace('"items": []', '"items": [' + item + ']')
    resp = _post(client500, body.encode())
    assert resp.status_code == 400, resp.text
    assert f"'{key}'" in resp.json()["detail"]
    assert _volumes(db_session) == {}


@pytest.mark.unit
def test_a_utf8_byte_order_mark_is_accepted(client500, db_session, reports_root):
    _as("op", "admin")
    resp = _post(client500, b"\xef\xbb\xbf" + VALID.read_bytes())
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 4


@pytest.mark.unit
def test_a_latin1_file_is_refused_as_not_utf8(client500, db_session, reports_root):
    _as("op", "admin")
    text = VALID.read_text(encoding="utf-8").replace("–", "-")
    resp = _post(client500, text.encode("latin-1"))
    assert resp.status_code == 400
    assert resp.json()["detail"] == "The file is not UTF-8 text."


@pytest.mark.unit
def test_text_that_is_not_json_is_refused_as_not_json(client500, db_session, reports_root):
    _as("op", "admin")
    resp = _post(client500, b"{not json")
    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("The file is not valid JSON")


# ---------------------------------------------------------------------------
# Unknown dates subfields
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_unknown_dates_subfields_are_kept_under_fisqua_dates(client, db_session, reports_root):
    _as("op", "admin")
    item = _item(code="xx-test-701", stem="xx-test-701", typology="Libro",
                 dates={"display": "1790", "start": "1790-01-01", "end": None,
                        "calendar": "julian", "certainty": "approx"})
    _post(client, _list_with([item]))
    v = _volumes(db_session)["xx-test-701"]
    assert json.loads(v.custom_attributes) == {
        "fisqua": {"typology": "Libro", "dates": {"calendar": "julian", "certainty": "approx"}},
    }
    assert v.date == "1790"


@pytest.mark.unit
def test_no_dates_key_under_fisqua_when_dates_has_only_mapped_subfields(client, db_session, reports_root):
    _as("op", "admin")
    _post(client, _list_with([_item(code="xx-test-702", stem="xx-test-702")]))
    assert "dates" not in json.loads(_volumes(db_session)["xx-test-702"].custom_attributes)["fisqua"]


# ---------------------------------------------------------------------------
# Runs, the file hash and a reused list id
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_runs_are_numbered_and_the_highest_is_served(client, db_session, reports_root):
    _as("op", "admin")
    _post_fixture(client, VALID)
    _post_fixture(client, VALID)
    names = sorted(p.name for p in (reports_root / VALID_ID).glob("import-report_*.json"))
    assert names == [f"import-report_{VALID_ID}_run0001.json", f"import-report_{VALID_ID}_run0002.json"]
    assert client.get(f"/imports/digitisation-list/{VALID_ID}/report").json()["created"] == 0


@pytest.mark.unit
def test_the_latest_run_is_served_after_the_clock_goes_back(client, db_session, reports_root, monkeypatch):
    """A unit without a real-time clock can boot with its clock behind the last run."""
    import app.core.list_import as li
    from datetime import datetime as real_dt, timezone

    times = iter([real_dt(2026, 10, 23, 12, 0, tzinfo=timezone.utc),
                  real_dt(2026, 1, 1, 0, 0, tzinfo=timezone.utc)])

    class FakeDT(real_dt):
        @classmethod
        def now(cls, tz=None):
            return next(times)

    monkeypatch.setattr(li, "datetime", FakeDT)
    _as("op", "admin")
    assert _post_fixture(client, VALID).json()["created"] == 4
    second = _post_fixture(client, VALID).json()
    got = client.get(f"/imports/digitisation-list/{VALID_ID}/report").json()
    assert got["importedAt"] == second["importedAt"] and got["created"] == 0


@pytest.mark.unit
def test_a_run_number_taken_meanwhile_is_skipped_not_overwritten(reports_root, monkeypatch):
    import app.core.list_import as li

    report = {"listId": VALID_ID, "importedAt": "x", "created": 1}
    monkeypatch.setattr(li, "_next_run_number", lambda directory, list_id: 1)
    first = li.store_report(report)
    second = li.store_report({**report, "created": 2})
    assert first.name.endswith("_run0001.json") and second.name.endswith("_run0002.json")
    assert json.loads(first.read_text())["created"] == 1
    assert not [p for p in (reports_root / VALID_ID).iterdir() if p.name.startswith(".tmp")]


@pytest.mark.unit
def test_the_report_carries_the_uploaded_files_sha256(client, db_session, reports_root):
    import hashlib

    _as("op", "admin")
    report = _post_fixture(client, VALID).json()
    assert report["fileSha256"] == hashlib.sha256(VALID.read_bytes()).hexdigest()
    stored = json.loads(next((reports_root / VALID_ID).glob("*.json")).read_text(encoding="utf-8"))
    assert stored["fileSha256"] == report["fileSha256"]


@pytest.mark.unit
def test_a_reused_list_id_with_different_content_is_refused_and_nothing_written(client, db_session, reports_root):
    from app.models.project import Project

    _as("op", "admin")
    assert _post_fixture(client, VALID).status_code == 200
    other = _list_with([_item(code="xx-test-997", stem="xx-test-997")])
    other["list"]["name"] = "Otra canasta"
    resp = _post(client, other)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "This list id was already imported with different content."
    assert [p.name for p in db_session.query(Project).all()] == ["Prueba válida"]
    assert "xx-test-997" not in _volumes(db_session)
    assert len(list((reports_root / VALID_ID).glob("import-report_*.json"))) == 1
    assert client.get(f"/imports/digitisation-list/{VALID_ID}/report").json()["listName"] == "Prueba válida"


@pytest.mark.unit
def test_imports_of_one_list_id_are_serialised_by_the_lock(reports_root):
    import threading
    import app.core.list_import as li

    entered = threading.Event()

    def second():
        with li._list_lock(VALID_ID):
            entered.set()

    with li._list_lock(VALID_ID):
        t = threading.Thread(target=second)
        t.start()
        assert not entered.wait(0.3)
    t.join(5)
    assert entered.is_set()


# ---------------------------------------------------------------------------
# Matching project names
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("variant", [
    "prueba válida", "PRUEBA VÁLIDA", "Prueba válida ", " Prueba válida", "\tPrueba válida\n",
    "Prueba válida",  # NFD: a + combining acute
])
def test_a_list_name_variant_reuses_the_existing_project(client, db_session, reports_root, variant):
    from app.models.project import Project

    project_id = _existing_project(db_session, "Prueba válida")
    doc = _load(VALID)
    doc["list"]["name"] = variant
    _as("op", "operator")  # an operator may import only into an existing project
    resp = _post(client, doc)
    assert resp.status_code == 200, resp.text
    assert resp.json()["projectId"] == project_id
    assert resp.json()["projectCreated"] is False
    assert resp.json()["projectName"] == "Prueba válida"
    assert db_session.query(Project).count() == 1


@pytest.mark.unit
def test_a_new_project_is_stored_under_the_stripped_nfc_name(client, db_session, reports_root):
    import unicodedata
    from app.models.project import Project

    doc = _load(VALID)
    doc["list"]["name"] = "  Prueba válida  "
    _as("op", "admin")
    resp = _post(client, doc)
    assert resp.json()["projectCreated"] is True
    name = db_session.query(Project).one().name
    assert name == "Prueba válida" and name == unicodedata.normalize("NFC", name)
    assert resp.json()["projectName"] == name


# ---------------------------------------------------------------------------
# The list's notes
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_new_project_takes_the_lists_notes_as_its_description(client, db_session, reports_root):
    from app.models.project import Project

    _as("op", "admin")
    report = _post_fixture(client, VALID).json()
    assert db_session.query(Project).one().description == _load(VALID)["list"]["notes"]
    assert report["projectNotesStored"] is True


@pytest.mark.unit
@pytest.mark.parametrize("notes, reason", [
    (None, "blank"), ("", "blank"), ("   ", "blank"),
    (5, "not_a_string"), ({"a": 1}, "not_a_string"), (["x"], "not_a_string"), (True, "not_a_string"),
    ("n" * 10_001, "too_long"), ("con\u0000nul", "contains_nul"),
], ids=["none", "empty", "spaces", "int", "object", "array", "bool", "too_long", "nul"])
def test_unusable_notes_leave_a_new_projects_description_empty(client, db_session, reports_root, notes, reason):
    from app.models.project import Project

    doc = _load(VALID)
    doc["list"]["notes"] = notes
    _as("op", "admin")
    report = _post(client, doc).json()
    assert db_session.query(Project).one().description is None
    assert report["projectNotesStored"] is False
    assert report["projectNotesSkipped"] == reason


@pytest.mark.unit
def test_notes_of_exactly_10000_characters_are_stored(client, db_session, reports_root):
    from app.models.project import Project

    doc = _load(VALID)
    doc["list"]["notes"] = "n" * 10_000
    _as("op", "admin")
    report = _post(client, doc).json()
    assert db_session.query(Project).one().description == "n" * 10_000
    assert report["projectNotesStored"] is True
    assert report["projectNotesSkipped"] is None


@pytest.mark.unit
def test_a_missing_notes_field_is_blank(client, db_session, reports_root):
    doc = _load(VALID)
    del doc["list"]["notes"]
    _as("op", "admin")
    report = _post(client, doc).json()
    assert report["projectNotesStored"] is False and report["projectNotesSkipped"] == "blank"


@pytest.mark.unit
@pytest.mark.parametrize("existing", [None, "Descripción del administrador"])
def test_an_existing_projects_description_is_never_overwritten(client, db_session, reports_root, existing):
    from app.models.project import Project

    p = Project(name="Prueba válida", description=existing)
    db_session.add(p)
    db_session.commit()
    _as("op", "admin")
    report = _post_fixture(client, VALID).json()
    db_session.expire_all()
    assert db_session.query(Project).one().description == existing
    assert report["projectNotesStored"] is False
    assert report["projectNotesSkipped"] == "project_exists"


# ---------------------------------------------------------------------------
# Rejects echo a bounded code
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("length, truncated", [(1_000_000, True), (121, True), (120, False)])
def test_a_reject_echoes_at_most_120_characters_of_the_code(client, db_session, reports_root, length, truncated):
    _as("op", "admin")
    code = "x" * length
    report = _post(client, _list_with([_item(code=code, stem="xx-test-994")])).json()
    (reject,) = report["rejects"]
    assert reject["referenceCode"] == code[:120]
    assert reject["referenceCodeTruncated"] is truncated
    stored = next((reports_root / VALID_ID).glob("*.json"))
    assert stored.stat().st_size < 10_000


@pytest.mark.unit
def test_an_ordinary_reject_says_its_code_is_not_truncated(client, db_session, reports_root):
    _as("op", "admin")
    report = _post_fixture(client, REJECTS).json()
    assert all(r["referenceCodeTruncated"] is False for r in report["rejects"])


# ---------------------------------------------------------------------------
# The hash check cannot be skipped
# ---------------------------------------------------------------------------

def _second_import(db_session):
    import app.core.list_import as li

    return li.import_list(db_session, _list_with([_item(code="xx-test-902", stem="xx-test-902")]),
                          "a", True, file_sha256="2" * 64)


@pytest.mark.unit
def test_the_first_import_records_the_lists_hash(db_session, reports_root):
    import app.core.list_import as li

    li.import_list(db_session, _list_with([_item()]), "a", True, file_sha256="1" * 64)
    assert (reports_root / VALID_ID / "sha256").read_text().strip() == "1" * 64
    assert not list((reports_root / VALID_ID).glob(".tmp-*"))


@pytest.mark.unit
@pytest.mark.parametrize("damage", ["garbage", "chmod000", "not_stored", "no_hash_in_report"])
def test_a_damaged_earlier_run_still_refuses_different_content(db_session, reports_root, monkeypatch, damage):
    import app.core.list_import as li

    def fail(report):
        raise OSError(28, "No space left on device")

    # A context, not monkeypatch.undo(), which would also undo reports_root.
    with monkeypatch.context() as m:
        if damage == "not_stored":
            m.setattr(li, "store_report", fail)
        first = li.import_list(db_session, _list_with([_item()]), "a", True, file_sha256="1" * 64)
    runs = list((reports_root / VALID_ID).glob("*_run*.json"))
    if damage == "garbage":
        runs[0].write_text("{not json")
    elif damage == "chmod000":
        runs[0].chmod(0)
    elif damage == "no_hash_in_report":
        runs[0].write_text(json.dumps({"listId": VALID_ID}))
    try:
        with pytest.raises(li.ListIdReused):
            _second_import(db_session)
    finally:
        for r in runs:
            r.chmod(0o644)
    assert first["reportStored"] is (damage != "not_stored")
    assert "xx-test-902" not in _volumes(db_session)


@pytest.mark.unit
@pytest.mark.parametrize("damage", ["garbage", "chmod000", "no_hash_in_report"])
def test_an_unreadable_earlier_run_without_a_hash_file_refuses_any_import(db_session, reports_root, damage):
    """A run stored before the hash file, or one whose hash file was lost: nothing to check against."""
    import app.core.list_import as li

    li.import_list(db_session, _list_with([_item()]), "a", True, file_sha256="1" * 64)
    (reports_root / VALID_ID / "sha256").unlink()
    (run,) = (reports_root / VALID_ID).glob("*_run*.json")
    if damage == "garbage":
        run.write_text("{not json")
    elif damage == "chmod000":
        run.chmod(0)
    else:
        run.write_text(json.dumps({"listId": VALID_ID}))
    try:
        with pytest.raises(li.ListUnverifiable):
            li.import_list(db_session, _list_with([_item()]), "a", True, file_sha256="1" * 64)
    finally:
        run.chmod(0o644)
    assert not (reports_root / VALID_ID / "sha256").exists()
    assert len(list((reports_root / VALID_ID).glob("*_run*.json"))) == 1


@pytest.mark.unit
def test_a_readable_earlier_run_without_a_hash_file_is_checked_and_the_file_written(db_session, reports_root):
    import app.core.list_import as li

    li.import_list(db_session, _list_with([_item()]), "a", True, file_sha256="1" * 64)
    (reports_root / VALID_ID / "sha256").unlink()
    with pytest.raises(li.ListIdReused):
        _second_import(db_session)
    li.import_list(db_session, _list_with([_item()]), "a", True, file_sha256="1" * 64)
    assert (reports_root / VALID_ID / "sha256").read_text().strip() == "1" * 64


@pytest.mark.unit
@pytest.mark.parametrize("damage", ["chmod000", "garbage"])
def test_an_unreadable_hash_file_refuses_the_import_with_a_409(client, db_session, reports_root, damage):
    from app.models.project import Project

    _as("op", "admin")
    assert _post_fixture(client, VALID).status_code == 200
    sha = reports_root / VALID_ID / "sha256"
    if damage == "chmod000":
        sha.chmod(0)
    else:
        sha.write_text("not a hash")
    try:
        resp = _post_fixture(client, VALID)
    finally:
        sha.chmod(0o644)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "An earlier import of this list cannot be checked."
    assert len(list((reports_root / VALID_ID).glob("*_run*.json"))) == 1
    assert db_session.query(Project).count() == 1


@pytest.mark.unit
def test_a_hash_file_that_cannot_be_written_refuses_the_import(client, db_session, reports_root, monkeypatch):
    import app.core.list_import as li
    from app.models.project import Project

    def fail(directory, digest):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(li, "_write_hash_file", fail)
    _as("op", "admin")
    resp = _post_fixture(client, VALID)
    assert resp.status_code == 503
    assert db_session.query(Project).count() == 0
    assert _volumes(db_session) == {}


# ---------------------------------------------------------------------------
# Stale temporary files
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_taking_the_lock_removes_temporary_files_older_than_an_hour(reports_root):
    import os
    import time
    import app.core.list_import as li

    d = reports_root / VALID_ID
    d.mkdir(parents=True)
    old = d / ".tmp-old.json"
    fresh = d / ".tmp-fresh.json"
    run = d / f"import-report_{VALID_ID}_run0001.json"
    for p in (old, fresh, run):
        p.write_text("{}")
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))
    os.utime(run, (two_hours_ago, two_hours_ago))
    other = reports_root / REJECTS_ID
    other.mkdir()
    other_old = other / ".tmp-old.json"
    other_old.write_text("{}")
    os.utime(other_old, (two_hours_ago, two_hours_ago))

    with li._list_lock(VALID_ID):
        pass
    assert not old.exists()
    assert fresh.exists() and run.exists()
    assert other_old.exists()  # only the locked list's directory


# ---------------------------------------------------------------------------
# Filesystems without hard links
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("errno_value", [1, 45, 18])  # EPERM, EOPNOTSUPP (macOS), EXDEV
def test_runs_are_stored_without_hard_links(reports_root, monkeypatch, errno_value):
    import app.core.list_import as li

    def nolink(src, dst):
        raise OSError(errno_value, "no hard links here")

    monkeypatch.setattr(li.os, "link", nolink)
    first = li.store_report({"listId": VALID_ID, "importedAt": "t", "created": 1})
    second = li.store_report({"listId": VALID_ID, "importedAt": "t", "created": 2})
    assert first.name.endswith("_run0001.json") and second.name.endswith("_run0002.json")
    assert json.loads(first.read_text())["created"] == 1
    assert json.loads(second.read_text())["created"] == 2
    assert not list((reports_root / VALID_ID).glob(".tmp-*"))


@pytest.mark.unit
def test_without_hard_links_a_taken_run_number_is_never_overwritten(reports_root, monkeypatch):
    import app.core.list_import as li

    def nolink(src, dst):
        raise OSError(1, "no hard links here")

    monkeypatch.setattr(li.os, "link", nolink)
    monkeypatch.setattr(li, "_next_run_number", lambda directory, list_id: 1)
    first = li.store_report({"listId": VALID_ID, "importedAt": "t", "created": 1})
    second = li.store_report({"listId": VALID_ID, "importedAt": "t", "created": 2})
    assert second.name.endswith("_run0002.json")
    assert json.loads(first.read_text())["created"] == 1


@pytest.mark.unit
def test_a_whole_import_without_hard_links(db_session, reports_root, monkeypatch):
    import app.core.list_import as li

    monkeypatch.setattr(li.os, "link", lambda src, dst: (_ for _ in ()).throw(OSError(1, "no")))
    r1 = li.import_list(db_session, _list_with([_item()]), "a", True, file_sha256="1" * 64)
    assert r1["reportStored"] is True
    with pytest.raises(li.ListIdReused):
        _second_import(db_session)


# ---------------------------------------------------------------------------
# NUL characters, which Postgres text cannot hold
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_list_name_with_nul_is_refused_at_the_envelope(client, db_session, reports_root):
    from app.models.project import Project

    doc = _load(VALID)
    doc["list"]["name"] = "Prueba\u0000válida"
    _as("op", "admin")
    resp = _post(client, doc)
    assert resp.status_code == 422
    assert "NUL" in resp.json()["detail"]
    assert db_session.query(Project).count() == 0


@pytest.mark.unit
@pytest.mark.parametrize("field, reason", [
    ({"title": "T\u0000"}, "invalid_title"),
    ({"dates": {"display": "1790\u0000", "start": None, "end": None}}, "invalid_dates"),
    ({"code": "xx-test-9\u000001"}, "invalid_reference_code"),
])
def test_an_item_text_field_with_nul_is_rejected(client, db_session, reports_root, field, reason):
    _as("op", "admin")
    kw = dict(field)
    code = kw.pop("code", "xx-test-901")
    report = _post(client, _list_with([_item(code=code, stem="xx-test-901", **kw),
                                       _item(code="xx-test-902", stem="xx-test-902")])).json()
    assert [(r["position"], r["reason"]) for r in report["rejects"]] == [(1, reason)]
    assert set(_volumes(db_session)) == {"xx-test-902"}


@pytest.mark.unit
def test_nul_in_an_unmapped_field_is_escaped_in_the_stored_json(client, db_session, reports_root):
    _as("op", "admin")
    _post(client, _list_with([_item(notes="a\u0000b")]))
    raw = _volumes(db_session)["xx-test-901"].custom_attributes
    assert "\x00" not in raw and "\\u0000" in raw
    assert json.loads(raw)["fisqua"]["notes"] == "a\u0000b"


# ---------------------------------------------------------------------------
# GET serves the highest run that parses
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("damage", ["garbage", "half_written", "not_an_object", "other_list", "chmod000"])
def test_get_skips_a_latest_run_that_cannot_be_read(client, db_session, reports_root, caplog, damage):
    _as("op", "admin")
    _post_fixture(client, VALID)
    _post_fixture(client, VALID)
    latest = reports_root / VALID_ID / f"import-report_{VALID_ID}_run0002.json"
    if damage == "garbage":
        latest.write_text("{not json")
    elif damage == "half_written":
        latest.write_text(latest.read_text()[:40])
    elif damage == "not_an_object":
        latest.write_text("[1, 2]")
    elif damage == "other_list":
        latest.write_text(json.dumps({"listId": REJECTS_ID, "created": 9}))
    else:
        latest.chmod(0)
    try:
        resp = client.get(f"/imports/digitisation-list/{VALID_ID}/report")
    finally:
        latest.chmod(0o644)
    assert resp.status_code == 200
    assert resp.json()["created"] == 4  # run0001
    assert f'filename="import-report_{VALID_ID}.json"' in resp.headers["content-disposition"]
    assert any("run0002" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
def test_get_is_a_404_when_no_run_parses(client, db_session, reports_root):
    _as("op", "admin")
    _post_fixture(client, VALID)
    for run in (reports_root / VALID_ID).glob("*_run*.json"):
        run.write_text("{not json")
    assert client.get(f"/imports/digitisation-list/{VALID_ID}/report").status_code == 404


# ---------------------------------------------------------------------------
# The first hash is created exclusively
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("other, expect_reused", [("b" * 64, True), ("1" * 64, False)])
@pytest.mark.parametrize("links", [True, False])
def test_a_hash_file_written_meanwhile_is_compared_not_replaced(db_session, reports_root, monkeypatch,
                                                                other, expect_reused, links):
    """Another process writes sha256 after this import's check found none and before its write."""
    import app.core.list_import as li

    if not links:
        monkeypatch.setattr(li.os, "link", lambda src, dst: (_ for _ in ()).throw(OSError(1, "no links")))
    real_write = li._write_hash_file

    def other_writes_first(directory, digest):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "sha256").write_text(other + "\n")
        return real_write(directory, digest)

    monkeypatch.setattr(li, "_write_hash_file", other_writes_first)
    doc = _list_with([_item()])
    if expect_reused:
        with pytest.raises(li.ListIdReused):
            li.import_list(db_session, doc, "a", True, file_sha256="1" * 64)
        assert _volumes(db_session) == {}
    else:
        assert li.import_list(db_session, doc, "a", True, file_sha256="1" * 64)["created"] == 1
    assert (reports_root / VALID_ID / "sha256").read_text().strip() == other
    assert not list((reports_root / VALID_ID).glob(".tmp-*"))


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["import", "import_noflock"])
def test_two_processes_racing_the_first_hash_create_once(tmp_path, mode):
    import os
    import subprocess
    import sys

    worker = str(Path(__file__).resolve().parent / "list_import_race_worker.py")
    env = {**os.environ, "CAPTUA_DATA_DIR": str(tmp_path)}
    procs = [subprocess.Popen([sys.executable, worker, mode, str(tmp_path / f"{t}.db"), t * 64],
                              env=env, cwd=os.getcwd(), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
             for t in ("a", "b")]
    outs = [p.communicate(timeout=60)[0] for p in procs]
    results = [line for out in outs for line in out.splitlines() if line.startswith("RESULT")]
    assert len(results) == 2, outs
    assert sum("created 1" in r for r in results) == 1, results
    assert sum("ListIdReused" in r for r in results) == 1, results
