"""Overlapping footprints: the SQL through GDAL + SpatiaLite, the report and the URL shape."""

import json
import sqlite3
import struct
from contextlib import closing
from pathlib import Path

import pytest

from app import overlap, pipeline, tools
from app.jobs import FileRegistry, Job


def _box(x0: float, y0: float, x1: float, y1: float) -> list:
    return [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]


X, Y, U = 106.70, 10.77, 0.0001  # U ≈ 11 m
# id, parent, footprint (in units of U from X, Y)
FOOTPRINTS = [
    ("A", None, (0, 0, 4, 4)),
    ("B", None, (2, 2, 6, 6)),      # overlaps A: 4 / (16 + 16 - 4) = 1/7
    ("C", None, (4, 0, 6, 2)),      # shares a wall with A, a corner with B: not an overlap
    ("A1", "A", (0, 0, 2, 2)),      # part of A: inside its parent
    ("A2", "A", (1, 1, 3, 3)),      # part of A over A1 (same parent) and over B (not its parent)
    ("D", None, (10, 0, 12, 2)),    # duplicate footprints
    ("D2", None, (10, 0, 12, 2)),
]


@pytest.fixture()
def footprints(tmp_path: Path) -> Path:
    features = [
        {"type": "Feature", "properties": {"building_id": b, "parent_building_id": p, "height_m": 10.0 + i},
         "geometry": {"type": "Polygon", "coordinates": _box(X + x0 * U, Y + y0 * U, X + x1 * U, Y + y1 * U)}}
        for i, (b, p, (x0, y0, x1, y1)) in enumerate(FOOTPRINTS)
    ]
    src = tmp_path / "fp.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    return src


def _config(path: Path, out_dir: Path, **output) -> dict:
    return {
        "input": {"path": str(path), "src_crs": "EPSG:4326"},
        "output": {"dir": str(out_dir), "name": "fp", "gpkg": True, "pmtiles": False, "gpkg_crs": "EPSG:4326",
                   **output},
        "attributes": {},
        "height": {"enabled": True, "height_col": "height_m", "min_valid_m": 2, "max_valid_m": 500},
        "parts": {"enabled": True, "id_col": "building_id", "parent_col": "parent_building_id"},
        "tiles": {},
        "report": {},
    }


def _run(cfg: dict) -> dict:
    job = Job(id="test", config=cfg, name="fp")
    pipeline.execute(job, FileRegistry())
    result = job.to_dict()
    assert result["status"] == "done", (result["error"], "\n".join(result["log"][-30:]))
    return result


needs_gdal = pytest.mark.skipif(any(tools.which(t) is None for t in ("ogr2ogr", "ogrinfo")),
                                reason="GDAL not installed")


@needs_gdal
def test_pairs_ratio_and_gpkg_layer(footprints: Path, tmp_path: Path) -> None:
    result = _run(_config(footprints, tmp_path / "out"))
    ov = result["report"]["overlaps"]
    pairs = {tuple(sorted((r["id_a"], r["id_b"]))): r for r in ov["rows"]}
    assert set(pairs) == {("A", "B"), ("A2", "B"), ("D", "D2")}
    assert ov["pairs"] == 3
    assert ov["buildings"] == 5 and ov["total"] == 7 and ov["pct"] == pytest.approx(71.43)
    assert [r["iou"] for r in ov["rows"]] == sorted((r["iou"] for r in ov["rows"]), reverse=True)

    ab = pairs[("A", "B")]
    assert ab["iou"] == pytest.approx(1 / 7, rel=1e-6)
    assert ab["inter_m2"] == pytest.approx(4 * (U * 111319.49) ** 2 * 0.98238, rel=1e-3)
    assert ab["cover_a"] == pytest.approx(0.25) and ab["cover_b"] == pytest.approx(0.25)
    assert {ab["h_a"], ab["h_b"]} == {10.0, 11.0}
    assert ab["bbox"] == pytest.approx([X, Y, X + 6 * U, Y + 6 * U])
    assert 15 <= ab["zoom"] <= 19
    rings = [[tuple(map(float, p.split())) for p in ring.split(",")] for ring in ab["shape"].split(";")]
    assert len(rings) == 1 and {pt for pt in rings[0]} == {
        (round(X + a * U, 7), round(Y + b * U, 7)) for a in (2, 4) for b in (2, 4)}
    assert pairs[("D", "D2")]["iou"] == pytest.approx(1.0)
    assert [b["count"] for b in ov["iou_bins"]] == [0, 1, 1, 0, 1]  # A2/B = 1/31, A/B = 1/7, D/D2 = 1

    gpkg = Path(result["outputs"]["gpkg"]["path"])
    with closing(sqlite3.connect(gpkg)) as conn:
        assert conn.execute(f'SELECT COUNT(*) FROM "{overlap.LAYER}"').fetchone()[0] == 3
        layers = {r[0] for r in conn.execute("SELECT table_name FROM gpkg_contents")}
    assert layers == {"buildings", overlap.LAYER}


@needs_gdal
def test_reprojected_gpkg_gets_the_layer_in_its_crs(footprints: Path, tmp_path: Path) -> None:
    result = _run(_config(footprints, tmp_path / "out", gpkg_crs="EPSG:32648"))
    with closing(sqlite3.connect(result["outputs"]["gpkg"]["path"])) as conn:
        srs = dict(conn.execute("SELECT table_name, srs_id FROM gpkg_geometry_columns"))
        assert conn.execute(f'SELECT COUNT(*) FROM "{overlap.LAYER}"').fetchone()[0] == 3
    assert srs == {"buildings": 32648, overlap.LAYER: 32648}


@needs_gdal
def test_no_overlap(tmp_path: Path) -> None:
    src = tmp_path / "apart.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"building_id": str(i), "parent_building_id": None, "height_m": 5},
         "geometry": {"type": "Polygon", "coordinates": _box(X + i * 2 * U, Y, X + (i * 2 + 1) * U, Y + U)}}
        for i in range(3)]}), encoding="utf-8")
    ov = _run(_config(src, tmp_path / "out"))["report"]["overlaps"]
    assert (ov["pairs"], ov["buildings"], ov["pct"], ov["rows"]) == (0, 0, 0.0, [])


def _wkb_polygon(rings: list, code: int = 3, dims: int = 2) -> bytes:
    out = struct.pack("<BII", 1, code, len(rings))
    for ring in rings:
        out += struct.pack("<I", len(ring))
        for pt in ring:
            out += struct.pack(f"<{dims}d", *pt, *([7.0] * (dims - 2)))
    return out


def _gpkg_blob(wkb: bytes, envelope: tuple[float, float, float, float] | None = None) -> bytes:
    flags = 0b1 | ((1 << 1) if envelope else 0)
    head = b"GP" + bytes([0, flags]) + struct.pack("<i", 4326)
    if envelope:
        x0, y0, x1, y1 = envelope
        head += struct.pack("<4d", x0, x1, y0, y1)
    return head + wkb


def test_encode_shape_multipolygon_with_z_and_hole() -> None:
    outer = [(0, 0), (1, 0), (1, 1), (0, 0)]
    hole = [(0.1, 0.1), (0.2, 0.1), (0.2, 0.2), (0.1, 0.1)]
    second = [(2.123456789, 0), (3, 0), (3, 1), (2.123456789, 0)]
    multi = struct.pack("<BII", 1, 1006, 2) + _wkb_polygon([outer, hole], 1003, 3) + _wkb_polygon([second], 1003, 3)
    assert overlap.encode_shape(_gpkg_blob(multi, (0, 0, 3, 1))) == (
        "0 0,1 0,1 1,0 0;0.1 0.1,0.2 0.1,0.2 0.2,0.1 0.1|2.1234568 0,3 0,3 1,2.1234568 0")


def test_encode_shape_falls_back_to_bbox() -> None:
    ring = [(i * 1e-5, (i % 2) * 1e-5) for i in range(overlap.LINK_MAX_POINTS + 1)] + [(0, 0)]
    shape = overlap.encode_shape(_gpkg_blob(_wkb_polygon([ring]), (0, 0, 2, 1e-5)))
    assert shape == "0 0,2 0,2 0.00001,0 0.00001,0 0"
    assert overlap.encode_shape(None) is None


def test_fit_zoom() -> None:
    assert overlap.fit_zoom([105.8, 21.0, 105.8001, 21.0001]) == 19.0
    assert overlap.fit_zoom([105.0, 21.0, 106.0, 22.0]) == 15.0
    mid = overlap.fit_zoom([105.8, 21.0, 105.8015, 21.0005])
    assert 15 < mid < 19
