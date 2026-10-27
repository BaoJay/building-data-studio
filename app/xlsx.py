"""Excel input: turn one sheet of an .xlsx into a GeoPackage the convert pipeline can read.

    .xlsx sheet (GDAL XLSX driver, every cell read as text)
      ├─ header row (auto-detected, the user can change it) ──► column names + types
      └─ geometry, one of:
           ├─ a WKT / GeoJSON column (in a CRS the user confirms)
           └─ an OSM way id column ── Overpass API ──► LineString ── buffer (m, per row) ──► Polygon
                └─ DuckDB spatial ──► staged GeoPackage (EPSG:4326)

The staged file then goes through probe → options → convert like any other input, so heights,
report and tiles need no Excel-specific code.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import __version__, net, tools
from .probe import SHEET_SUFFIXES
from .probe import quote_ident as q
from .sqlbuild import sql_str

log = logging.getLogger(__name__)

# Without these the driver guesses a header row and cell types per sheet; reading raw text keeps
# row numbers equal to Excel's and lets the user pick the header.
GDAL_CONFIG = ("--config", "OGR_XLSX_HEADERS", "DISABLE", "--config", "OGR_XLSX_FIELD_TYPES", "STRING")
READ_TIMEOUT_S = 300
SAMPLE_ROWS = 40
PREVIEW_BEFORE = 3
PREVIEW_AFTER = 6
PREVIEW_CELL_CHARS = 80
HEADER_MAX_CHARS = 100
HEADER_LOOKAHEAD = 5
MAX_NAME_LENGTH = 120
GUESS_SHARE = 0.8

WGS84 = "EPSG:4326"
DEFAULT_BUFFER_M = 25.0
MAX_BUFFER_M = 5000.0
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_BATCH = 500
OVERPASS_TIMEOUT_S = 180
MAX_LISTED_IDS = 20

ROW_COL = "xlsx_row"
WAY_COL = "osm_way_id"
BUFFER_COL = "buffer_m"
# Names the staged file or the pipeline writes itself; a sheet column with one of them is renamed.
RESERVED = frozenset({"fid", "geom", "geometry", ROW_COL, WAY_COL, BUFFER_COL})

_NUMBER_RE = re.compile(r"[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?")
_INT_RE = re.compile(r"[-+]?\d{1,15}")
_LEADING_ZERO_RE = re.compile(r"[-+]?0\d")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_:.]*")
_WAY_RE = re.compile(r"(?:\S*/way/|way\s*/?\s*|w)?(\d{1,15})(?:\.0+)?", re.IGNORECASE)
_ID_NAME_RE = re.compile(r"(way|osm)[\s_:]*id\b", re.IGNORECASE)
BUFFER_HINTS = ("buffer", "bán kính", "ban_kinh", "radius")
WIDTH_HINTS = ("rộng", "be_rong", "width", "mặt cắt", "mat_cat")
_WKT_RE = re.compile(r"\s*(MULTI)?(POINT|LINESTRING|POLYGON)|\s*GEOMETRYCOLLECTION", re.IGNORECASE)

GeomMode = Literal["wkt", "osm"]
Row = tuple[int, list[str]]  # (Excel row number, cell texts)


class XlsxError(RuntimeError):
    """The sheet cannot be read or staged (message is shown to the user)."""


@dataclass(frozen=True)
class Column:
    """A sheet column kept in the staged file."""

    index: int  # 0-based position in the sheet
    name: str
    type: str  # GDAL type name: Integer / Real / String


@dataclass(frozen=True)
class GeomSpec:
    """Where a sheet's geometry comes from."""

    mode: GeomMode
    column: str
    crs: str = WGS84  # wkt: CRS of the coordinates
    buffer_col: str | None = None  # osm: metres on each side of the way, per row
    buffer_m: float = DEFAULT_BUFFER_M  # osm: used where buffer_col is empty / not a number


def is_sheet(path: Path) -> bool:
    return path.suffix.lower() in SHEET_SUFFIXES


# ------------------------------------------------------------------ reading

def list_sheets(path: Path) -> list[dict[str, Any]]:
    """Sheets of a workbook with their row counts (blank rows included)."""
    proc = tools.run_capture([tools.require("ogrinfo"), *GDAL_CONFIG, "-ro", "-json", "-so", str(path)],
                             timeout=READ_TIMEOUT_S)
    if proc.returncode != 0:
        raise XlsxError(f"GDAL không đọc được file Excel: {tools.stderr_detail(proc.stderr) or proc.stdout.strip()}")
    try:
        layers = json.loads(proc.stdout).get("layers") or []
    except json.JSONDecodeError as exc:
        raise XlsxError(f"Không phân tích được output của ogrinfo: {exc}") from exc
    return [{"name": lyr["name"], "rows": lyr.get("featureCount")} for lyr in layers]


def read_rows(path: Path, sheet: str, limit: int | None = None) -> list[Row]:
    """Raw rows of a sheet as text, numbered like Excel (row 1 = first row)."""
    cmd = [
        tools.require("ogr2ogr"), *GDAL_CONFIG, "-f", "CSV", "/vsistdout/", str(path),
        "-sql", f'SELECT FID AS "__row", * FROM {q(sheet)}',
    ]
    if limit:
        cmd += ["-limit", str(limit)]
    proc = tools.run_capture(cmd, timeout=READ_TIMEOUT_S)
    if proc.returncode != 0:
        raise XlsxError(f"Không đọc được sheet '{sheet}': {tools.stderr_detail(proc.stderr) or proc.stdout.strip()}")
    records = csv.reader(io.StringIO(proc.stdout))
    next(records, None)  # __row, Field1, Field2…
    rows = []
    for rec in records:
        if not rec:
            continue
        cells = [c.strip() for c in rec[1:]]
        while cells and not cells[-1]:
            cells.pop()
        rows.append((int(rec[0]), cells))
    return rows


# ------------------------------------------------------------------ header & columns

def detect_header_row(rows: list[Row]) -> int:
    """Guess the header row: the first row of short labels as wide as the rows below it.

    Title and note rows above a table fill one cell, so they are skipped. When a row of
    human labels is followed by a row of machine names (way_id, buffer_m…), the machine names
    win: they make better attribute names and the row is not data either.
    """
    for i, (number, cells) in enumerate(rows):
        below = rows[i + 1:i + 1 + HEADER_LOOKAHEAD]
        if not below or not _header_like(cells, below):
            continue
        nxt = rows[i + 1]
        if (nxt[0] == number + 1 and _identifiers(nxt[1]) and not _identifiers(cells)
                and _header_like(nxt[1], rows[i + 2:i + 2 + HEADER_LOOKAHEAD] or below)):
            return nxt[0]
        return number
    return next((number for number, cells in rows if any(cells)), 1)


def _header_like(cells: list[str], below: list[Row]) -> bool:
    filled = [c for c in cells if c]
    width = max((sum(1 for c in r if c) for _, r in below), default=0)
    return (
        len(filled) >= 2
        and len(filled) >= 0.6 * width
        and not any(is_number(c) or len(c) > HEADER_MAX_CHARS for c in filled)
    )


def _identifiers(cells: list[str]) -> bool:
    filled = [c for c in cells if c]
    return bool(filled) and all(_IDENT_RE.fullmatch(c) for c in filled)


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def build_columns(header: list[str], data: list[Row]) -> list[Column]:
    """Name and type every column that has a header or a value; empty columns are dropped."""
    width = max([len(header), *(len(cells) for _, cells in data)], default=0)
    columns: list[Column] = []
    used: set[str] = set()
    for i in range(width):
        raw = " ".join((header[i] if i < len(header) else "").split())
        values = [cells[i] for _, cells in data if i < len(cells) and cells[i]]
        if not raw and not values:
            continue
        name = (raw or f"col_{column_letter(i)}")[:MAX_NAME_LENGTH]
        if name.lower() in RESERVED:
            name = f"{name}_xlsx"
        base, n = name, 2
        while name.lower() in used:
            name, n = f"{base}_{n}", n + 1
        used.add(name.lower())
        columns.append(Column(i, name, infer_type(values)))
    return columns


def is_number(text: str) -> bool:
    return bool(_NUMBER_RE.fullmatch(text)) and math.isfinite(float(text))


def infer_type(values: list[str]) -> str:
    """Integer / Real when every value is a number; codes with leading zeros stay text."""
    if not values or any(_LEADING_ZERO_RE.match(v) or not is_number(v) for v in values):
        return "String"
    return "Integer" if all(_INT_RE.fullmatch(v) for v in values) else "Real"


def split_rows(rows: list[Row], header_row: int) -> tuple[list[str], list[Row]]:
    """Header cells and the non-blank data rows below them (header_row 0 = no header)."""
    header = next((cells for number, cells in rows if number == header_row), [])
    data = [(number, cells) for number, cells in rows if number > header_row and any(cells)]
    return header, data


# ------------------------------------------------------------------ guesses

def parse_way_id(text: str) -> int | None:
    """OSM way id from `123`, `w123`, `way/123`, `123.0` or an openstreetmap.org/way/123 link."""
    match = _WAY_RE.fullmatch(text.strip())
    return int(match.group(1)) if match else None


def parse_buffer(text: str) -> float | None:
    """A buffer distance in metres, or None when the cell is not a usable number."""
    text = text.strip().replace(",", ".")
    if not is_number(text):
        return None
    value = float(text)
    return value if 0 < value <= MAX_BUFFER_M else None


def guess_columns(columns: list[Column], data: list[Row]) -> dict[str, Any]:
    """Suggest the geometry source: a WKT/GeoJSON column, else an OSM way id column (+ buffer)."""

    def sample(col: Column) -> list[str]:
        return [cells[col.index] for _, cells in data[:SAMPLE_ROWS] if col.index < len(cells) and cells[col.index]]

    def mostly(col: Column, test) -> bool:
        values = sample(col)
        return bool(values) and sum(1 for v in values if test(v)) >= GUESS_SHARE * len(values)

    def looks_geojson(v: str) -> bool:
        return v.lstrip().startswith("{") and '"coordinates"' in v

    wkt = next((c.name for c in columns if mostly(c, lambda v: bool(_WKT_RE.match(v)) or looks_geojson(v))), None)

    # A way id column is named like one ("way_id", "Way ID", "osm_id"); a count such as
    # "so_way_final" is numeric too but its name has no id.
    osm = next((c.name for c in columns if _ID_NAME_RE.search(c.name)
                and mostly(c, lambda v: parse_way_id(v) is not None)), None)

    # Only distances per side: a "width" column is the full road width, twice the buffer.
    buffer = None
    for hint in BUFFER_HINTS:
        buffer = next((c.name for c in columns if hint in c.name.lower()
                       and not any(w in c.name.lower() for w in WIDTH_HINTS)
                       and mostly(c, lambda v: parse_buffer(v) is not None)), None)
        if buffer:
            break
    return {"mode": "wkt" if wkt else "osm" if osm else None, "wkt_col": wkt, "osm_col": osm, "buffer_col": buffer}


# ------------------------------------------------------------------ inspect

def inspect(path: Path, sheet: str | None = None, header_row: int | None = None) -> dict[str, Any]:
    """Describe a workbook for the UI: sheets, header row, column preview and suggestions.

    Without `sheet`, the first sheet with a geometry suggestion is chosen.
    """
    sheets = list_sheets(path)
    if not sheets:
        raise XlsxError("File Excel không có sheet nào.")
    names = [s["name"] for s in sheets]
    if sheet is not None and sheet not in names:
        raise XlsxError(f"Không có sheet '{sheet}' trong file.")

    def describe(name: str) -> dict[str, Any]:
        rows = read_rows(path, name, limit=SAMPLE_ROWS + (header_row or 0))
        detected = detect_header_row(rows) if rows else 1
        chosen = detected if header_row is None else header_row
        header, data = split_rows(rows, chosen)
        columns = build_columns(header, data)
        return {"rows": rows, "header_row": chosen, "header_detected": detected,
                "columns": columns, "suggest": guess_columns(columns, data)}

    if sheet is None:
        found = None
        for name in names:
            info = describe(name)
            if info["suggest"]["mode"]:
                found = (name, info)
                break
        sheet, info = found or (names[0], describe(names[0]))
    else:
        info = describe(sheet)

    rows, chosen = info["rows"], info["header_row"]
    lo = max(1, chosen - PREVIEW_BEFORE)
    preview = [
        [number, [c[:PREVIEW_CELL_CHARS] for c in cells]]
        for number, cells in rows if lo <= number <= chosen + PREVIEW_AFTER
    ]
    count = next(s["rows"] for s in sheets if s["name"] == sheet)
    return {
        "kind": "sheet",
        "path": str(path),
        "name": path.name,
        "size": path.stat().st_size,
        "driver": "XLSX",
        "sheets": sheets,
        "sheet": sheet,
        "header_row": chosen,
        "header_detected": info["header_detected"],
        "data_rows": max(0, (count or 0) - chosen),
        "preview": preview,
        "columns": [{"name": c.name, "letter": column_letter(c.index), "type": c.type} for c in info["columns"]],
        "suggest": info["suggest"],
        "defaults": {"crs": WGS84, "buffer_m": DEFAULT_BUFFER_M},
        "warnings": [],
    }


# ------------------------------------------------------------------ stage

def parse_geom_spec(raw: dict[str, Any]) -> GeomSpec:
    """Validate the geometry settings sent by the UI."""
    mode = raw.get("mode")
    if mode not in ("wkt", "osm"):
        raise XlsxError("Chọn nguồn hình học: cột WKT / GeoJSON hoặc cột way ID OSM.")
    column = str(raw.get("column") or "").strip()
    if not column:
        raise XlsxError("Chọn cột chứa hình học." if mode == "wkt" else "Chọn cột chứa way ID.")
    buffer_m = raw.get("buffer_m", DEFAULT_BUFFER_M)
    try:
        buffer_m = float(DEFAULT_BUFFER_M if buffer_m in (None, "") else buffer_m)
    except (TypeError, ValueError) as exc:
        raise XlsxError("Buffer mặc định phải là số (m).") from exc
    if not 0 < buffer_m <= MAX_BUFFER_M:
        raise XlsxError(f"Buffer mặc định phải trong khoảng 0–{MAX_BUFFER_M:g} m.")
    return GeomSpec(
        mode=mode,
        column=column,
        crs=str(raw.get("crs") or WGS84).strip(),
        buffer_col=(str(raw["buffer_col"]).strip() or None) if raw.get("buffer_col") else None,
        buffer_m=buffer_m,
    )


def stage(path: Path, sheet: str, header_row: int, spec: GeomSpec, out_path: Path) -> dict[str, Any]:
    """Write the sheet as a GeoPackage layer named after the sheet.

    Rows without a usable geometry are left out and reported in `warnings`.
    """
    header, data = split_rows(read_rows(path, sheet), header_row)
    if not data:
        raise XlsxError(f"Sheet '{sheet}' không có dòng dữ liệu nào dưới dòng header {header_row}.")
    columns = build_columns(header, data)
    by_name = {c.name: c for c in columns}

    def column(name: str | None, label: str) -> Column | None:
        if name is None:
            return None
        if name not in by_name:
            raise XlsxError(f"Không có cột '{name}' ({label}) trong sheet.")
        return by_name[name]

    geom_col = column(spec.column, "hình học")
    buffer_col = column(spec.buffer_col, "buffer") if spec.mode == "osm" else None
    assert geom_col is not None

    def cell(cells: list[str], col: Column | None) -> str:
        return cells[col.index] if col is not None and col.index < len(cells) else ""

    warnings: list[str] = []
    stats: dict[str, Any] = {"rows": len(data)}
    geoms: list[str | None] = []
    extra: dict[str, list[Any]] = {}
    epsg: int | None = None
    srs = spec.crs
    if spec.mode == "osm":
        ids = [parse_way_id(cell(cells, geom_col)) for _, cells in data]
        unparsed = [(n, cell(cells, geom_col)) for (n, cells), i in zip(data, ids) if i is None]
        unique = sorted({i for i in ids if i is not None})
        if not unique:
            raise XlsxError(f"Cột '{geom_col.name}' không có way ID hợp lệ nào (vd 123456, w123456, way/123456).")
        ways = fetch_ways(unique)
        missing = [i for i in unique if i not in ways]
        coords = [p for pts in ways.values() for p in pts]
        if not coords:
            raise XlsxError("Overpass không trả về way nào trong danh sách (way đã bị xoá hoặc ID sai?).")
        epsg = utm_epsg(sum(p[0] for p in coords) / len(coords), sum(p[1] for p in coords) / len(coords))
        defaulted = 0
        buffers = []
        for (_, cells), way_id in zip(data, ids):
            geoms.append(linestring_wkt(ways[way_id]) if way_id in ways else None)
            value = parse_buffer(cell(cells, buffer_col)) if buffer_col else None
            if value is None and way_id in ways:
                defaulted += 1
            buffers.append(value if value is not None else spec.buffer_m)
        extra = {WAY_COL: ids, BUFFER_COL: buffers}
        srs = WGS84
        stats |= {"way_ids": len(unique), "ways_found": len(ways), "missing_ways": missing,
                  "buffer_default": defaulted, "utm": f"EPSG:{epsg}"}
        if unparsed:
            shown = ", ".join(f"dòng {n}: {v or '(trống)'}" for n, v in unparsed[:MAX_LISTED_IDS])
            warnings.append(f"{len(unparsed):,} dòng không có way ID hợp lệ, đã bỏ qua ({shown}"
                            f"{'…' if len(unparsed) > MAX_LISTED_IDS else ''}).")
        if missing:
            warnings.append(f"{len(missing):,} way không còn trên OpenStreetMap, đã bỏ qua: "
                            f"{', '.join(map(str, missing[:MAX_LISTED_IDS]))}{'…' if len(missing) > MAX_LISTED_IDS else ''}.")
        if buffer_col is None:
            warnings.append(f"Không chọn cột buffer: mọi way buffer {spec.buffer_m:g} m mỗi bên.")
        elif defaulted:
            warnings.append(f"{defaulted:,} dòng không có số ở cột '{buffer_col.name}', dùng buffer mặc định "
                            f"{spec.buffer_m:g} m.")
    else:
        geoms = [cell(cells, geom_col) or None for _, cells in data]

    features, invalid = _write_gpkg(out_path, sheet, columns, data, geoms, extra, srs=srs, utm=epsg)
    skipped = len(data) - features
    stats |= {"features": features, "invalid": invalid}
    if spec.mode == "wkt":
        empty = sum(1 for g in geoms if g is None)
        if empty:
            warnings.append(f"{empty:,} dòng trống ở cột '{geom_col.name}', đã bỏ qua.")
        if invalid:
            warnings.append(f"{invalid:,} dòng có WKT / GeoJSON không đọc được, đã bỏ qua.")
    if not features:
        raise XlsxError("Không dựng được geometry nào từ sheet. " + " ".join(warnings))
    log.info("Staged %s [%s] -> %s: %d/%d rows", path, sheet, out_path, features, len(data))
    return {"path": str(out_path), "layer": sheet, "features": features, "skipped": skipped,
            "stats": stats, "warnings": warnings}


def _write_gpkg(out_path: Path, layer: str, columns: list[Column], data: list[Row],
                geoms: list[str | None], extra: dict[str, list[Any]], srs: str, utm: int | None) -> tuple[int, int]:
    """Load the rows into DuckDB through a CSV, build the geometry and COPY to a GeoPackage.

    `extra` holds the osm columns (way id, buffer), empty for WKT. `utm` set means: buffer
    each line by its buffer_m in that metric CRS. Returns (features written, rows whose geometry
    text could not be parsed).
    """
    import duckdb

    from .osmbase import BaseError, load_extension

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)
    tmp_csv = out_path.with_suffix(".rows.csv")
    with tmp_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["r", *(f"c{c.index}" for c in columns), *(f"x{i}" for i in range(len(extra))), "g"])
        for k, ((number, cells), geom) in enumerate(zip(data, geoms)):
            values = [cells[c.index] if c.index < len(cells) else "" for c in columns]
            extras = ["" if col[k] is None else str(col[k]) for col in extra.values()]
            writer.writerow([number, *values, *extras, geom or ""])

    casts = {"Integer": "BIGINT", "Real": "DOUBLE"}
    select = [f"CAST(r AS INTEGER) AS {q(ROW_COL)}"]
    for c in columns:
        src = f"NULLIF(c{c.index}, '')"
        select.append(f"TRY_CAST({src} AS {casts[c.type]}) AS {q(c.name)}" if c.type in casts else f"{src} AS {q(c.name)}")
    for i, name in enumerate(extra):
        select.append(f"TRY_CAST(NULLIF(x{i}, '') AS {'BIGINT' if name == WAY_COL else 'DOUBLE'}) AS {q(name)}")
    if utm is not None:
        to_utm = f"ST_Transform(ST_GeomFromText(g), 'EPSG:4326', 'EPSG:{utm}', always_xy := true)"
        geom = (f"ST_Transform(ST_Buffer({to_utm}, CAST(NULLIF(x{list(extra).index(BUFFER_COL)}, '') AS DOUBLE)), "
                f"'EPSG:{utm}', 'EPSG:4326', always_xy := true)")
    else:
        geom = ("CASE WHEN starts_with(ltrim(g), '{') THEN TRY(ST_GeomFromGeoJSON(g)) "
                "ELSE TRY(ST_GeomFromText(g)) END")
    select += ["NULLIF(g, '') AS __text", f"CASE WHEN NULLIF(g, '') IS NULL THEN NULL ELSE {geom} END AS geom"]

    con = duckdb.connect()
    try:
        try:
            load_extension(con, "spatial")
        except BaseError as exc:
            raise XlsxError(str(exc)) from exc
        con.execute(f"CREATE TABLE staged AS SELECT {', '.join(select)} "
                    f"FROM read_csv({sql_str(str(tmp_csv))}, header = true, all_varchar = true)")
        features, invalid = con.execute(
            "SELECT COUNT(geom), COUNT(*) FILTER (WHERE geom IS NULL AND __text IS NOT NULL) FROM staged"
        ).fetchone()
        if features:
            con.execute(
                f"COPY (SELECT * EXCLUDE (__text) FROM staged WHERE geom IS NOT NULL ORDER BY {q(ROW_COL)}) "
                f"TO {sql_str(str(out_path))} "
                f"WITH (FORMAT GDAL, DRIVER 'GPKG', LAYER_NAME {sql_str(layer)}, SRS {sql_str(srs)})"
            )
    except duckdb.Error as exc:
        out_path.unlink(missing_ok=True)
        raise XlsxError(f"Không dựng được GeoPackage: {exc}") from exc
    finally:
        con.close()
        tmp_csv.unlink(missing_ok=True)
    return features, invalid


# ------------------------------------------------------------------ OpenStreetMap

def overpass_url() -> str:
    return os.environ.get("BDS_OVERPASS_URL") or OVERPASS_URL


def fetch_ways(ids: list[int]) -> dict[int, list[tuple[float, float]]]:
    """Geometry (lon, lat points) of OSM ways from the Overpass API, in batches.

    Ways that no longer exist are simply absent from the result.
    """
    url = overpass_url()
    ways: dict[int, list[tuple[float, float]]] = {}
    for start in range(0, len(ids), OVERPASS_BATCH):
        batch = ids[start:start + OVERPASS_BATCH]
        query = f"[out:json][timeout:{OVERPASS_TIMEOUT_S}];way(id:{','.join(map(str, batch))});out geom;"
        req = urllib.request.Request(
            url, data=urllib.parse.urlencode({"data": query}).encode(),
            headers={"User-Agent": f"building-data-studio/{__version__}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=OVERPASS_TIMEOUT_S + 30, context=net.ssl_context()) as res:  # noqa: S310 — https Overpass endpoint
                data = json.load(res)
        except urllib.error.HTTPError as exc:
            busy = " — máy chủ đang bận, thử lại sau ít phút" if exc.code in (429, 503, 504) else ""
            raise XlsxError(f"Overpass trả lỗi HTTP {exc.code}{busy}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            reason = getattr(exc, "reason", exc)
            raise XlsxError(f"Không tải được hình học từ Overpass ({url}): {reason}. "
                            "Chế độ way ID OSM cần internet.") from exc
        for el in data.get("elements", []):
            points = [(p["lon"], p["lat"]) for p in el.get("geometry") or [] if p]
            if el.get("type") == "way" and len(points) >= 2:
                ways[int(el["id"])] = points
    return ways


def linestring_wkt(points: list[tuple[float, float]]) -> str:
    return "LINESTRING (" + ", ".join(f"{x!r} {y!r}" for x, y in points) + ")"


def utm_epsg(lon: float, lat: float) -> int:
    """WGS 84 / UTM zone covering a point (EPSG:326xx north, 327xx south)."""
    zone = min(60, max(1, int((lon + 180) // 6) + 1))
    return (32600 if lat >= 0 else 32700) + zone
