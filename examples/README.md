# Example files

Nine small files that each exercise one behaviour. Upload any of them at
https://geo.lverma.com or straight at the API.

```bash
curl -F "file=@examples/delhi-parcels.kml" https://geo.lverma.com/api/files/
```

The upload returns an `id`; swap it into the two read endpoints:

```bash
curl https://geo.lverma.com/api/files/<id>/
curl https://geo.lverma.com/api/files/<id>/measurements/
```

| File | Exercises | What you should see |
| --- | --- | --- |
| `delhi-parcels.kml` | Happy path: two polygons (one with a hole), a line, a point, attributes | `COMPLETED`, 4 features, 3 measured, ~1.99e6 m², `EPSG:4326 → EPSG:32643`, point reported as "points have no measurable area or length" |
| `delhi-parcels.zip` | Zipped Shapefile with a `.prj` | `COMPLETED`, `crs_assumed: false`, ~1.08e6 m² |
| `no-crs-parcels.zip` | Shapefile with no `.prj` sidecar | `crs_assumed: true`, `crs_note: "no .prj sidecar found; source CRS will be assumed"` |
| `feet-grid.zip` | NAD83 / New York Long Island in US survey feet | `EPSG:2263`, `projection_strategy: "source"`, **23,226 m²** (a naive read gives 250,000) |
| `finder-export.zip` | Zip made by macOS Finder, carrying `__MACOSX/._parcels.shp` | `COMPLETED`, not rejected as "2 shapefiles" |
| `geometry-only.zip` | `.shp` + `.shx` with no `.dbf` | `COMPLETED`, empty properties, `crs_note` mentions the missing `.dbf` |
| `partial-failures.kml` | Two unmeasurable features beside a good line | `COMPLETED` anyway: 1 measured, 2 not, each with its own `reason` |
| `antimeridian.kml` | A line from 178°E to 179°W | `projection_strategy: "laea"`, length **333,949 m** (3° at the equator; a broken centre gives ~1.2e6) |
| `not-a-real.zip` | Text wearing a `.zip` extension | `415 "'not-a-real.zip' does not look like a zip file"`, refused at upload |

## Regenerating

```bash
cd examples
../backend/.venv/bin/python generate.py   # needs the backend venv: pyshp and pyproj
```

The outputs are committed, so you only need this after editing the fixtures.
