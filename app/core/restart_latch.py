"""One fact about this process: its settings may be stale, so a restart is required.

The backend reads its settings once, at import (app.core.config). The
moment pairing starts provision-rclone.sh, the files those settings came
from may change, and this process can no longer tell what it would read
from them. So the latch is set then, immediately before the script
process is launched (fisqua_pairing_apply._run_script), and cleared only
by the one branch that has positive evidence nothing changed: the script
refused, and the settings and the digests of the rclone config and
offsite.env read the same after the run as before
(fisqua_pairing_apply.apply_payload). Nothing else clears it. It lives in
memory only, so a restart clears it by construction.

A second fact sits beside it, under the same lock: shutdown has begun
(begin_shutdown, set first in app.main's shutdown). The script is launched
only through set_stale_unless_shutting_down, so once shutdown has begun no
provisioning run starts, and pairing starts no scheduler.

While the latch is set: no pairing session can begin or renew
(fisqua_pairing_policy.start_refusal answers `restart_required`); pairing
starts no scheduler (fisqua_pairing_service._start_again); and GET
/system/pairing reports `restartRequired`, so the card offers the unit's
restart after a reload, or to a second administrator.

Version: 0.3.3
"""

import threading

_lock = threading.Lock()
_stale = False
_shutting_down = False


def set_stale() -> None:
    global _stale
    with _lock:
        _stale = True


def set_stale_unless_shutting_down() -> bool:
    """Set the latch, unless shutdown has begun: then nothing is set and the answer is False. One lock decides both."""
    global _stale
    with _lock:
        if _shutting_down:
            return False
        _stale = True
        return True


def begin_shutdown() -> None:
    """Set first in the application's shutdown: from here no provisioning run starts and pairing starts no scheduler."""
    global _shutting_down
    with _lock:
        _shutting_down = True


def shutting_down() -> bool:
    with _lock:
        return _shutting_down


def clear_stale() -> None:
    """Only for apply_payload's positive-evidence branch."""
    global _stale
    with _lock:
        _stale = False


def is_stale() -> bool:
    with _lock:
        return _stale


def _reset_for_tests() -> None:
    global _stale, _shutting_down
    with _lock:
        _stale = False
        _shutting_down = False
