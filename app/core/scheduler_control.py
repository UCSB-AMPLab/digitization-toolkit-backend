"""Stopping a scheduler's worker and knowing when it has really exited.

The three in-process schedulers (catalogue_backup, image_upload,
fisqua_outbox's status poll) each run one daemon worker thread. Their
stop() sets the stop event and joins for a few seconds; a worker still
alive after that is kept, never forgotten, so a later start() cannot launch
a second worker beside it and worker_alive() never reports a live thread
as gone.

StoppableWorker adds what pairing needs, for a class with `_stop` (and,
for the image upload, `_wake`) events and a `_thread`:

* request_stop() sets the events and keeps the thread reference;
* wait_stopped(timeout) joins for at most `timeout` and answers whether no
  worker is alive, still keeping the reference while one is;
* worker_alive().

Each scheduler's start() returns early while its worker is alive, so
with the reference kept no start() can add a second worker.

Version: 0.3.3
"""

import threading
from typing import Optional


class StoppableWorker:
    _stop: threading.Event
    _thread: Optional[threading.Thread]

    def request_stop(self) -> None:
        """Ask the worker to stop after what it is doing; never forgets it."""
        self._stop.set()
        wake = getattr(self, "_wake", None)
        if wake is not None:
            wake.set()

    def worker_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def wait_stopped(self, timeout: float) -> bool:
        """Wait at most `timeout` seconds for the worker to exit. True when none is alive."""
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        return not self.worker_alive()
