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

    third = registry.reserve(3, "restore")

    assert registry.get(old.id) is None
    assert registry.get(recent.id) is recent
    # A reserved job is counted as offsite work until it runs or is discarded.
    registry.discard(third)


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
    third = None
    try:
        third = registry.reserve(3, "free")
        assert registry.get(queued.id) is queued
        assert registry.get(running.id) is running
    finally:
        gate.set()
        assert hold.released.wait(10)
        registry.discard(queued)
        if third is not None:
            registry.discard(third)


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


@pytest.mark.unit
def test_every_reservation_is_released_by_run_or_discard():
    """No exit leaves an offsite job counted: a finished run, a failed run, a discarded reservation."""
    from app.core import unit_activity
    from app.core.offsite_free import OffsiteJobRegistry

    registry = OffsiteJobRegistry()
    assert not unit_activity.in_progress(unit_activity.OFFSITE_JOB)
    _finish(registry, registry.reserve(1, "free"))
    _finish(registry, registry.reserve(2, "free"), lambda: 1 / 0)
    registry.discard(registry.reserve(3, "restore"))
    job = registry.reserve(4, "restore")
    registry.discard(job)
    registry.discard(job)  # twice: counted once
    assert unit_activity.wait_idle(unit_activity.OFFSITE_JOB, 2)


@pytest.mark.unit
def test_start_restore_releases_the_reservation_when_preparing_fails(monkeypatch):
    from app.core import offsite_free, unit_activity

    def refuse(db, collection_id):
        raise offsite_free.FreeRefused("no read remote")

    monkeypatch.setattr(offsite_free, "_prepare_restore", refuse)
    with pytest.raises(offsite_free.FreeRefused):
        offsite_free.start_restore(None, 99, "admin1")
    assert not unit_activity.in_progress(unit_activity.OFFSITE_JOB)
