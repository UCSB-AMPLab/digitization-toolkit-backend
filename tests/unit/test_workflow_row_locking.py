#!/usr/bin/env python3
"""NEH-260: the record row is the mutex for its own workflow state.

Validating a snapshot and then writing is not a guard. An adversarial round
reproduced three sequences against the unlocked version:

  - a promotion that loaded a clean `captured` record, a concurrent
    promote-reject-prepare landing a rejected image back on it, and the first
    promotion then committing `in_review` with that rejected image current,
    where a reviewer can approve it;
  - a deletion that read `captured`, a concurrent promotion and approval, and
    the deletion then unlinking the master of an approved record — which no
    transaction can take back;
  - a take-back that selected the `in_review` records, an approval committed in
    between, and the take-back overwriting it with `captured`, undoing a
    reviewer's ruling.

The first two are closed by taking the record row under SELECT ... FOR UPDATE
in every path that moves a status or changes which images are current. The
third is closed by making take-back one statement, so the database evaluates
the predicate and the write together.

The suite runs on SQLite, where SQLAlchemy emits no FOR UPDATE at all, so no
test here can demonstrate the exclusion itself. What these tests can do is
read the SQL that would reach PostgreSQL, which is where the lock has to
appear and where `of=Record` has to keep it off the outer-joined images —
PostgreSQL refuses FOR UPDATE on the nullable side of a LEFT JOIN, so a lock
written without `of` would fail on the appliance and pass here.

Run with: python -m pytest tests/unit/test_workflow_row_locking.py
"""

import pytest

from sqlalchemy.dialects import postgresql, sqlite


def _sql(query, dialect) -> str:
    return str(query.statement.compile(dialect=dialect)).upper()


@pytest.mark.unit
@pytest.mark.parametrize("with_images", [False, True])
def test_the_locked_loader_locks_the_record_row(db_session, with_images):
    from app.api.records import _locked_record_query

    sql = _sql(_locked_record_query(db_session, 1, with_images), postgresql.dialect())
    assert "FOR UPDATE OF RECORDS" in sql, sql


@pytest.mark.unit
def test_the_lock_is_scoped_off_the_images(db_session):
    """`of=Record` is not a refinement, it is what makes the statement legal.

    The images arrive through a LEFT OUTER JOIN, and PostgreSQL refuses FOR
    UPDATE on the nullable side of one. A lock written without `of` would
    raise on the appliance and pass on SQLite, which emits no lock at all.
    """
    from app.api.records import _locked_record_query

    sql = _sql(_locked_record_query(db_session, 1, True), postgresql.dialect())
    locked_tables = sql.split("FOR UPDATE", 1)[1]
    assert "RECORDS" in locked_tables, sql
    assert "RECORD_IMAGES" not in locked_tables, sql


@pytest.mark.unit
def test_sqlite_emits_no_lock_so_the_suite_cannot_prove_exclusion(db_session):
    """Stated as a test so the limitation is not rediscovered as a surprise."""
    from app.api.records import _locked_record_query

    sql = _sql(_locked_record_query(db_session, 1, False), sqlite.dialect())
    assert "FOR UPDATE" not in sql, (
        "SQLite now emits a lock clause; the note in this module and in "
        "_locked_record is out of date"
    )


@pytest.mark.unit
def test_every_workflow_path_loads_the_record_through_the_locked_loader():
    """A path that loads a record to change its status must not do it by hand.

    Checked against the source rather than by behaviour, because on SQLite the
    two loaders are indistinguishable at runtime.
    """
    import inspect

    from app.api import records as records_api

    for name in ("update_record_status", "bulk_update_status", "reject_record",
                 "delete_record", "delete_image"):
        src = inspect.getsource(getattr(records_api, name))
        assert "db.query(Record).filter(Record.id" not in src, (
            f"{name} loads a record without the row lock"
        )
        assert ("_locked_record" in src or "with_for_update" in src), (
            f"{name} does not take the record row lock"
        )

    from app.api import cameras as cameras_api

    for name in ("trigger_capture", "trigger_dual_capture"):
        src = inspect.getsource(getattr(cameras_api, name))
        assert "db.query(Record).filter(Record.id == request.record_id)" not in src, (
            f"{name} loads the record it retakes without the row lock"
        )
        assert "_locked_record(db, request.record_id)" in src, (
            f"{name} does not take the record row lock"
        )


@pytest.mark.unit
def test_promote_and_bulk_take_their_rows_in_id_order():
    """Two callers over overlapping sets must not take rows in opposite orders."""
    import inspect

    from app.api import collections as collections_api
    from app.api import records as records_api

    for fn in (collections_api.promote_collection, records_api.bulk_update_status):
        src = inspect.getsource(fn)
        assert "with_for_update" in src, f"{fn.__name__} does not lock its rows"
        assert "order_by(Record.id)" in src, f"{fn.__name__} locks rows in no fixed order"


@pytest.mark.unit
def test_take_back_is_a_single_statement():
    """A select-then-assign is what overwrote an approval."""
    import inspect

    from app.api.collections import take_back_collection

    src = inspect.getsource(take_back_collection)
    assert '.values(status="captured")' in src, src
    assert 'Record.status == "in_review")' in src.split(".values(")[0].rsplit("update(Record)", 1)[1], (
        "the in_review predicate must sit on the UPDATE itself"
    )
    assert 'rec.status = "captured"' not in src, (
        "take-back assigns per object again; the predicate and the write must "
        "be one statement"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
