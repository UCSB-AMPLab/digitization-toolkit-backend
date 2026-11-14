"""The pairing session (contract 1.11 §12): its states, its timers and its secret, on a fake clock and a fake client.

Each test drives PairingSession.step() by hand, advancing a fake
monotonic clock; only the thread tests run the session's own thread, with
waits of a few milliseconds.

    python -m pytest tests/unit/test_fisqua_pairing_session.py

Version: 0.3.3
"""

import logging
import threading
import time

import pytest

from tests.unit.test_fisqua_pairing_http import CODE, READ_APP_KEY, SECRET, TOKEN, WRITE_APP_KEY, payload

SECOND_CODE = "4HWB9R"
SECOND_SECRET = "9a" * 32


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def answer(outcome, **fields):
    from app.core.fisqua_pairing_http import PairingAnswer

    return PairingAnswer(outcome=outcome, **fields)


def code(value=CODE, secret=SECRET, poll=5, expires=120):
    return answer("code", status_code=201, code=value, collect_secret=secret, expires_in_seconds=expires,
                  poll_seconds=poll)


class FakeClient:
    """Answers request_code and collect from two scripts, and keeps what it was asked."""

    def __init__(self, codes=(), collects=()):
        self.codes = list(codes)
        self.collects = list(collects)
        self.code_requests = []
        self.collect_requests = []
        self.closed = False

    def request_code(self, unit_name, device_id, current_tenant):
        self.code_requests.append((unit_name, device_id, current_tenant))
        return self.codes.pop(0) if self.codes else answer("network", error="no more answers")

    def collect(self, code_value, secret):
        self.collect_requests.append((code_value, secret))
        return self.collects.pop(0) if self.collects else answer("waiting", status_code=202)

    def close(self):
        self.closed = True


class Applied:
    def __init__(self, result=None, raises=None):
        self.calls = []
        self.result = result
        self.raises = raises

    def __call__(self, body, *session):
        self.calls.append(body)
        if self.raises:
            raise self.raises
        return self.result


def session(client, clock, *, environment="production", current_tenant=None, apply=None, **kwargs):
    from app.core.fisqua_pairing import PairingSession

    return PairingSession(client=client, environment=environment, device_id="abcdefgh", unit_name="Sala 1",
                          current_tenant=current_tenant, apply=apply or Applied(), clock=clock, **kwargs)


def run_until(s, clock, state, limit=500):
    """Step the session, touching it and advancing the clock by each wait, until it reaches `state`."""
    for _ in range(limit):
        if s.state == state:
            return
        wait = s.step()
        if wait is None:
            break
        s.touch()
        clock.advance(wait)
    assert s.state == state


def secret_held(s):
    return s._secret is not None or s._code is not None or s._payload is not None


# ---------------------------------------------------------------------------
# Codes, polling, collection
# ---------------------------------------------------------------------------

def test_shows_the_code_and_polls_every_poll_seconds():
    clock, client = Clock(), FakeClient(codes=[code(poll=7)])
    s = session(client, clock)
    assert s.state == "requesting"
    assert s.step() == 7
    snap = s.snapshot()
    assert (snap["state"], snap["code"], snap["expiresInSeconds"]) == ("showing", CODE, 120)
    assert snap["renewalSecondsLeft"] == 600
    assert client.code_requests == [("Sala 1", "abcdefgh", None)]
    # Not due yet: nothing sent.
    clock.advance(3)
    s.touch()
    assert s.step() == pytest.approx(4)
    assert client.collect_requests == []
    clock.advance(4)
    assert s.step() == 7
    assert client.collect_requests == [(CODE, SECRET)]
    assert s.snapshot()["expiresInSeconds"] == 113


def test_poll_seconds_floor_is_five():
    clock, client = Clock(), FakeClient(codes=[code(poll=1)])
    assert session(client, clock).step() == 5


def test_payload_applied_and_secret_dropped():
    clock = Clock()
    client = FakeClient(codes=[code()], collects=[answer("waiting"), answer("payload", status_code=200,
                                                                            payload=payload())])
    applied = Applied(result="restarting")
    s = session(client, clock, apply=applied)
    run_until(s, clock, "restarting")
    assert applied.calls == [payload()]
    assert not secret_held(s)
    assert client.closed
    assert s.step() is None


def test_apply_without_a_state_is_applied():
    clock = Clock()
    client = FakeClient(codes=[code()], collects=[answer("payload", payload=payload())])
    s = session(client, clock)
    run_until(s, clock, "applied")


def test_apply_that_raises_is_apply_failed():
    clock = Clock()
    client = FakeClient(codes=[code()], collects=[answer("payload", payload=payload())])
    s = session(client, clock, apply=Applied(raises=RuntimeError(f"failed with {TOKEN}")))
    run_until(s, clock, "apply_failed")
    assert not secret_held(s)


def test_apply_reason_kept_with_its_final_state_but_progress_dropped():
    from app.core.fisqua_pairing import PairingSession

    for reasons, state, expected in ((["finishing_uploads", "uploads_still_running"], "apply_failed",
                                      "uploads_still_running"),
                                     (["finishing_uploads", "writing_link"], "restarting", None)):
        clock = Clock()
        client = FakeClient(codes=[code()], collects=[answer("payload", payload=payload())])
        holder = {}

        def apply(body, reasons=reasons, state=state):
            for reason in reasons:
                holder["s"].set_reason(reason)
                assert holder["s"].snapshot()["reason"] == reason
            return state

        s = PairingSession(client=client, environment="production", device_id="abcdefgh", unit_name="Sala 1",
                           current_tenant=None, apply=apply, clock=clock)
        holder["s"] = s
        run_until(s, clock, state)
        assert s.snapshot()["reason"] == expected


def test_current_tenant_sent_when_linked():
    clock, client = Clock(), FakeClient(codes=[code()])
    session(client, clock, current_tenant="ahr").step()
    assert client.code_requests == [("Sala 1", "abcdefgh", "ahr")]


@pytest.mark.parametrize("name", ["", "x" * 65, None])
def test_unit_name_is_one_to_sixty_four_characters(name):
    from app.core.fisqua_pairing import PairingSession

    with pytest.raises(ValueError):
        PairingSession(client=FakeClient(), environment="production", device_id="abcdefgh", unit_name=name,
                       current_tenant=None, apply=Applied())


# ---------------------------------------------------------------------------
# The renewal window
# ---------------------------------------------------------------------------

def test_renews_on_expired_within_ten_minutes():
    clock = Clock()
    client = FakeClient(codes=[code(), code(SECOND_CODE, SECOND_SECRET)],
                        collects=[answer("expired", status_code=410)])
    s = session(client, clock)
    s.step()
    clock.advance(5)
    s.touch()
    assert s.step() == 0
    assert s.state == "requesting"
    s.step()
    assert s.snapshot()["code"] == SECOND_CODE
    clock.advance(5)
    s.step()
    assert client.collect_requests[-1] == (SECOND_CODE, SECOND_SECRET)
    # The window counts from the first code.
    assert s.snapshot()["renewalSecondsLeft"] == 590


def test_lapses_after_ten_minutes():
    clock = Clock()
    client = FakeClient(codes=[code(), code(SECOND_CODE, SECOND_SECRET)],
                        collects=[answer("waiting")] * 200 + [answer("expired", status_code=410)])
    s = session(client, clock)
    s.step()
    # 202 waiting keeps the code past the window: Fisqua decides expiry.
    for _ in range(121):
        clock.advance(5)
        s.touch()
        s.step()
    assert s.state == "showing"
    client.collects = [answer("expired", status_code=410)]
    clock.advance(5)
    s.touch()
    assert s.step() is None
    assert s.state == "lapsed"
    assert not secret_held(s)
    assert len(client.code_requests) == 1


def test_window_uses_the_injected_monotonic_clock():
    from app.core import fisqua_pairing

    assert fisqua_pairing.PairingSession.__init__.__kwdefaults__["clock"] is time.monotonic


# ---------------------------------------------------------------------------
# Final answers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("outcome,fields,state", [
    ("collected", {"status_code": 410}, "lost_answer"),
    ("unknown", {"status_code": 404}, "unknown"),
    ("method", {"status_code": 405}, "error"),
    ("unexpected", {"status_code": 418}, "error"),
])
def test_final_collect_answers(outcome, fields, state):
    clock = Clock()
    client = FakeClient(codes=[code()], collects=[answer(outcome, **fields)])
    s = session(client, clock)
    run_until(s, clock, state)
    assert not secret_held(s)
    assert client.closed
    assert s.step() is None


@pytest.mark.parametrize("outcome,fields,state,reason", [
    ("cannot_pair", {"status_code": 404}, "cannot_pair", None),
    ("refused", {"status_code": 400, "rule": "unit-name"}, "error", "unit-name"),
    ("method", {"status_code": 405}, "error", "method"),
])
def test_final_code_answers(outcome, fields, state, reason):
    clock = Clock()
    client = FakeClient(codes=[answer(outcome, **fields)])
    s = session(client, clock)
    assert s.step() is None
    assert (s.state, s.snapshot()["reason"]) == (state, reason)
    assert client.collect_requests == []


def test_wrong_environment_refused():
    clock = Clock()
    client = FakeClient(codes=[code()], collects=[answer("payload", payload=payload(environment="staging"))])
    applied = Applied()
    s = session(client, clock, apply=applied)
    run_until(s, clock, "wrong_environment")
    assert applied.calls == []
    assert not secret_held(s)


def test_bad_payload_names_the_field_only():
    clock = Clock()
    client = FakeClient(codes=[code()], collects=[answer("payload", payload=payload(token="XYZ"))])
    applied = Applied()
    s = session(client, clock, apply=applied)
    run_until(s, clock, "bad_payload")
    assert s.snapshot()["reason"] == "token"
    assert applied.calls == []
    assert not secret_held(s)


def test_unsent_work_blocks_tenant_change():
    """The gate is injected: whatever it names stops the session before apply, and nothing is applied."""
    clock = Clock()
    client = FakeClient(codes=[code()], collects=[answer("payload", payload=payload(tenant="other"))])
    applied = Applied()
    seen = []

    def gate(body):
        seen.append(body["tenant"])
        return "unsent_work" if body["tenant"] != "ahr" else None

    s = session(client, clock, current_tenant="ahr", apply=applied, before_apply=gate)
    run_until(s, clock, "unsent_work")
    assert seen == ["other"]
    assert applied.calls == []
    assert not secret_held(s)


def test_same_tenant_relinks():
    clock = Clock()
    client = FakeClient(codes=[code()], collects=[answer("payload", payload=payload())])
    applied = Applied()
    s = session(client, clock, current_tenant="ahr", apply=applied,
                before_apply=lambda body: "unsent_work" if body["tenant"] != "ahr" else None)
    run_until(s, clock, "applied")
    assert applied.calls == [payload()]


def test_mode_gate_checked_before_each_code():
    clock = Clock()
    client = FakeClient(codes=[code(), code(SECOND_CODE, SECOND_SECRET)], collects=[answer("expired")])
    allowed = [None, "mode_not_eligible"]
    s = session(client, clock, before_code=lambda: allowed.pop(0))
    s.step()
    clock.advance(5)
    s.touch()
    s.step()
    assert s.state == "requesting"
    assert s.step() is None
    assert (s.state, s.snapshot()["reason"]) == ("not_eligible", "mode_not_eligible")
    assert len(client.code_requests) == 1


# ---------------------------------------------------------------------------
# Busy and network trouble
# ---------------------------------------------------------------------------

def test_busy_waits_retry_after_with_the_code_unchanged():
    clock = Clock()
    client = FakeClient(codes=[code(poll=5)], collects=[answer("busy", status_code=503, retry_after=30.0),
                                                        answer("busy", status_code=429, retry_after=1.0),
                                                        answer("busy", status_code=429)])
    s = session(client, clock)
    s.step()
    clock.advance(5)
    s.touch()
    assert s.step() == 30
    snap = s.snapshot()
    assert (snap["state"], snap["trouble"], snap["retryAfterSeconds"], snap["code"]) == ("showing", "busy", 30, CODE)
    clock.advance(30)
    s.touch()
    assert s.step() == 5  # Retry-After below pollSeconds: pollSeconds
    clock.advance(5)
    s.touch()
    assert s.step() == 5  # no Retry-After
    assert [c[0] for c in client.collect_requests] == [CODE] * 3
    assert len(client.code_requests) == 1


def test_busy_on_codes_waits_at_least_five_seconds():
    clock = Clock()
    client = FakeClient(codes=[answer("busy", status_code=429, retry_after=2.0), code()])
    s = session(client, clock)
    assert s.step() == 5
    assert s.state == "requesting"
    clock.advance(5)
    s.touch()
    s.step()
    assert s.state == "showing"


def test_network_trouble_backs_off_and_recovers():
    clock = Clock()
    client = FakeClient(codes=[code()], collects=[answer("network")] * 6 + [answer("waiting")])
    s = session(client, clock)
    s.step()
    clock.advance(5)
    backoffs = []
    for _ in range(7):
        # Touched at every step and every 15 s, as the card does.
        s.touch()
        s.step()
        backoffs.append(s.snapshot()["retryAfterSeconds"])
        left = backoffs[-1] if backoffs[-1] is not None else 5
        while left > 0:
            clock.advance(min(left, 15))
            left -= 15
            s.touch()
    assert backoffs == [5, 10, 20, 40, 60, 60, None]
    assert s.snapshot()["trouble"] is None
    assert len(client.collect_requests) == 7


def test_a_long_wait_wakes_for_the_heartbeat():
    clock = Clock()
    client = FakeClient(codes=[code()], collects=[answer("network")] * 4)
    s = session(client, clock)
    s.step()
    for seconds in (5, 5, 10, 20):
        clock.advance(seconds)
        s.touch()
        s.step()
    # 40 s of backoff: step answers 30, the heartbeat's limit, and sends nothing then.
    clock.advance(0)
    assert s.step() == 30
    clock.advance(30)
    s.touch()
    assert s.step() == 10
    assert len(client.collect_requests) == 4


def test_network_trouble_past_the_window_lapses():
    clock = Clock()
    client = FakeClient(codes=[code()], collects=[answer("network")] * 100)
    s = session(client, clock)
    run_until(s, clock, "lapsed")
    assert clock.now - 1000 >= 600
    assert not secret_held(s)


def test_network_trouble_before_the_first_code_retries():
    clock = Clock()
    client = FakeClient(codes=[answer("network"), answer("network"), code()])
    s = session(client, clock)
    assert s.step() == 5
    assert s.snapshot()["trouble"] == "network"
    clock.advance(5)
    s.touch()
    assert s.step() == 10
    clock.advance(10)
    s.touch()
    s.step()
    assert s.state == "showing"


# ---------------------------------------------------------------------------
# The heartbeat
# ---------------------------------------------------------------------------

def test_heartbeat_loss_drops_secret():
    clock = Clock()
    client = FakeClient(codes=[code()])
    s = session(client, clock)
    s.step()
    clock.advance(29)
    s.touch()
    clock.advance(29)
    assert s.step() is not None
    assert s.state == "showing"
    clock.advance(2)
    assert s.step() is None
    assert s.state == "ended"
    assert not secret_held(s)
    assert client.closed
    assert s.snapshot()["code"] is None


def test_wait_never_outlasts_the_heartbeat():
    clock = Clock()
    client = FakeClient(codes=[code()], collects=[answer("busy", retry_after=120.0)])
    s = session(client, clock)
    s.step()
    clock.advance(5)
    s.touch()
    assert s.step() == 30


def test_cancel_drops_secret():
    clock, client = Clock(), FakeClient(codes=[code()])
    s = session(client, clock)
    s.step()
    s.cancel()
    assert s.state == "cancelled"
    assert not secret_held(s)
    assert s.step() is None


# ---------------------------------------------------------------------------
# The secret
# ---------------------------------------------------------------------------

def test_secret_never_in_logs_or_repr(caplog):
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    client = FakeClient(codes=[code()], collects=[answer("busy", retry_after=5.0), answer("network"),
                                                  answer("payload", payload=payload())])
    seen = []

    def apply(body):
        seen.append(repr(s) + str(s))
        raise RuntimeError("could not apply")

    s = session(client, clock, apply=apply)
    s.step()
    seen.append(repr(s) + str(s))
    run_until(s, clock, "apply_failed")
    text = caplog.text + " ".join(seen) + repr(s) + str(s)
    for secret in (SECRET, CODE, TOKEN, WRITE_APP_KEY, READ_APP_KEY):
        assert secret not in text
    assert "showing" in seen[0]


def test_nothing_written_to_disk(tmp_path, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "DATA_DIR", str(tmp_path / "data"))
    clock = Clock()
    client = FakeClient(codes=[code()], collects=[answer("payload", payload=payload())])
    s = session(client, clock)
    run_until(s, clock, "applied")
    assert not (tmp_path / "data").exists()


# ---------------------------------------------------------------------------
# The thread
# ---------------------------------------------------------------------------

def _pairing_threads():
    return [t for t in threading.enumerate() if t.name == "fisqua-pairing" and t.is_alive()]


def test_thread_polls_and_stops_leaving_no_thread():
    clock = Clock()
    client = FakeClient(codes=[code(poll=5)])
    s = session(client, clock)
    real_step = s.step

    def quick_step():
        # The fake clock moves by each wait; the thread itself waits a millisecond.
        wait = real_step()
        if wait is None:
            return None
        s.touch()
        clock.advance(wait)
        return 0.001

    s.step = quick_step
    s.start()
    deadline = time.monotonic() + 2
    while len(client.collect_requests) < 2 and time.monotonic() < deadline:
        time.sleep(0.002)
    assert len(client.collect_requests) >= 2
    assert _pairing_threads()
    s.stop()
    assert not _pairing_threads()
    assert s.state == "cancelled"
    assert not secret_held(s)
    assert client.closed


def test_thread_ends_by_itself_on_a_final_answer():
    client = FakeClient(codes=[answer("cannot_pair", status_code=404)])
    s = session(client, Clock())
    s.start()
    s._thread.join(timeout=2)
    assert not _pairing_threads()
    assert s.state == "cannot_pair"


def test_status_reads_do_not_wait_for_a_request_in_flight():
    """A snapshot during a slow call answers at once: the call is made without the session's lock."""
    release = threading.Event()
    entered = threading.Event()

    class SlowClient(FakeClient):
        def request_code(self, *args):
            entered.set()
            release.wait(2)
            return code()

    client = SlowClient()
    s = session(client, Clock())
    s.start()
    try:
        assert entered.wait(2)
        started = time.monotonic()
        assert s.snapshot()["state"] == "requesting"
        s.touch()
        assert time.monotonic() - started < 0.1
    finally:
        release.set()
        s.stop()
    assert not _pairing_threads()


def test_cancel_during_a_request_discards_its_answer():
    release = threading.Event()
    entered = threading.Event()

    class SlowClient(FakeClient):
        def collect(self, *args):
            entered.set()
            release.wait(2)
            return answer("payload", payload=payload())

    client = SlowClient(codes=[code()])
    applied = Applied()
    clock = Clock()
    s = session(client, clock, apply=applied)
    s.step()
    clock.advance(5)
    s.touch()
    worker = threading.Thread(target=s.step)
    worker.start()
    assert entered.wait(2)
    s.cancel()
    release.set()
    worker.join(2)
    assert s.state == "cancelled"
    assert applied.calls == []
    assert not secret_held(s)


# ---------------------------------------------------------------------------
# One session per process, and a new code after a final answer
# ---------------------------------------------------------------------------

@pytest.fixture
def unit(monkeypatch, tmp_path):
    from app.core import config

    monkeypatch.setattr(config.settings, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config.settings, "FISQUA_ENV", "")
    monkeypatch.setattr(config.settings, "FISQUA_PAIRING_BASE_URL", "")
    return tmp_path / "data"


def registry(clients, clock, **kwargs):
    """A registry whose sessions take their clients, in order, from `clients`."""
    from app.core import fisqua_pairing

    made = []

    def factory(base_url):
        made.append(base_url)
        return clients.pop(0)

    reg = fisqua_pairing.PairingRegistry(apply=kwargs.pop("apply", Applied()), client_factory=factory,
                                         clock=clock, **kwargs)
    reg.made = made
    return reg


def test_registry_refuses_without_a_device_id(unit):
    from app.core import fisqua_pairing

    reg = registry([FakeClient()], Clock())
    with pytest.raises(fisqua_pairing.PairingRefused) as refused:
        reg.begin("Sala 1", start_thread=False)
    assert refused.value.reason == "device_id_missing"
    assert reg.current() is None


def test_registry_keeps_one_session(unit):
    from app.core import device_id

    device_id.initialise()
    clock = Clock()
    reg = registry([FakeClient(codes=[code()]), FakeClient(codes=[code()])], clock)
    first = reg.begin("Sala 1", start_thread=False)
    assert reg.begin("Sala 2", start_thread=False) is first
    assert reg.current() is first
    assert reg.made == ["https://fisqua.org"]
    reg.cancel()
    assert first.state == "cancelled"
    assert reg.current() is first  # its final state stays readable
    other = reg.begin("Sala 2", start_thread=False)
    assert other is not first
    assert other.snapshot()["unitName"] == "Sala 2"


def test_registry_refuses_when_the_mode_gate_says_so(unit):
    from app.core import device_id, fisqua_pairing

    device_id.initialise()
    reg = registry([FakeClient()], Clock(), before_begin=lambda: "mode_not_eligible")
    with pytest.raises(fisqua_pairing.PairingRefused) as refused:
        reg.begin("Sala 1", start_thread=False)
    assert refused.value.reason == "mode_not_eligible"
    assert reg.current() is None


@pytest.mark.parametrize("final", ["lapsed", "lost_answer", "unknown"])
def test_renew_after_a_final_answer_starts_a_new_window(unit, final):
    from app.core import device_id

    device_id.initialise()
    clock = Clock()
    collect = {"lapsed": answer("expired"), "lost_answer": answer("collected"), "unknown": answer("unknown")}[final]
    first_client = FakeClient(codes=[code()], collects=[collect])
    reg = registry([first_client, FakeClient(codes=[code(SECOND_CODE, SECOND_SECRET)])], clock)
    first = reg.begin("Sala 1", start_thread=False)
    first.step()
    clock.advance(601 if final == "lapsed" else 5)
    first.touch()
    first.step()
    assert first.state == final
    assert len(first_client.code_requests) == 1
    renewed = reg.renew(start_thread=False)
    assert renewed is not first
    renewed.step()
    snap = renewed.snapshot()
    assert (snap["state"], snap["code"], snap["renewalSecondsLeft"], snap["unitName"]) == (
        "showing", SECOND_CODE, 600, "Sala 1")


def test_renew_refused_while_a_session_runs_or_after_other_ends(unit):
    from app.core import device_id, fisqua_pairing

    device_id.initialise()
    clock = Clock()
    reg = registry([FakeClient(codes=[code()]), FakeClient(codes=[answer("cannot_pair")])], clock)
    with pytest.raises(fisqua_pairing.PairingRefused):
        reg.renew(start_thread=False)
    running = reg.begin("Sala 1", start_thread=False)
    running.step()
    with pytest.raises(fisqua_pairing.PairingRefused) as refused:
        reg.renew(start_thread=False)
    assert refused.value.reason == "not_renewable"
    reg.cancel()
    final = reg.begin("Sala 1", start_thread=False)
    final.step()
    assert final.state == "cannot_pair"
    with pytest.raises(fisqua_pairing.PairingRefused):
        reg.renew(start_thread=False)


def test_registry_threads_stop(unit):
    from app.core import device_id

    device_id.initialise()
    reg = registry([FakeClient(codes=[code()])], Clock())
    reg.begin("Sala 1")
    reg.cancel()
    assert not _pairing_threads()
