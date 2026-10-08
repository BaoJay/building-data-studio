"""Release diff job: compare A (production) with B (release candidate).

    A, B (.parquet / .gpkg / .fgb / .geojson / .pmtiles)
      └─ ogr2ogr ──► FlatGeobuf (EPSG:4326; PMTiles: tile pieces in EPSG:3857, attributes as JSON)
           └─ ogr2ogr + SpatiaLite SQL ──► Parquet + area / centroid / bbox / validity / WKB hash
                └─ DuckDB (diffcore) ──► match, changes, quality, grid ──► gate (diffgate)
                     ├─ GeoPackage: added / removed / changed / changed_before / grid (for QGIS)
                     ├─ PMTiles + GeoJSON grid (map in the app)
                     └─ diff_report.md / diff_report.json
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import duckdb

from . import __version__, probe, tools
from .diffcore import MERCATOR_HALF_WORLD, MVT_EXTENT, CompareSpec, DiffEngine, DiffError, SideSpec
from .diffgate import DEFAULT_IGNORE_COLS, GateError, Thresholds, compare_schema, evaluate, parse_thresholds, render_markdown
from .jobs import FileRegistry, Job, Step
from .pipeline import DEFAULT_OUTPUT_DIR, WGS84, ConfigError, StagedPipeline, _oo_args, build_height_rule, safe_name
from .probe import ProbeError
from .probe import quote_ident as q
from .sqlbuild import sql_str

log = logging.getLogger(__name__)

SIDES = ("a", "b")
SIDE_LABEL = {"a": "A (production)", "b": "B (release)"}
MODES = ("auto", "key", "location")
SUPERSEDED_NAMES = ("superseded_by", "superseded_by_id", "replaced_by")
DEFAULT_GRID_DEG = 0.05
MIN_GRID_DEG, MAX_GRID_DEG = 0.001, 2.0
NAME_TAIL = 40
MAP_LAYER = "diff"
MAP_MINZOOM, MAP_MAXZOOM = 10, 15
GPKG_LAYERS = (
    ("added", "Thêm mới (geometry B)"),
    ("removed", "Bị xoá (geometry A)"),
    ("changed", "Có thay đổi (geometry B)"),
    ("changed_before", "Có thay đổi (geometry A, để đối chiếu)"),
)
HEIGHT_PARAMS = ("m_per_level", "default_m", "min_valid_m", "max_valid_m", "max_levels", "outlier_mode", "decimals")
METRIC_SQL = (
    'ST_Area({g}) AS "__area", ST_X(ST_Centroid({g})) AS "__cx", ST_Y(ST_Centroid({g})) AS "__cy", '
    'ST_MinX({g}) AS "__x0", ST_MaxX({g}) AS "__x1", ST_MinY({g}) AS "__y0", ST_MaxY({g}) AS "__y1", '
    'ST_GeometryType({g}) AS "__gtype", ST_IsValid({g}) AS "__valid", ST_IsEmpty({g}) AS "__empty", '
    'MD5Checksum(ST_AsBinary({g})) AS "__gh"'
)


@dataclass(frozen=True)
class InputSpec:
    """One side as chosen in the UI."""

    path: Path
    layer: str | None
    geom_column: str | None
    src_crs: str | None


@dataclass(frozen=True)
class DiffConfig:
    """Validated diff job configuration."""

    a: InputSpec
    b: InputSpec
    mode: str
    key_a: str | None
    key_b: str | None
    columns: dict[str, dict[str, Any]]  # per side overrides of the suggested column mapping
    height: dict[str, Any]
    ignore_cols: tuple[str, ...] | None  # None = DEFAULT_IGNORE_COLS
    required_fields: tuple[str, ...] | None  # None = every field of A; () = none
    thresholds: Thresholds
    grid_deg: float
    out_dir: Path
    out_name: str
    want_tiles: bool
    keep_temp: bool


def default_name(a: Path, b: Path) -> str:
    """'diff__<B>__vs__<A>' using the distinctive tail of each file name."""
    return safe_name(f"diff__{b.stem[-NAME_TAIL:]}__vs__{a.stem[-NAME_TAIL:]}")


def parse_config(raw: dict[str, Any]) -> DiffConfig:
    """Validate the JSON sent by the UI.

    Raises:
        ConfigError: With a user-facing message for the first problem found.
    """
    inputs = {}
    for s in SIDES:
        side = raw.get(s)
        if not isinstance(side, dict):
            raise ConfigError(f"Thiếu file {SIDE_LABEL[s]}.")
        try:
            path = probe.resolve_input_path(str(side.get("path", "")))
        except ProbeError as exc:
            raise ConfigError(f"{SIDE_LABEL[s]}: {exc}") from exc
        inputs[s] = InputSpec(
            path=path,
            layer=side.get("layer") or None,
            geom_column=side.get("geom_column") or None,
            src_crs=(str(side.get("src_crs")).strip() or None) if side.get("src_crs") else None,
        )
    if inputs["a"].path == inputs["b"].path:
        raise ConfigError("A và B đang là cùng một file.")

    match = raw.get("match") or {}
    mode = str(match.get("mode") or "auto")
    if mode not in MODES:
        raise ConfigError(f"Chế độ khớp không hợp lệ: {mode}")
    key_a = match.get("key_a") or match.get("key") or None
    key_b = match.get("key_b") or match.get("key") or None
    if mode == "key" and not (key_a and key_b):
        raise ConfigError("Khớp theo ID: chọn cột ID.")

    try:
        thresholds = parse_thresholds(raw.get("thresholds"))
    except GateError as exc:
        raise ConfigError(str(exc)) from exc

    grid = raw.get("grid_deg", DEFAULT_GRID_DEG)
    try:
        grid_deg = float(grid)
    except (TypeError, ValueError) as exc:
        raise ConfigError("Kích thước ô lưới phải là số.") from exc
    if not MIN_GRID_DEG <= grid_deg <= MAX_GRID_DEG:
        raise ConfigError(f"Kích thước ô lưới phải trong khoảng {MIN_GRID_DEG}–{MAX_GRID_DEG} độ.")

    out = raw.get("output") or {}
    out_dir = Path(str(out.get("dir") or DEFAULT_OUTPUT_DIR)).expanduser()
    if out_dir.exists() and not out_dir.is_dir():
        raise ConfigError(f"Thư mục output không hợp lệ: {out_dir}")
    ignore = raw.get("ignore_cols")
    required = raw.get("required_fields")
    columns = raw.get("columns") or {}
    return DiffConfig(
        a=inputs["a"],
        b=inputs["b"],
        mode=mode,
        key_a=str(key_a) if key_a else None,
        key_b=str(key_b) if key_b else None,
        columns={s: dict(columns.get(s) or {}) for s in SIDES},
        height=dict(raw.get("height") or {}),
        ignore_cols=None if ignore is None else tuple(str(c) for c in ignore),
        required_fields=None if required is None else tuple(str(c) for c in required),
        thresholds=thresholds,
        grid_deg=grid_deg,
        out_dir=out_dir,
        out_name=safe_name(str(out.get("name") or default_name(inputs["a"].path, inputs["b"].path))),
        want_tiles=bool(out.get("pmtiles", True)),
        keep_temp=bool(out.get("keep_temp", False)),
    )


def build_extract_sql(layer: str, attributes: list[str], tile_zoom: int | None = None,
                      geom: str = "geometry") -> str:
    """SELECT run by ogr2ogr (GDAL SQLite dialect + SpatiaLite) to add geometry metrics.

    For PMTiles pieces (tile_zoom set, geometry in EPSG:3857, clipped at tile
    edges) it also records, for each edge of the piece's tile, the extent of
    the piece along that edge (`__e0/__e1` for east, `__w*`, `__n*`, `__s*`).
    Two pieces of one building cut by an edge share that extent; neighbours
    that merely touch at the edge do not — that is what stitching relies on.
    """
    cols = [q(a) for a in attributes]
    parts = [*cols, METRIC_SQL.format(g=geom)]
    if tile_zoom is not None:
        parts += edge_extent_sql(tile_zoom, geom)
    parts.append(geom)
    return f"SELECT {', '.join(parts)} FROM {q(layer)}"


def edge_extent_sql(zoom: int, g: str = "geometry") -> list[str]:
    """SpatiaLite expressions: extent of a clipped piece along each edge of its tile (NULL if not touching)."""
    tile = 2 * MERCATOR_HALF_WORLD / 2 ** zoom
    eps = tile / MVT_EXTENT / 2
    origin = -MERCATOR_HALF_WORLD
    cx, cy = f"((MbrMinX({g}) + MbrMaxX({g})) / 2)", f"((MbrMinY({g}) + MbrMaxY({g})) / 2)"
    west = f"({origin!r} + Floor(({cx} - {origin!r}) / {tile!r}) * {tile!r})"
    south = f"({origin!r} + Floor(({cy} - {origin!r}) / {tile!r}) * {tile!r})"
    east, north = f"({west} + {tile!r})", f"({south} + {tile!r})"

    def strip_x(x: str) -> str:
        return f"ST_Intersection({g}, BuildMbr({x} - {eps!r}, MbrMinY({g}) - 1, {x} + {eps!r}, MbrMaxY({g}) + 1, ST_SRID({g})))"

    def strip_y(y: str) -> str:
        return f"ST_Intersection({g}, BuildMbr(MbrMinX({g}) - 1, {y} - {eps!r}, MbrMaxX({g}) + 1, {y} + {eps!r}, ST_SRID({g})))"

    out = []
    for name, touches, strip, lo, hi in (
        ("e", f"Abs(MbrMaxX({g}) - {east}) < {eps!r}", strip_x(east), "MbrMinY", "MbrMaxY"),
        ("w", f"Abs(MbrMinX({g}) - {west}) < {eps!r}", strip_x(west), "MbrMinY", "MbrMaxY"),
        ("n", f"Abs(MbrMaxY({g}) - {north}) < {eps!r}", strip_y(north), "MbrMinX", "MbrMaxX"),
        ("s", f"Abs(MbrMinY({g}) - {south}) < {eps!r}", strip_y(south), "MbrMinX", "MbrMaxX"),
    ):
        out.append(f'CAST(CASE WHEN {touches} THEN {lo}({strip}) END AS REAL) AS "__{name}0"')
        out.append(f'CAST(CASE WHEN {touches} THEN {hi}({strip}) END AS REAL) AS "__{name}1"')
    return out


def grid_geojson(cells: list[dict[str, Any]], cell_deg: float) -> dict[str, Any]:
    """FeatureCollection of grid squares with their counts (EPSG:4326)."""
    features = []
    for c in cells:
        x0, y0 = c["gx"] * cell_deg, c["gy"] * cell_deg
        x1, y1 = x0 + cell_deg, y0 + cell_deg
        props = {k: c[k] for k in ("n_a", "n_b", "added", "removed", "changed")}
        props["total"] = props["added"] + props["removed"] + props["changed"]
        props["delta"] = props["n_b"] - props["n_a"]
        features.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]},
        })
    return {"type": "FeatureCollection", "features": features}


class DiffPipeline(StagedPipeline):
    """Runs one diff job."""

    handled_errors = (*StagedPipeline.handled_errors, duckdb.Error, DiffError, GateError)

    def __init__(self, job: Job, registry: FileRegistry, cfg: DiffConfig) -> None:
        super().__init__(job, registry, cfg.out_dir / f"{cfg.out_name}__{time.strftime('%Y%m%d-%H%M%S')}")
        self.cfg = cfg
        self.info: dict[str, dict[str, Any]] = {}
        self.specs: dict[str, SideSpec] = {}
        self.mode = cfg.mode
        self.key_a: str | None = None
        self.key_b: str | None = None
        self.con: duckdb.DuckDBPyConnection | None = None
        self.engine: DiffEngine | None = None
        self.result: dict[str, Any] = {}
        self.written: dict[str, dict[str, Any]] = {}
        self.gpkg_out = self.out_dir / f"{cfg.out_name}.gpkg"
        self.pmtiles_out = self.out_dir / f"{cfg.out_name}.pmtiles"
        self.grid_out = self.out_dir / "diff_grid.geojson"

    def intro(self) -> list[str]:
        return [
            f"Building Data Studio {__version__} — so sánh trước release",
            f"A (production): {self.cfg.a.path}",
            f"B (release):    {self.cfg.b.path}",
            f"Thư mục output: {self.out_dir}",
        ]

    def plan(self) -> list[Step]:
        steps = [
            Step("probe", "Đọc thông tin 2 file"),
            Step("extract_a", f"Đọc A ({self.cfg.a.path.name})"),
            Step("extract_b", f"Đọc B ({self.cfg.b.path.name})"),
            Step("compare", "So sánh & release gate"),
            Step("outputs", "Xuất GeoPackage các thay đổi"),
        ]
        if self.cfg.want_tiles:
            steps.append(Step("tiles", "Tạo PMTiles cho bản đồ diff"))
        steps.append(Step("finalize", "Ghi báo cáo & dọn file tạm"))
        return steps

    def cleanup(self) -> None:
        if self.con is not None:
            self.con.close()
            self.con = None

    # ---------------------------------------------------------------- stages
    def _step_probe(self, step: Step) -> None:
        cfg = self.cfg
        for s in SIDES:
            inp: InputSpec = getattr(cfg, s)
            info = probe.probe(inp.path, geom_column=inp.geom_column, layer=inp.layer)
            if info["kind"] == "vector" and info["crs"]["missing"] and not inp.src_crs:
                raise ConfigError(f"{SIDE_LABEL[s]} không khai báo CRS — hãy nhập CRS nguồn (vd EPSG:4326).")
            if info["kind"] == "pmtiles" and not info.get("layer"):
                raise ConfigError(f"{SIDE_LABEL[s]}: không đọc được layer của PMTiles (cần pmtiles CLI).")
            self.info[s] = info
            count = info.get("feature_count")
            self.job.add_log(
                f"{SIDE_LABEL[s]}: {info['name']} — {info['kind']}, layer '{info['layer']}', "
                f"{len(info['fields'])} field, {f'{count:,}' if count is not None else '?'} feature"
            )
            for warning in info.get("warnings", []):
                self.job.add_log(f"Lưu ý ({s.upper()}): {warning}")

        if self.mode == "auto":
            ida, idb = self.info["a"]["suggest"]["id_col"], self.info["b"]["suggest"]["id_col"]
            self.mode = "key" if ida and ida == idb else "location"
            self.key_a = self.key_b = ida if self.mode == "key" else None
        else:
            self.key_a, self.key_b = cfg.key_a, cfg.key_b
        self.job.add_log("Khớp theo " + (f"ID: A.{self.key_a} ↔ B.{self.key_b}" if self.mode == "key"
                                         else "vị trí (không dùng ID)"))

    def _side_spec(self, s: str, parquet: Path) -> SideSpec:
        info = self.info[s]
        fields = {f["name"]: f for f in info["fields"]}
        suggest = info["suggest"]
        override = self.cfg.columns.get(s, {})

        def col(key: str, default: str | None) -> str | None:
            name = override.get(key, default) or None
            if name is not None and name not in fields:
                raise ConfigError(f"{SIDE_LABEL[s]}: không có cột '{name}'.")
            return name

        key = self.key_a if s == "a" else self.key_b
        id_col = col("id_col", key if self.mode == "key" else suggest["id_col"])
        if self.mode == "key" and id_col is None:
            raise ConfigError(f"{SIDE_LABEL[s]}: không có cột ID '{key}'.")
        height_col = col("height_col", suggest["height_col"])
        levels_col = col("levels_col", suggest["levels_col"])
        prov_col = col("prov_col", suggest["prov_col"])
        prov_missing = override.get("prov_missing", suggest["prov_missing"])
        if isinstance(prov_missing, str):
            prov_missing = [v.strip() for v in prov_missing.split(",")]
        superseded = col("superseded_col", next((n for n in SUPERSEDED_NAMES if n in fields), None))
        categories = override.get("category_cols")
        if categories is None:
            categories = [c for c in suggest["category_cols"] if c != prov_col]
        rule = build_height_rule(
            {**{k: self.cfg.height[k] for k in HEIGHT_PARAMS if k in self.cfg.height},
             "height_col": height_col, "levels_col": levels_col, "prov_col": prov_col, "prov_missing": prov_missing},
            fields,
        )
        return SideSpec(
            name=s,
            parquet=parquet,
            kind=info["kind"],
            fields=tuple((f["name"], "Boolean" if f.get("subtype") == "Boolean" else f["type"]) for f in info["fields"]),
            id_col=id_col,
            rule=rule,
            prov_col=prov_col,
            prov_missing=tuple(v for v in prov_missing if v),
            parent_col=col("parent_col", suggest["parent_col"]),
            superseded_col=superseded,
            category_cols=tuple(c for c in categories if c in fields),
            zoom=(info.get("header") or {}).get("maxzoom") if info["kind"] == "pmtiles" else None,
        )

    def _step_extract_a(self, step: Step) -> None:
        self._extract("a", step)

    def _step_extract_b(self, step: Step) -> None:
        self._extract("b", step)

    def _extract(self, s: str, step: Step) -> None:
        info, inp = self.info[s], getattr(self.cfg, s)
        raw = self.work / f"{s}_src.fgb"
        out = self.work / f"{s}.parquet"
        raw.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
        ogr2ogr = tools.require("ogr2ogr")
        # FlatGeobuf has no list types: lists (GeoJSON arrays, Parquet list columns) are kept as JSON text.
        common = ["-nln", "src", "-nlt", "GEOMETRY", "-lco", "SPATIAL_INDEX=NO", "-progress",
                  "-mapFieldType", "StringList=String,IntegerList=String,Integer64List=String,RealList=String"]
        if info["kind"] == "pmtiles":
            zoom = (info.get("header") or {}).get("maxzoom")
            # JSON_FIELD keeps one JSON column instead of one column per attribute: ~15× faster
            # on wide OSM-tag schemas. CLIP (default) cuts pieces exactly at tile edges for stitching.
            cmd = [ogr2ogr, "-f", "FlatGeobuf", str(raw), str(inp.path), info["layer"],
                   "-oo", "JSON_FIELD=YES", "-oo", f"ZOOM_LEVEL={zoom}", *common]
            attrs = ["json"]
            tile_zoom = zoom
            to_wgs84 = ["-t_srs", WGS84]
            self.job.add_log(f"Đọc tile z{zoom} của {s.upper()} (mảnh building cắt theo biên tile sẽ được ghép lại).")
        else:
            cmd = [ogr2ogr, "-f", "FlatGeobuf", str(raw), str(inp.path), info["layer"],
                   *_oo_args(info["open_options"]), *common, "-t_srs", WGS84]
            if info["crs"]["missing"]:
                cmd += ["-s_srs", str(inp.src_crs)]
            attrs = [f["name"] for f in info["fields"]]
            tile_zoom = None
            to_wgs84 = []
        self._run(cmd, step)
        step.progress = 50.0
        sql = build_extract_sql("src", attrs, tile_zoom)
        self._run([ogr2ogr, "-f", "Parquet", str(out), str(raw), "-dialect", "SQLite", "-sql", sql,
                   "-nln", s, *to_wgs84], step)
        if not self.cfg.keep_temp:
            raw.unlink(missing_ok=True)
        self.specs[s] = self._side_spec(s, out)

    def _step_compare(self, step: Step) -> None:
        cfg = self.cfg
        a, b = self.specs["a"], self.specs["b"]
        common = [c for c in a.field_names if c in set(b.field_names)]
        if cfg.ignore_cols is not None:
            ignore = cfg.ignore_cols
        elif self.mode == "key":
            ignore = tuple(c for c in DEFAULT_IGNORE_COLS if c in common)
        else:
            # Without a shared ID, A and B usually come from different pipelines: same-named
            # columns rarely mean the same thing, so they are reported but do not flag changes.
            ignore = tuple(common)
        spec = CompareSpec(a=a, b=b, mode=self.mode, ignore_cols=ignore, height_tol_m=cfg.thresholds.height_tol_m,
                           geom_area_pct=cfg.thresholds.geom_area_pct, geom_shift_m=cfg.thresholds.geom_shift_m,
                           grid_deg=cfg.grid_deg)
        self.con = duckdb.connect(str(self.work / "diff.duckdb"))
        self.con.execute(f"SET temp_directory = {sql_str(str(self.work / 'duckdb_tmp'))}")
        self.job.runner.add_cancel_hook(self._interrupt)

        def progress(value: float, label: str) -> None:
            step.progress = value
            step.detail = label

        def check_cancel() -> None:
            if self.job.runner.cancelled:
                raise tools.CancelledError("Đã huỷ")

        self.engine = DiffEngine(self.con, spec, progress, check_cancel)
        result = self.engine.run()
        step.detail = None
        for s in SIDES:
            result["sides"][s] = {**self._side_meta(s), **result["sides"][s]}
        result["schema"] = compare_schema(result["sides"]["a"], result["sides"]["b"], result.pop("schema_nulls"),
                                          None if cfg.required_fields is None else list(cfg.required_fields))
        for s in SIDES:
            result["sides"][s].pop("fields")
        result["match"]["key_a"], result["match"]["key_b"] = (a.id_col, b.id_col) if self.mode == "key" else (None, None)
        result["columns"] = {s: self._columns_used(self.specs[s]) for s in SIDES}
        result["ignore_cols"] = list(ignore)
        result["gate"] = evaluate(result, cfg.thresholds)
        result["kind"] = "diff"
        result["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.result = result
        with self.job.lock:
            self.job.report = result
        m, gate = result["match"], result["gate"]
        self.job.add_log(
            f"A {result['sides']['a']['buildings']:,} · B {result['sides']['b']['buildings']:,} building — "
            f"khớp {m['matched']:,} (đổi {m['changed']:,}), thêm {m['added']:,}, xoá {m['removed']:,}"
        )
        self.job.add_log(f"Release gate: {gate['verdict'].upper()} "
                         f"({gate['counts']['fail']} fail, {gate['counts']['warn']} warn)")
        for f in gate["findings"]:
            if f["level"] != "info":
                self.job.add_log(f"  {f['level'].upper()}: {f['title']} — {f['detail']}")

    def _interrupt(self) -> None:
        con = self.con
        if con is not None:
            con.interrupt()

    def _side_meta(self, s: str) -> dict[str, Any]:
        info, inp = self.info[s], getattr(self.cfg, s)
        meta: dict[str, Any] = {
            "name": info["name"], "path": info["path"], "size": info["size"], "kind": info["kind"],
            "layer": info.get("layer"), "fields": info["fields"], "field_count": len(info["fields"]),
            "declared_count": info.get("feature_count"),
        }
        if info["kind"] == "pmtiles":
            header = info.get("header") or {}
            meta |= {"crs": probe.PMTILES_CRS, "minzoom": header.get("minzoom"), "maxzoom": header.get("maxzoom"),
                     "zoom": header.get("maxzoom"), "tilestats_count": info.get("feature_count"),
                     "generator": info.get("generator")}
        else:
            crs = info["crs"]
            meta |= {"crs": crs["label"] or inp.src_crs, "crs_declared": not crs["missing"], "driver": info.get("driver")}
        return meta

    @staticmethod
    def _columns_used(spec: SideSpec) -> dict[str, Any]:
        rule = spec.rule
        return {
            "id_col": spec.id_col, "height_col": rule.height_col if rule else None,
            "levels_col": rule.levels_col if rule else None, "prov_col": spec.prov_col,
            "prov_missing": list(spec.prov_missing), "parent_col": spec.parent_col,
            "superseded_col": spec.superseded_col, "category_cols": list(spec.category_cols),
            "height_from": "h_m" if spec.has("h_m") else "rule",
        }

    def _step_outputs(self, step: Step) -> None:
        assert self.engine is not None
        self.written = self.engine.write_outputs(self.work)
        self.gpkg_out.unlink(missing_ok=True)
        ogr2ogr = tools.require("ogr2ogr")
        first = True
        for i, (layer, label) in enumerate(GPKG_LAYERS):
            src = self.written[layer]
            cmd = [ogr2ogr, "-f", "GPKG", str(self.gpkg_out), str(src["path"]), "-nln", layer,
                   "-nlt", "PROMOTE_TO_MULTI", "-a_srs", WGS84, "-lco", "GEOMETRY_NAME=geom", "-lco", "FID=fid"]
            if not first:
                cmd.insert(1, "-update")
            self._run(cmd, step)
            first = False
            step.progress = 100.0 * (i + 1) / (len(GPKG_LAYERS) + 1)
            self.job.add_log(f"Layer '{layer}': {src['rows']:,} feature — {label}")
        grid = grid_geojson(self.engine.grid_cells(), self.cfg.grid_deg)
        self.grid_out.write_text(json.dumps(grid, ensure_ascii=False), encoding="utf-8")
        self._run([ogr2ogr, "-update", "-f", "GPKG", str(self.gpkg_out), str(self.grid_out), "-nln", "grid",
                   "-lco", "GEOMETRY_NAME=geom", "-lco", "FID=fid"], step)
        self._add_output("gpkg", "GeoPackage các thay đổi (mở bằng QGIS)", self.gpkg_out, "gpkg")
        self._add_output("grid", "Lưới thống kê (GeoJSON)", self.grid_out, "geojson")

    def _step_tiles(self, step: Step) -> None:
        rows = self.written["map"]["rows"]
        if not rows:
            step.status = "skipped"
            step.detail = "Không có thay đổi nào để vẽ"
            return
        seq = self.work / "map.geojsons"
        seq.unlink(missing_ok=True)
        self._run([tools.require("ogr2ogr"), "-f", "GeoJSONSeq", str(seq), str(self.written["map"]["path"]),
                   "-a_srs", WGS84], step)
        self.pmtiles_out.unlink(missing_ok=True)
        self._run([
            tools.require("tippecanoe"), "-o", str(self.pmtiles_out), "-l", MAP_LAYER, "-n", self.cfg.out_name,
            "-N", f"Diff {self.cfg.b.path.name} vs {self.cfg.a.path.name} — Building Data Studio {__version__}",
            "-Z", str(MAP_MINZOOM), "-z", str(MAP_MAXZOOM), "--drop-densest-as-needed",
            "--extend-zooms-if-still-dropping", "-P", "-t", str(self.work), "--force", str(seq),
        ], step)
        self._add_output("pmtiles", "PMTiles bản đồ diff", self.pmtiles_out, "pmtiles")

    def _step_finalize(self, step: Step) -> None:
        report = dict(self.result)
        report["provenance"] = {
            "app_version": __version__,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.job.started or time.time())),
            "tools": {k: v["version"] for k, v in tools.tool_versions().items()},
            "duckdb": duckdb.__version__,
            "commands": self.commands,
            "config": self.job.config,
            "thresholds": asdict(self.cfg.thresholds),
        }
        self.cleanup()
        report_json = self.out_dir / "diff_report.json"
        report_md = self.out_dir / "diff_report.md"
        report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        report_md.write_text(render_markdown(report), encoding="utf-8")
        self._add_output("report_md", "Báo cáo diff (Markdown)", report_md, "report")
        self._add_output("report_json", "Báo cáo diff (JSON)", report_json, "report")
        if self.cfg.keep_temp:
            self.job.add_log(f"Giữ file tạm trong {self.work}")
        else:
            shutil.rmtree(self.work, ignore_errors=True)


def execute(job: Job, registry: FileRegistry) -> None:
    """Entry point used by the JobManager worker thread."""
    try:
        cfg = parse_config(job.config)
    except ConfigError as exc:
        with job.lock:
            job.status = "failed"
            job.error = str(exc)
            job.ended = time.time()
        return
    DiffPipeline(job, registry, cfg).run()
