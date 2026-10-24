"""A record whose images are stored offsite cannot leave its volume.

Its objects are keyed under the volume's stem in the bucket, and those keys
never change, so a move would leave the volume's objects naming a record
that is elsewhere. Every path that changes records.collection_id answers
409: PATCH /records/{id} with another parent, POST
/collections/{id}/move-records, and POST /projects/{id}/add_record/{rec},
which takes a record out of its volume. A record already outside every
volume (one that left before these checks existed) is let back only into
the volume its keys are under, `<tenant>/<stem>/`.
"""

import pytest


@pytest.fixture
def admin(client):
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User

    app.dependency_overrides[get_current_user] = lambda: User(
        username="adm", email="adm@example.com", hashed_password="x", role="admin", is_active=True)
    return client


def _setup(db, *, stored=True):
    from app.models.collection import Collection
    from app.models.offsite_object import OffsiteObject
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    p = Project(name="P")
    db.add(p)
    db.commit()
    a = Collection(name="Tomo 1", project_id=p.id, filename_stem="xx-test-001")
    b = Collection(name="Tomo 2", project_id=p.id, filename_stem="xx-test-002")
    db.add_all([a, b])
    db.commit()
    rec = Record(title="f1", collection_id=a.id, capture_mode="single")
    other = Record(title="f2", collection_id=a.id, capture_mode="single")
    db.add_all([rec, other])
    db.commit()
    img = RecordImage(record_id=rec.id, filename="f1.jpg", file_path="/nonexistent/f1.jpg", format="JPG")
    db.add(img)
    db.commit()
    if stored:
        db.add(OffsiteObject(record_image_id=img.id, kind="capture",
                             object_key="captua-test/xx-test-001/captures/f1.jpg", local_sha1="0" * 40))
        db.commit()
    return a, b, rec, other


@pytest.mark.unit
def test_patch_refuses_to_move_a_record_stored_offsite(admin, db_session):
    a, b, rec, _ = _setup(db_session)
    response = admin.patch(f"/records/{rec.id}", json={"collection_id": b.id})
    assert response.status_code == 409, response.text
    assert "stored offsite under this volume" in response.json()["detail"]
    db_session.expire_all()
    assert db_session.get(type(rec), rec.id).collection_id == a.id


@pytest.mark.unit
def test_patch_refuses_to_take_a_record_stored_offsite_out_of_its_volume(admin, db_session):
    a, _, rec, _ = _setup(db_session)
    response = admin.patch(f"/records/{rec.id}", json={"project_id": a.project_id})
    assert response.status_code == 409, response.text


@pytest.mark.unit
def test_patch_of_a_record_stored_offsite_may_still_change_its_description(admin, db_session):
    _, _, rec, _ = _setup(db_session)
    response = admin.patch(f"/records/{rec.id}", json={"title": "folio 1r"})
    assert response.status_code == 200, response.text


@pytest.mark.unit
def test_patch_moves_a_record_with_nothing_stored_offsite(admin, db_session):
    _, b, rec, _ = _setup(db_session, stored=False)
    response = admin.patch(f"/records/{rec.id}", json={"collection_id": b.id})
    assert response.status_code == 200, response.text


@pytest.mark.unit
def test_move_records_refuses_a_volume_with_a_record_stored_offsite(admin, db_session):
    from app.models.record import Record

    a, b, rec, other = _setup(db_session)
    response = admin.post(f"/collections/{a.id}/move-records", params={"target_collection_id": b.id})
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "stored offsite under this volume" in detail["message"]
    assert detail["blocking_record_ids"] == [rec.id]
    db_session.expire_all()
    assert {r.collection_id for r in db_session.query(Record).all()} == {a.id}, "nothing moved"


@pytest.mark.unit
def test_add_record_refuses_to_take_a_record_stored_offsite_out_of_its_volume(admin, db_session):
    a, _, rec, _ = _setup(db_session)
    response = admin.post(f"/projects/{a.project_id}/add_record/{rec.id}")
    assert response.status_code == 409, response.text
    assert "stored offsite under this volume" in response.json()["detail"]
    db_session.expire_all()
    moved = db_session.get(type(rec), rec.id)
    assert (moved.project_id, moved.collection_id) == (None, a.id)


@pytest.mark.unit
def test_add_record_takes_a_record_with_nothing_stored_offsite_out_of_its_volume(admin, db_session):
    a, _, rec, _ = _setup(db_session, stored=False)
    response = admin.post(f"/projects/{a.project_id}/add_record/{rec.id}")
    assert response.status_code == 200, response.text


def _put_outside_every_volume(db, rec, project_id):
    """The state the unguarded add_record left behind: offsite rows, no volume."""
    rec.collection_id = None
    rec.project_id = project_id
    db.add(rec)
    db.commit()


@pytest.mark.unit
def test_patch_refuses_to_put_a_record_stored_offsite_into_a_volume(admin, db_session, tenant):
    a, b, rec, _ = _setup(db_session)
    _put_outside_every_volume(db_session, rec, a.project_id)
    response = admin.patch(f"/records/{rec.id}", json={"collection_id": b.id})
    assert response.status_code == 409, response.text
    assert "under the volume stem xx-test-001" in response.json()["detail"]
    db_session.expire_all()
    assert db_session.get(type(rec), rec.id).collection_id is None


@pytest.fixture
def tenant(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", "captua-test")


@pytest.mark.unit
def test_patch_puts_a_record_stored_offsite_back_into_the_volume_its_keys_are_under(admin, db_session, tenant):
    """Its keys name that volume's stem again, so nothing in the bucket is left naming a record elsewhere."""
    a, _, rec, _ = _setup(db_session)
    _put_outside_every_volume(db_session, rec, a.project_id)
    response = admin.patch(f"/records/{rec.id}", json={"collection_id": a.id})
    assert response.status_code == 200, response.text
    db_session.expire_all()
    back = db_session.get(type(rec), rec.id)
    assert (back.project_id, back.collection_id) == (None, a.id)


@pytest.mark.unit
def test_the_way_back_is_refused_when_one_key_lies_under_another_volume(admin, db_session, tenant):
    from app.models.offsite_object import OffsiteObject
    from app.models.record import RecordImage

    a, _, rec, _ = _setup(db_session)
    img = RecordImage(record_id=rec.id, filename="f9.jpg", file_path="/nonexistent/f9.jpg", format="JPG")
    db_session.add(img)
    db_session.commit()
    db_session.add(OffsiteObject(record_image_id=img.id, kind="capture",
                                 object_key="captua-test/xx-test-002/captures/f9.jpg", local_sha1="1" * 40))
    db_session.commit()
    _put_outside_every_volume(db_session, rec, a.project_id)
    response = admin.patch(f"/records/{rec.id}", json={"collection_id": a.id})
    assert response.status_code == 409, response.text


@pytest.mark.unit
def test_the_way_back_is_refused_without_a_configured_tenant(admin, db_session, monkeypatch):
    """The keys cannot be matched to `<tenant>/<stem>/` without the tenant, so the move is refused."""
    from app.core import config

    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", "")
    a, _, rec, _ = _setup(db_session)
    _put_outside_every_volume(db_session, rec, a.project_id)
    response = admin.patch(f"/records/{rec.id}", json={"collection_id": a.id})
    assert response.status_code == 409, response.text
    assert "once DTK_OFFSITE_TENANT is set" in response.json()["detail"]


@pytest.mark.unit
def test_two_calls_cannot_carry_a_record_stored_offsite_into_another_volume(admin, db_session):
    a, b, rec, _ = _setup(db_session)
    assert admin.post(f"/projects/{a.project_id}/add_record/{rec.id}").status_code == 409
    assert admin.patch(f"/records/{rec.id}", json={"collection_id": b.id}).status_code == 409
    db_session.expire_all()
    assert db_session.get(type(rec), rec.id).collection_id == a.id


@pytest.mark.unit
def test_patch_puts_a_record_with_nothing_stored_offsite_into_a_volume(admin, db_session):
    a, b, rec, _ = _setup(db_session, stored=False)
    _put_outside_every_volume(db_session, rec, a.project_id)
    response = admin.patch(f"/records/{rec.id}", json={"collection_id": b.id})
    assert response.status_code == 200, response.text


@pytest.mark.unit
def test_the_way_back_is_refused_into_a_volume_whose_stem_is_a_prefix_of_the_keys_stem(admin, db_session, tenant):
    """Keys under `xx-test-001/` do not lie under `xx-test-00/`."""
    from app.models.collection import Collection

    a, _, rec, _ = _setup(db_session)
    short = Collection(name="Tomo 0", project_id=a.project_id, filename_stem="xx-test-00")
    db_session.add(short)
    db_session.commit()
    _put_outside_every_volume(db_session, rec, a.project_id)
    response = admin.patch(f"/records/{rec.id}", json={"collection_id": short.id})
    assert response.status_code == 409, response.text
    assert "xx-test-001" in response.json()["detail"]


@pytest.mark.unit
def test_a_stranded_record_refused_the_way_back_is_told_the_stem_its_keys_are_under(admin, db_session, tenant):
    from app.core.image_upload import STORED_OFFSITE_MESSAGE

    a, b, rec, _ = _setup(db_session)
    _put_outside_every_volume(db_session, rec, a.project_id)
    detail = admin.patch(f"/records/{rec.id}", json={"collection_id": b.id}).json()["detail"]
    assert detail != STORED_OFFSITE_MESSAGE
    assert "xx-test-001" in detail and "only into" in detail


@pytest.mark.unit
def test_a_stranded_record_with_keys_under_two_stems_is_told_it_can_go_into_no_volume(admin, db_session, tenant):
    from app.models.offsite_object import OffsiteObject
    from app.models.record import RecordImage

    a, _, rec, _ = _setup(db_session)
    img = RecordImage(record_id=rec.id, filename="f9.jpg", file_path="/nonexistent/f9.jpg", format="JPG")
    db_session.add(img)
    db_session.commit()
    db_session.add(OffsiteObject(record_image_id=img.id, kind="capture",
                                 object_key="captua-test/xx-test-002/captures/f9.jpg", local_sha1="1" * 40))
    db_session.commit()
    _put_outside_every_volume(db_session, rec, a.project_id)
    detail = admin.patch(f"/records/{rec.id}", json={"collection_id": a.id}).json()["detail"]
    assert "xx-test-001" in detail and "xx-test-002" in detail and "no volume" in detail


@pytest.mark.unit
def test_a_record_leaving_its_volume_still_gets_the_original_message(admin, db_session):
    from app.core.image_upload import STORED_OFFSITE_MESSAGE

    a, b, rec, _ = _setup(db_session)
    assert admin.patch(f"/records/{rec.id}", json={"collection_id": b.id}).json()["detail"] == STORED_OFFSITE_MESSAGE
    assert admin.post(f"/projects/{a.project_id}/add_record/{rec.id}").json()["detail"] == STORED_OFFSITE_MESSAGE
