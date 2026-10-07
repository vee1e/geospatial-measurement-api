"""Background processing queue.

Uploads return immediately with status PENDING; a worker thread drains a queue and
moves each file to PROCESSING then COMPLETED or FAILED. The choice over handling the
work inside the POST request is documented in the README: parsing and reprojecting a
large layer takes seconds, and holding the request open for that couples client
timeouts to file size.

One thread is enough here because the work is CPU-bound in a single process; scaling
past that means more processes, each with its own queue, which is a change the code
does not lock us into (the queue is behind `Processor`).
"""

from __future__ import annotations

import logging
import queue
import threading

from .config import Settings
from .db import Database
from .pipeline import process_file

log = logging.getLogger("geo.worker")


class Processor:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="geo-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def enqueue(self, file_id: str) -> None:
        self._queue.put(file_id)

    def drain(self) -> None:
        """Process everything queued so far. Used by tests to avoid timing sleeps."""
        while True:
            try:
                file_id = self._queue.get_nowait()
            except queue.Empty:
                return
            if file_id is None:
                continue
            self._handle(file_id)

    def _handle(self, file_id: str) -> None:
        try:
            process_file(file_id, self._db, self._settings)
        except Exception:  # process_file already traps, this is a last line of defence
            log.exception("worker crashed on %s", file_id)

    def _run(self) -> None:
        while not self._stopping.is_set():
            file_id = self._queue.get()
            if file_id is None:
                break
            self._handle(file_id)
