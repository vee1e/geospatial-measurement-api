#!/usr/bin/env python3
"""Alternating A/B timings for the pipeline performance candidates.

One child process per (candidate, fixture): the same fixture is uploaded with the
candidate flag OFF, then ON, then OFF ... five times each, in order. Alternating
inside one process makes drift, warmup and cache effects hit both sides equally.

    cd backend
    uv run python bench/run_ab.py            # every candidate in CANDIDATES
    uv run python bench/run_ab.py c1 c4      # only some

Writes bench/results/pipeline-<candidate>.json and prints medians.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.common import BACKEND_DIR, RESULTS_DIR, dump_json, load_manifest, machine_info, summary

PHASES = 5  # per side: off, on, off, on ... => 5 samples each
FIXTURES = ("kml_1000", "kml_10000", "shp_1000")

# Every environment variable the experiment toggles. Stripped from the child
# environment before each run so nothing leaks in from the shell.
FLAG_VARS = (
    "GEO_BATCH_TRANSFORM",
    "GEO_BOUNDS_SOURCE",
    "GEO_KML_FAST_PARSE",
    "GEO_MEASURE_IMPL",
    "GEO_HEADER_LIMIT",
)

# Candidates under test. `flags` are set for the ON phases, `env` is fixed for the
# whole child (a low GEO_MAX_FEATURES, say) and applies to both sides.
CANDIDATES: dict[str, dict] = {
    # Control: the same flags on both sides, so off/on differences here are pure
    # noise plus the systematic "on phase runs second" warmup bias.
    "null": {"flags": {}, "env": {}, "fixtures": FIXTURES},
    "c1": {"flags": {"GEO_BATCH_TRANSFORM": "1"}, "env": {}, "fixtures": FIXTURES},
    "c2": {"flags": {"GEO_BOUNDS_SOURCE": "layer"}, "env": {}, "fixtures": FIXTURES},
    "c3": {"flags": {"GEO_KML_FAST_PARSE": "1"}, "env": {}, "fixtures": FIXTURES},
    "c4": {"flags": {"GEO_MEASURE_IMPL": "vector"}, "env": {}, "fixtures": FIXTURES},
    "c5": {"flags": {"GEO_HEADER_LIMIT": "1"}, "env": {}, "fixtures": FIXTURES},
    # A limit of 500 makes shp_1000 over-limit, the case C5 is aimed at. Both sides
    # must fail with the same message; only the timing may differ.
    "c5_overlimit": {
        "flags": {"GEO_HEADER_LIMIT": "1"},
        "env": {"GEO_MAX_FEATURES": "500"},
        "fixtures": FIXTURES,
    },
    # Everything kept, on together; the flags come from KEPT_FLAGS below.
    "combined": {"flags": {}, "env": {}, "fixtures": FIXTURES},
}

# Candidates that survived their A/B get their flags in here for the combined run.
# All five did: C1 -25..-41%, C2 removes the bounds walk (component-measured),
# C3 -24% on the parser, C4 -40..-63%, C5 -44% on over-limit rejection.
KEPT_FLAGS: dict[str, str] = {
    "GEO_BATCH_TRANSFORM": "1",
    "GEO_BOUNDS_SOURCE": "layer",
    "GEO_KML_FAST_PARSE": "1",
    "GEO_MEASURE_IMPL": "vector",
    "GEO_HEADER_LIMIT": "1",
}


def _child_env(extra: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    for name in FLAG_VARS:
        env.pop(name, None)
    env.update(extra)
    return env


def run_ab_child(fixture: Path, flags: dict[str, str], env: dict[str, str]) -> dict:
    argv = [
        sys.executable,
        str(BACKEND_DIR / "bench" / "_child.py"),
        "ab",
        str(fixture),
        json.dumps(flags),
        str(PHASES),
    ]
    proc = subprocess.run(
        argv, cwd=BACKEND_DIR, capture_output=True, text=True, timeout=900,
        env=_child_env(env),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ab child failed for {fixture.name}:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}"
        )
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError(f"ab child printed no JSON: {proc.stdout[-2000:]}")


def run_rss_child(fixture: Path, flags: dict[str, str]) -> dict:
    argv = [sys.executable, str(BACKEND_DIR / "bench" / "_child.py"), "rss", str(fixture)]
    proc = subprocess.run(
        argv, cwd=BACKEND_DIR, capture_output=True, text=True, timeout=900,
        env=_child_env(flags),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rss child failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}")
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError("rss child printed no JSON")


def bench_candidate(name: str, spec: dict, manifest: dict) -> dict:
    result: dict = {
        "candidate": name,
        "flags_on": spec["flags"],
        "child_env": spec["env"],
        "phases_per_side": PHASES,
        "machine": machine_info(),
        "fixtures": {},
    }
    for fixture_name in spec["fixtures"]:
        path = Path(manifest[fixture_name]["path"])
        print(f"  {name} {fixture_name} (off,on)x{PHASES} ...", flush=True)
        raw = run_ab_child(path, spec["flags"], spec["env"])
        off = [p["e2e_s"] for p in raw["off"]]
        on = [p["e2e_s"] for p in raw["on"]]
        off_statuses = sorted({p["status"] for p in raw["off"]})
        on_statuses = sorted({p["status"] for p in raw["on"]})
        off_errors = sorted({p["error"] for p in raw["off"]})
        on_errors = sorted({p["error"] for p in raw["on"]})
        if off_statuses != on_statuses or off_errors != on_errors:
            raise RuntimeError(
                f"{name} {fixture_name}: outcome differs off={off_statuses}/{off_errors} "
                f"on={on_statuses}/{on_errors}"
            )
        result["fixtures"][fixture_name] = {
            "off": summary(off),
            "on": summary(on),
            "statuses": off_statuses,
            "errors": off_errors,
            "features": manifest[fixture_name]["features"],
        }
    return result


def main() -> None:
    names = sys.argv[1:] or list(CANDIDATES)
    manifest = load_manifest()
    started = time.perf_counter()

    for name in names:
        spec = dict(CANDIDATES[name])
        if name == "combined":
            spec["flags"] = dict(KEPT_FLAGS)
        print(f"== {name} ==", flush=True)
        result = bench_candidate(name, spec, manifest)
        dump_json(RESULTS_DIR / f"pipeline-{name}.json", result)
        for fixture_name, row in result["fixtures"].items():
            off_med = row["off"]["median"] * 1000
            on_med = row["on"]["median"] * 1000
            delta = (on_med - off_med) / off_med * 100
            print(
                f"  {fixture_name}: off {off_med:.1f} ms "
                f"[{row['off']['min']*1000:.1f}-{row['off']['max']*1000:.1f}]  "
                f"on {on_med:.1f} ms "
                f"[{row['on']['min']*1000:.1f}-{row['on']['max']*1000:.1f}]  "
                f"delta {delta:+.1f}%"
            )

    if "combined" in names:
        for fixture_name in FIXTURES:
            runs = [
                run_rss_child(Path(manifest[fixture_name]["path"]), dict(KEPT_FLAGS))
                for _ in range(3)
            ]
            rss = summary([r["peak_rss_sampled_bytes"] for r in runs])
            path = RESULTS_DIR / "pipeline-combined.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.setdefault("peak_rss_bytes", {})[fixture_name] = rss
            dump_json(path, payload)
            print(f"  rss {fixture_name}: {rss['median']/1e6:.1f} MB")

    print(f"done in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
