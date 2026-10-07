"""Upload and retrieval endpoints."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool

from ..config import Settings
from ..config import settings as default_settings
from ..db import Database, now_iso
from ..geo.readers import FileRejected, detect_format
from ..worker import Processor

router = APIRouter(prefix="/api", tags=["files"])

MAX_FILENAME_LENGTH = 200


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_processor(request: Request) -> Processor:
    return request.app.state.processor


def get_settings() -> Settings:
    return default_settings


def info_record(record: dict[str, Any]) -> dict[str, Any]:
    """Public shape of a file record. Columns stay internal to the repository."""
    return {
        "id": record["id"],
        "filename": record["filename"],
        "format": record["format"],
        "size_bytes": record["size_bytes"],
        "feature_count": record["feature_count"],
        "crs": record["crs"],
        "crs_assumed": bool(record["crs_assumed"]),
        "calculation_crs": record["calculation_crs"],
        "status": record["status"],
        "error": record["error"],
        "created_at": record["created_at"],
        "completed_at": record["completed_at"],
    }


def _not_found(file_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"no file with id '{file_id}'")


@router.get("/health/")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/files/", status_code=202)
async def upload_file(
    request: Request,
    file: UploadFile,
    db: Database = Depends(get_db),
    processor: Processor = Depends(get_processor),
    config: Settings = Depends(get_settings),
) -> dict[str, Any]:
    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="multipart field 'file' must have a filename")
    if len(filename) > MAX_FILENAME_LENGTH:
        raise HTTPException(
            status_code=413, detail=f"filename longer than {MAX_FILENAME_LENGTH} characters"
        )

    try:
        source_format = detect_format(filename)
    except FileRejected as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    if len(payload) > config.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"file is {len(payload)} bytes, limit is {config.max_upload_bytes}",
        )

    file_id = secrets.token_hex(6)
    suffix = Path(filename).suffix.lower()
    destination = config.data_dir / "uploads" / f"{file_id}{suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    await run_in_threadpool(destination.write_bytes, payload)

    record = {
        "id": file_id,
        "filename": Path(filename).name,
        "format": source_format,
        "size_bytes": len(payload),
        "status": "PENDING",
        "stored_path": str(destination),
        "created_at": now_iso(),
    }
    db.create_file(record)
    processor.enqueue(file_id)

    return info_record(db.get_file(file_id))


@router.get("/files/")
def list_files(db: Database = Depends(get_db), limit: int = 50) -> dict[str, Any]:
    rows = db.list_files(limit=min(max(limit, 1), 200))
    return {"count": len(rows), "files": [info_record(row) for row in rows]}


@router.get("/files/{file_id}/")
def get_file(file_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    record = db.get_file(file_id)
    if record is None:
        raise _not_found(file_id)
    return info_record(record)


@router.get("/files/{file_id}/measurements/")
def get_measurements(file_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    record = db.get_file(file_id)
    if record is None:
        raise _not_found(file_id)

    if record["status"] == "FAILED":
        raise HTTPException(
            status_code=422,
            detail={"message": "file could not be processed", "error": record["error"]},
        )
    if record["status"] != "COMPLETED":
        raise HTTPException(
            status_code=409,
            detail=f"file is {record['status']}; retry after it reaches COMPLETED",
        )

    payload = db.get_measurements(file_id)
    if payload is None:
        raise HTTPException(status_code=409, detail="measurements are not stored yet")
    return payload
