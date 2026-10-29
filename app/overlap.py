"""Footprints that overlap one another (any IoU).

Two footprints overlap when their interiors meet, i.e. the intersection has an area: buildings
that only share a wall (touching edges) do not count. A part never overlaps its own parent nor
another part of the same parent: those pairs are left out when the parts columns are known.

The pairs are found in the normalised GeoPackage (EPSG:4326) with one SQL statement run by
ogr2ogr: the GeoPackage R-tree joined with itself gives the candidate pairs whose bounding boxes
meet, SpatiaLite keeps those whose interiors meet and computes the intersection. The result is a
layer of intersection polygons (one row per pair) that also goes into the output GeoPackage.

Areas are in m² on the Web Mercator sphere (degrees² scaled by cos(latitude)), as in diffcore.
"""

from __future__ import annotations

import math
import struct
from contextlib import closing
from pathlib import Path
from typing import Any

from . import tools
from .probe import quote_ident as q
from .report import gpkg_envelope, gpkg_wkb

LAYER = "overlaps"
ROW_LIMIT = 500
# Upper edges (exclusive, IoU as a fraction) of the IoU bins; the last bin ends at 1 inclusive.
IOU_EDGES = (0.01, 0.1, 0.5, 0.9)
# A pair link carries its intersection polygon in the URL; a bigger one is drawn as its bounding box.
LINK_MAX_POINTS = 200
LINK_DECIMALS = 7

EARTH_RADIUS_M = 6378137.0
M2_PER_DEG2 = (math.pi / 180 * EARTH_RADIUS_M) ** 2


def build_overlap_sql(table: str, geom: str, *, id_col: str | None = None,
                      parts: tuple[str, str] | None = None, height_col: str | None = None) -> str:
    """SELECT (GeoPackage native SQL + SpatiaLite) returning one row per overlapping pair.

    Columns: fid_a, fid_b (fid_a < fid_b), id_a, id_b, iou, inter_m2, area_a_m2, area_b_m2,
    h_a, h_b (when the columns exist) and the intersection as `geom`.

    The R-tree is the outer and the inner loop of the join (CROSS JOIN keeps that order), so
    each footprint looks up only its neighbours: about O(n log n) for city data.
    """
    t, g = q(table), q(geom)
    rtree = q(f"rtree_{table}_{geom}")
    carried = {f"{name}_{side}": f"{side}.{q(col)}" for name, col in (("id", id_col), ("h", height_col)) if col
               for side in ("a", "b")}
    inner = "".join(f"{expr} AS {alias}, " for alias, expr in carried.items())
    outer = "".join(f"{alias}, " for alias in carried)
    where = [f"ST_Relate(a.{g}, b.{g}, 'T********') = 1"]
    if parts:
        i, p = q(parts[0]), q(parts[1])
        where.append(
            f"NOT (coalesce(a.{p} = b.{i}, 0) OR coalesce(b.{p} = a.{i}, 0) "
            f"OR coalesce(a.{p} = b.{p} AND a.{p} <> '', 0))"
        )
    # MATERIALIZED: SQLite would otherwise inline the CTE and compute each intersection once per use.
    return f"""WITH pairs AS MATERIALIZED (
  SELECT a.fid AS fid_a, b.fid AS fid_b, {inner}ST_Area(a.{g}) AS area_a, ST_Area(b.{g}) AS area_b,
         CollectionExtract(ST_Intersection(a.{g}, b.{g}), 3) AS inter
  FROM {rtree} AS ra CROSS JOIN {rtree} AS rb
    ON rb.id > ra.id AND rb.minx <= ra.maxx AND rb.maxx >= ra.minx AND rb.miny <= ra.maxy AND rb.maxy >= ra.miny
  CROSS JOIN {t} AS a ON a.fid = ra.id
  CROSS JOIN {t} AS b ON b.fid = rb.id
  WHERE {' AND '.join(where)}
), sized AS MATERIALIZED (
  SELECT *, ST_Area(inter) AS area_i, {M2_PER_DEG2!r} * cos(radians((MbrMinY(inter) + MbrMaxY(inter)) / 2)) AS k
  FROM pairs
)
SELECT fid_a, fid_b, {outer}area_i / (area_a + area_b - area_i) AS iou,
       area_i * k AS inter_m2, area_a * k AS area_a_m2, area_b * k AS area_b_m2, inter AS {g}
FROM sized WHERE area_i > 0"""


def overlap_report(overlaps: Path, buildings: Path, table: str, geom: str, total: int) -> dict[str, Any]:
    """Summary of the overlaps layer plus its top pairs by IoU, each with where to look on the map.

    Args:
        overlaps: GeoPackage holding the layer made from build_overlap_sql (EPSG:4326).
        buildings: The normalised GeoPackage the pairs come from (for the buildings' extents).
        table: Buildings table in `buildings`.
        geom: Geometry column of both tables.
        total: Number of buildings checked (the denominator of the share).
    """
    edges = (0.0, *IOU_EDGES)
    pairs, inter_m2, involved, counts, wanted, top = 0, 0.0, 0, [0] * len(edges), [], []
    with closing(tools.connect_ro(overlaps)) as conn:
        # ogr2ogr writes no layer at all when the SELECT returns no row.
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({q(LAYER)})")]
        if columns:
            t = q(LAYER)
            pairs, inter_m2 = conn.execute(f"SELECT COUNT(*), SUM(inter_m2) FROM {t}").fetchone()
            involved = conn.execute(
                f"SELECT COUNT(*) FROM (SELECT fid_a FROM {t} UNION SELECT fid_b FROM {t})").fetchone()[0]
            bins_sql = ", ".join(
                f"SUM(iou >= {lo!r}{f' AND iou < {hi!r}' if hi is not None else ''})"
                for lo, hi in zip(edges, (*IOU_EDGES, None))
            )
            counts = conn.execute(f"SELECT {bins_sql} FROM {t}").fetchone()
            wanted = [c for c in columns if c not in ("fid", geom)]
            top = conn.execute(
                f"SELECT {', '.join(q(c) for c in wanted)}, {q(geom)} FROM {t} "
                f"ORDER BY iou DESC, inter_m2 DESC LIMIT {ROW_LIMIT}"
            ).fetchall()
    rows = [dict(zip(wanted, r[:-1])) | {"_geom": r[-1]} for r in top]
    extents = _extents(buildings, table, geom, {f for r in rows for f in (r["fid_a"], r["fid_b"])})

    bins = []
    for i, lo in enumerate(edges):
        hi = IOU_EDGES[i] if i < len(IOU_EDGES) else None
        label = f"{_pct_label(lo)}–{_pct_label(hi)}" if hi is not None else f"≥ {_pct_label(lo)}"
        n = int(counts[i] or 0)
        bins.append({"label": label, "lo": lo, "hi": hi, "count": n, "pct": _pct(n, pairs)})
    return {
        "layer": LAYER,
        "total": total,
        "pairs": pairs,
        "buildings": involved,
        "pct": _pct(involved, total),
        "inter_m2": inter_m2 or 0.0,
        "iou_bins": bins,
        "limit": ROW_LIMIT,
        "rows": [_row(r, extents) for r in rows],
    }


def _row(r: dict[str, Any], extents: dict[int, list[float]]) -> dict[str, Any]:
    blob = r.pop("_geom")
    a, b = extents.get(r["fid_a"]), extents.get(r["fid_b"])
    boxes = [x for x in (a, b, gpkg_envelope(blob)) if x]
    out = dict(r)
    out["cover_a"] = _ratio(r["inter_m2"], r["area_a_m2"])
    out["cover_b"] = _ratio(r["inter_m2"], r["area_b_m2"])
    if boxes:
        box = [min(x[0] for x in boxes), min(x[1] for x in boxes), max(x[2] for x in boxes), max(x[3] for x in boxes)]
        out["bbox"] = box
        out["center"] = [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2]
        out["zoom"] = fit_zoom(box)
    out["shape"] = encode_shape(blob)
    return out


def _extents(gpkg: Path, table: str, geom: str, fids: set[int]) -> dict[int, list[float]]:
    if not fids:
        return {}
    with closing(tools.connect_ro(gpkg)) as conn:
        ids = ",".join(str(int(f)) for f in fids)
        return {fid: box for fid, blob in conn.execute(f"SELECT fid, {q(geom)} FROM {q(table)} WHERE fid IN ({ids})")
                if (box := gpkg_envelope(blob))}


def fit_zoom(bbox: list[float], pixels: float = 320.0) -> float:
    """Web Mercator zoom at which `bbox` (EPSG:4326) spans about `pixels` on screen, within 15–19."""
    lat = math.radians((bbox[1] + bbox[3]) / 2)
    span = max(bbox[2] - bbox[0], (bbox[3] - bbox[1]) / max(math.cos(lat), 0.01), 1e-7)
    return round(min(19.0, max(15.0, math.log2(pixels * 360 / (256 * span)))), 1)


def encode_shape(blob: bytes | None) -> str | None:
    """Compact text of a polygon / multipolygon for a URL: polygons split by '|', rings by ';',
    points by ',' and 'lon lat' within a point. Too many points: its bounding box instead."""
    polygons = polygons_from_wkb(gpkg_wkb(blob)) if blob else []
    if not polygons:
        return None
    if sum(len(ring) for poly in polygons for ring in poly) > LINK_MAX_POINTS:
        x0, y0, x1, y1 = gpkg_envelope(blob) or (0, 0, 0, 0)
        polygons = [[[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]]
    fmt = f"{{:.{LINK_DECIMALS}f}}"

    def num(v: float) -> str:
        return fmt.format(v).rstrip("0").rstrip(".")

    return "|".join(";".join(",".join(f"{num(x)} {num(y)}" for x, y in ring) for ring in poly) for poly in polygons)


def polygons_from_wkb(wkb: bytes | None) -> list[list[list[tuple[float, float]]]]:
    """Rings of a WKB (multi)polygon, 2D or with Z/M (ISO or EWKB-style type codes); [] otherwise."""
    if not wkb:
        return []
    try:
        polygons, _ = _read_wkb(wkb, 0)
    except struct.error:
        return []
    return polygons


def _read_wkb(wkb: bytes, pos: int) -> tuple[list[list[list[tuple[float, float]]]], int]:
    endian = "<" if wkb[pos] == 1 else ">"
    (code,) = struct.unpack_from(endian + "I", wkb, pos + 1)
    pos += 5
    flags = code & 0xF0000000
    base = code & 0x0FFFFFFF
    dims = 2 + (base // 1000 in (1, 2)) + (base // 1000 == 3) * 2
    if flags:  # EWKB: Z = 0x80000000, M = 0x40000000
        dims = 2 + bool(flags & 0x80000000) + bool(flags & 0x40000000)
    kind = base % 1000
    if kind == 3:
        (n_rings,) = struct.unpack_from(endian + "I", wkb, pos)
        pos += 4
        rings = []
        for _ in range(n_rings):
            (n,) = struct.unpack_from(endian + "I", wkb, pos)
            pos += 4
            values = struct.unpack_from(endian + f"{n * dims}d", wkb, pos)
            pos += 8 * n * dims
            rings.append([(values[j], values[j + 1]) for j in range(0, n * dims, dims)])
        return [rings], pos
    if kind in (6, 7):  # multipolygon, or a collection whose polygons are kept
        (n,) = struct.unpack_from(endian + "I", wkb, pos)
        pos += 4
        out = []
        for _ in range(n):
            polys, pos = _read_wkb(wkb, pos)
            out += polys
        return out, pos
    raise struct.error(f"unsupported WKB type {code}")


def _ratio(part: float | None, whole: float | None) -> float | None:
    return round(part / whole, 4) if part is not None and whole else None


def _pct(part: int | float, whole: int | float | None) -> float:
    return round(100.0 * part / whole, 2) if whole else 0.0


def _pct_label(fraction: float) -> str:
    return f"{fraction * 100:g}%"

