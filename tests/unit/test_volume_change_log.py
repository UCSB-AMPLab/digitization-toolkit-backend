"""Renaming, moving and deleting a volume is written to the system log (NEH-275).

PATCH /collections/{id} logs `collection_renamed` when the name or the file
name stem changes, and `collection_moved` when the parent changes (the body
has no project_id; nesting a volume clears its project link). DELETE /collections/{id} logs `collection_deleted`.
POST /projects/{id}/move-collections logs `collection_moved` for each volume it
moves, beside its existing `collections_moved` summary. A request that is
refused or changes nothing logs nothing.
"""

import pytest


@pytest.fixture
def operator(client):
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User

    app.dependency_overrides[get_current_user] = lambda: User(
        username="op", email="op@example.com", hashed_password="x", role="operator", is_active=True)
    return client


def _project(db, name):
    from app.models.project import Project

    p = Project(name=name)
    db.add(p)
    db.commit()
    return p


def _volume(db, name, project=None, parent=None, **kw):
    from app.models.collection import Collection

    c = Collection(name=name, project_id=project.id if project else None,
                   parent_collection_id=parent.id if parent else None, **kw)
    db.add(c)
    db.commit()
    return c


def _events(db, *actions):
    from app.models.system_log import SystemLog

    db.expire_all()
    rows = db.query(SystemLog).filter(SystemLog.action.in_(actions)).order_by(SystemLog.id).all()
    return rows


# ---- PATCH: renamed ------------------------------------------------------

def test_patch_name_logs_collection_renamed(operator, db_session):
    p = _project(db_session, "P")
    v = _volume(db_session, "Old", p, filename_stem="old-stem")

    assert operator.patch(f"/collections/{v.id}", json={"name": "New"}).status_code == 200

    (e,) = _events(db_session, "collection_renamed")
    assert (e.level, e.category, e.actor, e.subject) == ("INFO", "activity", "op", "New")
    assert f"id={v.id}" in e.detail
    assert "name 'Old' -> 'New'" in e.detail
    assert "stem 'old-stem' -> 'old-stem'" in e.detail
    assert not _events(db_session, "collection_moved")


def test_patch_stem_logs_collection_renamed(operator, db_session):
    p = _project(db_session, "P")
    v = _volume(db_session, "V", p, filename_stem="one")

    assert operator.patch(f"/collections/{v.id}", json={"filename_stem": "two"}).status_code == 200

    (e,) = _events(db_session, "collection_renamed")
    assert e.subject == "V"
    assert "name 'V' -> 'V'" in e.detail
    assert "stem 'one' -> 'two'" in e.detail


def test_patch_first_stem_logs_old_stem_as_none(operator, db_session):
    p = _project(db_session, "P")
    v = _volume(db_session, "V", p)

    assert operator.patch(f"/collections/{v.id}", json={"filename_stem": "first"}).status_code == 200

    (e,) = _events(db_session, "collection_renamed")
    assert "stem None -> 'first'" in e.detail


def test_patch_other_fields_or_same_name_logs_nothing(operator, db_session):
    p = _project(db_session, "P")
    v = _volume(db_session, "V", p)

    assert operator.patch(f"/collections/{v.id}", json={"description": "d", "name": "V"}).status_code == 200

    assert not _events(db_session, "collection_renamed", "collection_moved")


def test_refused_stem_change_logs_nothing(operator, db_session):
    p = _project(db_session, "P")
    v = _volume(db_session, "V", p, filename_stem="fixed", capture_counter=1)

    r = operator.patch(f"/collections/{v.id}", json={"name": "W", "filename_stem": "other"})

    assert r.status_code == 409
    assert not _events(db_session, "collection_renamed", "collection_moved")


def test_patch_missing_volume_logs_nothing(operator, db_session):
    assert operator.patch("/collections/999", json={"name": "W"}).status_code == 404
    assert not _events(db_session, "collection_renamed", "collection_moved")


# ---- PATCH: moved --------------------------------------------------------

def test_patch_parent_logs_collection_moved(operator, db_session):
    p = _project(db_session, "P")
    parent = _volume(db_session, "Parent", p)
    v = _volume(db_session, "V", p)

    r = operator.patch(f"/collections/{v.id}", json={"parent_collection_id": parent.id})
    assert r.status_code == 200

    (e,) = _events(db_session, "collection_moved")
    assert (e.level, e.category, e.actor, e.subject) == ("INFO", "activity", "op", "V")
    assert f"id={v.id}" in e.detail
    assert f"project {p.id} -> None" in e.detail
    assert f"parent None -> {parent.id}" in e.detail
    assert not _events(db_session, "collection_renamed")


def test_patch_rename_and_move_log_both(operator, db_session):
    p = _project(db_session, "P")
    parent = _volume(db_session, "Parent", p)
    v = _volume(db_session, "V", p)

    r = operator.patch(f"/collections/{v.id}", json={"name": "W", "parent_collection_id": parent.id})
    assert r.status_code == 200

    assert len(_events(db_session, "collection_renamed")) == 1
    assert len(_events(db_session, "collection_moved")) == 1


def test_refused_move_logs_nothing(operator, db_session):
    p = _project(db_session, "P")
    v = _volume(db_session, "V", p)
    child = _volume(db_session, "Child", parent=v)

    own = operator.patch(f"/collections/{v.id}", json={"parent_collection_id": v.id})
    cycle = operator.patch(f"/collections/{v.id}", json={"parent_collection_id": child.id})
    missing = operator.patch(f"/collections/{v.id}", json={"parent_collection_id": 999})

    assert (own.status_code, cycle.status_code, missing.status_code) == (400, 400, 404)
    assert not _events(db_session, "collection_renamed", "collection_moved")


# ---- DELETE --------------------------------------------------------------

def test_delete_logs_collection_deleted(operator, db_session):
    p = _project(db_session, "P")
    v = _volume(db_session, "Gone", p, filename_stem="gone")
    vid = v.id

    assert operator.delete(f"/collections/{vid}").status_code == 204

    (e,) = _events(db_session, "collection_deleted")
    assert (e.level, e.category, e.actor, e.subject) == ("WARN", "activity", "op", "Gone")
    assert f"id={vid}" in e.detail
    assert "stem 'gone'" in e.detail
    assert f"project {p.id}" in e.detail
    assert "records=0" in e.detail and "images=0" in e.detail


def test_refused_delete_logs_nothing(operator, db_session):
    from app.models.record import Record

    p = _project(db_session, "P")
    full = _volume(db_session, "Full", p)
    db_session.add(Record(title="r", collection_id=full.id, status="captured", capture_mode="single"))
    db_session.commit()
    nested = _volume(db_session, "Parent", p)
    _volume(db_session, "Child", parent=nested)
    reserved = _volume(db_session, "Reserved", p, filename_stem="kept", capture_counter=2)

    codes = [operator.delete(f"/collections/{c.id}").status_code for c in (full, nested, reserved)]
    codes.append(operator.delete("/collections/999").status_code)

    assert codes == [409, 409, 409, 404]
    assert not _events(db_session, "collection_deleted")


# ---- POST /projects/{id}/move-collections --------------------------------

def test_move_collections_logs_each_volume(operator, db_session):
    p, q = _project(db_session, "P"), _project(db_session, "Q")
    a = _volume(db_session, "A", p)
    b = _volume(db_session, "B", p)
    child = _volume(db_session, "Child", parent=a)
    other = _volume(db_session, "Elsewhere", q)

    r = operator.post(f"/projects/{p.id}/move-collections", json={"target_project_id": q.id})
    assert r.status_code == 200

    moved = _events(db_session, "collection_moved")
    assert sorted(e.subject for e in moved) == ["A", "B"]
    for e in moved:
        assert (e.level, e.category, e.actor) == ("INFO", "activity", "op")
        assert f"project {p.id} -> {q.id}" in e.detail
        assert "parent None -> None" in e.detail
    assert {e.detail.split(";")[0] for e in moved} == {f"id={a.id}", f"id={b.id}"}
    assert len(_events(db_session, "collections_moved")) == 1
    assert child.id and other.id


def test_refused_move_collections_logs_nothing(operator, db_session):
    p = _project(db_session, "P")
    _volume(db_session, "A", p)

    same = operator.post(f"/projects/{p.id}/move-collections", json={"target_project_id": p.id})
    no_target = operator.post(f"/projects/{p.id}/move-collections", json={"target_project_id": 999})
    no_source = operator.post("/projects/999/move-collections", json={"target_project_id": p.id})

    assert (same.status_code, no_target.status_code, no_source.status_code) == (400, 404, 404)
    assert not _events(db_session, "collection_moved", "collections_moved")
