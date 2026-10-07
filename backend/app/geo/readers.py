"""Read geospatial files into plain feature records.

Two input formats are supported:

- ``.zip`` containing an ESRI Shapefile (a Shapefile is a set of sidecar files, so it
  is always transferred zipped). Read with pyshp, which has no GDAL dependency.
- ``.kml``, parsed with the standard library's ElementTree.

Both paths return the same structure, so the rest of the pipeline does not care which
format arrived.
"""

from __future__ import annotations

import io
import math
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

import shapefile  # pyshp
from pyproj import CRS

from ..config import Settings

# KML is defined to use WGS 84 longitude/latitude. Declared outright rather than left
# to the assumed-CRS path, so `crs_assumed` stays false for KML.
KML_CRS_WKT = CRS.from_epsg(4326).to_wkt()


class FileRejected(Exception):
    """The upload itself is not usable. Maps to a 4xx response."""


@dataclass
class SourceFeature:
    geometry: dict | None
    properties: dict = field(default_factory=dict)


@dataclass
class SourceLayer:
    features: list[SourceFeature]
    crs_wkt: str | None
    notes: list[str] = field(default_factory=list)
    # Extent gathered while reading: four compares per coordinate block, folded in
    # only for geometries that were actually accepted. None when no feature
    # exposes a finite coordinate pair.
    bounds: tuple[float, float, float, float] | None = None


class _BoundsAcc:
    """Running min/max of the coordinates a reader has accepted so far.

    Only finite x/y pairs count, so the extent feeds the projection choice directly.
    """

    __slots__ = ("found", "maxx", "maxy", "minx", "miny")

    def __init__(self) -> None:
        self.minx = self.miny = math.inf
        self.maxx = self.maxy = -math.inf
        self.found = False

    def add(self, x: float, y: float) -> None:
        if not (math.isfinite(x) and math.isfinite(y)):
            return
        self.found = True
        if x < self.minx:
            self.minx = x
        if x > self.maxx:
            self.maxx = x
        if y < self.miny:
            self.miny = y
        if y > self.maxy:
            self.maxy = y

    def value(self) -> tuple[float, float, float, float] | None:
        return (self.minx, self.miny, self.maxx, self.maxy) if self.found else None


def detect_format(filename: str) -> str:
    lowered = filename.lower()
    if lowered.endswith(".zip"):
        return "SHAPEFILE"
    if lowered.lower().endswith(".kml"):
        return "KML"
    raise FileRejected(
        f"unsupported file type '{Path(filename).suffix or filename}'. "
        "Send a .zip containing a Shapefile, or a .kml."
    )


def has_valid_magic(filename: str, head: bytes) -> bool:
    """Cheap content check so a text file named survey.zip is refused at upload."""
    kind = detect_format(filename)
    if kind == "SHAPEFILE":
        return head.startswith(b"PK\x03\x04")
    return b"<" in head[:512]  # an XML prolog or a root element


def _is_junk_entry(name: str) -> bool:
    """Finder writes __MACOSX/._file entries that must not count as real files."""
    parts = PurePosixPath(name).parts
    return "__MACOSX" in parts or PurePosixPath(name).name.startswith("._")


def read_layer(path: Path, filename: str, settings: Settings) -> SourceLayer:
    """Parse an uploaded file. Raises FileRejected for anything unusable."""
    if detect_format(filename) == "SHAPEFILE":
        return _read_shapefile_zip(path, settings)
    return _read_kml(path)


# --- Shapefile -----------------------------------------------------------------


def _read_shapefile_zip(path: Path, settings: Settings) -> SourceLayer:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise FileRejected("file is not a valid .zip archive") from exc

    notes: list[str] = []
    with archive:
        infos = archive.infolist()
        if len(infos) > settings.max_zip_entries:
            raise FileRejected(
                f"archive has {len(infos)} entries, limit is {settings.max_zip_entries}"
            )
        uncompressed = sum(info.file_size for info in infos)
        if uncompressed > settings.max_zip_uncompressed_bytes:
            raise FileRejected(
                f"archive expands to {uncompressed} bytes, "
                f"limit is {settings.max_zip_uncompressed_bytes}"
            )

        shp_names = [
            info.filename
            for info in infos
            if info.filename.lower().endswith(".shp") and not _is_junk_entry(info.filename)
        ]
        if not shp_names:
            raise FileRejected("zip archive does not contain a .shp file")
        if len(shp_names) > 1:
            raise FileRejected(
                "zip archive contains "
                f"{len(shp_names)} shapefiles ({', '.join(shp_names)}); one per upload"
            )

        shp_path = PurePosixPath(shp_names[0])
        base = shp_path.stem.lower()

        def sidecar(suffix: str) -> bytes | None:
            """Find a sidecar next to the .shp first, then anywhere in the archive."""
            same_dir: bytes | None = None
            anywhere: bytes | None = None
            for info in infos:
                name = PurePosixPath(info.filename)
                if _is_junk_entry(info.filename) or name.suffix.lower() != suffix:
                    continue
                if name.stem.lower() != base:
                    continue
                if name.parent == shp_path.parent:
                    same_dir = archive.read(info)
                    break
                if anywhere is None:
                    anywhere = archive.read(info)
            return same_dir if same_dir is not None else anywhere

        shp_bytes = archive.read(shp_names[0])
        dbf = sidecar(".dbf")
        shx = sidecar(".shx")
        prj_bytes = sidecar(".prj")

    if prj_bytes is None:
        notes.append("no .prj sidecar found; source CRS will be assumed")
    if dbf is None:
        notes.append("no .dbf sidecar found; attributes will be empty")

    features, bounds = _read_shapefile_records(
        shp_bytes, dbf, shx, max_features=settings.max_features
    )
    return SourceLayer(
        features=features,
        crs_wkt=prj_bytes.decode("utf-8", errors="replace") if prj_bytes else None,
        notes=notes,
        bounds=bounds,
    )


def _clean_attribute(value):
    """DBF fields can hold bytes, dates and NaN; JSON cannot hold any of those."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _read_shapefile_records(
    shp_bytes: bytes,
    dbf: bytes | None,
    shx: bytes | None,
    *,
    max_features: int,
) -> tuple[list[SourceFeature], tuple[float, float, float, float] | None]:
    kwargs: dict = {"shp": io.BytesIO(shp_bytes)}
    if dbf is not None:
        kwargs["dbf"] = io.BytesIO(dbf)
    if shx is not None:
        kwargs["shx"] = io.BytesIO(shx)
    try:
        reader = shapefile.Reader(**kwargs)
    except Exception as exc:
        raise FileRejected(f"could not read shapefile: {exc}") from exc

    # The header states the shape count, so an over-limit file is refused without
    # parsing a single record. The message matches the pipeline's limit error.
    if reader.numShapes > max_features:
        raise FileRejected(
            f"file has {reader.numShapes} features, limit is {max_features}"
        )

    acc = _BoundsAcc()
    features: list[SourceFeature] = []
    with reader:
        if dbf is None:
            # A geometry-only Shapefile is a normal export; pyshp reads it fine.
            for shape in reader.shapes():
                geometry = _geo_interface(shape)
                if geometry is not None:
                    _accumulate_shape_bounds(shape, geometry, acc)
                features.append(SourceFeature(geometry=geometry, properties={}))
            return features, acc.value()

        fields = [f[0] for f in reader.fields[1:]]  # skip the deletion-flag field
        for record in reader.shapeRecords():
            pairs = zip(fields, record.record, strict=False)
            props = {key: _clean_attribute(value) for key, value in pairs}
            geometry = _geo_interface(record.shape)
            if geometry is not None:
                _accumulate_shape_bounds(record.shape, geometry, acc)
            features.append(SourceFeature(geometry=geometry, properties=props))
    return features, acc.value()


def _accumulate_shape_bounds(shape, geometry, acc: _BoundsAcc) -> None:
    """Fold one shape's extent into the accumulator.

    pyshp carries a bbox in the record header for polygons and polylines, which is
    free; point and multipoint records have none, so those fall back to their points.
    Only coordinates that ended up in the geometry count, so the result matches a
    walk over the GeoJSON mapping.
    """
    bbox = getattr(shape, "bbox", None)
    if bbox:
        acc.add(bbox[0], bbox[1])
        acc.add(bbox[2], bbox[3])
        return
    _accumulate_geometry_bounds(geometry, acc)


def _accumulate_geometry_bounds(geometry, acc: _BoundsAcc) -> None:
    if geometry.get("type") == "GeometryCollection":
        for part in geometry.get("geometries") or ():
            _accumulate_geometry_bounds(part, acc)
        return
    def walk(node) -> None:
        if not isinstance(node, (list, tuple)) or not node:
            return
        if isinstance(node[0], (int, float)):
            if len(node) >= 2:
                acc.add(node[0], node[1])
            return
        for child in node:
            walk(child)

    walk(geometry.get("coordinates"))


def _geo_interface(shape) -> dict | None:
    try:
        return shape.__geo_interface__
    except Exception:
        return None  # a corrupt shape must not sink the whole file


# --- KML -----------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element, name: str) -> list:
    return [child for child in element if _local(child.tag) == name]


def _child_text(element, name: str) -> str | None:
    for child in element:
        if _local(child.tag) == name:
            return child.text
    return None


def _parse_coordinates(
    text: str | None, acc: _BoundsAcc | None = None
) -> list[tuple[float, ...]]:
    """Split a KML coordinate block into tuples of floats.

    When `acc` is given it records the bounds as the floats appear, so the caller
    can fold them into the layer extent for free if the geometry is accepted.
    `tuple(map(float, parts))` instead of a generator expression is ~24% quicker
    over the bench fixtures (bench/parse_variants.py), with identical output and
    identical handling of malformed chunks.
    """
    points: list[tuple[float, ...]] = []
    append = points.append
    for chunk in (text or "").split():
        parts = chunk.split(",")
        if len(parts) < 2:
            continue
        try:
            values = tuple(map(float, parts))
        except ValueError:
            continue
        append(values)
        if acc is not None:
            acc.add(values[0], values[1])
    return points


def _merge_bounds(dst: _BoundsAcc, src: _BoundsAcc) -> None:
    """Fold one coordinate block's extent into the layer extent (four compares)."""
    if not src.found:
        return
    dst.add(src.minx, src.miny)
    dst.add(src.maxx, src.maxy)


def _add_points(acc: _BoundsAcc, points) -> None:
    for point in points:
        acc.add(point[0], point[1])


def _geometry_from_kml(element, acc: _BoundsAcc) -> dict | None:
    """Turn a KML geometry element into a GeoJSON geometry mapping.

    `acc` receives the coordinates of every ring/line only once that geometry is
    actually accepted, so dropped sub-geometries never affect the layer extent.
    """
    kind = _local(element.tag)
    local = _BoundsAcc()
    if kind == "Point":
        coords = _parse_coordinates(_child_text(element, "coordinates"))
        if not coords:
            return None
        _add_points(acc, coords[:1])  # only the first tuple survives below
        return {"type": "Point", "coordinates": coords[0][:2]}
    if kind == "LineString":
        coords = _parse_coordinates(_child_text(element, "coordinates"), local)
        if len(coords) < 2:
            return None
        _merge_bounds(acc, local)
        return {"type": "LineString", "coordinates": [c[:2] for c in coords]}
    if kind == "Polygon":
        outer = None
        for boundary in _children(element, "outerBoundaryIs"):
            for ring in _children(boundary, "LinearRing"):
                outer = _child_text(ring, "coordinates")
        if outer is None:
            return None
        shell = [c[:2] for c in _parse_coordinates(outer, local)]
        if len(shell) < 3:
            return None
        _merge_bounds(acc, local)
        rings = [shell]
        for boundary in _children(element, "innerBoundaryIs"):
            for ring in _children(boundary, "LinearRing"):
                hole_local = _BoundsAcc()
                hole = [
                    c[:2]
                    for c in _parse_coordinates(
                        _child_text(ring, "coordinates"), hole_local
                    )
                ]
                if len(hole) >= 3:
                    rings.append(hole)
                    _merge_bounds(acc, hole_local)
        return {"type": "Polygon", "coordinates": rings}
    if kind == "MultiGeometry":
        parts = [
            geo
            for child in element
            if (geo := _geometry_from_kml(child, acc)) is not None
        ]
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        types = {p["type"] for p in parts}
        if len(types) == 1:
            only = types.pop()
            if only in {"Point", "LineString", "Polygon"}:
                return {"type": f"Multi{only}", "coordinates": [p["coordinates"] for p in parts]}
        return {"type": "GeometryCollection", "geometries": parts}
    return None


def _properties_from_placemark(placemark) -> dict:
    props: dict = {}
    name = _child_text(placemark, "name")
    description = _child_text(placemark, "description")
    if name and name.strip():
        props["name"] = name.strip()
    if description and description.strip():
        props["description"] = description.strip()
    for extended in _children(placemark, "ExtendedData"):
        for data in _children(extended, "Data"):
            key = data.get("name")
            if key:
                props[key] = (_child_text(data, "value") or "").strip()
        for data in _children(extended, "SimpleData"):
            key = data.get("name")
            if key:
                props[key] = (data.text or "").strip()
    return props


def _read_kml(path: Path) -> SourceLayer:
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise FileRejected(f"could not parse KML: {exc}") from exc
    except OSError as exc:
        raise FileRejected(f"could not read file: {exc}") from exc

    root = tree.getroot()
    placemarks = [node for node in root.iter() if _local(node.tag) == "Placemark"]
    if not placemarks:
        raise FileRejected("KML contains no placemarks")

    acc = _BoundsAcc()
    features: list[SourceFeature] = []
    for placemark in placemarks:
        geom_element = None
        for child in placemark:
            if _local(child.tag) in {"Point", "LineString", "Polygon", "MultiGeometry"}:
                geom_element = child
                break
        geometry = (
            _geometry_from_kml(geom_element, acc) if geom_element is not None else None
        )
        features.append(
            SourceFeature(geometry=geometry, properties=_properties_from_placemark(placemark))
        )

    return SourceLayer(features=features, crs_wkt=KML_CRS_WKT, bounds=acc.value())
