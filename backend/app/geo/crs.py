"""Coordinate reference system (CRS) handling.

Two rules drive this module:

1. Never measure in latitude/longitude degrees. Degrees are angles, so area would come
   out in square degrees and length in degrees, neither of which is a distance.
2. Pick one projected CRS per file, chosen from the file's extent, so that every feature
   in a layer is measured in the same projection and the numbers stay comparable.

Strategy (documented in the README as a design decision):

- Source is already projected (units are metres): measure in the source CRS.
- Source is geographic and the extent fits in one UTM zone: use that zone. UTM keeps
  distance and shape error under ~0.1% inside a zone.
- Anything wider (or polar): use Lambert Azimuthal Equal Area centred on the extent.
  Equal-area projection, so area stays correct at any extent; lengths become an
  approximation, which the API reports as the basis of the measurement.
"""

from __future__ import annotations

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


def crs_label(crs: CRS) -> str:
    """Short human-readable name: 'EPSG:32643' when an EPSG code exists, else authority."""
    epsg = crs.to_epsg()
    if epsg:
        return f"EPSG:{epsg}"
    name = crs.name or "unknown"
    return name


def resolve_source_crs(prj_text: str | None, assumed_crs: str) -> tuple[CRS, bool, str]:
    """Return (crs, was_assumed, label).

    A shapefile carries its CRS in a .prj sidecar (WKT text). KML always uses WGS 84.
    When neither is available we assume the configured CRS and flag it in the API
    response rather than silently guessing.
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


def _extent_width_degrees(minx: float, miny: float, maxx: float, maxy: float) -> float:
    return abs(maxx - minx)


def pick_projected_crs(source: CRS, bounds: tuple[float, float, float, float]) -> ProjectedCRS:
    """Choose the projection used for every measurement in one file."""
    if source.is_projected:
        units = {axis.unit_name for axis in source.axis_info}
        if units and units <= {"metre", "meter", "m"}:
            return ProjectedCRS(source, crs_label(source), "source")
        # Projected but in feet or similar: still valid for measurement, but we convert
        # to metres afterwards. Keep it rather than reprojecting twice.
        return ProjectedCRS(source, crs_label(source), "source")

    minx, miny, maxx, maxy = bounds
    lon = (minx + maxx) / 2.0
    lat = (miny + maxy) / 2.0
    fits_one_zone = _extent_width_degrees(minx, miny, maxx, maxy) <= 6.0
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
    crs = CRS.from_proj4(proj4)
    return ProjectedCRS(crs, "LAEA (centred on extent)", "laea")


class Projector:
    """Transforms geometries from the source CRS into the measurement CRS."""

    def __init__(self, source: CRS, target: ProjectedCRS) -> None:
        self.source = source
        self.target = target
        self._transformer = Transformer.from_crs(source, target.crs, always_xy=True)

    @property
    def identical(self) -> bool:
        return self.source == self.target.crs

    def project(self, geometry):
        if self.identical:
            return geometry
        return shapely_transform(lambda x, y, z=None: self._transformer.transform(x, y), geometry)
