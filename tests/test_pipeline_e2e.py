"""End-to-end: plain Parquet (WKT, no CRS) -> GPKG + PMTiles, then GPKG -> PMTiles.

Needs GDAL + tippecanoe on PATH; skipped otherwise. Set BC_SAMPLE_PARQUET to a
real export to also run the pipeline on it.
"""

import csv
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from app import pipeline, tools
from app.jobs import FileRegistry, Job

pytestmark = pytest.mark.skipif(
    any(tools.which(t) is None for t in ("ogr2ogr", "ogrinfo", "tippecanoe")),
    reason="GDAL/tippecanoe not installed",
)

# id, height_m, provenance, parent, tier
BUILDINGS = [
    ("A", 30.0, "osm_height", None, "T2_large"),
    ("A1", 45.0, "osm_height", "A", "T2_large"),
    ("B", 4.0, "default", None, "T3_residential"),
    ("C", 49380.0, "osm_levels", None, "T2_commercial"),
    ("D", 0.0, "osm_height", None, "T3_residential"),
    ("E", 12.36, "osm_height", None, "T3_residential"),
]


def _square(i: int) -> list:
    x, y = 106.70 + i * 0.001, 10.77
    return [[[x, y], [x + 0.0005, y], [x + 0.0005, y + 0.0005], [x, y + 0.0005], [x, y]]]


@pytest.fixture()
def plain_parquet(tmp_path: Path) -> Path:
    features = [
        {
            "type": "Feature",
            "properties": {"building_id": b, "height_m": h, "height_provenance": p,
                           "parent_building_id": parent, "building_tier": tier},
            "geometry": {"type": "Polygon", "coordinates": _square(i)},
        }
        for i, (b, h, p, parent, tier) in enumerate(BUILDINGS)
    ]
    src = tmp_path / "mini.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    out = tmp_path / "mini.parquet"
    # Mimic the company export: geometry stored as text, no GeoParquet metadata, no CRS.
    sql = ("SELECT building_id, height_m, height_provenance, parent_building_id, building_tier, "
           "ST_AsText(geometry) AS geometry_wkt FROM mini")
    subprocess.run([tools.require("ogr2ogr"), "-f", "Parquet", str(out), str(src), "-dialect", "SQLite",
                    "-sql", sql, "-nlt", "NONE"], check=True, capture_output=True)
    return out


def _config(path: Path, out_dir: Path, **overrides) -> dict:
    cfg = {
        "input": {"path": str(path), "src_crs": "EPSG:4326"},
        "output": {"dir": str(out_dir), "name": "mini", "gpkg": True, "pmtiles": True, "gpkg_crs": "EPSG:4326"},
        "attributes": {},
        "height": {"enabled": True, "height_col": "height_m", "prov_col": "height_provenance",
                   "prov_missing": ["default"], "min_valid_m": 2, "max_valid_m": 500},
        "parts": {"enabled": True, "id_col": "building_id", "parent_col": "parent_building_id"},
        "tiles": {"layer": "buildings", "minzoom": 14, "maxzoom": 15,
                  "attributes": ["building_id", "height_m", "has_parts", "is_part"]},
        "report": {"category_cols": ["height_provenance", "building_tier"]},
    }
    for key, value in overrides.items():
        cfg[key] = {**cfg[key], **value}
    return cfg


def _run(cfg: dict) -> dict:
    job = Job(id="test", config=cfg, name="mini")
    pipeline.execute(job, FileRegistry())
    result = job.to_dict()
    assert result["status"] == "done", (result["error"], "\n".join(result["log"][-30:]))
    return result


def _rows(gpkg: Path) -> dict:
    with closing(sqlite3.connect(gpkg)) as conn:
        return {r[0]: r[1:] for r in conn.execute(
            'SELECT building_id, h_raw, height_m, h_src, h_outlier, has_parts, is_part FROM "buildings"')}


def test_plain_parquet_to_gpkg_and_pmtiles(plain_parquet: Path, tmp_path: Path) -> None:
    result = _run(_config(plain_parquet, tmp_path / "out"))
    gpkg = Path(result["outputs"]["gpkg"]["path"])
    # height_m is the derived height; the measured one it replaced stays in h_raw.
    assert _rows(gpkg) == {
        "A": (30.0, 30.0, "height", 0, 1, 0),
        "A1": (45.0, 45.0, "height", 0, 0, 1),
        "B": (4.0, 3.5, "default", 0, 0, 0),
        "C": (49380.0, 3.5, "default", 1, 0, 0),
        "D": (0.0, 3.5, "default", 1, 0, 0),
        "E": (12.36, 12.4, "height", 0, 0, 0),
    }
    report = result["report"]
    assert report["total"] == 6
    assert report["height"]["real_count"] == 3
    assert report["height"]["outlier_count"] == 2
    assert report["accounting"]["input_count"] == 6
    assert report["accounting"]["pmtiles_features"] == 6
    assert report["pmtiles"]["verified"] in (True, None) or tools.which("pmtiles") is None
    assert {o["building_id"] for o in report["outliers"]} == {"C", "D"}
    # The outlier table and the raw-column summary show the measured value, not the derived one.
    assert {o["building_id"]: o["h_raw"] for o in report["outliers"]} == {"C": 49380.0, "D": 0.0}
    assert (report["height"]["raw"]["column"], report["height"]["raw"]["max"]) == ("h_raw", 49380.0)
    assert Path(result["outputs"]["pmtiles"]["path"]).stat().st_size > 0
    assert Path(result["outputs"]["report_md"]["path"]).read_text(encoding="utf-8").startswith("# Báo cáo")
    assert not (Path(result["output_dir"]) / "_work").exists()


def test_gpkg_input_reprocessed_to_pmtiles_only(plain_parquet: Path, tmp_path: Path) -> None:
    first = _run(_config(plain_parquet, tmp_path / "out"))
    gpkg = first["outputs"]["gpkg"]["path"]
    cfg = _config(Path(gpkg), tmp_path / "out2", output={"gpkg": False, "pmtiles": True})
    cfg["input"] = {"path": gpkg}
    second = _run(cfg)
    assert "gpkg" not in second["outputs"]
    assert second["report"]["accounting"]["pmtiles_features"] == 6
    # The output's height_m is already cleaned (edits made in QGIS are kept), so nothing is out of range.
    assert second["report"]["height"]["outlier_count"] == 0


def test_reprojected_gpkg_output(plain_parquet: Path, tmp_path: Path) -> None:
    cfg = _config(plain_parquet, tmp_path / "out", output={"pmtiles": False, "gpkg_crs": "EPSG:32648"})
    result = _run(cfg)
    gpkg = Path(result["outputs"]["gpkg"]["path"])
    with closing(sqlite3.connect(gpkg)) as conn:
        srs = conn.execute("SELECT srs_id FROM gpkg_geometry_columns").fetchone()[0]
    assert srs == 32648
    assert len(_rows(gpkg)) == 6


def test_where_filter_is_accounted(plain_parquet: Path, tmp_path: Path) -> None:
    cfg = _config(plain_parquet, tmp_path / "out", attributes={"where": "building_tier = 'T3_residential'"},
                  output={"pmtiles": False})
    result = _run(cfg)
    assert result["report"]["accounting"]["filtered_out"] == 3


def test_empty_column_list_keeps_only_required_columns(plain_parquet: Path, tmp_path: Path) -> None:
    cfg = _config(plain_parquet, tmp_path / "out", attributes={"columns": []}, output={"pmtiles": False})
    result = _run(cfg)
    with closing(sqlite3.connect(result["outputs"]["gpkg"]["path"])) as conn:
        cols = [r[1] for r in conn.execute('PRAGMA table_info("buildings")')]
    # Parts ids are kept because has_parts needs them; height inputs are not copied.
    assert cols == ["fid", "geom", "building_id", "parent_building_id", "h_raw", "height_m", "h_src", "h_outlier",
                    "has_parts", "is_part"]


def test_csv_only_output_for_qc(plain_parquet: Path, tmp_path: Path) -> None:
    pytest.importorskip("h3")
    cfg = _config(plain_parquet, tmp_path / "out", output={"gpkg": False, "pmtiles": False, "csv": True})
    result = _run(cfg)
    path = Path(result["outputs"]["csv"]["path"])
    assert path.name == "mini.csv" and path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert "gpkg" not in result["outputs"] and "pmtiles" not in result["outputs"]
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert sorted(r["building_id"] for r in rows) == sorted(b[0] for b in BUILDINGS)
    assert list(rows[0])[:4] == ["h3_r8", "lat", "lon", "map_link"]
    assert "geom" not in rows[0] and "fid" not in rows[0] and rows[0]["height_m"]


def test_no_output_is_rejected(plain_parquet: Path, tmp_path: Path) -> None:
    with pytest.raises(pipeline.ConfigError):
        pipeline.parse_config(_config(plain_parquet, tmp_path / "out", output={"gpkg": False, "pmtiles": False}))


def test_missing_crs_is_rejected(plain_parquet: Path, tmp_path: Path) -> None:
    cfg = _config(plain_parquet, tmp_path / "out")
    cfg["input"]["src_crs"] = None
    job = Job(id="t", config=cfg, name="mini")
    pipeline.execute(job, FileRegistry())
    assert job.status == "failed" and "CRS" in (job.error or "")


@pytest.mark.skipif(not os.environ.get("BC_SAMPLE_PARQUET"), reason="BC_SAMPLE_PARQUET not set")
def test_real_sample(tmp_path: Path) -> None:
    sample = Path(os.environ["BC_SAMPLE_PARQUET"])
    cfg = _config(sample, tmp_path / "out")
    cfg["tiles"]["minzoom"], cfg["tiles"]["maxzoom"] = 13, 15
    result = _run(cfg)
    acc = result["report"]["accounting"]
    assert acc["input_count"] == acc["normalized_count"] == acc["pmtiles_features"]


@pytest.mark.skipif(tools.which("tile-join") is None, reason="tile-join not installed")
def test_pmtiles_with_shadows_layer(plain_parquet: Path, tmp_path: Path, monkeypatch) -> None:
    from app import probe, vmap

    def offline(path, ttl):  # every sun comes from its mobile style: use the repo copies
        raise vmap.UpstreamUnreachable("offline")

    monkeypatch.setattr(vmap, "_fetch", offline)
    monkeypatch.setattr(vmap, "_offline_until", 0.0)
    result = _run(_config(plain_parquet, tmp_path / "out", tiles={"shadows": True, "minzoom": 14, "maxzoom": 16}))
    sh = result["report"]["shadows"]
    assert set(sh["presets"]) == {"dawn", "day", "dusk"}
    assert (sh["presets"]["dusk"]["azimuth"], sh["presets"]["dusk"]["altitude"]) == (240, 10)
    assert sh["casting"] == 5  # A is drawn by its part A1
    assert sh["features"] > 0 and (sh["minzoom"], sh["maxzoom"]) == (15, 16)
    assert any("shadow_tiles" == s["key"] and s["status"] == "done" for s in result["steps"])

    info = probe.probe(Path(result["outputs"]["pmtiles"]["path"]))
    if tools.which("pmtiles") is None:
        return
    assert info["layers"] == ["buildings", "shadows"]
    assert info["layer"] == "buildings"
    assert info["feature_count"] == 6  # tilestats put back after tile-join
    shadow_layer = next(v for v in info["vector_layers"] if v["id"] == "shadows")
    assert shadow_layer["fields"] == {"preset": "String"}


def test_shadows_need_heights(plain_parquet: Path, tmp_path: Path) -> None:
    cfg = _config(plain_parquet, tmp_path / "out", tiles={"shadows": True}, height={"enabled": False})
    with pytest.raises(pipeline.ConfigError):
        pipeline.parse_config(cfg)
    with pytest.raises(pipeline.ConfigError):
        pipeline.parse_config(_config(plain_parquet, tmp_path / "out", tiles={"shadows": True, "layer": "shadows"}))


@pytest.fixture()
def canonical_parquet(tmp_path: Path) -> Path:
    """A canonical export with height_m stored as text, a NULL first, and a column the schema lacks."""
    rows = [
        ("A", None, "default", "10", "2026-10-28T01:02:03Z", "x"),
        ("B", "12.5", "osm_height", None, "2026-10-28T01:02:04Z", None),
        ("C", "abc", "osm_levels", None, None, None),
    ]
    features = [
        {"type": "Feature",
         "properties": {"building_id": b, "height_m": h, "height_provenance": p, "min_height": mh,
                        "updated_at": ts, "superseded_by": sup, "status": "active"},
         "geometry": {"type": "Polygon", "coordinates": _square(i)}}
        for i, (b, h, p, mh, ts, sup) in enumerate(rows)
    ]
    src = tmp_path / "canon.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    out = tmp_path / "canon.parquet"
    subprocess.run([tools.require("ogr2ogr"), "-f", "Parquet", str(out), str(src)], check=True, capture_output=True)
    return out


def _tile_rows(pmtiles: Path, zoom: int) -> dict:
    proc = subprocess.run([tools.require("ogr2ogr"), "-f", "GeoJSON", "/vsistdout/", str(pmtiles), "buildings",
                           "-oo", f"ZOOM_LEVEL={zoom}"], check=True, capture_output=True, text=True)
    return {f["properties"]["building_id"]: f["properties"] for f in json.loads(proc.stdout)["features"]}


def test_pmtiles_follow_the_tile_schema(canonical_parquet: Path, tmp_path: Path) -> None:
    cfg = _config(canonical_parquet, tmp_path / "out", height={"enabled": False}, parts={"enabled": False},
                  tiles={"schema": True, "layer": "ignored", "minzoom": 14, "maxzoom": 15, "attributes": ["status"]},
                  report={"category_cols": []})
    result = _run(cfg)
    ts = result["report"]["tile_schema"]
    assert ts["name"] == "vmap-buildings"
    assert ts["dropped"] == ["superseded_by"]
    assert "build_id" in ts["missing_in_data"] and "height_m" not in ts["missing_in_data"]

    tiles = Path(result["outputs"]["pmtiles"]["path"])
    rows = _tile_rows(tiles, 16)
    # h_m and height repeat height_m; text that is not a number is left out, not 0.
    assert {k: rows["B"].get(k) for k in ("h_m", "height", "height_m", "min_height")} == {
        "h_m": 12.5, "height": 12.5, "height_m": 12.5, "min_height": None}
    assert rows["A"].get("height_m") is None and rows["C"].get("height_m") is None
    assert rows["A"]["min_height"] == "10" and rows["B"]["updated_at"].startswith("2026-10-28")
    assert "superseded_by" not in rows["A"]

    if tools.which("pmtiles") is None:
        return
    chk = ts["check"]
    # Schema layer and zooms win over the UI's; the first height_m is NULL, yet it is typed Number.
    assert (chk["layer_found"], chk["zooms"], chk["extra"], chk["wrong_type"]) == (True, [13, 16], [], [])
    assert set(chk["missing"]) == set(ts["missing_in_data"])
    assert chk["matched"] == 8  # building_id, h_m, height, height_m, height_provenance, min_height, status, updated_at
    md = Path(result["outputs"]["report_md"]["path"]).read_text(encoding="utf-8")
    assert "## Schema PMTiles `vmap-buildings`" in md and "superseded_by" in md


def test_footprint_overlap_report(tmp_path: Path) -> None:
    def box(x0: float, w: float) -> list:
        x, y = 106.70 + x0, 10.77
        return [[[x, y], [x + w, y], [x + w, y + 0.0005], [x, y + 0.0005], [x, y]]]

    # X and Y share half of X; Z is alone; P is a part of X (overlaps by design, left out).
    shapes = [("X", None, box(0, 0.0005)), ("Y", None, box(0.00025, 0.001)),
              ("Z", None, box(0.01, 0.0005)), ("P", "X", box(0, 0.0005))]
    src = tmp_path / "ov.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"building_id": b, "parent_building_id": p, "height_m": 10.0},
         "geometry": {"type": "Polygon", "coordinates": c}} for b, p, c in shapes]}), encoding="utf-8")
    cfg = _config(src, tmp_path / "out", output={"pmtiles": False},
                  height={"prov_col": None}, report={"category_cols": []})
    ov = _run(cfg)["report"]["overlap"]
    # X = 1 unit, Y = 2 units, Z = 1 unit, overlap = 0.5 unit -> 0.5 / 4.
    assert ov["ratio_pct"] == pytest.approx(12.5, abs=0.1)
    assert (ov["pairs"], ov["overlapping_footprints"], ov["footprints"]) == (1, 2, 3)
    top = ov["top"]
    assert [(r["building_id"], r["worst_with"]) for r in top] == [("X", "Y"), ("Y", "X")]
    assert top[0]["overlap_pct"] == pytest.approx(50, abs=0.5)
    assert top[0]["center"][1] == pytest.approx(10.77025, abs=1e-6)


def _box(x: float, y: float, w: float, h: float) -> list:
    return [[[x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]]]


def test_building_tier_v2(tmp_path: Path) -> None:
    """[B-05] tier v2 on every footprint: tags from an OSM snapshot joined by source_id, a part
    inheriting from the footprint holding it, a footprint with no OSM id (GOB)."""
    import duckdb

    x, y = 105.80, 21.02
    feats = [  # source_id, height_m, provenance, geometry (~0.001° ≈ 100 m)
        ("way/1", 3.5, "default", _box(x, y, 0.0015, 0.0012)),            # 'Landmark' footprint ~17,000 m²
        ("way/2", 336.0, "osm_height", _box(x + 0.0002, y + 0.0002, 0.0004, 0.0004)),  # its tower (part)
        ("way/3", 7.8, "osm_height", _box(x + 0.01, y, 0.0003, 0.0004)),    # historic=monument
        ("way/4", 3.5, "default", _box(x + 0.02, y, 0.00002, 0.00002)),     # garage
        ("7PH7XXXX+XX", 3.5, "default", _box(x + 0.03, y, 0.0001, 0.0001)),  # GOB, no tags
        ("way/6", 3.5, "default", _box(x + 0.05, y, 0.0002, 0.0002)),       # part with no footprint
    ]
    src = tmp_path / "canon.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"source_id": s, "height_m": h, "height_provenance": p,
                                           "building_tier": "T3_residential"},
         "geometry": {"type": "Polygon", "coordinates": g}} for s, h, p, g in feats]}), encoding="utf-8")
    snap = tmp_path / "osm.parquet"
    osm = [("way/1", "building", {"building": "commercial", "name": "Landmark72"}),
           ("way/2", "part", {"building:part": "yes", "building:levels": "72"}),
           ("way/3", "building", {"building": "yes", "historic": "monument", "name": "Lăng"}),
           ("way/4", "building", {"building": "garage"}),
           ("way/6", "part", {"building:part": "yes"})]
    with duckdb.connect() as con:
        con.execute("CREATE TABLE o (osm_id VARCHAR, kind VARCHAR, tags VARCHAR)")
        con.executemany("INSERT INTO o VALUES (?, ?, ?)", [(i, k, json.dumps(t)) for i, k, t in osm])
        con.execute(f"COPY o TO '{snap}' (FORMAT PARQUET)")
    cfg = _config(src, tmp_path / "out", parts={"enabled": False}, output={"pmtiles": False},
                  report={"category_cols": ["building_tier"]})
    cfg["height"]["prov_col"] = "height_provenance"
    cfg["tier"] = {"enabled": True, "osm_tags": str(snap)}
    result = _run(cfg)
    gpkg = Path(result["outputs"]["gpkg"]["path"])
    with closing(sqlite3.connect(gpkg)) as conn:
        rows = {r[0]: r[1:] for r in conn.execute(
            'SELECT source_id, building_tier, height_class, tier_rule, tier_basis, qc_flags FROM "buildings"')}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert rows["way/1"] == ("T2", "HC0", "t2_scale_medium_area", "gfa_10000_to_30000", "flag_needs_levels")
    assert rows["way/2"] == ("T2", "HC4", "t2_scale_medium_area", "gfa_10000_to_30000", None)
    assert rows["way/3"] == ("T1", "HC0", "t1_national_provincial_landmark", "osm_historic_monument", None)
    assert rows["way/4"] == ("T3", "HC0", "t3_minor_structure", "minor_tag", "flag_minor_structure")
    assert rows["7PH7XXXX+XX"][0] == "T3" and rows["7PH7XXXX+XX"][2] == "t3_default"
    assert rows["way/6"] == (None, "HC0", None, None, None)
    assert "_bds_tier_v2" not in tables
    tier = result["report"]["building_tier"]
    assert tier["parts"] == 2 and tier["parts_without_parent"] == 1 and tier["osm_tags_found"] == 5
    assert tier["counts"] == {"": 1, "T1": 1, "T2": 2, "T3": 2}
    cats = {c["column"]: {r["value"]: r["count"] for r in c["rows"]} for c in result["report"]["categories"]}
    assert cats["building_tier"]["T2"] == 2  # the report reads the new values, not the input's
