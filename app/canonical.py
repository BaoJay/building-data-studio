"""Canonical Building Data: one building dataset built from several sources.

    Ingest      OSM (Overpass, or a .osm.pbf), Google Open Buildings v3 (downloaded), GBA (a file,
                optional): each kept as an immutable snapshot in canonical/sources/
    Normalize   valid polygons in the area, GOB confidence >= 0.75, GBA split by origin
    Matching    overlap graph, connected components (app.conflation)
    Conflation  OSM-first footprints, evidence, heights (OSM height -> levels x 3.5 m -> GBA -> 3.5 m)
    Build       stable building_id (app.idregistry), landmarks kept, building_tier (app.buildingtier),
                invariants, then the convert pipeline: GeoPackage + PMTiles (VMAP schema)

    canonical/
      sources/osm/<snapshot>/osm.parquet, sources/gob/<snapshot>/gob.parquet (+ source.json)
      <area>/canonical.json                     the build in use (the 3D preview opens it)
      <area>/id_registry.parquet                source record -> building_id, across builds
      <area>/canonical_building_<area>__<time>/ one build: .parquet (the deliverable, columns of the
                                                platform's canonical_building), .gpkg, .pmtiles,
                                                review_osm.gpkg / review_gob.gpkg (MapOps lists),
                                                conflation_bridge.parquet, canonical_report.md, build.json

A build that fails (an invariant, a step) leaves canonical.json, the registry and the landmark
library as they were.
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import (
    __version__,
    buildingtier,
    conflation,
    gob,
    idregistry,
    landmark_library,
    osmdirect,
    osmgeom,
    osmtags,
    tileschema,
    tools,
)
from .jobs import FileRegistry, Job, Step
from .osmbase import AREAS, DEFAULT_AREA, Area, BaseError, load_extension
from .osmbase import default_dir as base_default_dir
from .osmbase import read_manifest as read_base_manifest
from .pipeline import WGS84, ConfigError, JobConfig, Pipeline, PipelineError, TilesConfig, run_job
from .sqlbuild import sql_str

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DIR = APP_DIR / "canonical"
ICONIC_PINS = APP_DIR / "config" / "building_tier" / "iconic_pins.geojson"
ACTIVE_NAME = "canonical.json"
BUILD_NAME = "build.json"
BOUNDARY_NAME = "boundary.geojson"
OSM_SOURCES = ("overpass", "file", "snapshot")
MARGIN_DEG = 0.01  # sources are read a little beyond the area so its edges resolve like its middle
OSM_ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"

H3_PRESETS = {
    "hanoi-48": {
        "label": "48 ô H3 r8 (release 25/11/2026)",
        "area": "hanoi",
        "cells": (
            "88415cb4a1fffff 88415cb4a5fffff 88415cb4a9fffff 88415cb4adfffff 88415cb4e7fffff 88415cb5d3fffff "
            "88415cb5dbfffff 88415cb4e5fffff 88415cb4edfffff 88415cb425fffff 88415cb427fffff 88415cb42dfffff "
            "88415cb503fffff 88415cb50bfffff 88415cb511fffff 88415cb513fffff 88415cb515fffff 88415cb519fffff "
            "88415cb51bfffff 88415cb51dfffff 88415cb551fffff 88415cb553fffff 88415cb555fffff 88415cb557fffff "
            "88415cb55bfffff 88415cb5c5fffff 88415cb5cdfffff 8841436901fffff 8841436905fffff 8841436909fffff "
            "884143690dfffff 8841436929fffff 884143692bfffff 8841436941fffff 8841436943fffff 8841436945fffff "
            "8841436947fffff 8841436949fffff 884143694bfffff 884143694dfffff 8841436961fffff 8841436963fffff "
            "8841436967fffff 8841436969fffff 884143696bfffff 8841436b27fffff 88415cb493fffff 88415cb49bfffff"
        ).split(),
    },
}
_H3_RE = re.compile(r"^[0-9a-f]{15}$")


def default_dir() -> Path:
    return Path(os.environ.get("BDS_CANONICAL_DIR") or DEFAULT_DIR).expanduser()


def area_dir(root: Path, area: Area) -> Path:
    return root / area.key


def out_name(area: Area) -> str:
    return f"canonical_building_{area.key}"


def sources_dir(root: Path, name: str) -> Path:
    return root / "sources" / name


# ------------------------------------------------------------------ options

@dataclass(frozen=True)
class Options:
    """A validated build request."""

    area: Area
    aoi: str  # "area": the whole city | "h3": a list of H3 cells
    h3_cells: tuple[str, ...] = ()
    aoi_label: str = ""
    osm_source: str = "overpass"
    osm_file: Path | None = None
    overpass_urls: tuple[str, ...] = osmdirect.OVERPASS_URLS
    gob_refresh: bool = False
    gba_file: Path | None = None
    poi_file: Path | None = None
    seed_file: Path | None = None
    config_version: str = conflation.DEFAULT_CONFIG
    tier_rule: str = buildingtier.DEFAULT_RULE
    activate: bool = True


def _file(value: Any, label: str, suffixes: tuple[str, ...]) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_file():
        raise ConfigError(f"Không thấy {label}: {path}")
    if not path.name.lower().endswith(suffixes):
        raise ConfigError(f"{label} phải là {' / '.join(suffixes)}: {path.name}")
    return path


def parse_options(raw: dict[str, Any]) -> Options:
    area = AREAS.get(str(raw.get("area") or DEFAULT_AREA))
    if area is None:
        raise ConfigError(f"Không có khu vực {raw.get('area')!r}.")
    aoi = str(raw.get("aoi") or "area")
    cells: tuple[str, ...] = ()
    label = area.label
    if aoi == "h3":
        preset = str(raw.get("h3_preset") or "").strip()
        if preset:
            if preset not in H3_PRESETS:
                raise ConfigError(f"Không có bộ ô H3 {preset!r}.")
            cells, label = tuple(H3_PRESETS[preset]["cells"]), H3_PRESETS[preset]["label"]
        else:
            text = raw.get("h3_cells") or ""
            items = text if isinstance(text, list) else re.split(r"[\s,;]+", str(text))
            cells = tuple(sorted({c.strip().lower() for c in items if c.strip()}))
            label = f"{len(cells)} ô H3"
        if not cells:
            raise ConfigError("Chọn bộ ô H3 hoặc dán danh sách ô H3.")
        bad = [c for c in cells if not _H3_RE.match(c)]
        if bad:
            raise ConfigError(f"Ô H3 không hợp lệ: {', '.join(bad[:5])}")
    elif aoi != "area":
        raise ConfigError(f"Vùng build không hợp lệ: {aoi} (area | h3).")
    osm_source = str(raw.get("osm_source") or "overpass")
    if osm_source not in OSM_SOURCES:
        raise ConfigError(f"Nguồn OSM không hợp lệ: {osm_source} ({' | '.join(OSM_SOURCES)}).")
    osm_file = _file(raw.get("osm_file"), "file OSM", (".pbf", ".osm")) if osm_source == "file" else None
    if osm_source == "file" and osm_file is None:
        raise ConfigError("Nguồn OSM 'file' cần đường dẫn file .osm.pbf / .osm.")
    url = str(raw.get("overpass_url") or os.environ.get("BDS_OVERPASS_URL") or "").strip()
    config_version = str(raw.get("config_version") or conflation.DEFAULT_CONFIG)
    if config_version not in conflation.CONFIGS:
        raise ConfigError(f"Không có cấu hình conflation {config_version!r}.")
    tier_rule = str(raw.get("tier_rule") or buildingtier.DEFAULT_RULE)
    if tier_rule not in buildingtier.RULES:
        raise ConfigError(f"Không có luật building_tier {tier_rule!r}.")
    return Options(
        area=area, aoi=aoi, h3_cells=cells, aoi_label=label, osm_source=osm_source, osm_file=osm_file,
        overpass_urls=(url,) if url else osmdirect.OVERPASS_URLS,
        gob_refresh=bool(raw.get("gob_refresh")),
        gba_file=_file(raw.get("gba_file"), "file GBA", (".parquet", ".gpkg", ".geojson", ".geojsonl", ".fgb", ".shp")),
        poi_file=_file(raw.get("poi_file"), "file POI", (".parquet",)),
        seed_file=_file(raw.get("seed_file"), "file canonical để giữ building_id", (".parquet",)),
        config_version=config_version, tier_rule=tier_rule, activate=raw.get("activate", True) is not False,
    )


# ------------------------------------------------------------------ builds on disk

def read_build(folder: Path) -> dict[str, Any] | None:
    try:
        return json.loads((folder / BUILD_NAME).read_text("utf-8"))
    except (OSError, ValueError):
        return None


def read_active(root: Path, area: Area) -> dict[str, Any] | None:
    """The build in use, with absolute paths, or None (none yet, or its files are gone)."""
    folder = area_dir(root, area)
    try:
        data = json.loads((folder / ACTIVE_NAME).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    paths = {k: folder / data[k] for k in ("folder", "parquet", "gpkg", "pmtiles") if data.get(k)}
    if not all(paths.get(k) and paths[k].exists() for k in ("pmtiles", "parquet")):
        return None
    return {**data, **{f"{k}_path": str(v) for k, v in paths.items()}}


def list_builds(root: Path, area: Area, limit: int = 20) -> list[dict[str, Any]]:
    """Finished builds of an area, newest first."""
    out = []
    folder = area_dir(root, area)
    for path in sorted(folder.glob(f"{out_name(area)}__*"), reverse=True):
        data = read_build(path)
        if data:
            out.append({**data, "folder_path": str(path)})
        if len(out) >= limit:
            break
    return out


def activate(root: Path, area: Area, folder_name: str) -> dict[str, Any]:
    """Put a finished build in use (also how a rollback is done)."""
    folder = area_dir(root, area) / folder_name
    data = read_build(folder)
    if data is None or folder.parent != area_dir(root, area):
        raise ConfigError(f"Không có build {folder_name}.")
    write_active(root, area, folder, data)
    return data


def write_active(root: Path, area: Area, folder: Path, build: dict[str, Any]) -> Path:
    files = build["files"]
    data = {
        "area": area.key, "label": area.label, "name": f"{area.label} · Canonical {build['build_id']}",
        "build_id": build["build_id"], "aoi": build["aoi"], "config_version": build["config_version"],
        "counts": build["counts"], "built_at": build["built_at"], "activated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "view": _view(area, build["aoi"].get("bbox")), "folder": folder.name,
        **{k: f"{folder.name}/{files[k]}" for k in ("parquet", "gpkg", "pmtiles") if files.get(k)},
    }
    path = area_dir(root, area) / ACTIVE_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def _view(area: Area, bbox: list[float] | None) -> dict[str, Any]:
    """Where the 3D preview opens: the city's usual view, or the middle of a smaller build area."""
    lon, lat, zoom = area.view
    if bbox and not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
        lon, lat, zoom = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2, 15.0
    return {"center": [round(lon, 6), round(lat, 6)], "zoom": zoom}


def file_context(root: Path, path: Path) -> dict[str, Any] | None:
    """Whether a PMTiles is a canonical build, and the building_id of each landmark in it."""
    try:
        folder = path.resolve().parent
        if folder.parent.parent != root.resolve():
            return None
    except OSError:
        return None
    data = read_build(folder)
    if not data or data.get("files", {}).get("pmtiles") != path.name:
        return None
    return {"kind": "canonical", "dataset": data["dataset"], "build_id": data["build_id"], "built_at": data.get("built_at"),
            "ids": data.get("landmarks", {}).get("ids", {})}


# ------------------------------------------------------------------ the build

def convert_config(area: Area, root: Path, attribution: str) -> JobConfig:
    """The convert settings of a canonical build: heights are final, tiles follow the VMAP schema."""
    try:
        schema = tileschema.load()
    except tileschema.SchemaError as exc:
        raise ConfigError(str(exc)) from exc
    return JobConfig(
        input_path=Path(), layer=None, geom_column=None, src_crs=WGS84,
        out_dir=area_dir(root, area), out_name=out_name(area), want_gpkg=True, want_pmtiles=True,
        gpkg_layer="buildings", gpkg_crs=WGS84, columns=None, where=None, makevalid=False,
        height={"height_col": "height_m", "prov_col": "height_provenance", "prov_missing": ["default"],
                "m_per_level": 3.5, "default_m": 3.5},
        parts=("building_id", "parent_building_id"),
        tiles=TilesConfig(layer=schema.layer, minzoom=schema.minzoom, maxzoom=schema.maxzoom,
                          attributes=tuple(schema.fields), schema=schema, attribution=attribution),
        category_cols=("height_provenance", "building_tier", "winning_source"),
        report_id_col="building_id", keep_temp=False,
    )


OSM_CSV_COLUMNS = ("osm_id", "kind", "osm_version", "osm_timestamp", "name", "building", "height", "levels",
                   "min_height", "min_level", "tags", "wkt")


def osm_row(f: osmdirect.Feature) -> list[Any]:
    t = f.tags
    name = (t.get("name") or t.get("name:vi") or "").replace("\n", " ").strip() or None
    return [f.building_id, f.kind, f.version, f.timestamp, name, (t.get("building") or t.get("building:part") or None),
            osmtags.parse_length(t.get("height")), osmtags.parse_levels(t.get("building:levels"), minimum=1),
            osmtags.parse_length(t.get("min_height")), osmtags.parse_levels(t.get("building:min_level")),
            json.dumps(t, ensure_ascii=False), osmgeom.to_wkt(f.polygons)]


def find_osm_snapshot(root: Path, bbox: tuple[float, float, float, float]) -> dict[str, Any] | None:
    found = []
    for meta in sources_dir(root, "osm").glob("*/source.json"):
        try:
            data = json.loads(meta.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if gob.covers(data.get("bbox") or [0, 0, 0, 0], bbox) and (meta.parent / data.get("file", "")).is_file():
            found.append({**data, "path": str(meta.parent / data["file"])})
    found.sort(key=lambda d: d.get("created_at") or "")
    return found[-1] if found else None


def list_snapshots(root: Path, name: str, limit: int = 5) -> list[dict[str, Any]]:
    """The snapshots of one source (osm / gob) already on disk, newest first."""
    out = []
    for meta in sources_dir(root, name).glob("*/source.json"):
        try:
            data = json.loads(meta.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if (meta.parent / data.get("file", "")).is_file():
            out.append({k: data.get(k) for k in ("snapshot_id", "source_kind", "rows", "bbox", "osm_base_time",
                                                 "newest_edit")}
                       | {"at": data.get("created_at") or data.get("downloaded_at")})
    out.sort(key=lambda d: d["at"] or "", reverse=True)
    return out[:limit]


def source_id_of(pmtiles: Path, building_id: str) -> str | None:
    """The source_id (OSM way, plus code…) of a building of a canonical build, from its Parquet."""
    import duckdb

    data = read_build(pmtiles.parent)
    if not data:
        return None
    parquet = pmtiles.parent / data["files"]["parquet"]
    with duckdb.connect() as con:
        row = con.execute(f"SELECT source_id FROM read_parquet({sql_str(str(parquet))}) WHERE building_id = ? LIMIT 1",
                          [building_id]).fetchone()
    return row[0] if row else None


class CanonicalPipeline(Pipeline):
    """Sources -> conflation -> canonical Parquet -> the convert pipeline -> canonical.json."""

    handled_errors = (*Pipeline.handled_errors, BaseError, gob.GobError, landmark_library.LibraryError, ValueError)

    def __init__(self, job: Job, registry: FileRegistry, opts: Options, root: Path,
                 library: landmark_library.Library | None = None) -> None:
        attribution = f"{OSM_ATTRIBUTION} · {gob.ATTRIBUTION}" + (" · GlobalBuildingAtlas" if opts.gba_file else "")
        super().__init__(job, registry, convert_config(opts.area, root, attribution))
        self.opts = opts
        self.root = root
        self.library = library or landmark_library.Library(landmark_library.default_dir())
        self.params = conflation.CONFIGS[opts.config_version]
        self.build_id = "build-bldg-" + self.out_dir.name.rsplit("__", 1)[-1]
        self.parquet_out = self.out_dir / f"{out_name(opts.area)}.parquet"
        self.cfg = replace(self.cfg, input_path=self.parquet_out)
        self.stats: dict[str, Any] = {}
        self.sources: dict[str, Any] = {}
        self.sql_log: list[str] = []
        self.aoi_polys: list[osmgeom.Polygon] = []
        self.aoi_bbox: tuple[float, float, float, float] = (0, 0, 0, 0)
        self.load_bbox: tuple[float, float, float, float] = (0, 0, 0, 0)
        self.reg: idregistry.Registry | None = None
        self.members: list[tuple[str, str]] = []
        self.landmark_ids: dict[str, str] = {}
        self.landmark_report: list[dict[str, Any]] = []
        self.review_files: dict[str, str] = {}

    # ---------------------------------------------------------------- plan
    def intro(self) -> list[str]:
        o = self.opts
        return [f"Building Data Studio {__version__} — Canonical Building Data {o.area.label}: {o.aoi_label}",
                f"Build {self.build_id}, cấu hình {o.config_version} ({self.params.params_hash()[:19]}…), "
                f"tier {o.tier_rule}", f"Thư mục output: {self.out_dir}"]

    def plan(self) -> list[Step]:
        o = self.opts
        osm_title = {"overpass": "Tải OSM qua Overpass", "file": f"Đọc OSM từ {o.osm_file.name if o.osm_file else ''}",
                     "snapshot": "Dùng snapshot OSM đã tải"}[o.osm_source]
        steps = [
            Step("aoi", f"Vùng build: {o.aoi_label}"),
            Step("osm", f"Ingest · {osm_title}"),
            Step("gob", "Ingest · Google Open Buildings v3"),
        ]
        if o.gba_file:
            steps.append(Step("gba", f"Ingest · GBA ({o.gba_file.name})"))
        steps += [
            Step("prepare", "Normalize · chuẩn hoá và lọc nguồn"),
            Step("graph", "Entity matching · đồ thị chồng lấn"),
            Step("components", "Conflation · component, footprint thắng, bằng chứng"),
            Step("heights", "Chiều cao: OSM → số tầng × 3,5 m → GBA → 3,5 m"),
            Step("landmarks", "Landmark: giữ building_id và footprint"),
            Step("ids", "building_id ổn định (ID registry)"),
            Step("tier", f"building_tier ({o.tier_rule})"),
            Step("qa", "Kiểm tra invariants"),
            Step("write", "Ghi Parquet canonical, conflation bridge, danh sách review"),
            *Pipeline.plan(self),
            Step("publish", "Lưu build" + (" và đặt làm canonical đang dùng" if o.activate else " (không đặt làm active)")),
        ]
        return steps

    # ---------------------------------------------------------------- helpers
    def _duck(self, sql: str) -> Any:
        import duckdb

        if self.job.runner.cancelled:
            raise tools.CancelledError("Đã huỷ")
        self.sql_log.append(" ".join(sql.split()))
        try:
            return self.con.execute(sql)
        except duckdb.Error as exc:
            if self.job.runner.cancelled:
                raise tools.CancelledError("Đã huỷ") from exc
            raise PipelineError(f"DuckDB: {exc}") from exc

    def _engine(self, fn, *args: Any) -> Any:
        """Run a conflation / tier function on the build's DuckDB, turning its errors into the job's."""
        import duckdb

        if self.job.runner.cancelled:
            raise tools.CancelledError("Đã huỷ")
        try:
            return fn(self.con, *args)
        except duckdb.Error as exc:
            if self.job.runner.cancelled:
                raise tools.CancelledError("Đã huỷ") from exc
            raise PipelineError(f"DuckDB: {exc}") from exc

    def _log(self, line: str) -> None:
        self.job.add_log(line)

    # ---------------------------------------------------------------- Ingest
    def _step_aoi(self, step: Step) -> None:
        import duckdb

        con = duckdb.connect(str(self.work / "canonical.duckdb"))
        self._use_duckdb(con)
        load_extension(con, "spatial")
        self._duck(f"SET temp_directory = {sql_str(str(self.work / 'duckdb_tmp'))}")
        conflation.register_udfs(con)
        o = self.opts
        if o.aoi == "h3":
            self.aoi_polys = self._h3_polygons(o.h3_cells)
        else:
            self.aoi_polys = self._area_boundary()
        # One polygon at a time, then their union: H3 cells share edges, which a single MULTIPOLYGON
        # may not do (ST_MakeValid would turn the shared parts into lines and lose area).
        self._duck("CREATE TABLE aoi_pieces (g GEOMETRY)")
        self.con.executemany("INSERT INTO aoi_pieces SELECT ST_CollectionExtract(ST_MakeValid(ST_GeomFromText(?::VARCHAR)), 3)",
                             [[osmgeom.to_wkt([poly])] for poly in self.aoi_polys])
        self._duck("CREATE TABLE aoi AS SELECT ST_Union_Agg(g) AS geometry FROM aoi_pieces")
        w, s, e, n = osmgeom.bbox(self.aoi_polys)
        self.aoi_bbox = (w, s, e, n)
        self.load_bbox = (round(w - MARGIN_DEG, 6), round(s - MARGIN_DEG, 6), round(e + MARGIN_DEG, 6), round(n + MARGIN_DEG, 6))
        step.detail = f"{o.aoi_label}"
        self._log(f"Vùng build {o.aoi_label}: khung {', '.join(f'{v:.4f}' for v in self.aoi_bbox)}")

    def _h3_polygons(self, cells: tuple[str, ...]) -> list[osmgeom.Polygon]:
        try:
            import h3
        except ImportError as exc:
            raise ConfigError("Thiếu thư viện h3: chạy lại ./run.sh để cài dependency.") from exc
        polys = []
        for cell in cells:
            if not h3.is_valid_cell(cell):
                raise ConfigError(f"Ô H3 không hợp lệ: {cell}")
            ring = [(lng, lat) for lat, lng in h3.cell_to_boundary(cell)]
            polys.append([ring + [ring[0]]])
        return polys

    def _area_boundary(self) -> list[osmgeom.Polygon]:
        """The city boundary: cached, else from Overpass, else the layer of the Hà Nội base."""
        a = self.opts.area
        cache = area_dir(self.root, a) / BOUNDARY_NAME
        if cache.is_file():
            polys = _geojson_polys(json.loads(cache.read_text("utf-8")))
            if polys:
                self._log(f"Ranh giới {a.label}: {cache}")
                return polys
        polys: list[osmgeom.Polygon] = []
        try:
            data = osmdirect.Overpass(self.opts.overpass_urls).query(osmdirect.boundary_query(a.region))
            polys = osmdirect.parse_boundary(data)
            origin = "OSM (Overpass)"
        except BaseError as exc:
            self._log(f"Không lấy được ranh giới từ Overpass: {exc}")
        if not polys:
            base = read_base_manifest(base_default_dir(), a)
            if base:
                row = self.con.execute(f"SELECT ST_AsGeoJSON(ST_Union_Agg(geom)) FROM ST_Read({sql_str(base['gpkg_path'])}, "
                                       "layer = 'boundary')").fetchone()
                polys = _geojson_polys(json.loads(row[0])) if row and row[0] else []
                origin = "base Hà Nội"
        if not polys:
            w, s, e, n = a.search_bbox
            self._log(f"Không có ranh giới {a.region}: dùng khung {a.search_bbox}.")
            return [[[(w, s), (e, s), (e, n), (w, n), (w, s)]]]
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"type": "MultiPolygon", "coordinates": [
            [[list(p) for p in ring] for ring in poly] for poly in polys]}), encoding="utf-8")
        self._log(f"Ranh giới {a.label} lấy từ {origin}, lưu ở {cache}")
        return polys

    def _step_osm(self, step: Step) -> None:
        o = self.opts
        snap = None
        if o.osm_source == "snapshot":
            snap = find_osm_snapshot(self.root, self.load_bbox)
            if snap is None:
                raise PipelineError("Chưa có snapshot OSM nào phủ vùng này: chọn nguồn Overpass hoặc file .osm.pbf.")
            self._log(f"Dùng snapshot OSM {snap['snapshot_id']} ({snap['rows']:,} đối tượng, tạo {snap['created_at']})")
        else:
            snap = self._osm_snapshot(step)
        self.sources["osm"] = {k: v for k, v in snap.items() if k != "path"}
        w, s, e, n = self.load_bbox
        self._duck(f"""CREATE TABLE src_osm AS SELECT * FROM read_parquet({sql_str(snap['path'])})
                       WHERE ST_Intersects(geometry, ST_MakeEnvelope({w}, {s}, {e}, {n}))""")
        rows = self.con.execute("SELECT count(*) FROM src_osm").fetchone()[0]
        step.detail = f"{rows:,} building / khối con · {snap['snapshot_id']}"

    def _osm_snapshot(self, step: Step) -> dict[str, Any]:
        o = self.opts
        stamp = time.strftime("%Y%m%d-%H%M%S")
        snap_id = f"osm-{o.osm_source}-{stamp}"
        folder = sources_dir(self.root, "osm") / snap_id
        folder.mkdir(parents=True, exist_ok=True)
        csv_path = self.work / "osm.csv"
        counters = osmdirect.Counters()
        rows = 0
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(OSM_CSV_COLUMNS)
            for f in self._osm_features(step, counters):
                writer.writerow(osm_row(f))
                rows += 1
        if not rows:
            raise PipelineError("Không có building OSM nào trong vùng.")
        out = folder / "osm.parquet"
        types = {"osm_id": "VARCHAR", "kind": "VARCHAR", "osm_version": "INTEGER", "osm_timestamp": "VARCHAR",
                 "name": "VARCHAR", "building": "VARCHAR", "height": "DOUBLE", "levels": "DOUBLE",
                 "min_height": "DOUBLE", "min_level": "DOUBLE", "tags": "VARCHAR", "wkt": "VARCHAR"}
        cols = "{" + ", ".join(f"'{k}': '{v}'" for k, v in types.items()) + "}"
        self._duck(f"""COPY (SELECT * EXCLUDE (wkt), ST_GeomFromText(wkt) AS geometry
                       FROM read_csv({sql_str(str(csv_path))}, header = true, columns = {cols}, quote = '"', escape = '"')
                       ORDER BY osm_id) TO {sql_str(str(out))} (FORMAT PARQUET, COMPRESSION ZSTD)""")
        csv_path.unlink(missing_ok=True)
        data = {
            "source_id": "osm", "source_kind": o.osm_source, "snapshot_id": snap_id, "role": "canonical",
            "license": "ODbL", "attribution": OSM_ATTRIBUTION, "osm_file": o.osm_file.name if o.osm_file else None,
            "osm_base_time": counters.base_time, "newest_edit": counters.newest_edit, "unclosed": counters.unclosed,
            "tag_issues": len(counters.issues), "bbox": list(self.load_bbox), "rows": rows, "file": out.name,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        (folder / "source.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._log(f"Snapshot {snap_id}: {rows:,} đối tượng OSM (dữ liệu tới {counters.base_time or counters.newest_edit or '?'})")
        return {**data, "path": str(out)}

    def _osm_features(self, step: Step, counters: osmdirect.Counters):
        o = self.opts
        if o.osm_source == "file":
            assert o.osm_file is not None
            conf = self.work / "osmconf.ini"
            conf.write_text(osmdirect.OSMCONF, encoding="utf-8")
            out = self.work / "osm_buildings.geojsonl"
            out.unlink(missing_ok=True)
            self._run(osmdirect.osm_file_command(tools.require("ogr2ogr"), o.osm_file, out, conf, self.load_bbox), step)
            with out.open(encoding="utf-8") as fh:
                yield from osmdirect.parse_geojsonseq(fh, counters)
            out.unlink(missing_ok=True)
            return
        client = osmdirect.Overpass(o.overpass_urls)
        boxes = [b for b in osmgeom.grid(self.load_bbox, osmdirect.TILE_DEG) if osmgeom.box_touches(self.aoi_polys, b)
                 or _box_near(b, self.aoi_bbox)]
        self._log(f"{len(boxes)} ô {osmdirect.TILE_DEG}° (Overpass, tự chia nhỏ nếu máy chủ quá tải)")

        def progress(done: int, total: int) -> None:
            step.progress = 95.0 * done / total
            if done % 20 == 0 or done == total:
                self._log(f"Ô {done}/{total}")

        seen: set[str] = set()
        for data in osmdirect.fetch_tiles(client, boxes, on_tile=progress, cancelled=lambda: self.job.runner.cancelled):
            for f in osmdirect.parse_overpass(data, counters):
                if f.building_id not in seen:  # a feature across two tiles comes twice
                    seen.add(f.building_id)
                    yield f

    def _step_gob(self, step: Step) -> None:
        root = sources_dir(self.root, "gob")
        snap = None if self.opts.gob_refresh else gob.find_snapshot(root, self.load_bbox)
        if snap is not None:
            self._log(f"Dùng snapshot Google Open Buildings {snap['snapshot_id']} ({snap['rows']:,} building, "
                      f"tải {snap['downloaded_at']})")
        else:
            try:
                snap = gob.make_snapshot(self.con, root, self.load_bbox, log=self._log,
                                         on_progress=lambda v: setattr(step, "progress", v),
                                         cancelled=lambda: self.job.runner.cancelled)
            except InterruptedError as exc:
                raise tools.CancelledError("Đã huỷ") from exc
        self.sources["gob"] = {k: v for k, v in snap.items() if k not in ("path", "folder")}
        w, s, e, n = self.load_bbox
        self._duck(f"""CREATE TABLE src_gob AS SELECT plus_code, confidence, geometry FROM read_parquet({sql_str(snap['path'])})
                       WHERE longitude BETWEEN {w} AND {e} AND latitude BETWEEN {s} AND {n}""")
        rows = self.con.execute("SELECT count(*) FROM src_gob").fetchone()[0]
        step.detail = f"{rows:,} footprint · {snap['snapshot_id']}"
        if not self.opts.gba_file:
            self._duck("CREATE TABLE src_gba (gba_id VARCHAR, origin VARCHAR, height DOUBLE, variance DOUBLE, geometry GEOMETRY)")

    def _step_gba(self, step: Step) -> None:
        path = self.opts.gba_file
        assert path is not None
        src = f"read_parquet({sql_str(str(path))})" if path.suffix.lower() == ".parquet" else f"ST_Read({sql_str(str(path))})"
        cols = {r[0]: r[1] for r in self.con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
        lower = {c.lower(): c for c in cols}

        def pick(*names: str) -> str | None:
            return next((lower[n] for n in names if n in lower), None)

        geom = next((c for c, t in cols.items() if t.startswith("GEOMETRY")), None)
        if geom is None and (wkb := pick("geometry", "geom", "wkb_geometry", "geometry_wkb")):
            geom_expr = f"ST_GeomFromWKB({_ident(wkb)})"
        elif geom is not None:
            geom_expr = _ident(geom)
        else:
            raise ConfigError(f"File GBA {path.name} không có cột geometry.")
        height = pick("height", "height_m", "mean_height", "h")
        if height is None:
            raise ConfigError(f"File GBA {path.name} không có cột chiều cao (height).")
        gid, origin = pick("gba_id", "id", "fid", "building_id"), pick("origin_source", "origin", "source", "src", "data_source")
        var = pick("var", "variance", "height_var", "height_variance")
        if origin is None:
            self._log("File GBA không có cột nguồn gốc (origin / source): chỉ dùng làm ứng viên chiều cao, "
                      "không dùng làm footprint gba_ms.")
        w, s, e, n = self.load_bbox
        self._duck(f"""CREATE TABLE src_gba AS
            SELECT {('CAST(' + _ident(gid) + ' AS VARCHAR)') if gid else "'gba' || row_number() OVER ()"} AS gba_id,
                   {('CAST(' + _ident(origin) + ' AS VARCHAR)') if origin else 'NULL::VARCHAR'} AS origin,
                   TRY_CAST({_ident(height)} AS DOUBLE) AS height,
                   {('TRY_CAST(' + _ident(var) + ' AS DOUBLE)') if var else 'NULL::DOUBLE'} AS variance,
                   g AS geometry
            FROM (SELECT *, {geom_expr} AS g FROM {src})
            WHERE ST_Intersects(g, ST_MakeEnvelope({w}, {s}, {e}, {n}))""")
        rows, by = self.con.execute("SELECT count(*), histogram(" + conflation.gba_family_sql("origin") + ") FROM src_gba").fetchone()
        self.sources["gba"] = {"source_id": "global_building_atlas", "file": str(path), "rows": rows,
                               "by_lineage": dict(by or {}), "role": "derived_observation",
                               "lineage": {"upstream_sources": ["osm", "google_open_buildings", "microsoft"],
                                           "independent_observation": False}}
        step.detail = f"{rows:,} bản ghi ({', '.join(f'{k} {v:,}' for k, v in (by or {}).items())})"

    # ---------------------------------------------------------------- conflation
    def _step_prepare(self, step: Step) -> None:
        st = self._engine(conflation.prepare, self.params, self.sql_log)
        self.stats["prepare"] = st
        inv = st["inventory"]
        step.detail = (f"OSM {inv['osm']['in_area']:,} · GOB {inv['gob']['kept']:,}/{inv['gob']['in_area']:,}"
                       + (f" · GBA-ms {inv['gba']['footprints_gba_ms']:,}" if self.opts.gba_file else ""))
        self._log(f"Hệ toạ độ tính diện tích: {st['crs']}; diện tích vùng {st['aoi_km2']:,} km²")
        self._log(f"OSM: {inv['osm']['buildings']:,} building + {inv['osm']['parts']:,} khối con trong vùng "
                  f"({inv['osm']['parts_without_outline']:,} khối con không có building chứa → coi là building)")
        g = inv["gob"]
        self._log(f"GOB: {g['in_area']:,} trong vùng, bỏ {g['dropped_low_confidence']:,} confidence < "
                  f"{self.params.gob_min_confidence}, bỏ {g['dropped_tiny']:,} nhỏ hơn {self.params.min_area_m2:g} m², "
                  f"còn {g['kept']:,}")

    def _step_graph(self, step: Step) -> None:
        st = self._engine(conflation.build_graph, self.params, self.sql_log)
        self.stats["graph"] = st
        step.detail = f"{st['nodes']:,} footprint · {st['pairs']:,} cặp chồng lấn · {st['merge_edges']:,} merge edge"
        self._log(step.detail)

    def _step_components(self, step: Step) -> None:
        st = self._engine(conflation.resolve, self.params, self.work, self.sql_log)
        self.stats["components"] = st
        rel = st["relation_counts"]
        step.detail = ", ".join(f"{k} {v:,}" for k, v in sorted(rel.items()))
        self._log(f"Footprint canonical: {st['canonical_footprints']}; quan hệ: {rel}")
        self._log(f"Bỏ vì chồng một phần lên footprint nguồn cao hơn: {st['dropped_partial_overlap'] or 0}; "
                  f"ngoài vùng (chỉ nạp để xử lý mép): {st['outside_area']:,}")

    def _step_heights(self, step: Step) -> None:
        st = self._engine(conflation.heights, self.params, self.sql_log)
        self.stats["heights"] = st
        step.detail = ", ".join(f"{k} {v:,}" for k, v in sorted(st["height_provenance"].items()))
        self._log(f"Nguồn chiều cao: {st['height_provenance']}")

    # ---------------------------------------------------------------- landmarks and ids
    def _load_registry(self) -> idregistry.Registry:
        path = area_dir(self.root, self.opts.area) / idregistry.FILE_NAME
        reg = idregistry.Registry.load(self.con, path)
        self._log(f"ID registry: {len(reg):,} khoá ({path})")
        if self.opts.seed_file:
            try:
                st = reg.seed(self.con, self.opts.seed_file)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            self._log(f"Giữ building_id của {self.opts.seed_file.name}: thêm {st['added']:,} khoá "
                      f"(trên {st['rows']:,} dòng, bỏ {st['skipped']:,} dòng không có source_id OSM / plus code)")
            self.stats["seed"] = {"file": str(self.opts.seed_file), **st}
        return reg

    def _step_landmarks(self, step: Step) -> None:
        """Find the building of every landmark in this build, lock its id and footprint."""
        self.reg = self._load_registry()
        p = self.params
        crs = self.stats["prepare"]["crs"]
        to_m = f"'EPSG:4326', '{crs}', always_xy := true"
        self._duck("CREATE TABLE lm (lid VARCHAR, name VARCHAR, uuid VARCHAR, osm_id VARCHAR, height DOUBLE, fp VARCHAR)")
        rows = []
        for rec in self.library.records():
            bid, sid = str(rec.get("building_id") or ""), str(rec.get("source_id") or "")
            uuid = bid if idregistry.UUID_RE.match(bid) else None
            osm_id = sid if idregistry.OSM_ID_RE.match(sid) else (bid if idregistry.OSM_ID_RE.match(bid) else None)
            fp = rec.get("footprint") or {}
            if not fp.get("geometry"):
                continue
            rows.append((rec["id"], rec.get("name") or "", uuid, osm_id, fp.get("height"), json.dumps(fp["geometry"])))
        if rows:
            self.con.executemany("INSERT INTO lm VALUES (?, ?, ?, ?, ?, ?)", rows)
        self._duck(f"""CREATE TABLE lm_in AS
            SELECT l.*, ST_Transform(ST_MakeValid(ST_GeomFromGeoJSON(fp)), {to_m}) AS gm FROM lm AS l""")
        self._duck("""DELETE FROM lm_in WHERE lid NOT IN (SELECT DISTINCT l.lid FROM lm_in AS l
                      JOIN core_parts AS a ON ST_Intersects(ST_PointOnSurface(l.gm), a.g))""")
        self._duck("CREATE TABLE landmark_lock (node BIGINT, lid VARCHAR, locked_id VARCHAR, how VARCHAR)")
        self._duck("CREATE TABLE lm_issues (lid VARCHAR, name VARCHAR, issue_type VARCHAR, node BIGINT, note VARCHAR, geometry GEOMETRY)")
        by_uuid: dict[str, list[str]] = {}
        wanted = {r[2] for r in rows if r[2]}
        for key, bid in self.reg.ids.items():
            if bid in wanted:
                by_uuid.setdefault(bid, []).append(key)
        landmarks = self.con.execute("SELECT lid, name, uuid, osm_id, height FROM lm_in ORDER BY lid").fetchall()
        next_node = self.con.execute("SELECT max(node) FROM obs").fetchone()[0] + 1_000_000
        for lid, name, uuid, osm_id, height in landmarks:
            keys = by_uuid.get(uuid, []) if uuid else []
            if osm_id:
                keys.append(f"osm:{osm_id}")
            node, how = None, None
            if keys:
                arr = "[" + ", ".join(sql_str(k) for k in keys) + "]"
                hit = self.con.execute(f"""SELECT node FROM canon WHERE kind = 'building'
                    AND (list_contains({arr}, key) OR list_has_any(evidence_keys, {arr}::VARCHAR[]))
                    ORDER BY list_contains({arr}, key) DESC, node LIMIT 1""").fetchone()
                if hit:
                    node, how = hit[0], "id"
            if node is None:
                hit = self.con.execute(f"""SELECT c.node, ST_Area(ST_Intersection(c.gm, l.gm)) / ST_Area(ST_Union(c.gm, l.gm)) AS iou
                    FROM canon AS c, lm_in AS l WHERE l.lid = ? AND c.kind = 'building' AND ST_Intersects(c.gm, l.gm)
                    ORDER BY iou DESC LIMIT 1""", [lid]).fetchone()
                if hit and hit[1] >= p.one_to_one_min_iou:
                    node, how = hit[0], f"footprint IoU {hit[1]:.2f}"
            locked = uuid or (self.reg.ids.get(f"osm:{osm_id}") if osm_id else None)
            entry = {"id": lid, "name": name, "osm_id": osm_id, "previous_building_id": uuid}
            if node is not None:
                iou = self.con.execute("""SELECT ST_Area(ST_Intersection(c.gm, l.gm)) / ST_Area(ST_Union(c.gm, l.gm))
                    FROM canon AS c, lm_in AS l WHERE c.node = ? AND l.lid = ?""", [node, lid]).fetchone()[0]
                if iou < 0.9:  # the source footprint moved: the landmark keeps the footprint it stands on
                    self.con.execute(f"""UPDATE canon SET gm = l.gm, g4 = ST_Transform(l.gm, '{crs}', 'EPSG:4326', always_xy := true),
                        area = ST_Area(l.gm), reasons = list_append(reasons, 'landmark_footprint_kept')
                        FROM lm_in AS l WHERE canon.node = ? AND l.lid = ?""", [node, lid])
                    self.con.execute("""INSERT INTO lm_issues SELECT ?, ?, 'landmark_footprint_changed', ?, ?, g4
                        FROM canon WHERE node = ?""", [lid, name, node, f"IoU footprint mới / footprint landmark {iou:.2f}", node])
                    entry["warning"] = f"Footprint nguồn đã đổi (IoU {iou:.2f}): giữ footprint của landmark."
                self.con.execute("INSERT INTO landmark_lock VALUES (?, ?, ?, ?)", [node, lid, locked, how])
                entry.update(found=how, footprint_iou=round(iou, 3))
            else:
                node = next_node
                next_node += 1
                hv = height if height and p.min_height_m <= height <= p.max_height_m else None
                self.con.execute(f"""INSERT INTO canon BY NAME
                    SELECT ? AS node, 'landmark' AS family, 0 AS rank, 'building' AS kind, ? AS source_id, ? AS key,
                           bds_uuid(?) AS observation_id, 'landmark' AS version, ST_Area(gm) AS area, gm,
                           ST_Transform(gm, '{crs}', 'EPSG:4326', always_xy := true) AS g4, 'single_source' AS relation,
                           ['landmark_lost_kept_footprint'] AS reasons, ? AS name, []::VARCHAR[] AS evidence_keys,
                           []::VARCHAR[] AS evidence_observation_ids,
                           ? AS height_provenance, ? AS height_m, ? AS height_lineage
                    FROM lm_in WHERE lid = ?""",
                                 [node, osm_id or lid, f"landmark:{lid}", f"landmark:{lid}", name,
                                  "landmark" if hv else "default", hv or p.default_height_m,
                                  f"landmark:{lid}" if hv else f"default:{p.default_height_m:g}m", lid])
                self.con.execute("INSERT INTO landmark_lock VALUES (?, ?, ?, 'lost')", [node, lid, locked])
                self.con.execute("""INSERT INTO lm_issues SELECT ?, ?, 'landmark_lost', ?,
                    'Không còn building nguồn nào khớp: giữ footprint cũ của landmark', g4 FROM canon WHERE node = ?""",
                                 [lid, name, node, node])
                entry.update(found=None, warning="Không tìm thấy building trong build này: giữ footprint cũ của landmark.")
            self.landmark_report.append(entry)
        outside = len(rows) - len(landmarks)
        warn = [e for e in self.landmark_report if e.get("warning")]
        step.detail = f"{len(landmarks)} landmark trong vùng" + (f", {len(warn)} cảnh báo" if warn else "")
        for e in self.landmark_report:
            self._log(f"Landmark {e['name'] or e['id']}: " + (f"khớp theo {e['found']}" if e.get("found") else "MẤT building")
                      + (f" — CẢNH BÁO: {e['warning']}" if e.get("warning") else ""))
        if outside:
            self._log(f"{outside} landmark nằm ngoài vùng build (không xét).")

    def _step_ids(self, step: Step) -> None:
        assert self.reg is not None
        rows = self.con.execute("""SELECT c.node, c.key, c.evidence_keys, l.locked_id FROM canon AS c
                                   LEFT JOIN landmark_lock AS l USING (node) ORDER BY c.key""").fetchall()
        ids, st = self.reg.assign((n, k, list(ev or []), lk) for n, k, ev, lk in rows)
        path = self.work / "ids.csv"
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["node", "building_id"])
            w.writerows(ids.items())
        self._duck(f"""CREATE TABLE ids AS SELECT * FROM read_csv({sql_str(str(path))}, header = true,
                       columns = {{'node': 'BIGINT', 'building_id': 'VARCHAR'}})""")
        self.members = [(k, ids[n]) for n, k, ev, _ in rows for k in [k, *(ev or [])]]
        for lid, node in self.con.execute("SELECT lid, node FROM landmark_lock").fetchall():
            self.landmark_ids[lid] = ids[node]
        for e in self.landmark_report:
            e["building_id"] = self.landmark_ids.get(e["id"])
        self.stats["ids"] = st
        step.detail = f"giữ {st['kept']:,} · kế thừa {st['inherited']:,} · mới {st['new']:,} · landmark {st['locked']}"
        self._log(f"building_id: {step.detail}")

    def _step_tier(self, step: Step) -> None:
        crs = self.stats["prepare"]["crs"]
        to_m = f"'EPSG:4326', '{crs}', always_xy := true"
        has_pins = ICONIC_PINS.is_file()
        if has_pins:
            self._duck(f"""CREATE TABLE tier_pins AS SELECT row_number() OVER () AS pin_id, name,
                           ST_Transform(geom, {to_m}) AS gm FROM ST_Read({sql_str(str(ICONIC_PINS))})""")
        has_poi = self.opts.poi_file is not None
        if has_poi:
            w, s, e, n = self.load_bbox
            src = sql_str(str(self.opts.poi_file))
            cols = {r[0] for r in self.con.execute(f"DESCRIBE SELECT * FROM read_parquet({src})").fetchall()}
            if not {"latitude", "longitude", "place_category_code"} <= cols:
                raise ConfigError("File POI cần cột latitude, longitude, place_category_code.")
            deleted = "AND deleted_at IS NULL" if "deleted_at" in cols else ""
            self._duck(f"""CREATE TABLE tier_poi AS SELECT ST_Transform(ST_Point(longitude, latitude), {to_m}) AS gm
                           FROM read_parquet({src}) WHERE ({buildingtier.poi_where()}) {deleted}
                             AND longitude BETWEEN {w} AND {e} AND latitude BETWEEN {s} AND {n}""")
        st = self._engine(lambda con: buildingtier.apply(con, self.opts.tier_rule, has_pins=has_pins, has_poi=has_poi))
        st["iconic_pins"] = str(ICONIC_PINS) if has_pins else None
        st["poi_file"] = str(self.opts.poi_file) if has_poi else None
        self.stats["tier"] = st
        step.detail = ", ".join(f"{k} {v:,}" for k, v in st["counts"].items())
        self._log(f"building_tier ({st['rule']}): {st['counts']}; cờ: {st['flags']}")

    def _step_qa(self, step: Step) -> None:
        p = self.params
        checks = [
            ("Geometry hợp lệ, không rỗng", "SELECT count(*) FROM canon WHERE g4 IS NULL OR ST_IsEmpty(g4) OR NOT ST_IsValid(g4)"),
            ("building_id không trùng", "SELECT count(*) - count(DISTINCT building_id) FROM ids"),
            ("Mỗi bản ghi nguồn thuộc tối đa 1 canonical",
             "SELECT count(*) FROM (SELECT k FROM (SELECT unnest(list_prepend(key, evidence_keys)) AS k FROM canon) "
             "GROUP BY k HAVING count(*) > 1)"),
            ("one_to_one luôn có group IoU ≥ 0,50",
             f"SELECT count(*) FROM canon WHERE relation = 'one_to_one' AND coalesce(group_iou, 0) < {p.one_to_one_min_iou}"),
            ("Coverage gap có reason", "SELECT count(*) FROM canon c JOIN groups g USING (gid) WHERE g.gap "
                                       "AND NOT list_contains(c.reasons, 'coverage_gap_in_component')"),
            (f"Chiều cao trong {p.min_height_m:g}–{p.max_height_m:g} m",
             f"SELECT count(*) FROM canon WHERE height_m IS NULL OR height_m < {p.min_height_m} OR height_m > {p.max_height_m}"),
            ("Khối con có building cha", "SELECT count(*) FROM canon c WHERE kind = 'part' AND NOT EXISTS "
                                         "(SELECT 1 FROM canon b WHERE b.key = 'osm:' || c.parent_source_id)"),
            ("min_height < height_m", "SELECT count(*) FROM canon WHERE min_height_m >= height_m"),
            ("Mỗi canonical có building_id", "SELECT count(*) FROM canon WHERE node NOT IN (SELECT node FROM ids)"),
        ]
        results, failed = [], []
        for name, sql in checks:
            bad = self._duck(sql).fetchone()[0]
            results.append({"check": name, "violations": bad, "ok": bad == 0})
            if bad:
                failed.append(f"{name}: {bad:,}")
        self.stats["invariants"] = results
        if failed:
            raise PipelineError("Invariant không đạt: " + "; ".join(failed))
        step.detail = f"{len(results)}/{len(results)} đạt"

    # ---------------------------------------------------------------- outputs
    def _step_write(self, step: Step) -> None:
        o, p = self.opts, self.params
        ws = "CASE c.family " + " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in conflation.WINNING_SOURCE.items()) + " END"
        now = time.strftime("%Y-%m-%d %H:%M:%S%z")
        t0 = time.time()
        self._duck("""CREATE TABLE comp_keys AS
            SELECT g.comp, bds_uuid('component:' || min(o.key)) AS group_id
            FROM membership AS m JOIN groups AS g USING (gid) JOIN obs AS o USING (node) GROUP BY g.comp""")
        # building:part -> the building_id of its building (equi-joins only: they stay hash joins at city scale)
        self._duck("""CREATE TABLE parent_ids AS
            SELECT c.node, pi.building_id AS parent_building_id
            FROM (SELECT node, 'osm:' || parent_source_id AS parent_key FROM canon WHERE kind = 'part') AS c
            JOIN canon AS pc ON pc.key = c.parent_key JOIN ids AS pi ON pi.node = pc.node""")
        self._duck(f"""CREATE TABLE final AS
            SELECT i.building_id, c.source_id, c.g4 AS geometry, c.height_m, c.height_provenance, c.height_lineage,
                   {ws} AS winning_source, c.observation_id AS winning_observation_id, 'active' AS status,
                   strptime({sql_str(now)}, '%Y-%m-%d %H:%M:%S%z') AS updated_at,
                   CASE WHEN l.node IS NOT NULL THEN 'manual' ELSE 'auto' END AS resolution_mode,
                   CASE WHEN l.node IS NOT NULL THEN 'landmark' ELSE 'auto' END AS decided_by,
                   {sql_str(self.build_id)} AS build_id,
                   CAST(c.min_height_m AS VARCHAR) AS min_height,
                   'auto' AS updated_by, pp.parent_building_id, t.building_tier,
                   CAST(round(c.confidence, 4) AS VARCHAR) AS confidence, NULL::VARCHAR AS superseded_by,
                   gk.group_id, {sql_str(o.config_version)} AS config_version,
                   'Rule ' || {sql_str(o.config_version)} || ', build ' || {sql_str(self.build_id)} || ': footprint from '
                     || {ws} || ', height from ' || c.height_provenance AS description,
                   c.name, c.levels AS building_levels, round(c.area, 1) AS footprint_area_m2,
                   c.relation AS conflation_relation_type, round(c.group_iou, 3) AS group_iou,
                   to_json(list_prepend(c.key, c.evidence_keys))::VARCHAR AS source_ids,
                   array_to_string(c.reasons, ',') AS conflation_reasons, array_to_string(t.tier_flags, ',') AS tier_flags
            FROM canon AS c JOIN ids AS i USING (node) LEFT JOIN landmark_lock AS l USING (node) LEFT JOIN tier AS t USING (node)
            LEFT JOIN parent_ids AS pp USING (node) LEFT JOIN comp_keys AS gk ON gk.comp = c.comp""")
        self._log(f"Bảng canonical: {time.time() - t0:.0f}s")
        self.parquet_out.unlink(missing_ok=True)
        self._duck(f"COPY (SELECT * FROM final ORDER BY winning_source, source_id) TO {sql_str(str(self.parquet_out))} "
                   "(FORMAT PARQUET, COMPRESSION ZSTD)")
        self._add_output("parquet", "Canonical Parquet", self.parquet_out, "data")
        self._log(f"Parquet: {self.parquet_out.stat().st_size / 1e6:,.0f} MB ({time.time() - t0:.0f}s)")
        counts = dict(self.con.execute("SELECT winning_source, count(*) FROM final GROUP BY 1").fetchall())
        self.stats["counts"] = {"total": sum(counts.values()), "by_winning_source": counts,
                                "parts": self.con.execute("SELECT count(*) FROM final WHERE parent_building_id IS NOT NULL").fetchone()[0]}
        # Conflation bridge: every cross-family overlap, merge edge or not.
        bridge = self.out_dir / "conflation_bridge.parquet"
        self._duck(f"""COPY (
            SELECT oa.key AS a_key, ob.key AS b_key, round(p.inter, 2) AS intersection_m2, round(p.iou, 4) AS iou,
                   round(p.cov_a, 4) AS coverage_a, round(p.cov_b, 4) AS coverage_b, round(p.area_sim, 4) AS area_similarity,
                   round(p.cdist, 2) AS centroid_distance_m, round(p.match_score, 4) AS match_score, p.is_merge,
                   gk.group_id
            FROM pairs AS p JOIN obs AS oa ON oa.node = p.a JOIN obs AS ob ON ob.node = p.b
            LEFT JOIN membership AS m ON m.node = p.a LEFT JOIN groups AS g ON g.gid = m.gid
            LEFT JOIN comp_keys AS gk ON gk.comp = g.comp
            QUALIFY row_number() OVER (PARTITION BY p.a, p.b ORDER BY m.role DESC) = 1
            ORDER BY a_key, b_key) TO {sql_str(str(bridge))} (FORMAT PARQUET, COMPRESSION ZSTD)""")
        self._add_output("bridge", "Conflation bridge (Parquet)", bridge, "data")
        self._log(f"Conflation bridge ({time.time() - t0:.0f}s)")
        self._write_review()
        step.detail = f"{self.stats['counts']['total']:,} building"
        self.con.close()
        self.con = None

    def _write_review(self) -> None:
        """MapOps lists: one GeoPackage per source, the footprints of every case to review."""
        issue_counts = self._engine(conflation.issues, self.params, self.sql_log)
        sev = "CASE issue_type " + " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in conflation.ISSUE_SEVERITY.items()) + " ELSE 'low' END"
        self._duck(f"""CREATE TABLE review AS
            SELECT o.family, i.issue_type, {sev} AS severity, gk.group_id, i.relation AS relation_type,
                   round(i.group_iou, 3) AS group_iou, i.role, o.source_id, wid.building_id AS canonical_building_id,
                   wo.source_id AS winner_source_id, CAST(round(o.confidence, 4) AS VARCHAR) AS confidence,
                   round(o.area, 1) AS area_m2, NULL::VARCHAR AS note, o.g4 AS geometry
            FROM issues AS i JOIN obs AS o ON o.node = i.node JOIN ids AS wid ON wid.node = i.winner
            LEFT JOIN obs AS wo ON wo.node = i.winner LEFT JOIN comp_keys AS gk ON gk.comp = i.comp
            UNION ALL
            SELECT 'osm', li.issue_type, {sev.replace('issue_type', 'li.issue_type')}, NULL, 'landmark', NULL, 'landmark',
                   li.lid, i2.building_id, NULL, NULL, NULL, li.note, li.geometry
            FROM lm_issues AS li LEFT JOIN ids AS i2 ON i2.node = li.node""")
        self.stats["review"] = {"issues": issue_counts,
                                "rows": dict(self.con.execute("SELECT family, count(*) FROM review GROUP BY 1").fetchall())}
        names = {"osm": "review_osm.gpkg", "gob": "review_gob.gpkg", "gba_ms": "review_gba.gpkg"}
        for fam, name in names.items():
            if fam == "gba_ms" and not self.opts.gba_file:
                continue
            path = self.out_dir / name
            path.unlink(missing_ok=True)
            n = self.con.execute("SELECT count(*) FROM review WHERE family = ?", [fam]).fetchone()[0]
            if n:
                self._duck(f"""COPY (SELECT * EXCLUDE (family) FROM review WHERE family = '{fam}'
                               ORDER BY severity, issue_type, group_id, role DESC)
                               TO {sql_str(str(path))} WITH (FORMAT GDAL, DRIVER 'GPKG', SRS 'EPSG:4326', LAYER_NAME 'review')""")
                self._add_output(f"review_{fam}", f"Danh sách review {fam.upper()} (GPKG)", path, "data")
                self.review_files[fam] = name
            self._log(f"Review {fam}: {n:,} footprint" + (f" → {name}" if n else ""))

    def _step_publish(self, step: Step) -> None:
        o = self.opts
        report = self.job.report or {}
        files = {"parquet": self.parquet_out.name, "gpkg": self.gpkg_out.name, "pmtiles": self.pmtiles_out.name,
                 "bridge": "conflation_bridge.parquet", "report": "canonical_report.md",
                 **{f"review_{k}": v for k, v in self.review_files.items()}}
        build = {
            "build_id": self.build_id, "dataset": o.area.key, "label": o.area.label,
            "aoi": {"mode": o.aoi, "label": o.aoi_label, "cells": list(o.h3_cells),
                    "km2": self.stats["prepare"]["aoi_km2"], "bbox": [round(v, 6) for v in self.aoi_bbox]},
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "app_version": __version__,
            "config_version": o.config_version, "params_hash": self.params.params_hash(), "params": self.params.as_dict(),
            "tier_rule": o.tier_rule, "tile_schema": "vmap-buildings",
            "schemas": {"canonical_building": "platform canonical_building + height_lineage"},
            "sources": self.sources, "counts": self.stats.get("counts"),
            "height": {k: (report.get("height") or {}).get(k) for k in ("real_count", "real_pct")},
            "stats": {k: v for k, v in self.stats.items() if k != "counts"},
            "landmarks": {"ids": self.landmark_ids, "report": self.landmark_report},
            "files": files, "activated": o.activate,
        }
        (self.out_dir / BUILD_NAME).write_text(json.dumps(build, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        md = render_report(build)
        (self.out_dir / "canonical_report.md").write_text(md, encoding="utf-8")
        self._add_output("canonical_report", "Báo cáo canonical (Markdown)", self.out_dir / "canonical_report.md", "report")
        self._add_output("build", "build.json", self.out_dir / BUILD_NAME, "report")
        # Ids are only handed out for good once the build succeeded.
        import duckdb

        assert self.reg is not None
        self.reg.update(self.members)
        with duckdb.connect() as con:
            self.reg.save(con, self.build_id)
        self._log(f"ID registry: {len(self.reg):,} khoá")
        for lid, bid in self.landmark_ids.items():
            entry = next((e for e in self.landmark_report if e["id"] == lid), {})
            self.library.bind(lid, dataset=o.area.key, building_id=bid, source_id=entry.get("osm_id"),
                              build_id=self.build_id)
        if self.landmark_ids:
            self._log(f"Gắn {len(self.landmark_ids)} landmark với building_id của canonical {o.area.key}.")
        if o.activate:
            path = write_active(self.root, o.area, self.out_dir, build)
            self._add_output("active", ACTIVE_NAME, path, "report")
            self._log(f"Canonical {o.area.label} đang dùng: {self.out_dir.name}")
        step.detail = self.build_id


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _box_near(box: tuple[float, float, float, float], bbox: tuple[float, float, float, float]) -> bool:
    """An Overpass tile fully inside a small area (an H3 set smaller than a tile) also counts."""
    w, s, e, n = box
    return w <= bbox[0] and s <= bbox[1] and e >= bbox[2] and n >= bbox[3]


def _geojson_polys(geom: dict[str, Any]) -> list[osmgeom.Polygon]:
    if geom.get("type") == "Polygon":
        raw = [geom["coordinates"]]
    elif geom.get("type") == "MultiPolygon":
        raw = geom["coordinates"]
    else:
        return []
    return [[[(p[0], p[1]) for p in ring] for ring in poly] for poly in raw]


def render_report(b: dict[str, Any]) -> str:
    """canonical_report.md: what was built, from which sources, with which rules, and the QA."""
    st = b["stats"]
    inv = st["prepare"]["inventory"]
    lines = [f"# Canonical Building Data — {b['label']} · {b['build_id']}", "",
             f"- Vùng: **{b['aoi']['label']}** ({b['aoi']['km2']:,} km²)",
             f"- Cấu hình conflation: `{b['config_version']}` (`{b['params_hash']}`) · tier `{b['tier_rule']}` · "
             f"schema tile `{b['tile_schema']}`",
             f"- Build lúc {b['built_at']} · app {b['app_version']} · "
             + ("**đặt làm canonical đang dùng**" if b["activated"] else "không đặt làm active"), "",
             "## Nguồn", "", "| Nguồn | Snapshot | Bản ghi | Ghi chú |", "|---|---|---:|---|"]
    for key, s in b["sources"].items():
        lines.append(f"| {key} | {s.get('snapshot_id') or s.get('file') or ''} | {s.get('rows', 0):,} | "
                     f"{s.get('source_kind') or s.get('dataset_version') or ''} {s.get('osm_base_time') or s.get('downloaded_at') or ''} |")
    g = inv["gob"]
    lines += ["", "## Normalize", "",
              f"- OSM: {inv['osm']['buildings']:,} building + {inv['osm']['parts']:,} khối con trong vùng",
              f"- GOB: {g['in_area']:,} trong vùng → bỏ {g['dropped_low_confidence']:,} confidence thấp, "
              f"{g['dropped_tiny']:,} quá nhỏ → còn **{g['kept']:,}**",
              f"- GBA: {inv['gba']['in_area']:,} bản ghi, {inv['gba']['footprints_gba_ms']:,} footprint gba_ms, "
              f"ứng viên chiều cao {inv['gba']['height_candidates']}", "",
              "## Entity matching và conflation", ""]
    gr, co = st["graph"], st["components"]
    lines += [f"- {gr['nodes']:,} footprint, {gr['pairs']:,} cặp chồng lấn khác nguồn, {gr['merge_edges']:,} merge edge, "
              f"{gr['slivers']:,} sliver",
              f"- {co['components_multi']:,} component nhiều footprint (lớn nhất {co['largest_component']:,})",
              f"- Bỏ vì chồng một phần lên nguồn cao hơn: {co['dropped_partial_overlap'] or 0}",
              "", "| Quan hệ | Canonical |", "|---|---:|"]
    lines += [f"| {k} | {v:,} |" for k, v in sorted(co["relation_counts"].items())]
    c = b["counts"]
    lines += ["", "## Kết quả", "", f"**{c['total']:,}** building ({c['parts']:,} khối con)", "",
              "| winning_source | Số |", "|---|---:|"]
    lines += [f"| {k} | {v:,} |" for k, v in sorted(c["by_winning_source"].items())]
    lines += ["", "| height_provenance | Số |", "|---|---:|"]
    lines += [f"| {k} | {v:,} |" for k, v in sorted(st["heights"]["height_provenance"].items())]
    lines += ["", f"| building_tier ({st['tier']['rule']}) | Số |", "|---|---:|"]
    lines += [f"| {k} | {v:,} |" for k, v in st["tier"]["counts"].items()]
    ids = st["ids"]
    lines += ["", f"building_id: giữ {ids['kept']:,} · kế thừa từ bằng chứng {ids['inherited']:,} · mới {ids['new']:,} · "
              f"landmark {ids['locked']}", "", "## Invariants", "", "| Kiểm tra | Vi phạm |", "|---|---:|"]
    lines += [f"| {r['check']} | {'✔' if r['ok'] else r['violations']} |" for r in st["invariants"]]
    lines += ["", "## Review MapOps", ""]
    lines += [f"- {k}: {v:,} nhóm" for k, v in sorted(st["review"]["issues"].items())] or ["- Không có"]
    lines += [f"- File: {', '.join(v for k, v in b['files'].items() if k.startswith('review_')) or 'không có'}", "",
              "## Landmark", ""]
    rep = b["landmarks"]["report"]
    if not rep:
        lines.append("Không có landmark trong vùng.")
    for e in rep:
        lines.append(f"- {e['name'] or e['id']} → `{e.get('building_id')}`"
                     + (f" (khớp theo {e['found']})" if e.get("found") else "")
                     + (f" — **cảnh báo:** {e['warning']}" if e.get("warning") else ""))
    return "\n".join(lines) + "\n"


def execute(job: Job, registry: FileRegistry, root: Path | None = None,
            library: landmark_library.Library | None = None) -> None:
    """Entry point used by the JobManager worker thread."""
    run_job(job, lambda: CanonicalPipeline(job, registry, parse_options(job.config), root or default_dir(), library))


def main() -> None:
    """python -m app.canonical [--h3 hanoi-48 | --cells …] [--osm overpass|snapshot|<file.pbf>] [--gba file] …"""
    import argparse
    import threading

    ap = argparse.ArgumentParser(description="Build Canonical Building Data (OSM + Google Open Buildings [+ GBA])")
    ap.add_argument("area", nargs="?", default=DEFAULT_AREA, choices=sorted(AREAS))
    ap.add_argument("--h3", choices=sorted(H3_PRESETS), help="Chỉ build bộ ô H3 này (mặc định: cả thành phố)")
    ap.add_argument("--cells", help="Danh sách ô H3 (cách nhau bằng dấu phẩy / khoảng trắng)")
    ap.add_argument("--osm", default="overpass", help="overpass | snapshot | đường dẫn .osm.pbf")
    ap.add_argument("--gob-refresh", action="store_true", help="Tải lại Google Open Buildings")
    ap.add_argument("--gba", help="File Global Building Atlas (tuỳ chọn)")
    ap.add_argument("--poi", help="File POI Parquet cho luật tier 4 (tuỳ chọn)")
    ap.add_argument("--seed", help="File canonical production để giữ building_id (vd canonical_building_v7….parquet)")
    ap.add_argument("--no-activate", action="store_true", help="Không đặt build làm canonical đang dùng")
    ap.add_argument("--dir", type=Path, help=f"Thư mục canonical (mặc định {default_dir()})")
    args = ap.parse_args()
    osm_source = args.osm if args.osm in ("overpass", "snapshot") else "file"
    config = {"area": args.area, "aoi": "h3" if (args.h3 or args.cells) else "area", "h3_preset": args.h3,
              "h3_cells": args.cells, "osm_source": osm_source, "osm_file": args.osm if osm_source == "file" else None,
              "gob_refresh": args.gob_refresh, "gba_file": args.gba, "poi_file": args.poi, "seed_file": args.seed,
              "activate": not args.no_activate}
    job = Job(id="cli", name="canonical", kind="canonical", config=config)
    printed = 0

    def show() -> None:
        nonlocal printed
        with job.lock:
            lines = job.log[printed:]
            printed += len(lines)
        for line in lines:
            print(line, flush=True)

    worker = threading.Thread(target=execute, args=(job, FileRegistry(), args.dir), daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.5)
            show()
    except KeyboardInterrupt:
        job.runner.cancel()
        worker.join()
    show()
    if job.status != "done":
        raise SystemExit(f"Build canonical không thành công: {job.error or job.status}")


if __name__ == "__main__":
    main()
