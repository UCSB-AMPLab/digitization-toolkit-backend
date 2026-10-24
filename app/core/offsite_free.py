"""Freeing a received volume's files from the card, and restoring them (NEH-227 follow-up).

Freeing removes a volume's masters (the RAW, or the JPEG on a JPEG-only rig)
and its RAW previews from the card. Thumbnails stay. The image rows keep
their paths, and `record_images.master_freed_at` / `preview_freed_at` say
that the file is stored offsite: every reader treats such a file as freed,
never as missing (NEH-248).

The rules:

* **Only a received volume.** Every uploadable object of the volume must be
  verified (image_upload.volume_upload_state). Review state and Fisqua play
  no part.
* **A RAW preview needs its sha256 first.** One whose capture records carry
  no sha256 for it stays on the card, reported as `no_preview_digest`
  (_digest_refusal); its master may still be freed.
* **Checked at the moment of freeing, file by file.** The local file's SHA1
  is recomputed and must equal its offsite row's `local_sha1`; then the
  bucket is listed again, and the listing must show the row's key with that
  same SHA1. A file that fails either check stays on the card and is
  reported. A listing that does not answer (VerificationUnavailable) frees
  nothing.
* **Only a volume wholly in `<stem>/`.** A volume with any image still in
  the name-based layout is refused: a project or volume rename moves such
  files under a hold that does not exclude this one, while nothing running
  moves a file in `<stem>/` (see _require_own_folder).
* **Under hold_for_file_changes**, so no export, record move, renumber or
  offsite read of the volume runs meanwhile. No database transaction stays
  open across a listing, a hash or a copy.
* **The row first, then the file.** A file's freed state is committed under
  its record's row lock, conditional on the row still naming that path, and
  the file is unlinked only after that commit. A failed unlink clears the
  state again and is reported.

Restore reads through a second rclone remote, OFFSITE_READ_REMOTE, whose key
may read the tenant's prefix (the upload key cannot), one object at a time
under the upload's transfer_lock and bandwidth cap. Each freed object is
copied to a staging file beside its final name, its SHA1 checked against
the offsite row, and moved into place with os.replace, under the same hold.
A file already at the path is never overwritten.

Both write a JSONL manifest, one line per file, to the volume's
`metadata/offsite-freed-<UTC>.jsonl` or `metadata/offsite-restored-<UTC>.jsonl`,
and a system log event, `volume_freed` or `volume_restored`. The catalogue
dump (app.core.catalogue_backup) is a pg_dumpall of the whole database, so
it carries the freed columns with everything else.

Hashing a volume's masters takes minutes on a Pi, past the proxy's read
timeout, so the routes check what can be refused at once (stem, receipt,
the hold, the listing) and run the rest as a background job.

`mark-freed` (python -m app.core.image_upload mark-freed <stem>) records
files already removed by hand: an absent file whose verified object a fresh
listing shows with the row's SHA1. It never deletes.
"""

import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core import config
from app.core.catalogue_backup import VerificationUnavailable, _rclone_argv, row_sha1, transfer_lock
from app.core.paths import METADATA_DIR

logger = logging.getLogger(__name__)

CATEGORY = "system"
FREED_ACTION = "volume_freed"
RESTORED_ACTION = "volume_restored"

KINDS = ("capture", "preview")

# The per-run manifests in a volume's metadata/ (app.core.volume_layout owns them).
RUN_MANIFEST_PATTERN = re.compile(r"^offsite-(freed|restored)-\d{8}T\d{6}Z(-\d+)?\.jsonl$")

_COPY_TIMEOUT_SECONDS = 60 * 60

NO_STEM_MESSAGE = (
    "This volume has no file name stem, so none of its files are stored offsite "
    "and none can be freed or restored."
)
NOT_RECEIVED_MESSAGE = (
    "Not every file of this volume is verified in the offsite bucket yet, so its "
    "files cannot be freed from the card. Wait for the upload to finish."
)
EXPORT_MESSAGE = (
    "An export of this volume is queued or running. Wait for it to finish, then try again."
)
LISTING_MESSAGE = (
    "The offsite bucket could not be listed, so nothing could be checked against it "
    "and nothing was freed. Try again later."
)
NO_READ_REMOTE_MESSAGE = (
    "Restore is not set up on this unit: it needs read access to the offsite bucket "
    "(DTK_OFFSITE_READ_REMOTE), and none is configured."
)
NOT_IN_OWN_FOLDER_MESSAGE = (
    "Some of this volume's files have not yet been moved into the volume's own folder, "
    "so its files cannot be freed from the card or restored. Captua moves them the next "
    "time it starts. If they are still not moved after a restart, the backend's log says "
    "why (for example, the folder already holds files that are not this volume's); once "
    "that is cleared and Captua has restarted, the volume can be freed."
)
JOB_RUNNING_MESSAGE = (
    "Files of this volume are already being freed or restored. Wait for that to finish."
)
FREED_FILE_MESSAGE = (
    "This file is stored offsite and has been freed from the card to make room. "
    "An admin can restore the volume to bring it back."
)
FREED_EXPORT_MESSAGE = (
    "{n} file(s) of this volume are stored offsite and freed from the card. "
    "Restore the volume before exporting it."
)


class FreeRefused(Exception):
    """A free, a dry run or a restore that cannot start; the route answers status_code with message."""

    def __init__(self, message: str, status_code: int = 409, detail: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail or {}

    def as_detail(self) -> dict:
        return {"message": self.message, **self.detail}


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------

def _columns(kind: str):
    from app.models.record import RecordImage

    if kind == "capture":
        return RecordImage.master_freed_at, RecordImage.master_freed_bytes
    return RecordImage.preview_freed_at, RecordImage.preview_freed_bytes


def freed_at_of(image, kind: str) -> Optional[datetime]:
    """When the image's master ("capture") or preview was freed, or None."""
    return image.master_freed_at if kind == "capture" else image.preview_freed_at


def freed_detail(db: Session, image, kind: str) -> dict:
    """The JSON detail the image routes answer 410 with for a freed file."""
    from app.models.offsite_object import OffsiteObject

    key = (db.query(OffsiteObject.object_key)
           .filter(OffsiteObject.record_image_id == image.id, OffsiteObject.kind == kind)
           .scalar())
    freed_at = freed_at_of(image, kind)
    return {
        "message": FREED_FILE_MESSAGE,
        "stored_offsite": True,
        "image_id": image.id,
        "kind": kind,
        "key": key,
        "freed_at": freed_at.isoformat() if freed_at else None,
    }


# ---------------------------------------------------------------------------
# Objects
# ---------------------------------------------------------------------------

@dataclass
class _Object:
    image_id: int
    record_id: int
    kind: str
    file_path: str          # the master's path as the row stores it
    path: Optional[Path]    # the file this object is, confined to storage
    key: str
    sha1: str               # the offsite row's local_sha1
    freed_bytes: Optional[int] = None
    capture_id: Optional[str] = None

    def entry(self, **extra) -> dict:
        return {"image_id": self.image_id, "kind": self.kind,
                "path": str(self.path) if self.path else self.file_path, "key": self.key, **extra}


def _objects(db: Session, collection_id: int, *, freed: bool) -> list:
    """The volume's objects with a verified offsite row, freed or not, in image then kind order."""
    from app.core.image_upload import local_file
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage

    rows = (db.query(RecordImage.id, RecordImage.record_id, RecordImage.file_path,
                     RecordImage.master_freed_at, RecordImage.master_freed_bytes,
                     RecordImage.preview_freed_at, RecordImage.preview_freed_bytes,
                     OffsiteObject.kind, OffsiteObject.object_key, OffsiteObject.local_sha1,
                     RecordImage.capture_id)
            .join(Record, RecordImage.record_id == Record.id)
            .join(OffsiteObject, OffsiteObject.record_image_id == RecordImage.id)
            .filter(Record.collection_id == collection_id, OffsiteObject.verified_at.isnot(None))
            .order_by(RecordImage.id, OffsiteObject.kind)
            .all())
    db.commit()
    out = []
    for (image_id, record_id, file_path, m_at, m_bytes, p_at, p_bytes, kind, key, sha1, capture_id) in rows:
        at, size = (m_at, m_bytes) if kind == "capture" else (p_at, p_bytes)
        if (at is not None) != freed:
            continue
        out.append(_Object(image_id, record_id, kind, file_path, local_file(kind, file_path), key, sha1, size,
                           capture_id))
    return out


@dataclass
class _Listed:
    present: bool
    sha1: Optional[str] = None
    size: Optional[int] = None


def _listings(keys: Iterable[str]) -> dict:
    """key -> _Listed, from one fresh listing of each key's directory. Raises VerificationUnavailable."""
    from app.core.image_upload import _entries, configured_tenant

    tenant = configured_tenant()
    if tenant is None:
        raise VerificationUnavailable("no tenant prefix is configured, so nothing is listed")
    by_dir: dict = {}
    for key in keys:
        parent, name = key.strip("/").rsplit("/", 1)
        by_dir.setdefault(parent, []).append((key, name))
    out = {}
    for parent, members in sorted(by_dir.items()):
        entries = _entries(parent, tenant)
        for key, name in members:
            row = (entries or {}).get(name)
            if row is None:
                out[key] = _Listed(False)
            elif row.get("IsDir"):
                out[key] = _Listed(True, None)
            else:
                out[key] = _Listed(True, row_sha1(row), row.get("Size"))
    return out


def _fixity_index():
    from app.core.integrity import build_fixity_index

    return build_fixity_index(config.settings.projects_dir.resolve())


def _digest_refusal(obj: _Object, index) -> Optional[dict]:
    """A RAW preview whose capture records carry no sha256 for it stays on the card.

    Fisqua needs a sha256 for every preview, computed on the unit from the
    local file; once freed, a preview without one could never get one. The
    lookup is the fixity index's (FixityIndex.expected) by the preview's
    path, then its name, under the image's capture id, and never by image
    id: a relocation record naming the image carries its master's sha256.
    The master is checked on its own and may be freed.
    """
    if obj.kind != "preview" or obj.path is None:
        return None
    if index.expected(obj.capture_id, None, obj.path) is None:
        return obj.entry(reason="no_preview_digest")
    return None


def _listing_refusal(obj: _Object, listed: Optional[_Listed]) -> Optional[dict]:
    if listed is None or not listed.present:
        return obj.entry(reason="not_in_bucket")
    if listed.sha1 is None:
        return obj.entry(reason="listed_without_sha1")
    if listed.sha1 != obj.sha1:
        return obj.entry(reason="bucket_sha1_differs", listed_sha1=listed.sha1, expected_sha1=obj.sha1)
    return None


# ---------------------------------------------------------------------------
# Preconditions and the hold
# ---------------------------------------------------------------------------

def _volume(db: Session, collection_id: int):
    from app.models.collection import Collection

    collection = db.query(Collection).filter(Collection.id == collection_id).first()
    if collection is None:
        raise FreeRefused(f"Collection {collection_id} not found", status_code=404)
    if collection.filename_stem is None:
        raise FreeRefused(NO_STEM_MESSAGE)
    return collection


def _require_own_folder(db: Session, collection) -> None:
    """Refuse a volume with any image still in the name-based layout.

    Only such files move while the backend runs, under holds that do not
    exclude this one: a project rename moves the project's directory tree,
    and a project move or a volume rename or re-parent relocates the files
    storage_ops.image_in_old_layout picks out. `<stem>/` sits directly under
    the projects root, outside every project's directory, so none of them
    touches it. Files in `<stem>/` are moved by nothing but the startup
    layout migration, which runs before any request. So a volume wholly in
    `<stem>/` cannot have a file moved under a free or a restore.
    """
    from app.core.storage_ops import image_in_old_layout
    from app.models.record import Record, RecordImage

    paths = (db.query(RecordImage.file_path)
             .join(Record, RecordImage.record_id == Record.id)
             .filter(Record.collection_id == collection.id).all())
    db.commit()
    outside = [fp for (fp,) in paths if image_in_old_layout(collection, fp)]
    if outside:
        raise FreeRefused(NOT_IN_OWN_FOLDER_MESSAGE, detail={
            "folder": f"{collection.filename_stem}/", "images_outside_own_folder": len(outside)})


def _stem_volume(db: Session, collection_id: int):
    """The volume, refused unless it has a stem and every image is in `<stem>/`."""
    collection = _volume(db, collection_id)
    _require_own_folder(db, collection)
    return collection


def _require_received(db: Session, collection) -> None:
    from app.core.image_upload import volume_upload_state

    state = volume_upload_state(db, collection)
    db.commit()
    if not state["received"]:
        raise FreeRefused(NOT_RECEIVED_MESSAGE, detail={
            "total": state["total"], "verified": state["verified"],
            "pending": state["pending"], "refused": state["refused"]})


class _Hold:
    """export_jobs.hold_for_file_changes on one volume, entered here and released by whoever finishes the work.

    Entered in the request, so its 409s are the request's; released in the
    job's thread when the work runs in the background.
    """

    def __init__(self, collection_id: int):
        from app.core.export_jobs import export_jobs

        self._cm = export_jobs.hold_for_file_changes([collection_id], EXPORT_MESSAGE)
        try:
            self._cm.__enter__()
        except HTTPException as exc:
            raise FreeRefused(exc.detail if isinstance(exc.detail, str) else str(exc.detail)) from exc
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._cm.__exit__(None, None, None)


def _check_listing(objects: list) -> dict:
    try:
        return _listings(o.key for o in objects)
    except VerificationUnavailable as exc:
        raise FreeRefused(LISTING_MESSAGE, detail={"error": str(exc)}) from exc


# ---------------------------------------------------------------------------
# Writing the state
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _image_exists(db: Session, image_id: int) -> bool:
    from app.models.record import RecordImage

    exists = db.query(RecordImage.id).filter(RecordImage.id == image_id).first() is not None
    db.commit()
    return exists


def _lock_and_recheck(db: Session, obj: _Object, *, freed: bool) -> bool:
    """Lock the object's record, then re-read its image: True when the row is there, names obj.file_path, and is freed (or not) as `freed` says.

    Leaves the transaction open, holding the record's row lock, for the
    caller to commit or roll back. Record before volume, as on every path;
    delete_record and delete_image take this same lock before they decide
    which files to unlink, and unlink only after their commit, so a delete
    and anything done here under the lock cannot interleave.
    """
    from app.models.record import Record, RecordImage

    at_col, _ = _columns(obj.kind)
    db.query(Record.id).filter(Record.id == obj.record_id).with_for_update().one_or_none()
    row = (db.query(RecordImage.file_path, at_col)
           .filter(RecordImage.id == obj.image_id).one_or_none())
    return row is not None and row[0] == obj.file_path and (row[1] is not None) == freed


def _set_freed(db: Session, obj: _Object, *, at: Optional[datetime], size: Optional[int]) -> bool:
    """Set (or, with at=None, clear) the object's freed state; True when the row still names obj.file_path.

    Under the record's row lock, the lock every path that changes an image
    takes first (record before volume; no volume lock is taken here). One
    record at a time, committed before the next, so no two record locks are
    ever held together.
    """
    from app.models.record import RecordImage

    at_col, bytes_col = _columns(obj.kind)
    if not _lock_and_recheck(db, obj, freed=at is None):
        db.rollback()
        return False
    (db.query(RecordImage).filter(RecordImage.id == obj.image_id)
     .update({at_col: at, bytes_col: size}, synchronize_session=False))
    db.commit()
    return True


class _RunManifest:
    """`<stem>/metadata/offsite-<action>-<UTC>.jsonl`, created on its first line, each line flushed to disk."""

    def __init__(self, stem: str, action: str):
        from capture.project_manager import volume_root

        self.directory = volume_root(stem) / METADATA_DIR
        self.action = action
        self.path: Optional[Path] = None

    def _open_path(self) -> Path:
        stamp = _now().strftime("%Y%m%dT%H%M%SZ")
        self.directory.mkdir(parents=True, exist_ok=True)
        for n in range(1, 1000):
            name = f"offsite-{self.action}-{stamp}{'' if n == 1 else f'-{n}'}.jsonl"
            candidate = self.directory / name
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                continue
            os.close(fd)
            return candidate
        raise OSError(f"no free manifest name in {self.directory}")

    def write(self, line: dict) -> None:
        try:
            if self.path is None:
                self.path = self._open_path()
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            # The database holds the state; the manifest is its record on the card.
            logger.exception("Could not write the %s manifest line for %s", self.action, line.get("key"))


def _log(db: Session, *, action: str, level: str, actor: Optional[str], stem: str, detail: str) -> None:
    from app.core import audit

    audit.log_event(db, level=level, category=CATEGORY, action=action, actor=actor,
                    subject=stem, detail=detail[:500])


# ---------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------

def plan_free(db: Session, collection_id: int) -> dict:
    """What freeing the volume would remove, and what it would refuse and why. Changes nothing.

    The same refusals as a free, and the same listing check. The local SHA1s
    are not recomputed here (that is the long part), so a file that would
    fail the local check is reported only when it is freed.
    """
    collection = _stem_volume(db, collection_id)
    _require_received(db, collection)
    hold = _Hold(collection_id)
    try:
        objects = _objects(db, collection_id, freed=False)
        listed = _check_listing(objects)
    finally:
        hold.release()
    files, refused = [], []
    index = _fixity_index()
    for obj in objects:
        refusal = _digest_refusal(obj, index) or _listing_refusal(obj, listed.get(obj.key))
        if refusal is None and (obj.path is None or not obj.path.is_file()):
            refusal = obj.entry(reason="local_file_missing")
        if refusal is not None:
            refused.append(refusal)
            continue
        files.append(obj.entry(bytes=obj.path.stat().st_size, sha1=obj.sha1))
    return {
        "collection_id": collection_id, "filename_stem": collection.filename_stem,
        "would_free": {"files": len(files), "bytes": sum(f["bytes"] for f in files)},
        "files": files, "refused": refused, "local_sha1_checked": False,
    }


# ---------------------------------------------------------------------------
# Freeing
# ---------------------------------------------------------------------------

def _stable_sha1(path: Path) -> Optional[str]:
    """The file's SHA1 alone (no sha256: freeing hashes every master), or None if it changed or could not be read."""
    import hashlib

    try:
        before = path.stat()
        sha1 = hashlib.sha1()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                sha1.update(chunk)
        after = path.stat()
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return None
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return None
    return sha1.hexdigest()


def _free_work(db: Session, collection_id: int, stem: str, actor: Optional[str]) -> dict:
    """Hash, list again, and free what passes. Runs under the caller's hold."""
    _stem_volume(db, collection_id)  # again, in the job: the request's check is a moment old
    objects = _objects(db, collection_id, freed=False)
    refused, hashed = [], []
    index = _fixity_index()
    for obj in objects:
        refusal = _digest_refusal(obj, index)
        if refusal is not None:
            refused.append(refusal)
            continue
        if obj.path is None or not obj.path.is_file():
            refused.append(obj.entry(reason="local_file_missing"))
            continue
        try:
            size = obj.path.stat().st_size
        except OSError:
            size = None
        local = _stable_sha1(obj.path)
        if local is None or size is None:
            refused.append(obj.entry(reason="local_file_unreadable"))
            continue
        if local != obj.sha1:
            refused.append(obj.entry(reason="local_sha1_differs", local_sha1=local, expected_sha1=obj.sha1))
            continue
        hashed.append((obj, size))

    # Listed after the hashing, so the check is as fresh as it can be when the file goes.
    listed = _listings(o.key for o, _ in hashed) if hashed else {}

    manifest = _RunManifest(stem, "freed")
    freed = []
    for obj, size in hashed:
        refusal = _listing_refusal(obj, listed.get(obj.key))
        if refusal is not None:
            refused.append(refusal)
            continue
        at = _now()
        if not _set_freed(db, obj, at=at, size=size):
            # Deleted, or its path changed, since the work list was read.
            refused.append(obj.entry(reason="changed"))
            continue
        # The record's lock is released at that commit, so a delete can run
        # between it and the unlink. The delete removes the row and then the
        # same file; whichever unlink comes second finds nothing. Either way
        # the row and the card agree, and only the report has to say which.
        try:
            obj.path.unlink()
        except FileNotFoundError:
            if not _image_exists(db, obj.image_id):
                refused.append(obj.entry(reason="changed"))
                continue
        except OSError as exc:
            logger.warning("Freed state set but %s could not be removed; clearing it: %s", obj.path, exc)
            _set_freed(db, obj, at=None, size=None)
            refused.append(obj.entry(reason="could_not_remove", error=str(exc)[:200]))
            continue
        entry = obj.entry(bytes=size, sha1=obj.sha1, freed_at=at.isoformat())
        freed.append(entry)
        manifest.write(entry)

    total = sum(f["bytes"] for f in freed)
    if freed or refused:
        _log(db, action=FREED_ACTION, level="WARN" if refused else "INFO", actor=actor, stem=stem,
             detail=f"files={len(freed)} bytes={total} refused={len(refused)}"
                    + (f" manifest={manifest.path.name}" if manifest.path else ""))
    return {"collection_id": collection_id, "filename_stem": stem,
            "freed": {"files": len(freed), "bytes": total},
            "files": freed, "refused": refused,
            "manifest": str(manifest.path) if manifest.path else None}


def _prepare_free(db: Session, collection_id: int):
    """The checks a free must pass before it starts; returns (collection, hold). The caller releases the hold."""
    collection = _stem_volume(db, collection_id)
    _require_received(db, collection)
    hold = _Hold(collection_id)
    try:
        _check_listing(_objects(db, collection_id, freed=False))
    except BaseException:
        hold.release()
        raise
    return collection, hold


def free_volume(db: Session, collection_id: int, actor: Optional[str] = None) -> dict:
    """Free the volume's files in this thread. Raises FreeRefused for what the route answers 409 to."""
    collection, hold = _prepare_free(db, collection_id)
    try:
        return _free_work(db, collection_id, collection.filename_stem, actor)
    except VerificationUnavailable as exc:
        raise FreeRefused(LISTING_MESSAGE, detail={"error": str(exc)}) from exc
    finally:
        hold.release()


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def read_remote_path(key: str) -> str:
    return f"{config.settings.OFFSITE_READ_REMOTE.strip().rstrip('/')}/{key.strip('/')}"


def _copy_from_bucket(key: str, dest: Path) -> Optional[str]:
    """`rclone copyto` one object from the read remote to `dest`; an error message, or None on success.

    --inplace, so rclone writes `dest` itself rather than a `.partial` file
    of its own beside it: the staging name is the only name a killed copy
    can leave. Under the upload's transfer_lock and OFFSITE_BWLIMIT, so one
    rclone transfer runs at a time and the unit's link carries the same
    load whichever way the bytes go.
    """
    argv = _rclone_argv("copyto", read_remote_path(key), str(dest), "--inplace",
                        "--bwlimit", config.settings.OFFSITE_BWLIMIT)
    try:
        with transfer_lock:
            result = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                    timeout=_COPY_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return f"the copy of {key} did not finish within {_COPY_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return f"rclone could not be started ({argv[0]}): {exc}"
    if result.returncode != 0:
        return f"the copy of {key} failed (rc={result.returncode}): {(result.stderr or result.stdout or '').strip()[:300]}"
    return None


def _fsync_file(path: Path) -> None:
    """fsync a file's contents; raises OSError, which refuses the restore of that file."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _restore_one(db: Session, obj: _Object) -> tuple:
    """(entry, None) when restored, (None, refusal) otherwise."""
    from app.core.storage_override import refuse_writes_under_unusable_root

    target = obj.path
    if target is None:
        return None, obj.entry(reason="outside_storage")
    # Before anything is created: with the drive gone, its mount point is a
    # directory on the card, and a file restored there would vanish under the
    # drive when it returns, with its freed state already cleared. Raises
    # StorageUnavailableError, which ends the job and clears nothing more.
    refuse_writes_under_unusable_root([target])
    if target.exists():
        # Put back by other means. Never overwritten: kept as it is, and the
        # state cleared only when the bytes are the object's.
        if target.is_file() and _stable_sha1(target) == obj.sha1:
            if _set_freed(db, obj, at=None, size=None):
                return obj.entry(bytes=target.stat().st_size, sha1=obj.sha1, already_present=True), None
            return None, obj.entry(reason="changed")
        return None, obj.entry(reason="local_file_differs")

    target.parent.mkdir(parents=True, exist_ok=True)
    # `.<name>.<token>.part` beside the final name: the same file system, so
    # os.replace is atomic, and a name app.core.volume_layout reads as a
    # leftover temporary file rather than as a foreign one.
    staging = target.parent / f".{target.name}.{secrets.token_hex(4)}.part"
    try:
        error = _copy_from_bucket(obj.key, staging)
        if error is not None:
            return None, obj.entry(reason="copy_failed", error=error)
        copied = _stable_sha1(staging)
        if copied != obj.sha1:
            return None, obj.entry(reason="sha1_differs", copied_sha1=copied, expected_sha1=obj.sha1)
        size = staging.stat().st_size
        # On disk before it can take the final name and the state is cleared,
        # as capture.utils.atomic_write does: otherwise a power cut could
        # leave a cleared state over a truncated file, which a later restore
        # would find in place and skip. Done before _place, so the record's
        # lock is not held across it.
        _fsync_file(staging)
        placed = _place(db, obj, staging, target)
    except OSError as exc:
        db.rollback()
        return None, obj.entry(reason="could_not_write", error=str(exc)[:200])
    finally:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
    if placed is not True:
        return None, obj.entry(reason=placed)
    return obj.entry(bytes=size, sha1=obj.sha1), None


def _place(db: Session, obj: _Object, staging: Path, target: Path):
    """Move the verified staging copy into place and clear the freed state, under the record's lock.

    True when placed; otherwise the refusal's reason, with the staging copy
    left for the caller to remove. A delete that committed during the copy
    leaves no row, so nothing is placed and nothing is written back to the
    card ("changed"). A delete waiting on the lock runs after this commit and
    removes the placed file with its row. If the commit fails after the
    replace, the file is back and still marked freed; the next restore finds
    it in place with the object's SHA1 and clears the state.
    """
    from app.models.record import RecordImage

    at_col, bytes_col = _columns(obj.kind)
    if not _lock_and_recheck(db, obj, freed=True):
        db.rollback()
        return "changed"
    if target.exists():
        db.rollback()
        return "local_file_differs"
    os.replace(staging, target)
    _fsync_dir(target.parent)
    (db.query(RecordImage).filter(RecordImage.id == obj.image_id)
     .update({at_col: None, bytes_col: None}, synchronize_session=False))
    db.commit()
    return True


def _restore_work(db: Session, collection_id: int, stem: str, actor: Optional[str]) -> dict:
    _stem_volume(db, collection_id)  # again, in the job: the request's check is a moment old
    manifest = _RunManifest(stem, "restored")
    restored, refused = [], []
    stopped = None
    try:
        for obj in _objects(db, collection_id, freed=True):
            entry, refusal = _restore_one(db, obj)
            if refusal is not None:
                logger.warning("Restore of %s (%s) refused: %s", obj.key, obj.kind, refusal["reason"])
                refused.append(refusal)
                continue
            entry["restored_at"] = _now().isoformat()
            restored.append(entry)
            manifest.write(entry)
    except Exception as exc:
        stopped = exc
        raise
    finally:
        total = sum(r["bytes"] for r in restored)
        if restored or refused or stopped is not None:
            _log(db, action=RESTORED_ACTION, level="WARN" if refused or stopped else "INFO", actor=actor,
                 stem=stem,
                 detail=f"files={len(restored)} bytes={total} refused={len(refused)}"
                        + (f" manifest={manifest.path.name}" if manifest.path else "")
                        + (f" stopped: {stopped}" if stopped is not None else ""))
    return {"collection_id": collection_id, "filename_stem": stem,
            "restored": {"files": len(restored), "bytes": total},
            "files": restored, "refused": refused,
            "manifest": str(manifest.path) if manifest.path else None}


def _prepare_restore(db: Session, collection_id: int):
    from app.core.storage_override import refuse_writes_under_unusable_root
    from capture.project_manager import volume_root

    collection = _stem_volume(db, collection_id)
    if not config.settings.OFFSITE_READ_REMOTE.strip():
        raise FreeRefused(NO_READ_REMOTE_MESSAGE)
    # StorageUnavailableError, which the app answers with 503, as it does for
    # every other writer; each file is checked again before it is written.
    refuse_writes_under_unusable_root([volume_root(collection.filename_stem, config.settings.projects_dir)])
    return collection, _Hold(collection_id)


def restore_volume(db: Session, collection_id: int, actor: Optional[str] = None) -> dict:
    """Restore the volume's freed files in this thread. Raises FreeRefused for the 409s."""
    collection, hold = _prepare_restore(db, collection_id)
    try:
        return _restore_work(db, collection_id, collection.filename_stem, actor)
    finally:
        hold.release()


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------

class OffsiteJob:
    def __init__(self, collection_id: int, action: str):
        self.id = uuid.uuid4().hex
        self.collection_id = collection_id
        self.action = action            # "free" | "restore"
        self.state = "queued"           # queued | running | done | failed
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.created_at = time.time()
        self.finished_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {"job_id": self.id, "collection_id": self.collection_id, "action": self.action,
                "state": self.state, "result": self.result, "error": self.error}


class OffsiteJobRegistry:
    """One free or restore per volume at a time, in this process (the appliance runs one)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict = {}
        self._active: dict = {}

    def reserve(self, collection_id: int, action: str) -> OffsiteJob:
        with self._lock:
            active = self._jobs.get(self._active.get(collection_id))
            if active is not None and active.state in ("queued", "running"):
                raise FreeRefused(JOB_RUNNING_MESSAGE, detail={"job_id": active.id})
            job = OffsiteJob(collection_id, action)
            self._jobs[job.id] = job
            self._active[collection_id] = job.id
            return job

    def discard(self, job: OffsiteJob) -> None:
        with self._lock:
            self._jobs.pop(job.id, None)
            if self._active.get(job.collection_id) == job.id:
                del self._active[job.collection_id]

    def get(self, job_id: str) -> Optional[OffsiteJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def run(self, job: OffsiteJob, work: Callable[[], dict], hold: _Hold) -> None:
        def target():
            job.state = "running"
            try:
                job.result = work()
                job.state = "done"
            except VerificationUnavailable as exc:
                job.state, job.error = "failed", f"{LISTING_MESSAGE} ({exc})"
            except FreeRefused as exc:
                job.state, job.error = "failed", exc.message
            except Exception as exc:
                job.state, job.error = "failed", str(exc)[:500]
                logger.exception("Offsite %s job for volume %s failed", job.action, job.collection_id)
            finally:
                hold.release()
                job.finished_at = time.time()
                with self._lock:
                    if self._active.get(job.collection_id) == job.id:
                        del self._active[job.collection_id]

        try:
            threading.Thread(target=target, name=f"offsite-{job.action}", daemon=True).start()
        except BaseException:
            hold.release()
            self.discard(job)
            raise


offsite_jobs = OffsiteJobRegistry()


def _in_own_session(fn: Callable[[Session], dict]) -> Callable[[], dict]:
    def work():
        from app.core.db import SessionLocal

        db = SessionLocal()
        try:
            return fn(db)
        finally:
            db.close()
    return work


def start_free(db: Session, collection_id: int, actor: Optional[str]) -> OffsiteJob:
    """Check what can be refused now, then free in the background. Raises FreeRefused."""
    job = offsite_jobs.reserve(collection_id, "free")
    try:
        collection, hold = _prepare_free(db, collection_id)
    except BaseException:
        offsite_jobs.discard(job)
        raise
    stem = collection.filename_stem
    offsite_jobs.run(job, _in_own_session(lambda s: _free_work(s, collection_id, stem, actor)), hold)
    return job


def start_restore(db: Session, collection_id: int, actor: Optional[str]) -> OffsiteJob:
    job = offsite_jobs.reserve(collection_id, "restore")
    try:
        collection, hold = _prepare_restore(db, collection_id)
    except BaseException:
        offsite_jobs.discard(job)
        raise
    stem = collection.filename_stem
    offsite_jobs.run(job, _in_own_session(lambda s: _restore_work(s, collection_id, stem, actor)), hold)
    return job


# ---------------------------------------------------------------------------
# Files removed by hand
# ---------------------------------------------------------------------------

def mark_freed(db: Session, stem: str, *, apply: bool = False) -> dict:
    """Mark as freed each absent file of the volume whose verified object a fresh listing shows with the row's SHA1.

    For units whose files were removed outside Captua. Nothing is deleted
    and nothing in the bucket changes. An absent file the bucket does not
    account for (no verified row, not listed, listed with another SHA1) is
    reported under `unaccounted` and left as it is. The size recorded is the
    listing's. Without `apply` nothing is written.

    Runs in a process of its own, outside the backend's hold, so each file's
    absence is checked again right before its row is written. Raises
    ValueError for an unknown stem and VerificationUnavailable when a
    listing does not answer.
    """
    from app.core.image_upload import local_file
    from app.models.collection import Collection
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage

    collection = db.query(Collection).filter(Collection.filename_stem == stem).one_or_none()
    if collection is None:
        raise ValueError(f"no volume has the file name stem {stem!r}")

    absent, unaccounted = [], []
    rows = (db.query(RecordImage.id, RecordImage.record_id, RecordImage.file_path,
                     RecordImage.master_freed_at, RecordImage.preview_freed_at)
            .join(Record, RecordImage.record_id == Record.id)
            .filter(Record.collection_id == collection.id)
            .order_by(RecordImage.id).all())
    objects = {(r.record_image_id, r.kind): r for r in db.query(OffsiteObject).filter(
        OffsiteObject.record_image_id.in_([r[0] for r in rows] or [-1]))}
    db.commit()
    from app.core.paths import is_raw_suffix

    for image_id, record_id, file_path, m_at, p_at in rows:
        for kind, at in (("capture", m_at), ("preview", p_at)):
            if at is not None:
                continue
            if kind == "preview" and not is_raw_suffix(Path(file_path)):
                continue
            path = local_file(kind, file_path)
            if path is not None and path.is_file():
                continue
            row = objects.get((image_id, kind))
            if kind == "preview" and row is None:
                continue  # a RAW master without a preview never had one to free
            if row is None or row.verified_at is None:
                unaccounted.append({"image_id": image_id, "kind": kind, "path": str(path) if path else file_path,
                                    "key": row.object_key if row else None, "reason": "no_verified_object"})
                continue
            absent.append(_Object(image_id, record_id, kind, file_path, path, row.object_key, row.local_sha1))

    listed = _listings(o.key for o in absent) if absent else {}
    manifest = _RunManifest(stem, "freed") if apply else None
    marked = []
    for obj in absent:
        info = listed.get(obj.key)
        refusal = _listing_refusal(obj, info)
        if refusal is not None:
            unaccounted.append(refusal)
            continue
        entry = obj.entry(bytes=info.size, sha1=obj.sha1)
        if apply:
            if obj.path is not None and obj.path.exists():
                unaccounted.append(obj.entry(reason="file_reappeared"))
                continue
            at = _now()
            if not _set_freed(db, obj, at=at, size=info.size if info.size is not None else 0):
                unaccounted.append(obj.entry(reason="changed"))
                continue
            entry["freed_at"] = at.isoformat()
            entry["method"] = "mark-freed"
            manifest.write(entry)
        marked.append(entry)

    report = {"filename_stem": stem, "collection_id": collection.id, "applied": apply,
              "marked": marked, "unaccounted": unaccounted,
              "marked_bytes": sum(m["bytes"] or 0 for m in marked),
              "manifest": str(manifest.path) if manifest and manifest.path else None}
    if apply and (marked or unaccounted):
        _log(db, action=FREED_ACTION, level="WARN" if unaccounted else "INFO", actor=None, stem=stem,
             detail=f"mark-freed: files={len(marked)} bytes={report['marked_bytes']} "
                    f"unaccounted={len(unaccounted)}")
    return report
