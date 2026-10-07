"""Application entry point: wiring, CORS, and the worker lifecycle."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.files import router as files_router
from .config import settings
from .db import Database
from .worker import Processor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = settings.prepared()
    app.state.settings = config
    app.state.db = Database(config.database_url)
    app.state.processor = Processor(app.state.db, config)
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
    logging.getLogger("geo.error").exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


app.include_router(files_router)
