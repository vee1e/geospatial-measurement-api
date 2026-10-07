"""E3: the byte limit is enforced inside the write loop.

The unit tests drive `_save_in_chunks` directly so the bytes written to disk can be
observed; the integration test sends a real chunked request with no Content-Length.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.api import files as files_api
from app.config import Settings

from .conftest import POLYGON_KML, upload

MB = 1024 * 1024


class FakeUpload:
    """Reads like starlette's UploadFile: an async read() over prepared chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self._pos = 0

    async def read(self, size: int) -> bytes:
        del size
        if self._pos >= len(self._chunks):
            return b""
        chunk = self._chunks[self._pos]
        self._pos += 1
        return chunk


def _small_limit(tmp_path: Path, limit: int) -> Settings:
    return Settings(data_dir=tmp_path, database_url=tmp_path / "geo.db",
                    max_upload_bytes=limit)


def test_loop_stops_writing_once_the_limit_is_crossed(tmp_path, monkeypatch):
    monkeypatch.setattr(files_api, "settings", _small_limit(tmp_path, limit=2_500_000))
    destination = tmp_path / "out.kml"
    upload_stub = FakeUpload([b"a" * MB, b"b" * MB, b"c" * MB, b"d" * MB])

    with pytest.raises(files_api._UploadTooLarge) as caught:
        asyncio.run(files_api._save_in_chunks(upload_stub, destination))

    # Three chunks read, only the two under the limit hit the disk.
    assert caught.value.seen_bytes == 3 * MB
    assert destination.stat().st_size == 2 * MB
    assert destination.stat().st_size <= 2_500_000


def test_a_body_exactly_at_the_limit_is_written_in_full(tmp_path, monkeypatch):
    monkeypatch.setattr(files_api, "settings", _small_limit(tmp_path, limit=2 * MB))
    destination = tmp_path / "out.kml"
    upload_stub = FakeUpload([b"a" * MB, b"b" * MB])

    written = asyncio.run(files_api._save_in_chunks(upload_stub, destination))

    assert written == 2 * MB  # exactly at the limit: accepted, whole body on disk
    assert destination.stat().st_size == 2 * MB


def test_chunked_request_without_content_length_is_refused(client):
    """No Content-Length at all: the body-size middleware cannot see this one."""
    boundary = "----edgetest"
    head = (
        f"--{boundary}\r\n"
        'content-disposition: form-data; name="file"; filename="chunked.kml"\r\n'
        "content-type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    first = b'<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2">'
    total = 30 * 1024 * 1024

    def body():
        yield head
        yield first
        block = b" " * MB
        remaining = total - len(first)
        while remaining > 0:
            n = min(remaining, len(block))
            yield block[:n]
            remaining -= n
        yield tail

    response = client.post(
        "/api/files/",
        content=body(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert response.status_code == 413
    assert "limit" in response.json()["detail"]


def test_valid_upload_is_unaffected(client, wait_for_completion):
    created = upload(client, "survey.kml", POLYGON_KML.encode())
    record = wait_for_completion(created["id"])
    assert record["status"] == "COMPLETED"
    assert record["size_bytes"] == len(POLYGON_KML.encode())
