#!/usr/bin/env python3
"""
Tests for NEH-121: deterministic collection listing order, GET /collections/count,
sequence-aware record ordering within a collection, and structured export 422 detail.
Run with: python -m pytest tests/unit/test_collections_order_count_export_detail.py
"""

import pytest


def _reader():
    from app.models.user import User
    return User(username="reader", email="r@example.com",
                hashed_password="x", role="reviewer", is_active=True)


@pytest.fixture
def read_client(client, db_session):
    from app.main import app
    # collections.py and records.py each bind their own allow_read_only
    # RoleChecker instance; override both exact objects (see
    # tests/unit/test_collections_hierarchy.py).
    import app.api.collections as collections
    import app.api.records as records
    app.dependency_overrides[collections.allow_read_only] = lambda: _reader()
    app.dependency_overrides[records.allow_read_only] = lambda: _reader()
    yield client


# ==============================================================================
# (a) list_collections ordering + skip/limit
# ==============================================================================

@pytest.mark.unit
def test_list_collections_is_id_ordered_and_paginates(read_client, db_session):
    from app.models.project import Project
    from app.models.collection import Collection

    proj = Project(name="P")
    db_session.add(proj)
    db_session.commit()

    # Insert out of any natural name order so an accidental name/heap sort
    # wouldn't accidentally look like id order.
    names = ["zeta", "alpha", "mu", "beta", "gamma"]
    for n in names:
        db_session.add(Collection(name=n, project_id=proj.id))
    db_session.commit()

    ids = [c.id for c in db_session.query(Collection).order_by(Collection.id).all()]

    resp = read_client.get("/collections/", params={"project_id": proj.id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [c["id"] for c in body] == ids

    resp = read_client.get("/collections/", params={"project_id": proj.id, "skip": 1, "limit": 2})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [c["id"] for c in body] == ids[1:3]


# ==============================================================================
# (b) GET /collections/count
# ==============================================================================

@pytest.mark.unit
def test_collections_count_with_and_without_project_filter(read_client, db_session):
    from app.models.project import Project
    from app.models.collection import Collection

    proj_a = Project(name="A")
    proj_b = Project(name="B")
    db_session.add_all([proj_a, proj_b])
    db_session.commit()

    for i in range(3):
        db_session.add(Collection(name=f"a{i}", project_id=proj_a.id))
    for i in range(2):
        db_session.add(Collection(name=f"b{i}", project_id=proj_b.id))
    db_session.commit()

    resp = read_client.get("/collections/count")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"count": 5}

    resp = read_client.get("/collections/count", params={"project_id": proj_a.id})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"count": 3}

    resp = read_client.get("/collections/count", params={"project_id": proj_b.id})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"count": 2}


@pytest.mark.unit
def test_collections_count_with_parent_collection_filter(read_client, db_session):
    from app.models.project import Project
    from app.models.collection import Collection

    proj = Project(name="P")
    db_session.add(proj)
    db_session.commit()

    parent = Collection(name="parent", project_id=proj.id)
    db_session.add(parent)
    db_session.commit()

    for i in range(4):
        db_session.add(Collection(name=f"child{i}", parent_collection_id=parent.id))
    db_session.commit()

    resp = read_client.get("/collections/count", params={"parent_collection_id": parent.id})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"count": 4}

    # A parent with no subcollections counts zero.
    resp = read_client.get("/collections/count", params={"parent_collection_id": 999999})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"count": 0}


@pytest.mark.unit
def test_collections_count_route_not_shadowed_by_collection_id_route(read_client, db_session):
    from app.models.project import Project
    from app.models.collection import Collection

    proj = Project(name="P")
    db_session.add(proj)
    db_session.commit()
    db_session.add(Collection(name="c", project_id=proj.id))
    db_session.commit()

    # If /count were parsed by GET /{collection_id}, this would 422 (int
    # validation failure on "count") instead of returning the count payload.
    resp = read_client.get("/collections/count")
    assert resp.status_code == 200, resp.text
    assert "count" in resp.json()


# ==============================================================================
# (c) list_records ordering: sequence.nulls_last() then id, within a collection
# ==============================================================================

@pytest.mark.unit
def test_list_records_orders_by_sequence_nulls_last_then_id_within_collection(read_client, db_session):
    from app.models.project import Project
    from app.models.collection import Collection
    from app.models.record import Record

    proj = Project(name="P")
    db_session.add(proj)
    db_session.commit()
    col = Collection(name="C", project_id=proj.id)
    db_session.add(col)
    db_session.commit()

    # Insert out of sequence order, mixing NULL and non-NULL sequence values.
    # Expected order: sequence 0, 1, 2 first (ascending), then NULLs by id.
    r_null_a = Record(title="null-a", collection_id=col.id, sequence=None, capture_mode="single")
    r_seq2 = Record(title="seq-2", collection_id=col.id, sequence=2, capture_mode="single")
    r_null_b = Record(title="null-b", collection_id=col.id, sequence=None, capture_mode="single")
    r_seq0 = Record(title="seq-0", collection_id=col.id, sequence=0, capture_mode="single")
    r_seq1 = Record(title="seq-1", collection_id=col.id, sequence=1, capture_mode="single")
    db_session.add_all([r_null_a, r_seq2, r_null_b, r_seq0, r_seq1])
    db_session.commit()
    for r in (r_null_a, r_seq2, r_null_b, r_seq0, r_seq1):
        db_session.refresh(r)

    # NULL-sequence records tiebreak by id, in insertion order here.
    expected_null_order = sorted([r_null_a.id, r_null_b.id])
    expected_ids = [r_seq0.id, r_seq1.id, r_seq2.id, *expected_null_order]

    resp = read_client.get("/records/", params={"collection_id": col.id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["id"] for r in body] == expected_ids


@pytest.mark.unit
def test_list_records_without_collection_id_orders_by_id_only(read_client, db_session):
    from app.models.project import Project
    from app.models.record import Record

    proj = Project(name="P")
    db_session.add(proj)
    db_session.commit()

    # Sequence values that would sort differently than id if honored;
    # project-wide listing must ignore them and use pure id order.
    r1 = Record(title="r1", project_id=proj.id, sequence=5, capture_mode="single")
    r2 = Record(title="r2", project_id=proj.id, sequence=1, capture_mode="single")
    r3 = Record(title="r3", project_id=proj.id, sequence=None, capture_mode="single")
    db_session.add_all([r1, r2, r3])
    db_session.commit()
    for r in (r1, r2, r3):
        db_session.refresh(r)

    resp = read_client.get("/records/", params={"project_id": proj.id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["id"] for r in body] == [r1.id, r2.id, r3.id]


# ==============================================================================
# (d) export 422 detail is a structured dict
# ==============================================================================

def _contributor():
    from app.models.user import User
    return User(username="contributor", email="c@example.com",
                hashed_password="x", role="operator", is_active=True)


@pytest.fixture
def contributor_client(client, db_session):
    from app.main import app
    import app.api.collections as collections
    # export_collection_bagit is behind allow_read_only, not allow_contributor;
    # reuse the reader override for the export calls in these tests.
    app.dependency_overrides[collections.allow_read_only] = lambda: _contributor()
    yield client


@pytest.mark.unit
def test_export_empty_collection_returns_structured_422(contributor_client, db_session):
    from app.models.project import Project
    from app.models.collection import Collection

    proj = Project(name="P")
    db_session.add(proj)
    db_session.commit()
    col = Collection(name="C", project_id=proj.id)
    db_session.add(col)
    db_session.commit()

    resp = contributor_client.post(f"/collections/{col.id}/export")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["message"] == "Collection has no records to export."
    assert detail["blocking_record_ids"] == []


@pytest.mark.unit
def test_export_non_approved_records_returns_structured_422(contributor_client, db_session):
    from app.models.project import Project
    from app.models.collection import Collection
    from app.models.record import Record

    proj = Project(name="P")
    db_session.add(proj)
    db_session.commit()
    col = Collection(name="C", project_id=proj.id)
    db_session.add(col)
    db_session.commit()

    r1 = Record(title="r1", collection_id=col.id, status="rejected", capture_mode="single")
    r2 = Record(title="r2", collection_id=col.id, status="approved", capture_mode="single")
    r3 = Record(title="r3", collection_id=col.id, status="in_review", capture_mode="single")
    db_session.add_all([r1, r2, r3])
    db_session.commit()
    for r in (r1, r2, r3):
        db_session.refresh(r)

    resp = contributor_client.post(f"/collections/{col.id}/export")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, dict)
    assert "Cannot export: 2 record(s) are not approved yet" in detail["message"]
    assert set(detail["blocking_record_ids"]) == {r1.id, r3.id}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
