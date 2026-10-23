"""Where a landmark model sits: the footprint of one building_id, for the 3D preview and the
landmark library (app/landmark_library.py).

A landmark (GLB / LMK3) replaces the extrusion of one building and is anchored at the
centroid of its footprint, on the ground. The preview reads the footprint from the PMTiles
being checked, at its max zoom: GDAL clips each tile's piece of the building to the tile edge
(MVT / PMTiles open option CLIP, on by default), so the pieces never overlap and the
area-weighted centroid of the pieces is the centroid of the whole footprint.

A building split into parts (outline with has_parts, parts with parent_building_id) is found
by its own id first, then as the parent of its parts.
"""

from __future__ import annotations

import json
import math
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import shadows, tools

SEARCH_M = 600.0  # half-size of the box searched around the point the user is looking at


class LandmarkError(ValueError):
    pass


@dataclass(frozen=True)
class Lookup:
    path: Path
    layer: str
    zoom: int | None  # PMTiles zoom to read (max zoom); None for other OGR sources
    id_field: str
    building_id: str
    lon: float  # where to look: the clicked point or the centre of the view
    lat: float
    height: str | None = None
    base: str | None = None
    parent: str | None = None


def _key(value: Any) -> str | None:
    """The id as the map sees it (MapLibre ["to-string", ["get", …]]): 12.0 -> "12"."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def ring_moments(ring: list[list[float]], lon0: float, lat0: float) -> tuple[float, float, float]:
    """(area m², Σx·A, Σy·A) of one ring in local metres around (lon0, lat0), area unsigned."""
    kx = shadows.METERS_PER_DEG_LON_EQ * math.cos(math.radians(lat0))
    ky = shadows.METERS_PER_DEG_LAT
    pts = [((x - lon0) * kx, (y - lat0) * ky) for x, y, *_ in ring]
    if len(pts) > 1 and pts[0] != pts[-1]:
        pts.append(pts[0])
    a = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        cross = x1 * y2 - x2 * y1
        a += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if a == 0:
        return 0.0, 0.0, 0.0
    sign = 1 if a > 0 else -1
    return abs(a) / 2, sign * cx / 6, sign * cy / 6


def footprint(polygons: list[list[list[list[float]]]], lon0: float, lat0: float) -> dict[str, Any]:
    """Area, centroid and bounding box of polygons that do not overlap (holes subtracted)."""
    area = mx = my = 0.0
    xs: list[float] = []
    ys: list[float] = []
    for rings in polygons:
        for i, ring in enumerate(rings):
            a, sx, sy = ring_moments(ring, lon0, lat0)
            sign = 1 if i == 0 else -1
            area += sign * a
            mx += sign * sx
            my += sign * sy
            if i == 0:
                xs += [p[0] for p in ring]
                ys += [p[1] for p in ring]
    if area <= 0:
        raise LandmarkError("Footprint của building có diện tích bằng 0.")
    kx = shadows.METERS_PER_DEG_LON_EQ * math.cos(math.radians(lat0))
    ky = shadows.METERS_PER_DEG_LAT
    lon, lat = lon0 + mx / area / kx, lat0 + my / area / ky
    w, s, e, n = min(xs), min(ys), max(xs), max(ys)
    return {
        "centroid": [round(lon, 8), round(lat, 8)],
        "area_m2": round(area, 2),
        "bbox": [w, s, e, n],
        "size_m": [round((e - w) * kx, 2), round((n - s) * ky, 2)],
    }


_OUTLINE_SQL = (
    "SELECT ST_Union(CASE WHEN ST_IsValid(geometry) THEN geometry ELSE MakeValid(geometry) END) AS geometry FROM pieces"
)


def outline(polygons: list[list[list[list[float]]]]) -> dict[str, Any]:
    """The footprint as one GeoJSON geometry: the tile pieces of a building dissolved back together.

    If GDAL cannot dissolve them, the pieces themselves (a MultiPolygon) still cover the footprint.
    """
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    pieces_geom = {"type": "MultiPolygon", "coordinates": polygons}
    ogr2ogr = tools.require("ogr2ogr")
    with tempfile.TemporaryDirectory(prefix="bds-landmark-") as tmp:
        pieces = Path(tmp) / "pieces.geojsonl"
        pieces.write_text("".join(
            json.dumps({"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": p}}) + "\n"
            for p in polygons))
        proc = tools.run_capture([ogr2ogr, "-f", "GeoJSON", "/vsistdout/", str(pieces), "-dialect", "SQLite",
                                  "-sql", _OUTLINE_SQL, "-lco", "COORDINATE_PRECISION=7"], timeout=shadows.TIMEOUT)
    try:
        geom = json.loads(proc.stdout)["features"][0]["geometry"] if proc.returncode == 0 else None
    except (ValueError, LookupError, TypeError):
        geom = None
    return geom if geom and geom.get("type") in ("Polygon", "MultiPolygon") else pieces_geom


def lookup(req: Lookup, with_outline: bool = False) -> dict[str, Any]:
    """Footprint, height and base of one building, read around (req.lon, req.lat).

    with_outline adds the footprint itself ("geometry"), for the landmark library.
    """
    ogr2ogr = tools.require("ogr2ogr")
    dlat = SEARCH_M / shadows.METERS_PER_DEG_LAT
    dlon = SEARCH_M / (shadows.METERS_PER_DEG_LON_EQ * math.cos(math.radians(req.lat)))
    fields = list(dict.fromkeys(f for f in (req.id_field, req.parent, req.height, req.base) if f))
    with tempfile.TemporaryDirectory(prefix="bds-landmark-") as tmp:
        out = Path(tmp) / "near.geojsonl"
        cmd = [ogr2ogr, "-f", "GeoJSONSeq", str(out), str(req.path), req.layer,
               "-spat", f"{req.lon - dlon}", f"{req.lat - dlat}", f"{req.lon + dlon}", f"{req.lat + dlat}",
               "-spat_srs", "EPSG:4326", "-select", ",".join(fields)]
        if req.zoom is not None:
            cmd += ["-oo", f"ZOOM_LEVEL={req.zoom}"]
        proc = tools.run_capture(cmd, timeout=shadows.TIMEOUT)
        if proc.returncode != 0:
            detail = "\n".join(line for line in proc.stderr.splitlines() if "warning" not in line.lower())[-500:]
            raise LandmarkError(f"Lỗi khi đọc building: {detail or f'mã thoát {proc.returncode}'}")
        own: list[dict[str, Any]] = []
        parts: list[dict[str, Any]] = []
        with out.open() as fh:
            for line in fh:
                feat = json.loads(line)
                props = feat.get("properties") or {}
                if _key(props.get(req.id_field)) == req.building_id:
                    own.append(feat)
                elif req.parent and _key(props.get(req.parent)) == req.building_id:
                    parts.append(feat)
    feats, matched = (own, "id") if own else (parts, "parent")
    polygons = [p for f in feats for p in shadows._polygons(f.get("geometry"))]
    if not polygons:
        raise LandmarkError(
            f"Không thấy {req.id_field} = {req.building_id} trong khoảng {SEARCH_M:g} m quanh điểm đang xem. "
            "Hãy dịch bản đồ tới building đó rồi thử lại."
        )
    result = footprint(polygons, req.lon, req.lat)
    heights = [h for f in feats if (h := shadows._number((f.get("properties") or {}).get(req.height))) is not None] if req.height else []
    bases = [b for f in feats if (b := shadows._number((f.get("properties") or {}).get(req.base))) is not None] if req.base else []
    out = {
        "id": req.building_id,
        "matched": matched,  # "id": the building itself; "parent": the union of its parts
        "pieces": len(polygons),
        **result,
        "height": max(heights) if heights else None,
        "base": max(min(bases), 0.0) if bases else 0.0,
        "height_field": req.height,
    }
    if with_outline:
        out["geometry"] = outline(polygons)
    return out
