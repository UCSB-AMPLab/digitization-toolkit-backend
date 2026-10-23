"""A route that moves, renames or deletes the files an export is reading refuses
while that export runs, and an export does not start while such a route is
moving files.

On the bench unit, renaming a collection while its export ran moved the files
out from under the export, which then failed with a raw FileNotFoundError that
named an internal path (bench D3, section 6). The routes below now hold the
collections they touch for the length of their file work
(`export_jobs.hold_for_file_changes`): the hold refuses with 409 when an export
of any of them is queued or running, and `export_jobs.start` refuses an export
of a held collection.

An export covers the records whose collection_id is the exported collection,
not its sub-collections. A collection's directory is named by its id, so a
rename or a re-parent inside the project moves no file and is not refused. A
re-parent under a collection of another project moves the files of every
record in the subtree, so it is refused when any collection of the subtree is
being exported.

The export used here is a registry job whose work waits on an event, so it
stays running for as long as the test needs.

Run with: python -m pytest tests/unit/test_export_file_guards.py
"""

import threading
import time

import pytest


def _user(username, role):
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


@pytest.fixture
def as_admin(client):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _user("adm", "admin")
    return client


@pytest.fixture
def capture_settings(override_projects_root, monkeypatch):
    """Point capture.project_manager at this test's projects root (see test_relocation_rollback)."""
    import capture.project_manager as pm
    from app.core import config
    monkeypatch.setattr(pm, "settings", config.settings)
    return override_projects_root


@pytest.fixture
def running_export():
    """Start a registry job for a collection that runs until the test ends."""
    from app.core.export_jobs import export_jobs
    release = threading.Event()
    jobs = []

    def start(collection_id):
        job = export_jobs.start(collection_id, lambda j: release.wait(30))
        jobs.append(job)
        deadline = time.time() + 5
        while job.state == "queued" and time.time() < deadline:
            time.sleep(0.01)
        return job

    yield start
    release.set()
    deadline = time.time() + 5
    while any(j.state in ("queued", "running") for j in jobs) and time.time() < deadline:
        time.sleep(0.01)


@pytest.fixture
def tree(db_session, capture_settings):
    """Project P with collection Parent > Child, and collection Other; one record
    with a real image file in each of Parent and Child."""
    from capture.project_manager import image_output_dir
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    proj = Project(name="P")
    db_session.add(proj)
    db_session.commit()
    parent = Collection(name="Parent", project_id=proj.id)
    other = Collection(name="Other", project_id=proj.id)
    db_session.add_all([parent, other])
    db_session.commit()
    child = Collection(name="Child", parent_collection_id=parent.id)
    db_session.add(child)
    db_session.commit()

    out = {"project": proj, "parent": parent, "child": child, "other": other}
    for key, col, status in (("parent_rec", parent, "approved"), ("child_rec", child, "approved")):
        d = image_output_dir("P", col.id)
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{key}.jpg"
        f.write_bytes(b"bytes of " + key.encode())
        rec = Record(title=key, collection_id=col.id, status=status, capture_mode="single")
        db_session.add(rec)
        db_session.commit()
        img = RecordImage(record_id=rec.id, filename=f.name, file_path=str(f),
                          format="jpg", role="single", is_current=True)
        db_session.add(img)
        db_session.commit()
        out[key] = rec
        out[key + "_img"] = img
        out[key + "_file"] = f
    return out


def _assert_export_refusal(resp):
    assert resp.status_code == 409, resp.text
    assert "export" in resp.json()["detail"].lower()
    assert "wait for it to finish" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Collection routes
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_rename_of_an_exporting_collection_is_allowed_and_moves_no_file(as_admin, tree, running_export, db_session):
    running_export(tree["parent"].id)
    resp = as_admin.patch(f"/collections/{tree['parent'].id}", json={"name": "Renamed"})
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert db_session.get(type(tree["parent"]), tree["parent"].id).name == "Renamed"
    assert tree["parent_rec_file"].exists()
    assert db_session.get(type(tree["parent_rec_img"]), tree["parent_rec_img"].id).file_path == str(tree["parent_rec_file"])


@pytest.mark.unit
def test_rename_of_an_ancestor_of_an_exporting_collection_is_allowed(as_admin, tree, running_export):
    running_export(tree["child"].id)
    resp = as_admin.patch(f"/collections/{tree['parent'].id}", json={"name": "Renamed"})
    assert resp.status_code == 200, resp.text
    assert tree["child_rec_file"].exists()


@pytest.mark.unit
def test_reparent_within_the_project_of_an_exporting_collection_is_allowed(as_admin, tree, running_export):
    running_export(tree["child"].id)
    resp = as_admin.patch(f"/collections/{tree['child'].id}",
                          json={"parent_collection_id": tree["other"].id})
    assert resp.status_code == 200, resp.text
    assert tree["child_rec_file"].exists()


@pytest.mark.unit
def test_reparent_into_another_project_of_an_ancestor_of_an_exporting_collection_is_refused(
        as_admin, tree, running_export, db_session):
    from app.models.collection import Collection
    from app.models.project import Project
    elsewhere = Project(name="Elsewhere")
    db_session.add(elsewhere)
    db_session.commit()
    host = Collection(name="Host", project_id=elsewhere.id)
    db_session.add(host)
    db_session.commit()
    running_export(tree["child"].id)
    resp = as_admin.patch(f"/collections/{tree['parent'].id}",
                          json={"parent_collection_id": host.id})
    _assert_export_refusal(resp)
    assert tree["child_rec_file"].exists()
    db_session.expire_all()
    assert db_session.get(Collection, tree["parent"].id).parent_collection_id is None


@pytest.mark.unit
def test_description_edit_of_an_exporting_collection_is_allowed(as_admin, tree, running_export):
    running_export(tree["parent"].id)
    resp = as_admin.patch(f"/collections/{tree['parent'].id}", json={"description": "notes"})
    assert resp.status_code == 200, resp.text


@pytest.mark.unit
def test_rename_of_a_collection_whose_export_finished_is_allowed(as_admin, tree):
    from app.core.export_jobs import export_jobs
    job = export_jobs.start(tree["parent"].id, lambda j: None)
    deadline = time.time() + 5
    while job.state in ("queued", "running") and time.time() < deadline:
        time.sleep(0.01)
    resp = as_admin.patch(f"/collections/{tree['parent'].id}", json={"name": "Renamed"})
    assert resp.status_code == 200, resp.text


@pytest.mark.unit
def test_move_records_out_of_an_exporting_collection_is_refused(as_admin, tree, running_export, db_session):
    # Movable records, as after a take-back; the export guard is what refuses.
    tree["parent_rec"].status = "captured"
    db_session.commit()
    running_export(tree["parent"].id)
    resp = as_admin.post(f"/collections/{tree['parent'].id}/move-records",
                         params={"target_collection_id": tree["other"].id})
    _assert_export_refusal(resp)
    assert tree["parent_rec_file"].exists()


@pytest.mark.unit
def test_move_records_into_an_exporting_collection_is_allowed(as_admin, tree, running_export, db_session):
    # Moving files into the target never touches the files the target's export
    # reads (relocate_image never overwrites), so the target is not held.
    tree["parent_rec"].status = "captured"
    db_session.commit()
    running_export(tree["other"].id)
    resp = as_admin.post(f"/collections/{tree['parent'].id}/move-records",
                         params={"target_collection_id": tree["other"].id})
    assert resp.status_code == 200, resp.text


@pytest.mark.unit
def test_renumber_of_an_exporting_collection_is_refused(as_admin, tree, running_export):
    running_export(tree["parent"].id)
    resp = as_admin.post(f"/collections/{tree['parent'].id}/images/renumber")
    _assert_export_refusal(resp)
    assert tree["parent_rec_file"].exists()


# ---------------------------------------------------------------------------
# Record routes
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_record_delete_in_an_exporting_collection_is_refused(as_admin, tree, running_export, db_session):
    running_export(tree["parent"].id)
    resp = as_admin.delete(f"/records/{tree['parent_rec'].id}")
    _assert_export_refusal(resp)
    assert tree["parent_rec_file"].exists()
    db_session.expire_all()
    assert db_session.get(type(tree["parent_rec"]), tree["parent_rec"].id) is not None


@pytest.mark.unit
def test_image_delete_in_an_exporting_collection_is_refused(as_admin, tree, running_export):
    running_export(tree["parent"].id)
    resp = as_admin.delete(f"/records/images/{tree['parent_rec_img'].id}")
    _assert_export_refusal(resp)
    assert tree["parent_rec_file"].exists()


@pytest.mark.unit
def test_record_delete_in_another_collection_is_allowed(as_admin, tree, running_export):
    running_export(tree["child"].id)
    resp = as_admin.delete(f"/records/{tree['parent_rec'].id}")
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Project routes
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_project_rename_with_an_exporting_collection_is_refused(as_admin, tree, running_export):
    running_export(tree["child"].id)
    resp = as_admin.put(f"/projects/{tree['project'].id}", json={"name": "Q"})
    _assert_export_refusal(resp)
    assert tree["child_rec_file"].exists()


@pytest.mark.unit
def test_move_collections_out_of_a_project_with_an_exporting_collection_is_refused(
        as_admin, tree, running_export, db_session):
    from app.models.project import Project
    target = Project(name="T")
    db_session.add(target)
    db_session.commit()
    running_export(tree["child"].id)
    resp = as_admin.post(f"/projects/{tree['project'].id}/move-collections",
                         json={"target_project_id": target.id})
    _assert_export_refusal(resp)
    assert tree["child_rec_file"].exists()


# ---------------------------------------------------------------------------
# The other direction: an export does not start while files are being moved
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_export_does_not_start_while_a_collection_is_held(as_admin, tree):
    from app.core.export_jobs import export_jobs
    with export_jobs.hold_for_file_changes([tree["parent"].id]):
        resp = as_admin.post(f"/collections/{tree['parent'].id}/export")
        assert resp.status_code == 409, resp.text
        assert export_jobs.active_for(tree["parent"].id) is None


@pytest.mark.unit
def test_hold_refuses_when_an_export_is_active_and_releases_on_exit():
    from fastapi import HTTPException
    from app.core.export_jobs import ExportBlockedError, ExportJobRegistry

    reg = ExportJobRegistry()
    release = threading.Event()
    job = reg.start(7, lambda j: release.wait(5))
    try:
        with pytest.raises(HTTPException) as exc:
            with reg.hold_for_file_changes([3, 7]):
                pass
        assert exc.value.status_code == 409
    finally:
        release.set()
    deadline = time.time() + 5
    while job.state in ("queued", "running") and time.time() < deadline:
        time.sleep(0.01)

    with reg.hold_for_file_changes([7]):
        with pytest.raises(ExportBlockedError):
            reg.start(7, lambda j: None)
    # Released: an export starts again.
    job2 = reg.start(7, lambda j: None)
    assert job2.id != job.id


@pytest.mark.unit
def test_nested_holds_on_one_collection_release_independently():
    from app.core.export_jobs import ExportBlockedError, ExportJobRegistry

    reg = ExportJobRegistry()
    with reg.hold_for_file_changes([1]):
        with reg.hold_for_file_changes([1, 1]):
            pass
        with pytest.raises(ExportBlockedError):
            reg.start(1, lambda j: None)
    reg.start(1, lambda j: None)


# ---------------------------------------------------------------------------
# The worker: a source file gone during the write is the "changed" ExportError
# ---------------------------------------------------------------------------

def _wait(client, cid, job_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get(f"/collections/{cid}/export/status/{job_id}").json()
        if st["state"] in ("done", "failed"):
            return st
        time.sleep(0.05)
    raise AssertionError("export job did not finish in time")


@pytest.mark.unit
def test_source_file_removed_during_the_write_fails_as_collection_changed(
        as_admin, tree, monkeypatch, tmp_path):
    from app.core import config
    import app.core.export_service as es

    exports = tmp_path / "exp"
    exports.mkdir()
    monkeypatch.setattr(config.settings, "EXPORTS_ROOT", str(exports))

    real_write = es.write_bag_zip

    def write_after_a_move(zip_path, bag_name, bag_info, payload_files, *a, **k):
        payload_files = list(payload_files)
        # What a concurrent rename does to the first planned file.
        src = payload_files[0][0]
        src.rename(src.with_name("moved-" + src.name))
        return real_write(zip_path, bag_name, bag_info, payload_files, *a, **k)

    monkeypatch.setattr(es, "write_bag_zip", write_after_a_move)

    cid = tree["parent"].id
    resp = as_admin.post(f"/collections/{cid}/export")
    assert resp.status_code == 202, resp.text
    st = _wait(as_admin, cid, resp.json()["job_id"])

    assert st["state"] == "failed"
    assert "changed while the archive was being assembled" in st["error"]
    assert str(tree["parent_rec_file"]) not in st["error"]
    assert "No such file" not in st["error"]
    assert st["detail"]["blocking_record_ids"] == [tree["parent_rec"].id]
    assert list(exports.iterdir()) == []


@pytest.fixture
def exports(monkeypatch, tmp_path):
    from app.core import config
    d = tmp_path / "exp"
    d.mkdir()
    monkeypatch.setattr(config.settings, "EXPORTS_ROOT", str(d))
    return d


def _export_with_hook_after_fixity(as_admin, tree, monkeypatch, hook):
    """Run an export of Parent, calling hook() once the fixity pass has run.

    The fixity pass is the longest phase between planning and the write; the
    size sum and the status re-check follow it."""
    import app.core.export_service as es
    real = es.verify_images_against_manifest

    def verify_then(images):
        out = real(images)
        hook()
        return out

    monkeypatch.setattr(es, "verify_images_against_manifest", verify_then)
    cid = tree["parent"].id
    resp = as_admin.post(f"/collections/{cid}/export")
    assert resp.status_code == 202, resp.text
    return _wait(as_admin, cid, resp.json()["job_id"])


@pytest.mark.unit
def test_source_lost_after_the_fixity_pass_fails_as_collection_changed(as_admin, tree, exports, monkeypatch):
    st = _export_with_hook_after_fixity(as_admin, tree, monkeypatch, tree["parent_rec_file"].unlink)
    assert st["state"] == "failed"
    assert "changed while the archive was being assembled" in st["error"]
    assert str(tree["parent_rec_file"]) not in st["error"]
    assert st["detail"]["blocking_record_ids"] == [tree["parent_rec"].id]
    assert list(exports.iterdir()) == []


@pytest.mark.unit
def test_source_that_cannot_be_opened_fails_as_a_read_error(as_admin, tree, exports, monkeypatch):
    f = tree["parent_rec_file"]

    def replace_with_a_directory():
        f.unlink()
        f.mkdir()

    st = _export_with_hook_after_fixity(as_admin, tree, monkeypatch, replace_with_a_directory)
    assert st["state"] == "failed"
    assert "could not be read" in st["error"]
    assert "storage drive" in st["error"]
    assert str(f) not in st["error"]
    assert st["detail"]["reason"] == "read_error"
    assert st["detail"]["blocking_record_ids"] == [tree["parent_rec"].id]
    assert list(exports.iterdir()) == []


@pytest.mark.unit
def test_source_read_failing_with_eio_fails_as_a_read_error(as_admin, tree, exports, monkeypatch):
    import builtins
    import errno
    import app.core.bag_export as bag_export

    target = str(tree["parent_rec_file"])

    class _Failing:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            raise OSError(errno.EIO, "Input/output error")

    def fake_open(path, *a, **k):
        if str(path) == target:
            return _Failing()
        return builtins.open(path, *a, **k)

    monkeypatch.setattr(bag_export, "open", fake_open, raising=False)
    cid = tree["parent"].id
    resp = as_admin.post(f"/collections/{cid}/export")
    assert resp.status_code == 202, resp.text
    st = _wait(as_admin, cid, resp.json()["job_id"])
    assert st["state"] == "failed"
    assert "could not be read" in st["error"]
    assert "Input/output" not in st["error"]
    assert st["detail"]["blocking_record_ids"] == [tree["parent_rec"].id]
    assert list(exports.iterdir()) == []


@pytest.mark.unit
def test_delete_of_a_superseded_image_in_an_exporting_collection_is_allowed(
        as_admin, tree, running_export, db_session):
    # The export reads only current images.
    from app.models.record import RecordImage
    old = tree["parent_rec_file"].parent / "superseded.jpg"
    old.write_bytes(b"old")
    img = RecordImage(record_id=tree["parent_rec"].id, filename=old.name, file_path=str(old),
                      format="jpg", role="single", is_current=False)
    db_session.add(img)
    db_session.commit()
    running_export(tree["parent"].id)
    resp = as_admin.delete(f"/records/images/{img.id}")
    assert resp.status_code == 200, resp.text
