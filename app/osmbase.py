"""Base dataset: the OpenStreetMap buildings of a whole city, ready in the 3D preview (and for
registering landmarks on) without uploading or converting a file.

    Overture Maps release (public S3 bucket, GeoParquet)
      ├─ divisions / division_area ──► city boundary (one polygon)
      └─ buildings / building + building_part, OpenStreetMap only, inside the boundary
           └─ DuckDB (httpfs + spatial) ──► raw GeoPackage (_work/)
                └─ convert pipeline ──► <area>_osm_buildings.gpkg (h_m, has_parts…, + layer boundary)
                                        + <area>_osm_buildings.pmtiles
                     └─ base/<area>/base.json: the build in use

Overture republishes OSM every month with a fixed schema (`height`, `num_floors`, `min_height`
parsed from the OSM tags) and records, per feature, which dataset its geometry came from. Only
features whose geometry comes from OpenStreetMap are kept (Overture adds Google / Microsoft ML
footprints elsewhere), and each keeps its OSM id (`way/123`) as `building_id`.

Every build is a new folder; base.json is only rewritten once a build succeeds, so a failed or
cancelled rebuild leaves the previous base in place.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__, net, tools
from .jobs import FileRegistry, Job, Step
from .pipeline import (
    WGS84,
    ConfigError,
    JobConfig,
    Pipeline,
    PipelineError,
    TilesConfig,
    run_job,
)
from .sqlbuild import sql_str

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DIR = APP_DIR / "base"
MANIFEST_NAME = "base.json"

S3_BUCKET = "overturemaps-us-west-2"
S3_REGION = "us-west-2"
DEFAULT_ROOT = f"s3://{S3_BUCKET}/release"
LIST_URL = f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/?list-type=2&prefix=release/&delimiter=/"
OSM_DATASET = "OpenStreetMap"
M_PER_LEVEL = 3.5  # the convert default; also turns min_level into min_height
TILE_ATTRIBUTES = ("building_id", "h_m", "h_src", "min_height", "has_parts", "is_part", "parent_building_id",
                   "name", "class", "height", "num_floors")
OUTPUT_COLUMNS = ("building_id", "parent_building_id", "name", "class", "subtype", "height", "num_floors",
                  "min_height", "min_floor", "roof_shape", "roof_height", "roof_color", "roof_material",
                  "facade_color", "facade_material", "osm_version", "osm_timestamp", "overture_id")
_RELEASE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")


class BaseError(RuntimeError):
    """The base could not be built (message is shown to the user)."""


@dataclass(frozen=True)
class Area:
    """A city whose OSM buildings make a base."""

    key: str
    label: str
    country: str  # ISO 3166-1
    region: str  # ISO 3166-2 of the division_area that bounds it
    search_bbox: tuple[float, float, float, float]  # w, s, e, n: generous box around the boundary
    view: tuple[float, float, float]  # lon, lat, zoom where the 3D preview opens


AREAS = {
    "hanoi": Area("hanoi", "Hà Nội", "VN", "VN-HN", (105.2, 20.5, 106.1, 21.45), (105.8522, 21.0285, 15.5)),
}
DEFAULT_AREA = "hanoi"


def default_dir() -> Path:
    return Path(os.environ.get("BDS_BASE_DIR") or DEFAULT_DIR).expanduser()


def area_dir(base_dir: Path, area: Area) -> Path:
    return base_dir / area.key


def out_name(area: Area) -> str:
    return f"{area.key}_osm_buildings"


# ------------------------------------------------------------------ Overture release

def releases(root: str = DEFAULT_ROOT) -> list[str]:
    """Overture releases under root, oldest first (an S3 listing, or the folders of a local root)."""
    if root.startswith("s3://"):
        if root.rstrip("/") != DEFAULT_ROOT:
            raise BaseError(f"Chỉ liệt kê được release của {DEFAULT_ROOT}; hãy đặt BDS_OVERTURE_RELEASE.")
        try:
            with urllib.request.urlopen(LIST_URL, timeout=30, context=net.ssl_context()) as res:  # noqa: S310 — fixed https URL
                body = res.read().decode("utf-8", "replace")
        except OSError as exc:
            raise BaseError(f"Không đọc được danh sách release Overture: {exc}") from exc
        names = re.findall(r"<Prefix>release/([^/<]+)/</Prefix>", body)
    else:
        path = Path(root)
        names = [p.name for p in path.iterdir() if p.is_dir()] if path.is_dir() else []
    return sorted((n for n in names if _RELEASE_RE.match(n)), key=_release_key)


def _release_key(name: str) -> tuple[str, int]:
    day, _, n = name.partition(".")
    return day, int(n or 0)


def latest_release(root: str = DEFAULT_ROOT) -> str:
    found = releases(root)
    if not found:
        raise BaseError(f"Không tìm thấy release Overture nào trong {root}.")
    return found[-1]


# ------------------------------------------------------------------ SQL (pure, unit-tested)

def theme_glob(root: str, release: str, theme: str, kind: str) -> str:
    return f"{root.rstrip('/')}/{release}/theme={theme}/type={kind}/*.parquet"


def bbox_where(bbox: tuple[float, float, float, float]) -> str:
    """Overlap with the feature's `bbox` column, which the Parquet row-group statistics prune on."""
    w, s, e, n = (repr(float(v)) for v in bbox)
    return f"bbox.xmin <= {e} AND bbox.xmax >= {w} AND bbox.ymin <= {n} AND bbox.ymax >= {s}"


def boundary_sql(source: str, area: Area) -> str:
    """The division_area of the city (the land polygon, not the maritime one)."""
    return (
        "SELECT id, names.primary AS name, geometry, bbox "
        f"FROM read_parquet({sql_str(source)}, hive_partitioning = false) "
        f"WHERE subtype = 'region' AND country = {sql_str(area.country)} AND region = {sql_str(area.region)} "
        f"AND coalesce(class, 'land') = 'land' AND {bbox_where(area.search_bbox)} "
        "ORDER BY ST_Area(geometry) DESC LIMIT 1"
    )


def osm_source_expr() -> str:
    """The source record of the geometry, when it comes from OpenStreetMap (NULL otherwise)."""
    return (f"list_filter(sources, s -> s.dataset = {sql_str(OSM_DATASET)} "
            "AND coalesce(s.property, '') = '')[1]")


def osm_id_expr(record: str) -> str:
    """'w123@4' -> 'way/123' (also r -> relation, n -> node): the id openstreetmap.org uses."""
    return (f"(CASE left({record}, 1) WHEN 'w' THEN 'way/' WHEN 'r' THEN 'relation/' WHEN 'n' THEN 'node/' END"
            f" || regexp_extract({record}, '^[a-z]([0-9]+)', 1))")


_ATTRS = ("names.primary AS name, height, num_floors, "
          f"coalesce(min_height, min_floor * {M_PER_LEVEL!r}) AS min_height, min_floor, "
          "roof_shape, roof_height, roof_color, roof_material, facade_color, facade_material")


def buildings_sql(building_src: str, part_src: str, bbox: tuple[float, float, float, float]) -> list[str]:
    """Statements that leave the OSM buildings and parts inside table `area` in table `osm_buildings`.

    A building counts when it touches the boundary; a part when its building was kept. Underground
    buildings are left out (nothing to extrude). Should a part share its OSM id with a building, the
    building wins.
    """
    rec = "osm.record_id"
    # `t.`: the boundary table `area` has an id and a geometry too.
    common = (f"{osm_id_expr(rec)} AS building_id, {_ATTRS}, "
              f"TRY_CAST(regexp_extract({rec}, '@([0-9]+)$', 1) AS INTEGER) AS osm_version, "
              "osm.update_time AS osm_timestamp, osm.version AS osm_snapshot, t.id AS overture_id, t.geometry")
    return [
        f"""CREATE TABLE b AS
            SELECT {common}, class, subtype FROM (
              SELECT *, {osm_source_expr()} AS osm
              FROM read_parquet({sql_str(building_src)}, hive_partitioning = false)
              WHERE {bbox_where(bbox)}
            ) AS t, area
            WHERE osm IS NOT NULL AND NOT coalesce(is_underground, false) AND ST_Intersects(t.geometry, area.geometry)""",
        f"""CREATE TABLE p AS
            SELECT {common}, t.building_id AS parent_overture_id FROM (
              SELECT *, {osm_source_expr()} AS osm
              FROM read_parquet({sql_str(part_src)}, hive_partitioning = false)
              WHERE {bbox_where(bbox)}
            ) AS t
            WHERE osm IS NOT NULL AND NOT coalesce(is_underground, false)
              AND t.building_id IN (SELECT overture_id FROM b)""",
        f"""CREATE TABLE osm_buildings AS
            SELECT {', '.join(OUTPUT_COLUMNS)}, osm_snapshot, geometry FROM (
              SELECT *, NULL::VARCHAR AS parent_building_id, 0 AS rank FROM b
              UNION ALL BY NAME
              SELECT p.* EXCLUDE (parent_overture_id), b.building_id AS parent_building_id, 1 AS rank,
                     b.class AS class, NULL::VARCHAR AS subtype
              FROM p JOIN b ON b.overture_id = p.parent_overture_id
            )
            WHERE building_id IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY building_id ORDER BY rank) = 1""",
    ]


# ------------------------------------------------------------------ DuckDB

def connect(root: str):
    """A DuckDB connection with spatial (+ httpfs and anonymous S3 access for an s3:// root)."""
    import duckdb

    con = duckdb.connect()
    load_extension(con, "spatial")
    if root.startswith("s3://"):
        load_extension(con, "httpfs")
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy:
            parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
            con.execute(f"SET http_proxy = {sql_str(f'{parts.hostname}:{parts.port or 80}')}")
            if parts.username:
                con.execute(f"SET http_proxy_username = {sql_str(parts.username)}")
                con.execute(f"SET http_proxy_password = {sql_str(parts.password or '')}")
        ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
        if ca and Path(ca).is_file():
            con.execute(f"SET ca_cert_file = {sql_str(ca)}")
        # Public bucket: empty keys, so credentials in the environment are not sent.
        con.execute(f"CREATE SECRET overture (TYPE s3, PROVIDER config, REGION {sql_str(S3_REGION)}, "
                    "KEY_ID '', SECRET '')")
    return con


def load_extension(con, name: str) -> None:
    """LOAD an extension: already installed, from a duckdb-extension-<name> wheel, or INSTALLed."""
    import duckdb

    try:
        con.execute(f"LOAD {name}")
        return
    except duckdb.Error:
        pass
    # PyPI wheels, for machines that cannot reach extensions.duckdb.org.
    site = Path(duckdb.__file__).resolve().parent.parent
    for path in site.glob(f"duckdb_extension_{name}/extensions/v{duckdb.__version__}/{name}.duckdb_extension"):
        con.execute(f"LOAD {sql_str(str(path))}")
        return
    try:
        con.execute(f"INSTALL {name}")
        con.execute(f"LOAD {name}")
    except duckdb.Error as exc:
        raise BaseError(f"Không tải được extension DuckDB '{name}' (cần internet lần đầu): {exc}") from exc


# ------------------------------------------------------------------ manifest

def read_manifest(base_dir: Path, area: Area) -> dict[str, Any] | None:
    """The build in use, with absolute paths, or None when there is none (or its files are gone)."""
    folder = area_dir(base_dir, area)
    try:
        data = json.loads((folder / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    paths = {k: folder / data[k] for k in ("gpkg", "pmtiles", "folder") if data.get(k)}
    if not all(paths.get(k) and paths[k].exists() for k in ("gpkg", "pmtiles")):
        return None
    return {**data, **{f"{k}_path": str(v) for k, v in paths.items()}}


def write_manifest(base_dir: Path, area: Area, data: dict[str, Any]) -> Path:
    folder = area_dir(base_dir, area)
    path = folder / MANIFEST_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


# ------------------------------------------------------------------ build job

def base_config(area: Area, base_dir: Path) -> JobConfig:
    """The convert settings of a base: OSM height, then levels × 3.5 m, then 4 m; parts by OSM id."""
    return JobConfig(
        input_path=Path(),  # set once the run folder exists (BasePipeline.__init__)
        layer=None,
        geom_column=None,
        src_crs=None,
        out_dir=area_dir(base_dir, area),
        out_name=out_name(area),
        want_gpkg=True,
        want_pmtiles=True,
        gpkg_layer="buildings",
        gpkg_crs=WGS84,
        columns=None,
        where=None,
        makevalid=False,
        height={"height_col": "height", "levels_col": "num_floors", "m_per_level": M_PER_LEVEL},
        parts=("building_id", "parent_building_id"),
        tiles=TilesConfig(minzoom=12, maxzoom=16, attributes=TILE_ATTRIBUTES),
        category_cols=("class",),
        report_id_col="building_id",
        keep_temp=False,
    )


class BasePipeline(Pipeline):
    """Overture -> OSM buildings of the area -> the convert pipeline -> base.json."""

    handled_errors = (*Pipeline.handled_errors, BaseError)

    def __init__(self, job: Job, registry: FileRegistry, area: Area, base_dir: Path,
                 root: str = DEFAULT_ROOT, release: str | None = None) -> None:
        super().__init__(job, registry, base_config(area, base_dir))
        self.area = area
        self.base_dir = base_dir
        self.root = root
        self.release = release
        self.raw = self.work / f"{out_name(area)}_raw.gpkg"
        self.boundary_file = self.work / "boundary.gpkg"
        self.cfg = replace(self.cfg, input_path=self.raw)
        self.extract: dict[str, Any] = {}

    def intro(self) -> list[str]:
        return [f"Building Data Studio {__version__} — base {self.area.label}: building OpenStreetMap qua Overture Maps",
                f"Thư mục output: {self.out_dir}"]

    def plan(self) -> list[Step]:
        steps = [
            Step("release", "Chọn bản phát hành Overture"),
            Step("boundary", f"Ranh giới {self.area.label}"),
            Step("extract", f"Tải building OSM trong ranh giới {self.area.label}"),
            *super().plan(),
            Step("publish", "Đặt làm base cho trang 3D và Landmark"),
        ]
        steps.insert(next(i for i, s in enumerate(steps) if s.key == "finalize"),
                     Step("boundary_layer", "Thêm layer ranh giới vào GeoPackage"))
        return steps

    # ---------------------------------------------------------------- stages
    def _step_release(self, step: Step) -> None:
        if not self.release:
            self.release = latest_release(self.root)
        elif not _RELEASE_RE.match(self.release):
            raise ConfigError(f"Tên release Overture không hợp lệ: {self.release}")
        step.detail = self.release
        self.job.add_log(f"Overture release {self.release} ({self.root})")

    def _step_boundary(self, step: Step) -> None:
        self._use_duckdb(connect(self.root))
        src = theme_glob(self.root, self.release, "divisions", "division_area")
        sql = boundary_sql(src, self.area)
        self.commands.append(f"duckdb: {sql}")
        self._duck(f"CREATE TABLE area AS {sql}")
        row = self.con.execute("SELECT id, name, bbox, ST_Area_Spheroid(ST_FlipCoordinates(geometry)) / 1e6 FROM area").fetchone()
        if row is None:
            raise PipelineError(f"Không tìm thấy ranh giới {self.area.region} trong Overture {self.release}.")
        bid, name, bbox, km2 = row
        self.extract["boundary"] = {"id": bid, "name": name, "area_km2": round(km2, 1)}
        self.bbox = (bbox["xmin"], bbox["ymin"], bbox["xmax"], bbox["ymax"])
        self._duck(f"COPY (SELECT id, name, geometry FROM area) TO {sql_str(str(self.boundary_file))} "
                   "WITH (FORMAT GDAL, DRIVER 'GPKG')")
        step.detail = f"{name}, {km2:,.0f} km²"
        self.job.add_log(f"Ranh giới: {name} ({bid}), {km2:,.1f} km², bbox {', '.join(f'{v:.4f}' for v in self.bbox)}")

    def _step_extract(self, step: Step) -> None:
        job = self.job
        statements = buildings_sql(theme_glob(self.root, self.release, "buildings", "building"),
                                   theme_glob(self.root, self.release, "buildings", "building_part"), self.bbox)
        started = time.time()
        for i, sql in enumerate(statements, start=1):
            self.commands.append(f"duckdb: {' '.join(sql.split())}")
            self._duck(sql)
            step.progress = 90.0 * i / len(statements)
        buildings, parts, snapshot = self.con.execute(
            "SELECT count(*) FILTER (WHERE parent_building_id IS NULL), count(parent_building_id), max(osm_snapshot) "
            "FROM osm_buildings").fetchone()
        dropped_parts = self.con.execute("SELECT count(*) FROM p").fetchone()[0] - parts
        if not buildings:
            raise PipelineError("Không có building OSM nào trong ranh giới.")
        self.raw.unlink(missing_ok=True)
        columns = ", ".join(OUTPUT_COLUMNS)
        self._duck(f"COPY (SELECT {columns}, geometry FROM osm_buildings ORDER BY building_id) "
                   f"TO {sql_str(str(self.raw))} WITH (FORMAT GDAL, DRIVER 'GPKG')")
        self.extract.update(buildings=buildings, parts=parts, dropped_parts=dropped_parts,
                            osm_snapshot=str(snapshot or "")[:10], seconds=round(time.time() - started, 1))
        step.detail = f"{buildings:,} building + {parts:,} khối con"
        job.add_log(f"OSM: {buildings:,} building, {parts:,} khối con (building:part)"
                    + (f", bỏ {dropped_parts:,} khối con trùng id building" if dropped_parts else "")
                    + f"; ảnh chụp OSM ngày {self.extract['osm_snapshot'] or '?'}; {time.time() - started:.0f}s")

    def _step_boundary_layer(self, step: Step) -> None:
        self._run([tools.require("ogr2ogr"), "-update", "-f", "GPKG", str(self.gpkg_out), str(self.boundary_file),
                   "-nln", "boundary", "-nlt", "MULTIPOLYGON", "-t_srs", WGS84, "-lco", "GEOMETRY_NAME=geom"], step)

    def _step_publish(self, step: Step) -> None:
        a = self.area
        report = self.job.report or {}
        data = {
            "area": a.key,
            "label": a.label,
            "name": f"{a.label} · OSM buildings",
            "source": f"OpenStreetMap qua Overture Maps {self.release}",
            "attribution": self.attribution,
            "release": self.release,
            "osm_snapshot": self.extract.get("osm_snapshot"),
            "boundary": self.extract.get("boundary"),
            "counts": {k: self.extract.get(k) for k in ("buildings", "parts", "dropped_parts")},
            "height": {k: (report.get("height") or {}).get(k) for k in ("real_count", "real_pct")},
            "view": {"center": list(a.view[:2]), "zoom": a.view[2]},
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "folder": self.out_dir.name,
            "gpkg": f"{self.out_dir.name}/{self.gpkg_out.name}",
            "pmtiles": f"{self.out_dir.name}/{self.pmtiles_out.name}",
            "app_version": __version__,
        }
        path = write_manifest(self.base_dir, a, data)
        self._add_output("manifest", "base.json", path, "report")
        self.job.add_log(f"Base {a.label} đang dùng: {self.out_dir.name} ({path})")

    # ---------------------------------------------------------------- helpers
    @property
    def attribution(self) -> str:
        return f"© OpenStreetMap contributors (ODbL) · Overture Maps {self.release}"

    def _duck(self, sql: str) -> None:
        import duckdb

        if self.job.runner.cancelled:
            raise tools.CancelledError("Đã huỷ")
        try:
            self.con.execute(sql)
        except duckdb.Error as exc:
            if self.job.runner.cancelled:
                raise tools.CancelledError("Đã huỷ") from exc
            raise PipelineError(f"DuckDB: {exc}") from exc

    def _step_tiles(self, step: Step) -> None:
        # The attribution needs the release, known only once the run started.
        self.cfg = replace(self.cfg, tiles=replace(self.cfg.tiles, attribution=self.attribution))
        super()._step_tiles(step)


def execute(job: Job, registry: FileRegistry, base_dir: Path | None = None) -> None:
    """Entry point used by the JobManager worker thread."""

    def build() -> BasePipeline:
        area = AREAS.get(str(job.config.get("area") or DEFAULT_AREA))
        if area is None:
            raise ConfigError(f"Không có khu vực {job.config.get('area')!r}.")
        root = str(job.config.get("root") or os.environ.get("BDS_OVERTURE_ROOT") or DEFAULT_ROOT)
        release = job.config.get("release") or os.environ.get("BDS_OVERTURE_RELEASE") or None
        return BasePipeline(job, registry, area, base_dir or default_dir(), root=root, release=release)

    run_job(job, build)


def main() -> None:
    """Build a base from the terminal: python -m app.osmbase [hanoi] [--release 2026-10-21.0]."""
    import argparse

    parser = argparse.ArgumentParser(description="Tạo base building OSM cho một thành phố (từ Overture Maps)")
    parser.add_argument("area", nargs="?", default=DEFAULT_AREA, choices=sorted(AREAS))
    parser.add_argument("--release", help="Release Overture, vd 2026-10-21.0 (mặc định: mới nhất)")
    parser.add_argument("--root", help=f"Gốc dữ liệu Overture (mặc định {DEFAULT_ROOT})")
    parser.add_argument("--dir", type=Path, help=f"Thư mục base (mặc định {default_dir()})")
    args = parser.parse_args()
    job = Job(id="cli", config={"area": args.area, "release": args.release, "root": args.root}, name="base")
    printed = 0

    def show() -> None:
        nonlocal printed
        with job.lock:
            lines = job.log[printed:]
            printed += len(lines)
        for line in lines:
            print(line, flush=True)

    import threading

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
        raise SystemExit(f"Không tạo được base: {job.error or job.status}")


if __name__ == "__main__":
    main()
