#!/usr/bin/env python3
"""GET /activity/collections and GET /activity/daily: dashboard totals.

The clock is pinned by patching app.api.activity._utcnow, so the window and
the local-midnight cases do not depend on when the suite runs. Timestamps are
written directly as naive UTC, the form they are stored in.

Run with: python -m pytest tests/unit/test_activity.py
"""
from datetime import date, datetime, timedelta

import pytest

from tests.unit.test_record_status_history import _client_as, _collection
from tests.unit.test_record_status_history import _record as _record_with_image

# 2026-10-22 04:30 UTC is still 2026-10-21 in Colombia (UTC-5, offset 300).
NOW = datetime(2026, 10, 22, 4, 30)


@pytest.fixture
def clock(monkeypatch):
    import app.api.activity as activity
    monkeypatch.setattr(activity, "_utcnow", lambda: NOW)
    return NOW


def _record(db_session, **kwargs):
    """A record whose own image is dated well outside any test window, so
    only the images a test adds with _image are counted."""
    rec, img = _record_with_image(db_session, **kwargs)
    img.created_at = datetime(2020, 1, 1)
    db_session.commit()
    return rec, img


def _image(db_session, rec, created_at, name="x.jpg", current=True):
    from app.models.record import RecordImage
    img = RecordImage(record_id=rec.id, filename=name, file_path=f"/{name}",
                      format="jpg", role="single", is_current=current,
                      created_at=created_at)
    db_session.add(img)
    db_session.commit()
    return img


def _change(db_session, rec_id, to_status, changed_at, collection_id=None,
            from_status="in_review"):
    from app.models.record import RecordStatusChange
    row = RecordStatusChange(record_id=rec_id, collection_id=collection_id,
                             from_status=from_status, to_status=to_status,
                             changed_at=changed_at)
    db_session.add(row)
    db_session.commit()
    return row


def _by_date(rows):
    return {r["date"]: r for r in rows}


# ---------------------------------------------------------------------------
# /activity/daily
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_daily_is_zero_filled_oldest_first_ending_today(client, db_session, clock):
    api = _client_as(client, "op", "operator")
    resp = api.get("/activity/daily", params={"days": 7})
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 7
    assert [r["date"] for r in rows] == [
        (date(2026, 10, 22) - timedelta(days=6 - i)).isoformat() for i in range(7)
    ]
    assert all(r["captured_images"] == 0 and r["reviewed_records"] == 0 for r in rows)


@pytest.mark.unit
def test_daily_default_is_182_days(client, db_session, clock):
    rows = _client_as(client, "op", "operator").get("/activity/daily").json()
    assert len(rows) == 182
    assert rows[-1]["date"] == "2026-10-22"


@pytest.mark.unit
def test_daily_today_follows_the_local_offset(client, db_session, clock):
    rows = _client_as(client, "op", "operator").get(
        "/activity/daily", params={"days": 3, "tz_offset_minutes": 300}).json()
    assert [r["date"] for r in rows] == ["2026-10-19", "2026-10-20", "2026-10-21"]


@pytest.mark.unit
def test_daily_local_midnight_with_an_offset(client, db_session, clock):
    """Colombia (offset 300): local midnight starting 21 Oct is 05:00 UTC."""
    rec, _ = _record(db_session, status="in_review")
    midnight_utc = datetime(2026, 10, 21, 5, 0)
    _image(db_session, rec, midnight_utc - timedelta(seconds=1), "before.jpg")
    _image(db_session, rec, midnight_utc + timedelta(seconds=1), "after.jpg")
    other, _ = _record(db_session, status="approved", name="o.jpg")
    _change(db_session, rec.id, "approved", midnight_utc - timedelta(seconds=1))
    _change(db_session, other.id, "approved", midnight_utc + timedelta(seconds=1))

    rows = _by_date(_client_as(client, "op", "operator").get(
        "/activity/daily", params={"days": 3, "tz_offset_minutes": 300}).json())
    assert rows["2026-10-20"]["captured_images"] == 1
    assert rows["2026-10-21"]["captured_images"] == 1
    assert rows["2026-10-20"]["reviewed_records"] == 1
    assert rows["2026-10-21"]["reviewed_records"] == 1


@pytest.mark.unit
def test_daily_negative_offset_moves_the_day_forward(client, db_session, clock):
    """East of UTC (offset -120): 22:30 UTC on 20 Oct is 00:30 on 21 Oct."""
    rec, _ = _record(db_session)
    _image(db_session, rec, datetime(2026, 10, 20, 22, 30), "late.jpg")
    rows = _by_date(_client_as(client, "op", "operator").get(
        "/activity/daily", params={"days": 3, "tz_offset_minutes": -120}).json())
    assert rows["2026-10-21"]["captured_images"] == 1
    assert rows["2026-10-20"]["captured_images"] == 0


@pytest.mark.unit
def test_daily_counts_retakes_as_images(client, db_session, clock):
    rec, _ = _record(db_session)
    t = datetime(2026, 10, 21, 15, 0)
    _image(db_session, rec, t, "first.jpg", current=False)
    _image(db_session, rec, t + timedelta(minutes=5), "retake.jpg", current=True)
    rows = _by_date(_client_as(client, "op", "operator").get(
        "/activity/daily", params={"days": 2}).json())
    assert rows["2026-10-21"]["captured_images"] == 2


@pytest.mark.unit
def test_daily_undo_and_reapprove_counts_the_record_once(client, db_session, clock):
    rec, _ = _record(db_session, status="approved")
    t = datetime(2026, 10, 21, 15, 0)
    _change(db_session, rec.id, "approved", t)
    _change(db_session, rec.id, "in_review", t + timedelta(minutes=1), from_status="approved")
    _change(db_session, rec.id, "approved", t + timedelta(minutes=2))
    rows = _by_date(_client_as(client, "op", "operator").get(
        "/activity/daily", params={"days": 2}).json())
    assert rows["2026-10-21"]["reviewed_records"] == 1


@pytest.mark.unit
def test_daily_counts_rejections_and_ignores_other_transitions(client, db_session, clock):
    a, _ = _record(db_session, name="a.jpg")
    b, _ = _record(db_session, name="b.jpg")
    t = datetime(2026, 10, 21, 15, 0)
    _change(db_session, a.id, "rejected", t)
    _change(db_session, b.id, "in_review", t, from_status="captured")
    rows = _by_date(_client_as(client, "op", "operator").get(
        "/activity/daily", params={"days": 2}).json())
    assert rows["2026-10-21"]["reviewed_records"] == 1


@pytest.mark.unit
def test_daily_counts_each_row_of_a_deleted_record(client, db_session, clock):
    col = _collection(db_session)
    t = datetime(2026, 10, 21, 15, 0)
    _change(db_session, None, "approved", t, collection_id=col.id)
    _change(db_session, None, "rejected", t + timedelta(hours=1), collection_id=col.id)
    rows = _by_date(_client_as(client, "op", "operator").get(
        "/activity/daily", params={"days": 2}).json())
    assert rows["2026-10-21"]["reviewed_records"] == 2


@pytest.mark.unit
def test_daily_leaves_out_what_falls_outside_the_window(client, db_session, clock):
    rec, _ = _record(db_session)
    _image(db_session, rec, datetime(2026, 10, 19, 23, 59), "old.jpg")
    _image(db_session, rec, datetime(2026, 10, 23, 0, 0), "future.jpg")
    rows = _client_as(client, "op", "operator").get(
        "/activity/daily", params={"days": 2}).json()
    assert sum(r["captured_images"] for r in rows) == 0


@pytest.mark.unit
def test_daily_counts_an_image_at_exactly_the_first_local_midnight(client, db_session, clock):
    rec, _ = _record(db_session)
    # NOW is 04:30 UTC on the 22nd, so at offset 300 today is the 21st and a
    # two-day window starts at local midnight on the 20th, 05:00 UTC.
    _image(db_session, rec, datetime(2026, 10, 20, 5, 0), "edge.jpg")
    rows = _client_as(client, "op", "operator").get(
        "/activity/daily", params={"days": 2, "tz_offset_minutes": 300}).json()
    assert rows[0]["date"] == "2026-10-20"
    assert rows[0]["captured_images"] == 1


@pytest.mark.unit
@pytest.mark.parametrize("params", [
    {"days": 0}, {"days": 401}, {"tz_offset_minutes": 841}, {"tz_offset_minutes": -841},
])
def test_daily_rejects_out_of_range_bounds(client, db_session, params):
    resp = _client_as(client, "op", "operator").get("/activity/daily", params=params)
    assert resp.status_code == 422


@pytest.mark.unit
@pytest.mark.parametrize("params", [
    {"days": 1}, {"days": 400}, {"tz_offset_minutes": 840}, {"tz_offset_minutes": -840},
])
def test_daily_accepts_the_bounds(client, db_session, clock, params):
    resp = _client_as(client, "op", "operator").get("/activity/daily", params=params)
    assert resp.status_code == 200
    assert len(resp.json()) == params.get("days", 182)


# --- named time zone (tz) --------------------------------------------------

def _pin(monkeypatch, now):
    import app.api.activity as activity
    monkeypatch.setattr(activity, "_utcnow", lambda: now)


def _captured(api, **params):
    resp = api.get("/activity/daily", params=params)
    assert resp.status_code == 200, resp.text
    return {r["date"]: r["captured_images"] for r in resp.json()}


@pytest.mark.unit
def test_daily_tz_across_the_november_change_in_los_angeles(client, db_session, monkeypatch):
    """DST ends 2026-11-01 02:00 PDT (UTC-7), clocks back to 01:00 PST (UTC-8).
    Today is in PST, so a single offset of 480 would move 00:30 PDT on
    31 Oct back to 30 Oct."""
    _pin(monkeypatch, datetime(2026, 11, 3, 20, 0))
    rec, _ = _record(db_session)
    _image(db_session, rec, datetime(2026, 10, 31, 7, 30), "a.jpg")   # 31 Oct 00:30 PDT
    _image(db_session, rec, datetime(2026, 11, 1, 6, 30), "b.jpg")    # 31 Oct 23:30 PDT
    _image(db_session, rec, datetime(2026, 11, 2, 7, 30), "c.jpg")    # 1 Nov 23:30 PST
    _image(db_session, rec, datetime(2026, 11, 2, 8, 30), "d.jpg")    # 2 Nov 00:30 PST
    other, _ = _record(db_session, name="o.jpg")
    _change(db_session, rec.id, "approved", datetime(2026, 11, 1, 6, 30))    # 31 Oct 23:30 PDT
    _change(db_session, other.id, "approved", datetime(2026, 11, 2, 7, 30))  # 1 Nov 23:30 PST

    api = _client_as(client, "op", "operator")
    rows = _by_date(api.get("/activity/daily", params={
        "days": 6, "tz": "America/Los_Angeles"}).json())
    assert rows["2026-10-30"]["captured_images"] == 0
    assert rows["2026-10-31"]["captured_images"] == 2
    assert rows["2026-11-01"]["captured_images"] == 1
    assert rows["2026-11-02"]["captured_images"] == 1
    assert rows["2026-10-31"]["reviewed_records"] == 1
    assert rows["2026-11-01"]["reviewed_records"] == 1
    assert max(rows) == "2026-11-03"


@pytest.mark.unit
def test_daily_tz_across_the_march_change_in_los_angeles(client, db_session, monkeypatch):
    """DST starts 2026-03-08 02:00 PST (UTC-8), clocks forward to 03:00 PDT
    (UTC-7). Today is in PDT, so a single offset of 420 would move 23:30 PST
    on 7 Mar forward to 8 Mar."""
    _pin(monkeypatch, datetime(2026, 3, 10, 20, 0))
    rec, _ = _record(db_session)
    _image(db_session, rec, datetime(2026, 3, 8, 7, 30), "a.jpg")   # 7 Mar 23:30 PST
    _image(db_session, rec, datetime(2026, 3, 9, 6, 30), "b.jpg")   # 8 Mar 23:30 PDT
    _image(db_session, rec, datetime(2026, 3, 9, 7, 30), "c.jpg")   # 9 Mar 00:30 PDT

    api = _client_as(client, "op", "operator")
    by_zone = _captured(api, days=5, tz="America/Los_Angeles")
    assert by_zone["2026-03-07"] == 1
    assert by_zone["2026-03-08"] == 1
    assert by_zone["2026-03-09"] == 1
    # The fixed-offset fallback counts with one offset for the whole window.
    by_offset = _captured(api, days=5, tz_offset_minutes=420)
    assert by_offset["2026-03-07"] == 0
    assert by_offset["2026-03-08"] == 2


@pytest.mark.unit
def test_daily_tz_window_starts_at_the_first_local_midnight(client, db_session, monkeypatch):
    """The window's first instant is local midnight in the zone, here PST on
    7 Mar (08:00 UTC), though today is in PDT."""
    _pin(monkeypatch, datetime(2026, 3, 10, 20, 0))
    rec, _ = _record(db_session)
    _image(db_session, rec, datetime(2026, 3, 7, 7, 59, 59), "before.jpg")
    _image(db_session, rec, datetime(2026, 3, 7, 8, 0), "edge.jpg")
    rows = _client_as(client, "op", "operator").get(
        "/activity/daily", params={"days": 4, "tz": "America/Los_Angeles"}).json()
    assert rows[0]["date"] == "2026-03-07"
    assert sum(r["captured_images"] for r in rows) == 1
    assert rows[0]["captured_images"] == 1


@pytest.mark.unit
def test_daily_tz_bogota(client, db_session, clock):
    """Colombia keeps UTC-5 all year. NOW (04:30 UTC on 22 Oct) is 23:30 on
    21 Oct there, so today is the 21st."""
    rec, _ = _record(db_session)
    _image(db_session, rec, datetime(2026, 10, 21, 4, 30), "a.jpg")   # 20 Oct 23:30
    _image(db_session, rec, datetime(2026, 10, 21, 5, 30), "b.jpg")   # 21 Oct 00:30
    rows = _client_as(client, "op", "operator").get(
        "/activity/daily", params={"days": 3, "tz": "America/Bogota"}).json()
    assert [r["date"] for r in rows] == ["2026-10-19", "2026-10-20", "2026-10-21"]
    by_date = _by_date(rows)
    assert by_date["2026-10-20"]["captured_images"] == 1
    assert by_date["2026-10-21"]["captured_images"] == 1


@pytest.mark.unit
def test_daily_tz_takes_precedence_over_the_offset(client, db_session, clock):
    rows = _client_as(client, "op", "operator").get("/activity/daily", params={
        "days": 1, "tz": "America/Bogota", "tz_offset_minutes": -840}).json()
    assert [r["date"] for r in rows] == ["2026-10-21"]


@pytest.mark.unit
@pytest.mark.parametrize("tz", ["Mars/Olympus_Mons", "../../etc/passwd", "/etc/localtime", "", "x" * 65])
def test_daily_rejects_an_unknown_zone(client, db_session, clock, tz):
    resp = _client_as(client, "op", "operator").get(
        "/activity/daily", params={"days": 3, "tz": tz})
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# /activity/collections
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_collections_counts_statuses(client, db_session):
    col = _collection(db_session)
    for i, status in enumerate(["captured", "in_review", "in_review", "approved"]):
        _record(db_session, status=status, col=col, name=f"{i}.jpg")
    rows = _client_as(client, "rev", "reviewer").get("/activity/collections").json()
    assert rows == [{
        "collection_id": col.id,
        "status_counts": {"captured": 1, "in_review": 2, "rejected": 0, "approved": 1},
        "record_count": 4,
        "last_status_change_at": None,
    }]


@pytest.mark.unit
def test_collections_empty_collection_has_zeros_and_null(client, db_session):
    col = _collection(db_session)
    rows = _client_as(client, "rev", "reviewer").get("/activity/collections").json()
    assert rows == [{
        "collection_id": col.id,
        "status_counts": {"captured": 0, "in_review": 0, "rejected": 0, "approved": 0},
        "record_count": 0,
        "last_status_change_at": None,
    }]


@pytest.mark.unit
def test_collections_last_change_includes_deleted_records(client, db_session):
    """A deleted record's rows count under the collection stored on them."""
    col = _collection(db_session)
    other = _collection(db_session, name="Tomo 2")
    live, _ = _record(db_session, status="approved", col=col, name="live.jpg")
    _change(db_session, live.id, "approved", datetime(2026, 10, 20, 12, 0), collection_id=col.id)
    _change(db_session, None, "rejected", datetime(2026, 10, 21, 9, 30), collection_id=col.id)

    rows = {r["collection_id"]: r for r in
            _client_as(client, "rev", "reviewer").get("/activity/collections").json()}
    assert rows[col.id]["last_status_change_at"] == "2026-10-21T09:30:00"
    assert rows[col.id]["record_count"] == 1
    assert rows[other.id]["last_status_change_at"] is None


@pytest.mark.unit
def test_collections_deleted_through_the_endpoint(client, db_session):
    col = _collection(db_session)
    rec, _ = _record(db_session, status="in_review", col=col, name="gone.jpg")
    api = _client_as(client, "adm", "admin")
    assert api.patch(f"/records/{rec.id}/status", json={"status": "approved"}).status_code == 200
    db_session.expire_all()
    assert api.delete(f"/records/{rec.id}").status_code in (200, 204)

    rows = api.get("/activity/collections").json()
    assert rows[0]["collection_id"] == col.id
    assert rows[0]["record_count"] == 0
    assert rows[0]["last_status_change_at"] is not None


@pytest.mark.unit
def test_collections_moved_record_follows_its_current_collection(client, db_session):
    """A live record's history follows it, whatever collection_id the row stored."""
    old = _collection(db_session, name="Tomo 1")
    new = _collection(db_session, name="Tomo 2")
    rec, _ = _record(db_session, status="approved", col=old)
    _change(db_session, rec.id, "approved", datetime(2026, 10, 21, 9, 0), collection_id=old.id)
    rec.collection_id = new.id
    db_session.commit()

    rows = {r["collection_id"]: r for r in
            _client_as(client, "rev", "reviewer").get("/activity/collections").json()}
    assert rows[old.id]["record_count"] == 0
    assert rows[old.id]["last_status_change_at"] is None
    assert rows[new.id]["status_counts"]["approved"] == 1
    assert rows[new.id]["last_status_change_at"] == "2026-10-21T09:00:00"


@pytest.mark.unit
def test_collections_project_filter_includes_nested_collections(client, db_session):
    from app.models.collection import Collection
    from app.models.project import Project
    mine = _collection(db_session)  # under project "P"
    child = Collection(name="Legajo", parent_collection_id=mine.id)
    other_proj = Project(name="Q")
    db_session.add_all([child, other_proj])
    db_session.commit()
    theirs = Collection(name="Ajeno", project_id=other_proj.id)
    db_session.add(theirs)
    db_session.commit()

    api = _client_as(client, "rev", "reviewer")
    ids = [r["collection_id"] for r in
           api.get("/activity/collections", params={"project_id": mine.project_id}).json()]
    assert ids == [mine.id, child.id]
    assert [r["collection_id"] for r in
            api.get("/activity/collections", params={"project_id": other_proj.id}).json()] == [theirs.id]
    assert len(api.get("/activity/collections").json()) == 3
    assert api.get("/activity/collections", params={"project_id": 9999}).json() == []


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("role", ["admin", "operator", "reviewer"])
@pytest.mark.parametrize("path", ["/activity/collections", "/activity/daily"])
def test_every_role_can_read(client, db_session, role, path):
    assert _client_as(client, "u", role).get(path).status_code == 200


@pytest.mark.unit
@pytest.mark.parametrize("path", ["/activity/collections", "/activity/daily"])
def test_an_unknown_role_is_refused(client, db_session, path):
    assert _client_as(client, "u", "guest").get(path).status_code == 403


@pytest.mark.unit
@pytest.mark.parametrize("path", ["/activity/collections", "/activity/daily"])
def test_no_credentials_is_refused(client, db_session, path):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides.pop(get_current_user, None)
    assert client.get(path).status_code == 401
