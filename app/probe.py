"""Inspect an input file and suggest how to map its columns.

The probe never modifies data. It answers: which layer, which geometry column,
which CRS (and whether it had to be assumed), what the fields are, and which
columns look like height / floors / id / parent / provenance.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import tools

log = logging.getLogger(__name__)

VECTOR_SUFFIXES = frozenset(
    {".parquet", ".geoparquet", ".gpkg", ".fgb", ".geojson", ".geojsonl", ".geojsons", ".json"}
)
TILES_SUFFIXES = frozenset({".pmtiles"})
ALLOWED_SUFFIXES = VECTOR_SUFFIXES | TILES_SUFFIXES
PARQUET_SUFFIXES = frozenset({".parquet", ".geoparquet"})

# Columns this app writes; on re-processing they are recomputed, not copied.
GENERATED_FIELDS = ("h_m", "h_src", "h_outlier", "has_parts", "is_part")

NUMERIC_TYPES = frozenset({"Integer", "Integer64", "Real"})
TEXT_TYPES = frozenset({"String"})
GEOMETRY_NAME_HINTS = ("geometry", "geom", "wkb", "wkt", "shape")

GEOM_SAMPLE_SIZE = 1000
PROBE_TIMEOUT_S = 300

HEIGHT_NAMES = ("height_m", "height", "building_height", "bldg_height", "height_meters", "est_height", "hgt")
HEIGHT_EXCLUDE = ("min", "provenance", "source", "src", "conf", "method", "type", "flag", "unit")
LEVELS_NAMES = (
    "num_floors", "building:levels", "building_levels", "levels", "floors",
    "num_levels", "storeys", "stories", "n_floors", "floor_count",
)
MIN_HEIGHT_NAMES = ("min_height", "min_height_m", "building:min_height", "base_height")
ID_NAMES = ("building_id", "bldg_id", "id", "osm_id", "uuid", "gid", "objectid")
PARENT_NAMES = ("parent_building_id", "parent_id", "building_parent_id", "parent")
CATEGORY_HINTS = ("provenance", "tier", "class", "category", "kind", "type", "source", "status")
STYLE_HINTS = ("tier", "class", "category", "kind")
MAX_CATEGORY_SUGGESTIONS = 2
DEFAULT_MISSING_PROVENANCE = ("default",)


class ProbeError(RuntimeError):
    """Raised when the input cannot be read as a supported dataset."""


@dataclass(frozen=True)
class Field:
    """A non-geometry attribute column."""

    name: str
    type: str
    subtype: str | None = None

    @property
    def is_numeric(self) -> bool:
        return self.type in NUMERIC_TYPES

    @property
    def is_text(self) -> bool:
        return self.type in TEXT_TYPES


def resolve_input_path(raw: str) -> Path:
    """Validate a user-supplied path and return it resolved.

    Raises:
        ProbeError: If the path does not exist or has an unsupported suffix.
    """
    path = Path(raw.strip().strip('"').strip("'")).expanduser()
    if not path.is_file():
        raise ProbeError(f"Không tìm thấy file: {path}")
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        allowed = ", ".join(sorted(ALLOWED_SUFFIXES))
        raise ProbeError(f"Định dạng {path.suffix or '(không có đuôi)'} chưa hỗ trợ. Hỗ trợ: {allowed}")
    return path.resolve()


def probe(path: Path, geom_column: str | None = None, layer: str | None = None) -> dict[str, Any]:
    """Inspect a vector dataset or a PMTiles archive.

    Args:
        path: Existing file with a supported suffix.
        geom_column: Force this column as geometry (Parquet without geo metadata).
        layer: Layer to inspect; defaults to the first one.

    Returns:
        A JSON-serialisable description used by the UI and the pipeline.

    Raises:
        ProbeError: If GDAL cannot read the file.
    """
    suffix = path.suffix.lower()
    if suffix in TILES_SUFFIXES:
        return probe_pmtiles(path)

    open_options: dict[str, str] = {}
    is_parquet = suffix in PARQUET_SUFFIXES
    if is_parquet and geom_column:
        open_options["GEOM_POSSIBLE_NAMES"] = geom_column

    info = _ogrinfo_json(path, open_options)
    layer_info = _pick_layer(info, layer)
    fields = _fields(layer_info)
    candidates = guess_geometry_columns(fields)
    warnings: list[str] = []

    if is_parquet and not layer_info.get("geometryFields") and candidates and not geom_column:
        # Plain Parquet with a WKB/WKT column but no GeoParquet metadata.
        open_options["GEOM_POSSIBLE_NAMES"] = candidates[0]
        info = _ogrinfo_json(path, open_options)
        layer_info = _pick_layer(info, layer_info["name"])
        warnings.append(
            f"File không có metadata GeoParquet; dùng cột '{candidates[0]}' làm geometry (WKB/WKT)."
        )
        fields = _fields(layer_info)

    geom_fields = layer_info.get("geometryFields") or []
    if not geom_fields:
        raise ProbeError(
            "Không tìm thấy cột geometry. Với Parquet thường, hãy chọn cột chứa WKB/WKT."
            + (f" Cột có thể là geometry: {', '.join(candidates)}" if candidates else "")
        )
    geom = geom_fields[0]
    extent = geom.get("extent")
    crs = describe_crs(geom.get("coordinateSystem"), extent)
    if crs["missing"]:
        warnings.append(crs["note"])

    geom_name = geom.get("name") or ""
    # GDAL's SQLite dialect exposes an unnamed geometry field as "GEOMETRY".
    sql_geom_name = geom_name or "GEOMETRY"
    declared_type = geom.get("type") or "Unknown"
    sampled = None
    if declared_type in ("Geometry", "Unknown", "Unknown (any)"):
        sampled = _sample_geometry_types(path, open_options, layer_info["name"], sql_geom_name)

    return {
        "kind": "vector",
        "path": str(path),
        "name": path.name,
        "size": path.stat().st_size,
        "driver": info.get("driverShortName"),
        "layers": [lyr["name"] for lyr in info.get("layers", [])],
        "layer": layer_info["name"],
        "feature_count": layer_info.get("featureCount"),
        "open_options": open_options,
        "geometry": {
            "column": geom_name,
            "sql_name": sql_geom_name,
            "type": declared_type,
            "sampled_types": sampled,
            "nlt": choose_nlt(declared_type, sampled),
        },
        "geometry_candidates": candidates,
        "crs": crs,
        "extent": extent,
        "fields": [asdict(f) | {"generated": f.name in GENERATED_FIELDS} for f in fields],
        "suggest": suggest_mapping(fields),
        "warnings": warnings,
    }


def probe_pmtiles(path: Path) -> dict[str, Any]:
    """Read header and layer metadata of a PMTiles archive."""
    result: dict[str, Any] = {
        "kind": "pmtiles",
        "path": str(path),
        "name": path.name,
        "size": path.stat().st_size,
        "header": None,
        "vector_layers": [],
        "warnings": [],
    }
    exe = tools.which("pmtiles")
    if exe is None:
        result["warnings"].append("Chưa cài pmtiles CLI nên không đọc được header (bản đồ vẫn xem được).")
        return result
    header = tools.run_capture([exe, "show", "--header-json", str(path)], timeout=60)
    meta = tools.run_capture([exe, "show", "--metadata", str(path)], timeout=60)
    if header.returncode != 0:
        raise ProbeError(f"Không đọc được PMTiles: {header.stderr.strip() or header.stdout.strip()}")
    result["header"] = json.loads(header.stdout)
    if meta.returncode == 0 and meta.stdout.strip():
        metadata = json.loads(meta.stdout)
        result["vector_layers"] = metadata.get("vector_layers", [])
        result["generator"] = metadata.get("generator")
    return result


def describe_crs(coordinate_system: dict | None, extent: list[float] | None) -> dict[str, Any]:
    """Summarise a GDAL coordinateSystem object, flagging a missing CRS.

    A missing CRS is never silently assumed: the result carries `missing=True`
    plus a suggestion and a note that the UI shows to the user.
    """
    if coordinate_system:
        projjson = coordinate_system.get("projjson") or {}
        ident = projjson.get("id") or {}
        auth = f"{ident['authority']}:{ident['code']}" if ident.get("authority") else None
        return {
            "missing": False,
            "label": auth or projjson.get("name") or "Custom CRS",
            "name": projjson.get("name"),
            "definition": auth or coordinate_system.get("wkt"),
            "suggested": None,
            "note": None,
        }
    lonlat = extent is not None and looks_like_lonlat(extent)
    return {
        "missing": True,
        "label": None,
        "name": None,
        "definition": None,
        "suggested": "EPSG:4326" if lonlat else None,
        "note": (
            "File không khai báo CRS. Extent nằm trong khoảng kinh/vĩ độ nên đề xuất EPSG:4326 — "
            "hãy xác nhận trước khi chạy."
            if lonlat
            else "File không khai báo CRS và extent không phải kinh/vĩ độ — bạn phải nhập CRS nguồn."
        ),
    }


def looks_like_lonlat(extent: list[float]) -> bool:
    """True when [minx, miny, maxx, maxy] fits inside geographic degree bounds."""
    minx, miny, maxx, maxy = extent
    return -180 <= minx <= maxx <= 180 and -90 <= miny <= maxy <= 90


def choose_nlt(declared_type: str, sampled: list[str] | None) -> str:
    """Pick ogr2ogr's -nlt so the GeoPackage layer gets a concrete type."""
    kinds = {declared_type.upper().replace(" ", "")} if not sampled else {t.upper() for t in sampled}
    kinds.discard("")
    if kinds and kinds <= {"POLYGON", "MULTIPOLYGON"}:
        return "MULTIPOLYGON"
    if kinds and kinds <= {"LINESTRING", "MULTILINESTRING"}:
        return "MULTILINESTRING"
    if kinds == {"POINT"}:
        return "POINT"
    return "PROMOTE_TO_MULTI"


def guess_geometry_columns(fields: list[Field]) -> list[str]:
    """Return columns that look like WKB (binary) or WKT (text) geometry."""
    binary = [f.name for f in fields if f.type == "Binary"]
    hinted_binary = [n for n in binary if any(h in n.lower() for h in GEOMETRY_NAME_HINTS)]
    wkt = [f.name for f in fields if f.is_text and "wkt" in f.name.lower()]
    ordered = hinted_binary + [n for n in binary if n not in hinted_binary] + wkt
    return list(dict.fromkeys(ordered))


def suggest_mapping(fields: list[Field]) -> dict[str, Any]:
    """Guess which columns hold height, floors, ids, parents and categories."""
    usable = [f for f in fields if f.name not in GENERATED_FIELDS]
    by_lower = {f.name.lower(): f for f in usable}

    def first_exact(names: tuple[str, ...], accept=lambda f: True) -> str | None:
        for name in names:
            f = by_lower.get(name)
            if f is not None and accept(f):
                return f.name
        return None

    def numeric_like(f: Field) -> bool:
        return f.is_numeric or f.is_text

    height = first_exact(HEIGHT_NAMES, numeric_like)
    if height is None:
        for f in usable:
            low = f.name.lower()
            if "height" in low and numeric_like(f) and not any(x in low for x in HEIGHT_EXCLUDE):
                height = f.name
                break

    levels = first_exact(LEVELS_NAMES, numeric_like)
    if levels is None:
        for f in usable:
            low = f.name.lower()
            if ("floor" in low or "level" in low) and numeric_like(f) and "min" not in low:
                levels = f.name
                break

    min_height = first_exact(MIN_HEIGHT_NAMES, numeric_like)
    id_col = first_exact(ID_NAMES)
    parent = first_exact(PARENT_NAMES)

    prov = None
    for f in usable:
        low = f.name.lower()
        if f.is_text and (
            "provenance" in low or ("height" in low and any(x in low for x in ("source", "src", "method")))
        ):
            prov = f.name
            break

    # Hints are ordered by usefulness, so "tier" beats an incidental "source".
    categories: list[str] = [prov] if prov else []
    for hint in CATEGORY_HINTS:
        for f in usable:
            low = f.name.lower()
            if len(categories) >= MAX_CATEGORY_SUGGESTIONS:
                break
            if f.is_text and f.name not in categories and not low.endswith("id") and hint in low:
                categories.append(f.name)

    style = [c for c in categories if any(h in c.lower() for h in STYLE_HINTS)]
    pmtiles_attrs = [
        a for a in (id_col, "h_m", min_height, "has_parts", "is_part", parent, *style) if a
    ]

    return {
        "height_col": height,
        "levels_col": levels,
        "min_height_col": min_height,
        "id_col": id_col,
        "parent_col": parent,
        "prov_col": prov,
        "prov_missing": list(DEFAULT_MISSING_PROVENANCE) if prov else [],
        "category_cols": categories,
        "pmtiles_attrs": list(dict.fromkeys(pmtiles_attrs)),
    }


def _ogrinfo_json(path: Path, open_options: dict[str, str]) -> dict[str, Any]:
    cmd = [tools.require("ogrinfo"), "-json", "-so", "-al", *_oo_args(open_options), str(path)]
    proc = tools.run_capture(cmd, timeout=PROBE_TIMEOUT_S)
    if proc.returncode != 0:
        raise ProbeError(f"GDAL không đọc được file: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"Không phân tích được output của ogrinfo: {exc}") from exc


def _pick_layer(info: dict[str, Any], layer: str | None) -> dict[str, Any]:
    layers = info.get("layers") or []
    if not layers:
        raise ProbeError("File không có layer nào.")
    if layer is None:
        return layers[0]
    for lyr in layers:
        if lyr["name"] == layer:
            return lyr
    raise ProbeError(f"Không có layer '{layer}' trong file.")


def _fields(layer_info: dict[str, Any]) -> list[Field]:
    return [
        Field(name=f["name"], type=f.get("type", "String"), subtype=f.get("subType"))
        for f in layer_info.get("fields", [])
    ]


def _sample_geometry_types(
    path: Path, open_options: dict[str, str], layer: str, sql_geom: str
) -> list[str] | None:
    """Look at the first rows to learn the real geometry type (Parquet has none)."""
    sql = (
        f"SELECT ST_GeometryType(g) AS t, COUNT(*) AS n FROM "
        f"(SELECT {quote_ident(sql_geom)} AS g FROM {quote_ident(layer)} LIMIT {GEOM_SAMPLE_SIZE}) "
        f"WHERE g IS NOT NULL GROUP BY t"
    )
    cmd = [
        tools.require("ogrinfo"), "-ro", "-json", "-features", *_oo_args(open_options),
        "-dialect", "SQLite", "-sql", sql, str(path),
    ]
    try:
        proc = tools.run_capture(cmd, timeout=PROBE_TIMEOUT_S)
        if proc.returncode != 0:
            log.info("Geometry sampling failed: %s", proc.stderr.strip())
            return None
        data = json.loads(proc.stdout)
        feats = data["layers"][0].get("features", [])
        return sorted({str(f["properties"]["t"]).upper() for f in feats if f["properties"].get("t")})
    except (json.JSONDecodeError, KeyError, IndexError, OSError) as exc:
        log.info("Geometry sampling failed: %s", exc)
        return None


def _oo_args(open_options: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in open_options.items():
        args += ["-oo", f"{key}={value}"]
    return args


def quote_ident(name: str) -> str:
    """Quote an SQL identifier (column / table) for SQLite."""
    return '"' + name.replace('"', '""') + '"'
