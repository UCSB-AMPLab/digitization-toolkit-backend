"""Offsite backup of the catalogue database (NEH-249).

Offsite delivery pushes captured images. The database is what makes them mean
anything — it ties each file to its project, volume, order and folio — and on
2026-09-15 a unit's card filled to 48 KB free with 87.7 GiB of images safely in
the bucket and no catalogue dump there at all. Six weeks of catalogue existed
only on a card that was failing writes.

Shape of a run:

1. dump the catalogue through gzip to a temporary file under the data dir;
2. upload it on the images' credential and bandwidth cap, through rclone;
3. verify the local digest against the SHA1 the destination's listing reports;
4. record the verified run in ``system_logs``, which is both the operator's
   view and the date marker that decides whether today still needs a run;
5. remove the temporary file — always, on every path.

``OFFSITE_REMOTE`` is whatever rclone destination the unit is configured with,
and that is deliberately not validated as remote-shaped: a venue with no usable
uplink may be given a local path or a mounted disk. So nothing here asserts
where the dump ended up. A verified run reports the configured destination by
name and says the digest matched there; it does not claim the data left the
appliance, because on some units it will not have.

Three rules it is built around:

* **A listing that fails aborts the run.** The scoped B2 key is write + list
  with no read, so a single-object lookup answers 401 and an empty result from
  a refused call is indistinguishable from an absent object. A matching row
  that carries no checksum is the same kind of silence. A check that cannot
  answer is not a negative answer. This is the rule ``free-masters.py`` is
  built around, for the same key.
* **A dump never survives the run.** The failure being guarded against is
  precisely a card with no room, so cleanup is the point rather than tidiness.
  The dump is a copy of live data, so removing an unverified one loses nothing;
  what an unverified run must never do is advance the marker. Cleanup is also
  idempotent: every run and every boot sweeps the dump directory first, because
  a process that exits mid-run never reaches its own cleanup.
* **Retry rather than skip.** A failed run leaves no verified marker for today,
  so the next tick tries again instead of waiting for tomorrow. "Today" is the
  appliance's local day, which is the day an operator means.

Nothing here deletes or overwrites anything at the destination, and the B2 key
is scoped so that it could not.
"""

import gzip
import hashlib
import json
import logging
import os
import secrets
import shlex
import signal
import subprocess
import tempfile
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

# Imported as a module, not as `settings`: tests (and the
# override_projects_root fixture) rebuild the Settings object with
# importlib.reload, so a module-level binding to the old instance would go
# stale. Every read below goes through config.settings.
from app.core import config

logger = logging.getLogger(__name__)

# system_logs.category carries a CHECK constraint
# (access | activity | capture | system), so backup events are "system".
BACKUP_CATEGORY = "system"
VERIFIED_ACTION = "catalogue_backup_verified"
FAILED_ACTION = "catalogue_backup_failed"

# The backend runs natively under pixi from <project root>/backend (scripts/
# start.sh), while the database runs in Docker. There is no postgres client in
# pixi.toml, so the dump goes through the container exactly as the pre-migration
# backup in scripts/update.sh does. The ".." is the same assumption config.py
# already makes for env_file="../.env".
_COMPOSE_FILES = ("../docker-compose.yml", "../docker-compose.pi.yml")

# A failed pg_dumpall can still produce a valid, nearly empty gzip stream — the
# failure scripts/update.sh refuses to migrate past. Same floor here.
_MIN_DUMP_BYTES = 100

_DUMP_TIMEOUT_SECONDS = 15 * 60
_UPLOAD_TIMEOUT_SECONDS = 60 * 60
_LISTING_TIMEOUT_SECONDS = 120

# One transfer at a time across the offsite path. OFFSITE_BWLIMIT is a cap for
# the unit's link, and rclone applies --bwlimit per process: the catalogue dump
# and an image upload running together would use twice the cap. Both take this
# lock around their rclone transfer (app.core.image_upload does the same).
transfer_lock = threading.Lock()


class BackupError(Exception):
    """A run that failed and must be retried on the next tick."""


class VerificationUnavailable(BackupError):
    """The bucket listing could not answer, so nothing may be called verified."""


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _invokes_pg_dumpall(argv: list[str]) -> bool:
    """Whether this argv runs pg_dumpall, directly or through a container."""
    return any(Path(word).name == "pg_dumpall" for word in argv)


def dump_argv() -> list[str]:
    """argv that writes the whole catalogue to stdout as SQL.

    On the default path below, --no-role-passwords is part of the argv this
    builds, so the module's own assurance holds: no role password leaves the
    appliance. An operator override (PG_DUMP_COMMAND) is run exactly as
    written — a wrapper script or a pg_dump is their arrangement, and on that
    path the assurance is theirs, since nothing here can know what a wrapper
    does. The single exception is an override that itself invokes pg_dumpall
    without --no-role-passwords: that would carry the per-unit database
    password's SCRAM verifier offsite on a timer, so the run is refused by
    name. Flags are never appended to an operator's argv.
    """
    override = config.settings.PG_DUMP_COMMAND.strip()
    if override:
        argv = shlex.split(override)
        if _invokes_pg_dumpall(argv) and "--no-role-passwords" not in argv:
            raise BackupError(
                "PG_DUMP_COMMAND runs pg_dumpall without --no-role-passwords, "
                "which would carry the database password's SCRAM verifier off "
                "the appliance; add --no-role-passwords to the command"
            )
        return argv
    argv = ["docker", "compose"]
    for compose_file in _COMPOSE_FILES:
        argv += ["-f", compose_file]
    # pg_dumpall rather than pg_dump: it carries the role definitions the
    # catalogue's objects are owned by, so a restore onto a bare appliance does
    # not depend on reproducing a per-unit generated database user by hand.
    #
    # --no-role-passwords keeps those CREATE ROLE statements and drops only the
    # secrets. Without it every scheduled dump would carry the per-unit database
    # password's SCRAM verifier into the bucket — a credential leaving the
    # appliance on a timer. The verifier is no loss: a freshly provisioned unit
    # generates its own password, so a restore target cannot use the old one.
    # This holds whatever is later decided about bucket visibility, rather than
    # depending on a setting one command reverses.
    argv += [
        "exec", "-T", "db",
        "pg_dumpall", "-U", config.settings.DATABASE_USER,
        "--no-role-passwords",
    ]
    return argv


def _rclone_argv(*args: str) -> list[str]:
    argv = [config.settings.RCLONE_BIN, *args]
    config_path = config.settings.RCLONE_CONFIG.strip()
    if config_path:
        argv += ["--config", config_path]
    return argv


def remote_dir() -> str:
    prefix = config.settings.OFFSITE_DB_PREFIX.strip("/")
    remote = config.settings.OFFSITE_REMOTE.rstrip("/")
    return f"{remote}/{prefix}" if prefix else remote


def _dump_dir() -> Path:
    """Where the temporary dump is written.

    Under the data dir rather than the system temp dir: that is the tree the
    appliance's own pre-update dumps already use (/var/lib/dtk/backups in
    scripts/update.sh), it is known to be writable by the service user, and it
    does not depend on what /tmp is backed by on a given image.
    """
    path = config.settings.data_dir / "tmp"
    # 0700: the dump holds every account's password hash, so no other local
    # user has any business listing this directory.
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _object_name() -> str:
    """The name this run's dump is stored under.

    The timestamp is for whoever reads a listing; uniqueness comes from the
    random token. A one-second stamp is not a unique name — two ticks inside
    one second would agree on it — and the upload runs --no-check-dest, so the
    second of them would write another version of the same object rather than
    fail. With the token, a retry adds an object and can never land on one that
    is already there.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"catalogue-{stamp}-{secrets.token_hex(3)}.sql.gz"


def _sweep_stale_dumps() -> int:
    """Remove every catalogue dump found in the dump directory. Returns a count.

    A run removes its own dump in a `finally`, but that is one attempt: an
    unlink that fails leaves the file behind, and the next tick can return
    up-to-date without reaching the cleanup at all. A process killed mid-run
    never reaches it either. So the sweep runs before every attempt and at every
    boot, and is safe to repeat. It is called before this run's dump exists, so
    it never removes a file that is in use — which holds because there is one
    backend process and one schedule in it (see CatalogueBackupScheduler.start,
    where the same single-process assumption is set out and its limits named).
    """
    try:
        stale = sorted(_dump_dir().glob("catalogue-*.sql.gz"))
    except OSError:
        logger.exception("Could not scan the dump directory for stale dumps")
        return 0
    removed = 0
    for path in stale:
        try:
            path.unlink()
        except OSError:
            logger.exception("Could not remove the stale catalogue dump %s", path)
        else:
            removed += 1
            logger.warning("Removed a stale catalogue dump: %s", path)
    return removed


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def _kill_dump(process: subprocess.Popen) -> None:
    """SIGKILL the dump's process group, or the process alone if it shares ours.

    The dump is started in its own session, so its group holds the argv's first
    process and anything that process started. Signalling only the first leaves
    a wrapper's children alive, and they hold the write end of the stdout pipe
    open: the read never returns EOF.

    The group is compared with this process's own before anything is signalled.
    A dump started without start_new_session=True shares the backend's group,
    and killing that group kills the backend: uvicorn, the API, the schedule.
    Removing start_new_session=True from the Popen above and running the
    stalled-wrapper test killed the test runner, which is how this guard came
    to be written. The two settings are one mechanism, and
    a future edit that separates them must not be able to take the backend
    down, so the fallback kills only the child and lets the caller's existing
    timeout path report the stall.

    Raises OSError if the group is already gone; callers guard it.
    """
    pgid = os.getpgid(process.pid)
    if pgid == os.getpgid(0):
        logger.error(
            "The catalogue dump shares this process's group; killing the dump "
            "process alone. A wrapper's children may hold the pipe open."
        )
        process.kill()
        return
    os.killpg(pgid, signal.SIGKILL)


def write_dump(dest: Path) -> tuple[int, str]:
    """Stream the catalogue dump through gzip into `dest`.

    Returns the uncompressed byte count and the gzip file's SHA1. Streamed
    rather than buffered: a catalogue grows with the collection, and the
    appliance has little memory to spare.
    """
    argv = dump_argv()
    digest = hashlib.sha1()
    raw_bytes = 0

    with tempfile.TemporaryFile() as errfile:
        try:
            process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=errfile,
                stdin=subprocess.DEVNULL,
                # Its own process group, so the deadline can signal a wrapper
                # and the children it started together (see _kill_dump).
                start_new_session=True,
            )
        except OSError as exc:
            # A binary that is missing or not executable fails here, before any
            # path below can report it, and the caller is promised a status
            # rather than an exception. The likeliest route is a
            # PG_DUMP_COMMAND naming a wrapper that is not on this unit.
            raise BackupError(
                f"the catalogue dump could not be started ({argv[0]}): {exc}"
            ) from exc
        # The read below blocks with no timeout of its own, and the wait after
        # the loop is only reached once the loop ends, so a child that neither
        # writes nor exits would hold the scheduler thread for the life of the
        # process. The deadline kills the child's whole process group instead:
        # a wrapper's children inherit stdout and would keep the pipe open
        # after the wrapper itself died. With every holder gone the read
        # returns EOF, the loop ends, and the error path below runs.
        deadline_passed = threading.Event()

        def _kill_on_deadline() -> None:
            deadline_passed.set()
            try:
                _kill_dump(process)
            except OSError:
                logger.exception("Could not kill the stalled catalogue dump")

        watchdog = threading.Timer(_DUMP_TIMEOUT_SECONDS, _kill_on_deadline)
        watchdog.daemon = True
        watchdog.start()
        try:
            try:
                # Created 0600 at open, not chmod'd afterwards: there is no
                # moment at which another local user could read it. O_EXCL
                # because the name is unique per run, so an existing file here
                # is not one to write into.
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as raw, \
                        gzip.GzipFile(fileobj=raw, mode="wb") as out:
                    while True:
                        chunk = process.stdout.read(1 << 20)
                        if not chunk:
                            break
                        raw_bytes += len(chunk)
                        out.write(chunk)
            finally:
                watchdog.cancel()
                if process.stdout is not None:
                    process.stdout.close()
                try:
                    returncode = process.wait(timeout=_DUMP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    try:
                        _kill_dump(process)
                    except OSError:
                        logger.exception("Could not kill the stalled catalogue dump")
                    process.wait()
                    raise BackupError(
                        f"the catalogue dump did not finish within "
                        f"{_DUMP_TIMEOUT_SECONDS}s"
                    ) from None
        except OSError as exc:
            # A full card fails creating, writing or closing the file with
            # OSError (ENOSPC), and ENOSPC is the condition this module exists
            # for. Converted here so run_catalogue_backup records it as a
            # failed run, as it promises to.
            raise BackupError(
                f"the catalogue dump could not be written to {dest}: {exc}"
            ) from exc

        if returncode != 0:
            if deadline_passed.is_set():
                # Checked inside this branch, not before it: a child that
                # finished in the instant before the timer fired exits 0 and the
                # kill is a no-op, and that run is not a timeout.
                raise BackupError(
                    f"the catalogue dump did not finish within "
                    f"{_DUMP_TIMEOUT_SECONDS}s and was killed"
                )
            errfile.seek(0)
            stderr = errfile.read().decode("utf-8", "replace").strip()
            raise BackupError(
                f"the catalogue dump failed (rc={returncode}): {stderr[:300]}"
            )

    if raw_bytes < _MIN_DUMP_BYTES:
        raise BackupError(
            f"the catalogue dump looks empty ({raw_bytes} bytes before "
            "compression); refusing to treat it as a backup"
        )

    try:
        with dest.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BackupError(
            f"the catalogue dump could not be read back from {dest}: {exc}"
        ) from exc
    return raw_bytes, digest.hexdigest()


def upload(local: Path) -> None:
    """Copy one file to the destination. Copy only: nothing here can remove.

    --no-check-dest because rclone's default destination HEAD needs readFiles
    and answers 401 against this write+list key. It cost the first attempt.
    """
    argv = _rclone_argv(
        "copy",
        str(local),
        remote_dir() + "/",
        "--no-check-dest",
        "--bwlimit",
        config.settings.OFFSITE_BWLIMIT,
    )
    try:
        with transfer_lock:
            result = subprocess.run(
                argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                timeout=_UPLOAD_TIMEOUT_SECONDS,
            )
    except subprocess.TimeoutExpired as exc:
        # An upload that ran out of time is a failed upload, and the caller
        # promises not to raise anything but BackupError.
        raise BackupError(
            f"the upload did not finish within {_UPLOAD_TIMEOUT_SECONDS}s"
        ) from exc
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise BackupError(f"the upload failed (rc={result.returncode}): {stderr[:300]}")


# rclone's documented exit code for "Directory not found".
RCLONE_EXIT_DIRECTORY_NOT_FOUND = 3


def lsjson_rows(path: str, *, recursive: bool = False,
                absent_ok: bool = False) -> Optional[list]:
    """The rows `rclone lsjson --hash --hash-type sha1` reports for `path`.

    Raises VerificationUnavailable when the listing did not answer: a refused
    call, a timeout, or output that is not JSON. With `absent_ok`, rclone's
    "directory not found" exit returns None instead of raising; the caller
    decides what that is evidence of, because on its own it is not evidence
    that nothing is there (app.core.image_upload confirms it against the
    parent's listing). Every other non-zero exit raises.

    This is the one listing parser for the offsite path: the catalogue backup
    and the image uploader both read checksums through it.
    """
    args = ["lsjson", "--hash", "--hash-type", "sha1"]
    if recursive:
        args += ["-R", "--files-only"]
    argv = _rclone_argv(*args, path)
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
            timeout=_LISTING_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise VerificationUnavailable(
            f"could not list {path.rstrip('/')} within "
            f"{_LISTING_TIMEOUT_SECONDS}s"
        ) from exc
    if absent_ok and result.returncode == RCLONE_EXIT_DIRECTORY_NOT_FOUND:
        return None
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise VerificationUnavailable(
            f"could not list {path.rstrip('/')} (rc={result.returncode}): {stderr[:300]}"
        )
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise VerificationUnavailable(
            f"unreadable listing for {path.rstrip('/')}: {exc}"
        ) from exc
    if not isinstance(rows, list):
        raise VerificationUnavailable(
            f"unreadable listing for {path.rstrip('/')}: not a list"
        )
    return rows


def row_sha1(row: dict) -> Optional[str]:
    """The lowercase SHA1 a listing row carries, or None when it carries none."""
    sha1 = (row.get("Hashes") or {}).get("sha1")
    return sha1.lower() if sha1 else None


def listing_sha1(name: str) -> Optional[str]:
    """The SHA1 the destination's listing reports for `name`, or None if absent.

    None means one thing only: nothing in the listing carries that name.

    Raises VerificationUnavailable when the listing did not answer — a refused
    call, an unreadable or timed-out one, or a row that matches the name but
    carries no checksum. All of those are silence, and silence about an object
    is not a report that the object is missing.
    """
    rows = lsjson_rows(remote_dir() + "/")
    for row in rows:
        if row.get("IsDir"):
            continue
        if row.get("Name") == name:
            sha1 = row_sha1(row)
            if not sha1:
                # The object is there; it is the checksum that is missing. The
                # same return value for this and for an absent name would turn
                # a present object into "not in the listing".
                raise VerificationUnavailable(
                    f"the listing of {remote_dir()} reports no sha1 for {name}"
                )
            return sha1
    return None


def verify_against_listing(name: str, local_sha1: str) -> None:
    """Confirm the destination holds `name` with the digest we uploaded.

    Raises VerificationUnavailable if the listing cannot answer and BackupError
    if it answers and disagrees. The two are different: only the second is
    evidence about the object.
    """
    remote = listing_sha1(name)
    if remote is None:
        raise BackupError(f"{name} is not in the listing of {remote_dir()}")
    if remote != local_sha1:
        raise BackupError(
            f"sha1 differs for {name}: local {local_sha1}, remote {remote}"
        )


# ---------------------------------------------------------------------------
# State: the operator's view and the date marker, in one place
# ---------------------------------------------------------------------------

def last_verified_backup(db: Session) -> Optional[dict]:
    """The most recent verified run, or None if there has never been one."""
    from app.models.system_log import SystemLog

    row = (
        db.query(SystemLog)
        .filter(
            SystemLog.category == BACKUP_CATEGORY,
            SystemLog.action == VERIFIED_ACTION,
        )
        .order_by(SystemLog.created_at.desc(), SystemLog.id.desc())
        .first()
    )
    if row is None:
        return None
    return {
        "verified_at": row.created_at,
        "object": row.subject,
        "detail": row.detail,
    }


def _as_local_date(value: datetime) -> date:
    """The appliance's local calendar day for `value`.

    A naive value is still assumed to be UTC, which is what both PostgreSQL's
    now() and this module's own timestamps give.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().date()


def needs_run_today(db: Session, today: Optional[date] = None) -> bool:
    """True unless a verified dump has already been recorded for today.

    This is the date marker. Deriving it from the verified record rather than
    from a separate file means a run that failed anywhere — dump, upload, or a
    listing that would not answer — leaves nothing behind and is retried on the
    next tick, which is the interim script's behaviour.

    The day is the appliance's local day. The deployed units run at UTC−5, so a
    UTC comparison would let two runs happen in one local day — a run after
    19:00 local is already tomorrow by UTC date — and skip another local day
    entirely.
    """
    today = today or datetime.now(timezone.utc).astimezone().date()
    state = last_verified_backup(db)
    if state is None:
        return True
    return _as_local_date(state["verified_at"]) != today


# ---------------------------------------------------------------------------
# A run
# ---------------------------------------------------------------------------

def _record(db: Session, *, action: str, level: str, subject: Optional[str],
            detail: Optional[str], created_at: Optional[datetime] = None) -> None:
    from app.core import audit

    audit.log_event(
        db, level=level, category=BACKUP_CATEGORY, action=action,
        actor=None, subject=subject, detail=detail, created_at=created_at,
    )


def run_catalogue_backup(db: Session, today: Optional[date] = None) -> dict:
    """Dump, upload, verify, record. Never raises; the caller gets a status.

    Statuses: "disabled" (no remote configured), "up-to-date" (already verified
    today), "verified", "failed" (the bucket answered and the answer was bad, or
    the dump or upload failed), "aborted" (verification could not answer).
    """
    # Before the disabled and up-to-date checks, not after them: both return
    # early, and a dump left behind by a failed unlink or a killed process has
    # to be cleared by some tick that does not run. A unit that died mid-dump
    # and then lost OFFSITE_REMOTE would otherwise keep the dump on its card.
    _sweep_stale_dumps()

    if not config.settings.OFFSITE_REMOTE.strip():
        return {"status": "disabled"}

    if not needs_run_today(db, today):
        state = last_verified_backup(db)
        return {
            "status": "up-to-date",
            "object": state["object"],
            "verified_at": state["verified_at"],
        }

    # A fresh name per run, unique by its random token: a retry adds an object
    # rather than overwriting one, so nothing in this path can destroy a dump
    # that is already at the destination.
    name = _object_name()
    dump = _dump_dir() / name

    try:
        try:
            raw_bytes, sha1 = write_dump(dump)
            upload(dump)
            verify_against_listing(name, sha1)
            # Captured here, at the moment verification succeeded, rather than
            # left to the column's server default: now() in PostgreSQL is the
            # transaction's start, and this session opened and queried before
            # the dump began. A long run would otherwise book itself to the
            # hour it started, and a run spanning midnight to the day before.
            verified_at = datetime.now(timezone.utc)
        except VerificationUnavailable as exc:
            # Abort, not a negative answer. No marker is advanced, so the next
            # tick retries. Declared a failure here, before any cleanup runs.
            logger.warning("Catalogue backup aborted: %s", exc)
            _record(db, action=FAILED_ACTION, level="WARN", subject=name,
                    detail=f"aborted: {exc}"[:500])
            return {"status": "aborted", "object": name, "error": str(exc)}
        except BackupError as exc:
            logger.warning("Catalogue backup failed: %s", exc)
            _record(db, action=FAILED_ACTION, level="ERR", subject=name,
                    detail=str(exc)[:500])
            return {"status": "failed", "object": name, "error": str(exc)}

        # The destination by name, not a claim about its nature: OFFSITE_REMOTE
        # may be a bucket, a mounted disk or a local path.
        detail = (
            f"verified at {remote_dir()}: {dump.stat().st_size} bytes gzipped, "
            f"{raw_bytes} raw, sha1 {sha1}"
        )
        _record(db, action=VERIFIED_ACTION, level="INFO", subject=name,
                detail=detail, created_at=verified_at)

        # audit.log_event swallows a failed commit by design, so _record cannot
        # report one and this is the only way to know the marker landed. It
        # matters because needs_run_today reads that same marker: unread, a lost
        # commit would have the unit dump and upload again on every interval,
        # each time under a new name, on a key that cannot delete.
        state = last_verified_backup(db)
        if state is None or state["object"] != name:
            error = (
                f"the verified run was not recorded: system_logs does not "
                f"report {name} as the most recent verified backup"
            )
            logger.error("Catalogue backup could not be recorded: %s", error)
            _record(db, action=FAILED_ACTION, level="ERR", subject=name,
                    detail=error[:500])
            return {"status": "failed", "object": name, "error": error}

        logger.info("Catalogue backup verified at %s: %s", remote_dir(), name)
        return {
            "status": "verified",
            "object": name,
            "sha1": sha1,
            "bytes": dump.stat().st_size,
            "raw_bytes": raw_bytes,
            "verified_at": state["verified_at"],
        }
    finally:
        # Unconditional: a filling card must never accumulate dumps, and the
        # live database is the authoritative copy, so an unverified dump is
        # nothing to keep. Reached only after the run's outcome is decided. If
        # this unlink fails, or the process dies before reaching it, the sweep
        # at the head of the next run and at the next boot clears what is left.
        try:
            dump.unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not remove the temporary dump %s", dump)


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

class CatalogueBackupScheduler:
    """In-process schedule for the catalogue dump.

    In-process rather than a systemd timer because the run's state has to reach
    the API: the backend owns the session that writes system_logs, so a timer
    would need a second credential path to the database to record anything an
    operator could see. systemd's Restart=on-failure plus the retry marker
    covers the crash case a timer would otherwise win on.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        """Start the loop. Returns False when there is nothing to schedule.

        Only one of these may run per appliance, and nothing enforces that: the
        guarantee is that there is one backend process. pixi.toml's `start` task
        is `uvicorn app.main:app` with no --workers, launched once by
        scripts/start.sh under a Type=simple systemd unit. Adding --workers, or
        starting a second uvicorn, would run this schedule once per process:
        concurrent dumps of the same database, competing for the bandwidth cap,
        each writing its own verified marker. The absent flag is not the whole
        assurance, either: the pinned uvicorn also reads its worker count from
        the WEB_CONCURRENCY environment variable, so a unit that sets it in the
        service environment gets multiple workers with no --workers anywhere.
        There is no process lock to catch that — export_jobs.py and
        login_throttle.py rest on the same assumption for their module-level
        singletons. If the appliance ever needs more than one worker, this has
        to move to a lock or to a timer first.

        Startup also sweeps the dump directory. stop() joins a daemon thread for
        five seconds and then lets the process exit, so a run can die between
        writing its dump and removing it; boot is where that one gets cleared.
        """
        # Ahead of the configured check: an abandoned dump is cleared even on
        # a unit whose remote has since been removed.
        _sweep_stale_dumps()
        if not config.settings.OFFSITE_REMOTE.strip():
            logger.info(
                "Offsite catalogue backup is not configured; no schedule started"
            )
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="catalogue-backup", daemon=True
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        if self._stop.wait(config.settings.OFFSITE_BACKUP_DELAY_SECONDS):
            return
        while True:
            self.tick()
            if self._stop.wait(config.settings.OFFSITE_BACKUP_INTERVAL_SECONDS):
                return

    def tick(self) -> Optional[dict]:
        """One scheduled attempt, on its own session."""
        from app.core.db import SessionLocal

        db = SessionLocal()
        try:
            return run_catalogue_backup(db)
        except Exception:
            # The loop outlives any single failure; the next tick retries.
            logger.exception("Scheduled catalogue backup raised")
            return None
        finally:
            db.close()


# Module-level singleton, as for the export job registry: one backend process.
catalogue_backup_scheduler = CatalogueBackupScheduler()
