"""Generate the example files in this directory.

    python3 generate.py

Each file exercises one behaviour worth checking by hand. The outputs are committed so
you can upload them without running anything; rerun this after editing the fixtures.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import shapefile
from pyproj import CRS

HERE = Path(__file__).parent

# 0.01 degree square near Delhi: ~1.08 km2 projected to UTM 43N.
DELHI_OUTER = [
    [77.2000, 28.6100],
    [77.2100, 28.6100],
    [77.2100, 28.6200],
    [77.2000, 28.6200],
    [77.2000, 28.6100],
]
DELHI_INNER = [
    [77.2030, 28.6130],
    [77.2070, 28.6130],
    [77.2070, 28.6170],
    [77.2030, 28.6170],
    [77.2030, 28.6130],
]
DELHI_BLOCK_B = [
    [77.2120, 28.6100],
    [77.2220, 28.6100],
    [77.2220, 28.6200],
    [77.2120, 28.6200],
    [77.2120, 28.6100],
]


def write_zip(name: str, entries: dict[str, bytes]) -> Path:
    path = HERE / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry, content in entries.items():
            archive.writestr(entry, content)
    return path


def write_text(name: str, content: str | bytes) -> Path:
    path = HERE / name
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


def shapefile_parts(
    ring: list[list[float]],
    *,
    holes: list[list[list[float]]] | None = None,
    name: str = "block-a",
    with_prj: bool = True,
    prj: str | None = None,
    with_dbf: bool = True,
) -> dict[str, bytes]:
    """A one-polygon Shapefile as the sidecar bytes that go into a zip."""
    writer = shapefile.Writer(shp=io.BytesIO(), dbf=io.BytesIO(), shx=io.BytesIO())
    writer.field("name", "C", 40)
    writer.field("zone", "C", 20)
    writer.poly([ring, *(holes or [])])
    writer.record(name, "east")
    writer.close()

    parts: dict[str, bytes] = {"parcels.shp": writer.shp.getvalue()}
    parts["parcels.shx"] = writer.shx.getvalue()
    if with_dbf:
        parts["parcels.dbf"] = writer.dbf.getvalue()
    if prj:
        parts["parcels.prj"] = prj.encode()
    elif with_prj:
        parts["parcels.prj"] = CRS.from_epsg(4326).to_wkt().encode()
    return parts


def polygon_placemark(
    name: str, ring: list[list[float]], hole: list[list[float]] | None = None, **attrs: str
) -> str:
    coords = " ".join(f"{x},{y}" for x, y in ring)
    inner = ""
    if hole:
        hole_coords = " ".join(f"{x},{y}" for x, y in hole)
        inner = (
            f"<innerBoundaryIs><LinearRing><coordinates>{hole_coords}"
            "</coordinates></LinearRing></innerBoundaryIs>"
        )
    data = "".join(
        f'<Data name="{key}"><value>{value}</value></Data>' for key, value in attrs.items()
    )
    return (
        f"<Placemark><name>{name}</name><ExtendedData>{data}</ExtendedData>"
        f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{coords}"
        f"</coordinates></LinearRing></outerBoundaryIs>{inner}"
        "</Polygon></Placemark>"
    )


def kml(*placemarks: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>\n'
        + "\n".join(placemarks)
        + "\n</Document></kml>\n"
    )


def build() -> list[Path]:
    made: list[Path] = []

    # 1. The happy path: two polygons (one with a hole), a line, a point, attributes.
    made.append(
        write_text(
            "delhi-parcels.kml",
            kml(
                polygon_placemark(
                    "block-a", DELHI_OUTER, DELHI_INNER, crop="wheat", survey="2026-01"
                ),
                polygon_placemark("block-b", DELHI_BLOCK_B, crop="mustard", survey="2026-01"),
                "<Placemark><name>canal</name><ExtendedData>"
                '<Data name="length_class"><value>primary</value></Data></ExtendedData>'
                "<LineString><coordinates>77.2000,28.6100 77.2100,28.6200 "
                "77.2150,28.6180</coordinates></LineString></Placemark>",
                "<Placemark><name>bench-mark</name>"
                "<Point><coordinates>77.2050,28.6150</coordinates></Point></Placemark>",
            ),
        )
    )

    # 2. The same idea as a zipped Shapefile with a .prj sidecar.
    made.append(write_zip("delhi-parcels.zip", shapefile_parts(DELHI_OUTER)))

    # 3. No .prj: the API must assume a CRS and flag it.
    made.append(write_zip("no-crs-parcels.zip", shapefile_parts(DELHI_OUTER, with_prj=False)))

    # 4. NAD83 / New York Long Island in US survey feet: 500x500 ft must come back
    #    as ~23,226 m2, not 250,000 m2.
    feet_ring = [
        [1000000.0, 200000.0],
        [1000000.0, 200500.0],
        [1000500.0, 200500.0],
        [1000500.0, 200000.0],
        [1000000.0, 200000.0],
    ]
    made.append(
        write_zip(
            "feet-grid.zip",
            shapefile_parts(feet_ring, name="500ft-square", prj=CRS.from_epsg(2263).to_wkt()),
        )
    )

    # 5. Finder adds resource forks to zips; they must not count as a shapefile.
    finder = shapefile_parts(DELHI_OUTER)
    finder["__MACOSX/._parcels.shp"] = b"\x00\x05\x16\x07 AppleDouble encoded"
    made.append(write_zip("finder-export.zip", finder))

    # 6. Geometry only: .shp and .shx with no .dbf, as some tools export it.
    made.append(write_zip("geometry-only.zip", shapefile_parts(DELHI_OUTER, with_dbf=False)))

    # 7. Features that cannot be measured, next to one that can.
    made.append(
        write_text(
            "partial-failures.kml",
            kml(
                "<Placemark><name>broken-ring</name>"
                "<Polygon><outerBoundaryIs><LinearRing>"
                "<coordinates>77.20,28.61 77.21,28.62</coordinates>"
                "</LinearRing></outerBoundaryIs></Polygon></Placemark>",
                "<Placemark><name>mixed</name><MultiGeometry>"
                "<Point><coordinates>77.2050,28.6150</coordinates></Point>"
                f"<Polygon><outerBoundaryIs><LinearRing><coordinates>"
                + " ".join(f"{x},{y}" for x, y in DELHI_OUTER)
                + "</coordinates></LinearRing></outerBoundaryIs></Polygon>"
                "</MultiGeometry></Placemark>",
                "<Placemark><name>healthy</name><LineString>"
                "<coordinates>77.2000,28.6100 77.2100,28.6100</coordinates>"
                "</LineString></Placemark>",
            ),
        )
    )

    # 8. A run across the antimeridian: 3 degrees of longitude, not 357.
    made.append(
        write_text(
            "antimeridian.kml",
            kml(
                "<Placemark><name>dateline-run</name><LineString>"
                "<coordinates>178.0,0.0 -179.0,0.0</coordinates></LineString></Placemark>"
            ),
        )
    )

    # 9. Text wearing a .zip extension: refused at upload, before any processing.
    made.append(write_text("not-a-real.zip", "this is a text file pretending to be a zip\n"))

    return made


if __name__ == "__main__":
    for path in sorted(build()):
        print(f"{path.name:28} {path.stat().st_size:>8,} bytes")
