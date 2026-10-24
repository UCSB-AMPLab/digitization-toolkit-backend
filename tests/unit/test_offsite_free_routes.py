"""The admin routes that free and restore a volume, and how every reader treats a freed file.

Freed is never missing (NEH-248): the image routes answer 410 with the key,
the integrity check counts freed files apart from missing ones, the export
refuses until the volume is restored, and the startup layout migration
neither reports a freed file as missing nor the run manifests as foreign.

    python -m pytest tests/unit/test_offsite_free_routes.py
"""

import threading
import time
from pathlib import Path

import pytest

from tests.unit.test_image_upload import STEM, TENANT, _image, _run, _volume, remote  # noqa: F401
from tests.unit.test_offsite_free import _as, _free, _image_row, _paths, _received, read_remote  # noqa: F401


def _wait(client, vol, job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/collections/{vol.id}/offsite/jobs/{job_id}").json()
        if body["state"] in ("done", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("role", ["operator", "reviewer"])
def test_only_an_admin_may_free_plan_or_restore(remote, client, db_session, override_projects_root, role):
    _as(role)
    vol, _, _ = _received(db_session, override_projects_root)
    assert client.get(f"/collections/{vol.id}/offsite/free").status_code == 403
    assert client.post(f"/collections/{vol.id}/offsite/free").status_code == 403
    assert client.post(f"/collections/{vol.id}/offsite/restore").status_code == 403
    assert _paths(override_projects_root, "f2.jpg")[0].exists()


@pytest.mark.unit
def test_the_dry_run_route(remote, client, db_session, override_projects_root):
    _as("admin")
    vol, _, _ = _received(db_session, override_projects_root)

    response = client.get(f"/collections/{vol.id}/offsite/free")

    assert response.status_code == 200, response.text
    assert response.json()["would_free"]["files"] == 3
    assert _paths(override_projects_root, "f2.jpg")[0].exists()
    assert client.get("/collections/999999/offsite/free").status_code == 404


@pytest.mark.unit
def test_free_then_restore_through_the_routes(read_remote, client, db_session, override_projects_root):
    _as("admin")
    vol, raw, jpg = _received(db_session, override_projects_root)

    response = client.post(f"/collections/{vol.id}/offsite/free")
    assert response.status_code == 202, response.text
    body = _wait(client, vol, response.json()["job_id"])
    assert body["state"] == "done", body
    assert body["result"]["freed"]["files"] == 3
    assert not _paths(override_projects_root, "f2.jpg")[0].exists()

    response = client.post(f"/collections/{vol.id}/offsite/restore")
    assert response.status_code == 202, response.text
    body = _wait(client, vol, response.json()["job_id"])
    assert body["state"] == "done", body
    assert body["result"]["restored"]["files"] == 3
    assert _paths(override_projects_root, "f2.jpg")[0].read_bytes() == b"jpeg two"
    assert client.get(f"/collections/{vol.id}/offsite/jobs/nope").status_code == 404


@pytest.mark.unit
def test_the_route_refusals_are_409_with_plain_messages(remote, client, db_session, override_projects_root):
    from app.core.export_jobs import FILES_BEING_READ_MESSAGE, export_jobs
    from app.core.offsite_free import (
        EXPORT_MESSAGE, LISTING_MESSAGE, NO_READ_REMOTE_MESSAGE, NO_STEM_MESSAGE, NOT_RECEIVED_MESSAGE)

    _as("admin")
    nostem = _volume(db_session, name="Sin raíz", stem=None)
    r = client.post(f"/collections/{nostem.id}/offsite/free")
    assert (r.status_code, r.json()["detail"]["message"]) == (409, NO_STEM_MESSAGE)

    vol, _, _ = _received(db_session, override_projects_root)
    pending = _volume(db_session, name="Tomo 2", stem="xx-test-002")
    _image(db_session, pending, override_projects_root, "g1.jpg", b"g")
    r = client.post(f"/collections/{pending.id}/offsite/free")
    assert (r.status_code, r.json()["detail"]["message"]) == (409, NOT_RECEIVED_MESSAGE)

    release = threading.Event()
    export_jobs.start(vol.id, lambda job: release.wait(10))
    try:
        r = client.post(f"/collections/{vol.id}/offsite/free")
        assert (r.status_code, r.json()["detail"]["message"]) == (409, EXPORT_MESSAGE)
    finally:
        release.set()
    while export_jobs.active_for(vol.id):
        time.sleep(0.01)

    with export_jobs.hold_for_reading([vol.id]):
        r = client.post(f"/collections/{vol.id}/offsite/free")
        assert (r.status_code, r.json()["detail"]["message"]) == (409, FILES_BEING_READ_MESSAGE)

    remote.set_modes("listing-refused")
    r = client.post(f"/collections/{vol.id}/offsite/free")
    assert (r.status_code, r.json()["detail"]["message"]) == (409, LISTING_MESSAGE)
    r = client.get(f"/collections/{vol.id}/offsite/free")
    assert (r.status_code, r.json()["detail"]["message"]) == (409, LISTING_MESSAGE)
    remote.set_modes()

    r = client.post(f"/collections/{vol.id}/offsite/restore")
    assert (r.status_code, r.json()["detail"]["message"]) == (409, NO_READ_REMOTE_MESSAGE)

    from app.core.offsite_free import NOT_IN_OWN_FOLDER_MESSAGE
    from app.models.record import RecordImage
    legacy = _volume(db_session, name="Tomo 3", stem="xx-test-003")
    img = _image(db_session, legacy, override_projects_root, "h1.jpg", b"h")
    _run(db_session)
    old = override_projects_root / "p" / "tomo3" / "images" / "main" / "h1.jpg"
    old.parent.mkdir(parents=True)
    Path(img.file_path).rename(old)
    row = db_session.get(RecordImage, img.id)
    row.file_path = str(old)
    db_session.commit()
    r = client.post(f"/collections/{legacy.id}/offsite/free")
    assert (r.status_code, r.json()["detail"]["message"]) == (409, NOT_IN_OWN_FOLDER_MESSAGE)
    assert r.json()["detail"]["images_outside_own_folder"] == 1
    assert old.exists()

    assert _paths(override_projects_root, "f2.jpg")[0].exists()


@pytest.mark.unit
def test_a_second_free_while_one_runs_is_refused(remote, client, db_session, override_projects_root, monkeypatch):
    import app.core.offsite_free as of

    _as("admin")
    vol, _, _ = _received(db_session, override_projects_root)
    started, release = threading.Event(), threading.Event()
    real = of._free_work

    def slow(*args, **kwargs):
        started.set()
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(of, "_free_work", slow)
    first = client.post(f"/collections/{vol.id}/offsite/free")
    assert first.status_code == 202
    assert started.wait(10)
    try:
        second = client.post(f"/collections/{vol.id}/offsite/free")
        assert (second.status_code, second.json()["detail"]["message"]) == (409, of.JOB_RUNNING_MESSAGE)
    finally:
        release.set()
    assert _wait(client, vol, first.json()["job_id"])["state"] == "done"


# ---------------------------------------------------------------------------
# The image routes
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_freed_file_answers_410_with_its_key_and_the_thumbnail_still_works(
        remote, client, db_session, override_projects_root):
    _as("reviewer")
    vol, raw, jpg = _received(db_session, override_projects_root)
    _free(db_session, vol)

    r = client.get(f"/records/images/{jpg.id}/file")
    assert r.status_code == 410
    detail = r.json()["detail"]
    assert detail["stored_offsite"] is True and detail["kind"] == "capture"
    assert detail["key"] == f"{TENANT}/{STEM}/captures/f2.jpg"
    assert "stored offsite" in detail["message"]

    r = client.get(f"/records/images/{jpg.id}/preview")
    assert r.status_code == 410 and r.json()["detail"]["key"] == f"{TENANT}/{STEM}/captures/f2.jpg"

    r = client.get(f"/records/images/{raw.id}/file")
    assert r.status_code == 410 and r.json()["detail"]["key"] == f"{TENANT}/{STEM}/captures/f1.CR2"
    r = client.get(f"/records/images/{raw.id}/preview")
    assert r.status_code == 410
    assert r.json()["detail"]["kind"] == "preview"
    assert r.json()["detail"]["key"] == f"{TENANT}/{STEM}/previews/f1_preview.jpg"

    for image in (raw, jpg):
        r = client.get(f"/records/images/{image.id}/thumbnail")
        assert r.status_code == 200 and r.content.startswith(b"thumb ")

    listed = client.get(f"/records/images/{raw.id}").json()
    assert listed["master_freed_at"] is not None and listed["preview_freed_at"] is not None


@pytest.mark.unit
def test_a_raw_master_freed_by_hand_still_shows_its_preview(remote, client, db_session, override_projects_root):
    from app.core.offsite_free import mark_freed

    _as("reviewer")
    vol, raw, _ = _received(db_session, override_projects_root)
    _paths(override_projects_root, "f1.CR2")[0].unlink()
    mark_freed(db_session, STEM, apply=True)

    assert client.get(f"/records/images/{raw.id}/file").status_code == 410
    r = client.get(f"/records/images/{raw.id}/preview")
    assert r.status_code == 200 and r.content == b"preview one"


@pytest.mark.unit
def test_a_freed_image_without_a_thumbnail_answers_410_not_404(remote, client, db_session, override_projects_root):
    _as("reviewer")
    vol, raw, jpg = _received(db_session, override_projects_root)
    _free(db_session, vol)
    _paths(override_projects_root, "f2.jpg")[2].unlink()

    r = client.get(f"/records/images/{jpg.id}/thumbnail")
    assert r.status_code == 410 and r.json()["detail"]["stored_offsite"] is True


@pytest.mark.unit
def test_a_missing_file_that_was_not_freed_is_still_a_404(remote, client, db_session, override_projects_root):
    _as("reviewer")
    vol, raw, jpg = _received(db_session, override_projects_root)
    _paths(override_projects_root, "f2.jpg")[0].unlink()
    assert client.get(f"/records/images/{jpg.id}/file").status_code == 404


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _approve(db, vol):
    from app.models.record import Record

    for rec in db.query(Record).filter(Record.collection_id == vol.id):
        rec.status = "approved"
    db.commit()


@pytest.mark.unit
def test_the_export_of_a_volume_with_freed_files_is_refused(remote, client, db_session, override_projects_root):
    _as("admin")
    vol, _, _ = _received(db_session, override_projects_root)
    _approve(db_session, vol)
    _free(db_session, vol)

    r = client.post(f"/collections/{vol.id}/export")

    assert r.status_code == 409
    assert "Restore the volume" in r.json()["detail"]["message"]


@pytest.mark.unit
def test_the_export_worker_refuses_freed_files_too(remote, db_session, override_projects_root):
    from app.core.export_jobs import ExportError, ExportJob
    from app.core.export_service import _do_export

    vol, _, jpg = _received(db_session, override_projects_root)
    _approve(db_session, vol)
    _free(db_session, vol)

    with pytest.raises(ExportError) as exc:
        _do_export(db_session, ExportJob(vol.id), vol.id)
    assert exc.value.detail["reason"] == "freed_files"
    assert jpg.id in exc.value.detail["freed_image_ids"]


@pytest.mark.unit
def test_the_fixity_gate_skips_a_freed_file(remote, db_session, override_projects_root):
    from app.core.integrity import verify_images_against_manifest
    from app.models.record import RecordImage

    vol, _, _ = _received(db_session, override_projects_root)
    _free(db_session, vol)
    db_session.expire_all()
    assert verify_images_against_manifest(db_session.query(RecordImage).all()) == []


# ---------------------------------------------------------------------------
# The integrity scan
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_integrity_scan_counts_freed_files_apart_from_missing_ones(remote, db_session, override_projects_root):
    from app.core.integrity import run_integrity_check

    vol, raw, jpg = _received(db_session, override_projects_root)
    _free(db_session, vol)

    report = run_integrity_check(db_session)

    assert report["summary"]["missing_files"] == 0
    assert report["summary"]["freed_files"] == 2
    assert {f["record_image_id"] for f in report["freed_files"]} == {raw.id, jpg.id}
    assert report["summary"]["orphan_files"] == 0
    assert report["summary"]["ok"] is True


@pytest.mark.unit
def test_a_kept_preview_of_a_freed_master_is_not_an_orphan(remote, db_session, override_projects_root):
    from app.core.integrity import run_integrity_check
    from app.core.offsite_free import mark_freed

    vol, raw, _ = _received(db_session, override_projects_root)
    _paths(override_projects_root, "f1.CR2")[0].unlink()
    mark_freed(db_session, STEM, apply=True)

    report = run_integrity_check(db_session)

    assert report["summary"]["orphan_files"] == 0, report["orphan_files"]
    assert report["summary"]["missing_files"] == 0
    assert report["summary"]["ok"] is True


# ---------------------------------------------------------------------------
# The startup layout migration
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_run_manifests_are_the_volumes_own(remote, db_session, override_projects_root):
    from app.core.volume_layout import _foreign_entries

    vol, _, _ = _received(db_session, override_projects_root)
    result = _free(db_session, vol)
    assert Path(result["manifest"]).is_file()

    assert _foreign_entries(db_session, override_projects_root / STEM, vol.id) == []


@pytest.mark.unit
def test_an_out_of_place_freed_master_is_reported_as_freed_not_missing(remote, db_session, override_projects_root):
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    vol, raw, jpg = _received(db_session, override_projects_root)
    _free(db_session, vol)
    # A freed row whose stored path is outside the volume's captures/ (a
    # file freed before the layout migration moved it).
    old = override_projects_root / "p" / "tomo" / "images" / "main" / "f2.jpg"
    row = db_session.get(RecordImage, jpg.id)
    row.file_path = str(old)
    db_session.commit()

    report = migrate_volume_layout(db_session, dry_run=True)

    skipped = {s["image_id"]: s["reason"] for s in report["skipped"]}
    assert skipped.get(jpg.id) == "freed", report["skipped"]
