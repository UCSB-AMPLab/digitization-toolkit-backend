"""Login throttle: lockout after repeated failures, reset on success, and that the /auth/login endpoint is actually wired to it.

An account locks after 5 failed sign-ins, from whichever machines they
came; a machine (the client address) locks after 20, across whichever
accounts they named. Both counts use the same window and lockout time.
The 429 names which lock it met in `detail.code`: `account_locked` or
`machine_locked`, the account's when both hold. A successful sign-in clears
the account's count only, never the machine's.

    python -m pytest tests/unit/test_login_throttle.py

Version: 0.4.0
"""

import pytest

PASSWORD = "fake-right-pw"
WRONG = "fake-wrong-pw"


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def test_locks_after_max_failures_then_unlocks_after_timeout():
    from app.core.login_throttle import LoginThrottle
    clk = FakeClock()
    th = LoginThrottle(account_max_failures=3, lockout_seconds=60, window_seconds=300, clock=clk)
    key = "user:alice"
    assert th.retry_after(key) == 0
    for _ in range(3):
        th.record_failure(key)
    assert 0 < th.retry_after(key) <= 60
    clk.advance(61)
    assert th.retry_after(key) == 0


def test_reset_clears_lockout():
    from app.core.login_throttle import LoginThrottle
    clk = FakeClock()
    th = LoginThrottle(machine_max_failures=2, lockout_seconds=60, clock=clk)
    key = "ip:1.2.3.4"
    th.record_failure(key)
    th.record_failure(key)
    assert th.retry_after(key) > 0
    th.reset(key)
    assert th.retry_after(key) == 0


def test_an_account_locks_at_5_failures_and_a_machine_at_20():
    from app.core.login_throttle import LoginThrottle
    th = LoginThrottle(clock=FakeClock())
    for _ in range(4):
        th.record_failure("user:alice", "ip:10.0.0.5")
    assert th.retry_after("user:alice") == 0
    th.record_failure("user:alice", "ip:10.0.0.5")
    assert th.retry_after("user:alice") > 0
    assert th.retry_after("ip:10.0.0.5") == 0
    for _ in range(14):
        th.record_failure("ip:10.0.0.5")
    assert th.retry_after("ip:10.0.0.5") == 0
    th.record_failure("ip:10.0.0.5")
    assert th.retry_after("ip:10.0.0.5") > 0


def test_lockout_names_the_locked_keys_in_the_order_given():
    from app.core.login_throttle import LoginThrottle
    th = LoginThrottle(account_max_failures=1, machine_max_failures=1, lockout_seconds=60, clock=FakeClock())
    assert th.lockout("user:alice", "ip:10.0.0.5") == (0, ())
    th.record_failure("ip:10.0.0.5")
    assert th.lockout("user:alice", "ip:10.0.0.5") == (60, ("ip:10.0.0.5",))
    th.record_failure("user:alice")
    assert th.lockout("user:alice", "ip:10.0.0.5") == (60, ("user:alice", "ip:10.0.0.5"))


def test_a_key_of_no_known_kind_is_refused():
    from app.core.login_throttle import LoginThrottle
    th = LoginThrottle()
    with pytest.raises(ValueError):
        th.record_failure("alice")


def test_login_endpoint_returns_429_after_repeated_failures(client, db_session, monkeypatch):
    import app.api.auth as auth_mod
    from app.core.login_throttle import LoginThrottle
    from app.models.user import User
    from app.core.security import hash_password

    monkeypatch.setattr(auth_mod, "login_throttle", LoginThrottle(account_max_failures=3, lockout_seconds=60))
    db_session.add(User(username="alice", email="a@example.com",
                        hashed_password=hash_password("rightpw"), role="admin", is_active=True))
    db_session.commit()

    for _ in range(3):
        assert client.post("/auth/login", json={"username": "alice", "password": "wrong"}).status_code == 401
    resp = client.post("/auth/login", json={"username": "alice", "password": "wrong"})
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


# ---------------------------------------------------------------------------
# The endpoint with the throttle's own limits
# ---------------------------------------------------------------------------

@pytest.fixture
def throttle(monkeypatch):
    """A throttle with the limits the unit runs with, of this test alone."""
    from app.api import auth as auth_mod
    from app.core.login_throttle import LoginThrottle
    th = LoginThrottle()
    monkeypatch.setattr(auth_mod, "login_throttle", th)
    return th


def _user(db, username):
    from app.core.security import hash_password
    from app.models.user import User
    db.add(User(username=username, hashed_password=hash_password(PASSWORD), role="operator", is_active=True))
    db.commit()


def _login(client, username, password, machine):
    return client.post("/auth/login", json={"username": username, "password": password},
                       headers={"X-Real-IP": machine})


def _locked(resp, code):
    assert resp.status_code == 429, resp.text
    assert resp.json()["detail"]["code"] == code, resp.text
    assert resp.json()["detail"]["message"]
    assert int(resp.headers["Retry-After"]) > 0


@pytest.mark.unit
def test_five_failures_lock_the_account_from_any_machine(client, db_session, throttle):
    _user(db_session, "alice")
    for _ in range(5):
        assert _login(client, "alice", WRONG, "10.0.0.5").status_code == 401
    _locked(_login(client, "alice", PASSWORD, "10.0.0.5"), "account_locked")
    _locked(_login(client, "alice", PASSWORD, "10.0.0.9"), "account_locked")


@pytest.mark.unit
def test_nineteen_failures_from_one_machine_do_not_lock_it_and_the_twentieth_does(client, db_session, throttle):
    _user(db_session, "carla")
    # Four accounts of four failures and three of one: 19 from the kiosk, none of them five on one account.
    names = [f"op{i}" for i in range(4) for _ in range(4)] + ["op4", "op5", "op6"]
    assert len(names) == 19
    for name in names:
        assert _login(client, name, WRONG, "10.0.0.5").status_code == 401
    assert _login(client, "op7", WRONG, "10.0.0.5").status_code == 401  # the twentieth
    _locked(_login(client, "carla", PASSWORD, "10.0.0.5"), "machine_locked")
    assert _login(client, "carla", PASSWORD, "10.0.0.9").status_code == 200


@pytest.mark.unit
def test_when_both_are_locked_the_account_lock_is_reported(client, db_session, throttle):
    _user(db_session, "alice")
    for _ in range(5):
        _login(client, "alice", WRONG, "10.0.0.5")
    for i in range(15):
        _login(client, f"op{i % 3}", WRONG, "10.0.0.5")
    _locked(_login(client, "alice", PASSWORD, "10.0.0.5"), "account_locked")
    _locked(_login(client, "bob", PASSWORD, "10.0.0.5"), "machine_locked")
    _locked(_login(client, "alice", PASSWORD, "10.0.0.9"), "account_locked")


@pytest.mark.unit
def test_the_429_says_in_english_which_is_locked(client, db_session, throttle):
    _user(db_session, "alice")
    for _ in range(5):
        _login(client, "alice", WRONG, "10.0.0.5")
    account = _login(client, "alice", PASSWORD, "10.0.0.5").json()["detail"]
    for i in range(15):
        _login(client, f"op{i % 3}", WRONG, "10.0.0.5")
    machine = _login(client, "bob", PASSWORD, "10.0.0.5").json()["detail"]
    assert account == {"code": "account_locked",
                       "message": "Too many failed login attempts for this account. Try again later."}
    assert machine == {"code": "machine_locked",
                       "message": "Too many failed login attempts from this machine. Try again later."}


@pytest.mark.unit
def test_a_successful_sign_in_does_not_clear_the_machine_count(client, db_session, throttle):
    """One valid account cannot reset the station's count to keep trying others past 20."""
    _user(db_session, "carla")
    names = [f"op{i}" for i in range(4) for _ in range(4)] + ["op4", "op5", "op6"]
    for name in names:
        assert _login(client, name, WRONG, "10.0.0.5").status_code == 401
    assert _login(client, "carla", PASSWORD, "10.0.0.5").status_code == 200
    assert _login(client, "op7", WRONG, "10.0.0.5").status_code == 401  # the twentieth
    _locked(_login(client, "carla", PASSWORD, "10.0.0.5"), "machine_locked")


@pytest.mark.unit
def test_accounts_whose_names_differ_only_in_case_have_counts_of_their_own(client, db_session, throttle):
    """Login compares the username exactly, so `Alice` and `alice` are two accounts and two counts."""
    _user(db_session, "Alice")
    _user(db_session, "alice")
    for _ in range(5):
        assert _login(client, "Alice", WRONG, "10.0.0.5").status_code == 401
    _locked(_login(client, "Alice", PASSWORD, "10.0.0.5"), "account_locked")
    assert _login(client, "alice", PASSWORD, "10.0.0.9").status_code == 200

    for _ in range(4):
        assert _login(client, "alice", WRONG, "10.0.0.9").status_code == 401
    assert _login(client, "ALICE", WRONG, "10.0.0.9").status_code == 401
    assert _login(client, "alice", PASSWORD, "10.0.0.9").status_code == 200


@pytest.mark.unit
def test_a_success_as_one_case_does_not_clear_the_other_cases_count(client, db_session, throttle):
    _user(db_session, "Alice")
    _user(db_session, "alice")
    for _ in range(4):
        assert _login(client, "Alice", WRONG, "10.0.0.5").status_code == 401
    assert _login(client, "alice", PASSWORD, "10.0.0.9").status_code == 200
    assert _login(client, "Alice", WRONG, "10.0.0.5").status_code == 401  # Alice's fifth
    _locked(_login(client, "Alice", PASSWORD, "10.0.0.5"), "account_locked")


@pytest.mark.unit
def test_a_sign_in_refused_at_the_hash_upgrade_keeps_the_earlier_failures(client, db_session, throttle, monkeypatch):
    """The count is cleared only once the sign-in has succeeded: a correct password whose
    legacy hash is replaced meanwhile is refused, and the four failures before it still count."""
    from app.api import auth as auth_mod
    from app.core import security
    from app.models.user import User
    _user(db_session, "alice")
    user = db_session.query(User).filter(User.username == "alice").one()
    legacy = "legacysalt$" + security._pbkdf2(PASSWORD, "legacysalt", security._LEGACY_ITERATIONS)
    user.hashed_password = legacy
    db_session.commit()
    assert security.needs_rehash(legacy)
    for _ in range(4):
        assert _login(client, "alice", WRONG, "10.0.0.5").status_code == 401

    real = security.verify_password

    def verify_then_password_changes(password, hashed):
        ok = real(password, hashed)
        db_session.query(User).filter(User.id == user.id).update(
            {User.hashed_password: security.hash_password("fake-set-meanwhile")},
            synchronize_session=False)
        db_session.commit()
        return ok

    monkeypatch.setattr(auth_mod, "verify_password", verify_then_password_changes)
    assert _login(client, "alice", PASSWORD, "10.0.0.5").status_code == 401  # the fifth failure
    monkeypatch.setattr(auth_mod, "verify_password", real)
    _locked(_login(client, "alice", "fake-set-meanwhile", "10.0.0.5"), "account_locked")
