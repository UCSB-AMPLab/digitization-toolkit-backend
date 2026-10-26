"""NEH-259: records carry neither `object_typology` nor `material`.

The loose/bound distinction lives in `capture_mode`, under its own name. An
`object_typology` derived from the capture mode would duplicate it and be
wrong for a loose bifolium shot as a pair; `material` has no writer and no UI.

The export payload carries `capture_mode`, so a bag still records single vs
dual, and a schema version that tells a bag carrying `object_typology` from
one carrying `capture_mode`.

Version: 0.1.0-beta
"""

import json

import capture.service as capture_service
import capture.project_manager as project_manager_module

import pytest


DROPPED = ("object_typology", "material")


def _user(username, role):
    from app.models.user import User

    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, username, role):
    from app.main import app
    import app.api.cameras as cameras
    import app.api.records as records

    user = _user(username, role)
    for dep in (cameras.allow_contributor, records.allow_contributor,
                records.allow_read_only):
        app.dependency_overrides[dep] = lambda u=user: u
    return client


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_record_table_has_neither_dropped_column():
    from app.models.record import Record

    present = [c for c in DROPPED if c in Record.__table__.columns]
    assert present == [], f"still columns on records: {present}"


@pytest.mark.unit
def test_record_schemas_expose_neither_dropped_field():
    from app.schemas.record import RecordCreate, RecordRead, RecordUpdate

    offenders = {
        schema.__name__: [f for f in DROPPED if f in schema.model_fields]
        for schema in (RecordCreate, RecordRead, RecordUpdate)
    }
    assert all(v == [] for v in offenders.values()), offenders


# ---------------------------------------------------------------------------
# The list endpoint's query filter
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_records_list_has_no_object_typology_query_parameter(client):
    from app.main import app

    params = app.openapi()["paths"]["/records/"]["get"].get("parameters", [])
    names = [p["name"] for p in params]
    assert "object_typology" not in names, names
    # The filters that stayed, so a mistaken over-removal shows up here too.
    assert {"project_id", "collection_id", "orphaned"} <= set(names), names


@pytest.mark.unit
def test_records_list_does_not_filter_on_a_stray_object_typology_param(client, db_session):
    from app.models.record import Record

    db_session.add_all([
        Record(title="a", status="in_review", capture_mode="single"),
        Record(title="b", status="in_review", capture_mode="dual"),
    ])
    db_session.commit()
    api = _client_as(client, "op", "operator")

    resp = api.get("/records/", params={"object_typology": "book"})

    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 2, resp.json()


# ---------------------------------------------------------------------------
# The capture path
# ---------------------------------------------------------------------------

def _install_capture_fakes(monkeypatch, tmp_path):
    """Stand in for the hardware, as test_orientation_route.py does."""
    def fake_config(camera_index, resolution="high", registry=None):
        return {"camera_index": camera_index, "rotate_deg": 0}, "hw"

    def fake_single(project_name, camera_config, check_camera=False,
                    include_resolution=False, collection_name=None, stem=None, filename_index=None, root=None):
        return str(tmp_path / "cap.jpg"), "cap-1", None

    def fake_dual(project_name, cam1_config, cam2_config, check_camera=False,
                  include_resolution=False, stagger_ms=0, collection_name=None, stem=None, filename_index=None, root=None, roles=None):
        return (str(tmp_path / "cap_c0.jpg"), str(tmp_path / "cap_c1.jpg"),
                "cap-2", "pair-1")

    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    monkeypatch.setattr(capture_service, "single_capture_image", fake_single)
    monkeypatch.setattr(capture_service, "dual_capture_image", fake_dual)
    monkeypatch.setattr(project_manager_module,
                        "default_camera_config_from_registry", fake_config)


def _volume(db_session, project):
    """A capture needs a volume with a stem."""
    from app.models.collection import Collection
    v = Collection(name="V", project_id=project.id, filename_stem="co-test-v")
    db_session.add(v)
    db_session.commit()
    return v


@pytest.mark.unit
def test_single_capture_records_only_capture_mode(client, db_session, test_project,
                                                  monkeypatch, tmp_path):
    from app.models.record import Record
    _install_capture_fakes(monkeypatch, tmp_path)
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/capture",
                    json={"project_name": test_project.name, "camera_index": 0,
                          "collection_id": _volume(db_session, test_project).id})

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True, resp.text
    rec = db_session.query(Record).order_by(Record.id.desc()).first()
    assert rec is not None
    assert rec.capture_mode == "single"
    for field in DROPPED:
        assert not hasattr(rec, field), f"capture still carries {field}"


@pytest.mark.unit
def test_dual_capture_records_only_capture_mode(client, db_session, test_project,
                                                monkeypatch, tmp_path):
    from app.models.record import Record
    _install_capture_fakes(monkeypatch, tmp_path)
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/capture/dual",
                    json={"project_name": test_project.name,
                          "collection_id": _volume(db_session, test_project).id})

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True, resp.text
    rec = db_session.query(Record).order_by(Record.id.desc()).first()
    assert rec is not None
    assert rec.capture_mode == "dual"
    for field in DROPPED:
        assert not hasattr(rec, field), f"dual capture still carries {field}"


# ---------------------------------------------------------------------------
# The export payload
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_metadata_payload_carries_capture_mode_and_a_schema_version(db_session):
    from app.core.export_service import _metadata_payload
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record

    proj = Project(name="P")
    db_session.add(proj)
    db_session.commit()
    col = Collection(name="C", project_id=proj.id)
    db_session.add(col)
    db_session.commit()
    recs = [
        Record(title="loose", collection_id=col.id, status="approved", capture_mode="single"),
        Record(title="bound", collection_id=col.id, status="approved", capture_mode="dual"),
    ]
    db_session.add_all(recs)
    db_session.commit()
    for r in recs:
        db_session.refresh(r)

    payload = _metadata_payload(col, proj, recs, "20260101T000000Z")

    # Asserted against the literal, not the constant: comparing the payload to the
    # same constant the serializer reads passes for any value the constant holds.
    # `type(...) is int` rather than isinstance, because True == 1 is also True.
    assert type(payload["schema_version"]) is int
    assert payload["schema_version"] == 2
    assert [r["capture_mode"] for r in payload["records"]] == ["single", "dual"]
    for r in payload["records"]:
        for field in DROPPED:
            assert field not in r, f"{field} still in the bag payload"
    # The keys the payload is meant to keep.
    assert {"id", "sequence", "title", "description", "author", "date",
            "status", "created_by", "created_at", "images"} <= set(payload["records"][0])
    # Serialisable as written to metadata.json.
    json.dumps(payload, ensure_ascii=False)


@pytest.mark.unit
def test_create_record_response_carries_neither_dropped_field(client, db_session):
    api = _client_as(client, "op", "operator")

    resp = api.post("/records/", json={"title": "t", "capture_mode": "single"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    for field in DROPPED:
        assert field not in body, f"{field} still on the record response"
    assert body["capture_mode"] == "single"
