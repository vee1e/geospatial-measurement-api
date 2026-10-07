"""Measurement correctness: units, projection choice, and failure isolation."""

from __future__ import annotations

import math

import pytest
from pyproj import Geod

from .conftest import BROKEN_KML, POLYGON_KML, upload


def _measurements(client, wait_for_completion, filename: str, content: bytes) -> dict:
    created = upload(client, filename, content)
    record = wait_for_completion(created["id"])
    assert record["status"] == "COMPLETED", record["error"]
    response = client.get(f"/api/files/{created['id']}/measurements/")
    assert response.status_code == 200
    return response.json()


def test_area_is_real_area_not_square_degrees(client, wait_for_completion):
    payload = _measurements(client, wait_for_completion, "survey.kml", POLYGON_KML.encode())
    area = payload["features"][0]["measurement"]["value"]

    # A degree of latitude is ~111 km, so the naive degree-based answer would be
    # 0.0001 "square degrees". Check the projected result against the geodesic truth.
    ring = [(0.0, 0.0), (0.01, 0.0), (0.01, 0.01), (0.0, 0.01), (0.0, 0.0)]
    geodesic_area, _ = Geod(ellps="WGS84").polygon_area_perimeter(
        [p[0] for p in ring], [p[1] for p in ring]
    )
    geodesic_area = abs(geodesic_area)

    assert area > 1_000_000, "area looks like it was measured in degrees"
    assert math.isclose(area, geodesic_area, rel_tol=0.01), (area, geodesic_area)


def test_length_is_metres(client, wait_for_completion):
    payload = _measurements(client, wait_for_completion, "survey.kml", POLYGON_KML.encode())
    line = payload["features"][1]["measurement"]

    assert line["unit"] == "m"
    # 0.01 degrees of longitude at the equator is about 1113 m.
    assert 1100 < line["value"] < 1130


def test_point_reports_no_measurement(client, wait_for_completion):
    payload = _measurements(client, wait_for_completion, "survey.kml", POLYGON_KML.encode())
    point = payload["features"][2]

    assert point["geometry_type"] == "Point"
    assert point["supported"] is False
    assert point["measurement"] is None
    assert "no measurable" in point["reason"]


def test_projection_is_recorded_per_file(client, wait_for_completion):
    payload = _measurements(client, wait_for_completion, "survey.kml", POLYGON_KML.encode())

    assert payload["projection_strategy"] == "utm"
    assert payload["calculation_crs"] == "EPSG:32631"
    assert all(feature["crs"] == "EPSG:32631" for feature in payload["features"])


def test_one_bad_feature_does_not_sink_the_file(client, wait_for_completion):
    payload = _measurements(client, wait_for_completion, "broken.kml", BROKEN_KML.encode())

    features = {f["properties"].get("name"): f for f in payload["features"]}

    # A polygon ring with two points cannot become a geometry: reported, not raised.
    assert features["bad-ring"]["supported"] is False
    assert features["bad-ring"]["measurement"] is None
    assert features["bad-ring"]["reason"] == "feature has no geometry"

    # A mixed MultiGeometry (GeometryCollection) is unsupported but still listed.
    assert features["mixed"]["geometry_type"] == "GeometryCollection"
    assert features["mixed"]["supported"] is False
    assert features["mixed"]["measurement"] is None
    assert features["mixed"]["reason"] == "geometry type is not measurable"

    # The healthy feature still measures.
    good = features["good"]
    assert good["supported"] is True
    assert good["measurement"]["unit"] == "m"
    assert payload["summary"]["measured"] == 1
    assert payload["summary"]["unsupported"] == 2
    assert payload["summary"]["failed"] == 0


def test_summary_totals_only_count_measured_features(client, wait_for_completion):
    payload = _measurements(client, wait_for_completion, "survey.kml", POLYGON_KML.encode())
    summary = payload["summary"]

    def total(unit: str) -> float:
        return sum(
            feature["measurement"]["value"]
            for feature in payload["features"]
            if feature["measurement"] and feature["measurement"]["unit"] == unit
        )

    # Totals must equal the sum over the listed features exactly: a feature counted
    # twice, or an unsupported one leaking in, breaks this.
    assert summary["total_area_m2"] == pytest.approx(total("m2"), rel=1e-9)
    assert summary["total_length_m"] == pytest.approx(total("m"), rel=1e-9)

    polygon_area = payload["features"][0]["measurement"]["value"]
    line_length = payload["features"][1]["measurement"]["value"]
    assert summary["total_area_m2"] == pytest.approx(polygon_area, rel=1e-6)
    assert summary["total_length_m"] == pytest.approx(line_length, rel=1e-6)
    assert summary["total_area_km2"] == pytest.approx(polygon_area / 1e6, rel=1e-6)
    assert summary["by_geometry_type"] == {"Polygon": 1, "LineString": 1, "Point": 1}
