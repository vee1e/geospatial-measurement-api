"""E4: parallel processing behind GEO_WORKER_PROCESSES (default: a 2-process pool).

Every test pins the variable itself, so the file passes with the suite run
with GEO_WORKER_PROCESSES=1 (single inline worker) and with the default.
"""

from __future__ import annotations

import multiprocessing
import multiprocessing.pool
import time
from pathlib import Path

from app import worker as worker_mod
from app.config import Settings
from app.db import Database, now_iso
from app.worker import Processor

from .conftest import POLYGON_KML


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, database_url=tmp_path / "geo.db")


def _seed(db: Database, tmp_path: Path, count: int) -> list[str]:
    """Records that look exactly like what upload_file writes."""
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    payload = POLYGON_KML.encode()
    ids = []
    for index in range(count):
        file_id = f"feed{index:04d}"
        destination = uploads / f"{file_id}.kml"
        destination.write_bytes(payload)
        db.create_file(
            {
                "id": file_id,
                "filename": "survey.kml",
                "format": "KML",
                "size_bytes": len(payload),
                "status": "PENDING",
                "stored_path": str(destination),
                "created_at": now_iso(),
            }
        )
        ids.append(file_id)
    return ids


def _wait_terminal(db: Database, ids: list[str], timeout: float = 60.0) -> list[str]:
    deadline = time.monotonic() + timeout
    statuses: list[str] = []
    while time.monotonic() < deadline:
        statuses = [db.get_file(file_id)["status"] for file_id in ids]
        if all(status in {"COMPLETED", "FAILED"} for status in statuses):
            return statuses
        time.sleep(0.02)
    raise AssertionError(f"records never finished: {statuses}")


def test_processes_one_is_a_single_inline_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("GEO_WORKER_PROCESSES", "1")
    db = Database(tmp_path / "geo.db")
    processed: list[str] = []
    monkeypatch.setattr(
        worker_mod, "process_file",
        lambda file_id, _db, _settings: processed.append(file_id),
    )

    processor = Processor(db, _settings(tmp_path))
    processor.start()
    try:
        assert processor.running
        assert processor._pool is None  # 1 means no pool: work runs inline
        assert processor._thread is not None
        for file_id in ("alpha", "beta", "gamma"):
            processor.enqueue(file_id)
        deadline = time.monotonic() + 10
        while len(processed) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        processor.stop()
    assert sorted(processed) == ["alpha", "beta", "gamma"]


def test_default_opens_a_two_process_pool(monkeypatch, tmp_path):
    monkeypatch.delenv("GEO_WORKER_PROCESSES", raising=False)
    db = Database(tmp_path / "geo.db")
    ids = _seed(db, tmp_path, count=3)
    processor = Processor(db, _settings(tmp_path))
    processor.start()
    try:
        assert processor._pool is not None
        assert processor._pool._processes == 2  # the shipped default
        for file_id in ids:
            processor.enqueue(file_id)
        statuses = _wait_terminal(db, ids)
    finally:
        processor.stop()
    assert statuses == ["COMPLETED"] * 3


def test_dispatcher_submits_each_file_exactly_once(monkeypatch, tmp_path):
    """One queue.get produces one pool submission: no file can run twice."""
    db = Database(tmp_path / "geo.db")

    class RecordingPool:
        def __init__(self) -> None:
            self.submitted: list[str] = []

        def apply_async(self, func, args=(), kwds=None, **kwargs):
            self.submitted.append(args[0])

        def close(self) -> None:
            pass

        def join(self) -> None:
            pass

        def terminate(self) -> None:
            pass

    processor = Processor(db, _settings(tmp_path))
    monkeypatch.setattr(processor, "_open_pool", lambda: None)  # inject, not spawn
    processor.start()
    pool = RecordingPool()
    processor._pool = pool  # queue is empty: nothing can be dispatched before this

    ids = [f"file-{n}" for n in range(6)]
    for file_id in ids:
        processor.enqueue(file_id)
    deadline = time.monotonic() + 10
    while len(pool.submitted) < len(ids) and time.monotonic() < deadline:
        time.sleep(0.01)
    processor.stop()

    assert sorted(pool.submitted) == sorted(ids)
    assert len(pool.submitted) == len(set(pool.submitted))


def test_pool_completes_several_files(monkeypatch, tmp_path):
    monkeypatch.setenv("GEO_WORKER_PROCESSES", "2")

    # Count submissions in the parent: children run in their own processes, so this
    # is the observable side of "submitted once, therefore processed once".
    submitted: list[str] = []
    original = multiprocessing.pool.Pool.apply_async

    def counting(self, func, args=(), kwds=None, **kwargs):
        submitted.append(args[0])
        return original(self, func, args, kwds or {}, **kwargs)

    monkeypatch.setattr(multiprocessing.pool.Pool, "apply_async", counting)

    db = Database(tmp_path / "geo.db")
    ids = _seed(db, tmp_path, count=4)
    processor = Processor(db, _settings(tmp_path))
    processor.start()
    try:
        assert processor._pool is not None  # the variable did open a pool
        for file_id in ids:
            processor.enqueue(file_id)
        statuses = _wait_terminal(db, ids)
    finally:
        processor.stop()

    assert statuses == ["COMPLETED"] * 4
    assert sorted(submitted) == sorted(ids)  # each file submitted exactly once
    for file_id in ids:
        assert db.get_file(file_id)["feature_count"] == 3
