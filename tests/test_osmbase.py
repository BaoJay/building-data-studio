"""Base of OSM buildings (app/osmbase.py): SQL helpers, releases, manifest, and an end-to-end build
from a small local copy of the Overture layout (no network).

The end-to-end test needs GDAL + tippecanoe and the DuckDB spatial extension; skipped otherwise.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import duckdb
import pytest

from app import osmbase, tools
from app.jobs import FileRegistry, Job

AREA = osmbase.AREAS["hanoi"]
RELEASE = "2026-10-21.0"


def test_osm_id_expr():
    con = duckdb.connect()
    got = [con.execute(f"SELECT {osmbase.osm_id_expr(osmbase.sql_str(r))}").fetchone()[0]
           for r in ("w1464527161@1", "r402@12", "n7", "x1")]
    assert got == ["way/1464527161", "relation/402", "node/7", None]


def test_osm_source_is_the_geometry_source():
    con = duckdb.connect()
    src = "{'property': '', 'dataset': 'OpenStreetMap', 'record_id': 'w1@2'}"
    height_only = "{'property': 'properties/height', 'dataset': 'OpenStreetMap', 'record_id': 'w9@1'}"
    google = "{'property': '', 'dataset': 'Google Open Buildings', 'record_id': 'g1'}"
    sql = f"SELECT {osmbase.osm_source_expr()}.record_id FROM (SELECT [{{}}] AS sources)"
    assert con.execute(sql.format(f"{src}")).fetchone()[0] == "w1@2"
    assert con.execute(sql.format(f"{google}, {height_only}")).fetchone()[0] is None


def test_bbox_where_and_glob():
    assert osmbase.bbox_where((105.0, 20.0, 106.0, 21.0)) == (
        "bbox.xmin <= 106.0 AND bbox.xmax >= 105.0 AND bbox.ymin <= 21.0 AND bbox.ymax >= 20.0")
    assert osmbase.theme_glob("s3://b/release/", RELEASE, "buildings", "building") == (
        f"s3://b/release/{RELEASE}/theme=buildings/type=building/*.parquet")


def test_releases_local_root(tmp_path: Path):
    for name in ("2026-10-21.0", "2026-09-23.1", "2026-09-23.0", "latest", "2026-09-23.10"):
        (tmp_path / name).mkdir()
    assert osmbase.releases(str(tmp_path)) == ["2026-09-23.0", "2026-09-23.1", "2026-09-23.10", "2026-10-21.0"]
    assert osmbase.latest_release(str(tmp_path)) == "2026-10-21.0"
    with pytest.raises(osmbase.BaseError):
        osmbase.latest_release(str(tmp_path / "missing"))


def test_manifest_needs_its_files(tmp_path: Path):
    assert osmbase.read_manifest(tmp_path, AREA) is None
    folder = tmp_path / AREA.key / "b1"
    folder.mkdir(parents=True)
    osmbase.write_manifest(tmp_path, AREA, {"folder": "b1", "gpkg": "b1/x.gpkg", "pmtiles": "b1/x.pmtiles"})
    assert osmbase.read_manifest(tmp_path, AREA) is None  # files missing
    (folder / "x.gpkg").write_bytes(b"")
    (folder / "x.pmtiles").write_bytes(b"")
    got = osmbase.read_manifest(tmp_path, AREA)
    assert got["pmtiles_path"] == str(folder / "x.pmtiles") and got["folder_path"] == str(folder)


# ------------------------------------------------------------------ end to end

def _spatial_ok() -> bool:
    try:
        osmbase.load_extension(duckdb.connect(), "spatial")
        return True
    except osmbase.BaseError:
        return False


needs_tools = pytest.mark.skipif(
    any(tools.which(t) is None for t in ("ogr2ogr", "ogrinfo", "tippecanoe")) or not _spatial_ok(),
    reason="GDAL/tippecanoe or the DuckDB spatial extension not available",
)


def _box(x: float, y: float, d: float = 0.0004) -> str:
    return f"POLYGON(({x} {y}, {x + d} {y}, {x + d} {y + d}, {x} {y + d}, {x} {y}))"


def _src(dataset: str, record: str) -> str:
    return (f"[{{'property': '', 'dataset': '{dataset}', 'record_id': '{record}', "
            f"'update_time': '2026-01-06T06:20:59.000Z', 'version': '2026-09-12T00:00:00.000Z'}}]")


@pytest.fixture()
def overture(tmp_path: Path) -> Path:
    """A local tree shaped like s3://overturemaps-us-west-2/release, a few features around Hà Nội."""
    root = tmp_path / "release"
    con = duckdb.connect()
    osmbase.load_extension(con, "spatial")

    def write(theme: str, kind: str, rows: list[str], columns: str) -> None:
        folder = root / RELEASE / f"theme={theme}" / f"type={kind}"
        folder.mkdir(parents=True)
        values = ", ".join(f"({r})" for r in rows)
        # Columns of the buildings theme the tests leave empty.
        extra = "".join(f"NULL::{t} AS {c}, " for c, t in (
            ("roof_shape", "VARCHAR"), ("roof_height", "DOUBLE"), ("roof_color", "VARCHAR"), ("roof_material", "VARCHAR"),
            ("facade_color", "VARCHAR"), ("facade_material", "VARCHAR"))) if theme == "buildings" else ""
        con.execute(f"""COPY (SELECT *, {{'xmin': ST_XMin(geometry), 'xmax': ST_XMax(geometry),
                        'ymin': ST_YMin(geometry), 'ymax': ST_YMax(geometry)}} AS bbox FROM (
                          SELECT * EXCLUDE (wkt), {extra}ST_GeomFromText(wkt) AS geometry
                          FROM (VALUES {values}) t({columns})
                        )) TO '{folder / 'part-0.parquet'}' (FORMAT parquet)""")

    hn = "POLYGON((105.80 21.00, 105.90 21.00, 105.90 21.06, 105.80 21.06, 105.80 21.00))"
    write("divisions", "division_area", [
        f"'hn', {{'primary': 'Thành phố Hà Nội'}}, 'region', 'land', 'VN', 'VN-HN', '{hn}'",
        f"'hn-sea', {{'primary': 'Hà Nội (biển)'}}, 'region', 'maritime', 'VN', 'VN-HN', '{_box(105.8, 21.0, 0.01)}'",
        f"'hy', {{'primary': 'Hưng Yên'}}, 'region', 'land', 'VN', 'VN-HY', '{_box(105.95, 20.9, 0.1)}'",
    ], "id, names, subtype, class, country, region, wkt")
    cols = "id, names, sources, height, num_floors, min_height, min_floor, is_underground, class, subtype, wkt"
    write("buildings", "building", [
        (f"'b1', {{'primary': 'Tháp A'}}, {_src('OpenStreetMap', 'w100@3')}, 120.0, 30, NULL, NULL, false, "
         f"'office', 'commercial', '{_box(105.85, 21.03)}'"),
        (f"'b2', {{'primary': NULL}}, {_src('OpenStreetMap', 'w200@1')}, NULL, 5, NULL, NULL, false, "
         f"NULL, NULL, '{_box(105.86, 21.03)}'"),
        (f"'b3', {{'primary': NULL}}, {_src('Google Open Buildings', 'g1')}, NULL, NULL, NULL, NULL, false, "
         f"NULL, NULL, '{_box(105.87, 21.03)}'"),
        (f"'b4', {{'primary': NULL}}, {_src('OpenStreetMap', 'w300@1')}, NULL, NULL, NULL, NULL, false, "
         f"NULL, NULL, '{_box(105.95, 20.95)}'"),  # outside the boundary
        (f"'b5', {{'primary': NULL}}, {_src('OpenStreetMap', 'r400@2')}, 9.0, NULL, NULL, NULL, true, "
         f"NULL, NULL, '{_box(105.84, 21.03)}'"),  # underground
    ], cols)
    write("buildings", "building_part", [
        (f"'p1', {{'primary': NULL}}, {_src('OpenStreetMap', 'w101@1')}, 150.0, NULL, 20.0, NULL, false, "
         f"'b1', '{_box(105.8501, 21.0301, 0.0002)}'"),
        (f"'p2', {{'primary': NULL}}, {_src('OpenStreetMap', 'w102@1')}, NULL, NULL, NULL, 2, false, "
         f"'b1', '{_box(105.8502, 21.0302, 0.0001)}'"),
        (f"'p3', {{'primary': NULL}}, {_src('OpenStreetMap', 'w301@1')}, NULL, NULL, NULL, NULL, false, "
         f"'b4', '{_box(105.9501, 20.9501, 0.0001)}'"),  # its building is outside
    ], "id, names, sources, height, num_floors, min_height, min_floor, is_underground, building_id, wkt")
    return root


@needs_tools
def test_build_base_end_to_end(overture: Path, tmp_path: Path):
    base_dir = tmp_path / "base"
    job = Job(id="t", config={"area": "hanoi", "root": str(overture)}, name="base", kind="base")
    osmbase.execute(job, FileRegistry(), base_dir)
    assert job.status == "done", (job.error, job.log[-20:])

    manifest = osmbase.read_manifest(base_dir, AREA)
    assert manifest["release"] == RELEASE
    assert manifest["counts"] == {"buildings": 2, "parts": 2, "dropped_parts": 0}
    assert manifest["boundary"]["name"] == "Thành phố Hà Nội"
    assert manifest["osm_snapshot"] == "2026-09-12"
    assert "OpenStreetMap" in manifest["attribution"]

    gpkg = Path(manifest["gpkg_path"])
    with closing(sqlite3.connect(gpkg)) as conn:
        rows = {r[0]: r[1:] for r in conn.execute(
            "SELECT building_id, parent_building_id, h_m, h_src, has_parts, is_part, min_height, class FROM buildings")}
        layers = {r[0] for r in conn.execute("SELECT table_name FROM gpkg_contents")}
    assert layers == {"buildings", "boundary"}
    assert rows == {
        "way/100": (None, 120.0, "height", 1, 0, None, "office"),
        "way/200": (None, 17.5, "levels", 0, 0, None, None),
        "way/101": ("way/100", 150.0, "height", 0, 1, 20.0, "office"),
        "way/102": ("way/100", 4.0, "default", 0, 1, 7.0, "office"),  # min_level 2 × 3.5 m
    }
    report = json.loads((Path(manifest["folder_path"]) / "report.json").read_text(encoding="utf-8"))
    assert report["accounting"]["pmtiles_features"] == 4
