"""Running scripts/provision-rclone.sh as the backend's own user, and deciding from evidence what it changed.

Both routes that put offsite settings on a unit run the superproject's
scripts/provision-rclone.sh, which writes the `[b2unit]` (and, for
Fisqua, `[b2read]`) remote and offsite.env and validates every value:
pairing with Fisqua (app.core.fisqua_pairing_apply) and the offsite
settings an administrator enters (app.core.offsite_settings). The script
is the only writer of those files. This module holds what the two share;
each caller supplies its own key file text, command line and
verification.

The script is found at `../scripts/provision-rclone.sh` from the working
directory (provision_script), the way app.core.version finds `../VERSION`:
the backend runs from `backend/` inside the superproject (scripts/start.sh).

**How it is run** (run_with_evidence):

* as the backend's own user, never with sudo: `pi` owns /var/lib/dtk and
  the files the script writes there (setup.sh), and running `pi`'s own
  checkout as `pi` adds no privilege;
* with only PATH, HOME, LANG=C.UTF-8 and DTK_OFFSITE_KEY_FILE in its
  environment (script_env), so that no inherited DTK_B2_*, RCLONE_* or
  SHELLOPTS reaches it;
* with only public values on its command line, `--config-path` always the
  rclone config the backend's own uploads use (rclone_config_path), and
  `--change-tenant` never;
* with the keys in a key file, mode 0600, in a directory of its own, mode
  0700, under /dev/shm when it exists, else under the system's temporary
  directory (key_file_root); the directory is removed in `finally`;
* with its stdout and stderr discarded: it can print a key id. Only its
  exit code is logged;
* in a process group of its own, for SCRIPT_TIMEOUT_SECONDS; then SIGTERM,
  so its traps can restore a credential it moved aside, and SIGKILL after
  TERM_GRACE_SECONDS (run_script).

A unit with a credential needs a script that takes `--replace-credential`
when the caller passes it (script_supports_replace, which reads the script
for the case label); an older script is `script_too_old` and is not run.

**Evidence, not exit codes.** Once the script has started, nothing is
assumed unchanged: run_with_evidence compares what the caller's snapshot
reads before and after the run, and its docstring states the rule.

**One admission** (admit, refusal): at most one piece of work that runs
the script is admitted at a time. An offsite settings save takes the
admission (admit) and holds it until it ends; a pairing session holds it
while it runs, read through `pairing_running` under the pairing
registry's lock, which also serializes a session's start
(app.core.offsite_settings.save). Each refusal's code is BUSY_CODES of
the work that holds it: `pairing_running`, `offsite_save_running`.

**Key directories** are made by private_directory and removed by
remove_private_directory. At the first one a process makes,
sweep_abandoned removes the directories under the same root that an
earlier process left (a SIGKILL skips `finally`): named with one of
KEY_DIRECTORY_PREFIXES, owned by this user, not made by this process,
untouched for ABANDONED_AFTER_SECONDS, longer than any run of the script
can last, and referred to by no process of this user (_referenced: an
open file or the working directory inside it, or DTK_OFFSITE_KEY_FILE or
CAPTUA_OFFSITE_KEY_FILE naming a path in it, read from /proc). A script
whose backend was killed keeps running in a session of its own, with no
watchdog, so age alone is no evidence that it has ended. Where /proc, or
any part of a living process's entry in it, cannot be read, the answer is
unknown and nothing is swept; the process that made it so is logged by
pid. On a host where other processes of the same user cannot be
inspected (Yama ptrace_scope 1 or more, as on Ubuntu), the sweep
therefore removes nothing: the chosen conservative outcome. Units run
Raspberry Pi OS, whose kernel has no Yama. The backend also sweeps
/dev/shm at start (app.main). memory_dir is /dev/shm when it is a tmpfs (or ramfs) directory,
else None; the offsite settings flow requires it, and pairing falls back
to the system's temporary directory (key_file_root).

Version: 0.4.0
"""

import errno
import hashlib
import logging
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from dotenv import dotenv_values

# As a module: tests rebuild the Settings object.
from app.core import config
from app.core import restart_latch

logger = logging.getLogger(__name__)

SCRIPT_NAME = "provision-rclone.sh"
REPLACE_FLAG = "--replace-credential"
SCRIPT_TIMEOUT_SECONDS = 15 * 60
# After SIGTERM at the time limit, how long the script has to restore and exit before SIGKILL.
TERM_GRACE_SECONDS = 30
DEV_SHM = Path("/dev/shm")
WRITE_REMOTE = "b2unit"
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
KEY_DIRECTORY_PREFIXES = ("captua-pairing-", "captua-offsite-")
# Longer than a run of the script can last: its time limit and two grace periods.
ABANDONED_AFTER_SECONDS = SCRIPT_TIMEOUT_SECONDS + 2 * TERM_GRACE_SECONDS
MOUNTS_TABLE = Path("/proc/self/mounts")
PROC = Path("/proc")
KEY_FILE_VARIABLES = (b"DTK_OFFSITE_KEY_FILE=", b"CAPTUA_OFFSITE_KEY_FILE=")
MEMORY_FS_TYPES = ("tmpfs", "ramfs")

PAIRING = "pairing"
OFFSITE_SAVE = "offsite_save"
BUSY_CODES = {PAIRING: "pairing_running", OFFSITE_SAVE: "offsite_save_running"}


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------

_admission_lock = threading.Lock()
_holder: Optional[str] = None


class Busy(Exception):
    """Admission is held: `holder` is the kind of work that holds it, `code` its refusal."""

    def __init__(self, holder: str):
        super().__init__(BUSY_CODES[holder])
        self.holder, self.code = holder, BUSY_CODES[holder]


class Admission:
    """The admission one piece of work holds. release() is idempotent."""

    def __init__(self, kind: str):
        self.kind = kind
        self._released = False

    def release(self) -> None:
        global _holder
        with _admission_lock:
            if not self._released and _holder == self.kind:
                _holder = None
            self._released = True


def admit(kind: str, *, pairing_running: Callable[[], bool]) -> Admission:
    """Take the admission for `kind`, or raise Busy naming what holds it. One lock decides.

    `pairing_running` answers whether a pairing session runs; the caller
    holds the pairing registry's lock while calling, so that no session
    can start between the answer and the admission.
    """
    global _holder
    with _admission_lock:
        if _holder is not None:
            raise Busy(_holder)
        if kind != PAIRING and pairing_running():
            raise Busy(PAIRING)
        _holder = kind
    return Admission(kind)


def refusal(kind: str) -> Optional[str]:
    """The code that refuses `kind` now: that of the other work holding the admission, or None."""
    with _admission_lock:
        return BUSY_CODES[_holder] if _holder is not None and _holder != kind else None


def holder() -> Optional[str]:
    with _admission_lock:
        return _holder


def provision_script(backend_dir: Optional[Path] = None) -> Path:
    """scripts/provision-rclone.sh in the superproject: `../scripts` from the backend's directory."""
    backend = Path(backend_dir) if backend_dir is not None else Path.cwd()
    return backend.parent / "scripts" / SCRIPT_NAME


def script_supports_replace(script: Path) -> bool:
    """Whether the script's argument loop has a `--replace-credential)` case label."""
    try:
        text = script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(line.strip().startswith(f"{REPLACE_FLAG})") for line in text.splitlines())


def rclone_available() -> bool:
    """Whether CAPTUA_RCLONE_BIN names an executable, as a path or on PATH."""
    binary = config.settings.RCLONE_BIN
    if "/" in binary:
        return os.path.isfile(binary) and os.access(binary, os.X_OK)
    return shutil.which(binary) is not None


def rclone_config_path() -> Path:
    """The rclone config the backend's uploads use (catalogue_backup.rclone_config_file); rclone.conf in the data directory when none is set. Absolute in both cases."""
    from app.core.catalogue_backup import absolute_from_backend, rclone_config_file

    return rclone_config_file() or absolute_from_backend(config.settings.data_dir / "rclone.conf")


def has_credential() -> bool:
    return rclone_config_path().exists()


def key_file_root() -> Path:
    """/dev/shm when it is a writable directory (memory, cleared at boot), else the system's temporary directory."""
    if DEV_SHM.is_dir() and os.access(DEV_SHM, os.W_OK | os.X_OK):
        return DEV_SHM
    return Path(tempfile.gettempdir())


def _is_tmpfs(path: Path, mounts: Optional[Path] = None) -> bool:
    """Whether the mount table has `path` as the mount point of a memory file system (MEMORY_FS_TYPES)."""
    try:
        text = (mounts or MOUNTS_TABLE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    target = os.path.realpath(path)
    found = False
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[1] == target:
            # The last entry for a mount point is the one in effect.
            found = fields[2] in MEMORY_FS_TYPES
    return found


def memory_dir() -> Optional[Path]:
    """/dev/shm when it is a writable tmpfs (or ramfs) directory, else None."""
    if DEV_SHM.is_dir() and os.access(DEV_SHM, os.W_OK | os.X_OK) and _is_tmpfs(DEV_SHM):
        return DEV_SHM
    return None


_live_lock = threading.Lock()
_live: set = set()
_swept = False


def _uid() -> int:
    return os.geteuid()


def _proc_uid(entry: Path) -> int:
    """The uid that owns a /proc entry. Raises OSError when it cannot be read."""
    return os.stat(entry, follow_symlinks=False).st_uid


def _inside(target: str, directory: str) -> bool:
    return target == directory or target.startswith(directory + os.sep)


def _exited(entry: Path) -> bool:
    """Whether the whole process at `entry` has exited and is not yet reaped.

    From one /proc/<pid>/stat record, read as bytes and split after the
    last `)` (the name before it may hold anything): field 3, the state,
    is `Z` and field 20, num_threads, is 1. A Z leader whose other threads
    still run can hold files, so any other count, or a record that cannot
    be read or parsed, is False.
    """
    try:
        record = (entry / "stat").read_bytes()
    except OSError:
        return False
    fields = record[record.rfind(b")") + 1:].split()
    # fields[0] is field 3 of the record, so field 20 is fields[17].
    if len(fields) < 18:
        return False
    return fields[0] == b"Z" and fields[17] == b"1"


def _gone(entry: Path, exc: OSError) -> bool:
    """Whether a read of `entry` failed because its process has exited.

    ENOENT or ESRCH, and lstat of its /proc entry raises FileNotFoundError
    or ProcessLookupError (any other error from lstat is not evidence); or
    the whole process has exited and is not yet reaped (_exited).
    """
    if exc.errno in (errno.ENOENT, errno.ESRCH):
        try:
            os.lstat(entry)
        except (FileNotFoundError, ProcessLookupError):
            return True
        except OSError:
            return False
    return _exited(entry)


def _refers(entry: Path, target: str) -> bool:
    """Whether the process at `entry` refers to `target`. Raises OSError when any of its references cannot be read."""
    if _inside(os.readlink(entry / "cwd"), target):
        return True
    for fd in os.listdir(entry / "fd"):
        try:
            link = os.readlink(entry / "fd" / fd)
        except FileNotFoundError:
            # Closed between the listing and the read, as the scan's own descriptors are in /proc/self/fd.
            continue
        if _inside(link, target):
            return True
    for item in (entry / "environ").read_bytes().split(b"\0"):
        for name in KEY_FILE_VARIABLES:
            if item.startswith(name) and _inside(os.path.realpath(os.fsdecode(item[len(name):])), target):
                return True
    return False


def _referenced(directory: Path, proc: Path) -> Optional[bool]:
    """Whether a process of this user refers to `directory`; None when that cannot be known.

    A reference is a working directory or an open file inside it, or a
    KEY_FILE_VARIABLES entry in the process's environment naming a path in
    it. Processes of other users are not read. None (unknown) when the
    process table cannot be listed, or when the uid, working directory,
    descriptors or environment of a process that still exists cannot be
    read; a process that has exited (_gone: its entry gone, or a zombie
    whose whole thread group has exited) is skipped.
    """
    try:
        entries = [Path(entry.path) for entry in os.scandir(proc) if entry.name.isdigit()]
    except OSError:
        logger.info("Key directories are kept: the process table could not be read")
        return None
    target = os.path.realpath(directory)
    uid = _uid()
    for entry in entries:
        try:
            if _proc_uid(entry) != uid:
                continue
            if _refers(entry, target):
                return True
        except OSError as exc:
            if _gone(entry, exc):
                continue
            logger.info("Key directories are kept: process %s could not be inspected", entry.name)
            return None
    return False


def sweep_abandoned(root: Path, proc: Optional[Path] = None) -> list:
    """Remove the key directories an earlier process left under `root`. Answers those removed.

    Each is a directory (not a symlink) named with one of
    KEY_DIRECTORY_PREFIXES, owned by this user, not made by this process,
    not modified for ABANDONED_AFTER_SECONDS, and referred to by no process
    of this user (_referenced, from `proc`, /proc by default; when it
    cannot be read, nothing is removed).
    """
    proc = proc if proc is not None else PROC
    removed = []
    now = time.time()
    try:
        entries = list(os.scandir(root))
    except OSError:
        return removed
    with _live_lock:
        live = set(_live)
    for entry in entries:
        if not entry.name.startswith(KEY_DIRECTORY_PREFIXES):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        path = Path(entry.path).absolute()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != _uid() or path in live
                or now - info.st_mtime < ABANDONED_AFTER_SECONDS):
            continue
        if _referenced(path, proc) is not False:
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed.append(path)
    if removed:
        logger.info("Removed %d key directories an earlier process left in %s", len(removed), root)
    return removed


def private_directory(prefix: str, root: Path) -> Path:
    """A new directory under `root`, mode 0700, absolute, recorded as this process's. Removed by remove_private_directory.

    The first one a process makes sweeps `root` first (sweep_abandoned).
    """
    global _swept
    if not _swept:
        _swept = True
        try:
            sweep_abandoned(root)
        except Exception as exc:
            logger.warning("Key directories left by an earlier process could not be swept (%s)", type(exc).__name__)
    directory = Path(tempfile.mkdtemp(prefix=prefix, dir=root)).absolute()
    with _live_lock:
        _live.add(directory)
    try:
        os.chmod(directory, 0o700)
    except BaseException:
        remove_private_directory(directory)
        raise
    return directory


def remove_private_directory(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)
    with _live_lock:
        _live.discard(Path(directory).absolute())


def open_key_file(key_file: Path) -> int:
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    return fd


def write_key_file(fd: int, text: str) -> None:
    os.write(fd, text.encode("ascii"))


def plain_env() -> dict:
    """PATH, HOME and LANG=C.UTF-8 only, so that no inherited DTK_*, RCLONE_* or SHELLOPTS reaches a child."""
    return {"PATH": os.environ.get("PATH") or DEFAULT_PATH,
            "HOME": os.environ.get("HOME") or str(Path.home()),
            "LANG": "C.UTF-8"}


def script_env(key_file: Path) -> dict:
    return {**plain_env(), "DTK_OFFSITE_KEY_FILE": str(key_file)}


def read_setting(settings: Any, name: str) -> str:
    """A setting as text, stripped; a secret's value read out (never logged)."""
    value = getattr(settings, name, "")
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    return (value or "").strip()


def overridden(names: list, env_file: Path) -> bool:
    """Whether any of these settings is set where it wins over offsite.env: a CAPTUA_ name in ../.env, or the process environment."""
    try:
        values = dotenv_values(env_file) if env_file.is_file() else {}
    except OSError:
        values = {}
    for name in names:
        if values.get(f"{config.NEW_PREFIX}{name}"):
            return True
        if os.environ.get(f"{config.NEW_PREFIX}{name}") or os.environ.get(f"{config.OLD_PREFIX}{name}"):
            return True
    return False


def write_key_id_in(config_path: Path) -> Optional[str]:
    """The `account` of the [b2unit] section of the rclone config, or None. Never logged."""
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
        elif section == WRITE_REMOTE and "=" in line:
            key, _, value = line.partition("=")
            if key.strip() == "account":
                return value.strip()
    return None


def digest(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def snapshot(settings_factory: Callable[[], Any], names: tuple, read: Callable[[Any, str], str], config_path: Path,
             offsite_env: Path) -> tuple:
    """What verification reads, as values and file digests, and the settings read: compared before and after a run, never logged."""
    settings = settings_factory()
    values = tuple(read(settings, name) for name in names)
    return (values, digest(config_path), digest(offsite_env)), settings


class ShuttingDown(Exception):
    """Shutdown has begun: the script was not launched."""


def signal_group(process, sig) -> None:
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        try:
            process.send_signal(sig)
        except ProcessLookupError:
            pass


def run_script(argv: list, env: dict, cwd: str, popen: Callable, time_limit: float, grace: float) -> tuple:
    """Run the script in a process group of its own. Answers (exit code or None, killed).

    At the time limit the group gets SIGTERM, so the script's traps can
    restore a credential it moved aside; after `grace` seconds, SIGKILL.
    """
    # From here this process's settings may be stale (app.core.restart_latch).
    # A caller launches only after every scheduler's worker has exited and no
    # offsite job is counted, and only while shutdown has not begun.
    if not restart_latch.set_stale_unless_shutting_down():
        raise ShuttingDown()
    process = popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        return process.wait(timeout=time_limit), False
    except subprocess.TimeoutExpired:
        pass
    logger.error("%s did not finish in %s s; asking it to stop", SCRIPT_NAME, time_limit)
    signal_group(process, signal.SIGTERM)
    try:
        return process.wait(timeout=grace), False
    except subprocess.TimeoutExpired:
        pass
    logger.error("%s did not stop in %s s after SIGTERM; killed", SCRIPT_NAME, grace)
    signal_group(process, signal.SIGKILL)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        logger.error("%s did not end after SIGKILL within %s s; it is left to the system", SCRIPT_NAME, grace)
    return None, True


@dataclass(frozen=True)
class Plan:
    script: Path
    replace: bool
    config_path: Path
    offsite_env: Path


def plan(script: Optional[Path], replace: Optional[bool], offsite_env: Optional[Path], *, before: str):
    """Before anything runs: `script_missing` or `script_too_old` (nothing written), or the Plan.

    `replace` None means: when the config exists (has_credential).
    `before` ends the log line for an old script ("pairing it").
    """
    script = Path(script) if script is not None else provision_script()
    if not script.is_file():
        logger.error("The provisioning script is not at %s", script)
        return "script_missing"
    if replace is None:
        replace = has_credential()
    if replace and not script_supports_replace(script):
        logger.error("%s does not take %s; update the unit before %s", script, REPLACE_FLAG, before)
        return "script_too_old"
    return Plan(script, replace, rclone_config_path(),
                offsite_env if offsite_env is not None else Path(config.ENV_FILES[1]))


def launch_and_wait(plan: Plan, *, argv: Callable[[], list], key_text: Callable[[], str], popen: Callable,
                    key_dir: Callable[[], Path], prefix: str, key_file_name: str, time_limit: float,
                    grace: float) -> tuple:
    """Write the key file, run the script, remove the key file. (exit code, killed); 127 when it could not be started."""
    directory = private_directory(prefix, key_dir())
    try:
        key_file = directory / key_file_name
        fd = open_key_file(key_file)
        try:
            write_key_file(fd, key_text())
        finally:
            os.close(fd)
        try:
            return run_script(argv(), script_env(key_file), str(plan.script.parent.parent), popen, time_limit, grace)
        except OSError as exc:
            logger.error("%s could not be started: %s", SCRIPT_NAME, type(exc).__name__)
            return 127, False
    finally:
        remove_private_directory(directory)


def run_with_evidence(plan: Plan, *, snapshot: Callable[[], tuple], verify: Callable[[Any], Optional[str]],
                      success: str, label: str, report: Optional[Callable[[str], None]] = None,
                      unchanged: Optional[str] = None, **launch) -> str:
    """Run the script as `plan` says and decide from evidence what happened. Never raises.

    `snapshot` answers (evidence, settings) and is taken before and after
    the run; `verify` is given the settings read after a changed run and
    answers None or a failing state; `launch` is launch_and_wait's
    keywords. Before the script is started (the before-snapshot) any
    exception is `apply_failed`: nothing can have been written, and the log
    line begins with `label`. From the moment it is started (the restart
    latch is set immediately before launch) the outcome is
    `apply_unverified`: the unit must be restarted and nothing is resumed
    in process. An exception after preparation began gives that outcome if
    and only if the latch is set; with the latch clear the script was never
    launched (the key file could not be written, say), so the outcome is
    `apply_failed`, reported with reason `prepare_failed` (`shutting_down`
    when shutdown had begun). Only two statements change the outcome:

    * down to `apply_failed`: the script exited non-zero, was not killed,
      and the after-snapshot was taken and equals the before-snapshot. This
      is also the only place that clears the process's restart latch
      (app.core.restart_latch), set at launch;
    * up to verify's answer: the script exited 0, was not killed, the
      snapshot changed, and verification ran; `success` when it passed,
      else its answer, failing closed.

    With `unchanged` given, a third: the script exited 0, was not killed,
    and the snapshot did not change. Verification runs on the settings
    read; `unchanged` when it passed, else its answer. Nothing changed, so
    the latch is cleared either way. Without it that run is
    `apply_unverified`, as before.
    """
    try:
        before, _ = snapshot()
    except Exception as exc:
        logger.error("%s could not start the provisioning script (%s); nothing was written", label,
                     type(exc).__name__)
        return "apply_failed"
    outcome = "apply_unverified"
    try:
        exit_code, killed = launch_and_wait(plan, **launch)
        after, fresh = snapshot()
        if not killed and exit_code != 0 and after == before:
            outcome = "apply_failed"
            restart_latch.clear_stale()
        elif not killed and exit_code == 0 and after != before:
            outcome = verify(fresh) or success
        elif unchanged is not None and not killed and exit_code == 0:
            outcome = verify(fresh) or unchanged
            restart_latch.clear_stale()
        logger.info("%s ended (exit code %s, killed %s, changed %s): %s", SCRIPT_NAME, exit_code, killed,
                    after != before, outcome)
    except Exception as exc:
        # The latch is the one source of truth: set only when the script was launched.
        if restart_latch.is_stale():
            outcome = "apply_unverified"
            logger.error("Provisioning ended without evidence of what it changed (%s); the unit must be "
                         "restarted", type(exc).__name__)
        else:
            outcome = "apply_failed"
            if report is not None:
                report("shutting_down" if isinstance(exc, ShuttingDown) else "prepare_failed")
            logger.error("The provisioning run could not be prepared (%s); the script was not started and "
                         "nothing was written", type(exc).__name__)
    return outcome
