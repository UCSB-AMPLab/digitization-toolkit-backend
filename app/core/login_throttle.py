"""In-memory login throttle for the single-process appliance.

Deliberately in-memory, not DB-backed: writing a row per failed login would hammer
the SD card, and the backend runs as one uvicorn process, so
process-local state is shared across all requests. State resets on restart, which
is acceptable — an attacker can't restart the service, and a restart only clears
lockouts, it never grants access.

Keys are `user:<name>` for an account and `ip:<address>` for a machine, the
client address the request came from; the caller passes both, so brute force is
bounded whether it targets one account from many machines or many accounts from
one machine. Each kind has its own limit: an account locks after 5 failures, a
machine after 20, both within the same sliding window and for the same lockout
time. The kiosk's browser is one machine for every operator who signs in at it,
so its limit is set above any one account's: one operator's wrong passwords
lock that operator's account long before they lock the station for the others.

Version: 0.4.0
"""

import math
import time
from threading import Lock
from typing import Callable


ACCOUNT_MAX_FAILURES = 5
MACHINE_MAX_FAILURES = 20
ACCOUNT_PREFIX = "user"
MACHINE_PREFIX = "ip"


class LoginThrottle:
    def __init__(
        self,
        account_max_failures: int = ACCOUNT_MAX_FAILURES,
        machine_max_failures: int = MACHINE_MAX_FAILURES,
        lockout_seconds: int = 300,
        window_seconds: int = 900,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._max = {ACCOUNT_PREFIX: account_max_failures, MACHINE_PREFIX: machine_max_failures}
        self._lockout = lockout_seconds
        self._window = window_seconds
        self._clock = clock
        self._lock = Lock()
        self._state: dict[str, dict] = {}

    def _limit(self, key: str) -> int:
        """The failures that lock `key`, by its kind; a key of no known kind is a ValueError."""
        kind, sep, _ = key.partition(":")
        if not sep or kind not in self._max:
            raise ValueError(f"not a login throttle key: {key!r}")
        return self._max[kind]

    def lockout(self, *keys: str) -> tuple[int, tuple[str, ...]]:
        """Seconds until every given key is free (0 if none is locked), and the
        locked keys in the order given, read together under one lock."""
        now = self._clock()
        remaining = 0
        locked = []
        with self._lock:
            for key in keys:
                st = self._state.get(key)
                if st and st["locked_until"] > now:
                    remaining = max(remaining, math.ceil(st["locked_until"] - now))
                    locked.append(key)
        return remaining, tuple(locked)

    def retry_after(self, *keys: str) -> int:
        """Seconds until every given key is free, or 0 if none are locked."""
        return self.lockout(*keys)[0]

    def record_failure(self, *keys: str) -> None:
        """Count a failed attempt against each key; lock the key once it reaches
        its kind's limit within the sliding window."""
        limits = [self._limit(key) for key in keys]
        now = self._clock()
        with self._lock:
            for key, limit in zip(keys, limits):
                st = self._state.setdefault(key, {"failures": [], "locked_until": 0.0})
                st["failures"] = [t for t in st["failures"] if t > now - self._window]
                st["failures"].append(now)
                if len(st["failures"]) >= limit:
                    st["locked_until"] = now + self._lockout
                    st["failures"] = []

    def reset(self, *keys: str) -> None:
        """Clear all state for the given keys (called on a successful login)."""
        with self._lock:
            for key in keys:
                self._state.pop(key, None)


# Module-level singleton shared across requests in the single backend process.
login_throttle = LoginThrottle()
