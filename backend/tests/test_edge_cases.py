"""Edge cases the first pass got wrong: units, zips real tools produce, and limits."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
import shapefile
from pyproj import CRS

from .conftest import upload

# A 1000 ft x 1000 ft square in NAD83 / New York Long Island (ftUS): exactly
# 1,000,000 square feet, which is 92,903 square metres.
FEET_SQUARE_FT2 = 1_000_000
FEET_SQUARE_M2 = 92_903.4


def build_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def write_shapefile(
    entries: dict[str, bytes] | None = None,
    *,
    prj: str | None = None,
    ring: list[list[float]] | None = None,
) -> bytes:
    """Build a one-polygon Shapefile in memory and zip it with the given extras.

    `ring` defaults to a 0.01 degree square near Delhi, which is valid in WGS 84.
    Pass coordinates in the units of `prj` for anything else.
    """
    if ring is None:
        ring = [
            [77.20, 28.61],
            [77.20, 28.62],
            [77.21, 28.62],
            [77.21, 28.61],
            [77.20, 28.61],
        ]
    writer = shapefile.Writer(shp=io.BytesIO(), dbf=io.BytesIO(), shx=io.BytesIO())
    writer.field("name", "C", 40)
    writer.poly([ring])
    writer.record("block")
    writer.close()

    parts = {
        "parcels.shp": writer.shp.getvalue(),
        "parcels.shx": writer.shx.getvalue(),
        "parcels.dbf": writer.dbf.getvalue(),
    }
    if prj is not None:
        parts["parcels.prj"] = prj.encode()
    if entries:
        parts.update(entries)
    return build_zip(parts)


def measure(client, wait_for_completion, filename: str, content: bytes) -> dict:
    created = upload(client, filename, content)
    record = wait_for_completion(created["id"])
    assert record["status"] == "COMPLETED", record["error"]
    response = client.get(f"/api/files/{created['id']}/measurements/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    return response.json()


def test_projected_source_in_feet_is_converted_to_metres(client, wait_for_completion):
    """US State Plane in feet must not be reported as square metres of feet."""
    payload = measure(
        client,
        wait_for_completion,
        "ny.zip",
        write_shapefile(
            prj=CRS.from_epsg(2263).to_wkt(),
            ring=[
                [100000.0, 200000.0],
                [100000.0, 201000.0],
                [101000.0, 201000.0],
                [101000.0, 200000.0],
                [100000.0, 200000.0],
            ],
        ),
    )

    area = payload["features"][0]["measurement"]["value"]
    assert payload["projection_strategy"] == "source"
    assert payload["calculation_crs"] == "EPSG:2263"
    assert payload["features"][0]["measurement"]["unit"] == "m2"
    assert area == pytest.approx(FEET_SQUARE_M2, rel=0.01), area
    assert area != pytest.approx(FEET_SQUARE_FT2, rel=0.01)


def test_antimeridian_extent_stays_accurate(client, wait_for_completion):
    """A line spanning 178E to 179W is 3 degrees wide, not 357."""
    kml = """<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
      <Placemark><name>dateline-run</name><LineString>
        <coordinates>178.0,0.0 -179.0,0.0</coordinates>
      </LineString></Placemark>
    </Document></kml>"""
    payload = measure(client, wait_for_completion, "dateline.kml", kml.encode())

    length = payload["features"][0]["measurement"]["value"]
    # 3 degrees at the equator is about 334 km; centring the projection on lon 0
    # would report roughly three times that.
    assert payload["projection_strategy"] == "laea"
    assert 300_000 < length < 370_000, length


def test_shapefile_without_attributes_completes(client, wait_for_completion):
    full = write_shapefile()
    entries = {}
    with zipfile.ZipFile(io.BytesIO(full)) as archive:
        for name in archive.namelist():
            if not name.endswith(".dbf"):
                entries[name] = archive.read(name)
    entries["parcels.shx"] = entries.get("parcels.shx", b"")

    payload = measure(client, wait_for_completion, "geometry-only.zip", build_zip(entries))
    assert payload["summary"]["measured"] == 1
    assert payload["features"][0]["properties"] == {}
    assert "no .dbf" in payload["crs_note"]


def test_sidecars_in_another_folder_are_found(client, wait_for_completion):
    """Finder and tooling routinely nest the sidecars; only the basename matters."""
    full = write_shapefile(prj=CRS.from_epsg(4326).to_wkt())
    entries: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(full)) as archive:
        for name in archive.namelist():
            target = name if name.endswith(".shp") else f"alt/{name}"
            entries[target] = archive.read(name)

    payload = measure(client, wait_for_completion, "nested.zip", build_zip(entries))
    assert payload["crs_assumed"] is False
    assert payload["summary"]["measured"] == 1


def test_macos_resource_fork_zip_is_accepted(client, wait_for_completion):
    """A Finder-compressed zip carries __MACOSX/._parcels.shp next to the real file."""
    full = write_shapefile(prj=CRS.from_epsg(4326).to_wkt())
    entries: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(full)) as archive:
        for name in archive.namelist():
            entries[name] = archive.read(name)
    entries["__MACOSX/._parcels.shp"] = b"\x00\x05\x16\x07 AppleDouble"

    payload = measure(client, wait_for_completion, "finder.zip", build_zip(entries))
    assert payload["summary"]["feature_count"] == 1


def test_text_file_named_zip_is_refused_at_upload(client):
    response = client.post(
        "/api/files/", files={"file": ("fake.zip", b"definitely not a zip", "application/zip")}
    )
    assert response.status_code == 415
    assert "does not look like" in response.json()["detail"]


def test_oversized_request_is_refused_before_parsing(client):
    response = client.post(
        "/api/files/",
        files={"file": ("big.kml", b" " * (26 * 1024 * 1024), "application/xml")},
    )
    assert response.status_code == 413
    assert "limit is" in response.json()["detail"]


def test_health_reports_a_dead_worker(client, monkeypatch):
    from app.main import app as fastapi_app

    monkeypatch.setattr(fastapi_app.state.processor, "_thread", None)
    assert client.get("/api/health/").status_code == 503


def test_nan_attribute_becomes_null():
    from app.geo.readers import _clean_attribute

    assert _clean_attribute(float("nan")) is None
    assert _clean_attribute(float("inf")) is None
    assert _clean_attribute(1.5) == 1.5


def test_stored_document_is_strict_json(client, wait_for_completion):
    """No NaN or Infinity literals reach the stored payload."""
    payload = measure(
        client,
        wait_for_completion,
        "point.kml",
        b"<kml><Document><Placemark><Point><coordinates>0,0</coordinates></Point>"
        b"</Placemark></Document></kml>",
    )

    text = json.dumps(payload)
    assert "NaN" not in text and "Infinity" not in text
    json.loads(text, parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)))
