"""Login throttle: lockout after repeated failures, reset on success, and that the /auth/login endpoint is actually wired to it."""


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
    th = LoginThrottle(max_failures=3, lockout_seconds=60, window_seconds=300, clock=clk)
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
    th = LoginThrottle(max_failures=2, lockout_seconds=60, clock=clk)
    key = "ip:1.2.3.4"
    th.record_failure(key)
    th.record_failure(key)
    assert th.retry_after(key) > 0
    th.reset(key)
    assert th.retry_after(key) == 0


def test_login_endpoint_returns_429_after_repeated_failures(client, db_session, monkeypatch):
    import app.api.auth as auth_mod
    from app.core.login_throttle import LoginThrottle
    from app.models.user import User
    from app.core.security import hash_password

    monkeypatch.setattr(auth_mod, "login_throttle", LoginThrottle(max_failures=3, lockout_seconds=60))
    db_session.add(User(username="alice", email="a@example.com",
                        hashed_password=hash_password("rightpw"), role="admin", is_active=True))
    db_session.commit()

    for _ in range(3):
        assert client.post("/auth/login", json={"username": "alice", "password": "wrong"}).status_code == 401
    resp = client.post("/auth/login", json={"username": "alice", "password": "wrong"})
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
