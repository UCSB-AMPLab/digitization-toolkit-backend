"""A volume's stem and a project's directory name share `<projects root>/`, so they may never be equal.

A stem is refused when a project's directory (secure_project_filename) has
its name, and a project name is refused when its directory would have an
existing stem's. The check is a before_flush listener in
app.models.collection, so every writer reaches it: the API routes answer
409, and any other writer (the list import) gets StemDirectoryCollisionError.
The project routes check before they move a tree. The startup migration
refuses to move a volume into a directory that is a live project tree.
"""

from pathlib import Path

import pytest
from sqlalchemy import text

from tests.unit.test_volume_layout_by_stem import _as_admin, capture_settings, data_dir  # noqa: F401


def _project(db, name):
    from app.models.project import Project

    p = Project(name=name)
    db.add(p)
    db.commit()
    return p


def _volume(db, project, name="V", **kw):
    from app.models.collection import Collection

    c = Collection(name=name, project_id=project.id, **kw)
    db.add(c)
    db.commit()
    return c


@pytest.mark.unit
def test_creating_a_volume_whose_stem_is_a_project_directory_is_refused(client, db_session, capture_settings):
    from app.models.collection import Collection

    p = _project(db_session, "CO-AHR-T055")

    resp = _as_admin(client).post("/collections/", json={"name": "T", "project_id": p.id,
                                                         "filename_stem": "co-ahr-t055"})

    assert resp.status_code == 409, resp.text
    assert "co-ahr-t055" in resp.json()["detail"]["message"]
    assert db_session.query(Collection).count() == 0


@pytest.mark.unit
def test_setting_a_stem_that_is_a_project_directory_is_refused(client, db_session, capture_settings):
    from app.models.collection import Collection

    p = _project(db_session, "co-ahr-t055")
    v = _volume(db_session, p)

    resp = _as_admin(client).patch(f"/collections/{v.id}", json={"filename_stem": "co-ahr-t055"})

    assert resp.status_code == 409, resp.text
    db_session.expire_all()
    assert db_session.get(Collection, v.id).filename_stem is None


@pytest.mark.unit
def test_a_writer_outside_the_routes_gets_the_error_at_flush(db_session, capture_settings):
    """The list import creates volumes through the model, so the listener is what refuses it."""
    from app.models.collection import Collection, StemDirectoryCollisionError

    p = _project(db_session, "xx-test-001")
    db_session.add(Collection(name="T", project_id=p.id, filename_stem="xx-test-001"))

    with pytest.raises(StemDirectoryCollisionError):
        db_session.flush()
    db_session.rollback()


@pytest.mark.unit
def test_a_stem_that_matches_no_project_directory_is_accepted(client, db_session, capture_settings):
    p = _project(db_session, "Proyecto Rionegro")

    resp = _as_admin(client).post("/collections/", json={"name": "T", "project_id": p.id,
                                                         "filename_stem": "proyecto-rionegro"})

    # "Proyecto Rionegro" lives in proyecto_rionegro/, not proyecto-rionegro/.
    assert resp.status_code == 201, resp.text


@pytest.mark.unit
@pytest.mark.parametrize("name", ["xx-test-001", "XX-Test-001", ".xx-test-001"])
def test_creating_a_project_whose_directory_is_a_stem_is_refused(client, db_session, capture_settings, name):
    from app.models.project import Project

    p = _project(db_session, "P")
    _volume(db_session, p, filename_stem="xx-test-001")

    resp = _as_admin(client).post("/projects/", json={"name": name})

    assert resp.status_code == 409, resp.text
    assert db_session.query(Project).filter(Project.name == name).count() == 0


@pytest.mark.unit
def test_renaming_a_project_to_a_stem_is_refused_before_its_tree_moves(client, db_session, capture_settings,
                                                                       override_projects_root):
    from app.models.project import Project

    p = _project(db_session, "P")
    _volume(db_session, p, filename_stem="xx-test-001")
    tree = override_projects_root / "p" / "packages"
    tree.mkdir(parents=True)

    resp = _as_admin(client).put(f"/projects/{p.id}", json={"name": "xx-test-001"})

    assert resp.status_code == 409, resp.text
    assert tree.is_dir()
    assert not (override_projects_root / "xx-test-001").exists()
    db_session.expire_all()
    assert db_session.get(Project, p.id).name == "P"


@pytest.mark.unit
def test_a_writer_outside_the_routes_cannot_name_a_project_after_a_stem(db_session, capture_settings):
    from app.models.collection import StemDirectoryCollisionError
    from app.models.project import Project

    p = _project(db_session, "P")
    _volume(db_session, p, filename_stem="xx-test-001")
    db_session.add(Project(name="XX-TEST-001"))

    with pytest.raises(StemDirectoryCollisionError):
        db_session.flush()
    db_session.rollback()


# ---------------------------------------------------------------------------
# The startup migration
# ---------------------------------------------------------------------------

def _legacy_image(db, volume, path: Path):
    from app.models.record import Record, RecordImage

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"page")
    rec = Record(title="r", collection_id=volume.id, status="captured", capture_mode="single")
    db.add(rec)
    db.commit()
    img = RecordImage(record_id=rec.id, filename=path.name, file_path=str(path), format="jpg", role="single")
    db.add(img)
    db.commit()
    return img


@pytest.mark.unit
def test_the_migration_refuses_a_stem_whose_directory_is_a_project_in_the_database(
        db_session, capture_settings, data_dir, override_projects_root):
    """A collision written before the check existed: the volume is reported and left where it is."""
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    p = _project(db_session, "Proj")
    v = _volume(db_session, p, name="Tomo")
    img = _legacy_image(db_session, v, override_projects_root / "proj" / "tomo" / "images" / "main" / "f.jpg")
    _project(db_session, "Other")
    db_session.execute(text("UPDATE collections SET filename_stem = 'other' WHERE id = :id"), {"id": v.id})
    db_session.commit()

    report = migrate_volume_layout(db_session)

    assert report["moved"] == []
    assert {"image_id": img.id, "path": str(Path(img.file_path).resolve()),
            "reason": "stem_dir_is_project_tree"} in report["skipped"]
    db_session.expire_all()
    assert db_session.get(RecordImage, img.id).file_path == img.file_path
    assert not (override_projects_root / "other").exists()


@pytest.mark.unit
@pytest.mark.parametrize("marker", ["packages", "images", "metadata/project_manifest.jsonl"])
def test_the_migration_refuses_a_stem_whose_directory_holds_a_project_tree_on_disk(
        db_session, capture_settings, data_dir, override_projects_root, marker):
    """A project tree left on disk by a project no longer in the database counts as live too."""
    from app.core.volume_layout import migrate_volume_layout

    p = _project(db_session, "Proj")
    v = _volume(db_session, p, name="Tomo", filename_stem="xx-test-001")
    img = _legacy_image(db_session, v, override_projects_root / "proj" / "tomo" / "images" / "main" / "f.jpg")
    m = override_projects_root / "xx-test-001" / marker
    if m.suffix:
        m.parent.mkdir(parents=True)
        m.write_text("{}\n")
    else:
        m.mkdir(parents=True)

    report = migrate_volume_layout(db_session)

    assert report["moved"] == []
    assert [s["reason"] for s in report["skipped"] if s["image_id"] == img.id] == ["stem_dir_is_project_tree"]
    assert Path(img.file_path).exists()
