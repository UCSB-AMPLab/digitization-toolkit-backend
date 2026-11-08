"""A handover made after a version refusal takes the volume's row first, and re-checks the volume under it.

The retest of a version stop (_retest_version_stop) and the resend once
the stop clears (_resend_version_refusals) each hand a volume over again
(enqueue_handover). They take the volume FOR UPDATE first
(app.core.volume_stem.locked_volume_query), as every other handover does,
so the lock order is the volume's row, then its outbox rows. Under that
lock each re-checks that the volume still qualifies: a volume that gained
a newer outbox row or a captured record after it was chosen is skipped.

CI runs on SQLite, which has no row locks: these tests show the order in
which the locks are asked for, not that one transaction waits for another.

    python -m pytest tests/unit/test_version_stop_volume_lock.py
"""

import pytest

from tests.unit.test_capture_manifest_export import TENANT, _as, tenant, vol  # noqa: F401
from tests.unit.test_fisqua_http import (  # noqa: F401
    T0, Server, _interval, _ok, _send, _stopped, _version_refusal, queued)
from tests.unit.test_fisqua_outbox import _rows, fisqua  # noqa: F401


def _spy(monkeypatch):
    """[("lock", volume id) for each volume FOR UPDATE, ("enqueue", volume id) for each enqueue_handover], in order."""
    from sqlalchemy.orm import Query

    from app.core import fisqua_outbox
    events = []
    real_lock = Query.with_for_update
    real_enqueue = fisqua_outbox.enqueue_handover

    def lock(self, *a, **kw):
        entity = self.column_descriptions[0].get("entity")
        if entity is not None and entity.__name__ == "Collection" and not kw:
            events.append(("lock", self.whereclause.right.value))
        return real_lock(self, *a, **kw)

    def enqueue(db, collection):
        events.append(("enqueue", collection.id))
        return real_enqueue(db, collection)

    monkeypatch.setattr(Query, "with_for_update", lock)
    monkeypatch.setattr(fisqua_outbox, "enqueue_handover", enqueue)
    return events


def _locked_before_enqueue(events, volume_id):
    assert ("enqueue", volume_id) in events, events
    first = events.index(("enqueue", volume_id))
    return ("lock", volume_id) in events[:first]


def _cleared(monkeypatch):
    """A software update, which clears the version stop."""
    from app.core import config
    from app.core.fisqua_outbox import config_error
    monkeypatch.setattr(config.settings, "app_version", "0.1.1")
    assert config_error() is None


# ---------------------------------------------------------------------------
# Lock order: the volume's row before enqueue_handover
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_retest_takes_the_volume_for_update_before_handing_it_over(db_session, queued, monkeypatch):
    _stopped(db_session, queued)
    events = _spy(monkeypatch)
    server = Server(_ok())
    _send(db_session, server, now=T0 + _interval())
    assert len(server.requests) == 1
    assert _locked_before_enqueue(events, queued.col.id), events


@pytest.mark.unit
def test_the_resend_takes_the_volume_for_update_before_handing_it_over(db_session, queued, monkeypatch):
    _stopped(db_session, queued)
    _cleared(monkeypatch)
    events = _spy(monkeypatch)
    server = Server(_ok())
    _send(db_session, server)
    assert [r.state for r in _rows(db_session, queued.col)] == ["refused", "delivered"]
    assert _locked_before_enqueue(events, queued.col.id), events


# ---------------------------------------------------------------------------
# The re-check under the lock: a volume changed after it was chosen is skipped
# ---------------------------------------------------------------------------

def _newer_handover(db, collection):
    """Another handover of the volume, committed: its newest row is then a waiting one, not the version refusal."""
    from app.core.fisqua_outbox import enqueue_handover
    enqueue_handover(db, collection)


def _captured_record(db, collection):
    """A record captured into the volume, committed: it is being captured again."""
    from app.models.record import Record
    db.add(Record(title="p", collection_id=collection.id, status="captured", capture_mode="dual", sequence=99))


def _change_after_choosing(monkeypatch, change):
    """Commit `change` to each chosen volume just after _version_refused_volumes chose it."""
    from app.core import fisqua_outbox
    real = fisqua_outbox._version_refused_volumes

    def chosen_then_changed(db):
        found = real(db)
        for _row, collection in found:
            change(db, collection)
        db.commit()
        return found

    monkeypatch.setattr(fisqua_outbox, "_version_refused_volumes", chosen_then_changed)


@pytest.mark.unit
@pytest.mark.parametrize("change", [_newer_handover, _captured_record], ids=["newer_row", "captured_record"])
def test_the_retest_skips_a_volume_changed_after_it_was_chosen(db_session, queued, monkeypatch, change):
    from app.core.fisqua_outbox import config_error
    _stopped(db_session, queued)
    before = len(_rows(db_session, queued.col))
    _change_after_choosing(monkeypatch, change)
    server = Server(_ok())
    _send(db_session, server, now=T0 + _interval())
    assert server.requests == []
    # Only what the change itself added: no handover by the retest.
    expected = before + (1 if change is _newer_handover else 0)
    assert len(_rows(db_session, queued.col)) == expected
    assert config_error()["reason"] == "version"


@pytest.mark.unit
@pytest.mark.parametrize("change", [_newer_handover, _captured_record], ids=["newer_row", "captured_record"])
def test_the_resend_skips_a_volume_changed_after_it_was_chosen(db_session, queued, monkeypatch, change):
    from app.core.fisqua_outbox import _resend_version_refusals
    _stopped(db_session, queued)
    _cleared(monkeypatch)
    before = len(_rows(db_session, queued.col))
    _change_after_choosing(monkeypatch, change)
    assert _resend_version_refusals(db_session) == 0
    expected = before + (1 if change is _newer_handover else 0)
    assert len(_rows(db_session, queued.col)) == expected
