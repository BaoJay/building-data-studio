"""End-to-end diff jobs: Parquet ↔ Parquet, Parquet ↔ PMTiles (built by the converter), location mode.

Needs GDAL + tippecanoe on PATH; skipped otherwise. Set BC_DIFF_A / BC_DIFF_B to
two real exports to also run the diff on them.
"""

import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from app import diff, pipeline, tools
from app.jobs import FileRegistry, Job

pytestmark = pytest.mark.skipif(
    any(tools.which(t) is None for t in ("ogr2ogr", "ogrinfo", "tippecanoe")),
    reason="GDAL/tippecanoe not installed",
)

TILE_DEG_Z15 = 360 / 2 ** 15
# Longitude of a z15 tile edge near Ho Chi Minh City: one building straddles it.
EDGE_LON = -180 + ((106.705 + 180) // TILE_DEG_Z15) * TILE_DEG_Z15
SIZE = 0.0003

# id, lon, lat, height_m, provenance
A = [
    ("A", 106.700, 10.770, 30.0, "osm_height"),
    ("B", 106.701, 10.770, 4.0, "default"),
    ("C", 106.702, 10.770, 12.0, "osm_height"),
    ("D", 106.703, 10.770, 9.0, "osm_levels"),
    ("X", EDGE_LON - SIZE / 2, 10.772, 20.0, "osm_height"),
]
B = [
    ("A", 106.700, 10.770, 30.0, "osm_height"),
    ("B", 106.701, 10.770, 18.0, "osm_height"),  # default -> measured, taller
    ("C", 106.70205, 10.770, 12.0, "osm_height"),  # moved ~5 m
    ("X", EDGE_LON - SIZE / 2, 10.772, 20.0, "osm_height"),
    ("E", 106.704, 10.770, 6.0, "osm_height"),  # new; D removed
]


def _parquet(tmp_path: Path, name: str, rows: list[tuple], with_id: bool = True) -> Path:
    features = [{
        "type": "Feature",
        "properties": ({"building_id": i} if with_id else {}) | {"height_m": h, "height_provenance": p,
                                                                 "build_id": name},
        "geometry": {"type": "Polygon", "coordinates": [[[x, y], [x + SIZE, y], [x + SIZE, y + SIZE], [x, y + SIZE], [x, y]]]},
    } for i, x, y, h, p in rows]
    src = tmp_path / f"{name}.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    out = tmp_path / f"{name}.parquet"
    cols = ("building_id, " if with_id else "") + "height_m, height_provenance, build_id"
    subprocess.run([tools.require("ogr2ogr"), "-f", "Parquet", str(out), str(src), "-dialect", "SQLite", "-sql",
                    f"SELECT {cols}, ST_AsText(geometry) AS geometry_wkt FROM {name}", "-nlt", "NONE"],
                   check=True, capture_output=True)
    return out


def _run(a: Path, b: Path, out: Path, **extra) -> dict:
    cfg = {"kind": "diff", "a": {"path": str(a), "src_crs": "EPSG:4326"}, "b": {"path": str(b), "src_crs": "EPSG:4326"},
           "output": {"dir": str(out)}, **extra}
    job = Job(id="t", config=cfg, name="t", kind="diff")
    diff.execute(job, FileRegistry())
    result = job.to_dict()
    assert result["status"] == "done", (result["error"], "\n".join(result["log"][-30:]))
    return result


def _layer_counts(gpkg: str) -> dict[str, int]:
    with closing(sqlite3.connect(gpkg)) as conn:
        names = [r[0] for r in conn.execute("SELECT table_name FROM gpkg_contents")]
        return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def test_parquet_vs_parquet_by_id(tmp_path: Path) -> None:
    result = _run(_parquet(tmp_path, "a", A), _parquet(tmp_path, "b", B), tmp_path / "out")
    r = result["report"]
    m = r["match"]
    assert (m["mode"], m["matched"], m["added"], m["removed"], m["changed"]) == ("key", 4, 1, 1, 2)
    assert r["changes"]["height"]["default_to_real"] == 1
    assert r["changes"]["geometry"]["moderate"] == 1  # C moved ~5 m: above noise, below "major"
    assert r["ignore_cols"] == ["build_id"]
    assert r["gate"]["verdict"] == "fail"  # 20 % of A removed
    assert "removed" in {f["code"] for f in r["gate"]["findings"] if f["level"] == "fail"}
    counts = _layer_counts(result["outputs"]["gpkg"]["path"])
    assert counts == {"added": 1, "removed": 1, "changed": 2, "changed_before": 2, "grid": counts["grid"]}
    assert counts["grid"] >= 1
    assert Path(result["outputs"]["pmtiles"]["path"]).stat().st_size > 0
    md = Path(result["outputs"]["report_md"]["path"]).read_text(encoding="utf-8")
    assert md.startswith("# So sánh trước release — FAIL")
    saved = json.loads(Path(result["outputs"]["report_json"]["path"]).read_text(encoding="utf-8"))
    assert saved["gate"]["verdict"] == "fail" and "provenance" in saved
    assert not (Path(result["output_dir"]) / "_work").exists()


def _to_pmtiles(src: Path, out: Path) -> Path:
    cfg = {
        "input": {"path": str(src), "src_crs": "EPSG:4326"},
        "output": {"dir": str(out), "name": src.stem, "gpkg": False, "pmtiles": True},
        "attributes": {},
        "height": {"enabled": True, "height_col": "height_m", "prov_col": "height_provenance", "prov_missing": ["default"]},
        "parts": {"enabled": False},
        "tiles": {"layer": "buildings", "minzoom": 14, "maxzoom": 15,
                  "attributes": ["building_id", "h_m", "height_provenance"]},
    }
    job = Job(id="c", config=cfg, name="c")
    pipeline.execute(job, FileRegistry())
    assert job.status == "done", job.error
    return Path(job.outputs["pmtiles"]["path"])


def test_parquet_vs_pmtiles_stitches_tile_pieces(tmp_path: Path) -> None:
    a = _parquet(tmp_path, "a", A)
    tiles = _to_pmtiles(a, tmp_path / "conv")
    result = _run(a, tiles, tmp_path / "out", required_fields=["building_id"])
    r = result["report"]
    sb = r["sides"]["b"]
    # X straddles a z15 tile edge: 6 pieces read back, 5 buildings after stitching.
    assert (sb["kind"], sb["pieces"], sb["buildings"], sb["zoom"]) == ("pmtiles", 6, 5, 15)
    m = r["match"]
    assert (m["matched"], m["added"], m["removed"], m["changed"]) == (5, 0, 0, 0)
    assert r["changes"]["geometry"]["major"] == 0
    assert r["schema"]["missing"] and not any(x["required"] for x in r["schema"]["missing"])
    codes = {f["code"]: f["level"] for f in r["gate"]["findings"]}
    assert codes["schema_missing_optional"] == "warn" and codes["cross_format"] == "info"
    assert r["gate"]["verdict"] == "warn"


def test_location_mode_without_shared_id(tmp_path: Path) -> None:
    a = _parquet(tmp_path, "a", A, with_id=False)
    b = _parquet(tmp_path, "b", B)
    result = _run(a, b, tmp_path / "out", output={"dir": str(tmp_path / "out"), "pmtiles": False})
    r = result["report"]
    m = r["match"]
    assert m["mode"] == "location"
    assert (m["matched"], m["added"], m["removed"]) == (4, 1, 1)
    assert set(r["ignore_cols"]) == {"height_m", "height_provenance", "build_id"}
    assert "pmtiles" not in result["outputs"]


def test_missing_crs_is_rejected(tmp_path: Path) -> None:
    a, b = _parquet(tmp_path, "a", A), _parquet(tmp_path, "b", B)
    job = Job(id="t", config={"kind": "diff", "a": {"path": str(a)}, "b": {"path": str(b)},
                              "output": {"dir": str(tmp_path / "out")}}, name="t", kind="diff")
    diff.execute(job, FileRegistry())
    assert job.status == "failed" and "CRS" in (job.error or "")


def test_parse_config_errors(tmp_path: Path) -> None:
    a = _parquet(tmp_path, "a", A)
    with pytest.raises(pipeline.ConfigError, match="cùng một file"):
        diff.parse_config({"a": {"path": str(a)}, "b": {"path": str(a)}})
    with pytest.raises(pipeline.ConfigError, match="Thiếu file"):
        diff.parse_config({"a": {"path": str(a)}})
    b = _parquet(tmp_path, "b", B)
    with pytest.raises(pipeline.ConfigError, match="Ngưỡng"):
        diff.parse_config({"a": {"path": str(a)}, "b": {"path": str(b)}, "thresholds": {"fail_count_pct": "abc"}})
    cfg = diff.parse_config({"a": {"path": str(a)}, "b": {"path": str(b)}})
    assert cfg.out_name == "diff__b__vs__a" and cfg.mode == "auto"


@pytest.mark.skipif(not (os.environ.get("BC_DIFF_A") and os.environ.get("BC_DIFF_B")), reason="BC_DIFF_A/B not set")
def test_real_files(tmp_path: Path) -> None:
    result = _run(Path(os.environ["BC_DIFF_A"]), Path(os.environ["BC_DIFF_B"]), tmp_path / "out")
    r = result["report"]
    assert r["sides"]["a"]["buildings"] > 0 and r["gate"]["verdict"] in ("pass", "warn", "fail")


def _geojson(tmp_path: Path, name: str, rows: list[tuple]) -> Path:
    features = [{
        "type": "Feature",
        "properties": {"building_id": i, "height_m": h, "tags": ["a", p], "flag": i == "C"},
        "geometry": {"type": "Polygon", "coordinates": [[[x, y], [x + SIZE, y], [x + SIZE, y + SIZE], [x, y + SIZE], [x, y]]]},
    } for i, x, y, h, p in rows]
    out = tmp_path / f"{name}.geojson"
    out.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    return out


def test_list_fields_quotes_in_path_and_no_required_fields(tmp_path: Path) -> None:
    a, b = _geojson(tmp_path, "a", A), _geojson(tmp_path, "b", B)
    out = tmp_path / "it's out"
    result = _run(a, b, out, output={"dir": str(out), "pmtiles": False}, required_fields=[])
    r = result["report"]
    assert r["match"]["matched"] == 4 and r["schema"]["required"] == []
    assert diff.parse_config({"a": {"path": str(a)}, "b": {"path": str(b)}, "required_fields": []}).required_fields == ()


def test_pmtiles_booleans_compare_with_vector(tmp_path: Path) -> None:
    a = _geojson(tmp_path, "a", A)
    cfg = {
        "input": {"path": str(a)},
        "output": {"dir": str(tmp_path / "conv"), "name": "a", "gpkg": False, "pmtiles": True},
        "attributes": {}, "height": {"enabled": False}, "parts": {"enabled": False},
        "tiles": {"layer": "buildings", "minzoom": 14, "maxzoom": 15, "attributes": ["building_id", "flag"]},
    }
    job = Job(id="c", config=cfg, name="c")
    pipeline.execute(job, FileRegistry())
    assert job.status == "done", job.error
    result = _run(a, Path(job.outputs["pmtiles"]["path"]), tmp_path / "out", required_fields=["building_id"],
                  output={"dir": str(tmp_path / "out"), "pmtiles": False})
    cols = {c["column"]: c["changed"] for c in result["report"]["changes"]["columns"]}
    # Heights differ (no height column in the tiles); attributes, booleans included, must not.
    assert cols["flag"] == 0 and result["report"]["changes"]["by_reason"]["attributes"] == 0
