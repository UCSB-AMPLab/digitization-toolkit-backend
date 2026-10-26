"""`records.date_start` / `records.date_end` — the queryable form of a record's date.

`records.date` is a free-text display field and stays one: it is where "1750",
"c. 1750" or "mediados del siglo XVIII" belongs, and none of those can be
compared, sorted or filtered in SQL. A structured range parked in
`custom_attributes` would be no better, because that column is `Text` holding
encoded JSON rather than a `JSON` column, so the range would not be queryable
there either.

So the range gets two real `DATE` columns beside the display field. What they
can express is a span with day resolution: a document known only to the year
is the whole of that year, a document known to the month is the whole of that
month, a single-day document is a span of one day. What they cannot express is
the *precision* of what is known — "sometime in 1750" and "throughout 1750"
are the same two values — nor approximation ("circa"). The display field
carries the human form; these two carry the machine form.

Both are independently nullable, because "after 1750" and "before 1800" are
real archival date expressions and both halves of a range are not always
known. What is refused is an end before its start, and it is refused by the
database, because a CHECK can compare two columns of one row.

Nothing writes these columns yet. They are absent from every request schema
and exposed read-only on the response; the importer that will fill them is
blocked on the exchange contract.

Version: 0.1.0-beta
"""

from datetime import date

import pytest
from sqlalchemy import Date
from sqlalchemy.exc import IntegrityError


def _user(username, role):
    from app.models.user import User

    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, username, role):
    from app.main import app
    import app.api.records as records

    user = _user(username, role)
    for dep in (records.allow_contributor, records.allow_read_only):
        app.dependency_overrides[dep] = lambda u=user: u
    return client


# ---------------------------------------------------------------------------
# The columns
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_records_table_has_a_nullable_date_start_and_date_end():
    from app.models.record import Record

    for name in ("date_start", "date_end"):
        col = Record.__table__.columns.get(name)
        assert col is not None, f"records.{name} is missing"
        assert col.nullable is True, (
            f"records.{name} must be nullable: a half-open range is a real "
            f"archival date, and no existing row has a value"
        )
        assert isinstance(col.type, Date), (
            f"records.{name} is {col.type!r}; a DATE is what makes the range "
            f"comparable in SQL, which is the whole reason for the columns"
        )


@pytest.mark.unit
def test_the_free_text_display_date_stays():
    """The structured range is additional, not a replacement.

    `date` is where an uncertain or verbal date lives, and nothing the two
    DATE columns can hold replaces "c. 1750".
    """
    from app.models.record import Record

    col = Record.__table__.columns.get("date")
    assert col is not None, "records.date was removed; it is the display field"
    assert col.type.length == 50, col.type
    assert col.nullable is True


@pytest.mark.unit
def test_neither_date_range_column_carries_an_index():
    """Deliberate: nothing queries them yet.

    A DATE column is comparable without an index — that is what distinguishes
    it from the `Text` `custom_attributes` the range would otherwise sit in.
    An index is what a *query path* needs, and no query path exists; adding
    one later is a one-line migration. Asserted so that an
    index arriving by accident, without a matching migration, shows up as a
    failure rather than as schema drift.
    """
    from app.models.record import Record

    assert Record.__table__.columns.get("date_start") is not None
    assert Record.__table__.columns.get("date_end") is not None

    indexed = sorted(
        ix.name for ix in Record.__table__.indexes
        if {"date_start", "date_end"} & {c.name for c in ix.columns}
    )
    assert indexed == [], indexed


# ---------------------------------------------------------------------------
# Read-only on the response
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_record_response_exposes_the_date_range():
    from app.schemas.record import RecordRead

    missing = [f for f in ("date_start", "date_end") if f not in RecordRead.model_fields]
    assert missing == [], missing


@pytest.mark.unit
def test_no_request_schema_accepts_the_date_range():
    """Who may write the range is not settled, so for now nobody may.

    Keeping the columns off the request schemas is the reversible half of that
    question: adding a field to a request schema later is additive, removing
    one that clients have started sending is not.
    """
    from app.schemas.record import RecordBase, RecordCreate, RecordUpdate

    offenders = [
        f"{schema.__name__}.{field}"
        for schema in (RecordBase, RecordCreate, RecordUpdate)
        for field in ("date_start", "date_end")
        if field in schema.model_fields
    ]
    assert offenders == [], offenders


@pytest.mark.unit
def test_create_record_ignores_a_client_supplied_date_range(client, db_session):
    from app.models.record import Record

    api = _client_as(client, "op", "operator")

    resp = api.post("/records/", json={"title": "t", "capture_mode": "single",
                                       "date": "1750",
                                       "date_start": "1750-01-01",
                                       "date_end": "1750-12-31"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["date"] == "1750", body
    assert body["date_start"] is None, body
    assert body["date_end"] is None, body
    rec = db_session.query(Record).order_by(Record.id.desc()).first()
    assert rec.date_start is None
    assert rec.date_end is None


@pytest.mark.unit
def test_patch_cannot_set_the_date_range(client, db_session):
    from app.models.record import Record

    rec = Record(title="t", status="in_review", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    api = _client_as(client, "op", "operator")

    resp = api.patch(f"/records/{rec.id}", json={"date": "1750",
                                                 "date_start": "1750-01-01",
                                                 "date_end": "1750-12-31"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["date"] == "1750", body
    assert body["date_start"] is None, body
    assert body["date_end"] is None, body
    db_session.refresh(rec)
    assert rec.date_start is None
    assert rec.date_end is None


@pytest.mark.unit
def test_a_set_date_range_reaches_the_response(client, db_session):
    from app.models.record import Record

    rec = Record(title="t", status="in_review", capture_mode="single",
                 date="1750", date_start=date(1750, 1, 1), date_end=date(1750, 12, 31))
    db_session.add(rec)
    db_session.commit()
    api = _client_as(client, "op", "operator")

    resp = api.get(f"/records/{rec.id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["date_start"] == "1750-01-01", body
    assert body["date_end"] == "1750-12-31", body


# ---------------------------------------------------------------------------
# What ranges are allowed
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_record_can_have_no_dates_at_all(db_session):
    """Which is every row on every unit today."""
    from app.models.record import Record

    rec = Record(title="t", status="in_review", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)

    assert rec.date_start is None
    assert rec.date_end is None


@pytest.mark.unit
def test_a_start_without_an_end_is_allowed(db_session):
    """"after 1750" — the start is known and the end is not."""
    from app.models.record import Record

    rec = Record(title="t", status="in_review", capture_mode="single",
                 date="after 1750", date_start=date(1750, 1, 1))
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)

    assert rec.date_start == date(1750, 1, 1)
    assert rec.date_end is None


@pytest.mark.unit
def test_an_end_without_a_start_is_allowed(db_session):
    """"before 1800" — the end is known and the start is not."""
    from app.models.record import Record

    rec = Record(title="t", status="in_review", capture_mode="single",
                 date="before 1800", date_end=date(1799, 12, 31))
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)

    assert rec.date_start is None
    assert rec.date_end == date(1799, 12, 31)


@pytest.mark.unit
def test_a_single_day_range_is_allowed(db_session):
    """A document dated to one day has a start equal to its end.

    Which is why the check is `>=` and not `>`.
    """
    from app.models.record import Record

    day = date(1750, 6, 14)
    rec = Record(title="t", status="in_review", capture_mode="single",
                 date="14 June 1750", date_start=day, date_end=day)
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)

    assert rec.date_start == day
    assert rec.date_end == day


@pytest.mark.unit
def test_a_year_long_range_is_allowed(db_session):
    """A document known only to the year is the whole of that year."""
    from app.models.record import Record

    rec = Record(title="t", status="in_review", capture_mode="single",
                 date="1750", date_start=date(1750, 1, 1), date_end=date(1750, 12, 31))
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)

    assert rec.date_start == date(1750, 1, 1)
    assert rec.date_end == date(1750, 12, 31)


# ---------------------------------------------------------------------------
# An end before its start is refused, by the database
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_range_check_is_declared_on_the_model(db_session):
    """Named, so the migration can create and drop the same constraint.

    `create_all` builds the test schema from the model, so a check declared
    only in the migration would never be exercised here, and one declared
    only on the model would never reach the appliance.
    """
    from sqlalchemy import CheckConstraint
    from app.models.record import Record

    names = [c.name for c in Record.__table__.constraints
             if isinstance(c, CheckConstraint)]
    assert "check_record_date_range" in names, names


@pytest.mark.unit
def test_an_end_before_its_start_is_refused_on_insert(db_session):
    from app.models.record import Record

    db_session.add(Record(title="t", status="in_review", capture_mode="single",
                          date_start=date(1750, 1, 1), date_end=date(1749, 12, 31)))

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
def test_an_end_before_its_start_is_refused_on_update(db_session):
    from app.models.record import Record

    rec = Record(title="t", status="in_review", capture_mode="single",
                 date_start=date(1750, 1, 1), date_end=date(1750, 12, 31))
    db_session.add(rec)
    db_session.commit()

    rec.date_end = date(1749, 12, 31)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
def test_the_range_check_also_catches_a_bulk_update(db_session):
    """The reason the check is in the database and not on the model.

    An immutability rule has to be an ORM guard, because it compares a new
    value against the stored one and no CHECK can see the old row. An
    ordering rule is different: it compares two columns of the same row,
    which is exactly what a CHECK does — so it holds against
    `query().update()` and raw SQL as well. The importer that will fill
    these columns is the writer most likely to use it.
    """
    from app.models.record import Record

    rec = Record(title="t", status="in_review", capture_mode="single",
                 date_start=date(1750, 1, 1), date_end=date(1750, 12, 31))
    db_session.add(rec)
    db_session.commit()

    with pytest.raises(IntegrityError):
        db_session.query(Record).filter(Record.id == rec.id).update(
            {"date_end": date(1749, 12, 31)}, synchronize_session=False)
        db_session.commit()
    db_session.rollback()

    db_session.refresh(rec)
    assert rec.date_end == date(1750, 12, 31)


@pytest.mark.unit
def test_a_half_open_range_cannot_violate_the_check(db_session):
    """With one end NULL there is nothing to compare, so the check passes.

    Asserted as a pair with the refusals above: a check that rejected a
    half-open range would make "after 1750" and "before 1800" unstorable, and
    those are the dates the columns exist to carry.

    It does not pin the *form* of the check, and nothing can. Writing it as a
    bare `date_end >= date_start` behaves identically — the comparison
    evaluates to NULL when either end is NULL, and a CHECK accepts NULL — so
    the explicit `IS NULL OR` form is a legibility choice with no behaviour
    behind it. Replacing the explicit form with the bare one leaves this file
    green.
    """
    from app.models.record import Record

    db_session.add_all([
        Record(title="a", status="in_review", capture_mode="single",
               date_start=date(1750, 1, 1)),
        Record(title="b", status="in_review", capture_mode="dual",
               date_end=date(1799, 12, 31)),
        Record(title="c", status="in_review", capture_mode="dual"),
    ])
    db_session.commit()

    assert db_session.query(Record).count() == 3


# ---------------------------------------------------------------------------
# The range is queryable, which is the point of the columns
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_records_can_be_filtered_by_an_overlapping_range(db_session):
    """What `custom_attributes` could not answer.

    Not an endpoint — no query filter exists — but the
    columns have to be able to carry one, and a `Text` column holding encoded
    JSON cannot.
    """
    from app.models.record import Record

    db_session.add_all([
        Record(title="1750", status="in_review", capture_mode="single",
               date_start=date(1750, 1, 1), date_end=date(1750, 12, 31)),
        Record(title="1790s", status="in_review", capture_mode="single",
               date_start=date(1790, 1, 1), date_end=date(1799, 12, 31)),
        Record(title="undated", status="in_review", capture_mode="single"),
    ])
    db_session.commit()

    # Everything overlapping the second half of the eighteenth century.
    hits = db_session.query(Record).filter(
        Record.date_start <= date(1800, 1, 1),
        Record.date_end >= date(1750, 1, 1),
    ).all()

    assert sorted(r.title for r in hits) == ["1750", "1790s"]


# ---------------------------------------------------------------------------
# The migration matches what the model declares
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_migration_adds_both_columns_and_the_range_check(backend_root):
    path = (backend_root / "alembic" / "versions"
            / "d3e4f5a6b7c8_add_record_date_range.py")
    assert path.exists(), path
    source = path.read_text()

    assert "down_revision: Union[str, None] = 'c2d3e4f5a6b7'" in source
    for name in ("date_start", "date_end"):
        assert f"sa.Column('{name}', sa.Date(), nullable=True)" in source, name
        assert f"op.drop_column('records', '{name}')" in source, name
    assert "op.create_check_constraint(" in source
    assert "'check_record_date_range'" in source
    assert "type_='check'" in source
