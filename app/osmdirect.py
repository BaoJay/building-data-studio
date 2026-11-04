"""Base of OSM buildings straight from OpenStreetMap, without Overture.

Overture republishes OSM once a month; this reads OSM itself:

    Overpass API (live, ~1 minute behind OSM)        or      a .osm.pbf / .osm extract (Geofabrik...)
      ├─ boundary: relation ISO3166-2=<region>
      └─ buildings + building:part, tile by tile             └─ ogr2ogr (custom osmconf) -> GeoJSONSeq
           └─ osmtags (height, levels, min_height…) + osmgeom (multipolygon rings)
                └─ DuckDB: parts get the smallest outline that contains them -> raw GeoPackage
                     └─ the same convert pipeline and base.json as osmbase (app.osmbase.BasePipeline)

The raw GeoPackage has the columns of the Overture base (osmbase.OUTPUT_COLUMNS), so everything
downstream (height_m, has_parts / is_part, PMTiles, report, 3D preview) is shared.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__, net, osmgeom, osmtags, tools
from .jobs import FileRegistry, Job, Step
from .osmbase import (
    AREAS,
    DEFAULT_AREA,
    M_PER_LEVEL,
    OUTPUT_COLUMNS,
    Area,
    BaseError,
    BasePipeline,
    connect,
    default_dir,
)
from .pipeline import ConfigError, Pipeline, PipelineError, run_job
from .sqlbuild import sql_str

log = logging.getLogger(__name__)

OVERPASS_URLS = ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter")
TILE_DEG = 0.05  # ~5.5 km: a tile of Hà Nội holds a few thousand buildings, well within a public instance's limits
MIN_TILE_DEG = 0.003  # a tile that still fails at this size is not going to work
ATTEMPTS = 4
PAUSE_SECONDS = 1.0  # between two requests to the same server
TIMEOUT_SECONDS = 300
ISSUE_LOG_LIMIT = 10
TAG_FILTER_KEYS = ("building", "building:part")
USER_AGENT = f"BuildingDataStudio/{__version__} (+https://github.com/BaoJay/building-data-studio)"


class OverpassError(BaseError):
    """The Overpass API could not answer."""


class OverpassTooBig(OverpassError):
    """The query ran out of time or memory on the server: ask for a smaller area."""


# ------------------------------------------------------------------ features

@dataclass
class Feature:
    """One OSM way or relation with its polygons, before attributes are resolved."""

    building_id: str  # 'way/123'
    kind: str  # 'building' | 'part'
    tags: dict[str, str]
    polygons: list[osmgeom.Polygon]
    version: int | None = None
    timestamp: str | None = None


@dataclass
class Counters:
    """What the extract saw and what it could not use (shown in the log and written as a CSV)."""

    features: int = 0
    unclosed: int = 0  # multipolygon relations / ways whose rings do not close
    issues: list[tuple[str, str, str]] = field(default_factory=list)  # (building_id, key, raw value)
    base_time: str | None = None  # Overpass: timestamp of the OSM data answered from
    newest_edit: str | None = None

    def seen(self, f: Feature) -> None:
        self.features += 1
        for key, value in osmtags.tag_issues(f.tags):
            self.issues.append((f.building_id, key, value))
        if f.timestamp and (self.newest_edit is None or f.timestamp > self.newest_edit):
            self.newest_edit = f.timestamp


def parse_overpass(data: dict[str, Any], counters: Counters | None = None) -> Iterator[Feature]:
    """Features of an Overpass JSON answer to a query ending in `out geom meta`.

    Ways must be closed; multipolygon relations are stitched from their member ways (role outer /
    inner). `type=building` relations only group other features, which are found on their own.
    """
    counters = counters if counters is not None else Counters()
    base = (data.get("osm3s") or {}).get("timestamp_osm_base")
    if base:
        counters.base_time = base
    for el in data.get("elements", ()):
        etype = el.get("type")
        tags = el.get("tags") or {}
        kind = osmtags.kind(tags)
        if kind is None or etype not in ("way", "relation"):
            continue
        if etype == "way":
            pts = [(p["lon"], p["lat"]) for p in el.get("geometry") or () if p]
            polys = osmgeom.closed_way(pts)
        else:
            if tags.get("type") != "multipolygon":
                continue
            outers, inners = [], []
            for m in el.get("members", ()):
                if m.get("type") != "way" or not m.get("geometry"):
                    continue
                pts = [(p["lon"], p["lat"]) for p in m["geometry"] if p]
                (inners if m.get("role") == "inner" else outers).append(pts)
            polys, bad = osmgeom.polygons(outers, inners)
            if bad:
                counters.unclosed += 1
        if not polys:
            counters.unclosed += 1
            continue
        f = Feature(osmtags.osm_id(etype, el["id"]), kind, tags, polys, el.get("version"), el.get("timestamp"))
        counters.seen(f)
        yield f


_HSTORE_RE = re.compile(r'"((?:[^"\\]|\\.)*)"\s*=>\s*(?:"((?:[^"\\]|\\.)*)"|NULL)')


def parse_hstore(text: str | None) -> dict[str, str]:
    """GDAL's `all_tags` / `other_tags` field: "k"=>"v","k2"=>"v2"."""
    unescape = lambda s: re.sub(r"\\(.)", r"\1", s)  # noqa: E731
    return {unescape(k): unescape(v or "") for k, v in _HSTORE_RE.findall(text or "")}


def geojson_polygons(geom: dict[str, Any] | None) -> list[osmgeom.Polygon]:
    if not geom:
        return []
    if geom.get("type") == "Polygon":
        raw = [geom["coordinates"]]
    elif geom.get("type") == "MultiPolygon":
        raw = geom["coordinates"]
    else:
        return []
    out = []
    for poly in raw:
        rings = [[(p[0], p[1]) for p in ring] for ring in poly]
        if rings and len(rings[0]) >= osmgeom.MIN_RING_POINTS and osmgeom.ring_area(rings[0]) != 0:
            out.append([r for i, r in enumerate(rings) if i == 0 or len(r) >= osmgeom.MIN_RING_POINTS])
    return out


def parse_geojsonseq(lines: Iterator[str], counters: Counters | None = None) -> Iterator[Feature]:
    """Features of the GeoJSONSeq that ogr2ogr writes from the OSM driver's `multipolygons` layer."""
    counters = counters if counters is not None else Counters()
    for line in lines:
        line = line.strip().lstrip("\x1e")
        if not line:
            continue
        feat = json.loads(line)
        props = feat.get("properties") or {}
        tags = parse_hstore(props.get("all_tags"))
        kind = osmtags.kind(tags)
        if kind is None:
            continue
        polys = geojson_polygons(feat.get("geometry"))
        if not polys:
            counters.unclosed += 1
            continue
        way, rel = props.get("osm_way_id"), props.get("osm_id")
        if way not in (None, ""):
            bid = osmtags.osm_id("way", way)
        elif rel not in (None, ""):
            bid = osmtags.osm_id("relation", rel)
        else:
            continue
        version = props.get("osm_version")
        f = Feature(bid, kind, tags, polys, int(version) if version not in (None, "") else None,
                    props.get("osm_timestamp") or None)
        counters.seen(f)
        yield f


# ------------------------------------------------------------------ Overpass

def building_query(box: tuple[float, float, float, float], timeout: int = TIMEOUT_SECONDS) -> str:
    """Overpass QL: buildings and parts (ways and multipolygon relations) touching box (w, s, e, n)."""
    w, s, e, n = box
    bb = f"({s:.6f},{w:.6f},{n:.6f},{e:.6f})"
    clauses = "".join(f'way["{k}"]{bb};rel["{k}"]["type"="multipolygon"]{bb};' for k in TAG_FILTER_KEYS)
    return f"[out:json][timeout:{timeout}];({clauses});out geom meta qt;"


def boundary_query(region: str, timeout: int = TIMEOUT_SECONDS) -> str:
    """Overpass QL: the administrative relation with this ISO 3166-2 code (e.g. VN-HN)."""
    if not re.fullmatch(r"[A-Z]{2}(-[A-Z0-9]{1,3})?", region):
        raise ConfigError(f"Mã ISO 3166-2 không hợp lệ: {region!r}")
    return (f'[out:json][timeout:{timeout}];rel["boundary"="administrative"]["ISO3166-2"="{region}"];out geom;')


def parse_boundary(data: dict[str, Any]) -> list[osmgeom.Polygon]:
    """The land polygons of the first boundary relation in an Overpass answer."""
    for el in data.get("elements", ()):
        if el.get("type") != "relation":
            continue
        outers, inners = [], []
        for m in el.get("members", ()):
            if m.get("type") != "way" or not m.get("geometry"):
                continue
            pts = [(p["lon"], p["lat"]) for p in m["geometry"] if p]
            (inners if m.get("role") == "inner" else outers if m.get("role") == "outer" else []).append(pts)
        polys, bad = osmgeom.polygons(outers, inners)
        if polys and not bad:
            return polys
    return []


def _http_post(url: str, ql: str) -> bytes:
    req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": ql}).encode(),
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS + 30, context=net.ssl_context()) as res:  # noqa: S310 — https endpoint
        return res.read()


class Overpass:
    """A polite Overpass client: rotates endpoints, backs off on 429 / 5xx, reports too-big queries."""

    def __init__(self, urls: tuple[str, ...] = OVERPASS_URLS, *, transport: Callable[[str, str], bytes] = _http_post,
                 sleep: Callable[[float], None] = time.sleep, pause: float = PAUSE_SECONDS) -> None:
        self.urls = urls
        self.transport = transport
        self.sleep = sleep
        self.pause = pause
        self._last = 0.0

    def query(self, ql: str) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(ATTEMPTS):
            url = self.urls[attempt % len(self.urls)]
            wait = self.pause - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            try:
                body = self.transport(url, ql)
                self._last = time.monotonic()
                data = json.loads(body)
            except urllib.error.HTTPError as exc:
                self._last = time.monotonic()
                if exc.code not in (429, 502, 503, 504):
                    raise OverpassError(f"Overpass {url}: HTTP {exc.code}") from exc
                last = exc
            except (OSError, ValueError) as exc:  # network error, or an HTML error page instead of JSON
                last = exc
            else:
                if "runtime error" in str(data.get("remark", "")):
                    raise OverpassTooBig(str(data["remark"]))
                return data
            self.sleep(min(10 * 2 ** attempt, 60))
        raise OverpassError(f"Overpass không trả lời sau {ATTEMPTS} lần thử: {last}")


def fetch_tiles(client: Overpass, boxes: list[tuple[float, float, float, float]], *,
                on_tile: Callable[[int, int], None] | None = None,
                cancelled: Callable[[], bool] = lambda: False) -> Iterator[dict[str, Any]]:
    """Overpass answers for every box; a box the server finds too big is split in four, down to MIN_TILE_DEG."""
    todo = list(reversed(boxes))
    done = 0
    total = len(boxes)
    while todo:
        if cancelled():
            raise tools.CancelledError("Đã huỷ")
        box = todo.pop()
        try:
            data = client.query(building_query(box))
        except OverpassTooBig as exc:
            if box[2] - box[0] <= MIN_TILE_DEG:
                raise OverpassError(f"Ô {box} vẫn quá lớn cho máy chủ Overpass: {exc}") from exc
            quarters = osmgeom.split(box)
            total += len(quarters) - 1
            todo.extend(reversed(quarters))
            continue
        done += 1
        if on_tile:
            on_tile(done, total)
        yield data


# ------------------------------------------------------------------ OSM file (pbf / xml)

OSMCONF = """\
# osmconf for Building Data Studio: building and building:part areas, every tag in all_tags.
[general]
closed_ways_are_polygons=building,building:part
all_tags=yes
report_all_tags=yes

[points]
[lines]
[multilinestrings]
[other_relations]

[multipolygons]
osm_id=yes
osm_way_id=yes
osm_version=yes
osm_timestamp=yes
attributes=
"""


def osm_file_command(ogr2ogr: str, src: Path, out: Path, conf: Path, bbox: tuple[float, float, float, float]) -> list[str]:
    """ogr2ogr command writing the building areas of an OSM file as GeoJSONSeq (clipped to bbox)."""
    w, s, e, n = bbox
    keys = " OR ".join(f"all_tags LIKE '%\"{k}\"=>%'" for k in TAG_FILTER_KEYS)
    return [ogr2ogr, "-f", "GeoJSONSeq", str(out), str(src), "multipolygons", "-oo", f"CONFIG_FILE={conf}",
            "-spat", str(w), str(s), str(e), str(n), "-where", keys, "-lco", "RS=NO", "-skipfailures"]


# ------------------------------------------------------------------ DuckDB

RAW_DDL = (
    "CREATE TABLE raw (building_id VARCHAR PRIMARY KEY, kind VARCHAR, name VARCHAR, class VARCHAR, subtype VARCHAR, "
    "height DOUBLE, num_floors DOUBLE, min_height DOUBLE, min_floor DOUBLE, roof_shape VARCHAR, roof_height DOUBLE, "
    "roof_color VARCHAR, roof_material VARCHAR, facade_color VARCHAR, facade_material VARCHAR, "
    "osm_version INTEGER, osm_timestamp VARCHAR, wkt VARCHAR)"
)
_RAW_FIELDS = ("name", "class", "subtype", "height", "num_floors", "min_height", "min_floor", "roof_shape",
               "roof_height", "roof_color", "roof_material", "facade_color", "facade_material")
INSERT_SQL = "INSERT OR IGNORE INTO raw VALUES (" + ", ".join("?" * (2 + len(_RAW_FIELDS) + 3)) + ")"


def raw_row(f: Feature) -> tuple[Any, ...]:
    """A `raw` table row: the id, kind, resolved attributes, OSM version / timestamp and the WKT."""
    attrs = osmtags.building_attrs(f.tags, M_PER_LEVEL)
    return (f.building_id, f.kind, *(attrs[k] for k in _RAW_FIELDS), f.version, f.timestamp, osmgeom.to_wkt(f.polygons))


def assemble_sql() -> list[str]:
    """Statements turning `raw` (+ table `area`) into `osm_buildings`, the table the Overture base also builds.

    A building:part gets the smallest building outline that contains a point of it as its parent
    (OSM has no explicit link; the 3D Simple Buildings scheme is purely geometric). A part without an
    outline stays a plain building, so it is not hidden. A part takes the class of its building, as in the Overture base. Features are kept when they touch the boundary.
    """
    skip = ("building_id", "parent_building_id", "overture_id", "osm_version", "osm_timestamp", "class")
    cols = ", ".join(f"g.{c}" for c in OUTPUT_COLUMNS if c not in skip)
    return [
        "CREATE TABLE g AS SELECT * EXCLUDE (wkt), "
        "ST_CollectionExtract(ST_MakeValid(ST_GeomFromText(wkt)), 3) AS geometry FROM raw",
        "DELETE FROM g WHERE geometry IS NULL OR ST_IsEmpty(geometry)",
        """CREATE TABLE parent AS
           SELECT p.building_id, arg_min(b.building_id, ST_Area(b.geometry)) AS parent_building_id
           FROM g AS p JOIN g AS b
             ON b.kind = 'building' AND b.building_id <> p.building_id
            AND ST_Contains(b.geometry, ST_PointOnSurface(p.geometry))
           WHERE p.kind = 'part'
           GROUP BY p.building_id""",
        f"""CREATE TABLE osm_buildings AS
           SELECT g.building_id, parent.parent_building_id, {cols},
                  CASE WHEN parent.parent_building_id IS NULL THEN g.class ELSE pb.class END AS class,
                  g.osm_version, g.osm_timestamp, NULL::VARCHAR AS overture_id, g.geometry
           FROM g LEFT JOIN parent USING (building_id) LEFT JOIN g AS pb ON pb.building_id = parent.parent_building_id, area
           WHERE ST_Intersects(g.geometry, area.geometry)""",
    ]


LINEAGE_COLUMN = "height_lineage"
DEFAULT_HEIGHT_M = 4.0  # sqlbuild's default when there is neither a height nor levels


def lineage_statements(table: str, m_per_level: float = M_PER_LEVEL, default_m: float = DEFAULT_HEIGHT_M) -> list[str]:
    """SQL (GeoPackage / SQLite) adding `height_lineage`: where each height_m came from, for QA.

    Derived from `h_src`, which the convert step already decided, so the two never disagree:
    osm:height (the tag), osm:levels*3.5m (building:levels x metres per level), default:4m, and
    osm:height:clamped for a tag above the valid range when the rule clamps.
    """
    t = '"' + table.replace('"', '""') + '"'
    return [
        f'ALTER TABLE {t} ADD COLUMN "{LINEAGE_COLUMN}" TEXT',
        f'UPDATE {t} SET "{LINEAGE_COLUMN}" = CASE "h_src" '
        f"WHEN 'height' THEN 'osm:height' "
        f"WHEN 'levels' THEN 'osm:levels*{m_per_level:g}m' "
        f"WHEN 'clamped' THEN 'osm:height:clamped' "
        f"ELSE 'default:{default_m:g}m' END",
    ]


def copy_raw_sql(raw: Path) -> str:
    cols = ", ".join(OUTPUT_COLUMNS)
    return (f"COPY (SELECT {cols}, geometry FROM osm_buildings ORDER BY building_id) "
            f"TO {sql_str(str(raw))} WITH (FORMAT GDAL, DRIVER 'GPKG')")


# ------------------------------------------------------------------ pipeline

class DirectBasePipeline(BasePipeline):
    """OpenStreetMap (Overpass or a file) -> OSM buildings of the area -> the convert pipeline -> base.json."""

    source_kind = "overpass"

    def __init__(self, job: Job, registry: FileRegistry, area: Area, base_dir: Path, *, osm_file: Path | None = None,
                 urls: tuple[str, ...] = OVERPASS_URLS, tile_deg: float = TILE_DEG,
                 client: Overpass | None = None) -> None:
        super().__init__(job, registry, area, base_dir)
        self.osm_file = osm_file
        self.source_kind = "file" if osm_file else "overpass"
        self.client = client or Overpass(urls)
        self.tile_deg = tile_deg
        self.counters = Counters()
        self.polygons: list[osmgeom.Polygon] = []

    # ---------------------------------------------------------------- labels
    @property
    def source_label(self) -> str:
        if self.osm_file:
            return f"OpenStreetMap trực tiếp, file {self.osm_file.name}"
        return "OpenStreetMap trực tiếp qua Overpass API"

    @property
    def attribution(self) -> str:
        return "© OpenStreetMap contributors (ODbL)"

    def intro(self) -> list[str]:
        return [f"Building Data Studio {__version__} — base {self.area.label}: {self.source_label}",
                f"Thư mục output: {self.out_dir}"]

    def plan(self) -> list[Step]:
        steps = [
            Step("boundary", f"Ranh giới {self.area.label}"),
            Step("extract", "Đọc building từ file OSM" if self.osm_file else f"Tải building {self.area.label} từ Overpass"),
            *Pipeline.plan(self),
            Step("publish", "Đặt làm base cho trang 3D và Landmark"),
        ]
        steps.insert(next(i for i, s in enumerate(steps) if s.key == "finalize"),
                     Step("boundary_layer", "Thêm layer ranh giới vào GeoPackage"))
        return steps

    # ---------------------------------------------------------------- stages
    def _step_boundary(self, step: Step) -> None:
        self._use_duckdb(connect(""))
        a = self.area
        try:
            data = self.client.query(boundary_query(a.region))
            self.polygons = parse_boundary(data)
        except OverpassError as exc:
            if self.osm_file is None:
                raise
            self.job.add_log(f"Không lấy được ranh giới từ Overpass ({exc}); dùng khung {a.search_bbox} thay thế.")
        if not self.polygons:
            if self.osm_file is None:
                raise PipelineError(f"Không tìm thấy ranh giới {a.region} trên OSM.")
            w, s, e, n = a.search_bbox
            self.polygons = [[[(w, s), (e, s), (e, n), (w, n), (w, s)]]]
        self.bbox = osmgeom.bbox(self.polygons)
        wkt = osmgeom.to_wkt(self.polygons)
        self._duck(f"CREATE TABLE area AS SELECT {sql_str(a.region)} AS id, {sql_str(a.label)} AS name, "
                   f"ST_GeomFromText({sql_str(wkt)}) AS geometry")
        km2 = self.con.execute("SELECT ST_Area_Spheroid(ST_FlipCoordinates(geometry)) / 1e6 FROM area").fetchone()[0]
        self.extract["boundary"] = {"id": a.region, "name": a.label, "area_km2": round(km2, 1)}
        self._duck(f"COPY (SELECT id, name, geometry FROM area) TO {sql_str(str(self.boundary_file))} "
                   "WITH (FORMAT GDAL, DRIVER 'GPKG')")
        step.detail = f"{a.label}, {km2:,.0f} km²"
        self.job.add_log(f"Ranh giới {a.label}: {km2:,.1f} km², bbox {', '.join(f'{v:.4f}' for v in self.bbox)}")

    def _step_extract(self, step: Step) -> None:
        started = time.time()
        self._duck(RAW_DDL)
        batch: list[tuple[Any, ...]] = []

        def flush() -> None:
            if batch:
                self.con.executemany(INSERT_SQL, batch)
                batch.clear()

        def feed(features: Iterator[Feature]) -> None:
            for f in features:
                batch.append(raw_row(f))
                if len(batch) >= 5000:
                    flush()

        if self.osm_file:
            feed(self._read_file(step))
        else:
            boxes = [b for b in osmgeom.grid(self.bbox, self.tile_deg) if osmgeom.box_touches(self.polygons, b)]
            self.job.add_log(f"{len(boxes)} ô {self.tile_deg}° trong ranh giới (Overpass, tự chia nhỏ nếu máy chủ quá tải)")

            def progress(done: int, total: int) -> None:
                step.progress = 90.0 * done / total
                if done % 20 == 0 or done == total:
                    self.job.add_log(f"Ô {done}/{total}")

            for data in fetch_tiles(self.client, boxes, on_tile=progress, cancelled=lambda: self.job.runner.cancelled):
                feed(parse_overpass(data, self.counters))
        flush()
        for sql in assemble_sql():
            self.commands.append(f"duckdb: {' '.join(sql.split())}")
            self._duck(sql)
        buildings, parts = self.con.execute(
            "SELECT count(*) FILTER (WHERE parent_building_id IS NULL), count(parent_building_id) FROM osm_buildings").fetchone()
        if not buildings:
            raise PipelineError("Không có building OSM nào trong ranh giới.")
        self.raw.unlink(missing_ok=True)
        self._duck(copy_raw_sql(self.raw))
        c = self.counters
        snapshot = (c.base_time or c.newest_edit or "")[:10]
        self.extract.update(buildings=buildings, parts=parts, dropped_parts=0, osm_snapshot=snapshot,
                            osm_base_time=c.base_time, unclosed=c.unclosed, tag_issues=len(c.issues),
                            seconds=round(time.time() - started, 1))
        step.detail = f"{buildings:,} building + {parts:,} khối con"
        self.job.add_log(f"OSM: {buildings:,} building, {parts:,} khối con (building:part); dữ liệu OSM tới "
                         f"{c.base_time or c.newest_edit or '?'}; {time.time() - started:.0f}s")
        if c.unclosed:
            self.job.add_log(f"Bỏ {c.unclosed:,} đối tượng có way không khép kín (relation chưa đủ member).")
        self._report_issues()

    def _read_file(self, step: Step) -> Iterator[Feature]:
        assert self.osm_file is not None
        conf = self.work / "osmconf.ini"
        conf.write_text(OSMCONF, encoding="utf-8")
        out = self.work / "osm_buildings.geojsonl"
        out.unlink(missing_ok=True)
        self._run(osm_file_command(tools.require("ogr2ogr"), self.osm_file, out, conf, self.bbox), step)
        with out.open(encoding="utf-8") as fh:
            yield from parse_geojsonseq(fh, self.counters)

    def _report_issues(self) -> None:
        """Unusable height / levels tags: logged, and written as a CSV for mappers to fix on OSM."""
        issues = self.counters.issues
        if not issues:
            return
        self.job.add_log(f"{len(issues):,} tag số không dùng được (height, building:levels…), ví dụ:")
        for bid, key, value in issues[:ISSUE_LOG_LIMIT]:
            self.job.add_log(f"  {bid}  {key}={value!r}")
        path = self.work / f"{self.cfg.out_name}_tag_issues.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["osm_id", "url", "key", "value"])
            writer.writerows((b, f"https://www.openstreetmap.org/{b}", k, v) for b, k, v in issues)
        self._tag_issues_csv = path

    def _step_normalize(self, step: Step) -> None:
        super()._step_normalize(step)
        ogrinfo = tools.require("ogrinfo")
        for sql in lineage_statements(self.cfg.gpkg_layer, M_PER_LEVEL):
            self._run([ogrinfo, str(self.normalized), "-q", "-sql", sql], step, fail_on_error_lines=True)
        self.job.add_log(f"Thêm cột {LINEAGE_COLUMN} (nguồn của height_m) vào GeoPackage.")

    def _step_finalize(self, step: Step) -> None:
        path = getattr(self, "_tag_issues_csv", None)
        if path is not None and path.exists():
            dest = self.out_dir / path.name
            path.replace(dest)
            self._add_output("tag_issues", "Tag OSM lỗi (CSV)", dest, "report")
        super()._step_finalize(step)


def execute(job: Job, registry: FileRegistry, base_dir: Path | None = None) -> None:
    """Entry point used by osmbase.execute when the job asks for source overpass / file."""

    def build() -> DirectBasePipeline:
        area = AREAS.get(str(job.config.get("area") or DEFAULT_AREA))
        if area is None:
            raise ConfigError(f"Không có khu vực {job.config.get('area')!r}.")
        osm_file = None
        if job.config.get("osm_file"):
            osm_file = Path(str(job.config["osm_file"])).expanduser()
            if not osm_file.is_file() or osm_file.suffix.lower() not in (".pbf", ".osm"):
                raise ConfigError(f"Không đọc được file OSM (.osm.pbf / .osm): {osm_file}")
        url = str(job.config.get("overpass_url") or os.environ.get("BDS_OVERPASS_URL") or "").strip()
        urls = (url,) if url else OVERPASS_URLS
        tile = float(job.config.get("tile_deg") or TILE_DEG)
        if not MIN_TILE_DEG <= tile <= 0.5:
            raise ConfigError(f"Cỡ ô Overpass phải trong {MIN_TILE_DEG}–0.5 độ, nhận {tile}.")
        return DirectBasePipeline(job, registry, area, base_dir or default_dir(), osm_file=osm_file, urls=urls,
                                  tile_deg=tile)

    run_job(job, build)


def main() -> None:
    """python -m app.osmdirect [hanoi] [--file vietnam.osm.pbf] [--overpass URL] [--tile 0.05]"""
    import argparse
    import threading

    parser = argparse.ArgumentParser(description="Tạo base building OSM trực tiếp từ OpenStreetMap (không qua Overture)")
    parser.add_argument("area", nargs="?", default=DEFAULT_AREA, choices=sorted(AREAS))
    parser.add_argument("--file", type=Path, help="Đọc từ file .osm.pbf / .osm (vd Geofabrik) thay vì Overpass")
    parser.add_argument("--overpass", help="URL Overpass (mặc định: overpass-api.de, rồi kumi.systems)")
    parser.add_argument("--tile", type=float, default=TILE_DEG, help=f"Cỡ ô tải Overpass, độ (mặc định {TILE_DEG})")
    parser.add_argument("--dir", type=Path, help=f"Thư mục base (mặc định {default_dir()})")
    args = parser.parse_args()
    job = Job(id="cli", name="base", config={"area": args.area, "osm_file": str(args.file) if args.file else None,
                                              "overpass_url": args.overpass, "tile_deg": args.tile,
                                              "source": "file" if args.file else "overpass"})
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
        raise SystemExit(f"Không tạo được base: {job.error or job.status}")


if __name__ == "__main__":
    main()
