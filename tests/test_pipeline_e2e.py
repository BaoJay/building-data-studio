"""End-to-end: plain Parquet (WKT, no CRS) -> GPKG + PMTiles, then GPKG -> PMTiles.

Needs GDAL + tippecanoe on PATH; skipped otherwise. Set BC_SAMPLE_PARQUET to a
real export to also run the pipeline on it.
"""

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
                  "attributes": ["building_id", "h_m", "has_parts", "is_part"]},
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
            'SELECT building_id, h_m, h_src, h_outlier, has_parts, is_part FROM "buildings"')}


def test_plain_parquet_to_gpkg_and_pmtiles(plain_parquet: Path, tmp_path: Path) -> None:
    result = _run(_config(plain_parquet, tmp_path / "out"))
    gpkg = Path(result["outputs"]["gpkg"]["path"])
    assert _rows(gpkg) == {
        "A": (30.0, "height", 0, 1, 0),
        "A1": (45.0, "height", 0, 0, 1),
        "B": (4.0, "default", 0, 0, 0),
        "C": (4.0, "default", 1, 0, 0),
        "D": (4.0, "default", 1, 0, 0),
        "E": (12.4, "height", 0, 0, 0),
    }
    report = result["report"]
    assert report["total"] == 6
    assert report["height"]["real_count"] == 3
    assert report["height"]["outlier_count"] == 2
    assert report["accounting"]["input_count"] == 6
    assert report["accounting"]["pmtiles_features"] == 6
    assert report["pmtiles"]["verified"] in (True, None) or tools.which("pmtiles") is None
    assert {o["building_id"] for o in report["outliers"]} == {"C", "D"}
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
    assert second["report"]["height"]["outlier_count"] == 2


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
    assert cols == ["fid", "geom", "building_id", "parent_building_id", "h_m", "h_src", "h_outlier",
                    "has_parts", "is_part"]


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
