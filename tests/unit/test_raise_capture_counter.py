"""`python -m app.core.image_upload raise-capture-counter <stem> <n> [--apply]` (app.core.capture_counter).

Images copied into the bucket under contract names (a rename map's
`counterAfter` is the highest number it issued) exist only in the bucket, so
nothing on the card tells capture those numbers are taken. The command
raises the volume's capture_counter to n so that the next capture takes
n + 1. A dry run unless `--apply`; it never lowers the counter, an equal
value changes nothing, and n stays within MAX_CAPTURE_NUMBER.

    python -m pytest tests/unit/test_raise_capture_counter.py
"""

import json

import pytest


STEM = "co-test-raise"


@pytest.fixture
def volume(db_session, test_project):
    from app.models.collection import Collection
    v = Collection(name="Tomo 6", project_id=test_project.id, filename_stem=STEM)
    db_session.add(v)
    db_session.commit()
    return v


def _counter(db_session, volume):
    from app.models.collection import Collection
    db_session.expire_all()
    return db_session.get(Collection, volume.id).capture_counter


def _set_counter(db_session, volume, n):
    from sqlalchemy import text
    db_session.execute(text("UPDATE collections SET capture_counter = :n WHERE id = :i"),
                       {"n": n, "i": volume.id})
    db_session.commit()


def _raise(db_session, stem=STEM, n=10, apply=False):
    from app.core.capture_counter import raise_capture_counter
    return raise_capture_counter(db_session, stem, n, apply=apply)


@pytest.mark.unit
def test_a_dry_run_reports_and_writes_nothing(db_session, volume):
    _set_counter(db_session, volume, 3)
    report = _raise(db_session, n=120)
    assert report == {"stem": STEM, "collection_id": volume.id, "counter_before": 3,
                      "counter_after": 120, "raised": True, "applied": False}
    assert _counter(db_session, volume) == 3


@pytest.mark.unit
def test_apply_raises_the_counter(db_session, volume):
    report = _raise(db_session, n=120, apply=True)
    assert report["counter_before"] == 0 and report["counter_after"] == 120
    assert report["raised"] is True and report["applied"] is True
    assert _counter(db_session, volume) == 120


@pytest.mark.unit
def test_a_lower_value_is_refused_and_nothing_changes(db_session, volume):
    _set_counter(db_session, volume, 50)
    with pytest.raises(ValueError, match="already 50"):
        _raise(db_session, n=49, apply=True)
    assert _counter(db_session, volume) == 50


@pytest.mark.unit
def test_an_equal_value_changes_nothing(db_session, volume):
    from app.models.system_log import SystemLog
    _set_counter(db_session, volume, 50)
    report = _raise(db_session, n=50, apply=True)
    assert report["raised"] is False and report["counter_before"] == report["counter_after"] == 50
    assert _counter(db_session, volume) == 50
    assert db_session.query(SystemLog).filter(SystemLog.action == "capture_counter_raised").count() == 0


@pytest.mark.unit
@pytest.mark.parametrize("n", [10000, -1])
def test_a_value_outside_the_cap_is_refused(db_session, volume, n):
    with pytest.raises(ValueError):
        _raise(db_session, n=n, apply=True)
    assert _counter(db_session, volume) == 0


@pytest.mark.unit
def test_the_cap_itself_is_allowed(db_session, volume):
    from app.core.capture_naming import MAX_CAPTURE_NUMBER
    _raise(db_session, n=MAX_CAPTURE_NUMBER, apply=True)
    assert _counter(db_session, volume) == MAX_CAPTURE_NUMBER


@pytest.mark.unit
@pytest.mark.parametrize("stem", ["no-such-stem", "", "  "])
def test_an_unknown_stem_is_refused(db_session, volume, stem):
    with pytest.raises(ValueError, match="stem"):
        _raise(db_session, stem=stem, n=5, apply=True)


@pytest.mark.unit
def test_apply_is_logged(db_session, volume):
    from app.models.system_log import SystemLog
    _set_counter(db_session, volume, 7)
    _raise(db_session, n=120, apply=True)
    row = db_session.query(SystemLog).filter(SystemLog.action == "capture_counter_raised").one()
    assert row.subject == STEM and row.category == "system"
    assert "from=7" in row.detail and "to=120" in row.detail


@pytest.mark.unit
def test_a_dry_run_is_not_logged(db_session, volume):
    from app.models.system_log import SystemLog
    _raise(db_session, n=120)
    assert db_session.query(SystemLog).filter(SystemLog.action == "capture_counter_raised").count() == 0


@pytest.mark.unit
def test_the_next_capture_number_follows_the_raised_counter(db_session, volume):
    from app.core.capture_counter import next_capture_number
    _raise(db_session, n=120, apply=True)
    assert next_capture_number(db_session, volume.id) == 121


@pytest.mark.unit
def test_the_raise_takes_the_volume_row_lock_before_it_reads_the_counter(db_session, volume, monkeypatch):
    """The suite runs on SQLite, which emits no FOR UPDATE, so the lock is shown by its call.

    A capture takes the same row through locked_volume_query (app/api/cameras.py)
    before take_filename_index raises the counter, so on Postgres the raise and
    a capture into the volume wait on each other and neither reads a counter
    the other is about to change. The raise locks no record, so it cannot take
    a record lock after the volume lock.
    """
    import app.core.capture_counter as cc
    from app.core.volume_stem import locked_volume_query

    calls = []

    def spy(db, collection_id):
        calls.append(collection_id)
        return locked_volume_query(db, collection_id)

    monkeypatch.setattr(cc, "locked_volume_query", spy)
    _raise(db_session, n=120, apply=True)
    assert calls == [volume.id]


@pytest.mark.unit
def test_the_capture_path_locks_the_same_row_through_the_same_query():
    import inspect
    import app.api.cameras as cameras
    import app.core.capture_counter as cc

    assert "locked_volume_query" in inspect.getsource(cameras)
    assert "locked_volume_query(" in inspect.getsource(cc.raise_capture_counter)
    assert "Record" not in inspect.getsource(cc.raise_capture_counter), "the raise locks no record"


@pytest.mark.unit
def test_the_command_line(db_session, volume, capsys):
    from app.core.image_upload import main

    assert main(["raise-capture-counter", STEM, "120"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is False and out["counter_after"] == 120
    assert _counter(db_session, volume) == 0

    assert main(["raise-capture-counter", STEM, "120", "--apply"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is True and out["raised"] is True
    assert _counter(db_session, volume) == 120

    assert main(["raise-capture-counter", STEM, "120", "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["raised"] is False

    assert main(["raise-capture-counter", STEM, "119", "--apply"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "already 120" in err
    assert _counter(db_session, volume) == 120

    assert main(["raise-capture-counter", STEM, "10000", "--apply"]) == 2
    assert main(["raise-capture-counter", "no-such-stem", "5"]) == 2
