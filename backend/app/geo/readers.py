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
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

import shapefile  # pyshp

from ..config import Settings


class FileRejected(Exception):
    """The upload itself is not usable. Maps to a 4xx response."""


@dataclass
class SourceFeature:
    geometry: dict | None
    properties: dict = field(default_factory=dict)
    geometry_type: str = "Unknown"


@dataclass
class SourceLayer:
    features: list[SourceFeature]
    crs_wkt: str | None
    source_format: str
    notes: list[str] = field(default_factory=list)


def detect_format(filename: str) -> str:
    lowered = filename.lower()
    if lowered.endswith(".zip"):
        return "SHAPEFILE"
    if lowered.endswith(".kml"):
        return "KML"
    raise FileRejected(
        f"unsupported file type '{Path(filename).suffix or filename}'. "
        "Send a .zip containing a Shapefile, or a .kml."
    )


def read_layer(path: Path, filename: str, settings: Settings) -> SourceLayer:
    """Parse an uploaded file. Raises FileRejected for anything unusable."""
    source_format = detect_format(filename)
    if source_format == "SHAPEFILE":
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

        shp_names = [i.filename for i in infos if i.filename.lower().endswith(".shp")]
        if not shp_names:
            raise FileRejected("zip archive does not contain a .shp file")
        if len(shp_names) > 1:
            raise FileRejected(
                "zip archive contains "
                f"{len(shp_names)} shapefiles ({', '.join(shp_names)}); one per upload"
            )

        base = shp_names[0][:-4].lower()

        def sidecar(suffix: str) -> bytes | None:
            for info in infos:
                name = info.filename.lower()
                if name.endswith(suffix) and name[: -len(suffix)] == base:
                    return archive.read(info)
            return None

        shp_bytes = archive.read(shp_names[0])
        dbf = sidecar(".dbf")
        shx = sidecar(".shx")
        prj_bytes = sidecar(".prj")

    if prj_bytes is None:
        notes.append("no .prj sidecar found; source CRS will be assumed")

    return SourceLayer(
        features=_read_shapefile_records(shp_bytes, dbf, shx),
        crs_wkt=prj_bytes.decode("utf-8", errors="replace") if prj_bytes else None,
        source_format="SHAPEFILE",
        notes=notes,
    )


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

    fields = [f[0] for f in reader.fields[1:]]  # skip the deletion-flag field
    features: list[SourceFeature] = []
    with reader:
        for record in reader.shapeRecords():
            clean: dict = {}
            for key, value in zip(fields, record.record, strict=False):
                if isinstance(value, bytes):
                    clean[key] = value.decode("utf-8", errors="replace")
                elif isinstance(value, (str, int, float, bool)) or value is None:
                    clean[key] = value
                else:
                    clean[key] = str(value)  # dates and anything else JSON cannot hold
            try:
                geo = record.shape.__geo_interface__
            except Exception:
                geo = None  # a corrupt shape must not sink the whole file
            features.append(
                SourceFeature(
                    geometry=geo,
                    properties=clean,
                    geometry_type=(geo or {}).get("type", "Null"),
                )
            )
    return features


# --- KML -----------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element, name: str) -> list:
    return [child for child in element if _local(child.tag) == name]


def _descendants(element, name: str) -> list:
    return [node for node in element.iter() if node is not element and _local(node.tag) == name]


def _child_text(element, name: str) -> str | None:
    for child in element:
        if _local(child.tag) == name:
            return child.text
    return None


def _parse_coordinates(text: str) -> list[tuple[float, ...]]:
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


def _coordinates_element(element, name: str) -> str | None:
    """Return the text of a direct `coordinates` child, namespace or not."""
    for child in element:
        if _local(child.tag) == name:
            return child.text
    return None


def _geometry_from_kml(element) -> dict | None:
    """Turn a KML geometry element into a GeoJSON geometry mapping."""
    kind = _local(element.tag)
    if kind == "Point":
        coords = _parse_coordinates(_coordinates_element(element, "coordinates") or "")
        return {"type": "Point", "coordinates": coords[0][:2]} if coords else None
    if kind == "LineString":
        coords = _parse_coordinates(_coordinates_element(element, "coordinates") or "")
        if len(coords) < 2:
            return None
        return {"type": "LineString", "coordinates": [c[:2] for c in coords]}
    if kind == "Polygon":
        outer = None
        for boundary in _children(element, "outerBoundaryIs"):
            for ring in _children(boundary, "LinearRing"):
                outer = _coordinates_element(ring, "coordinates")
        if outer is None:
            return None
        shell = [c[:2] for c in _parse_coordinates(outer)]
        if len(shell) < 3:
            return None
        rings = [shell]
        for boundary in _children(element, "innerBoundaryIs"):
            for ring in _children(boundary, "LinearRing"):
                text = _coordinates_element(ring, "coordinates")
                hole = [c[:2] for c in _parse_coordinates(text or "")]
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
    features: list[SourceFeature] = []
    for placemark in _descendants(root, "Placemark"):
        geom_element = None
        for child in placemark:
            if _local(child.tag) in {"Point", "LineString", "Polygon", "MultiGeometry"}:
                geom_element = child
                break
        geometry = _geometry_from_kml(geom_element) if geom_element is not None else None
        features.append(
            SourceFeature(
                geometry=geometry,
                properties=_properties_from_placemark(placemark),
                geometry_type=(geometry or {}).get("type", "Null"),
            )
        )

    if not features:
        raise FileRejected("KML contains no placemarks")
    return SourceLayer(features=features, crs_wkt=None, source_format="KML")
