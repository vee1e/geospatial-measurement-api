#!/usr/bin/env python3
"""HTTP benchmark against a real uvicorn on 127.0.0.1:8123.

Measures, best of 5 each:
  - cold start: process spawn -> /api/health/ answers (fresh temp data dir per run)
  - POST /api/files/ latency per fixture
  - upload -> status COMPLETED per fixture (the real work)
  - GET /api/files/{id}/ latency per fixture
  - GET /api/files/{id}/measurements/ latency and response bytes for the 10k file
  - 8 simultaneous uploads of the 1,000-feature file vs 8 serial uploads

Writes bench/results/http.json. The server is always terminated, even on failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from bench.common import BACKEND_DIR, RESULTS_DIR, dump_json, load_manifest, summary

HOST = "127.0.0.1"
PORT = 8123
BASE = f"http://{HOST}:{PORT}"
POLL_S = 0.005
COLD_START_RUNS = 5
REPS = 5


def server_env(data_dir: Path) -> dict:
    env = dict(os.environ)
    env["GEO_DATA_DIR"] = str(data_dir)
    env["GEO_DATABASE"] = str(data_dir / "geo.db")
    env.pop("GEO_CORS_ORIGINS", None)
    return env


def start_server(data_dir: Path, log_path: Path) -> subprocess.Popen:
    log = log_path.open("w")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", HOST, "--port", str(PORT), "--log-level", "warning"],
        cwd=BACKEND_DIR,
        env=server_env(data_dir),
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def stop_server(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def wait_healthy(timeout: float) -> float | None:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        try:
            response = httpx.get(f"{BASE}/api/health/", timeout=2.0)
            # Must be the app's own answer, not some other listener on 8123.
            if response.status_code == 200 and response.json().get("status") == "ok":
                return time.perf_counter()
        except Exception:
            pass
        time.sleep(POLL_S)
    return None


def port_report() -> list[str]:
    """Listeners already on 8123 before this run starts, for the log."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", "-iTCP:8123", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return []
    return [line for line in out.stdout.splitlines()[1:] if line.strip()]


def assert_server(proc: subprocess.Popen) -> None:
    """The health check answered; make sure it was the process we spawned."""
    if proc.poll() is not None:
        raise RuntimeError(
            f"spawned uvicorn exited with {proc.returncode} before it answered health"
        )


def measure_cold_start() -> dict:
    samples: list[float] = []
    for run in range(COLD_START_RUNS):
        tmp = Path(tempfile.mkdtemp(prefix=f"geo-bench-cold-{run}-"))
        proc = None
        try:
            started = time.perf_counter()
            proc = start_server(tmp, tmp / "server.log")
            healthy = wait_healthy(timeout=60)
            if healthy is None:
                raise RuntimeError(f"server never became healthy; see {tmp}/server.log")
            assert_server(proc)
            samples.append(healthy - started)
        finally:
            stop_server(proc)
            shutil.rmtree(tmp, ignore_errors=True)
    return summary(samples)


def upload(client: httpx.Client, path: Path) -> tuple[str, float, float]:
    """Returns (file_id, upload latency in ms, time the upload request started)."""
    started = time.perf_counter()
    with path.open("rb") as handle:
        response = client.post(
            "/api/files/",
            files={"file": (path.name, handle, "application/octet-stream")},
        )
    finished = time.perf_counter()
    if response.status_code != 202:
        raise RuntimeError(f"upload failed: {response.status_code} {response.text[:200]}")
    return response.json()["id"], (finished - started) * 1000, started


def wait_completed(client: httpx.Client, file_id: str, timeout: float = 300.0) -> tuple[str, float]:
    deadline = time.perf_counter() + timeout
    while True:
        status = client.get(f"/api/files/{file_id}/").json()["status"]
        if status in ("COMPLETED", "FAILED"):
            return status, time.perf_counter()
        if time.perf_counter() > deadline:
            return f"TIMEOUT({status})", time.perf_counter()
        time.sleep(POLL_S)


def bench_fixture(client: httpx.Client, name: str, path: Path) -> dict:
    upload_ms: list[float] = []
    e2e_s: list[float] = []
    info_ms: list[float] = []
    last_id = ""
    for _ in range(REPS):
        file_id, ms, started = upload(client, path)
        status, finished = wait_completed(client, file_id)
        if status != "COMPLETED":
            raise RuntimeError(f"{name}: {status}")
        e2e_s.append(finished - started)
        upload_ms.append(ms)
        last_id = file_id

    for _ in range(REPS):
        t0 = time.perf_counter()
        response = client.get(f"/api/files/{last_id}/")
        info_ms.append((time.perf_counter() - t0) * 1000)
        if response.status_code != 200:
            raise RuntimeError("file info GET failed")

    return {
        "size_bytes": path.stat().st_size,
        "upload_ms": summary(upload_ms),
        "e2e_s": summary(e2e_s),
        "info_ms": summary(info_ms),
        "file_id": last_id,
    }


def bench_measurements(client: httpx.Client, file_id: str) -> dict:
    latency: list[float] = []
    size = 0
    for _ in range(REPS):
        t0 = time.perf_counter()
        response = client.get(f"/api/files/{file_id}/measurements/")
        latency.append((time.perf_counter() - t0) * 1000)
        if response.status_code != 200:
            raise RuntimeError("measurements GET failed")
        size = len(response.content)
    return {"ms": summary(latency), "bytes": size}


def bench_concurrency(path: Path) -> dict:
    """8 uploads of the same fixture at once, then 8 uploads one after the other."""
    with httpx.Client(base_url=BASE, timeout=60.0) as probe:
        probe.get("/api/health/").raise_for_status()

    def one_upload() -> dict:
        with httpx.Client(base_url=BASE, timeout=60.0) as client:
            file_id, upload_ms, started = upload(client, path)
            status, finished = wait_completed(client, file_id)
        return {"file_id": file_id, "upload_ms": upload_ms, "status": status,
                "e2e_s": finished - started}

    wall_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        concurrent = list(pool.map(lambda _: one_upload(), range(8)))
    concurrent_wall = time.perf_counter() - wall_started

    serial_started = time.perf_counter()
    serial = [one_upload() for _ in range(8)]
    serial_wall = time.perf_counter() - serial_started

    return {
        "concurrent": {
            "wall_s": concurrent_wall,
            "upload_ms": summary([r["upload_ms"] for r in concurrent]),
            "e2e_s": summary([r["e2e_s"] for r in concurrent]),
            "statuses": [r["status"] for r in concurrent],
        },
        "serial": {
            "wall_s": serial_wall,
            "upload_ms": summary([r["upload_ms"] for r in serial]),
            "e2e_s": summary([r["e2e_s"] for r in serial]),
            "statuses": [r["status"] for r in serial],
        },
    }


def main() -> None:
    manifest = load_manifest()
    results: dict = {"host": BASE, "poll_s": POLL_S, "reps": REPS}

    listeners = port_report()
    if listeners:
        print("warning: something already listens on 8123:\n  " + "\n  ".join(listeners))

    print(f"cold start x{COLD_START_RUNS} ...", flush=True)
    results["cold_start_s"] = measure_cold_start()

    tmp = Path(tempfile.mkdtemp(prefix="geo-bench-http-"))
    proc = None
    try:
        proc = start_server(tmp, tmp / "server.log")
        if wait_healthy(timeout=60) is None:
            raise RuntimeError(f"server never became healthy; see {tmp}/server.log")
        assert_server(proc)
        results["data_dir"] = str(tmp)

        with httpx.Client(base_url=BASE, timeout=120.0) as client:
            for name, info in manifest.items():
                print(f"fixture {name} x{REPS} ...", flush=True)
                results.setdefault("fixtures", {})[name] = bench_fixture(
                    client, name, Path(info["path"])
                )
                results["fixtures"][name]["features"] = info["features"]

            big = results["fixtures"]["kml_10000"]
            print("measurements GET (10k) ...", flush=True)
            results["measurements_10k"] = bench_measurements(client, big["file_id"])

        print("concurrency 8x kml_1000 ...", flush=True)
        results["concurrency"] = bench_concurrency(Path(manifest["kml_1000"]["path"]))
    finally:
        stop_server(proc)
        shutil.rmtree(tmp, ignore_errors=True)

    dump_json(RESULTS_DIR / "http.json", results)
    print(f"wrote {RESULTS_DIR / 'http.json'}")


if __name__ == "__main__":
    # The app turns on INFO logging at import; keep child-process noise out of the JSON.
    import logging

    logging.getLogger().setLevel(logging.WARNING)
    main()
