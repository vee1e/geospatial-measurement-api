"""End-to-end processing of one uploaded file.

read -> resolve CRS -> choose projection -> measure every feature -> store JSON.

Any failure along the way marks the file FAILED with a human-readable error; a failure
inside a single feature only marks that feature.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shapely.geometry import shape as shapely_shape

from .config import Settings
from .db import Database, now_iso
from .geo import measure as measure_mod
from .geo.crs import CRSResolutionError, Projector, pick_projected_crs, resolve_source_crs
from .geo.readers import FileRejected, read_layer


class ProcessingError(Exception):
    """Fatal for this file. The record ends up FAILED."""


def _layer_bounds(features) -> tuple[float, float, float, float] | None:
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    found = False
    for feature in features:
        if not feature.geometry:
            continue
        try:
            bx0, by0, bx1, by1 = shapely_shape(feature.geometry).bounds
        except Exception:
            continue
        if bx0 is None or bx1 is None:
            continue
        found = True
        minx, miny = min(minx, bx0), min(miny, by0)
        maxx, maxy = max(maxx, bx1), max(maxy, by1)
    return (minx, miny, maxx, maxy) if found else None


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
    record = db.get_file(file_id)
    if record is None:
        return

    db.set_status(file_id, "PROCESSING")
    try:
        payload = _build_payload(record, settings)
    except (FileRejected, CRSResolutionError, ProcessingError) as exc:
        db.set_status(file_id, "FAILED", error=str(exc), completed_at=now_iso())
        return
    except Exception as exc:  # unexpected bugs still must not take the worker down
        db.set_status(file_id, "FAILED", error=f"unexpected error: {exc}", completed_at=now_iso())
        return

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


def _build_payload(record: dict[str, Any], settings: Settings) -> dict[str, Any]:
    path = Path(record["stored_path"])
    if not path.exists():
        raise ProcessingError("stored file is missing from disk")

    layer = read_layer(path, record["filename"], settings)
    if not layer.features:
        raise ProcessingError("file contains no features")

    source_crs, assumed, source_label = resolve_source_crs(layer.crs_wkt, settings.assumed_crs)
    bounds = _layer_bounds(layer.features)
    if bounds is None:
        raise ProcessingError("no feature exposes readable coordinates")

    target = pick_projected_crs(source_crs, bounds)
    projector = Projector(source_crs, target)

    results = [
        measure_mod.measure(feature.geometry, projector, index, feature.properties)
        for index, feature in enumerate(layer.features)
    ]

    crs_block = {
        "source_crs": source_label,
        "crs_assumed": assumed,
        "crs_note": "; ".join(layer.notes) if layer.notes else None,
        "calculation_crs": target.label,
        "projection_strategy": target.strategy,
    }
    return {
        "file_id": record["id"],
        "filename": record["filename"],
        "format": record["format"],
        **crs_block,
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
