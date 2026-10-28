"""GET /records/to-retake and GET /records/to-retake/count: the records waiting to be retaken.

Every record whose status is `rejected`, across all volumes, with its volume
and its latest RecordRejection, oldest latest rejection first. Rejections are
written directly with their rejected_at, so the order does not depend on the
clock.

Run with: python -m pytest tests/unit/test_records_to_retake.py

Version: 0.2.0
"""

from datetime import datetime

import pytest
from sqlalchemy import event

from tests.unit.test_record_status_history import _client_as, _collection, _record


def _rejection(db_session, rec, at, reason="blur", comment=None, by="rev"):
    from app.models.record import RecordRejection
    row = RecordRejection(record_id=rec.id, predefined_reason=reason, comment=comment,
                          rejected_by=by, rejected_at=at)
    db_session.add(row)
    db_session.commit()
    return row


def _rejected(db_session, col, name, *dates, **kw):
    rec, _ = _record(db_session, status="rejected", col=col, name=name)
    for at in dates:
        _rejection(db_session, rec, at, **kw)
    return rec


def _retakes(api):
    resp = api.get("/records/to-retake")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("path", ["/records/to-retake", "/records/to-retake/count"])
def test_to_retake_needs_authentication(client, db_session, path):
    assert client.get(path).status_code == 401


@pytest.mark.unit
@pytest.mark.parametrize("role", ["admin", "operator", "reviewer"])
def test_every_role_can_read_to_retake(client, db_session, role):
    api = _client_as(client, "u", role)
    assert api.get("/records/to-retake").status_code == 200
    assert api.get("/records/to-retake/count").status_code == 200


# ---------------------------------------------------------------------------
# What is listed
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_nothing_waiting(client, db_session):
    api = _client_as(client, "op", "operator")
    _record(db_session, status="captured", col=_collection(db_session))
    assert _retakes(api) == []
    assert api.get("/records/to-retake/count").json() == {"count": 0}


@pytest.mark.unit
def test_only_rejected_records_across_volumes(client, db_session):
    api = _client_as(client, "op", "operator")
    t1 = _collection(db_session, "Tomo 1")
    t2 = _collection(db_session, "Tomo 2")
    a = _rejected(db_session, t1, "a.jpg", datetime(2026, 10, 1))
    b = _rejected(db_session, t2, "b.jpg", datetime(2026, 10, 2))
    for status in ("captured", "in_review", "approved"):
        _record(db_session, status=status, col=t1, name=f"{status}.jpg")
    # A captured record with a rejection behind it is not waiting.
    retaken, _ = _record(db_session, status="captured", col=t1, name="retaken.jpg")
    _rejection(db_session, retaken, datetime(2026, 9, 1))

    assert [r["recordId"] for r in _retakes(api)] == [a.id, b.id]
    assert api.get("/records/to-retake/count").json() == {"count": 2}


@pytest.mark.unit
def test_a_row_carries_the_record_its_volume_and_the_latest_rejection(client, db_session):
    from app.models.collection import Collection
    from app.models.project import Project

    api = _client_as(client, "op", "operator")
    p = Project(name="P")
    db_session.add(p)
    db_session.commit()
    col = Collection(name="Libro de bautismos", project_id=p.id, filename_stem="co-ahr-001")
    db_session.add(col)
    db_session.commit()
    rec, _ = _record(db_session, status="rejected", col=col, name="folio 117r")
    rec.sequence = 12
    db_session.commit()
    _rejection(db_session, rec, datetime(2026, 10, 1, 9), reason="glare", comment="brillo", by="ana")
    _rejection(db_session, rec, datetime(2026, 10, 3, 15, 30), reason="other", comment="falta el margen", by="luis")
    _rejection(db_session, rec, datetime(2026, 10, 2, 11), reason="focus", by="ana")

    (row,) = _retakes(api)
    assert row == {
        "recordId": rec.id,
        "title": "folio 117r",
        "sequence": 12,
        "collectionId": col.id,
        "projectId": p.id,
        "name": "Libro de bautismos",
        "filenameStem": "co-ahr-001",
        "reason": "other",
        "comment": "falta el margen",
        "rejectedBy": "luis",
        "rejectedAt": "2026-10-03T15:30:00",
    }


@pytest.mark.unit
def test_oldest_latest_rejection_comes_first(client, db_session):
    api = _client_as(client, "op", "operator")
    col = _collection(db_session)
    # a's first rejection is the oldest of all, but its latest is the newest.
    a = _rejected(db_session, col, "a.jpg", datetime(2026, 10, 1), datetime(2026, 10, 9))
    b = _rejected(db_session, col, "b.jpg", datetime(2026, 10, 5))
    c = _rejected(db_session, col, "c.jpg", datetime(2026, 10, 3), datetime(2026, 10, 7))

    rows = _retakes(api)
    assert [r["recordId"] for r in rows] == [b.id, c.id, a.id]
    assert [r["rejectedAt"] for r in rows] == ["2026-10-05T00:00:00", "2026-10-07T00:00:00", "2026-10-09T00:00:00"]


@pytest.mark.unit
def test_a_rejected_record_without_a_rejection_row_comes_last(client, db_session):
    """Every path to rejected writes a RecordRejection; a row from before that still shows."""
    api = _client_as(client, "op", "operator")
    col = _collection(db_session)
    bare, _ = _record(db_session, status="rejected", col=col, name="bare.jpg")
    later = _rejected(db_session, col, "later.jpg", datetime(2026, 10, 9))

    rows = _retakes(api)
    assert [r["recordId"] for r in rows] == [later.id, bare.id]
    assert {k: rows[1][k] for k in ("reason", "comment", "rejectedBy", "rejectedAt")} == {
        "reason": None, "comment": None, "rejectedBy": None, "rejectedAt": None,
    }


@pytest.mark.unit
def test_a_rejected_record_outside_any_volume_is_listed(client, db_session):
    api = _client_as(client, "op", "operator")
    loose = _rejected(db_session, None, "loose.jpg", datetime(2026, 10, 1))
    (row,) = _retakes(api)
    assert row["recordId"] == loose.id
    assert (row["collectionId"], row["projectId"], row["name"], row["filenameStem"]) == (None, None, None, None)


# ---------------------------------------------------------------------------
# One query, whatever the number of rows
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_list_is_read_in_a_constant_number_of_queries(client, db_session):
    api = _client_as(client, "op", "operator")
    engine = db_session.get_bind()
    statements = []

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    def queries_for_list():
        statements.clear()
        event.listen(engine, "before_cursor_execute", count)
        try:
            _retakes(api)
        finally:
            event.remove(engine, "before_cursor_execute", count)
        return len(statements)

    col = _collection(db_session)
    _rejected(db_session, col, "r1.jpg", datetime(2026, 10, 1))
    one = queries_for_list()
    for n in range(2, 8):
        _rejected(db_session, _collection(db_session, f"Tomo {n}"), f"r{n}.jpg",
                  datetime(2026, 10, n), datetime(2026, 10, n, 12))
    assert len(_retakes(api)) == 7
    assert queries_for_list() == one == 1


@pytest.mark.unit
def test_rejections_at_the_same_time_are_ordered_by_id_in_both_endpoints(client, db_session):
    """GET /records/{id}/rejections and /to-retake agree on which rejection is the latest."""
    api = _client_as(client, "op", "operator")
    col = _collection(db_session)
    rec, _ = _record(db_session, status="rejected", col=col, name="tie.jpg")
    at = datetime(2026, 10, 4, 10)
    first = _rejection(db_session, rec, at, reason="blur")
    second = _rejection(db_session, rec, at, reason="glare")

    history = api.get(f"/records/{rec.id}/rejections").json()
    assert [r["id"] for r in history] == [second.id, first.id]
    (row,) = _retakes(api)
    assert row["reason"] == history[0]["predefined_reason"] == "glare"
