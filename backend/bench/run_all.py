#!/usr/bin/env python3
"""Run the whole harness: HTTP benchmark, then process benchmark.

    uv run python bench/run_all.py

Writes bench/results/http.json and bench/results/process.json. Render the report
with bench/render.py afterwards.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.common import BACKEND_DIR, MANIFEST, RESULTS_DIR


def step(argv: list[str]) -> None:
    print(f"\n=== {' '.join(argv)} ===", flush=True)
    subprocess.run(argv, cwd=BACKEND_DIR, check=True)


def main() -> None:
    started = time.perf_counter()
    if not MANIFEST.exists():
        step([sys.executable, "bench/gen_fixtures.py"])
    step([sys.executable, "bench/run_http.py"])
    step([sys.executable, "bench/run_process.py"])
    print(f"\nall benchmarks done in {time.perf_counter() - started:.1f}s")
    print(f"results in {RESULTS_DIR}")


if __name__ == "__main__":
    main()
