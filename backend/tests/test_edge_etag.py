"""E1: ETag + Cache-Control + 304 on GET /api/files/{id}/measurements/.

The validators are unconditional now, so every test asserts the behaviour directly.
"""

from __future__ import annotations

from .conftest import EMPTY_KML, POLYGON_KML, upload


def _completed_file(client, wait_for_completion) -> str:
    created = upload(client, "survey.kml", POLYGON_KML.encode())
    wait_for_completion(created["id"])
    return created["id"]


def test_200_carries_stable_validators(client, wait_for_completion):
    file_id = _completed_file(client, wait_for_completion)

    response = client.get(f"/api/files/{file_id}/measurements/")
    assert response.status_code == 200
    assert response.headers["etag"] == f'"{file_id}"'
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert response.json()["file_id"] == file_id  # body unchanged


def test_repeat_fetch_with_matching_etag_gets_304(client, wait_for_completion):
    file_id = _completed_file(client, wait_for_completion)
    etag = client.get(f"/api/files/{file_id}/measurements/").headers["etag"]

    response = client.get(
        f"/api/files/{file_id}/measurements/", headers={"If-None-Match": etag}
    )
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == etag
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"


def test_stale_etag_still_gets_the_full_body(client, wait_for_completion):
    file_id = _completed_file(client, wait_for_completion)

    stale = client.get(
        f"/api/files/{file_id}/measurements/", headers={"If-None-Match": '"nope"'}
    )
    assert stale.status_code == 200
    assert stale.json()["file_id"] == file_id

    weak = client.get(
        f"/api/files/{file_id}/measurements/", headers={"If-None-Match": f'W/"{file_id}"'}
    )
    assert weak.status_code == 304  # weak comparison is correct for GET


def test_star_if_none_match_gets_304(client, wait_for_completion):
    file_id = _completed_file(client, wait_for_completion)

    response = client.get(
        f"/api/files/{file_id}/measurements/", headers={"If-None-Match": "*"}
    )
    assert response.status_code == 304
    assert response.content == b""


def test_conflict_responses_carry_no_validators(client, monkeypatch):
    from app.main import app as fastapi_app

    # Hold the worker back like test_api does, so the 409 path is observable.
    monkeypatch.setattr(fastapi_app.state.processor, "enqueue", lambda file_id: None)
    pending = upload(client, "held.kml", POLYGON_KML.encode())

    conflict = client.get(f"/api/files/{pending['id']}/measurements/")
    assert conflict.status_code == 409
    assert "etag" not in conflict.headers
    assert "cache-control" not in conflict.headers


def test_failed_file_response_carries_no_validators(client, wait_for_completion):
    failed = upload(client, "empty.kml", EMPTY_KML.encode())
    wait_for_completion(failed["id"])

    unprocessable = client.get(f"/api/files/{failed['id']}/measurements/")
    assert unprocessable.status_code == 422
    assert "etag" not in unprocessable.headers
    assert "cache-control" not in unprocessable.headers

    not_found = client.get("/api/files/doesnotexist/measurements/")
    assert not_found.status_code == 404
    assert "etag" not in not_found.headers
