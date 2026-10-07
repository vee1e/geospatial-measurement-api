# Design decisions

The short version lives in the README. This file records the alternatives that were on the table, and what would change the answer.

## 1. Measure in the background, not inside the upload request

Three options:

| Option | What it costs | What it buys |
| --- | --- | --- |
| Measure during `POST /api/files/` | Client timeout is tied to file size; a dropped connection wastes all the work | One request, no polling |
| In-process worker thread (chosen) | The client polls `GET /api/files/{id}/` | Upload returns in milliseconds; parsing and reprojection run off the request path |
| External queue (Redis + RQ/Celery) | Another service to run, monitor and keep alive | Survives a process restart, scales to several workers |

Chosen: in-process worker. The service has one deployment unit, one CPU-bound task and no requirement to survive restarts mid-job. On restart a file stuck in `PROCESSING` is the only casualty, and it is visible in the database.

The seam that makes the swap cheap: `worker.Processor` is the only thing that knows a queue exists. The endpoints call `processor.enqueue(id)`, so replacing it with Redis means changing one class.

## 2. FastAPI over Django REST Framework

The service exposes five endpoints over two tables. DRF would add an ORM, an admin app and a settings module with nothing to configure in them. FastAPI gives request validation, generated OpenAPI docs at `/docs`, and response types from the same Python the handlers use.

The counter-argument is real: if this grew into a product with users, permissions and a background job history, Django's admin and migrations would start paying for themselves. It has not grown into that.

## 3. SQLite over PostgreSQL

Two tables, one writer, many reads. SQLite in WAL mode lets the API read while the worker writes, and the whole state is one file that is easy to copy off the box.

PostgreSQL is the answer as soon as there is more than one process writing, or a requirement for row-level locking, or a second service reading the same data. `db.Database` is the only module that knows which one it is, so the migration is local.

## 4. Measurements as one JSON document per file

The alternative was one row per feature, which lets SQL answer questions like "average area of every polygon across all files" without loading anything into the application.

Nothing does that today. The API always serves the file and its features together, so a single document keeps the read path at one query and the payload shape identical to the response. If per-feature queries appear, a `features` table generated from the same payload is a straightforward addition.

## 5. pyshp, pyproj and shapely instead of GDAL

GDAL reads Shapefile, KML, GeoJSON, GeoPackage, raster formats and more, and it is the standard tool for this job. It is also a large native dependency: wheel size, system libraries and version mismatches between machines are the usual cause of "works locally, fails in the container".

The stack here is pure wheels: pyshp reads the Shapefile binary format, pyproj wraps PROJ for coordinate maths, shapely wraps GEOS for geometry. The image builds in about 35 seconds and runs as a non-root user.

Cost: GeoJSON and GeoPackage input are missing, and reading a shapefile with unusual encodings relies on pyshp rather than GDAL's encoding handling. Adding GDAL later means swapping `geo/readers.py`; the feature shape it returns does not change.

## 6. KML parsed with `xml.etree`

KML is a small XML format: placemarks, a handful of geometry elements, and coordinate strings. A parser in the standard library handles it, including documents that ship without a namespace and `MultiGeometry` blocks.

The deliberate limit: no NetworkLink (KML that points at other files) and no style interpretation. Neither affects measurement.

## 7. Choosing one projection per file

Measured in three ways:

- **Degrees, no projection.** Wrong. A degree of latitude and a degree of longitude are different lengths, so area and length come out in meaningless units.
- **Geodesic measurement** (`pyproj.Geod`) on the WGS 84 ellipsoid. Accurate anywhere, no projection needed. It measures on the ellipsoid rather than in a projected plane, which is the right answer for "how big is this patch of ground" but not for data that will be consumed in a projected coordinate system.
- **Project first, then measure** (chosen), with the projection chosen from the file's extent: a UTM zone when the extent fits in one, an equal-area projection centred on the extent when it does not.

Chosen because the assignment asks for it explicitly, and because reporting `calculation_crs` makes the number auditable. The test suite checks the result against the geodesic answer within 1%, which catches a projection chosen badly enough to distort the result.

Why UTM for small extents: inside a zone, the scale error stays near 0.1%, and every GIS tool on earth can be asked to reproduce the number. Why equal-area beyond a zone: an area figure that is wrong by 5% because the geometry sits 800 km from the central meridian is worse than a length figure that is slightly off.

## 8. One projection for the whole file, not one per feature

Per-feature projection puts each feature in the CRS it sits in, which is marginally more accurate in isolation. It also means two neighbouring features are measured in different coordinate systems, so their numbers are not comparable, and the response has to explain a projection per row.

One CRS per file is simpler to explain, cheaper (one transformer per file) and consistent across the layer.

## 9. Fail a feature, not a file

A Shapefile with one self-intersecting ring is still a useful file. The pipeline traps errors per feature, writes an `error` or `reason` field on that feature, and completes the file. Only a file-level problem, unreadable archive, missing shapefile, unparseable CRS, fails the record.

The mirror of this rule is in the second assignment brief (bulk certificate generation): one bad recipient must not block the rest of the job. The requirement is the same shape, so the handling is the same shape.

## 10. No authentication

Nothing here is secret and the endpoint is a scoped demo. Adding keys would add a key-rotation story, a way to issue them, and a failure mode for the reviewer.

Before real use: a rate limit at the proxy, an upload quota per caller, and short-lived signed URLs for downloads.
