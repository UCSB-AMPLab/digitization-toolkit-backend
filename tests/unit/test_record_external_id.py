"""`records.external_id` — the catalogue's own identifier, written once.

A record captured on the appliance has to be traceable back to the catalogue
entry it was captured for. That entry's identifier is Fisqua's
`reference_code`, and `records.external_id` is where it lands: unique so two
records cannot claim the same catalogue entry, nullable because a record
captured without a catalogue entry has none, indexed because the exchange
looks records up by it.

No writer exists in the tree: the column is absent from every request schema,
so the only way a value arrives is an importer assigning the attribute. What
that importer has to be able to rely on is that once a record carries an
external_id, nothing can change it. A reference code that can be reassigned is
worse than no reference code, because every trace drawn through it is then
unverifiable.
"""

import pytest
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
# The column
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_records_table_has_a_nullable_external_id_column():
    from app.models.record import Record

    col = Record.__table__.columns.get("external_id")
    assert col is not None, "records.external_id is missing"
    assert col.nullable is True, "a record captured without a catalogue entry has none"
    assert col.type.length == 255, col.type


@pytest.mark.unit
def test_external_id_carries_one_unique_index():
    from app.models.record import Record

    on_external_id = [
        ix for ix in Record.__table__.indexes
        if [c.name for c in ix.columns] == ["external_id"]
    ]
    assert len(on_external_id) == 1, [ix.name for ix in on_external_id]
    assert on_external_id[0].unique is True, on_external_id[0].name
    assert on_external_id[0].name == "ix_records_external_id", on_external_id[0].name
    # Uniqueness is carried by that index and not also by a table constraint,
    # which is what the migration has to match to leave no drift.
    assert not any(
        [c.name for c in getattr(con, "columns", [])] == ["external_id"]
        for con in Record.__table__.constraints
    ), [type(c).__name__ for c in Record.__table__.constraints]


# ---------------------------------------------------------------------------
# Read-only on the response
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_record_response_exposes_external_id():
    from app.schemas.record import RecordRead

    assert "external_id" in RecordRead.model_fields


@pytest.mark.unit
def test_no_request_schema_accepts_external_id():
    from app.schemas.record import RecordBase, RecordCreate, RecordUpdate

    offenders = [
        schema.__name__ for schema in (RecordBase, RecordCreate, RecordUpdate)
        if "external_id" in schema.model_fields
    ]
    assert offenders == [], offenders


@pytest.mark.unit
def test_create_record_ignores_a_client_supplied_external_id(client, db_session):
    from app.models.record import Record

    api = _client_as(client, "op", "operator")

    resp = api.post("/records/", json={"title": "t", "capture_mode": "single",
                                       "external_id": "CO-NG-0001"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["external_id"] is None, resp.json()
    rec = db_session.query(Record).order_by(Record.id.desc()).first()
    assert rec.external_id is None


@pytest.mark.unit
def test_patch_cannot_set_external_id(client, db_session):
    from app.models.record import Record

    rec = Record(title="t", status="in_review", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    api = _client_as(client, "op", "operator")

    resp = api.patch(f"/records/{rec.id}", json={"external_id": "CO-NG-0002"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["external_id"] is None, resp.json()
    db_session.refresh(rec)
    assert rec.external_id is None


@pytest.mark.unit
def test_a_set_external_id_reaches_the_response(client, db_session):
    from app.models.record import Record

    rec = Record(title="t", status="in_review", capture_mode="single",
                 external_id="CO-NG-0003")
    db_session.add(rec)
    db_session.commit()
    api = _client_as(client, "op", "operator")

    resp = api.get(f"/records/{rec.id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["external_id"] == "CO-NG-0003", resp.json()


# ---------------------------------------------------------------------------
# Uniqueness
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_two_records_cannot_share_one_external_id(db_session):
    from app.models.record import Record

    db_session.add(Record(title="a", status="in_review", capture_mode="single",
                          external_id="CO-NG-0004"))
    db_session.commit()
    db_session.add(Record(title="b", status="in_review", capture_mode="dual",
                          external_id="CO-NG-0004"))

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
def test_many_records_can_have_no_external_id(db_session):
    from app.models.record import Record

    db_session.add_all([
        Record(title="a", status="in_review", capture_mode="single"),
        Record(title="b", status="in_review", capture_mode="dual"),
        Record(title="c", status="in_review", capture_mode="dual"),
    ])
    db_session.commit()

    assert db_session.query(Record).filter(Record.external_id.is_(None)).count() == 3


# ---------------------------------------------------------------------------
# Immutability once set
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_external_id_can_be_written_once(db_session):
    from app.models.record import Record

    rec = Record(title="t", status="in_review", capture_mode="single")
    db_session.add(rec)
    db_session.commit()

    rec.external_id = "CO-NG-0005"
    db_session.commit()
    db_session.refresh(rec)

    assert rec.external_id == "CO-NG-0005"


@pytest.mark.unit
def test_a_second_write_of_a_different_external_id_is_refused(db_session):
    from app.models.record import ImmutableFieldError, Record

    rec = Record(title="t", status="in_review", capture_mode="single",
                 external_id="CO-NG-0006")
    db_session.add(rec)
    db_session.commit()

    with pytest.raises(ImmutableFieldError):
        rec.external_id = "CO-NG-0007"

    # The refusal happens on assignment, so nothing reaches the database and
    # the stored value is the first one.
    db_session.rollback()
    db_session.refresh(rec)
    assert rec.external_id == "CO-NG-0006"


@pytest.mark.unit
def test_clearing_a_set_external_id_is_refused(db_session):
    from app.models.record import ImmutableFieldError, Record

    rec = Record(title="t", status="in_review", capture_mode="single",
                 external_id="CO-NG-0008")
    db_session.add(rec)
    db_session.commit()

    with pytest.raises(ImmutableFieldError):
        rec.external_id = None

    db_session.rollback()
    db_session.refresh(rec)
    assert rec.external_id == "CO-NG-0008"


@pytest.mark.unit
def test_rewriting_the_same_external_id_changes_nothing_and_is_allowed(db_session):
    """An importer re-running over a record it already stamped is not a second write."""
    from app.models.record import Record

    rec = Record(title="t", status="in_review", capture_mode="single",
                 external_id="CO-NG-0009")
    db_session.add(rec)
    db_session.commit()

    rec.external_id = "CO-NG-0009"
    db_session.commit()
    db_session.refresh(rec)

    assert rec.external_id == "CO-NG-0009"


@pytest.mark.unit
def test_the_guard_holds_on_a_record_loaded_in_a_fresh_identity_map(db_session):
    """A first write is first against what is stored, not against this session's history."""
    from app.models.record import ImmutableFieldError, Record

    rec = Record(title="t", status="in_review", capture_mode="single",
                 external_id="CO-NG-0010")
    db_session.add(rec)
    db_session.commit()
    rec_id = rec.id
    db_session.expunge_all()

    reloaded = db_session.query(Record).filter(Record.id == rec_id).one()
    with pytest.raises(ImmutableFieldError):
        reloaded.external_id = "CO-NG-0011"


@pytest.mark.unit
def test_removing_a_set_external_id_is_refused(db_session):
    """`del rec.external_id` is a change like any other, so it is refused.

    Removing the attribute and clearing it to NULL end in the same place: a
    record that carried a reference code no longer carries one. The refusal is
    the same refusal.
    """
    from app.models.record import ImmutableFieldError, Record

    rec = Record(title="t", status="in_review", capture_mode="single",
                 external_id="CO-NG-0014")
    db_session.add(rec)
    db_session.commit()

    with pytest.raises(ImmutableFieldError):
        del rec.external_id

    db_session.rollback()
    db_session.refresh(rec)
    assert rec.external_id == "CO-NG-0014"


@pytest.mark.unit
def test_a_removal_cannot_be_used_to_reassign_an_external_id(db_session):
    """The removal path has to be closed or the write-once guarantee is false.

    A `validates` guard receives set events and, unless it asks for them, not
    removes. A removal that goes through unchecked leaves the attribute empty,
    and the next assignment then reads nothing stored and is allowed through as
    a first write — two lines, no bulk update, no raw SQL, and the stored
    reference code has changed.
    """
    from app.models.record import ImmutableFieldError, Record

    rec = Record(title="t", status="in_review", capture_mode="single",
                 external_id="CO-NG-0015")
    db_session.add(rec)
    db_session.commit()

    with pytest.raises(ImmutableFieldError):
        del rec.external_id
    with pytest.raises(ImmutableFieldError):
        rec.external_id = "CO-NG-0016"

    db_session.rollback()
    db_session.refresh(rec)
    assert rec.external_id == "CO-NG-0015"


@pytest.mark.unit
def test_removing_an_unset_external_id_is_not_refused(db_session):
    """With nothing stored there is nothing to protect, so the guard stands aside.

    The symmetry is with the same-value rewrite the guard already allows: a
    removal that takes nothing away is not a write. On a record loaded from a
    row where the column is NULL the attribute holds a value of None, so the
    removal simply succeeds; on an instance that never held one at all
    SQLAlchemy raises its own AttributeError, which is not the guard's doing
    and is not changed here.
    """
    from app.models.record import Record

    rec = Record(title="t", status="in_review", capture_mode="single")
    db_session.add(rec)
    db_session.commit()

    del rec.external_id

    db_session.rollback()
    db_session.refresh(rec)
    assert rec.external_id is None


@pytest.mark.unit
def test_a_bulk_update_is_not_covered_by_the_guard(db_session):
    """The scope of the guard, asserted so a reader is not misled about it.

    It is an ORM attribute guard: `query().update()` and raw SQL do not go
    through the attribute event, so neither is refused. Every writer in the
    tree assigns attributes, and so will the importer, so the guard covers the
    paths that exist — but a bulk path would need its own check or a database
    trigger.
    """
    from app.models.record import Record

    rec = Record(title="t", status="in_review", capture_mode="single",
                 external_id="CO-NG-0012")
    db_session.add(rec)
    db_session.commit()

    db_session.query(Record).filter(Record.id == rec.id).update(
        {"external_id": "CO-NG-0013"}, synchronize_session=False)
    db_session.commit()
    db_session.refresh(rec)

    assert rec.external_id == "CO-NG-0013"


# ---------------------------------------------------------------------------
# The migration matches what the model declares
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_migration_adds_the_column_and_the_unique_index(backend_root):
    from app.models.record import Record

    path = (backend_root / "alembic" / "versions"
            / "c2d3e4f5a6b7_add_record_external_id.py")
    assert path.exists(), path
    source = path.read_text()

    index = next(ix for ix in Record.__table__.indexes
                 if [c.name for c in ix.columns] == ["external_id"])
    assert "down_revision: Union[str, None] = 'b1c2d3e4f5a6'" in source
    assert "op.add_column('records'" in source
    assert "sa.String(length=255)" in source
    assert f"'{index.name}'" in source
    assert "unique=True" in source
    assert "op.drop_index" in source
    assert "op.drop_column('records', 'external_id')" in source
