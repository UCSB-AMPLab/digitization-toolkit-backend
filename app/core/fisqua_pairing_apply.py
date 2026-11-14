"""Applying a collected pairing payload (contract 1.11 §12.2): provision-rclone.sh writes the link, and the backend checks it.

The superproject's scripts/provision-rclone.sh already writes the
`[b2unit]` and `[b2read]` remotes and offsite.env, and validates every
value. The backend runs that script rather than writing the files
itself, and then reads the settings back. The script is found at
`../scripts/provision-rclone.sh` from the working directory
(provision_script), the way app.core.version finds `../VERSION`: the
backend runs from `backend/` inside the superproject (scripts/start.sh).

**How it is run** (apply_payload):

* as the backend's own user, never with sudo: `pi` owns /var/lib/dtk and
  the files the script writes there (setup.sh), and running `pi`'s own
  checkout as `pi` adds no privilege;
* with only PATH, HOME, LANG=C.UTF-8 and DTK_OFFSITE_KEY_FILE in its
  environment, so that no inherited DTK_B2_*, RCLONE_* or SHELLOPTS
  reaches it;
* with only public values on its command line: `--tenant`,
  `--dump-recipient`, `--fisqua-env`, `--config-path` (always: the rclone
  config the backend's own uploads use, rclone_config_path, so that the
  script never writes another file), and `--replace-credential` when that
  config exists (has_credential). `--change-tenant` is never passed: a
  change of workspace is refused before this
  (fisqua_pairing_policy.workspace_gate);
* with the keys and the token in a key file (KEY_ID, APP_KEY, BUCKET,
  READ_KEY_ID, READ_APP_KEY, FISQUA_TOKEN), mode 0600, in a directory of
  its own, mode 0700, under /dev/shm when it exists, else under the
  system's temporary directory; the directory is removed in `finally`;
* with its stdout and stderr discarded: it can print a key id. Only its
  exit code is logged;
* in a process group of its own, for SCRIPT_TIMEOUT_SECONDS; then SIGTERM,
  so its traps can restore a credential it moved aside, and SIGKILL after
  TERM_GRACE_SECONDS.

Before it runs, every value is checked again for shape
(fisqua_pairing_http.validate_payload: `bad_payload`), since one with a
newline could plant a second FISQUA_TOKEN line in the key file. A unit
with a credential needs a script that takes `--replace-credential`
(script_supports_replace, which reads the script for the case label);
an older script is `script_too_old` and is not run.

**Evidence, not exit codes.** Once the script has started, the backend
never assumes that nothing changed: apply_payload compares what
verification reads before and after the run, and its docstring states the
rule. Verification (verify) compares the tenant, environment, write and
read remotes, age recipient, token and rclone config path of a fresh
Settings() with the payload, and checks that the config file holds the new
write key id (compared, never logged). A mismatch is `settings_overridden`
when a CAPTUA_ name in ../.env, or either name in the process environment,
sets one of those settings (they win over offsite.env), else
`apply_unverified`.

Answers: `linked`, `bad_payload`, `script_missing`, `script_too_old`,
`apply_failed` (nothing was written), `settings_overridden`,
`apply_unverified`.

**pairing-result.json** in the data directory, beside device-id: the
tenant, the environment, when, and the administrator who asked for the
code. No secret. Written atomically after a verified link, read by the
card after the restart.

Version: 0.3.3
"""

import hashlib
import hmac
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from dotenv import dotenv_values

# As a module: tests rebuild the Settings object.
from app.core import config
from app.core import fisqua_contract as fc
from app.core import restart_latch
from app.core.fisqua_pairing_http import validate_payload

logger = logging.getLogger(__name__)

SCRIPT_NAME = "provision-rclone.sh"
REPLACE_FLAG = "--replace-credential"
SCRIPT_TIMEOUT_SECONDS = 15 * 60
# After SIGTERM at the time limit, how long the script has to restore and exit before SIGKILL.
TERM_GRACE_SECONDS = 30
DEV_SHM = Path("/dev/shm")
RESULT_FILE = "pairing-result.json"
KEY_FILE_NAME = "pairing.keys"
WRITE_REMOTE = "b2unit"
READ_REMOTE = "b2read"
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"


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


def _key_file_text(payload: dict) -> str:
    write, read = payload[fc.F_WRITE_KEY], payload[fc.F_READ_KEY]
    lines = [("KEY_ID", write[fc.F_KEY_ID]), ("APP_KEY", write[fc.F_APPLICATION_KEY]),
             ("BUCKET", payload[fc.F_BUCKET]), ("READ_KEY_ID", read[fc.F_KEY_ID]),
             ("READ_APP_KEY", read[fc.F_APPLICATION_KEY]), ("FISQUA_TOKEN", payload[fc.F_TOKEN])]
    return "".join(f"{name}={value}\n" for name, value in lines)


def _script_env(key_file: Path) -> dict:
    return {"PATH": os.environ.get("PATH") or DEFAULT_PATH,
            "HOME": os.environ.get("HOME") or str(Path.home()),
            "LANG": "C.UTF-8",
            "DTK_OFFSITE_KEY_FILE": str(key_file)}


def script_argv(script: Path, payload: dict, replace: bool, config_path: Path) -> list:
    argv = [str(script), "--tenant", payload[fc.F_TENANT], "--dump-recipient", payload[fc.F_AGE_RECIPIENT],
            "--fisqua-env", payload[fc.F_ENVIRONMENT], "--config-path", str(config_path)]
    if replace:
        argv.append(REPLACE_FLAG)
    return argv


# The settings a link writes, and what each must read after it.
_CHECKED = ("OFFSITE_TENANT", "FISQUA_ENV", "OFFSITE_REMOTE", "OFFSITE_READ_REMOTE", "OFFSITE_DUMP_RECIPIENT",
            "FISQUA_TOKEN", "RCLONE_CONFIG")


def _expected(payload: dict, config_path: Path) -> dict:
    return {"OFFSITE_TENANT": payload[fc.F_TENANT], "FISQUA_ENV": payload[fc.F_ENVIRONMENT],
            "OFFSITE_REMOTE": f"{WRITE_REMOTE}:{payload[fc.F_BUCKET]}",
            "OFFSITE_READ_REMOTE": f"{READ_REMOTE}:{payload[fc.F_BUCKET]}",
            "OFFSITE_DUMP_RECIPIENT": payload[fc.F_AGE_RECIPIENT], "FISQUA_TOKEN": payload[fc.F_TOKEN],
            "RCLONE_CONFIG": str(config_path)}


def _read(settings: Any, name: str) -> str:
    value = getattr(settings, name, "")
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    value = (value or "").strip()
    return value.lower() if name == "FISQUA_ENV" else value


def _overridden(names: list, env_file: Path) -> bool:
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


def _write_key_id_in(config_path: Path) -> Optional[str]:
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


def verify(payload: dict, settings: Any, config_path: Path, env_file: Optional[Path] = None) -> Optional[str]:
    """None when every checked setting reads as the payload says and the config holds the new write key id.

    Else `settings_overridden` (a mismatched setting is set where it wins
    over offsite.env) or `apply_unverified`. Only setting names are logged.
    """
    expected = _expected(payload, config_path)
    wrong = [name for name in _CHECKED
             if not hmac.compare_digest(_read(settings, name).encode(), expected[name].encode())]
    if wrong:
        env_file = env_file if env_file is not None else Path(config.ENV_FILES[0])
        if _overridden(wrong, env_file):
            logger.warning("The link was written, but these settings are set where they win over it: %s",
                           ", ".join(wrong))
            return "settings_overridden"
        logger.warning("The link could not be verified: these settings do not read as written: %s",
                       ", ".join(wrong))
        return "apply_unverified"
    found = _write_key_id_in(config_path)
    if found is None or not hmac.compare_digest(found.encode(), payload[fc.F_WRITE_KEY][fc.F_KEY_ID].encode()):
        logger.warning("The link could not be verified: %s does not hold the new write key", config_path)
        return "apply_unverified"
    return None


def _digest(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _evidence(settings_factory: Callable[[], Any], config_path: Path, offsite_env: Path) -> tuple:
    """What verification reads, as values and file digests, and the settings read: compared before and after a run, never logged."""
    settings = settings_factory()
    values = tuple(_read(settings, name) for name in _CHECKED)
    return (values, _digest(config_path), _digest(offsite_env)), settings


def _run_script(argv: list, env: dict, cwd: str, popen: Callable, time_limit: float, grace: float) -> tuple:
    """Run the script in a process group of its own. Answers (exit code or None, killed).

    At the time limit the group gets SIGTERM, so the script's traps can
    restore a credential it moved aside; after `grace` seconds, SIGKILL.
    """
    # From here this process's settings may be stale (app.core.restart_latch).
    # Provisioning starts only here, only after apply_link has seen every
    # scheduler's worker exit (wait_stopped reads a thread reference that
    # stop() no longer drops while the worker lives) and no offsite job
    # counted, and only while shutdown has not begun, so no other route to
    # provisioning while a worker is alive exists.
    if not restart_latch.set_stale_unless_shutting_down():
        raise ShuttingDown()
    process = popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        return process.wait(timeout=time_limit), False
    except subprocess.TimeoutExpired:
        pass
    logger.error("%s did not finish in %s s; asking it to stop", SCRIPT_NAME, time_limit)
    _signal_group(process, signal.SIGTERM)
    try:
        return process.wait(timeout=grace), False
    except subprocess.TimeoutExpired:
        pass
    logger.error("%s did not stop in %s s after SIGTERM; killed", SCRIPT_NAME, grace)
    _signal_group(process, signal.SIGKILL)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        logger.error("%s did not end after SIGKILL within %s s; it is left to the system", SCRIPT_NAME, grace)
    return None, True


class ShuttingDown(Exception):
    """Shutdown has begun: the script was not launched."""


def _signal_group(process, sig) -> None:
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        try:
            process.send_signal(sig)
        except ProcessLookupError:
            pass


@dataclass(frozen=True)
class _Plan:
    script: Path
    replace: bool
    config_path: Path
    offsite_env: Path


def _preflight(payload: dict, script: Optional[Path], replace: Optional[bool],
               offsite_env: Optional[Path]):
    """Before anything runs: a refusal state (nothing written), or the plan."""
    if validate_payload(payload) is not None:
        return "bad_payload"
    script = Path(script) if script is not None else provision_script()
    if not script.is_file():
        logger.error("The provisioning script is not at %s", script)
        return "script_missing"
    if replace is None:
        replace = has_credential()
    if replace and not script_supports_replace(script):
        logger.error("%s does not take %s; update the unit before pairing it", script, REPLACE_FLAG)
        return "script_too_old"
    return _Plan(script, replace, rclone_config_path(),
                 offsite_env if offsite_env is not None else Path(config.ENV_FILES[1]))


def _open_key_file(key_file: Path) -> int:
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    return fd


def _write_key_file(fd: int, payload: dict) -> None:
    os.write(fd, _key_file_text(payload).encode("ascii"))


def _launch_and_wait(plan: _Plan, payload: dict, popen: Callable, key_root: Optional[Path],
                     time_limit: float, grace: float) -> tuple:
    """Write the key file, run the script, remove the key file. (exit code, killed); 127 when it could not be started."""
    directory = Path(tempfile.mkdtemp(prefix="captua-pairing-", dir=key_root or key_file_root())).absolute()
    try:
        os.chmod(directory, 0o700)
        key_file = directory / KEY_FILE_NAME
        fd = _open_key_file(key_file)
        try:
            _write_key_file(fd, payload)
        finally:
            os.close(fd)
        try:
            return _run_script(script_argv(plan.script, payload, plan.replace, plan.config_path),
                               _script_env(key_file), str(plan.script.parent.parent), popen, time_limit, grace)
        except OSError as exc:
            logger.error("%s could not be started: %s", SCRIPT_NAME, type(exc).__name__)
            return 127, False
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def apply_payload(payload: dict, *, script: Optional[Path] = None, replace: Optional[bool] = None,
                  settings_factory: Optional[Callable[[], Any]] = None, env_file: Optional[Path] = None,
                  offsite_env: Optional[Path] = None, popen: Callable = subprocess.Popen,
                  key_root: Optional[Path] = None, time_limit: float = SCRIPT_TIMEOUT_SECONDS,
                  grace: float = TERM_GRACE_SECONDS, report: Optional[Callable[[str], None]] = None) -> str:
    """Run provision-rclone.sh with the payload and decide from evidence what happened. Never raises.

    Before the script is started (checks, the before-snapshot), a refusal
    or any exception is its own state or `apply_failed`: nothing can have
    been written. From the moment it is started (the restart latch is set
    immediately before launch) the outcome is `apply_unverified`: the unit
    must be restarted and nothing is resumed in process. An exception
    after preparation began gives that outcome if and only if the latch is
    set; with the latch clear the script was never launched (the key file
    could not be written, say), so the outcome is `apply_failed`, reported
    with reason `prepare_failed`. Only two statements change the outcome:

    * down to `apply_failed`: the script exited non-zero, was not killed,
      and the after-snapshot was taken and equals the before-snapshot (the
      checked settings of a fresh Settings(), and the digests of the rclone
      config and offsite.env). This is also the only place that clears the
      process's restart latch (app.core.restart_latch), set at launch;
    * up to verify's answer: the script exited 0, was not killed, the
      snapshot changed, and verification ran; `linked` when it passed, else
      `settings_overridden` or `apply_unverified`, both failing closed.
    """
    settings_factory = settings_factory or config.Settings
    try:
        plan = _preflight(payload, script, replace, offsite_env)
        if isinstance(plan, str):
            return plan
        before, _ = _evidence(settings_factory, plan.config_path, plan.offsite_env)
    except Exception as exc:
        logger.error("Pairing could not start the provisioning script (%s); nothing was written",
                     type(exc).__name__)
        return "apply_failed"
    outcome = "apply_unverified"
    try:
        exit_code, killed = _launch_and_wait(plan, payload, popen, key_root, time_limit, grace)
        after, fresh = _evidence(settings_factory, plan.config_path, plan.offsite_env)
        if not killed and exit_code != 0 and after == before:
            outcome = "apply_failed"
            restart_latch.clear_stale()
        elif not killed and exit_code == 0 and after != before:
            outcome = verify(payload, fresh, plan.config_path, env_file) or "linked"
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


def _result_path() -> Path:
    return config.settings.data_dir / RESULT_FILE


def write_result(tenant: str, environment: str, by: Optional[str], now: Optional[datetime] = None) -> None:
    """Write pairing-result.json atomically, mode 0644: the tenant, the environment, when, and by whom."""
    path = _result_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    when = (now or datetime.now(timezone.utc)).isoformat()
    body = json.dumps({"tenant": tenant, "environment": environment, "at": when, "by": by})
    fd, tmp = tempfile.mkstemp(prefix=f".{RESULT_FILE}.", dir=path.parent)
    try:
        try:
            os.fchmod(fd, 0o644)
            os.write(fd, body.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_result() -> Optional[dict]:
    """The last link's record, or None when there is none or it cannot be read."""
    try:
        body = json.loads(_result_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    return {key: body.get(key) for key in ("tenant", "environment", "at", "by")}
