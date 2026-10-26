"""Ground shadow of a landmark, cast from its GLB mesh instead of its LoD1 box.

Same look as app/shadows.py: a flat polygon on the ground, along the sun of a light preset.
Unlike a LoD1 box, it is not cut by the building's footprint: the GLB rarely fills its LoD1
footprint, and the shadow falls on the ground left around the model (under the model itself it
is hidden by the model). Every triangle of the version in use is placed as
the 3D preview places the model (landmark-model.js placementMatrix), its vertices pushed along
the sun ray down to the ground, and the lot unioned:

    glTF root frame (metres, +Y up)
      - anchor ("origin": 0, "bbox": bottom centre of the model box), x scale
      -> (E, N, U) = (x, -z, y), turned `heading` clockwise seen from above
      -> ground point (E, N) + U x (shadow offset per metre of height)
      -> lon / lat on MapLibre's sphere around the placement's lon / lat

The result depends only on the GLB, its placement and the sun, so it is computed
once and kept next to the GLB:

    landmarks/<id>/shadows/v<n>-<preset>-<hash>.geojson

Only versions in use cast (a landmark without one keeps its LoD1 shadow), and only presets with
sun (app/vmap.py Preset.shadow): night and dark cast none.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import glb, shadows, tools, vmap

ALGO = 2  # bump to recompute every cached shadow (2: footprint no longer cut out)
SHADOW_DIR = "shadows"
# MapLibre's sphere: the model is drawn in Mercator, which is conformal, so one metre north or
# east (longitude / cos lat) is this many degrees around the anchor.
METERS_PER_DEG = 40_075_016.686 / 360
MIN_AREA_M2 = 1e-4  # sliver triangles (walls edge-on to the sun) cast nothing

_lock = threading.Lock()


@dataclass(frozen=True)
class LandmarkShadow:
    """A released landmark in a shadow request: its LoD1 pieces (and those of its parts) are
    left out and `path` (its GLB shadow) goes in instead."""
    building_id: str
    id_field: str
    parent_field: str | None
    layer: str  # the PMTiles layer it was registered on
    path: Path


def preset_sun(preset_id: str) -> shadows.Sun | None:
    """The sun of a preset, None when it casts no shadow (night, dark)."""
    preset = vmap.PRESETS.get(preset_id)
    if preset is None or preset.shadow is None:
        return None
    light = vmap.effective_light(preset_id)
    return shadows.Sun.from_light(light) if light and light.get("position") else None


def anchor_point(bounds: list[list[float]] | None, anchor: str) -> tuple[float, float, float]:
    """The model point that stands on the ground, as anchorPoint() in landmark-model.js."""
    if anchor == "bbox" and bounds:
        (x0, y0, z0), (x1, _, z1) = bounds
        return ((x0 + x1) / 2, y0, (z0 + z1) / 2)
    return (0.0, 0.0, 0.0)


def place(p: tuple[float, float, float], anchor: tuple[float, float, float],
          placement: dict[str, Any]) -> tuple[float, float, float]:
    """A model point (glTF root frame) -> (east, north, up) metres from the placement point."""
    s = placement["scale"]
    x, y, z = ((p[i] - anchor[i]) * s for i in range(3))
    e, n = x, -z
    h = math.radians(placement["heading"])
    return (e * math.cos(h) + n * math.sin(h), -e * math.sin(h) + n * math.cos(h), y)


def ground_triangles(path: Path, model: dict[str, Any], placement: dict[str, Any],
                     sun: shadows.Sun) -> list[list[tuple[float, float]]]:
    """The shadow triangles of a GLB on the ground, as open rings of (east, north) metres."""
    k = sun.length_factor
    de = -math.sin(math.radians(sun.azimuth)) * k
    dn = -math.cos(math.radians(sun.azimuth)) * k
    anchor = anchor_point(model.get("bounds"), placement["anchor"])
    out = []
    for tri in glb.triangles(path):
        pts = []
        for p in tri:
            e, n, u = place(p, anchor, placement)
            u = max(u, 0.0)
            pts.append((e + de * u, n + dn * u))
        (ax, ay), (bx, by), (cx, cy) = pts
        if abs((bx - ax) * (cy - ay) - (cx - ax) * (by - ay)) / 2 >= MIN_AREA_M2:
            out.append(pts)
    return out


def compute(path: Path, model: dict[str, Any], placement: dict[str, Any], sun: shadows.Sun) -> bytes:
    """GeoJSON of the dissolved ground shadow of a placed GLB."""
    lon0, lat0 = placement["lon"], placement["lat"]
    kx = 1 / (METERS_PER_DEG * math.cos(math.radians(lat0)))
    ky = 1 / METERS_PER_DEG
    tris = ground_triangles(path, model, placement, sun)
    if not tris:
        return shadows.EMPTY
    ogr2ogr = tools.require("ogr2ogr")
    with tempfile.TemporaryDirectory(prefix="bds-lmk-shadow-") as tmp:
        pieces = Path(tmp) / "pieces.geojsonl"
        with pieces.open("w") as fout:
            for tri in tris:
                ring = [(round(lon0 + e * kx, 9), round(lat0 + n * ky, 9)) for e, n in tri]
                fout.write(shadows._feature("s", [ring + ring[:1]]) + "\n")
        out = shadows._run([ogr2ogr, "-f", "GeoJSON", "/vsistdout/", str(pieces), "-dialect", "SQLite",
                            "-sql", shadows._UNION_ONLY_SQL, "-lco", "COORDINATE_PRECISION=7"], "gộp bóng landmark")
    return out.encode() if out.strip() else shadows.EMPTY


def _key(version: dict[str, Any], sun: shadows.Sun) -> str:
    text = json.dumps([ALGO, version["sha256"], version["placement"], (version.get("model") or {}).get("bounds"),
                       round(sun.azimuth, 6), round(sun.altitude, 6)],
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def for_version(folder: Path, rec: dict[str, Any], version: dict[str, Any], preset_id: str) -> Path:
    """The shadow file of one version of a landmark (folder: its library folder) for a preset,
    computed on first use. Raises glb.GlbError / ShadowError when it cannot be cast, ValueError
    for a preset without sun."""
    sun = preset_sun(preset_id)
    if sun is None:
        raise shadows.ShadowError("Chế độ này không có nắng nên không đổ bóng.")
    out_dir = folder / SHADOW_DIR
    stem = f"v{version['version']}-{preset_id}-"
    out = out_dir / f"{stem}{_key(version, sun)}.geojson"
    if out.is_file():
        return out
    with _lock:
        if out.is_file():
            return out
        body = compute(folder / version["file"], version.get("model") or {}, version["placement"], sun)
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f".{out.name}.tmp")
        tmp.write_bytes(body)
        os.replace(tmp, out)
        for old in out_dir.glob(f"{stem}*.geojson"):
            if old != out:
                old.unlink(missing_ok=True)  # an older placement or sun
    return out


def released(library, path: Path, preset_id: str) -> tuple[list[LandmarkShadow], list[str]]:
    """The released landmarks of one PMTiles with their GLB shadow for a preset, and why some
    fell back to their LoD1 shadow (one line each)."""
    found, problems = [], []
    for rec in library.released(path):
        version = library._version(rec, rec["in_use"])
        src = rec.get("source") or {}
        if not src.get("id_field"):
            continue
        try:
            file = for_version(library.folder(rec["id"]), rec, version, preset_id)
        except (glb.GlbError, shadows.ShadowError, tools.ToolMissingError, OSError) as exc:
            problems.append(f"{rec['name'] or rec['building_id']}: {exc}")
            continue
        found.append(LandmarkShadow(rec["building_id"], src["id_field"], src.get("parent_field"), src.get("layer") or "", file))
    return found, problems


def add_to_manifest(library, data: dict[str, Any]) -> dict[str, Any]:
    """The landmarks manifest (Library.manifest) with, for each version in use, its shadow file
    per preset with sun (paths relative to the library, like `file`) and, once, the look and sun
    of those presets. A shadow that cannot be cast is left out with its reason."""
    suns = {pid: sun for pid in vmap.PRESETS if (sun := preset_sun(pid)) is not None}
    for item in data["landmarks"]:
        rec = library._read(item["id"])
        version = library._version(rec, item["version"])
        folder = library.folder(item["id"])
        item["shadows"], errors = {}, {}
        for pid in suns:
            try:
                item["shadows"][pid] = for_version(folder, rec, version, pid).relative_to(library.root).as_posix()
            except (glb.GlbError, shadows.ShadowError, tools.ToolMissingError, OSError) as exc:
                errors[pid] = str(exc)
        if errors:
            item["shadow_errors"] = errors
    data["shadow_presets"] = {
        pid: {**vmap.PRESETS[pid].shadow, "sun": {"azimuth": sun.azimuth, "altitude": sun.altitude}}
        for pid, sun in suns.items()
    }
    return data
