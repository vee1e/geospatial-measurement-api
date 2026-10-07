"""Runtime settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("GEO_DATA_DIR", "./data")))
    database_url: Path = field(
        default_factory=lambda: Path(os.getenv("GEO_DATABASE", "./data/geo.db"))
    )

    max_upload_bytes: int = int(os.getenv("GEO_MAX_UPLOAD_BYTES", 25 * 1024 * 1024))
    max_zip_entries: int = int(os.getenv("GEO_MAX_ZIP_ENTRIES", 64))
    max_zip_uncompressed_bytes: int = int(os.getenv("GEO_MAX_ZIP_BYTES", 200 * 1024 * 1024))

    # KML is defined to use WGS 84, and a shapefile with no .prj sidecar has no CRS at
    # all, so we have to assume one. Overridable for files that are known to be else.
    assumed_crs: str = os.getenv("GEO_ASSUMED_CRS", "EPSG:4326")

    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: _csv(os.getenv("GEO_CORS_ORIGINS"))
    )

    def prepared(self) -> Settings:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_url.parent.mkdir(parents=True, exist_ok=True)
        return self


settings = Settings()
