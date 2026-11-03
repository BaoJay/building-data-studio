"""The building PMTiles schema agreed with VMAP (schemas/vmap-buildings.json).

With a schema, the convert pipeline puts exactly the schema's fields into the
tiles, each coerced to the schema's type, then checks the PMTiles it wrote
against the schema. Adding a field to the tiles is one line in the JSON file.

A field listed under "copies" repeats another field's value (e.g. `h_m` and
`height` repeat `height_m`, the height the app renders).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .probe import quote_ident as q
from .sqlbuild import numeric_expr

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
VMAP_BUILDINGS = SCHEMA_DIR / "vmap-buildings.json"
MAX_ZOOM = 22
# TileJSON field type -> tippecanoe -T type. GDAL types a computed column by its first row
# (a NULL there makes it a string), so the type is set again when tiling.
TIPPECANOE_TYPES = {"String": "string", "Number": "float", "Boolean": "bool"}
# Substrings of SQLite / GeoPackage declared types read as numbers or text without a cast.
NUMERIC_DECLS = ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC", "BOOL")
TEXT_DECLS = ("TEXT", "CHAR", "CLOB")


class SchemaError(ValueError):
    """The schema file is missing or malformed (message is shown to the user)."""


@dataclass(frozen=True)
class TileSchema:
    """One vector layer of a PMTiles archive: name, zooms and typed fields."""

    name: str
    source: str
    layer: str
    minzoom: int
    maxzoom: int
    fields: dict[str, str]  # field -> TileJSON type ("String" / "Number" / "Boolean"), in schema order
    copies: dict[str, str]  # field -> the field whose value it repeats
    deprecated: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def load(path: Path = VMAP_BUILDINGS) -> TileSchema:
    """Read and validate a schema file.

    Raises:
        SchemaError: The file cannot be read or breaks a rule (types, zooms, copies).
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaError(f"Không đọc được schema {path.name}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SchemaError(f"Schema {path.name} phải là một object JSON.")
    fields = raw.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise SchemaError(f"Schema {path.name} thiếu 'fields'.")
    bad = [f"{k}: {v}" for k, v in fields.items() if v not in TIPPECANOE_TYPES]
    if bad:
        raise SchemaError(f"Kiểu field không hợp lệ trong {path.name} (chỉ nhận "
                          f"{', '.join(TIPPECANOE_TYPES)}): {'; '.join(bad)}")
    copies = raw.get("copies") or {}
    for field, src in copies.items():
        if field not in fields or src not in fields or src in copies:
            raise SchemaError(f"'copies' trong {path.name}: {field} → {src} phải trỏ giữa hai field "
                              "của schema, và field nguồn không được là bản sao.")
    deprecated = tuple(raw.get("deprecated") or ())
    unknown = [f for f in deprecated if f not in fields]
    if unknown:
        raise SchemaError(f"'deprecated' trong {path.name} có field không thuộc schema: {', '.join(unknown)}")
    try:
        minzoom, maxzoom = int(raw["minzoom"]), int(raw["maxzoom"])
        layer = str(raw["layer"]).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise SchemaError(f"Schema {path.name} cần 'layer', 'minzoom' và 'maxzoom'.") from exc
    if not layer or not 0 <= minzoom <= maxzoom <= MAX_ZOOM:
        raise SchemaError(f"Schema {path.name}: layer rỗng hoặc zoom không hợp lệ ({minzoom}–{maxzoom}).")
    return TileSchema(
        name=str(raw.get("name") or path.stem),
        source=str(raw.get("source") or ""),
        layer=layer,
        minzoom=minzoom,
        maxzoom=maxzoom,
        fields={str(k): str(v) for k, v in fields.items()},
        copies={str(k): str(v) for k, v in copies.items()},
        deprecated=deprecated,
    )


def select_items(schema: TileSchema, columns: dict[str, str]) -> tuple[list[str], list[str]]:
    """SQL SELECT items that give the schema's fields from a GeoPackage table.

    Args:
        schema: The tile schema.
        columns: Column name -> declared type of the table (PRAGMA table_info).

    Returns:
        (items, missing): one `<expr> AS "<field>"` per field the table can fill, in schema
        order, and the fields it cannot (no column, or no source column for a copy).
    """
    items: list[str] = []
    missing: list[str] = []
    for name, ftype in schema.fields.items():
        col = schema.copies.get(name, name)
        decl = columns.get(col)
        if decl is None:
            missing.append(name)
            continue
        items.append(f"{_coerce(col, decl, ftype)} AS {q(name)}")
    return items, missing


def _coerce(col: str, decl: str, ftype: str) -> str:
    """Read a column as the field type; text that is not a number becomes NULL, not 0."""
    kind = decl.upper()
    if ftype == "Number":
        return q(col) if any(t in kind for t in NUMERIC_DECLS) else f"CAST({numeric_expr(col, True)} AS REAL)"
    if ftype == "String":
        return q(col) if any(t in kind for t in TEXT_DECLS) else f"CAST({q(col)} AS TEXT)"
    return q(col)


def tippecanoe_types(schema: TileSchema, fields: list[str]) -> list[str]:
    """tippecanoe arguments that fix the type of each field written to the tiles."""
    args: list[str] = []
    for name in fields:
        args += ["-T", f"{name}:{TIPPECANOE_TYPES[schema.fields[name]]}"]
    return args


def check(schema: TileSchema, vector_layers: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare the `vector_layers` metadata of a written PMTiles with the schema.

    tippecanoe leaves out a field that is NULL on every feature, so an empty column
    shows up here as missing.
    """
    layer = next((lyr for lyr in vector_layers if lyr.get("id") == schema.layer), None)
    got: dict[str, str] = (layer or {}).get("fields") or {}
    missing = [f for f in schema.fields if f not in got]
    extra = [f for f in got if f not in schema.fields]
    wrong_type = [{"field": f, "expected": t, "got": got[f]}
                  for f, t in schema.fields.items() if f in got and got[f] != t]
    zooms = [layer.get("minzoom"), layer.get("maxzoom")] if layer else None
    zoom_ok = zooms == [schema.minzoom, schema.maxzoom]
    return {
        "schema": schema.name,
        "layer": schema.layer,
        "layer_found": layer is not None,
        "fields": len(schema.fields),
        "matched": len(schema.fields) - len(missing) - len(wrong_type),
        "missing": missing,
        "extra": extra,
        "wrong_type": wrong_type,
        "zooms": zooms,
        "expected_zooms": [schema.minzoom, schema.maxzoom],
        "ok": layer is not None and not (missing or extra or wrong_type) and zoom_ok,
    }
