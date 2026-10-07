#!/usr/bin/env python3
"""Process-level benchmark: RSS, in-process API timings, cProfile, tracemalloc.

Everything runs in fresh child processes (bench/_child.py) so memory peaks and
profiler state are not shared between measurements.

Writes bench/results/process.json.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.common import BACKEND_DIR, RESULTS_DIR, dump_json, load_manifest, summary

RSS_RUNS = 3
INPROC_REPS = 5


def run_child(mode: str, fixture: Path, reps: int | None = None) -> dict:
    argv = [sys.executable, str(BACKEND_DIR / "bench" / "_child.py"), mode, str(fixture)]
    if reps is not None:
        argv.append(str(reps))
    proc = subprocess.run(argv, cwd=BACKEND_DIR, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        raise RuntimeError(
            f"child {mode} {fixture.name} failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}"
        )
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError(f"child {mode} {fixture.name} printed no JSON: {proc.stdout[-2000:]}")


def main() -> None:
    manifest = load_manifest()
    results: dict = {"rss_runs": RSS_RUNS, "inproc_reps": INPROC_REPS}

    for name, info in manifest.items():
        path = Path(info["path"])
        print(f"rss {name} x{RSS_RUNS} ...", flush=True)
        runs = [run_child("rss", path) for _ in range(RSS_RUNS)]
        results.setdefault("rss", {})[name] = {
            "process_s": summary([r["process_s"] for r in runs]),
            "baseline_rss_bytes": summary([r["baseline_rss_bytes"] for r in runs]),
            "peak_rss_bytes": summary([r["peak_rss_sampled_bytes"] for r in runs]),
            "peak_rusage_bytes": summary([r["peak_rss_rusage_bytes"] for r in runs]),
            "features": info["features"],
        }

    for name, info in manifest.items():
        path = Path(info["path"])
        print(f"in-process {name} x{INPROC_REPS} ...", flush=True)
        result = run_child("inproc", path, INPROC_REPS)
        if set(result["statuses"]) != {"COMPLETED"}:
            raise RuntimeError(f"{name}: {result['statuses']}")
        results.setdefault("inproc", {})[name] = {
            "upload_ms": summary(result["upload_ms"]),
            "e2e_s": summary(result["e2e_s"]),
            "info_ms": summary(result["info_ms"]),
            "measurements_ms": summary(result["measurements_ms"]),
            "measurements_bytes": result["measurements_bytes"],
            "features": info["features"],
        }

    for name in ("kml_1000", "kml_10000"):
        print(f"cProfile {name} ...", flush=True)
        results.setdefault("profile", {})[name] = run_child(
            "profile", Path(manifest[name]["path"])
        )

    print("tracemalloc kml_10000 ...", flush=True)
    results["tracemalloc_10k"] = run_child(
        "tracemalloc", Path(manifest["kml_10000"]["path"])
    )

    dump_json(RESULTS_DIR / "process.json", results)
    print(f"wrote {RESULTS_DIR / 'process.json'}")


if __name__ == "__main__":
    main()
