"""In-process registry of background export jobs for the single-process appliance"""

import logging
import threading
import time
import uuid
from typing import Callable, Optional

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


class ExportJobRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[str, ExportJob] = {}
        self._active_by_collection: dict[int, str] = {}

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
        already-running job so a retry never stacks a second export."""
        with self._lock:
            existing_id = self._active_by_collection.get(collection_id)
            existing = self._jobs.get(existing_id) if existing_id else None
            if existing and existing.state in ("queued", "running"):
                return existing
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


# Module-level singleton shared across requests in the single backend process
export_jobs = ExportJobRegistry()
