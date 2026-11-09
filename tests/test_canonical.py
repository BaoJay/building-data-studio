"""Canonical Building Data (app/s2cell, app/conflation, app/idregistry, app/buildingtier, app/canonical).

The pure parts (S2 tokens, component resolution, id assignment) need nothing; the conflation runs
on synthetic footprints in DuckDB (spatial extension); the end-to-end build reads OSM and Google
Open Buildings snapshots written by the test (no network) and needs GDAL + tippecanoe + h3.
"""

import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app import buildingtier, canonical, conflation, idregistry, landmark_library, s2cell, tools
from app.jobs import FileRegistry, Job

# ------------------------------------------------------------------ S2


def test_s2_tokens_of_hanoi():
    assert s2cell.cell_token(21.03, 105.85, 4) == "313"
    assert s2cell.cell_token(21.03, 105.85, 6) == "3135"
    assert s2cell.covering((105.28, 20.56, 106.03, 21.39), 6) == ["3135"]


def test_s2_parent_levels_nest():
    leaf = s2cell.leaf_id(10.77, 106.70)
    for level in range(1, 30):
        child, parent = s2cell.parent(leaf, level + 1), s2cell.parent(leaf, level)
        assert s2cell.parent(child, level) == parent


# ------------------------------------------------------------------ components

def _resolve(nodes, rank, inter, merge=None):
    area = {n: 100.0 + n for n in nodes}
    merge = set(merge if merge is not None else inter)
    return conflation.resolve_component(sorted(nodes), rank, area, inter, merge)


def test_one_to_one_split_aggregate_ambiguous():
    # OSM 1, GOB 2: one to one
    g = _resolve([1, 2], {1: 1, 2: 2}, {(1, 2): 80.0})
    assert [(x["relation"], x["winners"], x["attach"]) for x in g] == [("one_to_one", [1], {2: 1})]
    # 1 OSM, 3 GOB: split
    g = _resolve([1, 2, 3, 4], {1: 1, 2: 2, 3: 2, 4: 2}, {(1, 2): 30.0, (1, 3): 30.0, (1, 4): 30.0})
    assert g[0]["relation"] == "split" and g[0]["attach"] == {2: 1, 3: 1, 4: 1}
    # 3 OSM in 1 GBA-ms: aggregate, the GBA polygon goes to the OSM it overlaps most
    g = _resolve([1, 2, 3, 9], {1: 1, 2: 1, 3: 1, 9: 3}, {(1, 9): 20.0, (2, 9): 50.0, (3, 9): 30.0})
    assert g[0]["relation"] == "aggregate" and g[0]["attach"] == {9: 2}
    # N:M
    g = _resolve([1, 2, 3, 4], {1: 1, 2: 1, 3: 2, 4: 2}, {(1, 3): 40.0, (2, 3): 10.0, (2, 4): 60.0})
    assert g[0]["relation"] == "ambiguous" and g[0]["attach"] == {3: 1, 4: 2}


def test_coverage_gap_is_resolved_on_its_own():
    # OSM-1 – GOB-2 – GBAms-3, OSM-1 does not touch GBAms-3: the GBA polygon is its own building.
    g = _resolve([1, 2, 3], {1: 1, 2: 2, 3: 3}, {(1, 2): 70.0, (2, 3): 50.0})
    assert [(x["relation"], x["winners"], x["gap"]) for x in g] == [("one_to_one", [1], False),
                                                                    ("single_source", [3], True)]


# ------------------------------------------------------------------ ids

def test_registry_keeps_inherits_and_never_steals(tmp_path):
    reg = idregistry.Registry(tmp_path / "r.parquet")
    reg.ids = {"osm:way/1": "id-1", "gob:A": "id-a", "gob:B": "id-b"}
    out, st = reg.assign([
        ("r1", "osm:way/2", ["gob:A"], None),  # a new OSM way where GOB A stood: inherits A's id
        ("r2", "gob:B", [], None),  # still there: keeps its id
        ("r3", "osm:way/1", ["gob:B"], None),  # keeps its own id; B's is taken
        ("r4", "osm:way/9", ["gob:B"], None),  # nothing left to inherit: new
        ("r5", "osm:way/7", [], "id-locked"),  # a landmark
    ])
    assert out["r1"] == "id-a" and out["r2"] == "id-b" and out["r3"] == "id-1" and out["r5"] == "id-locked"
    assert idregistry.UUID_RE.match(out["r4"]) and out["r4"][14] == "7"
    assert st == {"kept": 2, "inherited": 1, "new": 1, "locked": 1}


def test_source_key():
    assert idregistry.source_key("way/12") == "osm:way/12"
    assert idregistry.source_key("7PH72Q6M+H2CX") == "gob:7PH72Q6M+H2CX"
    assert idregistry.source_key("abc") is None


# ------------------------------------------------------------------ conflation on footprints

LON0, LAT0 = 105.83, 21.03
M_LON = 111_320 * math.cos(math.radians(LAT0))
M_LAT = 110_574


def square(x, y, w, h=None):
    """A w x h m rectangle, its corner x, y metres east / north of (LON0, LAT0), as WKT."""
    h = w if h is None else h
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
    return "POLYGON((" + ", ".join(f"{LON0 + px / M_LON:.8f} {LAT0 + py / M_LAT:.8f}" for px, py in pts) + "))"


def _duck():
    duckdb = pytest.importorskip("duckdb")
    from app.osmbase import BaseError, load_extension

    con = duckdb.connect()
    try:
        load_extension(con, "spatial")
    except BaseError as exc:
        pytest.skip(str(exc))
    return con


OSM = [  # osm_id, kind, building, height, levels, tags, wkt
    ("way/1", "building", "yes", 21.0, None, {"building": "yes"}, square(0, 0, 10)),
    ("way/2", "building", "apartments", None, 12.0, {"building": "apartments"}, square(100, 0, 20)),
    ("way/3", "building", "yes", None, None, {"building": "yes"}, square(200, 0, 20)),
    ("way/4", "building", "commercial", None, None, {"building": "commercial", "name": "Chợ"}, square(300, 0, 60, 40)),
    ("way/5", "part", "yes", 40.0, None, {"building:part": "yes"}, square(305, 5, 10)),
    ("way/6", "building", "house", None, None, {"building": "house"}, square(400, 0, 10)),
]
GOB = [  # plus_code, confidence, wkt
    ("G-1", 0.9, square(0.5, 0.5, 10)),  # same house as way/1
    ("G-2", 0.9, square(206, 0, 20)),  # partly over way/3 (IoU 0.54 -> merge)
    ("G-3", 0.9, square(415, 0, 10)),  # touches nothing: a new building
    ("G-4", 0.7, square(500, 0, 10)),  # below 0.75: dropped
    ("G-5", 0.9, square(408, 0, 10)),  # 2 m over way/6 (IoU 0.11, cov 0.2): partial -> dropped
    ("G-6", 0.9, square(301, 1, 25)),  # inside the market: split
    ("G-7", 0.9, square(330, 1, 25)),
]
GBA = [  # gba_id, origin, height, wkt
    ("B-1", "microsoft", 9.0, square(200.5, 0.5, 20)),  # height for way/3
    ("B-2", "microsoft", 6.0, square(600, 0, 12)),  # a Microsoft footprint nobody else has
]


def _load(con, osm=OSM, gob=GOB, gba=GBA):
    con.execute("CREATE OR REPLACE TABLE aoi AS SELECT ST_GeomFromText(?::VARCHAR) AS geometry", [square(-50, -50, 800, 200)])
    con.execute("""CREATE OR REPLACE TABLE src_osm (osm_id VARCHAR, kind VARCHAR, osm_version INTEGER, osm_timestamp VARCHAR,
        name VARCHAR, building VARCHAR, height DOUBLE, levels DOUBLE, min_height DOUBLE, min_level DOUBLE, tags VARCHAR,
        geometry GEOMETRY)""")
    for oid, kind, b, h, lv, tags, wkt in osm:
        con.execute("INSERT INTO src_osm VALUES (?, ?, 1, NULL, ?, ?, ?, ?, NULL, NULL, ?, ST_GeomFromText(?::VARCHAR))",
                    [oid, kind, tags.get("name"), b, h, lv, json.dumps(tags), wkt])
    con.execute("CREATE OR REPLACE TABLE src_gob (plus_code VARCHAR, confidence DOUBLE, geometry GEOMETRY)")
    for pc, conf, wkt in gob:
        con.execute("INSERT INTO src_gob VALUES (?, ?, ST_GeomFromText(?::VARCHAR))", [pc, conf, wkt])
    con.execute("CREATE OR REPLACE TABLE src_gba (gba_id VARCHAR, origin VARCHAR, height DOUBLE, variance DOUBLE, geometry GEOMETRY)")
    for gid, origin, h, wkt in gba:
        con.execute("INSERT INTO src_gba VALUES (?, ?, ?, 1.0, ST_GeomFromText(?::VARCHAR))", [gid, origin, h, wkt])


def _conflate(con, tmp_path):
    p = conflation.Params()
    st = {"prepare": conflation.prepare(con, p), "graph": conflation.build_graph(con, p),
          "components": conflation.resolve(con, p, tmp_path), "heights": conflation.heights(con, p)}
    rows = {r[0]: r[1:] for r in con.execute(
        "SELECT source_id, family, relation, height_m, height_provenance, height_lineage, evidence_keys FROM canon").fetchall()}
    return st, rows


def test_conflation_osm_first(tmp_path):
    con = _duck()
    _load(con)
    st, rows = _conflate(con, tmp_path)
    inv = st["prepare"]["inventory"]
    assert inv["gob"]["dropped_low_confidence"] == 1 and inv["gba"]["footprints_gba_ms"] == 2
    # OSM keeps its footprint; GOB is evidence
    assert rows["way/1"][:2] == ("osm", "one_to_one") and rows["way/1"][5] == ["gob:G-1"]
    assert rows["way/4"][1] == "split" and sorted(rows["way/4"][5]) == ["gob:G-6", "gob:G-7"]
    # GOB alone: canonical; GOB partly over OSM without a merge edge: dropped
    assert rows["G-3"][:2] == ("gob", "single_source")
    assert "G-5" not in rows and "G-4" not in rows and st["components"]["dropped_partial_overlap"] == {"gob": 1}
    assert rows["B-2"][:2] == ("gba_ms", "single_source")
    # heights: tag, levels x 3.5, GBA (IoU >= 0.5), 3.5 m, part
    assert rows["way/1"][2:5] == (21.0, "osm_height", "osm:height")
    assert rows["way/2"][2:5] == (42.0, "osm_levels", "osm:levels*3.5m")
    assert rows["way/3"][2:5] == (9.0, "gba", "gba:gba_ms:B-1")
    assert rows["way/6"][2:5] == (3.5, "default", "default:3.5m")
    assert rows["G-3"][2:4] == (3.5, "default")
    assert rows["B-2"][2:4] == (6.0, "gba")
    assert rows["way/5"][1:4] == ("part", 40.0, "osm_height")
    # every source record in at most one canonical building
    keys = [k for r in con.execute("SELECT list_prepend(key, evidence_keys) FROM canon").fetchall() for k in r[0]]
    assert len(keys) == len(set(keys))


def test_gba_of_unknown_origin_is_only_a_height(tmp_path):
    con = _duck()
    _load(con, gba=[("B-9", "", 15.0, square(400, 0, 10)), ("B-8", "gba", 30.0, square(700, 0, 10))])
    _, rows = _conflate(con, tmp_path)
    assert rows["way/6"][2:4] == (15.0, "gba")
    assert "B-8" not in rows and "B-9" not in rows


def test_building_tier_v1(tmp_path):
    con = _duck()
    _load(con)
    _conflate(con, tmp_path)
    con.execute("CREATE TABLE landmark_lock (node BIGINT, lid VARCHAR, locked_id VARCHAR, how VARCHAR)")
    st = buildingtier.apply(con, "building_tier_v1")
    tiers = dict(con.execute("SELECT c.source_id, t.building_tier FROM tier t JOIN canon c USING (node)").fetchall())
    assert tiers["way/2"] == "T2_highrise_residential"  # apartments
    assert tiers["way/4"] == "T2_commercial" and tiers["way/5"] == "T2_commercial"  # the part follows its building
    assert tiers["way/6"] == "T3_residential" and tiers["G-3"] == "T3_residential"
    assert set(st["counts"]) <= set(buildingtier.TIERS)


# ------------------------------------------------------------------ landmark binding

def test_landmark_is_drawn_on_every_build_of_its_dataset(tmp_path):
    lib = landmark_library.Library(tmp_path / "lm")
    lib.root.mkdir()
    fp = {"geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}, "centroid": [0.5, 0.5],
          "area_m2": 1, "bbox": [0, 0, 1, 1], "size_m": [1, 1], "height": 10, "base": 0}
    old = tmp_path / "base" / "a.pmtiles"
    rec = lib.register(building_id="way/7", name="T", link="", footprint=fp, source={"path": str(old), "layer": "buildings",
                                                                                      "id_field": "building_id"})
    new = tmp_path / "canonical" / "b.pmtiles"
    assert landmark_library.in_file(rec, new, {"kind": "canonical", "dataset": "hanoi", "ids": {}}) is None
    seen = landmark_library.in_file(rec, new, {"kind": "canonical", "dataset": "hanoi", "ids": {rec["id"]: "uuid-7"}})
    assert seen["building_id"] == "uuid-7" and seen["source"]["id_field"] == "building_id"
    lib.bind(rec["id"], dataset="hanoi", building_id="uuid-7", source_id="way/7", build_id="b1")
    rec = lib.read(rec["id"])
    assert (rec["building_id"], rec["source_id"], rec["source"]["building_id"]) == ("uuid-7", "way/7", "way/7")
    assert rec["history"][-1]["action"] == "bind"
    # a later build of the dataset, the OSM base, and the file it was registered on
    later = {"kind": "canonical", "dataset": "hanoi", "built_at": "2000-01-01T00:00:00+07:00"}  # built before it was registered
    assert landmark_library.in_file(rec, tmp_path / "c.pmtiles", later)["building_id"] == "uuid-7"
    assert landmark_library.in_file(rec, tmp_path / "c.pmtiles", {**later, "built_at": "2999-01-01T00:00:00+07:00"}) is None
    assert landmark_library.in_file(rec, tmp_path / "d.pmtiles", {"kind": "base"})["building_id"] == "way/7"
    assert landmark_library.in_file(rec, old, None)["building_id"] == "way/7"
    assert landmark_library.in_file(rec, tmp_path / "e.pmtiles", {"kind": "canonical", "dataset": "hcm"}) is None
    with pytest.raises(landmark_library.LibraryError):
        lib.register(building_id="uuid-x", name="", link="", footprint=fp, source={}, dataset="hanoi", source_id="way/7")


# ------------------------------------------------------------------ end to end

needs_tools = pytest.mark.skipif(
    any(tools.which(t) is None for t in ("ogr2ogr", "ogrinfo", "tippecanoe")), reason="GDAL/tippecanoe not installed")


def _write_snapshot(con, folder: Path, name: str, sql: str, meta: dict) -> None:
    folder.mkdir(parents=True)
    con.execute(f"COPY ({sql}) TO '{folder / (name + '.parquet')}' (FORMAT PARQUET)")
    (folder / "source.json").write_text(json.dumps({**meta, "file": f"{name}.parquet", "bbox": [105, 20, 107, 22]}))


def _snapshots(con, root: Path, osm: list, stamp: str) -> None:
    _load(con, osm=osm)
    _write_snapshot(con, root / "sources" / "osm" / f"osm-{stamp}", "osm", "SELECT * FROM src_osm",
                    {"source_id": "osm", "snapshot_id": f"osm-{stamp}", "rows": len(osm), "created_at": stamp})
    if not (root / "sources" / "gob").exists():
        _write_snapshot(con, root / "sources" / "gob" / "gob-1", "gob",
                        "SELECT plus_code, confidence, 0.0 AS area_in_meters, ST_Y(ST_Centroid(geometry)) AS latitude, "
                        "ST_X(ST_Centroid(geometry)) AS longitude, geometry FROM src_gob",
                        {"source_id": "google_open_buildings", "snapshot_id": "gob-1", "rows": len(GOB),
                         "downloaded_at": "2026-11-01"})


def _build(root: Path, lib, cells: list[str], **extra) -> dict:
    job = Job(id="t", name="canonical", kind="canonical",
              config={"area": "hanoi", "aoi": "h3", "h3_cells": cells, "osm_source": "snapshot", **extra})
    canonical.execute(job, FileRegistry(), root, lib)
    result = job.to_dict()
    assert result["status"] == "done", (result["error"], "\n".join(result["log"][-25:]))
    return result


@needs_tools
def test_build_end_to_end_ids_and_landmarks(tmp_path, monkeypatch):
    h3 = pytest.importorskip("h3")
    con = _duck()
    # the cell of the test strip and its neighbours: the strip may cross a cell edge
    cell = sorted(h3.grid_disk(h3.latlng_to_cell(LAT0 + 0.0003, LON0 + 0.003, 7), 1))
    monkeypatch.setattr(canonical, "ICONIC_PINS", tmp_path / "none.geojson")
    root = tmp_path / "canonical"
    lib = landmark_library.Library(tmp_path / "landmarks")
    lib.root.mkdir()
    # a landmark registered on the OSM base (old style: building_id is the OSM id)
    wkt = square(100, 0, 20)
    fp_geom = json.loads(con.execute("SELECT ST_AsGeoJSON(ST_GeomFromText(?::VARCHAR))", [wkt]).fetchone()[0])
    lm = lib.register(building_id="way/2", name="Toà A", link="", source={"path": "/nowhere/base.pmtiles"},
                      footprint={"geometry": fp_geom, "centroid": [0, 0], "area_m2": 400, "bbox": [0, 0, 0, 0],
                                 "size_m": [20, 20], "height": 42.0, "base": 0})

    _snapshots(con, root, OSM, "2026-11-01")
    gba = tmp_path / "gba.parquet"  # a GBA file as published: id, source, height, geometry (WKB)
    con.execute(f"""COPY (SELECT gba_id AS id, origin AS source, height, variance AS var, ST_AsWKB(geometry) AS geometry
                         FROM src_gba) TO '{gba}' (FORMAT PARQUET)""")
    first = _build(root, lib, cell, gba_file=str(gba))
    folder = Path(first["output_dir"])
    build = json.loads((folder / "build.json").read_text())
    pq = folder / build["files"]["parquet"]
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{pq}')").fetchall()]
    for field in ("building_id", "source_id", "height_m", "height_provenance", "height_lineage", "winning_source",
                  "winning_observation_id", "status", "updated_at", "resolution_mode", "decided_by", "build_id",
                  "min_height", "updated_by", "parent_building_id", "building_tier", "confidence", "group_id",
                  "config_version", "description"):
        assert field in cols
    ids1 = dict(con.execute(f"SELECT source_id, building_id FROM read_parquet('{pq}')").fetchall())
    heights = dict(con.execute(f"SELECT source_id, height_lineage FROM read_parquet('{pq}')").fetchall())
    assert heights["way/3"] == "gba:gba_ms:B-1" and heights["B-2"] == "gba:gba_ms:B-2"
    assert build["sources"]["gba"]["by_lineage"] == {"gba_ms": 2}
    assert (folder / "review_gba.gpkg").is_file() or "review_gba" not in build["files"]
    landmark_id = build["landmarks"]["ids"][lm["id"]]
    assert ids1["way/2"] == landmark_id
    row = con.execute(f"SELECT resolution_mode, decided_by, building_tier FROM read_parquet('{pq}') "
                      "WHERE source_id = 'way/2'").fetchone()
    assert row == ("manual", "landmark", "T1_iconic")
    rec = lib.read(lm["id"])
    assert (rec["building_id"], rec["dataset"], rec["source_id"]) == (landmark_id, "hanoi", "way/2")
    active = canonical.read_active(root, canonical.AREAS["hanoi"])
    assert active["build_id"] == build["build_id"]
    ctx = canonical.file_context(root, Path(active["pmtiles_path"]))
    assert ctx["kind"] == "canonical" and ctx["ids"][lm["id"]] == landmark_id
    assert (folder / "review_osm.gpkg").is_file() and (folder / "review_gob.gpkg").is_file()
    with closing(sqlite3.connect(folder / build["files"]["gpkg"])) as c:
        assert c.execute("SELECT count(*) FROM buildings").fetchone()[0] == len(ids1)

    # Second build: way/2 is gone from OSM (and way/1 redrawn as way/11): ids stay, the landmark
    # keeps its old footprint and gets a warning.
    osm2 = [r for r in OSM if r[0] not in ("way/1", "way/2")] + [("way/11", "building", "yes", None, None, {}, square(0, 0, 10))]
    _snapshots(con, root, osm2, "2026-11-02")
    second = _build(root, lib, cell, gba_file=str(gba))
    build2 = json.loads((Path(second["output_dir"]) / "build.json").read_text())
    pq2 = Path(second["output_dir"]) / build2["files"]["parquet"]
    ids2 = dict(con.execute(f"SELECT source_id, building_id FROM read_parquet('{pq2}')").fetchall())
    for sid in ("way/3", "way/4", "way/5", "way/6", "G-3", "B-2"):
        if sid in ids1:
            assert ids2[sid] == ids1[sid], sid
    assert ids2["way/11"] == ids1["way/1"]  # inherited through the Google footprint both matched
    assert build2["landmarks"]["ids"][lm["id"]] == landmark_id
    report = build2["landmarks"]["report"][0]
    assert report["found"] is None and "giữ footprint" in report["warning"]
    assert ids2["way/2"] == landmark_id  # the kept footprint, under its landmark's source id
