"""SQLite storage.

A thin repository over the standard library's sqlite3. Every process opens its own
connection per thread (WAL mode lets the reader and the background worker overlap),
and all statements are parameterised.

Features and measurements are stored as one JSON document per file rather than one row
per feature: the API only ever serves them together with the file they came from, and a
single document keeps the read path to one query.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id              TEXT PRIMARY KEY,
    filename        TEXT NOT NULL,
    format          TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    status          TEXT NOT NULL,
    crs             TEXT,
    crs_assumed     INTEGER NOT NULL DEFAULT 0,
    calculation_crs TEXT,
    feature_count   INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    stored_path     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    completed_at    TEXT
);

CREATE TABLE IF NOT EXISTS measurements (
    file_id TEXT PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    payload TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.connection.executescript(SCHEMA)

    @property
    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def create_file(self, record: dict[str, Any]) -> None:
        self.connection.execute(
            """INSERT INTO files (id, filename, format, size_bytes, status, stored_path, created_at)
               VALUES (:id, :filename, :format, :size_bytes, :status, :stored_path, :created_at)""",
            record,
        )

    def set_status(self, file_id: str, status: str, **fields: Any) -> None:
        allowed = {
            "crs",
            "crs_assumed",
            "calculation_crs",
            "feature_count",
            "error",
            "completed_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unexpected columns: {sorted(unknown)}")
        assignments = ", ".join(f"{key} = :{key}" for key in fields)
        sql = (
            "UPDATE files SET status = :status"
            + (f", {assignments}" if assignments else "")
            + " WHERE id = :id"
        )
        self.connection.execute(sql, {"id": file_id, "status": status, **fields})

    def get_file(self, file_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return dict(row) if row else None

    def list_files(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM files ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def save_measurements(self, file_id: str, payload: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO measurements (file_id, payload) VALUES (?, ?)",
            (file_id, json.dumps(payload, separators=(",", ":"))),
        )

    def get_measurements(self, file_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload FROM measurements WHERE file_id = ?", (file_id,)
        ).fetchone()
        return json.loads(row["payload"]) if row else None
