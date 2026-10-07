"""Upload and retrieval endpoints."""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from ..config import settings
from ..db import Database, now_iso
from ..geo.readers import FileRejected, detect_format, has_valid_magic
from ..worker import Processor

log = logging.getLogger("geo.api")

router = APIRouter(prefix="/api", tags=["files"])

MAX_FILENAME_LENGTH = 200
CHUNK_BYTES = 1024 * 1024


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_processor(request: Request) -> Processor:
    return request.app.state.processor


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
def health(request: Request) -> dict[str, str]:
    """Liveness for the container health check: 503 while the worker is down.

    A dead worker would otherwise accept uploads forever while every file stayed
    PENDING, and the container would keep reporting healthy.
    """
    processor: Processor = request.app.state.processor
    if not processor.running:
        raise HTTPException(status_code=503, detail="processing worker is not running")
    return {"status": "ok"}


async def _save_in_chunks(file: UploadFile, destination: Path) -> int:
    """Copy the spooled upload to disk in chunks, so the whole file is never in RAM."""
    written = 0
    with destination.open("wb") as handle:
        while chunk := await file.read(CHUNK_BYTES):
            written += len(chunk)
            await run_in_threadpool(handle.write, chunk)
    return written


@router.post("/files/", status_code=202)
async def upload_file(
    file: UploadFile,
    db: Database = Depends(get_db),
    processor: Processor = Depends(get_processor),
) -> dict[str, Any]:
    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="multipart field 'file' must have a filename")
    if len(filename) > MAX_FILENAME_LENGTH:
        raise HTTPException(
            status_code=413, detail=f"filename longer than {MAX_FILENAME_LENGTH} characters"
        )

    try:
        detect_format(filename)
    except FileRejected as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    head = await file.read(512)
    await file.seek(0)
    if not head:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    if not has_valid_magic(filename, head):
        raise HTTPException(
            status_code=415,
            detail=f"'{filename}' does not look like a {filename.rsplit('.', 1)[-1]} file",
        )

    file_id = secrets.token_hex(6)
    suffix = Path(filename).suffix.lower()
    destination = settings.data_dir / "uploads" / f"{file_id}{suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = await _save_in_chunks(file, destination)

    if written > settings.max_upload_bytes:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=413,
            detail=f"file is {written} bytes, limit is {settings.max_upload_bytes}",
        )

    record = {
        "id": file_id,
        "filename": Path(filename).name,
        "format": detect_format(filename),
        "size_bytes": written,
        "status": "PENDING",
        "stored_path": str(destination),
        "created_at": now_iso(),
    }
    await run_in_threadpool(db.create_file, record)
    processor.enqueue(file_id)

    return info_record(await run_in_threadpool(db.get_file, file_id))


@router.get("/files/{file_id}/")
async def get_file(file_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    record = await run_in_threadpool(db.get_file, file_id)
    if record is None:
        raise _not_found(file_id)
    return info_record(record)


@router.get("/files/{file_id}/measurements/")
async def get_measurements(file_id: str, db: Database = Depends(get_db)) -> Response:
    record = await run_in_threadpool(db.get_file, file_id)
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

    document = await run_in_threadpool(db.get_measurements_document, file_id)
    if document is None:
        raise HTTPException(status_code=409, detail="measurements are not stored yet")
    # Served as stored bytes: no parse and re-serialise of a document that can reach
    # tens of megabytes on every request.
    return Response(content=document, media_type="application/json")
