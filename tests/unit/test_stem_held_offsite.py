"""A stem under which objects are stored offsite is never released or issued again.

Every offsite object is keyed `<tenant>/<stem>/<rest>`, and the key never
changes. A row of offsite_objects keeps its key after its image and record
are deleted (record_image_id is ON DELETE SET NULL), so the key is the
lasting evidence that names were issued under the stem, whoever the tenant
was. A volume whose stem has such keys under it is treated as having
captured: its stem cannot be changed or cleared, and the volume cannot be
deleted. A stem with such keys under it cannot be given to a volume, unless
every one of them belongs to an image of a record in that volume.
"""

import pytest

from tests.unit.test_list_import import _as, _item, _list_with, _post, _volumes, reports_root  # noqa: F401


@pytest.fixture
def admin(client):
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User

    app.dependency_overrides[get_current_user] = lambda: User(
        username="adm", email="adm@example.com", hashed_password="x", role="admin", is_active=True)
    return client


def _project(db):
    from app.models.project import Project

    p = Project(name="P")
    db.add(p)
    db.commit()
    return p


def _volume(db, project, name="Tomo 1", **kw):
    from app.models.collection import Collection

    v = Collection(name=name, project_id=project.id, **kw)
    db.add(v)
    db.commit()
    return v


def _stored_record(db, volume, key):
    """A record in `volume` with one image that has an offsite row at `key`."""
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage

    rec = Record(title="f1", collection_id=volume.id, capture_mode="single")
    db.add(rec)
    db.commit()
    img = RecordImage(record_id=rec.id, filename="f1.jpg", file_path="/nonexistent/f1.jpg", format="JPG")
    db.add(img)
    db.commit()
    db.add(OffsiteObject(record_image_id=img.id, kind="capture", object_key=key, local_sha1="0" * 40))
    db.commit()
    return rec


def _orphan_key(db, key):
    """An offsite row whose image is gone: the state ON DELETE SET NULL leaves."""
    from app.models.offsite_object import OffsiteObject

    db.add(OffsiteObject(record_image_id=None, kind="capture", object_key=key, local_sha1="0" * 40))
    db.commit()


def _emptied_volume(admin, db, key="captua-test/xx-test-001/captures/f1.jpg"):
    """A volume with stem xx-test-001, no counter, whose only stored record was deleted."""
    p = _project(db)
    v = _volume(db, p, filename_stem="xx-test-001")
    rec = _stored_record(db, v, key)
    response = admin.delete(f"/records/{rec.id}")
    assert response.status_code == 200, response.text
    db.expire_all()
    assert v.capture_counter == 0 and not v.has_images()
    return p, v


# ---------------------------------------------------------------------------
# The volume that issued the names keeps its stem
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("new", ["xx-test-002", None], ids=["change", "clear"])
def test_patch_refuses_to_change_a_stem_after_its_only_stored_record_was_deleted(admin, db_session, new):
    _, v = _emptied_volume(admin, db_session)

    response = admin.patch(f"/collections/{v.id}", json={"filename_stem": new})

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["message"] == (
        "The volume has already captured images, so its file name stem cannot be changed.")
    db_session.expire_all()
    assert v.filename_stem == "xx-test-001"


@pytest.mark.unit
def test_delete_refuses_a_volume_whose_only_stored_record_was_deleted(admin, db_session):
    from app.models.collection import STEM_RESERVED_MESSAGE, Collection

    _, v = _emptied_volume(admin, db_session)

    response = admin.delete(f"/collections/{v.id}")

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["message"] == STEM_RESERVED_MESSAGE
    db_session.expire_all()
    assert db_session.get(Collection, v.id) is not None


@pytest.mark.unit
def test_the_read_schema_says_the_stem_is_reserved(admin, db_session):
    _, v = _emptied_volume(admin, db_session)

    assert admin.get(f"/collections/{v.id}").json()["stem_reserved"] is True


@pytest.mark.unit
def test_the_model_refuses_the_change_and_the_delete_with_a_key_left_by_a_deleted_image(db_session):
    """record_image_id NULL, as Postgres leaves it; SQLite in the suite keeps the dangling id."""
    from app.models.collection import StemReservedError
    from app.models.record import ImmutableFieldError

    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="xx-test-001")
    _orphan_key(db_session, "captua-test/xx-test-001/previews/xx-test-001-img_0001_preview.jpg")

    with pytest.raises(ImmutableFieldError):
        v.filename_stem = "xx-test-002"
    with pytest.raises(ImmutableFieldError):
        v.filename_stem = None
    db_session.rollback()

    db_session.delete(v)
    with pytest.raises(StemReservedError):
        db_session.flush()
    db_session.rollback()


@pytest.mark.unit
def test_a_key_under_another_tenant_also_holds_the_stem(db_session):
    from app.models.record import ImmutableFieldError

    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="xx-test-001")
    _orphan_key(db_session, "earlier-tenant/xx-test-001/captures/f1.jpg")

    assert v.reserves_its_stem()
    with pytest.raises(ImmutableFieldError):
        v.filename_stem = "xx-test-002"


@pytest.mark.unit
@pytest.mark.parametrize("key", [
    "captua-test/xx-test-0010/captures/f1.jpg",
    "captua-test/xx-test-00/captures/f1.jpg",
    "captua-test/other/xx-test-001/f1.jpg",
    "xx-test-001/captures/f1.jpg",
    "captua-test/xx-test-001",
], ids=["longer-stem", "shorter-stem", "deeper-segment", "no-tenant", "nothing-below"])
def test_a_key_not_under_tenant_slash_stem_slash_holds_nothing(db_session, key):
    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="xx-test-001")
    _orphan_key(db_session, key)

    assert not v.reserves_its_stem()
    v.filename_stem = "xx-test-002"
    db_session.commit()


# ---------------------------------------------------------------------------
# A stem with keys under it is not given to another volume
# ---------------------------------------------------------------------------

HELD_MESSAGE = ("Images are stored offsite under the file name stem xx-test-777, "
                "so it cannot be given to this volume.")


@pytest.mark.unit
def test_post_refuses_a_stem_with_offsite_keys_under_it(admin, db_session):
    from app.models.collection import Collection

    p = _project(db_session)
    _orphan_key(db_session, "captua-test/xx-test-777/captures/f1.jpg")

    response = admin.post("/collections/", json={"name": "Tomo 9", "project_id": p.id,
                                                  "filename_stem": "xx-test-777"})

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["message"] == HELD_MESSAGE
    db_session.expire_all()
    assert db_session.query(Collection).filter(Collection.filename_stem == "xx-test-777").first() is None


@pytest.mark.unit
@pytest.mark.parametrize("before", [None, "xx-test-005"], ids=["first-stem", "free-stem"])
def test_patch_refuses_a_stem_with_offsite_keys_under_it(admin, db_session, before):
    p = _project(db_session)
    v = _volume(db_session, p, filename_stem=before)
    _orphan_key(db_session, "earlier-tenant/xx-test-777/captures/f1.jpg")

    response = admin.patch(f"/collections/{v.id}", json={"filename_stem": "xx-test-777"})

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["message"] == HELD_MESSAGE
    db_session.expire_all()
    assert v.filename_stem == before


@pytest.mark.unit
def test_a_stem_released_around_the_model_is_not_given_to_another_volume(admin, db_session):
    """Raw SQL goes around the model's guards; the keys still hold the stem."""
    import sqlalchemy as sa

    p, v = _emptied_volume(admin, db_session, key="captua-test/xx-test-777/captures/f1.jpg")
    volume_id = v.id
    db_session.expunge(v)
    db_session.execute(sa.text("DELETE FROM collections WHERE id = :i"), {"i": volume_id})
    db_session.commit()
    other = _volume(db_session, p, name="Tomo 2")

    response = admin.patch(f"/collections/{other.id}", json={"filename_stem": "xx-test-777"})

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["message"] == HELD_MESSAGE


@pytest.mark.unit
def test_a_volume_takes_back_a_stem_whose_keys_are_all_its_own(admin, db_session):
    """A volume whose stem was cleared before this guard can have it set again."""
    import sqlalchemy as sa

    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="xx-test-777")
    _stored_record(db_session, v, "captua-test/xx-test-777/captures/f1.jpg")
    db_session.execute(sa.text("UPDATE collections SET filename_stem = NULL WHERE id = :i"), {"i": v.id})
    db_session.commit()

    response = admin.patch(f"/collections/{v.id}", json={"filename_stem": "xx-test-777"})

    assert response.status_code == 200, response.text


@pytest.mark.unit
def test_a_volume_does_not_take_a_stem_with_keys_beyond_its_own(admin, db_session):
    import sqlalchemy as sa

    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="xx-test-777")
    _stored_record(db_session, v, "captua-test/xx-test-777/captures/f1.jpg")
    _orphan_key(db_session, "captua-test/xx-test-777/captures/f2.jpg")
    db_session.execute(sa.text("UPDATE collections SET filename_stem = NULL WHERE id = :i"), {"i": v.id})
    db_session.commit()

    response = admin.patch(f"/collections/{v.id}", json={"filename_stem": "xx-test-777"})

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["message"] == HELD_MESSAGE


@pytest.mark.unit
def test_the_list_import_rejects_an_item_whose_stem_has_offsite_keys(client, db_session, reports_root):
    _orphan_key(db_session, "captua-test/xx-test-901/captures/f1.jpg")
    _as("adm", "admin")

    resp = _post(client, _list_with([_item(), _item(code="xx-test-950", stem="xx-test-950")]))

    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert [(r["position"], r["reason"]) for r in report["rejects"]] == [(1, "filename_stem_held_offsite")]
    assert set(_volumes(db_session)) == {"xx-test-950"}
