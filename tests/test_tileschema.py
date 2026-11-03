"""The PMTiles schema agreed with VMAP: loading, the SELECT that fills it, and the check."""

import json
import sqlite3
from pathlib import Path

import pytest

from app import tileschema
from app.tileschema import SchemaError, TileSchema

SMALL = TileSchema(
    name="small", source="", layer="buildings", minzoom=13, maxzoom=16,
    fields={"building_id": "String", "h_m": "Number", "height_m": "Number", "min_height": "String",
            "updated_at": "String", "levels": "Number"},
    copies={"h_m": "height_m"},
)


def _write(tmp_path: Path, **overrides) -> Path:
    raw = {"name": "t", "layer": "buildings", "minzoom": 13, "maxzoom": 16,
           "fields": {"building_id": "String", "height_m": "Number", "h_m": "Number"},
           "copies": {"h_m": "height_m"}, "deprecated": ["h_m"]} | overrides
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_vmap_schema_renders_height_m_and_repeats_it() -> None:
    schema = tileschema.load()
    assert (schema.name, schema.layer, schema.minzoom, schema.maxzoom) == ("vmap-buildings", "buildings", 13, 16)
    assert schema.fields["height_m"] == "Number"
    assert schema.copies == {"h_m": "height_m", "height": "height_m"}
    assert schema.deprecated == ("h_m",)
    # Fields of the canonical parquet keep the types of the agreed TileJSON.
    assert schema.fields["min_height"] == "String" and schema.fields["updated_at"] == "String"


def test_load_accepts_a_valid_file(tmp_path: Path) -> None:
    schema = tileschema.load(_write(tmp_path))
    assert list(schema.fields) == ["building_id", "height_m", "h_m"]


@pytest.mark.parametrize("overrides", [
    {"fields": {"a": "Integer"}},
    {"fields": {}},
    {"copies": {"x": "height_m"}},
    {"copies": {"h_m": "nope"}},
    {"copies": {"h_m": "height_m", "height_m": "h_m"}},
    {"deprecated": ["nope"]},
    {"minzoom": 17, "maxzoom": 16},
    {"layer": " "},
])
def test_load_rejects_broken_files(tmp_path: Path, overrides: dict) -> None:
    with pytest.raises(SchemaError):
        tileschema.load(_write(tmp_path, **overrides))


def test_load_reports_unreadable_json(tmp_path: Path) -> None:
    path = tmp_path / "schema.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(SchemaError, match="schema.json"):
        tileschema.load(path)


def test_select_items_coerce_types_and_fill_copies() -> None:
    columns = {"building_id": "TEXT", "height_m": "REAL", "min_height": "TEXT", "updated_at": "DATETIME",
               "levels": "TEXT", "superseded_by": "TEXT"}
    items, missing = tileschema.select_items(SMALL, columns)
    assert missing == []
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (building_id TEXT, height_m REAL, min_height TEXT, updated_at DATETIME, "
                  "levels TEXT, superseded_by TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?)", [
        ("a", 12.5, "10", "2026-10-28T01:02:03Z", "7", "x"),
        ("b", None, None, None, "abc", None),
    ])
    cur = conn.execute(f"SELECT {', '.join(items)} FROM t")
    assert [d[0] for d in cur.description] == list(SMALL.fields)
    # Text that is not a number becomes NULL (tippecanoe then leaves it out), never 0.
    assert cur.fetchall() == [("a", 12.5, 12.5, "10", "2026-10-28T01:02:03Z", 7.0),
                              ("b", None, None, None, None, None)]


def test_select_items_turn_numbers_into_text_for_string_fields() -> None:
    items, _ = tileschema.select_items(SMALL, {"building_id": "INTEGER", "height_m": "REAL"})
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (building_id INTEGER, height_m REAL)")
    conn.execute("INSERT INTO t VALUES (42, 3)")
    assert conn.execute(f"SELECT {', '.join(items)} FROM t").fetchone() == ("42", 3.0, 3.0)


def test_select_items_list_what_the_data_lacks() -> None:
    items, missing = tileschema.select_items(SMALL, {"building_id": "TEXT"})
    assert len(items) == 1
    # A copy is missing with its source.
    assert missing == ["h_m", "height_m", "min_height", "updated_at", "levels"]


def test_tippecanoe_types_fix_each_written_field() -> None:
    assert tileschema.tippecanoe_types(SMALL, ["building_id", "h_m"]) == [
        "-T", "building_id:string", "-T", "h_m:float"]


def _layer(fields: dict, minzoom: int = 13, maxzoom: int = 16, layer: str = "buildings") -> list[dict]:
    return [{"id": "shadows", "fields": {"preset": "String"}},
            {"id": layer, "minzoom": minzoom, "maxzoom": maxzoom, "fields": fields}]


def test_check_passes_an_exact_match() -> None:
    result = tileschema.check(SMALL, _layer(dict(SMALL.fields)))
    assert result["ok"] and result["matched"] == len(SMALL.fields)


def test_check_lists_missing_extra_wrong_type_and_zoom() -> None:
    fields = dict(SMALL.fields) | {"min_height": "Number", "superseded_by": "String"}
    del fields["levels"]
    result = tileschema.check(SMALL, _layer(fields, maxzoom=17))
    assert not result["ok"]
    assert result["missing"] == ["levels"]
    assert result["extra"] == ["superseded_by"]
    assert result["wrong_type"] == [{"field": "min_height", "expected": "String", "got": "Number"}]
    assert (result["zooms"], result["expected_zooms"]) == ([13, 17], [13, 16])
    assert result["matched"] == len(SMALL.fields) - 2


def test_check_without_the_layer() -> None:
    result = tileschema.check(SMALL, _layer(dict(SMALL.fields), layer="other"))
    assert not result["ok"] and not result["layer_found"]
    assert result["missing"] == list(SMALL.fields)
