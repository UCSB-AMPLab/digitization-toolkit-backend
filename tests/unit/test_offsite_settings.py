"""Entering offsite settings without a terminal (NEH-341): the stick file, the listing, the script run and the rules.

Fakes stand in for rclone (FakeRun, both listings: it succeeds when the
config it is given holds the good application key) and for
scripts/provision-rclone.sh (FakeScript, a Popen stand-in that records its
argv, its environment and the key file, and then does what the script
does: it keeps an existing [b2unit] unless given --replace-credential,
rewrites offsite.env whole, which leaves it byte for byte the same when
nothing changed; or it writes nothing, or exits non-zero, as told).
Nothing reaches the network and no real script runs. The schedulers are
those of test_system_pairing.

    python -m pytest tests/unit/test_offsite_settings.py

Version: 0.4.0
"""

import logging
import os
import stat
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# Obviously fake values: none is a real key.
BUCKET = "captua-fake-bucket"
KEY_ID = "0000fakekeyid0000"
APP_KEY = "K000FAKEappKEYnotREAL0000000000"
OTHER_KEY_ID = "0000otherkeyid000"
TENANT = "fake-archive"


def recipient():
    import pyrage

    return str(pyrage.x25519.Identity.generate().to_public())


RECIPIENT = recipient()


def file_text(**over):
    values = {"BUCKET_NAME": BUCKET, "APPLICATION_KEY_ID": KEY_ID, "APPLICATION_KEY": APP_KEY,
              "TENANT": TENANT, "CATALOGUE_PUBLIC_KEY": RECIPIENT}
    values.update(over)
    return "".join(f"{k}={v}\n" for k, v in values.items() if v is not None)


def values(**over):
    from app.core import offsite_settings

    return offsite_settings.parse_text(file_text(**over))


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeRun:
    """subprocess.run for the listing: records the call and the temporary config while it exists."""

    def __init__(self, returncode=None, raises=None, good_key=APP_KEY):
        self.returncode, self.raises, self.good_key = returncode, raises, good_key
        self.calls = []

    def __call__(self, argv, **kwargs):
        config_path = Path(argv[argv.index("--config") + 1])
        self.calls.append(SimpleNamespace(
            argv=list(argv), kwargs=kwargs, config_text=config_path.read_text(),
            config_mode=stat.S_IMODE(config_path.stat().st_mode),
            dir_mode=stat.S_IMODE(config_path.parent.stat().st_mode), config_path=config_path))
        if self.raises is not None:
            raise self.raises
        if self.returncode is not None:
            return subprocess.CompletedProcess(argv, self.returncode)
        works = f"key = {self.good_key}\n" in self.calls[-1].config_text
        return subprocess.CompletedProcess(argv, 0 if works else 1)


class FakeProcess:
    pid = 999999

    def __init__(self, code):
        self.code = code

    def wait(self, timeout=None):
        return self.code

    def send_signal(self, sig):
        pass


class FakeScript:
    """Popen for provision-rclone.sh. `mode`: write, nothing, fail, write_fail, wrong_tenant, wrong_key.

    In `write` it does what the script does with a config that exists: it
    keeps it, the new key unread, unless given --replace-credential (the
    script's resume branch for a complete [b2unit] with the same key id);
    without one it writes [b2unit] from the key file. offsite.env is
    rewritten whole, so a run that changes nothing leaves it byte for byte
    the same. `during` runs while the script runs.
    """

    def __init__(self, root: Path, mode="write"):
        self.root = root
        self.mode = mode
        self.during = None
        self.path = root / "superproject" / "scripts" / "provision-rclone.sh"
        self.path.parent.mkdir(parents=True)
        self.path.write_text("#!/bin/bash\ncase \"$1\" in\n    --replace-credential) : ;;\nesac\n")
        self.offsite_env = root / "offsite.env"
        self.calls = []
        self.values = {"OFFSITE_TENANT": "", "OFFSITE_REMOTE": "", "OFFSITE_DUMP_RECIPIENT": "",
                       "RCLONE_CONFIG": ""}

    @property
    def ran(self):
        return bool(self.calls)

    def __call__(self, argv, env=None, **kwargs):
        key_file = Path(env["DTK_OFFSITE_KEY_FILE"])
        self.calls.append(SimpleNamespace(
            argv=list(argv), env=dict(env), kwargs=kwargs, key_text=key_file.read_text(),
            key_mode=stat.S_IMODE(key_file.stat().st_mode),
            dir_mode=stat.S_IMODE(key_file.parent.stat().st_mode), key_file=key_file))
        if self.during is not None:
            self.during()
        if self.mode in ("nothing", "fail"):
            return FakeProcess(0 if self.mode == "nothing" else 3)
        lines = dict(line.split("=", 1) for line in key_file.read_text().splitlines())
        tenant = argv[argv.index("--tenant") + 1]
        cfg = Path(argv[argv.index("--config-path") + 1])
        self.values.update({"OFFSITE_TENANT": "other" if self.mode == "wrong_tenant" else tenant,
                            "OFFSITE_REMOTE": f"b2unit:{lines['BUCKET']}",
                            "OFFSITE_DUMP_RECIPIENT": argv[argv.index("--dump-recipient") + 1],
                            "RCLONE_CONFIG": str(cfg)})
        if self.mode == "wrong_key":
            cfg.write_text(f"[b2unit]\ntype = b2\naccount = {OTHER_KEY_ID}\nkey = {lines['APP_KEY']}\n")
        elif "--replace-credential" in argv or not cfg.exists():
            cfg.write_text(f"[b2unit]\ntype = b2\naccount = {lines['KEY_ID']}\nkey = {lines['APP_KEY']}\n")
        self.offsite_env.write_text("".join(f"DTK_{k}={v}\n" for k, v in self.values.items()))
        return FakeProcess(4 if self.mode == "write_fail" else 0)

    def settings(self):
        return SimpleNamespace(**self.values)


@pytest.fixture
def unit(monkeypatch, tmp_path, db_session):
    """A unit with no offsite settings, rclone present, and scratch key and data directories.

    After db_session, whose override_projects_root rebuilds the Settings object these values are set on.
    """
    from app.core import config, provision_runner

    data = tmp_path / "data"
    data.mkdir()
    (tmp_path / "keys").mkdir()
    monkeypatch.setattr(config.settings, "DATA_DIR", str(data))
    monkeypatch.setattr(config.settings, "RCLONE_CONFIG", str(data / "rclone.conf"))
    monkeypatch.setattr(config.settings, "OFFSITE_HOLD_FILE", "")
    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", "")
    monkeypatch.setattr(config.settings, "OFFSITE_REMOTE", "")
    monkeypatch.setattr(config.settings, "RCLONE_BIN", "rclone")
    monkeypatch.setattr(provision_runner, "rclone_available", lambda: True)
    monkeypatch.setattr(provision_runner, "DEV_SHM", tmp_path / "keys")
    monkeypatch.setattr(provision_runner, "_is_tmpfs", lambda path, mounts=None: True)
    return tmp_path


@pytest.fixture
def script(unit, monkeypatch):
    from app.core import provision_runner

    fake = FakeScript(unit)
    monkeypatch.setattr(provision_runner, "provision_script", lambda backend_dir=None: fake.path)
    return fake


@pytest.fixture
def wired(unit, script, monkeypatch):
    """The listing and the script replaced by fakes, fake schedulers whose workers exit, and a restart that is stood in for."""
    import functools

    from app.core import fisqua_pairing_policy, fisqua_pairing_service, offsite_settings
    from tests.unit.test_system_pairing import FakeClock, Scheduler

    lister = FakeRun()
    real_list, real_provision = offsite_settings.list_bucket, offsite_settings.provision
    monkeypatch.setattr(offsite_settings, "list_bucket", functools.partial(real_list, run=lister))
    monkeypatch.setattr(offsite_settings, "provision", functools.partial(
        real_provision, popen=script, run=lister, settings_factory=script.settings, env_file=unit / ".env",
        offsite_env=script.offsite_env, time_limit=5.0))
    log, clock = [], FakeClock()
    made = [Scheduler(name, log, clock) for name in ("status", "images", "catalogue")]
    monkeypatch.setattr(fisqua_pairing_service, "_schedulers", lambda: made)
    monkeypatch.setattr(fisqua_pairing_service, "_clock", clock)
    restarts = []
    monkeypatch.setattr(fisqua_pairing_policy, "restart_after_link", lambda: restarts.append(1) or "restarting")
    return SimpleNamespace(unit=unit, script=script, lister=lister, log=log, restarts=restarts)


def has_credential(unit, account, key="old"):
    (unit / "data" / "rclone.conf").write_text(f"[b2unit]\ntype = b2\naccount = {account}\nkey = {key}\n")


# ---------------------------------------------------------------------------
# The stick file
# ---------------------------------------------------------------------------

def refusal(fn, *args, **kwargs):
    from app.core import offsite_settings

    with pytest.raises(offsite_settings.Refused) as caught:
        fn(*args, **kwargs)
    return caught.value


def test_the_five_keys_are_read_and_mapped():
    found = values()
    assert (found.bucket, found.key_id, found.application_key, found.tenant, found.recipient) == (
        BUCKET, KEY_ID, APP_KEY, TENANT, RECIPIENT)


def test_comments_and_blank_lines_are_allowed():
    from app.core import offsite_settings

    text = "# a comment\n\n   # an indented comment\n" + file_text().replace("TENANT=", "\n# the folder\nTENANT=")
    assert offsite_settings.parse_text(text).tenant == TENANT


def test_crlf_and_spaces_are_read_as_the_scripts_key_file_value_reads_them():
    from app.core import offsite_settings

    text = file_text().replace("\n", "\r\n").replace(f"TENANT={TENANT}", f"  TENANT =  {TENANT} \t")
    found = offsite_settings.parse_text(text)
    assert found.tenant == TENANT and found.application_key == APP_KEY


def test_a_utf8_byte_order_mark_is_allowed():
    from app.core import offsite_settings

    assert offsite_settings.parse_text("﻿" + file_text()).bucket == BUCKET


def test_the_filled_in_template_is_read():
    from app.core import offsite_settings

    text = offsite_settings.template_text()
    filled = {"BUCKET_NAME": BUCKET, "APPLICATION_KEY_ID": KEY_ID, "APPLICATION_KEY": APP_KEY, "TENANT": TENANT,
              "CATALOGUE_PUBLIC_KEY": RECIPIENT}
    for key, value in filled.items():
        text = text.replace(f"\n{key}=\n", f"\n{key}={value}\n")
    assert offsite_settings.parse_text(text).recipient == RECIPIENT


def test_the_template_holds_each_key_once_with_no_value_and_says_what_each_is():
    from app.core import offsite_settings

    text = offsite_settings.template_text()
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert lines == ["BUCKET_NAME=", "APPLICATION_KEY_ID=", "APPLICATION_KEY=", "TENANT=", "CATALOGUE_PUBLIC_KEY="]
    for key in ("BUCKET_NAME", "APPLICATION_KEY_ID", "APPLICATION_KEY", "TENANT", "CATALOGUE_PUBLIC_KEY"):
        assert f"# {key}:" in text
    assert refusal(offsite_settings.parse_text, text).code == "empty_value"


@pytest.mark.parametrize("text,code,key", [
    (file_text() + "BUCKET=zzunknownvaluezz\n", "unknown_key", None),
    (file_text() + f"TENANT={TENANT}\n", "duplicate_key", "TENANT"),
    (file_text(APPLICATION_KEY=None), "missing_key", "APPLICATION_KEY"),
    (file_text(TENANT=""), "empty_value", "TENANT"),
    (file_text(BUCKET_NAME="no"), "bad_value", "BUCKET_NAME"),
    (file_text(BUCKET_NAME="has space"), "bad_value", "BUCKET_NAME"),
    (file_text(APPLICATION_KEY_ID="key\"id"), "bad_value", "APPLICATION_KEY_ID"),
    (file_text(APPLICATION_KEY="K000 FAKE"), "bad_value", "APPLICATION_KEY"),
    (file_text(TENANT="Fake_Archive"), "bad_value", "TENANT"),
    (file_text(TENANT="admin"), "bad_value", "TENANT"),
    (file_text(CATALOGUE_PUBLIC_KEY="age1notakey"), "bad_value", "CATALOGUE_PUBLIC_KEY"),
    (file_text(CATALOGUE_PUBLIC_KEY="AGE-SECRET-KEY-1FAKEFAKEFAKE"), "bad_value", "CATALOGUE_PUBLIC_KEY"),
    (file_text(APPLICATION_KEY="AGE-SECRET-KEY-1FAKEFAKEFAKE"), "bad_value", "APPLICATION_KEY"),
])
def test_each_refusal_names_the_key_and_never_the_value(text, code, key):
    from app.core import offsite_settings

    refused = refusal(offsite_settings.parse_text, text)
    assert (refused.code, refused.key) == (code, key)
    shown = repr(refused.detail()) + str(refused) + repr(refused)
    for secret in (APP_KEY, "AGE-SECRET-KEY", "K000 FAKE", "zzunknownvaluezz", "age1notakey"):
        assert secret not in shown


def test_a_line_that_is_not_key_value_is_refused_by_its_number_only():
    from app.core import offsite_settings

    refused = refusal(offsite_settings.parse_text, file_text() + APP_KEY + "\n")
    assert refused.code == "bad_line" and refused.detail()["line"] == 6
    assert APP_KEY not in repr(refused.detail())


@pytest.mark.parametrize("name", ["BUCKET", "K000UPPERCASEKEY0000000000000000", "K000FakeSecretShaped"])
def test_an_unknown_key_is_refused_by_its_line_number_only(name):
    from app.core import offsite_settings

    refused = refusal(offsite_settings.parse_text, file_text() + f"{name}=x\n")
    assert (refused.code, refused.key, refused.line) == ("unknown_key", None, 6)
    assert name not in repr(refused.detail()) + repr(refused) + str(refused)


def test_the_values_repr_never_holds_the_application_key():
    found = values()
    assert APP_KEY not in repr(found) and APP_KEY not in str(found)


@pytest.fixture
def stick(tmp_path, monkeypatch):
    """A drive mounted directly under the mounts base; the mount itself is stood in for (_mounted)."""
    from app.core import offsite_settings

    root = tmp_path / "mounts" / "STICK"
    root.mkdir(parents=True)
    monkeypatch.setattr(offsite_settings, "_mounted", lambda fd, base_fd: True)
    return root


def read(stick_dir):
    from app.core import offsite_settings

    opened = offsite_settings.open_stick(stick_dir, stick_dir.parent)
    try:
        return offsite_settings.read_stick(opened)
    finally:
        opened.close()


def test_the_stick_file_is_read_from_the_top_level(stick):
    (stick / "captua-offsite.txt").write_text(file_text())
    assert read(stick).tenant == TENANT


def test_a_stick_without_the_file_is_refused(stick):
    (stick / "sub").mkdir()
    (stick / "sub" / "captua-offsite.txt").write_text(file_text())
    assert refusal(read, stick).code == "file_missing"


def test_a_symlink_is_refused(stick, tmp_path):
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text(file_text())
    (stick / "captua-offsite.txt").symlink_to(elsewhere)
    assert refusal(read, stick).code == "file_not_regular"


def test_a_directory_is_refused(stick):
    (stick / "captua-offsite.txt").mkdir()
    assert refusal(read, stick).code == "file_not_regular"


def test_a_file_over_the_limit_is_refused(stick):
    from app.core import offsite_settings

    (stick / "captua-offsite.txt").write_text(file_text() + "#" * offsite_settings.MAX_FILE_BYTES)
    assert refusal(read, stick).code == "file_too_large"


def test_a_file_that_is_not_text_is_refused(stick):
    (stick / "captua-offsite.txt").write_bytes(b"\xff\xfe" + file_text().encode("utf-16-le"))
    assert refusal(read, stick).code == "file_not_text"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_a_file_the_backend_cannot_read_is_refused(stick):
    path = stick / "captua-offsite.txt"
    path.write_text(file_text())
    path.chmod(0)
    try:
        assert refusal(read, stick).code == "file_unreadable"
    finally:
        path.chmod(0o600)


@pytest.mark.skipif(os.geteuid() == 0, reason="root deletes any file")
def test_a_file_the_backend_cannot_delete_is_refused(stick):
    (stick / "captua-offsite.txt").write_text(file_text())
    stick.chmod(0o555)
    try:
        assert refusal(read, stick).code == "file_not_deletable"
    finally:
        stick.chmod(0o755)


def test_only_a_mounted_direct_child_of_the_mounts_base_is_opened(stick, tmp_path, monkeypatch):
    """The request's path is never resolved: its parent is the base as written, its last part one name."""
    from app.core import offsite_settings

    (stick / "sub").mkdir()
    (stick / "sub" / "captua-offsite.txt").write_text(file_text())
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "captua-offsite.txt").write_text(file_text())
    base = stick.parent
    (base / "LINK").symlink_to(outside)
    (stick / "jump").symlink_to(stick / "sub")
    for target in (stick / "sub", f"{stick}/jump/..", f"{stick}/.", f"{base}/..", f"{base}/.", f"{base}/",
                   str(base), str(outside), f"{base}/STICK/../../outside", base / "LINK", f"{base}//STICK",
                   f"{tmp_path}/./mounts/STICK"):
        refused = refusal(offsite_settings.open_stick, target, base)
        assert (refused.code, refused.status) == ("not_a_drive", 400), target
    opened = offsite_settings.open_stick(f"{base}/STICK", base)
    opened.close()
    monkeypatch.setattr(offsite_settings, "_mounted", lambda fd, base_fd: False)
    assert refusal(offsite_settings.open_stick, stick, base).code == "not_a_drive"


def test_a_directory_on_the_mounts_base_file_system_is_not_a_mount(tmp_path):
    from app.core import offsite_settings

    child = tmp_path / "mounts" / "NOTMOUNTED"
    child.mkdir(parents=True)
    base_fd = os.open(child.parent, os.O_RDONLY | os.O_DIRECTORY)
    fd = os.open(child, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert offsite_settings._mounted(fd, base_fd) is False
    finally:
        os.close(fd)
        os.close(base_fd)


def test_the_directory_opened_is_the_name_under_the_base_opened_first(stick, monkeypatch):
    """Swapping the name for a symlink after the base is opened is refused: the name is opened O_NOFOLLOW at the base."""
    from app.core import offsite_settings

    real_open = os.open
    calls = []

    def recording_open(path, flags, *args, **kwargs):
        calls.append((path, flags, kwargs.get("dir_fd")))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(offsite_settings.os, "open", recording_open)
    opened = offsite_settings.open_stick(stick, stick.parent)
    opened.close()
    monkeypatch.setattr(offsite_settings.os, "open", real_open)
    (base_path, base_flags, base_dir_fd), (name, flags, dir_fd) = calls[:2]
    assert base_path == str(stick.parent) and base_dir_fd is None
    assert name == "STICK" and dir_fd is not None
    for f in (base_flags, flags):
        assert f & os.O_DIRECTORY and f & os.O_NOFOLLOW


# ---------------------------------------------------------------------------
# The listing
# ---------------------------------------------------------------------------

def test_the_listing_uses_a_private_temporary_config_and_is_removed(unit):
    from app.core import offsite_settings

    run = FakeRun()
    assert offsite_settings.list_bucket(values(), run=run) is True
    call = run.calls[0]
    assert call.argv[0] == "rclone"
    assert call.argv[-1] == f"b2:{BUCKET}/{TENANT}"
    assert "lsf" in call.argv and call.argv[call.argv.index("--max-depth") + 1] == "1"
    assert "--ask-password=false" in call.argv
    assert call.kwargs["timeout"] == offsite_settings.LISTING_TIMEOUT_SECONDS
    assert call.kwargs["stdout"] == subprocess.DEVNULL and call.kwargs["stderr"] == subprocess.DEVNULL
    assert set(call.kwargs["env"]) == {"PATH", "HOME", "LANG"}
    assert (call.config_mode, call.dir_mode) == (0o600, 0o700)
    assert call.config_path.parent.parent == unit / "keys"
    assert call.config_text == f"[b2]\ntype = b2\naccount = {KEY_ID}\nkey = {APP_KEY}\n"
    assert not call.config_path.parent.exists()


@pytest.mark.parametrize("run", [FakeRun(returncode=1), FakeRun(raises=subprocess.TimeoutExpired("rclone", 60)),
                                 FakeRun(raises=FileNotFoundError("rclone"))])
def test_a_listing_that_fails_answers_false_and_removes_the_config(unit, run):
    from app.core import offsite_settings

    run.calls.clear()
    assert offsite_settings.list_bucket(values(), run=run) is False
    assert not run.calls[0].config_path.parent.exists()


def test_a_failed_listing_writes_nothing(wired, db_session):
    from app.core import offsite_settings

    wired.lister.returncode = 1
    refused = refusal(offsite_settings.save_form, db_session, form(), replace=False, actor="admin1")
    assert refused.code == "listing_failed"
    assert not wired.script.ran
    assert not (wired.unit / "data" / "rclone.conf").exists()
    assert wired.log == []


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def form(**over):
    body = {"BUCKET_NAME": BUCKET, "APPLICATION_KEY_ID": KEY_ID, "APPLICATION_KEY": APP_KEY, "TENANT": TENANT,
            "CATALOGUE_PUBLIC_KEY": RECIPIENT}
    body.update(over)
    return body


def test_a_save_runs_the_script_with_the_exact_argv_and_key_file(wired, db_session):
    from app.core import offsite_settings

    result = offsite_settings.save_form(db_session, form(), replace=False, actor="admin1")
    assert result == {"outcome": "restarting", "stickFile": None}
    call = wired.script.calls[0]
    config_path = wired.unit / "data" / "rclone.conf"
    assert call.argv == [str(wired.script.path), "--tenant", TENANT, "--dump-recipient", RECIPIENT,
                         "--config-path", str(config_path)]
    assert call.key_text == f"KEY_ID={KEY_ID}\nAPP_KEY={APP_KEY}\nBUCKET={BUCKET}\n"
    assert (call.key_mode, call.dir_mode) == (0o600, 0o700)
    assert set(call.env) == {"PATH", "HOME", "LANG", "DTK_OFFSITE_KEY_FILE"}
    assert not call.key_file.parent.exists()
    assert wired.restarts == [1]
    # The listing came first, with the same values.
    assert wired.lister.calls[0].argv[-1] == f"b2:{BUCKET}/{TENANT}"


def test_values_are_stripped_from_the_form(wired, db_session):
    from app.core import offsite_settings

    offsite_settings.save_form(db_session, form(TENANT=f"  {TENANT}\n"), replace=False, actor="admin1")
    assert wired.script.calls[0].argv[2] == TENANT


@pytest.mark.parametrize("mode,code", [("wrong_tenant", "apply_unverified"), ("wrong_key", "apply_unverified"),
                                       ("nothing", "apply_unverified"), ("fail", "apply_failed"),
                                       ("write_fail", "apply_unverified")])
def test_a_run_that_does_not_verify_is_refused(wired, db_session, monkeypatch, mode, code):
    from app.core import offsite_settings, restart_latch

    # conftest puts a recipient in the environment, where it would win over offsite.env.
    monkeypatch.delenv("CAPTUA_OFFSITE_DUMP_RECIPIENT", raising=False)
    wired.script.mode = mode
    refused = refusal(offsite_settings.save_form, db_session, form(), replace=False, actor="admin1")
    assert refused.code == code
    assert wired.restarts == []
    starts = [entry for entry in wired.log if entry[0] == "start"]
    if mode in ("fail", "nothing"):
        # Nothing was written: the schedulers run again and the latch is clear.
        assert len(starts) == 3 and not restart_latch.is_stale()
    else:
        assert starts == [] and restart_latch.is_stale()


def test_a_setting_that_wins_over_offsite_env_is_settings_overridden(wired, db_session):
    from app.core import offsite_settings

    wired.script.mode = "wrong_tenant"
    (wired.unit / ".env").write_text("CAPTUA_OFFSITE_TENANT=other\n")
    refused = refusal(offsite_settings.save_form, db_session, form(), replace=False, actor="admin1")
    assert refused.code == "settings_overridden"


def test_verify_checks_only_the_offsite_values(unit, monkeypatch):
    from app.core import offsite_settings

    monkeypatch.delenv("CAPTUA_OFFSITE_DUMP_RECIPIENT", raising=False)

    config_path = unit / "data" / "rclone.conf"
    config_path.write_text(f"[b2unit]\ntype = b2\naccount = {KEY_ID}\nkey = {APP_KEY}\n")
    good = SimpleNamespace(OFFSITE_TENANT=TENANT, OFFSITE_REMOTE=f"b2unit:{BUCKET}", OFFSITE_DUMP_RECIPIENT=RECIPIENT,
                           RCLONE_CONFIG=str(config_path))
    assert offsite_settings.verify(values(), good, config_path, unit / ".env") is None
    for name, wrong in (("OFFSITE_TENANT", "other"), ("OFFSITE_REMOTE", f"b2read:{BUCKET}"),
                        ("OFFSITE_DUMP_RECIPIENT", recipient()), ("RCLONE_CONFIG", "/elsewhere")):
        settings = SimpleNamespace(**{**vars(good), name: wrong})
        assert offsite_settings.verify(values(), settings, config_path, unit / ".env") == "apply_unverified", name
    config_path.write_text(f"[b2unit]\ntype = b2\naccount = {OTHER_KEY_ID}\nkey = x\n")
    assert offsite_settings.verify(values(), good, config_path, unit / ".env") == "apply_unverified"


# ---------------------------------------------------------------------------
# The stick's copy
# ---------------------------------------------------------------------------

def test_the_stick_copy_is_deleted_only_after_a_verified_save(wired, db_session, stick, monkeypatch):
    from app.core import offsite_settings

    synced = []
    monkeypatch.setattr(offsite_settings, "_sync", lambda: synced.append(1))
    path = stick / "captua-offsite.txt"
    path.write_text(file_text())
    result = offsite_settings.import_stick(db_session, stick, base=stick.parent, replace=False, actor="admin1")
    assert result == {"outcome": "restarting", "stickFile": "deleted"}
    assert not path.exists() and synced == [1]


@pytest.mark.parametrize("failure", ["listing", "fail", "wrong_key", "bad_value"])
def test_the_stick_copy_is_kept_after_any_failure(wired, db_session, stick, failure):
    from app.core import offsite_settings

    path = stick / "captua-offsite.txt"
    path.write_text(file_text(TENANT="Bad Tenant") if failure == "bad_value" else file_text())
    if failure == "listing":
        wired.lister.returncode = 1
    elif failure in ("fail", "wrong_key"):
        wired.script.mode = failure
    refused = refusal(offsite_settings.import_stick, db_session, stick, base=stick.parent, replace=False, actor="admin1")
    assert refused.detail()["stickFile"] == "kept"
    assert path.read_text() == (file_text(TENANT="Bad Tenant") if failure == "bad_value" else file_text())


def test_a_stick_copy_that_cannot_be_deleted_after_the_save_is_reported(wired, db_session, stick, monkeypatch):
    from app.core import offsite_settings

    monkeypatch.setattr(offsite_settings, "_sync", lambda: None)

    (stick / "captua-offsite.txt").write_text(file_text())

    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(offsite_settings, "_remove", refuse)
    result = offsite_settings.import_stick(db_session, stick, base=stick.parent, replace=False, actor="admin1")
    assert result == {"outcome": "restarting", "stickFile": "not_deleted"}


# ---------------------------------------------------------------------------
# A unit that already has offsite settings
# ---------------------------------------------------------------------------

def test_another_tenant_is_refused_before_anything_runs(wired, db_session, monkeypatch):
    from app.core import config, offsite_settings

    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", "already-here")
    for replace in (False, True):
        refused = refusal(offsite_settings.save_form, db_session, form(), replace=replace, actor="admin1")
        assert refused.code == "other_tenant"
    assert not wired.script.ran and wired.lister.calls == []


def test_another_credential_needs_replace(wired, db_session, monkeypatch):
    from app.core import config, offsite_settings

    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", TENANT)
    has_credential(wired.unit, OTHER_KEY_ID)
    refused = refusal(offsite_settings.save_form, db_session, form(), replace=False, actor="admin1")
    assert refused.code == "replace_required"
    assert not wired.script.ran and wired.lister.calls == []
    assert offsite_settings.save_form(db_session, form(), replace=True, actor="admin1")["outcome"] == "restarting"
    argv = wired.script.calls[0].argv
    assert argv[-1] == "--replace-credential" and "--change-tenant" not in argv


def test_the_same_credential_runs_without_replace(wired, db_session, monkeypatch):
    from app.core import config, offsite_settings

    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", TENANT)
    has_credential(wired.unit, KEY_ID, key=APP_KEY)
    assert offsite_settings.save_form(db_session, form(), replace=False, actor="admin1")["outcome"] == "restarting"
    assert "--replace-credential" not in wired.script.calls[0].argv
    # The script kept the config it found.
    assert (wired.unit / "data" / "rclone.conf").read_text().endswith(f"key = {APP_KEY}\n")


def test_replace_on_a_unit_with_no_credential_passes_no_flag(wired, db_session):
    from app.core import offsite_settings

    offsite_settings.save_form(db_session, form(), replace=True, actor="admin1")
    assert "--replace-credential" not in wired.script.calls[0].argv


def test_replace_with_a_script_too_old_is_refused(wired, db_session, monkeypatch):
    from app.core import config, offsite_settings

    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", TENANT)
    has_credential(wired.unit, OTHER_KEY_ID)
    wired.script.path.write_text("#!/bin/bash\nexit 0\n")
    refused = refusal(offsite_settings.save_form, db_session, form(), replace=True, actor="admin1")
    assert refused.code == "script_too_old"
    assert not wired.script.ran


@pytest.mark.parametrize("gate,code", [("latch", "restart_required"), ("capture", "busy_capturing"),
                                       ("rclone", "rclone_missing"), ("script", "script_missing")])
def test_gates_refuse_before_anything_runs(wired, db_session, monkeypatch, gate, code):
    from app.core import offsite_settings, provision_runner, restart_latch, unit_activity

    if gate == "latch":
        restart_latch.set_stale()
    elif gate == "capture":
        unit_activity.enter(unit_activity.CAMERA)
    elif gate == "rclone":
        monkeypatch.setattr(provision_runner, "rclone_available", lambda: False)
    else:
        wired.script.path.unlink()
    refused = refusal(offsite_settings.save_form, db_session, form(), replace=False, actor="admin1")
    assert refused.code == code
    assert not wired.script.ran and wired.lister.calls == []


# ---------------------------------------------------------------------------
# The application key never leaves the unit's key file
# ---------------------------------------------------------------------------

def test_the_application_key_is_in_no_log_record_or_audit_entry(wired, db_session, stick, caplog, monkeypatch):
    from app.core import offsite_settings, restart_latch
    from app.models.system_log import SystemLog

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(offsite_settings, "_sync", lambda: None)
    (stick / "captua-offsite.txt").write_text(file_text())
    offsite_settings.import_stick(db_session, stick, base=stick.parent, replace=False, actor="admin1")
    restart_latch._reset_for_tests()
    wired.script.mode = "wrong_key"
    (stick / "captua-offsite.txt").write_text(file_text())
    refusal(offsite_settings.import_stick, db_session, stick, base=stick.parent, replace=False, actor="admin1")
    refusal(offsite_settings.save_form, db_session, form(APPLICATION_KEY="K000 FAKE"), replace=False, actor="a")
    (stick / "captua-offsite.txt").write_text(file_text() + "K000UPPERCASEKEY0000000000000000=x\n")
    refusal(offsite_settings.import_stick, db_session, stick, base=stick.parent, replace=False, actor="admin1")
    assert APP_KEY not in caplog.text and "K000 FAKE" not in caplog.text
    assert "K000UPPERCASEKEY" not in caplog.text
    for record in caplog.records:
        assert APP_KEY not in repr(record.args)
    rows = db_session.query(SystemLog).all()
    actions = [row.action for row in rows]
    assert "offsite_settings_saved" in actions and "offsite_settings_refused" in actions
    for row in rows:
        assert APP_KEY not in f"{row.subject} {row.detail} {row.actor}"
        assert KEY_ID not in f"{row.subject} {row.detail}"
        assert "K000UPPERCASEKEY" not in f"{row.subject} {row.detail}"
    assert any(row.detail.endswith("code=unknown_key line=6") for row in rows)


# ---------------------------------------------------------------------------
# A unit linked to Fisqua, and pairing while a save is in progress
# ---------------------------------------------------------------------------

def test_a_unit_linked_to_fisqua_is_refused_before_anything_runs(wired, db_session, stick, monkeypatch):
    from app.core import fisqua_outbox, offsite_settings

    monkeypatch.setattr(fisqua_outbox, "fisqua_configured", lambda: True)
    refused = refusal(offsite_settings.save_form, db_session, form(), replace=True, actor="admin1")
    assert (refused.code, refused.status) == ("fisqua_linked", 409)
    (stick / "captua-offsite.txt").write_text(file_text())
    refused = refusal(offsite_settings.import_stick, db_session, stick, base=stick.parent, replace=False, actor="admin1")
    assert refused.code == "fisqua_linked" and (stick / "captua-offsite.txt").exists()
    assert not wired.script.ran and wired.lister.calls == [] and wired.log == []


def test_a_save_is_in_progress_from_its_gates_to_its_end(wired, db_session, monkeypatch):
    from app.core import offsite_settings

    seen = []
    real = offsite_settings.list_bucket
    monkeypatch.setattr(offsite_settings, "list_bucket",
                        lambda values: seen.append(offsite_settings.saving()) or real(values))
    assert not offsite_settings.saving()
    offsite_settings.save_form(db_session, form(), replace=False, actor="admin1")
    assert seen == [True] and not offsite_settings.saving()
    wired.lister.returncode = 1
    refusal(offsite_settings.save_form, db_session, form(), replace=False, actor="admin1")
    assert not offsite_settings.saving()


def test_a_save_holds_admission_through_its_gates_and_releases_it_when_refused(wired, db_session, monkeypatch):
    from app.core import config, offsite_settings

    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", "already-here")
    calls = []
    real = offsite_settings._gate
    monkeypatch.setattr(offsite_settings, "_gate", lambda values, replace: calls.append(offsite_settings.saving())
                        or real(values, replace))
    assert refusal(offsite_settings.save_form, db_session, form(), replace=False, actor="admin1").code == "other_tenant"
    assert calls == [True] and not offsite_settings.saving()


def test_a_second_save_is_refused_while_one_holds_admission(wired, db_session, monkeypatch):
    from app.core import offsite_settings

    inner = []
    real = offsite_settings.list_bucket

    def listing(values):
        inner.append(refusal(offsite_settings.save_form, db_session, form(), replace=False, actor="b").code)
        return real(values)

    monkeypatch.setattr(offsite_settings, "list_bucket", listing)
    offsite_settings.save_form(db_session, form(), replace=False, actor="admin1")
    assert inner == ["offsite_save_running"]


def test_pairing_is_refused_while_a_save_is_in_progress(wired, db_session, monkeypatch):
    from app.core import fisqua_pairing_policy, offsite_settings

    during = []
    real = offsite_settings.list_bucket
    monkeypatch.setattr(offsite_settings, "list_bucket",
                        lambda values: during.append(fisqua_pairing_policy.start_refusal(db_session)) or real(values))
    offsite_settings.save_form(db_session, form(), replace=False, actor="admin1")
    assert during == ["offsite_save_running"]
    # Once it ends, the next reason is the restart the save asked for, not the save.
    assert fisqua_pairing_policy.start_refusal(db_session) != "offsite_save_running"


def test_the_pairing_route_has_a_message_for_a_save_in_progress():
    from app.api import system

    assert "offsite_save_running" in system._PAIRING_REFUSALS


# ---------------------------------------------------------------------------
# One admission for both runs of the script (pairing, offsite save)
# ---------------------------------------------------------------------------

def _registry(db_session, monkeypatch):
    from app.core import device_id, fisqua_pairing, fisqua_pairing_policy, fisqua_pairing_service
    from tests.unit.test_fisqua_pairing_session import FakeClient

    device_id.initialise()
    reg = fisqua_pairing.PairingRegistry(
        apply=lambda payload, session: "linked",
        before_begin=lambda: fisqua_pairing_policy.start_refusal(db_session),
        client_factory=lambda url: FakeClient(), start_threads=False)
    monkeypatch.setattr(fisqua_pairing_service, "pairing_registry", reg)
    return reg


def test_a_pairing_begun_while_a_save_checks_for_pairing_waits_and_is_refused(wired, db_session, monkeypatch):
    """The save pauses after its pairing check; pairing tries to begin meanwhile; the save resumes. One is admitted."""
    from app.core import fisqua_pairing, offsite_settings

    reg = _registry(db_session, monkeypatch)
    outcome, threads = [], []

    def begin():
        try:
            reg.begin("Unidad 1", start_thread=False)
            outcome.append("admitted")
        except fisqua_pairing.PairingRefused as exc:
            outcome.append(exc.reason)
        except Exception as exc:
            outcome.append(type(exc).__name__)

    real_check = offsite_settings._pairing_running

    def paused_check(registry):
        answer = real_check(registry)
        thread = threading.Thread(target=begin)
        thread.start()
        thread.join(0.3)
        assert thread.is_alive(), "pairing began while the save was deciding its admission"
        threads.append(thread)
        return answer

    real_list = offsite_settings.list_bucket

    def listing(values):
        threads[0].join(5)
        return real_list(values)

    monkeypatch.setattr(offsite_settings, "_pairing_running", paused_check)
    monkeypatch.setattr(offsite_settings, "list_bucket", listing)
    assert offsite_settings.save_form(db_session, form(), replace=False, actor="admin1")["outcome"] == "restarting"
    assert outcome == ["offsite_save_running"]
    assert reg.current() is None


def test_a_save_is_refused_while_a_pairing_session_runs(wired, db_session, monkeypatch):
    from app.core import offsite_settings

    reg = _registry(db_session, monkeypatch)
    session = reg.begin("Unidad 1", start_thread=False)
    assert session.running
    refused = refusal(offsite_settings.save_form, db_session, form(), replace=False, actor="admin1")
    assert refused.code == "pairing_running"
    assert not wired.script.ran and wired.lister.calls == []
    reg.cancel()
    assert offsite_settings.save_form(db_session, form(), replace=False, actor="admin1")["outcome"] == "restarting"


def test_both_refusal_codes_come_from_the_runners_admission():
    from app.core import provision_runner as runner

    token = runner.admit(runner.OFFSITE_SAVE, pairing_running=lambda: False)
    try:
        assert runner.refusal(runner.PAIRING) == "offsite_save_running"
        with pytest.raises(runner.Busy) as caught:
            runner.admit(runner.OFFSITE_SAVE, pairing_running=lambda: False)
        assert caught.value.code == "offsite_save_running"
    finally:
        token.release()
    assert runner.refusal(runner.PAIRING) is None
    with pytest.raises(runner.Busy) as caught:
        runner.admit(runner.OFFSITE_SAVE, pairing_running=lambda: True)
    assert caught.value.code == "pairing_running"
    assert runner.holder() is None


# ---------------------------------------------------------------------------
# The saved configuration must list the bucket
# ---------------------------------------------------------------------------

def test_a_save_lists_the_bucket_through_the_units_own_config(wired, db_session):
    from app.core import offsite_settings

    offsite_settings.save_form(db_session, form(), replace=False, actor="admin1")
    persisted = wired.lister.calls[-1]
    assert persisted.config_path == wired.unit / "data" / "rclone.conf"
    assert persisted.argv[-1] == f"b2unit:{BUCKET}/{TENANT}"
    assert persisted.argv[persisted.argv.index("--max-depth") + 1] == "1" and "--ask-password=false" in persisted.argv
    assert persisted.kwargs["timeout"] == offsite_settings.LISTING_TIMEOUT_SECONDS
    assert set(persisted.kwargs["env"]) == {"PATH", "HOME", "LANG"}


def test_a_kept_credential_that_cannot_list_is_refused_and_the_stick_kept(wired, db_session, stick, monkeypatch):
    """The unit's [b2unit] has the right key id and a key that no longer works; the stick has the working key."""
    from app.core import config, offsite_settings, unit_activity

    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", TENANT)
    has_credential(wired.unit, KEY_ID, key="WRONG-SECRET")
    path = stick / "captua-offsite.txt"
    path.write_text(file_text())
    refused = refusal(offsite_settings.import_stick, db_session, stick, base=stick.parent, replace=False,
                      actor="admin1")
    assert refused.code == "saved_credential_unusable" and refused.detail()["stickFile"] == "kept"
    assert "replace" in refused.detail()["message"]
    assert path.exists() and wired.restarts == []
    assert (wired.unit / "data" / "rclone.conf").read_text().endswith("key = WRONG-SECRET\n")
    # The files changed and the latch is set: offsite jobs stay blocked until the restart.
    from app.core import restart_latch
    assert restart_latch.is_stale() and unit_activity.offsite_jobs_blocked()
    assert [entry for entry in wired.log if entry[0] == "start"] == []


def test_replace_with_the_same_key_id_writes_the_working_key(wired, db_session, stick, monkeypatch):
    from app.core import config, offsite_settings

    monkeypatch.setattr(offsite_settings, "_sync", lambda: None)
    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", TENANT)
    has_credential(wired.unit, KEY_ID, key="WRONG-SECRET")
    (stick / "captua-offsite.txt").write_text(file_text())
    result = offsite_settings.import_stick(db_session, stick, base=stick.parent, replace=True, actor="admin1")
    assert result == {"outcome": "restarting", "stickFile": "deleted"}
    assert wired.script.calls[0].argv[-1] == "--replace-credential"
    assert (wired.unit / "data" / "rclone.conf").read_text().endswith(f"key = {APP_KEY}\n")


def _already_saved(wired, db_session, monkeypatch, key=APP_KEY):
    """The unit as a first save of the same values left it, with the restart that followed."""
    from app.core import config, offsite_settings, restart_latch, unit_activity

    offsite_settings.save_form(db_session, form(), replace=False, actor="admin1")
    if key != APP_KEY:
        has_credential(wired.unit, KEY_ID, key=key)
    restart_latch._reset_for_tests()
    unit_activity._reset_for_tests()
    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", TENANT)
    wired.log.clear()
    wired.restarts.clear()
    wired.script.calls.clear()


def test_an_unchanged_save_is_verified_and_resumes_without_a_restart(wired, db_session, stick, monkeypatch):
    from app.core import offsite_settings, restart_latch

    monkeypatch.setattr(offsite_settings, "_sync", lambda: None)
    _already_saved(wired, db_session, monkeypatch)
    before = (wired.unit / "data" / "rclone.conf").read_bytes(), wired.script.offsite_env.read_bytes()
    (stick / "captua-offsite.txt").write_text(file_text())
    result = offsite_settings.import_stick(db_session, stick, base=stick.parent, replace=False, actor="admin1")
    assert result == {"outcome": "unchanged", "stickFile": "deleted"}
    assert ((wired.unit / "data" / "rclone.conf").read_bytes(), wired.script.offsite_env.read_bytes()) == before
    assert wired.script.ran and wired.restarts == [] and not restart_latch.is_stale()
    assert len([entry for entry in wired.log if entry[0] == "start"]) == 3
    assert wired.lister.calls[-1].config_path == wired.unit / "data" / "rclone.conf"


def test_an_unchanged_save_whose_kept_key_cannot_list_is_refused(wired, db_session, monkeypatch):
    from app.core import offsite_settings

    _already_saved(wired, db_session, monkeypatch, key="WRONG-SECRET")
    refused = refusal(offsite_settings.save_form, db_session, form(), replace=False, actor="admin1")
    assert refused.code == "saved_credential_unusable"
    assert wired.restarts == []


# ---------------------------------------------------------------------------
# The stick's copy is deleted only when it is the file that was read
# ---------------------------------------------------------------------------

def test_a_stick_copy_replaced_during_the_save_is_not_deleted(wired, db_session, stick, monkeypatch):
    from app.core import offsite_settings

    monkeypatch.setattr(offsite_settings, "_sync", lambda: None)
    path = stick / "captua-offsite.txt"
    path.write_text(file_text())

    def swap():
        path.rename(stick / "original.txt")
        path.write_text("# another file\n")

    wired.script.during = swap
    result = offsite_settings.import_stick(db_session, stick, base=stick.parent, replace=False, actor="admin1")
    assert result == {"outcome": "restarting", "stickFile": "not_deleted"}
    assert path.read_text() == "# another file\n" and (stick / "original.txt").read_text() == file_text()


# ---------------------------------------------------------------------------
# Key directories in memory only, and abandoned ones removed
# ---------------------------------------------------------------------------

def test_the_flow_refuses_without_a_memory_file_system(wired, db_session, monkeypatch):
    from app.core import offsite_settings, provision_runner

    monkeypatch.setattr(provision_runner, "_is_tmpfs", lambda path, mounts=None: False)
    refused = refusal(offsite_settings.save_form, db_session, form(), replace=False, actor="admin1")
    assert (refused.code, refused.status) == ("no_memory_directory", 409)
    assert not wired.script.ran and wired.lister.calls == []


def test_tmpfs_is_read_from_the_mount_table(tmp_path):
    from app.core import provision_runner

    table = tmp_path / "mounts"
    table.write_text("proc /proc proc rw 0 0\ntmpfs /dev/shm tmpfs rw,nosuid,nodev 0 0\n"
                     "/dev/sda1 /var/lib/dtk ext4 rw 0 0\n")
    assert provision_runner._is_tmpfs(Path("/dev/shm"), mounts=table) is True
    assert provision_runner._is_tmpfs(Path("/var/lib/dtk"), mounts=table) is False
    assert provision_runner._is_tmpfs(Path("/dev/shm"), mounts=tmp_path / "absent") is False


def test_memory_dir_is_dev_shm_only_when_it_is_tmpfs(unit, monkeypatch):
    from app.core import provision_runner

    assert provision_runner.memory_dir() == unit / "keys"
    monkeypatch.setattr(provision_runner, "_is_tmpfs", lambda path, mounts=None: False)
    assert provision_runner.memory_dir() is None
    monkeypatch.setattr(provision_runner, "_is_tmpfs", lambda path, mounts=None: True)
    monkeypatch.setattr(provision_runner, "DEV_SHM", unit / "absent")
    assert provision_runner.memory_dir() is None


def test_abandoned_key_directories_are_removed(tmp_path, monkeypatch):
    from app.core import provision_runner

    root = tmp_path / "shm"
    root.mkdir()
    old = time.time() - provision_runner.ABANDONED_AFTER_SECONDS - 60
    made = {}
    for name in ("captua-offsite-old", "captua-pairing-old", "captua-offsite-recent", "other-old"):
        made[name] = root / name
        made[name].mkdir()
        (made[name] / "keys").write_text("KEY_ID=fake\n")
        if name.endswith("old"):
            os.utime(made[name], (old, old))
    (root / "captua-offsite-file").write_text("not a directory")
    os.utime(root / "captua-offsite-file", (old, old))
    live = provision_runner.private_directory("captua-offsite-", root)
    os.utime(live, (old, old))
    proc = tmp_path / "proc"
    proc.mkdir()
    try:
        removed = provision_runner.sweep_abandoned(root, proc=proc)
        assert sorted(p.name for p in removed) == ["captua-offsite-old", "captua-pairing-old"]
        assert not made["captua-offsite-old"].exists() and not made["captua-pairing-old"].exists()
        assert made["captua-offsite-recent"].exists() and made["other-old"].exists() and live.exists()
        assert (root / "captua-offsite-file").exists()
        monkeypatch.setattr(provision_runner, "_uid", lambda: os.getuid() + 1)
        os.utime(made["captua-offsite-recent"], (old, old))
        assert provision_runner.sweep_abandoned(root, proc=proc) == []
    finally:
        provision_runner.remove_private_directory(live)


def test_the_sweep_runs_once_per_process_at_the_first_key_directory(tmp_path, monkeypatch):
    from app.core import provision_runner

    swept = []
    monkeypatch.setattr(provision_runner, "_swept", False)
    monkeypatch.setattr(provision_runner, "sweep_abandoned", lambda root: swept.append(root) or [])
    first = provision_runner.private_directory("captua-offsite-", tmp_path)
    second = provision_runner.private_directory("captua-offsite-", tmp_path)
    provision_runner.remove_private_directory(first)
    provision_runner.remove_private_directory(second)
    assert swept == [tmp_path] and not first.exists() and not second.exists()


# ---------------------------------------------------------------------------
# The request models
# ---------------------------------------------------------------------------

def test_the_request_and_value_models_hold_no_secret_in_repr_or_str():
    from app.api.system import OffsiteForm

    model = OffsiteForm(bucketName=BUCKET, applicationKeyId=KEY_ID, applicationKey=APP_KEY, tenant=TENANT,
                        cataloguePublicKey=RECIPIENT)
    found = values()
    for shown in (repr(model), str(model), repr(found), str(found)):
        assert APP_KEY not in shown and KEY_ID not in shown


@pytest.mark.parametrize("present", [True, False])
def test_the_backend_sweeps_dev_shm_at_start_when_it_is_a_directory(tmp_path, monkeypatch, present):
    """The lifespan's own call, with the stubs test_backend_shutdown uses to run it without a database."""
    import asyncio

    import app.core.db as core_db
    import app.core.device_id as device_id
    import app.core.storage_ops as storage_ops
    import app.core.unit_mode as unit_mode
    import capture.service as capture_service
    from app import main as app_main
    from app.core import provision_runner

    monkeypatch.setattr(app_main, "init_db", lambda: None)
    monkeypatch.setattr(app_main, "assert_schema_at_head", lambda: None)
    monkeypatch.setattr(core_db, "SessionLocal", lambda: type("S", (), {"close": lambda self: None})())
    monkeypatch.setattr(storage_ops, "reconcile_pending_rename", lambda db: None)
    monkeypatch.setattr(unit_mode, "initialise", lambda: None)
    monkeypatch.setattr(device_id, "initialise", lambda: None)
    monkeypatch.setattr(capture_service, "shutdown_backend", lambda: None)
    shm = tmp_path / "shm"
    if present:
        shm.mkdir()
    monkeypatch.setattr(provision_runner, "DEV_SHM", shm)
    swept = []
    monkeypatch.setattr(provision_runner, "sweep_abandoned", lambda root: swept.append(root) or [])

    async def serve():
        async with app_main.lifespan(app_main.app):
            pass

    asyncio.run(serve())
    assert swept == ([shm] if present else [])


@pytest.mark.parametrize("outcome_mode", ["fail", "nothing", "write_fail", "wrong_key"])
def test_offsite_jobs_are_unblocked_only_when_nothing_was_written(wired, db_session, monkeypatch, outcome_mode):
    from app.core import offsite_settings, restart_latch, unit_activity

    monkeypatch.delenv("CAPTUA_OFFSITE_DUMP_RECIPIENT", raising=False)
    wired.script.mode = outcome_mode
    refusal(offsite_settings.save_form, db_session, form(), replace=False, actor="admin1")
    written = outcome_mode in ("write_fail", "wrong_key")
    assert restart_latch.is_stale() is written
    assert unit_activity.offsite_jobs_blocked() is written


# ---------------------------------------------------------------------------
# A pairing worker that cannot start
# ---------------------------------------------------------------------------

def test_a_pairing_session_whose_worker_cannot_start_is_never_left_running(wired, db_session, monkeypatch):
    import types

    from app.core import fisqua_pairing, offsite_settings

    class NoStart(threading.Thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    fake_threading = types.SimpleNamespace(**{name: getattr(threading, name) for name in dir(threading)
                                              if not name.startswith("__")})
    fake_threading.Thread = NoStart
    reg = _registry(db_session, monkeypatch)
    monkeypatch.setattr(fisqua_pairing, "threading", fake_threading)
    with pytest.raises(RuntimeError):
        reg.begin("Unidad 1", start_thread=True)
    session = reg.current()
    assert session is None or not session.running
    monkeypatch.setattr(fisqua_pairing, "threading", threading)
    assert offsite_settings.save_form(db_session, form(), replace=False, actor="admin1")["outcome"] == "restarting"


# ---------------------------------------------------------------------------
# A key directory another process still refers to is not abandoned
# ---------------------------------------------------------------------------

def _aged(directory):
    from app.core import provision_runner

    old = time.time() - provision_runner.ABANDONED_AFTER_SECONDS - 60
    os.utime(directory, (old, old))


def _fake_proc(root, pid, *, cwd=None, fd=None, environ=b""):
    entry = root / str(pid)
    (entry / "fd").mkdir(parents=True)
    os.symlink(cwd or "/", entry / "cwd")
    if fd is not None:
        os.symlink(fd, entry / "fd" / "3")
    (entry / "environ").write_bytes(environ)
    return entry


@pytest.mark.parametrize("how", ["cwd", "fd", "dtk_env", "captua_env", "none"])
def test_a_directory_a_process_refers_to_is_not_swept(tmp_path, how):
    """A /proc of our own making: each way a process can refer to the directory keeps it."""
    from app.core import provision_runner

    shm = tmp_path / "shm"
    held = shm / "captua-offsite-held"
    held.mkdir(parents=True)
    (held / "offsite.keys").write_text("KEY_ID=fake\n")
    _aged(held)
    proc = tmp_path / "proc"
    key_file = os.fsencode(str(held / "offsite.keys"))
    _fake_proc(proc, 101, cwd=str(held) if how == "cwd" else None,
               fd=str(held / "offsite.keys") if how == "fd" else None,
               environ=(b"PATH=/bin\0DTK_OFFSITE_KEY_FILE=" + key_file + b"\0") if how == "dtk_env"
               else (b"CAPTUA_OFFSITE_KEY_FILE=" + key_file + b"\0") if how == "captua_env" else b"PATH=/bin\0")
    removed = provision_runner.sweep_abandoned(shm, proc=proc)
    assert (removed == [held.absolute()]) is (how == "none")
    assert held.exists() is (how != "none")


def test_a_process_of_another_user_does_not_count(tmp_path, monkeypatch):
    from app.core import provision_runner

    shm = tmp_path / "shm"
    held = shm / "captua-offsite-held"
    held.mkdir(parents=True)
    _aged(held)
    proc = tmp_path / "proc"
    _fake_proc(proc, 101, cwd=str(held))
    # The directory is ours; the process entry is reported as another user's.
    monkeypatch.setattr(provision_runner, "_proc_uid", lambda entry: os.geteuid() + 1)
    assert provision_runner.sweep_abandoned(shm, proc=proc) == [held.absolute()]


def test_nothing_is_swept_when_processes_cannot_be_read(tmp_path):
    from app.core import provision_runner

    shm = tmp_path / "shm"
    old = shm / "captua-offsite-old"
    old.mkdir(parents=True)
    _aged(old)
    assert provision_runner.sweep_abandoned(shm, proc=tmp_path / "no-proc") == []
    assert old.exists()


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs Linux /proc; the unit has it, macOS does not")
@pytest.mark.parametrize("how", ["cwd", "fd", "env"])
def test_a_live_child_process_holding_the_directory_keeps_it(tmp_path, how):
    import sys

    from app.core import provision_runner

    shm = tmp_path / "shm"
    held = shm / "captua-offsite-held"
    held.mkdir(parents=True)
    key_file = held / "offsite.keys"
    key_file.write_text("KEY_ID=fake\n")
    _aged(held)
    code = {"cwd": "import time; time.sleep(30)",
            "fd": f"import time; f = open({str(key_file)!r}); time.sleep(30)",
            "env": "import time; time.sleep(30)"}[how]
    env = dict(os.environ, DTK_OFFSITE_KEY_FILE=str(key_file)) if how == "env" else None
    child = subprocess.Popen([sys.executable, "-c", code], cwd=str(held) if how == "cwd" else None, env=env)
    try:
        deadline = time.time() + 5
        while how == "fd" and time.time() < deadline and not any(
                os.path.realpath(f"/proc/{child.pid}/fd/{fd}") == os.path.realpath(key_file)
                for fd in os.listdir(f"/proc/{child.pid}/fd")):
            time.sleep(0.05)
        assert provision_runner.sweep_abandoned(shm) == []
        assert held.exists()
    finally:
        child.kill()
        child.wait()
    assert provision_runner.sweep_abandoned(shm) == [held.absolute()]


def _held_and_proc(tmp_path):
    shm = tmp_path / "shm"
    held = shm / "captua-offsite-held"
    held.mkdir(parents=True)
    _aged(held)
    return shm, held, tmp_path / "proc"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_a_process_whose_environment_cannot_be_read_keeps_the_directory(tmp_path):
    from app.core import provision_runner

    shm, held, proc = _held_and_proc(tmp_path)
    entry = _fake_proc(proc, 101, environ=b"PATH=/bin\0")
    (entry / "environ").chmod(0)
    try:
        assert provision_runner._referenced(held, proc) is None
        assert provision_runner.sweep_abandoned(shm, proc=proc) == []
        assert held.exists()
    finally:
        (entry / "environ").chmod(0o600)


@pytest.mark.parametrize("missing", ["cwd", "fd", "environ"])
def test_a_process_still_present_whose_entry_cannot_be_read_keeps_the_directory(tmp_path, missing):
    import shutil

    from app.core import provision_runner

    shm, held, proc = _held_and_proc(tmp_path)
    entry = _fake_proc(proc, 101, environ=b"PATH=/bin\0")
    target = entry / missing
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink()
    assert provision_runner._referenced(held, proc) is None
    assert provision_runner.sweep_abandoned(shm, proc=proc) == []


def test_a_process_whose_uid_cannot_be_read_keeps_the_directory(tmp_path, monkeypatch):
    from app.core import provision_runner

    shm, held, proc = _held_and_proc(tmp_path)
    _fake_proc(proc, 101)

    def denied(entry):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(provision_runner, "_proc_uid", denied)
    assert provision_runner._referenced(held, proc) is None


@pytest.mark.parametrize("stage", ["uid", "environ"])
def test_a_process_that_exits_during_the_scan_is_skipped(tmp_path, monkeypatch, stage):
    import shutil

    from app.core import provision_runner

    shm, held, proc = _held_and_proc(tmp_path)
    gone = _fake_proc(proc, 102, environ=b"PATH=/bin\0")
    real_uid = provision_runner._proc_uid

    def vanishing(entry):
        if entry == gone:
            if stage == "uid":
                shutil.rmtree(gone)
                raise FileNotFoundError(2, "No such file or directory")
            answer = real_uid(entry)
            # Gone after its uid is read: the rest of its entry is missing.
            shutil.rmtree(gone)
            return answer
        return real_uid(entry)

    monkeypatch.setattr(provision_runner, "_proc_uid", vanishing)
    assert provision_runner._referenced(held, proc) is False
    assert provision_runner.sweep_abandoned(shm, proc=proc) == [held.absolute()]


def test_a_descriptor_closed_during_the_scan_is_skipped(tmp_path, monkeypatch):
    """Listed, then closed before its link is read, as the scan's own descriptors are in /proc/self/fd."""
    from app.core import provision_runner

    shm, held, proc = _held_and_proc(tmp_path)
    entry = _fake_proc(proc, 101, environ=b"PATH=/bin\0")
    real_listdir = os.listdir

    def listdir(path):
        names = real_listdir(path)
        return names + ["9"] if str(path) == str(entry / "fd") else names

    monkeypatch.setattr(provision_runner.os, "listdir", listdir)
    assert provision_runner._referenced(held, proc) is False


def _stat_record(pid, name, state, threads):
    """A /proc/<pid>/stat record: field 3 the state, field 20 num_threads, after the name in parentheses."""
    after = [state, "1", str(pid), str(pid), "0", "-1", "4194572", "0", "0", "0", "0", "0", "0", "0", "0",
             "20", "0", threads, "0", "12345"]
    return f"{pid} ({name}) " + " ".join(after) + "\n"


def _zombie(proc, pid, *, state="Z", threads="1", name="provision (x)", stat=True):
    import shutil

    entry = _fake_proc(proc, pid, environ=b"")
    (entry / "cwd").unlink()
    shutil.rmtree(entry / "fd")
    if stat:
        (entry / "stat").write_bytes(_stat_record(pid, name, state, threads).encode())
    (entry / "status").write_bytes(f"Name:\t{name}\nState:\t{state}\nThreads:\t{threads}\n".encode())
    return entry


def test_an_exited_unreaped_process_is_skipped(tmp_path):
    """A zombie whose whole thread group has exited (num_threads 1 in its stat) holds nothing and runs nothing."""
    from app.core import provision_runner

    shm, held, proc = _held_and_proc(tmp_path)
    _zombie(proc, 103)
    assert provision_runner._referenced(held, proc) is False


@pytest.mark.parametrize("state,threads,stat", [("Z", "2", True), ("Z", "1", False), ("S", "1", True)])
def test_a_zombie_leader_with_live_threads_or_an_unreadable_stat_keeps_the_directory(tmp_path, state, threads, stat):
    """A Z leader can have live threads holding files; a stat that cannot be read is unknown too."""
    from app.core import provision_runner

    shm, held, proc = _held_and_proc(tmp_path)
    _zombie(proc, 103, state=state, threads=threads, stat=stat)
    assert provision_runner._referenced(held, proc) is None
    assert provision_runner.sweep_abandoned(shm, proc=proc) == []


def test_a_name_that_forges_a_thread_count_line_is_not_read_as_one(tmp_path):
    """The name `a\\rThreads:\\t1` becomes a line of its own when status is read as text; the count is the stat's."""
    from app.core import provision_runner

    shm, held, proc = _held_and_proc(tmp_path)
    _zombie(proc, 104, threads="2", name="a\rThreads:\t1")
    assert provision_runner._referenced(held, proc) is None


def test_an_access_error_on_the_existence_check_keeps_the_directory(tmp_path, monkeypatch):
    """A failed read with ENOENT means gone only when lstat of the entry says FileNotFoundError."""
    from app.core import provision_runner

    shm, held, proc = _held_and_proc(tmp_path)
    entry = _fake_proc(proc, 101, environ=b"PATH=/bin\0")
    (entry / "cwd").unlink()
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if str(path) == str(entry):
            raise PermissionError(13, "Permission denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(provision_runner.os, "lstat", lstat)
    assert provision_runner._referenced(held, proc) is None
