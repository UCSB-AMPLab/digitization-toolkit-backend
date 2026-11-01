"""The unit's mode (offline, backup, fisqua) and its projects setting (NEH-311, NEH-313).

One row in unit_settings, read and written by app.core.unit_mode only. The
mode decides what leaves the unit: offline sends nothing, backup copies the
images and the catalogue to the bucket, fisqua also talks to Fisqua. In
fisqua mode volumes come from Fisqua's queue: operators may not create a
volume, change its stem or reference code, or import a list; an admin may,
logged as an override, and a code an admin gives is checked with Fisqua
(contract 1.8) first. While projects are hidden (any mode but offline with
projects on), every new volume goes into the unit's default project.

    python -m pytest tests/unit/test_unit_mode.py

Version: 0.3.3
"""

import importlib.util
import json

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from tests.unit.test_capture_manifest_export import TENANT, _as, tenant, vol  # noqa: F401  (fixtures)
from tests.unit.test_fisqua_outbox import TOKEN, fisqua  # noqa: F401  (fixture)
from tests.unit.test_list_import import (  # noqa: F401  (reports_root is a fixture)
    _as as _as_importer, _item, _list_with, _post, reports_root,
)

BASE = "https://fisqua.test"
REVISION = "d7f9a1c3e5b2"
MIGRATION_FILE = f"{REVISION}_add_unit_settings.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result(outcome, **kw):
    from app.core.fisqua_outbox import TransportResult
    return TransportResult(outcome=outcome, **kw)


def _unit(db, mode, projects_enabled=False, default_project_id=None):
    """Write the unit's settings row directly, as a unit already set up would have it."""
    from app.models.unit_settings import UnitSettings
    row = db.get(UnitSettings, 1)
    if row is None:
        row = UnitSettings(id=1, mode=mode, projects_enabled=projects_enabled,
                           default_project_id=default_project_id)
        db.add(row)
    else:
        row.mode, row.projects_enabled, row.default_project_id = mode, projects_enabled, default_project_id
    db.commit()
    return row


def _logs(db, action):
    from app.models.system_log import SystemLog
    db.expire_all()
    return db.query(SystemLog).filter(SystemLog.action == action).order_by(SystemLog.id).all()


class LinkTransport:
    """Answers the operators PUT and the 1.8 description lookup from scripts; keeps what each was sent."""

    def __init__(self, operators=(), descriptions=()):
        self.operators_answers = list(operators)
        self.description_answers = list(descriptions)
        self.operators_sent = []
        self.descriptions_asked = []
        self.other_calls = []

    def _next(self, script):
        answer = script.pop(0) if script else _result("error", error="no answer")
        if isinstance(answer, Exception):
            raise answer
        return answer

    def put_operators(self, body):
        self.operators_sent.append(json.loads(json.dumps(body)))
        return self._next(self.operators_answers)

    def get_description(self, ref):
        self.descriptions_asked.append(ref)
        return self._next(self.description_answers)

    def send_manifest(self, manifest):
        self.other_calls.append("send_manifest")
        return _result("error", error="not scripted")

    def get_status(self, refs):
        self.other_calls.append("get_status")
        return _result("error", error="not scripted")

    def get_decisions(self, ref):
        self.other_calls.append("get_decisions")
        return _result("error", error="not scripted")

    def get_queue(self, revision):
        self.other_calls.append("get_queue")
        return _result("not_modified")

    def post_queue_ack(self, body):
        self.other_calls.append("post_queue_ack")
        return _result("acknowledged")


def _found(code):
    """A 200 to the 1.8 lookup for `code`."""
    return _result("description", response={"referenceCode": code, "title": "T", "level": "file",
                                             "dates": {"display": None, "start": None, "end": None}})


@pytest.fixture(autouse=True)
def recipient(monkeypatch):
    """A valid catalogue dump recipient, so the dump counts as configured wherever the remote is."""
    import pyrage
    from app.core import config
    key = str(pyrage.x25519.Identity.generate().to_public())
    # In the environment too: db_session's fixture rebuilds the settings from it.
    monkeypatch.setenv("CAPTUA_OFFSITE_DUMP_RECIPIENT", key)
    monkeypatch.setattr(config.settings, "OFFSITE_DUMP_RECIPIENT", key, raising=False)
    return key


@pytest.fixture
def link(fisqua, monkeypatch):
    """Fisqua configured, the operators already sent, and the link's transport a LinkTransport."""
    from app.core import fisqua_queue, unit_mode
    transport = LinkTransport()
    monkeypatch.setattr(unit_mode, "get_transport", lambda: transport)
    monkeypatch.setattr(unit_mode, "get_lookup_transport", lambda: transport)
    monkeypatch.setattr(fisqua_queue, "get_transport", lambda: transport)
    monkeypatch.setattr(fisqua_queue, "_operators", fisqua_queue._OperatorsState(due=False))
    return transport


def _project(db, name):
    from app.models.project import Project
    p = Project(name=name)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _volume(db, project, stem="xx-test-100", external_id=None, name="Tomo"):
    from app.models.collection import Collection
    c = Collection(name=name, project_id=project.id, filename_stem=stem, external_id=external_id)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


# ---------------------------------------------------------------------------
# Ruling 1: storage
# ---------------------------------------------------------------------------

def _load_migration(backend_root):
    path = backend_root / "alembic" / "versions" / MIGRATION_FILE
    spec = importlib.util.spec_from_file_location("unit_settings_migration", path)
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
def test_the_migration_follows_the_head_it_was_written_on_and_leaves_one_head(backend_root):
    import re
    mod = _load_migration(backend_root)
    assert mod.revision == REVISION
    assert mod.down_revision == "c4e6a8b0d2f4"
    revisions, downs = set(), set()
    for path in (backend_root / "alembic" / "versions").glob("*.py"):
        text = path.read_text()
        revisions.add(re.search(r"^revision(?:: str)? = '(\w+)'", text, re.M).group(1))
        downs.update(re.findall(r"'(\w+)'", re.search(r"^down_revision[^=]*= (.*)$", text, re.M).group(1)))
    assert revisions - downs == {REVISION}


@pytest.mark.unit
def test_the_migration_creates_one_checked_row_table_and_drops_it(backend_root, tmp_path):
    mod = _load_migration(backend_root)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    md = sa.MetaData()
    sa.Table("projects", md, sa.Column("id", sa.Integer, primary_key=True))
    md.create_all(engine)
    _run(engine, mod.upgrade)

    insp = sa.inspect(engine)
    assert {c["name"] for c in insp.get_columns("unit_settings")} == {
        "id", "mode", "projects_enabled", "default_project_id", "updated_at", "updated_by"}
    checks = {c["name"] for c in insp.get_check_constraints("unit_settings")}
    assert {"check_unit_settings_mode", "check_unit_settings_single_row"} <= checks
    fks = insp.get_foreign_keys("unit_settings")
    assert [(f["referred_table"], f["constrained_columns"]) for f in fks] == [("projects", ["default_project_id"])]

    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO unit_settings (id, mode, updated_at) VALUES (1, 'backup', CURRENT_TIMESTAMP)"))
        assert conn.execute(sa.text("SELECT projects_enabled FROM unit_settings")).scalar() in (0, False)
    for bad in ("INSERT INTO unit_settings (id, mode, updated_at) VALUES (2, 'offline', CURRENT_TIMESTAMP)",
                "UPDATE unit_settings SET mode = 'online'"):
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(sa.text(bad))

    _run(engine, mod.downgrade)
    assert "unit_settings" not in sa.inspect(engine).get_table_names()


@pytest.mark.unit
def test_only_unit_mode_touches_the_table(backend_root):
    """Ruling 1: one module reads and writes unit_settings; nothing else names the model.

    init_db's import, which only registers the model, is not a use.
    """
    users = []
    for path in (backend_root / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8").replace("import app.models.unit_settings", "")
        if "UnitSettings" in text or "unit_settings" in text:
            users.append(path.relative_to(backend_root).as_posix())
    assert sorted(users) == ["app/core/unit_mode.py", "app/models/unit_settings.py"]


# ---------------------------------------------------------------------------
# Ruling 2: the starting mode
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_with_nothing_configured_the_unit_starts_offline(db_session):
    from app.core import unit_mode
    state = unit_mode.current(db_session)
    assert (state.mode, state.projects_enabled, state.default_project_id) == ("offline", False, None)


@pytest.mark.unit
def test_with_offsite_upload_configured_the_unit_starts_in_backup(db_session, tenant, monkeypatch):
    from app.core import config, unit_mode
    monkeypatch.setattr(config.settings, "OFFSITE_REMOTE", "b2unit:bucket")
    assert unit_mode.current(db_session).mode == "backup"


@pytest.mark.unit
def test_with_fisqua_configured_the_unit_starts_in_fisqua(db_session, fisqua):
    from app.core import unit_mode
    assert unit_mode.current(db_session).mode == "fisqua"


@pytest.mark.unit
def test_initialise_writes_the_row_once_and_logs_the_inferred_mode_and_why(db_session, fisqua):
    from app.core import unit_mode
    from app.models.unit_settings import UnitSettings
    assert unit_mode.initialise() == "fisqua"
    assert unit_mode.initialise() is None
    db_session.expire_all()
    row = db_session.get(UnitSettings, 1)
    assert (row.mode, row.projects_enabled, row.default_project_id) == ("fisqua", False, None)
    logs = _logs(db_session, "unit_mode_initialised")
    assert len(logs) == 1
    assert "fisqua" in logs[0].detail and "configured" in logs[0].detail


@pytest.mark.unit
def test_a_stored_mode_is_kept_whatever_the_environment_says(db_session, fisqua):
    from app.core import unit_mode
    _unit(db_session, "offline")
    assert unit_mode.current(db_session).mode == "offline"


@pytest.mark.unit
def test_startup_initialises_the_mode(backend_root):
    text = (backend_root / "app" / "main.py").read_text(encoding="utf-8")
    assert "unit_mode.initialise()" in text or "initialise_unit_mode()" in text


# ---------------------------------------------------------------------------
# Ruling 3: what each mode switches on
# ---------------------------------------------------------------------------

def _no_rclone(monkeypatch):
    from app.core import image_upload, catalogue_backup
    calls = []
    monkeypatch.setattr(image_upload, "pending_work", lambda db: calls.append("listed") or [])
    monkeypatch.setattr(catalogue_backup, "needs_run_today", lambda db, today=None: calls.append("dump") or False)
    monkeypatch.setattr(catalogue_backup, "last_verified_backup",
                        lambda db: {"object": None, "verified_at": None, "detail": None})
    return calls


@pytest.mark.unit
def test_offline_sends_nothing_offsite_and_makes_no_fisqua_call(db_session, link, monkeypatch):
    from app.core import catalogue_backup, fisqua_outbox, fisqua_queue, image_upload
    calls = _no_rclone(monkeypatch)
    _unit(db_session, "offline")
    assert image_upload.run_image_upload(db_session)["status"] == "off_in_this_mode"
    assert catalogue_backup.run_catalogue_backup(db_session)["status"] == "off_in_this_mode"
    assert fisqua_outbox.run_fisqua_outbox(db_session, transport=link)["status"] == "off_in_this_mode"
    assert fisqua_outbox.poll_fisqua_status(db_session, transport=link)["status"] == "off_in_this_mode"
    assert fisqua_queue.sync_fisqua_queue(db_session, transport=link)["status"] == "off_in_this_mode"
    fisqua_queue.mark_operators_due()
    assert fisqua_queue.send_operators(db_session, transport=link)["status"] == "off_in_this_mode"
    assert calls == []
    assert link.operators_sent == [] and link.other_calls == []


@pytest.mark.unit
def test_backup_uploads_and_dumps_but_makes_no_fisqua_call(db_session, link, monkeypatch):
    from app.core import catalogue_backup, fisqua_outbox, fisqua_queue, image_upload
    calls = _no_rclone(monkeypatch)
    _unit(db_session, "backup")
    assert image_upload.run_image_upload(db_session)["status"] == "done"
    assert catalogue_backup.run_catalogue_backup(db_session)["status"] == "up-to-date"
    assert calls == ["listed", "dump"]
    assert fisqua_outbox.run_fisqua_outbox(db_session, transport=link)["status"] == "off_in_this_mode"
    assert fisqua_outbox.poll_fisqua_status(db_session, transport=link)["status"] == "off_in_this_mode"
    assert fisqua_queue.sync_fisqua_queue(db_session, transport=link)["status"] == "off_in_this_mode"
    fisqua_queue.mark_operators_due()
    assert fisqua_queue.send_operators(db_session, transport=link)["status"] == "off_in_this_mode"
    assert link.operators_sent == [] and link.other_calls == []


@pytest.mark.unit
def test_fisqua_mode_makes_the_calls(db_session, link, monkeypatch):
    from app.core import fisqua_queue
    _unit(db_session, "fisqua")
    link.operators_answers = [_result("delivered")]
    fisqua_queue.mark_operators_due()
    assert fisqua_queue.send_operators(db_session, transport=link)["status"] == "sent"
    assert fisqua_queue.sync_fisqua_queue(db_session, transport=link)["status"] == "unchanged"
    assert "get_queue" in link.other_calls


@pytest.mark.unit
def test_switching_down_changes_no_outbox_row(client, db_session, link):
    from app.models.fisqua import FisquaOutbox
    p = _project(db_session, "P")
    vol = _volume(db_session, p)
    db_session.add(FisquaOutbox(collection_id=vol.id, manifest_id="m-1", stem=vol.filename_stem,
                                state="ready", attempts=0))
    db_session.commit()
    _unit(db_session, "fisqua")
    link.operators_answers = [_result("delivered")]
    resp = _as(client, "admin").put("/system/unit", json={"mode": "offline"})
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert [(r.manifest_id, r.state) for r in db_session.query(FisquaOutbox).all()] == [("m-1", "ready")]


# ---------------------------------------------------------------------------
# Rulings 4, 5 and 8: switching, the operators PUT carrying the mode, the API
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_operators_body_carries_the_mode(db_session, fisqua):
    from app.core.fisqua_queue import operators_body
    _unit(db_session, "fisqua")
    assert operators_body(db_session)["mode"] == "fisqua"
    _unit(db_session, "backup")
    assert operators_body(db_session)["mode"] == "backup"


@pytest.mark.unit
def test_get_unit_shape_for_any_role(client, db_session, fisqua):
    _unit(db_session, "offline", projects_enabled=True)
    for role in ("reviewer", "operator", "admin"):
        resp = _as(client, role).get("/system/unit")
        assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"mode": "offline", "projectsEnabled": True, "projectsShown": True,
                    "defaultProjectId": None, "prerequisites": {"backup": [], "fisqua": []}}


@pytest.mark.unit
def test_get_unit_lists_missing_prerequisites(client, db_session):
    body = _as(client, "reviewer").get("/system/unit").json()
    assert body["mode"] == "offline" and body["projectsShown"] is False
    assert set(body["prerequisites"]["backup"]) == {"offsite_not_configured", "catalogue_dump_not_configured"}
    assert set(body["prerequisites"]["fisqua"]) == {"offsite_not_configured", "catalogue_dump_not_configured",
                                                    "fisqua_not_configured"}


@pytest.mark.unit
@pytest.mark.parametrize("role", ["operator", "reviewer"])
def test_only_an_admin_may_put_the_unit(client, db_session, role):
    assert _as(client, role).put("/system/unit", json={"mode": "offline"}).status_code == 403


def _problem_codes(resp):
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail["message"], str) and detail["message"]
    return [p["code"] for p in detail["problems"]]


@pytest.mark.unit
def test_backup_refused_without_offsite_and_nothing_changes(client, db_session):
    from app.core import unit_mode
    _unit(db_session, "offline")
    resp = _as(client, "admin").put("/system/unit", json={"mode": "backup", "projectsEnabled": True})
    assert set(_problem_codes(resp)) == {"offsite_not_configured", "catalogue_dump_not_configured"}
    state = unit_mode.current(db_session)
    assert (state.mode, state.projects_enabled) == ("offline", False)
    assert _logs(db_session, "unit_mode_changed") == []


@pytest.mark.unit
def test_fisqua_refused_without_the_link_configured(client, db_session, tenant, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config.settings, "OFFSITE_REMOTE", "b2unit:bucket")
    _unit(db_session, "backup")
    resp = _as(client, "admin").put("/system/unit", json={"mode": "fisqua"})
    assert _problem_codes(resp) == ["fisqua_not_configured"]


@pytest.mark.unit
@pytest.mark.parametrize("answer, error_class", [
    (_result("error", error=f"Fisqua could not be reached: timeout: read timed out {TOKEN}"), "error"),
    (httpx.ConnectError("boom"), "error"),
    (_result("config_error", status_code=401, reason="token", error="Fisqua refused this unit's token (HTTP 401)"),
     "config_error"),
    (_result("unsupported", status_code=404, error="no 1.7"), "unsupported"),
    (_result("refused", status_code=422, error="refused"), "refused"),
])
def test_fisqua_refused_when_the_operators_put_fails_and_nothing_changes(client, db_session, link, answer,
                                                                         error_class):
    from app.core import unit_mode
    _unit(db_session, "backup")
    link.operators_answers = [answer]
    resp = _as(client, "admin").put("/system/unit", json={"mode": "fisqua"})
    assert _problem_codes(resp) == ["fisqua_unreachable"]
    problem = resp.json()["detail"]["problems"][0]
    assert problem["error"].startswith(error_class)
    assert TOKEN not in resp.text
    assert link.operators_sent[0]["mode"] == "fisqua"
    assert unit_mode.current(db_session).mode == "backup"


@pytest.mark.unit
def test_fisqua_switch_sends_the_mode_and_is_logged(client, db_session, link):
    from app.core import unit_mode
    _unit(db_session, "backup")
    link.operators_answers = [_result("delivered")]
    resp = _as(client, "admin", "boss").put("/system/unit", json={"mode": "fisqua"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "fisqua" and resp.json()["projectsShown"] is False
    assert len(link.operators_sent) == 1 and link.operators_sent[0]["mode"] == "fisqua"
    assert unit_mode.current(db_session).mode == "fisqua"
    logs = _logs(db_session, "unit_mode_changed")
    assert len(logs) == 1 and logs[0].actor == "boss"
    assert "backup" in logs[0].detail and "fisqua" in logs[0].detail


@pytest.mark.unit
@pytest.mark.parametrize("target", ["backup", "offline"])
def test_from_fisqua_down_the_new_mode_is_sent_once_best_effort(client, db_session, link, target):
    from app.core import unit_mode
    _unit(db_session, "fisqua")
    link.operators_answers = [_result("error", error="HTTP 503")]
    resp = _as(client, "admin").put("/system/unit", json={"mode": target})
    assert resp.status_code == 200, resp.text
    assert [b["mode"] for b in link.operators_sent] == [target]
    assert unit_mode.current(db_session).mode == target


@pytest.mark.unit
def test_offline_needs_nothing_and_sends_nothing_from_backup(client, db_session, link):
    _unit(db_session, "backup")
    resp = _as(client, "admin").put("/system/unit", json={"mode": "offline"})
    assert resp.status_code == 200, resp.text
    assert link.operators_sent == []


@pytest.mark.unit
def test_projects_enabled_is_stored_in_any_mode_and_shown_only_offline(client, db_session, link):
    _unit(db_session, "fisqua")
    resp = _as(client, "admin").put("/system/unit", json={"projectsEnabled": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["projectsEnabled"] is True and resp.json()["projectsShown"] is False
    logs = _logs(db_session, "unit_projects_changed")
    assert len(logs) == 1 and "False" in logs[0].detail and "True" in logs[0].detail
    resp = _as(client, "admin").put("/system/unit", json={"mode": "offline"})
    assert resp.json()["projectsShown"] is True


@pytest.mark.unit
def test_an_unknown_mode_is_a_422(client, db_session):
    assert _as(client, "admin").put("/system/unit", json={"mode": "online"}).status_code == 422


# ---------------------------------------------------------------------------
# Ruling 6: fisqua-mode gating, and the 1.8 lookup
# ---------------------------------------------------------------------------

def _detail(resp, status, code):
    assert resp.status_code == status, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == code
    assert isinstance(detail["message"], str) and detail["message"]
    return detail


@pytest.mark.unit
def test_an_operator_cannot_create_a_volume_in_fisqua_mode(client, db_session, link):
    _unit(db_session, "fisqua")
    p = _project(db_session, "P")
    resp = _as(client, "operator").post("/collections/", json={"name": "T", "project_id": p.id,
                                                               "filename_stem": "xx-test-200"})
    detail = _detail(resp, 403, "volumes_from_fisqua")
    assert "Fisqua" in detail["message"] and "queue" in detail["message"]
    assert link.descriptions_asked == []


@pytest.mark.unit
@pytest.mark.parametrize("change", [{"filename_stem": "xx-test-201"}, {"external_id": "xx-test-100"},
                                    {"filename_stem": None}])
def test_an_operator_cannot_change_a_stem_or_code_in_fisqua_mode(client, db_session, link, change):
    _unit(db_session, "fisqua")
    vol = _volume(db_session, _project(db_session, "P"))
    resp = _as(client, "operator").patch(f"/collections/{vol.id}", json=change)
    _detail(resp, 403, "volumes_from_fisqua")


@pytest.mark.unit
def test_an_operator_may_change_other_fields_or_resend_the_same_stem_in_fisqua_mode(client, db_session, link):
    _unit(db_session, "fisqua")
    vol = _volume(db_session, _project(db_session, "P"))
    resp = _as(client, "operator").patch(f"/collections/{vol.id}",
                                         json={"name": "Otro", "filename_stem": "xx-test-100"})
    assert resp.status_code == 200, resp.text
    assert link.descriptions_asked == []


@pytest.mark.unit
def test_an_operator_cannot_import_a_list_in_fisqua_mode(client, db_session, link, reports_root):
    _unit(db_session, "fisqua")
    _as_importer("op", "operator")
    resp = _post(client, _list_with([_item()]))
    _detail(resp, 403, "volumes_from_fisqua")


@pytest.mark.unit
def test_an_admin_creates_a_volume_after_fisqua_knows_its_code_and_it_is_logged(client, db_session, link):
    from app.models.collection import Collection
    _unit(db_session, "fisqua")
    p = _project(db_session, "P")
    link.description_answers = [_result("description", response={"referenceCode": "xx-test-200"})]
    resp = _as(client, "admin", "boss").post("/collections/", json={
        "name": "T", "project_id": p.id, "filename_stem": "xx-test-200", "external_id": "xx-test-200"})
    assert resp.status_code == 201, resp.text
    assert link.descriptions_asked == ["xx-test-200"]
    assert db_session.query(Collection).filter_by(filename_stem="xx-test-200").count() == 1
    logs = _logs(db_session, "volume_created_override")
    assert len(logs) == 1 and logs[0].actor == "boss" and "xx-test-200" in logs[0].detail


@pytest.mark.unit
@pytest.mark.parametrize("answer, status, code", [
    (_result("unknown_code", status_code=404), 422, "unknown_reference_code"),
    (_result("unsupported", status_code=404), 409, "fisqua_no_lookup"),
    (_result("unsupported", status_code=405), 409, "fisqua_no_lookup"),
    (_result("error", error="Fisqua could not be reached: timeout: x"), 409, "fisqua_unreachable"),
    (_result("error", status_code=429, error="Fisqua answered HTTP 429", retry_after=30.0), 409,
     "fisqua_unreachable"),
    (_result("config_error", status_code=401, reason="token", error="Fisqua refused this unit's token (HTTP 401)"),
     409, "fisqua_unreachable"),
    (httpx.ReadTimeout("slow"), 409, "fisqua_unreachable"),
])
def test_an_admin_volume_is_refused_unless_fisqua_knows_its_code(client, db_session, link, answer, status, code):
    from app.models.collection import Collection
    _unit(db_session, "fisqua")
    p = _project(db_session, "P")
    link.description_answers = [answer]
    resp = _as(client, "admin").post("/collections/", json={"name": "T", "project_id": p.id,
                                                            "filename_stem": "xx-test-200"})
    detail = _detail(resp, status, code)
    if status == 409:
        assert "list" in detail["message"] and "Fisqua" in detail["message"]
    assert db_session.query(Collection).count() == 0
    assert _logs(db_session, "volume_created_override") == []


@pytest.mark.unit
def test_a_401_on_the_lookup_takes_the_link_config_error_handling(client, db_session, link):
    from app.core import fisqua_outbox
    _unit(db_session, "fisqua")
    p = _project(db_session, "P")
    link.description_answers = [_result("config_error", status_code=401, reason="token", error="x")]
    _as(client, "admin").post("/collections/", json={"name": "T", "project_id": p.id,
                                                     "filename_stem": "xx-test-200"})
    assert fisqua_outbox.config_error() is not None


@pytest.mark.unit
def test_an_admin_changing_a_stem_in_fisqua_mode_is_checked_and_logged(client, db_session, link):
    _unit(db_session, "fisqua")
    vol = _volume(db_session, _project(db_session, "P"), stem=None)
    link.description_answers = [_found("xx-test-300")]
    resp = _as(client, "admin").patch(f"/collections/{vol.id}", json={"filename_stem": "xx-test-300"})
    assert resp.status_code == 200, resp.text
    assert link.descriptions_asked == ["xx-test-300"]
    logs = _logs(db_session, "volume_code_override")
    assert len(logs) == 1 and "xx-test-300" in logs[0].detail


@pytest.mark.unit
def test_an_admin_stem_change_fisqua_does_not_know_is_refused(client, db_session, link):
    from app.models.collection import Collection
    _unit(db_session, "fisqua")
    vol = _volume(db_session, _project(db_session, "P"), stem=None)
    link.description_answers = [_result("unknown_code", status_code=404)]
    resp = _as(client, "admin").patch(f"/collections/{vol.id}", json={"filename_stem": "xx-test-300"})
    _detail(resp, 422, "unknown_reference_code")
    db_session.expire_all()
    assert db_session.get(Collection, vol.id).filename_stem is None


@pytest.mark.unit
def test_an_admin_list_import_in_fisqua_mode_is_logged(client, db_session, link, reports_root):
    _unit(db_session, "fisqua")
    _as_importer("boss", "admin")
    resp = _post(client, _list_with([_item()]))
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 1
    logs = _logs(db_session, "list_import_override")
    assert len(logs) == 1 and logs[0].actor == "boss"
    assert link.descriptions_asked == []


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["offline", "backup"])
def test_offline_and_backup_leave_operators_free(client, db_session, link, mode):
    _unit(db_session, mode, projects_enabled=True)
    p = _project(db_session, "P")
    resp = _as(client, "operator").post("/collections/", json={"name": "T", "project_id": p.id,
                                                               "filename_stem": "xx-test-200"})
    assert resp.status_code == 201, resp.text
    resp = _as(client, "operator").patch(f"/collections/{resp.json()['id']}", json={"filename_stem": "xx-test-201"})
    assert resp.status_code == 200, resp.text
    assert link.descriptions_asked == []
    assert _logs(db_session, "volume_created_override") == []


class Server:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _http(server, bypass=False):
    """The real transport over a fake Fisqua; `bypass` for the tests of how answers are read, which have no unit."""
    from app.core.fisqua_http import HttpFisquaTransport
    return HttpFisquaTransport(BASE, TOKEN, client=httpx.Client(transport=httpx.MockTransport(server)),
                               bypass_mode=bypass)


@pytest.mark.unit
def test_the_lookup_path_encodes_the_code_as_one_segment():
    from app.core import fisqua_contract as fc
    assert fc.DESCRIPTIONS_PATH == "/api/capture/v1/descriptions/{ref}"
    server = Server(httpx.Response(200, json={"referenceCode": "a/b c", "title": "T", "level": "file",
                                              "dates": {"display": None, "start": None, "end": None}}))
    result = _http(server, bypass=True).get_description("a/b c!*'()~")
    assert result.outcome == "description" and result.response["title"] == "T"
    request = server.requests[0]
    assert request.method == "GET"
    assert request.url.raw_path.decode() == "/api/capture/v1/descriptions/a%2Fb%20c!*'()~"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.unit
@pytest.mark.parametrize("response, outcome", [
    (httpx.Response(404, json={"rule": "unknown-reference-code"}), "unknown_code"),
    (httpx.Response(404), "unsupported"),
    (httpx.Response(404, json={"rule": "something-else"}), "unsupported"),
    (httpx.Response(405, headers={"Allow": "GET"}), "unsupported"),
    (httpx.Response(401), "config_error"),
    (httpx.Response(403), "config_error"),
    (httpx.Response(302, headers={"Location": "https://elsewhere"}), "config_error"),
    (httpx.Response(429, headers={"Retry-After": "12"}), "error"),
    (httpx.Response(500), "error"),
    (httpx.Response(200, text="not json"), "error"),
])
def test_the_lookup_answers(response, outcome):
    result = _http(Server(response), bypass=True).get_description("xx-test-1")
    assert result.outcome == outcome
    if response.status_code == 429:
        assert result.retry_after == 12.0


@pytest.mark.unit
def test_a_network_failure_on_the_lookup_is_an_error_without_the_token():
    result = _http(Server(httpx.ConnectError(f"refused {TOKEN}")), bypass=True).get_description("xx-test-1")
    assert result.outcome == "error" and "ConnectError" in result.error


@pytest.mark.unit
def test_the_lookup_takes_from_the_call_budget(client, db_session, link, monkeypatch):
    from app.core import fisqua_outbox
    _unit(db_session, "fisqua")
    p = _project(db_session, "P")
    monkeypatch.setattr(fisqua_outbox, "call_budget", fisqua_outbox.CallBudget(limit=0))
    resp = _as(client, "admin").post("/collections/", json={"name": "T", "project_id": p.id,
                                                            "filename_stem": "xx-test-200"})
    _detail(resp, 409, "fisqua_unreachable")
    assert link.descriptions_asked == []


# ---------------------------------------------------------------------------
# Ruling 7: projects
# ---------------------------------------------------------------------------

def _project_of(db, volume_id):
    from app.models.collection import Collection
    from app.models.project import Project
    db.expire_all()
    return db.get(Project, db.get(Collection, volume_id).project_id)


@pytest.mark.unit
def test_hidden_projects_file_a_new_volume_in_a_created_default_project(client, db_session):
    from app.core import unit_mode
    resp = _as(client, "operator").post("/collections/", json={"name": "T", "filename_stem": "xx-test-400"})
    assert resp.status_code == 201, resp.text
    project = _project_of(db_session, resp.json()["id"])
    assert project.name == "default"
    assert unit_mode.current(db_session).default_project_id == project.id
    assert len(_logs(db_session, "default_project_set")) == 1
    resp = _as(client, "operator").post("/collections/", json={"name": "U", "filename_stem": "xx-test-401"})
    assert _project_of(db_session, resp.json()["id"]).id == project.id
    assert len(_logs(db_session, "default_project_set")) == 1


@pytest.mark.unit
def test_the_only_project_is_adopted_as_the_default(client, db_session):
    only = _project(db_session, "AHR Digitalización 2026")
    resp = _as(client, "operator").post("/collections/", json={"name": "T", "filename_stem": "xx-test-400"})
    assert resp.status_code == 201, resp.text
    assert _project_of(db_session, resp.json()["id"]).id == only.id


@pytest.mark.unit
def test_with_several_projects_a_default_one_is_created(client, db_session):
    _project(db_session, "A")
    _project(db_session, "B")
    resp = _as(client, "operator").post("/collections/", json={"name": "T", "filename_stem": "xx-test-400"})
    assert _project_of(db_session, resp.json()["id"]).name == "default"


@pytest.mark.unit
def test_a_stored_default_project_is_used(client, db_session):
    _project(db_session, "A")
    b = _project(db_session, "B")
    _unit(db_session, "offline", default_project_id=b.id)
    resp = _as(client, "operator").post("/collections/", json={"name": "T", "filename_stem": "xx-test-400"})
    assert _project_of(db_session, resp.json()["id"]).id == b.id


@pytest.mark.unit
def test_with_projects_shown_a_volume_still_needs_a_project(client, db_session):
    _unit(db_session, "offline", projects_enabled=True)
    resp = _as(client, "operator").post("/collections/", json={"name": "T", "filename_stem": "xx-test-400"})
    assert resp.status_code == 400


@pytest.mark.unit
def test_hidden_projects_file_a_list_import_in_the_default_project_without_an_admin(
        client, db_session, reports_root):
    from app.models.project import Project
    _as_importer("op", "operator")
    resp = _post(client, _list_with([_item()]))
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["created"] == 1 and report["projectName"] == "default"
    assert [p.name for p in db_session.query(Project).all()] == ["default"]


@pytest.mark.unit
def test_shown_projects_keep_a_list_import_in_its_own_project(client, db_session, reports_root):
    _unit(db_session, "offline", projects_enabled=True)
    _as_importer("boss", "admin")
    doc = _list_with([_item()])
    resp = _post(client, doc)
    assert resp.status_code == 200, resp.text
    assert resp.json()["projectName"] == doc["list"]["name"]


@pytest.mark.unit
def test_the_queue_sync_files_new_volumes_in_the_default_project(db_session, link, reports_root):
    from app.core import fisqua_queue
    from tests.unit.test_fisqua_queue import QueueTransport, _ok_ack, _qitem, _queue
    _project(db_session, "A")
    _project(db_session, "B")
    transport = QueueTransport(queues=[_queue([_qitem()])], acks=[_ok_ack()])
    result = fisqua_queue.sync_fisqua_queue(db_session, transport=transport)
    assert result["status"] == "applied", result
    from app.models.collection import Collection
    vol = db_session.query(Collection).filter_by(external_id="xx-test-901").one()
    assert _project_of(db_session, vol.id).name == "default"


@pytest.mark.unit
def test_nothing_moves_existing_volumes(client, db_session):
    a = _project(db_session, "A")
    _project(db_session, "B")
    old = _volume(db_session, a, stem="xx-test-500")
    _as(client, "operator").post("/collections/", json={"name": "T", "filename_stem": "xx-test-400"})
    assert _project_of(db_session, old.id).id == a.id


# ---------------------------------------------------------------------------
# Rulings on the open questions (1 November)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_admin_volume_without_a_stem_is_refused_in_fisqua_mode(client, db_session, link):
    # POST /collections/ already requires a stem in every mode (CollectionCreate,
    # contract 1.1 §3), so the refusal is the schema's 422 and nothing is asked of Fisqua.
    from app.models.collection import Collection
    _unit(db_session, "fisqua")
    p = _project(db_session, "P")
    resp = _as(client, "admin").post("/collections/", json={"name": "T", "project_id": p.id})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "filename_stem"]
    resp = _as(client, "admin").post("/collections/", json={"name": "T", "project_id": p.id,
                                                            "filename_stem": None})
    assert resp.status_code == 422, resp.text
    assert db_session.query(Collection).count() == 0
    assert link.descriptions_asked == []


@pytest.mark.unit
def test_an_admin_cannot_clear_a_stem_in_fisqua_mode(client, db_session, link):
    from app.models.collection import Collection
    _unit(db_session, "fisqua")
    vol = _volume(db_session, _project(db_session, "P"))
    resp = _as(client, "admin").patch(f"/collections/{vol.id}", json={"filename_stem": None})
    _detail(resp, 422, "stem_required")
    db_session.expire_all()
    assert db_session.get(Collection, vol.id).filename_stem == "xx-test-100"


@pytest.mark.unit
def test_clearing_a_stem_is_still_allowed_outside_fisqua_mode(client, db_session):
    _unit(db_session, "backup", projects_enabled=True)
    vol = _volume(db_session, _project(db_session, "P"))
    resp = _as(client, "admin").patch(f"/collections/{vol.id}", json={"filename_stem": None})
    assert resp.status_code == 200, resp.text


@pytest.mark.unit
def test_the_hold_refuses_a_switch_to_fisqua_with_its_own_code(client, db_session, link):
    from app.core import offsite_hold, unit_mode
    _unit(db_session, "backup")
    offsite_hold.hold_file().parent.mkdir(parents=True, exist_ok=True)
    offsite_hold.hold_file().write_text("")
    link.operators_answers = [_result("delivered")]
    resp = _as(client, "admin").put("/system/unit", json={"mode": "fisqua"})
    assert _problem_codes(resp) == ["offsite_held"]
    assert link.operators_sent == []
    assert unit_mode.current(db_session).mode == "backup"


@pytest.mark.unit
def test_a_default_project_import_reports_its_notes_skipped_as_default_project(client, db_session, reports_root):
    _as_importer("op", "operator")
    doc = _list_with([_item()])
    doc["list"]["notes"] = "Con cuidado"
    resp = _post(client, doc)
    assert resp.status_code == 200, resp.text
    assert resp.json()["projectNotesSkipped"] == "default_project"
    assert resp.json()["projectNotesStored"] is False


@pytest.mark.unit
def test_override_events_are_info_in_activity(client, db_session, link, reports_root):
    _unit(db_session, "fisqua")
    p = _project(db_session, "P")
    link.description_answers = [_found("xx-test-200")] * 2
    resp = _as(client, "admin").post("/collections/", json={"name": "T", "project_id": p.id,
                                                            "filename_stem": "xx-test-200"})
    assert resp.status_code == 201, resp.text
    resp = _as(client, "admin").patch(f"/collections/{resp.json()['id']}", json={"external_id": "xx-test-200"})
    assert resp.status_code == 200, resp.text
    _as_importer("boss", "admin")
    assert _post(client, _list_with([_item()])).status_code == 200
    for action in ("volume_created_override", "volume_code_override", "list_import_override"):
        logs = _logs(db_session, action)
        assert len(logs) == 1, action
        assert (logs[0].level, logs[0].category) == ("INFO", "activity"), action


@pytest.mark.unit
def test_a_change_committed_during_the_switch_is_not_overwritten(client, db_session, link, monkeypatch):
    from app.core import fisqua_queue, unit_mode

    def racing_put(db, mode, transport=None, now=None, **kw):
        _unit(db_session, "offline")  # another admin's switch commits meanwhile
        return True, None

    monkeypatch.setattr(fisqua_queue, "send_operators_with_mode", racing_put)
    _unit(db_session, "backup")
    resp = _as(client, "admin").put("/system/unit", json={"mode": "fisqua"})
    assert _problem_codes(resp) == ["mode_changed"]
    assert unit_mode.current(db_session).mode == "offline"
    assert _logs(db_session, "unit_mode_changed") == []


# ---------------------------------------------------------------------------
# Round 3 (verifier and Codex)
# ---------------------------------------------------------------------------

# 1. A switch stops a run in progress.

@pytest.mark.unit
def test_a_switch_to_offline_stops_the_upload_drain_before_the_next_object(db_session, link, monkeypatch):
    from app.core import image_upload
    _unit(db_session, "backup")
    done = []
    monkeypatch.setattr(image_upload, "pending_work", lambda db: ["first", "second"])

    def process(db, tenant, item, result):
        done.append(item)
        _unit(db_session, "offline")

    monkeypatch.setattr(image_upload, "_process", process)
    result = image_upload.run_image_upload(db_session)
    assert done == ["first"]
    assert result["status"] == "off_in_this_mode"


def _ready_row(db, vol, manifest_id):
    from app.models.fisqua import FisquaOutbox
    db.add(FisquaOutbox(collection_id=vol.id, manifest_id=manifest_id, stem=vol.filename_stem,
                        state="ready", attempts=0, manifest={"id": manifest_id}))
    db.commit()


@pytest.mark.unit
def test_a_switch_down_stops_the_outbox_between_sends(db_session, link):
    from app.core import fisqua_outbox
    from app.models.fisqua import FisquaOutbox
    _unit(db_session, "fisqua")
    p = _project(db_session, "P")
    _ready_row(db_session, _volume(db_session, p, stem="xx-test-601"), "m-1")
    _ready_row(db_session, _volume(db_session, p, stem="xx-test-602"), "m-2")
    sent = []

    class Switching(LinkTransport):
        def send_manifest(self, manifest):
            sent.append(manifest["id"])
            _unit(db_session, "backup")
            return _result("delivered", external_id="fq-1", response={"volumeId": "fq-1"})

    result = fisqua_outbox.run_fisqua_outbox(db_session, transport=Switching())
    assert sent == ["m-1"]
    assert result["status"] == "off_in_this_mode"
    db_session.expire_all()
    assert db_session.query(FisquaOutbox).filter_by(manifest_id="m-2").one().state == "ready"


@pytest.mark.unit
def test_a_switch_down_stops_the_poll_between_batches(db_session, link, monkeypatch):
    from app.core import fisqua_contract, fisqua_outbox
    _unit(db_session, "fisqua")
    monkeypatch.setattr(fisqua_contract, "MAX_REFS_PER_CALL", 1)
    monkeypatch.setattr(fisqua_outbox, "volumes_to_poll", lambda db: {"xx-a": (1, "m-a"), "xx-b": (2, "m-b")})
    asked = []

    class Switching(LinkTransport):
        def get_status(self, refs):
            asked.append(list(refs))
            _unit(db_session, "offline")
            return _result("status", volumes=[])

    result = fisqua_outbox.poll_fisqua_status(db_session, transport=Switching())
    assert asked == [["xx-a"]]
    assert result["status"] == "off_in_this_mode"


# 2. Fisqua is told the mode actually stored.

@pytest.mark.unit
def test_a_lost_race_resends_the_stored_mode(client, db_session, link, monkeypatch):
    from app.core import fisqua_queue, unit_mode
    told = []

    def put(db, mode, transport=None, now=None, **kw):
        told.append(mode)
        if len(told) == 1:
            _unit(db_session, "offline")  # another admin's switch commits meanwhile
        return True, None

    monkeypatch.setattr(fisqua_queue, "send_operators_with_mode", put)
    _unit(db_session, "backup")
    resp = _as(client, "admin").put("/system/unit", json={"mode": "fisqua"})
    assert _problem_codes(resp) == ["mode_changed"]
    assert told == ["fisqua", "offline"]
    assert unit_mode.current(db_session).mode == "offline"
    assert len(_logs(db_session, "unit_mode_resent")) == 1


@pytest.mark.unit
def test_a_failed_commit_after_the_put_resends_the_stored_mode(db_session, link, monkeypatch):
    from sqlalchemy.exc import OperationalError
    from app.core import fisqua_queue, unit_mode
    told = []
    armed = []
    real_commit = db_session.commit

    def commit():
        if armed:
            armed.clear()
            raise OperationalError("COMMIT", {}, Exception("connection lost"))
        return real_commit()

    def put(db, mode, transport=None, now=None, **kw):
        told.append(mode)
        if len(told) == 1:
            armed.append(1)
        return True, None

    _unit(db_session, "backup")
    monkeypatch.setattr(fisqua_queue, "send_operators_with_mode", put)
    monkeypatch.setattr(db_session, "commit", commit)
    with pytest.raises(OperationalError):
        unit_mode.change(db_session, actor="boss", mode="fisqua")
    assert told == ["fisqua", "backup"]
    assert unit_mode.current(db_session).mode == "backup"


# 3. Retry-After before a mode-change PUT.

@pytest.mark.unit
def test_while_cooling_a_switch_to_fisqua_is_refused_without_a_put(client, db_session, link):
    from app.core import fisqua_outbox, unit_mode
    _unit(db_session, "backup")
    fisqua_outbox.call_budget.cool_down(60)
    link.operators_answers = [_result("delivered")]
    resp = _as(client, "admin").put("/system/unit", json={"mode": "fisqua"})
    assert _problem_codes(resp) == ["fisqua_unreachable"]
    assert resp.json()["detail"]["problems"][0]["error"].startswith("rate_limited")
    assert link.operators_sent == []
    assert unit_mode.current(db_session).mode == "backup"


@pytest.mark.unit
def test_while_cooling_a_down_switch_skips_the_put_and_is_logged(client, db_session, link):
    from app.core import fisqua_outbox, unit_mode
    _unit(db_session, "fisqua")
    fisqua_outbox.call_budget.cool_down(60)
    resp = _as(client, "admin").put("/system/unit", json={"mode": "backup"})
    assert resp.status_code == 200, resp.text
    assert link.operators_sent == []
    assert unit_mode.current(db_session).mode == "backup"
    logs = _logs(db_session, "unit_mode_changed")
    assert logs[0].level == "WARN" and "rate_limited" in logs[0].detail


# 4. The catalogue dump's recipient.

@pytest.mark.unit
@pytest.mark.parametrize("value", ["", "not-a-key", "AGE-SECRET-KEY-1QQQQ"])
def test_a_missing_or_invalid_recipient_is_a_missing_dump(db_session, tenant, monkeypatch, value):
    from app.core import config, unit_mode
    monkeypatch.setattr(config.settings, "OFFSITE_REMOTE", "b2unit:bucket")
    monkeypatch.setattr(config.settings, "OFFSITE_DUMP_RECIPIENT", value, raising=False)
    assert unit_mode.prerequisites()["backup"] == ["catalogue_dump_not_configured"]


@pytest.mark.unit
def test_a_valid_recipient_completes_the_dump(db_session, tenant, monkeypatch):
    from app.core import config, unit_mode
    monkeypatch.setattr(config.settings, "OFFSITE_REMOTE", "b2unit:bucket")
    assert unit_mode.prerequisites()["backup"] == []


# 5. The lookup's answer names the code asked for.

@pytest.mark.unit
@pytest.mark.parametrize("external_id, answered", [("xx-test-200", "xx-test-201"), (".", "xx-test-200")])
def test_a_200_for_another_code_is_an_unknown_code(client, db_session, link, external_id, answered):
    from app.models.collection import Collection
    _unit(db_session, "fisqua")
    p = _project(db_session, "P")
    link.description_answers = ([_found(answered)] if external_id == "xx-test-200"
                                else [_found("xx-test-200"), _found(answered)])
    resp = _as(client, "admin").post("/collections/", json={
        "name": "T", "project_id": p.id, "filename_stem": "xx-test-200", "external_id": external_id})
    _detail(resp, 422, "unknown_reference_code")
    assert db_session.query(Collection).count() == 0


@pytest.mark.unit
@pytest.mark.parametrize("body", [{}, {"referenceCode": 7}, {"title": "T"}, ["xx-test-200"]])
def test_a_200_without_a_string_reference_code_is_unreachable(client, db_session, link, body):
    _unit(db_session, "fisqua")
    p = _project(db_session, "P")
    link.description_answers = [_result("description", response=body)]
    resp = _as(client, "admin").post("/collections/", json={"name": "T", "project_id": p.id,
                                                            "filename_stem": "xx-test-200"})
    _detail(resp, 409, "fisqua_unreachable")


@pytest.mark.unit
def test_a_dot_code_is_sent_as_one_segment_not_a_dot_segment():
    server = Server(httpx.Response(200, json={"referenceCode": "."}))
    _http(server, bypass=True).get_description(".")
    assert server.requests[0].url.raw_path.decode() == "/api/capture/v1/descriptions/%2E"


# 6. Hidden projects ignore project_id.

@pytest.mark.unit
def test_hidden_projects_ignore_a_given_project_id(client, db_session):
    a = _project(db_session, "A")
    _project(db_session, "B")
    resp = _as(client, "operator").post("/collections/", json={"name": "T", "project_id": a.id,
                                                               "filename_stem": "xx-test-700"})
    assert resp.status_code == 201, resp.text
    assert _project_of(db_session, resp.json()["id"]).name == "default"


@pytest.mark.unit
def test_shown_projects_keep_a_given_project_id(client, db_session):
    _unit(db_session, "offline", projects_enabled=True)
    a = _project(db_session, "A")
    _project(db_session, "B")
    resp = _as(client, "operator").post("/collections/", json={"name": "T", "project_id": a.id,
                                                               "filename_stem": "xx-test-700"})
    assert _project_of(db_session, resp.json()["id"]).id == a.id


@pytest.mark.unit
def test_hidden_projects_keep_subcollections_outside_fisqua_mode(client, db_session):
    from app.models.collection import Collection
    parent = _volume(db_session, _project(db_session, "A"), stem="xx-test-701")
    resp = _as(client, "operator").post("/collections/", json={
        "name": "T", "parent_collection_id": parent.id, "filename_stem": "xx-test-702"})
    assert resp.status_code == 201, resp.text
    db_session.expire_all()
    child = db_session.get(Collection, resp.json()["id"])
    assert (child.parent_collection_id, child.project_id) == (parent.id, None)


# 7. Freeing, restore and mark-freed in offline mode.

@pytest.mark.unit
def test_offline_refuses_freeing(client, db_session, link):
    _unit(db_session, "offline")
    vol = _volume(db_session, _project(db_session, "P"))
    resp = _as(client, "admin").post(f"/collections/{vol.id}/offsite/free")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "off_in_this_mode"


@pytest.mark.unit
def test_backup_lets_freeing_reach_its_own_checks(client, db_session, link):
    _unit(db_session, "backup")
    vol = _volume(db_session, _project(db_session, "P"))
    resp = _as(client, "admin").post(f"/collections/{vol.id}/offsite/free")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"].get("code") != "off_in_this_mode"


@pytest.mark.unit
def test_offline_refuses_mark_freed(db_session, link):
    from app.core.offsite_free import mark_freed
    _unit(db_session, "offline")
    _volume(db_session, _project(db_session, "P"))
    with pytest.raises(ValueError, match="offline"):
        mark_freed(db_session, "xx-test-100")


# 8. Unknown keys.

@pytest.mark.unit
def test_put_unit_refuses_unknown_keys(client, db_session):
    resp = _as(client, "admin").put("/system/unit", json={"mode": "offline", "projects": True})
    assert resp.status_code == 422, resp.text


# 9. More.

@pytest.mark.unit
def test_the_token_never_reaches_the_lookup_refusal_or_the_log(client, db_session, link, monkeypatch, caplog):
    from app.core import unit_mode
    server = Server(httpx.ConnectError(f"refused for {TOKEN}"))
    monkeypatch.setattr(unit_mode, "get_lookup_transport", lambda: _http(server))
    _unit(db_session, "fisqua")
    p = _project(db_session, "P")
    resp = _as(client, "admin").post("/collections/", json={"name": "T", "project_id": p.id,
                                                            "filename_stem": "xx-test-200"})
    _detail(resp, 409, "fisqua_unreachable")
    assert server.requests and TOKEN not in resp.text
    assert TOKEN not in caplog.text


@pytest.mark.unit
def test_a_retry_after_on_the_lookup_cools_the_link(client, db_session, link):
    from app.core import fisqua_outbox
    _unit(db_session, "fisqua")
    p = _project(db_session, "P")
    link.description_answers = [_result("error", status_code=429, error="HTTP 429", retry_after=30.0)]
    _as(client, "admin").post("/collections/", json={"name": "T", "project_id": p.id,
                                                     "filename_stem": "xx-test-200"})
    assert fisqua_outbox.call_budget.cooling()


@pytest.mark.unit
def test_rows_queued_outside_fisqua_mode_are_sent_after_returning(client, db_session, vol, fisqua):
    from tests.unit.test_fisqua_outbox import FakeTransport, _delivered, _handover, _rows, _run
    _unit(db_session, "backup", projects_enabled=False)
    vol.record(status="captured")
    _handover(client, vol.col)
    assert _run(db_session, FakeTransport(sends=[_delivered()]))["status"] == "off_in_this_mode"
    assert [r.state for r in _rows(db_session, vol.col)] == ["waiting_for_upload"]
    _unit(db_session, "fisqua")
    _run(db_session, FakeTransport(sends=[_delivered("fq-9")]))
    assert [(r.state, r.external_id) for r in _rows(db_session, vol.col)] == [("delivered", "fq-9")]


@pytest.mark.unit
def test_a_down_switch_put_failure_is_logged_as_warn(client, db_session, link):
    _unit(db_session, "fisqua")
    link.operators_answers = [_result("error", error="HTTP 503")]
    assert _as(client, "admin").put("/system/unit", json={"mode": "backup"}).status_code == 200
    logs = _logs(db_session, "unit_mode_changed")
    assert len(logs) == 1 and logs[0].level == "WARN" and "Fisqua not told" in logs[0].detail


# ---------------------------------------------------------------------------
# Round 4
# ---------------------------------------------------------------------------

LINK_CALLS = [
    ("send_manifest", ({"id": "m-1"},)),
    ("get_status", (["xx-a"],)),
    ("get_decisions", ("xx-a",)),
    ("put_operators", ({"operators": [], "mode": "fisqua"},)),
    ("get_queue", (None,)),
    ("post_queue_ack", ({"revision": "r1", "applied": [], "skipped": []},)),
    ("get_description", ("xx-a",)),
]


# 1. The transport is the gate.

@pytest.mark.unit
@pytest.mark.parametrize("mode", ["offline", "backup"])
def test_outside_fisqua_mode_none_of_the_seven_calls_is_sent_and_each_bypass_is(db_session, fisqua, mode):
    from app.core.fisqua_http import HttpFisquaTransport
    _unit(db_session, mode)
    server = Server(*[httpx.Response(200, json={"referenceCode": "xx-a"}) for _ in LINK_CALLS])
    gated = _http(server)
    for name, args in LINK_CALLS:
        assert getattr(gated, name)(*args).outcome == "off_in_this_mode", name
    assert server.requests == []
    bypass = HttpFisquaTransport(BASE, TOKEN, client=httpx.Client(transport=httpx.MockTransport(server)),
                                 bypass_mode=True)
    for name, args in LINK_CALLS:
        getattr(bypass, name)(*args)
    assert len(server.requests) == len(LINK_CALLS)


@pytest.mark.unit
def test_in_fisqua_mode_the_gated_transport_sends(db_session, fisqua):
    _unit(db_session, "fisqua")
    server = Server(httpx.Response(200, json={"referenceCode": "xx-a"}))
    assert _http(server).get_description("xx-a").outcome == "description"
    assert len(server.requests) == 1


@pytest.mark.unit
def test_every_public_transport_call_reads_the_mode_before_it_sends():
    """The completeness check: each method that touches the client asks the gate first."""
    import inspect
    from app.core.fisqua_http import HttpFisquaTransport
    senders = []
    for name, fn in inspect.getmembers(HttpFisquaTransport, inspect.isfunction):
        source = inspect.getsource(fn)
        if "self._client." in source and name != "close":
            senders.append(name)
            assert source.index("self._mode_refusal()") < source.index("self._client."), name
    assert sorted(senders) == sorted(name for name, _ in LINK_CALLS)


@pytest.mark.unit
def test_only_unit_mode_asks_for_a_bypass(backend_root):
    users = sorted(p.relative_to(backend_root).as_posix() for p in (backend_root / "app").rglob("*.py")
                   if "bypass_mode=True" in p.read_text(encoding="utf-8"))
    assert users == ["app/core/unit_mode.py"]


@pytest.mark.unit
def test_a_send_refused_by_the_gate_leaves_the_row_ready_without_an_attempt(db_session, fisqua, monkeypatch):
    """With the run's own mode checks out of the way, the transport still sends nothing."""
    from app.core import fisqua_outbox, unit_mode
    from app.models.fisqua import FisquaOutbox
    p = _project(db_session, "P")
    _ready_row(db_session, _volume(db_session, p, stem="xx-test-801"), "m-1")
    _unit(db_session, "offline")
    monkeypatch.setattr(unit_mode, "link_on", lambda db: True)
    server = Server()
    result = fisqua_outbox.run_fisqua_outbox(db_session, transport=_http(server))
    assert server.requests == []
    assert result["status"] == "off_in_this_mode"
    db_session.expire_all()
    row = db_session.query(FisquaOutbox).one()
    assert (row.state, row.attempts) == ("ready", 0)


# 2. A queue ack refused by the mode is kept.

@pytest.mark.unit
def test_an_ack_refused_by_the_mode_is_kept_and_posted_after_the_return(db_session, link, reports_root):
    from app.core import fisqua_queue
    from tests.unit.test_fisqua_queue import QueueTransport, _ok_ack, _qitem, _queue
    transport = QueueTransport(queues=[_queue([_qitem()], revision="r1")],
                               acks=[_result("off_in_this_mode", error="not in fisqua mode"), _ok_ack()])
    result = fisqua_queue.sync_fisqua_queue(db_session, transport=transport)
    assert result["status"] == "off_in_this_mode", result
    pending = fisqua_queue._read_state()["pendingAck"]
    assert pending["revision"] == "r1"
    result = fisqua_queue.sync_fisqua_queue(db_session, transport=transport)
    assert len(transport.acks_sent) == 2 and transport.acks_sent[1] == pending
    assert fisqua_queue._read_state() == {"revision": "r1", "pendingAck": None}


# 3. The mode PUT and its commit are serialised with regular operators sends.

@pytest.mark.unit
def test_a_regular_operators_send_waits_for_a_switch_and_then_follows_it(db_session, link):
    import threading
    import time
    from app.core import fisqua_queue, unit_mode
    from app.core.db import SessionLocal
    _unit(db_session, "fisqua")
    regular = LinkTransport(operators=[_result("delivered")])
    outcome = {}

    def regular_send():
        db = SessionLocal()
        try:
            fisqua_queue.mark_operators_due()
            outcome["result"] = fisqua_queue.send_operators(db, transport=regular)
        finally:
            db.close()

    thread = threading.Thread(target=regular_send)

    class Switching(LinkTransport):
        def put_operators(self, body):
            self.operators_sent.append(body)
            thread.start()
            time.sleep(0.3)
            outcome["waited"] = thread.is_alive()
            return _result("delivered")

    unit_mode.change(db_session, actor="boss", mode="backup", transport=Switching())
    thread.join(5)
    assert outcome["waited"] is True
    assert regular.operators_sent == []
    assert outcome["result"]["status"] == "off_in_this_mode"


# 4. Reading from the bucket is allowed in offline mode.

@pytest.mark.unit
def test_offline_allows_restore(db_session, link, monkeypatch):
    from app.core import config, offsite_free
    monkeypatch.setattr(config.settings, "OFFSITE_READ_REMOTE", "b2read:bucket")
    _unit(db_session, "offline")
    vol = _volume(db_session, _project(db_session, "P"))
    collection, hold = offsite_free._prepare_restore(db_session, vol.id)
    hold.release()
    assert collection.id == vol.id


@pytest.mark.unit
def test_offline_allows_the_free_dry_run(client, db_session, link):
    _unit(db_session, "offline")
    vol = _volume(db_session, _project(db_session, "P"))
    resp = _as(client, "admin").get(f"/collections/{vol.id}/offsite/free")
    detail = resp.json().get("detail") if resp.status_code != 200 else {}
    assert not (isinstance(detail, dict) and detail.get("code") == "off_in_this_mode"), resp.text


# 5. A retest stopped by a switch is labelled as the mode's.

@pytest.mark.unit
def test_a_retest_stopped_by_a_switch_is_labelled_off_in_this_mode(db_session, link, monkeypatch):
    from datetime import datetime
    from app.core import fisqua_outbox, unit_mode
    fisqua_outbox.record_config_error(400, "version", datetime(2020, 1, 1))
    answers = iter([True])
    monkeypatch.setattr(unit_mode, "link_on", lambda db: next(answers, False))
    result = fisqua_outbox.run_fisqua_outbox(db_session, transport=link)
    assert result["status"] == "off_in_this_mode"


# 6. An admin subcollection in fisqua mode.

@pytest.mark.unit
def test_an_admin_subcollection_in_fisqua_mode_is_checked_and_logged(client, db_session, link):
    from app.models.collection import Collection
    _unit(db_session, "fisqua")
    parent = _volume(db_session, _project(db_session, "P"), stem="xx-test-901")
    link.description_answers = [_found("xx-test-902")]
    resp = _as(client, "admin").post("/collections/", json={
        "name": "T", "parent_collection_id": parent.id, "filename_stem": "xx-test-902"})
    assert resp.status_code == 201, resp.text
    assert link.descriptions_asked == ["xx-test-902"]
    db_session.expire_all()
    assert db_session.get(Collection, resp.json()["id"]).parent_collection_id == parent.id
    logs = _logs(db_session, "volume_created_override")
    assert len(logs) == 1 and "xx-test-902" in logs[0].detail
