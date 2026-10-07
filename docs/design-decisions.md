# Design decisions

The short version lives in the README. This file keeps only the decisions where the
alternative was a genuine trade-off worth recording.

## 1. Measure in the background, not inside the upload request

Three options:

| Option | What it costs | What it buys |
| --- | --- | --- |
| Measure during `POST /api/files/` | Client timeout is tied to file size; a dropped connection wastes all the work | One request, no polling |
| In-process dispatcher plus a process pool (chosen) | The client polls `GET /api/files/{id}/` | Upload returns in milliseconds; parsing and reprojection run off the request path, two files at a time by default |
| External queue (Redis + RQ/Celery) | Another service to run, monitor and keep alive | Survives a process restart, scales to several workers |

Chosen: in-process work. The service has one deployment unit and no requirement to
survive restarts mid-job. Records left `PENDING` or `PROCESSING` by a restart are
re-queued at startup, which is the part of the external queue this actually needed.

The work does not run on the serving thread. One dispatcher thread drains the queue
and submits each file once to a pool of separate processes, because the pipeline is
CPU-bound Python and extra threads in one process made it slower (8 files: 0.44 s in
one thread, 0.69 s in four; the same 8 files in a 4-process pool took 0.145 s against
0.532 s single-worker). `GEO_WORKER_PROCESSES` sets the pool size, default 2, and `1`
falls back to the original single inline worker.

The seam that makes the swap cheap: `worker.Processor` is the only thing that knows a
queue exists. The endpoints call `processor.enqueue(id)`, so replacing it with Redis
means changing one class.

## 2. SQLite over PostgreSQL

Two tables, one writer, many reads. SQLite in WAL mode lets the API read while the
worker writes, and the whole state is one file that is easy to copy off the box.

PostgreSQL is the answer as soon as there is more than one process writing, or a
requirement for row-level locking, or a second service reading the same data.
`db.Database` is the only module that knows which one it is, so the migration is local.

## 3. Measurements as one JSON document per file

The alternative was one row per feature, which lets SQL answer questions like "average
area of every polygon across all files" without loading anything into the application.

Nothing does that today. The API always serves the file and its features together, so a
single document keeps the read path at one query and the payload shape identical to the
response. The endpoint serves those stored bytes directly rather than parsing and
re-serialising them, because a document can reach tens of megabytes. If per-feature
queries appear, a `features` table generated from the same payload is a straightforward
addition.

## 4. Choosing one projection per file

Measured in three ways:

- **Degrees, no projection.** Wrong. A degree of latitude and a degree of longitude are
  different lengths, so area and length come out in meaningless units.
- **Geodesic measurement** (`pyproj.Geod`) on the WGS 84 ellipsoid. Accurate anywhere, no
  projection needed. It measures on the ellipsoid rather than in a projected plane, which
  is the right answer for "how big is this patch of ground" but not for data that will be
  consumed in a projected coordinate system.
- **Project first, then measure** (chosen), with the projection chosen from the file's
  extent: a UTM zone when the extent falls inside one, an equal-area projection centred
  on the extent when it does not.

Chosen because the assignment asks for it explicitly, and because reporting
`calculation_crs` makes the number auditable. The test suite checks the result against
the geodesic answer within 1%, which catches a projection chosen badly enough to distort
the result.

Why UTM for small extents: inside a zone, the scale error stays near 0.1%, and every GIS
tool on earth can be asked to reproduce the number. Why equal-area beyond a zone: an area
figure that is wrong by 5% because the geometry sits 800 km from the central meridian is
worse than a length figure that is slightly off.

Two edges this had to handle after review: a source projection in feet is converted
through its axis unit instead of being labelled square metres, and an extent that
crosses the antimeridian is centred with a circular mean of longitudes so the projection
does not end up on the opposite side of the planet.

## 5. One projection for the whole file, not one per feature

Per-feature projection puts each feature in the CRS it sits in, which is marginally more
accurate in isolation. It also means two neighbouring features are measured in different
coordinate systems, so their numbers are not comparable, and the response has to explain
a projection per row.

One CRS per file is simpler to explain, cheaper (one transformer per file) and consistent
across the layer.
