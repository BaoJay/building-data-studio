"""Build the SQL used to normalise attributes and compute building heights.

Pure string builders with no I/O, so every rule is unit-tested in isolation.

Height rule (per feature), in priority order:
    1. a valid value from the height column        -> h_src = 'height'
    2. levels x metres-per-level                   -> h_src = 'levels'
    3. the default height                          -> h_src = 'default'
A height counts as "valid" inside [min_valid_m, max_valid_m]. Values outside
that range set h_outlier = 1 so they can be reviewed; `outlier_mode` decides
what h_m becomes for them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .probe import quote_ident as q

OutlierMode = Literal["fallback", "clamp", "keep"]
OUTLIER_MODES: tuple[str, ...] = ("fallback", "clamp", "keep")

MIN_LEVELS = 1
MAX_DECIMALS = 3
TMP_PARENT_INDEX = "bc_tmp_parent_idx"


class SpecError(ValueError):
    """Raised for an invalid normalisation spec (shown to the user as-is)."""


@dataclass(frozen=True)
class HeightRule:
    """How to derive h_m (metres) for each building."""

    height_col: str | None = None
    height_is_text: bool = False
    levels_col: str | None = None
    levels_is_text: bool = False
    m_per_level: float = 3.5
    default_m: float = 4.0
    min_valid_m: float = 2.0
    max_valid_m: float = 500.0
    max_levels: float = 200.0
    outlier_mode: OutlierMode = "fallback"
    decimals: int = 1
    prov_col: str | None = None
    prov_missing: tuple[str, ...] = ()

    def validate(self) -> None:
        """Raise SpecError when numbers are non-finite or ranges are inverted."""
        for name in ("m_per_level", "default_m", "min_valid_m", "max_valid_m", "max_levels"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise SpecError(f"{name} phải là số hữu hạn (đang là {value!r}).")
        if self.m_per_level <= 0:
            raise SpecError("Số mét mỗi tầng phải > 0.")
        if self.default_m < 0:
            raise SpecError("Chiều cao mặc định không được âm.")
        if self.min_valid_m >= self.max_valid_m:
            raise SpecError("Khoảng chiều cao hợp lệ: giá trị nhỏ nhất phải < lớn nhất.")
        if self.max_levels < MIN_LEVELS:
            raise SpecError("Số tầng tối đa phải ≥ 1.")
        if self.outlier_mode not in OUTLIER_MODES:
            raise SpecError(f"Chế độ xử lý outlier không hợp lệ: {self.outlier_mode}")
        if not 0 <= self.decimals <= MAX_DECIMALS:
            raise SpecError(f"Số chữ số thập phân phải trong khoảng 0–{MAX_DECIMALS}.")


@dataclass(frozen=True)
class NormalizeSpec:
    """What the normalisation SELECT copies, computes and filters."""

    layer: str
    geom_sql_name: str
    columns: tuple[str, ...]
    height: HeightRule | None = None
    where: str | None = None


def lit(value: float) -> str:
    """Render a finite number as an SQL literal ('.' decimal separator)."""
    number = float(value)
    if not math.isfinite(number):
        raise SpecError(f"Giá trị số không hợp lệ: {value!r}")
    return repr(number)


def sql_str(value: str) -> str:
    """Render a Python string as an SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def numeric_expr(column: str, is_text: bool) -> str:
    """SQL that reads a column as REAL; text like '12,5' or '12 m' is tolerated.

    Text that does not start with a digit becomes NULL rather than 0, so junk
    values fall through to the next rule instead of looking like a 0 m height.
    """
    col = q(column)
    if not is_text:
        return f"CAST({col} AS REAL)"
    trimmed = f"TRIM({col})"
    return (
        f"(CASE WHEN {trimmed} GLOB '[0-9]*' OR {trimmed} GLOB '.[0-9]*' "
        f"THEN CAST(REPLACE({trimmed}, ',', '.') AS REAL) END)"
    )


def height_expressions(rule: HeightRule) -> dict[str, str]:
    """Return SQL expressions for the generated columns h_m, h_src and h_outlier."""
    rule.validate()
    d = int(rule.decimals)
    lo, hi = lit(rule.min_valid_m), lit(rule.max_valid_m)

    hv = numeric_expr(rule.height_col, rule.height_is_text) if rule.height_col else None
    if hv and rule.prov_col and rule.prov_missing:
        # Rows whose provenance says "default" carry a placeholder, not a measurement.
        values = ", ".join(sql_str(v) for v in rule.prov_missing)
        hv = f"(CASE WHEN {q(rule.prov_col)} IN ({values}) THEN NULL ELSE {hv} END)"
    lv = numeric_expr(rule.levels_col, rule.levels_is_text) if rule.levels_col else None

    valid_h = f"({hv} BETWEEN {lo} AND {hi})" if hv else None
    valid_l = f"({lv} BETWEEN {MIN_LEVELS} AND {lit(rule.max_levels)})" if lv else None
    keep = rule.outlier_mode == "keep"

    branches: list[tuple[str, str, str]] = []  # (condition, h_m value, h_src label)
    if hv:
        branches.append((f"{hv} IS NOT NULL" if keep else valid_h, f"ROUND({hv}, {d})", "height"))
        if rule.outlier_mode == "clamp":
            branches.append((f"{hv} > {hi}", hi, "clamped"))
    if lv:
        level_m = f"ROUND({lv} * {lit(rule.m_per_level)}, {d})"
        branches.append((f"{lv} IS NOT NULL" if keep else valid_l, level_m, "levels"))

    default = lit(rule.default_m)
    if branches:
        whens_m = " ".join(f"WHEN {cond} THEN {value}" for cond, value, _ in branches)
        whens_src = " ".join(f"WHEN {cond} THEN {sql_str(src)}" for cond, _, src in branches)
        h_m = f"CAST(CASE {whens_m} ELSE {default} END AS REAL)"
        h_src = f"CAST(CASE {whens_src} ELSE 'default' END AS TEXT)"
    else:
        h_m = f"CAST({default} AS REAL)"
        h_src = "CAST('default' AS TEXT)"

    outlier_terms = []
    if hv:
        outlier_terms.append(f"({hv} IS NOT NULL AND NOT {valid_h})")
    if lv:
        outlier_terms.append(f"({lv} IS NOT NULL AND NOT {valid_l})")
    h_outlier = (
        f"CAST(CASE WHEN {' OR '.join(outlier_terms)} THEN 1 ELSE 0 END AS INTEGER)"
        if outlier_terms
        else "CAST(0 AS INTEGER)"
    )
    return {"h_m": h_m, "h_src": h_src, "h_outlier": h_outlier}


def validate_where(where: str | None) -> str | None:
    """Normalise an optional user WHERE clause; reject statement separators."""
    if where is None or not where.strip():
        return None
    clause = where.strip()
    if ";" in clause:
        raise SpecError("Điều kiện lọc không được chứa dấu ';'.")
    return clause


def build_normalize_sql(spec: NormalizeSpec) -> str:
    """Build the SELECT run by ogr2ogr (GDAL SQLite dialect) to normalise a layer."""
    parts = [q(c) for c in spec.columns]
    if spec.height is not None:
        parts += [f"{expr} AS {q(name)}" for name, expr in height_expressions(spec.height).items()]
    parts.append(q(spec.geom_sql_name))
    sql = f"SELECT {', '.join(parts)} FROM {q(spec.layer)}"
    where = validate_where(spec.where)
    if where:
        sql += f" WHERE ({where})"
    return sql


def build_parts_statements(table: str, id_col: str, parent_col: str) -> list[str]:
    """SQL statements adding has_parts / is_part to a GeoPackage table.

    Runs through GDAL (not raw sqlite3) because GeoPackage triggers call
    GDAL-registered ST_* functions. With the temporary index on the parent
    column the correlated EXISTS is O(n log n).
    """
    t, i, p = q(table), q(id_col), q(parent_col)
    idx = q(TMP_PARENT_INDEX)
    return [
        f'ALTER TABLE {t} ADD COLUMN "has_parts" BOOLEAN',
        f'ALTER TABLE {t} ADD COLUMN "is_part" BOOLEAN',
        f"CREATE INDEX IF NOT EXISTS {idx} ON {t}({p})",
        (
            f'UPDATE {t} SET "has_parts" = EXISTS (SELECT 1 FROM {t} AS c WHERE c.{p} = {t}.{i}), '
            f"\"is_part\" = ({p} IS NOT NULL AND {p} <> '' AND {p} IS NOT {i})"
        ),
        f"DROP INDEX IF EXISTS {idx}",
    ]
