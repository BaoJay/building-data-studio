"""Precomputed building shadows.

MapLibre cannot cast shadows, but with a fixed sun per light preset the ground shadow of
every building can be computed ahead of time and drawn as a flat `fill` layer under the
extrusions. Two entry points share the geometry:

* compute(): one area of a PMTiles file, for the 3D preview (prototype):
    ogr2ogr (PMTiles, max zoom, bbox) -> GeoJSONSeq footprints with height / base
      -> Python: shadow pieces per building
      -> ogr2ogr SQLite/SpatiaLite: union of all pieces minus all footprints -> GeoJSON
* compute_dataset(): a whole normalized GeoPackage, for the `shadows` layer of the
  convert pipeline's PMTiles. Read cell by cell through the GeoPackage R-tree, pieces
  clipped to the cell, unions per (cell, preset) run in parallel ogr2ogr processes.

Shadow pieces of a prism (base .. top) along the sun ray: a convex footprint gives the
convex hull of the footprint shifted to both heights; any other footprint gives both
shifted copies plus one parallelogram per edge (holes included), whose union is exact.
Pieces of a building cut at tile or cell edges need no stitching: the sweep of a union is
the union of the sweeps. The dissolve keeps overlapping shadows from stacking darker under
a translucent fill. Shadows land on the ground only (not on roofs or walls of neighbours).

A landmark with a version in use casts from its GLB mesh instead (app/landmark_shadow.py): in
compute(), its shadow polygon replaces its LoD1 building and parts, inside the same dissolve,
and their footprints do not cut it: the shadow covers the ground around the model. Presets without sun (night, dark) cast nothing.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import struct
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import tools

MAX_SPAN_DEG = 0.05  # ~5 km: one request stays a few seconds at most
READ_MARGIN_M = 400.0  # buildings just outside the area still cast into it…
READ_MARGIN_HEIGHT_M = 150.0  # …up to this tall, however low the sun (dusk at 10° casts 5.7× the height)
METERS_PER_DEG_LAT = 110_574.0
METERS_PER_DEG_LON_EQ = 111_320.0
TIMEOUT = 120
CACHE_ENTRIES = 24

_FIELD_RE = re.compile(r"^[A-Za-z0-9_:.\- ]{1,64}$")


class ShadowError(ValueError):
    pass


@dataclass(frozen=True)
class Sun:
    azimuth: float  # degrees clockwise from north, where the sun is
    altitude: float  # degrees above the horizon

    @classmethod
    def from_light(cls, light: dict[str, Any]) -> Sun:
        """MapLibre `light.position` is [radial, azimuthal, polar]: the light source sits at
        `azimuthal` (clockwise from north with anchor "map") and `polar` from the zenith."""
        _, azimuthal, polar = light["position"]
        return cls(float(azimuthal) % 360, 90.0 - float(polar))

    @property
    def length_factor(self) -> float:
        """Shadow length per metre of height."""
        return 1.0 / math.tan(math.radians(self.altitude))

    def offset_deg(self, lat: float) -> tuple[float, float]:
        """(dlon, dlat) of the shadow of a point 1 m above the ground: away from the sun."""
        k = self.length_factor
        east = -math.sin(math.radians(self.azimuth)) * k
        north = -math.cos(math.radians(self.azimuth)) * k
        return east / (METERS_PER_DEG_LON_EQ * math.cos(math.radians(lat))), north / METERS_PER_DEG_LAT


@dataclass(frozen=True)
class Request:
    path: Path
    layer: str
    zoom: int | None  # PMTiles zoom to read (max zoom); None for other OGR sources
    bbox: tuple[float, float, float, float]  # west, south, east, north (WGS84)
    height: str
    base: str | None
    parts: str | None  # boolean field marking outlines that are drawn as parts instead
    sun: Sun
    # Released landmarks (landmark_shadow.LandmarkShadow): their LoD1 building and parts give
    # way to the shadow cast by their GLB, which their footprints do not cut out either.
    landmarks: tuple = ()


def parse_bbox(text: str) -> tuple[float, float, float, float]:
    try:
        w, s, e, n = (float(v) for v in text.split(","))
    except ValueError as exc:
        raise ShadowError("bbox phải có dạng tây,nam,đông,bắc.") from exc
    if not (-180 <= w < e <= 180 and -85 <= s < n <= 85):
        raise ShadowError("bbox không hợp lệ.")
    if e - w > MAX_SPAN_DEG or n - s > MAX_SPAN_DEG:
        raise ShadowError(f"Vùng quá lớn (tối đa {MAX_SPAN_DEG}° mỗi chiều) — hãy phóng to thêm.")
    return (w, s, e, n)


def check_field(name: str | None, label: str) -> str | None:
    if name in (None, ""):
        return None
    if not _FIELD_RE.fullmatch(name):
        raise ShadowError(f"Tên cột {label} không hợp lệ: {name!r}")
    return name


# ------------------------------------------------------------------ geometry


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _truthy(value: Any) -> bool:
    return value is True or value == 1 or (isinstance(value, str) and value.lower() in ("true", "1", "yes"))


def _clean_ring(ring: list[list[float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for x, y, *_ in ring:
        if not out or (x, y) != out[-1]:
            out.append((x, y))
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def _is_convex(ring: list[tuple[float, float]]) -> bool:
    sign = 0
    n = len(ring)
    for i in range(n):
        (x0, y0), (x1, y1), (x2, y2) = ring[i - 2], ring[i - 1], ring[i]
        cross = (x1 - x0) * (y2 - y1) - (y1 - y0) * (x2 - x1)
        if cross:
            if sign and (cross > 0) != (sign > 0):
                return False
            sign = 1 if cross > 0 else -1
    return sign != 0


def _hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Convex hull (monotone chain), counter-clockwise, not closed."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def half(seq):
        out: list[tuple[float, float]] = []
        for p in seq:
            while len(out) >= 2 and ((out[-1][0] - out[-2][0]) * (p[1] - out[-2][1])
                                     - (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0])) <= 0:
                out.pop()
            out.append(p)
        return out

    lower, upper = half(pts), half(reversed(pts))
    return lower[:-1] + upper[:-1]


def shadow_pieces(rings: list[list[list[float]]], low: float, high: float,
                  dlon: float, dlat: float) -> Iterable[list[list[tuple[float, float]]]]:
    """Polygons (as closed ring lists) whose union is the ground shadow of the prism from
    `low` to `high` metres over this polygon."""
    cleaned = [r for r in (_clean_ring(r) for r in rings) if len(r) >= 3]
    if not cleaned or high <= low:
        return
    lo = (dlon * low, dlat * low)
    hi = (dlon * high, dlat * high)
    if len(cleaned) == 1 and _is_convex(cleaned[0]):
        hull = _hull([(x + dx, y + dy) for dx, dy in (lo, hi) for x, y in cleaned[0]])
        if len(hull) >= 3:
            yield [hull + [hull[0]]]
        return
    for dx, dy in (lo, hi):
        yield [[(x + dx, y + dy) for x, y in r] + [(r[0][0] + dx, r[0][1] + dy)] for r in cleaned]
    vx, vy = hi[0] - lo[0], hi[1] - lo[1]
    for r in cleaned:
        for (x1, y1), (x2, y2) in zip(r, r[1:] + r[:1]):
            # An edge parallel to the sun ray sweeps no area.
            if abs((x2 - x1) * vy - (y2 - y1) * vx) < 1e-18:
                continue
            a, b = (x1 + lo[0], y1 + lo[1]), (x2 + lo[0], y2 + lo[1])
            yield [[a, b, (x2 + hi[0], y2 + hi[1]), (x1 + hi[0], y1 + hi[1]), a]]


def _polygons(geometry: dict[str, Any] | None) -> list[list[list[list[float]]]]:
    if not geometry:
        return []
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    if geometry["type"] == "MultiPolygon":
        return geometry["coordinates"]
    return []


def _feature(kind: str, rings: list) -> str:
    return json.dumps({"type": "Feature", "properties": {"k": kind},
                       "geometry": {"type": "Polygon", "coordinates": rings}}, separators=(",", ":"))


# ------------------------------------------------------------------ pipeline

_UNION_SQL = (
    "SELECT CastToMultiPolygon(ST_CollectionExtract(ST_Difference("
    "(SELECT ST_Union(CASE WHEN ST_IsValid(geometry) THEN geometry ELSE MakeValid(geometry) END) FROM pieces WHERE k = 's'),"
    "(SELECT ST_Union(CASE WHEN ST_IsValid(geometry) THEN geometry ELSE MakeValid(geometry) END) FROM pieces WHERE k = 'f')"
    "), 3)) AS geometry"
)

# The same without footprints to cut out (a difference with nothing is NULL).
_UNION_ONLY_SQL = ("SELECT CastToMultiPolygon(ST_CollectionExtract(ST_Union(CASE WHEN ST_IsValid(geometry) "
                   "THEN geometry ELSE MakeValid(geometry) END), 3)) AS geometry FROM pieces WHERE k = 's'")

EMPTY = b'{"type":"FeatureCollection","features":[]}'


def read_margin_m(sun: Sun) -> float:
    """How far outside the area buildings are read: their shadows may reach into it."""
    return max(READ_MARGIN_M, READ_MARGIN_HEIGHT_M * sun.length_factor)


def compute(req: Request) -> tuple[bytes, dict[str, Any]]:
    """(GeoJSON bytes, stats) of the dissolved ground shadow over req.bbox."""
    t0 = time.monotonic()
    ogr2ogr = tools.require("ogr2ogr")
    w, s, e, n = req.bbox
    lat = (s + n) / 2
    margin = read_margin_m(req.sun)
    m_lat = margin / METERS_PER_DEG_LAT
    m_lon = margin / (METERS_PER_DEG_LON_EQ * math.cos(math.radians(lat)))
    released = {lm.building_id for lm in req.landmarks}
    id_fields = {f for lm in req.landmarks for f in (lm.id_field, lm.parent_field) if f}
    fields = list(dict.fromkeys(f for f in (req.height, req.base, req.parts, *sorted(id_fields)) if f))
    dlon, dlat = req.sun.offset_deg(lat)
    stats = {"buildings": 0, "pieces": 0, "landmarks": 0}
    footprints = 0
    with tempfile.TemporaryDirectory(prefix="bds-shadow-") as tmp:
        src = Path(tmp) / "src.geojsonl"
        pieces = Path(tmp) / "pieces.geojsonl"
        cmd = [ogr2ogr, "-f", "GeoJSONSeq", str(src), str(req.path), req.layer,
               "-spat", f"{w - m_lon}", f"{s - m_lat}", f"{e + m_lon}", f"{n + m_lat}", "-spat_srs", "EPSG:4326",
               "-select", ",".join(fields)]
        if req.zoom is not None:
            cmd += ["-oo", f"ZOOM_LEVEL={req.zoom}"]
        _run(cmd, "đọc building")
        with src.open() as fin, pieces.open("w") as fout:
            for line in fin:
                feat = json.loads(line)
                props = feat.get("properties") or {}
                if req.parts and _truthy(props.get(req.parts)):
                    continue  # its parts carry the heights; the outline is not drawn either
                high = _number(props.get(req.height))
                low = _number(props.get(req.base)) if req.base else None
                low = max(low or 0.0, 0.0)
                polys = _polygons(feat.get("geometry"))
                if not polys:
                    continue
                stats["buildings"] += 1
                if released and any(str(props.get(f)) in released for f in id_fields if props.get(f) is not None):
                    continue  # a released landmark: its GLB shadow goes in below, over its footprint too
                for rings in polys:
                    fout.write(_feature("f", rings) + "\n")
                    footprints += 1
                    if high is None or high <= low:
                        continue
                    for piece in shadow_pieces(rings, low, high, dlon, dlat):
                        fout.write(_feature("s", piece) + "\n")
                        stats["pieces"] += 1
            for lm in req.landmarks:
                for feat in json.loads(lm.path.read_text()).get("features") or []:
                    for rings in _polygons(feat.get("geometry")):
                        fout.write(_feature("s", rings) + "\n")
                        stats["pieces"] += 1
                stats["landmarks"] += 1
        if not stats["pieces"]:
            body = EMPTY
        else:
            out = _run([ogr2ogr, "-f", "GeoJSON", "/vsistdout/", str(pieces), "-dialect", "SQLite",
                        "-sql", _UNION_SQL if footprints else _UNION_ONLY_SQL, "-lco", "COORDINATE_PRECISION=6"], "gộp bóng")
            body = out.encode() if out.strip() else EMPTY
    stats["ms"] = round((time.monotonic() - t0) * 1000)
    return body, stats


def _run(cmd: list[str], what: str) -> str:
    proc = tools.run_capture(cmd, timeout=TIMEOUT)
    if proc.returncode != 0:
        detail = "\n".join(line for line in proc.stderr.splitlines() if "warning" not in line.lower())[-500:]
        raise ShadowError(f"Lỗi khi {what}: {detail or f'mã thoát {proc.returncode}'}")
    return proc.stdout


_lock = threading.Lock()
_cache: OrderedDict[Request, tuple[bytes, dict[str, Any]]] = OrderedDict()


def cached_compute(req: Request) -> tuple[bytes, dict[str, Any]]:
    with _lock:
        hit = _cache.get(req)
        if hit:
            _cache.move_to_end(req)
            return hit
    result = compute(req)
    with _lock:
        _cache[req] = result
        while len(_cache) > CACHE_ENTRIES:
            _cache.popitem(last=False)
    return result


# ------------------------------------------------------------------ whole dataset (convert pipeline)

SHADOW_LAYER = "shadows"
CELL_DEG = 0.01  # ~1.1 km: unions stay small, pieces are clipped to their cell
BLOCK_CELLS = 4  # cells per side of a block (~4.4 km), the unit packed into union jobs
UNION_JOB_MIN_BYTES = 8_000_000  # WKB per ogr2ogr union process: small blocks are packed together…
UNION_JOB_MAX_BYTES = 64_000_000  # …up to this, so the jobs stay balanced across workers
INSERT_BATCH = 20_000

# Pieces live in a plain SQLite table as WKB: ogr2ogr runs this natively (SpatiaLite) with a
# few hundred MB of RAM. Read through GeoJSONSeq instead, GDAL holds ~40× the file in memory.
_VALID = "CASE WHEN ST_IsValid(g0) THEN g0 ELSE MakeValid(g0) END"
_JOB_SQL = (
    "SELECT preset, geometry FROM ("
    "SELECT s.p AS preset, CastToMultiPolygon(ST_CollectionExtract("
    "CASE WHEN f.g IS NULL THEN s.g ELSE ST_Difference(s.g, f.g) END, 3)) AS geometry "
    f"FROM (SELECT c, p, ST_Union({_VALID}) AS g FROM (SELECT c, p, ST_GeomFromWKB(wkb, 4326) AS g0 "
    "FROM pieces WHERE block IN ({blocks}) AND k = 's') GROUP BY c, p) AS s "
    f"LEFT JOIN (SELECT c, ST_Union({_VALID}) AS g FROM (SELECT c, ST_GeomFromWKB(wkb, 4326) AS g0 "
    "FROM pieces WHERE block IN ({blocks}) AND k = 'f') GROUP BY c) AS f ON f.c = s.c"
    ") WHERE geometry IS NOT NULL"
)


class Cancelled(Exception):
    """Raised by compute_dataset when its `cancelled` callback turns true."""


@dataclass
class DatasetSpec:
    gpkg: Path
    table: str
    geom_col: str
    height_col: str
    base_col: str | None
    parts_col: str | None
    suns: dict[str, Sun]  # preset id -> sun
    out: Path  # GeoJSONSeq, one MultiPolygon per (cell, preset) with property `preset`
    work: Path
    cell_deg: float = CELL_DEG
    min_height: float = 0.0  # only buildings taller than this cast (their footprints still cut shade out)
    workers: int = field(default_factory=lambda: max(2, (os.cpu_count() or 4) // 2))


def gpkg_polygons(blob: bytes | None) -> list[list[list[tuple[float, float]]]]:
    """Polygons (lists of rings of (x, y)) of a GeoPackage geometry blob."""
    if not blob or blob[:2] != b"GP":
        return []
    flags = blob[3]
    if flags & 0x10:  # empty
        return []
    envelope = (0, 32, 48, 48, 64)[(flags >> 1) & 0x07]
    polys: list = []
    _wkb(memoryview(blob), 8 + envelope, polys)
    return polys


def _wkb(buf: memoryview, off: int, polys: list) -> int:
    fmt = "<" if buf[off] == 1 else ">"
    (gtype,) = struct.unpack_from(fmt + "I", buf, off + 1)
    off += 5
    if gtype & 0xE0000000:  # EWKB flags
        dims = 2 + bool(gtype & 0x80000000) + bool(gtype & 0x40000000)
        base = gtype & 0x0FFFFFFF
        if gtype & 0x20000000:  # SRID
            off += 4
    else:
        dims = (2, 3, 3, 4)[min(gtype // 1000, 3)]
        base = gtype % 1000
    if base == 3:
        (nrings,) = struct.unpack_from(fmt + "I", buf, off)
        off += 4
        rings = []
        for _ in range(nrings):
            (npts,) = struct.unpack_from(fmt + "I", buf, off)
            off += 4
            vals = struct.unpack_from(f"{fmt}{npts * dims}d", buf, off)
            off += 8 * npts * dims
            rings.append(list(zip(vals[0::dims], vals[1::dims])))
        polys.append(rings)
        return off
    if base in (6, 7):  # MultiPolygon, GeometryCollection
        (count,) = struct.unpack_from(fmt + "I", buf, off)
        off += 4
        for _ in range(count):
            off = _wkb(buf, off, polys)
        return off
    raise ShadowError(f"Kiểu geometry WKB {gtype} chưa hỗ trợ khi tính bóng.")


def _polygon_wkb(rings) -> bytes:
    out = [struct.pack("<BII", 1, 3, len(rings))]
    for ring in rings:
        out.append(struct.pack(f"<I{2 * len(ring)}d", len(ring), *[v for pt in ring for v in pt]))
    return b"".join(out)


def _clip_ring(ring: list[tuple[float, float]], x0: float, y0: float, x1: float, y1: float):
    """Sutherland–Hodgman against an axis-aligned box. Open ring in, open ring (or None) out."""
    pts = ring
    for axis, bound, keep_above in ((0, x0, True), (0, x1, False), (1, y0, True), (1, y1, False)):
        if not pts:
            return None
        out = []
        prev = pts[-1]
        prev_in = (prev[axis] >= bound) if keep_above else (prev[axis] <= bound)
        for cur in pts:
            cur_in = (cur[axis] >= bound) if keep_above else (cur[axis] <= bound)
            if cur_in != prev_in:
                t = (bound - prev[axis]) / (cur[axis] - prev[axis])
                if axis == 0:
                    out.append((bound, prev[1] + t * (cur[1] - prev[1])))
                else:
                    out.append((prev[0] + t * (cur[0] - prev[0]), bound))
            if cur_in:
                out.append(cur)
            prev, prev_in = cur, cur_in
        pts = out
    return pts if len(pts) >= 3 else None


def _clip_polygon(rings, box) -> list | None:
    """Closed rings in, closed rings clipped to box out (None when nothing is left)."""
    x0, y0, x1, y1 = box
    xs = [x for x, _ in rings[0]]
    ys = [y for _, y in rings[0]]
    if min(xs) >= x1 or max(xs) <= x0 or min(ys) >= y1 or max(ys) <= y0:
        return None
    if min(xs) >= x0 and max(xs) <= x1 and min(ys) >= y0 and max(ys) <= y1:
        return rings
    out = []
    for i, ring in enumerate(rings):
        clipped = _clip_ring(ring[:-1] if ring[0] == ring[-1] else ring, x0, y0, x1, y1)
        if clipped is None:
            if i == 0:
                return None
            continue
        out.append(clipped + [clipped[0]])
    return out


def _closed(rings) -> list:
    return [list(r) + [r[0]] if r[0] != r[-1] else list(r) for r in rings]


def _cells(bounds: tuple[float, float, float, float], cell: float) -> Iterable[tuple[int, int]]:
    x0, y0, x1, y1 = bounds
    for cx in range(math.floor(x0 / cell), math.floor(x1 / cell) + 1):
        for cy in range(math.floor(y0 / cell), math.floor(y1 / cell) + 1):
            yield cx, cy


def _bounds(rings) -> tuple[float, float, float, float]:
    xs = [x for x, _ in rings[0]]
    ys = [y for _, y in rings[0]]
    return min(xs), min(ys), max(xs), max(ys)


def _key(x: int, y: int) -> int:
    return x * 1_000_000 + y


def compute_dataset(spec: DatasetSpec, *, progress: Callable[[float], None] = lambda _: None,
                    log: Callable[[str], None] = lambda _: None,
                    cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    """Write the dissolved shadows of every building of a GeoPackage layer to spec.out.

    One pass over the layer: each building's pieces (and footprint) are clipped to the cells
    they cover and stored as WKB with their cell and block. Blocks are then packed into
    ogr2ogr union jobs (GROUP BY cell, preset) run in parallel. Cells never overlap, so
    neither do the shadows.
    """
    t0 = time.monotonic()
    ogr2ogr = tools.require("ogr2ogr")
    cell = spec.cell_deg
    spec.work.mkdir(parents=True, exist_ok=True)
    db_path = spec.work / "pieces.sqlite"
    db_path.unlink(missing_ok=True)
    stats: dict[str, Any] = {"buildings": 0, "casting": 0, "pieces": 0}
    cols = [spec.height_col, spec.base_col, spec.parts_col]
    select = ", ".join(f'"{c}"' if c else "NULL" for c in cols)

    with closing(sqlite3.connect(db_path)) as db:
        db.execute("PRAGMA journal_mode = OFF")
        db.execute("PRAGMA synchronous = OFF")
        db.execute("CREATE TABLE pieces (block INTEGER, c INTEGER, p TEXT, k TEXT, wkb BLOB)")
        batch: list[tuple] = []

        def put(kind: str, preset: str, rings) -> int:
            n = 0
            for cx, cy in _cells(_bounds(rings), cell):
                clipped = _clip_polygon(rings, (cx * cell, cy * cell, (cx + 1) * cell, (cy + 1) * cell))
                if clipped:
                    block = _key(cx // BLOCK_CELLS, cy // BLOCK_CELLS)
                    batch.append((block, _key(cx, cy), preset, kind, _polygon_wkb(clipped)))
                    n += 1
            return n

        with closing(sqlite3.connect(spec.gpkg.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            total = conn.execute(f'SELECT COUNT(*) FROM "{spec.table}"').fetchone()[0] or 1
            rows = conn.execute(f'SELECT "{spec.geom_col}", {select} FROM "{spec.table}"')
            for i, (blob, h, base, parts) in enumerate(rows):
                if i % 2000 == 0:
                    if cancelled():
                        raise Cancelled()
                    progress(60.0 * i / total)
                if len(batch) >= INSERT_BATCH:
                    db.executemany("INSERT INTO pieces VALUES (?, ?, ?, ?, ?)", batch)
                    batch.clear()
                polys = gpkg_polygons(blob)
                if not polys:
                    continue
                stats["buildings"] += 1
                polys = [_closed(rings) for rings in polys]
                for rings in polys:
                    put("f", "", rings)
                high = _number(h)
                low = max(_number(base) or 0.0, 0.0)
                if high is None or high <= max(low, spec.min_height) or (spec.parts_col and _truthy(parts)):
                    continue
                stats["casting"] += 1
                lat = (polys[0][0][0][1] + polys[0][0][1][1]) / 2
                for preset, sun in spec.suns.items():
                    dlon, dlat = sun.offset_deg(lat)
                    for rings in polys:
                        for piece in shadow_pieces(rings, low, high, dlon, dlat):
                            stats["pieces"] += put("s", preset, piece)
        db.executemany("INSERT INTO pieces VALUES (?, ?, ?, ?, ?)", batch)
        batch.clear()
        db.execute("CREATE INDEX pieces_block ON pieces (block, k, c, p)")
        db.commit()
        # Blocks with shadows, packed into union jobs of roughly equal WKB size.
        blocks = db.execute(
            "SELECT block, SUM(LENGTH(wkb)) AS size FROM pieces GROUP BY block HAVING SUM(k = 's') > 0 ORDER BY size DESC"
        ).fetchall()

    total_bytes = sum(size for _, size in blocks)
    target = min(max(total_bytes // (spec.workers * 4), UNION_JOB_MIN_BYTES), UNION_JOB_MAX_BYTES)
    jobs: list[list[int]] = []
    sizes: list[int] = []
    for block, size in blocks:
        if sizes and sizes[-1] + size <= target:
            jobs[-1].append(block)
            sizes[-1] += size
        else:
            jobs.append([block])
            sizes.append(size)
    stats["blocks"] = len(blocks)
    stats["jobs"] = len(jobs)
    stats["pieces_bytes"] = total_bytes
    log(f"{stats['buildings']:,} building ({stats['casting']:,} có bóng), {stats['pieces']:,} mảnh bóng "
        f"({total_bytes / 1e6:.0f} MB WKB) trong {len(blocks):,} khối; gộp bằng {len(jobs)} lượt × {spec.workers} luồng…")

    procs: set[subprocess.Popen] = set()
    lock = threading.Lock()

    def union(index: int, job_blocks: list[int]) -> Path:
        dst = spec.work / f"shadows_{index:05d}.geojsonl"
        dst.unlink(missing_ok=True)
        sql = _JOB_SQL.replace("{blocks}", ",".join(str(b) for b in job_blocks))
        cmd = [ogr2ogr, "-f", "GeoJSONSeq", str(dst), str(db_path), "-sql", sql, "-lco", "COORDINATE_PRECISION=7"]
        if cancelled():
            raise Cancelled()
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        with lock:
            procs.add(proc)
        try:
            _, err = proc.communicate()
        finally:
            with lock:
                procs.discard(proc)
        if proc.returncode != 0:
            if cancelled():
                raise Cancelled()
            detail = "\n".join(x for x in err.splitlines() if "warning" not in x.lower())[-400:]
            raise ShadowError(f"Gộp bóng thất bại (mã {proc.returncode}): {detail}")
        return dst

    outputs: list[Path] = []
    with ThreadPoolExecutor(max_workers=spec.workers) as pool:
        futures = [pool.submit(union, i, job) for i, job in enumerate(jobs)]
        try:
            for i, fut in enumerate(futures):
                while True:
                    if cancelled():
                        raise Cancelled()
                    try:
                        outputs.append(fut.result(timeout=0.5))
                        break
                    except TimeoutError:
                        continue
                progress(60.0 + 40.0 * (i + 1) / len(futures))
        except BaseException:
            for fut in futures:
                fut.cancel()
            with lock:
                for proc in procs:
                    proc.kill()
            raise

    features = 0
    with spec.out.open("w") as fout:
        for path in outputs:
            with path.open() as fin:
                for line in fin:
                    fout.write(line)
                    features += 1
            path.unlink(missing_ok=True)
    db_path.unlink(missing_ok=True)
    stats["features"] = features
    stats["bytes"] = spec.out.stat().st_size
    stats["seconds"] = round(time.monotonic() - t0, 1)
    return stats
