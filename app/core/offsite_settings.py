"""Offsite settings entered without a terminal (NEH-341): from a file on a USB stick, or from a form.

A unit that keeps an offsite copy without Fisqua needs a bucket, its key
and the catalogue's public key on the unit. An administrator brings them
in one of two ways, both through scripts/provision-rclone.sh, which stays
the only writer of the rclone config and offsite.env
(app.core.provision_runner runs it as the backend's own user):

* **The stick file**: `captua-offsite.txt` (STICK_FILE) at the top level
  of a drive mounted directly under the mounts base. The request's path is
  never resolved (open_stick): its parent must be the mounts base as
  written and its last part a single name; the base is opened
  O_DIRECTORY|O_NOFOLLOW, the name is opened relative to it the same way,
  and that directory must be a mount point (its device differs from the
  base's). It is held open until the save ends. The file, at most
  MAX_FILE_BYTES and a regular file the backend can read and delete, is
  read, and later deleted, relative to that descriptor, and deleted only
  when its name still refers to the file that was read (st_dev, st_ino);
  else `not_deleted`. Plain `KEY=value` lines; blank lines and lines starting
  with `#` are allowed; UTF-8, with or without a byte order mark. Each
  line is read as the script's key_file_value reads one: carriage returns
  removed, and spaces around the key, around `=` and after the value
  stripped. The keys are exactly KEYS, each exactly once. template_text is
  the file to fill in.
* **The form**: the same five values.

Each value is checked against the rule the script applies to it (the
bucket, the key id and the key as B2 names and tokens, the tenant as
Fisqua's slug, the public key as an age X25519 recipient; an age private
key anywhere is refused). A refusal names its code and, for one of KEYS,
the key, never a value; a line that is not KEY=value, or whose key is not
one of KEYS, is named by its number only, since a pasted secret could sit
where a key belongs.

**Saving** (save), in order; nothing is written until the script runs:

1. Admission (provision_runner.admit), first: taken under the pairing
   registry's lock, so that no pairing session can begin between the
   check and the admission, and held until the save ends. A running
   pairing session is `pairing_running`, another save
   `offsite_save_running`; pairing is refused with `offsite_save_running`
   while the save holds it (fisqua_pairing_policy.start_refusal).
2. The gates (_gate): the restart latch; a unit linked to Fisqua
   (`fisqua_linked`: it receives its offsite settings from Fisqua); /dev/shm
   as a memory file system for the key files (`no_memory_directory`); a
   capture or an offsite job in progress; the offsite hold; rclone; the
   script. Then the unit's own settings: a tenant other than the one it
   has is `other_tenant` (`--change-tenant` is never passed); a key id
   other than the one in its `[b2unit]` needs `replace` in the request,
   else `replace_required`, and with it the script must take
   `--replace-credential` (`script_too_old`).
3. A listing of `b2:<bucket>/<tenant>` with the given keys (list_bucket),
   through a temporary rclone config in a 0700 directory under /dev/shm,
   removed in `finally`. A failed listing is `listing_failed`. The script
   itself writes first and only warns when its own listing fails, so this
   listing is the check of the values given.
4. The schedulers are stopped and waited for, and offsite jobs blocked
   and waited for, as pairing does, so that no upload uses the rclone
   config while the script replaces it. This depends on these private
   names of app.core.fisqua_pairing_service, which stay there: _schedulers,
   _clock, STOP_BOUND_SECONDS, _stop_and_wait, _offsite_work, _wait_offsite
   and _nothing_written (_quiesce, save); and on the pairing registry's
   `_lock` (save).
5. The script (provision), with `--tenant`, `--dump-recipient`,
   `--config-path` and, when replacing, `--replace-credential`, and a key
   file of KEY_ID, APP_KEY and BUCKET. Its outcome is decided from
   evidence (provision_runner.run_with_evidence); verification (verify)
   reads a fresh Settings() for the tenant, `OFFSITE_REMOTE` =
   `b2unit:<bucket>`, the recipient and the config path, and the config's
   `[b2unit]` account for the key id; then the unit's own saved config
   must list `b2unit:<bucket>/<tenant>` (list_saved), since the script
   keeps a complete `[b2unit]` with the same key id, and the key in it,
   unless it is given `--replace-credential`. A failure is
   `saved_credential_unusable`. A run that exits 0 and changes nothing is
   verified the same way and is `unchanged`.
6. After a verified save: the stick's copy is deleted and the file system
   synced (import only). After `saved`, the backend restarts as after a
   link (fisqua_pairing_policy.restart_after_link), since settings are
   read once, at import; after `unchanged` the restart latch is clear and
   the schedulers run again. After any refusal, `saved_credential_unusable`
   included, offsite jobs are unblocked and the schedulers started again
   only when the restart latch is clear, the evidence that nothing was
   written; with it set they stay blocked and stopped until the restart.
   After any refusal the stick's copy is kept.

The application key reaches the two temporary files only: never a
response, a log record, an audit entry or an exception message.
Audited: `offsite_settings_saved` and `offsite_settings_refused`.

Version: 0.4.0
"""

import errno
import hmac
import logging
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

# As a module: tests rebuild the Settings object.
from app.core import config
from app.core import provision_runner as runner

logger = logging.getLogger(__name__)

STICK_FILE = "captua-offsite.txt"
MAX_FILE_BYTES = 4096
KEYS = ("BUCKET_NAME", "APPLICATION_KEY_ID", "APPLICATION_KEY", "TENANT", "CATALOGUE_PUBLIC_KEY")
LISTING_REMOTE = "b2"
LISTING_TIMEOUT_SECONDS = 60
KEY_FILE_NAME = "offsite.keys"
DIRECTORY_PREFIX = "captua-offsite-"
SAVED = "saved"
UNCHANGED = "unchanged"

# The script's rules (valid_bucket, valid_b2_token in provision-rclone.sh).
BUCKET_PATTERN = re.compile(r"[A-Za-z0-9.-]{6,63}")
B2_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_+/=-]{1,256}")
AGE_SECRET_KEY = "AGE-SECRET-KEY-"

FILE_MISSING = "file_missing"
FILE_NOT_REGULAR = "file_not_regular"
FILE_TOO_LARGE = "file_too_large"
FILE_UNREADABLE = "file_unreadable"
FILE_NOT_DELETABLE = "file_not_deletable"
FILE_NOT_TEXT = "file_not_text"
BAD_LINE = "bad_line"
UNKNOWN_KEY = "unknown_key"
DUPLICATE_KEY = "duplicate_key"
MISSING_KEY = "missing_key"
EMPTY_VALUE = "empty_value"
BAD_VALUE = "bad_value"
LISTING_FAILED = "listing_failed"
OTHER_TENANT = "other_tenant"
REPLACE_REQUIRED = "replace_required"
FISQUA_LINKED = "fisqua_linked"
NOT_A_DRIVE = "not_a_drive"
NO_MEMORY_DIRECTORY = "no_memory_directory"
SAVED_CREDENTIAL_UNUSABLE = "saved_credential_unusable"

MESSAGES = {
    FILE_MISSING: f"There is no {STICK_FILE} at the top level of this drive.",
    FILE_NOT_REGULAR: f"{STICK_FILE} on this drive is not a regular file.",
    FILE_TOO_LARGE: f"{STICK_FILE} is larger than {MAX_FILE_BYTES} bytes.",
    FILE_UNREADABLE: f"Captua cannot read {STICK_FILE} on this drive.",
    FILE_NOT_DELETABLE: f"Captua cannot delete {STICK_FILE} from this drive after importing it, so it does not "
                        "import it. Use a drive formatted as FAT32 or exFAT.",
    FILE_NOT_TEXT: f"{STICK_FILE} is not plain text.",
    BAD_LINE: f"A line of {STICK_FILE} is neither a comment nor KEY=value.",
    UNKNOWN_KEY: f"A line of {STICK_FILE} has a key that the template does not have.",
    DUPLICATE_KEY: "A key is given more than once.",
    MISSING_KEY: "A key is missing.",
    EMPTY_VALUE: "A key has no value.",
    BAD_VALUE: "A value is not of the form its key needs.",
    LISTING_FAILED: "The unit could not list the bucket with these settings. Check the bucket name, the key ID, "
                    "the application key and the tenant, and that the unit is online.",
    OTHER_TENANT: "This unit already writes under a different tenant, which cannot be changed here.",
    REPLACE_REQUIRED: "This unit already has a different storage key. Confirm that the new key replaces it.",
    "pairing_running": "Pairing with Fisqua is in progress; try again when it ends.",
    "offsite_save_running": "Offsite settings are already being saved.",
    FISQUA_LINKED: "This unit is linked to Fisqua, which gives it its offsite settings.",
    NOT_A_DRIVE: "Only a drive Captua mounted can be read.",
    NO_MEMORY_DIRECTORY: "This unit has no memory file system at /dev/shm to hold the keys while they are "
                         "checked, so it does not save them.",
    SAVED_CREDENTIAL_UNUSABLE: "The settings were saved, but the storage key the unit keeps cannot list the "
                               "bucket. If the key ID has a new application key, confirm that it replaces "
                               "the stored one.",
    "restart_required": "Settings were written or may have been; restart the unit first.",
    "busy_capturing": "A capture is in progress; try again when it ends.",
    "offsite_job_running": "An offsite job is running; try again when it ends.",
    "offsite_held": "The offsite hold is in place; try again once it is removed.",
    "rclone_missing": "This unit lacks the program that uploads to the bucket.",
    "script_missing": "The unit's provisioning script was not found; the unit must be updated.",
    "script_too_old": "The unit's provisioning script cannot replace its storage key; the unit must be updated.",
    "uploads_still_running": "Uploads were still running, so nothing was saved; try again later.",
    "offsite_jobs_still_running": "An offsite job was still running, so nothing was saved; try again later.",
    "apply_failed": "The settings were not saved; nothing on the unit was changed.",
    "apply_unverified": "The settings may have been written but could not be checked; restart the unit.",
    "settings_overridden": "The settings were written, but other settings on the unit take precedence over "
                           "them; restart the unit and check its configuration.",
}
# Refusals of the values themselves; every other is a 409.
# Refusals of the path itself, answered 400.
PATH_CODES = frozenset({NOT_A_DRIVE})
VALUE_CODES = frozenset({FILE_MISSING, FILE_NOT_REGULAR, FILE_TOO_LARGE, FILE_UNREADABLE, FILE_NOT_DELETABLE,
                         FILE_NOT_TEXT, BAD_LINE, UNKNOWN_KEY, DUPLICATE_KEY, MISSING_KEY, EMPTY_VALUE, BAD_VALUE})
# The settings a save writes, and what each must read after it.
CHECKED = ("OFFSITE_TENANT", "OFFSITE_REMOTE", "OFFSITE_DUMP_RECIPIENT", "RCLONE_CONFIG")

TEMPLATE = """\
# Captua offsite settings
#
# Fill in the five values below, save this file as captua-offsite.txt at
# the top level of a USB stick (FAT32 or exFAT), plug the stick into the
# unit and import it from Settings. Write each value straight after the
# equals sign, with no quotes and no spaces. Lines that start with # are
# comments and are ignored.
#
# This file holds a secret key. After a successful import the unit deletes
# it from the stick. Do not keep another copy on a stick that travels.
#
# BUCKET_NAME: the name of the Backblaze B2 bucket the unit copies to.
BUCKET_NAME=
# APPLICATION_KEY_ID: the key ID of the B2 application key the unit
# writes with.
APPLICATION_KEY_ID=
# APPLICATION_KEY: the secret application key that goes with that key ID.
APPLICATION_KEY=
# TENANT: the folder in the bucket this unit writes under: lowercase
# letters, digits and single hyphens, starting with a letter (for example,
# ahr).
TENANT=
# CATALOGUE_PUBLIC_KEY: the age public key, starting with age1, that the
# copy of the catalogue is encrypted to. Never the private key.
CATALOGUE_PUBLIC_KEY=
"""

class Refused(Exception):
    """A refusal: its code, and the key or line number it concerns, never a value."""

    def __init__(self, code: str, *, key: Optional[str] = None, line: Optional[int] = None):
        super().__init__(code)
        self.code, self.key, self.line = code, key, line
        self.stick_file: Optional[str] = None

    @property
    def status(self) -> int:
        if self.code in PATH_CODES:
            return 400
        return 422 if self.code in VALUE_CODES else 409

    def detail(self) -> dict:
        body = {"code": self.code, "message": MESSAGES.get(self.code, "The offsite settings were not saved.")}
        if self.key is not None:
            body["key"] = self.key
        if self.line is not None:
            body["line"] = self.line
        if self.stick_file is not None:
            body["stickFile"] = self.stick_file
        return body

    def __repr__(self) -> str:
        return f"Refused({self.code!r}, key={self.key!r}, line={self.line!r})"


@dataclass(frozen=True, repr=False)
class OffsiteValues:
    """The five values, checked. Its repr and str never hold the application key."""

    bucket: str
    key_id: str
    application_key: str
    tenant: str
    recipient: str

    def __repr__(self) -> str:
        return f"OffsiteValues(bucket={self.bucket!r}, tenant={self.tenant!r})"

    __str__ = __repr__


# ---------------------------------------------------------------------------
# The values
# ---------------------------------------------------------------------------

def template_text() -> str:
    return TEMPLATE


def _valid_tenant(value: str) -> bool:
    from app.core.image_upload import TENANT_MAX_LENGTH, TENANT_PATTERN, TENANT_RESERVED

    return (len(value) <= TENANT_MAX_LENGTH and TENANT_PATTERN.fullmatch(value) is not None
            and value not in TENANT_RESERVED)


def _valid_recipient(value: str) -> bool:
    """An age X25519 public key, as the catalogue dump reads it (catalogue_backup.dump_recipient)."""
    if not value.lower().startswith("age1") or B2_TOKEN_PATTERN.fullmatch(value) is None:
        return False
    try:
        import pyrage
    except ImportError:
        return False
    try:
        pyrage.x25519.Recipient.from_str(value)
    except Exception:
        return False
    return True


_RULES = {
    "BUCKET_NAME": lambda v: BUCKET_PATTERN.fullmatch(v) is not None,
    "APPLICATION_KEY_ID": lambda v: B2_TOKEN_PATTERN.fullmatch(v) is not None,
    "APPLICATION_KEY": lambda v: B2_TOKEN_PATTERN.fullmatch(v) is not None,
    "TENANT": _valid_tenant,
    "CATALOGUE_PUBLIC_KEY": _valid_recipient,
}


def check_values(found: dict) -> OffsiteValues:
    """The five values, each checked against its key's rule, in KEYS order. Refused: `missing_key`, `empty_value`, `bad_value`."""
    for key in KEYS:
        value = found.get(key)
        if not isinstance(value, str):
            raise Refused(MISSING_KEY, key=key)
        if not value:
            raise Refused(EMPTY_VALUE, key=key)
        if AGE_SECRET_KEY in value.upper() or not _RULES[key](value):
            raise Refused(BAD_VALUE, key=key)
    return OffsiteValues(found["BUCKET_NAME"], found["APPLICATION_KEY_ID"], found["APPLICATION_KEY"],
                         found["TENANT"], found["CATALOGUE_PUBLIC_KEY"])


def parse_text(text: str) -> OffsiteValues:
    """The values of a stick file's text. Refused: `bad_line` (by number), `unknown_key`, `duplicate_key`, and check_values' refusals."""
    found: dict = {}
    for number, raw in enumerate(text.removeprefix("﻿").split("\n"), start=1):
        line = raw.replace("\r", "")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in line:
            raise Refused(BAD_LINE, line=number)
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if name not in KEYS:
            raise Refused(UNKNOWN_KEY, line=number)
        if name in found:
            raise Refused(DUPLICATE_KEY, key=name)
        found[name] = value
    return check_values(found)


def form_values(fields: dict) -> OffsiteValues:
    """The form's five values, keyed as the stick file keys them, each stripped of surrounding spaces as a file line is."""
    return check_values({key: value.strip() if isinstance(value, str) else value for key, value in fields.items()})


class StickDirectory:
    """A drive's top-level directory, held open, and the identity of the stick file read from it."""

    def __init__(self, fd: int):
        self.fd = fd
        self.identity: Optional[tuple] = None

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _mounted(fd: int, base_fd: int) -> bool:
    """Whether the directory open at `fd` is on a file system other than that of the base open at `base_fd`: a mount point."""
    return os.fstat(fd).st_dev != os.fstat(base_fd).st_dev


def open_stick(mountpoint, base) -> StickDirectory:
    """The top-level directory of a drive mounted directly under `base`. Refused: `not_a_drive` (400).

    `mountpoint` is taken as written and never resolved: its parent must
    be `base` as written, its last part one name (not `.` or `..`). The
    base is opened O_DIRECTORY|O_NOFOLLOW, the name is opened relative to
    it the same way, and that directory must be a mount point (_mounted).
    """
    text, prefix = str(mountpoint), str(base) + os.sep
    name = text[len(prefix):] if text.startswith(prefix) else ""
    if name in ("", ".", "..") or os.sep in name or "\x00" in name:
        raise Refused(NOT_A_DRIVE)
    try:
        base_fd = os.open(str(base), _DIRECTORY_FLAGS)
    except OSError:
        raise Refused(NOT_A_DRIVE) from None
    try:
        try:
            fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=base_fd)
        except OSError:
            raise Refused(NOT_A_DRIVE) from None
        try:
            if not _mounted(fd, base_fd):
                raise Refused(NOT_A_DRIVE)
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(base_fd)
    return StickDirectory(fd)


def read_stick(stick: StickDirectory) -> OffsiteValues:
    """The values of STICK_FILE in the open directory, its identity recorded on `stick`.

    Refused: `file_missing`; `file_not_regular` (a symlink, a directory or
    anything else that is not a regular file); `file_too_large` (over
    MAX_FILE_BYTES); `file_not_deletable` (the backend cannot write the
    drive's top level, so it could not delete the file after a save);
    `file_unreadable`; `file_not_text`; and parse_text's refusals.
    """
    try:
        found = os.stat(STICK_FILE, dir_fd=stick.fd, follow_symlinks=False)
    except FileNotFoundError:
        raise Refused(FILE_MISSING) from None
    except OSError:
        raise Refused(FILE_UNREADABLE) from None
    if not stat.S_ISREG(found.st_mode):
        raise Refused(FILE_NOT_REGULAR)
    if found.st_size > MAX_FILE_BYTES:
        raise Refused(FILE_TOO_LARGE)
    if not os.access(".", os.W_OK | os.X_OK, dir_fd=stick.fd):
        raise Refused(FILE_NOT_DELETABLE)
    try:
        fd = os.open(STICK_FILE, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=stick.fd)
    except OSError as exc:
        raise Refused(FILE_NOT_REGULAR if exc.errno == errno.ELOOP else FILE_UNREADABLE) from None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise Refused(FILE_NOT_REGULAR)
        data = b""
        while len(data) <= MAX_FILE_BYTES:
            chunk = os.read(fd, MAX_FILE_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        raise Refused(FILE_UNREADABLE) from None
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        raise Refused(FILE_TOO_LARGE)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused(FILE_NOT_TEXT) from None
    if "\x00" in text:
        raise Refused(FILE_NOT_TEXT)
    values = parse_text(text)
    stick.identity = (opened.st_dev, opened.st_ino)
    return values


# ---------------------------------------------------------------------------
# What the unit has
# ---------------------------------------------------------------------------

def current() -> dict:
    """GET /system/offsite's body, from this process's settings and the rclone config. Never the application key."""
    from app.core import restart_latch

    settings = config.settings
    remote = settings.OFFSITE_REMOTE.strip()
    prefix = f"{runner.WRITE_REMOTE}:"
    bucket = remote[len(prefix):] if remote.startswith(prefix) else ""
    key_id = runner.write_key_id_in(runner.rclone_config_path()) if runner.has_credential() else None
    tenant = settings.OFFSITE_TENANT.strip()
    recipient = settings.OFFSITE_DUMP_RECIPIENT.strip()
    return {"configured": bool(remote and tenant and recipient and key_id),
            "bucketName": bucket or None, "tenant": tenant or None, "applicationKeyId": key_id or None,
            "cataloguePublicKey": recipient or None, "restartRequired": restart_latch.is_stale()}


def saving() -> bool:
    """Whether an offsite settings save holds the admission to run the script."""
    return runner.holder() == runner.OFFSITE_SAVE


def _pairing_running(registry) -> bool:
    session = registry.current()
    return session is not None and session.running


def _gate(values: OffsiteValues, replace: bool) -> tuple:
    """Everything checked, once admitted, before anything runs. Answers (script, whether to pass --replace-credential); raises Refused."""
    from app.core import fisqua_outbox, offsite_hold, restart_latch, unit_activity

    if restart_latch.is_stale():
        raise Refused("restart_required")
    if fisqua_outbox.fisqua_configured():
        raise Refused(FISQUA_LINKED)
    if runner.memory_dir() is None:
        raise Refused(NO_MEMORY_DIRECTORY)
    if unit_activity.in_progress(unit_activity.CAMERA):
        raise Refused("busy_capturing")
    if unit_activity.in_progress(unit_activity.OFFSITE_JOB):
        raise Refused("offsite_job_running")
    if offsite_hold.is_held():
        raise Refused("offsite_held")
    if not runner.rclone_available():
        raise Refused("rclone_missing")
    script = runner.provision_script()
    if not script.is_file():
        raise Refused("script_missing")
    tenant = config.settings.OFFSITE_TENANT.strip()
    if tenant and tenant != values.tenant:
        raise Refused(OTHER_TENANT)
    flag = False
    if runner.has_credential():
        found = runner.write_key_id_in(runner.rclone_config_path())
        same = found is not None and hmac.compare_digest(found.encode(), values.key_id.encode())
        if not same and not replace:
            raise Refused(REPLACE_REQUIRED)
        flag = replace
    if flag and not runner.script_supports_replace(script):
        raise Refused("script_too_old")
    return script, flag


# ---------------------------------------------------------------------------
# The listing
# ---------------------------------------------------------------------------

def _memory_root() -> Path:
    root = runner.memory_dir()
    if root is None:
        raise Refused(NO_MEMORY_DIRECTORY)
    return root


def _lsf(config_path: Path, where: str, run: Callable, time_limit: float, what: str) -> bool:
    """Whether `rclone --config <config_path> lsf <where>`, with the script's probe flags, exits 0 within `time_limit`.

    Only PATH, HOME and LANG reach rclone, with no terminal; its output is
    discarded and only the exit code is logged.
    """
    argv = [config.settings.RCLONE_BIN, "--config", str(config_path), "--ask-password=false", "lsf",
            "--max-depth", "1", "--retries", "1", "--low-level-retries", "1", "--contimeout", "10s",
            "--timeout", "20s", where]
    try:
        result = run(argv, env=runner.plain_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, timeout=time_limit, check=False)
    except subprocess.TimeoutExpired:
        logger.warning("The listing of the bucket with %s did not finish in %s s", what, time_limit)
        return False
    except OSError as exc:
        logger.warning("The listing of the bucket with %s could not be run: %s", what, type(exc).__name__)
        return False
    if result.returncode != 0:
        logger.warning("The listing of the bucket with %s failed (exit code %s)", what, result.returncode)
        return False
    return True


def list_bucket(values: OffsiteValues, *, run: Callable = subprocess.run, key_root: Optional[Path] = None,
                time_limit: float = LISTING_TIMEOUT_SECONDS) -> bool:
    """Whether `rclone lsf b2:<bucket>/<tenant>` with the given keys exits 0 within `time_limit`.

    The keys are in a temporary config (remote LISTING_REMOTE), mode 0600,
    in a directory of its own, mode 0700, under /dev/shm
    (provision_runner.memory_dir; `no_memory_directory` without it),
    removed in `finally`.
    """
    directory = runner.private_directory(DIRECTORY_PREFIX, key_root or _memory_root())
    try:
        conf = directory / "rclone.conf"
        fd = runner.open_key_file(conf)
        try:
            runner.write_key_file(fd, f"[{LISTING_REMOTE}]\ntype = b2\naccount = {values.key_id}\n"
                                      f"key = {values.application_key}\n")
        finally:
            os.close(fd)
        return _lsf(conf, f"{LISTING_REMOTE}:{values.bucket}/{values.tenant}", run, time_limit, "the given keys")
    finally:
        runner.remove_private_directory(directory)


def list_saved(values: OffsiteValues, config_path: Path, *, run: Callable = subprocess.run,
               time_limit: float = LISTING_TIMEOUT_SECONDS) -> bool:
    """Whether the unit's own saved rclone config lists `b2unit:<bucket>/<tenant>`: the credential its uploads will use."""
    return _lsf(config_path, f"{runner.WRITE_REMOTE}:{values.bucket}/{values.tenant}", run, time_limit,
                "the unit's saved key")


# ---------------------------------------------------------------------------
# The script
# ---------------------------------------------------------------------------

def key_file_text(values: OffsiteValues) -> str:
    lines = (("KEY_ID", values.key_id), ("APP_KEY", values.application_key), ("BUCKET", values.bucket))
    return "".join(f"{name}={value}\n" for name, value in lines)


def script_argv(plan: runner.Plan, values: OffsiteValues) -> list:
    argv = [str(plan.script), "--tenant", values.tenant, "--dump-recipient", values.recipient,
            "--config-path", str(plan.config_path)]
    if plan.replace:
        argv.append(runner.REPLACE_FLAG)
    return argv


def verify(values: OffsiteValues, settings: Any, config_path: Path, env_file: Optional[Path] = None) -> Optional[str]:
    """None when each CHECKED setting reads as saved and the config's [b2unit] holds the key id.

    Else `settings_overridden` (a mismatched setting is set where it wins
    over offsite.env) or `apply_unverified`. Only setting names are logged.
    """
    expected = {"OFFSITE_TENANT": values.tenant, "OFFSITE_REMOTE": f"{runner.WRITE_REMOTE}:{values.bucket}",
                "OFFSITE_DUMP_RECIPIENT": values.recipient, "RCLONE_CONFIG": str(config_path)}
    wrong = [name for name in CHECKED
             if not hmac.compare_digest(runner.read_setting(settings, name).encode(), expected[name].encode())]
    if wrong:
        env_file = env_file if env_file is not None else Path(config.ENV_FILES[0])
        if runner.overridden(wrong, env_file):
            logger.warning("The offsite settings were written, but these settings are set where they win over "
                           "them: %s", ", ".join(wrong))
            return "settings_overridden"
        logger.warning("The offsite settings could not be verified: these settings do not read as written: %s",
                       ", ".join(wrong))
        return "apply_unverified"
    found = runner.write_key_id_in(config_path)
    if found is None or not hmac.compare_digest(found.encode(), values.key_id.encode()):
        logger.warning("The offsite settings could not be verified: %s does not hold the new key", config_path)
        return "apply_unverified"
    return None


def provision(values: OffsiteValues, *, replace: bool, script: Optional[Path] = None,
              settings_factory: Optional[Callable[[], Any]] = None, env_file: Optional[Path] = None,
              offsite_env: Optional[Path] = None, popen: Callable = subprocess.Popen,
              run: Callable = subprocess.run, key_root: Optional[Path] = None,
              time_limit: float = runner.SCRIPT_TIMEOUT_SECONDS, grace: float = runner.TERM_GRACE_SECONDS) -> str:
    """Run provision-rclone.sh with the values. Never raises (provision_runner.run_with_evidence).

    `saved` when the run changed the settings, `unchanged` when it exited
    0 and changed nothing, each only when verify passes and the saved
    config lists the bucket (list_saved, else `saved_credential_unusable`).
    """
    settings_factory = settings_factory or config.Settings
    try:
        plan = runner.plan(script, replace, offsite_env, before="saving offsite settings")
    except Exception as exc:
        logger.error("Offsite settings could not start the provisioning script (%s); nothing was written",
                     type(exc).__name__)
        return "apply_failed"
    if isinstance(plan, str):
        return plan
    def checked(fresh) -> Optional[str]:
        found = verify(values, fresh, plan.config_path, env_file)
        if found is not None:
            return found
        return None if list_saved(values, plan.config_path, run=run) else SAVED_CREDENTIAL_UNUSABLE

    return runner.run_with_evidence(
        plan,
        snapshot=lambda: runner.snapshot(settings_factory, CHECKED, runner.read_setting, plan.config_path,
                                         plan.offsite_env),
        verify=checked, success=SAVED, unchanged=UNCHANGED, label="Offsite settings",
        argv=lambda: script_argv(plan, values), key_text=lambda: key_file_text(values), popen=popen,
        key_dir=lambda: key_root or _memory_root(), prefix=DIRECTORY_PREFIX, key_file_name=KEY_FILE_NAME,
        time_limit=time_limit, grace=grace)


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def _quiesce() -> tuple:
    """Block offsite jobs, stop the schedulers and wait, as pairing's apply_link does. (refusal or None, stopped).

    On a refusal nothing was written: offsite jobs are unblocked and the
    schedulers whose workers exited run again.
    """
    from app.core import fisqua_pairing_service as pairing, unit_activity

    unit_activity.block_offsite_jobs()
    stopped: list = []
    try:
        schedulers = pairing._schedulers()
        deadline = pairing._clock() + pairing.STOP_BOUND_SECONDS
        stopped = pairing._stop_and_wait(schedulers, deadline)
        if len(stopped) < len(schedulers):
            logger.error("Uploads were still running after %s s; the offsite settings were not written",
                         pairing.STOP_BOUND_SECONDS)
            pairing._nothing_written(stopped)
            return "uploads_still_running", []
        offsite = pairing._offsite_work()
        if offsite.running() and not pairing._wait_offsite(offsite, deadline):
            logger.error("An offsite job was still running after %s s; the offsite settings were not written",
                         pairing.STOP_BOUND_SECONDS)
            pairing._nothing_written(stopped)
            return "offsite_jobs_still_running", []
    except Exception as exc:
        logger.error("The unit could not be prepared for the offsite settings (%s); nothing was written",
                     type(exc).__name__)
        pairing._nothing_written([s for s in stopped if not s.worker_alive()])
        return "apply_failed", []
    return None, stopped


def _remove(name: str, dir_fd: int) -> None:
    os.unlink(name, dir_fd=dir_fd)


def _sync() -> None:
    os.sync()


def _delete_stick_copy(stick: StickDirectory) -> str:
    """`deleted` (and the file system synced) when STICK_FILE is still the file that was read; else `not_deleted`."""
    try:
        now = os.stat(STICK_FILE, dir_fd=stick.fd, follow_symlinks=False)
    except OSError as exc:
        logger.error("%s could not be found on the drive after the save (%s)", STICK_FILE, type(exc).__name__)
        return "not_deleted"
    if not stat.S_ISREG(now.st_mode) or (now.st_dev, now.st_ino) != stick.identity:
        logger.error("%s on the drive is no longer the file that was read; it was not deleted", STICK_FILE)
        return "not_deleted"
    # The check and the unlink are two calls, and no call unlinks a name only if it still refers to a
    # given inode. Only another local process could replace the file between them, and such a process
    # is outside what this guards against: the backend's user (pi) has sudo for everything on a unit.
    try:
        _remove(STICK_FILE, stick.fd)
    except OSError as exc:
        logger.error("%s could not be deleted from the drive after the save (%s)", STICK_FILE, type(exc).__name__)
        return "not_deleted"
    try:
        _sync()
    except OSError as exc:
        logger.warning("The drive could not be synced after %s was deleted (%s)", STICK_FILE, type(exc).__name__)
    return "deleted"


def _admit():
    """The admission to run the script, taken under the pairing registry's lock. Refused: `pairing_running`, `offsite_save_running`."""
    from app.core import fisqua_pairing_service

    registry = fisqua_pairing_service.pairing_registry
    with registry._lock:
        try:
            return runner.admit(runner.OFFSITE_SAVE, pairing_running=lambda: _pairing_running(registry))
        except runner.Busy as exc:
            raise Refused(exc.code) from None


def save(db, values: OffsiteValues, *, replace: bool, actor: Optional[str], source: str,
         stick: Optional[StickDirectory] = None) -> dict:
    """Admit, gate, list, stop the schedulers, run the script, and after a verified save delete the stick's copy, audit and restart.

    Answers {outcome: `restarting` | `restart_needed` | `unchanged`,
    stickFile: `deleted` | `not_deleted` | None}; raises Refused otherwise.
    """
    from app.core import fisqua_pairing_policy, fisqua_pairing_service, restart_latch

    admission = _admit()
    try:
        script, flag = _gate(values, replace)
        if not list_bucket(values):
            raise Refused(LISTING_FAILED)
        refusal, stopped = _quiesce()
        if refusal is not None:
            raise Refused(refusal)
        try:
            outcome = provision(values, replace=flag, script=script)
        except Exception as exc:
            logger.error("Provisioning raised (%s); the unit must be restarted", type(exc).__name__)
            outcome = "apply_unverified"
        if outcome not in (SAVED, UNCHANGED):
            # Only with the latch clear is there evidence that nothing was written (provision_runner).
            if not restart_latch.is_stale():
                fisqua_pairing_service._nothing_written(stopped)
            raise Refused(outcome)
        stick_file = _delete_stick_copy(stick) if stick is not None else None
        _audit(db, "INFO", "offsite_settings_saved", actor, values.tenant,
               f"source={source} bucket={values.bucket} replaced={flag} outcome={outcome} stick_file={stick_file}")
        if outcome == UNCHANGED:
            fisqua_pairing_service._nothing_written(stopped)
            return {"outcome": UNCHANGED, "stickFile": stick_file}
        try:
            restart = fisqua_pairing_policy.restart_after_link()
        except Exception as exc:
            logger.error("The restart after the save raised (%s); the unit must be restarted", type(exc).__name__)
            restart = "restart_needed"
        return {"outcome": restart, "stickFile": stick_file}
    finally:
        admission.release()


def _audit(db, level: str, action: str, actor: Optional[str], subject: Optional[str], detail: str) -> None:
    from app.core.audit import log_event

    try:
        log_event(db, level=level, category="system", action=action, actor=actor, subject=subject, detail=detail)
    except Exception as exc:
        logger.error("The audit entry %s could not be written (%s)", action, type(exc).__name__)


def _refused(db, exc: Refused, actor: Optional[str], source: str) -> Refused:
    detail = f"source={source} code={exc.code}"
    if exc.key is not None:
        detail += f" key={exc.key}"
    if exc.line is not None:
        detail += f" line={exc.line}"
    _audit(db, "WARN", "offsite_settings_refused", actor, None, detail)
    return exc


def save_form(db, fields: dict, *, replace: bool, actor: Optional[str]) -> dict:
    """The form's route: the five values keyed as KEYS. Raises Refused, audited."""
    try:
        return save(db, form_values(fields), replace=replace, actor=actor, source="form")
    except Refused as exc:
        raise _refused(db, exc, actor, "form")


def import_stick(db, mountpoint: Path, *, base: Path, replace: bool, actor: Optional[str]) -> dict:
    """The stick's route: STICK_FILE on a drive mounted directly under `base`, deleted after a verified save.

    Raises Refused, audited; every refusal past open_stick carries stickFile `kept`.
    """
    try:
        stick = open_stick(mountpoint, base)
    except Refused as exc:
        raise _refused(db, exc, actor, "stick")
    try:
        values = read_stick(stick)
        return save(db, values, replace=replace, actor=actor, source="stick", stick=stick)
    except Refused as exc:
        exc.stick_file = "kept"
        raise _refused(db, exc, actor, "stick")
    finally:
        stick.close()
