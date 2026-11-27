"""The unit's time zone, read without root: the zone it is set to and the zones it can be set to.

* current_zone() is `timedatectl show -p Timezone --value`; when that
  cannot be run or answers nothing, the target of the LOCALTIME link
  (read against the link's own directory when relative) with the ZONEINFO
  prefix taken off; None when neither gives a name.
* zones() is `timedatectl list-timezones`, one name a line, in its order;
  an empty list when it cannot be run or fails.

Each command is an argument list with no shell, stdin closed and a bound
of TIMEOUT_SECONDS. Setting the zone needs root and goes through the
system helper's `set-timezone` verb (app.core.system_helper), which
accepts only a name zones() lists.

Version: 0.4.0
"""

import logging
import os
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

LOCALTIME = "/etc/localtime"
ZONEINFO = "/usr/share/zoneinfo/"
TIMEOUT_SECONDS = 10


def _timedatectl(*args: str) -> Optional[str]:
    """The stdout of `timedatectl <args>` on exit 0, else None."""
    try:
        result = subprocess.run(["timedatectl", *args], capture_output=True, text=True,
                                timeout=TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("timedatectl %s could not be run: %s", " ".join(args), type(exc).__name__)
        return None
    if result.returncode != 0:
        logger.info("timedatectl %s failed (rc=%s)", " ".join(args), result.returncode)
        return None
    return result.stdout or ""


def _localtime_zone() -> Optional[str]:
    """The zone the LOCALTIME link names under ZONEINFO, or None."""
    try:
        target = os.readlink(LOCALTIME)
    except OSError:
        return None
    path = os.path.normpath(os.path.join(os.path.dirname(LOCALTIME), target))
    if not path.startswith(ZONEINFO):
        return None
    zone = path[len(ZONEINFO):]
    return zone or None


def current_zone() -> Optional[str]:
    """The unit's zone, from timedatectl, else the LOCALTIME link, else None."""
    out = _timedatectl("show", "-p", "Timezone", "--value")
    zone = out.strip() if out is not None else ""
    return zone or _localtime_zone()


def zones() -> list[str]:
    """The names `timedatectl list-timezones` gives, or an empty list."""
    out = _timedatectl("list-timezones")
    if out is None:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]
