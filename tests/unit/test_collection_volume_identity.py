"""A volume's identity: `collections.filename_stem`, `external_id` and the descriptive fields.

Exchange contract 1.1 maps each Fisqua item onto a Captua volume, which in code
is a row of `collections`. The volume carries Fisqua's reference code as
`external_id`, and a `filename_stem` from which its image file names
(`<stem>-img_<nnnn>_c<n>`), its disk layout and its bucket keys will later be
built. Those later uses are not built here; what is built is the identity they
rely on, and the rules that keep it stable:

- `external_id` is written once and never changed or cleared;
- `filename_stem` can be set and changed until the volume holds its first
  image, superseded images included; a NULL stem may be set once even after
  that, and a set stem on a volume with images is fixed;
- both are unique, and a duplicate is a 409 through the sanitised
  constraint-violation handler;
- a stem is a lowercase slug, short enough that `<stem>-img_<nnnn>` fits
  in `pair_id`;
- the date range refuses an end before its start, as on records.

The test database is SQLite (tests/conftest.py, `db_session`), so the
database-level checks exercised here are the SQLite forms. The Postgres forms
are exercised by CI's drift guard and the migration rehearsal, not by this file.
"""

import importlib.util
import json
from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError


MIGRATION_FILE = "a7c1e9d2b4f6_add_collection_volume_identity.py"


def _user(username, role):
    from app.models.user import User

    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, username="op", role="operator"):
    from app.main import app
    import app.api.collections as collections

    user = _user(username, role)
    for dep in (collections.allow_contributor, collections.allow_read_only):
        app.dependency_overrides[dep] = lambda u=user: u
    return client


def _project(db_session, name="P"):
    from app.models.project import Project

    p = Project(name=name)
    db_session.add(p)
    db_session.commit()
    return p


def _volume(db_session, project, name="V", **kw):
    from app.models.collection import Collection

    c = Collection(name=name, project_id=project.id, **kw)
    db_session.add(c)
    db_session.commit()
    return c


def _image_in(db_session, volume, *, is_current=True):
    from app.models.record import Record, RecordImage

    rec = Record(title="r", status="captured", capture_mode="single",
                 collection_id=volume.id)
    db_session.add(rec)
    db_session.flush()
    img = RecordImage(record_id=rec.id, filename="x.jpg", file_path="/tmp/x.jpg",
                      format="jpeg", is_current=is_current)
    db_session.add(img)
    db_session.commit()
    return img


# ---------------------------------------------------------------------------
# The columns
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_collections_has_the_volume_identity_columns():
    from app.models.collection import Collection

    cols = Collection.__table__.columns
    expected = {
        "filename_stem": sa.String,
        "external_id": sa.String,
        "date": sa.String,
        "date_start": sa.Date,
        "date_end": sa.Date,
        "sequence": sa.Integer,
        "custom_attributes": sa.Text,
    }
    for name, type_ in expected.items():
        col = cols.get(name)
        assert col is not None, f"collections.{name} is missing"
        assert col.nullable is True, f"collections.{name} must be nullable: existing volumes have none"
        assert isinstance(col.type, type_), (name, col.type)
    assert cols["external_id"].type.length == 255
    assert cols["date"].type.length == 50


@pytest.mark.unit
def test_external_id_carries_one_unique_index():
    from app.models.collection import Collection

    on_external_id = [ix for ix in Collection.__table__.indexes
                      if [c.name for c in ix.columns] == ["external_id"]]
    assert len(on_external_id) == 1, [ix.name for ix in on_external_id]
    assert on_external_id[0].unique is True
    assert on_external_id[0].name == "ix_collections_external_id"


@pytest.mark.unit
def test_filename_stem_is_unique_by_a_named_constraint():
    from app.models.collection import Collection

    uniques = [c for c in Collection.__table__.constraints
               if isinstance(c, sa.UniqueConstraint)
               and [col.name for col in c.columns] == ["filename_stem"]]
    assert [u.name for u in uniques] == ["collections_filename_stem_key"]


@pytest.mark.unit
def test_the_stem_cap_is_derived_from_pair_id():
    """`<stem>-img_<nnnn>` has to fit in pair_id, which is 36 characters."""
    from app.models.collection import FILENAME_STEM_MAX_LENGTH
    from app.models.record import RecordImage

    pair_id_length = RecordImage.__table__.columns["pair_id"].type.length
    assert pair_id_length == 36
    assert FILENAME_STEM_MAX_LENGTH == pair_id_length - len("-img_0000") == 27


# ---------------------------------------------------------------------------
# The stem's pattern and length
# ---------------------------------------------------------------------------

VALID_STEMS = ["co-ahrb-aht-003", "a", "0", "abc123", "a-b-c", "x" * 27]
INVALID_STEMS = ["", "Co-ahrb", "co_ahrb", "-co", "co-", "co--ahrb", "co ahrb",
                 "co.ahrb", "có-ahrb", "x" * 28]


@pytest.mark.unit
@pytest.mark.parametrize("stem", VALID_STEMS)
def test_the_create_schema_accepts_a_valid_stem(stem):
    from app.schemas.collection import CollectionCreate

    assert CollectionCreate(name="v", project_id=1, filename_stem=stem).filename_stem == stem


@pytest.mark.unit
@pytest.mark.parametrize("stem", INVALID_STEMS)
def test_the_create_and_update_schemas_refuse_an_invalid_stem(stem):
    from pydantic import ValidationError
    from app.schemas.collection import CollectionCreate, CollectionUpdate

    with pytest.raises(ValidationError):
        CollectionCreate(name="v", project_id=1, filename_stem=stem)
    with pytest.raises(ValidationError):
        CollectionUpdate(filename_stem=stem)


@pytest.mark.unit
@pytest.mark.parametrize("stem", VALID_STEMS)
def test_the_database_accepts_a_valid_stem(db_session, stem):
    p = _project(db_session)
    v = _volume(db_session, p, filename_stem=stem)
    db_session.refresh(v)
    assert v.filename_stem == stem


@pytest.mark.unit
@pytest.mark.parametrize("stem", INVALID_STEMS)
def test_the_database_refuses_an_invalid_stem_written_around_the_schema(db_session, stem):
    """Raw SQL goes around both the schema and the ORM; the CHECKs still hold."""
    p = _project(db_session)
    with pytest.raises(IntegrityError):
        db_session.execute(
            sa.text("INSERT INTO collections (name, project_id, filename_stem, created_at) "
                    "VALUES ('v', :p, :s, CURRENT_TIMESTAMP)"),
            {"p": p.id, "s": stem},
        )
    db_session.rollback()


@pytest.mark.unit
def test_post_refuses_a_28_character_stem_and_accepts_27(client, db_session):
    p = _project(db_session)
    api = _client_as(client)

    bad = api.post("/collections/", json={"name": "a", "project_id": p.id,
                                          "filename_stem": "x" * 28})
    good = api.post("/collections/", json={"name": "b", "project_id": p.id,
                                           "filename_stem": "x" * 27})

    assert bad.status_code == 422, bad.text
    assert good.status_code == 201, good.text
    assert good.json()["filename_stem"] == "x" * 27


# ---------------------------------------------------------------------------
# Uniqueness
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_two_volumes_cannot_share_a_stem(db_session):
    p = _project(db_session)
    _volume(db_session, p, "a", filename_stem="co-x-001")
    from app.models.collection import Collection
    db_session.add(Collection(name="b", project_id=p.id, filename_stem="co-x-001"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
def test_two_volumes_cannot_share_an_external_id(db_session):
    p = _project(db_session)
    _volume(db_session, p, "a", external_id="CO.AHRB.1")
    from app.models.collection import Collection
    db_session.add(Collection(name="b", project_id=p.id, external_id="CO.AHRB.1"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
def test_many_volumes_can_have_neither(db_session):
    from app.models.collection import Collection

    p = _project(db_session)
    for n in "abcd":
        _volume(db_session, p, n)
    assert db_session.query(Collection).filter(
        Collection.filename_stem.is_(None), Collection.external_id.is_(None)).count() == 4


@pytest.mark.unit
@pytest.mark.parametrize("field,value,constraint", [
    ("filename_stem", "co-x-002", "collections_filename_stem_key"),
    ("external_id", "CO.AHRB.2", "ix_collections_external_id"),
])
def test_a_duplicate_on_post_is_a_sanitised_409(client, db_session, field, value, constraint):
    p = _project(db_session)
    _volume(db_session, p, "a", **{field: value})
    api = _client_as(client)

    resp = api.post("/collections/", json={"name": "b", "project_id": p.id, field: value})

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["constraint"] == constraint, detail
    assert "INSERT" not in json.dumps(detail) and value not in json.dumps(detail), detail
    assert "another volume" in detail["message"].lower(), detail


@pytest.mark.unit
@pytest.mark.parametrize("field,value,constraint", [
    ("filename_stem", "co-x-003", "collections_filename_stem_key"),
    ("external_id", "CO.AHRB.3", "ix_collections_external_id"),
])
def test_a_duplicate_on_patch_is_a_sanitised_409(client, db_session, field, value, constraint):
    p = _project(db_session)
    _volume(db_session, p, "a", **{field: value})
    b = _volume(db_session, p, "b")
    api = _client_as(client)

    resp = api.patch(f"/collections/{b.id}", json={field: value})

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["constraint"] == constraint
    db_session.refresh(b)
    assert getattr(b, field) is None


@pytest.mark.unit
def test_a_duplicate_on_a_renaming_patch_is_a_409_and_the_name_is_unchanged(client, db_session):
    """A rename moves files, so the duplicate has to be refused before any move."""
    p = _project(db_session)
    _volume(db_session, p, "a", filename_stem="co-x-004")
    b = _volume(db_session, p, "b")
    api = _client_as(client)

    resp = api.patch(f"/collections/{b.id}", json={"name": "b2", "filename_stem": "co-x-004"})

    assert resp.status_code == 409, resp.text
    db_session.refresh(b)
    assert b.name == "b" and b.filename_stem is None


# ---------------------------------------------------------------------------
# external_id is written once
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_external_id_can_be_written_once_and_rewritten_with_the_same_value(db_session):
    p = _project(db_session)
    v = _volume(db_session, p)
    v.external_id = "CO.AHRB.10"
    db_session.commit()
    v.external_id = "CO.AHRB.10"
    db_session.commit()
    db_session.refresh(v)
    assert v.external_id == "CO.AHRB.10"


@pytest.mark.unit
@pytest.mark.parametrize("op", ["change", "clear", "delete"])
def test_a_set_external_id_cannot_be_changed_cleared_or_removed(db_session, op):
    from app.models.record import ImmutableFieldError

    p = _project(db_session)
    v = _volume(db_session, p, external_id="CO.AHRB.11")
    with pytest.raises(ImmutableFieldError):
        if op == "change":
            v.external_id = "CO.AHRB.12"
        elif op == "clear":
            v.external_id = None
        else:
            del v.external_id
    db_session.rollback()
    db_session.refresh(v)
    assert v.external_id == "CO.AHRB.11"


@pytest.mark.unit
def test_the_external_id_guard_holds_in_a_fresh_identity_map(db_session):
    from app.models.collection import Collection
    from app.models.record import ImmutableFieldError

    p = _project(db_session)
    v = _volume(db_session, p, external_id="CO.AHRB.13")
    vid = v.id
    db_session.expunge_all()
    reloaded = db_session.query(Collection).filter(Collection.id == vid).one()
    with pytest.raises(ImmutableFieldError):
        reloaded.external_id = "CO.AHRB.14"


@pytest.mark.unit
def test_patch_can_set_an_unset_external_id_and_cannot_change_it(client, db_session):
    p = _project(db_session)
    v = _volume(db_session, p)
    api = _client_as(client)

    first = api.patch(f"/collections/{v.id}", json={"external_id": "CO.AHRB.15"})
    second = api.patch(f"/collections/{v.id}", json={"external_id": "CO.AHRB.16"})
    cleared = api.patch(f"/collections/{v.id}", json={"external_id": None})
    same = api.patch(f"/collections/{v.id}", json={"external_id": "CO.AHRB.15"})

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert "written once" in second.json()["detail"]["message"], second.json()
    assert cleared.status_code == 409, cleared.text
    assert same.status_code == 200, same.text
    db_session.refresh(v)
    assert v.external_id == "CO.AHRB.15"


# ---------------------------------------------------------------------------
# filename_stem is fixed by the volume's first image
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_stem_can_be_set_changed_and_cleared_before_the_first_image(db_session):
    p = _project(db_session)
    v = _volume(db_session, p)
    v.filename_stem = "co-x-020"
    db_session.commit()
    v.filename_stem = "co-x-021"
    db_session.commit()
    v.filename_stem = None
    db_session.commit()
    db_session.refresh(v)
    assert v.filename_stem is None


@pytest.mark.unit
def test_a_record_without_images_does_not_fix_the_stem(db_session):
    from app.models.record import Record

    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="co-x-022")
    db_session.add(Record(title="r", status="captured", capture_mode="single",
                          collection_id=v.id))
    db_session.commit()
    v.filename_stem = "co-x-023"
    db_session.commit()
    db_session.refresh(v)
    assert v.filename_stem == "co-x-023"


@pytest.mark.unit
@pytest.mark.parametrize("is_current", [True, False], ids=["current", "superseded"])
@pytest.mark.parametrize("op", ["change", "clear", "delete"])
def test_a_stem_is_fixed_once_the_volume_has_an_image(db_session, is_current, op):
    from app.models.record import ImmutableFieldError

    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="co-x-024")
    _image_in(db_session, v, is_current=is_current)

    with pytest.raises(ImmutableFieldError):
        if op == "change":
            v.filename_stem = "co-x-025"
        elif op == "clear":
            v.filename_stem = None
        else:
            del v.filename_stem
    db_session.rollback()
    db_session.refresh(v)
    assert v.filename_stem == "co-x-024"


@pytest.mark.unit
def test_a_stem_is_fixed_after_its_only_image_is_superseded(db_session):
    """Superseding is what a retake does; the superseded file keeps its name."""
    from app.models.record import ImmutableFieldError

    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="co-x-026")
    img = _image_in(db_session, v)
    img.is_current = False
    db_session.commit()

    with pytest.raises(ImmutableFieldError):
        v.filename_stem = "co-x-027"


@pytest.mark.unit
def test_an_existing_volume_with_images_accepts_its_first_stem_then_refuses_a_second(db_session):
    """Existing volumes get their stems from an operator after capture.

    A NULL stem may be set once whether or not the volume has images; from
    then on the images fix it like any other set stem.
    """
    from app.models.record import ImmutableFieldError

    p = _project(db_session)
    v = _volume(db_session, p)
    _image_in(db_session, v)

    v.filename_stem = "co-x-028"
    db_session.commit()
    db_session.refresh(v)
    assert v.filename_stem == "co-x-028"

    with pytest.raises(ImmutableFieldError):
        v.filename_stem = "co-x-036"
    with pytest.raises(ImmutableFieldError):
        v.filename_stem = None
    db_session.rollback()
    db_session.refresh(v)
    assert v.filename_stem == "co-x-028"


@pytest.mark.unit
def test_patch_gives_a_volume_with_images_its_first_stem_and_refuses_a_second(client, db_session):
    p = _project(db_session)
    v = _volume(db_session, p)
    _image_in(db_session, v)
    api = _client_as(client)

    first = api.patch(f"/collections/{v.id}", json={"filename_stem": "co-x-037"})
    second = api.patch(f"/collections/{v.id}", json={"filename_stem": "co-x-038"})

    assert first.status_code == 200, first.text
    assert first.json()["filename_stem"] == "co-x-037"
    assert second.status_code == 409, second.text
    db_session.refresh(v)
    assert v.filename_stem == "co-x-037"


@pytest.mark.unit
def test_rewriting_the_same_stem_after_images_is_allowed(db_session):
    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="co-x-029")
    _image_in(db_session, v)
    v.filename_stem = "co-x-029"
    db_session.commit()


@pytest.mark.unit
def test_an_image_in_another_volume_does_not_fix_this_stem(db_session):
    p = _project(db_session)
    other = _volume(db_session, p, "other", filename_stem="co-x-030")
    _image_in(db_session, other)
    v = _volume(db_session, p, "v", filename_stem="co-x-031")
    v.filename_stem = "co-x-032"
    db_session.commit()


@pytest.mark.unit
def test_patch_changes_a_stem_before_images_and_refuses_it_after(client, db_session):
    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="co-x-033")
    api = _client_as(client)

    before = api.patch(f"/collections/{v.id}", json={"filename_stem": "co-x-034"})
    _image_in(db_session, v, is_current=False)
    after = api.patch(f"/collections/{v.id}", json={"filename_stem": "co-x-035"})

    assert before.status_code == 200, before.text
    assert before.json()["filename_stem"] == "co-x-034"
    assert after.status_code == 409, after.text
    assert "image" in after.json()["detail"]["message"], after.json()
    db_session.refresh(v)
    assert v.filename_stem == "co-x-034"


# ---------------------------------------------------------------------------
# The date range
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_database_refuses_an_end_before_its_start(db_session):
    from app.models.collection import Collection

    p = _project(db_session)
    db_session.add(Collection(name="v", project_id=p.id,
                              date_start=date(1750, 2, 1), date_end=date(1750, 1, 31)))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
@pytest.mark.parametrize("start,end", [
    (None, None), (date(1750, 1, 1), None), (None, date(1800, 1, 1)),
    (date(1750, 1, 1), date(1750, 1, 1)), (date(1750, 1, 1), date(1750, 12, 31)),
])
def test_the_database_accepts_half_open_single_day_and_ordered_ranges(db_session, start, end):
    p = _project(db_session)
    v = _volume(db_session, p, date_start=start, date_end=end)
    db_session.refresh(v)
    assert (v.date_start, v.date_end) == (start, end)


@pytest.mark.unit
def test_the_schemas_refuse_an_end_before_its_start():
    from pydantic import ValidationError
    from app.schemas.collection import CollectionCreate, CollectionUpdate

    with pytest.raises(ValidationError):
        CollectionCreate(name="v", project_id=1, date_start="1750-02-01", date_end="1750-01-31")
    with pytest.raises(ValidationError):
        CollectionUpdate(date_start="1750-02-01", date_end="1750-01-31")


@pytest.mark.unit
def test_a_patch_that_puts_the_end_before_the_stored_start_is_a_409(client, db_session):
    p = _project(db_session)
    v = _volume(db_session, p, date_start=date(1750, 1, 1))
    api = _client_as(client)

    resp = api.patch(f"/collections/{v.id}", json={"date_end": "1749-12-31"})

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["constraint"] == "check_collection_date_range"
    db_session.refresh(v)
    assert v.date_end is None


# ---------------------------------------------------------------------------
# The read schema
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_post_and_get_round_trip_every_new_field(client, db_session):
    p = _project(db_session)
    api = _client_as(client)
    attrs = json.dumps({"fisqua": {"typology": "Libro"}})

    created = api.post("/collections/", json={
        "name": "Tomo 6", "project_id": p.id, "collection_type": "volume",
        "filename_stem": "co-ahrb-aht-006", "external_id": "CO.AHRB.AHT.6",
        "date": "1750-1760", "date_start": "1750-01-01", "date_end": "1760-12-31",
        "sequence": 6, "custom_attributes": attrs,
    })
    assert created.status_code == 201, created.text

    body = api.get(f"/collections/{created.json()['id']}").json()
    assert body["filename_stem"] == "co-ahrb-aht-006"
    assert body["external_id"] == "CO.AHRB.AHT.6"
    assert body["date"] == "1750-1760"
    assert body["date_start"] == "1750-01-01"
    assert body["date_end"] == "1760-12-31"
    assert body["sequence"] == 6
    assert body["custom_attributes"] == attrs


@pytest.mark.unit
def test_an_existing_volume_reads_back_with_the_new_fields_null(client, db_session):
    p = _project(db_session)
    v = _volume(db_session, p)
    api = _client_as(client)

    body = api.get(f"/collections/{v.id}").json()
    for f in ("filename_stem", "external_id", "date", "date_start", "date_end",
              "sequence", "custom_attributes"):
        assert f in body and body[f] is None, (f, body)


# ---------------------------------------------------------------------------
# The migration, run against SQLite
# ---------------------------------------------------------------------------

def _load_migration(backend_root):
    path = backend_root / "alembic" / "versions" / MIGRATION_FILE
    assert path.exists(), path
    spec = importlib.util.spec_from_file_location("volume_identity_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pre_migration_schema(engine):
    """`collections` as it stands at f5a6b7c8d9e0, and enough of its neighbours.

    `records` and `record_images` carry only the columns the backfill reads.
    """
    md = sa.MetaData()
    sa.Table("projects", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("name", sa.String(255), nullable=False))
    sa.Table("collections", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("name", sa.String(255), nullable=False, index=True),
             sa.Column("description", sa.Text),
             sa.Column("collection_type", sa.String(50)),
             sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="CASCADE")),
             sa.Column("parent_collection_id", sa.Integer,
                       sa.ForeignKey("collections.id", ondelete="CASCADE")),
             sa.Column("archival_metadata", sa.JSON),
             sa.Column("created_by", sa.String(255)),
             sa.Column("created_at", sa.DateTime, nullable=False),
             sa.Column("updated_at", sa.DateTime),
             sa.CheckConstraint(
                 "(project_id IS NOT NULL AND parent_collection_id IS NULL) OR "
                 "(project_id IS NULL AND parent_collection_id IS NOT NULL)",
                 name="check_collection_parent"))
    sa.Table("records", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("collection_id", sa.Integer,
                       sa.ForeignKey("collections.id", ondelete="SET NULL")))
    sa.Table("record_images", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("record_id", sa.Integer,
                       sa.ForeignKey("records.id", ondelete="CASCADE"), nullable=False),
             sa.Column("capture_id", sa.String(36)),
             sa.Column("pair_id", sa.String(36)),
             sa.Column("is_current", sa.Boolean, nullable=False))
    md.create_all(engine)


def _run(engine, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


NEW_COLUMNS = {"filename_stem", "external_id", "date", "date_start", "date_end",
               "sequence", "custom_attributes", "capture_counter"}


@pytest.fixture
def sqlite_engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    yield engine
    engine.dispose()


@pytest.mark.unit
def test_the_migration_chains_onto_the_single_previous_head(backend_root):
    mod = _load_migration(backend_root)
    assert mod.down_revision == "f5a6b7c8d9e0"
    # Nothing else chains onto the previous head, so the tree has one head.
    siblings = [p.name for p in (backend_root / "alembic" / "versions").glob("*.py")
                if p.name != MIGRATION_FILE and "'f5a6b7c8d9e0'" in p.read_text()
                and not p.name.startswith("f5a6b7c8d9e0")]
    assert siblings == [], siblings


@pytest.mark.unit
@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
def test_the_migration_upgrades_and_downgrades_on_sqlite(backend_root, sqlite_engine, populated):
    mod = _load_migration(backend_root)
    _pre_migration_schema(sqlite_engine)
    before = {c["name"] for c in sa.inspect(sqlite_engine).get_columns("collections")}

    if populated:
        with sqlite_engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO projects (id, name) VALUES (1, 'Rionegro')"))
            for i in range(1, 5):
                conn.execute(sa.text(
                    "INSERT INTO collections (id, name, project_id, archival_metadata, created_at) "
                    "VALUES (:i, :n, 1, '{\"k\": 1}', CURRENT_TIMESTAMP)"),
                    {"i": i, "n": f"Tomo {i}"})
            # A nested volume: the schema allows it even though the bench unit has none.
            conn.execute(sa.text(
                "INSERT INTO collections (id, name, parent_collection_id, created_at) "
                "VALUES (5, 'Legajo', 1, CURRENT_TIMESTAMP)"))

    _run(sqlite_engine, mod.upgrade)

    insp = sa.inspect(sqlite_engine)
    after = {c["name"] for c in insp.get_columns("collections")}
    assert after == before | NEW_COLUMNS
    checks = {c["name"] for c in insp.get_check_constraints("collections")}
    assert {"check_collection_parent", "check_collection_date_range",
            "check_collection_filename_stem_pattern",
            "check_collection_filename_stem_length",
            "check_collection_capture_counter",
            "check_collection_external_id_trimmed"} <= checks, checks
    assert "collections_filename_stem_key" in {u["name"] for u in insp.get_unique_constraints("collections")}
    ix = {i["name"]: i for i in insp.get_indexes("collections")}
    assert ix["ix_collections_external_id"]["unique"] in (1, True)
    assert "ix_collections_name" in ix

    with sqlite_engine.begin() as conn:
        rows = conn.execute(sa.text(
            "SELECT id, name, project_id, parent_collection_id, archival_metadata, "
            "filename_stem, external_id, date, date_start, date_end, sequence, custom_attributes "
            "FROM collections ORDER BY id")).all()
    if populated:
        assert [r.name for r in rows] == ["Tomo 1", "Tomo 2", "Tomo 3", "Tomo 4", "Legajo"]
        assert rows[4].parent_collection_id == 1 and rows[4].project_id is None
        assert json.loads(rows[0].archival_metadata) == {"k": 1}
        assert all(r[5:] == (None,) * 7 for r in rows), "no value is generated for any row"
        with sqlite_engine.begin() as conn:
            counters = [r[0] for r in conn.execute(sa.text(
                "SELECT capture_counter FROM collections ORDER BY id"))]
        assert counters == [0] * 5, "a volume with no images counts no captures"
    else:
        assert rows == []

    # The constraints are live after the upgrade.
    with pytest.raises(IntegrityError), sqlite_engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO collections (name, project_id, filename_stem, created_at) "
            "VALUES ('bad', NULL, 'ok', CURRENT_TIMESTAMP)"))
    with pytest.raises(IntegrityError), sqlite_engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO projects (id, name) VALUES (9, 'x') ON CONFLICT DO NOTHING"))
        conn.execute(sa.text(
            "INSERT INTO collections (name, project_id, filename_stem, created_at) "
            "VALUES ('bad', 9, 'Bad_Stem', CURRENT_TIMESTAMP)"))
    with pytest.raises(IntegrityError), sqlite_engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO collections (name, project_id, date_start, date_end, created_at) "
            "VALUES ('bad', 9, '1750-02-01', '1750-01-31', CURRENT_TIMESTAMP)"))

    with pytest.raises(IntegrityError), sqlite_engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO collections (name, project_id, external_id, created_at) "
            "VALUES ('bad', 9, ' padded', CURRENT_TIMESTAMP)"))

    _run(sqlite_engine, mod.downgrade)

    insp = sa.inspect(sqlite_engine)
    assert {c["name"] for c in insp.get_columns("collections")} == before
    assert {c["name"] for c in insp.get_check_constraints("collections")} == {"check_collection_parent"}
    with sqlite_engine.begin() as conn:
        names = [r[0] for r in conn.execute(sa.text("SELECT name FROM collections ORDER BY id"))]
    assert names == (["Tomo 1", "Tomo 2", "Tomo 3", "Tomo 4", "Legajo"] if populated else [])


# ---------------------------------------------------------------------------
# The capture counter
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_migration_backfills_the_capture_counter_from_capture_events(backend_root, sqlite_engine):
    """One capture event is one capture_id; an image with none is an event of its own.

    Volume 1: a dual spread (two images, one capture_id), a single capture,
    and a superseded single whose retake is current: four events from five
    images. Volume 2: two uploads with no capture_id and one with an empty
    one: three events. Volume 3: a record with no images: zero. Volume 4,
    nested in volume 1: its image counts for volume 4, not for volume 1.
    """
    mod = _load_migration(backend_root)
    _pre_migration_schema(sqlite_engine)
    with sqlite_engine.begin() as conn:
        def x(q, **kw):
            conn.execute(sa.text(q), kw)
        x("INSERT INTO projects (id, name) VALUES (1, 'P')")
        for i in (1, 2, 3):
            x("INSERT INTO collections (id, name, project_id, created_at) "
              "VALUES (:i, :n, 1, CURRENT_TIMESTAMP)", i=i, n=f"V{i}")
        x("INSERT INTO collections (id, name, parent_collection_id, created_at) "
          "VALUES (4, 'child', 1, CURRENT_TIMESTAMP)")
        for rid, cid in ((10, 1), (11, 1), (12, 1), (20, 2), (30, 3), (40, 4)):
            x("INSERT INTO records (id, collection_id) VALUES (:r, :c)", r=rid, c=cid)
        imgs = [
            (10, "cap-dual", "0001", True), (10, "cap-dual", "0001", True),     # spread
            (11, "cap-single", None, True),                                      # single
            (12, "cap-rejected", None, False), (12, "cap-retake", None, True),   # retake
            (20, None, None, True), (20, None, None, True), (20, "", None, True),  # uploads
            (40, "cap-child", None, True),
        ]
        for rid, cap, pair, cur in imgs:
            x("INSERT INTO record_images (record_id, capture_id, pair_id, is_current) "
              "VALUES (:r, :c, :p, :cur)", r=rid, c=cap, p=pair, cur=cur)

    _run(sqlite_engine, mod.upgrade)

    with sqlite_engine.begin() as conn:
        got = dict(conn.execute(sa.text("SELECT id, capture_counter FROM collections")).all())
    assert got == {1: 4, 2: 3, 3: 0, 4: 1}, got


@pytest.mark.unit
def test_capture_counter_is_not_null_default_zero_and_checked():
    from app.models.collection import Collection

    col = Collection.__table__.columns["capture_counter"]
    assert col.nullable is False
    assert isinstance(col.type, sa.Integer)
    assert col.server_default is not None and str(col.server_default.arg) == "0"
    assert "check_collection_capture_counter" in {
        c.name for c in Collection.__table__.constraints if isinstance(c, sa.CheckConstraint)}


@pytest.mark.unit
def test_a_new_volume_starts_at_zero(db_session):
    p = _project(db_session)
    v = _volume(db_session, p)
    db_session.refresh(v)
    assert v.capture_counter == 0


@pytest.mark.unit
def test_the_database_refuses_a_negative_counter(db_session):
    p = _project(db_session)
    v = _volume(db_session, p)
    with pytest.raises(IntegrityError):
        db_session.execute(sa.text("UPDATE collections SET capture_counter = -1 WHERE id = :i"),
                           {"i": v.id})
    db_session.rollback()


@pytest.mark.unit
def test_the_counter_can_be_raised(db_session):
    p = _project(db_session)
    v = _volume(db_session, p)
    v.capture_counter = 1
    db_session.commit()
    v.capture_counter = 5
    db_session.commit()
    v.capture_counter = 5
    db_session.commit()
    db_session.refresh(v)
    assert v.capture_counter == 5


@pytest.mark.unit
@pytest.mark.parametrize("op", ["lower", "zero", "clear", "delete"])
def test_the_counter_never_decreases(db_session, op):
    from app.models.record import ImmutableFieldError

    p = _project(db_session)
    v = _volume(db_session, p, capture_counter=3)
    with pytest.raises(ImmutableFieldError):
        if op == "lower":
            v.capture_counter = 2
        elif op == "zero":
            v.capture_counter = 0
        elif op == "clear":
            v.capture_counter = None
        else:
            del v.capture_counter
    db_session.rollback()
    db_session.refresh(v)
    assert v.capture_counter == 3


@pytest.mark.unit
def test_the_counter_guard_holds_in_a_fresh_identity_map(db_session):
    from app.models.collection import Collection
    from app.models.record import ImmutableFieldError

    p = _project(db_session)
    v = _volume(db_session, p, capture_counter=2)
    vid = v.id
    db_session.expunge_all()
    reloaded = db_session.query(Collection).filter(Collection.id == vid).one()
    with pytest.raises(ImmutableFieldError):
        reloaded.capture_counter = 1


@pytest.mark.unit
def test_no_request_schema_accepts_the_counter():
    """The capture path raises it; no client writes it."""
    from app.schemas.collection import CollectionCreate, CollectionUpdate

    assert "capture_counter" not in CollectionCreate.model_fields
    assert "capture_counter" not in CollectionUpdate.model_fields


@pytest.mark.unit
def test_the_read_schema_returns_the_counter(client, db_session):
    p = _project(db_session)
    v = _volume(db_session, p, capture_counter=7)
    api = _client_as(client)

    body = api.get(f"/collections/{v.id}").json()

    assert body["capture_counter"] == 7, body


def _volume_that_has_captured(db_session):
    """A volume with a stem, one image, and the count the capture path will keep."""
    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="co-x-040", capture_counter=1)
    _image_in(db_session, v)
    return p, v


@pytest.mark.unit
def test_the_stem_stays_fixed_after_every_record_in_the_volume_is_deleted(db_session):
    from app.models.record import ImmutableFieldError, Record

    p, v = _volume_that_has_captured(db_session)
    for rec in db_session.query(Record).filter(Record.collection_id == v.id).all():
        db_session.delete(rec)
    db_session.commit()
    assert not v.has_images()

    with pytest.raises(ImmutableFieldError):
        v.filename_stem = "co-x-041"
    with pytest.raises(ImmutableFieldError):
        v.filename_stem = None


@pytest.mark.unit
def test_the_stem_stays_fixed_after_every_record_is_moved_out(db_session):
    from app.models.record import ImmutableFieldError, Record

    p, v = _volume_that_has_captured(db_session)
    other = _volume(db_session, p, "other")
    for rec in db_session.query(Record).filter(Record.collection_id == v.id).all():
        rec.collection_id = other.id
    db_session.commit()
    assert not v.has_images()

    with pytest.raises(ImmutableFieldError):
        v.filename_stem = "co-x-042"


@pytest.mark.unit
def test_a_counter_above_zero_fixes_a_set_stem_without_any_image(db_session):
    from app.models.record import ImmutableFieldError

    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="co-x-043", capture_counter=2)
    with pytest.raises(ImmutableFieldError):
        v.filename_stem = "co-x-044"


@pytest.mark.unit
def test_a_counted_volume_with_no_stem_still_takes_its_first(db_session):
    from app.models.record import ImmutableFieldError

    p = _project(db_session)
    v = _volume(db_session, p, capture_counter=4)
    v.filename_stem = "co-x-045"
    db_session.commit()
    with pytest.raises(ImmutableFieldError):
        v.filename_stem = "co-x-046"


# ---------------------------------------------------------------------------
# Concurrent first writes: the PATCH takes the row lock
# ---------------------------------------------------------------------------
#
# Two PATCHes that both read a NULL stem or external_id would each pass the
# write-once guard, and the later commit would overwrite the earlier. The
# PATCH loads the volume under SELECT ... FOR UPDATE, so on Postgres the
# second waits for the first to commit and its guard then compares against
# the committed value. SQLite emits no FOR UPDATE, so, as in
# test_workflow_row_locking.py, what this suite can check is the SQL that
# would reach Postgres, and that the PATCH uses the locked loader.

@pytest.mark.unit
def test_the_patch_loader_locks_the_collection_row(db_session):
    from sqlalchemy.dialects import postgresql
    from app.api.collections import _locked_collection_query

    sql = str(_locked_collection_query(db_session, 1).statement.compile(
        dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE" in sql, sql


@pytest.mark.unit
def test_update_collection_loads_through_the_locked_loader():
    import inspect
    from app.api import collections as collections_api

    src = inspect.getsource(collections_api.update_collection)
    assert "db.query(Collection).filter(Collection.id == collection_id).first()" not in src
    assert "_locked_collection_query(db, collection_id)" in src


def _two_sessions_race(db_session, field, first, second):
    """Two sessions both load the row, both write a first value, both commit."""
    from sqlalchemy.orm import Session
    from app.models.collection import Collection

    p = _project(db_session)
    v = _volume(db_session, p)
    _image_in(db_session, v)
    vid = v.id
    engine = db_session.get_bind()
    s1, s2 = Session(bind=engine), Session(bind=engine)
    try:
        a, b = s1.get(Collection, vid), s2.get(Collection, vid)
        setattr(a, field, first)
        s1.commit()
        setattr(b, field, second)
        s2.commit()
    finally:
        s1.close()
        s2.close()
    db_session.expire_all()
    return getattr(db_session.get(Collection, vid), field)


_SQLITE_CANNOT_LOCK = (
    "The verifier's two-session race, at the ORM level. Both sessions load "
    "the row before either commits, which the PATCH's FOR UPDATE prevents on "
    "Postgres; SQLite emits no lock, and a writer that does not go through "
    "the PATCH's locked loader is not covered by it."
)


@pytest.mark.unit
@pytest.mark.xfail(strict=True, reason=_SQLITE_CANNOT_LOCK)
def test_two_concurrent_first_stems_do_not_overwrite(db_session):
    assert _two_sessions_race(db_session, "filename_stem", "co-e-001", "co-e-002") == "co-e-001"


@pytest.mark.unit
@pytest.mark.xfail(strict=True, reason=_SQLITE_CANNOT_LOCK)
def test_two_concurrent_first_external_ids_do_not_overwrite(db_session):
    assert _two_sessions_race(db_session, "external_id", "co-ref-a", "co-ref-b") == "co-ref-a"


# ---------------------------------------------------------------------------
# An image pending in the session counts
# ---------------------------------------------------------------------------

def _pending_image(db_session, volume, *, record_pending=False, by_relationship=False):
    """Add a record and image to the session without flushing the image."""
    from app.models.record import Record, RecordImage

    rec = Record(title="r", status="captured", capture_mode="single",
                 **({"collection": volume} if by_relationship else {"collection_id": volume.id}))
    db_session.add(rec)
    if not record_pending:
        db_session.flush()
    img = RecordImage(filename="x.jpg", file_path="/tmp/x.jpg", format="jpeg", is_current=True,
                      **({"record": rec} if record_pending else {"record_id": rec.id}))
    db_session.add(img)
    return img


@pytest.mark.unit
@pytest.mark.parametrize("record_pending,by_relationship", [
    (False, False), (True, False), (True, True),
], ids=["flushed-record", "pending-record", "pending-record-by-relationship"])
def test_a_stem_change_in_the_same_flush_as_the_first_image_is_refused(
        db_session, record_pending, by_relationship):
    from app.models.record import ImmutableFieldError

    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="co-b-001")
    _pending_image(db_session, v, record_pending=record_pending, by_relationship=by_relationship)

    with pytest.raises(ImmutableFieldError):
        v.filename_stem = "co-b-002"


@pytest.mark.unit
def test_a_pending_image_in_another_volume_does_not_fix_this_stem(db_session):
    p = _project(db_session)
    other = _volume(db_session, p, "other")
    v = _volume(db_session, p, "v", filename_stem="co-b-003")
    _pending_image(db_session, other)

    v.filename_stem = "co-b-004"


# ---------------------------------------------------------------------------
# external_id is stripped, and never blank or padded in the database
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_a_blank_external_id_is_a_422(client, db_session, value):
    p = _project(db_session)
    api = _client_as(client)

    resp = api.post("/collections/", json={"name": "A", "project_id": p.id, "external_id": value})

    assert resp.status_code == 422, resp.text


@pytest.mark.unit
def test_the_schemas_strip_external_id():
    from app.schemas.collection import CollectionCreate, CollectionUpdate

    assert CollectionCreate(name="v", project_id=1, external_id="  co-ahr-1 ").external_id == "co-ahr-1"
    assert CollectionUpdate(external_id="co-ahr-1\n").external_id == "co-ahr-1"


@pytest.mark.unit
def test_a_padded_duplicate_external_id_is_a_409(client, db_session):
    p = _project(db_session)
    api = _client_as(client)

    first = api.post("/collections/", json={"name": "A", "project_id": p.id, "external_id": "co-ahr-1"})
    padded = api.post("/collections/", json={"name": "B", "project_id": p.id, "external_id": "co-ahr-1 "})

    assert first.status_code == 201, first.text
    assert padded.status_code == 409, padded.text
    assert padded.json()["detail"]["constraint"] == "ix_collections_external_id"


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", " co-ahr-2", "co-ahr-2 ", "\tco-ahr-2", "co-ahr-2\n"])
def test_the_database_refuses_a_blank_or_padded_external_id(db_session, value):
    """An ORM writer cannot store '' and so lock the real code out for good."""
    from app.models.collection import Collection

    p = _project(db_session)
    db_session.add(Collection(name="v", project_id=p.id, external_id=value))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
def test_a_blank_external_id_on_the_orm_is_a_named_409_through_the_handler(db_session):
    from app.core.db_errors import integrity_conflict
    from app.models.collection import Collection

    p = _project(db_session)
    db_session.add(Collection(name="v", project_id=p.id, external_id=""))
    with pytest.raises(IntegrityError) as caught:
        db_session.commit()
    db_session.rollback()
    exc = integrity_conflict(caught.value)
    assert exc.status_code == 409
    assert exc.detail["constraint"] == "check_collection_external_id_trimmed", exc.detail


# ---------------------------------------------------------------------------
# The stem is optional on create for now
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_volume_can_be_created_without_a_stem_for_now(client, db_session):
    """Contract 1.1 requires one; it becomes required with its sources.

    Those are the stem screen and the list import. Until they exist an
    operator has no way to supply one, so create accepts its absence.
    """
    p = _project(db_session)
    api = _client_as(client)

    resp = api.post("/collections/", json={"name": "no stem", "project_id": p.id})

    assert resp.status_code == 201, resp.text
    assert resp.json()["filename_stem"] is None
