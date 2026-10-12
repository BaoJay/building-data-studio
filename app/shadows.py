"""Precomputed building shadows (prototype) for the 3D preview.

MapLibre cannot cast shadows, but with a fixed sun per light preset the ground shadow of
every building can be computed ahead of time and drawn as a flat `fill` layer under the
extrusions. This module does it for one area of a PMTiles file:

  ogr2ogr (PMTiles, max zoom, bbox) -> GeoJSONSeq footprints with height / base
    -> Python: shadow pieces per building (footprint shifted to its base and top height,
       plus one parallelogram per edge = the exact sweep of the prism along the sun ray)
    -> ogr2ogr SQLite/SpatiaLite: union of all pieces minus all footprints -> GeoJSON

Pieces of a building cut at tile edges need no stitching: the sweep of a union is the
union of the sweeps. The dissolve keeps overlapping shadows from stacking darker under a
translucent fill. Shadows land on the ground only (not on roofs or walls of neighbours).
"""

from __future__ import annotations

import json
import math
import re
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import tools

MAX_SPAN_DEG = 0.05  # ~5 km: one request stays a few seconds at most
READ_MARGIN_M = 400.0  # buildings just outside the area still cast into it
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


def shadow_pieces(rings: list[list[list[float]]], low: float, high: float,
                  dlon: float, dlat: float) -> Iterable[list[list[tuple[float, float]]]]:
    """Polygons (as ring lists) whose union is the ground shadow of the prism from `low` to
    `high` metres over this polygon: the polygon shifted to both heights plus the
    parallelogram swept by every edge (holes included, so courtyards are exact)."""
    cleaned = [r for r in (_clean_ring(r) for r in rings) if len(r) >= 3]
    if not cleaned or high <= low:
        return
    lo = (dlon * low, dlat * low)
    hi = (dlon * high, dlat * high)
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

EMPTY = b'{"type":"FeatureCollection","features":[]}'


def compute(req: Request) -> tuple[bytes, dict[str, Any]]:
    """(GeoJSON bytes, stats) of the dissolved ground shadow over req.bbox."""
    t0 = time.monotonic()
    ogr2ogr = tools.require("ogr2ogr")
    w, s, e, n = req.bbox
    lat = (s + n) / 2
    m_lat = READ_MARGIN_M / METERS_PER_DEG_LAT
    m_lon = READ_MARGIN_M / (METERS_PER_DEG_LON_EQ * math.cos(math.radians(lat)))
    fields = [f for f in (req.height, req.base, req.parts) if f]
    dlon, dlat = req.sun.offset_deg(lat)
    stats = {"buildings": 0, "pieces": 0}
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
                for rings in polys:
                    fout.write(_feature("f", rings) + "\n")
                    if high is None or high <= low:
                        continue
                    for piece in shadow_pieces(rings, low, high, dlon, dlat):
                        fout.write(_feature("s", piece) + "\n")
                        stats["pieces"] += 1
        if not stats["pieces"]:
            body = EMPTY
        else:
            out = _run([ogr2ogr, "-f", "GeoJSON", "/vsistdout/", str(pieces), "-dialect", "SQLite",
                        "-sql", _UNION_SQL, "-lco", "COORDINATE_PRECISION=6"], "gộp bóng")
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
