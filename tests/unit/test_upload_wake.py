"""A handover wakes the offsite upload, and the Fisqua send pass after it, instead of waiting out the interval.

The upload scheduler waits on an Event that wake() sets (and stop() too);
a wake while a run is going queues one more run, however many wakes come.
The tick runs the upload and then the Fisqua send pass, so a volume whose
last object the woken run verifies is promoted and sent in that tick.

    python -m pytest tests/unit/test_upload_wake.py
"""

import threading
import time

import pytest

from tests.unit.test_capture_manifest_export import TENANT, Volume, _as, tenant, vol  # noqa: F401
from tests.unit.test_fisqua_outbox import _handover, _rows, fisqua  # noqa: F401

LONG = 3600


@pytest.fixture
def scheduler(monkeypatch):
    """An upload scheduler whose delay and interval are an hour, with the run steps recorded."""
    from app.core import config, fisqua_outbox, image_upload
    monkeypatch.setattr(config.settings, "OFFSITE_BACKUP_DELAY_SECONDS", LONG)
    monkeypatch.setattr(config.settings, "OFFSITE_BACKUP_INTERVAL_SECONDS", LONG)
    monkeypatch.setattr(image_upload, "is_enabled", lambda: True)
    monkeypatch.setattr(image_upload, "sweep_staging", lambda: None)
    calls = []
    ran = threading.Event()
    gate = {"block": None}

    def upload(db, stop=None):
        calls.append("upload")
        if gate["block"] is not None:
            gate["block"].wait(5)
            gate["block"] = None
        return {"status": "done"}

    def outbox(db, stop=None):
        calls.append("fisqua")
        ran.set()
        return {"status": "done"}

    monkeypatch.setattr(image_upload, "run_image_upload", upload)
    monkeypatch.setattr(fisqua_outbox, "run_fisqua_outbox", outbox)
    s = image_upload.ImageUploadScheduler()
    s.calls, s.ran, s.gate = calls, ran, gate
    yield s
    s.stop()


def _settle(seconds=0.4):
    time.sleep(seconds)


@pytest.mark.unit
def test_a_wake_runs_the_upload_and_then_the_fisqua_pass_within_a_second(db_session, scheduler):
    assert scheduler.start()
    _settle(0.1)
    assert scheduler.calls == []  # an hour's delay
    scheduler.wake()
    assert scheduler.ran.wait(1.0)
    assert scheduler.calls == ["upload", "fisqua"]


@pytest.mark.unit
def test_wakes_during_a_run_queue_exactly_one_more(db_session, scheduler):
    release = threading.Event()
    scheduler.gate["block"] = release
    scheduler.start()
    scheduler.wake()
    deadline = time.monotonic() + 1
    while scheduler.calls != ["upload"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert scheduler.calls == ["upload"]  # the first run, held inside the upload
    for _ in range(3):
        scheduler.wake()
    release.set()
    _settle()
    assert scheduler.calls == ["upload", "fisqua", "upload", "fisqua"]


@pytest.mark.unit
def test_stop_ends_the_wait_without_a_run(db_session, scheduler):
    scheduler.start()
    scheduler.stop()
    assert not scheduler.is_running()
    assert scheduler.calls == []


@pytest.mark.unit
def test_a_wake_with_no_schedule_running_does_nothing(db_session, monkeypatch):
    from app.core import image_upload
    monkeypatch.setattr(image_upload, "is_enabled", lambda: False)
    monkeypatch.setattr(image_upload, "sweep_staging", lambda: None)
    s = image_upload.ImageUploadScheduler()
    assert s.start() is False
    s.wake()
    assert not s.is_running()


@pytest.mark.unit
def test_a_woken_run_under_the_hold_is_the_held_run(db_session, monkeypatch, tmp_path):
    """The hold still wins: the woken run reads it and uploads nothing, as a scheduled one does."""
    from app.core import config, fisqua_outbox, image_upload
    monkeypatch.setattr(config.settings, "OFFSITE_BACKUP_DELAY_SECONDS", LONG)
    monkeypatch.setattr(config.settings, "OFFSITE_BACKUP_INTERVAL_SECONDS", LONG)
    monkeypatch.setattr(config.settings, "OFFSITE_REMOTE", "/nonexistent")
    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", TENANT)
    hold = tmp_path / "offsite-hold"
    hold.touch()
    monkeypatch.setattr(config.settings, "OFFSITE_HOLD_FILE", str(hold))
    monkeypatch.setattr(image_upload, "sweep_staging", lambda: None)
    done = threading.Event()
    monkeypatch.setattr(fisqua_outbox, "run_fisqua_outbox", lambda db, stop=None: done.set() or {"status": "disabled"})
    s = image_upload.ImageUploadScheduler()
    try:
        assert s.start()
        s.wake()
        assert done.wait(1.0)
        assert s.last_result["status"] == "held"
    finally:
        s.stop()


# ---------------------------------------------------------------------------
# The handover wakes it
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_handover_wakes_the_upload_after_its_commit(client, db_session, vol, fisqua, monkeypatch):
    from app.core import image_upload
    from app.core.db import SessionLocal
    from app.models.fisqua import FisquaOutbox
    seen = []

    def wake():
        other = SessionLocal()
        try:  # the queued manifest is committed by the time the worker is woken
            seen.append(other.query(FisquaOutbox).filter(FisquaOutbox.collection_id == vol.col.id).count())
        finally:
            other.close()

    monkeypatch.setattr(image_upload.image_upload_scheduler, "wake", wake)
    vol.record(status="captured")
    _handover(client, vol.col)
    assert seen == [1]


@pytest.mark.unit
def test_a_handover_that_promotes_nothing_wakes_nothing(client, db_session, vol, fisqua, monkeypatch):
    from app.core import image_upload
    seen = []
    monkeypatch.setattr(image_upload.image_upload_scheduler, "wake", lambda: seen.append(1))
    vol.record(status="in_review")
    _handover(client, vol.col)
    assert seen == []


@pytest.mark.unit
def test_a_handover_wakes_the_upload_without_fisqua_too(client, db_session, vol, monkeypatch):
    """The upload is the unit's offsite copy whether or not Fisqua is configured."""
    from app.core import image_upload
    seen = []
    monkeypatch.setattr(image_upload.image_upload_scheduler, "wake", lambda: seen.append(1))
    vol.record(status="captured")
    _handover(client, vol.col)
    assert seen == [1]
