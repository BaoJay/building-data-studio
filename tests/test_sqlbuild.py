"""Height-rule SQL is evaluated with stdlib sqlite3 — same SQL GDAL runs."""

import math
import sqlite3

import pytest

from app.sqlbuild import (
    HeightRule,
    NormalizeSpec,
    SpecError,
    build_normalize_sql,
    build_parts_statements,
    height_expressions,
    validate_where,
)


def evaluate(rule: HeightRule, rows: list[tuple]) -> list[tuple]:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (height, levels, prov)")
    conn.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
    e = height_expressions(rule)
    return conn.execute(f"SELECT {e['h_m']}, {e['h_src']}, {e['h_outlier']} FROM t").fetchall()


BASE = HeightRule(height_col="height", levels_col="levels", min_valid_m=2, max_valid_m=500, max_levels=200)


def test_valid_height_is_used_and_rounded() -> None:
    assert evaluate(BASE, [(12.34, 3, None)]) == [(12.3, "height", 0)]


def test_falls_back_to_levels_then_default() -> None:
    assert evaluate(BASE, [(None, 5, None), (None, None, None)]) == [(17.5, "levels", 0), (4.0, "default", 0)]


def test_too_tall_height_is_outlier_and_falls_back() -> None:
    assert evaluate(BASE, [(49380, None, None), (49380, 10, None)]) == [(4.0, "default", 1), (35.0, "levels", 1)]


def test_zero_height_is_outlier() -> None:
    assert evaluate(BASE, [(0, None, None)]) == [(4.0, "default", 1)]


def test_invalid_levels_flagged() -> None:
    assert evaluate(BASE, [(None, 0, None), (None, 14108, None)]) == [(4.0, "default", 1), (4.0, "default", 1)]


def test_provenance_default_means_no_measurement() -> None:
    rule = HeightRule(height_col="height", prov_col="prov", prov_missing=("default",))
    assert evaluate(rule, [(4, None, "default"), (30, None, "osm_height")]) == [
        (4.0, "default", 0),
        (30.0, "height", 0),
    ]


def test_clamp_mode() -> None:
    rule = HeightRule(height_col="height", max_valid_m=500, outlier_mode="clamp")
    assert evaluate(rule, [(900, None, None), (0, None, None)]) == [(500.0, "clamped", 1), (4.0, "default", 1)]


def test_keep_mode_keeps_raw_value_but_flags_it() -> None:
    rule = HeightRule(height_col="height", outlier_mode="keep")
    assert evaluate(rule, [(900, None, None), (0, None, None)]) == [(900.0, "height", 1), (0.0, "height", 1)]


def test_text_columns_are_parsed_leniently() -> None:
    rule = HeightRule(height_col="height", height_is_text=True, levels_col="levels", levels_is_text=True)
    rows = [("12,5 m", None, None), ("abc", "4", None), ("", None, None)]
    assert evaluate(rule, rows) == [(12.5, "height", 0), (14.0, "levels", 0), (4.0, "default", 0)]


def test_no_columns_gives_default_everywhere() -> None:
    assert evaluate(HeightRule(default_m=3), [(10, 2, None)]) == [(3.0, "default", 0)]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_valid_m": 10, "max_valid_m": 5},
        {"m_per_level": 0},
        {"default_m": math.nan},
        {"outlier_mode": "nope"},
        {"decimals": 7},
    ],
)
def test_invalid_rules_rejected(kwargs: dict) -> None:
    with pytest.raises(SpecError):
        height_expressions(HeightRule(height_col="height", **kwargs))


def test_normalize_sql_quotes_identifiers_and_adds_where() -> None:
    sql = build_normalize_sql(
        NormalizeSpec(layer='my "layer"', geom_sql_name="geometry_wkb", columns=("building:levels", "name"),
                      height=None, where="status = 'active'")
    )
    assert sql == (
        'SELECT "building:levels", "name", "geometry_wkb" FROM "my ""layer""" WHERE (status = \'active\')'
    )


def test_where_rejects_statement_separator() -> None:
    assert validate_where("  ") is None
    with pytest.raises(SpecError):
        validate_where("1=1; DROP TABLE x")


def test_parts_statements_mark_parents_and_parts() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute('CREATE TABLE "b" (id TEXT, parent TEXT)')
    conn.executemany('INSERT INTO "b" VALUES (?, ?)', [("A", None), ("A1", "A"), ("A2", "A"), ("B", None), ("C", "C"), ("D", "")])
    for sql in build_parts_statements("b", "id", "parent"):
        conn.execute(sql)
    rows = dict((r[0], (r[1], r[2])) for r in conn.execute('SELECT id, has_parts, is_part FROM "b"'))
    assert rows == {"A": (1, 0), "A1": (0, 1), "A2": (0, 1), "B": (0, 0), "C": (1, 0), "D": (0, 0)}
