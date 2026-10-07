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


def _iter_coordinate_leaves(node):
    """Yield every coordinate pair in a nesting of lists, depth first, left to right.

    The same traversal order is used to flatten and to rebuild, so the flat arrays
    line up with the reconstructed geometry without any bookkeeping.
    """
    if isinstance(node, (list, tuple)):
        if node and isinstance(node[0], (int, float)):
            yield node
        else:
            for child in node:
                yield from _iter_coordinate_leaves(child)


def _iter_geometry_leaves(mapping):
    """Coordinate leaves of one GeoJSON geometry mapping (nested collections too)."""
    if not isinstance(mapping, dict):
        return
    if "coordinates" in mapping:
        yield from _iter_coordinate_leaves(mapping["coordinates"])
    for part in mapping.get("geometries") or ():
        yield from _iter_geometry_leaves(part)


def _rebuild_leaves(node, xs, ys, index):
    """Return (structure, next_index): a copy of `node` with transformed pairs."""
    if isinstance(node, (list, tuple)):
        if node and isinstance(node[0], (int, float)):
            rebuilt = [xs[index], ys[index]]
            if len(node) > 2:  # keep a third (z) dimension the transform never touches
                rebuilt.extend(node[2:])
            return rebuilt, index + 1
        out = []
        for child in node:
            child_node, index = _rebuild_leaves(child, xs, ys, index)
            out.append(child_node)
        return out, index
    return node, index


def _rebuild_geometry(mapping, xs, ys, index=0):
    """A copy of `mapping` whose coordinate pairs come from the transformed arrays.

    Returns (copy, next_index); the index tracks the same depth-first, left-to-right
    order the flattening walk used.
    """
    if not isinstance(mapping, dict):
        return mapping, index
    out = dict(mapping)
    if "coordinates" in mapping:
        out["coordinates"], index = _rebuild_leaves(mapping["coordinates"], xs, ys, index)
    if "geometries" in mapping:
        geometries = []
        for part in mapping["geometries"]:
            part, index = _rebuild_geometry(part, xs, ys, index)
            geometries.append(part)
        out["geometries"] = geometries
    return out, index


class Projector:
    """Transforms geometries from the source CRS into the measurement CRS.

    The whole layer's coordinates are pushed through pyproj in one call by
    `prepare()` before measurement starts; `prepared_mapping()` hands each feature
    its already-transformed copy. When the batch call fails (or one geometry cannot
    be rebuilt), that feature falls back to a transform of its own through
    `project_xy`, so one bad layer never fails the file.
    """

    def __init__(self, source: CRS, target: ProjectedCRS) -> None:
        self.source = source
        self.target = target
        self._transformer = Transformer.from_crs(source, target.crs, always_xy=True)
        # None means prepare() has not run: every geometry transforms on its own.
        self._prepared: dict[int, dict] | None = None

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

    def prepare(self, mappings) -> None:
        """Flatten every geometry of the layer, transform once, cache the copies."""
        live = [mapping for mapping in mappings if mapping]
        if self.identical:
            # Nothing to transform; the prepared copy is the original mapping.
            self._prepared = {id(mapping): mapping for mapping in live}
            return
        xs: list[float] = []
        ys: list[float] = []
        offsets: list[tuple[dict, int, int]] = []
        for mapping in live:
            start = len(xs)
            for leaf in _iter_geometry_leaves(mapping):
                xs.append(leaf[0])
                ys.append(leaf[1])
            offsets.append((mapping, start, len(xs)))
        try:
            tx, ty = self._transformer.transform(xs, ys)
        except Exception:
            # One bad layer must not fail the file: drop to per-geometry transforms.
            self._prepared = None
            return
        prepared: dict[int, dict] = {}
        for mapping, start, end in offsets:
            try:
                rebuilt, _ = _rebuild_geometry(mapping, tx[start:end], ty[start:end])
            except Exception:
                continue  # leave this geometry out; it transforms the old way
            prepared[id(mapping)] = rebuilt
        self._prepared = prepared

    def prepared_mapping(self, mapping):
        """The already-transformed copy of this mapping, or None when there is none."""
        if self._prepared is None:
            return None
        return self._prepared.get(id(mapping))

    def project_xy(self, xs, ys):
        """Transform two flat coordinate arrays (per-geometry fallback path)."""
        if self.identical:
            return xs, ys
        return self._transformer.transform(xs, ys)
