"""End-to-end processing of one uploaded file.

read -> resolve CRS -> choose projection -> measure every feature -> store JSON.

`process_file` never raises. Any failure, including a database error, marks the record
FAILED with a message, so a client polling the file always reaches a terminal state.
A failure inside a single feature only marks that feature.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database, now_iso
from .geo import measure as measure_mod
from .geo.crs import CRSResolutionError, Projector, pick_projected_crs, resolve_source_crs
from .geo.readers import FileRejected, read_layer

log = logging.getLogger("geo.pipeline")


class ProcessingError(Exception):
    """Fatal for this file. The record ends up FAILED."""


def _summarise(results: list[measure_mod.FeatureResult]) -> dict[str, Any]:
    area = sum(r.measurement.value for r in results if r.measurement.unit == "m2")
    length = sum(r.measurement.value for r in results if r.measurement.unit == "m")
    by_type: dict[str, int] = {}
    for result in results:
        by_type[result.geometry_type] = by_type.get(result.geometry_type, 0) + 1
    return {
        "feature_count": len(results),
        "measured": sum(1 for r in results if r.measurement.supported),
        "unsupported": sum(
            1 for r in results if not r.measurement.supported and not r.measurement.error
        ),
        "failed": sum(1 for r in results if r.measurement.error),
        "total_area_m2": round(area, 4),
        "total_area_km2": round(area / 1_000_000, 6),
        "total_length_m": round(length, 4),
        "total_length_km": round(length / 1_000, 6),
        "by_geometry_type": by_type,
    }


def process_file(file_id: str, db: Database, settings: Settings) -> None:
    """Process one record. Raises nothing: errors are recorded on the record itself."""
    try:
        record = db.get_file(file_id)
        if record is None:
            return

        db.set_status(file_id, "PROCESSING")
        payload = _build_payload(record, settings)
        db.save_measurements(file_id, payload)
        db.set_status(
            file_id,
            "COMPLETED",
            crs=payload["source_crs"],
            crs_assumed=int(payload["crs_assumed"]),
            calculation_crs=payload["calculation_crs"],
            feature_count=payload["summary"]["feature_count"],
            error=None,
            completed_at=now_iso(),
        )
    except (FileRejected, CRSResolutionError, ProcessingError) as exc:
        _fail(db, file_id, str(exc))
    except Exception:
        # Message text reaches anonymous callers, so it stays generic; detail goes to
        # the server log.
        log.exception("processing %s failed", file_id)
        _fail(db, file_id, "internal error while processing the file; see server logs")


def _fail(db: Database, file_id: str, message: str) -> None:
    try:
        db.set_status(file_id, "FAILED", error=message, completed_at=now_iso())
    except Exception:
        log.exception("could not record failure for %s", file_id)


def _build_payload(record: dict[str, Any], settings: Settings) -> dict[str, Any]:
    path = Path(record["stored_path"])
    if not path.exists():
        raise ProcessingError("stored file is missing from disk")

    layer = read_layer(path, record["filename"], settings)
    if not layer.features:
        raise ProcessingError("file contains no features")
    if len(layer.features) > settings.max_features:
        raise ProcessingError(
            f"file has {len(layer.features)} features, limit is {settings.max_features}"
        )

    source_crs, assumed, source_label = resolve_source_crs(layer.crs_wkt, settings.assumed_crs)
    # The extent the readers gathered while parsing, four compares per coordinate
    # block. Walking the coordinate arrays a second time here would do the same work
    # again for nothing.
    if layer.bounds is None:
        raise ProcessingError("no feature exposes readable coordinates")

    target = pick_projected_crs(source_crs, layer.bounds)
    projector = Projector(source_crs, target)
    projector.prepare(feature.geometry for feature in layer.features)

    results = [
        measure_mod.measure(feature.geometry, projector, index, feature.properties)
        for index, feature in enumerate(layer.features)
    ]

    return {
        "file_id": record["id"],
        "filename": record["filename"],
        "format": record["format"],
        "source_crs": source_label,
        "crs_assumed": assumed,
        "crs_note": "; ".join(layer.notes) if layer.notes else None,
        "calculation_crs": target.label,
        "projection_strategy": target.strategy,
        "summary": _summarise(results),
        "features": [
            {
                "index": r.index,
                "geometry_type": r.geometry_type,
                "crs": target.label,
                "properties": r.properties,
                "supported": r.measurement.supported,
                "measurement": (
                    {"value": r.measurement.value, "unit": r.measurement.unit}
                    if r.measurement.supported and r.measurement.value is not None
                    else None
                ),
                "reason": r.measurement.reason,
                "error": r.measurement.error,
                "warnings": r.warnings,
            }
            for r in results
        ],
    }
