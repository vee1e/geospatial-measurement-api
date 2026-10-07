# Edge A/B: E1-E4 on the HTTP path

Run 2026-10-07 on this machine. All timings ran alongside another benchmark process
(`sewasetu-handout/bench` spike runs, 40 workers, loadavg 3.9-8.2 during these
measurements), so anything inside a few milliseconds of run-to-run drift is noise.

Method: each condition is a fresh uvicorn on 127.0.0.1:8127 with a fresh temp data
dir; every timing is best-of-5 (min / median / max, raw samples in the JSON) taken in
one tight loop at the end of its section, never interleaved with long work. Raw
results: `bench/results/edge-00-baseline.json`, `edge-e1.json`, `edge-e2.json`,
`edge-e3.json`, `edge-e4.json`, `edge-e4-upload25.json`, `edge-e3-upload25.json`,
`edge-00-upload25-rerun.json`, `edge-all.json`. The harness itself lives outside the
repo (`/tmp/edge/edge_bench.py`); nothing else under `bench/` was touched.

Flags (all read per request or at worker start; unset = today's behaviour):
`GEO_EDGE_ETAG`, `GEO_EDGE_INLINE_LIMIT`, `GEO_WORKER_PROCESSES` (default 1).

## Results

| candidate | baseline | candidate | delta | structure (bytes/IO saved) | kept or reverted |
| --- | --- | --- | --- | --- | --- |
| E1 ETag + 304 | GET 200: 2.29/2.67/3.12 ms over HTTP, 1.50/1.62/2.04 ms in-process, 3,030,007 wire bytes; no validators | 200 path unchanged: 2.32/2.52/3.25 ms HTTP, 1.35/1.66/1.93 ms in-process. Conditional GET: 304 in 0.50/0.56/0.63 ms HTTP, 0.50/0.50/0.54 ms in-process, 158 wire bytes | repeat fetch 3,030,007 -> 158 bytes; server-side median 1.66 -> 0.50 ms (spreads do not overlap) | 3,029,849 bytes not sent per repeat fetch; the 3,029,802-byte SQLite payload is not read (304 returns before `get_measurements_document`) | **kept** (`GEO_EDGE_ETAG`, default off) |
| E2 move the spool | 25 MB POST: 167.8/180.5/194.3 ms (second baseline run 151.3/170.9/200.1 ms) | 170.5/178.9/191.4 ms with the move path in place | none: inside the ~10 ms run-to-run drift | 0 bytes saved - the move is impossible (see below) | **reverted** |
| E3 limit inside the loop | chunked 30 MB body, no Content-Length -> 413, full body written to the data dir first: 66.4/72.1/82.5 ms | 413 in 61.0/72.4/74.3 ms | latency: none measurable (spans overlap) | data-dir write capped at the 26,214,400-byte limit instead of growing with the body: ~5.2 MB saved on this request, unbounded excess saved on a larger one; the unit test shows the destination never exceeds the limit | **kept** (`GEO_EDGE_INLINE_LIMIT`, default off) |
| E4 worker pool | 8 concurrent `kml_1000` uploads, wall to COMPLETED: 0.519/0.532/0.708 s (task's earlier baseline 0.554 s) | 0.135/0.145/0.200 s with a 4-process pool | -0.387 s median, 3.7x; no overlap with the baseline range | processing spread over 4 cores; the serving process stops holding the GIL while earlier uploads parse - visible on its own: 25 MB POST median 170.9 -> 62.9 ms (`edge-e4-upload25.json`) | **kept** (`GEO_WORKER_PROCESSES`, default 1) |

All 40 uploads in every concurrency run reached COMPLETED with 40 distinct ids, and
`tests/test_edge_worker_pool.py` asserts each queue item is submitted to the pool
exactly once, so no file runs twice.

## Mechanism choice for E4

Threads were tried first and lost: 8 files through one thread took 0.44 s, the same
8 files through a 4-thread pool took 0.69 s - the pipeline is CPU-bound Python, so
threads just fight over the GIL. A process pool of 4 did the same 8 files in 0.145 s
(pool creation 4-8 ms with the default start method). So: one dispatcher thread keeps
draining the queue exactly as before and hands each file to `Pool.apply_async` once;
`_thread` still exists and health reporting is unchanged. The pool is only created
when `GEO_WORKER_PROCESSES` is above 1, and it is created during startup, so the
cold-start baseline is untouched with the default.

## Test suite

- defaults, flags off: `cd backend && uv run pytest` -> **44 passed** (31 original
  + 13 new in `tests/test_edge_etag.py`, `tests/test_edge_upload_limit.py`,
  `tests/test_edge_worker_pool.py`)
- all candidates enabled: `GEO_EDGE_ETAG=1 GEO_EDGE_INLINE_LIMIT=1
  GEO_WORKER_PROCESSES=4 uv run pytest` -> **44 passed**
- `ruff check` on every file I changed: clean

## What was reverted and why

E2, the spool move, is impossible on this stack and was reverted. Starlette does
spool multipart bodies over 1 MB to a real file, but `tempfile.TemporaryFile`
unlinks the file at creation: probing the parsed upload through Starlette's own
`MultiPartParser` shows `file.file._rolled == True`, `file.file.name` is the integer
file descriptor (`6`), and `fstat(...).st_nlink == 0` - there is no directory entry
to rename, and no path exists to hand to `shutil.move`. The attempted fast path
therefore never triggered, the 25 MB POST stayed at 170.5/178.9/191.4 ms against
167.8/180.5/194.3 ms baseline (run-to-run drift, recorded in `edge-e2.json` with the
probe facts), and the code was removed again. The only levers left for that copy are
kernel-side (`sendfile` from the spool fd), which still writes every byte to the
data directory; not worth a platform-specific path for a microsecond-class gain.
E3 covers the part of E2 worth keeping: an over-limit request can no longer write
more than the limit to disk. Everything else - E1, E3, E4 - is kept behind its flag,
defaults preserve today's behaviour, and nothing was committed.
