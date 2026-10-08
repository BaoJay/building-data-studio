"""Compare two building datasets with DuckDB.

The diff job first extracts each input with GDAL into a Parquet file holding
one row per feature (one row per tile piece for PMTiles) with the original
attributes, the geometry in EPSG:4326 and planar metrics computed by
SpatiaLite in the extract's working CRS:

    __area                planar area (deg² for vectors, m² of EPSG:3857 for PMTiles)
    __cx, __cy            planar centroid
    __x0, __x1, __y0, __y1  bounding box
    __gtype, __valid, __empty, __gh (MD5 of the WKB)

Everything after that is SQL here:

    raw rows ─► buildings (PMTiles pieces stitched back together)
             ─► height / key per building
             ─► matching (by key, or by location when there is no common ID)
             ─► per-pair geometry / height / attribute changes
             ─► quality, schema null rates, grid statistics, samples
             ─► Parquet files of added / removed / changed features

Metres are derived on a sphere of radius 6378137 m (the Web Mercator sphere)
for both kinds of input, so areas from PMTiles and from Parquet stay
comparable; the ~0.5 % bias against the ellipsoid cancels out in a diff.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from .probe import quote_ident as q
from .sqlbuild import HeightRule, height_expressions, sql_str

EARTH_RADIUS_M = 6378137.0
M_PER_DEG = math.pi / 180 * EARTH_RADIUS_M
MERCATOR_HALF_WORLD = math.pi * EARTH_RADIUS_M  # 20037508.34…
MVT_EXTENT = 4096

# Location matching: candidates come from a grid of MATCH_CELL_DEG cells (3×3 neighbourhood),
# so the search radius must stay below one cell (~100 m at Vietnam's latitudes).
MATCH_CELL_DEG = 0.001
MATCH_MIN_RADIUS_M = 3.0
MATCH_MAX_RADIUS_M = 50.0
MATCH_MAX_AREA_RATIO = 2.0
MATCH_ROUNDS = 3

# Geometry differences at or below these are tile quantisation / float noise, not edits.
GEOM_MINOR_AREA_PCT = 5.0
GEOM_MINOR_SHIFT_M = 1.0

AREA_PCT_BINS = (0.0, 1.0, 5.0, 20.0, 50.0, 100.0)
SHIFT_M_BINS = (0.0, 0.5, 1.0, 5.0, 10.0, 50.0)
DH_M_BINS = (0.0, 0.5, 1.0, 3.0, 10.0, 50.0)
REAL_SOURCES = ("height", "levels", "clamped")
CATEGORY_LIMIT = 15
MAX_STITCH_ITERATIONS = 64
# Two pieces of one building agree on their extent along the shared tile edge to within a few
# pixels (tippecanoe simplifies the clipped outline); measured: 99 % within 6 px at z17.
STITCH_TOLERANCE_PX = 8
# Compared like any column, but never what makes a building "changed": the
# effective height is judged separately, with a tolerance.
NEVER_FLAG_COLS = ("h_m",)
# Well-known secondary identifiers used to explain "removed + added" pairs.
SOURCE_ID_COLS = ("source_id", "osm_id")
# Column names the engine creates; attributes with these names (or starting with "__", the
# extract's metric columns) are left out of the comparison instead of silently shadowing them.
RESERVED_COLS = frozenset({"_pid", "_bid", "_pieces", "_area", "_cx", "_cy", "_x0", "_x1", "_y0", "_y1", "_gtype",
                           "_valid", "_empty", "_gh", "_key", "_h", "_real", "_out", "_status", "file_row_number",
                           "geometry"})

GEOM_FAMILIES = {"POLYGON": "polygon", "MULTIPOLYGON": "polygon", "POINT": "point", "MULTIPOINT": "point",
                 "LINESTRING": "line", "MULTILINESTRING": "line"}


class DiffError(RuntimeError):
    """A comparison could not be computed (message is shown to the user)."""


@dataclass(frozen=True)
class SideSpec:
    """One input of the comparison, as extracted by the diff job."""

    name: str  # "a" or "b" — also the SQL table prefix
    parquet: Path
    kind: str  # "vector" (metrics in EPSG:4326 degrees) | "pmtiles" (EPSG:3857 metres, tile pieces)
    fields: tuple[tuple[str, str], ...]  # declared (name, GDAL type) of the attributes
    id_col: str | None = None
    rule: HeightRule | None = None
    prov_col: str | None = None
    prov_missing: tuple[str, ...] = ()
    parent_col: str | None = None
    superseded_col: str | None = None
    category_cols: tuple[str, ...] = ()
    zoom: int | None = None  # PMTiles: zoom level the pieces were read at

    @property
    def field_names(self) -> list[str]:
        return [n for n, _ in self.fields]

    def has(self, name: str | None) -> bool:
        return bool(name) and name in self.field_names


@dataclass(frozen=True)
class CompareSpec:
    """What to compare and the tolerances that decide what counts as a change."""

    a: SideSpec
    b: SideSpec
    mode: str = "key"  # "key" (match on id_col of both sides) | "location"
    ignore_cols: tuple[str, ...] = ()
    height_tol_m: float = 0.5
    geom_area_pct: float = 20.0
    geom_shift_m: float = 10.0
    grid_deg: float = 0.05
    sample_limit: int = 50
    grid_limit: int = 30
    extra_cols: tuple[str, ...] = ("build_id", "config_version")  # summarised per side in the overview


ProgressFn = Callable[[float, str], None]


class DiffEngine:
    """Runs the comparison inside one DuckDB connection.

    Call run() for the result dict, then write_outputs() for the change files.
    """

    def __init__(self, con: duckdb.DuckDBPyConnection, spec: CompareSpec,
                 progress: ProgressFn | None = None, check_cancel: Callable[[], None] | None = None) -> None:
        if spec.mode not in ("key", "location"):
            raise DiffError(f"Chế độ khớp không hợp lệ: {spec.mode}")
        if spec.mode == "key" and not (spec.a.id_col and spec.b.id_col):
            raise DiffError("Khớp theo ID cần chọn cột ID ở cả A và B.")
        self.skipped: dict[str, list[str]] = {}
        spec = dataclasses.replace(spec, a=self._without_reserved(spec.a), b=self._without_reserved(spec.b))
        self.con = con
        self.spec = spec
        self._progress = progress or (lambda _v, _m: None)
        self._check_cancel = check_cancel or (lambda: None)
        self.common = [n for n in spec.a.field_names if n in set(spec.b.field_names)]
        self.compare_cols: list[str] = []
        self.col_types: dict[str, dict[str, str]] = {}
        con.execute("SET TimeZone = 'UTC'")

    def _without_reserved(self, side: SideSpec) -> SideSpec:
        reserved = [n for n in side.field_names
                    if n in RESERVED_COLS or n.startswith("__") or (side.kind == "pmtiles" and n == "json")]
        self.skipped[side.name] = reserved
        if not reserved:
            return side
        rule = side.rule
        roles = {side.id_col, side.prov_col, side.parent_col, side.superseded_col,
                 rule.height_col if rule else None, rule.levels_col if rule else None}
        clash = sorted(n for n in reserved if n in roles)
        if clash:
            raise DiffError(f"Cột {', '.join(clash)} của {side.name.upper()} trùng tên cột nội bộ của app — "
                            "hãy chọn cột khác hoặc đổi tên cột.")
        return dataclasses.replace(side, fields=tuple(f for f in side.fields if f[0] not in reserved),
                                   category_cols=tuple(c for c in side.category_cols if c not in reserved))

    # ------------------------------------------------------------------ driver
    def run(self) -> dict[str, Any]:
        spec = self.spec
        result: dict[str, Any] = {"mode": spec.mode}
        steps: list[tuple[str, Callable[[], None]]] = [
            ("Nạp A", lambda: self._load(spec.a)),
            ("Nạp B", lambda: self._load(spec.b)),
            ("Khớp building", self._match),
            ("So sánh thay đổi", self._compare),
        ]
        for i, (label, fn) in enumerate(steps):
            self._progress(100.0 * i / (len(steps) + 1), label)
            fn()
        self._progress(100.0 * len(steps) / (len(steps) + 1), "Thống kê")
        result["sides"] = {s.name: self._side_overview(s) | {"skipped_fields": self.skipped[s.name]}
                           for s in (spec.a, spec.b)}
        result["schema_nulls"] = {s.name: self._non_null_counts(s) for s in (spec.a, spec.b)}
        result["match"] = self._match_stats()
        result["changes"] = self._change_stats()
        result["quality"] = {s.name: self._quality(s) for s in (spec.a, spec.b)}
        result["grid"] = self._grid_stats()
        result["samples"] = self._samples()
        self._progress(100.0, "Xong")
        return result

    # ------------------------------------------------------------------ sql helpers
    def _x(self, sql: str, params: list[Any] | None = None) -> duckdb.DuckDBPyConnection:
        self._check_cancel()
        return self.con.execute(sql, params or [])

    def _one(self, sql: str) -> tuple:
        return tuple(_json_safe(v) for v in self._x(sql).fetchone())

    def _all(self, sql: str) -> list[tuple]:
        return [tuple(_json_safe(v) for v in row) for row in self._x(sql).fetchall()]

    def _dicts(self, sql: str) -> list[dict[str, Any]]:
        cur = self._x(sql)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, (_json_safe(v) for v in row))) for row in cur.fetchall()]

    def _types(self, table: str) -> dict[str, str]:
        return {r[0]: r[1] for r in self._x(f"DESCRIBE {table}").fetchall()}

    # ------------------------------------------------------------------ loading
    def _needed_fields(self, side: SideSpec) -> list[str]:
        """Attributes the comparison reads (PMTiles keep the rest inside their JSON)."""
        wanted: list[str | None] = [side.id_col, side.prov_col, side.parent_col, side.superseded_col,
                                    *side.category_cols, *self.common, *self.spec.extra_cols, "h_m", "h_src",
                                    "h_outlier"]
        if side.rule:
            wanted += [side.rule.height_col, side.rule.levels_col, side.rule.prov_col]
        names = side.field_names
        return [n for n in dict.fromkeys(w for w in wanted if w) if n in names]

    def _load(self, side: SideSpec) -> None:
        s = side.name
        src = f"read_parquet({sql_str(str(side.parquet))}, file_row_number = true)"
        metrics = ['"__area"', '"__cx"', '"__cy"', '"__x0"', '"__x1"', '"__y0"', '"__y1"',
                   '"__gtype"', '"__valid"', '"__empty"', '"__gh"']
        if side.kind == "pmtiles":
            # GDAL types these from the first rows, which are mostly NULL -> often VARCHAR.
            metrics += [f'TRY_CAST("__{e}{i}" AS DOUBLE) AS "__{e}{i}"' for e in "ewns" for i in "01"]
            types = dict(side.fields)
            extracted = [f"{_json_value(n, types[n])} AS {q(n)}" for n in self._needed_fields(side)]
            cols = ", ".join(["json", *extracted, *metrics])
        else:
            present = {r[0] for r in self._all(f"DESCRIBE SELECT * FROM {src}")}
            missing = [n for n in side.field_names if n not in present]
            if missing:
                raise DiffError(f"File tạm của {s.upper()} thiếu cột: {', '.join(missing[:10])}")
            cols = ", ".join([*(q(n) for n in side.field_names), *metrics])
        self._x(f"CREATE OR REPLACE TABLE {s}_raw AS SELECT file_row_number AS _pid, {cols} FROM {src}")
        if side.kind == "pmtiles":
            self._stitch(side)
            self._buildings_from_pieces(side)
        else:
            self._x(f"CREATE OR REPLACE TABLE {s}_map AS SELECT _pid, _pid AS _bid FROM {s}_raw")
            attrs = "".join(f"{q(n)}, " for n in side.field_names)
            area = f'"__area" * {M_PER_DEG ** 2!r} * cos(radians("__cy"))'
            self._x(f"""
                CREATE OR REPLACE TABLE {s}_bld AS SELECT
                  _pid AS _bid, 1 AS _pieces, {attrs}
                  {area} AS _area, "__cx" AS _cx, "__cy" AS _cy,
                  "__x0" AS _x0, "__y0" AS _y0, "__x1" AS _x1, "__y1" AS _y1,
                  upper("__gtype") AS _gtype, coalesce("__valid" = 1, FALSE) AS _valid,
                  coalesce("__empty" = 1, FALSE) AS _empty, "__gh" AS _gh
                FROM {s}_raw""")
        self._derive(side)
        self.col_types[s] = {r[0]: r[1] for r in self._all(f"DESCRIBE {s}_bld")}

    def _stitch(self, side: SideSpec) -> None:
        """Group the tile pieces of each building into {s}_map(_pid, _bid).

        With CLIP=YES a building crossing a tile edge comes back as one piece
        per tile. The extract records each piece's extent along the edges of
        its tile (`__e*`, `__w*`, `__n*`, `__s*`). Pieces in neighbouring tiles
        are joined when they have the same identity and, without an ID, the
        same extent along the shared edge (neighbours that only touch there
        have different extents). Connected components become buildings; a key
        seen in two separate components is a genuine duplicate.
        """
        s, z = side.name, side.zoom
        if z is None:
            raise DiffError("Thiếu zoom của PMTiles để ghép mảnh tile.")
        tile = 2 * MERCATOR_HALF_WORLD / (2 ** z)
        tol = STITCH_TOLERANCE_PX * tile / MVT_EXTENT
        origin = -MERCATOR_HALF_WORLD
        ident = "'j:' || md5(coalesce(json, ''))"
        if side.id_col:
            key = key_text(q(side.id_col), self._types(f"{s}_raw")[side.id_col])
            ident = f"coalesce('k:' || {key}, {ident})"
        same_extent = ("(p.ident LIKE 'k:%' OR (abs(p.{a}0 - n.{b}0) < {tol!r} AND abs(p.{a}1 - n.{b}1) < {tol!r}))")
        self._x(f"""
            CREATE OR REPLACE TEMP TABLE {s}_edge AS
            SELECT _pid, {ident} AS ident,
              CAST(floor((("__x0" + "__x1") / 2 - {origin!r}) / {tile!r}) AS BIGINT) AS tx,
              CAST(floor((("__y0" + "__y1") / 2 - {origin!r}) / {tile!r}) AS BIGINT) AS ty,
              "__e0" AS e0, "__e1" AS e1, "__w0" AS w0, "__w1" AS w1,
              "__n0" AS n0, "__n1" AS n1, "__s0" AS s0, "__s1" AS s1
            FROM {s}_raw
            WHERE "__e0" IS NOT NULL OR "__w0" IS NOT NULL OR "__n0" IS NOT NULL OR "__s0" IS NOT NULL""")
        self._x(f"""
            CREATE OR REPLACE TEMP TABLE {s}_pairs AS
            SELECT p._pid AS u, n._pid AS v FROM {s}_edge p JOIN {s}_edge n
              ON n.tx = p.tx + 1 AND n.ty = p.ty AND n.ident = p.ident
             AND p.e0 IS NOT NULL AND n.w0 IS NOT NULL AND {same_extent.format(a="e", b="w", tol=tol)}
            UNION ALL
            SELECT p._pid, n._pid FROM {s}_edge p JOIN {s}_edge n
              ON n.ty = p.ty + 1 AND n.tx = p.tx AND n.ident = p.ident
             AND p.n0 IS NOT NULL AND n.s0 IS NOT NULL AND {same_extent.format(a="n", b="s", tol=tol)}""")
        self._x(f"CREATE OR REPLACE TEMP TABLE {s}_links AS SELECT u, v FROM {s}_pairs UNION ALL SELECT v, u FROM {s}_pairs")
        self._x(f"CREATE OR REPLACE TEMP TABLE {s}_lbl AS SELECT DISTINCT u AS _pid, u AS c FROM {s}_links")
        # Label propagation: every piece takes the smallest label among its neighbours until stable.
        for _ in range(MAX_STITCH_ITERATIONS):
            self._x(f"""
                CREATE OR REPLACE TEMP TABLE {s}_lbl2 AS
                SELECT l._pid, least(l.c, min(n.c)) AS c
                FROM {s}_lbl l JOIN {s}_links e ON e.u = l._pid JOIN {s}_lbl n ON n._pid = e.v
                GROUP BY l._pid, l.c""")
            changed = self._one(f"SELECT count(*) FROM {s}_lbl2 JOIN {s}_lbl USING (_pid) WHERE {s}_lbl2.c <> {s}_lbl.c")[0]
            self._x(f"CREATE OR REPLACE TEMP TABLE {s}_lbl AS SELECT * FROM {s}_lbl2")
            if not changed:
                break
        self._x(f"""
            CREATE OR REPLACE TABLE {s}_map AS
            SELECT r._pid, coalesce(l.c, r._pid) AS _bid FROM {s}_raw r LEFT JOIN {s}_lbl l USING (_pid)""")
        for t in ("edge", "pairs", "links", "lbl", "lbl2"):
            self._x(f"DROP TABLE IF EXISTS {s}_{t}")

    def _buildings_from_pieces(self, side: SideSpec) -> None:
        s, r = side.name, EARTH_RADIUS_M
        attrs = self._needed_fields(side)
        any_attrs = "".join(f", any_value({q(n)}) AS {q(n)}" for n in attrs)
        lon = f'("__cx" / {r!r} * 180 / pi())'
        lat = f'degrees(2 * atan(exp("__cy" / {r!r})) - pi() / 2)'
        self._x(f"""
            CREATE OR REPLACE TABLE {s}_bld AS
            WITH p AS (
              SELECT m._bid, r.*,
                {lon} AS plon, {lat} AS plat,
                "__area" * pow(cos(radians({lat})), 2) AS parea,
                ("__x0" / {r!r} * 180 / pi()) AS px0, ("__x1" / {r!r} * 180 / pi()) AS px1,
                degrees(2 * atan(exp("__y0" / {r!r})) - pi() / 2) AS py0,
                degrees(2 * atan(exp("__y1" / {r!r})) - pi() / 2) AS py1
              FROM {s}_raw r JOIN {s}_map m USING (_pid)
            )
            SELECT _bid, count(*) AS _pieces, any_value(json) AS json{any_attrs},
              sum(parea) AS _area,
              CASE WHEN sum(parea) > 0 THEN sum(plon * parea) / sum(parea) ELSE avg(plon) END AS _cx,
              CASE WHEN sum(parea) > 0 THEN sum(plat * parea) / sum(parea) ELSE avg(plat) END AS _cy,
              min(px0) AS _x0, min(py0) AS _y0, max(px1) AS _x1, max(py1) AS _y1,
              CASE WHEN count(*) > 1 THEN 'MULTIPOLYGON' ELSE upper(any_value("__gtype")) END AS _gtype,
              coalesce(bool_and("__valid" = 1), FALSE) AS _valid,
              coalesce(bool_and("__empty" = 1), FALSE) AS _empty,
              md5(string_agg("__gh", ',' ORDER BY "__gh")) AS _gh
            FROM p GROUP BY _bid""")

    def _derive(self, side: SideSpec) -> None:
        """Add _key, _h (effective height), _real (measured height) and _out (outlier)."""
        s = side.name
        key = key_text(q(side.id_col), self._types(f"{s}_bld")[side.id_col]) if side.id_col else "CAST(NULL AS VARCHAR)"
        rule_exprs = height_expressions(side.rule, "duckdb") if side.rule else None
        if side.has("h_m"):
            h = 'TRY_CAST("h_m" AS DOUBLE)'
        elif rule_exprs:
            h = rule_exprs["h_m"]
        else:
            h = "CAST(NULL AS DOUBLE)"
        if side.has("h_src"):
            real = f'"h_src" IN ({", ".join(sql_str(v) for v in REAL_SOURCES)})'
        elif side.has("h_m") and side.prov_col and side.prov_missing:
            values = ", ".join(sql_str(v) for v in side.prov_missing)
            real = f"({q(side.prov_col)} IS NULL OR {q(side.prov_col)} NOT IN ({values}))"
        elif rule_exprs:
            real = f"({rule_exprs['h_src']}) <> 'default'"
        else:
            real = "FALSE"
        if side.has("h_outlier"):
            out = 'TRY_CAST("h_outlier" AS INTEGER) = 1'
        elif rule_exprs:
            out = f"({rule_exprs['h_outlier']}) = 1"
        else:
            out = "FALSE"
        self._x(f"""
            CREATE OR REPLACE TABLE {s}_bld AS SELECT *,
              {key} AS _key, {h} AS _h, coalesce({real}, FALSE) AS _real, coalesce({out}, FALSE) AS _out
            FROM {s}_bld""")

    # ------------------------------------------------------------------ matching
    def _match(self) -> None:
        if self.spec.mode == "key":
            for s in ("a", "b"):
                self._x(f"""
                    CREATE OR REPLACE TABLE {s}_rep AS
                    SELECT _key, min(_bid) AS _bid, count(*) AS n FROM {s}_bld WHERE _key IS NOT NULL GROUP BY _key""")
            self._x("""
                CREATE OR REPLACE TABLE pairs AS
                SELECT a._bid AS a_bid, b._bid AS b_bid FROM a_rep a JOIN b_rep b USING (_key)""")
        else:
            self._x("CREATE OR REPLACE TABLE pairs (a_bid BIGINT, b_bid BIGINT)")
            self._location_match("a_bld", "b_bld", "pairs", MATCH_ROUNDS)
        for s, other, col in (("a", "b", "a_bid"), ("b", "a", "b_bid")):
            unmatched = "removed" if s == "a" else "added"
            if self.spec.mode == "key":
                status = f"""CASE WHEN x._key IS NULL THEN 'nokey'
                                  WHEN x._bid NOT IN (SELECT _bid FROM {s}_rep) THEN 'dup'
                                  WHEN p.{col} IS NOT NULL THEN 'matched' ELSE '{unmatched}' END"""
            else:
                status = f"""CASE WHEN p.{col} IS NOT NULL THEN 'matched'
                                  WHEN x._cx IS NULL THEN 'nogeom' ELSE '{unmatched}' END"""
            self._x(f"""
                CREATE OR REPLACE TABLE {s}_status AS
                SELECT x._bid, {status} AS _status FROM {s}_bld x LEFT JOIN pairs p ON p.{col} = x._bid""")

    def _location_match(self, a_table: str, b_table: str, out: str, rounds: int,
                        a_where: str = "TRUE", b_where: str = "TRUE") -> None:
        """Pair buildings whose centroids are close and areas similar (mutual nearest, a few rounds)."""
        cell = MATCH_CELL_DEG
        radius = (f"least({MATCH_MAX_RADIUS_M!r}, greatest({MATCH_MIN_RADIUS_M!r}, "
                  f"0.5 * sqrt(greatest(coalesce(a._area, 0), coalesce(b._area, 0)))))")
        dist = (f"sqrt(pow((b._cx - a._cx) * cos(radians(a._cy)) * {M_PER_DEG!r}, 2) + "
                f"pow((b._cy - a._cy) * {M_PER_DEG!r}, 2))")
        for _ in range(rounds):
            self._x(f"""
                CREATE OR REPLACE TEMP TABLE cand AS
                WITH a AS (
                  SELECT _bid, _cx, _cy, _area, CAST(floor(_cx / {cell!r}) AS BIGINT) AS gx,
                         CAST(floor(_cy / {cell!r}) AS BIGINT) AS gy
                  FROM {a_table} WHERE _cx IS NOT NULL AND {a_where} AND _bid NOT IN (SELECT a_bid FROM {out})
                ), b AS (
                  SELECT _bid, _cx, _cy, _area, CAST(floor(_cx / {cell!r}) AS BIGINT) + dx AS gx,
                         CAST(floor(_cy / {cell!r}) AS BIGINT) + dy AS gy
                  FROM {b_table}, (VALUES (-1), (0), (1)) AS ox(dx), (VALUES (-1), (0), (1)) AS oy(dy)
                  WHERE _cx IS NOT NULL AND {b_where} AND _bid NOT IN (SELECT b_bid FROM {out})
                )
                SELECT a._bid AS a_bid, b._bid AS b_bid, {dist} AS d
                FROM a JOIN b USING (gx, gy)
                WHERE {dist} <= {radius}
                  AND (coalesce(a._area, 0) = 0 OR coalesce(b._area, 0) = 0
                       OR greatest(a._area, b._area) / least(a._area, b._area) <= {MATCH_MAX_AREA_RATIO!r})""")
            self._x("""
                CREATE OR REPLACE TEMP TABLE best AS
                SELECT a_bid, b_bid FROM (
                  SELECT a_bid, b_bid,
                    row_number() OVER (PARTITION BY a_bid ORDER BY d, b_bid) AS ra,
                    row_number() OVER (PARTITION BY b_bid ORDER BY d, a_bid) AS rb
                  FROM cand
                ) WHERE ra = 1 AND rb = 1""")
            added = self._one("SELECT count(*) FROM best")[0]
            self._x(f"INSERT INTO {out} SELECT a_bid, b_bid FROM best")
            if not added:
                break
        self._x("DROP TABLE IF EXISTS cand")
        self._x("DROP TABLE IF EXISTS best")

    # ------------------------------------------------------------------ comparison
    def _compare(self) -> None:
        spec = self.spec
        key_cols = {spec.a.id_col, spec.b.id_col} if spec.mode == "key" else set()
        self.compare_cols = [c for c in self.common if c not in key_cols
                             and c in self.col_types["a"] and c in self.col_types["b"]]
        diffs = [_diff_expr(f"a.{q(c)}", f"b.{q(c)}", self.col_types["a"][c], self.col_types["b"][c])
                 for c in self.compare_cols]
        flagged = [i for i, c in enumerate(self.compare_cols)
                   if c not in spec.ignore_cols and c not in NEVER_FLAG_COLS]
        diff_cols = "".join(f", {d} AS d{i}" for i, d in enumerate(diffs))
        names = [f"CASE WHEN d{i} THEN {sql_str(self.compare_cols[i])} END" for i in flagged]
        cols_changed = f"array_to_string(list_filter([{', '.join(names)}], x -> x IS NOT NULL), ',')" if names else "''"
        shift = (f"sqrt(pow((b._cx - a._cx) * cos(radians((a._cy + b._cy) / 2)) * {M_PER_DEG!r}, 2) + "
                 f"pow((b._cy - a._cy) * {M_PER_DEG!r}, 2))")
        self._x(f"""
            CREATE OR REPLACE TABLE cmp0 AS
            SELECT p.a_bid, p.b_bid, a._key AS key_a, b._key AS key_b,
              a._area AS area_a, b._area AS area_b, b._cx AS cx, b._cy AS cy, a._cx AS cx_a, a._cy AS cy_a,
              a._h AS h_a, b._h AS h_b, a._real AS real_a, b._real AS real_b,
              (a._gh IS NOT NULL AND a._gh = b._gh) AS same_geom,
              CASE WHEN a._area > 0 THEN 100 * abs(b._area - a._area) / a._area END AS area_pct,
              {shift} AS shift_m,
              b._h - a._h AS dh{diff_cols}
            FROM pairs p JOIN a_bld a ON a._bid = p.a_bid JOIN b_bld b ON b._bid = p.b_bid""")
        self._x(f"""
            CREATE OR REPLACE TABLE cmp AS
            WITH c AS (
              SELECT *,
                CASE WHEN same_geom THEN 'identical'
                     WHEN shift_m IS NULL THEN 'nogeom'
                     WHEN area_pct > {spec.geom_area_pct!r} OR shift_m > {spec.geom_shift_m!r} THEN 'major'
                     WHEN coalesce(area_pct, 0) <= {GEOM_MINOR_AREA_PCT!r} AND shift_m <= {GEOM_MINOR_SHIFT_M!r} THEN 'minor'
                     ELSE 'moderate' END AS geom_class,
                coalesce(abs(dh) > {spec.height_tol_m!r}, (h_a IS NULL) <> (h_b IS NULL)) AS h_changed,
                {cols_changed} AS cols_changed
              FROM cmp0
            )
            SELECT *, (geom_class IN ('major', 'moderate') OR h_changed OR cols_changed <> ''
                       OR (geom_class = 'nogeom' AND (cx IS NOT NULL OR cx_a IS NOT NULL))) AS changed FROM c""")
        self._x("DROP TABLE cmp0")

    # ------------------------------------------------------------------ statistics
    def _side_overview(self, side: SideSpec) -> dict[str, Any]:
        s = side.name
        row = self._one(f"""
            SELECT count(*), sum(_pieces), count(*) FILTER (WHERE _gtype IS NULL),
              count(*) FILTER (WHERE _empty), count(*) FILTER (WHERE _gtype IS NOT NULL AND NOT _valid),
              min(_x0), min(_y0), max(_x1), max(_y1), sum(_area)
            FROM {s}_bld""")
        families: dict[str, int] = {}
        for gtype, n in self._all(f"SELECT _gtype, count(*) FROM {s}_bld WHERE _gtype IS NOT NULL GROUP BY 1"):
            fam = geometry_family(gtype)
            families[fam] = families.get(fam, 0) + n
        out: dict[str, Any] = {
            "buildings": row[0], "pieces": row[1], "null_geometry": row[2], "empty_geometry": row[3],
            "invalid_geometry": row[4], "extent": None if row[5] is None else [row[5], row[6], row[7], row[8]],
            "total_area_m2": row[9], "geometry_families": families, "values": {},
        }
        for col in self.spec.extra_cols:
            if side.has(col):
                top = self._all(f"SELECT CAST({q(col)} AS VARCHAR), count(*) FROM {s}_bld GROUP BY 1 ORDER BY 2 DESC LIMIT 5")
                distinct = self._one(f"SELECT count(DISTINCT {q(col)}) FROM {s}_bld")[0]
                out["values"][col] = {"distinct": distinct, "top": [{"value": v, "count": n} for v, n in top]}
        return out

    def _non_null_counts(self, side: SideSpec) -> dict[str, int]:
        s = side.name
        if side.kind == "pmtiles":
            # MVT has no NULL: an absent key is the null.
            rows = self._all(f"SELECT k, count(*) FROM (SELECT unnest(json_keys(json)) AS k FROM {s}_bld) GROUP BY k")
            counts = dict(rows)
            return {n: int(counts.get(n, 0)) for n in side.field_names}
        names = side.field_names
        if not names:
            return {}
        row = self._one(f"SELECT {', '.join(f'count({q(n)})' for n in names)} FROM {s}_bld")
        return dict(zip(names, (int(v) for v in row)))

    def _match_stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {"mode": self.spec.mode}
        for s in ("a", "b"):
            out[f"status_{s}"] = dict(self._all(f"SELECT _status, count(*) FROM {s}_status GROUP BY 1"))
            dup = self._one(f"""
                SELECT count(*), coalesce(sum(n), 0) FROM (
                  SELECT _key, count(*) AS n FROM {s}_bld WHERE _key IS NOT NULL GROUP BY 1 HAVING count(*) > 1)""")
            out[f"dup_keys_{s}"] = dup[0]
            out[f"dup_rows_{s}"] = dup[1]
            out[f"empty_keys_{s}"] = self._one(f"SELECT count(*) FROM {s}_bld WHERE _key IS NULL")[0] \
                if getattr(self.spec, s).id_col else None
        out["matched"] = self._one("SELECT count(*) FROM pairs")[0]
        out["removed"] = out["status_a"].get("removed", 0)
        out["added"] = out["status_b"].get("added", 0)
        out["changed"] = self._one("SELECT count(*) FROM cmp WHERE changed")[0]
        out["unchanged"] = out["matched"] - out["changed"]
        out.update(self._reid_stats())
        return out

    def _reid_stats(self) -> dict[str, Any]:
        """Explain removed + added pairs that are really the same building under a new ID."""
        out: dict[str, Any] = {"reid_same_location": None, "reid_same_source": None, "reid_source_col": None}
        if self.spec.mode != "key":
            return out
        self._x("CREATE OR REPLACE TEMP TABLE reid (a_bid BIGINT, b_bid BIGINT)")
        self._location_match(
            "a_bld", "b_bld", "reid", 1,
            a_where="_bid IN (SELECT _bid FROM a_status WHERE _status = 'removed')",
            b_where="_bid IN (SELECT _bid FROM b_status WHERE _status = 'added')")
        out["reid_same_location"] = self._one("SELECT count(*) FROM reid")[0]
        self._x("DROP TABLE reid")
        src = next((c for c in SOURCE_ID_COLS if self.spec.a.has(c) and self.spec.b.has(c)), None)
        if src:
            ka = key_text(f"a.{q(src)}", self.col_types["a"][src])
            kb = key_text(f"b.{q(src)}", self.col_types["b"][src])
            out["reid_source_col"] = src
            out["reid_same_source"] = self._one(f"""
                SELECT count(*) FROM a_bld a JOIN a_status s USING (_bid)
                WHERE s._status = 'removed' AND {ka} IS NOT NULL AND {ka} IN (
                  SELECT {kb} FROM b_bld b JOIN b_status t USING (_bid) WHERE t._status = 'added')""")[0]
        return out

    def _change_stats(self) -> dict[str, Any]:
        spec = self.spec
        geom = dict(self._all("SELECT geom_class, count(*) FROM cmp GROUP BY 1"))
        height = self._one(f"""
            SELECT count(*) FILTER (WHERE h_changed),
              count(*) FILTER (WHERE NOT real_a AND real_b), count(*) FILTER (WHERE real_a AND NOT real_b),
              avg(dh) FILTER (WHERE h_changed), max(abs(dh)),
              count(*) FILTER (WHERE dh > {spec.height_tol_m!r}), count(*) FILTER (WHERE dh < -{spec.height_tol_m!r})
            FROM cmp""")
        columns = []
        if self.compare_cols:
            counts = self._one(f"SELECT {', '.join(f'count(*) FILTER (WHERE d{i})' for i in range(len(self.compare_cols)))} FROM cmp")
            matched = self._one("SELECT count(*) FROM cmp")[0]
            for c, n in zip(self.compare_cols, counts):
                columns.append({"column": c, "changed": n, "pct": _pct(n, matched),
                                "ignored": c in spec.ignore_cols or c in NEVER_FLAG_COLS})
            columns.sort(key=lambda r: -r["changed"])
        return {
            "geometry": {k: geom.get(k, 0) for k in ("identical", "minor", "moderate", "major", "nogeom")},
            "area_pct_hist": self._hist("area_pct", AREA_PCT_BINS, "%", "NOT same_geom"),
            "shift_hist": self._hist("shift_m", SHIFT_M_BINS, " m", "NOT same_geom"),
            "height": {
                "changed": height[0], "default_to_real": height[1], "real_to_default": height[2],
                "mean_dh_changed": height[3], "max_abs_dh": height[4], "taller": height[5], "lower": height[6],
            },
            "dh_hist": self._hist("abs(dh)", DH_M_BINS, " m", "dh IS NOT NULL AND dh <> 0"),
            "columns": columns,
            "by_reason": dict(zip(("geometry", "height", "attributes"), self._one("""
                SELECT count(*) FILTER (WHERE geom_class IN ('major', 'moderate')),
                       count(*) FILTER (WHERE h_changed), count(*) FILTER (WHERE cols_changed <> '') FROM cmp"""))),
        }

    def _hist(self, expr: str, edges: tuple[float, ...], unit: str, where: str) -> list[dict[str, Any]]:
        cases = " ".join(f"WHEN v >= {e!r} THEN {i}" for i, e in reversed(list(enumerate(edges))))
        rows = dict(self._all(f"""
            SELECT CASE {cases} ELSE 0 END AS bin, count(*) FROM (SELECT {expr} AS v FROM cmp WHERE {where})
            WHERE v IS NOT NULL GROUP BY 1"""))
        bins = []
        for i, lo in enumerate(edges):
            hi = edges[i + 1] if i + 1 < len(edges) else None
            label = f"{_fmt(lo)}–{_fmt(hi)}{unit}" if hi is not None else f"≥ {_fmt(lo)}{unit}"
            bins.append({"label": label, "lo": lo, "hi": hi, "count": rows.get(i, 0)})
        return bins

    def _quality(self, side: SideSpec) -> dict[str, Any]:
        s = side.name
        row = self._one(f"SELECT count(*), count(*) FILTER (WHERE _real), count(*) FILTER (WHERE _out), "
                        f"median(_h), max(_h) FROM {s}_bld")
        total = row[0]
        out: dict[str, Any] = {
            "total": total, "real_height": row[1], "real_pct": _pct(row[1], total), "outliers": row[2],
            "median_h": row[3], "max_h": row[4], "categories": [], "orphan_parts": None, "dangling_superseded": None,
            "height_method": "h_m" if side.has("h_m") else ("rule" if side.rule else None),
        }
        cats = [c for c in dict.fromkeys([side.prov_col, *side.category_cols]) if c and side.has(c)]
        for col in cats:
            rows = self._all(f"SELECT CAST({q(col)} AS VARCHAR), count(*) FROM {s}_bld "
                             f"GROUP BY 1 ORDER BY 2 DESC LIMIT {CATEGORY_LIMIT}")
            out["categories"].append({"column": col, "rows": [{"value": v, "count": n, "pct": _pct(n, total)}
                                                               for v, n in rows]})
        if side.id_col:
            for attr, col in (("orphan_parts", side.parent_col), ("dangling_superseded", side.superseded_col)):
                if col and side.has(col):
                    ref = f"NULLIF(TRIM(CAST({q(col)} AS VARCHAR)), '')"
                    out[attr] = self._one(f"""
                        SELECT count(*) FROM {s}_bld
                        WHERE {ref} IS NOT NULL AND {ref} NOT IN (SELECT _key FROM {s}_bld WHERE _key IS NOT NULL)""")[0]
        return out

    def _grid_stats(self) -> dict[str, Any]:
        g = float(self.spec.grid_deg)
        cell = f"CAST(floor(_cx / {g!r}) AS BIGINT) AS gx, CAST(floor(_cy / {g!r}) AS BIGINT) AS gy"
        self._x(f"""
            CREATE OR REPLACE TABLE grid AS
            WITH a AS (
              SELECT {cell}, count(*) AS n_a, count(*) FILTER (WHERE s._status = 'removed') AS removed
              FROM a_bld JOIN a_status s USING (_bid) WHERE _cx IS NOT NULL GROUP BY gx, gy
            ), b AS (
              SELECT {cell}, count(*) AS n_b, count(*) FILTER (WHERE s._status = 'added') AS added
              FROM b_bld JOIN b_status s USING (_bid) WHERE _cx IS NOT NULL GROUP BY gx, gy
            ), c AS (
              SELECT CAST(floor(cx / {g!r}) AS BIGINT) AS gx, CAST(floor(cy / {g!r}) AS BIGINT) AS gy,
                     count(*) AS changed
              FROM cmp WHERE changed AND cx IS NOT NULL GROUP BY 1, 2
            )
            SELECT gx, gy, coalesce(n_a, 0) AS n_a, coalesce(n_b, 0) AS n_b, coalesce(added, 0) AS added,
                   coalesce(removed, 0) AS removed, coalesce(changed, 0) AS changed
            FROM a FULL JOIN b USING (gx, gy) FULL JOIN c USING (gx, gy)""")
        total = self._one("SELECT count(*), count(*) FILTER (WHERE n_a > 0 AND n_b = 0), "
                          "count(*) FILTER (WHERE n_b > 0 AND n_a = 0) FROM grid")
        rows = self._dicts(f"""
            SELECT gx, gy, n_a, n_b, added, removed, changed FROM grid
            ORDER BY added + removed + changed DESC, abs(n_b - n_a) DESC LIMIT {int(self.spec.grid_limit)}""")
        for r in rows:
            r["bbox"] = [r["gx"] * g, r["gy"] * g, (r["gx"] + 1) * g, (r["gy"] + 1) * g]
            r["center"] = [(r["gx"] + 0.5) * g, (r["gy"] + 0.5) * g]
        return {"cell_deg": g, "cells": total[0], "only_a": total[1], "only_b": total[2], "top": rows}

    def grid_cells(self) -> list[dict[str, Any]]:
        """Every non-empty grid cell (for the grid layer of the outputs)."""
        return self._dicts("SELECT gx, gy, n_a, n_b, added, removed, changed FROM grid ORDER BY gy, gx")

    def _samples(self) -> dict[str, list[dict[str, Any]]]:
        lim = int(self.spec.sample_limit)
        out = {}
        for s, status in (("a", "removed"), ("b", "added")):
            side = getattr(self.spec, s)
            extra = "".join(f", CAST({q(c)} AS VARCHAR) AS {q(c)}" for c in dict.fromkeys([side.prov_col, *side.category_cols[:1]])
                            if c and side.has(c))
            out[status] = self._dicts(f"""
                SELECT _key AS key, _cx AS lon, _cy AS lat, round(_area, 1) AS area_m2, round(_h, 1) AS h{extra}
                FROM {s}_bld JOIN {s}_status USING (_bid) WHERE _status = '{status}'
                ORDER BY _area DESC NULLS LAST LIMIT {lim}""")
        out["changed"] = self._dicts(f"""
            SELECT coalesce(key_b, key_a) AS key, cx AS lon, cy AS lat, geom_class AS geom,
              round(area_pct, 1) AS area_pct, round(shift_m, 2) AS shift_m,
              round(h_a, 1) AS h_a, round(h_b, 1) AS h_b, cols_changed AS cols
            FROM cmp WHERE changed
            ORDER BY (geom_class = 'major') DESC, abs(coalesce(dh, 0)) DESC, coalesce(area_pct, 0) DESC
            LIMIT {lim}""")
        return out

    # ------------------------------------------------------------------ outputs
    def write_outputs(self, out_dir: Path) -> dict[str, dict[str, Any]]:
        """Write added / removed / changed (+ before) and the map layer as GeoParquet.

        Returns:
            {name: {"path": Path, "rows": int}} for each file written.
        """
        spec = self.spec
        written: dict[str, dict[str, Any]] = {}
        diff_cols = ("c.cols_changed AS diff_cols, c.geom_class AS diff_geom, round(c.area_pct, 2) AS diff_area_pct, "
                     "round(c.shift_m, 2) AS diff_shift_m, round(c.h_a, 2) AS diff_h_a, round(c.h_b, 2) AS diff_h_b, "
                     "round(c.dh, 2) AS diff_dh")
        jobs = {
            "added": ("b", "'added'", "JOIN b_status s ON s._bid = m._bid AND s._status = 'added'",
                      "x._key AS diff_key, round(x._h, 2) AS diff_h"),
            "removed": ("a", "'removed'", "JOIN a_status s ON s._bid = m._bid AND s._status = 'removed'",
                        "x._key AS diff_key, round(x._h, 2) AS diff_h"),
            "changed": ("b", "'changed'", "JOIN cmp c ON c.b_bid = m._bid AND c.changed",
                        f"coalesce(c.key_b, c.key_a) AS diff_key, round(x._h, 2) AS diff_h, {diff_cols}"),
            "changed_before": ("a", "'changed'", "JOIN cmp c ON c.a_bid = m._bid AND c.changed",
                               f"coalesce(c.key_b, c.key_a) AS diff_key, round(x._h, 2) AS diff_h, {diff_cols}"),
        }
        for name, (s, change, join, extra) in jobs.items():
            self._check_cancel()
            side = getattr(spec, s)
            path = out_dir / f"{name}.parquet"
            attrs = "r.json" if side.kind == "pmtiles" else ", ".join(f"r.{q(n)}" for n in side.field_names)
            select = f"{change} AS diff_change, {extra}{', ' + attrs if attrs else ''}, r.geometry"
            self._x(f"""
                COPY (
                  SELECT {select}
                  FROM read_parquet({sql_str(str(side.parquet))}, file_row_number = true) r
                  JOIN {s}_map m ON m._pid = r.file_row_number
                  JOIN {s}_bld x ON x._bid = m._bid
                  {join}
                ) TO {sql_str(str(path))} (FORMAT parquet)""")
            written[name] = {"path": path, "rows": self._one(f"SELECT count(*) FROM read_parquet({sql_str(str(path))})")[0]}
        # Map layer: the three change kinds, slim attributes, B geometry for "changed".
        map_path = out_dir / "map.parquet"
        slim = ("diff_change, diff_key, diff_h, "
                "CAST(NULL AS DOUBLE) AS diff_dh, CAST(NULL AS VARCHAR) AS diff_cols, CAST(NULL AS VARCHAR) AS diff_geom")
        parts = [f"SELECT {slim}, geometry FROM read_parquet({sql_str(str(written[n]['path']))})" for n in ("added", "removed")]
        parts.append(f"SELECT diff_change, diff_key, diff_h, diff_dh, diff_cols, diff_geom, geometry "
                     f"FROM read_parquet({sql_str(str(written['changed']['path']))})")
        self._x(f"COPY ({' UNION ALL '.join(parts)}) TO {sql_str(str(map_path))} (FORMAT parquet)")
        written["map"] = {"path": map_path, "rows": sum(written[n]["rows"] for n in ("added", "removed", "changed"))}
        return written


# ---------------------------------------------------------------------- pure helpers
def geometry_family(gtype: str | None) -> str:
    """'MULTIPOLYGON Z' -> 'polygon'; unknown types -> 'other'."""
    if not gtype:
        return "none"
    base = gtype.upper().split()[0]
    return GEOM_FAMILIES.get(base, "other")


def _json_value(name: str, gdal_type: str) -> str:
    """DuckDB expression reading one PMTiles attribute out of GDAL's JSON_FIELD."""
    path = sql_str('$."' + name.replace("\\", "\\\\").replace('"', '\\"') + '"')
    text = f"json_extract_string(json, {path})"
    if gdal_type == "Boolean":
        return f"TRY_CAST({text} AS BOOLEAN)"
    if gdal_type in ("Real", "Integer", "Integer64"):
        return f"TRY_CAST({text} AS DOUBLE)"
    return text


def key_text(col: str, duck_type: str) -> str:
    """SQL turning an identifier into comparable text: 123, 123.0 and '123' all become '123'.

    PMTiles store every number as a double, so a numeric ID read from tiles
    must still match the same ID read from Parquet / GeoPackage.
    """
    t = duck_type.upper()
    if t in ("DOUBLE", "FLOAT", "REAL") or t.startswith("DECIMAL"):
        v = f"TRY_CAST({col} AS DOUBLE)"
        text = (f"CASE WHEN {v} = trunc({v}) AND abs({v}) < 1e15 THEN CAST(CAST({v} AS BIGINT) AS VARCHAR) "
                f"ELSE CAST({col} AS VARCHAR) END")
    else:
        text = f"CAST({col} AS VARCHAR)"
    return f"NULLIF(TRIM({text}), '')"


def _json_safe(value: Any) -> Any:
    """Make a DuckDB value JSON-serialisable for the job report (dates, decimals, blobs…)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)


NUMERIC_DUCK = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER",
                "UBIGINT", "UHUGEINT", "FLOAT", "DOUBLE", "REAL", "DECIMAL")
TEMPORAL_DUCK = ("DATE", "TIME", "TIMESTAMP", "INTERVAL")


def duck_class(duck_type: str) -> str:
    """Coarse class of a DuckDB column type: numeric | bool | temporal | text | other."""
    t = duck_type.upper()
    if t.startswith(NUMERIC_DUCK):
        return "numeric"
    if t == "BOOLEAN":
        return "bool"
    if t.startswith(TEMPORAL_DUCK):
        return "temporal"
    if t == "VARCHAR":
        return "text"
    return "other"


def _diff_expr(a: str, b: str, type_a: str, type_b: str) -> str:
    """SQL boolean: do two values differ, NULL-safe and tolerant to type changes between formats?

    PMTiles store numbers as doubles, booleans as bools and dates as text, so
    values are compared in the most specific common class.
    """
    ca, cb = duck_class(type_a), duck_class(type_b)
    classes = {ca, cb}
    if classes <= {"numeric"} or classes == {"numeric", "text"}:
        va, vb = f"TRY_CAST({a} AS DOUBLE)", f"TRY_CAST({b} AS DOUBLE)"
        differ = f"abs({va} - {vb}) > 1e-9 * greatest(1, abs({va}))"
    elif "bool" in classes and classes <= {"bool", "numeric", "text"}:
        va, vb = f"TRY_CAST({a} AS BOOLEAN)", f"TRY_CAST({b} AS BOOLEAN)"
        differ = f"{va} <> {vb}"
    elif "temporal" in classes and classes <= {"temporal", "text"}:
        va, vb = f"TRY_CAST({a} AS TIMESTAMPTZ)", f"TRY_CAST({b} AS TIMESTAMPTZ)"
        differ = f"{va} <> {vb}"
    else:
        va, vb = f"CAST({a} AS VARCHAR)", f"CAST({b} AS VARCHAR)"
        differ = f"{va} <> {vb}"
    return (f"(CASE WHEN {va} IS NULL AND {vb} IS NULL THEN FALSE "
            f"WHEN {va} IS NULL OR {vb} IS NULL THEN TRUE ELSE coalesce({differ}, TRUE) END)")


def _pct(part: int | float | None, whole: int | float | None) -> float:
    return round(100.0 * (part or 0) / whole, 2) if whole else 0.0


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
