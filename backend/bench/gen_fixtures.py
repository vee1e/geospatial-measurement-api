#!/usr/bin/env python3
"""Generate synthetic benchmark fixtures into /tmp/geo-bench-fixtures.

KML files with ~100, ~1,000 and ~10,000 features (40% polygon, 40% line, 20% point)
plus one zipped Shapefile of 1,000 polygons. A Shapefile holds one geometry type per
file, so the Shapefile cannot carry the mix the KML files do.

Everything is written under /tmp so no fixture can land in git. Re-running replaces
the whole directory.
"""

from __future__ import annotations

import json
import random
import shutil
import time
import zipfile
from pathlib import Path

OUT = Path("/tmp/geo-bench-fixtures")

KML_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>\n'
)
KML_TAIL = "</Document></kml>\n"


def _rng(seed: int) -> random.Random:
    return random.Random(seed)


def _polygon(rng: random.Random) -> str:
    lon = rng.uniform(-122.50, -122.30)
    lat = rng.uniform(37.40, 37.60)
    d = rng.uniform(0.0005, 0.0020)
    ring = [(lon, lat), (lon + d, lat), (lon + d, lat + d), (lon, lat + d), (lon, lat)]
    coords = " ".join(f"{x:.6f},{y:.6f}" for x, y in ring)
    return (
        "<Polygon><outerBoundaryIs><LinearRing>"
        f"<coordinates>{coords}</coordinates>"
        "</LinearRing></outerBoundaryIs></Polygon>"
    )


def _line(rng: random.Random) -> str:
    lon = rng.uniform(-122.50, -122.30)
    lat = rng.uniform(37.40, 37.60)
    points = []
    for step in range(rng.randint(4, 9)):
        points.append((lon + step * 0.0004, lat + step * 0.0003))
    coords = " ".join(f"{x:.6f},{y:.6f}" for x, y in points)
    return f"<LineString><coordinates>{coords}</coordinates></LineString>"


def _point(rng: random.Random) -> str:
    lon = rng.uniform(-122.50, -122.30)
    lat = rng.uniform(37.40, 37.60)
    return f"<Point><coordinates>{lon:.6f},{lat:.6f}</coordinates></Point>"


def _placemark(index: int, rng: random.Random) -> str:
    kind_slot = index % 5
    if kind_slot in (0, 1):
        kind, geom = "polygon", _polygon(rng)
    elif kind_slot in (2, 3):
        kind, geom = "line", _line(rng)
    else:
        kind, geom = "point", _point(rng)
    return (
        "<Placemark>"
        f"<name>feature-{index}</name>"
        f"<description>synthetic {kind} feature number {index}</description>"
        "<ExtendedData>"
        f'<Data name="kind"><value>{kind}</value></Data>'
        f'<Data name="seq"><value>{index}</value></Data>'
        f'<Data name="survey"><value>block-{index % 7}</value></Data>'
        "</ExtendedData>"
        f"{geom}"
        "</Placemark>\n"
    )


def build_kml(path: Path, count: int) -> None:
    rng = _rng(42 + count)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(KML_HEAD)
        for index in range(count):
            handle.write(_placemark(index, rng))
        handle.write(KML_TAIL)


def build_shapefile_zip(path: Path, count: int) -> None:
    import shapefile
    from pyproj import CRS

    rng = _rng(7)
    base = OUT / "_shp_staging"
    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    writer.field("name", "C", 40)
    writer.field("zone", "C", 12)
    writer.field("seq", "N", 10, 0)
    for index in range(count):
        lon = rng.uniform(-122.50, -122.30)
        lat = rng.uniform(37.40, 37.60)
        d = rng.uniform(0.0005, 0.0020)
        ring = [(lon, lat), (lon + d, lat), (lon + d, lat + d), (lon, lat + d), (lon, lat)]
        writer.poly([ring])
        writer.record(f"feature-{index}", f"block-{index % 7}", index)
    writer.close()
    Path(f"{base}.prj").write_text(CRS.from_epsg(4326).to_wkt(), encoding="utf-8")

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for suffix in (".shp", ".shx", ".dbf", ".prj"):
            archive.write(f"{base}{suffix}", arcname=f"bench{suffix}")
    for suffix in (".shp", ".shx", ".dbf", ".prj"):
        Path(f"{base}{suffix}").unlink(missing_ok=True)


def count_kml_features(path: Path) -> int:
    return path.read_text(encoding="utf-8").count("<Placemark>")


def main() -> None:
    started = time.perf_counter()
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    manifest: dict[str, dict] = {}

    for count in (100, 1000, 10000):
        name = f"kml_{count}"
        path = OUT / f"{name}.kml"
        build_kml(path, count)
        actual = count_kml_features(path)
        manifest[name] = {
            "path": str(path),
            "kind": "KML",
            "features": actual,
            "size_bytes": path.stat().st_size,
        }

    shp = OUT / "shp_1000.zip"
    build_shapefile_zip(shp, 1000)
    manifest["shp_1000"] = {
        "path": str(shp),
        "kind": "SHAPEFILE",
        "features": 1000,
        "size_bytes": shp.stat().st_size,
    }

    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for name, info in manifest.items():
        print(f"{name:12s} {info['features']:6d} features  {info['size_bytes']:9d} bytes  "
              f"{info['path']}")
    print(f"fixtures written to {OUT} in {time.perf_counter() - started:.2f}s")


if __name__ == "__main__":
    main()
