"""Pairing a unit with its Fisqua workspace by a code (contract 1.11, §12): the session, in memory.

An administrator asks for a code on the unit; the unit shows it beside
its device id (app.core.device_id) and polls the collect route until an
administrator of the workspace approves the code in Fisqua, and then
hands the payload to an injected `apply` callable, which writes the link
settings. The HTTP calls and the reading of each answer are in
app.core.fisqua_pairing_http.

**The secret.** The code, the collect secret and the payload live only in
the PairingSession object, in memory: never logged, never written to disk,
never in a repr, a str or an exception's text. They are dropped when the
session reaches any state but `requesting` and `showing` (the payload
when `apply` returns). snapshot() returns the code, for the card that
shows it; it never returns the secret or the payload.

**States.** `requesting` (asking for a code) and `showing` (polling with
the code shown) run; `applying` runs `apply`. Every other state is final:

    applied, or what apply returned     apply returned
    apply_failed                        apply raised (its text not kept), or
                                        returned it; a reason apply set, such
                                        as `uploads_still_running`, is kept
    lapsed                              the code expired, or network trouble
                                        lasted, past the renewal window
    lost_answer                         410 collected: the payload went to an
                                        earlier poll that never arrived
    unknown                             404 unknown
    cannot_pair                         codes 404, not JSON (§12.3)
    wrong_environment                   the payload names another environment
                                        than the host that issued the code
    bad_payload                         a field missing or out of shape;
                                        `reason` names the field, never its value
    not_eligible                        the injected before_code named a reason
    error                               400 (reason: Fisqua's rule), 405
                                        (`method`), any other answer
                                        (`unexpected`)
    ended                               no status read (touch) for HEARTBEAT_SECONDS
    cancelled                           cancel() or stop()
    whatever before_apply names         e.g. `other_workspace`; nothing applied

After `lapsed`, `lost_answer` or `unknown` the administrator can ask for a
new code (PairingRegistry.renew), which starts a new session: a new code,
and a new renewal window.

**Timers**, all on an injected monotonic clock:

* the collect route is polled every pollSeconds (at least
  MIN_POLL_SECONDS);
* when Fisqua answers 410 expired within RENEWAL_WINDOW_SECONDS of the
  session's first code, the session asks for a new code at once; after it,
  `lapsed`. Expiry is Fisqua's answer, never the unit's clock: a 202 keeps
  the code past the window. expiresInSeconds is counted down on the
  unit's clock for display only;
* 429 and 503 busy wait Retry-After, at least pollSeconds and at least
  MIN_POLL_SECONDS, with the code unchanged;
* network trouble (fisqua_pairing_http's `network`) is retried after
  pollSeconds, doubled at each further failure up to
  NETWORK_BACKOFF_MAX_SECONDS. Before the first code it is retried while
  the card is open; after it, while the window lasts, and then `lapsed`;
* the heartbeat: the card reads the session's state every few seconds
  (touch). With no read for HEARTBEAT_SECONDS the session ends, except
  while `apply` runs, which is not interrupted.

**Gates**, injected (app.core.fisqua_pairing_service wires the rules of
app.core.fisqua_pairing_policy): before_code() is
called before each request for a code (the mode gate), and
before_apply(payload) after the payload's environment and shape are
checked and before `apply` (the workspace gate). Either answers None to
go on, or a reason, which ends the session (`not_eligible` with the reason,
or the reason itself as the state for before_apply). Order at a 200:
environment, shape, before_apply, apply.

**Threads.** Each running session polls on one daemon thread of its own
(start), as the schedulers do (catalogue_backup, image_upload): it waits on
an Event between steps, so stop() wakes it at once, and joins it. The HTTP
call is made without the session's lock, so a request thread reading the
state never waits for Fisqua; an answer that arrives after cancel() is
discarded. A thread caught in a call when stop() is asked finishes that
call (at most fisqua_pairing_http.TIMEOUT_SECONDS) and exits; it is a
daemon, so it never holds the process.

**One session per process** (PairingRegistry), the same single-process
assumption as catalogue_backup's scheduler.

Version: 0.3.3
"""

import logging
import threading
import time
from typing import Any, Callable, Optional

from app.core import fisqua_contract as fc
from app.core.fisqua_pairing_http import validate_payload

logger = logging.getLogger(__name__)

RENEWAL_WINDOW_SECONDS = 600
HEARTBEAT_SECONDS = 30
MIN_POLL_SECONDS = 5
NETWORK_BACKOFF_MAX_SECONDS = 60
STOP_JOIN_SECONDS = 5.0

REQUESTING = "requesting"
SHOWING = "showing"
APPLYING = "applying"
RUNNING = (REQUESTING, SHOWING, APPLYING)
# Final states after which the administrator may ask for a new code.
RENEWABLE = ("lapsed", "lost_answer", "unknown")
# Reasons the apply step shows while `applying`; not kept with the final state.
PROGRESS_REASONS = ("finishing_uploads", "writing_link")


class PairingRefused(Exception):
    """A session could not be begun or renewed; `reason` says why (a code, never a secret)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class PairingSession:
    """One attempt to pair: codes requested, one shown at a time, polled until a final state."""

    def __init__(self, *, client, environment: str, device_id: str, unit_name: str,
                 current_tenant: Optional[str], apply: Callable[[dict], Optional[str]],
                 before_code: Optional[Callable[[], Optional[str]]] = None,
                 before_apply: Optional[Callable[[dict], Optional[str]]] = None,
                 clock: Callable[[], float] = time.monotonic, actor: Optional[str] = None):
        if not isinstance(unit_name, str) or not 1 <= len(unit_name) <= fc.UNIT_NAME_MAX_CHARS:
            raise ValueError(f"the unit name must be 1 to {fc.UNIT_NAME_MAX_CHARS} characters")
        self._client = client
        self.environment = environment
        self.device_id = device_id
        self.unit_name = unit_name
        self.current_tenant = current_tenant
        # The username of the administrator who asked for the code; kept in pairing-result.json.
        self.actor = actor
        self._apply = apply
        self._before_code = before_code
        self._before_apply = before_apply
        self._clock = clock

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._generation = 0

        self._state = REQUESTING
        self._reason: Optional[str] = None
        self._trouble: Optional[str] = None
        self._retry_after: Optional[float] = None
        self._result: Any = None
        self._code: Optional[str] = None
        self._secret: Optional[str] = None
        self._payload: Optional[dict] = None
        self._code_at: Optional[float] = None
        self._expires_in: Optional[int] = None
        self._poll: Optional[int] = None
        self._window_start: Optional[float] = None
        self._failures = 0
        now = clock()
        self._due = now
        self._last_touch = now

    def __repr__(self) -> str:
        return f"PairingSession(state={self._state!r}, environment={self.environment!r})"

    __str__ = __repr__

    # -- reading ------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def running(self) -> bool:
        return self._state in RUNNING

    def touch(self) -> None:
        """A status read: keeps the session alive for another HEARTBEAT_SECONDS."""
        with self._lock:
            self._last_touch = self._clock()

    def set_reason(self, reason: Optional[str]) -> None:
        """While `applying`: what the apply step is doing now (`finishing_uploads`, `writing_link`), for the card."""
        with self._lock:
            if self._state == APPLYING:
                self._reason = reason

    def snapshot(self) -> dict:
        """The state for the card. Holds the code while it is shown; never the secret or the payload."""
        with self._lock:
            now = self._clock()
            expires = None
            if self._code is not None and self._code_at is not None and self._expires_in is not None:
                expires = max(0, round(self._expires_in - (now - self._code_at)))
            renewal = None
            if self._window_start is not None and self.running:
                renewal = max(0, round(RENEWAL_WINDOW_SECONDS - (now - self._window_start)))
            retry = None
            if self._trouble is not None and self.running:
                retry = max(0, round(self._due - now))
            return {"state": self._state, "reason": self._reason, "trouble": self._trouble,
                    "retryAfterSeconds": retry, "code": self._code, "expiresInSeconds": expires,
                    "renewalSecondsLeft": renewal, "environment": self.environment, "deviceId": self.device_id,
                    "unitName": self.unit_name, "currentTenant": self.current_tenant, "result": self._result}

    # -- stepping -----------------------------------------------------------

    def _poll_interval(self) -> float:
        return float(max(self._poll or MIN_POLL_SECONDS, MIN_POLL_SECONDS))

    def _finish(self, state: str, reason: Optional[str] = None) -> None:
        """Enter a final state: drop the secret, the code and the payload, close the client. Lock held."""
        self._state = state
        self._reason = reason
        self._trouble = None
        self._code = self._secret = self._payload = None
        self._code_at = self._expires_in = None
        self._generation += 1
        self._stop.set()
        try:
            self._client.close()
        except Exception:
            logger.warning("The pairing client could not be closed", exc_info=False)
        logger.info("Pairing session ended: %s%s", state, f" ({reason})" if reason else "")

    def _past_window(self, now: float) -> bool:
        return self._window_start is not None and now - self._window_start >= RENEWAL_WINDOW_SECONDS

    def _wait(self, now: float, seconds: float) -> float:
        """Wait `seconds` from now, but never past the heartbeat. Lock held."""
        self._due = now + seconds
        return max(0.0, min(seconds, HEARTBEAT_SECONDS - (now - self._last_touch)))

    def step(self) -> Optional[float]:
        """Do what is due now. Answers the seconds until the next step, or None once the session is final."""
        with self._lock:
            if not self.running:
                return None
            now = self._clock()
            if self._state != APPLYING and now - self._last_touch >= HEARTBEAT_SECONDS:
                self._finish("ended")
                return None
            if now < self._due:
                return self._wait(now, self._due - now)
            state, generation = self._state, self._generation
            code, secret = self._code, self._secret

        if state == REQUESTING:
            if self._before_code is not None:
                reason = self._before_code()
                if reason is not None:
                    with self._lock:
                        if generation == self._generation:
                            self._finish("not_eligible", reason)
                    return None
            answer = self._client.request_code(self.unit_name, self.device_id, self.current_tenant)
            with self._lock:
                if generation != self._generation:
                    return None
                return self._after_codes(answer)
        if state == SHOWING:
            answer = self._client.collect(code, secret)
            with self._lock:
                if generation != self._generation:
                    return None
                wait = self._after_collect(answer)
                if wait is not None or self._state != APPLYING:
                    return wait
                payload, generation = self._payload, self._generation
            return self._run_apply(payload, generation)
        return None

    def _busy(self, now: float, retry_after: Optional[float]) -> float:
        self._trouble = "busy"
        self._failures = 0
        return self._wait(now, max(retry_after or 0.0, self._poll_interval(), float(MIN_POLL_SECONDS)))

    def _network(self, now: float) -> Optional[float]:
        if self._past_window(now):
            self._finish("lapsed")
            return None
        self._trouble = "network"
        self._failures += 1
        backoff = min(NETWORK_BACKOFF_MAX_SECONDS, self._poll_interval() * 2 ** (self._failures - 1))
        return self._wait(now, backoff)

    def _final_answer(self, answer) -> None:
        if answer.outcome == "cannot_pair":
            self._finish("cannot_pair")
        elif answer.outcome == "refused":
            self._finish("error", answer.rule or "refused")
        elif answer.outcome == "method":
            self._finish("error", "method")
        else:
            self._finish("error", "unexpected")

    def _after_codes(self, answer) -> Optional[float]:
        now = self._clock()
        if answer.outcome == "code":
            self._code, self._secret = answer.code, answer.collect_secret
            self._code_at, self._expires_in, self._poll = now, answer.expires_in_seconds, answer.poll_seconds
            if self._window_start is None:
                self._window_start = now
            self._state = SHOWING
            self._trouble = None
            self._failures = 0
            logger.info("Pairing code received; polling every %s s", int(self._poll_interval()))
            return self._wait(now, self._poll_interval())
        if answer.outcome == "busy":
            return self._busy(now, answer.retry_after)
        if answer.outcome == "network":
            return self._network(now)
        self._final_answer(answer)
        return None

    def _after_collect(self, answer) -> Optional[float]:
        now = self._clock()
        outcome = answer.outcome
        if outcome == "waiting":
            self._trouble = None
            self._failures = 0
            return self._wait(now, self._poll_interval())
        if outcome == "busy":
            return self._busy(now, answer.retry_after)
        if outcome == "network":
            return self._network(now)
        if outcome == "expired":
            if self._past_window(now):
                self._finish("lapsed")
                return None
            self._code = self._secret = None
            self._code_at = self._expires_in = None
            self._state = REQUESTING
            self._trouble = None
            logger.info("Pairing code expired; asking for a new one")
            return self._wait(now, 0.0)
        if outcome == "collected":
            self._finish("lost_answer")
            return None
        if outcome == "unknown":
            self._finish("unknown")
            return None
        if outcome == "payload":
            return self._check_payload(answer.payload)
        self._final_answer(answer)
        return None

    def _check_payload(self, payload: Any) -> None:
        """Environment and shape; on success the session is `applying` with the payload held. Lock held."""
        named = payload.get(fc.F_ENVIRONMENT) if isinstance(payload, dict) else None
        if isinstance(named, str) and named != self.environment:
            self._finish("wrong_environment")
            return None
        field = validate_payload(payload)
        if field is not None:
            self._finish("bad_payload", field)
            return None
        self._code = self._secret = None
        self._code_at = self._expires_in = None
        self._payload = payload
        self._state = APPLYING
        self._trouble = None
        logger.info("Pairing approved; applying the link")
        return None

    def _run_apply(self, payload: dict, generation: int) -> None:
        state, reason, result = None, None, None
        try:
            if self._before_apply is not None:
                state = self._before_apply(payload)
            if state is None:
                result = self._apply(payload)
                state = result if isinstance(result, str) else "applied"
        except Exception as exc:
            state, result = "apply_failed", None
            reason = type(exc).__name__
        finally:
            payload = None
        with self._lock:
            if generation == self._generation:
                self._result = result if not isinstance(result, str) else None
                # A reason the apply step set that is not progress (such as
                # `uploads_still_running`) stays with its final state.
                if reason is None and self._reason not in (None, *PROGRESS_REASONS):
                    reason = self._reason
                self._finish(state, reason)
        return None

    # -- the thread ---------------------------------------------------------

    def start(self) -> None:
        """Poll on a thread of its own until a final state or stop()."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="fisqua-pairing", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                wait = self.step()
            except Exception:
                logger.error("The pairing session failed; it is ended")
                with self._lock:
                    if self.running:
                        self._finish("error", "unexpected")
                return
            if wait is None:
                return
            if self._stop.wait(wait):
                return

    def cancel(self) -> bool:
        """Cancel a session that has not reached `applying`. False when it is applying or already final."""
        with self._lock:
            if self._state not in (REQUESTING, SHOWING):
                return False
            self._finish("cancelled")
            return True

    def stop(self, timeout: float = STOP_JOIN_SECONDS) -> None:
        """Cancel, and wait for the thread to exit."""
        self.cancel()
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)


class PairingRegistry:
    """At most one running session per process, built from the unit's device id and pairing target.

    The apply step, the gates, the client factory and the clock are given
    once, here; the API keeps one registry
    (app.core.fisqua_pairing_service.pairing_registry). The registry's
    `apply(payload, session)` and `before_apply(payload, session)` receive
    the session, for its actor and its current tenant. `before_begin()` is
    asked before a session is begun or renewed (PairingRefused with its
    reason); `before_code()` is handed to the session, which asks it before
    each request for a code. A final session stays readable (current)
    until the next begin or renew replaces it.
    """

    def __init__(self, *, apply: Callable[[dict, "PairingSession"], Optional[str]],
                 before_begin: Optional[Callable[[], Optional[str]]] = None,
                 before_code: Optional[Callable[[], Optional[str]]] = None,
                 before_apply: Optional[Callable[[dict, "PairingSession"], Optional[str]]] = None,
                 client_factory: Optional[Callable[[str], Any]] = None,
                 clock: Callable[[], float] = time.monotonic, start_threads: bool = True):
        self._apply = apply
        # False only in tests, which step each session by hand.
        self._start_threads = start_threads
        self._before_begin = before_begin
        self._before_code = before_code
        self._before_apply = before_apply
        self._client_factory = client_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._session: Optional[PairingSession] = None

    def current(self) -> Optional[PairingSession]:
        return self._session

    def _new(self, unit_name: str, actor: Optional[str], start_thread: Optional[bool]) -> PairingSession:
        from app.core import device_id
        from app.core.fisqua_pairing_http import FisquaPairingClient, pairing_target

        found = device_id.state()
        if found["error"] is not None:
            raise PairingRefused(f"device_id_{found['error']}")
        if not isinstance(unit_name, str) or not 1 <= len(unit_name) <= fc.UNIT_NAME_MAX_CHARS:
            raise PairingRefused("unit_name")
        if self._before_begin is not None:
            reason = self._before_begin()
            if reason is not None:
                raise PairingRefused(reason)
        target = pairing_target()
        client = (self._client_factory or FisquaPairingClient)(target.base_url)
        session: Optional[PairingSession] = None
        before_apply = None
        if self._before_apply is not None:
            def before_apply(payload):
                return self._before_apply(payload, session)
        session = PairingSession(client=client, environment=target.environment, device_id=found["deviceId"],
                                 unit_name=unit_name, current_tenant=target.current_tenant,
                                 apply=lambda payload: self._apply(payload, session),
                                 before_code=self._before_code, before_apply=before_apply, clock=self._clock,
                                 actor=actor)
        self._session = session
        if start_thread if start_thread is not None else self._start_threads:
            session.start()
        return session

    def begin(self, unit_name: str, *, actor: Optional[str] = None,
              start_thread: Optional[bool] = None) -> PairingSession:
        """The running session if there is one, else a new one. PairingRefused when one cannot be begun."""
        with self._lock:
            if self._session is not None and self._session.running:
                return self._session
            return self._new(unit_name, actor, start_thread)

    def renew(self, *, actor: Optional[str] = None, start_thread: Optional[bool] = None) -> PairingSession:
        """A new session with the same unit name, after `lapsed`, `lost_answer` or `unknown` only."""
        with self._lock:
            if self._session is None or self._session.state not in RENEWABLE:
                raise PairingRefused("not_renewable")
            return self._new(self._session.unit_name, actor or self._session.actor, start_thread)

    def cancel(self) -> bool:
        """Cancel the current session unless it is applying, and wait for its thread."""
        with self._lock:
            session = self._session
        if session is None:
            return False
        cancelled = session.cancel()
        if cancelled:
            session.stop()
        return cancelled

    def shutdown(self, timeout: float = STOP_JOIN_SECONDS) -> None:
        """At process exit: cancel the current session and wait for its thread. An apply in progress is not waited for past `timeout`."""
        with self._lock:
            session = self._session
        if session is not None:
            session.stop(timeout=timeout)
