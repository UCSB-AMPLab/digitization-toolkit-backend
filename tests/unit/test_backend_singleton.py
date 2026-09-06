"""get_backend() must not race on the module-global singleton.

get_backend() does an unsynchronised check-then-set on `capture.service._backend`.
Camera endpoints run on FastAPI's thread pool, and the kiosk fires a device
list plus two previews on boot, so two first callers can each pass the
`if _backend is None:` check before either one assigns - each constructs its
own backend, and the loser's PTP sessions and locks are never seen by the
winner holding the module global.

The fake constructor below models exactly that window: it counts entries,
records (and signals) each entry into the constructor body, and then blocks
on a shared release Event before returning a backend object. That lets a
test hold a first caller inside the constructor and observe whether a second
caller reaches the constructor body before the first one has returned.
"""

import threading
import time

import pytest

import capture.service as capture_service


class _FakeBackend:
    """Stand-in for a CameraBackend - only get_backend_name() is used by
    the code under test (it is logged on construction)."""

    def get_backend_name(self):
        return "fake"


class _FakeConstructor:
    """Fake for get_camera_backend(). Counts calls under its own lock,
    records one threading.Event per entry (set as soon as the call begins),
    and then blocks on the shared `release` Event before returning a fresh
    _FakeBackend(). With `raise_on_first=True`, the first call raises
    RuntimeError("boom") instead of blocking/returning.
    """

    def __init__(self, release: threading.Event, raise_on_first: bool = False):
        self._release = release
        self._raise_on_first = raise_on_first
        self._lock = threading.Lock()
        self._entries_cond = threading.Condition(self._lock)
        self.entries = []
        self.call_count = 0

    def __call__(self):
        with self._lock:
            self.call_count += 1
            call_index = self.call_count
            entry = threading.Event()
            self.entries.append(entry)
            self._entries_cond.notify_all()
        entry.set()

        if self._raise_on_first and call_index == 1:
            raise RuntimeError("boom")

        if not self._release.wait(timeout=30):
            raise AssertionError("fake constructor was never released")
        return _FakeBackend()

    def wait_for_entries(self, count: int, timeout: float) -> bool:
        """Block (bounded) until at least `count` calls have begun."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while len(self.entries) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                if not self._entries_cond.wait(remaining):
                    return False
            return True


@pytest.fixture(autouse=True)
def reset_backend():
    original = capture_service._backend
    capture_service._backend = None
    yield
    capture_service._backend = original


def test_overlapping_first_callers_share_one_backend(monkeypatch):
    release = threading.Event()
    fake_constructor = _FakeConstructor(release)
    monkeypatch.setattr(capture_service, "get_camera_backend", fake_constructor)

    results = {}
    errors = {}

    def call_a():
        try:
            results["A"] = capture_service.get_backend()
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["A"] = exc

    def call_b():
        try:
            results["B"] = capture_service.get_backend()
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors["B"] = exc

    thread_a = threading.Thread(target=call_a, name="caller-A")
    thread_b = threading.Thread(target=call_b, name="caller-B")
    started = []

    try:
        thread_a.start()
        started.append(thread_a)
        assert fake_constructor.wait_for_entries(1, timeout=5.0), (
            "thread A never reached the constructor"
        )

        # Deterministic proof, taken while A is blocked inside the fake
        # constructor: construction can only be underway right now if
        # _backend_lock is held. This is the mechanism assertion; the
        # timed second-entry observation below is the behavioural check
        # layered on top of it (a reviewer showed the timing alone can pass
        # against unfixed code if thread B happens to be scheduled late).
        lock = getattr(capture_service, "_backend_lock", None)
        assert lock is not None, "get_backend() has no construction lock"
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        assert not acquired, "construction ran outside _backend_lock"

        thread_b.start()
        started.append(thread_b)

        # On unfixed code, B's None-check is not held back by anything before it
        # reaches the constructor, so a second entry fires almost immediately.
        # On fixed code, B blocks on _backend_lock before the check, and no
        # second entry appears within the wait.
        second_entry_seen = fake_constructor.wait_for_entries(2, timeout=1.0)
    finally:
        # Unbounded joins: a worker can only be inside the fake constructor
        # (which caps its own wait at 30 s) or blocked on _backend_lock (freed
        # when A leaves), so every started thread terminates before the
        # fixture restores the module global. A bounded join would only
        # attempt cleanup; a late worker could then overwrite the restored
        # value with its fake.
        release.set()
        for thread in started:
            thread.join()

    assert not errors, errors
    assert not second_entry_seen, (
        "a second caller entered get_camera_backend() before the first "
        "caller's construction completed - the check-then-set is not locked"
    )
    assert fake_constructor.call_count == 1
    assert results["A"] is results["B"]
    assert capture_service._backend is results["A"]


def test_failed_construction_leaves_singleton_unset_and_retries(monkeypatch):
    release = threading.Event()
    release.set()  # this test is sequential; nothing needs to block on it
    fake_constructor = _FakeConstructor(release, raise_on_first=True)
    monkeypatch.setattr(capture_service, "get_camera_backend", fake_constructor)

    with pytest.raises(RuntimeError, match="boom"):
        capture_service.get_backend()

    assert capture_service._backend is None

    result = capture_service.get_backend()

    assert isinstance(result, _FakeBackend)
    assert capture_service._backend is result
    assert fake_constructor.call_count == 2


def test_repeat_calls_return_same_object(monkeypatch):
    release = threading.Event()
    release.set()
    fake_constructor = _FakeConstructor(release)
    monkeypatch.setattr(capture_service, "get_camera_backend", fake_constructor)

    first = capture_service.get_backend()
    second = capture_service.get_backend()

    assert first is second
    assert fake_constructor.call_count == 1
    assert capture_service._backend is first
