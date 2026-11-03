"""Direct OSM source (app/osmtags.py, app/osmgeom.py, app/osmdirect.py).

Everything but the last test is pure Python or DuckDB-only; the end-to-end build needs GDAL +
tippecanoe and the DuckDB spatial extension and is skipped otherwise. Overpass is never contacted:
the client takes a fake transport.
"""

import json
import sqlite3
import urllib.error
from contextlib import closing
from pathlib import Path

import pytest

from app import osmbase, osmdirect, osmgeom, osmtags, tools
from app.jobs import FileRegistry, Job

AREA = osmbase.AREAS["hanoi"]


# ------------------------------------------------------------------ osmtags

@pytest.mark.parametrize("raw, expected", [
    ("12", 12.0), ("12.5", 12.5), ("12,5", 12.5), (" 12 m ", 12.0), ("12m", 12.0), ("12 meters", 12.0),
    ("40'", 12.192), ("40 ft", 12.192), ("12'6\"", 3.81), ("100 feet", 30.48),
    ("", None), (None, None), ("tall", None), ("10-12", None), ("-5", None), ("0", None),
    ("5000", None), ("12 km", None), ("1.2.3", None),
])
def test_parse_length(raw, expected):
    assert osmtags.parse_length(raw) == expected


@pytest.mark.parametrize("raw, minimum, expected", [
    ("5", 0, 5.0), ("3;4", 0, 4.0), ("2,5", 0, 2.5), ("0", 0, 0.0), ("0", 1, None), ("-1", 0, None),
    ("many", 0, None), ("5;x", 0, None), ("999", 0, None), (None, 0, None), ("nan", 0, None),
])
def test_parse_levels(raw, minimum, expected):
    assert osmtags.parse_levels(raw, minimum=minimum) == expected


def test_kind():
    assert osmtags.kind({"building": "yes"}) == "building"
    assert osmtags.kind({"building": "no"}) is None
    assert osmtags.kind({"building:part": "yes"}) == "part"
    assert osmtags.kind({"building:part": "yes", "building": "yes"}) == "part"
    assert osmtags.kind({"building:part": "no", "building": "house"}) == "building"
    assert osmtags.kind({"building": "yes", "location": "underground"}) is None
    assert osmtags.kind({"building": "yes", "tunnel": "building_passage"}) is None
    assert osmtags.kind({"highway": "residential"}) is None


def test_building_attrs():
    a = osmtags.building_attrs({
        "building": "office", "height": "120 m", "building:levels": "30", "building:min_level": "2",
        "roof:shape": "flat", "roof:height": "3", "name": "Tháp A", "building:colour": "grey",
    }, 3.5)
    assert (a["class"], a["height"], a["num_floors"], a["min_floor"], a["min_height"]) == ("office", 120.0, 30.0, 2.0, 7.0)
    assert (a["roof_shape"], a["roof_height"], a["name"], a["facade_color"]) == ("flat", 3.0, "Tháp A", "grey")
    # an explicit min_height wins over min_level; "yes" carries no class; a bad height is dropped
    b = osmtags.building_attrs({"building": "yes", "min_height": "10", "building:min_level": "2", "height": "tall"}, 3.5)
    assert (b["class"], b["min_height"], b["height"]) == (None, 10.0, None)
    # a part: class comes from building:part
    p = osmtags.building_attrs({"building:part": "roof", "height": "8"}, 3.5)
    assert (p["class"], p["height"]) == ("roof", 8.0)


def test_tag_issues():
    got = osmtags.tag_issues({"building": "yes", "height": "tall", "building:levels": "0", "min_height": "2",
                              "roof:height": "10-12", "building:min_level": "-1"})
    assert sorted(k for k, _ in got) == ["building:levels", "building:min_level", "height", "roof:height"]
    assert osmtags.tag_issues({"building": "yes", "height": "12"}) == []


# ------------------------------------------------------------------ osmgeom

SQ = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)]


def test_stitch_joins_open_ways_in_any_direction():
    a, b, c = [(0, 0), (1, 0)], [(1, 1), (1, 0)], [(1, 1), (0, 1), (0, 0)]  # b is reversed
    rings, bad = osmgeom.stitch([a, b, c])
    assert bad == 0 and len(rings) == 1 and rings[0][0] == rings[0][-1] and len(rings[0]) == 5
    rings, bad = osmgeom.stitch([a, b])  # not closed
    assert rings == [] and bad >= 1


def test_polygons_assigns_holes_to_the_containing_outer():
    big = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    far = [(20, 0), (30, 0), (30, 10), (20, 10), (20, 0)]
    hole = [(2, 2), (3, 2), (3, 3), (2, 3), (2, 2)]
    lost = [(50, 50), (51, 50), (51, 51), (50, 51), (50, 50)]
    polys, bad = osmgeom.polygons([far, big], [hole, lost])
    assert bad == 0 and len(polys) == 2
    host = next(p for p in polys if p[0] == big)
    assert host[1:] == [hole]
    assert all(len(p) == 1 for p in polys if p[0] == far)


def test_to_wkt_orientation_and_kind():
    cw = SQ[::-1]
    wkt = osmgeom.to_wkt([[cw]])
    assert wkt.startswith("POLYGON ((") and "0.0000000 0.0000000, 1.0000000 0.0000000" in wkt  # made CCW
    assert osmgeom.to_wkt([[SQ], [[(5, 5), (6, 5), (6, 6), (5, 5)]]]).startswith("MULTIPOLYGON")
    assert osmgeom.closed_way([(0, 0), (1, 1), (0, 0)]) == []  # no area
    assert osmgeom.closed_way(SQ[:-1]) == []  # open


def test_grid_and_box_touches():
    cells = osmgeom.grid((0, 0, 1, 0.5), 0.5)
    assert cells == [(0, 0, 0.5, 0.5), (0.5, 0, 1, 0.5)]
    tri = [[[(0.0, 0.0), (0.6, 0.0), (0.0, 0.6), (0.0, 0.0)]]]
    assert osmgeom.box_touches(tri, (0, 0, 0.5, 0.5))
    assert not osmgeom.box_touches(tri, (0.8, 0.8, 1, 1))
    assert len(osmgeom.split((0, 0, 1, 1))) == 4


# ------------------------------------------------------------------ Overpass answers

def _way(i, pts, **tags):
    return {"type": "way", "id": i, "version": 3, "timestamp": "2026-11-01T10:00:00Z", "tags": tags,
            "geometry": [{"lat": y, "lon": x} for x, y in pts]}


def _sq(x, y, d=0.0004):
    return [(x, y), (x + d, y), (x + d, y + d), (x, y + d), (x, y)]


def test_parse_overpass_ways_and_relations():
    outer, inner = _sq(105.80, 21.0, 0.001), _sq(105.8003, 21.0003, 0.0002)
    data = {"osm3s": {"timestamp_osm_base": "2026-11-03T04:00:00Z"}, "elements": [
        _way(1, _sq(105.85, 21.03), building="office", height="120"),
        _way(2, _sq(105.86, 21.03), **{"building:part": "yes", "height": "tall"}),
        _way(3, _sq(105.87, 21.03), highway="service"),  # not a building
        _way(4, _sq(105.88, 21.03)[:3], building="yes"),  # open
        {"type": "relation", "id": 9, "tags": {"type": "multipolygon", "building": "yes"}, "members": [
            {"type": "way", "role": "outer", "geometry": [{"lat": y, "lon": x} for x, y in outer[:3]]},
            {"type": "way", "role": "outer", "geometry": [{"lat": y, "lon": x} for x, y in outer[2:]]},
            {"type": "way", "role": "inner", "geometry": [{"lat": y, "lon": x} for x, y in inner]}]},
        {"type": "relation", "id": 10, "tags": {"type": "building", "building": "yes"}, "members": []},
        {"type": "node", "id": 5, "tags": {"building": "yes"}},
    ]}
    c = osmdirect.Counters()
    got = {f.building_id: f for f in osmdirect.parse_overpass(data, c)}
    assert set(got) == {"way/1", "way/2", "relation/9"}
    assert got["relation/9"].kind == "building" and len(got["relation/9"].polygons[0]) == 2  # outer + hole
    assert got["way/2"].kind == "part"
    assert (c.base_time, c.unclosed, c.features) == ("2026-11-03T04:00:00Z", 1, 3)
    assert c.issues == [("way/2", "height", "tall")] and c.newest_edit == "2026-11-01T10:00:00Z"
    row = osmdirect.raw_row(got["way/1"])
    assert row[:2] == ("way/1", "building") and row[5] == 120.0 and row[-3:-1] == (3, "2026-11-01T10:00:00Z")
    assert len(row) == 18 and osmdirect.INSERT_SQL.count("?") == 18


def test_parse_boundary():
    ring = _sq(105.0, 20.0, 1.0)
    halves = [ring[:3], ring[2:]]
    data = {"elements": [{"type": "relation", "id": 1, "members": [
        {"type": "way", "role": "outer", "geometry": [{"lat": y, "lon": x} for x, y in w]} for w in halves]}]}
    polys = osmdirect.parse_boundary(data)
    assert len(polys) == 1 and osmgeom.bbox(polys) == (105.0, 20.0, 106.0, 21.0)
    assert osmdirect.parse_boundary({"elements": []}) == []
    with pytest.raises(osmdirect.ConfigError):
        osmdirect.boundary_query('VN-HN"];out;')


def test_queries():
    q = osmdirect.building_query((105.8, 21.0, 105.85, 21.05))
    assert q.count("(21.000000,105.800000,21.050000,105.850000)") == 4  # overpass wants s,w,n,e
    assert 'way["building:part"]' in q and q.endswith("out geom meta qt;")
    assert "VN-HN" in osmdirect.boundary_query("VN-HN")


def test_parse_hstore_and_geojsonseq():
    assert osmdirect.parse_hstore('"building"=>"yes","name"=>"Nhà \\"A\\"","x"=>NULL') == {
        "building": "yes", "name": 'Nhà "A"', "x": ""}
    feat = {"type": "Feature", "properties": {"osm_id": None, "osm_way_id": "42", "osm_version": "5",
            "osm_timestamp": "2026-10-01T00:00:00Z", "all_tags": '"building:part"=>"yes","height"=>"9 m"'},
            "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in SQ]]}}
    rel = {"type": "Feature", "properties": {"osm_id": "7", "osm_way_id": None, "all_tags": '"building"=>"yes"'},
           "geometry": {"type": "MultiPolygon", "coordinates": [[[list(p) for p in SQ]]]}}
    skip = {"type": "Feature", "properties": {"osm_way_id": "1", "all_tags": '"landuse"=>"farm"'}, "geometry": None}
    lines = ["\x1e" + json.dumps(feat), json.dumps(rel), json.dumps(skip), ""]
    got = list(osmdirect.parse_geojsonseq(iter(lines)))
    assert [(f.building_id, f.kind, f.version) for f in got] == [("way/42", "part", 5), ("relation/7", "building", None)]


def test_osm_file_command():
    cmd = osmdirect.osm_file_command("ogr2ogr", Path("v.osm.pbf"), Path("o.geojsonl"), Path("c.ini"), (1, 2, 3, 4))
    assert cmd[:4] == ["ogr2ogr", "-f", "GeoJSONSeq", "o.geojsonl"] and "CONFIG_FILE=c.ini" in cmd
    assert cmd[cmd.index("-spat") + 1:cmd.index("-spat") + 5] == ["1", "2", "3", "4"]
    assert "building:part" in cmd[cmd.index("-where") + 1]


# ------------------------------------------------------------------ client

def test_overpass_retries_rotates_and_splits():
    calls: list[str] = []
    answers = iter([
        urllib.error.HTTPError("u", 429, "busy", {}, None),
        b"<html>gateway</html>",
        json.dumps({"elements": [], "remark": "runtime error: Query ran out of memory"}).encode(),
    ])

    def transport(url, ql):
        calls.append(url)
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    client = osmdirect.Overpass(("https://a", "https://b"), transport=transport, sleep=lambda s: None, pause=0)
    with pytest.raises(osmdirect.OverpassTooBig):
        client.query("q")
    assert calls == ["https://a", "https://b", "https://a"]

    def always_busy(url, ql):
        raise urllib.error.HTTPError(url, 504, "timeout", {}, None)

    with pytest.raises(osmdirect.OverpassError):
        osmdirect.Overpass(("https://a",), transport=always_busy, sleep=lambda s: None, pause=0).query("q")

    def not_found(url, ql):
        raise urllib.error.HTTPError(url, 400, "bad query", {}, None)

    with pytest.raises(osmdirect.OverpassError, match="HTTP 400"):
        osmdirect.Overpass(("https://a",), transport=not_found, sleep=lambda s: None, pause=0).query("q")


def test_fetch_tiles_splits_a_too_big_tile():
    seen: list[tuple[float, float, float, float]] = []

    class Fake:
        def query(self, ql):
            box = ql.split("way[\"building\"](")[1].split(")")[0].split(",")  # s,w,n,e
            s, w, n, e = map(float, box)
            seen.append((w, s, e, n))
            if e - w > 0.03:
                raise osmdirect.OverpassTooBig("memory")
            return {"elements": []}

    progress: list[tuple[int, int]] = []
    out = list(osmdirect.fetch_tiles(Fake(), [(0, 0, 0.05, 0.05)], on_tile=lambda d, t: progress.append((d, t))))
    assert len(out) == 4 and progress[-1] == (4, 4)  # the big tile was split in four
    assert seen[0] == (0, 0, 0.05, 0.05)


# ------------------------------------------------------------------ DuckDB

def _spatial_ok() -> bool:
    try:
        import duckdb

        osmbase.load_extension(duckdb.connect(), "spatial")
        return True
    except (ImportError, osmbase.BaseError):
        return False


needs_spatial = pytest.mark.skipif(not _spatial_ok(), reason="DuckDB spatial extension not available")
needs_tools = pytest.mark.skipif(
    any(tools.which(t) is None for t in ("ogr2ogr", "ogrinfo", "tippecanoe")) or not _spatial_ok(),
    reason="GDAL/tippecanoe or the DuckDB spatial extension not available",
)

HN = [(105.80, 21.00), (105.90, 21.00), (105.90, 21.06), (105.80, 21.06), (105.80, 21.00)]


def _hanoi_answer() -> dict:
    """Overpass answer: a tower with two parts (one mapped as part of an outline, one standalone),
    a house with only levels, one far outside the boundary, one tagged underground."""
    return {"osm3s": {"timestamp_osm_base": "2026-11-03T04:00:00Z"}, "elements": [
        _way(100, _sq(105.85, 21.03), building="office", height="120", name="Tháp A"),
        _way(101, _sq(105.8501, 21.0301, 0.0002), **{"building:part": "yes", "height": "150", "min_height": "20"}),
        _way(102, _sq(105.8502, 21.0302, 0.0001), **{"building:part": "yes", "building:min_level": "2"}),
        _way(103, _sq(105.88, 21.03, 0.0001), **{"building:part": "yes", "height": "6"}),  # no outline around it
        _way(200, _sq(105.86, 21.03), building="yes", **{"building:levels": "5"}),
        _way(300, _sq(105.95, 20.95), building="yes"),  # outside the boundary
        _way(400, _sq(105.84, 21.03), building="yes", location="underground"),
    ]}


@needs_spatial
def test_assemble_parents_parts_and_clips():
    import duckdb

    con = duckdb.connect()
    osmbase.load_extension(con, "spatial")
    con.execute(osmdirect.RAW_DDL)
    con.execute(f"CREATE TABLE area AS SELECT 'VN-HN' AS id, 'HN' AS name, ST_GeomFromText('{osmgeom.to_wkt([[HN]])}') AS geometry")
    con.executemany(osmdirect.INSERT_SQL, [osmdirect.raw_row(f) for f in osmdirect.parse_overpass(_hanoi_answer())])
    for sql in osmdirect.assemble_sql():
        con.execute(sql)
    rows = {r[0]: r[1:] for r in con.execute(
        "SELECT building_id, parent_building_id, height, num_floors, min_height, class FROM osm_buildings").fetchall()}
    assert rows == {
        "way/100": (None, 120.0, None, None, "office"),
        "way/101": ("way/100", 150.0, None, 20.0, "office"),  # a part takes its building's class
        "way/102": ("way/100", None, None, 7.0, "office"),
        "way/103": (None, 6.0, None, None, None),  # standalone part stays a building
        "way/200": (None, None, 5.0, None, None),
    }


class _Client:
    def __init__(self, data):
        self.data, self.queries = data, []

    def query(self, ql):
        self.queries.append(ql)
        if "boundary" in ql:
            return {"elements": [{"type": "relation", "id": 1, "members": [
                {"type": "way", "role": "outer", "geometry": [{"lat": y, "lon": x} for x, y in HN]}]}]}
        return self.data


@needs_tools
def test_direct_build_end_to_end(tmp_path: Path):
    base_dir = tmp_path / "base"
    job = Job(id="t", config={"area": "hanoi", "source": "overpass"}, name="base", kind="base")
    pipe = osmdirect.DirectBasePipeline(job, FileRegistry(), AREA, base_dir, tile_deg=0.5, client=_Client(_hanoi_answer()))
    from app.pipeline import run_job

    run_job(job, lambda: pipe)
    assert job.status == "done", (job.error, job.log[-20:])
    manifest = osmbase.read_manifest(base_dir, AREA)
    assert manifest["source_kind"] == "overpass" and manifest["osm_snapshot"] == "2026-11-03"
    assert manifest["counts"]["parts"] == 2 and manifest["counts"]["buildings"] == 3
    assert manifest["attribution"] == "© OpenStreetMap contributors (ODbL)"
    with closing(sqlite3.connect(manifest["gpkg_path"])) as conn:
        rows = {r[0]: r[1:] for r in conn.execute(
            "SELECT building_id, parent_building_id, height_m, h_src, has_parts, is_part FROM buildings")}
    assert rows["way/100"] == (None, 120.0, "height", 1, 0)
    assert rows["way/101"] == ("way/100", 150.0, "height", 0, 1)
    assert rows["way/200"] == (None, 17.5, "levels", 0, 0)
    assert "way/300" not in rows and "way/400" not in rows
