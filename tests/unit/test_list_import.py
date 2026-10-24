"""The digitisation list import (exchange contract 1.1, artefact 1).

POST /imports/digitisation-list takes a list file and creates one volume per
item, in a project named after the basket; items that cannot be created are
reported as rejects and the rest are still created. The fixtures are the two
synthetic lists in tests/fixtures/ (tenant captua-test, codes xx-test-...).
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
    monkeypatch.setattr(config.settings, "DTK_DATA_DIR", str(root))
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
    import app.api.imports as imports

    def fail(report):
        raise OSError("disk full")

    monkeypatch.setattr(imports, "store_report", fail)
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
