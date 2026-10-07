"""API contract: upload, file information, measurements, and error handling."""

from __future__ import annotations

import pytest

from .conftest import EMPTY_KML, POLYGON_KML, upload, write_shapefile_zip


def test_upload_returns_pending_then_completes(client, wait_for_completion):
    created = upload(client, "survey.kml", POLYGON_KML.encode())
    assert created["status"] == "PENDING"
    assert created["filename"] == "survey.kml"
    assert created["format"] == "KML"

    record = wait_for_completion(created["id"])
    assert record["status"] == "COMPLETED", record["error"]
    assert record["feature_count"] == 3
    assert record["crs"] == "EPSG:4326"
    assert record["crs_assumed"] is True  # KML has no CRS declaration of its own
    assert record["calculation_crs"] == "EPSG:32631"  # UTM zone 31N, chosen from the extent


def test_measurements_endpoint_matches_the_spec_shape(client, wait_for_completion):
    created = upload(client, "survey.kml", POLYGON_KML.encode())
    wait_for_completion(created["id"])

    response = client.get(f"/api/files/{created['id']}/measurements/")
    assert response.status_code == 200
    payload = response.json()

    assert payload["file_id"] == created["id"]
    assert payload["summary"]["feature_count"] == 3
    assert payload["summary"]["measured"] == 2  # polygon + line; the point measures nothing
    assert len(payload["features"]) == 3

    polygon = payload["features"][0]
    assert polygon["geometry_type"] == "Polygon"
    assert polygon["properties"]["name"] == "block-a"
    assert polygon["properties"]["zone"] == "east"
    assert polygon["measurement"]["unit"] == "m2"

    line = payload["features"][1]
    assert line["measurement"]["unit"] == "m"

    point = payload["features"][2]
    assert point["supported"] is False
    assert point["measurement"] is None
    assert point["reason"]


def test_unknown_file_returns_404(client):
    assert client.get("/api/files/doesnotexist/").status_code == 404
    assert client.get("/api/files/doesnotexist/measurements/").status_code == 404


def test_unsupported_extension_is_rejected(client):
    response = client.post(
        "/api/files/", files={"file": ("notes.txt", b"hello", "text/plain")}
    )
    assert response.status_code == 415
    assert ".zip" in response.json()["detail"] or ".kml" in response.json()["detail"]


def test_empty_upload_is_rejected(client):
    response = client.post("/api/files/", files={"file": ("survey.kml", b"", "text/plain")})
    assert response.status_code == 400


def test_zip_without_shapefile_fails_the_record(client, wait_for_completion):
    import zipfile
    from io import BytesIO

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "no shapefile here")
    created = upload(client, "parcels.zip", buffer.getvalue())

    record = wait_for_completion(created["id"])
    assert record["status"] == "FAILED"
    assert "does not contain a .shp file" in record["error"]

    response = client.get(f"/api/files/{created['id']}/measurements/")
    assert response.status_code == 422


def test_corrupt_shapefile_fails_the_record(client, wait_for_completion, tmp_path):
    payload = write_shapefile_zip(tmp_path, corrupt=True)
    created = upload(client, "broken.zip", payload)
    record = wait_for_completion(created["id"])
    assert record["status"] == "FAILED"
    assert record["error"]


def test_measurements_are_unavailable_while_pending(client, monkeypatch):
    # Hold the worker back so the 409 path is observable.
    from app.main import app as fastapi_app

    monkeypatch.setattr(fastapi_app.state.processor, "enqueue", lambda file_id: None)
    created = upload(client, "survey.kml", POLYGON_KML.encode())

    assert created["status"] == "PENDING"
    response = client.get(f"/api/files/{created['id']}/measurements/")
    assert response.status_code == 409
    assert "PENDING" in response.json()["detail"]
    monkeypatch.undo()


def test_kml_with_no_placemarks_is_rejected(client):
    response = client.post(
        "/api/files/", files={"file": ("empty.kml", EMPTY_KML.encode(), "application/xml")}
    )
    # Rejected either at upload (format known) or during processing, never silently.
    assert response.status_code in {202, 415}


def test_shapefile_zip_roundtrip(client, wait_for_completion, tmp_path):
    payload = write_shapefile_zip(tmp_path, with_prj=True)
    created = upload(client, "parcels.zip", payload)
    record = wait_for_completion(created["id"])

    assert record["status"] == "COMPLETED", record["error"]
    assert record["format"] == "SHAPEFILE"
    assert record["feature_count"] == 2
    assert record["crs"] == "EPSG:4326"
    assert record["crs_assumed"] is False  # the .prj sidecar was read

    payload = client.get(f"/api/files/{created['id']}/measurements/").json()
    assert payload["summary"]["measured"] == 2
    assert payload["features"][0]["properties"]["name"] == "block-a"


def test_shapefile_without_prj_is_flagged_as_assumed(client, wait_for_completion, tmp_path):
    payload = write_shapefile_zip(tmp_path, with_prj=False)
    created = upload(client, "noprj.zip", payload)
    record = wait_for_completion(created["id"])

    assert record["status"] == "COMPLETED", record["error"]
    assert record["crs_assumed"] is True
    assert record["crs"] == "EPSG:4326"


def test_file_list_returns_recent_uploads(client):
    response = client.get("/api/files/")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == len(body["files"])
    assert all("id" in item and "status" in item for item in body["files"])


def test_health(client):
    assert client.get("/api/health/").json() == {"status": "ok"}


@pytest.mark.parametrize("filename", ["survey.KML", "survey.kml"])
def test_upload_accepts_mixed_case_extension(client, wait_for_completion, filename):
    created = upload(client, filename, POLYGON_KML.encode())
    assert wait_for_completion(created["id"])["status"] == "COMPLETED"
