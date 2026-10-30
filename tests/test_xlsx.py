"""Excel input: header detection, column guesses, and staging a sheet into a GeoPackage.

The staging tests need GDAL (and DuckDB spatial); they are skipped without it. Overpass is never
called: fetch_ways is replaced by fixed geometry.
"""

import csv
import math
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from app import pipeline, tools, xlsx
from app.jobs import FileRegistry, Job
from app.xlsx import Column, build_columns, detect_header_row, guess_columns, infer_type, parse_buffer, parse_way_id

needs_gdal = pytest.mark.skipif(
    any(tools.which(t) is None for t in ("ogr2ogr", "ogrinfo", "tippecanoe")),
    reason="GDAL/tippecanoe not installed",
)

# Like sheet DS_Way_Final of the GPMB report: a title, two notes, labels, machine names, data.
REPORT_ROWS = [
    (1, ["DS WAY FINAL – WAY V2 THUỘC DIỆN ĐÃ / ĐANG GPMB"]),
    (2, ["Nguồn: (1) vùng & chứng thực trên https://example.org …"]),
    (3, ["Quy tắc lấy (chốt 22/10/2026): vùng Đỏ/Hồng/Cam -> lấy …"]),
    (4, ["STT", "Way ID", "Tên đường", "Buffer mỗi bên (m)", "Độ rộng (m)"]),
    (5, ["STT", "way_id", "ten_duong", "buffer_moi_ben_m", "do_rong_m"]),
    (6, ["1", "708903075", "Phố Hoàng Cầu", "25", "50"]),
    (7, ["2", "w180083173", "Ngõ 371 Đường La Thành", "30.25", "60.5"]),
    (8, ["3", "way/37891457", "Đường La Thành", "", "40–50"]),
]


def test_header_skips_titles_and_prefers_machine_names() -> None:
    assert detect_header_row(REPORT_ROWS) == 5


def test_header_of_labels_only() -> None:
    rows = [r for r in REPORT_ROWS if r[0] != 5]
    assert detect_header_row(rows) == 4


def test_header_on_first_row_with_text_data() -> None:
    rows = [(1, ["name", "wkt"]), (2, ["A", "POINT (105.8 21.0)"]), (3, ["B", "POINT (105.9 21.1)"])]
    assert detect_header_row(rows) == 1


@pytest.mark.parametrize("text,expected", [
    ("708903075", 708903075), ("w180083173", 180083173), ("way/37891457", 37891457), ("Way 12", 12),
    ("123.0", 123), ("https://www.openstreetmap.org/way/708903075", 708903075),
    ("", None), ("Mở OSM", None), ("n123", None),
])
def test_parse_way_id(text: str, expected: int | None) -> None:
    assert parse_way_id(text) == expected


@pytest.mark.parametrize("text,expected", [("25", 25.0), ("30.25", 30.25), ("10,35", 10.35),
                                           ("40–50", None), ("0", None), ("", None)])
def test_parse_buffer(text: str, expected: float | None) -> None:
    assert parse_buffer(text) == expected


def test_infer_type() -> None:
    assert infer_type(["1", "2", "-3"]) == "Integer"
    assert infer_type(["1", "2.5", "1e3"]) == "Real"
    assert infer_type(["001", "002"]) == "String"
    assert infer_type(["1", "a"]) == "String"
    assert infer_type([]) == "String"


def test_columns_names_types_and_guesses() -> None:
    header = ["STT", "way_id", "", "geom", "Số way", "buffer_moi_ben_m", "do_rong_m", "STT"]
    data = [(6, ["1", "708903075", "x", "g", "3", "25", "50", "a"]), (7, ["2", "w1", "", "g", "4", "", "60", "b"])]
    cols = build_columns(header, data)
    assert [(c.name, c.type) for c in cols] == [
        ("STT", "Integer"), ("way_id", "String"), ("col_C", "String"), ("geom_xlsx", "String"),
        ("Số way", "Integer"), ("buffer_moi_ben_m", "Integer"), ("do_rong_m", "Integer"), ("STT_2", "String"),
    ]
    g = guess_columns(cols, data)
    # A count ("Số way") is not an id; a full width ("do_rong") is not a buffer.
    assert (g["mode"], g["osm_col"], g["buffer_col"], g["wkt_col"]) == ("osm", "way_id", "buffer_moi_ben_m", None)


def test_guess_wkt_column() -> None:
    cols = [Column(0, "name", "String"), Column(1, "shape", "String")]
    data = [(2, ["A", "POLYGON ((0 0, 1 0, 1 1, 0 0))"]), (3, ["B", '{"type":"Point","coordinates":[1,2]}'])]
    assert guess_columns(cols, data)["wkt_col"] == "shape"


def test_parse_geom_spec_errors() -> None:
    with pytest.raises(xlsx.XlsxError):
        xlsx.parse_geom_spec({"mode": "osm"})
    with pytest.raises(xlsx.XlsxError):
        xlsx.parse_geom_spec({"mode": "osm", "column": "way_id", "buffer_m": -1})
    spec = xlsx.parse_geom_spec({"mode": "osm", "column": "way_id", "buffer_col": "", "buffer_m": "12.5"})
    assert (spec.buffer_col, spec.buffer_m) == (None, 12.5)


# ------------------------------------------------------------------ staging (GDAL)

def _xlsx(tmp_path: Path, rows: list[list[str]], name: str = "data") -> Path:
    src = tmp_path / f"{name}.csv"
    with src.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(rows)
    out = tmp_path / f"{name}.xlsx"
    subprocess.run([tools.require("ogr2ogr"), "-f", "XLSX", str(out), str(src), "-nln", "Sheet A"],
                   check=True, capture_output=True)
    return out


def _square(i: int) -> str:
    x, y = 105.80 + i * 0.001, 21.02
    return f"POLYGON (({x} {y}, {x + 0.0005} {y}, {x + 0.0005} {y + 0.0005}, {x} {y + 0.0005}, {x} {y}))"


@needs_gdal
def test_stage_wkt_sheet_then_convert_with_heights(tmp_path: Path) -> None:
    book = _xlsx(tmp_path, [
        ["building_id", "height_m", "wkt"],
        ["A", "30", _square(0)],
        ["B", "12.5", _square(1)],
        ["C", "", ""],
        ["D", "9", "POLYGON broken"],
    ])
    info = xlsx.inspect(book)
    assert (info["sheet"], info["header_row"], info["suggest"]["wkt_col"]) == ("Sheet A", 1, "wkt")

    spec = xlsx.parse_geom_spec({"mode": "wkt", "column": "wkt", "crs": "EPSG:4326"})
    staged = xlsx.stage(book, "Sheet A", 1, spec, tmp_path / "staged" / "data__Sheet_A.gpkg")
    assert (staged["features"], staged["skipped"]) == (2, 2)
    assert len(staged["warnings"]) == 2  # one empty, one unreadable

    cfg = {
        "input": {"path": staged["path"]},
        "output": {"dir": str(tmp_path / "out"), "name": "data", "gpkg": True, "pmtiles": True},
        "height": {"enabled": True, "height_col": "height_m"},
        "tiles": {"minzoom": 14, "maxzoom": 15, "attributes": ["building_id", "h_m", "xlsx_row"]},
    }
    job = Job(id="xlsx", config=cfg, name="data")
    pipeline.execute(job, FileRegistry())
    result = job.to_dict()
    assert result["status"] == "done", (result["error"], "\n".join(result["log"][-30:]))
    with closing(sqlite3.connect(result["outputs"]["gpkg"]["path"])) as conn:
        rows = dict(conn.execute('SELECT building_id, h_m FROM "buildings"'))
    assert rows == {"A": 30.0, "B": 12.5}


@needs_gdal
def test_stage_osm_ways_buffered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    book = _xlsx(tmp_path, [
        ["way_id", "ten_duong", "buffer_moi_ben_m"],
        ["101", "Phố A", "20"],
        ["w102", "Phố B", ""],
        ["way/999", "Đã xoá", "20"],
        ["Mở OSM", "?", "20"],
    ], name="ways")
    # 101: ~1 km east-west; 102: ~500 m north-south (Hà Nội).
    ways = {101: [(105.80, 21.02), (105.8096, 21.02)], 102: [(105.85, 21.03), (105.85, 21.0345)]}
    calls = []
    monkeypatch.setattr(xlsx, "fetch_ways", lambda ids: calls.append(ids) or {i: ways[i] for i in ids if i in ways})

    assert xlsx.inspect(book)["suggest"]["buffer_col"] == "buffer_moi_ben_m"
    spec = xlsx.parse_geom_spec({"mode": "osm", "column": "way_id", "buffer_col": "buffer_moi_ben_m", "buffer_m": 10})
    staged = xlsx.stage(book, "Sheet A", 1, spec, tmp_path / "ways.gpkg")

    assert calls == [[101, 102, 999]]
    assert staged["features"] == 2
    assert staged["stats"]["missing_ways"] == [999]
    assert staged["stats"]["utm"] == "EPSG:32648"
    assert any("999" in w for w in staged["warnings"])
    assert any("dòng 5" in w for w in staged["warnings"])  # "Mở OSM" is not a way id

    out = subprocess.run(
        [tools.require("ogrinfo"), "-ro", "-q", "-dialect", "SQLite", "-sql",
         ('SELECT osm_way_id, buffer_m, xlsx_row, ST_Area(ST_Transform(geom, 32648)) AS a, '
          'ST_Length(ST_Transform(ST_ExteriorRing(geom), 32648)) AS p FROM "Sheet A" ORDER BY osm_way_id'),
         str(tmp_path / "ways.gpkg")],
        check=True, capture_output=True, text=True,
    ).stdout
    values = [line.split("=")[1].strip() for line in out.splitlines() if "=" in line]
    (id1, b1, r1, a1, _), (id2, b2, r2, a2, _) = values[:5], values[5:]
    assert (id1, b1, r1, id2, b2, r2) == ("101", "20", "2", "102", "10", "3")
    # A buffered line: length × 2b + π b² (round caps).
    length1 = float(a1) - math.pi * 20 ** 2
    assert 990 * 40 < length1 < 1010 * 40
    assert 490 * 20 < float(a2) - math.pi * 10 ** 2 < 510 * 20
