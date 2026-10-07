"""Measurement rules: what gets measured, in what unit, and how failures are isolated.

Rules, straight from the assignment:

- Polygon -> area (square metres)
- LineString -> length (metres)
- Point -> nothing to measure, reported as such instead of failing
- anything else -> unsupported, reported as such instead of crashing

Every feature is measured inside its own try block. One broken geometry produces one
error entry and leaves the rest of the file intact, which is the same isolation rule the
certificate-generator brief asks for on its per-recipient failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import shape as shapely_shape

AREA_TYPES = {"Polygon", "MultiPolygon"}
LENGTH_TYPES = {"LineString", "MultiLineString", "LinearRing"}
POINT_TYPES = {"Point", "MultiPoint"}
NO_MEASUREMENT_REASON = "points have no measurable area or length"
UNSUPPORTED_REASON = "geometry type is not measurable"


@dataclass
class Measurement:
    supported: bool
    value: float | None = None
    unit: str | None = None
    reason: str | None = None
    error: str | None = None


@dataclass
class FeatureResult:
    index: int
    geometry_type: str
    properties: dict[str, Any]
    measurement: Measurement
    warnings: list[str] = field(default_factory=list)


def measure(
    geometry_mapping: dict | None, projector, index: int, properties: dict
) -> FeatureResult:
    """Measure one feature. Never raises: every outcome comes back as a result."""
    def result(
        geometry_type: str,
        *,
        supported: bool = False,
        value: float | None = None,
        unit: str | None = None,
        reason: str | None = None,
        error: str | None = None,
        warnings: list[str] | None = None,
    ) -> FeatureResult:
        return FeatureResult(
            index=index,
            geometry_type=geometry_type,
            properties=properties,
            measurement=Measurement(supported, value, unit, reason, error),
            warnings=warnings or [],
        )

    if not geometry_mapping:
        return result("Null", reason="feature has no geometry")

    geometry_type = geometry_mapping.get("type") or "Unknown"

    try:
        geometry = shapely_shape(geometry_mapping)
    except Exception as exc:
        return result(geometry_type, error=f"could not read geometry: {exc}")

    if geometry.is_empty:
        return result(geometry_type, reason="geometry is empty")

    if geometry_type in POINT_TYPES:
        return result(geometry_type, reason=NO_MEASUREMENT_REASON)

    if geometry_type not in AREA_TYPES | LENGTH_TYPES:
        return result(geometry_type, reason=UNSUPPORTED_REASON)

    unit = "m2" if geometry_type in AREA_TYPES else "m"
    try:
        projected = projector.project(geometry)
    except Exception as exc:
        return result(geometry_type, error=f"reprojection failed: {exc}")

    # A ring that crosses itself still measures in shapely, but the number deserves a
    # caveat, so the caller can see it is approximate.
    warnings: list[str] = []
    if not projected.is_valid:
        warnings.append("geometry is invalid (self-intersecting ring); value is approximate")

    try:
        raw = projected.area if geometry_type in AREA_TYPES else projected.length
        scale = projector.area_scale if geometry_type in AREA_TYPES else projector.length_scale
        value = raw * scale
    except Exception as exc:
        return result(geometry_type, error=f"measurement failed: {exc}")

    return result(
        geometry_type,
        supported=True,
        value=round(float(value), 4),
        unit=unit,
        warnings=warnings,
    )
