# Geospatial File Measurement API

Upload a Shapefile (zip) or a KML, get every feature back with its geometry type, coordinate system, attributes and measurements. Areas come back in square metres, lengths in metres.

Live at https://geo.lverma.com, API at https://geo-api.lverma.com.

## Contents

- [Run it locally](#run-it-locally)
- [API](#api)
- [Architecture](#architecture)
- [Design decisions](#design-decisions)
- [Tests](#tests)
- [Deployment](#deployment)
- [Learning and future scope](#learning-and-future-scope)

## Run it locally

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
cd backend
uv sync                     # creates .venv and installs dependencies
uv run uvicorn app.main:app --reload --port 8000
```

The API is now on http://127.0.0.1:8000, with interactive docs at http://127.0.0.1:8000/docs.

In a second terminal, run the web interface:

```bash
cd frontend
npm install
npm run dev
```

The site is on http://127.0.0.1:5173 and proxies `/api` to the backend.

### Docker

```bash
docker compose up --build
```

This builds `backend/` and stores uploads and the database in `./data`.

## API

Every response is JSON. Errors use `{"detail": ...}`, the shape FastAPI sends by default.

### POST /api/files/

Multipart upload. The field name must be `file`.

```bash
curl -F "file=@survey.zip" http://127.0.0.1:8000/api/files/
```

Returns `202 Accepted` as soon as the file is on disk. Processing happens in the background.

```json
{
  "id": "a63a3d4f062d",
  "filename": "survey.zip",
  "format": "SHAPEFILE",
  "size_bytes": 684,
  "feature_count": 0,
  "crs": null,
  "crs_assumed": false,
  "calculation_crs": null,
  "status": "PENDING",
  "error": null,
  "created_at": "2026-10-07T09:04:48+00:00",
  "completed_at": null
}
```

Rejected uploads:

| Status | When |
| --- | --- |
| `400` | No file, empty file, or no filename |
| `413` | Larger than 25 MB (configurable with `GEO_MAX_UPLOAD_BYTES`) |
| `415` | Not a `.zip` or `.kml` |

### GET /api/files/{id}/

File information, same shape as the upload response plus the results of processing.

```json
{
  "id": "a63a3d4f062d",
  "filename": "survey.kml",
  "format": "KML",
  "size_bytes": 684,
  "feature_count": 3,
  "crs": "EPSG:4326",
  "crs_assumed": true,
  "calculation_crs": "EPSG:32643",
  "status": "COMPLETED",
  "error": null,
  "created_at": "2026-10-07T09:04:48+00:00",
  "completed_at": "2026-10-07T09:04:48+00:00"
}
```

`status` moves `PENDING` → `PROCESSING` → `COMPLETED`, or `FAILED` with a reason in `error`. A file that failed returns that reason on this endpoint too.

### GET /api/files/{id}/measurements/

Measurements for every feature.

```bash
curl http://127.0.0.1:8000/api/files/a63a3d4f062d/measurements/
```

```json
{
  "file_id": "a63a3d4f062d",
  "filename": "survey.kml",
  "format": "KML",
  "source_crs": "EPSG:4326",
  "crs_assumed": true,
  "crs_note": null,
  "calculation_crs": "EPSG:32643",
  "projection_strategy": "utm",
  "summary": {
    "feature_count": 3,
    "measured": 2,
    "unsupported": 1,
    "failed": 0,
    "total_area_m2": 1084263.6587,
    "total_area_km2": 1.084264,
    "total_length_m": 2015.3099,
    "total_length_km": 2.01531,
    "by_geometry_type": { "Polygon": 1, "LineString": 1, "Point": 1 }
  },
  "features": [
    {
      "index": 0,
      "geometry_type": "Polygon",
      "crs": "EPSG:32643",
      "properties": { "name": "field-north", "crop": "wheat" },
      "supported": true,
      "measurement": { "value": 1084263.6587, "unit": "m2" },
      "reason": null,
      "error": null,
      "warnings": []
    }
  ]
}
```

A feature that could not be measured reports why instead of disappearing:

- `"reason": "points have no measurable area or length"` for points
- `"reason": "geometry type is not measurable"` for anything else, such as a mixed `GeometryCollection`
- `"reason": "feature has no geometry"` when a placemark or shape carries no readable geometry
- `"error"` set when that one feature failed, for example `reprojection failed: ...`

Either way the rest of the file still completes.

| Status | When |
| --- | --- |
| `404` | Unknown id |
| `409` | Still `PENDING` or `PROCESSING`; try again shortly |
| `422` | The file finished as `FAILED`; `detail.error` says why |

### GET /api/files/

The 50 most recent uploads, newest first. `?limit=200` caps it at 200.

### GET /api/health/

`{"status": "ok"}`. Used by the container health check.

## Architecture

```
geospatial-measurement-api/
├── backend/
│   ├── app/
│   │   ├── main.py          app wiring, CORS, worker lifecycle
│   │   ├── api/files.py     endpoints and response shapes
│   │   ├── config.py        settings read from the environment
│   │   ├── db.py            SQLite schema and repository
│   │   ├── pipeline.py      read → reproject → measure → store
│   │   ├── worker.py        background queue, one thread
│   │   └── geo/
│   │       ├── readers.py   Shapefile and KML parsing
│   │       ├── crs.py       CRS resolution and projection choice
│   │       └── measure.py   measurement rules, per feature
│   ├── tests/               21 tests over the API and the measurements
│   └── Dockerfile
├── frontend/                Vite site, no framework
├── compose.yaml
└── docs/                    longer notes on design and deployment
```

### File-processing flow

1. `POST /api/files/` checks the extension, size and name, writes the bytes to `data/uploads/{id}.{ext}`, inserts a row with status `PENDING`, and pushes the id onto a queue. The response goes out immediately.
2. A worker thread takes the id, sets `PROCESSING`, and parses the file. A zip must contain exactly one `.shp` plus its sidecars; a KML is parsed with the standard library's XML parser.
3. The parser returns a list of features in the same shape for both formats, so nothing downstream knows which format arrived.
4. The pipeline resolves the source coordinate system, picks one projected coordinate system for the whole file, measures each feature, and writes one JSON document.
5. Status becomes `COMPLETED`, or `FAILED` with the error message when the file itself is unusable.

### Measurement flow

Every feature passes through `geo/measure.py`, which returns a result object and never raises:

- Polygon, MultiPolygon → area in m²
- LineString, MultiLineString → length in m
- Point, MultiPoint → reported with a reason and no value
- Anything else → reported as not measurable

A feature that throws (bad ring, failed reprojection) produces an `error` field on that feature and the loop continues.

### CRS handling

Latitude and longitude are angles. Measuring in degrees gives square degrees and degrees, which are not distances, so the geometry is projected first.

- The source coordinate system comes from a `.prj` sidecar for Shapefiles. KML is defined to use WGS 84. With neither, the configured default (`EPSG:4326`) is used and the response sets `crs_assumed: true` so nobody mistakes a guess for a declaration.
- One projection is chosen for the whole file from the file's extent, so features stay comparable:
  - already projected in metres → measure in place
  - extent fits one UTM zone (6° wide) and latitude is between 80°S and 84°N → that UTM zone
  - anything wider or polar → Lambert Azimuthal Equal Area centred on the extent
- The choice is reported as `calculation_crs` and `projection_strategy`, never hidden.

Worked example: a 0.01° by 0.01° square near Delhi is projected to EPSG:32643 and measures 1,084,264 m². The same square measured naively in degrees would read 0.0001.

## Design decisions

**FastAPI, not Django.** The service is a handful of endpoints over an async-capable framework. Django REST Framework would bring an ORM, admin and settings module that this workload never touches. FastAPI also gives request validation, OpenAPI docs and typed responses from the same code.

**Background worker instead of measuring inside the POST.** Parsing and reprojecting a large layer takes seconds. Holding the request open for that ties client timeouts to file size, and a dropped connection would waste the work. The trade-off: the client polls, so the status endpoint matters. See [docs/design-decisions.md](docs/design-decisions.md) for the alternatives.

**SQLite plus a thin repository, not an ORM.** Two tables, mostly reads. `sqlite3` in WAL mode handles concurrent reads while the worker writes. An ORM would add a mapping layer with nothing to map. Every statement is parameterised.

**Measurements stored as one JSON document per file.** The API always serves them with the file they came from, so one document means one query per request. Storing one row per feature would help only if features were queried alone, which nothing does.

**pyshp plus pyproj plus shapely, no GDAL.** GDAL is the standard tool and reads more formats, but its wheels and system libraries make the image several hundred megabytes and the build slower. pyshp reads Shapefiles, pyproj handles coordinate maths, shapely does the geometry. The image stays under 300 MB. The cost: no GeoJSON, GeoPackage or raster support yet.

**KML parsed with `xml.etree`.** KML is a small, flat format. A dependency-free parser handles placemarks, polygons with holes, and `MultiGeometry`, and it treats a missing namespace the way some exporters write it.

**Uploads on disk, not in the database.** Files are large and read once at processing time. They live under `data/uploads/` next to the SQLite file, which keeps backup to a single directory.

**Isolation per feature.** One bad geometry marks one feature and leaves the file `COMPLETED`. Failing the whole file over one bad ring throws away usable measurements.

**No authentication.** This is a scoped assignment service. Rate limiting, quotas and signed URLs would be the next step before wider use.

## Tests

```bash
cd backend
uv run pytest          # 21 tests, about a second
uv run ruff check .    # lint
```

Coverage of the required paths:

| Requirement | Test |
| --- | --- |
| Creating a job (upload) | `test_upload_returns_pending_then_completes` |
| Input validation | `test_unsupported_extension_is_rejected`, `test_empty_upload_is_rejected`, `test_zip_without_shapefile_fails_the_record` |
| File information | `test_measurements_endpoint_matches_the_spec_shape`, `test_shapefile_zip_roundtrip` |
| Measurements | `test_area_is_real_area_not_square_degrees`, `test_length_is_metres`, `test_projection_is_recorded_per_file` |
| Status and progress | `test_measurements_are_unavailable_while_pending` |
| Individual failure handling | `test_one_bad_feature_does_not_sink_the_file`, `test_corrupt_shapefile_fails_the_record` |
| CRS handling | `test_shapefile_without_prj_is_flagged_as_assumed` |
| Totals | `test_summary_totals_only_count_measured_features` |

`test_area_is_real_area_not_square_degrees` compares the API's answer against the geodesic area computed by `pyproj.Geod` for the same ring, with a 1% tolerance. That catches a projection mistake rather than only checking that a number exists.

## Deployment

Frontend on Vercel, backend on a VPS behind Caddy. Steps, DNS records and the Caddy block are in [docs/deployment.md](docs/deployment.md).

```bash
# backend, on the VPS
cd /srv/geoapi && ./deploy.sh
# frontend, locally
cd frontend && vercel deploy --prod
```

Configuration is read from the environment:

| Variable | Default | Purpose |
| --- | --- | --- |
| `GEO_DATA_DIR` | `./data` | Uploads directory |
| `GEO_DATABASE` | `./data/geo.db` | SQLite file |
| `GEO_CORS_ORIGINS` | empty | Comma-separated allowed origins |
| `GEO_MAX_UPLOAD_BYTES` | `26214400` | Upload size limit |
| `GEO_MAX_ZIP_ENTRIES` | `64` | Entries allowed in a zip |
| `GEO_MAX_ZIP_BYTES` | `209715200` | Uncompressed size limit |
| `GEO_ASSUMED_CRS` | `EPSG:4326` | Used when a file declares no CRS |

## Learning and future scope

What this taught: coordinate systems are where geospatial code goes wrong. The numbers only became trustworthy once the projection choice was explicit, reported in the response, and checked against an independent method. Reading the Shapefile format directly also removed the usual "works on my machine, breaks on the server" problem with native libraries.

What is deliberately missing, in the order it should be added:

1. **GeoJSON and GeoPackage input.** Both come almost free with a GDAL-based reader, and the readers module already returns one shape for every format.
2. **Idempotent uploads.** Hash the file so a re-upload of the same bytes reuses the stored result instead of reprocessing.
3. **Cleanup of old files.** Uploads accumulate on disk. A retention job with a TTL keeps storage bounded.
4. **Authentication and rate limiting.** Required before this is open to more than a reviewer.
5. **A real queue.** One thread is enough for one process. More processes or a shared queue (Redis, RQ) is the step beyond that, and `Processor` is the seam where it would go.
6. **Geometry simplification for very large layers.** A 50 MB Shapefile with a million features takes seconds to project. Simplifying before measurement would cut that, at the cost of precision, and only where the caller asks for it.
