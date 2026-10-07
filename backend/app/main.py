"""Application entry point: wiring, CORS, body limits, and worker lifecycle."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .api.files import router as files_router
from .config import Settings, settings
from .db import Database
from .worker import Processor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("geo.app")


def recover_stranded(db: Database, processor: Processor) -> int:
    """Re-queue rows a previous process left mid-flight.

    Without this a restart during processing strands the record in PROCESSING and the
    client polls 409 forever.
    """
    ids = db.stranded_file_ids()
    for file_id in ids:
        db.set_status(file_id, "PENDING")
        processor.enqueue(file_id)
    return len(ids)


def cleanup_expired(db: Database, config: Settings) -> int:
    """Delete uploads older than the retention window, rows included."""
    cutoff = datetime.now(UTC) - timedelta(days=config.retention_days)
    removed = 0
    for record in db.expired_records(cutoff.isoformat(timespec="seconds")):
        Path(record["stored_path"]).unlink(missing_ok=True)
        db.delete_file(record["id"])
        removed += 1
    return removed


class BodySizeLimit(BaseHTTPMiddleware):
    """Reject oversized requests before the multipart parser buffers the body.

    The handler cannot do this: by the time FastAPI calls it, Starlette has already
    received the whole body. Caddy applies the same limit at the edge, so this is the
    in-process defence for direct access.
    """

    async def dispatch(self, request: Request, call_next):
        declared = request.headers.get("content-length")
        limit = settings.max_upload_bytes
        if declared and declared.isdigit() and int(declared) > limit:
            return JSONResponse(
                status_code=413,
                content={"detail": f"request body is {declared} bytes, limit is {limit}"},
            )
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = settings.prepared()
    app.state.db = Database(config.database_url)
    app.state.processor = Processor(app.state.db, config)

    stranded = recover_stranded(app.state.db, app.state.processor)
    if stranded:
        log.info("re-queued %d file(s) left behind by a previous run", stranded)
    expired = cleanup_expired(app.state.db, config)
    if expired:
        log.info("removed %d expired upload(s)", expired)

    app.state.processor.start()
    try:
        yield
    finally:
        app.state.processor.stop()


app = FastAPI(
    title="Geospatial File Measurement API",
    description=(
        "Upload a Shapefile (zip) or KML, then read per-feature measurements. "
        "Areas are computed in square metres and lengths in metres after the geometry "
        "is transformed to a projected coordinate system."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# Registered before CORS so an oversized body is refused without parsing it.
app.add_middleware(BodySizeLimit)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Clients get JSON on every failure, not the plain-text default."""
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


app.include_router(files_router)
