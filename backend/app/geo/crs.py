"""Coordinate reference system (CRS) handling.

Two rules drive this module:

1. Never measure in latitude/longitude degrees. Degrees are angles, so area would come
   out in square degrees and length in degrees, neither of which is a distance.
2. Pick one projected CRS per file, chosen from the file's extent, so that every feature
   in a layer is measured in the same projection and the numbers stay comparable.

Strategy (documented in the README as a design decision):

- Source is already projected: measure in it, converting to metres when its axes are
  not in metres (US State Plane feet, for example).
- Source is geographic and the extent fits one UTM zone: use that zone.
- Anything wider (or polar): Lambert Azimuthal Equal Area centred on the extent,
  using a circular mean of longitudes so extents crossing the antimeridian do not end
  up centred on the far side of the planet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from pyproj import CRS, Transformer
from shapely.ops import transform as shapely_transform

# Latitude range where the UTM system is defined.
_UTM_MIN_LAT = -80.0
_UTM_MAX_LAT = 84.0


class CRSResolutionError(Exception):
    """Raised when a source CRS cannot be understood."""


@dataclass(frozen=True)
class ProjectedCRS:
    crs: CRS
    label: str
    strategy: str  # "source" | "utm" | "laea"
    unit_to_metre: float = 1.0  # metres per axis unit of the projected CRS


def crs_label(crs: CRS) -> str:
    epsg = crs.to_epsg()
    if epsg:
        return f"EPSG:{epsg}"
    return crs.name or "unknown"


def resolve_source_crs(prj_text: str | None, assumed_crs: str) -> tuple[CRS, bool, str]:
    """Return (crs, was_assumed, label).

    A shapefile carries its CRS in a .prj sidecar (WKT text). When it is missing we
    assume the configured CRS and flag that in the API response rather than guessing
    silently.
    """
    if not prj_text or not prj_text.strip():
        crs = CRS.from_user_input(assumed_crs)
        # The label stays a plain "EPSG:xxxx"; the assumption is reported as its own
        # boolean field so clients can tell a declared CRS from a guessed one.
        return crs, True, crs_label(crs)
    try:
        crs = CRS.from_wkt(prj_text.strip())
    except Exception as exc:  # pyproj raises a range of errors for malformed WKT
        raise CRSResolutionError(f"could not parse .prj: {exc}") from exc
    return crs, False, crs_label(crs)


def _utm_epsg(lon: float, lat: float) -> int:
    zone = int((lon + 180.0) // 6.0) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


def _circular_mean_longitude(minx: float, maxx: float) -> float:
    """Mean of two longitudes on a circle, so 179 and -179 average to 180, not 0."""
    start, end = math.radians(minx), math.radians(maxx)
    x = math.cos(start) + math.cos(end)
    y = math.sin(start) + math.sin(end)
    if x == 0 and y == 0:  # exactly opposite points: any centre will do
        return minx
    return math.degrees(math.atan2(y, x))


def _same_utm_zone(minx: float, maxx: float) -> bool:
    return int((minx + 180.0) // 6.0) == int((maxx + 180.0) // 6.0)


def pick_projected_crs(source: CRS, bounds: tuple[float, float, float, float]) -> ProjectedCRS:
    """Choose the projection used for every measurement in one file."""
    if source.is_projected:
        factor = 1.0
        if source.axis_info:
            # metres per axis unit: 1.0 for metre projections, ~0.3048 for feet
            factor = source.axis_info[0].unit_conversion_factor or 1.0
        return ProjectedCRS(source, crs_label(source), "source", unit_to_metre=factor)

    minx, miny, maxx, maxy = bounds
    lat = (miny + maxy) / 2.0
    crosses_antimeridian = (maxx - minx) > 180.0
    lon = _circular_mean_longitude(minx, maxx) if crosses_antimeridian else (minx + maxx) / 2.0

    fits_one_zone = not crosses_antimeridian and _same_utm_zone(minx, maxx)
    in_utm_band = (
        _UTM_MIN_LAT <= lat <= _UTM_MAX_LAT and _UTM_MIN_LAT <= miny and maxy <= _UTM_MAX_LAT
    )

    if fits_one_zone and in_utm_band:
        epsg = _utm_epsg(lon, lat)
        return ProjectedCRS(CRS.from_epsg(epsg), f"EPSG:{epsg}", "utm")

    proj4 = (
        f"+proj=laea +lat_0={lat:.6f} +lon_0={lon:.6f} +x_0=0 +y_0=0 "
        "+datum=WGS84 +units=m +no_defs"
    )
    return ProjectedCRS(CRS.from_proj4(proj4), "LAEA (centred on extent)", "laea")


class Projector:
    """Transforms geometries from the source CRS into the measurement CRS."""

    def __init__(self, source: CRS, target: ProjectedCRS) -> None:
        self.source = source
        self.target = target
        self._transformer = Transformer.from_crs(source, target.crs, always_xy=True)

    @property
    def identical(self) -> bool:
        return self.source == self.target.crs

    @property
    def length_scale(self) -> float:
        """Metres per projected axis unit."""
        return self.target.unit_to_metre

    @property
    def area_scale(self) -> float:
        return self.target.unit_to_metre**2

    def project(self, geometry):
        if self.identical:
            return geometry
        return shapely_transform(lambda x, y, z=None: self._transformer.transform(x, y), geometry)
