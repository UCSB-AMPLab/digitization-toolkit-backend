"""The offsite upload hold (app.core.offsite_hold).

While the hold file exists the image upload uploads nothing, the catalogue
dump does not run and freeing is refused. The schedulers still start, and
each tick reads the file again, so creating or removing it takes effect at
the next tick without a restart. The one-time commands that run during an
upgrade (adopt, adopt-map, raise-capture-counter, mark-freed,
backfill-preview-digests) are not held.

    python -m pytest tests/unit/test_offsite_hold.py
"""

import json
import logging

import pytest

from tests.unit.test_catalogue_backup import offsite  # noqa: F401
from tests.unit.test_image_upload import STEM, TENANT, _image, _rows, _run, _volume, remote  # noqa: F401
from tests.unit.test_offsite_free import _as, _free, _received, _restore, read_remote  # noqa: F401


@pytest.fixture
def hold(tmp_path, monkeypatch, db_session):
    """The hold file's path, set explicitly and not yet created."""
    from app.core import config

    path = tmp_path / "hold-dir" / "offsite-hold"
    path.parent.mkdir()
    monkeypatch.setattr(config.settings, "OFFSITE_HOLD_FILE", str(path))
    return path


class _Shared:
    """db_session handed to a scheduler tick, which closes its session when done."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        pass


@pytest.fixture
def ticks_on_test_db(db_session, monkeypatch):
    import app.core.db as db_module

    monkeypatch.setattr(db_module, "SessionLocal", lambda: _Shared(db_session))


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_hold_file_defaults_to_offsite_hold_in_the_data_dir(db_session, monkeypatch, tmp_path):
    from app.core import config
    from app.core.offsite_hold import hold_file

    monkeypatch.setattr(config.settings, "OFFSITE_HOLD_FILE", "")
    monkeypatch.setattr(config.settings, "DATA_DIR", "/var/lib/dtk")
    assert str(hold_file()) == "/var/lib/dtk/offsite-hold"

    monkeypatch.setattr(config.settings, "OFFSITE_HOLD_FILE", str(tmp_path / "elsewhere"))
    assert hold_file() == tmp_path / "elsewhere"


@pytest.mark.unit
def test_the_setting_is_read_from_its_environment_name(monkeypatch, tmp_path):
    from app.core.config import Settings

    monkeypatch.setenv("CAPTUA_OFFSITE_HOLD_FILE", str(tmp_path / "h"))
    assert Settings(_env_file=None).OFFSITE_HOLD_FILE == str(tmp_path / "h")


# ---------------------------------------------------------------------------
# The image upload
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_held_run_uploads_nothing_and_lists_nothing(remote, hold, db_session, override_projects_root):
    vol = _volume(db_session)
    _image(db_session, vol, override_projects_root, "f1.jpg", b"one")
    hold.write_text("")

    result = _run(db_session)

    assert result == {"status": "held", "hold_file": str(hold)}
    assert remote.calls() == [], "a held run calls rclone not at all"
    assert remote.files() == {}
    assert _rows(db_session) == {}


@pytest.mark.unit
def test_the_run_after_the_hold_is_removed_uploads(remote, hold, db_session, override_projects_root):
    vol = _volume(db_session)
    img = _image(db_session, vol, override_projects_root, "f1.jpg", b"one")
    hold.write_text("")
    assert _run(db_session)["status"] == "held"

    hold.unlink()
    result = _run(db_session)

    assert result["status"] == "done" and result["uploaded"] == 1
    assert _rows(db_session)[(img.id, "capture")].verified_at is not None


@pytest.mark.unit
def test_a_hold_created_while_the_scheduler_runs_takes_effect_at_the_next_tick(
        remote, hold, db_session, override_projects_root, ticks_on_test_db):
    from app.core.image_upload import ImageUploadScheduler

    scheduler = ImageUploadScheduler()
    vol = _volume(db_session)
    first = _image(db_session, vol, override_projects_root, "f1.jpg", b"one")
    assert scheduler.tick()["uploaded"] == 1

    hold.write_text("")
    second = _image(db_session, vol, override_projects_root, "f2.jpg", b"two")
    assert scheduler.tick()["status"] == "held"
    assert scheduler.last_result["status"] == "held"
    assert (second.id, "capture") not in _rows(db_session)

    hold.unlink()
    result = scheduler.tick()
    assert result["status"] == "done" and result["uploaded"] == 1
    assert set(_rows(db_session)) == {(first.id, "capture"), (second.id, "capture")}


@pytest.mark.unit
def test_a_hold_created_during_a_drain_stops_it_after_the_object_in_progress(
        remote, hold, db_session, override_projects_root, monkeypatch):
    from app.core import image_upload

    vol = _volume(db_session)
    _image(db_session, vol, override_projects_root, "f1.jpg", b"one")
    _image(db_session, vol, override_projects_root, "f2.jpg", b"two")
    real = image_upload._process

    def process_then_hold(*args):
        real(*args)
        hold.write_text("")

    monkeypatch.setattr(image_upload, "_process", process_then_hold)
    result = _run(db_session)

    assert result["status"] == "held" and result["uploaded"] == 1
    assert len(remote.calls("copyto")) == 1


@pytest.mark.unit
def test_both_schedulers_start_while_the_hold_exists(remote, offsite, hold, monkeypatch):
    from app.core import config
    from app.core.catalogue_backup import CatalogueBackupScheduler
    from app.core.image_upload import ImageUploadScheduler

    monkeypatch.setattr(config.settings, "OFFSITE_BACKUP_DELAY_SECONDS", 3600)
    hold.write_text("")
    for scheduler in (ImageUploadScheduler(), CatalogueBackupScheduler()):
        try:
            assert scheduler.start() is True
            assert scheduler.is_running()
        finally:
            scheduler.stop()


@pytest.mark.unit
def test_a_held_tick_logs_one_info_line(remote, hold, db_session, override_projects_root, caplog):
    hold.write_text("")
    with caplog.at_level(logging.INFO):
        _run(db_session)
    lines = [r for r in caplog.records if "on hold" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].levelno == logging.INFO
    assert f"offsite uploads are on hold: {hold} exists" in lines[0].getMessage()


@pytest.mark.unit
def test_the_one_time_commands_run_while_the_hold_exists(remote, hold, db_session, override_projects_root, capsys):
    from app.core import image_upload

    vol = _volume(db_session)
    img = _image(db_session, vol, override_projects_root, "a.jpg", b"jpeg a")
    remote.put("ahr/tomo-6/images/a.jpg", b"jpeg a")
    hold.write_text("")

    assert image_upload.main(["adopt", "ahr/tomo-6", "--apply"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is True and len(out["adopted"]) == 1
    assert _rows(db_session)[(img.id, "capture")].object_key == "ahr/tomo-6/images/a.jpg"

    assert image_upload.main(["raise-capture-counter", STEM, "5", "--apply"]) == 0
    capsys.readouterr()
    assert image_upload.main(["backfill-preview-digests", STEM]) == 0
    capsys.readouterr()
    assert image_upload.main(["mark-freed", STEM]) == 0
    capsys.readouterr()
    assert remote.calls("copyto") == []


# ---------------------------------------------------------------------------
# The catalogue dump
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_held_catalogue_backup_does_not_dump(offsite, hold, db_session, monkeypatch, caplog):
    cb = offsite.module
    dumped = []
    monkeypatch.setattr(cb, "write_dump", lambda dest: dumped.append(dest) or (0, ""))
    hold.write_text("")

    with caplog.at_level(logging.INFO):
        result = cb.run_catalogue_backup(db_session)

    assert result == {"status": "held", "hold_file": str(hold)}
    assert dumped == [] and offsite.upload_calls() == []
    assert len([r for r in caplog.records if "on hold" in r.getMessage()]) == 1


@pytest.mark.unit
def test_the_catalogue_backup_runs_once_the_hold_is_removed(offsite, hold, db_session):
    cb = offsite.module
    hold.write_text("")
    assert cb.run_catalogue_backup(db_session)["status"] == "held"

    hold.unlink()
    assert cb.run_catalogue_backup(db_session)["status"] == "verified"
    assert len(offsite.upload_calls()) == 1


@pytest.mark.unit
def test_a_held_catalogue_backup_still_sweeps_a_stale_dump(offsite, hold, db_session):
    stale = offsite.dump_dir / "catalogue-20260101T000000Z-abcd.sql.gz.age"
    stale.write_bytes(b"x")
    hold.write_text("")
    assert offsite.module.run_catalogue_backup(db_session)["status"] == "held"
    assert offsite.leftovers() == []


# ---------------------------------------------------------------------------
# Freeing and restore
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_freeing_is_refused_while_the_hold_exists(remote, hold, db_session, override_projects_root):
    from app.core.offsite_free import HOLD_MESSAGE, FreeRefused

    vol, raw, jpg = _received(db_session, override_projects_root)
    hold.write_text("")
    with pytest.raises(FreeRefused) as refused:
        _free(db_session, vol)
    assert refused.value.message == HOLD_MESSAGE
    assert refused.value.detail == {"hold_file": str(hold)}
    assert (override_projects_root / STEM / "captures" / "f2.jpg").exists()

    hold.unlink()
    assert _free(db_session, vol)["freed"]["files"] == 3


@pytest.mark.unit
def test_the_free_route_answers_409_while_the_hold_exists(remote, hold, client, db_session, override_projects_root):
    from app.core.offsite_free import HOLD_MESSAGE

    _as("admin")
    vol, _, _ = _received(db_session, override_projects_root)
    hold.write_text("")
    response = client.post(f"/collections/{vol.id}/offsite/free")
    assert response.status_code == 409
    assert response.json()["detail"] == {"message": HOLD_MESSAGE, "hold_file": str(hold)}
    # The dry run changes nothing and is still answered.
    assert client.get(f"/collections/{vol.id}/offsite/free").status_code == 200


@pytest.mark.unit
def test_restore_runs_while_the_hold_exists(read_remote, hold, db_session, override_projects_root):
    vol, _, _ = _received(db_session, override_projects_root)
    assert _free(db_session, vol)["freed"]["files"] == 3
    hold.write_text("")
    assert _restore(db_session, vol)["restored"]["files"] == 3


# ---------------------------------------------------------------------------
# What the API reports
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_status_routes_report_the_hold(remote, hold, client, db_session, override_projects_root):
    _as("reviewer")
    vol = _volume(db_session)

    body = client.get("/system/catalogue-backup").json()
    assert (body["held"], body["hold_file"]) == (False, str(hold))
    assert (body["images"]["held"], body["images"]["hold_file"]) == (False, str(hold))
    vol_body = client.get(f"/collections/{vol.id}/offsite").json()
    assert (vol_body["held"], vol_body["hold_file"]) == (False, str(hold))

    hold.write_text("")
    body = client.get("/system/catalogue-backup").json()
    assert body["held"] is True and body["images"]["held"] is True
    assert client.get(f"/collections/{vol.id}/offsite").json()["held"] is True

    nostem = _volume(db_session, name="Sin raíz", stem=None)
    assert client.get(f"/collections/{nostem.id}/offsite").json()["held"] is True
