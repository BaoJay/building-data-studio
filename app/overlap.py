"""Footprint overlap QA of the normalised GeoPackage.

ratio = Σ area(A∩B) over every overlapping pair / Σ footprint area. Building parts (is_part) are left
out: they overlap their outline by design. Pairs come from the GeoPackage R-tree; GDAL's SpatiaLite
functions read the GeoPackage geometries, so this runs through ogr2ogr (CSV out), not raw sqlite3.
"""

from __future__ import annotations

import csv
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from . import tools
from .probe import quote_ident as q

MIN_OVERLAP_M2 = 1.0  # smaller intersections are drawing / snapping noise
TOP_LIMIT = 50


def _m2(area_deg2: str, lat: str) -> str:
    # shortcut: degrees² × local scale (±0.5% vs geodesic); use ST_Area(g, 1) if exact m² ever matter.
    return f"({area_deg2}) * 12308520000.0 * COS(RADIANS({lat}))"


def pairs_sql(table: str, geom: str, has_is_part: bool) -> str:
    t, g, rt = q(table), q(geom), q(f"rtree_{table}_{geom}")
    lat = "(r1.miny + r1.maxy) / 2"
    parts = 'AND NOT coalesce(A."is_part", 0) AND NOT coalesce(B."is_part", 0) ' if has_is_part else ""
    return (
        "SELECT * FROM (SELECT r1.id AS a, r2.id AS b, "
        f"{_m2(f'ST_Area(ST_Intersection(A.{g}, B.{g}))', lat)} AS i, "
        f"{_m2(f'ST_Area(A.{g})', lat)} AS a_m2, {_m2(f'ST_Area(B.{g})', lat)} AS b_m2, "
        "(r1.minx + r1.maxx) / 2 AS ax, (r1.miny + r1.maxy) / 2 AS ay, "
        "(r2.minx + r2.maxx) / 2 AS bx, (r2.miny + r2.maxy) / 2 AS by "
        f"FROM {rt} r1 JOIN {rt} r2 ON r2.id > r1.id AND r2.minx <= r1.maxx AND r2.maxx >= r1.minx "
        "AND r2.miny <= r1.maxy AND r2.maxy >= r1.miny "
        f"JOIN {t} A ON A.fid = r1.id JOIN {t} B ON B.fid = r2.id "
        f"WHERE ST_Intersects(A.{g}, B.{g}) {parts}) WHERE i >= {MIN_OVERLAP_M2}"
    )


def total_sql(table: str, geom: str, has_is_part: bool) -> str:
    g = q(geom)
    where = 'WHERE NOT coalesce("is_part", 0)' if has_is_part else ""
    return (f"SELECT SUM({_m2(f'ST_Area({g})', f'(ST_MinY({g}) + ST_MaxY({g})) / 2')}) AS m2, COUNT(*) AS n "
            f"FROM {q(table)} {where}")


def _query_csv(gpkg: Path, sql: str, out: Path, run: Callable[[list[str]], None]) -> list[dict[str, str]]:
    out.unlink(missing_ok=True)
    run([tools.require("ogr2ogr"), "-f", "CSV", str(out), str(gpkg), "-sql", sql])
    with out.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def overlap_section(gpkg: Path, table: str, geom: str, id_col: str | None, work: Path,
                    run: Callable[[list[str]], None]) -> dict[str, Any]:
    with closing(tools.connect_ro(gpkg)) as conn:
        has_is_part = "is_part" in {r[1] for r in conn.execute(f"PRAGMA table_info({q(table)})")}
    total = _query_csv(gpkg, total_sql(table, geom, has_is_part), work / "overlap_total.csv", run)[0]
    pairs = _query_csv(gpkg, pairs_sql(table, geom, has_is_part), work / "overlap_pairs.csv", run)
    return summarize(pairs, float(total["m2"] or 0), int(total["n"] or 0),
                     lambda fids: _ids(gpkg, table, id_col, fids), id_col, has_is_part)


def summarize(pairs: list[dict[str, str]], total_m2: float, footprints: int,
              ids: Callable[[set[int]], dict[int, Any]], id_col: str | None, parts_excluded: bool) -> dict[str, Any]:
    """Aggregate the pair rows into the ratio and the worst footprints (by % of their area overlapped)."""
    per: dict[int, dict[str, Any]] = {}
    overlap_m2 = 0.0
    for p in pairs:
        i = float(p["i"])
        overlap_m2 += i
        for me, other, area, x, y in ((p["a"], p["b"], p["a_m2"], p["ax"], p["ay"]),
                                      (p["b"], p["a"], p["b_m2"], p["bx"], p["by"])):
            f = per.setdefault(int(me), {"area": float(area), "overlap": 0.0, "n": 0, "worst": (0.0, None),
                                         "center": [float(x), float(y)]})
            f["overlap"] += i
            f["n"] += 1
            f["worst"] = max(f["worst"], (i, int(other)), key=lambda w: w[0])
    top = sorted(per.items(), key=lambda kv: kv[1]["overlap"] / kv[1]["area"] if kv[1]["area"] else 0,
                 reverse=True)[:TOP_LIMIT]
    names = ids({fid for fid, _ in top} | {f["worst"][1] for _, f in top})
    key = id_col or "feature_fid"  # the report tables hide a column named "fid"
    rows = [{
        key: names.get(fid, fid),
        "area_m2": round(f["area"], 1),
        "overlap_m2": round(f["overlap"], 1),
        "overlap_pct": round(100 * f["overlap"] / f["area"], 1) if f["area"] else None,
        "overlaps": f["n"],
        "worst_with": names.get(f["worst"][1], f["worst"][1]),
        "center": f["center"],
    } for fid, f in top]
    return {
        "footprints": footprints,
        "total_m2": round(total_m2, 1),
        "overlap_m2": round(overlap_m2, 1),
        "ratio_pct": round(100 * overlap_m2 / total_m2, 3) if total_m2 else 0.0,
        "pairs": len(pairs),
        "overlapping_footprints": len(per),
        "min_m2": MIN_OVERLAP_M2,
        "parts_excluded": parts_excluded,
        "top": rows,
    }


def _ids(gpkg: Path, table: str, id_col: str | None, fids: set[int]) -> dict[int, Any]:
    if not id_col or not fids:
        return {}
    with closing(tools.connect_ro(gpkg)) as conn:
        try:
            return dict(conn.execute(f"SELECT fid, {q(id_col)} FROM {q(table)} WHERE fid IN "
                                     f"({','.join(str(int(f)) for f in fids)})"))
        except sqlite3.OperationalError:  # id column not in this table
            return {}
