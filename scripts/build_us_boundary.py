"""Build ``data/us_boundary_ne10m.geojson`` from Natural Earth (public domain).

Extracts the United States polygon (50 states + DC, ``ADM0_A3 == "USA"``) from
the 1:10m Admin 0 countries file, rounds coordinates to 5 decimals (~1 m), and
classifies every polygon edge as either a *land/lake border* shared with Canada
or Mexico or a *coastline*. ``stations.boundary`` grants a small tolerance to
points just off the coast (coarse coastline generalisation puts some downtowns
"in the water") but none along the land border.

Usage:
    python scripts/build_us_boundary.py [--source ne_10m_admin_0_countries.geojson] [--out data/us_boundary_ne10m.geojson]

Without ``--source`` the file is downloaded from the natural-earth-vector repo.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

SOURCE_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_10m_admin_0_countries.geojson"
)
NEIGHBOURS = ("CAN", "MEX")
ROOT = Path(__file__).resolve().parent.parent


def _rounded_rings(geometry) -> list[list[list[list[float]]]]:
    polygons = (
        geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    )
    return [
        [[[round(x, 5), round(y, 5)] for x, y in ring] for ring in polygon] for polygon in polygons
    ]


def _vertex_set(polygons) -> set[tuple[float, float]]:
    return {(x, y) for polygon in polygons for ring in polygon for x, y in ring}


def _border_runs(polygons, neighbour_vertices) -> list[list[int]]:
    """[polygon, ring, first_edge, last_edge] runs where edge i (vertex i-1 -> i)
    has both endpoints on a neighbouring country's polygon."""
    runs = []
    for p, polygon in enumerate(polygons):
        for r, ring in enumerate(polygon):
            shared = [
                (tuple(ring[i - 1]) in neighbour_vertices and tuple(ring[i]) in neighbour_vertices)
                for i in range(len(ring))
            ]
            start = None
            for i, flag in enumerate(shared + [False]):
                if flag and start is None:
                    start = i
                elif not flag and start is not None:
                    runs.append([p, r, start, i - 1])
                    start = None
    return runs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--source", help="local ne_10m_admin_0_countries.geojson (downloaded if omitted)"
    )
    ap.add_argument("--out", default=str(ROOT / "data" / "us_boundary_ne10m.geojson"))
    args = ap.parse_args()

    if args.source:
        raw = json.loads(Path(args.source).read_text(encoding="utf-8"))
    else:
        print(f"downloading {SOURCE_URL} ...", file=sys.stderr)
        with urllib.request.urlopen(SOURCE_URL, timeout=120) as resp:  # noqa: S310 - fixed https URL
            raw = json.load(resp)

    by_code = {f["properties"].get("ADM0_A3"): f for f in raw["features"]}
    usa = _rounded_rings(by_code["USA"]["geometry"])
    neighbour_vertices = set()
    for code in NEIGHBOURS:
        neighbour_vertices |= _vertex_set(_rounded_rings(by_code[code]["geometry"]))
    runs = _border_runs(usa, neighbour_vertices)

    feature = {
        "type": "Feature",
        "properties": {
            "name": "United States of America (50 states + DC)",
            "source": "Natural Earth 1:10m Admin 0 - Countries (ADM0_A3=USA), public domain",
            "source_url": "https://www.naturalearthdata.com/",
            "built_by": "scripts/build_us_boundary.py",
            "border_edges_note": (
                "[polygon, ring, first_edge, last_edge] runs of edges (vertex i-1 -> i) "
                "shared with the Canada or Mexico polygon; all other edges are coastline"
            ),
            "border_edges": runs,
        },
        "geometry": {"type": "MultiPolygon", "coordinates": usa},
    }
    out = Path(args.out)
    out.write_text(json.dumps(feature, separators=(",", ":")), encoding="utf-8")

    vertices = sum(len(ring) for polygon in usa for ring in polygon)
    border_edges = sum(last - first + 1 for _, _, first, last in runs)
    print(
        f"wrote {out} ({out.stat().st_size:,} bytes): {len(usa)} polygons, {vertices:,} vertices, "
        f"{border_edges:,} land/lake-border edges in {len(runs)} runs, {vertices - border_edges:,} coastline edges"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
