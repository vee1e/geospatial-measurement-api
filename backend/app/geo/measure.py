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
    geometry_type_raw: str | None = None
    warnings: list[str] = field(default_factory=list)


def measure(
    geometry_mapping: dict | None, projector, index: int, properties: dict
) -> FeatureResult:
    """Measure one feature. Never raises: every outcome comes back as a result."""
    if not geometry_mapping:
        return FeatureResult(
            index=index,
            geometry_type="Null",
            properties=properties,
            measurement=Measurement(supported=False, reason="feature has no geometry"),
        )

    geometry_type = geometry_mapping.get("type") or "Unknown"

    try:
        geometry = shapely_shape(geometry_mapping)
    except Exception as exc:
        return FeatureResult(
            index=index,
            geometry_type=geometry_type,
            properties=properties,
            measurement=Measurement(supported=False, error=f"could not read geometry: {exc}"),
        )

    if geometry.is_empty:
        return FeatureResult(
            index=index,
            geometry_type=geometry_type,
            properties=properties,
            measurement=Measurement(supported=False, reason="geometry is empty"),
        )

    if geometry_type in POINT_TYPES:
        return FeatureResult(
            index=index,
            geometry_type=geometry_type,
            properties=properties,
            measurement=Measurement(supported=False, reason=NO_MEASUREMENT_REASON),
        )

    if geometry_type not in AREA_TYPES | LENGTH_TYPES:
        return FeatureResult(
            index=index,
            geometry_type=geometry_type,
            properties=properties,
            measurement=Measurement(supported=False, reason=UNSUPPORTED_REASON),
        )

    unit = "m2" if geometry_type in AREA_TYPES else "m"
    try:
        projected = projector.project(geometry)
    except Exception as exc:
        return FeatureResult(
            index=index,
            geometry_type=geometry_type,
            properties=properties,
            measurement=Measurement(supported=False, error=f"reprojection failed: {exc}"),
        )

    # A projected geometry that lost its validity to a bad ring still measures with
    # shapely, but flag it so the caller can treat the number with suspicion.
    warnings: list[str] = []
    if not projected.is_valid:
        warnings.append("geometry is invalid (self-intersecting ring); value is approximate")

    try:
        value = projected.area if geometry_type in AREA_TYPES else projected.length
    except Exception as exc:
        return FeatureResult(
            index=index,
            geometry_type=geometry_type,
            properties=properties,
            measurement=Measurement(supported=False, error=f"measurement failed: {exc}"),
        )

    return FeatureResult(
        index=index,
        geometry_type=geometry_type,
        properties=properties,
        measurement=Measurement(supported=True, value=round(float(value), 4), unit=unit),
        warnings=warnings,
    )
