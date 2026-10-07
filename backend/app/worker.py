"""Background processing queue.

Uploads return immediately with status PENDING; a dispatcher thread drains a queue and
moves each file to PROCESSING then COMPLETED or FAILED. The choice over handling the
work inside the POST request is documented in the README: parsing and reprojecting a
large layer takes seconds, and holding the request open for that couples client
timeouts to file size.

The pipeline is CPU-bound in Python code, so threads inside one process do not help
(measured: 8 files in 4 threads took 0.69 s against 0.44 s in one thread). The work
therefore runs in a pool of separate processes: the dispatcher thread drains the
queue exactly as before and submits every file to the pool once, while the work
overlaps across cores (measured: 8 files in a 4-process pool took 0.145 s against
0.532 s in one). GEO_WORKER_PROCESSES sets the pool size; the default is 2, and 1
turns it off, running each file inline on the dispatcher thread.
"""

from __future__ import annotations

import logging
import multiprocessing
import multiprocessing.pool
import os
import queue
import threading

from .config import Settings
from .db import Database
from .pipeline import process_file

log = logging.getLogger("geo.worker")

# Set in the pool's child processes by the initializer. Each child opens its own
# database connection: sqlite connections must never be used from a process they
# were not created in.
_child_db: Database | None = None
_child_settings: Settings | None = None


def _pool_size() -> int:
    """GEO_WORKER_PROCESSES, defaulting to 2: two files processed at a time.

    Set to 1 for the single inline worker (processing on the dispatcher thread,
    no pool), which is what this service ran before the pool existed.
    """
    raw = os.getenv("GEO_WORKER_PROCESSES", "2").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        log.warning("GEO_WORKER_PROCESSES=%r is not a number; using 2 workers", raw)
        return 2


def _child_init(settings: Settings) -> None:
    global _child_db, _child_settings
    _child_settings = settings
    _child_db = Database(settings.database_url)


def _child_handle(file_id: str) -> None:
    try:
        process_file(file_id, _child_db, _child_settings)  # type: ignore[arg-type]
    except Exception:  # process_file already traps, this is a last line of defence
        log.exception("pool worker crashed on %s", file_id)


class Processor:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._pool: multiprocessing.pool.Pool | None = None
        self._stopping = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stopping.clear()
        self._open_pool()
        self._thread = threading.Thread(target=self._run, name="geo-worker", daemon=True)
        self._thread.start()

    def _open_pool(self) -> None:
        """Create the process pool, unless GEO_WORKER_PROCESSES is 1 (inline worker)."""
        size = _pool_size()
        if size <= 1:
            self._pool = None
            return
        try:
            self._pool = multiprocessing.get_context().Pool(
                processes=size, initializer=_child_init, initargs=(self._settings,)
            )
        except Exception:
            log.exception("could not start %d pool workers; processing inline", size)
            self._pool = None
        else:
            log.info("processing with %d pool workers", size)

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        pool, self._pool = self._pool, None
        if pool is not None:
            # Files already handed to the pool finish; anything left after the
            # timeout is terminated here and re-queued by recover_stranded on the
            # next startup, which is what an abrupt exit did before.
            pool.close()
            joiner = threading.Thread(target=pool.join, name="geo-pool-join", daemon=True)
            joiner.start()
            joiner.join(timeout=timeout)
            if joiner.is_alive():
                pool.terminate()
                joiner.join(timeout=1.0)

    def enqueue(self, file_id: str) -> None:
        self._queue.put(file_id)

    def _handle(self, file_id: str) -> None:
        if self._pool is not None:
            # One queue.get produced exactly one submission: no file runs twice.
            self._pool.apply_async(
                _child_handle,
                (file_id,),
                error_callback=lambda error: log.error(
                    "pool task failed on %s: %s", file_id, error
                ),
            )
            return
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
