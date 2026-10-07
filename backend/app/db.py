"""SQLite storage.

A thin repository over the standard library's sqlite3. Every thread opens its own
connection (WAL mode lets readers and the background worker overlap), and all values
are bound as parameters.

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
        assignments = "".join(f", {key} = :{key}" for key in fields)
        self.connection.execute(
            f"UPDATE files SET status = :status{assignments} WHERE id = :id",
            {"id": file_id, "status": status, **fields},
        )

    def get_file(self, file_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return dict(row) if row else None

    def save_measurements(self, file_id: str, payload: dict[str, Any]) -> None:
        # allow_nan=False: a NaN or Infinity in the document would produce JSON that
        # strict parsers reject.
        document = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        self.connection.execute(
            "INSERT OR REPLACE INTO measurements (file_id, payload) VALUES (?, ?)",
            (file_id, document),
        )

    def get_measurements_document(self, file_id: str) -> str | None:
        """Raw JSON text, so the endpoint can serve it without parsing it back."""
        row = self.connection.execute(
            "SELECT payload FROM measurements WHERE file_id = ?", (file_id,)
        ).fetchone()
        return row["payload"] if row else None

    def stranded_file_ids(self) -> list[str]:
        """Records left mid-flight by a previous process, for replay on startup."""
        rows = self.connection.execute(
            "SELECT id FROM files WHERE status IN ('PENDING', 'PROCESSING')"
        ).fetchall()
        return [row["id"] for row in rows]

    def expired_records(self, before_iso: str) -> list[dict[str, str]]:
        rows = self.connection.execute(
            "SELECT id, stored_path FROM files WHERE created_at < ?", (before_iso,)
        ).fetchall()
        return [dict(row) for row in rows]

    def delete_file(self, file_id: str) -> None:
        self.connection.execute("DELETE FROM files WHERE id = ?", (file_id,))
