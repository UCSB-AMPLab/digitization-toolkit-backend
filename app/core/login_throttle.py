"""In-memory login throttle for the single-process appliance.

Deliberately in-memory, not DB-backed: writing a row per failed login would hammer
the SD card, and the backend runs as one uvicorn process, so
process-local state is shared across all requests. State resets on restart, which
is acceptable — an attacker can't restart the service, and a restart only clears
lockouts, it never grants access.

Keys are opaque strings; the caller throttles per account and per client IP by
passing both keys, so brute force is bounded whether it targets one account from
many IPs or many accounts from one IP.
"""

import math
import time
from threading import Lock
from typing import Callable


class LoginThrottle:
    def __init__(
        self,
        max_failures: int = 5,
        lockout_seconds: int = 300,
        window_seconds: int = 900,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._max = max_failures
        self._lockout = lockout_seconds
        self._window = window_seconds
        self._clock = clock
        self._lock = Lock()
        self._state: dict[str, dict] = {}

    def retry_after(self, *keys: str) -> int:
        """Seconds until the soonest-unlocking key frees up, or 0 if none are locked."""
        now = self._clock()
        remaining = 0
        with self._lock:
            for key in keys:
                st = self._state.get(key)
                if st and st["locked_until"] > now:
                    remaining = max(remaining, math.ceil(st["locked_until"] - now))
        return remaining

    def record_failure(self, *keys: str) -> None:
        """Count a failed attempt against each key; lock the key once it crosses
        max_failures within the sliding window."""
        now = self._clock()
        with self._lock:
            for key in keys:
                st = self._state.setdefault(key, {"failures": [], "locked_until": 0.0})
                st["failures"] = [t for t in st["failures"] if t > now - self._window]
                st["failures"].append(now)
                if len(st["failures"]) >= self._max:
                    st["locked_until"] = now + self._lockout
                    st["failures"] = []

    def reset(self, *keys: str) -> None:
        """Clear all state for the given keys (called on a successful login)."""
        with self._lock:
            for key in keys:
                self._state.pop(key, None)


# Module-level singleton shared across requests in the single backend process.
login_throttle = LoginThrottle()
