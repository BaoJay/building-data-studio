"""Conversion pipeline.

    input (.parquet / .gpkg / ...)
      └─ ogr2ogr + SQL ──► normalized GeoPackage (EPSG:4326, h_m, h_src, h_outlier)
           ├─ ogrinfo SQL ──► has_parts / is_part
           ├─ ogr2ogr ──► output GeoPackage in another CRS (optional)
           ├─ sqlite3 ──► height report (report.json / report.md)
           └─ ogr2ogr ──► FlatGeobuf (temp) ── tippecanoe ──► PMTiles

Every stage logs a feature-count accounting line so silent data loss shows up.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import __version__, probe, tools
from .jobs import FileRegistry, Job, Step
from .probe import GENERATED_FIELDS, ProbeError
from .probe import quote_ident as q
from .report import ReportSpec, build_report, render_markdown
from .sqlbuild import (
    HeightRule,
    NormalizeSpec,
    SpecError,
    build_normalize_sql,
    build_parts_statements,
    validate_where,
)

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = APP_DIR / "output"
WGS84 = "EPSG:4326"
GEOM_NAME = "geom"
DEFAULT_LAYER = "buildings"
WORK_DIR_NAME = "_work"
NORMALIZED_NAME = "normalized.gpkg"
TILES_INPUT_NAME = "tiles_input.fgb"
MAX_ZOOM = 22
MAX_NAME_LENGTH = 120
DEFAULT_MAX_TILE_KB = 500
HEIGHT_GENERATED = ("h_m", "h_src", "h_outlier")
PARTS_GENERATED = ("has_parts", "is_part")
DROP_FLAGS = {
    "drop-densest": "--drop-densest-as-needed",
    "coalesce-densest": "--coalesce-densest-as-needed",
    "none": None,
}
_FEATURES_RE = re.compile(r"^(\d+) features,")
_SAFE_NAME_RE = re.compile(r"[^\w.\-]+", re.UNICODE)


class ConfigError(ValueError):
    """Invalid job configuration (message is shown to the user)."""


class PipelineError(RuntimeError):
    """A pipeline stage failed (message is shown to the user)."""


@dataclass(frozen=True)
class TilesConfig:
    """tippecanoe options."""

    layer: str = DEFAULT_LAYER
    minzoom: int = 13
    maxzoom: int = 15
    attributes: tuple[str, ...] = ()
    drop: str = "drop-densest"
    extend_zooms: bool = True
    simplification: float | None = None
    max_tile_kb: int = DEFAULT_MAX_TILE_KB
    shared_borders: bool = False
    attribution: str = ""


@dataclass(frozen=True)
class JobConfig:
    """Validated job configuration."""

    input_path: Path
    layer: str | None
    geom_column: str | None
    src_crs: str | None
    out_dir: Path
    out_name: str
    want_gpkg: bool
    want_pmtiles: bool
    gpkg_layer: str
    gpkg_crs: str
    columns: tuple[str, ...] | None  # None = every column; () = none (ids for parts are re-added)
    where: str | None
    makevalid: bool
    height: dict[str, Any] | None
    parts: tuple[str, str] | None
    tiles: TilesConfig
    category_cols: tuple[str, ...]
    report_id_col: str | None
    keep_temp: bool


def safe_name(raw: str) -> str:
    """Make a string safe to use as a file / folder name (keeps Unicode letters)."""
    name = _SAFE_NAME_RE.sub("_", raw.strip()).strip("._")
    return name[:MAX_NAME_LENGTH] or "output"


def parse_config(raw: dict[str, Any]) -> JobConfig:
    """Validate the JSON sent by the UI.

    Raises:
        ConfigError: With a user-facing message for the first problem found.
    """
    try:
        inp = raw["input"]
        out = raw.get("output", {})
        attrs = raw.get("attributes", {})
        tiles_raw = raw.get("tiles", {})
        height_raw = raw.get("height", {})
        parts_raw = raw.get("parts", {})
        report_raw = raw.get("report", {})
    except (KeyError, TypeError) as exc:
        raise ConfigError("Cấu hình thiếu phần 'input'.") from exc

    try:
        input_path = probe.resolve_input_path(str(inp.get("path", "")))
    except ProbeError as exc:
        raise ConfigError(str(exc)) from exc
    if input_path.suffix.lower() in probe.TILES_SUFFIXES:
        raise ConfigError("File .pmtiles chỉ dùng để xem bản đồ, không convert được.")

    want_gpkg = bool(out.get("gpkg", True))
    want_pmtiles = bool(out.get("pmtiles", True))
    if not (want_gpkg or want_pmtiles):
        raise ConfigError("Chọn ít nhất một output: GeoPackage hoặc PMTiles.")

    out_dir = Path(str(out.get("dir") or DEFAULT_OUTPUT_DIR)).expanduser()
    if out_dir.exists() and not out_dir.is_dir():
        raise ConfigError(f"Thư mục output không hợp lệ: {out_dir}")

    minzoom = _int(tiles_raw.get("minzoom", 13), "min zoom", 0, MAX_ZOOM)
    maxzoom = _int(tiles_raw.get("maxzoom", 15), "max zoom", 0, MAX_ZOOM)
    if minzoom > maxzoom:
        raise ConfigError("Min zoom phải ≤ max zoom.")
    drop = str(tiles_raw.get("drop", "drop-densest"))
    if drop not in DROP_FLAGS:
        raise ConfigError(f"Chiến lược giảm feature không hợp lệ: {drop}")
    simplification = tiles_raw.get("simplification")
    tiles = TilesConfig(
        layer=_ident(tiles_raw.get("layer") or DEFAULT_LAYER, "Tên layer PMTiles"),
        minzoom=minzoom,
        maxzoom=maxzoom,
        attributes=tuple(str(a) for a in tiles_raw.get("attributes", [])),
        drop=drop,
        extend_zooms=bool(tiles_raw.get("extend_zooms", True)),
        simplification=(
            None if simplification in (None, "") else _float(simplification, "Simplification", 0, 100)
        ),
        max_tile_kb=_int(tiles_raw.get("max_tile_kb", DEFAULT_MAX_TILE_KB), "Dung lượng tile tối đa", 50, 100000),
        shared_borders=bool(tiles_raw.get("shared_borders", False)),
        attribution=str(tiles_raw.get("attribution") or "").strip(),
    )

    height = None
    if height_raw.get("enabled", True):
        height = dict(height_raw)

    parts = None
    if parts_raw.get("enabled") and parts_raw.get("id_col") and parts_raw.get("parent_col"):
        parts = (str(parts_raw["id_col"]), str(parts_raw["parent_col"]))

    try:
        where = validate_where(attrs.get("where"))
    except SpecError as exc:
        raise ConfigError(str(exc)) from exc

    return JobConfig(
        input_path=input_path,
        layer=inp.get("layer") or None,
        geom_column=inp.get("geom_column") or None,
        src_crs=(str(inp.get("src_crs")).strip() or None) if inp.get("src_crs") else None,
        out_dir=out_dir,
        out_name=safe_name(str(out.get("name") or input_path.stem)),
        want_gpkg=want_gpkg,
        want_pmtiles=want_pmtiles,
        gpkg_layer=_ident(out.get("gpkg_layer") or DEFAULT_LAYER, "Tên layer GPKG"),
        gpkg_crs=str(out.get("gpkg_crs") or WGS84).strip(),
        columns=None if attrs.get("columns") is None else tuple(str(c) for c in attrs["columns"]),
        where=where,
        makevalid=bool(attrs.get("makevalid", False)),
        height=height,
        parts=parts,
        tiles=tiles,
        category_cols=tuple(str(c) for c in report_raw.get("category_cols", [])),
        report_id_col=report_raw.get("id_col") or None,
        keep_temp=bool(out.get("keep_temp", False)),
    )


def _int(value: Any, label: str, lo: int, hi: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} phải là số nguyên.") from exc
    if not lo <= number <= hi:
        raise ConfigError(f"{label} phải trong khoảng {lo}–{hi}.")
    return number


def _float(value: Any, label: str, lo: float, hi: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} phải là số.") from exc
    if not lo <= number <= hi:
        raise ConfigError(f"{label} phải trong khoảng {lo}–{hi}.")
    return number


def _ident(value: Any, label: str) -> str:
    text = str(value).strip()
    if not text or len(text) > MAX_NAME_LENGTH or any(c in text for c in '"/\\'):
        raise ConfigError(f"{label} không hợp lệ: {text!r}")
    return text


def build_height_rule(raw: dict[str, Any], fields: dict[str, dict[str, Any]]) -> HeightRule:
    """Turn the UI height settings into a HeightRule, checking columns exist."""

    def col(key: str) -> str | None:
        name = raw.get(key) or None
        if name is not None and name not in fields:
            raise ConfigError(f"Không có cột '{name}' trong file.")
        return name

    def is_text(name: str | None) -> bool:
        return bool(name) and fields[name]["type"] == "String"

    height_col, levels_col, prov_col = col("height_col"), col("levels_col"), col("prov_col")
    prov_missing = raw.get("prov_missing") or []
    if isinstance(prov_missing, str):
        prov_missing = [v.strip() for v in prov_missing.split(",")]
    rule = HeightRule(
        height_col=height_col,
        height_is_text=is_text(height_col),
        levels_col=levels_col,
        levels_is_text=is_text(levels_col),
        m_per_level=float(raw.get("m_per_level", 3.5)),
        default_m=float(raw.get("default_m", 4.0)),
        min_valid_m=float(raw.get("min_valid_m", 2.0)),
        max_valid_m=float(raw.get("max_valid_m", 500.0)),
        max_levels=float(raw.get("max_levels", 200)),
        outlier_mode=str(raw.get("outlier_mode", "fallback")),  # type: ignore[arg-type]
        decimals=int(raw.get("decimals", 1)),
        prov_col=prov_col if height_col else None,
        prov_missing=tuple(v for v in prov_missing if v),
    )
    try:
        rule.validate()
    except SpecError as exc:
        raise ConfigError(str(exc)) from exc
    return rule


class StagedPipeline:
    """Runs a job's steps in order, updating the Job for the UI.

    Subclasses provide plan(), intro(), the `_step_<key>` methods and the
    output folder; this class owns the run loop, logging, command execution,
    cancellation and output registration.
    """

    # Errors that end a run as "failed" with their message shown to the user.
    handled_errors: tuple[type[BaseException], ...] = (
        PipelineError, ConfigError, ProbeError, SpecError, tools.ToolMissingError,
        OSError, sqlite3.Error, json.JSONDecodeError,
    )

    def __init__(self, job: Job, registry: FileRegistry, out_dir: Path) -> None:
        self.job = job
        self.registry = registry
        self.out_dir = out_dir
        self.work = out_dir / WORK_DIR_NAME
        self.commands: list[str] = []

    def plan(self) -> list[Step]:
        raise NotImplementedError

    def intro(self) -> list[str]:
        """First log lines of a run."""
        return [f"Building Data Studio {__version__}", f"Thư mục output: {self.out_dir}"]

    # ---------------------------------------------------------------- driver
    def run(self) -> None:
        job = self.job
        self.work.mkdir(parents=True, exist_ok=True)
        with job.lock:
            job.steps = self.plan()
            job.status = "running"
            job.started = time.time()
            job.output_dir = str(self.out_dir)
            job.output_dir_token = self.registry.register(self.out_dir)
            job.log_file = (self.out_dir / "log.txt").open("a", encoding="utf-8")
        self._add_output("log", "Log chạy", self.out_dir / "log.txt", "log")
        for line in self.intro():
            job.add_log(line)
        try:
            for step in job.steps:
                if job.runner.cancelled:
                    raise tools.CancelledError("Đã huỷ")
                self._run_step(step)
            with job.lock:
                job.status = "done"
            job.add_log("HOÀN TẤT ✔")
        except tools.CancelledError:
            self._mark_cancelled()
        except self.handled_errors as exc:
            if job.runner.cancelled:
                # An interrupted in-process query surfaces as its own error type.
                self._mark_cancelled()
            else:
                with job.lock:
                    job.status = "failed"
                    job.error = str(exc)
                job.add_log(f"LỖI: {exc}")
                job.add_log(f"File trung gian được giữ lại để debug: {self.work}")
        finally:
            self.cleanup()
            with job.lock:
                job.ended = time.time()
                if job.log_file is not None:
                    job.log_file.close()
                    job.log_file = None
            self._refresh_output_sizes()

    def cleanup(self) -> None:
        """Release resources held by the run (called on success, failure and cancel)."""

    def _mark_cancelled(self) -> None:
        with self.job.lock:
            self.job.status = "cancelled"
            self.job.error = "Đã huỷ theo yêu cầu."
        self.job.add_log("Đã huỷ. File trung gian giữ lại trong _work/.")

    def _run_step(self, step: Step) -> None:
        job = self.job
        with job.lock:
            step.status = "running"
            step.started = time.time()
        job.add_log(f"▶ {step.title}")
        try:
            getattr(self, f"_step_{step.key}")(step)
        except BaseException:
            with job.lock:
                step.status = "failed"
                step.ended = time.time()
            raise
        with job.lock:
            if step.status == "running":
                step.status = "done"
                step.progress = 100.0
            step.ended = time.time()
        job.add_log(f"✔ {step.title} ({step.ended - step.started:.1f}s)")

    # ---------------------------------------------------------------- helpers
    def _run(self, cmd: list[str], step: Step, *, fail_on_error_lines: bool = False,
             line_hook=None) -> None:
        display = shlex.join([Path(cmd[0]).name, *cmd[1:]])
        self.commands.append(display)
        self.job.add_log(f"$ {display}")
        errors: list[str] = []

        def on_line(line: str) -> None:
            self.job.add_log(line)
            if line.startswith("ERROR"):
                errors.append(line)
            if line_hook:
                line_hook(line)

        def on_progress(value: float) -> None:
            step.progress = value

        code = self.job.runner.run(cmd, on_line, on_progress, cwd=self.work)
        tool = Path(cmd[0]).name
        if code != 0:
            raise PipelineError(f"{tool} thất bại (mã {code}): {errors[-1] if errors else 'xem log'}")
        if fail_on_error_lines and errors:
            raise PipelineError(f"{tool} báo lỗi: {errors[-1]}")

    def _add_output(self, key: str, label: str, path: Path, kind: str) -> None:
        with self.job.lock:
            self.job.outputs[key] = {
                "key": key,
                "label": label,
                "name": path.name,
                "path": str(path),
                "size": path.stat().st_size if path.exists() else None,
                "token": self.registry.register(path),
                "kind": kind,
            }

    def _refresh_output_sizes(self) -> None:
        with self.job.lock:
            for out in self.job.outputs.values():
                p = Path(out["path"])
                out["size"] = p.stat().st_size if p.exists() else None


class Pipeline(StagedPipeline):
    """Conversion: input -> normalised GeoPackage (+ reprojected copy) -> PMTiles."""

    def __init__(self, job: Job, registry: FileRegistry, cfg: JobConfig) -> None:
        super().__init__(job, registry, cfg.out_dir / f"{cfg.out_name}__{time.strftime('%Y%m%d-%H%M%S')}")
        self.cfg = cfg
        self.gpkg_out = self.out_dir / f"{cfg.out_name}.gpkg"
        self.pmtiles_out = self.out_dir / f"{cfg.out_name}.pmtiles"
        direct = cfg.want_gpkg and _same_crs(cfg.gpkg_crs, WGS84)
        self.normalized = self.gpkg_out if direct else self.work / NORMALIZED_NAME
        self.accounting: dict[str, Any] = {}
        self.info: dict[str, Any] = {}
        self.rule: HeightRule | None = None
        self.passthrough: list[str] = []
        self.src_crs: str | None = None
        self.tiles_info: dict[str, Any] | None = None

    def intro(self) -> list[str]:
        return [f"Building Data Studio {__version__} — input: {self.cfg.input_path}", f"Thư mục output: {self.out_dir}"]

    def plan(self) -> list[Step]:
        cfg = self.cfg
        steps = [
            Step("probe", "Đọc file đầu vào"),
            Step("normalize", "Chuẩn hoá → GeoPackage (EPSG:4326)"),
        ]
        if cfg.parts:
            steps.append(Step("parts", "Gán has_parts / is_part"))
        steps.append(Step("verify", "Đối soát số lượng feature"))
        if cfg.want_gpkg and not _same_crs(cfg.gpkg_crs, WGS84):
            steps.append(Step("reproject", f"Đổi hệ toạ độ GPKG → {cfg.gpkg_crs}"))
        steps.append(Step("report", "Báo cáo chiều cao"))
        if cfg.want_pmtiles:
            steps += [
                Step("fgb", "Chuẩn bị dữ liệu cho tippecanoe"),
                Step("tiles", f"Cắt tile z{cfg.tiles.minzoom}–{cfg.tiles.maxzoom} → PMTiles"),
                Step("check", "Kiểm tra PMTiles"),
            ]
        steps.append(Step("finalize", "Ghi báo cáo & dọn file tạm"))
        return steps

    # ---------------------------------------------------------------- stages
    def _step_probe(self, step: Step) -> None:
        cfg, job = self.cfg, self.job
        info = probe.probe(cfg.input_path, geom_column=cfg.geom_column, layer=cfg.layer)
        self.info = info
        fields = {f["name"]: f for f in info["fields"]}

        if info["crs"]["missing"]:
            if not cfg.src_crs:
                raise ConfigError("File không khai báo CRS — hãy nhập CRS nguồn (vd EPSG:4326).")
            self.src_crs = cfg.src_crs
            job.add_log(f"CRS nguồn (người dùng xác nhận): {cfg.src_crs}")
        else:
            self.src_crs = info["crs"]["definition"]
            job.add_log(f"CRS nguồn (từ file): {info['crs']['label']}")

        regenerated = set()
        if cfg.height is not None:
            self.rule = build_height_rule(cfg.height, fields)
            regenerated |= set(HEIGHT_GENERATED)
        if cfg.parts:
            regenerated |= set(PARTS_GENERATED)
            for name in cfg.parts:
                if name not in fields:
                    raise ConfigError(f"Không có cột '{name}' trong file.")

        requested = list(cfg.columns) if cfg.columns is not None else [f for f in fields if f not in GENERATED_FIELDS]
        unknown = [c for c in requested if c not in fields]
        if unknown:
            raise ConfigError(f"Không có cột: {', '.join(unknown)}")
        dropped = [c for c in requested if c in regenerated]
        if dropped:
            job.add_log(f"Cột {', '.join(dropped)} có sẵn trong input sẽ được tính lại.")
        passthrough = [c for c in requested if c not in regenerated]
        if cfg.parts:
            for name in cfg.parts:
                if name not in passthrough:
                    passthrough.append(name)
                    job.add_log(f"Giữ thêm cột '{name}' vì cần để tính has_parts.")
        self.passthrough = passthrough
        for c in cfg.category_cols:
            if c not in fields and c not in regenerated:
                raise ConfigError(f"Cột phân loại '{c}' không tồn tại.")

        self.accounting["input_count"] = info["feature_count"]
        geom = info["geometry"]
        job.add_log(
            f"Layer '{info['layer']}' — {info['feature_count']:,} feature, geometry '{geom['column']}' "
            f"({geom['type']}{', mẫu: ' + '/'.join(geom['sampled_types']) if geom.get('sampled_types') else ''})"
        )
        for warning in info.get("warnings", []):
            job.add_log(f"Lưu ý: {warning}")

    def _step_normalize(self, step: Step) -> None:
        cfg, info = self.cfg, self.info
        sql = build_normalize_sql(
            NormalizeSpec(
                layer=info["layer"],
                geom_sql_name=info["geometry"]["sql_name"],
                columns=tuple(self.passthrough),
                height=self.rule,
                where=cfg.where,
            )
        )
        self.normalized.unlink(missing_ok=True)
        cmd = [
            tools.require("ogr2ogr"), "-f", "GPKG", str(self.normalized), str(cfg.input_path),
            *_oo_args(info["open_options"]),
            "-dialect", "SQLite", "-sql", sql,
            "-nln", cfg.gpkg_layer, "-nlt", info["geometry"]["nlt"],
            "-s_srs", str(self.src_crs), "-t_srs", WGS84,
            "-lco", f"GEOMETRY_NAME={GEOM_NAME}", "-lco", "FID=fid", "-lco", "SPATIAL_INDEX=YES",
            "-progress",
        ]
        if cfg.makevalid:
            cmd.append("-makevalid")
        self._run(cmd, step)
        if self.normalized == self.gpkg_out:
            self._add_output("gpkg", "GeoPackage", self.gpkg_out, "gpkg")

    def _step_parts(self, step: Step) -> None:
        assert self.cfg.parts is not None
        id_col, parent_col = self.cfg.parts
        statements = build_parts_statements(self.cfg.gpkg_layer, id_col, parent_col)
        ogrinfo = tools.require("ogrinfo")
        for i, sql in enumerate(statements, start=1):
            self._run([ogrinfo, str(self.normalized), "-q", "-sql", sql], step, fail_on_error_lines=True)
            step.progress = 100.0 * i / len(statements)
        with closing(sqlite3.connect(self.normalized.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            has_parts, is_part = conn.execute(
                f'SELECT SUM("has_parts"), SUM("is_part") FROM {q(self.cfg.gpkg_layer)}'
            ).fetchone()
        self.accounting["has_parts"] = has_parts or 0
        self.accounting["is_part"] = is_part or 0
        self.job.add_log(f"{has_parts or 0:,} toà nhà có khối con; {is_part or 0:,} khối con (is_part).")

    def _step_verify(self, step: Step) -> None:
        with closing(sqlite3.connect(self.normalized.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            table = q(self.cfg.gpkg_layer)
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            null_geom = conn.execute(f'SELECT COUNT(*) FROM {table} WHERE "{GEOM_NAME}" IS NULL').fetchone()[0]
        self.accounting["normalized_count"] = count
        self.accounting["null_geometry"] = null_geom
        expected = self.accounting.get("input_count")
        self.job.add_log(f"Đối soát: input {expected:,} → GPKG {count:,} feature")
        if self.cfg.where and expected is not None:
            self.accounting["filtered_out"] = expected - count
            self.job.add_log(f"Điều kiện lọc đã loại {expected - count:,} feature.")
        elif expected is not None and count != expected:
            step.detail = f"Lệch {expected - count:,} feature so với input"
            self.job.add_log(f"CẢNH BÁO: số feature lệch {expected - count:,} so với input — xem log ogr2ogr.")
        if null_geom:
            self.job.add_log(f"CẢNH BÁO: {null_geom:,} feature không có geometry.")

    def _step_reproject(self, step: Step) -> None:
        self.gpkg_out.unlink(missing_ok=True)
        cmd = [
            tools.require("ogr2ogr"), "-f", "GPKG", str(self.gpkg_out), str(self.normalized),
            self.cfg.gpkg_layer, "-t_srs", self.cfg.gpkg_crs, "-nln", self.cfg.gpkg_layer,
            "-preserve_fid", "-lco", f"GEOMETRY_NAME={GEOM_NAME}", "-lco", "FID=fid", "-progress",
        ]
        self._run(cmd, step)
        self._add_output("gpkg", f"GeoPackage ({self.cfg.gpkg_crs})", self.gpkg_out, "gpkg")

    def _step_report(self, step: Step) -> None:
        cfg, rule = self.cfg, self.rule
        id_col = cfg.report_id_col or (cfg.parts[0] if cfg.parts else None) or self.info["suggest"].get("id_col")
        spec = ReportSpec(
            table=cfg.gpkg_layer,
            geom_col=GEOM_NAME,
            has_height=rule is not None,
            id_col=id_col,
            height_col=rule.height_col if rule else None,
            levels_col=rule.levels_col if rule else None,
            prov_col=rule.prov_col if rule else None,
            category_cols=cfg.category_cols,
        )
        report = build_report(self.normalized, spec)
        report["accounting"] = self.accounting
        report["rule"] = asdict(rule) if rule else None
        report["id_col"] = id_col
        with self.job.lock:
            self.job.report = report
        height = report.get("height")
        if height:
            s = height["stats"]
            self.job.add_log(
                f"Chiều cao thực: {height['real_count']:,}/{report['total']:,} ({height['real_pct']}%), "
                f"outlier: {height['outlier_count']:,}, h_m trung vị {s['median']} m, max {s['max']} m"
            )

    def _step_fgb(self, step: Step) -> None:
        cfg = self.cfg
        with closing(sqlite3.connect(self.normalized.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            columns = [r[1] for r in conn.execute(f"PRAGMA table_info({q(cfg.gpkg_layer)})")]
        wanted = list(cfg.tiles.attributes) if cfg.tiles.attributes else []
        attrs = [a for a in wanted if a in columns and a not in ("fid", GEOM_NAME)]
        missing = [a for a in wanted if a not in attrs]
        if missing:
            self.job.add_log(f"Bỏ qua thuộc tính không có trong dữ liệu: {', '.join(missing)}")
        self.job.add_log(f"Thuộc tính đưa vào tile: {', '.join(attrs) or '(không có)'}")
        select = ", ".join([*(q(a) for a in attrs), q(GEOM_NAME)])
        fgb = self.work / TILES_INPUT_NAME
        fgb.unlink(missing_ok=True)
        cmd = [
            tools.require("ogr2ogr"), "-f", "FlatGeobuf", str(fgb), str(self.normalized),
            "-sql", f"SELECT {select} FROM {q(cfg.gpkg_layer)}",
            "-nln", cfg.tiles.layer, "-lco", "SPATIAL_INDEX=NO", "-progress",
        ]
        self._run(cmd, step)

    def _step_tiles(self, step: Step) -> None:
        cfg, t = self.cfg, self.cfg.tiles
        self.pmtiles_out.unlink(missing_ok=True)
        cmd = [
            tools.require("tippecanoe"), "-o", str(self.pmtiles_out),
            "-l", t.layer, "-n", cfg.out_name,
            "-N", f"{cfg.out_name} — Building Data Studio {__version__}",
            "-Z", str(t.minzoom), "-z", str(t.maxzoom),
            "-M", str(t.max_tile_kb * 1000),
            "-t", str(self.work), "--force",
        ]
        if DROP_FLAGS[t.drop]:
            cmd.append(DROP_FLAGS[t.drop])
            if t.extend_zooms:
                cmd.append("--extend-zooms-if-still-dropping")
        if t.simplification is not None:
            cmd += ["-S", f"{t.simplification:g}"]
        if t.shared_borders:
            cmd.append("--detect-shared-borders")
        if t.attribution:
            cmd += ["-A", t.attribution]
        cmd.append(str(self.work / TILES_INPUT_NAME))

        def watch(line: str) -> None:
            match = _FEATURES_RE.match(line)
            if match:
                self.accounting["pmtiles_features"] = int(match.group(1))

        self._run(cmd, step, line_hook=watch)
        self.accounting["pmtiles_bytes"] = self.pmtiles_out.stat().st_size
        self._add_output("pmtiles", "PMTiles", self.pmtiles_out, "pmtiles")
        expected = self.accounting.get("normalized_count")
        got = self.accounting.get("pmtiles_features")
        if expected is not None and got is not None and got != expected - self.accounting.get("null_geometry", 0):
            self.job.add_log(f"CẢNH BÁO: tippecanoe đọc {got:,} feature, GPKG có {expected:,}.")

    def _step_check(self, step: Step) -> None:
        exe = tools.which("pmtiles")
        if exe is None:
            step.status = "skipped"
            step.detail = "Chưa cài pmtiles CLI"
            self.job.add_log("Bỏ qua kiểm tra vì chưa cài pmtiles CLI (brew install pmtiles).")
            return
        header = tools.run_capture([exe, "show", "--header-json", str(self.pmtiles_out)], timeout=120)
        plain = tools.run_capture([exe, "show", str(self.pmtiles_out)], timeout=120)
        verify = tools.run_capture([exe, "verify", str(self.pmtiles_out)], timeout=600)
        if header.returncode != 0:
            raise PipelineError(f"pmtiles show lỗi: {header.stderr.strip()}")
        info: dict[str, Any] = json.loads(header.stdout)
        counts = dict(re.findall(r"^(addressed tiles count|tile contents count): (\d+)$", plain.stdout, re.M))
        info["addressed_tiles"] = int(counts.get("addressed tiles count", 0)) or None
        info["tile_contents"] = int(counts.get("tile contents count", 0)) or None
        info["verified"] = verify.returncode == 0
        info["size_bytes"] = self.pmtiles_out.stat().st_size
        self.tiles_info = info
        self.job.add_log(
            f"PMTiles z{info.get('minzoom')}–{info.get('maxzoom')}, {info['addressed_tiles'] or '?'} tile, "
            f"{info['size_bytes'] / 1e6:.1f} MB, verify: {'OK' if info['verified'] else 'LỖI'}"
        )
        if not info["verified"]:
            raise PipelineError(f"pmtiles verify thất bại: {verify.stderr.strip() or verify.stdout.strip()}")

    def _step_finalize(self, step: Step) -> None:
        report = dict(self.job.report or {})
        report["accounting"] = self.accounting
        report["pmtiles"] = self.tiles_info
        report["provenance"] = self._provenance()
        with self.job.lock:
            self.job.report = report
        report_json = self.out_dir / "report.json"
        report_md = self.out_dir / "report.md"
        report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        report_md.write_text(render_markdown(report, self.cfg.out_name), encoding="utf-8")
        self._add_output("report_md", "Báo cáo (Markdown)", report_md, "report")
        self._add_output("report_json", "Báo cáo (JSON)", report_json, "report")
        if self.cfg.keep_temp:
            self.job.add_log(f"Giữ file tạm trong {self.work}")
        else:
            shutil.rmtree(self.work, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    def _provenance(self) -> dict[str, Any]:
        stat = self.cfg.input_path.stat()
        return {
            "app_version": __version__,
            "input": str(self.cfg.input_path),
            "input_size": stat.st_size,
            "input_modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime)),
            "source_crs": self.src_crs,
            "crs_assumed": bool(self.info.get("crs", {}).get("missing")),
            "open_options": self.info.get("open_options"),
            "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.job.started or time.time())),
            "tools": {k: v["version"] for k, v in tools.tool_versions().items()},
            "commands": self.commands,
            "config": self.job.config,
        }


def _same_crs(a: str, b: str) -> bool:
    return a.strip().upper().replace(" ", "") == b.strip().upper().replace(" ", "")


def _oo_args(open_options: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in open_options.items():
        args += ["-oo", f"{key}={value}"]
    return args


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
    Pipeline(job, registry, cfg).run()
