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

Version: 0.1.0-beta
"""

import errno
import gzip
import hashlib
import json
import logging
import os
import stat
import subprocess
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

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

_PG_DUMP_STALL_STUB = '''#!/usr/bin/env python3
"""A dump that writes nothing and does not exit.

The sleep is bounded only so that a test run cannot leave a process behind for
ever; it is far longer than any deadline a test gives the module, so from the
module's side this child never finishes on its own.
"""
import time

time.sleep(20)
'''

# The one object the fake listing carries, and the checksum the module is
# supposed to read out of it. A transport that drops matching objects'
# checksums fails every test that reads one.
_LISTED_NAME = "catalogue-20261016-000000-a1b2c3.sql.gz.age"
_LISTED_SHA1 = "6f" * 20


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

    dump_tmp = tmp_path / "captua" / "tmp"
    dump_tmp.mkdir(parents=True)

    pg_dump = _install(tmp_path, "pg-dump-stub", _PG_DUMP_STUB)

    monkeypatch.delenv("STUB_DUMP_FAIL", raising=False)
    monkeypatch.delenv("STUB_DUMP_EMPTY", raising=False)

    monkeypatch.setattr(settings, "OFFSITE_REMOTE", "b2stub:bucket", raising=False)
    monkeypatch.setattr(settings, "OFFSITE_DB_PREFIX", "db-backups", raising=False)
    monkeypatch.setattr(settings, "OFFSITE_TENANT", "captua-test", raising=False)
    # A throwaway keypair: the unit is given only the public half, and the
    # tests decrypt with the private half to prove the round trip.
    import pyrage
    identity = pyrage.x25519.Identity.generate()
    monkeypatch.setattr(settings, "OFFSITE_DUMP_RECIPIENT", str(identity.to_public()), raising=False)
    monkeypatch.setattr(settings, "OFFSITE_BWLIMIT", "750k", raising=False)
    monkeypatch.setattr(settings, "PG_DUMP_COMMAND", str(pg_dump), raising=False)

    import app.core.catalogue_backup as cb

    monkeypatch.setattr(cb, "_dump_dir", lambda: dump_tmp)

    # Kept so a test can call the module's own rclone transport directly,
    # with subprocess captured, instead of going through the fake bucket.
    real_upload_fn, real_listing_fn = cb.upload, cb.listing_sha1

    bucket = FakeBucket(cb)
    monkeypatch.setattr(cb, "upload", bucket.upload)
    monkeypatch.setattr(cb, "listing_sha1", bucket.listing_sha1)

    class Harness:
        module = cb
        dump_dir = dump_tmp
        key = identity
        fail = staticmethod(bucket.fail)
        real_upload = staticmethod(real_upload_fn)
        real_listing_sha1 = staticmethod(real_listing_fn)

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
    # The plaintext is removed as soon as the ciphertext exists, before the
    # upload; the ciphertext is the dump whose removal must wait for the abort.
    unlinks = [i for i, step in enumerate(order)
               if step[0] == "unlink" and step[1].endswith(cb.AGE_SUFFIX)]
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
    """A failed pg_dumpall still gzips to a valid ~20-byte stream (the same guard as scripts/update.sh's database dump)."""
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
@pytest.mark.parametrize(
    "seconds_apart",
    [
        pytest.param(1, id="a-second-apart"),
        # The collision the one-second stamp actually permits: two ticks inside
        # the same second. Without this case, a name made from the stamp alone
        # passes the no-overwrite test.
        pytest.param(0, id="same-second"),
    ],
)
def test_nothing_in_a_run_removes_or_replaces_an_object_already_in_the_bucket(
    offsite, db_session, monkeypatch, seconds_apart
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

    offsets = iter(range(0, 100 * seconds_apart + 1, seconds_apart)) \
        if seconds_apart else iter(lambda: 0, 1)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(
                2026, 10, 1, 12, 0, next(offsets), tzinfo=tz or timezone.utc
            )

    monkeypatch.setattr(cb, "datetime", _Clock)

    # The failure modes first and the success last: a verified run marks the
    # frozen day as done, and every run after it would return up-to-date
    # without uploading anything, which would leave this assertion with one
    # object to look at instead of five.
    for mode in (("corrupt",), ("absent",), ("listing",), ("upload",)):
        offsite.fail(*mode)
        cb.run_catalogue_backup(db_session)
    offsite.fail()
    assert cb.run_catalogue_backup(db_session)["status"] == "verified"

    recorded = offsite.mutations()[seeded:]
    assert recorded, "the runs should have uploaded something"
    assert {op for op, _ in recorded} == {"put"}, recorded
    assert pre_existing not in {name for _, name in recorded}
    assert offsite.objects()[pre_existing] == before


# ---------------------------------------------------------------------------
# The rclone invocation itself, called directly with subprocess captured
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_default_dump_goes_through_the_database_container(monkeypatch):
    """No postgres client in pixi.toml; scripts/update.sh's database dump is the precedent."""
    from app.core.config import settings
    import app.core.catalogue_backup as cb

    monkeypatch.setattr(settings, "PG_DUMP_COMMAND", "", raising=False)
    monkeypatch.setattr(settings, "DATABASE_USER", "captuauser")

    argv = cb.dump_argv()

    assert argv[:2] == ["docker", "compose"]
    assert "exec" in argv and "-T" in argv
    assert argv[argv.index("-T") + 1] == "db"
    assert "pg_dumpall" in argv
    assert argv[argv.index("-U") + 1] == "captuauser"


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

    monkeypatch.setattr(
        settings, "PG_DUMP_COMMAND", "/opt/captua/bin/dump-catalogue --stdout",
        raising=False,
    )

    assert cb.dump_argv() == ["/opt/captua/bin/dump-catalogue", "--stdout"]


# ---------------------------------------------------------------------------
# What the dump actually contains, and what the operator can see
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_uploaded_object_is_the_encrypted_gzipped_dump(offsite, db_session):
    """The bytes handed to upload() are the dump, and their digest is the one
    verification compares against the listing."""
    cb = offsite.module
    result = cb.run_catalogue_backup(db_session)

    assert result["status"] == "verified", result
    received = offsite.received()
    assert list(received) == [result["object"]]
    assert result["object"].endswith(".sql.gz.age")
    import pyrage
    body = gzip.decompress(pyrage.decrypt(received[result["object"]], [offsite.key])).decode()
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
    """Two verified rows, so "most recent" is a claim the test can check. With
    one row the assertion would hold whichever way the ordering ran."""
    from datetime import datetime, timezone

    from app.models.system_log import SystemLog

    cb = offsite.module
    assert cb.last_verified_backup(db_session) is None

    earlier = SystemLog(
        level="INFO", category=cb.BACKUP_CATEGORY, action=cb.VERIFIED_ACTION,
        subject="catalogue-20260901-030000-eeeeee.sql.gz",
        detail="a verified run from six weeks ago",
        created_at=datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc),
    )
    db_session.add(earlier)
    db_session.commit()

    result = cb.run_catalogue_backup(db_session)
    assert result["status"] == "verified", result

    state = cb.last_verified_backup(db_session)
    assert state is not None
    assert state["object"] == result["object"]
    assert state["object"] != earlier.subject
    assert state["object"].endswith(".sql.gz.age")
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
    assert after["object"].endswith(".sql.gz.age")


@pytest.mark.unit
def test_the_status_endpoint_is_read_only(offsite, reader_client):
    for method in ("post", "put", "patch", "delete"):
        response = getattr(reader_client, method)("/system/catalogue-backup")
        assert response.status_code == 405, (method, response.status_code)

@pytest.fixture
def rclone(offsite, monkeypatch):
    """Capture every rclone call the real transport makes, and answer listings.

    The listing answers with a real row carrying a real checksum, so a test that
    reads a checksum out of it is testing the extraction rather than agreeing
    with an empty list. `rows` and `raises` are settable per test.
    """
    cb = offsite.module

    class Rclone:
        def __init__(self):
            self.calls = []
            self.returncode = 0
            self.raises = None
            self.rows = [
                {
                    "Path": _LISTED_NAME, "Name": _LISTED_NAME, "IsDir": False,
                    "Size": 4096, "Hashes": {"sha1": _LISTED_SHA1},
                },
                # A directory and a different object, so a match is a match on
                # the name rather than on being the only row.
                {"Path": "older", "Name": "older", "IsDir": True, "Hashes": {}},
                {
                    "Path": "catalogue-19990101-000000-zzzzzz.sql.gz.age",
                    "Name": "catalogue-19990101-000000-zzzzzz.sql.gz.age",
                    "IsDir": False, "Hashes": {"sha1": "00" * 20},
                },
            ]

        def run(self, argv, **kwargs):
            self.calls.append(list(argv))
            if self.raises is not None:
                raise self.raises
            stdout = json.dumps(self.rows) if "lsjson" in argv else ""
            return SimpleNamespace(
                returncode=self.returncode, stdout=stdout, stderr=""
            )

    fake = Rclone()
    monkeypatch.setattr(cb.subprocess, "run", fake.run)
    return fake


@pytest.mark.unit
def test_the_upload_skips_the_destination_check_and_caps_bandwidth(offsite, rclone):
    """The scoped key is write + list: rclone's default destination HEAD 401s."""
    dump = offsite.dump_dir / _LISTED_NAME
    dump.write_bytes(b"age-encryption.org/v1\n" + b"x")

    offsite.real_upload(dump)

    copy = next(c for c in rclone.calls if "copy" in c)
    assert "--no-check-dest" in copy
    assert copy[copy.index("--bwlimit") + 1] == "750k"


@pytest.mark.unit
def test_the_object_is_age_ciphertext_for_the_configured_recipient(offsite, db_session):
    """Nothing in the bucket is the plaintext: only the recipient's private key opens it."""
    import pyrage

    cb = offsite.module
    result = cb.run_catalogue_backup(db_session)
    data = offsite.received()[result["object"]]

    assert data.startswith(b"age-encryption.org/v1\n")
    assert b"stub catalogue dump" not in data
    with pytest.raises(pyrage.DecryptError):
        pyrage.decrypt(data, [pyrage.x25519.Identity.generate()])
    assert gzip.decompress(pyrage.decrypt(data, [offsite.key])).startswith(b"-- stub catalogue dump")


@pytest.mark.unit
@pytest.mark.parametrize("recipient", [
    "",
    "age1notarecipient",
    "AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ",
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl",
])
def test_no_dump_is_uploaded_without_a_valid_recipient(offsite, db_session, monkeypatch, caplog, recipient):
    from app.core.config import settings
    from app.models.system_log import SystemLog

    cb = offsite.module
    monkeypatch.setattr(settings, "OFFSITE_DUMP_RECIPIENT", recipient, raising=False)

    with caplog.at_level(logging.ERROR, logger="app.core.catalogue_backup"):
        result = cb.run_catalogue_backup(db_session)

    assert result["status"] == "misconfigured", result
    assert "CAPTUA_OFFSITE_DUMP_RECIPIENT" in result["error"]
    assert offsite.upload_calls() == []
    assert offsite.leftovers() == [], "no dump is written either"
    assert "CAPTUA_OFFSITE_DUMP_RECIPIENT" in caplog.text
    logged = db_session.query(SystemLog).filter(SystemLog.action == cb.FAILED_ACTION).all()
    assert logged and "CAPTUA_OFFSITE_DUMP_RECIPIENT" in logged[-1].detail


@pytest.mark.unit
def test_the_transport_refuses_a_file_that_is_not_age_ciphertext(offsite, rclone):
    cb = offsite.module
    plain = offsite.dump_dir / "catalogue-20261016-000000-a1b2c3.sql.gz"
    plain.write_bytes(gzip.compress(b"-- plaintext"))
    with pytest.raises(cb.BackupError):
        offsite.real_upload(plain)
    assert not [c for c in rclone.calls if "copy" in c]

    named = offsite.dump_dir / "catalogue-20261016-000000-a1b2c3.sql.gz.age"
    named.write_bytes(gzip.compress(b"-- plaintext under the right name"))
    with pytest.raises(cb.BackupError):
        offsite.real_upload(named)
    assert not [c for c in rclone.calls if "copy" in c]


@pytest.mark.unit
def test_both_temporary_files_are_removed_on_every_path(offsite, db_session):
    from datetime import date

    cb = offsite.module
    cb.run_catalogue_backup(db_session)
    assert offsite.leftovers() == []
    offsite.fail("upload")
    cb.run_catalogue_backup(db_session, today=date(2099, 1, 1))
    assert offsite.leftovers() == []


@pytest.mark.unit
def test_the_sweep_removes_a_stale_encrypted_dump(offsite):
    cb = offsite.module
    for name in ("catalogue-19990101-000000-abc123.sql.gz", "catalogue-19990101-000000-abc123.sql.gz.age"):
        (offsite.dump_dir / name).write_bytes(b"left by a killed run")
    cb._sweep_stale_dumps()
    assert offsite.leftovers() == []


@pytest.mark.unit
def test_the_dump_goes_under_the_tenant_prefix(offsite, rclone):
    """The appliance key may write only under <tenant>/, so the dump does too."""
    dump = offsite.dump_dir / _LISTED_NAME
    dump.write_bytes(b"age-encryption.org/v1\n" + b"x")

    offsite.real_upload(dump)
    offsite.real_listing_sha1(_LISTED_NAME)

    copy = next(c for c in rclone.calls if "copy" in c)
    assert copy[copy.index(str(dump)) + 1] == "b2stub:bucket/captua-test/db-backups/"
    ls = next(c for c in rclone.calls if "lsjson" in c)
    assert "b2stub:bucket/captua-test/db-backups/" in ls


@pytest.mark.unit
@pytest.mark.parametrize("tenant", ["", "  ", "/"])
def test_the_run_is_disabled_without_a_tenant(offsite, db_session, monkeypatch, tenant):
    from app.core.config import settings

    cb = offsite.module
    monkeypatch.setattr(settings, "OFFSITE_TENANT", tenant, raising=False)

    assert cb.run_catalogue_backup(db_session)["status"] == "disabled"
    assert offsite.upload_calls() == []
    scheduler = cb.CatalogueBackupScheduler()
    try:
        assert scheduler.start() is False
    finally:
        scheduler.stop()


@pytest.mark.unit
def test_a_tenant_that_is_not_a_prefix_stops_the_run(offsite, db_session, monkeypatch):
    from app.core.config import settings

    cb = offsite.module
    monkeypatch.setattr(settings, "OFFSITE_TENANT", "../elsewhere", raising=False)

    result = cb.run_catalogue_backup(db_session)
    assert result["status"] == "misconfigured", result
    assert offsite.upload_calls() == []


@pytest.mark.unit
def test_the_listing_asks_for_sha1_hashes_and_reads_the_one_it_gets(offsite, rclone):
    found = offsite.real_listing_sha1(_LISTED_NAME)

    ls = next(c for c in rclone.calls if "lsjson" in c)
    assert "--hash" in ls
    assert ls[ls.index("--hash-type") + 1] == "sha1"
    # Asking for the hash is half of it; the answer has to come back out.
    assert found == _LISTED_SHA1


@pytest.mark.unit
def test_neither_transport_call_asks_rclone_to_remove_anything(offsite, rclone):
    """The key cannot delete. Neither may the code ask it to, on any path."""
    forbidden = {
        "delete", "deletefile", "purge", "rmdir", "rmdirs", "sync", "move",
        "moveto", "--delete-before", "--delete-during", "--delete-after",
        "--delete-excluded",
    }

    dump = offsite.dump_dir / _LISTED_NAME
    dump.write_bytes(b"age-encryption.org/v1\n" + b"x")
    offsite.real_upload(dump)

    assert offsite.real_listing_sha1(dump.name) == _LISTED_SHA1
    assert rclone.calls
    for call in rclone.calls:
        assert not (set(call) & forbidden), call


def test_a_dump_command_whose_binary_is_missing_is_a_plain_failure(tmp_path, monkeypatch):
    """A missing binary is a failed run, not an exception out of the run.

    Sibling of the transport timeouts: run_catalogue_backup promises its caller
    a status, and Popen raises FileNotFoundError before any error path inside
    write_dump can see it. The likeliest way there is a PG_DUMP_COMMAND naming
    a wrapper that is not on this unit.
    """
    from app.core.config import settings
    import app.core.catalogue_backup as cb

    missing = tmp_path / "pg_dump_wrapper"
    monkeypatch.setattr(settings, "PG_DUMP_COMMAND", str(missing), raising=False)

    with pytest.raises(cb.BackupError) as exc:
        cb.write_dump(tmp_path / "out.sql.gz")

    assert str(missing) in str(exc.value)


# ---------------------------------------------------------------------------
# A listing row with no checksum is not an absent object
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_listed_object_with_no_checksum_does_not_read_as_absent(offsite, rclone):
    """A row matching the name but carrying no SHA1 means the listing could not
    answer. Absence and silence are different answers and only one of them is
    evidence about the object."""
    cb = offsite.module
    rclone.rows = [{"Name": _LISTED_NAME, "IsDir": False, "Hashes": {}}]

    with pytest.raises(cb.VerificationUnavailable):
        offsite.real_listing_sha1(_LISTED_NAME)


@pytest.mark.unit
def test_an_empty_checksum_in_the_listing_aborts_rather_than_fails(
    offsite, rclone, monkeypatch
):
    cb = offsite.module
    rclone.rows = [{"Name": _LISTED_NAME, "IsDir": False, "Hashes": {"sha1": ""}}]
    monkeypatch.setattr(cb, "listing_sha1", offsite.real_listing_sha1)

    with pytest.raises(cb.VerificationUnavailable):
        cb.verify_against_listing(_LISTED_NAME, "a" * 40)


@pytest.mark.unit
def test_a_genuinely_absent_object_is_still_a_plain_failure(
    offsite, rclone, monkeypatch
):
    """The other half of the distinction: nothing matching the name is a real
    negative answer, and must stay a BackupError rather than an abort."""
    cb = offsite.module
    rclone.rows = []
    monkeypatch.setattr(cb, "listing_sha1", offsite.real_listing_sha1)

    with pytest.raises(cb.BackupError) as excinfo:
        cb.verify_against_listing(_LISTED_NAME, "a" * 40)

    assert not isinstance(excinfo.value, cb.VerificationUnavailable)


# ---------------------------------------------------------------------------
# Cleanup is idempotent, not one finally
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_stale_dump_is_swept_before_the_up_to_date_check(offsite, db_session):
    """An unlink that failed, or a process that exited mid-run, leaves a dump on
    the card. The next tick returns up-to-date without reaching a run's own
    cleanup, so the sweep has to come first and has to run on every tick."""
    cb = offsite.module
    assert cb.run_catalogue_backup(db_session)["status"] == "verified"

    stale = offsite.dump_dir / "catalogue-19990101-000000-abc123.sql.gz"
    stale.write_bytes(b"left behind by a run that died")
    unrelated = offsite.dump_dir / "keep-me.txt"
    unrelated.write_text("not a dump")

    result = cb.run_catalogue_backup(db_session)

    assert result["status"] == "up-to-date", result
    assert offsite.leftovers() == ["keep-me.txt"]


@pytest.mark.unit
def test_the_scheduler_sweeps_an_abandoned_dump_at_startup(offsite, monkeypatch):
    """Shutdown joins a daemon thread for five seconds and then lets the process
    go, so a run can die before its own cleanup. Boot is the second chance."""
    from app.core.config import settings

    cb = offsite.module
    monkeypatch.setattr(
        settings, "OFFSITE_BACKUP_DELAY_SECONDS", 3600, raising=False
    )
    stale = offsite.dump_dir / "catalogue-19990101-000000-abc123.sql.gz"
    stale.write_bytes(b"abandoned when the process exited")

    scheduler = cb.CatalogueBackupScheduler()
    assert scheduler.start() is True
    try:
        assert offsite.leftovers() == []
    finally:
        scheduler.stop()


# ---------------------------------------------------------------------------
# The object name is collision-proof
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_two_runs_in_the_same_second_do_not_share_an_object_name(
    offsite, db_session, monkeypatch
):
    """A one-second stamp is not a unique name, and the upload is
    --no-check-dest, so two runs inside one second would write two versions of
    one object."""
    from datetime import datetime, timezone

    cb = offsite.module

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 1, 12, 0, 0, tzinfo=tz or timezone.utc)

    monkeypatch.setattr(cb, "datetime", _Frozen)
    # Both attempts fail at verification, so neither leaves a marker that would
    # turn the second one into "up-to-date".
    offsite.fail("absent")

    first = cb.run_catalogue_backup(db_session)
    second = cb.run_catalogue_backup(db_session)

    assert first["object"] != second["object"], (first, second)
    assert {op for op, _ in offsite.mutations()} == {"put"}, offsite.mutations()


# ---------------------------------------------------------------------------
# The override may not quietly ship role passwords
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_override_running_pg_dumpall_without_the_flag_is_refused(monkeypatch):
    """The module's assurance is that no SCRAM verifier leaves the appliance.
    An override that runs pg_dumpall without --no-role-passwords breaks it, and
    the answer is to refuse the run, not to edit an operator's argv."""
    from app.core.config import settings
    import app.core.catalogue_backup as cb

    monkeypatch.setattr(
        settings, "PG_DUMP_COMMAND", "/usr/bin/pg_dumpall -U me", raising=False
    )

    with pytest.raises(cb.BackupError) as excinfo:
        cb.dump_argv()

    assert "--no-role-passwords" in str(excinfo.value)


@pytest.mark.unit
def test_an_override_carrying_the_flag_is_passed_through_untouched(monkeypatch):
    from app.core.config import settings
    import app.core.catalogue_backup as cb

    monkeypatch.setattr(
        settings,
        "PG_DUMP_COMMAND",
        "/usr/bin/pg_dumpall -U me --no-role-passwords",
        raising=False,
    )

    assert cb.dump_argv() == [
        "/usr/bin/pg_dumpall", "-U", "me", "--no-role-passwords",
    ]


@pytest.mark.unit
def test_an_override_that_is_not_pg_dumpall_is_accepted_as_given(monkeypatch):
    """A wrapper or a pg_dump is the operator's own arrangement; the module has
    no business guessing at its flags."""
    from app.core.config import settings
    import app.core.catalogue_backup as cb

    monkeypatch.setattr(
        settings, "PG_DUMP_COMMAND", "/usr/local/bin/dump-catalogue --stdout",
        raising=False,
    )

    assert cb.dump_argv() == ["/usr/local/bin/dump-catalogue", "--stdout"]


@pytest.mark.unit
def test_a_run_with_an_unsafe_override_uploads_nothing(
    offsite, db_session, monkeypatch
):
    from app.core.config import settings

    cb = offsite.module
    # /usr/bin/env exists, so the refusal has to come from the check rather
    # than from a missing binary.
    monkeypatch.setattr(
        settings, "PG_DUMP_COMMAND", "/usr/bin/env pg_dumpall -U me", raising=False
    )

    result = cb.run_catalogue_backup(db_session)

    assert result["status"] == "failed", result
    assert "--no-role-passwords" in result["error"]
    assert offsite.upload_calls() == []
    assert offsite.leftovers() == []


# ---------------------------------------------------------------------------
# The dump timeout actually bounds the dump
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_stalled_dump_is_killed_and_reported(offsite, tmp_path, monkeypatch):
    """A blocking read has no timeout, and process.wait is only reached once the
    read loop ends, so a child that neither writes nor exits would hold the
    scheduler thread for ever. The deadline has to kill the child."""
    from app.core.config import settings

    cb = offsite.module
    stub = _install(tmp_path, "pg-dump-stall", _PG_DUMP_STALL_STUB)
    monkeypatch.setattr(settings, "PG_DUMP_COMMAND", str(stub), raising=False)
    monkeypatch.setattr(cb, "_DUMP_TIMEOUT_SECONDS", 0.5)

    outcome = {}

    def attempt():
        try:
            cb.write_dump(offsite.dump_dir / "catalogue-stalled.sql.gz")
        except BaseException as exc:  # noqa: BLE001 - recorded, then asserted on
            outcome["raised"] = exc
        else:
            outcome["raised"] = None

    worker = threading.Thread(target=attempt, name="stalled-dump", daemon=True)
    worker.start()
    worker.join(10)

    assert not worker.is_alive(), "the stalled dump was never bounded"
    assert isinstance(outcome["raised"], cb.BackupError), outcome


# ---------------------------------------------------------------------------
# verified_at is the verification time, not the transaction's
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_recorded_time_is_when_verification_succeeded(
    offsite, db_session, monkeypatch
):
    """created_at defaults to now(), which in PostgreSQL is the transaction's
    start. The session is already open before the dump begins, so a long run
    would book itself to the hour it started - or to the previous day."""
    from datetime import datetime, timezone

    from app.models.system_log import SystemLog

    cb = offsite.module
    moment = datetime(2026, 10, 1, 12, 34, 56, tzinfo=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is not None else moment.replace(tzinfo=None)

    monkeypatch.setattr(cb, "datetime", _Frozen)

    result = cb.run_catalogue_backup(db_session)
    assert result["status"] == "verified", result

    row = (
        db_session.query(SystemLog)
        .filter(SystemLog.action == cb.VERIFIED_ACTION)
        .one()
    )
    stored = row.created_at
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    assert stored == moment


@pytest.mark.unit
def test_an_audit_call_without_a_timestamp_still_takes_the_database_default(
    db_session,
):
    """The timestamp parameter is optional: a call without one gets the
    column's own default, filled in with the time of the call."""
    from datetime import datetime, timedelta, timezone

    from app.core import audit
    from app.models.system_log import SystemLog

    before = datetime.now(timezone.utc)

    audit.log_event(
        db_session, level="INFO", category="system", action="storage_mount",
        actor="someone", subject="/mnt/offsite", detail="no timestamp given",
    )

    row = (
        db_session.query(SystemLog)
        .filter(SystemLog.action == "storage_mount")
        .one()
    )
    stored = row.created_at
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    # A minute of slack: the point is that the default ran, not its precision.
    assert abs(stored - before) < timedelta(minutes=1), stored


# ---------------------------------------------------------------------------
# A swallowed commit does not read as verified
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_lost_verified_record_is_not_reported_as_verified(
    offsite, db_session, monkeypatch
):
    """audit.log_event swallows its own commit failure, so _record cannot report
    one. With the marker unwritten, needs_run_today stays true and the unit
    would dump and upload again on every interval, each under a new name, on a
    key that cannot delete."""
    cb = offsite.module
    real_commit = db_session.commit

    def losing_commit(*a, **kw):
        if any(
            getattr(obj, "action", None) == cb.VERIFIED_ACTION
            for obj in db_session.new
        ):
            raise RuntimeError("fake: the commit was lost")
        return real_commit(*a, **kw)

    monkeypatch.setattr(db_session, "commit", losing_commit)

    result = cb.run_catalogue_backup(db_session)

    assert result["status"] == "failed", result
    assert result.get("error")
    assert cb.last_verified_backup(db_session) is None
    assert cb.needs_run_today(db_session) is True


# ---------------------------------------------------------------------------
# What the single-process assumption rests on
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_single_process_assumption_names_the_worker_count_variable(offsite):
    """The pinned uvicorn reads its worker count from WEB_CONCURRENCY as well as
    from --workers, so the absence of the flag is not the whole assurance."""
    cb = offsite.module

    assert "WEB_CONCURRENCY" in cb.CatalogueBackupScheduler.start.__doc__


# ---------------------------------------------------------------------------
# Name the configured destination, do not claim a bucket
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_verified_run_names_the_configured_destination(
    offsite, db_session, monkeypatch, caplog
):
    """A local path is an accepted destination, so "verified in the bucket" can
    be false while every byte is still on the card. Report where it went."""
    from app.core.config import settings

    cb = offsite.module
    monkeypatch.setattr(settings, "OFFSITE_REMOTE", "/mnt/offsite-dump", raising=False)

    with caplog.at_level(logging.INFO, logger="app.core.catalogue_backup"):
        result = cb.run_catalogue_backup(db_session)

    assert result["status"] == "verified", result

    state = cb.last_verified_backup(db_session)
    assert "/mnt/offsite-dump/captua-test/db-backups" in state["detail"], state
    assert "bucket" not in state["detail"].lower(), state

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "/mnt/offsite-dump/captua-test/db-backups" in logged, logged
    assert "in the bucket" not in logged, logged


# ---------------------------------------------------------------------------
# A timeout becomes the right error
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_timed_out_upload_is_a_plain_failure(offsite, rclone):
    cb = offsite.module
    rclone.raises = subprocess.TimeoutExpired(cmd=["rclone", "copy"], timeout=1)
    dump = offsite.dump_dir / _LISTED_NAME
    dump.write_bytes(b"age-encryption.org/v1\n" + b"x")

    with pytest.raises(cb.BackupError) as excinfo:
        offsite.real_upload(dump)

    assert not isinstance(excinfo.value, cb.VerificationUnavailable)


@pytest.mark.unit
def test_a_timed_out_listing_is_not_a_negative_answer(offsite, rclone):
    """A listing that did not answer in time has said nothing about the object."""
    cb = offsite.module
    rclone.raises = subprocess.TimeoutExpired(cmd=["rclone", "lsjson"], timeout=1)

    with pytest.raises(cb.VerificationUnavailable):
        offsite.real_listing_sha1(_LISTED_NAME)


@pytest.mark.unit
def test_a_transport_timeout_does_not_escape_the_run(
    offsite, db_session, monkeypatch, rclone
):
    """run_catalogue_backup promises never to raise, and the scheduler would
    otherwise swallow the escape with a log line and no failure row."""
    cb = offsite.module
    monkeypatch.setattr(cb, "upload", offsite.real_upload)
    rclone.raises = subprocess.TimeoutExpired(cmd=["rclone", "copy"], timeout=1)

    result = cb.run_catalogue_backup(db_session)

    assert result["status"] == "failed", result
    assert offsite.leftovers() == []
    assert cb.last_verified_backup(db_session) is None


# ---------------------------------------------------------------------------
# The daily boundary is the appliance's local day
# ---------------------------------------------------------------------------

@pytest.fixture
def local_zone_utc_minus_5(monkeypatch):
    """Run the body in the zone the deployed units sit in."""
    monkeypatch.setenv("TZ", "America/Bogota")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _seed_verified(db_session, cb, *, created_at, subject):
    from app.models.system_log import SystemLog

    db_session.add(
        SystemLog(
            level="INFO", category=cb.BACKUP_CATEGORY, action=cb.VERIFIED_ACTION,
            subject=subject, detail="seeded", created_at=created_at,
        )
    )
    db_session.commit()


@pytest.mark.unit
def test_a_run_late_in_the_local_evening_satisfies_that_local_day(
    offsite, db_session, local_zone_utc_minus_5
):
    """21:00 on 1 October in Bogota is already 2 October by UTC date, so a UTC
    comparison would let a second run happen the same local day."""
    from datetime import date, datetime, timezone

    cb = offsite.module
    _seed_verified(
        db_session, cb,
        created_at=datetime(2026, 10, 2, 2, 0, tzinfo=timezone.utc),
        subject="catalogue-20261002-020000-aaaaaa.sql.gz",
    )

    assert cb.needs_run_today(db_session, date(2026, 10, 1)) is False


@pytest.mark.unit
def test_a_run_just_before_local_midnight_does_not_satisfy_the_next_day(
    offsite, db_session, local_zone_utc_minus_5
):
    """And the other way: 23:00 on 30 September in Bogota is 1 October by UTC
    date, which would have the unit skip 1 October altogether."""
    from datetime import date, datetime, timezone

    cb = offsite.module
    _seed_verified(
        db_session, cb,
        created_at=datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc),
        subject="catalogue-20261001-040000-aaaaaa.sql.gz",
    )

    assert cb.needs_run_today(db_session, date(2026, 10, 1)) is True


@pytest.mark.unit
def test_a_naive_timestamp_is_still_read_as_utc(offsite, local_zone_utc_minus_5):
    from datetime import date, datetime

    cb = offsite.module

    assert cb._as_local_date(datetime(2026, 10, 2, 2, 0)) == date(2026, 10, 1)


# ---------------------------------------------------------------------------
# The deadline bounds the read, not only the direct child
# ---------------------------------------------------------------------------

# A wrapper script, which PG_DUMP_COMMAND may name: it starts a child that
# inherits stdout, then execs a second process that also neither writes nor
# exits. Killing only the direct child leaves the first one holding the write
# end of the pipe. The sleeps are bounded so a failing run cannot leave
# processes behind for ever, and are far longer than the test's deadline.
_PG_DUMP_WRAPPER_STALL_STUB = """#!/bin/sh
sleep 20 &
exec sleep 20
"""


@pytest.mark.unit
def test_a_stalled_wrapper_with_a_child_is_killed_and_reported(
    offsite, tmp_path, monkeypatch
):
    """A wrapper's child inherits the dump's stdout. The read only sees EOF once
    every holder of the write end is gone, so the deadline has to signal the
    whole process group, not just the process the module started."""
    from app.core.config import settings

    cb = offsite.module
    stub = _install(tmp_path, "pg-dump-wrapper-stall", _PG_DUMP_WRAPPER_STALL_STUB)
    monkeypatch.setattr(settings, "PG_DUMP_COMMAND", str(stub), raising=False)
    monkeypatch.setattr(cb, "_DUMP_TIMEOUT_SECONDS", 0.5)

    outcome = {}

    def attempt():
        try:
            cb.write_dump(offsite.dump_dir / "catalogue-wrapper-stalled.sql.gz")
        except BaseException as exc:  # noqa: BLE001 - recorded, then asserted on
            outcome["raised"] = exc
        else:
            outcome["raised"] = None

    worker = threading.Thread(target=attempt, name="stalled-wrapper", daemon=True)
    worker.start()
    worker.join(8)

    assert not worker.is_alive(), "a child holding the pipe kept the dump unbounded"
    assert isinstance(outcome["raised"], cb.BackupError), outcome
    assert "did not finish within" in str(outcome["raised"])


# ---------------------------------------------------------------------------
# The dump is not readable by other local users
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_dump_is_created_owner_only(offsite, db_session, monkeypatch):
    """The dump carries users.hashed_password for every account, which
    --no-role-passwords does not touch. The mode is checked while the file
    exists, from inside upload(), because the run removes it."""
    cb = offsite.module
    seen = {}
    real_upload = cb.upload

    def capturing_upload(local):
        seen["mode"] = stat.S_IMODE(Path(local).stat().st_mode)
        real_upload(local)

    monkeypatch.setattr(cb, "upload", capturing_upload)

    assert cb.run_catalogue_backup(db_session)["status"] == "verified"
    assert seen["mode"] == 0o600, oct(seen["mode"])


@pytest.mark.unit
def test_the_dump_directory_is_created_owner_only(db_session, tmp_path, monkeypatch):
    from app.core import config
    import app.core.catalogue_backup as cb

    monkeypatch.setattr(config.settings, "DATA_DIR", str(tmp_path / "data"))
    old_umask = os.umask(0o022)
    try:
        created = cb._dump_dir()
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(created.stat().st_mode) == 0o700


# ---------------------------------------------------------------------------
# A filesystem error during the dump is a failed status
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_full_card_during_the_dump_is_a_recorded_failure(
    offsite, db_session, monkeypatch
):
    """ENOSPC is the condition this feature exists for. It must reach the
    operator as catalogue_backup_failed, not escape a function that promises a
    status."""
    from app.models.system_log import SystemLog

    cb = offsite.module

    def full_card(self, data):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(gzip.GzipFile, "write", full_card)

    result = cb.run_catalogue_backup(db_session)

    assert result["status"] == "failed", result
    assert "space" in result["error"].lower()
    rows = (
        db_session.query(SystemLog)
        .filter(SystemLog.action == cb.FAILED_ACTION)
        .all()
    )
    assert len(rows) == 1
    assert offsite.upload_calls() == []
    assert offsite.leftovers() == []


# ---------------------------------------------------------------------------
# The stale sweep does not sit behind the enabled check
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_stale_dump_is_swept_when_the_remote_is_no_longer_configured(
    offsite, db_session, monkeypatch
):
    """A unit that died mid-dump and then lost OFFSITE_REMOTE keeps the dump on
    its card unless the sweep runs ahead of the disabled return."""
    from app.core.config import settings

    cb = offsite.module
    monkeypatch.setattr(settings, "OFFSITE_REMOTE", "", raising=False)
    stale = offsite.dump_dir / "catalogue-19990101-000000-abc123.sql.gz"
    stale.write_bytes(b"left behind before the remote was cleared")

    assert cb.run_catalogue_backup(db_session)["status"] == "disabled"
    assert offsite.leftovers() == []


@pytest.mark.unit
def test_the_scheduler_sweeps_at_startup_when_it_has_nothing_to_schedule(
    offsite, monkeypatch
):
    from app.core.config import settings

    cb = offsite.module
    monkeypatch.setattr(settings, "OFFSITE_REMOTE", "", raising=False)
    stale = offsite.dump_dir / "catalogue-19990101-000000-abc123.sql.gz"
    stale.write_bytes(b"abandoned before the remote was cleared")

    scheduler = cb.CatalogueBackupScheduler()
    try:
        assert scheduler.start() is False
        assert offsite.leftovers() == []
    finally:
        scheduler.stop()
