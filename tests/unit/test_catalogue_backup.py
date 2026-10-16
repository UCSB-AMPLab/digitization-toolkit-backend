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

The dump command is a stub written to tmp_path and injected through settings,
and the offsite transport is faked at its two seam functions, upload() and
listing_sha1(), over an in-memory bucket. Nothing here needs a real docker, a
real Postgres or a real bucket, and nothing depends on how a transport is
built. Everything else — the gzip stream, the SHA1, the subprocess, the unlink —
is the production path.

    python -m pytest tests/unit/test_catalogue_backup.py
"""

import gzip
import hashlib
import stat
import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# The dump stub, injected by configuration
# ---------------------------------------------------------------------------

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


class FakeBucket:
    """An in-memory bucket behind the two seam functions.

    `objects` maps name -> sha1 of the bytes stored; `received` maps name -> the
    bytes upload() was handed. Every change to the bucket is appended to
    `mutations` as (operation, name), where an upload to a name that is already
    present is recorded as "overwrite". The seam has no delete, so "delete" can
    only appear if a future transport adds one.

    Failure is steered by `fail(*modes)`:
        upload   -> upload() raises BackupError
        listing  -> listing_sha1() raises VerificationUnavailable (must abort)
        corrupt  -> the listing reports a SHA1 that is not the object's
        absent   -> the listing reports the uploaded object as missing
    """

    def __init__(self, module):
        self.module = module
        self.objects = {}
        self.received = {}
        self.mutations = []
        self.upload_calls = []
        self.listing_calls = []
        self.modes = set()

    def fail(self, *modes):
        self.modes = set(modes)

    def upload(self, local):
        self.upload_calls.append(Path(local).name)
        if "upload" in self.modes:
            raise self.module.BackupError("fake: upload refused")
        name = Path(local).name
        data = Path(local).read_bytes()
        self.mutations.append(("overwrite" if name in self.objects else "put", name))
        self.received[name] = data
        self.objects[name] = hashlib.sha1(data).hexdigest()

    def listing_sha1(self, name):
        self.listing_calls.append(name)
        if "listing" in self.modes:
            raise self.module.VerificationUnavailable(
                "fake: could not list the bucket (401 Unauthorized)"
            )
        if "absent" in self.modes:
            return None
        if "corrupt" in self.modes:
            return "0" * 40
        return self.objects.get(name)


@pytest.fixture
def offsite(tmp_path, monkeypatch, db_session):
    """Wire the backup module to a stub dump command and a fake bucket.

    Depends on db_session so it runs *after* override_projects_root's
    importlib.reload of app.core.config — otherwise it would patch a Settings
    instance the reload has already replaced.
    """
    from app.core.config import settings

    dump_tmp = tmp_path / "dtk" / "tmp"
    dump_tmp.mkdir(parents=True)

    pg_dump = _install(tmp_path, "pg-dump-stub", _PG_DUMP_STUB)

    monkeypatch.delenv("STUB_DUMP_FAIL", raising=False)
    monkeypatch.delenv("STUB_DUMP_EMPTY", raising=False)

    monkeypatch.setattr(settings, "OFFSITE_REMOTE", "b2stub:bucket", raising=False)
    monkeypatch.setattr(settings, "OFFSITE_DB_PREFIX", "db-backups", raising=False)
    monkeypatch.setattr(settings, "OFFSITE_BWLIMIT", "750k", raising=False)
    monkeypatch.setattr(settings, "PG_DUMP_COMMAND", str(pg_dump), raising=False)

    import app.core.catalogue_backup as cb

    monkeypatch.setattr(cb, "_dump_dir", lambda: dump_tmp)

    # Kept so a test can put the module's own, unbuilt transport back.
    unbuilt_upload, unbuilt_listing = cb.upload, cb.listing_sha1

    bucket = FakeBucket(cb)
    monkeypatch.setattr(cb, "upload", bucket.upload)
    monkeypatch.setattr(cb, "listing_sha1", bucket.listing_sha1)

    class Harness:
        module = cb
        dump_dir = dump_tmp
        fail = staticmethod(bucket.fail)
        real_upload = staticmethod(unbuilt_upload)
        real_listing_sha1 = staticmethod(unbuilt_listing)

        @staticmethod
        def objects():
            return dict(bucket.objects)

        @staticmethod
        def received():
            return dict(bucket.received)

        @staticmethod
        def mutations():
            return list(bucket.mutations)

        @staticmethod
        def upload_calls():
            return list(bucket.upload_calls)

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
    offsite.fail("listing")

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
    offsite.fail("listing")

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
    assert len(offsite.objects()) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "arrange",
    [
        pytest.param(lambda h, mp: h.fail("upload"), id="upload-refused"),
        pytest.param(lambda h, mp: h.fail("listing"), id="listing-refused"),
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
    assert offsite.upload_calls() == []


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
    assert len(offsite.upload_calls()) == 1


@pytest.mark.unit
def test_a_failed_run_is_retried_on_the_next_tick(offsite, db_session):
    """No verified marker for today means the next run tries again."""
    cb = offsite.module
    offsite.fail("listing")
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
# The rule: nothing already offsite is removed or replaced
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_nothing_in_a_run_removes_or_replaces_an_object_already_in_the_bucket(
    offsite, db_session, monkeypatch
):
    """The key cannot delete or overwrite. Neither may the code try, on any path.

    Runs the whole pipeline through success, a refused upload, a mismatched
    digest, an absent object and a refused listing, with an object already in
    the bucket, and asserts that the fake recorded only first-time puts of new
    names and that the pre-existing object is untouched.
    """
    from datetime import datetime, timezone

    cb = offsite.module
    pre_existing = "catalogue-19990101-000000.sql.gz"
    # Seeded through the fake's own upload path so it is a genuine stored object.
    seed = offsite.dump_dir / pre_existing
    seed.write_bytes(b"an earlier dump")
    cb.upload(seed)
    seed.unlink()
    before = offsite.objects()[pre_existing]
    seeded = len(offsite.mutations())

    # A distinct name per run, as in production where runs are separate ticks.
    ticks = iter(range(100))

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 1, 12, 0, next(ticks), tzinfo=tz or timezone.utc)

    monkeypatch.setattr(cb, "datetime", _Clock)

    for mode in ((), ("corrupt",), ("absent",), ("listing",), ("upload",)):
        offsite.fail(*mode)
        cb.run_catalogue_backup(db_session)
    offsite.fail()
    cb.run_catalogue_backup(db_session)

    recorded = offsite.mutations()[seeded:]
    assert recorded, "the runs should have uploaded something"
    assert {op for op, _ in recorded} == {"put"}, recorded
    assert pre_existing not in {name for _, name in recorded}
    assert offsite.objects()[pre_existing] == before


# ---------------------------------------------------------------------------
# The transport that is not built yet fails through the module's own contracts
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_unbuilt_upload_is_a_failed_run_that_leaves_no_marker(offsite, db_session, monkeypatch):
    cb = offsite.module
    monkeypatch.setattr(cb, "upload", offsite.real_upload)

    result = cb.run_catalogue_backup(db_session)

    assert result["status"] == "failed", result
    assert "not built" in result["error"]
    assert cb.last_verified_backup(db_session) is None
    assert cb.needs_run_today(db_session) is True
    assert offsite.leftovers() == []


@pytest.mark.unit
def test_the_unbuilt_listing_aborts_the_run_and_leaves_no_dump(offsite, db_session, monkeypatch):
    cb = offsite.module
    monkeypatch.setattr(cb, "listing_sha1", offsite.real_listing_sha1)

    result = cb.run_catalogue_backup(db_session)

    assert result["status"] == "aborted", result
    assert "not built" in result["error"]
    assert cb.last_verified_backup(db_session) is None
    assert offsite.leftovers() == []


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
    """The bytes handed to upload() are the dump, and their digest is the one
    verification compares against the listing."""
    cb = offsite.module
    result = cb.run_catalogue_backup(db_session)

    assert result["status"] == "verified", result
    received = offsite.received()
    assert list(received) == [result["object"]]
    assert result["object"].endswith(".sql.gz")
    body = gzip.decompress(received[result["object"]]).decode()
    assert "stub catalogue dump" in body
    assert hashlib.sha1(received[result["object"]]).hexdigest() == result["sha1"]
    assert offsite.objects()[result["object"]] == result["sha1"]


@pytest.mark.unit
def test_the_run_is_disabled_without_a_configured_remote(offsite, db_session, monkeypatch):
    from app.core.config import settings

    cb = offsite.module
    monkeypatch.setattr(settings, "OFFSITE_REMOTE", "", raising=False)

    assert cb.run_catalogue_backup(db_session)["status"] == "disabled"
    assert offsite.upload_calls() == []


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
