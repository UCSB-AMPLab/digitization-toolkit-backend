"""A volume given a stem while the backend runs keeps its files in the name-based layout until the next startup.

Until then it is held against its export and its files are moved like a
volume without a stem: a project rename moves them with the tree and the
rows follow, and a re-parent, a volume rename or a move to another project
moves them to its name-based directory there. Captures it made since in
`<stem>/` stay where they are. Whether a volume's files are still in the old
layout is decided by storage_ops.collections_in_old_layout: it has no stem,
or it has an image under the projects root outside `<stem>/`.
"""

from pathlib import Path

import pytest

from tests.unit.test_volume_layout_by_stem import (  # noqa: F401
    STEMS, _as_admin, capture_settings, data_dir, legacy, running_export,
)


@pytest.mark.unit
def test_a_project_rename_moves_the_files_of_a_stemmed_volume_not_yet_migrated_and_its_rows_follow(
        client, db_session, legacy, override_projects_root):
    """a1 has a stem but its files are still in proj/fondo_a (no startup since)."""
    from app.models.record import RecordImage

    before = Path(db_session.get(RecordImage, legacy["i1"].id).file_path)
    r = _as_admin(client).put(f"/projects/{legacy['proj'].id}", json={"name": "Proyecto Dos"})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    after = Path(db_session.get(RecordImage, legacy["i1"].id).file_path)
    assert after == override_projects_root / "proyecto_dos" / before.relative_to(override_projects_root / "proj")
    assert after.read_bytes() == b"bytes of IMG_0001.CR2"
    assert not before.exists()


@pytest.mark.unit
def test_a_project_rename_is_refused_while_a_not_yet_migrated_stemmed_volume_exports(
        client, db_session, legacy, running_export):
    job = running_export(legacy["a1"].id)
    assert job.state == "running"
    r = _as_admin(client).put(f"/projects/{legacy['proj'].id}", json={"name": "Proyecto Dos"})
    assert r.status_code == 409, (r.status_code, r.text)


@pytest.mark.unit
def test_a_project_rename_is_not_held_by_a_migrated_stemmed_volume(client, db_session, legacy, running_export):
    from app.core.volume_layout import migrate_volume_layout

    migrate_volume_layout(db_session)
    running_export(legacy["a1"].id)
    r = _as_admin(client).put(f"/projects/{legacy['proj'].id}", json={"name": "Proyecto Dos"})
    assert r.status_code == 200, r.text


@pytest.mark.unit
def test_which_volumes_are_still_in_the_old_layout(db_session, legacy):
    from app.core.storage_ops import collections_in_old_layout
    from app.core.volume_layout import migrate_volume_layout

    ids = [legacy[k].id for k in ("a1", "a2", "tomo", "u")]
    assert sorted(collections_in_old_layout(db_session, ids)) == sorted(ids)
    migrate_volume_layout(db_session)
    assert collections_in_old_layout(db_session, ids) == [legacy["u"].id]


@pytest.mark.unit
def test_moving_a_not_yet_migrated_stemmed_volume_to_another_project_moves_its_old_files_only(
        client, db_session, legacy, override_projects_root):
    """Its old files go to its name-based directory in the target; a capture already in <stem>/ stays."""
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    vol = override_projects_root / STEMS["a1"] / "captures"
    vol.mkdir(parents=True)
    new = vol / "NEW.jpg"
    new.write_bytes(b"new")
    rec = Record(title="n", collection_id=legacy["a1"].id, status="captured", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    img = RecordImage(record_id=rec.id, filename=new.name, file_path=str(new), format="jpg", role="single")
    db_session.add(img)
    db_session.commit()
    q = Project(name="Otro")
    db_session.add(q)
    db_session.commit()

    r = _as_admin(client).post(f"/projects/{legacy['proj'].id}/move-collections", json={"target_project_id": q.id})

    assert r.status_code == 200, r.text
    db_session.expire_all()
    i1 = Path(db_session.get(RecordImage, legacy["i1"].id).file_path)
    assert i1.parent == (override_projects_root / "otro" / "fondo_a" / "images" / "main").resolve()
    assert i1.read_bytes() == b"bytes of IMG_0001.CR2"
    assert db_session.get(RecordImage, img.id).file_path == str(new) and new.exists()


@pytest.mark.unit
def test_renaming_a_not_yet_migrated_stemmed_volume_moves_its_old_files_and_the_rows_follow(
        client, db_session, legacy, override_projects_root):
    from app.models.record import RecordImage

    r = _as_admin(client).patch(f"/collections/{legacy['tomo'].id}", json={"name": "Tomo Uno"})

    assert r.status_code == 200, r.text
    db_session.expire_all()
    i4 = Path(db_session.get(RecordImage, legacy["i4"].id).file_path)
    assert i4.parent == (override_projects_root / "proj" / "tomo_uno" / "images" / "main").resolve()
    assert i4.read_bytes() == b"bytes of 0001.jpg"


@pytest.mark.unit
def test_a_rename_of_a_not_yet_migrated_stemmed_volume_is_refused_while_it_exports(
        client, db_session, legacy, running_export):
    running_export(legacy["tomo"].id)
    r = _as_admin(client).patch(f"/collections/{legacy['tomo'].id}", json={"name": "Tomo Uno"})
    assert r.status_code == 409, r.text
