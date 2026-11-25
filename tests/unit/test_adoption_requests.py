"""An adoption a client named with its own id: what it records, and what a repeat or a lookup answers.

POST /collections/{source}/adopt-into/{target} may carry `client_request_id`
(app.core.adoption_requests). The unit records the request when it takes
ADOPTION_LOCK and its outcome when the request ends; a second POST with the
same id never starts a second adoption; GET /system/adoption/requests/{id}
answers the request's state. A process death leaves the record without an
outcome (`interrupted`) until startup recovery settles the journal, which
carries the id. GET /system/adoption reports what holds ADOPTION_LOCK
(`running`), and POST /system/adoption/repair takes the journal's `run_id`.

The volumes, the kill points and the bucket are those of
tests/unit/test_adopt_apply.py, tests/unit/test_adopt_journal.py and
tests/unit/test_adopt_repair.py.

    python -m pytest tests/unit/test_adoption_requests.py

Version: 0.3.3
"""

import json
import threading
import uuid

import pytest

from tests.unit.test_adopt_apply import NOW, _adopt, _failing_rename, _journal, _setup, _snapshot
from tests.unit.test_adopt_apply_routes import empty_bucket  # noqa: F401
from tests.unit.test_adopt_captures_plan import SRC, TGT, _record, own_data_dir, tenant  # noqa: F401
from tests.unit.test_adopt_journal import Killed, _kill_after_renames, _kill_at_phase, _recover
from tests.unit.test_adopt_repair import _blocked, _fill_bucket
from tests.unit.test_image_upload import remote  # noqa: F401
from tests.unit.test_offsite_free import _as, read_remote  # noqa: F401


@pytest.fixture
def kill():
    with pytest.MonkeyPatch.context() as patch:
        yield patch


def _url(src, tgt):
    return f"/collections/{src.id}/adopt-into/{tgt.id}"


def _post(client, src, tgt, rid=None):
    return client.post(_url(src, tgt), json={"client_request_id": rid} if rid is not None else None)


def _lookup(client, rid):
    return client.get(f"/system/adoption/requests/{rid}")


def _rows(db):
    from app.models.adoption_request import AdoptionRequest

    db.expire_all()
    return db.query(AdoptionRequest).order_by(AdoptionRequest.id).all()


def _but_requests(snapshot):
    """A _snapshot with the adoption_requests table left out."""
    rows, files = snapshot
    return {k: v for k, v in rows.items() if k != "adoption_requests"}, files


def _paused_adoption(src_id, tgt_id, rid, kill):
    """Run the adoption with `rid` in a thread, paused at its bucket listing (before the journal); (thread, go, out)."""
    from app.core.adopt_apply import adopt_captures
    from app.core.db import SessionLocal

    paused, go, out = threading.Event(), threading.Event(), {}

    def listing(tenant, stem):
        paused.set()
        go.wait(30)
        return []

    def run():
        session = SessionLocal()
        try:
            out["result"] = adopt_captures(session, src_id, tgt_id, actor="admin", now=NOW,
                                           list_target_prefix=listing, request_id=rid)
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            out["result"] = exc
        finally:
            session.close()

    thread = threading.Thread(target=run)
    thread.start()
    assert paused.wait(30)
    return thread, go, out


# ---------------------------------------------------------------------------
# The id: its form, and no id at all
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("bad", ["not-a-uuid", "1234", str(uuid.uuid4()) + "0", str(uuid.uuid4()).replace("-", "")])
def test_an_invalid_request_id_is_422_and_nothing_runs(client, db_session, override_projects_root, tenant,
                                                       empty_bucket, bad):
    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    before = _snapshot(db_session, root)
    _as("admin")

    assert _post(client, src, tgt, bad).status_code == 422
    assert _lookup(client, bad).status_code == 422
    assert _snapshot(db_session, root) == before


@pytest.mark.unit
def test_without_a_request_id_nothing_is_recorded_and_the_journal_carries_none(
        client, db_session, override_projects_root, tenant, empty_bucket, kill):
    from app.core import adopt_journal

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    journals = []
    real = adopt_journal.create_journal
    kill.setattr(adopt_journal, "create_journal", lambda payload: (journals.append(dict(payload)), real(payload)))
    _as("admin")

    r = _post(client, src, tgt)

    assert r.status_code == 200, r.text
    assert _rows(db_session) == []
    assert "request_id" not in journals[0]


# ---------------------------------------------------------------------------
# Each outcome, and the same id posted again
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_applied_request_is_recorded_and_a_repeat_answers_it_without_a_second_adoption(
        client, db_session, override_projects_root, tenant, empty_bucket):
    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    rid = str(uuid.uuid4())
    _as("admin")

    first = _post(client, src, tgt, rid.upper())

    assert first.status_code == 200, first.text
    [row] = _rows(db_session)
    assert (row.request_id, row.outcome, row.source_id, row.target_id) == (rid, "applied", src.id, tgt.id)
    after = _but_requests(_snapshot(db_session, root))
    looked = _lookup(client, rid)
    assert looked.status_code == 200, looked.text
    body = looked.json()
    assert (body["state"], body["source_id"], body["target_id"], body["result"]) == \
        ("applied", src.id, tgt.id, first.json())
    assert body["started_at"] and body["ended_at"] and body["ended_at"] >= body["started_at"]

    again = _post(client, src, tgt, rid)

    assert (again.status_code, again.json()) == (200, first.json())
    assert _but_requests(_snapshot(db_session, root)) == after
    assert len(_rows(db_session)) == 1


@pytest.mark.unit
def test_a_refused_request_is_recorded_and_a_repeat_answers_the_same_refusal(
        client, db_session, override_projects_root, tenant, empty_bucket, monkeypatch):
    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    _record(db_session, tgt, "already here")
    before = _snapshot(db_session, root)
    rid = str(uuid.uuid4())
    _as("admin")

    first = _post(client, src, tgt, rid)

    assert first.status_code == 409, first.text
    body = _lookup(client, rid).json()
    assert (body["state"], body["status"], body["detail"]) == ("refused", 409, first.json()["detail"])
    assert body["ended_at"]
    # The cause of the refusal goes: a repeat answers the refusal remembered, and plans nothing.
    from app.core import adopt_captures
    from app.models.record import Record

    db_session.query(Record).filter(Record.collection_id == tgt.id).delete()
    db_session.commit()
    plans = []
    real_plan = adopt_captures.plan_adopt_captures
    monkeypatch.setattr(adopt_captures, "plan_adopt_captures", lambda *a, **kw: plans.append(a) or real_plan(*a, **kw))
    again = _post(client, src, tgt, rid)
    assert (again.status_code, again.json()) == (409, first.json())
    assert plans == []


@pytest.mark.unit
def test_a_request_that_failed_and_was_put_back_is_recorded_and_a_repeat_renames_nothing(
        client, db_session, override_projects_root, tenant, empty_bucket, monkeypatch):
    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    calls = _failing_rename(monkeypatch, 1)
    rid = str(uuid.uuid4())
    _as("admin")

    first = _post(client, src, tgt, rid)

    assert first.status_code == 500, first.text
    assert first.json()["detail"]["restored"] is True
    body = _lookup(client, rid).json()
    assert (body["state"], body["message"], body["restored"]) == ("failed", first.json()["detail"]["message"], True)
    again = _post(client, src, tgt, rid)
    assert (again.status_code, again.json()) == (500, first.json())
    assert len(calls) == 1


@pytest.mark.unit
def test_an_id_repeated_for_other_volumes_is_refused_and_names_nothing_of_its_request(
        client, db_session, override_projects_root, tenant, empty_bucket):
    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    rid = str(uuid.uuid4())
    _as("admin")
    first = _post(client, src, tgt, rid)
    assert first.status_code == 200, first.text
    after = _snapshot(db_session, root)

    r = client.post(f"/collections/{tgt.id}/adopt-into/{src.id}", json={"client_request_id": rid})

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["codes"] == ["client_request_id_in_use"]
    assert str(first.json()["records"]) not in json.dumps(r.json()["detail"]["details"])
    assert _snapshot(db_session, root) == after


@pytest.mark.unit
def test_an_unknown_id_is_404(client, db_session, override_projects_root):
    _as("admin")

    r = _lookup(client, uuid.uuid4())

    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# A lookup that answers "no such request" closes the id
# ---------------------------------------------------------------------------

def _admin():
    from app.models.user import User

    return User(username="admin", email="admin@example.com", hashed_password="x", role="admin", is_active=True)


def _route_post(src_id, tgt_id, rid):
    """POST /collections/{src}/adopt-into/{tgt} as its handler runs it, on a session of its own; (status, body)."""
    from fastapi import HTTPException

    from app.api.collections import adopt_into
    from app.core.db import SessionLocal
    from app.schemas.collection import AdoptRequest

    session = SessionLocal()
    try:
        return 200, adopt_into(src_id, tgt_id, body=AdoptRequest(client_request_id=rid), current_user=_admin(),
                               db=session)
    except HTTPException as exc:
        return exc.status_code, exc.detail
    finally:
        session.close()


def _assert_closed(client, db, rid):
    r = _lookup(client, rid)
    assert r.status_code == 404, r.text
    assert (r.json()["detail"]["code"], r.json()["detail"]["state"]) == ("not_found", "closed")
    [row] = [row for row in _rows(db) if row.request_id == rid]
    assert (row.outcome, row.source_id, row.target_id) == ("closed", None, None)


@pytest.mark.unit
def test_a_lookup_that_finds_nothing_closes_the_id_and_a_later_post_never_adopts(
        client, db_session, override_projects_root, tenant, empty_bucket, monkeypatch):
    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    calls = _failing_rename(monkeypatch, 10 ** 6)
    rid = str(uuid.uuid4())
    _as("admin")

    _assert_closed(client, db_session, rid)
    before = _but_requests(_snapshot(db_session, root))

    r = _post(client, src, tgt, rid)

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["codes"] == ["adoption_abandoned"] and r.json()["detail"]["request_id"] == rid
    assert calls == [] and not _journal().exists()
    assert _but_requests(_snapshot(db_session, root)) == before
    _assert_closed(client, db_session, rid)


@pytest.mark.unit
def test_a_lookup_after_the_post_answers_it_and_closes_nothing(client, db_session, override_projects_root, tenant,
                                                               empty_bucket):
    src, tgt, _, _ = _setup(db_session, override_projects_root)
    rid = str(uuid.uuid4())
    _as("admin")
    assert _post(client, src, tgt, rid).status_code == 200

    assert _lookup(client, rid).json()["state"] == "applied"
    assert [row.outcome for row in _rows(db_session)] == ["applied"]


@pytest.mark.unit
def test_a_lookup_that_closes_the_id_before_the_posts_insert_wins_and_the_post_adopts_nothing(
        client, db_session, override_projects_root, tenant, empty_bucket, kill):
    from app.core import adoption_requests

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    src_id, tgt_id = src.id, tgt.id
    rid = str(uuid.uuid4())
    before = _but_requests(_snapshot(db_session, root))
    at_insert, go = threading.Event(), threading.Event()
    real = adoption_requests.begin

    def begin(*args):
        at_insert.set()
        go.wait(30)
        return real(*args)

    kill.setattr(adoption_requests, "begin", begin)
    out = {}
    thread = threading.Thread(target=lambda: out.setdefault("post", _route_post(src_id, tgt_id, rid)))
    thread.start()
    try:
        assert at_insert.wait(30)
        _as("admin")
        looked = _lookup(client, rid)
    finally:
        go.set()
        thread.join(60)
    assert not thread.is_alive()

    assert looked.status_code == 404 and looked.json()["detail"]["state"] == "closed"
    status, detail = out["post"]
    assert status == 409 and detail["codes"] == ["adoption_abandoned"], out
    assert not _journal().exists()
    assert _but_requests(_snapshot(db_session, root)) == before
    _assert_closed(client, db_session, rid)


@pytest.mark.unit
def test_a_post_that_inserts_before_the_lookups_close_wins_and_the_lookup_answers_it_running(
        client, db_session, override_projects_root, tenant, empty_bucket, kill):
    from app.core import adoption_requests
    from app.core.db import SessionLocal

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    rid = str(uuid.uuid4())
    at_close, go_close = threading.Event(), threading.Event()
    real = adoption_requests._close

    def close(request_id):
        at_close.set()
        go_close.wait(30)
        return real(request_id)

    kill.setattr(adoption_requests, "_close", close)
    looked = {}

    def lookup():
        session = SessionLocal()
        try:
            looked["state"] = adoption_requests.lookup(session, rid)
        finally:
            session.close()

    looker = threading.Thread(target=lookup)
    looker.start()
    assert at_close.wait(30)
    thread, go, out = _paused_adoption(src.id, tgt.id, rid, kill)
    try:
        go_close.set()
        looker.join(60)
        assert not looker.is_alive()
        assert looked["state"]["state"] == "running", looked
    finally:
        go.set()
        thread.join(60)
    assert not thread.is_alive()

    assert isinstance(out["result"], dict), out
    assert [row.outcome for row in _rows(db_session)] == ["applied"]
    _as("admin")
    assert _lookup(client, rid).json()["state"] == "applied"


# ---------------------------------------------------------------------------
# While it runs
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_request_is_recorded_running_from_the_lock_before_the_journal_and_a_repeat_is_409(
        client, db_session, override_projects_root, tenant, empty_bucket, kill):
    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    src_id, tgt_id = src.id, tgt.id
    rid, other = str(uuid.uuid4()), str(uuid.uuid4())
    _as("admin")
    thread, go, out = _paused_adoption(src_id, tgt_id, rid, kill)
    try:
        assert not _journal().exists()
        looked = _lookup(client, rid)
        report = client.get("/system/adoption").json()
        again = _post(client, src, tgt, rid)
        elsewhere = _post(client, src, tgt, other)
        other_looked = _lookup(client, other)
    finally:
        go.set()
        thread.join(60)
    assert not thread.is_alive()

    assert looked.status_code == 200, looked.text
    body = looked.json()
    assert (body["state"], body["source_id"], body["target_id"], body["ended_at"]) == ("running", src_id, tgt_id, None)
    assert body["started_at"]
    assert report["unfinished"] is None
    assert report["running"] == {"request_id": rid, "kind": "adoption", "started_at": body["started_at"],
                                 "source": {"id": src_id, "name": src.name, "filename_stem": SRC},
                                 "target": {"id": tgt_id, "name": tgt.name, "filename_stem": TGT}}
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["codes"] == ["adoption_in_progress"]
    assert again.json()["detail"]["request_id"] == rid
    # Refused before it took the lock: nothing is recorded for it.
    assert elsewhere.status_code == 409 and other_looked.status_code == 404
    assert isinstance(out["result"], dict), out
    assert out["result"]["images"] == len(plan.renames)
    assert _lookup(client, rid).json()["state"] == "applied"
    assert client.get("/system/adoption").json() == {"unfinished": None, "running": None}


# ---------------------------------------------------------------------------
# A process death, and recovery
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_kill_before_the_commit_leaves_the_request_interrupted_until_recovery_undoes_it(
        client, db_session, override_projects_root, tenant, kill):
    src, tgt, _, _ = _setup(db_session, override_projects_root)
    rid = str(uuid.uuid4())
    _kill_after_renames(kill, 10)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt, request_id=rid)
    kill.undo()
    assert json.loads(_journal().read_text())["request_id"] == rid
    _as("admin")

    body = _lookup(client, rid).json()
    assert (body["state"], body["ended_at"], body["journal_kept"]) == ("interrupted", None, True)
    again = _post(client, src, tgt, rid)
    assert again.status_code == 409 and again.json()["detail"]["codes"] == ["adoption_interrupted"]
    assert again.json()["detail"]["state"] == "interrupted"
    assert [row.outcome for row in _rows(db_session)] == [None]

    _recover()

    body = _lookup(client, rid).json()
    assert (body["state"], body["result"], body["recovered"]) == ("reverted", None, True)
    assert body["ended_at"]
    again = _post(client, src, tgt, rid)
    assert again.status_code == 409 and again.json()["detail"]["state"] == "reverted"


@pytest.mark.unit
def test_a_kill_right_after_the_commit_leaves_the_request_applied_by_its_own_commit(
        client, db_session, override_projects_root, tenant, kill):
    src, tgt, _, _ = _setup(db_session, override_projects_root)
    rid = str(uuid.uuid4())
    _kill_at_phase(kill, "committed")
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt, request_id=rid)
    kill.undo()
    _as("admin")

    [row] = _rows(db_session)
    assert (row.outcome, row.status) == ("applied", 200)
    before_recovery = _lookup(client, rid).json()
    assert before_recovery["state"] == "applied"
    assert before_recovery["result"]["records"] == 4 and before_recovery["result"]["recovery_pending"] is True

    assert _recover()["outcome"] == "completed"

    assert _lookup(client, rid).json() == before_recovery
    again = _post(client, src, tgt, rid)
    assert (again.status_code, again.json()) == (200, before_recovery["result"])


@pytest.mark.unit
def test_a_blocked_recovery_leaves_the_request_interrupted_and_the_repair_settles_it(
        client, db_session, override_projects_root, read_remote, kill, monkeypatch):
    from app.core import adopt_apply

    root = override_projects_root
    rid = str(uuid.uuid4())
    real = adopt_apply.adopt_captures
    monkeypatch.setattr(adopt_apply, "adopt_captures",
                        lambda db, s, t, **kw: real(db, s, t, request_id=rid, **kw))
    _blocked(db_session, root, kill, bucket=read_remote)
    _as("admin")

    body = _lookup(client, rid).json()
    assert (body["state"], body["journal_kept"]) == ("interrupted", True)

    assert client.post("/system/adoption/repair").json()["result"] == "reverted"
    assert _lookup(client, rid).json()["state"] == "reverted"


# ---------------------------------------------------------------------------
# What holds the lock, read without taking it
# ---------------------------------------------------------------------------

class _SpyLock:
    """ADOPTION_LOCK as it is, counting every acquire."""

    def __init__(self, lock):
        self.lock, self.acquired = lock, 0

    def acquire(self, *a, **kw):
        self.acquired += 1
        return self.lock.acquire(*a, **kw)

    def release(self):
        self.lock.release()

    def locked(self):
        return self.lock.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


@pytest.mark.unit
def test_the_report_names_nothing_running_and_takes_no_lock(client, db_session, override_projects_root, monkeypatch):
    from app.core import adopt_journal

    spy = _SpyLock(adopt_journal.ADOPTION_LOCK)
    monkeypatch.setattr(adopt_journal, "ADOPTION_LOCK", spy)
    _as("admin")

    assert client.get("/system/adoption").json() == {"unfinished": None, "running": None}
    assert spy.acquired == 0


@pytest.mark.unit
def test_a_recovery_is_reported_running_with_the_journals_request(client, db_session, override_projects_root,
                                                                  tenant, kill):
    from app.core import adopt_journal

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    rid = str(uuid.uuid4())
    _kill_after_renames(kill, 10)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt, request_id=rid)
    kill.undo()
    run_id = json.loads(_journal().read_text())["run_id"]
    paused, go = threading.Event(), threading.Event()
    real = adopt_journal._rename

    def rename(a, b):
        paused.set()
        go.wait(30)
        real(a, b)

    kill.setattr(adopt_journal, "_rename", rename)
    out = {}
    thread = threading.Thread(target=lambda: out.setdefault("result", _recover()))
    thread.start()
    try:
        assert paused.wait(30)
        _as("admin")
        report = client.get("/system/adoption").json()
        looked = _lookup(client, rid).json()
    finally:
        go.set()
        thread.join(60)
    assert not thread.is_alive()

    running = report["running"]
    assert (running["kind"], running["request_id"]) == ("recovery", rid)
    assert (running["source"]["id"], running["target"]["id"]) == (src.id, tgt.id)
    assert report["unfinished"]["state"] == "recovering" and report["unfinished"]["run_id"] == run_id
    assert looked["state"] == "running"
    assert out["result"]["outcome"] == "reverted"
    assert client.get("/system/adoption").json() == {"unfinished": None, "running": None}


@pytest.mark.unit
def test_a_repair_is_reported_running(client, db_session, override_projects_root, read_remote, kill):
    from app.core import adopt_repair, offsite_free
    from app.core.db import SessionLocal

    root = override_projects_root
    src, tgt, _, _ = _blocked(db_session, root, kill, bucket=read_remote)
    paused, go = threading.Event(), threading.Event()
    real = offsite_free._copy_result

    def copy(key, dest):
        paused.set()
        go.wait(30)
        return real(key, dest)

    kill.setattr(offsite_free, "_copy_result", copy)
    out = {}

    def run():
        session = SessionLocal()
        try:
            out["result"] = adopt_repair.repair(session, actor="admin")
        finally:
            session.close()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert paused.wait(30)
        _as("admin")
        report = client.get("/system/adoption").json()
    finally:
        go.set()
        thread.join(60)
    assert not thread.is_alive()

    running = report["running"]
    assert (running["kind"], running["request_id"]) == ("repair", None)
    assert (running["source"]["id"], running["target"]["id"]) == (src.id, tgt.id)
    assert out["result"]["result"] == "reverted"


# ---------------------------------------------------------------------------
# The repair and the journal's run id
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_repair_naming_another_run_repairs_nothing_and_one_naming_this_run_repairs(
        client, db_session, override_projects_root, read_remote, kill):
    root = override_projects_root
    src, tgt, _, lost = _blocked(db_session, root, kill, bucket=read_remote)
    journal = _journal().read_bytes()
    _as("admin")
    report = client.get("/system/adoption").json()["unfinished"]
    run_id = json.loads(journal)["run_id"]
    assert report["run_id"] == run_id
    named = {"source": {"id": src.id, "name": src.name, "filename_stem": SRC},
             "target": {"id": tgt.id, "name": tgt.name, "filename_stem": TGT}}

    other = client.post("/system/adoption/repair", json={"run_id": "0" * 32})

    assert other.status_code == 409, other.text
    detail = other.json()["detail"]
    assert detail["code"] == "journal_changed"
    assert (detail["source"], detail["target"]) == (named["source"], named["target"])
    assert _journal().read_bytes() == journal and not lost.old_path.exists() and not lost.new_path.exists()

    same = client.post("/system/adoption/repair", json={"run_id": run_id})

    assert same.status_code == 200, same.text
    assert same.json()["result"] == "reverted"
    assert (same.json()["source"], same.json()["target"]) == (named["source"], named["target"])

    gone = client.post("/system/adoption/repair", json={"run_id": run_id})

    assert gone.status_code == 409 and gone.json()["detail"]["code"] == "journal_changed"
    assert (gone.json()["detail"]["source"], gone.json()["detail"]["target"]) == (None, None)


@pytest.mark.unit
def test_a_repair_refused_after_the_journal_was_read_names_the_volumes(client, db_session, override_projects_root,
                                                                       tenant, kill):
    root = override_projects_root
    src, tgt, _, _ = _blocked(db_session, root, kill)
    _as("admin")

    r = client.post("/system/adoption/repair")

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "no_read_remote"
    assert (detail["source"]["id"], detail["target"]["id"]) == (src.id, tgt.id)


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("role", ["operator", "reviewer"])
def test_only_an_admin_may_ask_for_a_request(client, db_session, override_projects_root, role):
    _as(role)

    assert _lookup(client, uuid.uuid4()).status_code == 403


# ---------------------------------------------------------------------------
# The record keeping and the snapshot are observers
# ---------------------------------------------------------------------------

def _locks_free():
    from app.core import adopt_journal, adopt_repair

    free = adopt_journal.ADOPTION_LOCK.acquire(blocking=False) and adopt_repair._REPAIR_LOCK.acquire(blocking=False)
    if free:
        adopt_journal.ADOPTION_LOCK.release()
        adopt_repair._REPAIR_LOCK.release()
    return free


def _snapshot_write_raises(kill, at):
    """Make the snapshot's write raise when a run publishes itself (`publish`) or withdraws (`clear`)."""
    from app.core import adopt_journal

    real = adopt_journal._set_running

    def write(value):
        if (value is None) == (at == "clear"):
            raise MemoryError("the snapshot cannot be written")
        real(value)

    kill.setattr(adopt_journal, "_set_running", write)


@pytest.mark.unit
@pytest.mark.parametrize("at", ["publish", "clear"])
def test_an_adoption_whose_snapshot_cannot_be_written_answers_as_before_and_frees_the_lock(
        client, db_session, override_projects_root, tenant, empty_bucket, kill, at):
    from app.core import adopt_journal

    src, tgt, _, _ = _setup(db_session, override_projects_root)
    _snapshot_write_raises(kill, at)
    _as("admin")

    r = _post(client, src, tgt, str(uuid.uuid4()))

    assert r.status_code == 200, r.text
    assert _locks_free() and adopt_journal.running() is None


@pytest.mark.unit
@pytest.mark.parametrize("at", ["publish", "clear"])
def test_a_recovery_whose_snapshot_cannot_be_written_recovers_and_frees_the_lock(
        db_session, override_projects_root, tenant, kill, at):
    from app.core import adopt_journal

    src, tgt, _, _ = _setup(db_session, override_projects_root)
    _kill_after_renames(kill, 10)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    _snapshot_write_raises(kill, at)

    assert _recover()["outcome"] == "reverted"
    assert _locks_free() and adopt_journal.running() is None


@pytest.mark.unit
@pytest.mark.parametrize("at", ["publish", "clear"])
def test_a_repair_whose_snapshot_cannot_be_written_repairs_and_frees_both_locks(
        client, db_session, override_projects_root, read_remote, kill, at):
    from app.core import adopt_journal

    _blocked(db_session, override_projects_root, kill, bucket=read_remote)
    _snapshot_write_raises(kill, at)
    _as("admin")

    r = client.post("/system/adoption/repair")

    assert r.status_code == 200 and r.json()["result"] == "reverted", r.text
    assert _locks_free() and adopt_journal.running() is None


class _BrokenEnd:
    """A session whose update, rollback and close all raise once the outcome is being written."""

    def __init__(self, real):
        self.real, self.writing = real, False

    def __getattr__(self, name):
        return getattr(self.real, name)

    def query(self, *a, **kw):
        self.writing = True
        raise MemoryError("the outcome cannot be written")

    def rollback(self):
        if self.writing:
            raise MemoryError("the rollback cannot be made")
        self.real.rollback()

    def close(self):
        self.real.close()
        if self.writing:
            raise MemoryError("the session cannot be closed")


def _outcome_write_fails(kill):
    from app.core import adoption_requests

    real = adoption_requests._session
    kill.setattr(adoption_requests, "_session", lambda: _BrokenEnd(real()))


@pytest.mark.unit
def test_flags_that_cannot_be_stored_after_the_commit_leave_the_answer_and_the_commits_result(
        client, db_session, override_projects_root, tenant, empty_bucket, kill):
    src, tgt, _, _ = _setup(db_session, override_projects_root)
    rid = str(uuid.uuid4())
    _outcome_write_fails(kill)
    _as("admin")

    r = _post(client, src, tgt, rid)
    kill.undo()

    assert r.status_code == 200, r.text
    assert r.json()["records"] == 4 and r.json()["target_lines_pending"] is False and not _journal().exists()
    # Applied by the adoption's own commit; the flags known only after it could not be stored.
    assert [row.outcome for row in _rows(db_session)] == ["applied"]
    body = _lookup(client, rid).json()
    assert body["state"] == "applied"
    assert body["result"] == {**r.json(), "target_lines_appended": False, "target_lines_pending": True,
                              "recovery_pending": True}
    again = _post(client, src, tgt, rid)
    assert (again.status_code, again.json()) == (200, body["result"])


@pytest.mark.unit
def test_a_refusal_whose_outcome_cannot_be_written_answers_the_refusal_and_reads_reverted(
        client, db_session, override_projects_root, tenant, empty_bucket, kill):
    src, tgt, _, _ = _setup(db_session, override_projects_root)
    _record(db_session, tgt, "already here")
    rid = str(uuid.uuid4())
    _outcome_write_fails(kill)
    _as("admin")

    r = _post(client, src, tgt, rid)
    kill.undo()

    assert r.status_code == 409 and r.json()["detail"]["codes"] == ["target_has_records"], r.text
    body = _lookup(client, rid).json()
    assert (body["state"], body["recovered"]) == ("reverted", True)
    assert [row.outcome for row in _rows(db_session)] == ["reverted"]


@pytest.mark.unit
def test_a_recovery_whose_record_cannot_be_written_still_finishes_and_the_record_reads_applied(
        client, db_session, override_projects_root, tenant, kill):
    src, tgt, _, _ = _setup(db_session, override_projects_root)
    rid = str(uuid.uuid4())
    _kill_at_phase(kill, "committed")
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt, request_id=rid)
    kill.undo()
    _outcome_write_fails(kill)

    assert _recover()["outcome"] == "completed"
    kill.undo()

    assert not _journal().exists() and _locks_free()
    _as("admin")
    body = _lookup(client, rid).json()
    assert body["state"] == "applied" and body["result"]["records"] == 4


# ---------------------------------------------------------------------------
# Applied only by the adoption's own commit; not applied only under the lock
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_request_that_died_before_its_journal_reads_reverted_after_another_adopted_the_same_volumes(
        client, db_session, override_projects_root, tenant):
    from app.core.adopt_apply import adopt_captures

    src, tgt, _, _ = _setup(db_session, override_projects_root)
    first, second = str(uuid.uuid4()), str(uuid.uuid4())

    def dies(tenant, stem):
        raise Killed()

    with pytest.raises(Killed):
        adopt_captures(db_session, src.id, tgt.id, actor="admin", now=NOW, list_target_prefix=dies,
                       request_id=first)
    assert not _journal().exists()
    _adopt(db_session, src, tgt, request_id=second)
    _as("admin")

    assert _lookup(client, second).json()["state"] == "applied"
    body = _lookup(client, first).json()
    assert body["state"] == "reverted", body
    assert {row.request_id: row.outcome for row in _rows(db_session)} == {first: "reverted", second: "applied"}


@pytest.mark.unit
def test_a_committed_request_reads_applied_after_its_records_moved_on(
        client, db_session, override_projects_root, tenant, empty_bucket, kill):
    from tests.unit.test_adopt_captures_plan import _volume
    from app.models.record import Record

    src, tgt, _, _ = _setup(db_session, override_projects_root)
    rid = str(uuid.uuid4())
    _outcome_write_fails(kill)
    _as("admin")
    assert _post(client, src, tgt, rid).status_code == 200
    kill.undo()
    # Another adoption moves the target's records on to a third volume (its row writes, as SQL).
    onward = _volume(db_session, "Siguiente", "xx-onward-003", external_id="xx-onward-003")
    db_session.query(Record).filter(Record.collection_id == tgt.id).update({"collection_id": onward.id})
    db_session.commit()

    body = _lookup(client, rid).json()

    assert body["state"] == "applied" and body["result"]["records"] == 4, body


@pytest.mark.unit
def test_a_request_paused_before_its_journal_with_no_snapshot_reads_running_then_applied(
        client, db_session, override_projects_root, tenant, empty_bucket, kill):
    from app.core import adopt_journal

    src, tgt, _, _ = _setup(db_session, override_projects_root)
    rid = str(uuid.uuid4())
    _snapshot_write_raises(kill, "publish")
    _as("admin")
    thread, go, out = _paused_adoption(src.id, tgt.id, rid, kill)
    try:
        assert adopt_journal.running() is None and not _journal().exists()
        looked = _lookup(client, rid).json()
        rows = [row.outcome for row in _rows(db_session)]
    finally:
        go.set()
        thread.join(60)
    assert not thread.is_alive()

    assert looked["state"] == "running", looked
    assert rows == [None]
    assert isinstance(out["result"], dict), out
    assert _lookup(client, rid).json()["state"] == "applied"


@pytest.mark.unit
def test_a_lookup_while_another_request_holds_the_lock_is_busy_and_writes_nothing(
        client, db_session, override_projects_root, tenant, empty_bucket, kill):
    from datetime import datetime

    from app.models.adoption_request import AdoptionRequest

    src, tgt, _, _ = _setup(db_session, override_projects_root)
    holder, waiting = str(uuid.uuid4()), str(uuid.uuid4())
    db_session.add(AdoptionRequest(request_id=waiting, source_id=src.id, target_id=tgt.id,
                                   started_at=datetime(2026, 11, 24, 11, 0)))
    db_session.commit()
    _as("admin")
    thread, go, out = _paused_adoption(src.id, tgt.id, holder, kill)
    try:
        looked = _lookup(client, waiting)
        again = _post(client, src, tgt, waiting)
        rows = {row.request_id: row.outcome for row in _rows(db_session)}
    finally:
        go.set()
        thread.join(60)
    assert not thread.is_alive()

    assert looked.status_code == 200 and looked.json()["state"] == "busy", looked.text
    assert again.status_code == 409 and again.json()["detail"]["codes"] == ["adoption_in_progress"]
    assert rows == {waiting: None, holder: None}


@pytest.mark.unit
def test_an_applied_mark_that_cannot_be_written_undoes_the_adoption(
        client, db_session, override_projects_root, tenant, empty_bucket, monkeypatch):
    from app.core import adopt_apply

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    before = _but_requests(_snapshot(db_session, root))
    rid = str(uuid.uuid4())

    def refused(db, request_id, summary):
        raise OSError("the request's row cannot be written")

    monkeypatch.setattr(adopt_apply, "_mark_applied", refused)
    _as("admin")

    r = _post(client, src, tgt, rid)

    assert r.status_code == 500 and r.json()["detail"]["restored"] is True, r.text
    assert _but_requests(_snapshot(db_session, root)) == before and not _journal().exists()
    assert [row.outcome for row in _rows(db_session)] == ["failed"]
