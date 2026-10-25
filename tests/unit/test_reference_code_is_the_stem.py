"""A volume's reference code is its file name stem.

Fisqua refuses a handover whose reference code differs from the volume's
stem, so a volume whose `external_id` and `filename_stem` are both set and
differ can never be sent. external_id is written once, so such a pair could
not be repaired either. The model refuses it at the flush, for every writer:

- with a stem set, an external_id other than the stem is refused, and the
  stem itself is allowed;
- with an external_id set, a stem other than the external_id is refused;
- either one NULL is allowed: an empty reference code is read as the stem.

The routes answer 409 with a detail that names both values. A refusal
changes nothing in the database.
"""

import json

import pytest


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


def _stored(db_session, volume_id):
    """(filename_stem, external_id) as committed, after any refused flush is discarded."""
    from app.models.collection import Collection

    db_session.rollback()
    db_session.expire_all()
    v = db_session.get(Collection, volume_id)
    return v.filename_stem, v.external_id


def _refused(resp, external_id, stem):
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "reference_code_differs_from_stem", detail
    assert detail["external_id"] == external_id
    assert detail["filename_stem"] == stem
    assert detail["message"] == (
        f"The reference code {external_id!r} is not the file name stem {stem!r}; "
        "a volume's reference code is its file name stem."
    )


# ---------------------------------------------------------------------------
# The model, for every writer
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_model_refuses_an_external_id_other_than_the_stem(db_session):
    from app.models.collection import ReferenceCodeDiffersFromStemError

    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="co-ahr-001")
    v.external_id = "co-ahr-002"
    with pytest.raises(ReferenceCodeDiffersFromStemError) as exc:
        db_session.commit()
    assert (exc.value.external_id, exc.value.filename_stem) == ("co-ahr-002", "co-ahr-001")
    assert _stored(db_session, v.id) == ("co-ahr-001", None)


@pytest.mark.unit
def test_the_model_refuses_a_stem_other_than_the_external_id(db_session):
    from app.models.collection import ReferenceCodeDiffersFromStemError

    p = _project(db_session)
    v = _volume(db_session, p, external_id="co-ahr-003")
    v.filename_stem = "co-ahr-004"
    with pytest.raises(ReferenceCodeDiffersFromStemError):
        db_session.commit()
    assert _stored(db_session, v.id) == (None, "co-ahr-003")


@pytest.mark.unit
@pytest.mark.parametrize("order", ["stem_first", "code_first"])
def test_the_model_refuses_a_new_volume_with_a_differing_pair_in_either_order(db_session, order):
    from app.models.collection import Collection, ReferenceCodeDiffersFromStemError

    p = _project(db_session)
    kw = ({"filename_stem": "co-ahr-005", "external_id": "co-ahr-006"} if order == "stem_first"
          else {"external_id": "co-ahr-006", "filename_stem": "co-ahr-005"})
    db_session.add(Collection(name="V", project_id=p.id, **kw))
    with pytest.raises(ReferenceCodeDiffersFromStemError):
        db_session.commit()
    db_session.rollback()
    assert db_session.query(Collection).count() == 0


@pytest.mark.unit
@pytest.mark.parametrize("stem, code", [
    ("co-ahr-007", "co-ahr-007"),
    ("co-ahr-008", None),
    (None, "CO.AHR.9"),
    (None, None),
])
def test_the_model_allows_an_equal_pair_or_either_one_null(db_session, stem, code):
    p = _project(db_session)
    v = _volume(db_session, p, filename_stem=stem, external_id=code)
    assert _stored(db_session, v.id) == (stem, code)


@pytest.mark.unit
def test_a_volume_whose_pair_differs_already_can_still_be_renamed(db_session):
    """Only a new volume or a changed stem or code is checked, so a row written
    before the rule (by raw SQL here) is not frozen for every other edit."""
    import sqlalchemy as sa
    from app.models.collection import Collection

    p = _project(db_session)
    v = _volume(db_session, p)
    db_session.execute(sa.text("UPDATE collections SET filename_stem = 'co-ahr-010', "
                               "external_id = 'co-ahr-011' WHERE id = :id"), {"id": v.id})
    db_session.commit()
    db_session.expire_all()
    v = db_session.get(Collection, v.id)
    v.name = "Renamed"
    db_session.commit()
    assert db_session.get(Collection, v.id).name == "Renamed"


# ---------------------------------------------------------------------------
# PATCH /collections/{id}
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_patch_refuses_an_external_id_other_than_the_stem(client, db_session):
    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="co-ahr-020")
    api = _client_as(client)

    resp = api.patch(f"/collections/{v.id}", json={"external_id": "CO.AHR.20"})

    _refused(resp, "CO.AHR.20", "co-ahr-020")
    assert _stored(db_session, v.id) == ("co-ahr-020", None)


@pytest.mark.unit
def test_patch_allows_the_stem_as_the_external_id(client, db_session):
    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="co-ahr-021")
    api = _client_as(client)

    resp = api.patch(f"/collections/{v.id}", json={"external_id": "co-ahr-021"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["external_id"] == "co-ahr-021"
    assert _stored(db_session, v.id) == ("co-ahr-021", "co-ahr-021")


@pytest.mark.unit
def test_patch_allows_a_null_external_id_beside_a_stem(client, db_session):
    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="co-ahr-022")
    api = _client_as(client)

    resp = api.patch(f"/collections/{v.id}", json={"external_id": None, "name": "Tomo 22"})

    assert resp.status_code == 200, resp.text
    assert _stored(db_session, v.id) == ("co-ahr-022", None)


@pytest.mark.unit
def test_patch_refuses_a_stem_other_than_the_stored_external_id(client, db_session):
    p = _project(db_session)
    v = _volume(db_session, p, external_id="co-ahr-023")
    api = _client_as(client)

    resp = api.patch(f"/collections/{v.id}", json={"filename_stem": "co-ahr-024"})

    _refused(resp, "co-ahr-023", "co-ahr-024")
    assert _stored(db_session, v.id) == (None, "co-ahr-023")


@pytest.mark.unit
def test_patch_allows_the_external_id_as_the_stem(client, db_session):
    p = _project(db_session)
    v = _volume(db_session, p, external_id="co-ahr-025")
    api = _client_as(client)

    resp = api.patch(f"/collections/{v.id}", json={"filename_stem": "co-ahr-025"})

    assert resp.status_code == 200, resp.text
    assert _stored(db_session, v.id) == ("co-ahr-025", "co-ahr-025")


@pytest.mark.unit
def test_patch_refuses_a_stem_change_away_from_the_external_id(client, db_session):
    """A volume that has not captured may change its stem, but not away from its code."""
    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="co-ahr-026", external_id="co-ahr-026")
    api = _client_as(client)

    resp = api.patch(f"/collections/{v.id}", json={"filename_stem": "co-ahr-027"})

    _refused(resp, "co-ahr-026", "co-ahr-027")
    assert _stored(db_session, v.id) == ("co-ahr-026", "co-ahr-026")


@pytest.mark.unit
def test_patch_setting_both_to_one_value_is_allowed(client, db_session):
    p = _project(db_session)
    v = _volume(db_session, p, filename_stem="co-ahr-028")
    api = _client_as(client)

    resp = api.patch(f"/collections/{v.id}",
                     json={"external_id": "co-ahr-029", "filename_stem": "co-ahr-029"})

    assert resp.status_code == 200, resp.text
    assert _stored(db_session, v.id) == ("co-ahr-029", "co-ahr-029")


@pytest.mark.unit
def test_patch_setting_both_to_different_values_is_refused(client, db_session):
    p = _project(db_session)
    v = _volume(db_session, p)
    api = _client_as(client)

    resp = api.patch(f"/collections/{v.id}",
                     json={"external_id": "co-ahr-030", "filename_stem": "co-ahr-031", "name": "X"})

    _refused(resp, "co-ahr-030", "co-ahr-031")
    db_session.rollback()
    db_session.expire_all()
    from app.models.collection import Collection
    stored = db_session.get(Collection, v.id)
    assert (stored.filename_stem, stored.external_id, stored.name) == (None, None, "V")


# ---------------------------------------------------------------------------
# POST /collections/
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_post_refuses_a_volume_whose_external_id_is_not_its_stem(client, db_session):
    from app.models.collection import Collection

    p = _project(db_session)
    api = _client_as(client)

    resp = api.post("/collections/", json={"name": "T", "project_id": p.id,
                                           "filename_stem": "co-ahr-040", "external_id": "CO.AHR.40"})

    _refused(resp, "CO.AHR.40", "co-ahr-040")
    db_session.rollback()
    assert db_session.query(Collection).count() == 0


@pytest.mark.unit
def test_post_allows_a_volume_whose_external_id_is_its_stem_or_null(client, db_session):
    p = _project(db_session)
    api = _client_as(client)

    same = api.post("/collections/", json={"name": "A", "project_id": p.id,
                                           "filename_stem": "co-ahr-041", "external_id": "co-ahr-041"})
    none = api.post("/collections/", json={"name": "B", "project_id": p.id,
                                           "filename_stem": "co-ahr-042"})

    assert same.status_code == 201, same.text
    assert none.status_code == 201, none.text
    assert none.json()["external_id"] is None


# ---------------------------------------------------------------------------
# POST /imports/digitisation-list
# ---------------------------------------------------------------------------

@pytest.fixture
def reports_root(tmp_path, monkeypatch):
    from app.core import config

    root = tmp_path / "data"
    monkeypatch.setattr(config.settings, "DTK_DATA_DIR", str(root))
    return root / "imports"


def _import(client, items):
    from pathlib import Path
    from app.main import app
    import app.api.imports as imports

    user = _user("op", "admin")
    for dep in (imports.allow_contributor, imports.allow_read_only):
        app.dependency_overrides[dep] = lambda u=user: u
    fixture = (Path(__file__).resolve().parent.parent / "fixtures"
               / "digitisation-list_captua-test_prueba-valida_2026-10-23.json")
    doc = json.loads(fixture.read_text(encoding="utf-8"))
    doc["items"] = items
    return client.post("/imports/digitisation-list",
                       files={"file": ("list.json", json.dumps(doc).encode("utf-8"), "application/json")})


def _item(code, stem):
    return {"referenceCode": code, "filenameStem": stem, "title": "T", "sequence": 1,
            "dates": {"display": None, "start": None, "end": None}}


@pytest.mark.unit
def test_the_import_creates_volumes_whose_code_is_their_stem(client, db_session, reports_root):
    from app.models.collection import Collection

    resp = _import(client, [_item("xx-test-950", "xx-test-950")])

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert [(c.filename_stem, c.external_id) for c in db_session.query(Collection).all()] == [
        ("xx-test-950", "xx-test-950")]


@pytest.mark.unit
def test_the_import_refuses_a_differing_pair_before_the_database(client, db_session, reports_root):
    from app.models.collection import Collection

    resp = _import(client, [_item("xx-test-951", "xx-test-952")])

    assert resp.status_code == 200, resp.text
    assert [r["reason"] for r in resp.json()["rejects"]] == ["filename_stem_differs_from_reference_code"]
    db_session.expire_all()
    assert db_session.query(Collection).count() == 0


@pytest.mark.unit
def test_the_import_reports_a_differing_pair_that_reaches_the_flush(client, db_session, reports_root, monkeypatch):
    """Past the item check (bypassed here), the model's flush listener refuses the
    pair; that item is rolled back and reported, and the rest are created."""
    import app.core.list_import as list_import
    from app.models.collection import Collection

    real = list_import._check_item

    def lenient(position, item, first_code, first_stem):
        reason, values = real(position, item, first_code, first_stem)
        if reason is None and values["external_id"] == "xx-test-953":
            values = dict(values, filename_stem="xx-test-954")
        return reason, values

    monkeypatch.setattr(list_import, "_check_item", lenient)
    resp = _import(client, [_item("xx-test-953", "xx-test-953"), _item("xx-test-955", "xx-test-955")])

    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert [(r["position"], r["reason"]) for r in report["rejects"]] == [
        (1, "filename_stem_differs_from_reference_code")]
    assert report["created"] == 1
    db_session.expire_all()
    assert [(c.filename_stem, c.external_id) for c in db_session.query(Collection).all()] == [
        ("xx-test-955", "xx-test-955")]


# ---------------------------------------------------------------------------
# Existing data
# ---------------------------------------------------------------------------

DIFFERING_PAIRS_SQL = (
    "SELECT id, filename_stem, external_id FROM collections "
    "WHERE filename_stem IS NOT NULL AND external_id IS NOT NULL "
    "AND filename_stem <> external_id"
)


@pytest.mark.unit
def test_the_existing_data_query_finds_a_differing_pair(db_session):
    """The read-only query that lists rows written before the rule which break it."""
    import sqlalchemy as sa

    p = _project(db_session)
    ok = _volume(db_session, p, "ok", filename_stem="co-ahr-060", external_id="co-ahr-060")
    bad = _volume(db_session, p, "bad")
    db_session.execute(sa.text("UPDATE collections SET filename_stem = 'co-ahr-061', "
                               "external_id = 'co-ahr-062' WHERE id = :id"), {"id": bad.id})
    db_session.commit()

    rows = db_session.execute(sa.text(DIFFERING_PAIRS_SQL)).all()
    assert [tuple(r) for r in rows] == [(bad.id, "co-ahr-061", "co-ahr-062")]
    assert ok.id != bad.id
