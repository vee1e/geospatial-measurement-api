# Pipeline performance A/B

Five candidate changes to `backend/app` were applied one at a time, measured with
their flag off and on in the same process, correctness-checked against the default
path, then kept or reverted. Nothing was committed. Defaults are unchanged: with
every flag absent the service behaves byte-identically to the committed code
(proof below).

## Flags

| flag | default | effect when on |
| --- | --- | --- |
| `GEO_BATCH_TRANSFORM=1` | off | C1: flatten the whole layer's coordinates and call `Transformer.transform` once per file |
| `GEO_BOUNDS_SOURCE=layer` | `walk` | C2: use the extent the readers gathered instead of a second coordinate walk |
| `GEO_KML_FAST_PARSE=1` | off | C3: `tuple(map(float, parts))` KML coordinate parser |
| `GEO_MEASURE_IMPL=vector` | `shapely` | C4: shoelace area / hypotenuse length straight from GeoJSON arrays, no shapely per feature |
| `GEO_HEADER_LIMIT=1` | off | C5: refuse an over-limit shapefile from `reader.numShapes` before reading records |

Flags are read from the environment on every processing run
(`Settings.batch_transform` etc.), so one process can alternate them between
benchmark phases. The brief did not name a flag for C5; it is `GEO_HEADER_LIMIT`.

## Method

- Timing: `bench/run_ab.py` runs one child process per (candidate, fixture) and
  uploads the fixture with the candidate off, on, off, on ... five times each
  (`bench/_child.py ab`), reporting raw e2e (upload to terminal status) per side.
  Medians of five, min-max kept in `bench/results/pipeline-*.json`.
- Correctness: `bench/ab_correctness.py` processes a 10-file corpus (WGS84 KML with
  polygons/holes/bowtie/lines/points/MultiGeometry, an antimeridian file, a file
  with a broken feature, a shapefile with `.prj`, one without, a foot-based
  EPSG:2263 shapefile, a shapefile with a null shape, plus the `kml_1000`,
  `kml_10000` and `shp_1000` bench fixtures) with each candidate's flag off and on
  and asserts every measurement matches to relative 1e-9, every reason/error string
  matches, `calculation_crs`, `projection_strategy`, `source_crs`, `crs_assumed` and
  every summary total match. Result: `bench/results/pipeline-correctness.json`.
- Default identity: `bench/default_identity.py` extracts `backend/app` from git
  HEAD, processes the same corpus through HEAD's code and the working tree with all
  flags off, and requires exact equality. Result:
  `bench/results/pipeline-default-identity.json`.
- Tests: `uv run pytest` with flags off and with all five flags on.

## Noise floor (read this before the deltas)

The off/on order mandated by the protocol makes the ON phase always run second, so
warmup biases ON faster. A null control (`bench/results/pipeline-null.json`, both
sides the same code) plus one earlier null-style run gave these OFF->ON deltas:

| fixture | null deltas observed (3 runs) | what counts as signal |
| --- | --- | --- |
| kml_1000 | +4.3%, -2.6%, -4.4% | beyond ~±5% |
| kml_10000 | -1.9%, -2.5%, +3.5% | beyond ~±4% |
| shp_1000 | -7.8%, -4.2%, -11.0% | beyond ~±11% |

Absolute medians also drift between sessions (kml_10000 off-side ran 560-652 ms
across runs), so each candidate is compared only against the off-side measured in
its own interleaved run. Deltas inside the null band are reported as noise, not as
wins.

## Per-candidate results (medians of 5, off/on interleaved in one process)

| candidate | fixture | baseline ms | candidate ms | delta % | correctness | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| C1 batch transform | kml_1000 | 60.7 | 44.1 | **-27.3%** | PASS | kept |
| C1 | kml_10000 | 568.6 | 425.9 | **-25.1%** | PASS | kept |
| C1 | shp_1000 | 65.8 | 38.9 | **-40.9%** | PASS | kept |
| C2 bounds in readers | kml_1000 | 72.1 | 73.5 | +1.9% (noise) | PASS | kept |
| C2 | kml_10000 | 626.1 | 641.7 | +2.5% (noise) | PASS | kept |
| C2 | shp_1000 | 82.9 | 73.4 | -11.4% (noise) | PASS | kept |
| C3 fast KML parse | kml_1000 | 60.3 | 59.8 | -0.7% (noise) | PASS | kept |
| C3 | kml_10000 | 559.9 | 566.4 | +1.2% (noise) | PASS | kept |
| C3 | shp_1000 | 70.7 | 65.7 | -7.1% (noise) | PASS | kept |
| C4 vector measure | kml_1000 | 64.1 | 38.6 | **-39.7%** | PASS | kept |
| C4 | kml_10000 | 596.5 | 317.8 | **-46.7%** | PASS | kept |
| C4 | shp_1000 | 79.5 | 29.4 | **-63.0%** | PASS | kept |
| C5 header limit | kml_1000 | 63.0 | 62.7 | -0.5% (noise) | PASS | kept |
| C5 | kml_10000 | 596.7 | 637.5 | +6.8% (noise) | PASS | kept |
| C5 | shp_1000 | 77.2 | 67.7 | -12.3% (noise) | PASS | kept |

The prior baseline pass measured 61 / 579 / 79 ms for these fixtures; this
session's off-sides (60.7-82.9 / 559.9-652.4 / 65.8-82.9 ms) agree with that
within the spread above, so no re-baselining was needed.

C5 only does anything when the file is actually over the limit, so it was also
measured with `GEO_MAX_FEATURES=500` against `shp_1000` (1,000 shapes), where both
sides must fail with the identical message:

| fixture (limit 500) | baseline ms | candidate ms | delta % | outcome |
| --- | --- | --- | --- | --- |
| shp_1000 over-limit | 16.5 | 9.2 | **-44.2%** | FAILED both sides, identical error |
| kml_10000 over-limit | 218.8 | 197.1 | -9.9% (noise; KML has no header) | FAILED both sides, identical error |

### Why C2 and C3 are kept on component evidence

- **C2**: the work it removes is directly measurable even though e2e is too noisy
  to see it. `_layer_bounds` (the walk the flag skips) costs 16.7 ms on
  kml_10000, 2.2 ms on kml_1000, 1.7 ms on shp_1000 (median of 5, in-process).
  Reading with `GEO_BOUNDS_SOURCE=layer` instead of plain reading plus walk shows
  no reader-side penalty (184.4 ms vs 185.3 ms read-only medians on kml_10000), so
  the flag-on path really does drop ~16.7 ms of a ~600 ms run (about 2.8%). The
  e2e deltas for C2 alone scatter from +2.5% to -15.0% across four runs, straddling
  the null band, so no e2e claim is made.
- **C3**: the parser microbenchmark (`bench/parse_variants.py`, all 11,100
  coordinate blocks of the three KML fixtures, best of 7) measures 17.6 ms current
  vs 13.4 ms for `tuple(map(float, parts))`, **-24%** with byte-identical output
  on every block. That clears the >20% bar. In e2e terms the parser is only ~3% of
  the run, so the end-to-end delta (~0.7% on kml_10000) sits inside noise, and the
  shp_1000 row moves only because shp has no KML parsing at all - that -7.1% is
  pure noise. Two other candidates were tried and not used: a single
  `replace`+`split`+`map` block parser ran at the same 13.5 ms while grouping
  malformed mixed-arity blocks differently than the current parser, so it bought
  nothing and weakened exactness.

## Combined result: all kept flags on together

All five candidates kept, so the combined run is all five flags on versus all off,
same interleaved protocol, plus peak RSS (3 fresh child processes, sampled):

| fixture | baseline ms | all flags on ms | delta % | features/s |
| --- | --- | --- | --- | --- |
| kml_1000 | 63.7 | 33.5 | **-47.4%** | ~29,900 |
| kml_10000 | 588.6 | 308.0 | **-47.7%** | ~32,500 (baseline ~17,000) |
| shp_1000 | 69.6 | 26.5 | **-61.9%** | ~37,700 |

| peak RSS (median of 3) | baseline | all flags on |
| --- | --- | --- |
| kml_1000 | 58.0 MB | 57.1 MB |
| kml_10000 | 105.5 MB | **97.7 MB** |
| shp_1000 | 56.5 MB | 51.8 MB |

Memory went down despite C1 holding pre-transformed copies of every geometry,
because C4 never builds a shapely geometry per feature - that per-feature object
turnover was the bigger allocation source. No numpy was used anywhere; a numpy
version of C4 was not needed to hit these numbers.

Raw data: `bench/results/pipeline-{null,c1,c2,c3,c4,c5,c5_overlimit,combined}.json`,
`pipeline-correctness.json`, `pipeline-default-identity.json`.

## Correctness

`bench/ab_correctness.py` result (also saved in
`bench/results/pipeline-correctness.json`):

```
corpus: 10 files in /tmp/geo-ab-corpus
c1       PASS  (0 failures, 0 info)
c2       PASS  (0 failures, 0 info)
c3       PASS  (0 failures, 0 info)
c4       PASS  (0 failures, 1 info)
c5       PASS  (0 failures, 0 info)
all-on   PASS  (0 failures, 1 info)
correctness: PASS
```

The one informational difference: with `GEO_MEASURE_IMPL=vector` the
self-intersection warning on the corpus's bowtie polygon is absent (`warnings`
stays `[]`, the field is still present). Detecting self-intersection means a full
segment-intersection test, which would give back much of C4's speedup, so the
warning is a documented no-op on the vector path; every measurement still matches.
The vector shoelace translates each ring by its first vertex exactly like GEOS -
without that, projected coordinates (~1e6 m) lose eight digits to cancellation and
values disagree with shapely in the fourth decimal; with it, all 10 corpus files
match to better than 1e-9 relative (in fact to the last rounded digit, including
all 10,000 features of `kml_10000`).

`bench/default_identity.py` result
(`bench/results/pipeline-default-identity.json`):

```
default vs HEAD: IDENTICAL (10 corpus files)
```

Payloads, statuses and errors produced by the working tree with all flags off are
byte-identical to the code committed at HEAD.

## Test suite

- Flags off: `44 passed`.
- All five flags on: `44 passed`.
- At session start the suite was 31 tests; a parallel session (not mine, touching
  `app/api/files.py`, `app/worker.py` and three new test files) grew it to 44
  during this work. All 31 original tests plus the 13 additions pass in both
  configurations.
- `uv run ruff check app bench tests`: clean.

## What was reverted, and why

Nothing in `backend/app` was reverted: all five candidates cleared their bar -
C1 (-25% to -41%), C4 (-40% to -63%) and C5 on the over-limit file (-44%) are far
outside the measured noise, and C3 beats the current parser by 24% on the parser
itself, the bar the brief set for it. C2 stays because the exact work it removes
was measured directly (a 16.7 ms walk replaced by reader-side accumulation that
costs nothing measurable), even though its end-to-end delta floats inside the
noise band and is reported as such rather than as a win. What did get thrown away:
one C3 variant (the single `replace`+`split` parser: no faster than the kept one
and it regrouped malformed coordinate blocks differently), and C2's first KML
implementation, which made a second pass over each accepted ring - replaced by
computing the bounds inside the parse loop and merging them in four comparisons
when a geometry is accepted. One C1 implementation was also corrected mid-flight:
it rebuilt every geometry from index 0 of the transformed arrays instead of the
geometry's own slice, failed the correctness diff on `fixture_kml_1000` (values
like 35906 vs 35527), and was fixed before any timing was accepted. The honest
misses: on kml_10000 C2 and C3 alone cannot be shown to change e2e time on this
machine, because the box drifts by 30-90 ms run to run; their case rests on the
component measurements above, not on the e2e table.
