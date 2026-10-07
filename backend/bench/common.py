"""Shared helpers for the benchmark harness."""

from __future__ import annotations

import os
import platform
import statistics
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BENCH_DIR.parent
# bench/results/ holds the baseline run behind BASELINE.md plus the A/B raw samples:
# the audit trail, never overwritten. Set BENCH_RESULTS_DIR to write a second run's
# http.json / process.json somewhere else (used for the final report in FINAL.md).
_results_env = os.environ.get("BENCH_RESULTS_DIR")
RESULTS_DIR = Path(_results_env).resolve() if _results_env else BENCH_DIR / "results"
FIXTURES_DIR = Path("/tmp/geo-bench-fixtures")
MANIFEST = FIXTURES_DIR / "manifest.json"


def load_manifest() -> dict[str, dict]:
    import json

    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def summary(values: list[float]) -> dict:
    """Best-of-N plus the spread: min, median, max of the raw samples."""
    ordered = sorted(values)
    return {
        "n": len(values),
        "min": ordered[0],
        "median": statistics.median(ordered),
        "max": ordered[-1],
        "raw": values,
    }


def machine_info() -> dict:
    uname = platform.uname()
    return {
        "uname": " ".join((uname.system, uname.node, uname.release, uname.version,
                           uname.machine)),
        "machine": uname.machine,
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "cpu_count": os.cpu_count(),
        "processor": platform.processor(),
    }


def load_json(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, payload: dict) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
