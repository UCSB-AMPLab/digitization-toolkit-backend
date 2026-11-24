"""DELETE /projects/{id} refuses with a code and a message.

Every refusal of the route is a detail object {"code", "message"}; the
message is the sentence the interface shows an administrator. A refusal
deletes nothing, and a delete of an empty project is unchanged.

    python -m pytest tests/unit/test_project_delete_codes.py

Version: 0.3.0
"""

import pytest

from tests.unit.test_capture_manifest_export import _as, tenant  # noqa: F401  (tenant is a fixture)
from tests.unit.test_testing_space_schema import _record


def _project(db, name="Para borrar", testing=False):
    from app.models.project import Project
    p = Project(name=name, testing=testing)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _delete(client, project):
    return _as(client, "admin").delete(f"/projects/{project.id}")


def _still_there(db, project):
    from app.models.project import Project
    db.expire_all()
    return db.get(Project, project.id) is not None


def _refused(resp, status, code, message):
    assert resp.status_code == status, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == code, detail
    assert detail["message"] == message, detail
    return detail


@pytest.mark.unit
def test_a_project_with_volumes_and_records_is_project_not_empty(client, db_session):
    from app.models.collection import Collection
    p = _project(db_session)
    db_session.add(Collection(name="Tomo", project_id=p.id))
    db_session.commit()
    _record(db_session, project_id=p.id, title="R1")
    _record(db_session, project_id=p.id, title="R2")
    detail = _refused(
        _delete(client, p), 409, "project_not_empty",
        "Cannot delete a non-empty project: 1 collection(s) and 2 record(s) remain. Move or delete them first.")
    assert detail["collections"] == 1 and detail["records"] == 2
    assert _still_there(db_session, p)


@pytest.mark.unit
def test_files_a_surviving_record_holds_are_project_files_held(client, db_session):
    from capture.project_manager import project_capture_root
    from app.models.record import RecordImage
    p = _project(db_session)
    directory = project_capture_root(p.name)
    directory.mkdir(parents=True)
    image = directory / "a.jpg"
    image.write_bytes(b"x")
    record = _record(db_session, project_id=_project(db_session, name="Otro").id, title="Elsewhere")
    db_session.add(RecordImage(record_id=record.id, filename="a.jpg", file_path=str(image), format="JPEG"))
    db_session.commit()
    _refused(_delete(client, p), 409, "project_files_held",
             "Project directory still holds files referenced by other records. Move those records first.")
    assert _still_there(db_session, p) and image.exists()


@pytest.mark.unit
def test_another_manifest_in_the_tree_is_project_manifest_foreign(client, db_session):
    from capture.project_manager import project_capture_root
    p = _project(db_session)
    other = project_capture_root(p.name) / "sub" / "metadata" / "manifest.jsonl"
    other.parent.mkdir(parents=True)
    other.write_text("{}\n")
    _refused(_delete(client, p), 409, "project_manifest_foreign",
             "Project directory holds a capture manifest other than its own. Move it out first.")
    assert _still_there(db_session, p) and other.exists()


@pytest.mark.unit
def test_a_manifest_that_cannot_be_kept_is_project_manifest_keep_failed(client, db_session, monkeypatch):
    from capture.project_manager import project_capture_root
    from app.api import projects
    p = _project(db_session)
    project_capture_root(p.name).mkdir(parents=True)

    def refuse(directory):
        raise OSError("read-only")
    monkeypatch.setattr(projects, "retire_project_manifest", refuse)
    _refused(_delete(client, p), 500, "project_manifest_keep_failed",
             "Failed to keep the project's capture manifest; nothing was deleted")
    assert _still_there(db_session, p) and project_capture_root(p.name).is_dir()


@pytest.mark.unit
def test_files_that_cannot_be_removed_are_project_files_remove_failed(client, db_session, monkeypatch):
    from capture.project_manager import project_capture_root
    from app.api import projects
    p = _project(db_session)
    project_capture_root(p.name).mkdir(parents=True)

    def refuse(directory):
        raise OSError("busy")
    monkeypatch.setattr(projects, "remove_tree", refuse)
    _refused(_delete(client, p), 500, "project_files_remove_failed",
             "Failed to remove project files from disk")
    assert _still_there(db_session, p)


@pytest.mark.unit
def test_an_unreadable_drive_for_a_testing_project_stays_a_503_with_a_string_detail(client, db_session, monkeypatch):
    from app.core import testing_space
    p = _project(db_session, name=testing_space.TESTING_PROJECT_PREFIX + "Pruebas", testing=True)
    monkeypatch.setattr(testing_space, "remove_project_directory", lambda db, name: None)
    resp = _delete(client, p)
    assert resp.status_code == 503, resp.text
    assert "cannot be read now" in resp.json()["detail"]
    assert _still_there(db_session, p)


@pytest.mark.unit
def test_an_empty_project_is_deleted(client, db_session):
    from capture.project_manager import project_capture_root
    p = _project(db_session)
    project_capture_root(p.name).mkdir(parents=True)
    resp = _delete(client, p)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"detail": "project deleted"}
    assert not _still_there(db_session, p)
    assert not project_capture_root(p.name).exists()
