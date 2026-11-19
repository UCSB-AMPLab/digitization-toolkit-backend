"""The root-owned system helper (scripts/dtk-system-helper in the superproject), and asking whether it has a verb.

The helper is installed at HELPER, root-owned, with a sudoers line that
lets the service user run it with any arguments
(install-system-helper.sh). Each verb is one case label in its `main`;
an unknown verb exits 1 with "unknown verb". The backend calls it as the
power route does: `sudo HELPER <verb>`, stdin closed, output captured.

A unit's helper is reinstalled only when install-system-helper.sh runs
again, which update.sh does not do, so a newer backend can meet an older
helper. supports() reads the installed file for the verb's case label
(`restart-backend)`) instead of calling it and reading the refusal. A
helper that cannot be read counts as lacking the verb.

Version: 0.3.3
"""

import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

HELPER = "/usr/local/bin/dtk-system-helper"
RESTART_BACKEND = "restart-backend"
TIMEOUT_SECONDS = 30


def supports(verb: str) -> bool:
    """Whether sudo is present and the installed helper has a case label for `verb`."""
    if shutil.which("sudo") is None:
        return False
    try:
        with open(HELPER, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    return any(line.strip().startswith(f"{verb})") for line in text.splitlines())


def run(verb: str) -> bool:
    """Run `sudo HELPER <verb>`. True on exit 0. Its output goes to the log only on failure."""
    try:
        result = subprocess.run(["sudo", HELPER, verb], capture_output=True, text=True,
                                timeout=TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("dtk-system-helper %s could not be run: %s", verb, type(exc).__name__)
        return False
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip() or "no output"
        logger.error("dtk-system-helper %s failed (rc=%s): %s", verb, result.returncode, detail)
        return False
    return True
