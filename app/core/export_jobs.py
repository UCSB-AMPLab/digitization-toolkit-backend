"""In-process registry of background export jobs for the single-process appliance"""

import logging
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from typing import Callable, Iterable, Iterator, Optional

from fastapi import HTTPException

logger = logging.getLogger(__name__)


class ExportJob:
    def __init__(self, collection_id: int):
        self.id = uuid.uuid4().hex
        self.collection_id = collection_id
        self.state = "queued"        # queued | running | done | failed
        self.done = 0
        self.total = 0
        self.zip_filename: Optional[str] = None
        self.error: Optional[str] = None       # human-readable message
        self.detail = None                      # structured detail (e.g. missing/mismatch lists)
        self.created_at = time.time()
        self.finished_at: Optional[float] = None

    def set_progress(self, done: int, total: int) -> None:
        self.done = done
        self.total = total

    def to_dict(self) -> dict:
        return {
            "job_id": self.id,
            "collection_id": self.collection_id,
            "state": self.state,
            "done": self.done,
            "total": self.total,
            "zip_filename": self.zip_filename,
            "error": self.error,
            "detail": self.detail,
        }


EXPORT_RUNNING_MESSAGE = (
    "An export of this collection is running. Wait for it to finish, then try again."
)
FILES_CHANGING_MESSAGE = (
    "The files of this collection are being moved or renamed. Try the export again in a moment."
)
FILES_BEING_READ_MESSAGE = (
    "Captua is preparing a file of this volume for the offsite copy. Try again in a moment."
)


class FilesChanging(Exception):
    """A read of a collection's files was asked for while a request is changing them."""


class ExportJobRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[str, ExportJob] = {}
        self._active_by_collection: dict[int, str] = {}
        # Collections whose files a request is moving, renaming or deleting,
        # with how many such requests hold each one.
        self._held: Counter = Counter()
        # Collections whose files the offsite upload is reading by path
        # (hold_for_reading), with how many such reads hold each one.
        self._reading: Counter = Counter()

    @contextmanager
    def hold_for_file_changes(
        self, collection_ids: Iterable[int], message: str = EXPORT_RUNNING_MESSAGE
    ) -> Iterator[None]:
        """Hold collections for a request that moves, renames or deletes their files.

        Raises HTTPException 409 with `message` when an export of any of the
        collections is queued or running. Otherwise the collections are held
        until the block exits, and `start` refuses to export a held collection.
        The check and the hold are taken under one lock, and `start` checks the
        hold under the same lock, so an export cannot begin between the check
        and the file work. This is in-process state: it covers the single
        backend process the appliance runs.
        """
        ids = set(collection_ids)
        with self._lock:
            for cid in ids:
                jid = self._active_by_collection.get(cid)
                job = self._jobs.get(jid) if jid else None
                if job and job.state in ("queued", "running"):
                    raise HTTPException(status_code=409, detail=message)
                if self._reading[cid] > 0:
                    raise HTTPException(status_code=409, detail=FILES_BEING_READ_MESSAGE)
            self._held.update(ids)
        try:
            yield
        finally:
            with self._lock:
                self._held.subtract(ids)
                self._held += Counter()  # drop the ids that reached zero

    @contextmanager
    def hold_for_reading(self, collection_ids: Iterable[int]) -> Iterator[None]:
        """Hold collections while their files are read by the paths their rows name.

        The other side of hold_for_file_changes, for the offsite upload
        (app.core.image_upload): raises FilesChanging when a request holds any
        of the collections for file changes; otherwise, until the block exits,
        hold_for_file_changes refuses them with a 409. The check and the hold
        are taken under the one lock, so a move of the collection's records,
        a rename of its files, a move of it to another project, a re-parent
        of it, a record delete, a current image's delete, and a free or a
        restore either finished before the read began or start after it ends.
        A renumber, a single-record PATCH and add_record_to_project take no
        hold: they rewrite rows and touch no file. Neither does the delete of
        a superseded image, nor a rename of another project whose tree still
        holds files of a record moved into this collection. The upload's
        re-reads catch both, and image_upload._claim catches both before a
        first offsite row is written. Meant for a short span (the upload
        holds it to re-read a row and make a hard link), since a request
        refused here is the operator's to retry.
        """
        ids = set(collection_ids)
        with self._lock:
            if any(self._held[cid] > 0 for cid in ids):
                raise FilesChanging(FILES_CHANGING_MESSAGE)
            self._reading.update(ids)
        try:
            yield
        finally:
            with self._lock:
                self._reading.subtract(ids)
                self._reading += Counter()

    def active_for(self, collection_id: int) -> Optional[ExportJob]:
        with self._lock:
            jid = self._active_by_collection.get(collection_id)
            job = self._jobs.get(jid) if jid else None
            if job and job.state in ("queued", "running"):
                return job
            return None

    def get(self, job_id: str) -> Optional[ExportJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, collection_id: int, work: Callable[[ExportJob], None]) -> ExportJob:
        """Start `work(job)` in a background thread, or return the collection's
        already-running job so a retry never stacks a second export.

        Raises ExportBlockedError when a request holds the collection for file
        changes (see `hold_for_file_changes`)."""
        with self._lock:
            existing_id = self._active_by_collection.get(collection_id)
            existing = self._jobs.get(existing_id) if existing_id else None
            if existing and existing.state in ("queued", "running"):
                return existing
            if self._held[collection_id] > 0:
                raise ExportBlockedError(FILES_CHANGING_MESSAGE)
            job = ExportJob(collection_id)
            self._jobs[job.id] = job
            self._active_by_collection[collection_id] = job.id
        threading.Thread(target=self._run, args=(job, work), daemon=True).start()
        return job

    def _run(self, job: ExportJob, work: Callable[[ExportJob], None]) -> None:
        job.state = "running"
        try:
            work(job)
            if job.state == "running":
                job.state = "done"
        except ExportError as e:
            job.state = "failed"
            job.error = str(e)
            job.detail = e.detail
        except Exception as e:
            job.state = "failed"
            job.error = str(e)
            logger.exception(f"Export job {job.id} for collection {job.collection_id} failed")
        finally:
            job.finished_at = time.time()
            with self._lock:
                if self._active_by_collection.get(job.collection_id) == job.id:
                    del self._active_by_collection[job.collection_id]


class ExportError(Exception):
    """Expected export failure (missing files, checksum mismatch, low disk).

    Carries an optional structured `detail` the status endpoint surfaces to the UI.
    """
    def __init__(self, message: str, detail=None):
        super().__init__(message)
        self.detail = detail


class ExportBlockedError(Exception):
    """An export was asked for while a request is moving the collection's files."""


# Module-level singleton shared across requests in the single backend process
export_jobs = ExportJobRegistry()
