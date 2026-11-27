"""Applying a collected pairing payload (contract 1.11 §12.2): provision-rclone.sh writes the link, and the backend checks it.

The superproject's scripts/provision-rclone.sh already writes the
`[b2unit]` and `[b2read]` remotes and offsite.env, and validates every
value. The backend runs that script rather than writing the files
itself, and then reads the settings back. How it is found and run, and
how its outcome is decided from evidence, is app.core.provision_runner's,
shared with the offsite settings an administrator enters
(app.core.offsite_settings); this module holds what is Fisqua's: the key
file, the command line, the verification and the result file.

**How it is run** (apply_payload), as provision_runner runs it:

* as the backend's own user, never with sudo;
* with only PATH, HOME, LANG=C.UTF-8 and DTK_OFFSITE_KEY_FILE in its
  environment;
* with only public values on its command line: `--tenant`,
  `--dump-recipient`, `--fisqua-env`, `--config-path` (always: the rclone
  config the backend's own uploads use, provision_runner.rclone_config_path,
  so that the script never writes another file), and
  `--replace-credential` when that config exists
  (provision_runner.has_credential). `--change-tenant` is never passed: a
  change of workspace is refused before this
  (fisqua_pairing_policy.workspace_gate);
* with the keys and the token in a key file (KEY_ID, APP_KEY, BUCKET,
  READ_KEY_ID, READ_APP_KEY, FISQUA_TOKEN), mode 0600, in a directory of
  its own, mode 0700, under /dev/shm when it exists, else under the
  system's temporary directory; the directory is removed in `finally`;
* with its stdout and stderr discarded: it can print a key id. Only its
  exit code is logged;
* in a process group of its own, for SCRIPT_TIMEOUT_SECONDS; then SIGTERM,
  and SIGKILL after TERM_GRACE_SECONDS.

Before it runs, every value is checked again for shape
(fisqua_pairing_http.validate_payload: `bad_payload`), since one with a
newline could plant a second FISQUA_TOKEN line in the key file. A unit
with a credential needs a script that takes `--replace-credential`
(provision_runner.script_supports_replace, which reads the script for the case label);
an older script is `script_too_old` and is not run.

**Evidence, not exit codes.** Once the script has started, the backend
never assumes that nothing changed: apply_payload compares what
verification reads before and after the run, and
provision_runner.run_with_evidence states the rule. Verification (verify)
compares the tenant, environment, write and read remotes, age recipient,
token and rclone config path of a fresh
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

Version: 0.4.0
"""

import hmac
import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# As a module: tests rebuild the Settings object.
from app.core import config
from app.core import fisqua_contract as fc
from app.core import provision_runner as runner
from app.core.fisqua_pairing_http import validate_payload

logger = logging.getLogger(__name__)

RESULT_FILE = "pairing-result.json"
KEY_FILE_NAME = "pairing.keys"
READ_REMOTE = "b2read"


def _key_file_text(payload: dict) -> str:
    write, read = payload[fc.F_WRITE_KEY], payload[fc.F_READ_KEY]
    lines = [("KEY_ID", write[fc.F_KEY_ID]), ("APP_KEY", write[fc.F_APPLICATION_KEY]),
             ("BUCKET", payload[fc.F_BUCKET]), ("READ_KEY_ID", read[fc.F_KEY_ID]),
             ("READ_APP_KEY", read[fc.F_APPLICATION_KEY]), ("FISQUA_TOKEN", payload[fc.F_TOKEN])]
    return "".join(f"{name}={value}\n" for name, value in lines)


def script_argv(script: Path, payload: dict, replace: bool, config_path: Path) -> list:
    argv = [str(script), "--tenant", payload[fc.F_TENANT], "--dump-recipient", payload[fc.F_AGE_RECIPIENT],
            "--fisqua-env", payload[fc.F_ENVIRONMENT], "--config-path", str(config_path)]
    if replace:
        argv.append(runner.REPLACE_FLAG)
    return argv


# The settings a link writes, and what each must read after it.
_CHECKED = ("OFFSITE_TENANT", "FISQUA_ENV", "OFFSITE_REMOTE", "OFFSITE_READ_REMOTE", "OFFSITE_DUMP_RECIPIENT",
            "FISQUA_TOKEN", "RCLONE_CONFIG")


def _expected(payload: dict, config_path: Path) -> dict:
    return {"OFFSITE_TENANT": payload[fc.F_TENANT], "FISQUA_ENV": payload[fc.F_ENVIRONMENT],
            "OFFSITE_REMOTE": f"{runner.WRITE_REMOTE}:{payload[fc.F_BUCKET]}",
            "OFFSITE_READ_REMOTE": f"{READ_REMOTE}:{payload[fc.F_BUCKET]}",
            "OFFSITE_DUMP_RECIPIENT": payload[fc.F_AGE_RECIPIENT], "FISQUA_TOKEN": payload[fc.F_TOKEN],
            "RCLONE_CONFIG": str(config_path)}


def _read(settings: Any, name: str) -> str:
    value = runner.read_setting(settings, name)
    return value.lower() if name == "FISQUA_ENV" else value


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
        if runner.overridden(wrong, env_file):
            logger.warning("The link was written, but these settings are set where they win over it: %s",
                           ", ".join(wrong))
            return "settings_overridden"
        logger.warning("The link could not be verified: these settings do not read as written: %s",
                       ", ".join(wrong))
        return "apply_unverified"
    found = runner.write_key_id_in(config_path)
    if found is None or not hmac.compare_digest(found.encode(), payload[fc.F_WRITE_KEY][fc.F_KEY_ID].encode()):
        logger.warning("The link could not be verified: %s does not hold the new write key", config_path)
        return "apply_unverified"
    return None


def _evidence(settings_factory: Callable[[], Any], config_path: Path, offsite_env: Path) -> tuple:
    """What verification reads, as values and file digests, and the settings read: compared before and after a run, never logged."""
    return runner.snapshot(settings_factory, _CHECKED, _read, config_path, offsite_env)


def _preflight(payload: dict, script: Optional[Path], replace: Optional[bool],
               offsite_env: Optional[Path]):
    """Before anything runs: a refusal state (nothing written), or the plan."""
    if validate_payload(payload) is not None:
        return "bad_payload"
    script = Path(script) if script is not None else runner.provision_script()
    if replace is None:
        replace = runner.has_credential()
    return runner.plan(script, replace, offsite_env, before="pairing it")


def apply_payload(payload: dict, *, script: Optional[Path] = None, replace: Optional[bool] = None,
                  settings_factory: Optional[Callable[[], Any]] = None, env_file: Optional[Path] = None,
                  offsite_env: Optional[Path] = None, popen: Callable = subprocess.Popen,
                  key_root: Optional[Path] = None, time_limit: float = runner.SCRIPT_TIMEOUT_SECONDS,
                  grace: float = runner.TERM_GRACE_SECONDS, report: Optional[Callable[[str], None]] = None) -> str:
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
    except Exception as exc:
        logger.error("Pairing could not start the provisioning script (%s); nothing was written",
                     type(exc).__name__)
        return "apply_failed"
    if isinstance(plan, str):
        return plan
    return runner.run_with_evidence(
        plan,
        snapshot=lambda: _evidence(settings_factory, plan.config_path, plan.offsite_env),
        verify=lambda fresh: verify(payload, fresh, plan.config_path, env_file),
        success="linked", label="Pairing", report=report,
        argv=lambda: script_argv(plan.script, payload, plan.replace, plan.config_path),
        key_text=lambda: _key_file_text(payload), popen=popen,
        key_dir=lambda: key_root or runner.key_file_root(), prefix="captua-pairing-",
        key_file_name=KEY_FILE_NAME, time_limit=time_limit, grace=grace)


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
