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

    return SourceLayer(
        features=_read_shapefile_records(shp_bytes, dbf, shx),
        crs_wkt=prj_bytes.decode("utf-8", errors="replace") if prj_bytes else None,
        notes=notes,
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
    shp_bytes: bytes, dbf: bytes | None, shx: bytes | None
) -> list[SourceFeature]:
    kwargs: dict = {"shp": io.BytesIO(shp_bytes)}
    if dbf is not None:
        kwargs["dbf"] = io.BytesIO(dbf)
    if shx is not None:
        kwargs["shx"] = io.BytesIO(shx)
    try:
        reader = shapefile.Reader(**kwargs)
    except Exception as exc:
        raise FileRejected(f"could not read shapefile: {exc}") from exc

    features: list[SourceFeature] = []
    with reader:
        if dbf is None:
            # A geometry-only Shapefile is a normal export; pyshp reads it fine.
            for shape in reader.shapes():
                features.append(SourceFeature(geometry=_geo_interface(shape), properties={}))
            return features

        fields = [f[0] for f in reader.fields[1:]]  # skip the deletion-flag field
        for record in reader.shapeRecords():
            pairs = zip(fields, record.record, strict=False)
            props = {key: _clean_attribute(value) for key, value in pairs}
            features.append(SourceFeature(geometry=_geo_interface(record.shape), properties=props))
    return features


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


def _parse_coordinates(text: str | None) -> list[tuple[float, ...]]:
    points: list[tuple[float, ...]] = []
    for chunk in (text or "").split():
        parts = chunk.split(",")
        if len(parts) < 2:
            continue
        try:
            values = tuple(float(p) for p in parts)
        except ValueError:
            continue
        points.append(values)
    return points


def _geometry_from_kml(element) -> dict | None:
    """Turn a KML geometry element into a GeoJSON geometry mapping."""
    kind = _local(element.tag)
    if kind == "Point":
        coords = _parse_coordinates(_child_text(element, "coordinates"))
        return {"type": "Point", "coordinates": coords[0][:2]} if coords else None
    if kind == "LineString":
        coords = _parse_coordinates(_child_text(element, "coordinates"))
        if len(coords) < 2:
            return None
        return {"type": "LineString", "coordinates": [c[:2] for c in coords]}
    if kind == "Polygon":
        outer = None
        for boundary in _children(element, "outerBoundaryIs"):
            for ring in _children(boundary, "LinearRing"):
                outer = _child_text(ring, "coordinates")
        if outer is None:
            return None
        shell = [c[:2] for c in _parse_coordinates(outer)]
        if len(shell) < 3:
            return None
        rings = [shell]
        for boundary in _children(element, "innerBoundaryIs"):
            for ring in _children(boundary, "LinearRing"):
                hole = [c[:2] for c in _parse_coordinates(_child_text(ring, "coordinates"))]
                if len(hole) >= 3:
                    rings.append(hole)
        return {"type": "Polygon", "coordinates": rings}
    if kind == "MultiGeometry":
        parts = [geo for child in element if (geo := _geometry_from_kml(child)) is not None]
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

    features: list[SourceFeature] = []
    for placemark in placemarks:
        geom_element = None
        for child in placemark:
            if _local(child.tag) in {"Point", "LineString", "Polygon", "MultiGeometry"}:
                geom_element = child
                break
        geometry = _geometry_from_kml(geom_element) if geom_element is not None else None
        features.append(
            SourceFeature(geometry=geometry, properties=_properties_from_placemark(placemark))
        )

    return SourceLayer(features=features, crs_wkt=KML_CRS_WKT)
