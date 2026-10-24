"""`records.external_id` is gone; the reference code lives on the volume.

Exchange contract 1.1 maps each Fisqua item onto a volume, so Fisqua's
`referenceCode` is `collections.external_id` and a record carries none. The
column `c2d3e4f5a6b7` added to `records` had no writer and the frontend never
read it, so `a7c1e9d2b4f6` drops it with its unique index, and the write-once
guard that protected it goes with it.
"""

import importlib.util

import pytest
import sqlalchemy as sa


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


@pytest.mark.unit
def test_records_has_no_external_id_column_or_index():
    from app.models.record import Record

    assert "external_id" not in Record.__table__.columns
    assert not any("external_id" in [c.name for c in ix.columns]
                   for ix in Record.__table__.indexes)


@pytest.mark.unit
def test_record_has_no_external_id_guard():
    from app.models.record import Record

    assert not hasattr(Record, "_external_id_is_write_once")


@pytest.mark.unit
def test_no_record_schema_has_external_id():
    from app.schemas.record import RecordBase, RecordCreate, RecordRead, RecordUpdate

    offenders = [s.__name__ for s in (RecordBase, RecordCreate, RecordRead, RecordUpdate)
                 if "external_id" in s.model_fields]
    assert offenders == [], offenders


@pytest.mark.unit
def test_a_record_response_carries_no_external_id(client, db_session):
    api = _client_as(client, "op", "operator")

    resp = api.post("/records/", json={"title": "t", "capture_mode": "single",
                                       "external_id": "CO-NG-0001"})

    assert resp.status_code == 200, resp.text
    assert "external_id" not in resp.json(), resp.json()


@pytest.mark.unit
def test_the_migration_drops_it_and_the_downgrade_restores_it_empty(backend_root, tmp_path):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = (backend_root / "alembic" / "versions"
            / "a7c1e9d2b4f6_add_collection_volume_identity.py")
    spec = importlib.util.spec_from_file_location("volid_mig", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'r.db'}")
    md = sa.MetaData()
    sa.Table("projects", md, sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("name", sa.String(255), nullable=False))
    sa.Table("collections", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("name", sa.String(255), nullable=False),
             sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id")),
             sa.Column("parent_collection_id", sa.Integer, sa.ForeignKey("collections.id")),
             sa.Column("created_at", sa.DateTime, nullable=False))
    sa.Table("records", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("title", sa.String(255), nullable=False),
             sa.Column("collection_id", sa.Integer, sa.ForeignKey("collections.id")),
             sa.Column("external_id", sa.String(255)),
             sa.Index("ix_records_external_id", "external_id", unique=True))
    sa.Table("record_images", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("record_id", sa.Integer, sa.ForeignKey("records.id"), nullable=False),
             sa.Column("capture_id", sa.String(36)),
             sa.Column("is_current", sa.Boolean, nullable=False))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO records (id, title, external_id) VALUES (1, 'a', 'CO-1')"))
        conn.execute(sa.text("INSERT INTO records (id, title) VALUES (2, 'b')"))

    def run(fn):
        with engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                fn()

    try:
        run(mod.upgrade)
        insp = sa.inspect(engine)
        assert "external_id" not in {c["name"] for c in insp.get_columns("records")}
        assert "ix_records_external_id" not in {i["name"] for i in insp.get_indexes("records")}
        with engine.begin() as conn:
            assert [r[0] for r in conn.execute(sa.text("SELECT title FROM records ORDER BY id"))] == ["a", "b"]

        run(mod.downgrade)
        insp = sa.inspect(engine)
        col = {c["name"]: c for c in insp.get_columns("records")}["external_id"]
        assert col["nullable"] is True
        ix = {i["name"]: i for i in insp.get_indexes("records")}["ix_records_external_id"]
        assert ix["unique"] in (1, True) and ix["column_names"] == ["external_id"]
        with engine.begin() as conn:
            values = [r[0] for r in conn.execute(sa.text("SELECT external_id FROM records"))]
        assert values == [None, None], "values are lost on downgrade"
    finally:
        engine.dispose()
