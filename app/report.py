"""Height QA report computed from the normalised GeoPackage.

Reads the GeoPackage read-only through Python's sqlite3 (attribute queries
only; no spatial functions needed). Feature locations come from the bounding
box stored in each GeoPackage geometry header.
"""

from __future__ import annotations

import math
import sqlite3
from contextlib import closing
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .probe import quote_ident as q

HEIGHT_BIN_EDGES: tuple[float, ...] = (0, 3, 6, 10, 15, 25, 40, 60, 100, 200)
CATEGORY_LIMIT = 25
CATEGORY_FETCH_LIMIT = 1000
OUTLIER_LIMIT = 100
TALLEST_LIMIT = 20
REAL_HEIGHT_SOURCES = ("height", "levels", "clamped")

GPKG_MAGIC = b"GP"
GPKG_HEADER_BYTES = 8
# Envelope indicator -> number of doubles stored after the 8-byte header.
GPKG_ENVELOPE_DOUBLES = {0: 0, 1: 4, 2: 6, 3: 6, 4: 8}
WKB_POINT_TYPES = {1, 1001, 2001, 3001}


@dataclass(frozen=True)
class ReportSpec:
    """Which columns the report should look at."""

    table: str
    geom_col: str = "geom"
    has_height: bool = True
    id_col: str | None = None
    height_col: str | None = None
    levels_col: str | None = None
    prov_col: str | None = None
    category_cols: tuple[str, ...] = field(default_factory=tuple)


def build_report(gpkg: Path, spec: ReportSpec) -> dict[str, Any]:
    """Compute the height report for one GeoPackage table.

    Complexity: a handful of full scans plus GROUP BYs; fine for 10^7 rows.
    """
    uri = gpkg.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        columns = _table_columns(conn, spec.table)
        t = q(spec.table)
        total = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        report: dict[str, Any] = {"total": total, "height": None, "categories": [], "outliers": [], "tallest": []}
        has_height = spec.has_height and "h_m" in columns
        if has_height and total:
            report["height"] = _height_section(conn, spec, columns, total)
        for col in spec.category_cols:
            if col in columns:
                report["categories"].append(_category_section(conn, spec, col, total, has_height))
        if has_height:
            report["outliers"] = _feature_rows(conn, spec, columns, where='"h_outlier" = 1',
                                               order=_outlier_order(spec, columns), limit=OUTLIER_LIMIT)
            report["tallest"] = _feature_rows(conn, spec, columns, where='"h_outlier" = 0',
                                              order='"h_m" DESC', limit=TALLEST_LIMIT)
    return report


def _height_section(conn: sqlite3.Connection, spec: ReportSpec, columns: set[str], total: int) -> dict[str, Any]:
    t = q(spec.table)
    pairs = [(float(v), int(n)) for v, n in conn.execute(
        f'SELECT "h_m", COUNT(*) FROM {t} WHERE "h_m" IS NOT NULL GROUP BY "h_m" ORDER BY "h_m"'
    )]
    counted = sum(n for _, n in pairs)
    mean = sum(v * n for v, n in pairs) / counted if counted else None

    by_source = []
    if "h_src" in columns:
        for src, n in conn.execute(f'SELECT "h_src", COUNT(*) FROM {t} GROUP BY 1 ORDER BY 2 DESC'):
            by_source.append({"src": src, "count": n, "pct": _pct(n, total)})
    real = sum(s["count"] for s in by_source if s["src"] in REAL_HEIGHT_SOURCES)
    outliers = conn.execute(f'SELECT COUNT(*) FROM {t} WHERE "h_outlier" = 1').fetchone()[0] \
        if "h_outlier" in columns else 0

    raw = None
    if spec.height_col and spec.height_col in columns:
        h = q(spec.height_col)
        row = conn.execute(
            f"SELECT SUM({h} IS NULL), SUM(CAST({h} AS REAL) <= 0), MIN(CAST({h} AS REAL)), "
            f"MAX(CAST({h} AS REAL)) FROM {t}"
        ).fetchone()
        raw = {"column": spec.height_col, "null": row[0] or 0, "zero_or_negative": row[1] or 0,
               "min": row[2], "max": row[3]}

    return {
        "stats": {
            "count": counted,
            "min": pairs[0][0] if pairs else None,
            "max": pairs[-1][0] if pairs else None,
            "mean": mean,
            "median": quantile(pairs, 0.5),
            "p90": quantile(pairs, 0.9),
            "p99": quantile(pairs, 0.99),
        },
        "real_count": real,
        "real_pct": _pct(real, total),
        "outlier_count": outliers,
        "by_source": by_source,
        "histogram": histogram(pairs, HEIGHT_BIN_EDGES, total),
        "raw": raw,
    }


def _category_section(conn: sqlite3.Connection, spec: ReportSpec, col: str, total: int,
                      has_height: bool) -> dict[str, Any]:
    t, c = q(spec.table), q(col)
    metrics = ', AVG("h_m"), MAX("h_m"), SUM("h_src" = \'default\')' if has_height else ""
    rows = conn.execute(
        f"SELECT {c}, COUNT(*){metrics} FROM {t} GROUP BY 1 ORDER BY 2 DESC LIMIT {CATEGORY_FETCH_LIMIT}"
    ).fetchall()
    out = []
    for r in rows[:CATEGORY_LIMIT]:
        item = {"value": r[0], "count": r[1], "pct": _pct(r[1], total)}
        if has_height:
            item |= {"mean_h": r[2], "max_h": r[3], "default_pct": _pct(r[4] or 0, r[1])}
        out.append(item)
    shown = sum(item["count"] for item in out)
    return {"column": col, "rows": out, "other_count": total - shown, "distinct_shown": len(out)}


def _outlier_order(spec: ReportSpec, columns: set[str]) -> str:
    if spec.height_col and spec.height_col in columns:
        return f"CAST({q(spec.height_col)} AS REAL) DESC"
    return '"h_m" DESC'


def _feature_rows(conn: sqlite3.Connection, spec: ReportSpec, columns: set[str], *, where: str,
                  order: str, limit: int) -> list[dict[str, Any]]:
    wanted = ["fid"]
    for col in (spec.id_col, spec.height_col, spec.levels_col, spec.prov_col, *spec.category_cols,
                "h_m", "h_src"):
        if col and col in columns and col not in wanted:
            wanted.append(col)
    select = ", ".join(q(c) for c in wanted)
    geom = q(spec.geom_col) if spec.geom_col in columns else "NULL"
    sql = f"SELECT {select}, {geom} FROM {q(spec.table)} WHERE {where} ORDER BY {order} LIMIT {limit}"
    rows = []
    for record in conn.execute(sql):
        item = dict(zip(wanted, record[:-1]))
        bbox = gpkg_envelope(record[-1])
        item["bbox"] = bbox
        item["center"] = [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2] if bbox else None
        rows.append(item)
    return rows


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({q(table)})")}


def _pct(part: int | float, whole: int | float) -> float:
    return round(100.0 * part / whole, 2) if whole else 0.0


def quantile(pairs: list[tuple[float, int]], p: float) -> float | None:
    """Nearest-rank quantile over sorted (value, count) pairs.

    Args:
        pairs: (value, count) sorted ascending by value.
        p: Quantile in [0, 1].

    Returns:
        The value at that rank, or None for empty input.
    """
    total = sum(n for _, n in pairs)
    if total == 0:
        return None
    rank = max(1, math.ceil(p * total))
    cumulative = 0
    for value, n in pairs:
        cumulative += n
        if cumulative >= rank:
            return value
    return pairs[-1][0]


def histogram(pairs: list[tuple[float, int]], edges: tuple[float, ...], total: int) -> list[dict[str, Any]]:
    """Bucket (value, count) pairs into [edge_i, edge_i+1) bins plus a last open bin.

    Values below the first edge (negative heights in "keep" mode) land in the first bin.
    """
    counts = [0] * len(edges)
    for value, n in pairs:
        idx = 0
        for i, edge in enumerate(edges):
            if value >= edge:
                idx = i
        counts[idx] += n
    bins = []
    for i, edge in enumerate(edges):
        hi = edges[i + 1] if i + 1 < len(edges) else None
        label = f"{_fmt_edge(edge)}–{_fmt_edge(hi)} m" if hi is not None else f"≥ {_fmt_edge(edge)} m"
        bins.append({"label": label, "lo": edge, "hi": hi, "count": counts[i], "pct": _pct(counts[i], total)})
    return bins


def _fmt_edge(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def gpkg_envelope(blob: bytes | None) -> list[float] | None:
    """Return [minx, miny, maxx, maxy] from a GeoPackage geometry blob header.

    Falls back to the coordinates of a WKB point when no envelope is stored
    (GDAL omits it for points).
    """
    if not blob or len(blob) < GPKG_HEADER_BYTES or blob[:2] != GPKG_MAGIC:
        return None
    flags = blob[3]
    endian = "<" if flags & 0b1 else ">"
    envelope_kind = (flags >> 1) & 0b111
    if envelope_kind not in GPKG_ENVELOPE_DOUBLES:
        return None
    if envelope_kind:
        minx, maxx, miny, maxy = struct.unpack_from(endian + "4d", blob, GPKG_HEADER_BYTES)
        return [minx, miny, maxx, maxy]
    wkb = blob[GPKG_HEADER_BYTES:]
    if len(wkb) < 21:
        return None
    wkb_endian = "<" if wkb[0] == 1 else ">"
    (geom_type,) = struct.unpack_from(wkb_endian + "I", wkb, 1)
    if geom_type not in WKB_POINT_TYPES:
        return None
    x, y = struct.unpack_from(wkb_endian + "2d", wkb, 5)
    return [x, y, x, y]


def render_markdown(report: dict[str, Any], title: str) -> str:
    """Render the report as Markdown for sharing (e.g. in a ticket or chat)."""
    lines = [f"# Báo cáo chiều cao — {title}", ""]
    acc = report.get("accounting") or {}
    lines.append(f"- Tổng số building: **{report['total']:,}**")
    if acc:
        lines.append(f"- Input: {acc.get('input_count', '?'):,} → GPKG: {acc.get('normalized_count', '?'):,}"
                     + (f" → PMTiles: {acc['pmtiles_features']:,} feature" if acc.get("pmtiles_features") else ""))
    height = report.get("height")
    if height:
        s = height["stats"]
        lines += [
            f"- Có chiều cao thực (height/levels): **{height['real_count']:,}** ({height['real_pct']}%)",
            f"- Outlier (ngoài khoảng hợp lệ): **{height['outlier_count']:,}**",
            f"- h_m: min {_n(s['min'])} · trung vị {_n(s['median'])} · TB {_n(s['mean'])} · "
            f"p90 {_n(s['p90'])} · p99 {_n(s['p99'])} · max {_n(s['max'])} m",
            "",
            "## Nguồn chiều cao (h_src)",
            "",
            "| h_src | Số lượng | % |",
            "|---|---:|---:|",
            *[f"| {r['src']} | {r['count']:,} | {r['pct']} |" for r in height["by_source"]],
            "",
            "## Phân bố h_m",
            "",
            "| Khoảng | Số lượng | % |",
            "|---|---:|---:|",
            *[f"| {b['label']} | {b['count']:,} | {b['pct']} |" for b in height["histogram"]],
        ]
    for cat in report.get("categories", []):
        lines += ["", f"## Theo `{cat['column']}`", ""]
        if height:
            lines += ["| Giá trị | Số lượng | % | h_m TB | h_m max | % mặc định |", "|---|---:|---:|---:|---:|---:|"]
            lines += [f"| {r['value']} | {r['count']:,} | {r['pct']} | {_n(r.get('mean_h'))} | "
                      f"{_n(r.get('max_h'))} | {r.get('default_pct')} |" for r in cat["rows"]]
        else:
            lines += ["| Giá trị | Số lượng | % |", "|---|---:|---:|"]
            lines += [f"| {r['value']} | {r['count']:,} | {r['pct']} |" for r in cat["rows"]]
    if report.get("outliers"):
        lines += ["", f"## Outlier (tối đa {OUTLIER_LIMIT})", ""]
        keys = [k for k in report["outliers"][0] if k not in ("bbox", "center")]
        lines += ["| " + " | ".join(keys) + " | lon, lat |", "|" + "---|" * (len(keys) + 1)]
        for r in report["outliers"]:
            center = f"{r['center'][0]:.6f}, {r['center'][1]:.6f}" if r.get("center") else ""
            lines.append("| " + " | ".join(str(r.get(k, "")) for k in keys) + f" | {center} |")
    return "\n".join(lines) + "\n"


def _n(value: float | None) -> str:
    return "—" if value is None else f"{value:,.1f}"
