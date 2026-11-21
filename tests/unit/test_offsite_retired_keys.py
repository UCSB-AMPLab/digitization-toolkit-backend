"""Retired offsite keys (NEH-387): the rows an adoption detaches from their images, and who reads them.

An adoption renames a volume's files and uploads each again under a new key.
The unit's bucket key cannot read, copy or delete, so the old object stays
in the bucket: its offsite_objects row is detached from the image
(record_image_id NULL), marked retired (retired_at, retired_image_id), and
kept, so its key stays recorded and reserved until a person deletes the
object from the list retired_keys gives.

The migration is exercised on SQLite always, and on PostgreSQL when
DATABASE_* reach a server, as in tests/unit/test_offsite_objects_migration.py.
The readers are exercised with real rows and real files under the test
projects root; the upload pass runs a real rclone against the local
directory remote of tests/unit/test_image_upload.py.

    python -m pytest tests/unit/test_offsite_retired_keys.py

Version: 0.3.3
"""

import hashlib
import importlib.util
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from tests.unit.test_image_upload import STEM, TENANT, _image, _rows, _run, _volume, remote  # noqa: F401

MIGRATION_FILE = "c8e0a2f4b6d9_add_offsite_object_retirement.py"
TABLE_MIGRATION_FILE = "b8d2f4a6c1e3_add_offsite_objects.py"
PREVIOUS_HEAD = "c8e2a4f6b0d1"
NEW_STEM = "xx-test-002"


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------

def _load(backend_root, name, module):
    path = backend_root / "alembic" / "versions" / name
    spec = importlib.util.spec_from_file_location(module, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_op(engine, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def _table_before(engine, backend_root):
    """record_images with two rows, and offsite_objects as the previous head leaves it, with one verified row."""
    md = sa.MetaData()
    sa.Table("record_images", md, sa.Column("id", sa.Integer, primary_key=True))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO record_images (id) VALUES (1), (2)"))
    _run_op(engine, _load(backend_root, TABLE_MIGRATION_FILE, "offsite_objects_table").upgrade)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO offsite_objects (record_image_id, kind, object_key, local_sha1, verified_at, created_at) "
            "VALUES (1, 'capture', 't/s/captures/a', :s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"), {"s": "0" * 40})


_INSERT = sa.text(
    "INSERT INTO offsite_objects (record_image_id, kind, object_key, local_sha1, verified_at, refused_at, refusal, "
    "retired_at, retired_image_id, created_at) "
    "VALUES (:i, :k, :key, :s, :v, :r, :why, :ret, :reti, CURRENT_TIMESTAMP)")


def _exercise(engine, backend_root):
    _table_before(engine, backend_root)
    mod = _load(backend_root, MIGRATION_FILE, "offsite_retirement")
    _run_op(engine, mod.upgrade)

    insp = sa.inspect(engine)
    columns = {c["name"]: c for c in insp.get_columns("offsite_objects")}
    assert {"retired_at", "retired_image_id"} <= set(columns)
    assert columns["retired_at"]["nullable"] and columns["retired_image_id"]["nullable"]
    checks = {c["name"] for c in insp.get_check_constraints("offsite_objects")}
    assert {"check_offsite_object_kind", "check_offsite_object_state", "check_offsite_object_refusal",
            "check_offsite_object_retired"} <= checks
    assert ("record_images", ("retired_image_id",), "SET NULL") in {
        (f["referred_table"], tuple(f["constrained_columns"]), f["options"].get("ondelete"))
        for f in insp.get_foreign_keys("offsite_objects")}
    assert "ix_offsite_objects_retired_image_id" in {i["name"] for i in insp.get_indexes("offsite_objects")}
    uniques = {u["name"] for u in insp.get_unique_constraints("offsite_objects")}
    assert {"offsite_objects_object_key_key", "offsite_objects_image_kind_key"} <= uniques
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT record_image_id, retired_at, retired_image_id FROM offsite_objects")
                            ).all() == [(1, None, None)]

    v = "2026-11-19 00:00:00"
    with engine.begin() as conn:
        # Retired: detached, verified, and the image it came from.
        conn.execute(_INSERT, dict(i=None, k="capture", key="t/s/captures/b", s="0" * 40, v=v, r=None, why=None,
                                   ret=v, reti=2))
        # Detached and not retired, as a deleted image leaves its row.
        conn.execute(_INSERT, dict(i=None, k="capture", key="t/s/captures/c", s="0" * 40, v=None, r=None, why=None,
                                   ret=None, reti=None))
    for bad in (
        dict(i=2, k="capture", key="t/s/captures/d", s="0" * 40, v=v, r=None, why=None, ret=v, reti=2),   # attached
        dict(i=None, k="capture", key="t/s/captures/e", s="0" * 40, v=None, r=None, why=None, ret=v, reti=2),  # pending
        dict(i=None, k="capture", key="t/s/captures/f", s="0" * 40, v=None, r=v, why="fixity", ret=v, reti=2),  # refused
    ):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(_INSERT, bad)

    # The image a retired row came from is deleted: the row stays, retired, with no image.
    with engine.connect() as conn:
        if engine.dialect.name == "sqlite":
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.execute(sa.text("DELETE FROM record_images WHERE id = 2"))
        conn.commit()
        if engine.dialect.name == "sqlite":
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        assert conn.execute(sa.text(
            "SELECT record_image_id, retired_image_id, retired_at IS NOT NULL FROM offsite_objects "
            "WHERE object_key = 't/s/captures/b'")).one() == (None, None, True)

    _run_op(engine, mod.downgrade)
    insp = sa.inspect(engine)
    assert not {"retired_at", "retired_image_id"} & {c["name"] for c in insp.get_columns("offsite_objects")}
    checks = {c["name"] for c in insp.get_check_constraints("offsite_objects")}
    assert {"check_offsite_object_kind", "check_offsite_object_state", "check_offsite_object_refusal"} <= checks
    assert "check_offsite_object_retired" not in checks
    with engine.connect() as conn:
        assert sorted(conn.execute(sa.text("SELECT object_key FROM offsite_objects")).scalars()) == [
            "t/s/captures/a", "t/s/captures/b", "t/s/captures/c"]
    # And upgrades again.
    _run_op(engine, mod.upgrade)
    assert "retired_at" in {c["name"] for c in sa.inspect(engine).get_columns("offsite_objects")}


@pytest.mark.unit
def test_the_migration_chains_onto_the_single_previous_head(backend_root):
    mod = _load(backend_root, MIGRATION_FILE, "offsite_retirement")
    assert mod.down_revision == PREVIOUS_HEAD
    siblings = [p.name for p in (backend_root / "alembic" / "versions").glob("*.py")
                if p.name != MIGRATION_FILE and "down_revision" in p.read_text()
                and f"'{PREVIOUS_HEAD}'" in p.read_text().split("down_revision", 1)[1].splitlines()[0]]
    assert siblings == [], siblings


@pytest.mark.unit
def test_the_migration_is_the_only_head(backend_root):
    import re

    revisions, downs = set(), set()
    for path in (backend_root / "alembic" / "versions").glob("*.py"):
        text = path.read_text()
        revisions.add(re.search(r"^revision(?:: str)? = '(\w+)'", text, re.M).group(1))
        downs.update(re.findall(r"'(\w+)'", re.search(r"^down_revision[^=]*= (.*)$", text, re.M).group(1)))
    assert revisions - downs == {_load(backend_root, MIGRATION_FILE, "offsite_retirement").revision}


@pytest.mark.unit
def test_the_migration_upgrades_and_downgrades_on_sqlite(backend_root, tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    try:
        _exercise(engine, backend_root)
    finally:
        engine.dispose()


@pytest.mark.unit
def test_the_model_matches_the_migration(backend_root, tmp_path):
    """The ORM table and the migrated one agree on columns, constraints, indexes and foreign keys."""
    from app.core.db import Base
    import app.models.offsite_object  # noqa: F401
    import app.models.record  # noqa: F401

    a = sa.create_engine(f"sqlite:///{tmp_path / 'orm.db'}")
    b = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    try:
        md = sa.MetaData()
        sa.Table("record_images", md, sa.Column("id", sa.Integer, primary_key=True))
        md.create_all(a)
        Base.metadata.tables["offsite_objects"].to_metadata(md).create(a)
        _table_before(b, backend_root)
        _run_op(b, _load(backend_root, MIGRATION_FILE, "offsite_retirement").upgrade)

        def shape(engine):
            insp = sa.inspect(engine)
            return (
                sorted((c["name"], str(c["type"]), c["nullable"]) for c in insp.get_columns("offsite_objects")),
                sorted(u["name"] for u in insp.get_unique_constraints("offsite_objects")),
                sorted(c["name"] for c in insp.get_check_constraints("offsite_objects")),
                sorted(i["name"] for i in insp.get_indexes("offsite_objects")),
                sorted((f["referred_table"], tuple(f["constrained_columns"]), f["options"].get("ondelete"))
                       for f in insp.get_foreign_keys("offsite_objects")),
            )
        assert shape(a) == shape(b)
    finally:
        a.dispose()
        b.dispose()


@pytest.mark.unit
def test_the_migration_upgrades_and_downgrades_on_postgresql(backend_root):
    if "DATABASE_HOST" not in os.environ:
        pytest.skip("opt-in: set DATABASE_* to a PostgreSQL server")
    from sqlalchemy.exc import OperationalError

    from app.core.db import DATABASE_URL

    schema = f"captua_migtest_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(DATABASE_URL)
    try:
        with admin.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    except OperationalError as exc:
        admin.dispose()
        pytest.skip(f"DATABASE_* does not reach a PostgreSQL server: {exc.orig}")
    engine = sa.create_engine(DATABASE_URL, connect_args={"options": f"-csearch_path={schema}"})
    try:
        _exercise(engine, backend_root)
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.unit
def test_the_model_refuses_a_retired_row_that_is_attached_or_unverified(db_session, override_projects_root):
    from app.models.offsite_object import OffsiteObject

    vol = _volume(db_session)
    img = _image(db_session, vol, override_projects_root, "a.jpg", b"a")
    for fields in (
        dict(record_image_id=img.id, verified_at=_now()),
        dict(record_image_id=None, verified_at=None),
    ):
        db_session.add(OffsiteObject(kind="capture", object_key=f"k/{uuid.uuid4().hex}", local_sha1="0" * 40,
                                     retired_at=_now(), retired_image_id=img.id, **fields))
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()


# ---------------------------------------------------------------------------
# retire_rows
# ---------------------------------------------------------------------------

def _row(db, image, kind, key, *, sha1="1" * 40, state="verified"):
    """An offsite_objects row as the uploader leaves it."""
    from app.models.offsite_object import OffsiteObject

    fields = {"verified": dict(verified_at=_now()),
              "pending": {},
              "uploaded": dict(uploaded_at=_now()),
              "refused": dict(refused_at=_now(), refusal="remote_differs", remote_sha1="2" * 40)}[state]
    row = OffsiteObject(record_image_id=image.id if image is not None else None, kind=kind,
                        object_key=key, local_sha1=sha1, **fields)
    db.add(row)
    db.commit()
    return row


def _key(name, stem=STEM, kind="captures"):
    return f"{TENANT}/{stem}/{kind}/{name}"


def _all(db):
    from app.models.offsite_object import OffsiteObject

    db.expire_all()
    return {r.object_key: r for r in db.query(OffsiteObject).all()}


@pytest.mark.unit
def test_retiring_detaches_each_verified_row_of_the_images_and_keeps_its_key(db_session, override_projects_root):
    from app.core.offsite_retired import retire_rows

    vol = _volume(db_session)
    a = _image(db_session, vol, override_projects_root, "a.CR2", b"raw a", preview=b"prev a")
    b = _image(db_session, vol, override_projects_root, "b.jpg", b"jpeg b")
    other = _image(db_session, vol, override_projects_root, "c.jpg", b"jpeg c")
    _row(db_session, a, "capture", _key("a.CR2"), sha1=_sha1(b"raw a"))
    _row(db_session, a, "preview", _key("a_preview.jpg", kind="previews"), sha1=_sha1(b"prev a"))
    _row(db_session, b, "capture", _key("b.jpg"), sha1=_sha1(b"jpeg b"))
    _row(db_session, other, "capture", _key("c.jpg"))
    at = datetime(2026, 11, 19, 12, 0, 0)

    retired = retire_rows(db_session, [a.id, b.id], now=at)
    db_session.commit()

    assert sorted((r.retired_image_id, r.kind) for r in retired) == [
        (a.id, "capture"), (a.id, "preview"), (b.id, "capture")]
    rows = _all(db_session)
    for key, image_id in ((_key("a.CR2"), a.id), (_key("a_preview.jpg", kind="previews"), a.id),
                          (_key("b.jpg"), b.id)):
        row = rows[key]
        assert row.record_image_id is None
        assert row.retired_image_id == image_id
        assert row.retired_at.replace(tzinfo=None) == at
        assert row.verified_at is not None and row.object_key == key
    assert rows[_key("a.CR2")].local_sha1 == _sha1(b"raw a")
    assert rows[_key("c.jpg")].record_image_id == other.id and rows[_key("c.jpg")].retired_at is None


@pytest.mark.unit
@pytest.mark.parametrize("state", ["pending", "uploaded", "refused"])
def test_retiring_refuses_and_changes_nothing_when_a_row_is_not_verified(db_session, override_projects_root, state):
    from app.core.offsite_retired import RetireRefused, retire_rows

    vol = _volume(db_session)
    a = _image(db_session, vol, override_projects_root, "a.CR2", b"raw a", preview=b"prev a")
    b = _image(db_session, vol, override_projects_root, "b.jpg", b"jpeg b")
    _row(db_session, a, "capture", _key("a.CR2"))
    _row(db_session, b, "capture", _key("b.jpg"))
    _row(db_session, a, "preview", _key("a_preview.jpg", kind="previews"), state=state)

    with pytest.raises(RetireRefused) as caught:
        retire_rows(db_session, [a.id, b.id], now=_now())
    assert [(u["image_id"], u["kind"], u["state"]) for u in caught.value.unverified] == [(a.id, "preview", state)]
    db_session.commit()

    rows = _all(db_session)
    assert {k: (r.record_image_id, r.retired_at) for k, r in rows.items()} == {
        _key("a.CR2"): (a.id, None), _key("b.jpg"): (b.id, None),
        _key("a_preview.jpg", kind="previews"): (a.id, None)}


@pytest.mark.unit
def test_retiring_does_not_commit(db_session, override_projects_root):
    from app.core.offsite_retired import retire_rows

    vol = _volume(db_session)
    a = _image(db_session, vol, override_projects_root, "a.jpg", b"jpeg a")
    _row(db_session, a, "capture", _key("a.jpg"))

    retire_rows(db_session, [a.id], now=_now())
    db_session.rollback()

    row = _all(db_session)[_key("a.jpg")]
    assert row.record_image_id == a.id and row.retired_at is None


@pytest.mark.unit
def test_after_retiring_a_new_row_of_the_same_kind_can_be_attached(db_session, override_projects_root):
    from app.core.offsite_retired import retire_rows

    vol = _volume(db_session)
    a = _image(db_session, vol, override_projects_root, "a.jpg", b"jpeg a")
    _row(db_session, a, "capture", _key("a.jpg"))
    retire_rows(db_session, [a.id], now=_now())
    _row(db_session, a, "capture", _key("a.jpg", stem=NEW_STEM), state="pending")

    rows = _all(db_session)
    assert rows[_key("a.jpg", stem=NEW_STEM)].record_image_id == a.id
    assert rows[_key("a.jpg")].record_image_id is None


@pytest.mark.unit
def test_a_retired_rows_key_is_still_frozen(db_session, override_projects_root):
    from app.core.offsite_retired import retire_rows
    from app.models.record import ImmutableFieldError

    vol = _volume(db_session)
    a = _image(db_session, vol, override_projects_root, "a.jpg", b"jpeg a")
    _row(db_session, a, "capture", _key("a.jpg"))
    [row] = retire_rows(db_session, [a.id], now=_now())
    db_session.commit()

    row = _all(db_session)[_key("a.jpg")]
    row.object_key = _key("other.jpg")
    with pytest.raises(ImmutableFieldError):
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------------
# retired_keys
# ---------------------------------------------------------------------------

def _retired_fixture(db, root):
    """Image a (RAW, capture and preview) and image b (JPEG), each verified, then retired."""
    from app.core.offsite_retired import retire_rows

    vol = _volume(db)
    a = _image(db, vol, root, "a.CR2", b"raw a", preview=b"prev a")
    b = _image(db, vol, root, "b.jpg", b"jpeg b")
    _row(db, a, "capture", _key("a.CR2"), sha1=_sha1(b"raw a"))
    _row(db, a, "preview", _key("a_preview.jpg", kind="previews"), sha1=_sha1(b"prev a"))
    _row(db, b, "capture", _key("b.jpg"), sha1=_sha1(b"jpeg b"))
    retire_rows(db, [a.id, b.id], now=_now())
    db.commit()
    return vol, a, b


def _listed(db):
    from app.core.offsite_retired import retired_keys

    db.expire_all()
    return {e["key"]: e for e in retired_keys(db)}


@pytest.mark.unit
def test_retired_keys_lists_each_retired_row_with_what_is_stored(db_session, override_projects_root):
    vol, a, b = _retired_fixture(db_session, override_projects_root)
    _row(db_session, None, "capture", _key("gone.jpg"))   # a deleted image's row: not retired, not listed

    listed = _listed(db_session)

    assert set(listed) == {_key("a.CR2"), _key("a_preview.jpg", kind="previews"), _key("b.jpg")}
    entry = listed[_key("a.CR2")]
    assert entry["kind"] == "capture" and entry["image_id"] == a.id
    assert entry["local_sha1"] == _sha1(b"raw a")
    assert entry["retired_at"] is not None and entry["verified_at"] is not None
    assert entry["bytes"] is None   # offsite_objects stores no size
    assert listed[_key("a_preview.jpg", kind="previews")]["kind"] == "preview"


@pytest.mark.unit
def test_a_retired_key_is_not_deletable_until_the_new_row_of_its_kind_verifies_with_the_same_sha1(
        db_session, override_projects_root):
    vol, a, b = _retired_fixture(db_session, override_projects_root)

    # No new row yet.
    assert not any(e["deletable"] for e in _listed(db_session).values())

    # A new capture row, pending: not yet.
    new_a = _row(db_session, a, "capture", _key("a.CR2", stem=NEW_STEM), sha1=_sha1(b"raw a"), state="pending")
    assert _listed(db_session)[_key("a.CR2")]["deletable"] is False
    new_a.uploaded_at = _now()
    db_session.commit()
    assert _listed(db_session)[_key("a.CR2")]["deletable"] is False

    # Verified with the same SHA1: the capture may be deleted, the preview not.
    new_a.verified_at = _now()
    db_session.commit()
    listed = _listed(db_session)
    assert listed[_key("a.CR2")]["deletable"] is True
    assert listed[_key("a.CR2")]["current_key"] == _key("a.CR2", stem=NEW_STEM)
    assert listed[_key("a_preview.jpg", kind="previews")]["deletable"] is False

    # Verified with another SHA1: never.
    _row(db_session, b, "capture", _key("b.jpg", stem=NEW_STEM), sha1="3" * 40)
    assert _listed(db_session)[_key("b.jpg")]["deletable"] is False


@pytest.mark.unit
def test_a_retired_key_is_not_deletable_when_the_new_row_is_refused(db_session, override_projects_root):
    vol, a, b = _retired_fixture(db_session, override_projects_root)
    _row(db_session, b, "capture", _key("b.jpg", stem=NEW_STEM), sha1=_sha1(b"jpeg b"), state="refused")

    assert _listed(db_session)[_key("b.jpg")]["deletable"] is False


@pytest.mark.unit
def test_a_retired_key_whose_image_was_deleted_is_listed_and_not_deletable(db_session, override_projects_root):
    from app.models.offsite_object import OffsiteObject
    from app.models.record import RecordImage

    vol, a, b = _retired_fixture(db_session, override_projects_root)
    _row(db_session, b, "capture", _key("b.jpg", stem=NEW_STEM), sha1=_sha1(b"jpeg b"))
    db_session.delete(db_session.get(RecordImage, b.id))
    db_session.commit()
    # PostgreSQL's ON DELETE SET NULL on both columns; the test database is
    # SQLite without foreign keys, so the updates do it here (the migration
    # test checks the action itself).
    db_session.query(OffsiteObject).filter(OffsiteObject.record_image_id == b.id).update(
        {OffsiteObject.record_image_id: None})
    db_session.query(OffsiteObject).filter(OffsiteObject.retired_image_id == b.id).update(
        {OffsiteObject.retired_image_id: None})
    db_session.commit()

    entry = _listed(db_session)[_key("b.jpg")]
    assert entry["image_id"] is None
    assert entry["current_key"] is None
    assert entry["deletable"] is False
    assert _all(db_session)[_key("b.jpg")].retired_at is not None


# ---------------------------------------------------------------------------
# The readers
# ---------------------------------------------------------------------------

def _adopt_on_disk(db, root, image, vol, new_name):
    """What an adoption's apply step does to one image, short of its rows: rename and move into `vol`."""
    from app.models.record import Record

    old = Path(image.file_path)
    new = root / vol.filename_stem / "captures" / new_name
    new.parent.mkdir(parents=True, exist_ok=True)
    old.rename(new)
    image.file_path = str(new)
    image.filename = new_name
    db.get(Record, image.record_id).collection_id = vol.id
    db.commit()


@pytest.mark.unit
def test_an_image_whose_rows_were_retired_is_pending_again_under_its_current_name_and_stem(
        remote, db_session, override_projects_root):
    from app.core.image_upload import pending_work
    from app.core.offsite_retired import retire_rows

    old = _volume(db_session)
    img = _image(db_session, old, override_projects_root, "a.jpg", b"jpeg a")
    assert _run(db_session)["uploaded"] == 1
    assert pending_work(db_session) == []

    retire_rows(db_session, [img.id], now=_now())
    db_session.commit()
    new = _volume(db_session, name="Tomo 2", stem=NEW_STEM)
    _adopt_on_disk(db_session, override_projects_root, img, new, f"{NEW_STEM}-img_0001_c1.jpg")

    assert [(w.image_id, w.kind, w.stem) for w in pending_work(db_session)] == [(img.id, "capture", NEW_STEM)]
    result = _run(db_session)
    assert result["status"] == "done" and result["uploaded"] == 1, result

    rows = _all(db_session)
    new_key = _key(f"{NEW_STEM}-img_0001_c1.jpg", stem=NEW_STEM)
    assert rows[new_key].record_image_id == img.id and rows[new_key].verified_at is not None
    assert rows[_key("a.jpg")].record_image_id is None and rows[_key("a.jpg")].retired_at is not None
    files = remote.files()
    assert files[new_key] == b"jpeg a"
    assert files[_key("a.jpg")] == b"jpeg a", "the old object stays in the bucket"
    assert _listed(db_session)[_key("a.jpg")]["deletable"] is True


@pytest.mark.unit
def test_a_volume_is_not_received_until_the_new_rows_verify(remote, db_session, override_projects_root):
    from app.core.image_upload import volume_upload_state
    from app.core.offsite_retired import retire_rows

    vol = _volume(db_session)
    img = _image(db_session, vol, override_projects_root, "a.CR2", b"raw a", preview=b"prev a")
    _run(db_session)
    assert volume_upload_state(db_session, vol)["received"] is True

    retire_rows(db_session, [img.id], now=_now())
    db_session.commit()
    state = volume_upload_state(db_session, vol)
    assert (state["total"], state["verified"], state["pending"], state["received"]) == (2, 0, 2, False)

    old = _volume(db_session, name="Tomo 0", stem="xx-test-000")
    assert volume_upload_state(db_session, old)["total"] == 0, "retired rows count for no volume"


@pytest.mark.unit
def test_the_old_stem_stays_reserved_by_its_retired_keys(db_session, override_projects_root):
    from app.models.collection import offsite_keys_under_stem

    vol, a, b = _retired_fixture(db_session, override_projects_root)

    assert offsite_keys_under_stem(db_session, STEM) is True
    assert offsite_keys_under_stem(db_session, STEM, collection=vol) is True


@pytest.mark.unit
def test_an_image_with_only_retired_rows_is_still_stored_offsite(db_session, override_projects_root, monkeypatch):
    from app.core import config
    from app.core.image_upload import offsite_keys_under_volume, records_stored_offsite

    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", TENANT)
    vol, a, b = _retired_fixture(db_session, override_projects_root)

    # Its bytes are offsite under the retired keys until the new upload verifies.
    assert records_stored_offsite(db_session, [a.record_id, b.record_id]) == sorted({a.record_id, b.record_id})
    # Its retired keys do not lead a record outside every volume back into one.
    assert offsite_keys_under_volume(db_session, a.record_id, STEM) is False


@pytest.mark.unit
def test_the_free_job_selects_only_attached_rows(db_session, override_projects_root):
    from app.core.offsite_free import _objects

    vol, a, b = _retired_fixture(db_session, override_projects_root)
    assert _objects(db_session, vol.id, freed=False) == []

    _row(db_session, b, "capture", _key("b.jpg", stem=NEW_STEM), sha1=_sha1(b"jpeg b"))
    assert [(o.image_id, o.kind, o.key) for o in _objects(db_session, vol.id, freed=False)] == [
        (b.id, "capture", _key("b.jpg", stem=NEW_STEM))]


@pytest.mark.unit
def test_the_capture_manifest_sees_only_attached_rows(db_session, override_projects_root, monkeypatch):
    from app.core import config
    from app.core.capture_manifest import ManifestRefused, build_capture_manifest

    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", TENANT)
    vol, a, b = _retired_fixture(db_session, override_projects_root)

    with pytest.raises(ManifestRefused) as caught:
        build_capture_manifest(db_session, vol)
    rules = {p.rule for p in caught.value.problems}
    assert "not_uploaded" in rules


@pytest.mark.unit
def test_a_retired_key_is_never_adopted_again(remote, db_session, override_projects_root):
    from app.core.image_upload import adopt_objects
    from app.core.offsite_retired import retire_rows

    vol = _volume(db_session)
    img = _image(db_session, vol, override_projects_root, "a.jpg", b"jpeg a")
    _run(db_session)
    retire_rows(db_session, [img.id], now=_now())
    db_session.commit()

    report = adopt_objects(db_session, f"{TENANT}/{STEM}", apply=True)

    assert report["adopted"] == []
    assert [e["key"] for e in report["key_already_held"]] == [_key("a.jpg")]
    rows = _all(db_session)
    assert set(rows) == {_key("a.jpg")} and rows[_key("a.jpg")].record_image_id is None


@pytest.mark.unit
def test_the_new_key_is_still_never_overwritten(remote, db_session, override_projects_root):
    from app.core.offsite_retired import retire_rows

    old = _volume(db_session)
    img = _image(db_session, old, override_projects_root, "a.jpg", b"jpeg a")
    _run(db_session)
    retire_rows(db_session, [img.id], now=_now())
    db_session.commit()
    new = _volume(db_session, name="Tomo 2", stem=NEW_STEM)
    new_name = f"{NEW_STEM}-img_0001_c1.jpg"
    _adopt_on_disk(db_session, override_projects_root, img, new, new_name)
    remote.put(_key(new_name, stem=NEW_STEM), b"something else")

    result = _run(db_session)

    assert [r["reason"] for r in result["refused"]] == ["remote_differs"]
    assert remote.files()[_key(new_name, stem=NEW_STEM)] == b"something else"
    assert _listed(db_session)[_key("a.jpg")]["deletable"] is False


@pytest.mark.unit
def test_a_new_name_equal_to_the_retired_key_gets_a_key_of_its_own(remote, db_session, override_projects_root):
    from app.core.offsite_retired import retire_rows

    vol = _volume(db_session)
    img = _image(db_session, vol, override_projects_root, "a.jpg", b"jpeg a")
    _run(db_session)
    retire_rows(db_session, [img.id], now=_now())
    db_session.commit()

    result = _run(db_session)

    assert result["uploaded"] == 1, result
    rows = _all(db_session)
    assert rows[_key(f"a_img{img.id}.jpg")].record_image_id == img.id
    assert rows[_key("a.jpg")].retired_image_id == img.id
