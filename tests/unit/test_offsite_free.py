"""Freeing a received volume's files from the card, restoring them, and marking hand-cleared ones.

The bucket is the local directory remote of tests/unit/test_image_upload.py,
behind the same rclone wrapper, so every listing and copy is a real rclone
call. Restore reads through OFFSITE_READ_REMOTE, here the same directory.

    python -m pytest tests/unit/test_offsite_free.py
"""

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from tests.unit.test_image_upload import STEM, TENANT, _image, _rows, _run, _volume, remote  # noqa: F401


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _as(role):
    from app.api.auth import get_current_user
    from app.main import app
    from app.models.user import User

    app.dependency_overrides[get_current_user] = lambda: User(
        username=role, email=f"{role}@example.com", hashed_password="x", role=role, is_active=True)


@pytest.fixture
def read_remote(remote, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "OFFSITE_READ_REMOTE", str(remote.root))
    return remote


def _capture_record(db, root, image, files: dict):
    """Give `image` a capture id and write its capture record, with a sha256 for each of `files` (relative path -> bytes)."""
    import uuid

    image.capture_id = str(uuid.uuid4())
    db.commit()
    metadata = root / STEM / "metadata"
    metadata.mkdir(parents=True, exist_ok=True)
    line = {"capture_id": image.capture_id,
            "files": [{"relative_path": rel, "sha256": hashlib.sha256(data).hexdigest()}
                      for rel, data in files.items()]}
    with open(metadata / "manifest.jsonl", "a") as fh:
        fh.write(json.dumps(line) + "\n")


def _received(db, root, *, preview_digest=True):
    """A volume with a RAW image (master, preview, thumbnail) and a JPEG image, every object verified.

    The RAW image's capture record carries its master's sha256 and, unless
    `preview_digest` is false, its preview's: freeing leaves a preview
    without one on the card.
    """
    vol = _volume(db)
    raw = _image(db, vol, root, "f1.CR2", b"raw one", preview=b"preview one")
    files = {"captures/f1.CR2": b"raw one"}
    if preview_digest:
        files["previews/f1_preview.jpg"] = b"preview one"
    _capture_record(db, root, raw, files)
    jpg = _image(db, vol, root, "f2.jpg", b"jpeg two")
    result = _run(db)
    assert result["status"] == "done" and result["uploaded"] == 3, result
    return vol, raw, jpg


def _paths(root, name):
    base = root / STEM
    stem = Path(name).stem
    return base / "captures" / name, base / "previews" / f"{stem}_preview.jpg", base / "thumbnails" / f"{stem}_thumb.jpg"


def _image_row(db, image_id):
    from app.models.record import RecordImage

    db.expire_all()
    return db.get(RecordImage, image_id)


def _free(db, vol):
    from app.core.offsite_free import free_volume

    return free_volume(db, vol.id, actor="adm")


def _restore(db, vol):
    from app.core.offsite_free import restore_volume

    return restore_volume(db, vol.id, actor="adm")


def _logs(db, action):
    from app.models.system_log import SystemLog

    db.expire_all()
    return db.query(SystemLog).filter(SystemLog.action == action).all()


# ---------------------------------------------------------------------------
# Freeing
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_freeing_removes_the_masters_and_previews_and_keeps_the_thumbnails(remote, db_session, override_projects_root):
    vol, raw, jpg = _received(db_session, override_projects_root)
    raw_master, raw_preview, raw_thumb = _paths(override_projects_root, "f1.CR2")
    jpg_master, _, jpg_thumb = _paths(override_projects_root, "f2.jpg")

    result = _free(db_session, vol)

    assert result["refused"] == [], result
    assert result["freed"] == {"files": 3, "bytes": len(b"raw one") + len(b"preview one") + len(b"jpeg two")}
    assert not raw_master.exists() and not raw_preview.exists() and not jpg_master.exists()
    assert raw_thumb.read_bytes() == b"thumb f1.CR2" and jpg_thumb.read_bytes() == b"thumb f2.jpg"
    r, j = _image_row(db_session, raw.id), _image_row(db_session, jpg.id)
    assert r.master_freed_at is not None and r.master_freed_bytes == len(b"raw one")
    assert r.preview_freed_at is not None and r.preview_freed_bytes == len(b"preview one")
    assert j.master_freed_at is not None and j.preview_freed_at is None
    # The bucket is untouched: freeing lists and never writes there.
    assert len(remote.files()) == 3
    assert remote.calls("copyto")[3:] == []


@pytest.mark.unit
def test_freeing_writes_a_manifest_and_a_system_log_event(remote, db_session, override_projects_root):
    vol, raw, jpg = _received(db_session, override_projects_root)

    result = _free(db_session, vol)

    manifest = Path(result["manifest"])
    assert manifest.parent == override_projects_root / STEM / "metadata"
    lines = [json.loads(line) for line in manifest.read_text().splitlines()]
    assert {(l["image_id"], l["kind"]) for l in lines} == {(raw.id, "capture"), (raw.id, "preview"), (jpg.id, "capture")}
    by_kind = {(l["image_id"], l["kind"]): l for l in lines}
    first = by_kind[(raw.id, "capture")]
    assert first["path"] == str(_paths(override_projects_root, "f1.CR2")[0].resolve())
    assert first["bytes"] == len(b"raw one") and first["sha1"] == _sha1(b"raw one")
    assert first["key"] == f"{TENANT}/{STEM}/captures/f1.CR2"
    events = _logs(db_session, "volume_freed")
    assert len(events) == 1
    assert events[0].subject == STEM and events[0].actor == "adm"
    assert "files=3" in events[0].detail and f"bytes={len(b'raw one') + len(b'preview one') + len(b'jpeg two')}" in events[0].detail


@pytest.mark.unit
def test_a_bucket_sha1_that_differs_leaves_that_file_and_reports_it(remote, db_session, override_projects_root):
    vol, raw, jpg = _received(db_session, override_projects_root)
    remote.put(f"{TENANT}/{STEM}/captures/f2.jpg", b"someone else's")
    jpg_master = _paths(override_projects_root, "f2.jpg")[0]

    result = _free(db_session, vol)

    assert jpg_master.read_bytes() == b"jpeg two"
    assert _image_row(db_session, jpg.id).master_freed_at is None
    assert [(r["image_id"], r["kind"], r["reason"]) for r in result["refused"]] == [(jpg.id, "capture", "bucket_sha1_differs")]
    assert result["refused"][0]["listed_sha1"] == _sha1(b"someone else's")
    assert result["freed"]["files"] == 2


@pytest.mark.unit
def test_a_local_sha1_that_differs_leaves_that_file_and_reports_it(remote, db_session, override_projects_root):
    vol, raw, jpg = _received(db_session, override_projects_root)
    raw_master = _paths(override_projects_root, "f1.CR2")[0]
    raw_master.write_bytes(b"raw ONE")

    result = _free(db_session, vol)

    assert raw_master.read_bytes() == b"raw ONE"
    assert _image_row(db_session, raw.id).master_freed_at is None
    assert [(r["image_id"], r["kind"], r["reason"]) for r in result["refused"]] == [(raw.id, "capture", "local_sha1_differs")]
    assert result["refused"][0]["local_sha1"] == _sha1(b"raw ONE")
    # The preview is its own object, and it passed.
    assert _image_row(db_session, raw.id).preview_freed_at is not None


@pytest.mark.unit
def test_an_object_missing_from_the_bucket_is_not_freed(remote, db_session, override_projects_root):
    vol, raw, jpg = _received(db_session, override_projects_root)
    (remote.root / TENANT / STEM / "previews" / "f1_preview.jpg").unlink()

    result = _free(db_session, vol)

    assert _paths(override_projects_root, "f1.CR2")[1].exists()
    assert [(r["kind"], r["reason"]) for r in result["refused"]] == [("preview", "not_in_bucket")]


@pytest.mark.unit
def test_freeing_twice_frees_nothing_the_second_time(remote, db_session, override_projects_root):
    vol, _, _ = _received(db_session, override_projects_root)
    _free(db_session, vol)

    again = _free(db_session, vol)

    assert again["freed"] == {"files": 0, "bytes": 0} and again["refused"] == []
    assert again["manifest"] is None


@pytest.mark.unit
def test_the_dry_run_reports_and_changes_nothing(remote, db_session, override_projects_root):
    from app.core.offsite_free import plan_free

    vol, raw, jpg = _received(db_session, override_projects_root)
    remote.put(f"{TENANT}/{STEM}/captures/f2.jpg", b"other")

    plan = plan_free(db_session, vol.id)

    assert plan["would_free"] == {"files": 2, "bytes": len(b"raw one") + len(b"preview one")}
    assert {(f["image_id"], f["kind"]) for f in plan["files"]} == {(raw.id, "capture"), (raw.id, "preview")}
    assert [(r["image_id"], r["reason"]) for r in plan["refused"]] == [(jpg.id, "bucket_sha1_differs")]
    assert plan["local_sha1_checked"] is False
    assert all(p.exists() for p in _paths(override_projects_root, "f1.CR2"))
    assert _image_row(db_session, raw.id).master_freed_at is None


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

def _refusal(fn):
    from app.core.offsite_free import FreeRefused

    with pytest.raises(FreeRefused) as exc:
        fn()
    return exc.value


@pytest.mark.unit
def test_a_volume_without_a_stem_is_refused(remote, db_session, override_projects_root):
    vol = _volume(db_session, stem=None)
    refused = _refusal(lambda: _free(db_session, vol))
    assert refused.status_code == 409 and "stem" in refused.message


@pytest.mark.unit
def test_a_volume_not_yet_received_is_refused(remote, db_session, override_projects_root):
    vol = _volume(db_session)
    _image(db_session, vol, override_projects_root, "f1.jpg", b"one")
    refused = _refusal(lambda: _free(db_session, vol))
    assert refused.status_code == 409 and "verified" in refused.message
    assert _paths(override_projects_root, "f1.jpg")[0].exists()


@pytest.mark.unit
def test_a_volume_with_an_export_queued_or_running_is_refused(remote, db_session, override_projects_root):
    from app.core.export_jobs import export_jobs

    vol, _, _ = _received(db_session, override_projects_root)
    release = threading.Event()
    export_jobs.start(vol.id, lambda job: release.wait(10))
    try:
        refused = _refusal(lambda: _free(db_session, vol))
    finally:
        release.set()
    assert refused.status_code == 409 and "export" in refused.message.lower()
    assert _paths(override_projects_root, "f2.jpg")[0].exists()


@pytest.mark.unit
def test_a_volume_whose_files_are_being_read_is_refused(remote, db_session, override_projects_root):
    from app.core.export_jobs import FILES_BEING_READ_MESSAGE, export_jobs

    vol, _, _ = _received(db_session, override_projects_root)
    with export_jobs.hold_for_reading([vol.id]):
        refused = _refusal(lambda: _free(db_session, vol))
    assert refused.status_code == 409 and refused.message == FILES_BEING_READ_MESSAGE


@pytest.mark.unit
def test_an_unanswered_listing_refuses_and_frees_nothing(remote, db_session, override_projects_root):
    vol, raw, _ = _received(db_session, override_projects_root)
    remote.set_modes("listing-refused")

    refused = _refusal(lambda: _free(db_session, vol))

    assert refused.status_code == 409 and "listed" in refused.message
    assert all(p.exists() for p in _paths(override_projects_root, "f1.CR2"))
    assert _image_row(db_session, raw.id).master_freed_at is None


@pytest.mark.unit
def test_the_hold_is_released_after_a_refusal(remote, db_session, override_projects_root):
    from app.core.export_jobs import export_jobs

    vol, _, _ = _received(db_session, override_projects_root)
    remote.set_modes("listing-refused")
    _refusal(lambda: _free(db_session, vol))
    with export_jobs.hold_for_reading([vol.id]):
        pass  # would raise FilesChanging if the free had left its hold behind


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_restore_round_trips_the_bytes(read_remote, db_session, override_projects_root):
    vol, raw, jpg = _received(db_session, override_projects_root)
    raw_master, raw_preview, _ = _paths(override_projects_root, "f1.CR2")
    jpg_master = _paths(override_projects_root, "f2.jpg")[0]
    _free(db_session, vol)

    result = _restore(db_session, vol)

    assert result["refused"] == [], result
    assert result["restored"] == {"files": 3, "bytes": len(b"raw one") + len(b"preview one") + len(b"jpeg two")}
    assert raw_master.read_bytes() == b"raw one"
    assert raw_preview.read_bytes() == b"preview one"
    assert jpg_master.read_bytes() == b"jpeg two"
    for image in (raw, jpg):
        row = _image_row(db_session, image.id)
        assert row.master_freed_at is None and row.master_freed_bytes is None
        assert row.preview_freed_at is None and row.preview_freed_bytes is None
    events = _logs(db_session, "volume_restored")
    assert len(events) == 1 and "files=3" in events[0].detail
    lines = [json.loads(l) for l in Path(result["manifest"]).read_text().splitlines()]
    assert len(lines) == 3
    # No staging file is left beside the restored ones.
    leftovers = [p for p in (override_projects_root / STEM).rglob("*") if p.name.endswith(".part")]
    assert leftovers == []


@pytest.mark.unit
def test_restore_refuses_an_object_whose_bytes_do_not_match(read_remote, db_session, override_projects_root):
    vol, raw, jpg = _received(db_session, override_projects_root)
    _free(db_session, vol)
    read_remote.put(f"{TENANT}/{STEM}/captures/f2.jpg", b"tampered")
    jpg_master = _paths(override_projects_root, "f2.jpg")[0]

    result = _restore(db_session, vol)

    assert not jpg_master.exists()
    assert _image_row(db_session, jpg.id).master_freed_at is not None
    assert [(r["image_id"], r["kind"], r["reason"]) for r in result["refused"]] == [(jpg.id, "capture", "sha1_differs")]
    assert result["refused"][0]["copied_sha1"] == _sha1(b"tampered")
    assert result["restored"]["files"] == 2
    leftovers = [p for p in (override_projects_root / STEM).rglob("*") if p.name.endswith(".part")]
    assert leftovers == []


@pytest.mark.unit
def test_restore_without_a_read_remote_is_refused(remote, db_session, override_projects_root):
    vol, _, _ = _received(db_session, override_projects_root)
    _free(db_session, vol)

    refused = _refusal(lambda: _restore(db_session, vol))

    assert refused.status_code == 409 and "DTK_OFFSITE_READ_REMOTE" in refused.message


@pytest.mark.unit
def test_restore_never_overwrites_a_file_put_back_by_hand(read_remote, db_session, override_projects_root):
    vol, raw, jpg = _received(db_session, override_projects_root)
    _free(db_session, vol)
    jpg_master = _paths(override_projects_root, "f2.jpg")[0]
    jpg_master.write_bytes(b"not the original")

    result = _restore(db_session, vol)

    assert jpg_master.read_bytes() == b"not the original"
    assert [(r["image_id"], r["reason"]) for r in result["refused"]] == [(jpg.id, "local_file_differs")]
    assert _image_row(db_session, jpg.id).master_freed_at is not None


# ---------------------------------------------------------------------------
# Capture into a volume with freed images
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_upload_into_a_freed_volume_is_allowed_and_not_freed(remote, client, db_session, override_projects_root):
    import io

    from PIL import Image

    _as("operator")
    vol, raw, _ = _received(db_session, override_projects_root)
    _free(db_session, vol)
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, format="JPEG")

    response = client.post(f"/records/{raw.record_id}/images",
                           files={"file": ("new.jpg", buf.getvalue(), "image/jpeg")})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["master_freed_at"] is None
    assert Path(body["file_path"]).is_file()
    state = client.get(f"/collections/{vol.id}/offsite").json()
    assert state["received"] is False and state["pending"] == 1


# ---------------------------------------------------------------------------
# The offsite state
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_offsite_state_counts_freed_files_and_bytes(remote, client, db_session, override_projects_root):
    _as("reviewer")
    vol, _, _ = _received(db_session, override_projects_root)
    before = client.get(f"/collections/{vol.id}/offsite").json()
    assert (before["freed_files"], before["freed_bytes"]) == (0, 0)

    _free(db_session, vol)

    after = client.get(f"/collections/{vol.id}/offsite").json()
    assert after["received"] is True and after["verified"] == 3
    assert after["freed_files"] == 3
    assert after["freed_bytes"] == len(b"raw one") + len(b"preview one") + len(b"jpeg two")
    unit = client.get("/system/catalogue-backup").json()["images"]
    assert unit["freed_files"] == 3


# ---------------------------------------------------------------------------
# mark-freed
# ---------------------------------------------------------------------------

def _hand_cleared(db, root, remote):
    """A received volume whose RAW master someone removed by hand, plus an absent file the bucket cannot account for."""
    vol, raw, jpg = _received(db, root)
    lost = _image(db, vol, root, "f3.jpg", b"three")  # never uploaded
    _paths(root, "f1.CR2")[0].unlink()
    _paths(root, "f3.jpg")[0].unlink()
    return vol, raw, jpg, lost


@pytest.mark.unit
def test_mark_freed_dry_run_reports_and_writes_nothing(remote, db_session, override_projects_root):
    from app.core.offsite_free import mark_freed

    vol, raw, jpg, lost = _hand_cleared(db_session, override_projects_root, remote)

    report = mark_freed(db_session, STEM, apply=False)

    assert [(m["image_id"], m["kind"]) for m in report["marked"]] == [(raw.id, "capture")]
    assert report["marked"][0]["bytes"] == len(b"raw one")
    assert [(u["image_id"], u["kind"], u["reason"]) for u in report["unaccounted"]] == [(lost.id, "capture", "no_verified_object")]
    assert report["applied"] is False
    assert _image_row(db_session, raw.id).master_freed_at is None


@pytest.mark.unit
def test_mark_freed_apply_marks_only_what_the_bucket_accounts_for(remote, db_session, override_projects_root):
    from app.core.offsite_free import mark_freed

    vol, raw, jpg, lost = _hand_cleared(db_session, override_projects_root, remote)
    remote.put(f"{TENANT}/{STEM}/captures/f1.CR2", b"replaced in the bucket")
    remote.put(f"{TENANT}/{STEM}/captures/other.CR2", b"x")

    report = mark_freed(db_session, STEM, apply=True)

    assert report["marked"] == []
    assert {(u["image_id"], u["reason"]) for u in report["unaccounted"]} == {
        (raw.id, "bucket_sha1_differs"), (lost.id, "no_verified_object")}
    assert _image_row(db_session, raw.id).master_freed_at is None

    remote.put(f"{TENANT}/{STEM}/captures/f1.CR2", b"raw one")
    report = mark_freed(db_session, STEM, apply=True)

    assert [(m["image_id"], m["kind"]) for m in report["marked"]] == [(raw.id, "capture")]
    row = _image_row(db_session, raw.id)
    assert row.master_freed_at is not None and row.master_freed_bytes == len(b"raw one")
    assert row.preview_freed_at is None  # the preview is still on the card
    assert _paths(override_projects_root, "f1.CR2")[1].read_bytes() == b"preview one"
    assert _paths(override_projects_root, "f2.jpg")[0].read_bytes() == b"jpeg two"
    events = _logs(db_session, "volume_freed")
    assert len(events) == 2  # the first run reported its unaccounted files too
    assert "mark-freed: files=1" in events[-1].detail and "unaccounted=1" in events[-1].detail
    lines = [json.loads(l) for l in Path(report["manifest"]).read_text().splitlines()]
    assert [(l["image_id"], l["kind"]) for l in lines] == [(raw.id, "capture")]


@pytest.mark.unit
def test_the_mark_freed_command_reports_as_json(remote, db_session, override_projects_root, capsys):
    from app.core.image_upload import main

    vol, raw, _, _ = _hand_cleared(db_session, override_projects_root, remote)

    assert main(["mark-freed", STEM]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is False and len(report["marked"]) == 1
    assert _image_row(db_session, raw.id).master_freed_at is None

    assert main(["mark-freed", STEM, "--apply"]) == 0
    assert _image_row(db_session, raw.id).master_freed_at is not None

    assert main(["mark-freed", "xx-no-such-stem"]) == 2


@pytest.mark.unit
def test_mark_freed_of_an_unanswered_listing_raises(remote, db_session, override_projects_root):
    from app.core.catalogue_backup import VerificationUnavailable
    from app.core.offsite_free import mark_freed

    _hand_cleared(db_session, override_projects_root, remote)
    remote.set_modes("listing-refused")
    with pytest.raises(VerificationUnavailable):
        mark_freed(db_session, STEM, apply=True)


@pytest.mark.unit
def test_a_retake_of_a_freed_record_works_as_usual(remote, client, db_session, override_projects_root,
                                                     monkeypatch, tmp_path):
    from tests.unit.test_capture_birth_and_retake import _capture, _install_capture_fakes

    _install_capture_fakes(monkeypatch, tmp_path)
    _as("operator")
    vol, raw, jpg = _received(db_session, override_projects_root)
    _free(db_session, vol)

    response = _capture(client, "P", collection_id=vol.id, record_id=jpg.record_id)

    assert response.status_code == 200, response.text
    old = _image_row(db_session, jpg.id)
    assert old.is_current is False and old.master_freed_at is not None
    from app.models.record import RecordImage
    new = (db_session.query(RecordImage)
           .filter(RecordImage.record_id == jpg.record_id, RecordImage.is_current.is_(True)).one())
    assert new.master_freed_at is None and new.preview_freed_at is None


@pytest.mark.unit
def test_restore_shares_the_uploads_transfer_lock_and_bandwidth_cap(read_remote, db_session, override_projects_root,
                                                                    monkeypatch):
    from app.core import config
    from app.core.catalogue_backup import transfer_lock
    from app.core.offsite_free import restore_volume

    vol, _, _ = _received(db_session, override_projects_root)
    _free(db_session, vol)
    monkeypatch.setattr(config.settings, "OFFSITE_BWLIMIT", "123k")
    before = len(read_remote.calls("copyto"))
    done = {}

    with transfer_lock:
        worker = threading.Thread(target=lambda: done.setdefault(
            "result", restore_volume(db_session, vol.id, actor="adm")))
        worker.start()
        time.sleep(1.0)
        # Held by another transfer: the restore waits and copies nothing.
        assert len(read_remote.calls("copyto")) == before
        assert worker.is_alive()
    worker.join(30)

    assert done["result"]["restored"]["files"] == 3
    copies = read_remote.calls("copyto")[before:]
    assert len(copies) == 3
    for call in copies:
        assert call[call.index("--bwlimit") + 1] == "123k"


# ---------------------------------------------------------------------------
# A delete during a restore or a free (both run under a hold that a delete
# shares, so they are serialised on the record's row lock instead)
# ---------------------------------------------------------------------------

from tests.unit.test_image_upload import api  # noqa: E402,F401


@pytest.mark.unit
def test_a_delete_committed_during_a_restores_copy_leaves_no_file_and_is_reported(
        read_remote, api, db_session, override_projects_root, monkeypatch):
    import app.core.offsite_free as of

    vol, raw, jpg = _received(db_session, override_projects_root)
    _free(db_session, vol)
    jpg_id, jpg_record = jpg.id, jpg.record_id
    jpg_master = _paths(override_projects_root, "f2.jpg")[0]
    real = of._copy_from_bucket

    def copy_then_delete(key, dest):
        error = real(key, dest)
        if key.endswith("/f2.jpg"):
            response = api.delete(f"/records/{jpg_record}")
            assert response.status_code == 200, response.text
        return error

    monkeypatch.setattr(of, "_copy_from_bucket", copy_then_delete)

    result = _restore(db_session, vol)

    assert not jpg_master.exists(), "the deleted image's master was written back to the card"
    assert [(r["image_id"], r["kind"], r["reason"]) for r in result["refused"]] == [(jpg_id, "capture", "changed")]
    assert result["restored"]["files"] == 2
    leftovers = [p for p in (override_projects_root / STEM).rglob("*") if p.name.endswith(".part")]
    assert leftovers == []


@pytest.mark.unit
@pytest.mark.parametrize("when", ["before_the_state_is_set", "after_the_state_is_committed"])
def test_a_delete_during_freeing_leaves_a_consistent_state(
        remote, api, db_session, override_projects_root, monkeypatch, when):
    import app.core.offsite_free as of
    from app.models.record import RecordImage

    vol, raw, jpg = _received(db_session, override_projects_root)
    jpg_id, jpg_record, raw_id = jpg.id, jpg.record_id, raw.id
    jpg_master = _paths(override_projects_root, "f2.jpg")[0]
    real = of._set_freed
    deleted = []

    def delete():
        response = api.delete(f"/records/{jpg_record}")
        assert response.status_code == 200, response.text
        deleted.append(True)

    def set_freed(db, obj, *, at, size):
        is_target = obj.image_id == jpg_id and at is not None and not deleted
        if is_target and when == "before_the_state_is_set":
            delete()
        changed = real(db, obj, at=at, size=size)
        if is_target and when == "after_the_state_is_committed":
            delete()
        return changed

    monkeypatch.setattr(of, "_set_freed", set_freed)

    result = _free(db_session, vol)

    db_session.expire_all()
    assert db_session.get(RecordImage, jpg_id) is None
    assert not jpg_master.exists()
    assert [(r["image_id"], r["reason"]) for r in result["refused"]] == [(jpg_id, "changed")]
    assert result["freed"]["files"] == 2
    assert {(f["image_id"], f["kind"]) for f in result["files"]} == {(raw_id, "capture"), (raw_id, "preview")}
    row = _image_row(db_session, raw_id)
    assert row.master_freed_at is not None and row.preview_freed_at is not None


def _fd_path(fd):
    """The path an open descriptor names (Linux /proc, macOS F_GETPATH)."""
    import os
    import sys

    if sys.platform == "darwin":
        import fcntl
        return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0").decode()
    return os.readlink(f"/proc/self/fd/{fd}")


@pytest.mark.unit
def test_restore_fsyncs_the_staging_file_before_the_replace_and_the_directory_after(
        read_remote, db_session, override_projects_root, monkeypatch):
    import os

    vol, _, jpg = _received(db_session, override_projects_root)
    _free(db_session, vol)
    target = _paths(override_projects_root, "f2.jpg")[0].resolve()
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        events.append(("fsync", os.path.realpath(_fd_path(fd))))
        return real_fsync(fd)

    def replace(src, dst):
        events.append(("replace", os.path.realpath(src), os.path.realpath(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)

    _restore(db_session, vol)

    placed = [i for i, e in enumerate(events) if e[0] == "replace" and e[2] == str(target)]
    assert len(placed) == 1, events
    staging = events[placed[0]][1]
    assert Path(staging).parent == target.parent and staging.endswith(".part")
    assert ("fsync", staging) in events[:placed[0]], "the staging file was not fsynced before the replace"
    assert ("fsync", str(target.parent)) in events[placed[0]:], "the directory was not fsynced after the replace"


@pytest.mark.unit
def test_a_missing_thumbnail_of_a_local_raw_whose_preview_is_freed_answers_410_with_the_preview_key(
        remote, client, db_session, override_projects_root):
    from app.models.record import RecordImage

    _as("reviewer")
    vol, raw, _ = _received(db_session, override_projects_root)
    _free(db_session, vol)
    master, preview, thumb = _paths(override_projects_root, "f1.CR2")
    # A partial restore: the master is back, the preview still freed.
    master.write_bytes(b"raw one")
    row = db_session.get(RecordImage, raw.id)
    row.master_freed_at, row.master_freed_bytes = None, None
    db_session.commit()
    thumb.unlink()

    r = client.get(f"/records/images/{raw.id}/thumbnail")

    assert r.status_code == 410, r.text
    detail = r.json()["detail"]
    assert detail["kind"] == "preview" and detail["key"] == f"{TENANT}/{STEM}/previews/f1_preview.jpg"


# ---------------------------------------------------------------------------
# A volume still partly in the name-based layout
# ---------------------------------------------------------------------------

def _partly_in_the_old_layout(db, root):
    """A received volume one of whose images the layout migration has not yet moved into `<stem>/`."""
    from app.models.record import RecordImage

    vol, raw, jpg = _received(db, root)
    old = root / "p" / "tomo" / "images" / "main" / "f2.jpg"
    old.parent.mkdir(parents=True)
    _paths(root, "f2.jpg")[0].rename(old)
    row = db.get(RecordImage, jpg.id)
    row.file_path = str(old)
    db.commit()
    return vol, raw, jpg, old


@pytest.mark.unit
def test_a_volume_not_yet_in_its_own_folder_is_refused_for_freeing_and_the_dry_run(
        remote, db_session, override_projects_root):
    from app.core.offsite_free import NOT_IN_OWN_FOLDER_MESSAGE, plan_free

    vol, raw, jpg, old = _partly_in_the_old_layout(db_session, override_projects_root)

    for attempt in (lambda: _free(db_session, vol), lambda: plan_free(db_session, vol.id)):
        refused = _refusal(attempt)
        assert refused.status_code == 409 and refused.message == NOT_IN_OWN_FOLDER_MESSAGE
    assert old.read_bytes() == b"jpeg two"
    assert all(p.exists() for p in _paths(override_projects_root, "f1.CR2"))
    assert _image_row(db_session, raw.id).master_freed_at is None


@pytest.mark.unit
def test_a_volume_not_yet_in_its_own_folder_is_refused_for_restore(read_remote, db_session, override_projects_root):
    from app.core.offsite_free import NOT_IN_OWN_FOLDER_MESSAGE
    from app.models.record import RecordImage

    vol, raw, jpg = _received(db_session, override_projects_root)
    _free(db_session, vol)
    row = db_session.get(RecordImage, jpg.id)
    row.file_path = str(override_projects_root / "p" / "tomo" / "images" / "main" / "f2.jpg")
    db_session.commit()

    refused = _refusal(lambda: _restore(db_session, vol))

    assert refused.status_code == 409 and refused.message == NOT_IN_OWN_FOLDER_MESSAGE
    assert not _paths(override_projects_root, "f1.CR2")[0].exists()


@pytest.mark.unit
@pytest.mark.parametrize("work", ["free", "restore"])
def test_the_job_checks_the_layout_again(read_remote, db_session, override_projects_root, work):
    import app.core.offsite_free as of

    vol, raw, jpg = _received(db_session, override_projects_root)
    if work == "restore":
        _free(db_session, vol)
    from app.models.record import RecordImage
    row = db_session.get(RecordImage, jpg.id)
    row.file_path = str(override_projects_root / "p" / "tomo" / "images" / "main" / "f2.jpg")
    db_session.commit()
    fn = of._free_work if work == "free" else of._restore_work

    refused = _refusal(lambda: fn(db_session, vol.id, STEM, "adm"))

    assert refused.message == of.NOT_IN_OWN_FOLDER_MESSAGE
    assert _paths(override_projects_root, "f1.CR2")[1].exists() == (work == "free")


# ---------------------------------------------------------------------------
# An unusable storage root
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_restore_refuses_an_unusable_storage_root_and_writes_nothing(
        read_remote, api, db_session, override_projects_root, monkeypatch):
    import os

    from app.core.storage_override import StorageUnavailableError

    vol, raw, jpg = _received(db_session, override_projects_root)
    _free(db_session, vol)
    previews = override_projects_root / STEM / "previews"
    captures = override_projects_root / STEM / "captures"
    assert list(previews.iterdir()) == [] and list(captures.iterdir()) == []
    previews.rmdir()
    captures.rmdir()
    # The projects root is the override's, and it sits on the card: a drive's
    # mount point with no drive mounted.
    monkeypatch.setattr("app.core.storage_override.get_storage_override", lambda: str(override_projects_root))
    dev = os.stat(override_projects_root).st_dev
    monkeypatch.setattr("app.core.storage_override._internal_device", lambda: dev)

    with pytest.raises(StorageUnavailableError):
        _restore(db_session, vol)
    r = api.post(f"/collections/{vol.id}/offsite/restore")
    assert r.status_code == 503, r.text

    assert not previews.exists() and not captures.exists(), "a restore wrote under an unusable root"
    for image in (raw, jpg):
        assert _image_row(db_session, image.id).master_freed_at is not None
    assert read_remote.calls("copyto")[3:] == []


@pytest.mark.unit
def test_a_job_whose_root_becomes_unusable_fails_with_the_storage_message_and_clears_no_state(
        read_remote, db_session, override_projects_root, monkeypatch):
    import os

    import app.core.offsite_free as of
    from app.core.storage_override import StorageUnavailableError

    vol, raw, jpg = _received(db_session, override_projects_root)
    _free(db_session, vol)
    objects = of._objects(db_session, vol.id, freed=True)  # read while the root is still usable
    previews = override_projects_root / STEM / "previews"
    previews.rmdir()
    # The drive goes during the job, after its checks at the start.
    monkeypatch.setattr("app.core.storage_override.get_storage_override", lambda: str(override_projects_root))
    dev = os.stat(override_projects_root).st_dev
    monkeypatch.setattr("app.core.storage_override._internal_device", lambda: dev)

    for obj in objects:
        with pytest.raises(StorageUnavailableError) as exc:
            of._restore_one(db_session, obj)
        assert "not on a mounted drive" in str(exc.value)

    assert not previews.exists(), "a restore created a directory under an unusable root"
    assert read_remote.calls("copyto")[3:] == []
    for image in (raw, jpg):
        row = _image_row(db_session, image.id)
        assert row.master_freed_at is not None
    assert not _paths(override_projects_root, "f2.jpg")[0].exists()


# ---------------------------------------------------------------------------
# A preview without a recorded sha256
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_preview_whose_capture_record_has_no_sha256_stays_and_its_master_is_freed(
        remote, db_session, override_projects_root):
    from app.core.offsite_free import plan_free

    vol, raw, jpg = _received(db_session, override_projects_root, preview_digest=False)
    raw_master, raw_preview, _ = _paths(override_projects_root, "f1.CR2")

    plan = plan_free(db_session, vol.id)
    assert [(r["image_id"], r["kind"], r["reason"]) for r in plan["refused"]] == [(raw.id, "preview", "no_preview_digest")]

    result = _free(db_session, vol)

    assert [(r["image_id"], r["kind"], r["reason"]) for r in result["refused"]] == [(raw.id, "preview", "no_preview_digest")]
    assert raw_preview.read_bytes() == b"preview one"
    assert not raw_master.exists() and not _paths(override_projects_root, "f2.jpg")[0].exists()
    row = _image_row(db_session, raw.id)
    assert row.master_freed_at is not None and row.preview_freed_at is None
    assert result["freed"]["files"] == 2


@pytest.mark.unit
def test_a_preview_digest_is_never_taken_from_the_masters_relocation_record(
        remote, db_session, override_projects_root):
    """A relocation record naming the image carries the master's sha256, which is not the preview's."""
    from capture.manifestHandler import append_relocation_record

    vol, raw, jpg = _received(db_session, override_projects_root, preview_digest=False)
    append_relocation_record(override_projects_root / STEM, _image_row(db_session, raw.id).capture_id,
                             "captures/old.CR2", "captures/f1.CR2", image_id=raw.id,
                             sha256=hashlib.sha256(b"raw one").hexdigest())

    result = _free(db_session, vol)

    assert [(r["kind"], r["reason"]) for r in result["refused"]] == [("preview", "no_preview_digest")]
    assert _paths(override_projects_root, "f1.CR2")[1].exists()


@pytest.mark.unit
def test_a_preview_given_its_digest_by_the_backfill_is_freed(remote, db_session, override_projects_root):
    """backfill-preview-digests (app.core.preview_digests) gives an older preview the digest freeing needs."""
    from app.core.preview_digests import backfill_preview_digests

    vol, raw, jpg = _received(db_session, override_projects_root, preview_digest=False)
    raw_preview = _paths(override_projects_root, "f1.CR2")[1]

    report = backfill_preview_digests(db_session, STEM, apply=True)
    assert [e["image_id"] for e in report["backfilled"]] == [raw.id]
    assert report["backfilled"][0]["checked_against_offsite"] is True

    result = _free(db_session, vol)

    assert result["refused"] == []
    assert not raw_preview.exists()
    assert _image_row(db_session, raw.id).preview_freed_at is not None


@pytest.mark.unit
def test_a_record_let_back_into_the_volume_mid_free_with_files_in_the_old_layout_is_not_freed(
        remote, db_session, override_projects_root, monkeypatch):
    """A record outside every volume may come back into the one its keys are under (PATCH /records/{id}).

    If it comes back between the job's _require_own_folder and its reading
    of the objects, with its files still in a name-based tree, a project
    rename could move them under the free; such an object is refused.
    """
    from datetime import datetime, timezone

    from app.core import offsite_free
    from app.models.offsite_object import OffsiteObject

    vol, _, _ = _received(db_session, override_projects_root)
    old = _volume(db_session, name="Sin stem", stem=None)
    stray = _image(db_session, old, override_projects_root, "f9.jpg", b"jpeg nine")
    key = f"{TENANT}/{STEM}/captures/f9.jpg"
    remote.put(key, b"jpeg nine")
    db_session.add(OffsiteObject(record_image_id=stray.id, kind="capture", object_key=key,
                                 local_sha1=_sha1(b"jpeg nine"), verified_at=datetime.now(timezone.utc)))
    rec = stray.record
    rec.collection_id, rec.project_id = None, old.project_id
    db_session.commit()
    stray_path = Path(stray.file_path)

    original = offsite_free._require_own_folder
    calls = []

    def way_back_after_the_check(db, collection):
        original(db, collection)
        calls.append(collection.id)
        if len(calls) == 2:  # the job's own check, after the request's
            rec.collection_id, rec.project_id = vol.id, None
            db.commit()

    monkeypatch.setattr(offsite_free, "_require_own_folder", way_back_after_the_check)
    result = _free(db_session, vol)

    assert len(calls) == 2
    assert [(r["image_id"], r["reason"]) for r in result["refused"]] == [(stray.id, "outside_own_folder")]
    assert result["freed"]["files"] == 3
    assert stray_path.read_bytes() == b"jpeg nine"
    assert _image_row(db_session, stray.id).master_freed_at is None


@pytest.mark.unit
def test_a_record_let_back_into_the_volume_mid_restore_with_files_in_the_old_layout_is_not_restored(
        read_remote, db_session, override_projects_root, monkeypatch):
    """As for freeing: a freed record outside every volume may come back into it mid-restore.

    With its files in a name-based tree, a project rename could move that
    tree under the restore and leave the restored file away from its row;
    such an object is refused and stays freed.
    """
    from datetime import datetime, timezone

    from app.core import offsite_free
    from app.models.offsite_object import OffsiteObject

    vol, _, _ = _received(db_session, override_projects_root)
    _free(db_session, vol)
    old = _volume(db_session, name="Sin stem", stem=None)
    stray = _image(db_session, old, override_projects_root, "f9.jpg", b"jpeg nine")
    key = f"{TENANT}/{STEM}/captures/f9.jpg"
    read_remote.put(key, b"jpeg nine")
    now = datetime.now(timezone.utc)
    db_session.add(OffsiteObject(record_image_id=stray.id, kind="capture", object_key=key,
                                 local_sha1=_sha1(b"jpeg nine"), verified_at=now))
    stray.master_freed_at, stray.master_freed_bytes = now, len(b"jpeg nine")
    rec = stray.record
    rec.collection_id, rec.project_id = None, old.project_id
    db_session.commit()
    stray_path = Path(stray.file_path)
    stray_path.unlink()

    original = offsite_free._require_own_folder
    calls = []

    def way_back_after_the_check(db, collection):
        original(db, collection)
        calls.append(collection.id)
        if len(calls) == 2:  # the job's own check, after the request's
            rec.collection_id, rec.project_id = vol.id, None
            db.commit()

    monkeypatch.setattr(offsite_free, "_require_own_folder", way_back_after_the_check)
    result = _restore(db_session, vol)

    assert len(calls) == 2
    assert [(r["image_id"], r["reason"]) for r in result["refused"]] == [(stray.id, "outside_own_folder")]
    assert result["restored"]["files"] == 3
    assert not stray_path.exists()
    assert _image_row(db_session, stray.id).master_freed_at is not None

@pytest.mark.unit
@pytest.mark.parametrize("freed", [False, True])
def test_reading_the_objects_leaves_no_transaction_open(remote, db_session, override_projects_root, freed):
    """The work list is read and committed; no transaction stays open across the hashing and listing that follow."""
    from app.core.offsite_free import _objects

    vol, _, _ = _received(db_session, override_projects_root)
    db_session.commit()
    objects = _objects(db_session, vol.id, freed=freed)
    assert not db_session.in_transaction()
    assert all(o.old_layout is False for o in objects)
