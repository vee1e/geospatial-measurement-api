# Benchmark harness

Baseline performance profile for the geospatial measurement API. Everything lives in
`backend/bench/`; no application code, tests, config or docs are touched.

## Run it in 3 commands

```bash
cd backend
uv run python bench/gen_fixtures.py   # write fixtures to /tmp/geo-bench-fixtures
uv run python bench/run_all.py        # HTTP + process benchmarks -> bench/results/*.json
uv run python bench/render.py         # tables -> bench/BASELINE.md
```

`run_all.py` skips fixture generation if `/tmp/geo-bench-fixtures/manifest.json`
exists. Total wall time on the machine below is about 1 minute.

## What it measures

| script | measurement |
| --- | --- |
| `gen_fixtures.py` | synthetic KML at 100 / 1,000 / 10,000 features (40% polygon, 40% line, 20% point) and a zipped Shapefile of 1,000 polygons, all inside one UTM zone |
| `run_http.py` | cold start (process spawn to `/api/health/` answering), `POST /api/files/` latency, upload to status COMPLETED, `GET /api/files/{id}/`, `GET /api/files/{id}/measurements/` latency and bytes, 8 simultaneous uploads of the 1,000-feature file versus 8 serial ones |
| `run_process.py` | peak RSS per fixture (sampled `ps` plus `getrusage`, 3 runs), in-process timings through `fastapi.testclient` (5 runs), `cProfile` top 12 by cumulative time over `app.pipeline.process_file` for 1,000 and 10,000 features, `tracemalloc` top 8 for 10,000 features |
| `render.py` | turns the results directory into a Markdown report (`--out`), with `--baseline` adding a median-against-median delta table |

Every latency is best of 5, reported as min / median / max. RSS is best of 3.

## The two reports

- `BASELINE.md` is the frozen pre-optimisation profile: rendered from
  `bench/results/http.json` and `process.json`, which stay untouched as the audit
  trail together with the `pipeline-*.json` / `edge-*.json` A/B samples.
- `FINAL.md` is the same tables after the A/B winners landed (no flags, one code
  path). It ran through the same harness with the raw output diverted so nothing
  in `bench/results/` is overwritten:

```bash
cd backend
uv run python bench/gen_fixtures.py              # skipped when the manifest exists
BENCH_RESULTS_DIR="$PWD/bench/results/final" uv run python bench/run_all.py
BENCH_RESULTS_DIR="$PWD/bench/results/final" uv run python bench/render.py \
    --out bench/FINAL.md --title "Final profile (fast paths only, no A/B flags)" \
    --baseline bench/results
```

## How it runs the app

- `fastapi.testclient` drives the app in-process for CPU-bound timings: no socket, no
  uvicorn, same upload and poll flow.
- A real `uvicorn` on `127.0.0.1:8123` serves the HTTP timings, with `GEO_DATA_DIR`
  and `GEO_DATABASE` pointed at a throwaway directory under `/tmp`. Cold start uses a
  fresh directory per run so no run inherits another's database.
- Each RSS, profile and tracemalloc measurement runs in its own child process
  (`_child.py`) so peaks and profiler state never leak between measurements.
- The harness starts and stops every server itself; check with
  `lsof -i :8123` that nothing is left behind.
- `run_http.py` refuses to accept an answer that is not the app's own
  `{"status": "ok"}` from `/api/health/`, and fails if the uvicorn it spawned died,
  so another process squatting on 8123 cannot be mistaken for the service under test.
  It logs any listener it finds on 8123 before it starts.

## Fixtures

Written to `/tmp/geo-bench-fixtures/`, deliberately outside the repo, so nothing large
can be committed. Delete with `rm -rf /tmp/geo-bench-fixtures`. The Shapefile carries
one geometry type per file by specification, so `shp_1000.zip` is all polygons; the KML
files carry the mixed geometry set.

## Machine this ran on

| item | value |
| --- | --- |
| uname | `Darwin Lakshits-MacBook-Pro.local 25.6.0 Darwin Kernel Version 25.6.0: Fri Jul 31 19:16:36 PDT 2026; root:xnu-12377.161.14~5/RELEASE_ARM64_T6030 arm64` |
| cpu count | 11 |
| python | 3.12.12, `backend/.venv/bin/python` via `uv run` |
| dependencies | fastapi 0.142.2, shapely 2.1.2, pyproj 3.8.0, pyshp 3.1.6 |

The current numbers, with spreads, are in `BASELINE.md`; the same tables after the
performance work landed are in `FINAL.md`, which also lists every median delta
against the baseline.
