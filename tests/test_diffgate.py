import copy

import pytest

from app.diffgate import (
    GateError,
    Thresholds,
    compare_schema,
    evaluate,
    parse_thresholds,
    render_markdown,
    type_change,
)


def test_parse_thresholds_defaults_and_overrides() -> None:
    assert parse_thresholds(None) == Thresholds()
    t = parse_thresholds({"fail_count_pct": "15", "warn_count_pct": 3, "fail_layer_change": False, "geom_shift_m": ""})
    assert (t.fail_count_pct, t.warn_count_pct, t.fail_layer_change, t.geom_shift_m) == (15.0, 3.0, False, 10.0)


@pytest.mark.parametrize("raw", [{"fail_count_pct": "x"}, {"warn_removed_pct": -1}, {"warn_count_pct": 20}])
def test_parse_thresholds_rejects(raw: dict) -> None:
    with pytest.raises(GateError):
        parse_thresholds(raw)


@pytest.mark.parametrize(("a", "b", "tiles", "level"), [
    ({"type": "Integer"}, {"type": "Integer64"}, False, "info"),
    ({"type": "Real"}, {"type": "Integer64"}, False, "warn"),
    ({"type": "Integer64"}, {"type": "Real"}, True, "info"),
    ({"type": "DateTime"}, {"type": "String"}, True, "info"),
    ({"type": "DateTime"}, {"type": "String"}, False, "fail"),
    ({"type": "String"}, {"type": "Integer64"}, False, "fail"),
    ({"type": "Integer", "subtype": "Boolean"}, {"type": "Real"}, True, "info"),
])
def test_type_change(a: dict, b: dict, tiles: bool, level: str) -> None:
    assert type_change(a, b, tiles)[0] == level


def test_type_unchanged() -> None:
    assert type_change({"type": "String"}, {"type": "String", "subtype": None}, False) is None


def _side(fields: list[tuple[str, str]], kind: str = "vector", buildings: int = 100) -> dict:
    return {"fields": [{"name": n, "type": t} for n, t in fields], "kind": kind, "buildings": buildings}


def test_compare_schema() -> None:
    a = _side([("id", "String"), ("h", "Real"), ("tag", "String"), ("old", "String")])
    b = _side([("id", "String"), ("h", "String"), ("tag", "String"), ("new", "Integer")])
    nonnull = {"a": {"id": 100, "h": 90, "tag": 100, "old": 3}, "b": {"id": 100, "h": 90, "tag": 80, "new": 50}}
    s = compare_schema(a, b, nonnull, required=["id", "h"])
    assert [m["name"] for m in s["missing"]] == ["old"] and not s["missing"][0]["required"]
    assert s["missing"][0]["a_filled_pct"] == 3.0
    assert s["added"] == [{"name": "new", "type": "Integer", "b_filled_pct": 50.0}]
    assert s["type_changed"][0]["name"] == "h" and s["type_changed"][0]["level"] == "fail"
    assert s["null_changes"] == [{"name": "tag", "a_null_pct": 0.0, "b_null_pct": 20.0, "delta_pts": 20.0}]
    assert compare_schema(a, b, nonnull, required=None)["missing"][0]["required"]
    assert not compare_schema(a, b, nonnull, required=[])["missing"][0]["required"]


def _result(**over) -> dict:
    base = {
        "sides": {
            "a": {"name": "a.parquet", "kind": "vector", "buildings": 1000, "crs": "EPSG:4326",
                  "geometry_families": {"polygon": 1000}, "null_geometry": 0, "empty_geometry": 0, "invalid_geometry": 0},
            "b": {"name": "b.parquet", "kind": "vector", "buildings": 1000, "crs": "EPSG:4326",
                  "geometry_families": {"polygon": 1000}, "null_geometry": 0, "empty_geometry": 0, "invalid_geometry": 0},
        },
        "schema": {"fields_a": 3, "fields_b": 3, "common": 3, "missing": [], "added": [], "type_changed": [],
                   "null_changes": []},
        "match": {"mode": "key", "key_a": "id", "matched": 1000, "removed": 0, "added": 0, "changed": 0,
                  "unchanged": 1000, "dup_keys_b": 0, "empty_keys_b": 0},
        "changes": {"geometry": {"identical": 1000, "minor": 0, "moderate": 0, "major": 0},
                    "height": {"changed": 0, "taller": 0, "lower": 0, "default_to_real": 0, "real_to_default": 0},
                    "columns": []},
        "quality": {"a": {"real_height": 50, "real_pct": 5.0, "outliers": 1, "orphan_parts": 0, "dangling_superseded": 0},
                    "b": {"real_height": 50, "real_pct": 5.0, "outliers": 1, "orphan_parts": 0, "dangling_superseded": 0}},
        "grid": {"cell_deg": 0.05, "top": []},
    }
    for path, value in over.items():
        node = base
        keys = path.split(".")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value
    return base


def _codes(gate: dict, level: str | None = None) -> set[str]:
    return {f["code"] for f in gate["findings"] if level is None or f["level"] == level}


def test_identical_release_passes() -> None:
    gate = evaluate(_result(), Thresholds())
    assert gate["verdict"] == "pass" and gate["findings"] == []


@pytest.mark.parametrize(("b_count", "verdict"), [(1015, "pass"), (1030, "warn"), (850, "fail")])
def test_count_change_thresholds(b_count: int, verdict: str) -> None:
    assert evaluate(_result(**{"sides.b.buildings": b_count}), Thresholds())["verdict"] == verdict


@pytest.mark.parametrize(("removed", "verdict"), [(5, "pass"), (20, "warn"), (60, "fail")])
def test_removed_thresholds(removed: int, verdict: str) -> None:
    assert evaluate(_result(**{"match.removed": removed}), Thresholds())["verdict"] == verdict


def test_schema_rules() -> None:
    missing = [{"name": "h", "type": "Real", "a_filled_pct": 10.0, "required": True},
               {"name": "x", "type": "String", "a_filled_pct": 0.0, "required": False}]
    gate = evaluate(_result(**{"schema.missing": missing}), Thresholds())
    assert {"schema_missing"} <= _codes(gate, "fail") and {"schema_missing_optional"} <= _codes(gate, "warn")
    nulls = [{"name": "tier", "a_null_pct": 1.0, "b_null_pct": 9.0, "delta_pts": 8.0}]
    assert "null_increase" in _codes(evaluate(_result(**{"schema.null_changes": nulls}), Thresholds()), "warn")


def test_identity_fails_only_in_key_mode() -> None:
    gate = evaluate(_result(**{"match.dup_keys_b": 2, "match.dup_rows_b": 4, "match.empty_keys_b": 1}), Thresholds())
    assert {"dup_keys_b", "empty_keys_b"} <= _codes(gate, "fail")
    loc = evaluate(_result(**{"match.mode": "location", "match.dup_keys_b": 2, "match.dup_rows_b": 4}), Thresholds())
    assert "dup_keys_b" in _codes(loc, "info") and "location_mode" in _codes(loc, "info")


def test_pmtiles_layer_and_zoom() -> None:
    r = _result()
    for s, layer, zmax in (("a", "buildings", 17), ("b", "canonical_building", 15)):
        r["sides"][s] |= {"kind": "pmtiles", "layer": layer, "minzoom": 13, "maxzoom": zmax, "crs": "EPSG:3857"}
    gate = evaluate(r, Thresholds())
    assert "layer_changed" in _codes(gate, "fail") and "zoom_changed" in _codes(gate, "warn")
    assert "layer_changed" in _codes(evaluate(r, Thresholds(fail_layer_change=False)), "warn")


def test_quality_and_geometry_warnings() -> None:
    r = _result(**{"changes.geometry": {"identical": 900, "minor": 0, "moderate": 50, "major": 50}})
    r["quality"]["b"] |= {"real_pct": 3.0, "outliers": 5, "orphan_parts": 2}
    r["sides"]["b"]["geometry_families"] = {"polygon": 990, "point": 10}
    r["sides"]["b"]["crs"] = "EPSG:32648"
    r["sides"]["b"]["null_geometry"] = 1
    gate = evaluate(r, Thresholds())
    assert {"geom_major", "real_height_drop", "outliers_up", "orphan_parts_up"} <= _codes(gate, "warn")
    assert {"geom_type_new", "crs_changed", "null_geometry_b"} <= _codes(gate, "fail")
    assert [f["level"] for f in gate["findings"]] == sorted((f["level"] for f in gate["findings"]),
                                                           key=["fail", "warn", "info"].index)


def test_render_markdown_smoke() -> None:
    r = copy.deepcopy(_result(**{"match.removed": 60}))
    r["gate"] = evaluate(r, Thresholds())
    r["quality"]["a"] |= {"real_pct": 5.0}
    r["grid"]["top"] = [{"center": [106.7, 10.8], "n_a": 10, "n_b": 4, "added": 0, "removed": 6, "changed": 0}]
    md = render_markdown(r)
    assert md.startswith("# So sánh trước release — FAIL") and "| FAIL |" in md and "10.800, 106.700" in md
