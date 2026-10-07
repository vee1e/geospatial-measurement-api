"""Shared fixtures.

The app reads its configuration once at import time, so the test environment is set up
before anything imports `app`. Every test talks to one temp database; assertions are
always scoped to the file id created by that test.
"""

from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="geo-api-tests-"))
os.environ["GEO_DATA_DIR"] = str(_TMP / "data")
os.environ["GEO_DATABASE"] = str(_TMP / "data" / "geo.db")
os.environ.pop("GEO_CORS_ORIGINS", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

# A 0.01 degree square at the equator: about 1.11 km on a side, so ~1.23e6 m2.
POLYGON_KML = """
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>block-a</name>
      <ExtendedData>
        <Data name="zone"><value>east</value></Data>
      </ExtendedData>
      <Polygon>
        <outerBoundaryIs><LinearRing><coordinates>
          0.000,0.000 0.010,0.000 0.010,0.010 0.000,0.010 0.000,0.000
        </coordinates></LinearRing></outerBoundaryIs>
      </Polygon>
    </Placemark>
    <Placemark>
      <name>edge-line</name>
      <LineString><coordinates>0.000,0.000 0.010,0.000</coordinates></LineString>
    </Placemark>
    <Placemark>
      <name>survey-pin</name>
      <Point><coordinates>0.005,0.005</coordinates></Point>
    </Placemark>
  </Document>
</kml>
"""

# Same shape, but exported without a namespace: some tools do this.
BROKEN_KML = """
<kml>
  <Document>
    <Placemark><name>bad-ring</name>
      <Polygon><outerBoundaryIs><LinearRing><coordinates>0,0 1,1
        </coordinates></LinearRing></outerBoundaryIs></Polygon>
    </Placemark>
    <Placemark><name>mixed</name>
      <MultiGeometry>
        <Point><coordinates>0.005,0.005</coordinates></Point>
        <Polygon><outerBoundaryIs><LinearRing><coordinates>
          0.000,0.000 0.010,0.000 0.010,0.010 0.000,0.010 0.000,0.000
        </coordinates></LinearRing></outerBoundaryIs></Polygon>
      </MultiGeometry>
    </Placemark>
    <Placemark><name>good</name>
      <LineString><coordinates>0.000,0.000 0.010,0.000</coordinates></LineString>
    </Placemark>
  </Document>
</kml>
"""

EMPTY_KML = "<kml xmlns=\"http://www.opengis.net/kml/2.2\"><Document/></kml>"


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def wait_for_completion(client):
    """Upload returns 202 with PENDING; poll until the worker settles the record."""
    import time

    def _wait(file_id: str, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = client.get(f"/api/files/{file_id}/").json()
            if record["status"] in {"COMPLETED", "FAILED"}:
                return record
            time.sleep(0.05)
        raise AssertionError(f"file {file_id} never finished processing")

    return _wait


def upload(client, filename: str, content: bytes) -> dict:
    response = client.post(
        "/api/files/",
        files={"file": (filename, content, "application/octet-stream")},
    )
    assert response.status_code == 202, response.text
    return response.json()


def write_shapefile_zip(
    destination: Path, *, with_prj: bool = True, corrupt: bool = False
) -> bytes:
    """Build a real Shapefile on disk and zip it, so the reader path is exercised."""
    import shapefile
    from pyproj import CRS

    work = destination / "shp"
    work.mkdir(parents=True, exist_ok=True)
    writer = shapefile.Writer(str(work / "parcels"))
    writer.field("name", "C", 40)
    # A shapefile holds one geometry type per file, so this one is all polygons.
    # Shapefile stores exterior rings clockwise, so the points run clockwise here.
    writer.poly([[[0.0, 0.0], [0.0, 0.01], [0.01, 0.01], [0.01, 0.0], [0.0, 0.0]]])
    writer.record("block-a")
    writer.poly([[[0.02, 0.0], [0.02, 0.01], [0.03, 0.01], [0.03, 0.0], [0.02, 0.0]]])
    writer.record("block-b")
    writer.close()

    if with_prj:
        (work / "parcels.prj").write_text(CRS.from_epsg(4326).to_wkt())

    zip_path = destination / "parcels.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for part in ("shx", "dbf", "prj"):
            file = work / f"parcels.{part}"
            if file.exists():
                archive.write(file, f"parcels.{part}")
        if corrupt:
            archive.writestr("parcels.shp", b"this is not a shapefile")
        else:
            archive.write(work / "parcels.shp", "parcels.shp")

    return zip_path.read_bytes()
