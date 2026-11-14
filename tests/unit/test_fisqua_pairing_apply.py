"""Applying a collected pairing payload (contract 1.11 §12.2): provision-rclone.sh, run as the backend's user, then checked.

A stub script in a temporary directory stands in for
scripts/provision-rclone.sh: it records its argv, its environment, the key
file's mode and content and its directory's mode, and then writes the
settings, writes nothing, or exits non-zero, as told.

    python -m pytest tests/unit/test_fisqua_pairing_apply.py

Version: 0.3.3
"""

import json
import logging
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from tests.unit.test_fisqua_pairing_http import READ_APP_KEY, TOKEN, WRITE_APP_KEY, payload

STUB = r"""#!/bin/bash
here="__HERE__"
printf '%s\n' "$@" > "$here/argv"
env > "$here/env"
f="$DTK_OFFSITE_KEY_FILE"
if [ -n "$f" ] && [ -r "$f" ]; then
    stat -f %Lp "$f" 2>/dev/null > "$here/key_mode" || stat -c %a "$f" > "$here/key_mode"
    d="$(dirname "$f")"
    stat -f %Lp "$d" 2>/dev/null > "$here/dir_mode" || stat -c %a "$d" > "$here/dir_mode"
    cat "$f" > "$here/key_content"
    echo "$f" > "$here/key_path"
fi
echo "  wrote [b2unit] with key id 005writekeyid"
echo "refused: key 005writekeyid" >&2
mode="$(cat "$here/mode")"
kv() { sed -n "s/^$1=//p" "$f" | tail -1; }
tenant=""; recipient=""; fenv=""; cfg=""
while [ $# -gt 0 ]; do
    case "$1" in
        --tenant) tenant="$2"; shift ;;
        --dump-recipient) recipient="$2"; shift ;;
        --fisqua-env) fenv="$2"; shift ;;
        --config-path) cfg="$2"; shift ;;
        __FLAG__) : ;;
    esac
    shift
done
bucket="$(kv BUCKET)"
write_all() {
    {
        echo "OFFSITE_TENANT=$tenant"
        echo "FISQUA_ENV=$fenv"
        echo "OFFSITE_REMOTE=b2unit:$bucket"
        echo "OFFSITE_READ_REMOTE=b2read:$bucket"
        echo "OFFSITE_DUMP_RECIPIENT=$recipient"
        echo "FISQUA_TOKEN=$(kv FISQUA_TOKEN)"
        echo "RCLONE_CONFIG=$cfg"
    } > "$here/written.env"
    [ -f "$cfg" ] && cp "$cfg" "$cfg.before"
    printf '[b2unit]\ntype = b2\naccount = %s\nkey = %s\n' "$(kv KEY_ID)" "$(kv APP_KEY)" > "$cfg"
}
spin() { while :; do sleep 0.02; done; }
case "$mode" in
    fail) exit 3 ;;
    nothing) exit 0 ;;
    write_fail) write_all; exit 4 ;;
    hang_ignore_term) trap '' TERM; spin ;;
    hang_restore) write_all; trap 'rm -f "$here/written.env"; if [ -f "$cfg.before" ]; then mv "$cfg.before" "$cfg"; else rm -f "$cfg"; fi; exit 143' TERM; spin ;;
    write_hang) write_all; spin ;;
esac
write_all
exit 0
"""


class Stub:
    def __init__(self, root: Path, flag: bool = True, mode: str = "write"):
        self.dir = root / "superproject" / "scripts"
        self.dir.mkdir(parents=True)
        self.path = self.dir / "provision-rclone.sh"
        text = STUB.replace("__HERE__", str(self.dir))
        text = text.replace("__FLAG__)", "--replace-credential)" if flag else "--no-such-flag)")
        self.path.write_text(text)
        self.path.chmod(0o755)
        self.set(mode)

    def set(self, mode):
        (self.dir / "mode").write_text(mode)

    def read(self, name):
        p = self.dir / name
        return p.read_text() if p.exists() else None

    @property
    def ran(self):
        return (self.dir / "argv").exists()

    def argv(self):
        return self.read("argv").splitlines()

    def env(self):
        return dict(line.split("=", 1) for line in self.read("env").splitlines() if "=" in line)

    def settings(self):
        """What a fresh Settings() would read after the run: the stub's written.env, or nothing."""
        values = {"OFFSITE_TENANT": "", "FISQUA_ENV": "", "OFFSITE_REMOTE": "", "OFFSITE_READ_REMOTE": "",
                  "OFFSITE_DUMP_RECIPIENT": "", "FISQUA_TOKEN": "", "RCLONE_CONFIG": ""}
        written = self.read("written.env")
        if written:
            values.update(dict(line.split("=", 1) for line in written.splitlines()))
        values["FISQUA_TOKEN"] = SecretStr(values["FISQUA_TOKEN"])
        return SimpleNamespace(**values)


@pytest.fixture
def unit(monkeypatch, tmp_path):
    from app.core import config

    monkeypatch.setattr(config.settings, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config.settings, "RCLONE_CONFIG", str(tmp_path / "data" / "rclone.conf"))
    (tmp_path / "data").mkdir()
    (tmp_path / "keys").mkdir()
    return tmp_path


@pytest.fixture
def stub(unit):
    return Stub(unit)


def apply(stub, body=None, **kwargs):
    from app.core import fisqua_pairing_apply

    kwargs.setdefault("settings_factory", stub.settings)
    kwargs.setdefault("key_root", stub.dir.parent.parent / "keys")
    kwargs.setdefault("env_file", stub.dir.parent / ".env")
    kwargs.setdefault("offsite_env", stub.dir / "written.env")
    kwargs.setdefault("time_limit", 10.0)
    return fisqua_pairing_apply.apply_payload(body or payload(), script=stub.path, **kwargs)


def test_verified_link_is_linked(stub):
    assert apply(stub) == "linked"


def test_key_file_0600_in_private_dir_and_removed(stub):
    assert apply(stub) == "linked"
    assert stub.read("key_mode").strip() == "600"
    assert stub.read("dir_mode").strip() == "700"
    key_path = Path(stub.read("key_path").strip())
    assert key_path.parent.parent == stub.dir.parent.parent / "keys"
    assert not key_path.exists() and not key_path.parent.exists()
    lines = dict(line.split("=", 1) for line in stub.read("key_content").splitlines())
    assert lines == {"KEY_ID": "005writekeyid", "APP_KEY": WRITE_APP_KEY, "BUCKET": "captua-masters",
                     "READ_KEY_ID": "005readkeyid", "READ_APP_KEY": READ_APP_KEY, "FISQUA_TOKEN": TOKEN}


def test_key_file_removed_when_the_run_fails(stub):
    stub.set("fail")
    assert apply(stub) == "apply_failed"
    assert not Path(stub.read("key_path").strip()).parent.exists()


def test_key_file_root_is_dev_shm_when_it_exists(monkeypatch, tmp_path):
    import tempfile

    from app.core import fisqua_pairing_apply as fpa

    shm = tmp_path / "shm"
    shm.mkdir()
    monkeypatch.setattr(fpa, "DEV_SHM", shm)
    assert fpa.key_file_root() == shm
    monkeypatch.setattr(fpa, "DEV_SHM", tmp_path / "absent")
    assert fpa.key_file_root() == Path(tempfile.gettempdir())


def test_values_with_newlines_refused(stub):
    body = payload(tenant="ahr\nFISQUA_TOKEN=" + "0" * 64)
    assert apply(stub, body) == "bad_payload"
    body = payload(writeKey={"keyId": "005", "applicationKey": "k\nFISQUA_TOKEN=x"})
    assert apply(stub, body) == "bad_payload"
    assert not stub.ran


def test_env_is_scrubbed(stub, monkeypatch):
    monkeypatch.setenv("DTK_B2_KEY_ID", "inherited")
    monkeypatch.setenv("RCLONE_CONFIG_B2UNIT_TYPE", "b2")
    monkeypatch.setenv("CAPTUA_FISQUA_TOKEN", "inherited")
    monkeypatch.setenv("SHELLOPTS", "xtrace")
    assert apply(stub) == "linked"
    env = stub.env()
    # bash itself adds PWD, SHLVL and _.
    assert set(env) - {"PWD", "SHLVL", "_"} == {"PATH", "HOME", "LANG", "DTK_OFFSITE_KEY_FILE"}
    assert env["PATH"] == os.environ["PATH"]
    assert env["LANG"] == "C.UTF-8"


def test_only_public_values_on_the_command_line(stub):
    assert apply(stub) == "linked"
    argv = stub.argv()
    assert argv == ["--tenant", "ahr", "--dump-recipient", payload()["ageRecipient"], "--fisqua-env", "production",
                    "--config-path", str(stub.dir.parent.parent / "data" / "rclone.conf")]
    for secret in (TOKEN, WRITE_APP_KEY, READ_APP_KEY, "005writekeyid", "005readkeyid"):
        assert all(secret not in a for a in argv)


def test_change_tenant_never_passed(stub, unit):
    (unit / "data" / "rclone.conf").write_text("[b2unit]\n")
    assert apply(stub, payload(tenant="other")) in ("linked",)
    assert "--change-tenant" not in stub.argv()


def test_replace_credential_only_when_a_credential_exists(stub):
    assert apply(stub) == "linked"
    assert "--replace-credential" not in stub.argv()
    # The first link wrote the config, so the unit now has a credential.
    # A second collection always carries a new token.
    assert apply(stub, payload(token="cd" * 32)) == "linked"
    assert stub.argv()[-1] == "--replace-credential"


def test_exit_zero_with_nothing_new_is_unverified(stub):
    """The same payload twice: the second run changes nothing, and exit 0 alone proves nothing."""
    assert apply(stub) == "linked"
    assert apply(stub) == "apply_unverified"


def test_script_without_the_flag_is_too_old_and_not_run(unit):
    old = Stub(unit, flag=False)
    (unit / "data" / "rclone.conf").write_text("[b2unit]\n")
    assert apply(old) == "script_too_old"
    assert not old.ran


def test_script_without_the_flag_runs_on_a_unit_with_no_credential(unit):
    old = Stub(unit, flag=False)
    assert apply(old) == "linked"


def test_missing_script(stub):
    from app.core import fisqua_pairing_apply

    assert fisqua_pairing_apply.apply_payload(payload(), script=stub.dir / "absent.sh",
                                              settings_factory=stub.settings) == "script_missing"


def test_exit_nonzero_is_apply_failed_and_output_not_kept(stub, caplog):
    caplog.set_level(logging.DEBUG)
    stub.set("fail")
    assert apply(stub) == "apply_failed"
    assert "005writekeyid" not in caplog.text
    assert "exit code 3" in caplog.text


# ---------------------------------------------------------------------------
# After the script: evidence, never assumption
# ---------------------------------------------------------------------------

def test_refusal_that_changed_nothing_is_apply_failed(stub):
    stub.set("fail")
    assert apply(stub) == "apply_failed"


def test_refusal_after_writing_is_unverified(stub):
    stub.set("write_fail")
    assert apply(stub) == "apply_unverified"


def test_time_out_restored_on_sigterm_is_apply_failed(stub, unit):
    (unit / "data" / "rclone.conf").write_text("[b2unit]\naccount = 005old\n")
    stub.set("hang_restore")
    assert apply(stub, time_limit=0.5, grace=5.0) == "apply_failed"
    assert (unit / "data" / "rclone.conf").read_text() == "[b2unit]\naccount = 005old\n"


def test_time_out_ignoring_sigterm_is_killed_and_unverified(stub):
    stub.set("hang_ignore_term")
    assert apply(stub, time_limit=0.3, grace=0.3) == "apply_unverified"


def test_write_then_hang_is_unverified(stub):
    stub.set("write_hang")
    assert apply(stub, time_limit=0.5, grace=2.0) == "apply_unverified"


def test_no_script_process_left_behind(stub):
    stub.set("hang_ignore_term")
    apply(stub, time_limit=0.3, grace=0.3)
    found = subprocess.run(["pgrep", "-f", str(stub.path)], capture_output=True)
    assert found.returncode == 1, found.stdout


# ---------------------------------------------------------------------------
# The rclone config the backend uses
# ---------------------------------------------------------------------------

def test_config_path_is_the_backends_own(stub, unit, monkeypatch):
    from app.core import config

    custom = unit / "elsewhere" / "rclone.conf"
    custom.parent.mkdir()
    monkeypatch.setattr(config.settings, "RCLONE_CONFIG", str(custom))
    assert apply(stub) == "linked"
    assert stub.argv()[-2:] == ["--config-path", str(custom)]
    assert "account = 005writekeyid" in custom.read_text()


def test_config_path_defaults_to_the_data_directory(stub, unit, monkeypatch):
    from app.core import config, fisqua_pairing_apply

    monkeypatch.setattr(config.settings, "RCLONE_CONFIG", "")
    assert fisqua_pairing_apply.rclone_config_path() == unit / "data" / "rclone.conf"
    assert apply(stub) == "linked"
    assert stub.argv()[-2:] == ["--config-path", str(unit / "data" / "rclone.conf")]


def test_settings_naming_another_config_is_unverified(stub):
    def other():
        s = stub.settings()
        s.RCLONE_CONFIG = "/somewhere/else.conf"
        return s

    assert apply(stub, settings_factory=other) == "apply_unverified"


def test_config_without_the_new_key_id_is_unverified(stub, unit, caplog):
    caplog.set_level(logging.DEBUG)
    calls = []

    def replaced_after():
        calls.append(1)
        if len(calls) > 1:  # after the run: another key in the file
            (unit / "data" / "rclone.conf").write_text("[b2unit]\ntype = b2\naccount = 005other\nkey = x\n")
        return stub.settings()

    assert apply(stub, settings_factory=replaced_after) == "apply_unverified"
    assert "005writekeyid" not in caplog.text and "005other" not in caplog.text


def test_exit_zero_without_write_is_unverified(stub):
    stub.set("nothing")
    assert apply(stub) == "apply_unverified"


def test_captua_override_detected(stub):
    (stub.dir.parent / ".env").write_text("CAPTUA_OFFSITE_TENANT=elsewhere\n")

    def overridden():
        s = stub.settings()
        s.OFFSITE_TENANT = "elsewhere"
        return s

    assert apply(stub, settings_factory=overridden) == "settings_overridden"


def test_process_environment_override_detected(stub, monkeypatch):
    monkeypatch.setenv("DTK_FISQUA_ENV", "staging")

    def overridden():
        s = stub.settings()
        s.FISQUA_ENV = "staging"
        return s

    assert apply(stub, settings_factory=overridden) == "settings_overridden"


def test_token_mismatch_is_unverified(stub):
    def other():
        s = stub.settings()
        s.FISQUA_TOKEN = SecretStr("cd" * 32)
        return s

    assert apply(stub, settings_factory=other) == "apply_unverified"


def test_never_run_with_sudo(stub):
    seen = []

    def popen(argv, **kwargs):
        seen.append(argv)
        return subprocess.Popen(argv, **kwargs)

    assert apply(stub, popen=popen) == "linked"
    assert "sudo" not in seen[0]
    assert seen[0][1:] == stub.argv()


def test_script_location_is_beside_the_backend():
    from app.core import fisqua_pairing_apply

    backend = Path("/home/pi/dtk/backend")
    assert fisqua_pairing_apply.provision_script(backend) == Path("/home/pi/dtk/scripts/provision-rclone.sh")


# ---------------------------------------------------------------------------
# pairing-result.json
# ---------------------------------------------------------------------------

def test_result_file_written_without_secrets(unit):
    from app.core import fisqua_pairing_apply

    fisqua_pairing_apply.write_result("ahr", "production", "admin1")
    path = unit / "data" / "pairing-result.json"
    body = json.loads(path.read_text())
    assert set(body) == {"tenant", "environment", "at", "by"}
    assert (body["tenant"], body["environment"], body["by"]) == ("ahr", "production", "admin1")
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert fisqua_pairing_apply.read_result() == body
    assert [p.name for p in (unit / "data").iterdir()] == ["pairing-result.json"]


def test_unreadable_result_reads_as_none(unit):
    from app.core import fisqua_pairing_apply

    assert fisqua_pairing_apply.read_result() is None
    (unit / "data" / "pairing-result.json").write_text("{not json")
    assert fisqua_pairing_apply.read_result() is None


# ---------------------------------------------------------------------------
# Fail closed after the script has started
# ---------------------------------------------------------------------------

class Boom(Exception):
    pass


@pytest.fixture
def wired(stub, unit, monkeypatch):
    """apply_link over the stub script, with fake schedulers whose workers have exited."""
    import functools

    from app.core import unit_activity, fisqua_pairing_apply, fisqua_pairing_policy, fisqua_pairing_service
    from tests.unit.test_system_pairing import FakeClock, Scheduler

    original = fisqua_pairing_apply.apply_payload
    monkeypatch.setattr(fisqua_pairing_apply, "apply_payload", functools.partial(
        original, script=stub.path, settings_factory=stub.settings, env_file=stub.dir.parent / ".env",
        offsite_env=stub.dir / "written.env", key_root=unit / "keys", time_limit=10.0))
    log, clock = [], FakeClock()
    made = [Scheduler(name, log, clock) for name in ("status", "images", "catalogue")]
    monkeypatch.setattr(fisqua_pairing_service, "_schedulers", lambda: made)
    monkeypatch.setattr(fisqua_pairing_service, "_clock", clock)
    monkeypatch.setattr(fisqua_pairing_policy, "restart_after_link", lambda: "restarting")
    unit_activity.release_restart()
    yield SimpleNamespace(log=log, stub=stub, unit=unit)
    unit_activity.release_restart()


def _link(session=None):
    from app.core import fisqua_pairing_service
    from tests.unit.test_system_pairing import Progress

    return fisqua_pairing_service.apply_link(payload(), session or Progress())


def _starts(log):
    return [entry for entry in log if entry[0] == "start"]


def _failing_after(monkeypatch, target, name, calls_before=0):
    """Make target.name raise from its (calls_before + 1)th call on."""
    real = getattr(target, name)
    count = [0]

    def wrapper(*args, **kwargs):
        count[0] += 1
        if count[0] > calls_before:
            raise Boom(name)
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, wrapper)


def _point_after_launch(point, monkeypatch, wired):
    """Inject one failure at a call made after the script process has started."""
    from app.core import fisqua_pairing_apply as fpa

    if point == "wait":
        real_popen = subprocess.Popen

        def popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            process.wait()

            def broken_wait(timeout=None):
                raise Boom("wait")
            process.wait = broken_wait
            return process
        real = fpa.apply_payload
        monkeypatch.setattr(fpa, "apply_payload", lambda body, **kw: real(body, popen=popen, **kw))
    elif point == "settings_after":
        calls = [0]

        def factory():
            calls[0] += 1
            if calls[0] > 1:
                raise Boom("settings")
            return wired.stub.settings()
        real = fpa.apply_payload
        monkeypatch.setattr(fpa, "apply_payload", lambda body, **kw: real(body, **{**kw, "settings_factory": factory}))
    elif point == "digest_after":
        _failing_after(monkeypatch, fpa, "_digest", calls_before=2)
    elif point == "verify":
        _failing_after(monkeypatch, fpa, "verify")
    elif point == "key_id":
        _failing_after(monkeypatch, fpa, "_write_key_id_in")
    elif point == "rmtree":
        _failing_after(monkeypatch, fpa.shutil, "rmtree")


AFTER_LAUNCH = ["wait", "settings_after", "digest_after", "verify", "key_id", "rmtree"]


# Every point is reached by a run that writes; a refusal that changes
# nothing never reaches verification, so only the points it reaches.
CASES = [("write", point) for point in AFTER_LAUNCH] + [
    ("fail", point) for point in ("wait", "settings_after", "digest_after", "rmtree")]


@pytest.mark.parametrize("mode,point", CASES)
def test_any_failure_after_launch_is_unverified_and_resumes_nothing(wired, monkeypatch, point, mode):
    from app.core import unit_activity

    wired.stub.set(mode)
    _point_after_launch(point, monkeypatch, wired)
    outcome = _link()
    assert outcome in ("apply_unverified", "restarting"), outcome
    # The latch is the single source: a conservative outcome iff it is set.
    from app.core import restart_latch
    assert restart_latch.is_stale()
    if outcome == "restarting":
        # Only a run that verified can get here; verification itself was not the failure point.
        assert point not in ("verify", "key_id", "settings_after", "digest_after", "wait")
    assert _starts(wired.log) == []
    assert not unit_activity.restart_pending()
    assert not unit_activity.in_progress()


def test_settings_read_raising_after_provisioning_is_unverified(wired, monkeypatch):
    _point_after_launch("settings_after", monkeypatch, wired)
    assert _link() == "apply_unverified"
    assert _starts(wired.log) == []


def test_result_file_failure_after_a_verified_link_still_restarts(wired, monkeypatch, caplog):
    from app.core import fisqua_pairing_apply

    _failing_after(monkeypatch, fisqua_pairing_apply, "write_result")
    assert _link() == "restarting"
    assert _starts(wired.log) == []
    assert "pairing-result.json" in caplog.text


def test_restart_raising_after_a_verified_link_is_restart_needed(wired, monkeypatch):
    from app.core import fisqua_pairing_policy

    _failing_after(monkeypatch, fisqua_pairing_policy, "restart_after_link")
    assert _link() == "restart_needed"
    assert _starts(wired.log) == []


def test_only_positive_evidence_resumes(wired):
    wired.stub.set("fail")
    assert _link() == "apply_failed"
    assert len(_starts(wired.log)) == 3


def test_a_script_that_cannot_be_started_is_apply_failed(wired, monkeypatch):
    """Popen raising: nothing ran, the snapshot is unchanged, so the one downgrade applies."""
    from app.core import fisqua_pairing_apply as fpa

    def popen(*args, **kwargs):
        raise OSError("exec format error")

    real = fpa.apply_payload
    monkeypatch.setattr(fpa, "apply_payload", lambda body, **kw: real(body, popen=popen, **kw))
    assert _link() == "apply_failed"
    assert len(_starts(wired.log)) == 3


def test_a_failure_before_launch_resumes(wired, monkeypatch):
    from app.core import fisqua_pairing_apply as fpa

    _failing_after(monkeypatch, fpa, "_digest")  # the before-snapshot
    assert _link() == "apply_failed"
    assert len(_starts(wired.log)) == 3
    assert not wired.stub.ran


def test_link_shown_without_the_result_file(client, db_session, wired, monkeypatch):
    """After the restart the card reads the link from the settings; only `at` and `by` are lost."""
    from app.core import config
    from tests.unit.test_capture_manifest_export import _as

    monkeypatch.setattr(config.settings, "FISQUA_ENV", "production")
    monkeypatch.setattr(config.settings, "FISQUA_TOKEN", SecretStr("ab" * 32))
    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", "ahr")
    monkeypatch.setattr(config.settings, "OFFSITE_REMOTE", "b2unit:captua-masters")
    body = _as(client, "admin").get("/system/pairing").json()
    assert body["linked"] == {"configured": True, "tenant": "ahr", "environment": "production"}
    assert body["result"] is None


# ---------------------------------------------------------------------------
# The process-wide restart latch
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def latch():
    """Every test starts and ends with the process's restart latch clear."""
    from app.core import restart_latch

    restart_latch._reset_for_tests()
    yield restart_latch
    restart_latch._reset_for_tests()


@pytest.mark.parametrize("mode,outcome,stale", [
    ("fail", "apply_failed", False),       # refused, unchanged: the one branch that clears it
    ("write_fail", "apply_unverified", True),
    ("nothing", "apply_unverified", True),
    ("write", "linked", True),             # linked: the process holds the old settings until it restarts
])
def test_latch_set_at_launch_and_cleared_only_by_unchanged_refusal(stub, latch, mode, outcome, stale):
    stub.set(mode)
    assert not latch.is_stale()
    assert apply(stub) == outcome
    assert latch.is_stale() is stale


def test_latch_set_before_popen_returns(stub, latch):
    seen = []

    def popen(*args, **kwargs):
        seen.append(latch.is_stale())
        return subprocess.Popen(*args, **kwargs)

    assert apply(stub, popen=popen) == "linked"
    assert seen == [True]


def test_latch_not_set_when_nothing_runs(stub, latch):
    assert apply(stub, payload(token="XYZ")) == "bad_payload"
    assert not latch.is_stale()


def test_latch_survives_an_exception_after_launch(stub, latch, monkeypatch):
    from app.core import fisqua_pairing_apply as fpa

    _failing_after(monkeypatch, fpa, "_digest", calls_before=2)
    assert apply(stub) == "apply_unverified"
    assert latch.is_stale()


def test_no_scheduler_start_while_the_latch_is_set(latch, monkeypatch, caplog):
    from app.core import fisqua_pairing_service
    from tests.unit.test_system_pairing import FakeClock, Scheduler

    log, clock = [], FakeClock()
    scheduler = Scheduler("images", log, clock, running=False)
    latch.set_stale()
    fisqua_pairing_service._start_again([scheduler])
    assert log == []
    assert "restart" in caplog.text


# ---------------------------------------------------------------------------
# A relative CAPTUA_RCLONE_CONFIG
# ---------------------------------------------------------------------------

def test_relative_config_resolved_as_the_uploads_resolve_it(stub, unit, monkeypatch):
    from app.core import catalogue_backup, config, fisqua_pairing_apply

    monkeypatch.chdir(unit)
    monkeypatch.setattr(config.settings, "RCLONE_CONFIG", "conf/rclone.conf")
    (unit / "conf").mkdir()
    uploads = catalogue_backup._rclone_argv("lsf", "b2unit:x")
    assert uploads[uploads.index("--config") + 1] == str(unit / "conf" / "rclone.conf")
    assert fisqua_pairing_apply.rclone_config_path() == unit / "conf" / "rclone.conf"
    assert apply(stub) == "linked"
    assert stub.argv()[-2:] == ["--config-path", str(unit / "conf" / "rclone.conf")]


# ---------------------------------------------------------------------------
# A failure before the script is launched
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("point", ["mkdtemp", "open", "write"])
def test_key_file_failure_before_launch_is_apply_failed_and_resumes(wired, monkeypatch, point):
    from app.core import fisqua_pairing_apply as fpa
    from app.core import restart_latch
    from tests.unit.test_system_pairing import Progress

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    if point == "mkdtemp":
        monkeypatch.setattr(fpa.tempfile, "mkdtemp", boom)
    elif point == "open":
        monkeypatch.setattr(fpa, "_open_key_file", boom)
    else:
        monkeypatch.setattr(fpa, "_write_key_file", boom)
    session = Progress()
    assert _link(session) == "apply_failed"
    assert session.reasons[-1] == "prepare_failed"
    assert not restart_latch.is_stale()
    assert not wired.stub.ran
    assert len(_starts(wired.log)) == 3
    assert list((wired.unit / "keys").iterdir()) == []


def test_relative_data_dir_gives_an_absolute_config_path(stub, unit, monkeypatch):
    from app.core import config, fisqua_pairing_apply

    monkeypatch.chdir(unit)
    monkeypatch.setattr(config.settings, "DATA_DIR", "data")
    monkeypatch.setattr(config.settings, "RCLONE_CONFIG", "")
    assert fisqua_pairing_apply.rclone_config_path() == unit / "data" / "rclone.conf"
    assert fisqua_pairing_apply.rclone_config_path().is_absolute()
    assert apply(stub) == "linked"
    assert stub.argv()[-2:] == ["--config-path", str(unit / "data" / "rclone.conf")]
    key_path = Path(stub.read("key_path").strip())
    assert key_path.is_absolute()


@pytest.fixture(autouse=True)
def clean_activity():
    """A linked apply leaves offsite jobs blocked until the process restarts; each test starts and ends clean."""
    from app.core import unit_activity

    unit_activity._reset_for_tests()
    yield
    unit_activity._reset_for_tests()


# ---------------------------------------------------------------------------
# Shutdown, and bounded waits
# ---------------------------------------------------------------------------

def test_no_provisioning_once_shutdown_begins(stub, latch):
    reasons = []
    latch.begin_shutdown()
    assert apply(stub, report=reasons.append) == "apply_failed"
    assert reasons == ["shutting_down"]
    assert not stub.ran
    assert not latch.is_stale()


def test_no_scheduler_start_once_shutdown_begins(latch, caplog):
    from app.core import fisqua_pairing_service
    from tests.unit.test_system_pairing import FakeClock, Scheduler

    log = []
    latch.begin_shutdown()
    fisqua_pairing_service._start_again([Scheduler("images", log, FakeClock(), running=False)])
    assert log == []


def test_the_wait_after_sigkill_is_bounded(monkeypatch):
    from app.core import fisqua_pairing_apply as fpa

    class Stuck:
        pid = 999999

        def wait(self, timeout=None):
            if timeout is None:
                raise AssertionError("an unbounded wait")
            raise subprocess.TimeoutExpired("stub", timeout)

        def send_signal(self, sig):
            pass

    monkeypatch.setattr(fpa, "_signal_group", lambda process, sig: None)
    monkeypatch.setattr(fpa.restart_latch, "set_stale_unless_shutting_down", lambda: True)
    exit_code, killed = fpa._run_script(["stub"], {}, "/", lambda *a, **k: Stuck(), 0.01, 0.01)
    assert (exit_code, killed) == (None, True)
