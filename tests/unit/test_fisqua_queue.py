"""The digitisation queue synced with Fisqua (contract 1.7).

The unit sends its users (PUT /api/capture/v1/operators), polls the queue
(GET /api/capture/v1/queue) with the status poll, applies each revision to
its volumes, and acknowledges it (POST /api/capture/v1/queue/ack). The
applied queue is a list in GET /imports/digitisation-lists beside file
imports.

    python -m pytest tests/unit/test_fisqua_queue.py

Version: 0.3.0
"""

import json

import httpx
import pytest

from tests.unit.test_capture_manifest_export import tenant  # noqa: F401  (fixture)
from tests.unit.test_fisqua_outbox import TOKEN, fisqua  # noqa: F401  (fixture)
from tests.unit.test_list_import import (  # noqa: F401  (reports_root is a fixture)
    _as as _as_importer, _item, _list_with, _post, reports_root,
)

LIST_NAME = "Cola del escáner"
BASE = "https://fisqua.test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result(outcome, **kw):
    from app.core.fisqua_outbox import TransportResult
    return TransportResult(outcome=outcome, **kw)


class QueueTransport:
    """Answers the 1.7 calls from scripts and keeps what each was sent."""

    def __init__(self, operators=(), queues=(), acks=()):
        self.operators_answers = list(operators)
        self.queue_answers = list(queues)
        self.ack_answers = list(acks)
        self.operators_sent = []
        self.queue_asked = []
        self.acks_sent = []

    def _next(self, script):
        answer = script.pop(0) if script else _result("error", error="no answer")
        if isinstance(answer, Exception):
            raise answer
        return answer

    def put_operators(self, body):
        self.operators_sent.append(body)
        return self._next(self.operators_answers)

    def get_queue(self, revision):
        self.queue_asked.append(revision)
        return self._next(self.queue_answers)

    def post_queue_ack(self, body):
        self.acks_sent.append(json.loads(json.dumps(body)))
        return self._next(self.ack_answers)


def _queue(items, revision="r1", list_name=LIST_NAME):
    body = {"revision": revision, "listName": list_name, "items": items}
    return _result("queue", response=body)


def _qitem(code="xx-test-901", item_id=None, **kw):
    item = {
        "itemId": item_id or f"00000000-0000-4000-8000-{abs(hash(code)) % 10**12:012d}",
        "referenceCode": code, "filenameStem": code, "sequence": 1, "title": f"Tomo {code}",
        "dates": {"display": "1790", "start": None, "end": None}, "extent": None,
        "containerPath": [], "action": "full-digitisation", "notes": None,
        "assignee": None, "withdrawn": False,
    }
    item.update(kw)
    return item


def _ok_ack():
    return _result("acknowledged")


def _sync(db, transport, now=None):
    from app.core.fisqua_queue import sync_fisqua_queue
    return sync_fisqua_queue(db, transport=transport, now=now)


def _operators_sent_already(monkeypatch):
    """Operators are due at start; most tests are about the queue, so mark them sent."""
    from app.core import fisqua_queue
    monkeypatch.setattr(fisqua_queue, "_operators", fisqua_queue._OperatorsState(due=False))


@pytest.fixture
def synced(fisqua, reports_root, monkeypatch):
    _operators_sent_already(monkeypatch)
    return reports_root


def _volumes(db):
    from app.models.collection import Collection
    db.expire_all()
    return {c.external_id: c for c in db.query(Collection).all()}


def _user(db, username, role, active=True):
    from app.models.user import User
    user = User(username=username, email=f"{username}@example.com", hashed_password="x",
                role=role, is_active=active)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _record(db, collection_id):
    from app.models.record import Record
    rec = Record(collection_id=collection_id, title="f1", status="captured", capture_mode="single")
    db.add(rec)
    db.commit()
    return rec


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_operators_body_has_admins_and_operators_only_by_username_without_email(db_session):
    from app.core.fisqua_queue import operators_body
    _user(db_session, "ana", "admin")
    _user(db_session, "beto", "operator", active=False)
    _user(db_session, "carla", "reviewer")
    body = operators_body(db_session)
    assert body == {"operators": [
        {"username": "ana", "displayName": "ana", "role": "admin", "active": True},
        {"username": "beto", "displayName": "beto", "role": "operator", "active": False},
    ]}
    assert "@" not in json.dumps(body)


@pytest.fixture
def admin_client(client, db_session, fisqua, monkeypatch):
    from app.main import app
    from app.api.auth import get_current_user
    from app.core import fisqua_queue
    admin = _user(db_session, "admin_caller", "admin")
    _user(db_session, "admin_other", "admin")
    app.dependency_overrides[get_current_user] = lambda: admin
    transport = QueueTransport(operators=[_result("delivered")] * 10)
    monkeypatch.setattr(fisqua_queue, "get_transport", lambda: transport)
    _operators_sent_already(monkeypatch)
    return client, transport


@pytest.mark.unit
def test_operators_are_sent_when_a_user_is_created_changed_or_deleted(admin_client, db_session):
    client, transport = admin_client
    from app.core.security import create_access_token
    resp = client.post("/auth/register", json={"username": "dora", "password": "secret-pass-1"},
                       headers={"Authorization": f"Bearer {create_access_token(subject='1')}"})
    assert resp.status_code == 200, resp.text
    assert len(transport.operators_sent) == 1
    uid = resp.json()["id"]

    assert client.patch(f"/auth/users/{uid}/role", json={"role": "operator"}).status_code == 200
    assert len(transport.operators_sent) == 2
    assert {"username": "dora", "displayName": "dora", "role": "operator", "active": True} \
        in transport.operators_sent[-1]["operators"]

    assert client.patch(f"/auth/users/{uid}/active", params={"is_active": False}).status_code == 200
    assert len(transport.operators_sent) == 3
    assert {"username": "dora", "displayName": "dora", "role": "operator", "active": False} \
        in transport.operators_sent[-1]["operators"]

    assert client.delete(f"/auth/users/{uid}").status_code == 200
    assert len(transport.operators_sent) == 4
    assert all(o["username"] != "dora" for o in transport.operators_sent[-1]["operators"])


@pytest.mark.unit
def test_a_failed_operators_send_does_not_fail_the_route_and_is_retried_at_the_next_cycle(
        admin_client, db_session, synced):
    client, transport = admin_client
    transport.operators_answers = [RuntimeError("boom"), _result("error", error="HTTP 500"),
                                   _result("delivered")]
    victim = _user(db_session, "victim", "operator")
    assert client.delete(f"/auth/users/{victim.id}").status_code == 200
    assert client.patch(f"/auth/users/{victim.id - 1}/role", json={"role": "operator"}).status_code == 200
    assert len(transport.operators_sent) == 2

    transport.queue_answers = [_result("not_modified")]
    _sync(db_session, transport)
    assert len(transport.operators_sent) == 3
    # Sent now: not sent again at the next tick.
    transport.queue_answers = [_result("not_modified")]
    _sync(db_session, transport)
    assert len(transport.operators_sent) == 3


@pytest.mark.unit
def test_operators_are_sent_at_start_and_again_after_a_day(fisqua, reports_root, db_session, monkeypatch):
    from app.core import fisqua_queue
    monkeypatch.setattr(fisqua_queue, "_operators", fisqua_queue._OperatorsState())
    _user(db_session, "ana", "operator")
    clock = [1000.0]
    monkeypatch.setattr(fisqua_queue, "_monotonic", lambda: clock[0])
    transport = QueueTransport(operators=[_result("delivered")] * 3,
                               queues=[_result("not_modified")] * 3)
    _sync(db_session, transport)
    assert len(transport.operators_sent) == 1
    clock[0] += 3600
    _sync(db_session, transport)
    assert len(transport.operators_sent) == 1
    clock[0] += 86400
    _sync(db_session, transport)
    assert len(transport.operators_sent) == 2


# ---------------------------------------------------------------------------
# The poll
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_poll_sends_if_none_match_and_does_nothing_on_304(synced, db_session):
    seen = []

    def server(request):
        seen.append(request)
        if request.url.path.endswith("/queue/ack"):
            return httpx.Response(204)
        if request.headers.get("If-None-Match") == '"r1"':
            return httpx.Response(304)
        return httpx.Response(200, headers={"ETag": '"r1"'},
                              json={"revision": "r1", "listName": LIST_NAME, "items": [_qitem()]})

    from app.core.fisqua_http import HttpFisquaTransport
    transport = HttpFisquaTransport(BASE, TOKEN, client=httpx.Client(transport=httpx.MockTransport(server)))
    first = _sync(db_session, transport)
    assert first["status"] == "applied", first
    assert "If-None-Match" not in seen[0].headers
    assert seen[0].url.path == "/api/capture/v1/queue"
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"

    before = sorted(p.name for p in (synced).rglob("*"))
    seen.clear()
    second = _sync(db_session, transport)
    assert second["status"] == "unchanged"
    assert [r.headers.get("If-None-Match") for r in seen] == ['"r1"']
    assert sorted(p.name for p in synced.rglob("*")) == before


@pytest.mark.unit
def test_the_hold_is_respected(synced, db_session, monkeypatch, tmp_path):
    from app.core import config
    hold = tmp_path / "offsite-hold"
    hold.write_text("")
    monkeypatch.setattr(config.settings, "OFFSITE_HOLD_FILE", str(hold))
    from app.core import fisqua_queue
    monkeypatch.setattr(fisqua_queue, "_operators", fisqua_queue._OperatorsState())
    transport = QueueTransport(queues=[_queue([_qitem()])])
    result = _sync(db_session, transport)
    assert result == {"status": "held", "hold_file": str(hold)}
    assert transport.queue_asked == [] and transport.operators_sent == []
    assert _volumes(db_session) == {}


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_new_item_creates_its_volume_in_the_list_project_created_if_missing(synced, db_session):
    from app.models.project import Project
    transport = QueueTransport(queues=[_queue([_qitem(assignee="ana", notes="Con cuidado")])],
                               acks=[_ok_ack()])
    result = _sync(db_session, transport)
    assert result["status"] == "applied", result
    vol = _volumes(db_session)["xx-test-901"]
    project = db_session.get(Project, vol.project_id)
    assert project.name == LIST_NAME
    assert vol.filename_stem == "xx-test-901" and vol.name == "Tomo xx-test-901"
    assert vol.assignee_username == "ana"
    assert json.loads(vol.custom_attributes)["fisqua"]["notes"] == "Con cuidado"


@pytest.mark.unit
def test_a_new_item_goes_into_an_existing_project_of_that_name(synced, db_session):
    from app.models.project import Project
    existing = Project(name=LIST_NAME)
    db_session.add(existing)
    db_session.commit()
    _sync(db_session, QueueTransport(queues=[_queue([_qitem()])], acks=[_ok_ack()]))
    assert _volumes(db_session)["xx-test-901"].project_id == existing.id
    assert db_session.query(Project).count() == 1


@pytest.mark.unit
def test_an_existing_volume_is_updated_and_keeps_its_project_stem_and_code(synced, db_session):
    from app.models.collection import Collection
    from app.models.project import Project
    other = Project(name="Otro fondo")
    db_session.add(other)
    db_session.commit()
    vol = Collection(project_id=other.id, name="Tomo propio", external_id="xx-test-901",
                     filename_stem="xx-test-901", sequence=9,
                     custom_attributes=json.dumps({"fisqua": {"action": "complete", "keep": 1}, "mine": True}))
    db_session.add(vol)
    db_session.commit()
    item = _qitem(sequence=3, action="spot-recapture", notes="Repetir 12r", assignee="beto",
                  title="Otro título")
    transport = QueueTransport(queues=[_queue([item])], acks=[_ok_ack()])
    result = _sync(db_session, transport)
    assert result["status"] == "applied", result
    vol = _volumes(db_session)["xx-test-901"]
    assert (vol.project_id, vol.filename_stem, vol.external_id, vol.name) == \
        (other.id, "xx-test-901", "xx-test-901", "Tomo propio")
    assert vol.sequence == 3 and vol.assignee_username == "beto"
    attrs = json.loads(vol.custom_attributes)
    assert attrs["mine"] is True and attrs["fisqua"]["keep"] == 1
    assert attrs["fisqua"]["action"] == "spot-recapture" and attrs["fisqua"]["notes"] == "Repetir 12r"
    assert transport.acks_sent[0]["applied"] == [{"itemId": item["itemId"], "collectionId": vol.id}]
    # No project named after the list was made for an update.
    assert db_session.query(Project).filter(Project.name == LIST_NAME).count() == 0


@pytest.mark.unit
def test_a_queue_update_never_changes_a_record_status(synced, db_session):
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record
    project = Project(name=LIST_NAME)
    db_session.add(project)
    db_session.commit()
    vol = Collection(project_id=project.id, name="T", external_id="xx-test-901", filename_stem="xx-test-901")
    db_session.add(vol)
    db_session.commit()
    rec = Record(collection_id=vol.id, title="f1", status="approved", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    _sync(db_session, QueueTransport(queues=[_queue([_qitem(action="spot-recapture",
                                                            unnamedRetakeField=[{"captuaRecordId": rec.id}])])],
                                     acks=[_ok_ack()]))
    db_session.expire_all()
    assert db_session.get(Record, rec.id).status == "approved"


@pytest.mark.unit
def test_a_withdrawn_item_without_records_hides_its_volume_and_a_later_item_unhides_it(synced, db_session):
    item = _qitem()
    _sync(db_session, QueueTransport(queues=[_queue([item], "r1")], acks=[_ok_ack()]))
    vol_id = _volumes(db_session)["xx-test-901"].id

    transport = QueueTransport(queues=[_queue([{**item, "withdrawn": True}], "r2")], acks=[_ok_ack()])
    _sync(db_session, transport)
    vol = _volumes(db_session)["xx-test-901"]
    assert vol.id == vol_id and vol.queue_hidden_at is not None
    assert transport.acks_sent[0]["applied"] == [{"itemId": item["itemId"], "collectionId": vol_id}]

    again = _qitem(item_id="00000000-0000-4000-8000-000000000777")
    _sync(db_session, QueueTransport(queues=[_queue([again], "r3")], acks=[_ok_ack()]))
    vol = _volumes(db_session)["xx-test-901"]
    assert vol.id == vol_id and vol.queue_hidden_at is None


@pytest.mark.unit
def test_a_withdrawn_item_whose_volume_has_records_changes_nothing_and_is_applied(synced, db_session):
    item = _qitem(assignee="ana")
    _sync(db_session, QueueTransport(queues=[_queue([item], "r1")], acks=[_ok_ack()]))
    vol = _volumes(db_session)["xx-test-901"]
    _record(db_session, vol.id)
    before = (vol.sequence, vol.assignee_username, vol.custom_attributes, vol.queue_hidden_at)

    transport = QueueTransport(queues=[_queue([{**item, "withdrawn": True, "assignee": None,
                                                "sequence": 7}], "r2")], acks=[_ok_ack()])
    _sync(db_session, transport)
    vol = _volumes(db_session)["xx-test-901"]
    assert (vol.sequence, vol.assignee_username, vol.custom_attributes, vol.queue_hidden_at) == before
    assert transport.acks_sent[0]["applied"] == [{"itemId": item["itemId"], "collectionId": vol.id}]
    assert transport.acks_sent[0]["skipped"] == []


@pytest.mark.unit
def test_invalid_items_are_skipped_with_reason_codes(synced, db_session):
    from app.models.collection import Collection
    from app.models.project import Project
    project = Project(name="Otro")
    db_session.add(project)
    db_session.commit()
    db_session.add(Collection(project_id=project.id, name="sin código", filename_stem="xx-test-904"))
    db_session.commit()
    items = [
        _qitem("xx-test-901", filenameStem="xx-test-999"),
        _qitem("xx-test-902", title=None),
        _qitem("XX Test 903", filenameStem="XX Test 903"),
        _qitem("xx-test-904"),
        _qitem("xx-test-905", sequence="primero"),
        _qitem("xx-test-906", assignee=7),
        _qitem("xx-test-907"),
    ]
    transport = QueueTransport(queues=[_queue(items)], acks=[_ok_ack()])
    _sync(db_session, transport)
    [ack] = transport.acks_sent
    reasons = {s["itemId"]: s["reason"] for s in ack["skipped"]}
    assert [reasons.get(i["itemId"]) for i in items] == [
        "filename_stem_differs_from_reference_code", "missing_title", "filename_stem_not_a_slug",
        "filename_stem_on_unit", "invalid_sequence", "invalid_assignee", None,
    ]
    assert [a["itemId"] for a in ack["applied"]] == [items[-1]["itemId"]]


# ---------------------------------------------------------------------------
# The ack
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_ack_body_and_a_failed_ack_is_retried_with_the_same_body(synced, db_session):
    good, bad = _qitem("xx-test-901"), _qitem("xx-test-902", title=None)
    transport = QueueTransport(queues=[_queue([good, bad], "r1")],
                               acks=[_result("error", error="HTTP 503")])
    result = _sync(db_session, transport)
    assert result["status"] == "ack_failed"
    vol = _volumes(db_session)["xx-test-901"]
    expected = {"revision": "r1",
                "applied": [{"itemId": good["itemId"], "collectionId": vol.id}],
                "skipped": [{"itemId": bad["itemId"], "reason": "missing_title"}]}
    assert transport.acks_sent == [expected]

    # Next poll: the same body again, before any GET; the revision is kept only now.
    transport.ack_answers = [_ok_ack()]
    transport.queue_answers = [_result("not_modified")]
    _sync(db_session, transport)
    assert transport.acks_sent == [expected, expected]
    assert transport.queue_asked == [None, "r1"]


@pytest.mark.unit
def test_the_revision_is_not_kept_while_the_ack_fails(synced, db_session):
    transport = QueueTransport(queues=[_queue([_qitem()], "r1")],
                               acks=[_result("error", error="x"), _result("error", error="y")])
    _sync(db_session, transport)
    _sync(db_session, transport)
    # The queue is not asked for again while the ack is owed.
    assert transport.queue_asked == [None]
    assert len(transport.acks_sent) == 2


# ---------------------------------------------------------------------------
# Cola: the queue in GET /imports/digitisation-lists
# ---------------------------------------------------------------------------

def _lists(client):
    _as_importer("op", "operator")
    resp = client.get("/imports/digitisation-lists")
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.unit
def test_the_queue_appears_in_the_digitisation_lists(client, synced, db_session):
    from app.core.fisqua_queue import QUEUE_LIST_ID
    items = [_qitem("xx-test-901", assignee="ana", sequence=2),
             _qitem("xx-test-902", sequence=1, withdrawn=True)]
    _sync(db_session, QueueTransport(queues=[_queue(items)], acks=[_ok_ack()]))
    [queue] = [entry for entry in _lists(client) if entry["listId"] == QUEUE_LIST_ID]
    assert queue["source"] == "queue" and queue["listName"] == LIST_NAME
    assert queue["projectId"] is not None and queue["importedAt"]
    by_code = {i["referenceCode"]: i for i in queue["items"]}
    assert by_code["xx-test-901"]["assignee"] == "ana"
    assert by_code["xx-test-901"]["withdrawn"] is False
    assert by_code["xx-test-901"]["collectionId"] == _volumes(db_session)["xx-test-901"].id
    assert by_code["xx-test-902"]["withdrawn"] is True


@pytest.mark.unit
def test_a_file_import_beside_the_queue_has_its_own_source_and_null_assignee(client, synced, db_session):
    _as_importer("op", "admin")
    assert _post(client, _list_with([_item("xx-test-950", "xx-test-950")])).status_code == 200
    _sync(db_session, QueueTransport(queues=[_queue([_qitem()])], acks=[_ok_ack()]))
    lists = _lists(client)
    sources = sorted(entry["source"] for entry in lists)
    assert sources == ["file", "queue"]
    [file_list] = [e for e in lists if e["source"] == "file"]
    assert file_list["items"][0]["assignee"] is None and file_list["items"][0]["withdrawn"] is False


@pytest.mark.unit
def test_a_file_upload_cannot_take_the_queue_list_id(client, synced, db_session):
    from app.core.fisqua_queue import QUEUE_LIST_ID
    _sync(db_session, QueueTransport(queues=[_queue([_qitem()])], acks=[_ok_ack()]))
    _as_importer("op", "admin")
    resp = _post(client, _list_with([_item("xx-test-950", "xx-test-950")], id=QUEUE_LIST_ID))
    assert resp.status_code == 409


@pytest.mark.unit
def test_notes_come_from_the_newest_of_a_file_import_and_a_queue_revision(client, synced, db_session):
    """The band reads the first list in GET /imports/digitisation-lists that names the volume."""
    import time

    def first_notes():
        vid = _volumes(db_session)["xx-test-901"].id
        for entry in _lists(client):
            for item in entry["items"]:
                if item["collectionId"] == vid:
                    return item["notes"]
        return None

    _as_importer("op", "admin")
    assert _post(client, _list_with([_item(notes="del archivo")])).status_code == 200
    time.sleep(0.01)
    _sync(db_session, QueueTransport(queues=[_queue([_qitem(notes="de la cola")], "r1")], acks=[_ok_ack()]))
    assert first_notes() == "de la cola"

    time.sleep(0.01)
    _as_importer("op", "admin")
    doc = _list_with([_item(notes="archivo nuevo")], id="00000000-0000-4000-8000-0000000000aa")
    assert _post(client, doc).status_code == 200
    assert first_notes() == "archivo nuevo"


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------

def _http(server):
    from app.core.fisqua_http import HttpFisquaTransport
    return HttpFisquaTransport(BASE, TOKEN, client=httpx.Client(transport=httpx.MockTransport(server)))


@pytest.mark.unit
def test_the_transport_paths_and_answers():
    seen = []

    def server(request):
        seen.append(request)
        path = request.url.path
        if path == "/api/capture/v1/operators":
            return httpx.Response(204)
        if path == "/api/capture/v1/queue/ack":
            return httpx.Response(204)
        return httpx.Response(304)

    t = _http(server)
    assert t.put_operators({"operators": []}).outcome == "delivered"
    assert t.get_queue("r9").outcome == "not_modified"
    assert t.post_queue_ack({"revision": "r9", "applied": [], "skipped": []}).outcome == "acknowledged"
    assert [(r.method, r.url.path) for r in seen] == [
        ("PUT", "/api/capture/v1/operators"), ("GET", "/api/capture/v1/queue"),
        ("POST", "/api/capture/v1/queue/ack")]
    assert seen[1].headers["If-None-Match"] == '"r9"'


@pytest.mark.unit
@pytest.mark.parametrize("code,outcome", [(401, "config_error"), (403, "config_error"),
                                          (404, "error"), (500, "error"), (422, "refused")])
def test_the_transport_reads_failures(code, outcome):
    t = _http(lambda request: httpx.Response(code, json={"problems": [{"rule": "x"}]}))
    assert t.put_operators({"operators": []}).outcome == outcome


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------

def _load_migration():
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    [path] = (root / "alembic" / "versions").glob("*_collection_queue_fields.py")
    spec = importlib.util.spec_from_file_location("collection_queue_fields_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(engine, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


@pytest.mark.unit
def test_the_migration_follows_the_excluded_revision():
    assert _load_migration().down_revision == "33773ea9f2b8"


@pytest.mark.unit
def test_the_migration_upgrade_and_downgrade(tmp_path):
    import sqlalchemy as sa
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    md = sa.MetaData()
    sa.Table("collections", md, sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("name", sa.String(255), nullable=False))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO collections (id, name) VALUES (1, 'T')"))
    mod = _load_migration()

    _run(engine, mod.upgrade)
    cols = {c["name"]: c for c in sa.inspect(engine).get_columns("collections")}
    assert cols["assignee_username"]["nullable"] and cols["queue_hidden_at"]["nullable"]
    assert cols["assignee_username"]["type"].length == 150
    assert isinstance(cols["queue_hidden_at"]["type"], sa.DateTime)
    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT name, assignee_username FROM collections")).all() == [("T", None)]

    _run(engine, mod.downgrade)
    cols = {c["name"] for c in sa.inspect(engine).get_columns("collections")}
    assert cols == {"id", "name"}


@pytest.mark.unit
def test_the_collection_read_schema_exposes_assignee_and_hidden():
    from app.schemas.collection import CollectionRead, CollectionUpdate
    assert {"assignee_username", "queue_hidden_at"} <= set(CollectionRead.model_fields)
    assert not {"assignee_username", "queue_hidden_at"} & set(CollectionUpdate.model_fields)
