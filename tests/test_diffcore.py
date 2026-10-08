"""DuckDB diff engine on synthetic extracts (no GDAL needed).

The extracts mimic what the diff job writes: attributes + geometry metrics
(+ GeoParquet geometry), so matching, change classification, stitching of
PMTiles pieces and the change files are tested end to end inside DuckDB.
"""

import dataclasses
import math
import struct
from pathlib import Path

import duckdb
import pytest

from app.diffcore import (
    M_PER_DEG,
    MERCATOR_HALF_WORLD,
    MVT_EXTENT,
    CompareSpec,
    DiffEngine,
    DiffError,
    SideSpec,
    _diff_expr,
    _json_value,
    geometry_family,
    key_text,
)
from app.sqlbuild import HeightRule

FIELDS = (("building_id", "String"), ("height_m", "Real"), ("height_provenance", "String"),
          ("building_tier", "String"), ("build_id", "String"))
RULE = HeightRule(height_col="height_m", prov_col="height_provenance", prov_missing=("default",))
SIDE = 0.0002  # ~22 m squares


def _wkb_square(x: float, y: float, size: float) -> bytes:
    ring = [(x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y)]
    return struct.pack("<BII", 1, 3, 1) + struct.pack("<I", len(ring)) + b"".join(struct.pack("<2d", *p) for p in ring)


def _vector_extract(path: Path, rows: list[dict]) -> Path:
    """rows: building_id, x, y (lower-left, degrees), size, height_m, prov, tier."""
    con = duckdb.connect()
    con.execute("""CREATE TABLE t (building_id VARCHAR, height_m DOUBLE, height_provenance VARCHAR,
                   building_tier VARCHAR, build_id VARCHAR, wkb BLOB, "__area" DOUBLE, "__cx" DOUBLE, "__cy" DOUBLE,
                   "__x0" DOUBLE, "__x1" DOUBLE, "__y0" DOUBLE, "__y1" DOUBLE, "__gtype" VARCHAR, "__valid" INTEGER,
                   "__empty" INTEGER, "__gh" VARCHAR)""")
    for r in rows:
        x, y, s = r["x"], r["y"], r.get("size", SIDE)
        wkb = _wkb_square(x, y, s)
        con.execute("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'POLYGON', 1, 0, md5(?))", [
            r["id"], r.get("h"), r.get("prov", "osm_height"), r.get("tier", "T3"), r.get("build", "b1"), wkb,
            s * s, x + s / 2, y + s / 2, x, x + s, y, y + s, wkb.hex()])
    con.execute(f"COPY (SELECT * EXCLUDE (wkb), ST_GeomFromWKB(wkb) AS geometry FROM t) TO '{path}' (FORMAT parquet)")
    con.close()
    return path


def _side(name: str, path: Path, **kw) -> SideSpec:
    return SideSpec(name=name, parquet=path, kind="vector", fields=FIELDS, id_col="building_id", rule=RULE,
                    prov_col="height_provenance", prov_missing=("default",), category_cols=("building_tier",), **kw)


A_ROWS = [
    {"id": "1", "x": 106.70, "y": 10.77, "h": 10},
    {"id": "2", "x": 106.71, "y": 10.77, "h": 12},
    {"id": "3", "x": 106.72, "y": 10.77, "h": 15},
    {"id": "4", "x": 106.73, "y": 10.77, "h": 20},
    {"id": "6", "x": 106.75, "y": 10.77, "h": 4, "prov": "default"},
]
B_ROWS = [
    {"id": "2", "x": 106.71, "y": 10.77, "h": 12, "build": "b2"},  # only an ignored column changed
    {"id": "3", "x": 106.72, "y": 10.77, "h": 25},  # taller
    {"id": "4", "x": 106.7302, "y": 10.77, "h": 20},  # moved ~22 m -> major geometry change
    {"id": "5", "x": 106.74, "y": 10.77, "h": 8},  # new
    {"id": "6", "x": 106.75, "y": 10.77, "h": 30, "prov": "osm_height"},  # default -> measured
]


@pytest.fixture()
def extracts(tmp_path: Path) -> tuple[Path, Path]:
    return _vector_extract(tmp_path / "a.parquet", A_ROWS), _vector_extract(tmp_path / "b.parquet", B_ROWS)


def _run(spec: CompareSpec, tmp_path: Path) -> tuple[dict, DiffEngine]:
    engine = DiffEngine(duckdb.connect(), spec)
    return engine.run(), engine


def test_key_mode_counts_and_reasons(extracts, tmp_path: Path) -> None:
    a, b = extracts
    spec = CompareSpec(a=_side("a", a), b=_side("b", b), mode="key", ignore_cols=("build_id",), grid_deg=0.01)
    result, engine = _run(spec, tmp_path)
    m = result["match"]
    assert (m["matched"], m["added"], m["removed"], m["changed"], m["unchanged"]) == (4, 1, 1, 3, 1)
    ch = result["changes"]
    assert ch["geometry"]["major"] == 1 and ch["geometry"]["identical"] == 3
    assert ch["height"]["changed"] == 2 and ch["height"]["taller"] == 2
    assert ch["height"]["default_to_real"] == 1
    cols = {c["column"]: c for c in ch["columns"]}
    assert cols["build_id"]["changed"] == 1 and cols["build_id"]["ignored"]
    assert cols["height_m"]["changed"] == 2
    assert result["sides"]["a"]["buildings"] == 5 and result["sides"]["b"]["geometry_families"] == {"polygon": 5}
    assert result["quality"]["a"]["real_height"] == 4 and result["quality"]["b"]["real_height"] == 5
    assert result["samples"]["removed"][0]["key"] == "1"
    assert {s["key"] for s in result["samples"]["changed"]} == {"3", "4", "6"}
    assert result["schema_nulls"]["a"]["height_m"] == 5
    # Area of a 0.0002° square at 10.77°N, on the 6378137 m sphere.
    expected = (SIDE * M_PER_DEG) ** 2 * math.cos(math.radians(10.7701))
    assert result["sides"]["a"]["total_area_m2"] == pytest.approx(5 * expected, rel=1e-3)

    written = engine.write_outputs(tmp_path)
    rows = {k: v["rows"] for k, v in written.items()}
    assert rows == {"added": 1, "removed": 1, "changed": 3, "changed_before": 3, "map": 5}
    con = duckdb.connect()
    changed = dict(con.execute(f"SELECT diff_key, diff_geom FROM '{written['changed']['path']}'").fetchall())
    assert changed["4"] == "major"
    assert con.execute(f"SELECT diff_dh FROM '{written['changed']['path']}' WHERE diff_key = '3'").fetchone()[0] == 10


def test_location_mode_matches_without_ids(extracts, tmp_path: Path) -> None:
    a, b = extracts
    spec = CompareSpec(a=_side("a", a), b=_side("b", b), mode="location", ignore_cols=("building_id", "build_id"))
    result, _ = _run(spec, tmp_path)
    m = result["match"]
    # Building 4 moved by a full footprint: too far to be the same one -> removed + added.
    assert (m["matched"], m["added"], m["removed"]) == (3, 2, 2)
    assert m["reid_same_location"] is None


def test_reid_detected_when_ids_change(tmp_path: Path) -> None:
    a = _vector_extract(tmp_path / "a.parquet", A_ROWS)
    b = _vector_extract(tmp_path / "b.parquet", [{**r, "id": f"new-{r['id']}"} for r in A_ROWS])
    result, _ = _run(CompareSpec(a=_side("a", a), b=_side("b", b), mode="key"), tmp_path)
    m = result["match"]
    assert (m["matched"], m["added"], m["removed"], m["reid_same_location"]) == (0, 5, 5, 5)


def test_duplicate_and_empty_keys(tmp_path: Path) -> None:
    a = _vector_extract(tmp_path / "a.parquet", A_ROWS)
    rows = [*A_ROWS, {"id": "2", "x": 106.76, "y": 10.77, "h": 5}, {"id": "", "x": 106.77, "y": 10.77, "h": 5}]
    b = _vector_extract(tmp_path / "b.parquet", rows)
    result, _ = _run(CompareSpec(a=_side("a", a), b=_side("b", b), mode="key"), tmp_path)
    m = result["match"]
    assert (m["dup_keys_b"], m["dup_rows_b"], m["empty_keys_b"]) == (1, 2, 1)
    assert m["matched"] == 5 and m["added"] == 0


def test_key_mode_requires_ids(extracts) -> None:
    a, b = extracts
    with pytest.raises(DiffError):
        DiffEngine(duckdb.connect(), CompareSpec(a=_side("a", a), b=SideSpec(name="b", parquet=b, kind="vector",
                                                                                fields=FIELDS), mode="key"))


# ---------------------------------------------------------------- PMTiles pieces
ZOOM = 17
TILE = 2 * MERCATOR_HALF_WORLD / 2 ** ZOOM
ORIGIN = -MERCATOR_HALF_WORLD
EDGE_X = ORIGIN + 400_000 * TILE  # a vertical tile edge (x of tile column 400000)
BASE_Y = ORIGIN + 200_000 * TILE + 10


def _piece(attrs: str, x0: float, x1: float, y0: float, y1: float, edges: dict[str, tuple[float, float]]) -> dict:
    row = {"json": attrs, "x0": x0, "x1": x1, "y0": y0, "y1": y1}
    for e in "ewns":
        lo, hi = edges.get(e, (None, None))
        row[f"{e}0"], row[f"{e}1"] = lo, hi
    return row


def _pmtiles_extract(path: Path, pieces: list[dict]) -> Path:
    con = duckdb.connect()
    cols = ", ".join(f'"__{e}{i}" DOUBLE' for e in "ewns" for i in "01")
    con.execute(f"""CREATE TABLE t (json VARCHAR, wkb BLOB, "__area" DOUBLE, "__cx" DOUBLE, "__cy" DOUBLE,
                    "__x0" DOUBLE, "__x1" DOUBLE, "__y0" DOUBLE, "__y1" DOUBLE, "__gtype" VARCHAR,
                    "__valid" INTEGER, "__empty" INTEGER, "__gh" VARCHAR, {cols})""")
    for p in pieces:
        w, h = p["x1"] - p["x0"], p["y1"] - p["y0"]
        wkb = _wkb_square(0, 0, 1)  # geometry content is irrelevant for stitching
        con.execute("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'POLYGON', 1, 0, ?, " + ", ".join("?" * 8) + ")", [
            p["json"], wkb, w * h, p["x0"] + w / 2, p["y0"] + h / 2, p["x0"], p["x1"], p["y0"], p["y1"],
            f"{p['x0']}-{p['y0']}", *(p[f"{e}{i}"] for e in "ewns" for i in "01")])
    con.execute(f"COPY (SELECT * EXCLUDE (wkb), ST_GeomFromWKB(wkb) AS geometry FROM t) TO '{path}' (FORMAT parquet)")
    con.close()
    return path


def test_stitching_joins_pieces_but_not_touching_neighbours(tmp_path: Path) -> None:
    px = TILE / MVT_EXTENT
    same = '{"building":"yes"}'
    pieces = [
        # Building X crosses the edge: [0, 10] m on the edge, pieces left and right.
        _piece(same, EDGE_X - 8, EDGE_X, BASE_Y, BASE_Y + 10, {"e": (BASE_Y, BASE_Y + 10)}),
        _piece(same, EDGE_X, EDGE_X + 5, BASE_Y + 0.5 * px, BASE_Y + 10, {"w": (BASE_Y + 0.5 * px, BASE_Y + 10)}),
        # Neighbour Y (same attributes) shares X's wall and also crosses the edge: [10, 20] m.
        _piece(same, EDGE_X - 8, EDGE_X, BASE_Y + 10, BASE_Y + 20, {"e": (BASE_Y + 10, BASE_Y + 20)}),
        _piece(same, EDGE_X, EDGE_X + 5, BASE_Y + 10, BASE_Y + 20, {"w": (BASE_Y + 10, BASE_Y + 20)}),
        # Z only touches the edge from the right, its left part was dropped (tiny sliver): stays alone.
        _piece(same, EDGE_X, EDGE_X + 6, BASE_Y + 30, BASE_Y + 36, {"w": (BASE_Y + 30, BASE_Y + 36)}),
        # Different attributes, same extent as X: never joined.
        _piece('{"building":"roof"}', EDGE_X, EDGE_X + 5, BASE_Y, BASE_Y + 10, {"w": (BASE_Y, BASE_Y + 10)}),
    ]
    path = _pmtiles_extract(tmp_path / "a.parquet", pieces)
    side = SideSpec(name="a", parquet=path, kind="pmtiles", fields=(("building", "String"),), zoom=ZOOM)
    engine = DiffEngine(duckdb.connect(), CompareSpec(a=side, b=side, mode="location"))
    engine._load(side)
    groups = sorted(n for (n,) in engine.con.execute("SELECT count(*) FROM a_map GROUP BY _bid").fetchall())
    assert groups == [1, 1, 2, 2]
    pieces_per_building = engine.con.execute("SELECT _pieces FROM a_bld ORDER BY _pieces").fetchall()
    assert [p for (p,) in pieces_per_building] == [1, 1, 2, 2]


def test_stitching_by_key_flags_duplicates(tmp_path: Path) -> None:
    pieces = [
        _piece('{"building_id":"k1"}', EDGE_X - 8, EDGE_X, BASE_Y, BASE_Y + 10, {"e": (BASE_Y, BASE_Y + 10)}),
        # Same key, extent off by much more than the tolerance: joined anyway (the key decides).
        _piece('{"building_id":"k1"}', EDGE_X, EDGE_X + 5, BASE_Y, BASE_Y + 4, {"w": (BASE_Y, BASE_Y + 4)}),
        # Same key far away: a genuine duplicate.
        _piece('{"building_id":"k1"}', EDGE_X + 50, EDGE_X + 60, BASE_Y, BASE_Y + 10, {}),
    ]
    path = _pmtiles_extract(tmp_path / "a.parquet", pieces)
    side = SideSpec(name="a", parquet=path, kind="pmtiles", fields=(("building_id", "String"),),
                    id_col="building_id", zoom=ZOOM)
    result, _ = _run(CompareSpec(a=side, b=dataclasses.replace(side, name="b"), mode="key"), tmp_path)
    assert result["sides"]["a"]["buildings"] == 2 and result["sides"]["a"]["pieces"] == 3
    assert result["match"]["dup_keys_a"] == 1 and result["match"]["matched"] == 1


# ---------------------------------------------------------------- value comparison
@pytest.mark.parametrize(("a", "ta", "b", "tb", "differ"), [
    ("4", "VARCHAR", "4.0", "DOUBLE", False),
    ("4", "BIGINT", "4.5", "DOUBLE", True),
    ("NULL", "VARCHAR", "NULL", "VARCHAR", False),
    ("'x'", "VARCHAR", "NULL", "VARCHAR", True),
    ("TIMESTAMPTZ '2026-10-08 03:33:34+00'", "TIMESTAMP WITH TIME ZONE", "'2026-10-08 03:33:34+00'", "VARCHAR", False),
    ("true", "BOOLEAN", "1", "DOUBLE", False),
    ("'abc'", "VARCHAR", "'abd'", "VARCHAR", True),
])
def test_diff_expr(a: str, ta: str, b: str, tb: str, differ: bool) -> None:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    sql = _diff_expr(f"CAST({a} AS {ta})", f"CAST({b} AS {tb})", ta, tb)
    assert con.execute(f"SELECT {sql}").fetchone()[0] is differ


def test_geometry_family() -> None:
    assert geometry_family("MULTIPOLYGON Z") == "polygon"
    assert geometry_family("POINT") == "point"
    assert geometry_family(None) == "none"
    assert geometry_family("GEOMETRYCOLLECTION") == "other"


# ---------------------------------------------------------------- review regressions
def _rewrite(src: Path, dst: Path, select: str) -> Path:
    con = duckdb.connect()
    con.execute(f"COPY (SELECT {select} FROM '{src}') TO '{dst}' (FORMAT parquet)")
    con.close()
    return dst


def test_json_value_reads_booleans_and_numbers() -> None:
    con = duckdb.connect()
    row = con.execute(f"""SELECT {_json_value('flag', 'Boolean')}, {_json_value('n', 'Real')}, {_json_value('a:b', 'String')}
                          FROM (SELECT '{{"flag": true, "n": 3, "a:b": "x"}}' AS json)""").fetchone()
    assert row == (True, 3.0, "x")


@pytest.mark.parametrize(("expr", "duck_type", "expected"), [
    ("CAST(123.0 AS DOUBLE)", "DOUBLE", "123"), ("CAST(123 AS BIGINT)", "BIGINT", "123"),
    ("' 123 '", "VARCHAR", "123"), ("CAST(1.5 AS DOUBLE)", "DOUBLE", "1.5"), ("''", "VARCHAR", None),
])
def test_key_text(expr: str, duck_type: str, expected: str | None) -> None:
    assert duckdb.connect().execute(f"SELECT {key_text(expr, duck_type)}").fetchone()[0] == expected


def test_numeric_ids_match_across_types_and_reid_by_source(extracts, tmp_path: Path) -> None:
    a, b = extracts
    a_num = _rewrite(a, tmp_path / "an.parquet",
                     "* REPLACE (CAST(building_id AS BIGINT) AS building_id), CAST(building_id AS BIGINT) AS osm_id")
    b_dbl = _rewrite(b, tmp_path / "bd.parquet",
                     "* REPLACE (CAST(building_id AS DOUBLE) AS building_id), CAST(building_id AS DOUBLE) AS osm_id")
    fa = (("building_id", "Integer64"), *FIELDS[1:], ("osm_id", "Integer64"))
    fb = (("building_id", "Real"), *FIELDS[1:], ("osm_id", "Real"))
    spec = CompareSpec(a=dataclasses.replace(_side("a", a_num), fields=fa),
                       b=dataclasses.replace(_side("b", b_dbl), fields=fb), mode="key")
    m = _run(spec, tmp_path)[0]["match"]
    assert (m["matched"], m["added"], m["removed"], m["reid_same_source"]) == (4, 1, 1, 0)


def test_vector_side_without_attributes(extracts, tmp_path: Path) -> None:
    a, b = extracts
    bare = dict(fields=(), id_col=None, rule=None, prov_col=None, prov_missing=(), category_cols=())
    spec = CompareSpec(a=dataclasses.replace(_side("a", a), **bare), b=dataclasses.replace(_side("b", b), **bare),
                       mode="location")
    result, engine = _run(spec, tmp_path)
    assert result["match"]["matched"] == 3
    assert engine.write_outputs(tmp_path)["added"]["rows"] == 2


def test_reserved_column_names_are_skipped(extracts, tmp_path: Path) -> None:
    a, b = extracts
    a2 = _rewrite(a, tmp_path / "a2.parquet", "*, 'x' AS _key, 1.0 AS _area")
    fields = (*FIELDS, ("_key", "String"), ("_area", "Real"))
    spec = CompareSpec(a=dataclasses.replace(_side("a", a2), fields=fields), b=_side("b", b), mode="key")
    result, _ = _run(spec, tmp_path)
    assert result["sides"]["a"]["skipped_fields"] == ["_key", "_area"]
    assert result["match"]["matched"] == 4
    assert result["sides"]["a"]["total_area_m2"] > 1000
    with pytest.raises(DiffError, match="trùng tên"):
        DiffEngine(duckdb.connect(), CompareSpec(a=dataclasses.replace(_side("a", a2), fields=fields, id_col="_key"),
                                                 b=_side("b", b), mode="key"))


def test_dates_in_report_are_json_safe(extracts, tmp_path: Path) -> None:
    import json

    a, b = extracts
    a2 = _rewrite(a, tmp_path / "a2.parquet", "*, DATE '2024-05-01' AS config_version, TIMESTAMPTZ '2024-05-01 10:00:00+00' AS t")
    fields = (*FIELDS, ("config_version", "Date"), ("t", "DateTime"))
    spec = CompareSpec(a=dataclasses.replace(_side("a", a2), fields=fields, category_cols=("building_tier", "t")),
                       b=_side("b", b), mode="key")
    result, _ = _run(spec, tmp_path)
    assert result["sides"]["a"]["values"]["config_version"]["top"][0]["value"] == "2024-05-01"
    json.dumps(result)


def test_lost_geometry_counts_as_changed(extracts, tmp_path: Path) -> None:
    a, b = extracts
    nulls = ", ".join(f'CASE WHEN building_id = \'2\' THEN NULL ELSE "{c}" END AS "{c}"'
                      for c in ("__area", "__cx", "__cy", "__x0", "__x1", "__y0", "__y1", "__gtype", "__gh"))
    b2 = _rewrite(b, tmp_path / "b2.parquet", f"* REPLACE ({nulls})")
    result, engine = _run(CompareSpec(a=_side("a", a), b=_side("b", b2), mode="key", ignore_cols=("build_id",)), tmp_path)
    assert result["changes"]["geometry"]["nogeom"] == 1 and result["match"]["changed"] == 4
