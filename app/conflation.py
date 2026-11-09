"""Multi-source building conflation (OSM-first), in DuckDB: observations -> canonical footprints.

Follows "Tài liệu chi tiết thuật toán Building Conflation" (tu-liem-v1.3, spec v1.6), with the
rules agreed for Building Data Studio (config osm-first-v1):

    Normalize   OSM buildings / parts, Google Open Buildings (confidence >= 0.75), GBA by origin:
                Microsoft (gba_ms) is a footprint, every GBA record with a height is a height
                candidate. Tiny machine footprints (< 10 m²) are dropped.
    Graph       one node per footprint (osm, gob, gba_ms); an edge per cross-family pair that
                overlaps; a merge edge when max(coverage) >= 0.40 or IoU >= 0.25. Every pair is
                kept (the conflation bridge).
    Components  union-find over merge edges. In a component the family with the highest
                precedence (osm > gob > gba_ms) wins: all its nodes are canonical footprints; the
                other nodes are evidence of the winner they overlap most. Evidence touching no
                winner (a coverage gap) is resolved again on its own. Relation: single_source,
                one_to_one, split (1:N), aggregate (N:1), ambiguous (N:M, or 1:1 with a group
                IoU < 0.50).
    Drop        a gob / gba_ms footprint that partly overlaps a footprint of a higher family
                without a merge edge (IoU >= 0.10 or coverage >= 0.20) is dropped, not reviewed.
    Height      OSM `height` -> `building:levels` x 3.5 m -> GBA height (the GBA polygon has its
                largest overlap with this footprint and IoU >= 0.50) -> 3.5 m. Valid 2–500 m,
                levels 1–100; `height_lineage` says where each height came from.

The footprint is copied from the winner (no union, average or snap). Areas and overlaps are in
the UTM zone of the area (metres). Input tables (EPSG:4326): aoi, src_osm, src_gob, src_gba.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

FAMILIES = ("osm", "gob", "gba_ms")
RANK = {f: i + 1 for i, f in enumerate(FAMILIES)}
WINNING_SOURCE = {"osm": "osm", "gob": "google_open_building", "gba_ms": "gba_ms", "landmark": "landmark"}
ID_PREFIX = "bds:"  # name-based ids (observation, component): UUIDv8 from md5(prefix + name)
GRID_M = 2000  # the area is cut in squares of this size so the in-area test stays fast


@dataclass(frozen=True)
class Params:
    """The parameters of one conflation config. Never edited in place: a change is a new config."""

    config_version: str = "osm-first-v1"
    gob_min_confidence: float = 0.75
    min_area_m2: float = 10.0
    large_area_m2: float = 5000.0
    merge_min_iou: float = 0.25
    merge_min_coverage: float = 0.40
    one_to_one_min_iou: float = 0.50
    component_trust_iou: float = 0.80
    partial_min_iou: float = 0.10
    partial_min_coverage: float = 0.20
    gba_height_min_iou: float = 0.50
    centroid_scale_m: float = 20.0
    m_per_level: float = 3.5
    default_height_m: float = 3.5
    min_height_m: float = 2.0
    max_height_m: float = 500.0
    max_levels: float = 100.0
    edge_margin_m: float = 200.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def params_hash(self) -> str:
        body = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


CONFIGS = {"osm-first-v1": Params()}
DEFAULT_CONFIG = "osm-first-v1"


def utm_epsg(lon: float, lat: float) -> str:
    zone = int(math.floor((lon + 180) / 6)) + 1
    return f"EPSG:{32600 + zone if lat >= 0 else 32700 + zone}"


def name_uuid(name: str) -> str:
    """A deterministic id for a name (an observation key@version, a component): a UUIDv8 from md5,
    the same as the SQL macro bds_uuid."""
    h = hashlib.md5((ID_PREFIX + name).encode()).hexdigest()  # noqa: S324 — an id, not a secret
    return f"{h[:8]}-{h[8:12]}-8{h[13:16]}-8{h[17:20]}-{h[20:32]}"


def observation_id(key: str, version: str | None) -> str:
    return name_uuid(f"{key}@{version or ''}")


def register_udfs(con: Any) -> None:
    """SQL macro bds_uuid(name) (DuckDB Python UDFs would need numpy)."""
    h = f"md5('{ID_PREFIX}' || name)"
    con.execute(f"""CREATE OR REPLACE MACRO bds_uuid(name) AS
        substr({h}, 1, 8) || '-' || substr({h}, 9, 4) || '-8' || substr({h}, 14, 3) || '-8' || substr({h}, 18, 3)
        || '-' || substr({h}, 21, 12)""")


def gba_family_sql(col: str) -> str:
    """GBA record -> lineage: a copy of OSM, of Google (height candidates only), or a Microsoft
    footprint (an independent footprint). An origin the app does not know is only a height
    candidate: counting it as a footprint could put a copy of an OSM / Google polygon on the map twice."""
    o = f"lower(coalesce({col}, ''))"
    return (f"CASE WHEN {o} SIMILAR TO '.*(osm|openstreetmap).*' THEN 'gba_osm' "
            f"WHEN {o} SIMILAR TO '.*(google|gob).*' THEN 'gba_google' "
            f"WHEN {o} IN ('ms', 'msft', 'gba_ms') OR {o} SIMILAR TO '.*(microsoft|bing).*' THEN 'gba_ms' "
            "ELSE 'gba_other' END")


def _q(con: Any, sql: str, log: list[str] | None = None) -> Any:
    if log is not None:
        log.append(" ".join(sql.split()))
    return con.execute(sql)


# ------------------------------------------------------------------ Step 1: normalize

def prepare(con: Any, p: Params, log: list[str] | None = None) -> dict[str, Any]:
    """Project, cut the area in squares, and build `obs` (one row per footprint) and `gba_h`."""
    register_udfs(con)
    lon, lat = con.execute("SELECT ST_X(c), ST_Y(c) FROM (SELECT ST_Centroid(ST_Union_Agg(geometry)) c FROM aoi)").fetchone()
    crs = utm_epsg(lon, lat)
    to_m = f"'EPSG:4326', '{crs}', always_xy := true"
    _q(con, f"""CREATE OR REPLACE TABLE aoi_m AS
        SELECT ST_Union_Agg(ST_Transform(ST_CollectionExtract(ST_MakeValid(geometry), 3), {to_m})) AS g FROM aoi""", log)
    _q(con, f"CREATE OR REPLACE TABLE aoi_load AS SELECT ST_Buffer(g, {p.edge_margin_m}) AS g FROM aoi_m", log)
    x0, y0, x1, y1 = con.execute("SELECT ST_XMin(g), ST_YMin(g), ST_XMax(g), ST_YMax(g) FROM aoi_load").fetchone()
    gx0, gy0 = int(x0 // GRID_M) * GRID_M, int(y0 // GRID_M) * GRID_M
    _q(con, f"""CREATE OR REPLACE TABLE grid AS
        SELECT ST_MakeEnvelope(x, y, x + {GRID_M}, y + {GRID_M}) AS cell
        FROM range({gx0}, {int(x1) + GRID_M}, {GRID_M}) tx(x), range({gy0}, {int(y1) + GRID_M}, {GRID_M}) ty(y)""", log)
    for name, src in (("load_parts", "aoi_load"), ("core_parts", "aoi_m")):
        _q(con, f"""CREATE OR REPLACE TABLE {name} AS
            SELECT ST_Intersection(grid.cell, a.g) AS g FROM grid, {src} AS a WHERE ST_Intersects(grid.cell, a.g)""", log)
    load_bbox = con.execute(
        f"SELECT ST_XMin(b), ST_YMin(b), ST_XMax(b), ST_YMax(b) FROM "
        f"(SELECT ST_Transform(g, '{crs}', 'EPSG:4326', always_xy := true) AS b FROM aoi_load)").fetchone()

    valid = "CASE WHEN ST_IsValid(geometry) THEN geometry ELSE ST_MakeValid(geometry) END"

    def projected(table: str, cols: str) -> None:
        """table -> o_<table>: valid polygons in metres that touch the (buffered) area."""
        _q(con, f"""CREATE OR REPLACE TABLE p_{table} AS
            SELECT * FROM (SELECT {cols}, ST_Transform(g4, {to_m}) AS gm, g4
                           FROM (SELECT *, ST_CollectionExtract({valid}, 3) AS g4 FROM {table}))
            WHERE g4 IS NOT NULL AND NOT ST_IsEmpty(g4)""", log)
        _q(con, f"""CREATE OR REPLACE TABLE o_{table} AS
            SELECT * FROM p_{table} WHERE rid IN (
              SELECT DISTINCT t.rid FROM p_{table} AS t JOIN load_parts AS l ON ST_Intersects(t.gm, l.g))""", log)
        _q(con, f"DROP TABLE p_{table}", log)

    projected("src_osm", "row_number() OVER () AS rid, osm_id AS source_id, kind, osm_version, osm_timestamp, name, "
                         "building, height, levels, min_height, min_level, tags")
    projected("src_gob", "row_number() OVER () AS rid, plus_code AS source_id, confidence")
    projected("src_gba", "row_number() OVER () AS rid, gba_id AS source_id, origin, height, variance")

    # building:part -> the smallest OSM outline holding a point of it; a part without one is a building.
    _q(con, """CREATE OR REPLACE TABLE part_parent AS
        SELECT pt.source_id, arg_min(b.source_id, ST_Area(b.gm)) AS parent_source_id
        FROM (SELECT source_id, ST_PointOnSurface(gm) AS pt FROM o_src_osm WHERE kind = 'part') AS pt
        JOIN (SELECT source_id, gm FROM o_src_osm WHERE kind = 'building') AS b ON ST_Contains(b.gm, pt.pt)
        GROUP BY pt.source_id""", log)
    gba_family = gba_family_sql("origin")
    _q(con, f"""CREATE OR REPLACE TABLE obs AS
        SELECT row_number() OVER (ORDER BY rank, source_id) AS node, *, family || ':' || source_id AS key,
               bds_uuid(family || ':' || source_id || '@' || coalesce(version, '')) AS observation_id
        FROM (
          SELECT 1 AS rank, 'osm' AS family,
                 CASE WHEN o.kind = 'part' AND pp.parent_source_id IS NOT NULL THEN 'part' ELSE 'building' END AS kind,
                 o.source_id, pp.parent_source_id, o.osm_version::VARCHAR AS version, o.osm_timestamp::VARCHAR AS osm_timestamp,
                 o.name, o.building, o.height, o.levels, o.min_height, o.min_level, o.tags,
                 NULL::DOUBLE AS confidence, NULL::VARCHAR AS gba_origin, NULL::DOUBLE AS gba_height,
                 ST_Area(o.gm) AS area, o.gm, o.g4
          FROM o_src_osm AS o LEFT JOIN part_parent AS pp USING (source_id)
          QUALIFY row_number() OVER (PARTITION BY o.source_id ORDER BY o.kind) = 1
          UNION ALL BY NAME
          SELECT 2 AS rank, 'gob' AS family, 'building' AS kind, source_id, 'v3' AS version, confidence,
                 ST_Area(gm) AS area, gm, g4
          FROM o_src_gob
          WHERE confidence >= {p.gob_min_confidence} AND ST_Area(gm) >= {p.min_area_m2}
          QUALIFY row_number() OVER (PARTITION BY source_id ORDER BY ST_Area(gm) DESC) = 1
          UNION ALL BY NAME
          SELECT 3 AS rank, 'gba_ms' AS family, 'building' AS kind, source_id, 'gba' AS version, origin AS gba_origin,
                 height AS gba_height, ST_Area(gm) AS area, gm, g4
          FROM o_src_gba
          WHERE {gba_family} = 'gba_ms' AND ST_Area(gm) >= {p.min_area_m2}
          QUALIFY row_number() OVER (PARTITION BY source_id ORDER BY ST_Area(gm) DESC) = 1
        )""", log)
    _q(con, f"""CREATE OR REPLACE TABLE gba_h AS
        SELECT source_id AS gba_id, {gba_family} AS gba_family, coalesce(origin, '') AS origin, height, variance,
               gm, ST_Area(gm) AS area
        FROM o_src_gba
        WHERE height BETWEEN {p.min_height_m} AND {p.max_height_m}""", log)

    stats: dict[str, Any] = {"crs": crs, "load_bbox": [round(v, 6) for v in load_bbox]}
    stats["aoi_km2"] = round(con.execute("SELECT ST_Area(g) / 1e6 FROM aoi_m").fetchone()[0], 2)
    stats["inventory"] = inventory(con, p)
    return stats


def inventory(con: Any, p: Params) -> dict[str, Any]:
    """Step 0: what each source holds in the area, and what normalisation dropped."""
    out: dict[str, Any] = {}
    q = lambda sql: con.execute(sql).fetchone()  # noqa: E731
    osm = q("SELECT count(*) FILTER (WHERE kind = 'building'), count(*) FILTER (WHERE kind = 'part'), "
            "count(*) FILTER (WHERE height IS NOT NULL), count(*) FILTER (WHERE levels IS NOT NULL) FROM o_src_osm")
    raw_osm = q("SELECT count(*) FROM src_osm")[0]
    out["osm"] = {"raw": raw_osm, "in_area": osm[0] + osm[1], "buildings": osm[0], "parts": osm[1],
                  "with_height": osm[2], "with_levels": osm[3],
                  "parts_without_outline": q("SELECT count(*) FROM o_src_osm WHERE kind = 'part' AND source_id NOT IN "
                                             "(SELECT source_id FROM part_parent)")[0]}
    gob = q(f"""SELECT count(*), count(*) FILTER (WHERE confidence < {p.gob_min_confidence}),
                       count(*) FILTER (WHERE confidence >= {p.gob_min_confidence} AND ST_Area(gm) < {p.min_area_m2}),
                       quantile_cont(confidence, [0.1, 0.5, 0.9]), quantile_cont(ST_Area(gm), [0.1, 0.5, 0.9])
                FROM o_src_gob""")
    out["gob"] = {"raw": q("SELECT count(*) FROM src_gob")[0], "in_area": gob[0], "dropped_low_confidence": gob[1],
                  "dropped_tiny": gob[2], "kept": q("SELECT count(*) FROM obs WHERE family = 'gob'")[0],
                  "confidence_p10_p50_p90": [round(v, 3) for v in (gob[3] or [])],
                  "area_m2_p10_p50_p90": [round(v, 1) for v in (gob[4] or [])]}
    gba = q("SELECT count(*), count(*) FILTER (WHERE height IS NOT NULL) FROM o_src_gba")
    by_family = dict(con.execute("SELECT gba_family, count(*) FROM gba_h GROUP BY 1").fetchall())
    out["gba"] = {"raw": q("SELECT count(*) FROM src_gba")[0], "in_area": gba[0], "with_height": gba[1],
                  "height_candidates": by_family, "footprints_gba_ms": q("SELECT count(*) FROM obs WHERE family = 'gba_ms'")[0]}
    return out


# ------------------------------------------------------------------ Step 3: overlap graph

def build_graph(con: Any, p: Params, log: list[str] | None = None) -> dict[str, Any]:
    """`pairs`: every cross-family overlap (a = higher precedence) with its metrics."""
    for fam in FAMILIES:
        _q(con, f"""CREATE OR REPLACE TABLE n_{fam} AS
            SELECT node, gm, area, ST_Centroid(gm) AS c FROM obs WHERE family = '{fam}' AND kind = 'building'""", log)
    raw = " UNION ALL ".join(
        f"SELECT a.node AS a, b.node AS b, a.area AS area_a, b.area AS area_b, "
        f"ST_Area(TRY(ST_Intersection(a.gm, b.gm))) AS inter, ST_Distance(a.c, b.c) AS cdist "
        f"FROM n_{fa} AS a JOIN n_{fb} AS b ON ST_Intersects(a.gm, b.gm)"
        for i, fa in enumerate(FAMILIES) for fb in FAMILIES[i + 1:])
    _q(con, f"""CREATE OR REPLACE TABLE pairs AS
        SELECT *, iou >= {p.merge_min_iou} OR greatest(cov_a, cov_b) >= {p.merge_min_coverage} AS is_merge,
               0.50 * iou + 0.25 * area_sim + 0.25 * cprox AS match_score
        FROM (SELECT a, b, inter, area_a, area_b, cdist,
                     inter / (area_a + area_b - inter) AS iou, inter / area_a AS cov_a, inter / area_b AS cov_b,
                     1 - abs(area_a - area_b) / greatest(area_a, area_b) AS area_sim,
                     greatest(0, 1 - cdist / {p.centroid_scale_m}) AS cprox
              FROM ({raw}) WHERE inter > 0.01)""", log)
    n, m = con.execute("SELECT count(*), count(*) FILTER (WHERE is_merge) FROM pairs").fetchone()
    nodes = con.execute("SELECT count(*) FROM obs WHERE kind = 'building'").fetchone()[0]
    return {"nodes": nodes, "pairs": n, "merge_edges": m, "slivers": n - m}


# ------------------------------------------------------------------ Step 4: components

class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        parent = self.parent
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if ra < rb:
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb


def _components(nodes: list[int], edges: list[tuple[int, int]]) -> list[list[int]]:
    uf = _UnionFind()
    for n in nodes:
        uf.find(n)
    for a, b in edges:
        uf.union(a, b)
    groups: dict[int, list[int]] = defaultdict(list)
    for n in nodes:
        groups[uf.find(n)].append(n)
    return [sorted(g) for _, g in sorted(groups.items())]


def resolve_component(nodes: list[int], rank: dict[int, int], area: dict[int, float],
                      inter: dict[tuple[int, int], float], merge: set[tuple[int, int]],
                      gap: bool = False) -> list[dict[str, Any]]:
    """Resolve one connected component (pure Python, unit-tested).

    Returns groups: {relation, winners, attach: {evidence: winner}, gap}. Evidence that overlaps
    no winner is resolved again, as its own components (a coverage gap).
    """
    top = min(rank[n] for n in nodes)
    winners = [n for n in nodes if rank[n] == top]
    evidence = [n for n in nodes if rank[n] != top]
    if not evidence:
        return [{"relation": "single_source", "winners": winners, "attach": {}, "gap": gap}]
    per_family: dict[int, int] = defaultdict(int)
    for e in evidence:
        per_family[rank[e]] += 1
    n_w, many_l = len(winners), any(c >= 2 for c in per_family.values())
    if n_w == 1 and not many_l:
        relation = "one_to_one"
    elif n_w >= 2 and not many_l:
        relation = "aggregate"
    elif n_w == 1:
        relation = "split"
    else:
        relation = "ambiguous"
    attach: dict[int, int] = {}
    rest: list[int] = []
    for e in evidence:
        best = None
        for w in winners:
            a = inter.get((w, e)) or inter.get((e, w))
            if a and (best is None or a > best[0] or (a == best[0] and area[w] < area[best[1]])):
                best = (a, w)
        if best is None:
            rest.append(e)
        else:
            attach[e] = best[1]
    groups = [{"relation": relation, "winners": winners, "attach": attach, "gap": gap}]
    if rest:
        rest_set = set(rest)
        sub_edges = [(a, b) for a, b in merge if a in rest_set and b in rest_set]
        for comp in _components(rest, sub_edges):
            groups += resolve_component(comp, rank, area, inter, merge, gap=True)
    return groups


def resolve(con: Any, p: Params, work: Path, log: list[str] | None = None) -> dict[str, Any]:
    """Components, winners and evidence -> tables `groups`, `membership`; then `canon0` (footprints)."""
    nodes = {n: (r, a) for n, r, a in con.execute("SELECT node, rank, area FROM obs WHERE kind = 'building'").fetchall()}
    pairs = con.execute("SELECT a, b, inter, is_merge FROM pairs").fetchall()
    merge_edges = [(a, b) for a, b, _, m in pairs if m]
    uf = _UnionFind()
    for a, b in merge_edges:
        uf.union(a, b)
    comps: dict[int, list[int]] = defaultdict(list)
    for n in uf.parent:
        comps[uf.find(n)].append(n)
    inter_by_comp: dict[int, dict[tuple[int, int], float]] = defaultdict(dict)
    merge_by_comp: dict[int, set[tuple[int, int]]] = defaultdict(set)
    for a, b, area_ab, m in pairs:
        if a in uf.parent and b in uf.parent:
            ra = uf.find(a)
            if ra == uf.find(b):
                inter_by_comp[ra][(a, b)] = area_ab
                if m:
                    merge_by_comp[ra].add((a, b))
    rank = {n: v[0] for n, v in nodes.items()}
    area = {n: v[1] for n, v in nodes.items()}
    work.mkdir(parents=True, exist_ok=True)
    groups_csv, members_csv = work / "groups.csv", work / "membership.csv"
    gid = 0
    with groups_csv.open("w", newline="") as gf, members_csv.open("w", newline="") as mf:
        gw, mw = csv.writer(gf), csv.writer(mf)
        gw.writerow(["gid", "comp", "relation", "gap"])
        mw.writerow(["node", "gid", "role", "winner", "inter"])
        for root in sorted(comps):
            members = sorted(comps[root])
            for g in resolve_component(members, rank, area, inter_by_comp[root], merge_by_comp[root]):
                gid += 1
                gw.writerow([gid, root, g["relation"], g["gap"]])
                for w in g["winners"]:
                    mw.writerow([w, gid, "winner", w, ""])
                for e, w in sorted(g["attach"].items()):
                    a = inter_by_comp[root].get((w, e)) or inter_by_comp[root].get((e, w))
                    mw.writerow([e, gid, "evidence", w, a])
    _q(con, f"""CREATE OR REPLACE TABLE groups AS SELECT * FROM read_csv({_s(groups_csv)}, header = true,
        columns = {{'gid': 'BIGINT', 'comp': 'BIGINT', 'relation': 'VARCHAR', 'gap': 'BOOLEAN'}})""", log)
    _q(con, f"""CREATE OR REPLACE TABLE membership AS SELECT * FROM read_csv({_s(members_csv)}, header = true,
        columns = {{'node': 'BIGINT', 'gid': 'BIGINT', 'role': 'VARCHAR', 'winner': 'BIGINT', 'inter': 'DOUBLE'}})""", log)
    # Group IoU: union of the winners vs union of the evidence attached to them.
    _q(con, """CREATE OR REPLACE TABLE group_iou AS
        WITH w AS (SELECT m.gid, ST_Union_Agg(o.gm) AS u FROM membership m JOIN obs o USING (node)
                   WHERE m.role = 'winner' GROUP BY m.gid),
             e AS (SELECT m.gid, ST_Union_Agg(o.gm) AS u FROM membership m JOIN obs o USING (node)
                   WHERE m.role = 'evidence' GROUP BY m.gid)
        SELECT gid, ST_Area(TRY(ST_Intersection(w.u, e.u))) / nullif(ST_Area(TRY(ST_Union(w.u, e.u))), 0) AS group_iou
        FROM w JOIN e USING (gid)""", log)
    _q(con, f"""CREATE OR REPLACE TABLE groups AS
        SELECT g.gid, g.comp, g.gap, gi.group_iou,
               CASE WHEN g.relation = 'one_to_one' AND coalesce(gi.group_iou, 0) < {p.one_to_one_min_iou}
                    THEN 'ambiguous' ELSE g.relation END AS relation,
               list_filter([
                 CASE WHEN g.relation = 'one_to_one' AND coalesce(gi.group_iou, 0) < {p.one_to_one_min_iou} THEN 'low_group_iou' END,
                 CASE WHEN gi.group_iou >= {p.component_trust_iou} THEN 'high_group_agreement' END,
                 CASE WHEN g.gap THEN 'coverage_gap_in_component' END], x -> x IS NOT NULL) AS reasons
        FROM groups AS g LEFT JOIN group_iou AS gi USING (gid)""", log)
    # Canonical footprints: the winners, and every node of a single-node component.
    _q(con, """CREATE OR REPLACE TABLE canon0 AS
        SELECT o.node, o.family, o.rank, o.kind, o.source_id, o.key, o.observation_id, o.version, o.area, o.gm, o.g4,
               coalesce(g.relation, 'single_source') AS relation, coalesce(g.reasons, []::VARCHAR[]) AS reasons,
               g.gid, g.comp, g.group_iou
        FROM obs AS o LEFT JOIN membership AS m ON m.node = o.node AND m.role = 'winner' LEFT JOIN groups AS g ON g.gid = m.gid
        WHERE o.kind = 'building' AND o.node NOT IN (SELECT node FROM membership WHERE role = 'evidence')""", log)
    # A machine footprint partly over a footprint of a higher family (no merge edge): dropped.
    _q(con, "CREATE OR REPLACE TABLE dropped (node BIGINT, reason VARCHAR, other BIGINT, iou DOUBLE, coverage DOUBLE)", log)
    for fam in FAMILIES[1:]:
        _q(con, f"""INSERT INTO dropped
            SELECT p.b, 'partial_overlap', arg_max(p.a, p.inter), max(p.iou), max(greatest(p.cov_a, p.cov_b))
            FROM pairs AS p JOIN canon0 AS lo ON lo.node = p.b JOIN canon0 AS hi ON hi.node = p.a
            WHERE lo.family = '{fam}' AND NOT p.is_merge
              AND (p.iou >= {p.partial_min_iou} OR greatest(p.cov_a, p.cov_b) >= {p.partial_min_coverage})
            GROUP BY p.b""", log)
        _q(con, "DELETE FROM canon0 WHERE node IN (SELECT node FROM dropped)", log)
    # Outside the area itself (only loaded so edges resolve the same way on both sides): left out.
    _q(con, """CREATE OR REPLACE TABLE outside AS
        SELECT node FROM canon0 WHERE node NOT IN (
          SELECT DISTINCT c.node FROM (SELECT node, ST_PointOnSurface(gm) AS pt FROM canon0) AS c
          JOIN core_parts AS a ON ST_Intersects(c.pt, a.g))""", log)
    _q(con, "DELETE FROM canon0 WHERE node IN (SELECT node FROM outside)", log)

    rel = dict(con.execute("SELECT relation, count(*) FROM canon0 GROUP BY 1").fetchall())
    comp_sizes = sorted((len(v) for v in comps.values()), reverse=True)
    return {
        "components_multi": len(comps),
        "largest_component": comp_sizes[0] if comp_sizes else 0,
        "groups": gid,
        "relation_counts": rel,
        "dropped_partial_overlap": dict(con.execute(
            "SELECT o.family, count(*) FROM dropped d JOIN obs o USING (node) GROUP BY 1").fetchall()),
        "outside_area": con.execute("SELECT count(*) FROM outside").fetchone()[0],
        "canonical_footprints": dict(con.execute("SELECT family, count(*) FROM canon0 GROUP BY 1").fetchall()),
    }


def _s(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


# ------------------------------------------------------------------ Step 6: height

def heights(con: Any, p: Params, log: list[str] | None = None) -> dict[str, Any]:
    """`canon`: footprints + OSM parts, each with height_m, its provenance and lineage."""
    # GBA height candidate: every GBA polygon belongs to the footprint it overlaps most; a
    # footprint takes the one it shares most area with, if their IoU is high enough.
    _q(con, f"""CREATE OR REPLACE TABLE gba_pick AS
        SELECT * FROM (
          SELECT * FROM (
            SELECT c.node, c.area AS c_area, h.gba_id, h.gba_family, h.origin, h.height, h.variance,
                   ST_Area(TRY(ST_Intersection(c.gm, h.gm))) AS inter, h.area AS h_area
            FROM canon0 AS c JOIN gba_h AS h ON ST_Intersects(c.gm, h.gm))
          WHERE inter > 0
          QUALIFY row_number() OVER (PARTITION BY gba_id ORDER BY inter DESC, c_area, node) = 1)
        WHERE inter / (c_area + h_area - inter) >= {p.gba_height_min_iou}
        QUALIFY row_number() OVER (PARTITION BY node ORDER BY inter DESC, gba_id) = 1""", log)
    lo, hi, lv = p.min_height_m, p.max_height_m, p.max_levels
    mpl, default = p.m_per_level, p.default_height_m
    _q(con, f"""CREATE OR REPLACE TABLE evidence AS
        SELECT m.winner AS node, list(o.key ORDER BY m.inter DESC, o.key) AS keys,
               list(o.observation_id ORDER BY m.inter DESC, o.key) AS observation_ids
        FROM membership AS m JOIN obs AS o USING (node) WHERE m.role = 'evidence' GROUP BY m.winner""", log)
    osm_h = f"CASE WHEN o.height BETWEEN {lo} AND {hi} THEN o.height END"
    osm_l = f"CASE WHEN o.levels BETWEEN 1 AND {lv} AND o.levels * {mpl} BETWEEN {lo} AND {hi} THEN o.levels * {mpl} END"
    _q(con, f"""CREATE OR REPLACE TABLE canon AS
        SELECT c.*, o.parent_source_id, o.name, o.tags, o.building, o.levels, o.min_height AS min_height_tag,
               o.min_level, o.confidence, o.osm_timestamp,
               coalesce(e.keys, []::VARCHAR[]) AS evidence_keys,
               coalesce(e.observation_ids, []::VARCHAR[]) AS evidence_observation_ids,
               CASE WHEN {osm_h} IS NOT NULL THEN 'osm_height' WHEN {osm_l} IS NOT NULL THEN 'osm_levels'
                    WHEN g.node IS NOT NULL THEN 'gba' ELSE 'default' END AS height_provenance,
               round(coalesce({osm_h}, {osm_l}, g.height, {default}), 2) AS height_m,
               CASE WHEN {osm_h} IS NOT NULL THEN 'osm:height'
                    WHEN {osm_l} IS NOT NULL THEN 'osm:levels*{mpl:g}m'
                    WHEN g.node IS NOT NULL THEN 'gba:' || g.gba_family || ':' || g.gba_id
                    ELSE 'default:{default:g}m' END AS height_lineage,
               g.gba_id AS height_gba_id
        FROM canon0 AS c JOIN obs AS o USING (node) LEFT JOIN evidence AS e USING (node) LEFT JOIN gba_pick AS g USING (node)""", log)
    # Building parts: own height, own levels, else the height of their building.
    _q(con, f"""INSERT INTO canon BY NAME
        SELECT o.node, o.family, o.rank, o.kind, o.source_id, o.key, o.observation_id, o.version, o.area, o.gm, o.g4,
               'part' AS relation, []::VARCHAR[] AS reasons, o.parent_source_id, o.name, o.tags, o.building, o.levels,
               o.min_height AS min_height_tag, o.min_level, o.osm_timestamp,
               []::VARCHAR[] AS evidence_keys, []::VARCHAR[] AS evidence_observation_ids,
               CASE WHEN {osm_h} IS NOT NULL THEN 'osm_height' WHEN {osm_l} IS NOT NULL THEN 'osm_levels'
                    ELSE b.height_provenance END AS height_provenance,
               round(coalesce({osm_h}, {osm_l}, b.height_m), 2) AS height_m,
               CASE WHEN {osm_h} IS NOT NULL THEN 'osm:height' WHEN {osm_l} IS NOT NULL THEN 'osm:levels*{mpl:g}m'
                    ELSE 'parent:' || o.parent_source_id END AS height_lineage
        FROM obs AS o JOIN canon AS b ON b.key = 'osm:' || o.parent_source_id
        WHERE o.kind = 'part'""", log)
    # min_height: the tag, else building:min_level x 3.5 m; only when it stays below the top.
    _q(con, f"""ALTER TABLE canon ADD COLUMN min_height_m DOUBLE""", log)
    _q(con, f"""UPDATE canon SET min_height_m = round(coalesce(min_height_tag, CASE WHEN min_level BETWEEN 0 AND {lv}
                THEN min_level * {mpl} END), 2)""", log)
    _q(con, "UPDATE canon SET min_height_m = NULL WHERE min_height_m < 0 OR min_height_m >= height_m", log)
    prov = dict(con.execute("SELECT height_provenance, count(*) FROM canon GROUP BY 1").fetchall())
    return {"height_provenance": prov,
            "gba_heights_used": con.execute("SELECT count(*) FROM canon WHERE height_provenance = 'gba'").fetchone()[0],
            "parts": con.execute("SELECT count(*) FROM canon WHERE kind = 'part'").fetchone()[0]}


# ------------------------------------------------------------------ review queue

ISSUE_SEVERITY = {"ambiguous": "high", "landmark_lost": "high", "landmark_footprint_changed": "high",
                  "split": "medium", "aggregate": "medium", "large_footprint": "medium",
                  "coverage_gap": "low"}


def issues(con: Any, p: Params, log: list[str] | None = None) -> dict[str, Any]:
    """`issues`: one row per (source footprint, issue) for MapOps, winners and their evidence."""
    _q(con, f"""CREATE OR REPLACE TABLE issues AS
        WITH flagged AS (
          SELECT gid, relation AS issue_type FROM groups WHERE relation IN ('ambiguous', 'split', 'aggregate')
          UNION ALL
          SELECT gid, 'coverage_gap' FROM groups WHERE gap
        )
        SELECT m.node, f.issue_type, g.gid, g.comp, g.relation, g.group_iou, m.role, m.winner
        FROM flagged AS f JOIN groups AS g USING (gid) JOIN membership AS m USING (gid)
        UNION ALL
        SELECT c.node, 'large_footprint', c.gid, c.comp, c.relation, c.group_iou, 'winner', c.node
        FROM canon AS c WHERE c.family <> 'osm' AND c.area > {p.large_area_m2}""", log)
    return dict(con.execute("SELECT issue_type, count(DISTINCT coalesce(gid, node)) FROM issues GROUP BY 1").fetchall())
