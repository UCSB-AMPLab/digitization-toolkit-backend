"""The unit's time zone (NEH-347): GET and PUT /system/timezone, the reads behind them, and the helper's verb.

GET /system/timezone answers every signed-in role and the testing
account with {zone, zones, canSet}: the zone read without root
(`timedatectl show -p Timezone --value`, then the /etc/localtime link
target under /usr/share/zoneinfo, then null), the names `timedatectl
list-timezones` gives (empty when it cannot be read), and whether the
caller is an admin and the installed helper has `set-timezone`. PUT sets
the zone through `sudo HELPER set-timezone <zone>`, admin
only, and refuses `unknown_zone` (422), `timezone_unavailable` (503) and
`timezone_set_failed` (500); the helper's output reaches the log and
never a response. A change is logged `unit_timezone_changed`, naming the
admin, the zone before and the zone after. GET /system/unit carries the
release as `version`.

timedatectl, sudo and the helper are never run: subprocess.run is
replaced, and the helper is a file in a temporary directory.

    python -m pytest tests/unit/test_system_timezone.py

Version: 0.4.0
"""

import logging
import re
import subprocess
import threading
from pathlib import Path

import pytest

from tests.unit.test_capture_manifest_export import _as

ZONES = ["America/Bogota", "America/Los_Angeles", "Europe/Madrid", "UTC"]
HELPER_SECRET = "timedatectl: Access denied SECRET-HELPER-OUTPUT"
STDOUT_CANARY = "CANARY-HELPER-STDOUT-7f3a"
STDERR_CANARY = "CANARY-HELPER-STDERR-91c4"

WITH_VERB = """#!/bin/bash
case "$verb" in
    reboot)        cmd_reboot "$@" ;;
    set-timezone)  cmd_set_timezone "$@" ;;
esac
"""
WITHOUT_VERB = """#!/bin/bash
case "$verb" in
    reboot)        cmd_reboot "$@" ;;
esac
"""


class Unit:
    """A unit's timedatectl, /etc/localtime and helper, as subprocess.run sees them."""

    def __init__(self, tmp_path: Path):
        self.zone = "America/Bogota"
        self.timedatectl = True      # False: the command is not installed
        self.show_answers = True     # False: `show` exits non-zero
        self.helper_rc = 0
        self.helper_stderr = ""
        self.helper_stdout = ""
        self.helper_timeout = False  # True: the helper raises TimeoutExpired carrying both outputs
        self.calls = []
        self.helper = tmp_path / "system-helper"
        self.localtime = tmp_path / "etc" / "localtime"

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        assert isinstance(argv, list), argv
        assert not kwargs.get("shell"), kwargs
        if argv[0] == "timedatectl":
            if not self.timedatectl:
                raise FileNotFoundError(2, "No such file or directory", "timedatectl")
            if argv[1:] == ["show", "-p", "Timezone", "--value"]:
                if not self.show_answers:
                    return subprocess.CompletedProcess(argv, 1, "", "Failed to connect to bus")
                return subprocess.CompletedProcess(argv, 0, f"{self.zone}\n", "")
            if argv[1:] == ["list-timezones"]:
                return subprocess.CompletedProcess(argv, 0, "\n".join(ZONES) + "\n", "")
        if argv[:3] == ["sudo", str(self.helper), "set-timezone"]:
            if self.helper_timeout:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"), output=self.helper_stdout,
                                                stderr=self.helper_stderr)
            if self.helper_rc == 0:
                self.zone = argv[3]
            return subprocess.CompletedProcess(argv, self.helper_rc, self.helper_stdout, self.helper_stderr)
        raise AssertionError(f"unexpected command {argv}")

    def helper_calls(self):
        return [argv for argv, _ in self.calls if argv[0] == "sudo"]


@pytest.fixture
def unit(monkeypatch, tmp_path):
    from app.core import system_helper, timezone

    u = Unit(tmp_path)
    u.helper.write_text(WITH_VERB)
    monkeypatch.setattr(system_helper, "HELPER", str(u.helper))
    monkeypatch.setattr(system_helper.shutil, "which", lambda name: "/usr/bin/sudo" if name == "sudo" else None)
    monkeypatch.setattr(system_helper.subprocess, "run", u.run)
    monkeypatch.setattr(timezone.subprocess, "run", u.run)
    monkeypatch.setattr(timezone, "LOCALTIME", str(u.localtime))
    return u


def _zoneinfo_link(u: Unit, monkeypatch, target: str):
    """/etc/localtime as a link to `target`."""
    u.localtime.parent.mkdir(parents=True, exist_ok=True)
    u.localtime.symlink_to(target)


# ---------------------------------------------------------------------------
# GET /system/timezone
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("role, can_set", [("admin", True), ("operator", False), ("reviewer", False),
                                           ("testing", False)])
def test_every_role_reads_the_zone_and_only_an_admin_can_set_it(client, unit, role, can_set):
    resp = _as(client, role).get("/system/timezone")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"zone": "America/Bogota", "zones": ZONES, "canSet": can_set}


@pytest.mark.unit
def test_get_needs_a_session(client, unit):
    assert client.get("/system/timezone").status_code == 401


@pytest.mark.unit
def test_an_admin_cannot_set_it_when_the_helper_lacks_the_verb(client, unit):
    unit.helper.write_text(WITHOUT_VERB)
    assert _as(client, "admin").get("/system/timezone").json()["canSet"] is False


@pytest.mark.unit
def test_an_admin_cannot_set_it_when_the_helper_cannot_be_read(client, unit):
    unit.helper.unlink()
    assert _as(client, "admin").get("/system/timezone").json()["canSet"] is False


@pytest.mark.unit
def test_the_reads_run_without_root_without_a_shell_with_stdin_closed_and_bounded(client, unit):
    _as(client, "reviewer").get("/system/timezone")
    assert unit.calls
    for argv, kwargs in unit.calls:
        assert argv[0] == "timedatectl"
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["timeout"] and kwargs["timeout"] > 0
        assert not kwargs.get("shell")


@pytest.mark.unit
def test_the_zone_falls_back_to_the_localtime_link(client, unit, monkeypatch):
    unit.show_answers = False
    _zoneinfo_link(unit, monkeypatch, "/usr/share/zoneinfo/Europe/Madrid")
    assert _as(client, "reviewer").get("/system/timezone").json()["zone"] == "Europe/Madrid"


@pytest.mark.unit
def test_a_relative_localtime_link_is_read_against_etc(unit, monkeypatch):
    from app.core import timezone
    unit.show_answers = False
    monkeypatch.setattr(timezone, "LOCALTIME", "/etc/localtime")
    monkeypatch.setattr(timezone.os, "readlink", lambda p: "../usr/share/zoneinfo/America/Bogota")
    assert timezone.current_zone() == "America/Bogota"


@pytest.mark.unit
def test_timedatectl_absent_falls_back_and_lists_no_zones(client, unit, monkeypatch):
    unit.timedatectl = False
    _zoneinfo_link(unit, monkeypatch, "/usr/share/zoneinfo/America/Los_Angeles")
    body = _as(client, "admin").get("/system/timezone").json()
    assert body == {"zone": "America/Los_Angeles", "zones": [], "canSet": True}


@pytest.mark.unit
def test_no_zone_at_all_is_null(client, unit):
    unit.timedatectl = False
    assert _as(client, "reviewer").get("/system/timezone").json() == {"zone": None, "zones": [],
                                                                      "canSet": False}


@pytest.mark.unit
def test_a_link_outside_zoneinfo_is_null(client, unit, monkeypatch):
    unit.show_answers = False
    _zoneinfo_link(unit, monkeypatch, "/tmp/somewhere/else")
    assert _as(client, "reviewer").get("/system/timezone").json()["zone"] is None


# ---------------------------------------------------------------------------
# PUT /system/timezone
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_admin_sets_the_zone_and_the_answer_is_read_again(client, unit):
    resp = _as(client, "admin", "boss").put("/system/timezone", json={"zone": "Europe/Madrid"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"zone": "Europe/Madrid"}
    assert unit.helper_calls() == [["sudo", str(unit.helper), "set-timezone", "Europe/Madrid"]]
    # The zone is read after the set, not echoed from the request.
    show_calls = [argv for argv, _ in unit.calls if argv[1:2] == ["show"]]
    assert len(show_calls) == 2


@pytest.mark.unit
def test_the_answer_is_what_the_unit_reports_after_the_set(client, unit, monkeypatch):
    original = unit.run

    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[0] == "sudo":
            unit.zone = "UTC"
        return result

    from app.core import system_helper, timezone
    monkeypatch.setattr(system_helper.subprocess, "run", run)
    monkeypatch.setattr(timezone.subprocess, "run", run)
    assert _as(client, "admin").put("/system/timezone", json={"zone": "Europe/Madrid"}).json() == {"zone": "UTC"}


@pytest.mark.unit
def test_a_change_is_logged_with_the_admin_from_and_to(client, db_session, unit):
    from app.models.system_log import SystemLog
    _as(client, "admin", "boss").put("/system/timezone", json={"zone": "Europe/Madrid"})
    rows = db_session.query(SystemLog).filter(SystemLog.action == "unit_timezone_changed").all()
    assert len(rows) == 1
    row = rows[0]
    assert row.actor == "boss"
    assert row.category == "system"
    assert row.level == "INFO"
    assert row.detail == "America/Bogota -> Europe/Madrid"


@pytest.mark.unit
@pytest.mark.parametrize("role", ["operator", "reviewer", "testing"])
def test_only_an_admin_sets_the_zone(client, unit, role):
    resp = _as(client, role).put("/system/timezone", json={"zone": "Europe/Madrid"})
    assert resp.status_code == 403
    assert unit.helper_calls() == []


@pytest.mark.unit
def test_put_needs_a_session(client, unit):
    assert client.put("/system/timezone", json={"zone": "Europe/Madrid"}).status_code == 401
    assert unit.helper_calls() == []


@pytest.mark.unit
@pytest.mark.parametrize("zone", ["Mars/Olympus_Mons", "", "america/bogota", "../../etc/passwd",
                                  "Europe/Madrid; reboot"])
def test_a_zone_not_listed_now_is_refused(client, db_session, unit, zone):
    from app.models.system_log import SystemLog
    resp = _as(client, "admin").put("/system/timezone", json={"zone": zone})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "unknown_zone"
    assert unit.helper_calls() == []
    assert db_session.query(SystemLog).filter(SystemLog.action == "unit_timezone_changed").count() == 0


@pytest.mark.unit
def test_an_unknown_zone_is_refused_first_when_the_helper_also_lacks_the_verb(client, unit):
    unit.helper.write_text(WITHOUT_VERB)
    resp = _as(client, "admin").put("/system/timezone", json={"zone": "Mars/Olympus_Mons"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "unknown_zone"
    assert unit.helper_calls() == []


@pytest.mark.unit
def test_every_zone_is_unknown_when_the_list_cannot_be_read(client, unit):
    unit.timedatectl = False
    resp = _as(client, "admin").put("/system/timezone", json={"zone": "Europe/Madrid"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "unknown_zone"
    assert unit.helper_calls() == []


@pytest.mark.unit
@pytest.mark.parametrize("body", [{}, {"zone": 5}, {"zone": "UTC", "other": 1}])
def test_a_malformed_body_is_refused(client, unit, body):
    assert _as(client, "admin").put("/system/timezone", json=body).status_code == 422
    assert unit.helper_calls() == []


@pytest.mark.unit
@pytest.mark.parametrize("lacking", ["no_verb", "unreadable", "no_sudo"])
def test_a_helper_without_the_verb_is_timezone_unavailable(client, unit, monkeypatch, lacking):
    from app.core import system_helper
    if lacking == "no_verb":
        unit.helper.write_text(WITHOUT_VERB)
    elif lacking == "unreadable":
        unit.helper.unlink()
    else:
        monkeypatch.setattr(system_helper.shutil, "which", lambda name: None)
    resp = _as(client, "admin").put("/system/timezone", json={"zone": "Europe/Madrid"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "timezone_unavailable"
    assert unit.helper_calls() == []


@pytest.mark.unit
def test_a_helper_that_fails_is_timezone_set_failed_and_its_output_goes_to_the_log_only(
        client, db_session, unit, caplog):
    from app.models.system_log import SystemLog
    unit.helper_rc = 1
    unit.helper_stderr = HELPER_SECRET
    with caplog.at_level(logging.ERROR):
        resp = _as(client, "admin").put("/system/timezone", json={"zone": "Europe/Madrid"})
    assert resp.status_code == 500
    assert resp.json()["detail"]["code"] == "timezone_set_failed"
    assert "SECRET-HELPER-OUTPUT" not in resp.text
    assert "SECRET-HELPER-OUTPUT" in caplog.text
    assert "system-helper set-timezone failed" in caplog.text
    assert db_session.query(SystemLog).filter(SystemLog.action == "unit_timezone_changed").count() == 0


@pytest.mark.unit
def test_no_response_carries_helper_output(client, unit):
    unit.helper_stderr = HELPER_SECRET
    for role in ("admin", "operator", "reviewer", "testing"):
        assert "SECRET" not in _as(client, role).get("/system/timezone").text
    assert "SECRET" not in _as(client, "admin").put("/system/timezone", json={"zone": "UTC"}).text
    unit.helper_rc = 1
    assert "SECRET" not in _as(client, "admin").put("/system/timezone", json={"zone": "UTC"}).text


@pytest.mark.unit
@pytest.mark.parametrize("outcome", ["success", "failure", "timeout"])
def test_neither_helper_stream_reaches_a_response(client, unit, caplog, outcome):
    unit.helper_stdout = STDOUT_CANARY
    unit.helper_stderr = STDERR_CANARY
    unit.helper_rc = 1 if outcome == "failure" else 0
    unit.helper_timeout = outcome == "timeout"
    with caplog.at_level(logging.INFO):
        resp = _as(client, "admin").put("/system/timezone", json={"zone": "UTC"})
    if outcome == "success":
        assert resp.status_code == 200
        assert resp.json() == {"zone": "UTC"}
    else:
        assert resp.status_code == 500
        assert resp.json()["detail"]["code"] == "timezone_set_failed"
    for role in ("admin", "operator", "reviewer", "testing"):
        get = _as(client, role).get("/system/timezone")
        assert STDOUT_CANARY not in get.text and STDERR_CANARY not in get.text
    assert STDOUT_CANARY not in resp.text
    assert STDERR_CANARY not in resp.text
    if outcome == "timeout":
        assert "TimeoutExpired" in caplog.text


@pytest.mark.unit
def test_concurrent_setters_each_log_their_own_change(unit, monkeypatch):
    """A sets Madrid and is held after the set; B, started meanwhile, sets UTC. Each entry is its own change."""
    import app.main  # noqa: F401  (every model mapped, so User can be built outside a request)
    import app.api.system as system
    from app.core import audit, system_helper, timezone
    from app.models.user import User

    entries = []
    monkeypatch.setattr(audit, "log_event", lambda db, **kw: entries.append((kw["actor"], kw["detail"])))
    a_set = threading.Event()
    release_a = threading.Event()
    original = unit.run

    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[0] == "sudo" and argv[3] == "Europe/Madrid":
            a_set.set()
            assert release_a.wait(10)
        return result

    monkeypatch.setattr(system_helper.subprocess, "run", run)
    monkeypatch.setattr(timezone.subprocess, "run", run)
    answers = {}

    def setter(name, zone):
        user = User(username=name, email=f"{name}@example.com", hashed_password="x", role="admin", is_active=True)
        answers[name] = system.put_timezone(system.TimezoneUpdate(zone=zone), current_user=user, db=None)

    ta = threading.Thread(target=setter, args=("a", "Europe/Madrid"))
    tb = threading.Thread(target=setter, args=("b", "UTC"))
    ta.start()
    assert a_set.wait(10)
    tb.start()
    tb.join(0.5)          # B finishes here only when nothing holds it behind A
    release_a.set()
    ta.join(10)
    tb.join(10)
    assert not ta.is_alive() and not tb.is_alive()
    assert entries == [("a", "America/Bogota -> Europe/Madrid"), ("b", "Europe/Madrid -> UTC")]
    assert answers == {"a": {"zone": "Europe/Madrid"}, "b": {"zone": "UTC"}}


@pytest.mark.unit
def test_a_second_setter_reads_nothing_while_the_first_holds_the_lock(unit, monkeypatch):
    """A is held inside the helper call before the zone changes; B, started meanwhile, reads no zone until A is done."""
    import app.main  # noqa: F401  (every model mapped, so User can be built outside a request)
    import app.api.system as system
    from app.core import audit, system_helper, timezone
    from app.models.user import User

    entries = []
    monkeypatch.setattr(audit, "log_event", lambda db, **kw: entries.append((kw["actor"], kw["detail"])))
    in_helper = threading.Event()
    release_a = threading.Event()
    zone_reads = []   # the thread name of each `timedatectl show`, in order
    original = unit.run

    def run(argv, **kwargs):
        if argv[0] == "timedatectl" and argv[1] == "show":
            zone_reads.append(threading.current_thread().name)
        if argv[0] == "sudo" and argv[3] == "Europe/Madrid":
            in_helper.set()
            assert release_a.wait(10)
        return original(argv, **kwargs)

    monkeypatch.setattr(system_helper.subprocess, "run", run)
    monkeypatch.setattr(timezone.subprocess, "run", run)

    def setter(name, zone):
        user = User(username=name, email=f"{name}@example.com", hashed_password="x", role="admin", is_active=True)
        system.put_timezone(system.TimezoneUpdate(zone=zone), current_user=user, db=None)

    ta = threading.Thread(target=setter, args=("a", "Europe/Madrid"), name="setter-a")
    tb = threading.Thread(target=setter, args=("b", "UTC"), name="setter-b")
    ta.start()
    assert in_helper.wait(10)
    tb.start()
    tb.join(0.5)
    assert "setter-b" not in zone_reads, zone_reads
    release_a.set()
    ta.join(10)
    tb.join(10)
    assert not ta.is_alive() and not tb.is_alive()
    assert zone_reads == ["setter-a", "setter-a", "setter-b", "setter-b"]
    assert entries == [("a", "America/Bogota -> Europe/Madrid"), ("b", "Europe/Madrid -> UTC")]


@pytest.mark.unit
def test_the_helper_runs_without_a_shell_with_stdin_closed_and_bounded(client, unit):
    _as(client, "admin").put("/system/timezone", json={"zone": "America/Los_Angeles"})
    (argv, kwargs), = [c for c in unit.calls if c[0][0] == "sudo"]
    assert argv == ["sudo", str(unit.helper), "set-timezone", "America/Los_Angeles"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] and kwargs["timeout"] > 0
    assert not kwargs.get("shell")


# ---------------------------------------------------------------------------
# The helper: its name, supports(), and run() with an argument
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_helper_and_its_verb():
    from app.core import system_helper
    assert system_helper.HELPER == "/usr/local/bin/dtk-system-helper"
    assert system_helper.SET_TIMEZONE == "set-timezone"


@pytest.mark.unit
def test_the_helper_path_is_a_string_only_in_system_helper():
    """Every caller reads system_helper.HELPER; no other module holds the path as a string literal."""
    literal = re.compile(r"""["']/usr/local/bin/[^"']*helper""")
    app_dir = Path(__file__).resolve().parents[2] / "app"
    hits = [str(p.relative_to(app_dir)) for p in app_dir.rglob("*.py")
            if literal.search(p.read_text(encoding="utf-8"))]
    assert hits == ["core/system_helper.py"]


@pytest.mark.unit
def test_supports_reads_the_case_label_at_the_new_path(monkeypatch, tmp_path):
    from app.core import system_helper
    path = tmp_path / "system-helper"
    monkeypatch.setattr(system_helper, "HELPER", str(path))
    monkeypatch.setattr(system_helper.shutil, "which", lambda name: "/usr/bin/sudo")
    assert system_helper.supports("set-timezone") is False   # cannot be read
    path.write_text(WITHOUT_VERB)
    assert system_helper.supports("set-timezone") is False   # no case label
    path.write_text(WITH_VERB)
    assert system_helper.supports("set-timezone") is True
    assert system_helper.supports("reboot") is True


@pytest.mark.unit
def test_run_passes_an_argument_as_one_argv_element_and_never_through_a_shell(monkeypatch):
    from app.core import system_helper
    seen = []
    monkeypatch.setattr(system_helper.subprocess, "run",
                        lambda argv, **kw: seen.append((argv, kw)) or subprocess.CompletedProcess(argv, 0, "", ""))
    zone = "Europe/Madrid; reboot $(id) `id` 'x y'"
    assert system_helper.run("set-timezone", zone) is True
    (argv, kwargs), = seen
    assert argv == ["sudo", system_helper.HELPER, "set-timezone", zone]
    assert isinstance(argv, list)
    assert not kwargs.get("shell")
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] == system_helper.TIMEOUT_SECONDS


@pytest.mark.unit
@pytest.mark.parametrize("helper", ["/usr/local/bin/dtk-system-helper", "/opt/elsewhere/other-helper"])
def test_run_logs_failures_naming_the_helper_from_its_path(monkeypatch, caplog, helper):
    from app.core import system_helper
    monkeypatch.setattr(system_helper, "HELPER", helper)
    monkeypatch.setattr(system_helper.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "refused"))
    with caplog.at_level(logging.ERROR):
        assert system_helper.run("set-timezone", "UTC") is False
    assert f"{Path(helper).name} set-timezone failed (rc=1): refused" in caplog.text


# ---------------------------------------------------------------------------
# The release on GET /system/unit
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("role", ["admin", "operator", "reviewer", "testing"])
def test_get_unit_carries_the_release(client, monkeypatch, role):
    from app.core import config
    monkeypatch.setattr(config.settings, "app_version", "0.4.0")
    body = _as(client, role).get("/system/unit").json()
    assert body["version"] == "0.4.0"


@pytest.mark.unit
def test_the_unit_puts_carry_the_release(client, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config.settings, "app_version", "0.4.0")
    assert _as(client, "admin").put("/system/unit", json={"projectsEnabled": True}).json()["version"] == "0.4.0"
    resp = _as(client, "operator").put("/system/unit/capture-key", json={"capture_key": None})
    assert resp.json()["version"] == "0.4.0"
