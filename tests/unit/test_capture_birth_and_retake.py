#!/usr/bin/env python3
"""NEH-260: what state a capture is born in, and what a retake does to it.

A record is born `captured` — it is the operator's until she promotes the
collection. A retake supersedes the current images either way, but only a
rejected record is moved into the review queue by it: a captured record stays
where it is, or every retake would promote the record it was meant to leave in
her hands.

An approved record is deliberately not retakeable. Approved is the only state
that means something may already have left the appliance, and the supersede
chain preserves the old image without recording that a bag was built from it.

The hardware is stood in for as test_record_typology_dropped.py does.

Run with: python -m pytest tests/unit/test_capture_birth_and_retake.py
"""

import capture.service as capture_service
import capture.project_manager as project_manager_module

import pytest


def _user(username, role):
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, username, role):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


def _install_capture_fakes(monkeypatch, tmp_path):
    def fake_config(camera_index, resolution="high", registry=None):
        return {"camera_index": camera_index, "rotate_deg": 0}, "hw"

    def fake_single(project_name, camera_config, check_camera=False,
                    include_resolution=False, collection_name=None):
        return str(tmp_path / "cap.jpg"), "cap-1", None

    def fake_dual(project_name, cam1_config, cam2_config, check_camera=False,
                  include_resolution=False, stagger_ms=0, collection_name=None):
        return (str(tmp_path / "cap_c0.jpg"), str(tmp_path / "cap_c1.jpg"),
                "cap-2", "pair-1")

    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    monkeypatch.setattr(capture_service, "single_capture_image", fake_single)
    monkeypatch.setattr(capture_service, "dual_capture_image", fake_dual)
    monkeypatch.setattr(project_manager_module,
                        "default_camera_config_from_registry", fake_config)


def _record_with_image(db_session, status, capture_mode="single"):
    from app.models.record import Record, RecordImage
    rec = Record(title="folio", status=status, capture_mode=capture_mode)
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)
    img = RecordImage(record_id=rec.id, filename="old.jpg", file_path="/old.jpg",
                      format="jpg", role="single", is_current=True)
    db_session.add(img)
    db_session.commit()
    db_session.refresh(img)
    return rec, img


def _capture(api, project_name, **extra):
    payload = {"project_name": project_name, "camera_index": 0}
    payload.update(extra)
    return api.post("/cameras/capture", json=payload)


def _capture_dual(api, project_name, **extra):
    payload = {"project_name": project_name}
    payload.update(extra)
    return api.post("/cameras/capture/dual", json=payload)


# ---------------------------------------------------------------------------
# The birth state
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_fresh_single_capture_is_born_captured(client, db_session, test_project,
                                                 monkeypatch, tmp_path):
    from app.models.record import Record
    _install_capture_fakes(monkeypatch, tmp_path)
    api = _client_as(client, "op", "operator")

    resp = _capture(api, test_project.name)
    assert resp.status_code == 200, resp.text

    rec = db_session.query(Record).order_by(Record.id.desc()).first()
    assert rec.status == "captured"


@pytest.mark.unit
def test_a_fresh_dual_capture_is_born_captured(client, db_session, test_project,
                                               monkeypatch, tmp_path):
    from app.models.record import Record
    _install_capture_fakes(monkeypatch, tmp_path)
    api = _client_as(client, "op", "operator")

    resp = _capture_dual(api, test_project.name)
    assert resp.status_code == 200, resp.text

    rec = db_session.query(Record).order_by(Record.id.desc()).first()
    assert rec.status == "captured"


# ---------------------------------------------------------------------------
# A retake
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_retaking_a_captured_record_supersedes_but_does_not_promote(
        client, db_session, test_project, monkeypatch, tmp_path):
    _install_capture_fakes(monkeypatch, tmp_path)
    rec, old = _record_with_image(db_session, "captured")
    api = _client_as(client, "op", "operator")

    resp = _capture(api, test_project.name, record_id=rec.id)
    assert resp.status_code == 200, resp.text

    db_session.refresh(rec)
    db_session.refresh(old)
    assert rec.status == "captured", "a retake must leave the record with the operator"
    assert old.is_current is False
    assert old.superseded_at is not None
    assert [img.filename for img in rec.images if img.is_current] != ["old.jpg"]


@pytest.mark.unit
def test_retaking_a_rejected_record_supersedes_and_promotes(
        client, db_session, test_project, monkeypatch, tmp_path):
    _install_capture_fakes(monkeypatch, tmp_path)
    rec, old = _record_with_image(db_session, "rejected")
    api = _client_as(client, "op", "operator")

    resp = _capture(api, test_project.name, record_id=rec.id)
    assert resp.status_code == 200, resp.text

    db_session.refresh(rec)
    db_session.refresh(old)
    assert rec.status == "in_review"
    assert old.is_current is False


@pytest.mark.unit
def test_retaking_a_captured_dual_record_supersedes_but_does_not_promote(
        client, db_session, test_project, monkeypatch, tmp_path):
    _install_capture_fakes(monkeypatch, tmp_path)
    rec, old = _record_with_image(db_session, "captured", capture_mode="dual")
    api = _client_as(client, "op", "operator")

    resp = _capture_dual(api, test_project.name, record_id=rec.id)
    assert resp.status_code == 200, resp.text

    db_session.refresh(rec)
    db_session.refresh(old)
    assert rec.status == "captured"
    assert old.is_current is False


@pytest.mark.unit
def test_a_captured_record_can_only_be_retaken_in_its_own_capture_mode(
        client, db_session, test_project, monkeypatch, tmp_path):
    _install_capture_fakes(monkeypatch, tmp_path)
    rec, _ = _record_with_image(db_session, "captured", capture_mode="dual")
    api = _client_as(client, "op", "operator")

    resp = _capture(api, test_project.name, record_id=rec.id)
    assert resp.status_code == 422, resp.text


@pytest.mark.unit
def test_an_approved_record_is_not_retaken(client, db_session, test_project,
                                           monkeypatch, tmp_path):
    """The supersede branch must not fire: approved work may already have left."""
    _install_capture_fakes(monkeypatch, tmp_path)
    rec, old = _record_with_image(db_session, "approved")
    api = _client_as(client, "op", "operator")

    resp = _capture(api, test_project.name, record_id=rec.id)
    assert resp.status_code == 200, resp.text

    db_session.refresh(rec)
    db_session.refresh(old)
    assert rec.status == "approved"
    assert old.is_current is True, "the approved capture must still be the current one"
    assert old.superseded_at is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
