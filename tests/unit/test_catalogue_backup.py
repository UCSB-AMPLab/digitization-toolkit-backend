#!/usr/bin/env python3
"""Offsite backup of the catalogue database (NEH-249).

On 2026-09-15 an appliance card filled completely while 87.7 GiB of images sat
safely in the bucket and no database dump did. These tests pin the three
behaviours that decide whether that can happen again:

* a bucket listing that cannot answer aborts the run, and aborts it *before*
  the dump is treated as verified and before the dump file is removed — a check
  that cannot answer is not a negative answer (the rule `free-masters.py` is
  built around);
* the dump never survives the run, on any path, because the failure it guards
  against is a card with no room;
* a failed run leaves no "verified today" marker, so the next tick retries it
  instead of silently skipping to tomorrow.

The two external binaries are stubs written to tmp_path and injected through
settings, so nothing here needs a real rclone, a real docker, or a real
Postgres. Everything else — the gzip stream, the SHA1, the subprocess, the
unlink — is the production path.

    python -m pytest tests/unit/test_catalogue_backup.py
"""

import gzip
import hashlib
import json
import stat
import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Stub binaries, injected by configuration
# ---------------------------------------------------------------------------

_RCLONE_STUB = '''#!/usr/bin/env python3
"""Stand-in for rclone. Serves one fake bucket directory out of STUB_BUCKET.

Behaviour is steered by STUB_FAIL, a comma-separated set of:
    copy    -> the upload fails
    lsjson  -> the listing fails (the case that must abort the run)
    corrupt -> the listing reports a SHA1 that is not the file's
    absent  -> the listing omits the uploaded object
"""
import hashlib, json, os, shutil, sys

bucket = os.environ["STUB_BUCKET"]
fail = set(filter(None, os.environ.get("STUB_FAIL", "").split(",")))
log = os.environ.get("STUB_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\\n")

verb = sys.argv[1] if len(sys.argv) > 1 else ""

if verb == "copy":
    if "copy" in fail:
        sys.stderr.write("stub: upload refused\\n")
        sys.exit(1)
    src = sys.argv[2]
    os.makedirs(bucket, exist_ok=True)
    shutil.copy2(src, os.path.join(bucket, os.path.basename(src)))
    sys.exit(0)

if verb == "lsjson":
    if "lsjson" in fail:
        sys.stderr.write("stub: 401 Unauthorized\\n")
        sys.exit(1)
    rows = []
    if not ("absent" in fail) and os.path.isdir(bucket):
        for name in sorted(os.listdir(bucket)):
            path = os.path.join(bucket, name)
            h = hashlib.sha1()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            digest = "0" * 40 if "corrupt" in fail else h.hexdigest()
            rows.append({
                "Path": name, "Name": name, "Size": os.path.getsize(path),
                "IsDir": False, "Hashes": {"sha1": digest},
            })
    sys.stdout.write(json.dumps(rows))
    sys.exit(0)

sys.stderr.write("stub: unsupported verb %r\\n" % verb)
sys.exit(2)
'''

_PG_DUMP_STUB = '''#!/usr/bin/env python3
"""Stand-in for `docker compose exec -T db pg_dumpall`.

STUB_DUMP_FAIL makes it exit non-zero; STUB_DUMP_EMPTY makes it succeed while
writing almost nothing, which is the shape of the failure update.sh guards
against — a failed dump still gzips to a valid ~20-byte stream.
"""
import os, sys

if os.environ.get("STUB_DUMP_FAIL"):
    sys.stderr.write("stub: could not connect to server\\n")
    sys.exit(1)
if os.environ.get("STUB_DUMP_EMPTY"):
    sys.exit(0)
sys.stdout.write("-- stub catalogue dump\\n")
for i in range(200):
    sys.stdout.write("INSERT INTO records VALUES (%d, 'folio %dr');\\n" % (i, i))
sys.exit(0)
'''


def _install(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(source))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def offsite(tmp_path, monkeypatch, db_session):
    """Wire the backup module to stub binaries and a fake bucket directory.

    Depends on db_session so it runs *after* override_projects_root's
    importlib.reload of app.core.config — otherwise it would patch a Settings
    instance the reload has already replaced.
    """
    from app.core.config import settings

    bucket = tmp_path / "bucket" / "db-backups"
    dump_tmp = tmp_path / "dtk" / "tmp"
    dump_tmp.mkdir(parents=True)
    calls = tmp_path / "rclone-calls.jsonl"

    rclone = _install(tmp_path, "rclone-stub", _RCLONE_STUB)
    pg_dump = _install(tmp_path, "pg-dump-stub", _PG_DUMP_STUB)

    monkeypatch.setenv("STUB_BUCKET", str(bucket))
    monkeypatch.setenv("STUB_LOG", str(calls))
    monkeypatch.setenv("STUB_FAIL", "")
    monkeypatch.delenv("STUB_DUMP_FAIL", raising=False)
    monkeypatch.delenv("STUB_DUMP_EMPTY", raising=False)

    monkeypatch.setattr(settings, "OFFSITE_REMOTE", "b2stub:bucket", raising=False)
    monkeypatch.setattr(settings, "OFFSITE_DB_PREFIX", "db-backups", raising=False)
    monkeypatch.setattr(settings, "OFFSITE_BWLIMIT", "750k", raising=False)
    monkeypatch.setattr(settings, "RCLONE_BIN", str(rclone), raising=False)
    monkeypatch.setattr(settings, "RCLONE_CONFIG", "", raising=False)
    monkeypatch.setattr(settings, "PG_DUMP_COMMAND", str(pg_dump), raising=False)

    import app.core.catalogue_backup as cb

    monkeypatch.setattr(cb, "_dump_dir", lambda: dump_tmp)

    class Harness:
        module = cb
        bucket_dir = bucket
        dump_dir = dump_tmp

        @staticmethod
        def fail(*modes):
            monkeypatch.setenv("STUB_FAIL", ",".join(modes))

        @staticmethod
        def rclone_calls():
            if not calls.exists():
                return []
            return [json.loads(line) for line in calls.read_text().splitlines()]

        @staticmethod
        def leftovers():
            return sorted(p.name for p in dump_tmp.iterdir())

    return Harness


# ---------------------------------------------------------------------------
# The rule: a listing that cannot answer is not a negative answer
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_failed_listing_aborts_the_run(offsite, db_session):
    """A refused listing must abort, not be read as "the object is missing"."""
    cb = offsite.module
    offsite.fail("lsjson")

    result = cb.run_catalogue_backup(db_session)

    assert result["status"] == "aborted"
    # Same wording as free-masters.py's abort, for the same key and the same cause.
    assert "could not list" in result["error"].lower()
    assert cb.last_verified_backup(db_session) is None


@pytest.mark.unit
def test_a_failed_listing_aborts_before_the_dump_is_removed(offsite, db_session, monkeypatch):
    """The abort decision must precede the removal, as in free-masters.py.

    Recorded as an ordering assertion rather than a leftover file: the dump is
    still removed (a filling card must never keep one), but it is removed by the
    cleanup path after the run has been declared a failure, never as the tail of
    a "verified" path.
    """
    cb = offsite.module
    offsite.fail("lsjson")

    order = []
    real_unlink = Path.unlink

    def recording_unlink(self, *a, **kw):
        order.append(("unlink", self.name))
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", recording_unlink)

    real_verify = cb.verify_against_listing

    def recording_verify(*a, **kw):
        try:
            return real_verify(*a, **kw)
        except cb.VerificationUnavailable:
            order.append(("abort", "listing"))
            raise

    monkeypatch.setattr(cb, "verify_against_listing", recording_verify)

    cb.run_catalogue_backup(db_session)

    assert ("abort", "listing") in order, order
    unlinks = [i for i, step in enumerate(order) if step[0] == "unlink"]
    assert unlinks, "the dump must not be left on the card"
    assert order.index(("abort", "listing")) < min(unlinks)


@pytest.mark.unit
def test_an_object_absent_from_the_listing_is_not_verified(offsite, db_session):
    cb = offsite.module
    offsite.fail("absent")

    result = cb.run_catalogue_backup(db_session)

    assert result["status"] == "failed"
    assert cb.last_verified_backup(db_session) is None


@pytest.mark.unit
def test_a_digest_mismatch_is_not_verified(offsite, db_session):
    cb = offsite.module
    offsite.fail("corrupt")

    result = cb.run_catalogue_backup(db_session)

    assert result["status"] == "failed"
    assert "sha1" in result["error"].lower()
    assert cb.last_verified_backup(db_session) is None


# ---------------------------------------------------------------------------
# The rule: dumps never accumulate on the card
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_verified_run_leaves_no_dump_on_the_card(offsite, db_session):
    cb = offsite.module

    result = cb.run_catalogue_backup(db_session)

    assert result["status"] == "verified", result
    assert offsite.leftovers() == []
    assert len(list(offsite.bucket_dir.iterdir())) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "arrange",
    [
        pytest.param(lambda h, mp: h.fail("copy"), id="upload-refused"),
        pytest.param(lambda h, mp: h.fail("lsjson"), id="listing-refused"),
        pytest.param(lambda h, mp: h.fail("corrupt"), id="digest-mismatch"),
        pytest.param(lambda h, mp: mp.setenv("STUB_DUMP_FAIL", "1"), id="dump-failed"),
        pytest.param(lambda h, mp: mp.setenv("STUB_DUMP_EMPTY", "1"), id="dump-empty"),
    ],
)
def test_no_failure_path_leaves_a_dump_on_the_card(offsite, db_session, monkeypatch, arrange):
    cb = offsite.module
    arrange(offsite, monkeypatch)

    result = cb.run_catalogue_backup(db_session)

    assert result["status"] in ("failed", "aborted"), result
    assert offsite.leftovers() == []


@pytest.mark.unit
def test_an_empty_dump_is_refused_before_it_is_uploaded(offsite, db_session, monkeypatch):
    """A failed pg_dumpall still gzips to a valid ~20-byte stream (update.sh:445)."""
    cb = offsite.module
    monkeypatch.setenv("STUB_DUMP_EMPTY", "1")

    result = cb.run_catalogue_backup(db_session)

    assert result["status"] == "failed"
    assert "empty" in result["error"].lower()
    assert [c for c in offsite.rclone_calls() if c and c[0] == "copy"] == []


# ---------------------------------------------------------------------------
# The rule: retry rather than skip
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_verified_run_is_not_repeated_the_same_day(offsite, db_session):
    cb = offsite.module

    first = cb.run_catalogue_backup(db_session)
    second = cb.run_catalogue_backup(db_session)

    assert first["status"] == "verified"
    assert second["status"] == "up-to-date"
    assert len([c for c in offsite.rclone_calls() if c and c[0] == "copy"]) == 1


@pytest.mark.unit
def test_a_failed_run_is_retried_on_the_next_tick(offsite, db_session):
    """No verified marker for today means the next run tries again."""
    cb = offsite.module
    offsite.fail("lsjson")
    assert cb.run_catalogue_backup(db_session)["status"] == "aborted"

    offsite.fail()
    assert cb.run_catalogue_backup(db_session)["status"] == "verified"


@pytest.mark.unit
def test_a_verified_dump_from_an_earlier_day_does_not_satisfy_today(offsite, db_session):
    from datetime import datetime, timedelta, timezone

    cb = offsite.module
    assert cb.run_catalogue_backup(db_session)["status"] == "verified"

    from app.models.system_log import SystemLog

    row = (
        db_session.query(SystemLog)
        .filter(SystemLog.action == cb.VERIFIED_ACTION)
        .one()
    )
    row.created_at = datetime.now(timezone.utc) - timedelta(days=2)
    db_session.commit()

    assert cb.run_catalogue_backup(db_session)["status"] == "verified"


# ---------------------------------------------------------------------------
# The commands we hand to rclone
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_upload_skips_the_destination_check_and_caps_bandwidth(offsite, db_session):
    """The scoped key is write + list: rclone's default destination HEAD 401s."""
    cb = offsite.module
    cb.run_catalogue_backup(db_session)

    copy = next(c for c in offsite.rclone_calls() if c and c[0] == "copy")
    assert "--no-check-dest" in copy
    assert "--bwlimit" in copy
    assert copy[copy.index("--bwlimit") + 1] == "750k"


@pytest.mark.unit
def test_the_listing_asks_for_sha1_hashes(offsite, db_session):
    cb = offsite.module
    cb.run_catalogue_backup(db_session)

    ls = next(c for c in offsite.rclone_calls() if c and c[0] == "lsjson")
    assert "--hash" in ls
    assert ls[ls.index("--hash-type") + 1] == "sha1"


@pytest.mark.unit
def test_nothing_in_the_pipeline_asks_rclone_to_remove_anything(offsite, db_session):
    """The key cannot delete. Neither may the code try, on any path."""
    cb = offsite.module
    forbidden = {
        "delete", "deletefile", "purge", "rmdir", "rmdirs", "sync", "move",
        "moveto", "--delete-before", "--delete-during", "--delete-after",
        "--delete-excluded",
    }

    for arrange in (lambda: None, lambda: offsite.fail("corrupt"), lambda: offsite.fail("absent")):
        arrange()
        cb.run_catalogue_backup(db_session)

    for call in offsite.rclone_calls():
        assert not (set(call) & forbidden), call


@pytest.mark.unit
def test_the_default_dump_goes_through_the_database_container(monkeypatch):
    """No postgres client in pixi.toml; update.sh:438 is the precedent."""
    from app.core.config import settings
    import app.core.catalogue_backup as cb

    monkeypatch.setattr(settings, "PG_DUMP_COMMAND", "", raising=False)
    monkeypatch.setattr(settings, "DATABASE_USER", "dtkuser")

    argv = cb.dump_argv()

    assert argv[:2] == ["docker", "compose"]
    assert "exec" in argv and "-T" in argv
    assert argv[argv.index("-T") + 1] == "db"
    assert "pg_dumpall" in argv
    assert argv[argv.index("-U") + 1] == "dtkuser"


@pytest.mark.unit
def test_the_dump_leaves_role_passwords_on_the_appliance(monkeypatch):
    """pg_dumpall ships role definitions. Without this flag it ships the
    per-unit database password's SCRAM verifier with them, which would send a
    credential off the appliance on a schedule.

    The flag keeps the CREATE ROLE statements, which is the reason pg_dumpall
    was chosen over pg_dump, and drops only the secrets — which a restore
    target has no use for anyway, since a freshly provisioned unit generates
    its own password.
    """
    from app.core.config import settings
    import app.core.catalogue_backup as cb

    monkeypatch.setattr(settings, "PG_DUMP_COMMAND", "", raising=False)

    assert "--no-role-passwords" in cb.dump_argv()


@pytest.mark.unit
def test_an_explicit_dump_command_overrides_the_default(monkeypatch):
    from app.core.config import settings
    import app.core.catalogue_backup as cb

    monkeypatch.setattr(settings, "PG_DUMP_COMMAND", "/usr/bin/pg_dumpall -U me", raising=False)

    assert cb.dump_argv() == ["/usr/bin/pg_dumpall", "-U", "me"]


# ---------------------------------------------------------------------------
# What the dump actually contains, and what the operator can see
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_uploaded_object_is_the_gzipped_dump(offsite, db_session):
    cb = offsite.module
    result = cb.run_catalogue_backup(db_session)

    uploaded = next(offsite.bucket_dir.iterdir())
    assert uploaded.name.endswith(".sql.gz")
    body = gzip.decompress(uploaded.read_bytes()).decode()
    assert "stub catalogue dump" in body
    assert hashlib.sha1(uploaded.read_bytes()).hexdigest() == result["sha1"]


@pytest.mark.unit
def test_the_run_is_disabled_without_a_configured_remote(offsite, db_session, monkeypatch):
    from app.core.config import settings

    cb = offsite.module
    monkeypatch.setattr(settings, "OFFSITE_REMOTE", "", raising=False)

    assert cb.run_catalogue_backup(db_session)["status"] == "disabled"
    assert offsite.rclone_calls() == []


@pytest.mark.unit
def test_the_scheduler_does_not_start_without_a_configured_remote(offsite, monkeypatch):
    from app.core.config import settings

    cb = offsite.module
    monkeypatch.setattr(settings, "OFFSITE_REMOTE", "", raising=False)

    scheduler = cb.CatalogueBackupScheduler()
    started = scheduler.start()
    try:
        assert started is False
        assert scheduler.is_running() is False
    finally:
        scheduler.stop()


@pytest.mark.unit
def test_last_verified_backup_reports_the_most_recent_verified_run(offsite, db_session):
    cb = offsite.module
    assert cb.last_verified_backup(db_session) is None

    cb.run_catalogue_backup(db_session)

    state = cb.last_verified_backup(db_session)
    assert state is not None
    assert state["object"].endswith(".sql.gz")
    assert state["verified_at"] is not None


@pytest.fixture
def reader_client(client):
    """`client` with the system router's read-only role check satisfied.

    system.py binds its own RoleChecker instance, so that exact object is the
    one to override (see test_system_storage_helper.py).
    """
    from app.main import app
    from app.models.user import User
    import app.api.system as system

    def _reader():
        return User(username="reader1", email="r@example.com",
                    hashed_password="x", role="reviewer", is_active=True)

    app.dependency_overrides[system.allow_read_only] = _reader
    yield client


@pytest.mark.unit
def test_the_status_endpoint_reports_the_last_verified_dump(offsite, reader_client, db_session):
    cb = offsite.module
    client = reader_client

    before = client.get("/system/catalogue-backup")
    assert before.status_code == 200
    assert before.json()["last_verified_at"] is None
    assert before.json()["enabled"] is True

    cb.run_catalogue_backup(db_session)

    after = client.get("/system/catalogue-backup").json()
    assert after["last_verified_at"] is not None
    assert after["object"].endswith(".sql.gz")


@pytest.mark.unit
def test_the_status_endpoint_is_read_only(offsite, reader_client):
    for method in ("post", "put", "patch", "delete"):
        response = getattr(reader_client, method)("/system/catalogue-backup")
        assert response.status_code == 405, (method, response.status_code)
