#!/usr/bin/env python3
"""Micro-benchmark for the C3 KML coordinate parser candidates.

Extracts every <coordinates> block from the bench fixtures, checks each candidate
produces byte-identical output to the current parser, then times them.

    cd backend && uv run python bench/parse_variants.py
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.geo.readers import _parse_coordinates

FIXTURES = Path("/tmp/geo-bench-fixtures")
BLOCK = re.compile(rb"<coordinates>(.*?)</coordinates>", re.S)


def current(text, acc=None):
    return _parse_coordinates(text, acc)


def variant_map(text, acc=None):
    """Same loop, tuple(map(float, parts)) instead of a genexp."""
    points = []
    append = points.append
    for chunk in (text or "").split():
        parts = chunk.split(",")
        if len(parts) < 2:
            continue
        try:
            append(tuple(map(float, parts)))
        except ValueError:
            continue
    return points


def variant_comp(text, acc=None):
    """List comprehension with a whole-block fallback for malformed input."""
    try:
        points = [
            point
            for point in (
                tuple(map(float, chunk.split(","))) for chunk in (text or "").split()
            )
            if len(point) >= 2
        ]
    except ValueError:
        return current(text, acc)
    return points


def variant_flat(text, acc=None):
    """One replace + one split + one map; per-chunk shape checked from the totals.

    Falls back to the current parser whenever the block is not uniform: first chunk
    with fewer than two values, or a comma count that does not match every chunk
    having the same arity. Malformed blocks that still pass those two checks would
    group differently; real-world KML blocks are uniform by specification.
    """
    text = text or ""
    chunks = text.split()
    if not chunks:
        return []
    arity = chunks[0].count(",") + 1
    if arity < 2:
        return current(text, None)
    if text.count(",") != len(chunks) * (arity - 1):
        return current(text, None)
    values = text.replace(",", " ").split()
    try:
        floats = list(map(float, values))
    except ValueError:
        return current(text, None)
    if len(floats) != len(chunks) * arity:
        return current(text, None)
    if arity == 2:
        it = iter(floats)
        return [(x, y) for x, y in zip(it, it, strict=True)]
    return [tuple(floats[i : i + arity]) for i in range(0, len(floats), arity)]


VARIANTS = [current, variant_map, variant_comp, variant_flat]


def load_blocks() -> list[str]:
    blocks: list[str] = []
    for name in ("kml_100.kml", "kml_1000.kml", "kml_10000.kml"):
        data = (FIXTURES / name).read_bytes()
        blocks.extend(m.group(1).decode() for m in BLOCK.finditer(data))
    return blocks


def timeit(fn, blocks, repeats=7) -> float:
    best = float("inf")
    for _ in range(repeats):
        started = time.perf_counter()
        for block in blocks:
            fn(block)
        best = min(best, time.perf_counter() - started)
    return best


def main() -> None:
    blocks = load_blocks()
    total_bytes = sum(len(b) for b in blocks)
    baseline = [current(b) for b in blocks]
    print(f"{len(blocks)} coordinate blocks, {total_bytes/1e6:.1f} MB")

    reference = baseline
    rows = []
    for fn in VARIANTS:
        if fn is not current:
            got = [fn(b) for b in blocks]
            same = got == reference
        else:
            same = True
        seconds = timeit(fn, blocks)
        rows.append((fn.__name__, seconds, same))
        print(f"{fn.__name__:16s} {seconds*1000:8.1f} ms  identical={same}")

    base = dict((n, t) for n, t, _ in rows)["current"]
    print("\nvs current parser:")
    for name, seconds, same in rows:
        print(f"  {name:16s} {(seconds-base)/base*100:+6.1f}%  (identical={same})")


if __name__ == "__main__":
    main()
