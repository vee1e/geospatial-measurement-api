#!/usr/bin/env python3
"""Isolated measurement child. One process per measurement, so peaks are not shared.

Modes:
  rss          process one fixture directly; report RSS baseline/peak and duration
  tracemalloc  process one fixture under tracemalloc; report peak and top 8 traces
  profile      process one fixture under cProfile; report top 12 by cumulative time
  inproc       drive the app through fastapi.testclient; upload and poll to COMPLETED
  ab           alternating off/on benchmark phases in this one process (run_ab.py)

Usage: python bench/_child.py <mode> <fixture-path> [reps]
Prints one JSON object on stdout.
"""

from __future__ import annotations

import cProfile
import json
import os
import pstats
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
FIXTURES = Path("/tmp/geo-bench-fixtures")

# pyshp prints per-shape GeoJSON warnings to stdout; they would drown the JSON line.
try:
    import shapefile as _shapefile

    _shapefile.VERBOSE = False
except Exception:
    pass

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))  # app.* lives one directory up


def _settings(tmp: Path):
    from app.config import Settings

    settings = Settings(
        data_dir=tmp / "data",
        database_url=tmp / "data" / "geo.db",
    )
    return settings.prepared()


def _prepare(tmp: Path, fixture: Path, create_record: bool = True):
    """Create a database row pointing at a copy of the fixture, as upload would."""
    from app.db import Database, now_iso
    from app.geo.readers import detect_format

    settings = _settings(tmp)
    db = Database(settings.database_url)
    suffix = fixture.suffix.lower()
    destination = settings.data_dir / "uploads" / f"{secrets.token_hex(6)}{suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture, destination)
    if not create_record:
        return settings, db, None
    record = {
        "id": secrets.token_hex(6),
        "filename": fixture.name,
        "format": detect_format(fixture.name),
        "size_bytes": destination.stat().st_size,
        "status": "PENDING",
        "stored_path": str(destination),
        "created_at": now_iso(),
    }
    db.create_file(record)
    return settings, db, record


def _scratch() -> Path:
    return Path(tempfile.mkdtemp(prefix="geo-bench-child-"))


def current_rss_bytes() -> int:
    """Resident set size of this process, in bytes (via ps; macOS has no /proc)."""
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            check=True,
        )
        return int(out.stdout.split()[0]) * 1024
    except Exception:
        return -1


def peak_rss_bytes() -> int:
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return peak  # bytes on macOS
    return peak * 1024  # kilobytes on Linux


def mode_rss(fixture: Path) -> dict:
    from app.pipeline import process_file

    settings, db, record = _prepare(_scratch(), fixture)
    baseline = current_rss_bytes()
    samples: list[int] = []
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            samples.append(current_rss_bytes())
            time.sleep(0.01)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.perf_counter()
    process_file(record["id"], db, settings)
    duration = time.perf_counter() - started
    stop.set()
    sampler.join(timeout=1)

    return {
        "mode": "rss",
        "fixture": fixture.name,
        "process_s": duration,
        "baseline_rss_bytes": baseline,
        "peak_rss_sampled_bytes": max(samples, default=baseline),
        "peak_rss_rusage_bytes": peak_rss_bytes(),
        "baseline_after_child_bytes": current_rss_bytes(),
    }


def mode_tracemalloc(fixture: Path) -> dict:
    """tracemalloc only sees Python allocations, and only what is live at snapshot time.

    A sampler thread takes snapshots during processing and keeps the largest one, so
    the report describes the peak rather than what survived the free at the end.
    """
    from app.pipeline import process_file

    settings, db, record = _prepare(_scratch(), fixture)
    tracemalloc.start(20)
    kept: list = []
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            snapshot = tracemalloc.take_snapshot()
            total = sum(stat.size for stat in snapshot.statistics("lineno"))
            if not kept or total > kept[0][0]:
                kept.append((total, snapshot))
                kept.sort(key=lambda item: item[0], reverse=True)
                del kept[1:]
            stop.wait(0.5)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.perf_counter()
    process_file(record["id"], db, settings)
    duration = time.perf_counter() - started
    stop.set()
    sampler.join(timeout=5)
    current, peak = tracemalloc.get_traced_memory()
    snapshot = kept[0][1] if kept else tracemalloc.take_snapshot()
    top = [
        {
            "size": stat.size,
            "count": stat.count,
            "trace": stat.traceback.format(),
        }
        for stat in snapshot.statistics("lineno")[:8]
    ]
    tracemalloc.stop()
    return {
        "mode": "tracemalloc",
        "fixture": fixture.name,
        "process_s": duration,
        "current_bytes": current,
        "peak_bytes": peak,
        "largest_snapshot_bytes": kept[0][0] if kept else None,
        "top": top,
    }


def mode_ab(fixture: Path, on_env: dict[str, str], phases: int) -> dict:
    """Alternating A/B: upload the fixture with the candidate flags off, then on,
    then off ... `phases` times each, in this one process.

    Alternating inside a single process makes machine drift, interpreter warmup and
    cache effects hit both sides equally. The flags are re-read from the environment
    on every processing run, so flipping os.environ between phases is enough.
    """
    from fastapi.testclient import TestClient

    from app.config import clear_flags

    scratch = _scratch()
    os.environ["GEO_DATA_DIR"] = str(scratch / "data")
    os.environ["GEO_DATABASE"] = str(scratch / "data" / "geo.db")
    _prepare(scratch, fixture, create_record=False)
    from app.main import app

    body = fixture.read_bytes()
    out: dict[str, list] = {"off": [], "on": []}

    with TestClient(app) as client:
        for _ in range(phases):
            for state in ("off", "on"):
                clear_flags()
                if state == "on":
                    os.environ.update(on_env)
                started = time.perf_counter()
                response = client.post(
                    "/api/files/",
                    files={"file": (fixture.name, body, "application/octet-stream")},
                )
                assert response.status_code == 202, response.text
                file_id = response.json()["id"]
                status, finished = _poll(client, file_id)
                record = client.get(f"/api/files/{file_id}/").json()
                out[state].append(
                    {
                        "e2e_s": finished - started,
                        "status": status,
                        "error": record.get("error"),
                    }
                )
    clear_flags()
    return {"mode": "ab", "fixture": fixture.name, "phases": phases, **out}


def _short(path: str) -> str:
    marker = str(BACKEND) + os.sep
    return path.replace(marker, "") if marker in path else path


def mode_profile(fixture: Path) -> dict:
    from app.pipeline import process_file

    settings, db, record = _prepare(_scratch(), fixture)
    profiler = cProfile.Profile()
    started = time.perf_counter()
    profiler.enable()
    process_file(record["id"], db, settings)
    profiler.disable()
    duration = time.perf_counter() - started

    rows = []
    stats = pstats.Stats(profiler).stats
    ranked = sorted(stats.items(), key=lambda item: item[1][3], reverse=True)[:12]
    for (filename, lineno, func), (_cc, nc, tt, ct, _callers) in ranked:
        rows.append(
            {
                "name": f"{_short(filename)}:{lineno} ({func})",
                "cumulative_s": ct,
                "per_call_s": ct / nc if nc else 0.0,
                "tottime_s": tt,
                "ncalls": nc,
            }
        )
    return {
        "mode": "profile",
        "fixture": fixture.name,
        "process_s": duration,
        "total_profiled_s": sum(v[2] for v in stats.values()),
        "total_cumulative_s": max((v[3] for v in stats.values()), default=0.0),
        "top12": rows,
    }


def _poll(client, file_id: str, timeout: float = 120.0) -> tuple[str, float]:
    deadline = time.perf_counter() + timeout
    while True:
        response = client.get(f"/api/files/{file_id}/")
        status = response.json()["status"]
        if status in ("COMPLETED", "FAILED"):
            return status, time.perf_counter()
        if time.perf_counter() > deadline:
            return f"TIMEOUT({status})", time.perf_counter()
        time.sleep(0.005)


def mode_inproc(fixture: Path, reps: int) -> dict:
    from fastapi.testclient import TestClient

    # The app reads the environment once at import time, so it is set before anything
    # under app.* is imported.
    scratch = _scratch()
    os.environ["GEO_DATA_DIR"] = str(scratch / "data")
    os.environ["GEO_DATABASE"] = str(scratch / "data" / "geo.db")
    _settings, _db, _ = _prepare(scratch, fixture, create_record=False)
    from app.main import app

    upload_ms: list[float] = []
    e2e_s: list[float] = []
    info_ms: list[float] = []
    measurements_ms: list[float] = []
    measurement_bytes = 0
    statuses: list[str] = []

    with TestClient(app) as client:
        for _ in range(reps):
            started = time.perf_counter()
            response = client.post(
                "/api/files/",
                files={"file": (fixture.name, fixture.read_bytes(),
                                "application/octet-stream")},
            )
            assert response.status_code == 202, response.text
            upload_ms.append((time.perf_counter() - started) * 1000)
            file_id = response.json()["id"]

            status, finished = _poll(client, file_id)
            statuses.append(status)
            e2e_s.append(finished - started)

            t0 = time.perf_counter()
            info = client.get(f"/api/files/{file_id}/")
            info_ms.append((time.perf_counter() - t0) * 1000)
            assert info.status_code == 200

            t0 = time.perf_counter()
            measurements = client.get(f"/api/files/{file_id}/measurements/")
            measurements_ms.append((time.perf_counter() - t0) * 1000)
            assert measurements.status_code == 200
            measurement_bytes = len(measurements.content)

    return {
        "mode": "inproc",
        "fixture": fixture.name,
        "reps": reps,
        "statuses": statuses,
        "upload_ms": upload_ms,
        "e2e_s": e2e_s,
        "info_ms": info_ms,
        "measurements_ms": measurements_ms,
        "measurements_bytes": measurement_bytes,
    }


def main() -> None:
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    mode, fixture = sys.argv[1], Path(sys.argv[2])
    if mode == "ab":
        # argv: ab <fixture> <on-env-json> <phases>
        result = mode_ab(fixture, json.loads(sys.argv[3]), int(sys.argv[4]))
        print(json.dumps(result))
        return
    reps = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    if mode == "rss":
        result = mode_rss(fixture)
    elif mode == "tracemalloc":
        result = mode_tracemalloc(fixture)
    elif mode == "profile":
        result = mode_profile(fixture)
    elif mode == "inproc":
        result = mode_inproc(fixture, reps)
    else:
        raise SystemExit(f"unknown mode {mode}")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
