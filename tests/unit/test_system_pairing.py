"""Pairing by a code, through the API (NEH-345): GET/POST/DELETE /system/pairing and /system/pairing/renew, and their gates.

The registry the routes use is replaced by one whose sessions talk to a
fake client on a fake clock, with no thread; the gates are the real ones
(app.core.fisqua_pairing_policy), over a scratch data directory, a stub
provisioning script and a test database. No answer may carry the collect
secret, the token or a key: every response body in these tests is kept
and scanned.

    python -m pytest tests/unit/test_system_pairing.py

Version: 0.3.3
"""

import json

import pytest
from pydantic import SecretStr

from tests.unit.test_capture_manifest_export import _as
from tests.unit.test_fisqua_pairing_http import CODE, READ_APP_KEY, SECRET, TOKEN, WRITE_APP_KEY, payload
from tests.unit.test_fisqua_pairing_session import (SECOND_CODE, SECOND_SECRET, Applied, Clock, FakeClient,
                                                    answer, code)

SECRETS = (SECRET, SECOND_SECRET, TOKEN, WRITE_APP_KEY, READ_APP_KEY, "005writekeyid", "005readkeyid")


class Bodies:
    """Every response body, kept for the final scan."""

    def __init__(self):
        self.seen = []

    def keep(self, response):
        self.seen.append(response.text)
        return response

    def assert_clean(self):
        text = "\n".join(self.seen)
        for secret in SECRETS:
            assert secret not in text


@pytest.fixture
def bodies():
    kept = Bodies()
    yield kept
    kept.assert_clean()


@pytest.fixture
def unit(monkeypatch, tmp_path, db_session):
    """A never-linked unit with a device id, rclone, and a provisioning script that takes --replace-credential."""
    from app.core import config, device_id, fisqua_pairing_apply

    data = tmp_path / "data"
    monkeypatch.setattr(config.settings, "DATA_DIR", str(data))
    monkeypatch.setattr(config.settings, "RCLONE_CONFIG", str(data / "rclone.conf"))
    monkeypatch.setattr(config.settings, "OFFSITE_HOLD_FILE", "")
    monkeypatch.setattr(config.settings, "FISQUA_ENV", "")
    monkeypatch.setattr(config.settings, "FISQUA_TOKEN", SecretStr(""))
    monkeypatch.setattr(config.settings, "FISQUA_PAIRING_BASE_URL", "")
    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", "")
    monkeypatch.setattr(config.settings, "OFFSITE_REMOTE", "")
    script = tmp_path / "scripts" / "provision-rclone.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/bash\ncase \"$1\" in\n    --replace-credential) : ;;\nesac\n")
    monkeypatch.setattr(fisqua_pairing_apply, "provision_script", lambda backend_dir=None: script)
    monkeypatch.setattr(fisqua_pairing_apply, "rclone_available", lambda: True)
    device_id.initialise()
    return data


@pytest.fixture
def linked(unit, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "FISQUA_ENV", "production")
    monkeypatch.setattr(config.settings, "FISQUA_TOKEN", SecretStr("ab" * 32))
    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", "ahr")
    monkeypatch.setattr(config.settings, "OFFSITE_REMOTE", "b2unit:captua-masters")
    return "ahr"


def set_mode(db, mode, by=None):
    from app.models.unit_settings import UnitSettings

    row = db.get(UnitSettings, 1)
    if row is None:
        row = UnitSettings(id=1, mode=mode, projects_enabled=False)
        db.add(row)
    row.mode, row.updated_by = mode, by
    db.commit()


@pytest.fixture
def registry(monkeypatch, unit):
    """The routes' registry: fake clients in order, the fake clock, the real gates, a stub apply."""
    from app.core import fisqua_pairing, fisqua_pairing_service

    clients = []
    clock = Clock()
    applied = Applied(result="restarting")
    reg = fisqua_pairing.PairingRegistry(
        apply=applied, before_begin=fisqua_pairing_service.before_begin,
        before_code=fisqua_pairing_service.before_code, before_apply=fisqua_pairing_service.before_apply,
        client_factory=lambda url: clients.pop(0), clock=clock, start_threads=False)
    monkeypatch.setattr(fisqua_pairing_service, "pairing_registry", reg)
    reg.clients, reg.clock, reg.applied = clients, clock, applied
    yield reg
    reg.shutdown()


def admin(client):
    return _as(client, "admin", "admin1")


def step(registry, seconds=0):
    """Advance the clock and step the current session once, as its thread would."""
    registry.clock.advance(seconds)
    session = registry.current()
    session.touch()
    return session.step()


# ---------------------------------------------------------------------------
# Who may call
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["operator", "reviewer"])
def test_admin_only(client, registry, role):
    c = _as(client, role)
    assert c.get("/system/pairing").status_code == 403
    assert c.post("/system/pairing", json={"unitName": "Sala 1"}).status_code == 403
    assert c.post("/system/pairing/renew").status_code == 403
    assert c.delete("/system/pairing").status_code == 403
    assert registry.current() is None


# ---------------------------------------------------------------------------
# The body
# ---------------------------------------------------------------------------

def test_get_idle(client, registry, bodies):
    body = bodies.keep(admin(client).get("/system/pairing")).json()
    assert body["state"] == "idle"
    assert len(body["deviceId"]) == 8
    assert body["code"] is None
    assert body["environment"] == "production"
    assert body["currentTenant"] is None
    assert body["linked"] == {"configured": False, "tenant": None, "environment": None}
    assert body["eligible"] == {"ok": True, "reason": None}
    assert body["result"] is None


def test_post_starts_and_get_shows_code(client, registry, bodies):
    registry.clients.append(FakeClient(codes=[code()]))
    c = admin(client)
    body = bodies.keep(c.post("/system/pairing", json={"unitName": "Sala 1"})).json()
    assert body["state"] == "requesting"
    assert registry.current().actor == "admin1"
    step(registry)
    body = bodies.keep(c.get("/system/pairing")).json()
    assert (body["state"], body["code"], body["expiresInSeconds"], body["unitName"]) == (
        "showing", CODE, 120, "Sala 1")
    assert body["renewalSecondsLeft"] == 600
    # A second POST answers the running session.
    again = bodies.keep(c.post("/system/pairing", json={"unitName": "Sala 2"})).json()
    assert again["unitName"] == "Sala 1"


def test_get_is_the_heartbeat(client, registry, bodies):
    registry.clients.append(FakeClient(codes=[code()]))
    c = admin(client)
    bodies.keep(c.post("/system/pairing", json={"unitName": "Sala 1"}))
    session = registry.current()
    session.step()
    for _ in range(3):
        registry.clock.advance(25)
        bodies.keep(c.get("/system/pairing"))
        session.step()
    assert session.state == "showing"
    registry.clock.advance(31)
    session.step()
    assert bodies.keep(c.get("/system/pairing")).json()["state"] == "ended"


@pytest.mark.parametrize("name", ["", "x" * 65])
def test_unit_name_checked(client, registry, name):
    resp = admin(client).post("/system/pairing", json={"unitName": name})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "unit_name"
    assert registry.current() is None


def test_response_never_contains_secret(client, registry, bodies):
    registry.clients.append(FakeClient(codes=[code()], collects=[
        answer("waiting"), answer("busy", retry_after=5.0), answer("network"),
        answer("payload", payload=payload())]))
    c = admin(client)
    bodies.keep(c.post("/system/pairing", json={"unitName": "Sala 1"}))
    for _ in range(12):
        bodies.keep(c.get("/system/pairing"))
        if step(registry, 5) is None:
            break
    body = bodies.keep(c.get("/system/pairing")).json()
    assert body["state"] == "restarting"
    assert registry.applied.calls == [payload()]
    bodies.keep(c.delete("/system/pairing"))


def test_delete_cancels(client, registry, bodies):
    registry.clients.append(FakeClient(codes=[code()]))
    c = admin(client)
    bodies.keep(c.post("/system/pairing", json={"unitName": "Sala 1"}))
    step(registry)
    body = bodies.keep(c.delete("/system/pairing")).json()
    assert (body["state"], body["code"]) == ("cancelled", None)


def test_renew_after_lapse(client, registry, bodies):
    registry.clients += [FakeClient(codes=[code()], collects=[answer("expired")]),
                         FakeClient(codes=[code(SECOND_CODE, SECOND_SECRET)])]
    c = admin(client)
    assert c.post("/system/pairing/renew").status_code == 409
    bodies.keep(c.post("/system/pairing", json={"unitName": "Sala 1"}))
    step(registry)
    step(registry, 601)
    assert bodies.keep(c.get("/system/pairing")).json()["state"] == "lapsed"
    body = bodies.keep(c.post("/system/pairing/renew")).json()
    assert body["state"] == "requesting"
    step(registry)
    body = bodies.keep(c.get("/system/pairing")).json()
    assert (body["state"], body["code"], body["unitName"]) == ("showing", SECOND_CODE, "Sala 1")


def test_renew_refused_while_running(client, registry):
    registry.clients.append(FakeClient(codes=[code()]))
    c = admin(client)
    c.post("/system/pairing", json={"unitName": "Sala 1"})
    resp = c.post("/system/pairing/renew")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "not_renewable"


def test_result_and_link_shown_after_restart(client, registry, linked, bodies):
    from app.core import fisqua_pairing_apply

    fisqua_pairing_apply.write_result("ahr", "production", "admin1")
    body = bodies.keep(admin(client).get("/system/pairing")).json()
    assert body["state"] == "idle"
    assert body["linked"] == {"configured": True, "tenant": "ahr", "environment": "production"}
    assert body["currentTenant"] == "ahr"
    assert (body["result"]["tenant"], body["result"]["by"]) == ("ahr", "admin1")


# ---------------------------------------------------------------------------
# The gates before a code
# ---------------------------------------------------------------------------

def _refused(client, reason):
    resp = admin(client).post("/system/pairing", json={"unitName": "Sala 1"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == reason
    body = admin(client).get("/system/pairing").json()
    assert body["eligible"] == {"ok": False, "reason": reason}


def test_offline_without_a_link_may_pair(client, registry, db_session):
    from app.core import fisqua_pairing_policy

    assert fisqua_pairing_policy.mode_gate(db_session) is None  # no row yet: inferred offline
    set_mode(db_session, "offline", by=None)
    assert fisqua_pairing_policy.mode_gate(db_session) is None


def test_fisqua_mode_may_pair(client, registry, db_session, linked):
    from app.core import fisqua_pairing_policy

    set_mode(db_session, "fisqua", by="admin1")
    assert fisqua_pairing_policy.mode_gate(db_session) is None


def test_offline_chosen_by_an_admin_may_pair(client, registry, db_session):
    """Whoever last changed the unit's settings: asking for a code is itself the administrator's act."""
    from app.core import fisqua_pairing_policy

    set_mode(db_session, "offline", by="admin1")
    assert fisqua_pairing_policy.mode_gate(db_session) is None
    registry.clients.append(FakeClient(codes=[code()]))
    assert admin(client).post("/system/pairing", json={"unitName": "Sala 1"}).status_code == 200


@pytest.mark.parametrize("by", [None, "admin1"])
def test_offline_and_backup_refused(client, registry, db_session, by):
    set_mode(db_session, "backup", by=by)
    _refused(client, "mode_not_eligible")


@pytest.mark.parametrize("by", [None, "admin1"])
def test_offline_with_a_link_configured_refused(client, registry, db_session, linked, by):
    set_mode(db_session, "offline", by=by)
    _refused(client, "mode_not_eligible")


def test_hold_refused(client, registry, unit):
    (unit / "offsite-hold").write_text("")
    _refused(client, "offsite_held")


def test_rclone_missing_refused(client, registry, monkeypatch):
    from app.core import fisqua_pairing_apply

    monkeypatch.setattr(fisqua_pairing_apply, "rclone_available", lambda: False)
    _refused(client, "rclone_missing")


def test_capture_in_progress_refused(client, registry):
    from app.core import unit_activity

    with unit_activity.tracking():
        _refused(client, "busy_capturing")
    assert admin(client).get("/system/pairing").json()["eligible"]["ok"] is True


def test_script_too_old_refused_before_a_code(client, registry, unit, monkeypatch, tmp_path):
    from app.core import fisqua_pairing_apply

    old = tmp_path / "old.sh"
    old.write_text("#!/bin/bash\n")
    monkeypatch.setattr(fisqua_pairing_apply, "provision_script", lambda backend_dir=None: old)
    (unit / "rclone.conf").write_text("[b2unit]\n")
    _refused(client, "script_too_old")


def test_script_missing_refused(client, registry, monkeypatch, tmp_path):
    from app.core import fisqua_pairing_apply

    monkeypatch.setattr(fisqua_pairing_apply, "provision_script", lambda backend_dir=None: tmp_path / "absent.sh")
    _refused(client, "script_missing")


def test_device_id_invalid_refused(client, registry, unit):
    (unit / "device-id").write_text("not an id")
    resp = admin(client).post("/system/pairing", json={"unitName": "Sala 1"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "device_id_invalid"
    body = admin(client).get("/system/pairing").json()
    assert (body["deviceId"], body["deviceIdError"]) == (None, "invalid")


TRACKED_CAMERA_ROUTES = {
    ("POST", "/capture"), ("POST", "/capture/dual"), ("POST", "/test-capture/{camera_index}"),
    ("POST", "/focus/{camera_index}"), ("POST", "/focus-lock/{camera_index}"),
    ("POST", "/settings/{camera_index}"), ("POST", "/calibrate"), ("POST", "/calibrate/white-balance"),
    ("PUT", "/dslr/{camera_index}/settings"), ("POST", "/rescan"),
}


def test_camera_routes_are_tracked():
    """Every route that takes a picture or holds a camera busy; the live preview's single frames are not."""
    from app.api.cameras import router
    from app.core import unit_activity

    tracked = {(method, route.path) for route in router.routes for method in route.methods
               if any(d.call is unit_activity.tracked for d in route.dependant.dependencies)}
    assert tracked == TRACKED_CAMERA_ROUTES


def test_test_capture_counts_as_a_capture(client, monkeypatch):
    from capture import service as capture_service
    from app.core import unit_activity

    seen = []
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda index: True)

    def fake_test_capture(index, resolution, focus_check=False):
        seen.append(unit_activity.in_progress())
        raise RuntimeError("no camera here")

    monkeypatch.setattr(capture_service, "test_capture_bytes", fake_test_capture)
    resp = _as(client, "operator").post("/cameras/test-capture/0")
    assert resp.status_code == 500
    assert seen == [True]
    assert not unit_activity.in_progress()


def test_unit_activity_counts():
    from app.core import unit_activity

    assert not unit_activity.in_progress()
    with unit_activity.tracking():
        with unit_activity.tracking():
            assert unit_activity.in_progress()
        assert unit_activity.in_progress()
    assert not unit_activity.in_progress()
    with pytest.raises(RuntimeError):
        with unit_activity.tracking():
            raise RuntimeError("capture failed")
    assert not unit_activity.in_progress()


# ---------------------------------------------------------------------------
# The workspace gate at collection
# ---------------------------------------------------------------------------

def test_other_workspace_refused(client, registry, linked, db_session, bodies):
    set_mode(db_session, "fisqua", by="admin1")
    registry.clients.append(FakeClient(codes=[code()], collects=[answer("payload", payload=payload(tenant="other"))]))
    c = admin(client)
    bodies.keep(c.post("/system/pairing", json={"unitName": "Sala 1"}))
    assert registry.clients == []
    session = registry.current()
    assert session.current_tenant == "ahr"
    step(registry)
    step(registry, 5)
    assert session.state == "other_workspace"
    assert registry.applied.calls == []
    assert bodies.keep(c.get("/system/pairing")).json()["state"] == "other_workspace"


def test_same_tenant_relinks(client, registry, linked, db_session):
    set_mode(db_session, "fisqua", by="admin1")
    registry.clients.append(FakeClient(codes=[code()], collects=[answer("payload", payload=payload())]))
    admin(client).post("/system/pairing", json={"unitName": "Sala 1"})
    step(registry)
    step(registry, 5)
    assert registry.current().state == "restarting"
    assert registry.applied.calls == [payload()]


def test_never_linked_accepts_any_tenant():
    from app.core import fisqua_pairing_policy

    assert fisqua_pairing_policy.workspace_gate(payload(tenant="other"), None) is None
    assert fisqua_pairing_policy.workspace_gate(payload(tenant="ahr"), "ahr") is None
    assert fisqua_pairing_policy.workspace_gate(payload(tenant="other"), "ahr") == "other_workspace"


# ---------------------------------------------------------------------------
# The apply step's wiring
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Scheduler:
    """A scheduler whose worker exits `exits_after` seconds of waiting after its stop is requested (None: never)."""

    def __init__(self, name, log, clock, exits_after=0.0, running=True):
        self.name, self.log, self.clock = name, log, clock
        self.exits_after = exits_after
        self.alive = running
        self.stop_requested_at = None

    def request_stop(self):
        self.log.append(("request_stop", self.name))
        self.stop_requested_at = self.clock.now

    def wait_stopped(self, timeout):
        if self.alive and self.exits_after is not None and \
                self.clock.now + timeout >= self.stop_requested_at + self.exits_after:
            self.clock.now = max(self.clock.now, self.stop_requested_at + self.exits_after)
            self.alive = False
        elif self.alive:
            self.clock.now += timeout
        return not self.alive

    def worker_alive(self):
        return self.alive

    def start(self):
        # The invariant under test: never started while its earlier worker lives.
        assert not self.alive, f"{self.name} started while its worker is alive"
        self.log.append(("start", self.name))
        self.alive = True
        return True

    def stop(self, timeout=5.0):
        raise AssertionError("pairing must not use stop(), which forgets a live worker")


@pytest.fixture
def schedulers(monkeypatch):
    from app.core import fisqua_pairing_service

    log, clock = [], FakeClock()
    made = {name: Scheduler(name, log, clock) for name in ("status", "images", "catalogue")}
    monkeypatch.setattr(fisqua_pairing_service, "_schedulers",
                        lambda: [made["status"], made["images"], made["catalogue"]])
    monkeypatch.setattr(fisqua_pairing_service, "_clock", clock)
    made["log"] = log
    made["clock"] = clock
    return made


class Progress:
    def __init__(self, actor="admin1"):
        self.actor, self.current_tenant = actor, None
        self.reasons = []

    def set_reason(self, reason):
        self.reasons.append(reason)


def test_apply_link_writes_the_result_and_restarts(unit, schedulers, monkeypatch):
    from app.core import fisqua_pairing_apply, fisqua_pairing_policy, fisqua_pairing_service

    monkeypatch.setattr(fisqua_pairing_apply, "apply_payload", lambda body, **kw: "linked")
    monkeypatch.setattr(fisqua_pairing_policy, "restart_after_link", lambda: "restarting")
    session = Progress()
    assert fisqua_pairing_service.apply_link(payload(), session) == "restarting"
    assert schedulers["log"] == [("request_stop", "status"), ("request_stop", "images"),
                                 ("request_stop", "catalogue")]
    assert session.reasons == ["finishing_uploads", "writing_link"]
    result = json.loads((unit / "pairing-result.json").read_text())
    assert (result["tenant"], result["environment"], result["by"]) == ("ahr", "production", "admin1")


def test_apply_waits_for_a_late_worker_before_running_the_script(unit, schedulers, monkeypatch):
    from app.core import fisqua_pairing_apply, fisqua_pairing_policy, fisqua_pairing_service

    schedulers["images"].exits_after = 240.0
    ran = []

    def script(body, **kw):
        ran.append([s.alive for s in (schedulers["status"], schedulers["images"], schedulers["catalogue"])])
        return "linked"

    monkeypatch.setattr(fisqua_pairing_apply, "apply_payload", script)
    monkeypatch.setattr(fisqua_pairing_policy, "restart_after_link", lambda: "restarting")
    assert fisqua_pairing_service.apply_link(payload(), Progress()) == "restarting"
    assert ran == [[False, False, False]]
    assert 240 <= schedulers["clock"].now < 600


def test_a_worker_that_never_exits_stops_the_apply(unit, schedulers, monkeypatch):
    from app.core import fisqua_pairing_apply, fisqua_pairing_service

    schedulers["images"].exits_after = None
    ran = []
    monkeypatch.setattr(fisqua_pairing_apply, "apply_payload", lambda body, **kw: ran.append(1) or "linked")
    session = Progress()
    assert fisqua_pairing_service.apply_link(payload(), session) == "apply_failed"
    assert session.reasons[-1] == "uploads_still_running"
    assert ran == []
    assert schedulers["clock"].now >= 600
    # The two whose workers exited are started again; the live one is not.
    starts = [entry for entry in schedulers["log"] if entry[0] == "start"]
    assert sorted(starts) == [("start", "catalogue"), ("start", "status")]
    assert not (unit / "pairing-result.json").exists()


@pytest.mark.parametrize("state", ["apply_failed", "script_too_old", "script_missing", "bad_payload"])
def test_nothing_written_restarts_the_exited_schedulers(unit, schedulers, monkeypatch, state):
    from app.core import fisqua_pairing_apply, fisqua_pairing_service

    monkeypatch.setattr(fisqua_pairing_apply, "apply_payload", lambda body, **kw: state)
    assert fisqua_pairing_service.apply_link(payload(), Progress()) == state
    assert schedulers["log"][-3:] == [("start", "catalogue"), ("start", "images"), ("start", "status")]
    assert not (unit / "pairing-result.json").exists()


@pytest.mark.parametrize("state", ["apply_unverified", "settings_overridden"])
def test_something_written_starts_nothing_in_process(unit, schedulers, monkeypatch, state):
    from app.core import fisqua_pairing_apply, fisqua_pairing_service

    monkeypatch.setattr(fisqua_pairing_apply, "apply_payload", lambda body, **kw: state)
    assert fisqua_pairing_service.apply_link(payload(), Progress()) == state
    assert not [entry for entry in schedulers["log"] if entry[0] == "start"]


def test_apply_link_restart_needed_starts_nothing(unit, schedulers, monkeypatch):
    from app.core import fisqua_pairing_apply, fisqua_pairing_policy, fisqua_pairing_service

    monkeypatch.setattr(fisqua_pairing_apply, "apply_payload", lambda body, **kw: "linked")
    monkeypatch.setattr(fisqua_pairing_policy, "restart_after_link", lambda: "restart_needed")
    assert fisqua_pairing_service.apply_link(payload(), Progress()) == "restart_needed"
    assert not [entry for entry in schedulers["log"] if entry[0] == "start"]
    assert (unit / "pairing-result.json").exists()


def test_session_shows_the_wait(unit, registry, client, monkeypatch, bodies):
    """While the apply waits, the card reads `applying` with reason `finishing_uploads`."""
    import threading

    release, entered = threading.Event(), threading.Event()

    def slow_apply(body, session):
        session.set_reason("finishing_uploads")
        entered.set()
        release.wait(2)
        return "restarting"

    registry._apply = slow_apply
    registry.clients.append(FakeClient(codes=[code()], collects=[answer("payload", payload=payload())]))
    c = admin(client)
    bodies.keep(c.post("/system/pairing", json={"unitName": "Sala 1"}))
    step(registry)
    worker = threading.Thread(target=step, args=(registry, 5))
    worker.start()
    try:
        assert entered.wait(2)
        body = bodies.keep(c.get("/system/pairing")).json()
        assert (body["state"], body["reason"]) == ("applying", "finishing_uploads")
    finally:
        release.set()
        worker.join(2)
    assert bodies.keep(c.get("/system/pairing")).json()["state"] == "restarting"


# ---------------------------------------------------------------------------
# The schedulers' stop, against real threads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module,name", [("app.core.catalogue_backup", "CatalogueBackupScheduler"),
                                         ("app.core.image_upload", "ImageUploadScheduler"),
                                         ("app.core.fisqua_outbox", "FisquaStatusScheduler")])
def test_wait_stopped_keeps_a_live_worker_and_start_adds_none(module, name, monkeypatch):
    import importlib
    import threading

    cls = getattr(importlib.import_module(module), name)
    scheduler = cls()
    release = threading.Event()
    worker = threading.Thread(target=lambda: release.wait(5), name="slow-tick", daemon=True)
    worker.start()
    scheduler._thread = worker
    monkeypatch.setattr(cls, "_loop", lambda self: (_ for _ in ()).throw(AssertionError("second worker")))
    try:
        scheduler.request_stop()
        assert scheduler._stop.is_set()
        assert scheduler.wait_stopped(0.01) is False
        assert scheduler.worker_alive()
        assert scheduler._thread is worker
    finally:
        release.set()
    assert scheduler.wait_stopped(2) is True
    assert not scheduler.worker_alive()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

def test_lifespan_stops_the_registry(monkeypatch):
    import asyncio

    import app.core.db as core_db
    import app.core.storage_ops as storage_ops
    from app import main as app_main
    from app.core import device_id, fisqua_pairing_service, unit_mode
    from capture import service as capture_service

    monkeypatch.setattr(app_main, "init_db", lambda: None)
    monkeypatch.setattr(app_main, "assert_schema_at_head", lambda: None)
    monkeypatch.setattr(core_db, "SessionLocal", lambda: type("S", (), {"close": lambda self: None})())
    monkeypatch.setattr(storage_ops, "reconcile_pending_rename", lambda db: None)
    monkeypatch.setattr(unit_mode, "initialise", lambda: None)
    monkeypatch.setattr(device_id, "initialise", lambda: None)
    monkeypatch.setattr(capture_service, "shutdown_backend", lambda: None)
    stopped = []
    from app.core import restart_latch
    monkeypatch.setattr(fisqua_pairing_service.pairing_registry, "shutdown",
                        lambda *a, **k: stopped.append(restart_latch.shutting_down()))

    async def serve():
        async with app_main.app.router.lifespan_context(app_main.app):
            assert stopped == []

    asyncio.run(serve())
    # The shutting-down flag is set before the registry is stopped.
    assert stopped == [True]
    restart_latch._reset_for_tests()


# ---------------------------------------------------------------------------
# The restart latch, through the API
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def latch():
    """Every test starts and ends with the process's restart latch clear."""
    from app.core import restart_latch

    restart_latch._reset_for_tests()
    yield restart_latch
    restart_latch._reset_for_tests()


@pytest.mark.parametrize("final", ["restart_needed", "settings_overridden", "apply_unverified"])
def test_second_session_refused_after_a_stale_outcome(client, registry, latch, bodies, final):
    def stale_apply(body, session):
        latch.set_stale()
        return final

    registry._apply = stale_apply
    registry.clients.append(FakeClient(codes=[code()], collects=[answer("payload", payload=payload())]))
    c = admin(client)
    bodies.keep(c.post("/system/pairing", json={"unitName": "Sala 1"}))
    step(registry)
    step(registry, 5)
    assert registry.current().state == final
    body = bodies.keep(c.get("/system/pairing")).json()
    assert body["restartRequired"] is True
    assert body["lastOutcome"] == final
    assert body["eligible"] == {"ok": False, "reason": "restart_required"}
    resp = c.post("/system/pairing", json={"unitName": "Sala 2"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "restart_required"


def test_get_reports_the_latch_after_a_reload(client, registry, latch):
    latch.set_stale()
    body = admin(client).get("/system/pairing").json()
    assert body["restartRequired"] is True
    assert body["state"] == "idle"


def test_get_without_the_latch(client, registry, latch):
    body = admin(client).get("/system/pairing").json()
    assert body["restartRequired"] is False
    assert body["lastOutcome"] is None


# ---------------------------------------------------------------------------
# Offsite jobs (free, restore): one rule with cameras
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_activity():
    from app.core import unit_activity

    unit_activity._reset_for_tests()
    yield
    unit_activity._reset_for_tests()


def test_offsite_job_running_refused(client, registry):
    from app.core import unit_activity

    with unit_activity.tracking(unit_activity.OFFSITE_JOB):
        _refused(client, "offsite_job_running")
    assert admin(client).get("/system/pairing").json()["eligible"]["ok"] is True


class FakeOffsite:
    """Running offsite jobs that end `ends_after` seconds into the wait (None: never), on the fake clock."""

    def __init__(self, clock, ends_after=None, running=True):
        self.clock, self.ends_after, self._running = clock, ends_after, running
        self.started = None

    def running(self):
        return self._running

    def wait_idle(self, timeout):
        if self.started is None:
            self.started = self.clock.now
        if self._running and self.ends_after is not None and \
                self.clock.now + timeout >= self.started + self.ends_after:
            self.clock.now = max(self.clock.now, self.started + self.ends_after)
            self._running = False
        elif self._running:
            self.clock.now += timeout
        return not self._running


def test_offsite_job_finishing_during_the_wait(unit, schedulers, monkeypatch):
    from app.core import fisqua_pairing_apply, fisqua_pairing_policy, fisqua_pairing_service

    offsite = FakeOffsite(schedulers["clock"], ends_after=120.0)
    monkeypatch.setattr(fisqua_pairing_service, "_offsite_work", lambda: offsite)
    ran = []
    monkeypatch.setattr(fisqua_pairing_apply, "apply_payload",
                        lambda body, **kw: ran.append(offsite.running()) or "linked")
    monkeypatch.setattr(fisqua_pairing_policy, "restart_after_link", lambda: "restarting")
    session = Progress()
    assert fisqua_pairing_service.apply_link(payload(), session) == "restarting"
    assert ran == [False]
    assert session.reasons == ["finishing_uploads", "finishing_offsite_jobs", "writing_link"]


def test_offsite_job_that_never_finishes_stops_the_apply(unit, schedulers, monkeypatch):
    from app.core import fisqua_pairing_apply, fisqua_pairing_service, unit_activity

    offsite = FakeOffsite(schedulers["clock"], ends_after=None)
    monkeypatch.setattr(fisqua_pairing_service, "_offsite_work", lambda: offsite)
    ran = []
    monkeypatch.setattr(fisqua_pairing_apply, "apply_payload", lambda body, **kw: ran.append(1) or "linked")
    session = Progress()
    assert fisqua_pairing_service.apply_link(payload(), session) == "apply_failed"
    assert session.reasons[-1] == "offsite_jobs_still_running"
    assert ran == []
    assert schedulers["clock"].now >= 600
    assert len([e for e in schedulers["log"] if e[0] == "start"]) == 3
    assert not unit_activity.offsite_jobs_blocked()


def _try_offsite_job():
    from app.core.offsite_free import FreeRefused, offsite_jobs

    try:
        job = offsite_jobs.reserve(1, "restore")
    except FreeRefused as exc:
        return exc
    offsite_jobs.discard(job)
    return None


def test_offsite_job_refused_during_apply_and_allowed_after_nothing_written(unit, schedulers, monkeypatch):
    from app.core import fisqua_pairing_apply, fisqua_pairing_service, unit_activity

    seen = []

    def apply(body, **kw):
        seen.append(_try_offsite_job())
        return "apply_failed"

    monkeypatch.setattr(fisqua_pairing_apply, "apply_payload", apply)
    assert fisqua_pairing_service.apply_link(payload(), Progress()) == "apply_failed"
    refused = seen[0]
    assert refused is not None and refused.status_code == 503
    assert refused.detail["code"] == "backend_restarting"
    assert _try_offsite_job() is None
    assert not unit_activity.in_progress(unit_activity.OFFSITE_JOB)


def test_offsite_job_stays_refused_after_a_link(unit, schedulers, monkeypatch):
    from app.core import fisqua_pairing_apply, fisqua_pairing_policy, fisqua_pairing_service

    monkeypatch.setattr(fisqua_pairing_apply, "apply_payload", lambda body, **kw: "linked")
    monkeypatch.setattr(fisqua_pairing_policy, "restart_after_link", lambda: "restart_needed")
    assert fisqua_pairing_service.apply_link(payload(), Progress()) == "restart_needed"
    assert _try_offsite_job() is not None


def test_offsite_job_refused_during_a_pending_restart():
    from app.core import unit_activity

    assert unit_activity.reserve_restart() is True
    refused = _try_offsite_job()
    assert refused is not None and refused.status_code == 503
    assert refused.detail["code"] == "backend_restarting"


def test_running_offsite_job_prevents_the_restart():
    from app.core import unit_activity

    with unit_activity.tracking(unit_activity.OFFSITE_JOB):
        assert unit_activity.reserve_restart() is False


def test_offsite_job_counted_until_its_thread_ends():
    import threading

    from app.core import unit_activity
    from app.core.offsite_free import offsite_jobs

    release, inside = threading.Event(), threading.Event()

    class Hold:
        def release(self):
            pass

    def work():
        inside.set()
        release.wait(2)
        return {}

    job = offsite_jobs.reserve(7, "restore")
    assert unit_activity.in_progress(unit_activity.OFFSITE_JOB)
    offsite_jobs.run(job, work, Hold())
    assert inside.wait(2)
    assert unit_activity.in_progress(unit_activity.OFFSITE_JOB)
    release.set()
    assert unit_activity.wait_idle(unit_activity.OFFSITE_JOB, 2) is True
    assert job.state == "done"


def test_restore_route_answers_503_while_blocked(client):
    from app.core import unit_activity

    unit_activity.block_offsite_jobs()
    resp = admin(client).post("/collections/1/offsite/restore")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "backend_restarting"
    assert "Retry-After" in resp.headers



@pytest.mark.parametrize("module,name", [("app.core.catalogue_backup", "CatalogueBackupScheduler"),
                                         ("app.core.image_upload", "ImageUploadScheduler"),
                                         ("app.core.fisqua_outbox", "FisquaStatusScheduler")])
def test_stop_keeps_a_live_worker(module, name):
    import importlib
    import threading

    scheduler = getattr(importlib.import_module(module), name)()
    release = threading.Event()
    worker = threading.Thread(target=lambda: release.wait(5), daemon=True)
    worker.start()
    scheduler._thread = worker
    try:
        scheduler.stop(timeout=0.01)
        assert scheduler.worker_alive()
        assert scheduler._thread is worker
        assert scheduler.wait_stopped(0.01) is False
    finally:
        release.set()
    scheduler.stop(timeout=2)
    assert not scheduler.worker_alive()
    assert scheduler._thread is None
