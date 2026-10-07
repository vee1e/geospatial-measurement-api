"""Runtime settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("GEO_DATA_DIR", "./data")))
    database_url: Path = field(
        default_factory=lambda: Path(os.getenv("GEO_DATABASE", "./data/geo.db"))
    )

    max_upload_bytes: int = int(os.getenv("GEO_MAX_UPLOAD_BYTES", 25 * 1024 * 1024))
    max_zip_entries: int = int(os.getenv("GEO_MAX_ZIP_ENTRIES", 64))
    # Peak memory is roughly 4x the expanded archive size once geometries exist twice,
    # so this stays well under the container's memory limit.
    max_zip_uncompressed_bytes: int = int(os.getenv("GEO_MAX_ZIP_BYTES", 64 * 1024 * 1024))
    max_features: int = int(os.getenv("GEO_MAX_FEATURES", "50000"))

    # Uploaded files are removed on startup once they are older than this. Keeps the
    # bind mount from growing without bound, since nothing else deletes anything.
    retention_days: int = int(os.getenv("GEO_RETENTION_DAYS", "7"))

    # A shapefile with no .prj sidecar has no CRS at all. KML is not affected: it is
    # always WGS 84 by specification, and readers declare that explicitly.
    assumed_crs: str = os.getenv("GEO_ASSUMED_CRS", "EPSG:4326")

    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            origin.strip()
            for origin in os.getenv("GEO_CORS_ORIGINS", "").split(",")
            if origin.strip()
        )
    )

    def prepared(self) -> Settings:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_url.parent.mkdir(parents=True, exist_ok=True)
        return self


settings = Settings()
