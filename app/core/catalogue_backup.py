"""Offsite backup of the catalogue database (NEH-249).

Offsite delivery pushes captured images. The database is what makes them mean
anything — it ties each file to its project, volume, order and folio — and on
2026-09-15 a unit's card filled to 48 KB free with 87.7 GiB of images safely in
the bucket and no catalogue dump there at all. Six weeks of catalogue existed
only on a card that was failing writes.

Shape of a run:

1. dump the catalogue through gzip to a temporary file under the data dir;
2. upload it on the images' credential, under the shared bandwidth cap;
3. verify the local digest against the SHA1 the bucket listing reports;
4. record the verified run in ``system_logs``, which is both the operator's
   view and the date marker that decides whether today still needs a run;
5. remove the temporary file — always, on every path.

Three rules it is built around:

* **A listing that fails aborts the run.** The scoped key is write + list with
  no read, so a single-object lookup answers 401 and an empty result from a
  refused call is indistinguishable from an absent object. A check that cannot
  answer is not a negative answer. This is the rule ``free-masters.py`` is
  built around, for the same key.
* **A dump never survives the run.** The failure being guarded against is
  precisely a card with no room, so cleanup is the point rather than tidiness.
  The dump is a copy of live data, so removing an unverified one loses nothing;
  what an unverified run must never do is advance the marker.
* **Retry rather than skip.** A failed run leaves no verified marker for today,
  so the next tick tries again instead of waiting for tomorrow.

Nothing here deletes or overwrites anything on the remote, and the key is
scoped so that it could not.

**The transport is not built yet.** ``upload`` and ``listing_sha1`` are the
only two operations this module needs from it, and both raise until they are
implemented against b2sdk, which the 2026-09-14 decision settled on and
which is not yet a pinned dependency. Everything else here — the dump, the
digest, the marker, the ordering rules above — is transport-agnostic and
tested. Do not schedule this module until those two functions exist.
"""

import gzip
import hashlib
import logging
import shlex
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


class BackupError(Exception):
    """A run that failed and must be retried on the next tick."""


class VerificationUnavailable(BackupError):
    """The bucket listing could not answer, so nothing may be called verified."""


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def dump_argv() -> list[str]:
    """argv that writes the whole catalogue to stdout as SQL."""
    override = config.settings.PG_DUMP_COMMAND.strip()
    if override:
        return shlex.split(override)
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


def remote_dir() -> str:
    """Bucket and key prefix the dumps land under."""
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
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

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
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=errfile,
            stdin=subprocess.DEVNULL,
        )
        try:
            with gzip.open(dest, "wb") as out:
                while True:
                    chunk = process.stdout.read(1 << 20)
                    if not chunk:
                        break
                    raw_bytes += len(chunk)
                    out.write(chunk)
        finally:
            if process.stdout is not None:
                process.stdout.close()
            try:
                returncode = process.wait(timeout=_DUMP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise BackupError(
                    f"the catalogue dump did not finish within "
                    f"{_DUMP_TIMEOUT_SECONDS}s"
                ) from None

        if returncode != 0:
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

    with dest.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return raw_bytes, digest.hexdigest()


def upload(local: Path) -> None:
    """Copy one file to the bucket. Copy only: nothing here may remove anything.

    Not implemented. The b2sdk upload goes here, needing only `writeFiles`, and
    carrying the OFFSITE_BWLIMIT cap itself because b2sdk has no bandwidth
    limiting of its own. It is blocking and threaded, so it must not run on the
    event loop.
    """
    raise BackupError(
        "the offsite transport is not built: upload() has no implementation "
        "(NEH-249, pending the b2sdk dependency)"
    )


def listing_sha1(name: str) -> Optional[str]:
    """The SHA1 the bucket listing reports for `name`, or None if absent.

    Not implemented. It must raise VerificationUnavailable whenever the listing
    does not answer, because an empty result from a refused call looks exactly
    like an absent object. b2sdk's ``get_content_sha1()`` reads the stored SHA1
    and falls back to the ``large_file_sha1`` fileInfo key when B2 stores
    ``none``, which is the awkward half of this check.
    """
    raise VerificationUnavailable(
        "the offsite transport is not built: listing_sha1() has no "
        "implementation (NEH-249, pending the b2sdk dependency)"
    )


def verify_against_listing(name: str, local_sha1: str) -> None:
    """Confirm the bucket holds `name` with the digest we uploaded.

    Raises VerificationUnavailable if the listing cannot answer and BackupError
    if it answers and disagrees. The two are different: only the second is
    evidence about the object.
    """
    remote = listing_sha1(name)
    if remote is None:
        raise BackupError(f"{name} is not in the bucket listing")
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


def _as_utc_date(value: datetime) -> date:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).date()
    return value.astimezone(timezone.utc).date()


def needs_run_today(db: Session, today: Optional[date] = None) -> bool:
    """True unless a verified dump has already been recorded for today.

    This is the date marker. Deriving it from the verified record rather than
    from a separate file means a run that failed anywhere — dump, upload, or a
    listing that would not answer — leaves nothing behind and is retried on the
    next tick, which is the interim script's behaviour.
    """
    today = today or datetime.now(timezone.utc).date()
    state = last_verified_backup(db)
    if state is None:
        return True
    return _as_utc_date(state["verified_at"]) != today


# ---------------------------------------------------------------------------
# A run
# ---------------------------------------------------------------------------

def _record(db: Session, *, action: str, level: str, subject: Optional[str],
            detail: Optional[str]) -> None:
    from app.core import audit

    audit.log_event(
        db, level=level, category=BACKUP_CATEGORY, action=action,
        actor=None, subject=subject, detail=detail,
    )


def run_catalogue_backup(db: Session, today: Optional[date] = None) -> dict:
    """Dump, upload, verify, record. Never raises; the caller gets a status.

    Statuses: "disabled" (no remote configured), "up-to-date" (already verified
    today), "verified", "failed" (the bucket answered and the answer was bad, or
    the dump or upload failed), "aborted" (verification could not answer).
    """
    if not config.settings.OFFSITE_REMOTE.strip():
        return {"status": "disabled"}

    if not needs_run_today(db, today):
        state = last_verified_backup(db)
        return {
            "status": "up-to-date",
            "object": state["object"],
            "verified_at": state["verified_at"],
        }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"catalogue-{stamp}.sql.gz"
    # A fresh name per run: a retry adds an object rather than overwriting one,
    # so nothing in this path can destroy a dump that is already offsite.
    dump = _dump_dir() / name

    try:
        try:
            raw_bytes, sha1 = write_dump(dump)
            upload(dump)
            verify_against_listing(name, sha1)
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

        detail = f"{dump.stat().st_size} bytes gzipped, {raw_bytes} raw, sha1 {sha1}"
        _record(db, action=VERIFIED_ACTION, level="INFO", subject=name, detail=detail)
        logger.info("Catalogue backup verified in the bucket: %s", name)
        state = last_verified_backup(db)
        return {
            "status": "verified",
            "object": name,
            "sha1": sha1,
            "bytes": dump.stat().st_size,
            "raw_bytes": raw_bytes,
            "verified_at": state["verified_at"] if state else None,
        }
    finally:
        # Unconditional: a filling card must never accumulate dumps, and the
        # live database is the authoritative copy, so an unverified dump is
        # nothing to keep. Reached only after the run's outcome is decided.
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
        each writing its own verified marker. There is no process lock to catch
        that — export_jobs.py and login_throttle.py rest on the same assumption
        for their module-level singletons. If the appliance ever needs more than
        one worker, this has to move to a lock or to a timer first.
        """
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
