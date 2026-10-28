"""The digitisation list as a queue: the stored list file and GET /imports/digitisation-lists.

A successful import keeps the uploaded file unchanged as
`<DATA_DIR>/imports/<list-id>/list.json`, and GET /imports/digitisation-lists
reads it back, item by item in list order, beside the volume each item's stem
names on this unit and that volume's status counts. An item's optional
`action` is not a contract field: the import keeps it under
custom_attributes.fisqua like any other unmapped field.

Run with: python -m pytest tests/unit/test_import_queue.py

Version: 0.2.0
"""

import json

import pytest

from tests.unit.test_list_import import (
    REJECTS, REJECTS_ID, VALID, VALID_ID,
    _as, _item, _list_with, _post, _post_fixture, _volumes,
)
from tests.unit.test_list_import import reports_root  # noqa: F401  (fixture)


def _lists(client):
    resp = client.get("/imports/digitisation-lists")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _one(client, list_id):
    found = [entry for entry in _lists(client) if entry["listId"] == list_id]
    assert len(found) == 1
    return found[0]


def _list_file(reports_root, list_id):
    return reports_root / list_id / "list.json"


# ---------------------------------------------------------------------------
# The stored list file
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_import_keeps_the_uploaded_file_unchanged(client, db_session, reports_root):
    _as("op", "admin")
    resp = _post_fixture(client, VALID)
    assert resp.status_code == 200, resp.text
    assert resp.json()["listStored"] is True
    assert _list_file(reports_root, VALID_ID).read_bytes() == VALID.read_bytes()


@pytest.mark.unit
def test_the_stored_report_says_whether_the_list_was_kept(client, db_session, reports_root):
    _as("op", "admin")
    _post_fixture(client, VALID)
    run = next((reports_root / VALID_ID).glob("import-report_*_run0001.json"))
    assert json.loads(run.read_text(encoding="utf-8"))["listStored"] is True


@pytest.mark.unit
def test_a_reimport_of_the_same_file_keeps_the_existing_copy(client, db_session, reports_root):
    _as("op", "admin")
    _post_fixture(client, VALID)
    path = _list_file(reports_root, VALID_ID)
    before = path.stat()

    resp = _post_fixture(client, VALID)
    assert resp.status_code == 200
    assert resp.json()["listStored"] is True
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert path.read_bytes() == VALID.read_bytes()
    assert not list((reports_root / VALID_ID).glob(".tmp-*"))


@pytest.mark.unit
def test_a_list_that_cannot_be_kept_does_not_undo_the_import(client, db_session, reports_root, monkeypatch):
    import app.core.list_import as li

    def fail(list_id, raw):
        raise OSError("disk full")

    monkeypatch.setattr(li, "store_list_file", fail)
    _as("op", "admin")
    resp = _post_fixture(client, VALID)
    assert resp.status_code == 200
    report = resp.json()
    assert report["listStored"] is False
    assert report["reportStored"] is True
    assert report["created"] == 4
    assert len(_volumes(db_session)) == 4
    assert not _list_file(reports_root, VALID_ID).exists()


@pytest.mark.unit
def test_a_refused_list_is_not_kept(client, db_session, reports_root):
    _as("op", "admin")
    doc = json.loads(VALID.read_text(encoding="utf-8"))
    doc["kind"] = "qc-decisions"
    assert _post(client, doc).status_code == 422
    assert not _list_file(reports_root, VALID_ID).exists()


@pytest.mark.unit
def test_the_list_file_is_written_without_hard_links_too(client, db_session, reports_root, monkeypatch):
    """On a filesystem without hard links (FAT, exFAT) the file is created with O_EXCL instead."""
    import os

    def no_links(src, dst):
        raise OSError("operation not permitted")

    monkeypatch.setattr(os, "link", no_links)
    _as("op", "admin")
    resp = _post_fixture(client, VALID)
    assert resp.json()["listStored"] is True
    assert _list_file(reports_root, VALID_ID).read_bytes() == VALID.read_bytes()

    # And a re-import there keeps the copy too.
    assert _post_fixture(client, VALID).json()["listStored"] is True
    assert _list_file(reports_root, VALID_ID).read_bytes() == VALID.read_bytes()


# ---------------------------------------------------------------------------
# The action field
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("action", ["spot-recapture", "full-rescan", "full-digitisation", "complete", "anything else"])
def test_an_item_action_lands_under_fisqua_unvalidated(client, db_session, reports_root, action):
    _as("op", "admin")
    report = _post(client, _list_with([_item(action=action)])).json()
    assert report["created"] == 1 and report["rejects"] == []
    attrs = json.loads(_volumes(db_session)["xx-test-901"].custom_attributes)
    assert attrs["fisqua"]["action"] == action


# ---------------------------------------------------------------------------
# GET /imports/digitisation-lists: permissions
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_lists_need_authentication(client, db_session, reports_root):
    assert client.get("/imports/digitisation-lists").status_code == 401


@pytest.mark.unit
@pytest.mark.parametrize("role", ["admin", "operator", "reviewer"])
def test_every_role_can_read_the_lists(client, db_session, reports_root, role):
    from app.main import app
    import app.api.imports as imports
    from app.api.auth import get_current_user
    from app.models.user import User

    _as("op", "admin")
    _post_fixture(client, VALID)
    # The role check itself, not the override _as puts in its place.
    for dep in (imports.allow_contributor, imports.allow_read_only):
        app.dependency_overrides.pop(dep)
    user = User(username="u", email="u@example.com", hashed_password="x", role=role, is_active=True)
    app.dependency_overrides[get_current_user] = lambda: user
    entries = _lists(client)
    assert [e["listId"] for e in entries] == [VALID_ID]


# ---------------------------------------------------------------------------
# GET /imports/digitisation-lists: the lists and their order
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_no_lists_imported(client, db_session, reports_root):
    _as("op", "admin")
    assert _lists(client) == []


@pytest.mark.unit
def test_lists_come_newest_import_first(client, db_session, reports_root):
    _as("op", "admin")
    _post_fixture(client, VALID)
    _post_fixture(client, REJECTS)
    entries = _lists(client)
    assert [e["listId"] for e in entries] == [REJECTS_ID, VALID_ID]
    assert entries[0]["importedAt"] >= entries[1]["importedAt"]

    # Importing VALID again makes it the newest import.
    _post_fixture(client, VALID)
    assert [e["listId"] for e in _lists(client)] == [VALID_ID, REJECTS_ID]


@pytest.mark.unit
def test_a_list_carries_its_name_project_and_import_time(client, db_session, reports_root):
    _as("op", "admin")
    report = _post_fixture(client, VALID).json()
    entry = _one(client, VALID_ID)
    assert entry["listName"] == "Prueba válida"
    assert entry["projectId"] == report["projectId"]
    assert entry["importedAt"] == report["importedAt"]
    assert entry["listFileMissing"] is False


@pytest.mark.unit
def test_items_come_in_sequence_then_file_order(client, db_session, reports_root):
    _as("op", "admin")
    items = [
        _item(code="xx-test-901", stem="xx-test-901", sequence=3),
        _item(code="xx-test-902", stem="xx-test-902", sequence=None),
        _item(code="xx-test-903", stem="xx-test-903", sequence=1),
        _item(code="xx-test-904", stem="xx-test-904", sequence=3),
        _item(code="xx-test-905", stem="xx-test-905", sequence=2),
    ]
    _post(client, _list_with(items))
    entry = _one(client, VALID_ID)
    got = [(i["position"], i["sequence"], i["referenceCode"]) for i in entry["items"]]
    assert got == [
        (3, 1, "xx-test-903"),
        (5, 2, "xx-test-905"),
        (1, 3, "xx-test-901"),
        (4, 3, "xx-test-904"),
        (2, None, "xx-test-902"),
    ]


@pytest.mark.unit
def test_an_item_carries_its_fields_notes_verbatim_and_action(client, db_session, reports_root):
    _as("op", "admin")
    notes = "117r, 116v; 193r, 192v\n  y desde el folio 49r hasta el final  "
    items = [
        _item(code="xx-test-901", stem="xx-test-901", title="Tomo 1", notes=notes, action="spot-recapture"),
        _item(code="xx-test-902", stem="xx-test-902", title="Tomo 2", sequence=2),
    ]
    _post(client, _list_with(items))
    first, second = _one(client, VALID_ID)["items"]
    assert first["position"] == 1
    assert first["sequence"] == 1
    assert first["referenceCode"] == "xx-test-901"
    assert first["filenameStem"] == "xx-test-901"
    assert first["title"] == "Tomo 1"
    assert first["notes"] == notes
    assert first["action"] == "spot-recapture"
    assert second["notes"] is None
    assert second["action"] is None


@pytest.mark.unit
def test_rejected_items_are_listed_too(client, db_session, reports_root):
    """The list is the file, not what the import made of it: a skipped item keeps its place."""
    _as("op", "admin")
    _post_fixture(client, REJECTS)
    doc = json.loads(REJECTS.read_text(encoding="utf-8"))
    objects = [i for i in doc["items"] if isinstance(i, dict)]
    assert len(_one(client, REJECTS_ID)["items"]) == len(objects)


# ---------------------------------------------------------------------------
# The volume, joined by stem, and its status counts
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_each_item_is_joined_to_the_volume_with_its_stem(client, db_session, reports_root):
    from tests.unit.test_record_status_history import _record

    _as("op", "admin")
    _post_fixture(client, VALID)
    vols = _volumes(db_session)
    v1 = vols["xx-test-001"]
    _record(db_session, status="captured", col=v1, name="a.jpg")
    _record(db_session, status="rejected", col=v1, name="b.jpg")
    _record(db_session, status="approved", col=v1, name="c.jpg")

    items = {i["filenameStem"]: i for i in _one(client, VALID_ID)["items"]}
    assert items["xx-test-001"]["collectionId"] == v1.id
    assert items["xx-test-001"]["status"] == {"captured": 1, "in_review": 0, "rejected": 1, "approved": 1}
    assert items["xx-test-002"]["collectionId"] == vols["xx-test-002"].id
    assert items["xx-test-002"]["status"] == {"captured": 0, "in_review": 0, "rejected": 0, "approved": 0}


@pytest.mark.unit
def test_the_counts_are_the_ones_activity_reports(client, db_session, reports_root):
    from tests.unit.test_record_status_history import _record

    _as("op", "admin")
    _post_fixture(client, VALID)
    for stem, statuses in {"xx-test-001": ["captured", "in_review"], "xx-test-003": ["approved"] * 3}.items():
        col = _volumes(db_session)[stem]
        for n, status in enumerate(statuses):
            _record(db_session, status=status, col=col, name=f"{stem}-{n}.jpg")

    from tests.unit.test_record_status_history import _client_as
    activity = {row["collection_id"]: row["status_counts"]
                for row in _client_as(client, "op", "operator").get("/activity/collections").json()}
    for item in _one(client, VALID_ID)["items"]:
        assert item["status"] == activity[item["collectionId"]]


@pytest.mark.unit
def test_an_item_whose_stem_has_no_volume_has_none(client, db_session, reports_root):
    _as("op", "admin")
    _post_fixture(client, VALID)
    from app.models.collection import Collection
    vol = _volumes(db_session)["xx-test-002"]
    db_session.delete(vol)
    db_session.commit()
    assert db_session.query(Collection).filter(Collection.filename_stem == "xx-test-002").first() is None

    items = {i["filenameStem"]: i for i in _one(client, VALID_ID)["items"]}
    assert items["xx-test-002"]["collectionId"] is None
    assert items["xx-test-002"]["status"] is None


@pytest.mark.unit
def test_the_join_is_by_stem_not_reference_code(client, db_session, reports_root):
    """A volume made on the device holds the stem with no external_id: the item is joined to it."""
    from app.models.collection import Collection
    from app.models.project import Project

    p = Project(name="Local")
    db_session.add(p)
    db_session.commit()
    local = Collection(name="local", project_id=p.id, filename_stem="xx-test-901")
    db_session.add(local)
    db_session.commit()

    _as("op", "admin")
    report = _post(client, _list_with([_item()])).json()
    assert report["created"] == 0  # filename_stem_on_unit
    (item,) = _one(client, VALID_ID)["items"]
    assert item["collectionId"] == local.id


@pytest.mark.unit
def test_reading_the_lists_writes_nothing(client, db_session, reports_root):
    from app.models.collection import Collection
    from app.models.project import Project

    _as("op", "admin")
    _post_fixture(client, VALID)
    before = (db_session.query(Collection).count(), db_session.query(Project).count())
    files_before = sorted(p.name for p in (reports_root / VALID_ID).iterdir())
    _lists(client)
    db_session.expire_all()
    assert (db_session.query(Collection).count(), db_session.query(Project).count()) == before
    assert sorted(p.name for p in (reports_root / VALID_ID).iterdir()) == files_before


# ---------------------------------------------------------------------------
# A list whose file is missing or unreadable
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_list_without_its_file_is_listed_empty(client, db_session, reports_root):
    """A list whose file was never kept (an import by an earlier version) has a report and no list.json."""
    _as("op", "admin")
    report = _post_fixture(client, VALID).json()
    _list_file(reports_root, VALID_ID).unlink()

    entry = _one(client, VALID_ID)
    assert entry["items"] == []
    assert entry["listFileMissing"] is True
    assert entry["listName"] == "Prueba válida"
    assert entry["projectId"] == report["projectId"]


@pytest.mark.unit
@pytest.mark.parametrize("content", [
    b"not json",
    b"\xff\xfe",
    b'{"contract": "fisqua-captua-exchange"}',
    json.dumps({"contract": "fisqua-captua-exchange", "version": "1.1", "kind": "digitisation-list",
                "id": REJECTS_ID, "list": {"name": "x"}, "items": []}).encode(),
])
def test_a_list_whose_file_cannot_be_read_is_listed_empty(client, db_session, reports_root, content):
    """Damaged, not a list, or another list's file (the last case)."""
    _as("op", "admin")
    _post_fixture(client, VALID)
    _list_file(reports_root, VALID_ID).write_bytes(content)

    entry = _one(client, VALID_ID)
    assert entry["items"] == []
    assert entry["listFileMissing"] is True


@pytest.mark.unit
def test_a_directory_with_no_readable_report_is_not_listed(client, db_session, reports_root):
    _as("op", "admin")
    _post_fixture(client, VALID)
    stray = reports_root / REJECTS_ID
    stray.mkdir()
    (stray / "sha256").write_text("0" * 64 + "\n")
    (reports_root / "not-a-uuid").mkdir()
    assert [e["listId"] for e in _lists(client)] == [VALID_ID]


# ---------------------------------------------------------------------------
# Numbers a float cannot hold are refused at parse
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("number", ["1e400", "-1e400", "1" + "0" * 400])
def test_a_number_too_large_for_a_float_is_refused_whole(client, db_session, reports_root, number):
    """json reads 1e400 as inf, which json.dumps then writes as the bare word Infinity."""
    _as("op", "admin")
    text = json.dumps(_list_with([_item(notes="PLACEHOLDER")])).replace('"PLACEHOLDER"', number)
    resp = _post(client, text.encode("utf-8"))
    assert resp.status_code == 400, resp.text
    assert "number" in resp.json()["detail"]
    assert _volumes(db_session) == {}
    assert not (reports_root / VALID_ID).exists()


@pytest.mark.unit
def test_an_ordinary_float_is_still_read(client, db_session, reports_root):
    _as("op", "admin")
    text = json.dumps(_list_with([_item(expectedImages="PLACEHOLDER")])).replace('"PLACEHOLDER"', "2.5e3")
    assert _post(client, text.encode("utf-8")).json()["created"] == 1
    attrs = json.loads(_volumes(db_session)["xx-test-901"].custom_attributes)
    assert attrs["fisqua"]["expectedImages"] == 2500.0


@pytest.mark.unit
@pytest.mark.parametrize("notes", [12, 1.5, ["117r"], {"folios": "117r"}, True])
def test_notes_that_are_not_a_string_are_shown_as_null(client, db_session, reports_root, notes):
    _as("op", "admin")
    _post(client, _list_with([_item(notes=notes)]))
    (item,) = _one(client, VALID_ID)["items"]
    assert item["notes"] is None


# ---------------------------------------------------------------------------
# Only stems the stem rule allows are looked up
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_stem_outside_the_stem_rule_is_not_queried(client, db_session, reports_root):
    """A NUL in a bound parameter is refused by psycopg on Postgres, failing the whole response."""
    from sqlalchemy import event

    _as("op", "admin")
    _post_fixture(client, VALID)
    doc = json.loads(VALID.read_text(encoding="utf-8"))
    bad = ["xx-test-001\u0000", "XX Test", "xx-test-001\n", "a" * 200, "xx--test"]
    for item, stem in zip(doc["items"], bad):
        item["filenameStem"] = stem
    doc["items"].append(dict(doc["items"][0], filenameStem="xx-test-002", referenceCode="xx-test-002"))
    _list_file(reports_root, VALID_ID).write_text(json.dumps(doc), encoding="utf-8")

    seen = []

    def capture(conn, cursor, statement, parameters, *args):
        seen.append(repr(parameters))

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        items = _one(client, VALID_ID)["items"]
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    by_stem = {i["filenameStem"]: i for i in items}
    for stem in bad[:4]:
        assert by_stem[stem]["collectionId"] is None and by_stem[stem]["status"] is None
    assert by_stem["xx-test-002"]["collectionId"] == _volumes(db_session)["xx-test-002"].id
    assert not any("\\x00" in p or "XX Test" in p or "\\n" in p for p in seen)


@pytest.mark.unit
def test_only_the_first_item_with_a_stem_is_joined_to_its_volume(client, db_session, reports_root):
    _as("op", "admin")
    items = [
        _item(code="xx-test-901", stem="xx-test-901", sequence=2),
        _item(code="xx-test-901", stem="xx-test-901", sequence=1),  # a repeat, first in list order
    ]
    report = _post(client, _list_with(items)).json()
    assert report["created"] == 1
    got = {i["position"]: i for i in _one(client, VALID_ID)["items"]}
    assert got[1]["collectionId"] == _volumes(db_session)["xx-test-901"].id
    assert got[1]["status"] is not None
    assert (got[2]["collectionId"], got[2]["status"]) == (None, None)


# ---------------------------------------------------------------------------
# A damaged copy is replaced on re-import
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_damaged_copy_is_replaced_by_a_reimport(client, db_session, reports_root):
    _as("op", "admin")
    _post_fixture(client, VALID)
    _list_file(reports_root, VALID_ID).write_bytes(b"{damaged")

    resp = _post_fixture(client, VALID)
    assert resp.json()["listStored"] is True
    assert _list_file(reports_root, VALID_ID).read_bytes() == VALID.read_bytes()
    assert not list((reports_root / VALID_ID).glob(".tmp-*"))
    assert _one(client, VALID_ID)["listFileMissing"] is False


@pytest.mark.unit
def test_a_damaged_copy_that_cannot_be_replaced_is_reported(client, db_session, reports_root, monkeypatch):
    import os

    _as("op", "admin")
    _post_fixture(client, VALID)
    _list_file(reports_root, VALID_ID).write_bytes(b"{damaged")

    def fail(src, dst):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(os, "replace", fail)
    resp = _post_fixture(client, VALID)
    assert resp.status_code == 200
    assert resp.json()["listStored"] is False
    assert _list_file(reports_root, VALID_ID).read_bytes() == b"{damaged"
    assert not list((reports_root / VALID_ID).glob(".tmp-*"))


# ---------------------------------------------------------------------------
# A list whose report could not be stored
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_list_with_its_file_and_no_report_is_listed_after_the_dated_ones(client, db_session, reports_root):
    _as("op", "admin")
    _post_fixture(client, VALID)
    _post_fixture(client, REJECTS)
    for run in (reports_root / REJECTS_ID).glob("import-report_*.json"):
        run.unlink()

    entries = _lists(client)
    assert [e["listId"] for e in entries] == [VALID_ID, REJECTS_ID]
    undated = entries[1]
    assert undated["listName"] == "Prueba de rechazos"
    assert (undated["projectId"], undated["importedAt"]) == (None, None)
    assert undated["listFileMissing"] is False
    assert undated["items"]


# ---------------------------------------------------------------------------
# Each item's volume's project
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_each_item_carries_its_volumes_project(client, db_session, reports_root):
    _as("op", "admin")
    report = _post(client, _list_with([
        _item(code="xx-test-901", stem="xx-test-901"),
        _item(code="xx-test-901", stem="xx-test-901", sequence=2),  # a repeat: no volume
    ])).json()
    first, repeat = _one(client, VALID_ID)["items"]
    assert first["projectId"] == report["projectId"]
    assert repeat["projectId"] is None


@pytest.mark.unit
def test_an_item_with_no_volume_has_no_project(client, db_session, reports_root):
    _as("op", "admin")
    _post(client, _list_with([_item(code="xx-test-901", stem="xx-test-901")]))
    db_session.delete(_volumes(db_session)["xx-test-901"])
    db_session.commit()
    (item,) = _one(client, VALID_ID)["items"]
    assert (item["collectionId"], item["projectId"]) == (None, None)


# ---------------------------------------------------------------------------
# Marking an item done by hand
# ---------------------------------------------------------------------------

def _as_role(username, role):
    """The real role checks, with get_current_user giving this user."""
    from app.main import app
    import app.api.imports as imports
    from app.api.auth import get_current_user
    from app.models.user import User

    for dep in (imports.allow_contributor, imports.allow_read_only):
        app.dependency_overrides.pop(dep, None)
    user = User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)
    app.dependency_overrides[get_current_user] = lambda: user


def _mark(client, position, done=True, list_id=VALID_ID):
    return client.put(f"/imports/digitisation-lists/{list_id}/items/{position}/done", json={"done": done})


def _marks(client, list_id=VALID_ID):
    return {i["position"]: i["doneMark"] for i in _one(client, list_id)["items"]}


@pytest.fixture
def imported(client, db_session, reports_root):
    _as("op", "admin")
    _post_fixture(client, VALID)
    return reports_root / VALID_ID


@pytest.mark.unit
@pytest.mark.parametrize("role, allowed", [("admin", True), ("reviewer", True), ("operator", False)])
def test_marking_done_is_admin_or_reviewer(client, imported, role, allowed):
    _as_role("u", role)
    resp = _mark(client, 1)
    assert resp.status_code == (200 if allowed else 403), resp.text
    assert (imported / "done.json").exists() is allowed


@pytest.mark.unit
def test_marking_done_needs_authentication(client, db_session, reports_root):
    assert _mark(client, 1).status_code == 401


@pytest.mark.unit
def test_a_mark_is_stored_and_shown_on_the_item(client, imported):
    _as_role("rev", "reviewer")
    resp = _mark(client, 2)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["listId"] == VALID_ID and body["position"] == 2
    assert body["doneMark"]["by"] == "rev"
    at = body["doneMark"]["at"]
    assert at.endswith("Z")

    stored = json.loads((imported / "done.json").read_text(encoding="utf-8"))
    assert stored == {"2": {"done": True, "by": "rev", "at": at}}
    assert not list(imported.glob(".tmp-*"))

    marks = _marks(client)
    assert marks[2] == {"by": "rev", "at": at}
    assert marks[1] is None and marks[3] is None


@pytest.mark.unit
def test_unmarking_removes_the_entry(client, imported):
    _as_role("rev", "reviewer")
    _mark(client, 1)
    _mark(client, 3)
    resp = _mark(client, 1, done=False)
    assert resp.status_code == 200
    assert resp.json()["doneMark"] is None
    stored = json.loads((imported / "done.json").read_text(encoding="utf-8"))
    assert set(stored) == {"3"}
    assert _marks(client)[1] is None and _marks(client)[3] is not None

    # Unmarking an item that is not marked is not an error.
    assert _mark(client, 2, done=False).status_code == 200


@pytest.mark.unit
def test_marking_again_keeps_one_entry(client, imported):
    _as_role("adm", "admin")
    _mark(client, 1)
    _mark(client, 1)
    assert set(json.loads((imported / "done.json").read_text(encoding="utf-8"))) == {"1"}


@pytest.mark.unit
def test_marking_an_unknown_list_or_position_is_404(client, imported):
    _as_role("rev", "reviewer")
    assert _mark(client, 1, list_id=REJECTS_ID).status_code == 404
    assert _mark(client, 99).status_code == 404
    assert _mark(client, 0).status_code == 404


@pytest.mark.unit
def test_a_position_that_is_not_an_item_is_404(client, db_session, reports_root):
    """A position the stored file holds no item object at: not shown, so not markable."""
    _as("op", "admin")
    _post(client, _list_with([_item(), "not an object"]))
    _as_role("rev", "reviewer")
    assert _mark(client, 2).status_code == 404
    assert _mark(client, 1).status_code == 200


@pytest.mark.unit
def test_marking_a_list_without_its_file_is_404(client, imported):
    (imported / "list.json").unlink()
    _as_role("rev", "reviewer")
    assert _mark(client, 1).status_code == 404


@pytest.mark.unit
@pytest.mark.parametrize("body", [{}, {"done": "yes"}, {"done": 1}, {"done": None}, ["done"]])
def test_a_malformed_body_is_422(client, imported, body):
    _as_role("rev", "reviewer")
    resp = client.put(f"/imports/digitisation-lists/{VALID_ID}/items/1/done", json=body)
    assert resp.status_code == 422
    assert not (imported / "done.json").exists()


@pytest.mark.unit
def test_a_list_id_that_is_not_a_uuid_is_422(client, imported):
    _as_role("rev", "reviewer")
    assert _mark(client, 1, list_id="not-a-uuid").status_code == 422
    assert client.put("/imports/digitisation-lists/not-a-uuid/items/x/done", json={"done": True}).status_code == 422


@pytest.mark.unit
@pytest.mark.parametrize("content", [
    b"{damaged",
    b"[1, 2]",
    b'{"1": "yes", "x": {"done": true}, "2": {"done": false, "by": "a", "at": "b"}}',
    b"\xff\xfe",
])
def test_a_damaged_done_file_reads_as_no_marks(client, imported, content):
    (imported / "done.json").write_bytes(content)
    _as("op", "admin")
    assert all(mark is None for mark in _marks(client).values())


@pytest.mark.unit
def test_marking_over_a_damaged_done_file_starts_afresh(client, imported):
    (imported / "done.json").write_bytes(b"{damaged")
    _as_role("rev", "reviewer")
    assert _mark(client, 1).status_code == 200
    assert set(json.loads((imported / "done.json").read_text(encoding="utf-8"))) == {"1"}


@pytest.mark.unit
def test_a_done_file_that_cannot_be_written_is_a_503_and_changes_nothing(client, imported, monkeypatch):
    import os

    _as_role("rev", "reviewer")
    _mark(client, 1)
    before = (imported / "done.json").read_bytes()

    def fail(src, dst):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(os, "replace", fail)
    assert _mark(client, 2).status_code == 503
    assert (imported / "done.json").read_bytes() == before
    assert not list(imported.glob(".tmp-*"))


@pytest.mark.unit
def test_marking_takes_the_list_lock(client, imported, monkeypatch):
    import app.core.list_import as li

    taken = []
    real = li._list_lock

    class spy(real):
        def __enter__(self):
            taken.append(self.path)
            return super().__enter__()

    monkeypatch.setattr(li, "_list_lock", spy)
    _as_role("rev", "reviewer")
    _mark(client, 1)
    assert taken == [imported / ".lock"]


@pytest.mark.unit
def test_marking_and_unmarking_are_logged(client, db_session, imported):
    from app.models.system_log import SystemLog

    _as_role("rev", "reviewer")
    _mark(client, 1)
    _mark(client, 1, done=False)
    rows = (db_session.query(SystemLog)
            .filter(SystemLog.action.like("digitisation_list_item_%"))
            .order_by(SystemLog.id).all())
    assert [r.action for r in rows] == ["digitisation_list_item_marked_done", "digitisation_list_item_unmarked"]
    assert all(r.actor == "rev" and r.category == "activity" for r in rows)
    assert rows[0].subject == "Prueba válida"
    assert "xx-test-001" in rows[0].detail
