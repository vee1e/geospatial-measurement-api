#!/usr/bin/env python3
"""Prove the default configuration is byte-identical to the committed code.

Rule 1 of the A/B: with every experiment flag off, behaviour must match the original
exactly. This script extracts `backend/app` from git HEAD into a temp directory,
processes the whole correctness corpus through HEAD's code and through the working
tree (flags off), in two separate processes, and compares the payloads, statuses and
errors for exact equality.

    cd backend && uv run python bench/default_identity.py

Writes bench/results/pipeline-default-identity.json.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from bench.ab_correctness import generate_corpus  # noqa: E402
from bench.common import RESULTS_DIR, dump_json  # noqa: E402

ORIGIN = "HEAD"
DUMP_NAME = "__dump__"


def run_corpus(paths: list[Path]) -> dict:
    """process_file over every corpus file; self-contained so it also runs against
    HEAD's code, which has none of the new config helpers."""
    import shutil as _shutil
    import tempfile as _tempfile

    from app.config import Settings
    from app.db import Database, now_iso
    from app.geo.readers import detect_format
    from app.pipeline import process_file

    out: dict[str, dict] = {}
    for path in paths:
        tmp = Path(_tempfile.mkdtemp(prefix="geo-id-"))
        settings = Settings(
            data_dir=tmp / "data", database_url=tmp / "data" / "geo.db"
        ).prepared()
        db = Database(settings.database_url)
        stored = settings.data_dir / "uploads" / path.name
        stored.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copyfile(path, stored)
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
        _shutil.rmtree(tmp, ignore_errors=True)
    return out


def dump(app_root: Path) -> dict:
    """Process the corpus with `app` resolved from app_root; return results as JSON."""
    # Import bench from the working tree, but resolve `app` from app_root: the path
    # entry carrying app_root wins, and `app.*` is imported lazily inside run_corpus.
    sys.path.insert(0, str(app_root))
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        del sys.modules[name]
    return run_corpus(generate_corpus())


def extract_head(target: Path) -> Path:
    tar = subprocess.run(
        ["git", "-C", str(REPO), "archive", "--format=tar", ORIGIN, "backend/app"],
        capture_output=True, check=True,
    ).stdout
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
        archive.extractall(target)
    return target / "backend"


def run_side(app_root: Path) -> dict:
    env = dict(os.environ)
    for name in (
        "GEO_BATCH_TRANSFORM", "GEO_BOUNDS_SOURCE", "GEO_KML_FAST_PARSE",
        "GEO_MEASURE_IMPL", "GEO_HEADER_LIMIT",
    ):
        env.pop(name, None)
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), DUMP_NAME, str(app_root)],
        capture_output=True, text=True, env=env, timeout=600,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"dump failed for {app_root}:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}"
        )
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    raise SystemExit(f"no JSON from {app_root}: {proc.stdout[-2000:]}")


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == DUMP_NAME:
        print(json.dumps(dump(Path(sys.argv[2]))))
        return

    tmp = Path(tempfile.mkdtemp(prefix="geo-ab-head-"))
    try:
        head_root = extract_head(tmp)
        head = run_side(head_root)
        current = run_side(BACKEND)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    files = sorted(current)
    failures = []
    for name in files:
        a, b = head.get(name), current.get(name)
        if a != b:
            failures.append(name)
            if isinstance(a, dict) and isinstance(b, dict):
                for key in a:
                    if a[key] != b.get(key):
                        failures.append(f"{name}.{key}")
    report = {
        "origin": ORIGIN,
        "files": files,
        "identical": not failures,
        "differences": failures,
    }
    dump_json(RESULTS_DIR / "pipeline-default-identity.json", report)
    print(f"default vs {ORIGIN}: {'IDENTICAL' if not failures else 'DIFFERS'} "
          f"({len(files)} corpus files)")
    for line in failures[:20]:
        print(f"  differs: {line}")
    raise SystemExit(0 if not failures else 1)


if __name__ == "__main__":
    main()
