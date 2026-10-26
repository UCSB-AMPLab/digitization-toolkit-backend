"""The registry of free and restore jobs (app.core.offsite_free.OffsiteJobRegistry).

A finished job is kept for FINISHED_JOB_TTL after it ends, so the page that
started it can read its result, and then dropped, so the registry does not
grow for as long as the backend runs. A running job is never dropped.

    python -m pytest tests/unit/test_offsite_job_registry.py

Version: 0.1.0-beta
"""

import logging
import threading

import pytest


class _Hold:
    def __init__(self):
        self.released = threading.Event()

    def release(self):
        self.released.set()


def _finish(registry, job, work=lambda: {}):
    hold = _Hold()
    registry.run(job, work, hold)
    assert hold.released.wait(10), "the job did not finish"
    # finished_at is set before the hold is released; wait for the active slot to clear too
    for _ in range(1000):
        if job.finished_at is not None:
            break
        threading.Event().wait(0.01)
    return job


@pytest.mark.unit
def test_a_finished_job_is_dropped_once_it_is_older_than_the_ttl():
    from app.core.offsite_free import FINISHED_JOB_TTL, OffsiteJobRegistry

    registry = OffsiteJobRegistry()
    old = _finish(registry, registry.reserve(1, "free"))
    recent = _finish(registry, registry.reserve(2, "free"))
    old.finished_at -= FINISHED_JOB_TTL + 1

    registry.reserve(3, "restore")

    assert registry.get(old.id) is None
    assert registry.get(recent.id) is recent


@pytest.mark.unit
def test_a_queued_or_running_job_is_never_dropped():
    from app.core.offsite_free import FINISHED_JOB_TTL, OffsiteJobRegistry

    registry = OffsiteJobRegistry()
    queued = registry.reserve(1, "free")
    queued.created_at -= FINISHED_JOB_TTL + 1
    gate = threading.Event()
    running = registry.reserve(2, "free")
    running.created_at -= FINISHED_JOB_TTL + 1
    hold = _Hold()
    registry.run(running, lambda: gate.wait(10) and {}, hold)
    try:
        registry.reserve(3, "free")
        assert registry.get(queued.id) is queued
        assert registry.get(running.id) is running
    finally:
        gate.set()
        assert hold.released.wait(10)


@pytest.mark.unit
def test_storage_unavailable_fails_the_job_with_its_message_and_one_warning(caplog):
    from app.core.offsite_free import OffsiteJobRegistry
    from app.core.storage_override import StorageUnavailableError

    def work():
        raise StorageUnavailableError("The external drive is not connected.")

    registry = OffsiteJobRegistry()
    with caplog.at_level(logging.WARNING, logger="app.core.offsite_free"):
        job = _finish(registry, registry.reserve(1, "restore"), work)

    assert (job.state, job.error) == ("failed", "The external drive is not connected.")
    records = [r for r in caplog.records if r.name == "app.core.offsite_free"]
    assert [r.levelno for r in records] == [logging.WARNING]
    assert records[0].exc_info is None, "no traceback"
    assert "The external drive is not connected." in records[0].getMessage()
