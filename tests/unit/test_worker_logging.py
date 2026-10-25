"""The offsite and Fisqua workers write a line to the journal for each run that did something.

Each worker logs through its module's logger (app.core.image_upload,
app.core.catalogue_backup, app.core.fisqua_outbox, app.core.fisqua_decisions),
which app.core.logging_setup's handler on the `app` logger carries to stderr
and so to the journal, from the scheduler threads too. A run that did
something writes one INFO line with its counts; a failure a WARNING or an
ERROR. No line carries the token or a key's credentials.

    python -m pytest tests/unit/test_worker_logging.py
"""

import logging
import threading

import httpx
import pytest

from tests.unit.test_capture_manifest_export import TENANT, Volume, _as, tenant, vol  # noqa: F401
from tests.unit.test_catalogue_backup import offsite  # noqa: F401
from tests.unit.test_fisqua_http import Server, _ok, _poll, _send, queued  # noqa: F401
from tests.unit.test_fisqua_outbox import TOKEN, _handover, _rows, fisqua  # noqa: F401
from tests.unit.test_image_upload import _image, _run, _volume, remote  # noqa: F401

WORKER_LOGGERS = ("app.core.image_upload", "app.core.catalogue_backup",
                  "app.core.fisqua_outbox", "app.core.fisqua_decisions")


def _lines(caplog, name, level=logging.INFO):
    return [r.getMessage() for r in caplog.records if r.name == name and r.levelno == level]


# ---------------------------------------------------------------------------
# The handler reaches the workers' loggers, from any thread
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("name", WORKER_LOGGERS)
def test_the_journal_handler_carries_each_workers_info_lines_from_a_thread(name, capsys):
    from app.core.logging_setup import HANDLER_NAME, configure_logging
    configure_logging("INFO")
    logger = logging.getLogger(name)
    assert logger.isEnabledFor(logging.INFO)
    chain, current = [], logger
    while current is not None:
        chain += [h.get_name() for h in current.handlers]
        current = current.parent if current.propagate else None
    assert HANDLER_NAME in chain

    t = threading.Thread(target=lambda: logger.info("a line from the scheduler thread"))
    t.start()
    t.join()
    assert f"INFO {name}: a line from the scheduler thread" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Image upload
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_upload_run_that_did_something_writes_one_info_line(remote, db_session, override_projects_root, caplog):
    caplog.set_level(logging.INFO, logger="app.core.image_upload")
    v = _volume(db_session)
    _image(db_session, v, override_projects_root, "f1.CR2", b"raw one", preview=b"preview one")
    _image(db_session, v, override_projects_root, "f2.jpg", b"jpeg two")
    _run(db_session)
    [line] = [m for m in _lines(caplog, "app.core.image_upload") if m.startswith("Image upload run")]
    assert line == ("Image upload run done: uploaded=3 already_present=0 unverified=0 refused=0 "
                    "conflicts=0 skipped=0")


@pytest.mark.unit
def test_an_upload_run_with_nothing_to_do_writes_nothing(remote, db_session, caplog):
    caplog.set_level(logging.INFO, logger="app.core.image_upload")
    _run(db_session)
    assert [m for m in _lines(caplog, "app.core.image_upload") if m.startswith("Image upload run")] == []


# ---------------------------------------------------------------------------
# Catalogue backup
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_verified_backup_writes_one_info_line(offsite, db_session, caplog):
    caplog.set_level(logging.INFO, logger="app.core.catalogue_backup")
    assert offsite.module.run_catalogue_backup(db_session)["status"] == "verified"
    assert [m for m in _lines(caplog, "app.core.catalogue_backup")
            if m.startswith("Catalogue backup verified at")] != []


@pytest.mark.unit
def test_an_aborted_backup_writes_a_warning(offsite, db_session, caplog):
    caplog.set_level(logging.INFO, logger="app.core.catalogue_backup")
    offsite.fail("listing")
    assert offsite.module.run_catalogue_backup(db_session)["status"] == "aborted"
    assert [m for m in _lines(caplog, "app.core.catalogue_backup", logging.WARNING)
            if m.startswith("Catalogue backup aborted")] != []


# ---------------------------------------------------------------------------
# Fisqua: sending, polling, decisions
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_send_pass_writes_its_summary_and_each_accepted_manifest(db_session, queued, caplog):
    caplog.set_level(logging.INFO, logger="app.core.fisqua_outbox")
    _send(db_session, Server(_ok(state="received")))
    row = _rows(db_session, queued.col)[0]
    lines = _lines(caplog, "app.core.fisqua_outbox")
    assert f"Fisqua accepted manifest {row.manifest_id} of volume {queued.col.id}: received" in lines
    assert ("Fisqua outbox pass done: promoted=1 not_exportable=0 delivered=1 refused=0 failed=0 waiting=0"
            in lines)


@pytest.mark.unit
def test_a_pass_with_nothing_to_do_writes_nothing(db_session, fisqua, caplog):
    caplog.set_level(logging.INFO, logger="app.core.fisqua_outbox")
    _send(db_session, Server())
    assert [m for m in _lines(caplog, "app.core.fisqua_outbox") if m.startswith("Fisqua outbox pass")] == []


@pytest.mark.unit
def test_a_failed_send_writes_a_warning_without_the_token(db_session, queued, caplog):
    caplog.set_level(logging.INFO)
    _send(db_session, Server(httpx.Response(503, json={"echo": TOKEN})))
    warnings = _lines(caplog, "app.core.fisqua_outbox", logging.WARNING)
    assert any("failed (attempt 1)" in m for m in warnings)
    assert TOKEN not in caplog.text


@pytest.mark.unit
def test_a_poll_writes_its_summary_with_the_changes(db_session, queued, caplog):
    _send(db_session, Server(_ok(state="received")))
    row = _rows(db_session, queued.col)[0]
    caplog.set_level(logging.INFO, logger="app.core.fisqua_outbox")
    answer = {"volumes": [{"ref": queued.stem, "manifestId": row.manifest_id, "state": "registered", "problems": []}]}
    _poll(db_session, Server(httpx.Response(200, json=answer)))
    lines = [m for m in _lines(caplog, "app.core.fisqua_outbox") if m.startswith("Fisqua status poll")]
    assert lines == ["Fisqua status poll done: asked=1 calls=1 answered=1 changed=1 recovered=0 "
                     "decisions_fetched=0 decisions_applied=0 decisions_stale=0"]
    caplog.clear()
    _poll(db_session, Server(httpx.Response(200, json=answer)))
    lines = [m for m in _lines(caplog, "app.core.fisqua_outbox") if m.startswith("Fisqua status poll")]
    assert lines == ["Fisqua status poll done: asked=1 calls=1 answered=1 changed=0 recovered=0 "
                     "decisions_fetched=0 decisions_applied=0 decisions_stale=0"]


@pytest.mark.unit
def test_a_poll_with_nothing_to_ask_writes_nothing(db_session, fisqua, caplog):
    caplog.set_level(logging.INFO, logger="app.core.fisqua_outbox")
    _poll(db_session, Server())
    assert [m for m in _lines(caplog, "app.core.fisqua_outbox") if m.startswith("Fisqua status poll")] == []


@pytest.mark.unit
def test_applied_decisions_write_one_info_line(client, db_session, vol, fisqua, caplog):
    from app.core.fisqua_decisions import apply_decisions
    rec = vol.record(status="captured")
    _handover(client, vol.col)
    _send(db_session, Server(_ok()))
    row = _rows(db_session, vol.col)[0]
    caplog.set_level(logging.INFO, logger="app.core.fisqua_decisions")
    apply_decisions(db_session, vol.col.id, {"decisionsId": "d-1", "manifestId": row.manifest_id, "records": [
        {"captuaRecordId": rec.id, "decision": "approved", "decidedBy": "Ana", "rotations": []},
        {"captuaRecordId": 999999, "decision": "approved", "decidedBy": "Ana", "rotations": []}]})
    assert _lines(caplog, "app.core.fisqua_decisions") == [
        f"Fisqua decisions d-1 applied to volume {vol.col.id}: approved=1 rejected=0 unchanged=0 "
        "skipped=1 rotations=0 rotations_skipped=0"]
