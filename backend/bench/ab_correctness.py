#!/usr/bin/env python3
"""Correctness A/B: run process_file with every experiment flag off, then on, over a
fixed corpus, and prove the payloads agree.

    cd backend
    uv run python bench/ab_correctness.py            # every candidate + all-on
    uv run python bench/ab_correctness.py --force    # regenerate the corpus

Corpus (generated into /tmp/geo-ab-corpus): WGS84 KML polygons/lines/points with
holes, bowtie, MultiGeometry, a shapefile with .prj, one without, a foot-based
EPSG:2263 shapefile, an antimeridian file, a file with a broken feature, plus the
bench fixtures when they exist.

For every file the harness asserts: status and error equal, every measurement value
equal to relative 1e-9, every reason/error string equal, calculation_crs,
projection_strategy, source_crs and crs_assumed equal, and every summary total equal
to relative 1e-9. Warnings are compared too, but reported as information rather than
a failure, because a candidate is allowed to drop the self-intersection warning.

Writes bench/results/pipeline-correctness.json.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.common import RESULTS_DIR, dump_json, load_manifest

CORPUS_DIR = Path("/tmp/geo-ab-corpus")
REL_TOL = 1e-9

try:  # pyshp prints per-shape GeoJSON warnings to stdout
    import shapefile

    shapefile.VERBOSE = False
except Exception:
    pass

FLAG_ENV = {
    "c1": {"GEO_BATCH_TRANSFORM": "1"},
    "c2": {"GEO_BOUNDS_SOURCE": "layer"},
    "c3": {"GEO_KML_FAST_PARSE": "1"},
    "c4": {"GEO_MEASURE_IMPL": "vector"},
    "c5": {"GEO_HEADER_LIMIT": "1"},
}
ALL_ON = {name: value for flags in FLAG_ENV.values() for name, value in flags.items()}
FLAG_VARS = tuple({name for flags in FLAG_ENV.values() for name in flags})


# --- corpus --------------------------------------------------------------------

MIXED_KML = """<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><name>square</name><Polygon><outerBoundaryIs><LinearRing><coordinates>
0.000,0.000 0.010,0.000 0.010,0.010 0.000,0.010 0.000,0.000
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
<Placemark><name>donut</name><Polygon>
<outerBoundaryIs><LinearRing><coordinates>
0.020,0.000 0.050,0.000 0.050,0.030 0.020,0.030 0.020,0.000
</coordinates></LinearRing></outerBoundaryIs>
<innerBoundaryIs><LinearRing><coordinates>
0.030,0.010 0.040,0.010 0.040,0.020 0.030,0.020 0.030,0.010
</coordinates></LinearRing></innerBoundaryIs>
</Polygon></Placemark>
<Placemark><name>bowtie</name><Polygon><outerBoundaryIs><LinearRing><coordinates>
0.060,0.000 0.070,0.010 0.070,0.000 0.060,0.010 0.060,0.000
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
<Placemark><name>run</name><LineString><coordinates>
0.000,0.020 0.010,0.030 0.020,0.025
</coordinates></LineString></Placemark>
<Placemark><name>pin</name><Point><coordinates>0.005,0.005</coordinates></Point></Placemark>
<Placemark><name>two-lines</name><MultiGeometry>
<LineString><coordinates>0.100,0.100 0.110,0.110</coordinates></LineString>
<LineString><coordinates>0.120,0.100 0.130,0.110</coordinates></LineString>
</MultiGeometry></Placemark>
<Placemark><name>two-polys</name><MultiGeometry>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
0.200,0.200 0.210,0.200 0.210,0.210 0.200,0.210 0.200,0.200
</coordinates></LinearRing></outerBoundaryIs></Polygon>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
0.220,0.200 0.230,0.200 0.230,0.210 0.220,0.210 0.220,0.200
</coordinates></LinearRing></outerBoundaryIs></Polygon>
</MultiGeometry></Placemark>
<Placemark><name>mixed</name><MultiGeometry>
<Point><coordinates>0.300,0.300</coordinates></Point>
<LineString><coordinates>0.310,0.310 0.320,0.320</coordinates></LineString>
</MultiGeometry></Placemark>
</Document></kml>
"""

ANTE_KML = """<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><name>dateline-run</name><LineString><coordinates>
178.0,0.0 -179.0,0.0
</coordinates></LineString></Placemark>
<Placemark><name>dateline-block</name><Polygon><outerBoundaryIs><LinearRing><coordinates>
175.0,10.0 179.0,10.0 -176.0,10.0 -176.0,12.0 175.0,12.0 175.0,10.0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
</Document></kml>
"""

BROKEN_KML = """<kml>
<Document>
<Placemark><name>bad-ring</name>
<Polygon><outerBoundaryIs><LinearRing><coordinates>0,0 1,1
</coordinates></LinearRing></outerBoundaryIs></Polygon>
</Placemark>
<Placemark><name>mixed</name>
<MultiGeometry>
<Point><coordinates>0.005,0.005</coordinates></Point>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
0.000,0.000 0.010,0.000 0.010,0.010 0.000,0.010 0.000,0.000
</coordinates></LinearRing></outerBoundaryIs></Polygon>
</MultiGeometry>
</Placemark>
<Placemark><name>good</name><LineString><coordinates>0.000,0.000 0.010,0.000
</coordinates></LineString></Placemark>
</Document></kml>
"""


def _zip(entries: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _shapefile_zip(rings: list[list[list[float]]], names: list[str],
                   prj: str | None, null_at: int | None = None) -> bytes:
    import io

    import shapefile

    writer = shapefile.Writer(shp=io.BytesIO(), dbf=io.BytesIO(), shx=io.BytesIO())
    writer.field("name", "C", 40)
    for index, ring in enumerate(rings):
        if null_at == index:
            writer.null()
        else:
            writer.poly([ring])
        writer.record(names[index])
    writer.close()
    parts = {
        "parcels.shp": writer.shp.getvalue(),
        "parcels.shx": writer.shx.getvalue(),
        "parcels.dbf": writer.dbf.getvalue(),
    }
    if prj is not None:
        parts["parcels.prj"] = prj.encode()
    return _zip(parts)


def generate_corpus(force: bool = False) -> list[Path]:
    from pyproj import CRS

    if CORPUS_DIR.exists() and force:
        shutil.rmtree(CORPUS_DIR)
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)

    def write(name: str, data: bytes) -> Path:
        path = CORPUS_DIR / name
        if not path.exists():
            path.write_bytes(data)
        return path

    paths = [
        write("mixed.kml", MIXED_KML.encode()),
        write("antimeridian.kml", ANTE_KML.encode()),
        write("broken.kml", BROKEN_KML.encode()),
    ]

    wgs_ring = [
        [77.20, 28.61], [77.20, 28.62], [77.21, 28.62], [77.21, 28.61], [77.20, 28.61],
    ]
    wgs_ring2 = [
        [77.22, 28.61], [77.22, 28.63], [77.23, 28.63], [77.23, 28.61], [77.22, 28.61],
    ]
    paths.append(
        write(
            "shp_prj.zip",
            _shapefile_zip([wgs_ring, wgs_ring2], ["a", "b"], CRS.from_epsg(4326).to_wkt()),
        )
    )
    paths.append(write("shp_noprj.zip", _shapefile_zip([wgs_ring], ["a"], None)))

    feet_ring = [
        [100000.0, 200000.0], [100000.0, 201000.0],
        [101000.0, 201000.0], [101000.0, 200000.0], [100000.0, 200000.0],
    ]
    paths.append(
        write(
            "shp_feet.zip",
            _shapefile_zip([feet_ring], ["block"], CRS.from_epsg(2263).to_wkt()),
        )
    )
    paths.append(
        write(
            "shp_null_shape.zip",
            _shapefile_zip([wgs_ring, wgs_ring2], ["a", "b"],
                           CRS.from_epsg(4326).to_wkt(), null_at=1),
        )
    )

    # The large generated fixtures exercise the batch paths with real volume.
    try:
        manifest = load_manifest()
    except FileNotFoundError:
        manifest = {}
    for name in ("kml_1000", "kml_10000", "shp_1000"):
        if name in manifest:
            source = Path(manifest[name]["path"])
            paths.append(write(f"fixture_{source.name}", source.read_bytes()))
    return paths


# --- runner --------------------------------------------------------------------


def run_config(paths: list[Path], flags: dict[str, str]) -> dict:
    from app.config import Settings, clear_flags
    from app.db import Database, now_iso
    from app.geo.readers import detect_format
    from app.pipeline import process_file

    clear_flags()
    os.environ.update(flags)
    out: dict[str, dict] = {}
    for path in paths:
        tmp = Path(tempfile.mkdtemp(prefix="geo-ab-run-"))
        settings = Settings(
            data_dir=tmp / "data", database_url=tmp / "data" / "geo.db"
        ).prepared()
        db = Database(settings.database_url)
        stored = settings.data_dir / "uploads" / path.name
        stored.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, stored)
        record = {
            "id": "f1",
            "filename": path.name,
            "format": detect_format(path.name),
            "size_bytes": path.stat().st_size,
            "status": "PENDING",
            "stored_path": str(stored),
            "created_at": now_iso(),
        }
        db.create_file(record)
        process_file(record["id"], db, settings)
        row = db.get_file(record["id"])
        document = db.get_measurements_document(record["id"])
        out[path.name] = {
            "status": row["status"],
            "error": row["error"],
            "payload": json.loads(document) if document else None,
        }
        shutil.rmtree(tmp, ignore_errors=True)
    clear_flags()
    return out


# --- comparison ----------------------------------------------------------------


def _close(a, b) -> bool:
    if a == b:
        return True
    if a is None or b is None or isinstance(a, bool) or isinstance(b, bool):
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= REL_TOL * max(abs(a), abs(b), 1e-300)
    return False


def compare(off: dict, on: dict) -> tuple[list[str], list[str]]:
    """Returns (failures, informational differences)."""
    failures: list[str] = []
    info: list[str] = []

    for name in sorted(off):
        base, cand = off[name], on.get(name)
        if cand is None:
            failures.append(f"{name}: missing from candidate run")
            continue
        if base["status"] != cand["status"]:
            failures.append(f"{name}: status {base['status']} != {cand['status']}")
        if base["error"] != cand["error"]:
            failures.append(f"{name}: error {base['error']!r} != {cand['error']!r}")
        bp, cp = base["payload"], cand["payload"]
        if bp is None or cp is None:
            if bp != cp:
                failures.append(f"{name}: payload presence {bp is not None} != {cp is not None}")
            continue
        for key in ("source_crs", "crs_assumed", "crs_note", "calculation_crs",
                    "projection_strategy"):
            if bp[key] != cp[key]:
                failures.append(f"{name}: {key} {bp[key]!r} != {cp[key]!r}")
        for key, bval in bp["summary"].items():
            cval = cp["summary"].get(key)
            if isinstance(bval, float) or isinstance(cval, float):
                if not _close(bval, cval):
                    failures.append(f"{name}: summary.{key} {bval} != {cval}")
            elif bval != cval:
                failures.append(f"{name}: summary.{key} {bval} != {cval}")
        if len(bp["features"]) != len(cp["features"]):
            failures.append(f"{name}: feature count {len(bp['features'])} != {len(cp['features'])}")
            continue
        for bf, cf in zip(bp["features"], cp["features"], strict=True):
            tag = f"{name}[{bf['index']} {bf['geometry_type']}]"
            for key in ("geometry_type", "supported", "reason", "error"):
                if bf[key] != cf[key]:
                    failures.append(f"{tag}: {key} {bf[key]!r} != {cf[key]!r}")
            bm, cm = bf["measurement"], cf["measurement"]
            if (bm is None) != (cm is None):
                failures.append(f"{tag}: measurement presence {bm} != {cm}")
            elif bm is not None:
                if bm["unit"] != cm["unit"]:
                    failures.append(f"{tag}: unit {bm['unit']} != {cm['unit']}")
                if not _close(bm["value"], cm["value"]):
                    failures.append(f"{tag}: value {bm['value']} != {cm['value']}")
            if bf["warnings"] != cf["warnings"]:
                info.append(
                    f"{tag}: warnings {bf['warnings']} (baseline) != {cf['warnings']} (candidate)"
                )
    return failures, info


def main() -> None:
    force = "--force" in sys.argv
    paths = generate_corpus(force=force)
    print(f"corpus: {len(paths)} files in {CORPUS_DIR}")

    baseline = run_config(paths, {})
    report: dict = {"corpus": [p.name for p in paths], "rel_tol": REL_TOL, "runs": {}}
    ok = True

    for label, flags in [*sorted(FLAG_ENV.items()), ("all-on", ALL_ON)]:
        result = run_config(paths, flags)
        failures, info = compare(baseline, result)
        report["runs"][label] = {
            "flags": flags,
            "pass": not failures,
            "failures": failures,
            "info": info,
        }
        verdict = "PASS" if not failures else "FAIL"
        print(f"{label:8s} {verdict}  ({len(failures)} failures, {len(info)} info)")
        for line in failures[:20]:
            print(f"    {line}")
        for line in info[:5]:
            print(f"    info: {line}")
        ok = ok and not failures

    dump_json(RESULTS_DIR / "pipeline-correctness.json", report)
    print("correctness:", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
