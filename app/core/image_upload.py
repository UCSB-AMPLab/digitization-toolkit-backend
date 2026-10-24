"""Offsite upload of a volume's images (NEH-227, contract 1.1 §4).

Every image of a volume with a file name stem goes to the bucket, current and
superseded alike, at

    <tenant>/<stem>/captures/<file name>             the image as shot
    <tenant>/<stem>/previews/<stem>_preview.jpg      a RAW master's preview

relative to OFFSITE_REMOTE. Thumbnails are never uploaded. The file name is
the image's name on disk when this unit first uploads the object. An adopted
object keeps the key it was found at, whose last segment may differ from the
name on disk (a contract name given in the bucket to an image captured before
contract naming), so nothing here takes the key's name from the disk once a
row exists, or the disk name from the key. Each object's key
is stored in offsite_objects when it is first uploaded or adopted and never
changes, so a later move on disk, a renumber or a project rename leaves the
bucket as it is.

The rules a run follows:

* **Never overwrite.** Before each upload the key's directory is listed. An
  object already at the key with the local SHA1 counts as received and is
  recorded without an upload. One with another SHA1 stops that object: the
  row is marked refused with both digests, both files are reported, and
  nothing is retried over it.
* **A listing that cannot answer stops the run** (VerificationUnavailable,
  from app.core.catalogue_backup). An unanswered listing is never read as an
  absent object. rclone's "directory not found" is accepted as absence only
  when the parent directory's listing answers and does not show it, or for
  the tenant prefix itself. Nothing above `<remote>/<tenant>/` is listed.
* **Verified only from a listing.** An object is verified when a listing
  taken after the upload shows the key with the local SHA1. A copy command
  that succeeded is recorded as uploaded, and stays pending until then.
* **One object at a time**, under catalogue_backup.transfer_lock, so the
  catalogue dump and the images share the one bandwidth cap.

Finding work does not scan the disk. One query selects, among the images of
stemmed volumes, those with no verified or refused row for their capture, or
(for a RAW master) for their preview. The only file system calls are a stat
and a read of each candidate's own files.

A capture is never blocked. The worker takes no database lock and holds no
transaction across an upload. It reads only images whose rows the capture
has committed, and capture writes each file whole before that commit
(capture/utils.atomic_write). A file whose size or modification time changes
while it is hashed is skipped for this run.

Each file is uploaded from a hard link made in a staging directory under the
projects root, so a renumber that renames the file during the upload cannot
change which bytes are sent. Where a hard link cannot be made (a file system
without hard links), a copy is made there instead, and removed after the
upload.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.core import config
from app.core.catalogue_backup import (
    BackupError,
    VerificationUnavailable,
    _rclone_argv,
    lsjson_rows,
    row_sha1,
    transfer_lock,
)
from app.core.paths import (
    CAPTURES_DIR,
    OFFSITE_STAGING_DIR,
    PREVIEWS_DIR,
    RAW_SUFFIXES,
    is_raw_suffix,
    preview_sidecar_of,
    resolve_within_storage,
)

logger = logging.getLogger(__name__)

CATEGORY = "system"
REFUSED_ACTION = "image_upload_refused"
RUN_ACTION = "image_upload_run"
ADOPT_ACTION = "image_upload_adopted"

# A tenant prefix is a single path segment of the stem alphabet: lowercase
# letters and digits, joined by single hyphens ("sbmal", "ahr", "captua-test").
TENANT_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Under the projects root, so that a hard link to an image is on the image's
# file system in the usual case. Removed after each object and swept at start.
# The integrity check skips it (app.core.integrity._iter_image_files).
STAGING_DIR_NAME = OFFSITE_STAGING_DIR

_UPLOAD_TIMEOUT_SECONDS = 60 * 60
_SHA_CHUNK = 1024 * 1024


class ImageUploadMisconfigured(BackupError):
    """OFFSITE_TENANT is set to something that is not a tenant prefix."""


class UploadFailed(BackupError):
    """The copy command failed or timed out; the run stops and the next tick retries."""


# ---------------------------------------------------------------------------
# Configuration and keys
# ---------------------------------------------------------------------------

def configured_tenant() -> Optional[str]:
    """The tenant prefix, or None when image upload is disabled.

    Raises ImageUploadMisconfigured for a value that is not one path segment
    of the stem alphabet, so a mistyped prefix never becomes a key.
    """
    value = config.settings.OFFSITE_TENANT.strip().strip("/")
    if not value:
        return None
    if not TENANT_PATTERN.match(value):
        raise ImageUploadMisconfigured(
            f"DTK_OFFSITE_TENANT={value!r} is not a tenant prefix "
            "(lowercase letters and digits joined by single hyphens)"
        )
    return value


def is_enabled() -> bool:
    """Whether a remote and a valid tenant are configured."""
    if not config.settings.OFFSITE_REMOTE.strip():
        return False
    try:
        return configured_tenant() is not None
    except ImageUploadMisconfigured:
        return False


def capture_key(tenant: str, stem: str, file_name: str) -> str:
    return f"{tenant}/{stem}/{CAPTURES_DIR}/{file_name}"


def preview_key(tenant: str, stem: str, file_name: str) -> str:
    return f"{tenant}/{stem}/{PREVIEWS_DIR}/{file_name}"


def disambiguated_key(key: str, image_id: int) -> str:
    """`key` with `_img<image id>` before its suffix, in the same directory."""
    parent, name = key.rsplit("/", 1)
    p = Path(name)
    return f"{parent}/{p.stem}_img{image_id}{p.suffix}"


def remote_path(key: str) -> str:
    """`key` under OFFSITE_REMOTE, as rclone addresses it."""
    return f"{config.settings.OFFSITE_REMOTE.rstrip('/')}/{key.strip('/')}"


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

@dataclass
class Listed:
    present: bool
    sha1: Optional[str] = None


def _entries(rel_dir: str, tenant: str) -> Optional[dict]:
    """Name -> listing row for the directory `rel_dir` under the remote, or None if it is absent.

    Nothing above `<remote>/<tenant>/` is ever listed: the appliance key may
    list only under its tenant's prefix. Below the tenant, None only on
    positive evidence: rclone said "directory not found", and the parent's
    listing answered and does not show `rel_dir`. A parent that shows it
    means the two listings disagree, which is not an answer. The tenant
    prefix itself counts as absent when its own listing reports "directory
    not found" (nothing uploaded yet), and any other failure is an error.

    To confirm on B2 with the real appliance key: that listing an empty
    tenant prefix answers with an empty list or "directory not found"
    (exit 3), never a refusal that exits 3.
    """
    rel_dir = rel_dir.strip("/")
    if rel_dir != tenant and not rel_dir.startswith(tenant + "/"):
        raise VerificationUnavailable(
            f"{rel_dir} is not under the tenant prefix {tenant}/, and nothing above it is listed"
        )
    rows = lsjson_rows(remote_path(rel_dir) + "/", absent_ok=True)
    if rows is None:
        if rel_dir == tenant:
            return None
        parent, leaf = rel_dir.rsplit("/", 1)
        parent_entries = _entries(parent, tenant)
        if parent_entries is not None and leaf in parent_entries:
            raise VerificationUnavailable(
                f"rclone reported {rel_dir} as not found, but its parent's listing shows it"
            )
        return None
    return {r.get("Name"): r for r in rows}


def listed_object(key: str) -> Listed:
    """What the listing of `key`'s directory reports at `key`.

    The directory is listed, not the key. `rclone lsjson` on a file path
    answers the same way on the local backend, but on B2 it first looks the
    path up as an object (NewFs calls NewObject, which in rclone 1.70.2
    issues a HEAD on /file/<bucket>/<key>). That needs readFiles, which the
    appliance key withholds, and when the lookup fails rclone treats the
    path as a directory and lists under it, where the object is not: a
    refused lookup then reads as an absent object, the failure decision 2
    is built against. (Read from rclone's source, not tried against B2.)

    Raises VerificationUnavailable when a listing does not answer.
    """
    parent, name = key.strip("/").rsplit("/", 1)
    tenant = configured_tenant()
    if tenant is None:
        raise VerificationUnavailable("no tenant prefix is configured, so nothing is listed")
    entries = _entries(parent, tenant)
    if entries is None or name not in entries:
        return Listed(present=False)
    row = entries[name]
    if row.get("IsDir"):
        # Something is at the key, and it carries no checksum.
        return Listed(present=True, sha1=None)
    return Listed(present=True, sha1=row_sha1(row))


# ---------------------------------------------------------------------------
# Local files
# ---------------------------------------------------------------------------

def _staging_dir() -> Path:
    return config.settings.projects_dir / STAGING_DIR_NAME


def sweep_staging() -> None:
    """Remove the staging directory and any link a killed run left in it."""
    path = _staging_dir()
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
    except OSError:
        logger.exception("Could not remove the offsite staging directory %s", path)


@dataclass
class Staged:
    """A private name for an image's bytes in the staging directory: a hard link, or a copy."""
    path: Path
    linked: bool  # a hard link to the image's file; False for a copy

    def release(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
            self.path.parent.rmdir()
        except OSError:
            # Another file still in it, or a failed unlink; the sweep at the
            # next start clears it.
            pass


def _stage(path: Path) -> Optional[Staged]:
    """A hard link to `path` in the staging directory, or a copy of it where no link can be made.

    A copy where the projects root is on a file system without hard links
    (an exFAT or FAT override drive), so the upload never reads the image by
    its own name. None, with the copy removed, when neither can be made.
    """
    staging = _staging_dir()
    target = staging / f"{secrets.token_hex(6)}{path.suffix}"
    try:
        staging.mkdir(mode=0o700, exist_ok=True)
        os.link(path, target)
        return Staged(target, True)
    except OSError as exc:
        link_error = exc
    try:
        shutil.copyfile(path, target)
    except OSError as exc:
        logger.warning("Could not stage %s for upload (no hard link: %s; no copy: %s)",
                       path, link_error, exc)
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    logger.info("Staged a copy of %s for upload (no hard link: %s)", path, link_error)
    return Staged(target, False)


def stable_digests(path: Path) -> Optional[tuple]:
    """(sha1, sha256) of the file, or None when its size or modification time changed while it was read."""
    try:
        before = path.stat()
        sha1, sha256 = hashlib.sha1(), hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(_SHA_CHUNK), b""):
                sha1.update(chunk)
                sha256.update(chunk)
        after = path.stat()
    except OSError as exc:
        logger.warning("Could not read %s for upload: %s", path, exc)
        return None
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return None
    return sha1.hexdigest(), sha256.hexdigest()


def stable_sha1(path: Path) -> Optional[str]:
    """The file's SHA1, or None when its size or modification time changed while it was read."""
    digests = stable_digests(path)
    return digests[0] if digests else None


def upload_file(local: Path, key: str) -> None:
    """`rclone copyto` one file to `key`. Copy only; nothing here can remove.

    --no-check-dest because the appliance key cannot read, so rclone's
    destination check answers 401 (decision 2). The listing check before the
    upload is what keeps this from overwriting.
    """
    argv = _rclone_argv(
        "copyto", str(local), remote_path(key),
        "--no-check-dest",
        "--bwlimit", config.settings.OFFSITE_BWLIMIT,
    )
    try:
        with transfer_lock:
            result = subprocess.run(
                argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                timeout=_UPLOAD_TIMEOUT_SECONDS,
            )
    except subprocess.TimeoutExpired as exc:
        raise UploadFailed(f"the upload of {key} did not finish within {_UPLOAD_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise UploadFailed(f"rclone could not be started ({argv[0]}): {exc}") from exc
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise UploadFailed(f"the upload of {key} failed (rc={result.returncode}): {stderr[:300]}")


# ---------------------------------------------------------------------------
# Which objects
# ---------------------------------------------------------------------------

def _raw_path_condition(column):
    lowered = func.lower(column)
    return or_(*[lowered.like(f"%{suffix}") for suffix in sorted(RAW_SUFFIXES)])


def _pending(row) -> bool:
    return row is None or (row.verified_at is None and row.refused_at is None)


@dataclass
class Work:
    image_id: int
    kind: str
    stem: str
    master_path: str


def pending_work(db: Session) -> list:
    """The objects of stemmed volumes that have no verified or refused row, in volume and image order.

    A RAW master's preview is listed whether or not its file exists; the run
    skips one that is not on disk.
    """
    from app.models.collection import Collection
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage

    cap = aliased(OffsiteObject)
    prev = aliased(OffsiteObject)
    rows = (
        db.query(RecordImage.id, RecordImage.file_path, Collection.filename_stem, cap, prev)
        .join(Record, RecordImage.record_id == Record.id)
        .join(Collection, Record.collection_id == Collection.id)
        .outerjoin(cap, and_(cap.record_image_id == RecordImage.id, cap.kind == "capture"))
        .outerjoin(prev, and_(prev.record_image_id == RecordImage.id, prev.kind == "preview"))
        .filter(Collection.filename_stem.isnot(None))
        .filter(or_(
            cap.id.is_(None),
            and_(cap.verified_at.is_(None), cap.refused_at.is_(None)),
            and_(prev.id.is_(None), _raw_path_condition(RecordImage.file_path)),
            and_(prev.id.isnot(None), prev.verified_at.is_(None), prev.refused_at.is_(None)),
        ))
        .order_by(Collection.id, RecordImage.id)
        .all()
    )
    work = []
    for image_id, file_path, stem, cap_row, prev_row in rows:
        if _pending(cap_row):
            work.append(Work(image_id, "capture", stem, file_path))
        if is_raw_suffix(Path(file_path)) and _pending(prev_row):
            work.append(Work(image_id, "preview", stem, file_path))
    return work


def local_file(kind: str, master_path: str) -> Optional[Path]:
    """The file on disk an object is read from, confined to storage, or None."""
    master = resolve_within_storage(master_path)
    if master is None:
        return None
    if kind == "capture":
        return master
    if not is_raw_suffix(master):
        return None
    return resolve_within_storage(str(preview_sidecar_of(master)))


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_for(db: Session, image_id: int, kind: str):
    from app.models.offsite_object import OffsiteObject

    return (db.query(OffsiteObject)
            .filter(OffsiteObject.record_image_id == image_id, OffsiteObject.kind == kind)
            .one_or_none())


def _key_holder(db: Session, key: str):
    from app.models.offsite_object import OffsiteObject

    return db.query(OffsiteObject).filter(OffsiteObject.object_key == key).one_or_none()


def _save(db: Session, row, image_id: int, kind: str, key: str, **fields):
    """Create the row at `key`, or update the existing one (whose key is never changed)."""
    from app.models.offsite_object import OffsiteObject

    if row is None:
        row = OffsiteObject(record_image_id=image_id, kind=kind, object_key=key, **fields)
        db.add(row)
    else:
        for name, value in fields.items():
            setattr(row, name, value)
    db.commit()
    return row


def _log(db: Session, *, action: str, level: str, subject: Optional[str], detail: str) -> None:
    from app.core import audit

    audit.log_event(db, level=level, category=CATEGORY, action=action,
                    actor=None, subject=subject, detail=detail[:500])


# ---------------------------------------------------------------------------
# A run
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    status: str = "done"
    uploaded: int = 0          # uploaded and verified by the listing after it
    already_present: int = 0   # same SHA1 already at the key: recorded, not uploaded
    unverified: int = 0        # uploaded, and the listing after it did not show the SHA1
    refused: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    error: Optional[str] = None
    finished_at: Optional[datetime] = None
    fixity: object = None      # the run's FixityIndex, built on first use

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "uploaded": self.uploaded,
            "already_present": self.already_present,
            "unverified": self.unverified,
            "refused": self.refused,
            "conflicts": self.conflicts,
            "skipped": self.skipped,
            "error": self.error,
            "finished_at": self.finished_at,
        }


def _image_state(db: Session, image_id: int) -> Optional[tuple]:
    """(file_path, capture_id, collection_id) of an image as committed now, or None if it is gone.

    A column query, so it reads the database rather than the session's
    identity map.
    """
    from app.models.record import Record, RecordImage

    return (db.query(RecordImage.file_path, RecordImage.capture_id, Record.collection_id)
            .join(Record, RecordImage.record_id == Record.id)
            .filter(RecordImage.id == image_id)
            .one_or_none())


@dataclass
class _Read:
    staged: Staged
    path: Path
    capture_id: Optional[str]
    digests: Optional[tuple]  # (sha1, sha256), or None when the file changed while read


def _read_object(db: Session, item: Work, result: RunResult) -> Optional[_Read]:
    """Pin the bytes of the file the image's row names, or None (reported) when that cannot be done.

    Under export_jobs.hold_for_reading, which a renumber, a record move, a
    rename or a delete cannot overlap (they take hold_for_file_changes): the
    row is re-read, the file is staged as a hard link, and the row is read a
    third time and must name the same file. Where no hard link can be made,
    the file is copied into the staging directory instead, still under the
    hold. From then on the staged file holds this image's bytes whatever
    happens to its name, and it is what is hashed and uploaded.
    """
    from app.core.export_jobs import FilesChanging, export_jobs

    first = _image_state(db, item.image_id)
    db.commit()  # no transaction stays open from here to the copy
    if first is None:
        return None
    try:
        with export_jobs.hold_for_reading([first.collection_id] if first.collection_id else []):
            if _image_state(db, item.image_id) != first:
                db.commit()
                result.skipped.append({"image_id": item.image_id, "kind": item.kind,
                                       "reason": "image changed while it was read"})
                return None
            path = local_file(item.kind, first.file_path)
            if path is None or not path.is_file():
                db.commit()
                if item.kind == "capture":
                    result.skipped.append({"image_id": item.image_id, "kind": item.kind,
                                           "reason": "file not found", "path": first.file_path})
                return None  # a RAW master without a preview has no preview to upload
            staged = _stage(path)
            if staged is None:
                db.commit()
                result.skipped.append({"image_id": item.image_id, "kind": item.kind,
                                       "reason": "file could not be staged", "path": str(path)})
                return None
            same = _image_state(db, item.image_id) == first
            db.commit()
            if not same:
                staged.release()
                result.skipped.append({"image_id": item.image_id, "kind": item.kind,
                                       "reason": "image changed while it was read"})
                return None
    except FilesChanging:
        db.commit()
        result.skipped.append({"image_id": item.image_id, "kind": item.kind,
                               "reason": "files of the volume being changed"})
        return None
    return _Read(staged, path, first.capture_id, stable_digests(staged.path))


def _fixity_index(result: RunResult, *, fresh: bool = False):
    """The capture-time sha256 index, built once per run and only when a run reaches a capture.

    `fresh` rebuilds it: a renumber or a move during the run appends
    relocation records the run's copy does not have.
    """
    if result.fixity is None or fresh:
        from app.core.integrity import build_fixity_index

        result.fixity = build_fixity_index(config.settings.projects_dir.resolve())
    return result.fixity


def _refuse(db: Session, row, item: Work, key: str, result: RunResult, *, reason: str,
            local_sha1: str, remote_sha1: Optional[str] = None, detail: dict) -> None:
    _save(db, row, item.image_id, item.kind, key, local_sha1=local_sha1,
          remote_sha1=remote_sha1, refused_at=_now(), refusal=reason)
    refusal = {"image_id": item.image_id, "kind": item.kind, "reason": reason, **detail}
    result.refused.append(refusal)
    text = "; ".join(f"{k} {v}" for k, v in detail.items())
    logger.error("Refused %s (%s): %s", key, reason, text)
    _log(db, action=REFUSED_ACTION, level="ERR", subject=key, detail=f"{reason}: {text}")


def _process(db: Session, tenant: str, item: Work, result: RunResult) -> None:
    """Upload, record or refuse one object. Raises VerificationUnavailable and UploadFailed."""
    row = _row_for(db, item.image_id, item.kind)
    if not _pending(row):
        return  # recorded since the work list was built (an adoption, say)

    read = _read_object(db, item, result)
    if read is None:
        return
    staged, path = read.staged, read.path
    try:
        if read.digests is None:
            result.skipped.append({"image_id": item.image_id, "kind": item.kind,
                                   "reason": "file changed while it was read", "path": str(path)})
            return
        sha1, sha256 = read.digests

        if row is not None:
            key = row.object_key
        elif item.kind == "capture":
            key = capture_key(tenant, item.stem, path.name)
        else:
            key = preview_key(tenant, item.stem, path.name)

        holder = _key_holder(db, key)
        if holder is not None and row is None:
            # Two images whose names on disk met (legacy names after record
            # moves): the name is already another image's frozen key. This one
            # goes beside it under a name that carries its own id; the listing
            # check below still keeps it from overwriting anything there.
            taken = key
            key = disambiguated_key(key, item.image_id)
            logger.warning("Image %s (%s): %s is image %s's key; using %s",
                           item.image_id, item.kind, taken, holder.record_image_id, key)
            holder = _key_holder(db, key)
        if holder is not None and (row is None or holder.id != row.id):
            result.conflicts.append({"image_id": item.image_id, "kind": item.kind, "key": key,
                                     "held_by_image_id": holder.record_image_id})
            logger.error("Image %s (%s): key %s is already recorded for image %s; not uploaded",
                         item.image_id, item.kind, key, holder.record_image_id)
            return

        if item.kind == "capture":
            expected = _fixity_index(result).expected(read.capture_id, item.image_id, path)
            if expected is not None and expected != sha256:
                # Refused only on the manifests as they stand now, never on
                # the copy read at the start of the run.
                expected = _fixity_index(result, fresh=True).expected(read.capture_id, item.image_id, path)
            if expected is not None and expected != sha256:
                _refuse(db, row, item, key, result, reason="fixity", local_sha1=sha1,
                        detail={"local_path": str(path), "local_sha256": sha256,
                                "capture_sha256": expected, "key": key})
                return

        # No transaction stays open on this session while rclone runs: the
        # reads above began one, and a long copy must not hold it.
        db.commit()
        before = listed_object(key)
        if before.present:
            if before.sha1 is None:
                logger.warning("%s is in the listing with no SHA1; not uploaded, not verified", key)
                result.skipped.append({"image_id": item.image_id, "kind": item.kind,
                                       "reason": "listed with no SHA1", "key": key})
                return
            if before.sha1 == sha1:
                _save(db, row, item.image_id, item.kind, key, local_sha1=sha1, verified_at=_now())
                result.already_present += 1
                return
            _refuse(db, row, item, key, result, reason="remote_differs", local_sha1=sha1,
                    remote_sha1=before.sha1,
                    detail={"local_path": str(path), "local_sha1": sha1,
                            "key": key, "remote_sha1": before.sha1})
            return

        db.commit()
        upload_file(staged.path, key)
        row = _save(db, row, item.image_id, item.kind, key, local_sha1=sha1, uploaded_at=_now())

        after = listed_object(key)
        if after.present and after.sha1 == sha1:
            _save(db, row, item.image_id, item.kind, key, verified_at=_now())
            result.uploaded += 1
            return
        result.unverified += 1
        if after.present and after.sha1 is None:
            # rclone's B2 backend uploads a file above --b2-upload-cutoff
            # (200 MiB by default) in parts, and the listing's SHA1 for such a
            # file comes from its large_file_sha1 metadata. rclone sets that
            # from a local source, but a part-uploaded object without it lists
            # no SHA1, and without a SHA1 nothing is verified.
            logger.warning("Uploaded %s, but its listing carries no SHA1; not verified", key)
        elif after.present:
            logger.warning("Uploaded %s, but the listing shows sha1 %s, not %s; not verified",
                           key, after.sha1, sha1)
        else:
            logger.warning("Uploaded %s, but it is not in the listing; not verified", key)
    finally:
        staged.release()


def run_image_upload(db: Session, stop: Optional[threading.Event] = None) -> dict:
    """One pass over the pending objects. Never raises; the caller gets a status.

    Statuses: "disabled" (no remote or no tenant), "misconfigured" (a tenant
    that is not a prefix), "done", "stopped" (asked to stop between objects),
    "aborted" (a listing did not answer), "failed" (an upload failed).
    """
    result = RunResult()
    if not config.settings.OFFSITE_REMOTE.strip():
        return {"status": "disabled"}
    try:
        tenant = configured_tenant()
    except ImageUploadMisconfigured as exc:
        logger.error("Image upload not run: %s", exc)
        return {"status": "misconfigured", "error": str(exc)}
    if tenant is None:
        return {"status": "disabled"}

    try:
        for item in pending_work(db):
            if stop is not None and stop.is_set():
                result.status = "stopped"
                break
            try:
                _process(db, tenant, item, result)
            except IntegrityError as exc:
                db.rollback()
                result.conflicts.append({"image_id": item.image_id, "kind": item.kind,
                                         "error": str(exc.orig)[:200]})
                logger.error("Image %s (%s) could not be recorded: %s", item.image_id, item.kind, exc.orig)
    except VerificationUnavailable as exc:
        result.status = "aborted"
        result.error = str(exc)
        logger.warning("Image upload aborted: %s", exc)
    except UploadFailed as exc:
        result.status = "failed"
        result.error = str(exc)
        logger.warning("Image upload stopped: %s", exc)

    result.finished_at = _now()
    worked = (result.uploaded or result.already_present or result.unverified
              or result.refused or result.conflicts or result.error)
    if worked:
        level = "INFO" if result.status in ("done", "stopped") and not result.refused else "WARN"
        _log(db, action=RUN_ACTION, level=level, subject=result.status,
             detail=(f"uploaded={result.uploaded} already_present={result.already_present} "
                     f"unverified={result.unverified} refused={len(result.refused)} "
                     f"conflicts={len(result.conflicts)} skipped={len(result.skipped)}"
                     + (f" error: {result.error}" if result.error else "")))
    return result.as_dict()


# ---------------------------------------------------------------------------
# Adoption of objects already in a bucket
# ---------------------------------------------------------------------------

def adopt_objects(db: Session, prefix: str, *, apply: bool = False) -> dict:
    """Record objects already under `prefix` as the frozen keys of matching local images.

    Lists `prefix` (relative to OFFSITE_REMOTE) recursively. Each uploadable
    object of a stemmed volume that has no row yet is hashed; when exactly one
    listed object carries its SHA1, that object's key is recorded as its key,
    verified, with no upload. Two or more listed objects with the SHA1, two
    local objects matching one listed object, or a key another row already
    holds are reported and nothing is recorded for them. Nothing is uploaded
    and nothing at the destination changes.

    Without `apply` nothing is written either: the report says what would be
    recorded. Raises VerificationUnavailable if the listing does not answer.
    """
    from app.models.offsite_object import OffsiteObject

    prefix = prefix.strip().strip("/")
    if not prefix:
        raise ValueError("a prefix is required")
    if not config.settings.OFFSITE_REMOTE.strip():
        raise ValueError("DTK_OFFSITE_REMOTE is not configured")

    listed = lsjson_rows(remote_path(prefix) + "/", recursive=True)
    by_sha1: dict = {}
    no_sha1 = []
    for row in listed:
        if row.get("IsDir"):
            continue
        key = f"{prefix}/{row.get('Path')}"
        sha1 = row_sha1(row)
        if sha1 is None:
            no_sha1.append(key)
        else:
            by_sha1.setdefault(sha1, []).append(key)

    held_keys = {k for (k,) in db.query(OffsiteObject.object_key).all()}

    report = {"prefix": prefix, "listed": len(listed), "listed_without_sha1": no_sha1,
              "adopted": [], "ambiguous_remote": [], "ambiguous_local": [],
              "key_already_held": [], "unmatched": 0, "unreadable": [], "applied": apply}

    candidates = {}  # key -> list of (image_id, kind, path, sha1)
    for item in pending_work(db):
        if _row_for(db, item.image_id, item.kind) is not None:
            continue  # has a key already: an upload in progress, or refused
        # This command runs in a process of its own, outside the backend's
        # read hold, so the file is identified by its inode on both sides of
        # the hash and the row must still name it afterwards: a renumber or a
        # move in between makes the digest someone else's.
        state = _image_state(db, item.image_id)
        path = local_file(item.kind, state.file_path) if state else None
        if path is None or not path.is_file():
            continue
        try:
            before = path.stat()
            sha1 = stable_sha1(path)
            after = path.stat()
        except OSError:
            sha1 = None
        if (sha1 is None or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or _image_state(db, item.image_id) != state):
            report["unreadable"].append(str(path))
            continue
        matches = by_sha1.get(sha1, [])
        if not matches:
            report["unmatched"] += 1
        elif len(matches) > 1:
            report["ambiguous_remote"].append(
                {"image_id": item.image_id, "kind": item.kind, "local_path": str(path),
                 "sha1": sha1, "keys": sorted(matches)})
        elif matches[0] in held_keys:
            report["key_already_held"].append(
                {"image_id": item.image_id, "kind": item.kind, "local_path": str(path),
                 "key": matches[0]})
        else:
            candidates.setdefault(matches[0], []).append((item.image_id, item.kind, path, sha1))

    for key, owners in sorted(candidates.items()):
        if len(owners) > 1:
            report["ambiguous_local"].append(
                {"key": key, "images": [{"image_id": i, "kind": k, "local_path": str(p)}
                                        for i, k, p, _ in owners]})
            continue
        image_id, kind, path, sha1 = owners[0]
        entry = {"image_id": image_id, "kind": kind, "local_path": str(path), "key": key, "sha1": sha1}
        if apply:
            try:
                db.add(OffsiteObject(record_image_id=image_id, kind=kind, object_key=key,
                                     local_sha1=sha1, verified_at=_now()))
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                entry["error"] = str(exc.orig)[:200]
                report["key_already_held"].append(entry)
                continue
        report["adopted"].append(entry)

    if apply:
        _log(db, action=ADOPT_ACTION, level="INFO", subject=prefix,
             detail=(f"adopted={len(report['adopted'])} ambiguous_remote={len(report['ambiguous_remote'])} "
                     f"ambiguous_local={len(report['ambiguous_local'])} "
                     f"key_already_held={len(report['key_already_held'])} unmatched={report['unmatched']}"))
    return report


# ---------------------------------------------------------------------------
# The received indicator, derived
# ---------------------------------------------------------------------------

def _object_counts(db: Session, collection_ids: Optional[Iterable[int]] = None) -> dict:
    """Per stemmed volume: total, verified, pending, refused, last verification, refusals.

    A volume's uploadable objects are each image's capture, and, for a RAW
    master, its preview when the preview has a row or its file is on disk.
    """
    from app.models.collection import Collection
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage

    q = (db.query(RecordImage.id, RecordImage.file_path, Collection.id)
         .join(Record, RecordImage.record_id == Record.id)
         .join(Collection, Record.collection_id == Collection.id)
         .filter(Collection.filename_stem.isnot(None)))
    stemmed = db.query(Collection.id).filter(Collection.filename_stem.isnot(None))
    if collection_ids is not None:
        ids = list(collection_ids)
        q = q.filter(Collection.id.in_(ids))
        stemmed = stemmed.filter(Collection.id.in_(ids))
    images = q.all()

    rows = {}
    image_ids = [i for i, _, _ in images]
    for start in range(0, len(image_ids), 500):
        chunk = image_ids[start:start + 500]
        for row in db.query(OffsiteObject).filter(OffsiteObject.record_image_id.in_(chunk)):
            rows[(row.record_image_id, row.kind)] = row

    out = {cid: {"total": 0, "verified": 0, "refused": 0, "last_verified_at": None, "refusals": []}
           for (cid,) in stemmed.all()}
    for image_id, file_path, cid in images:
        state = out[cid]
        kinds = ["capture"]
        if is_raw_suffix(Path(file_path)):
            if (image_id, "preview") in rows:
                kinds.append("preview")
            else:
                sidecar = local_file("preview", file_path)
                if sidecar is not None and sidecar.is_file():
                    kinds.append("preview")
        for kind in kinds:
            state["total"] += 1
            row = rows.get((image_id, kind))
            if row is None:
                continue
            if row.verified_at is not None:
                state["verified"] += 1
                if state["last_verified_at"] is None or row.verified_at > state["last_verified_at"]:
                    state["last_verified_at"] = row.verified_at
            elif row.refused_at is not None:
                state["refused"] += 1
                state["refusals"].append({"image_id": image_id, "kind": kind, "reason": row.refusal,
                                          "key": row.object_key,
                                          "local_sha1": row.local_sha1, "remote_sha1": row.remote_sha1,
                                          "refused_at": row.refused_at})
    for state in out.values():
        state["pending"] = state["total"] - state["verified"] - state["refused"]
        # A volume with nothing to upload has received nothing.
        state["received"] = state["total"] > 0 and state["verified"] == state["total"]
    return out


def volume_upload_state(db: Session, collection) -> dict:
    """The received indicator of one volume."""
    base = {"collection_id": collection.id, "filename_stem": collection.filename_stem,
            "uploadable": collection.filename_stem is not None}
    if collection.filename_stem is None:
        return {**base, "total": 0, "verified": 0, "pending": 0, "refused": 0,
                "last_verified_at": None, "received": False, "refusals": []}
    return {**base, **_object_counts(db, [collection.id])[collection.id]}


def unit_upload_summary(db: Session) -> dict:
    """Totals over every stemmed volume, for the system panel."""
    try:
        tenant = configured_tenant()
        misconfigured = None
    except ImageUploadMisconfigured as exc:
        tenant, misconfigured = None, str(exc)
    per_volume = _object_counts(db)
    last = [s["last_verified_at"] for s in per_volume.values() if s["last_verified_at"]]
    return {
        "enabled": is_enabled(),
        "tenant": tenant,
        "misconfigured": misconfigured,
        "volumes": len(per_volume),
        "volumes_received": sum(1 for s in per_volume.values() if s["received"]),
        "total": sum(s["total"] for s in per_volume.values()),
        "verified": sum(s["verified"] for s in per_volume.values()),
        "pending": sum(s["pending"] for s in per_volume.values()),
        "refused": sum(s["refused"] for s in per_volume.values()),
        "last_verified_at": max(last) if last else None,
        "last_run": image_upload_scheduler.last_result,
    }


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

class ImageUploadScheduler:
    """In-process schedule for the image upload, beside the catalogue backup's.

    The same single-process assumption as CatalogueBackupScheduler.start, and
    the same delay and interval settings. A run drains every pending object
    one at a time, then waits for the interval. stop() is checked between
    objects; an upload in progress when the process exits is finished by
    nobody, and the next run's listing decides what it left.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_result: Optional[dict] = None

    def start(self) -> bool:
        sweep_staging()
        if not is_enabled():
            logger.info("Offsite image upload is not configured; no schedule started")
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="image-upload", daemon=True)
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
        from app.core.db import SessionLocal

        db = SessionLocal()
        try:
            self.last_result = run_image_upload(db, stop=self._stop)
            return self.last_result
        except Exception:
            logger.exception("Scheduled image upload raised")
            return None
        finally:
            db.close()


image_upload_scheduler = ImageUploadScheduler()


# ---------------------------------------------------------------------------
# Operator command
# ---------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.core.image_upload",
        description="Offsite image upload: operator commands.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    adopt = sub.add_parser(
        "adopt",
        help="record objects already under a prefix as the keys of matching local images",
    )
    adopt.add_argument("prefix", help="remote prefix, relative to DTK_OFFSITE_REMOTE")
    adopt.add_argument("--apply", action="store_true",
                       help="write the rows; without it, only report what would be recorded")
    args = parser.parse_args(argv)

    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        report = adopt_objects(db, args.prefix, apply=args.apply)
    except (ValueError, VerificationUnavailable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
