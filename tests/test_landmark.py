"""Landmark in the 3D preview: the light / colour math of app/static/landmark-light.js
(ported here, checked against MapLibre's own code and against pixels MapLibre drew, and run
under Node against this port) and the footprint lookup that anchors the model (app/landmark.py)."""

import json
import math
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import landmark, shadows, tools, vmap

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
LIGHT_JS = STATIC / "landmark-light.js"
PRESET_IDS = ["dawn", "day", "dusk", "night", "dark"]
BUILDING = "#ddcca9"  # the inferred mobile building colour (vmap.INFERRED_BUILDING)

needs_gdal = pytest.mark.skipif(tools.which("ogr2ogr") is None, reason="needs GDAL (ogr2ogr)")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js to run landmark-light.js")


# ------------------------------------------------------------------ port of landmark-light.js

LUMA = (0.2126, 0.7152, 0.0722)
DEFAULTS = {"anchor": "viewport", "color": "#ffffff", "intensity": 0.5, "position": [1.15, 210, 30]}


def resolve(light):
    light = {**DEFAULTS, **(light or {})}
    r, g, b, a = vmap.parse_color(light["color"])
    return {"anchor": light["anchor"], "color": [r * a, g * a, b * a], "intensity": float(light["intensity"]),
            "position": [float(v) for v in light["position"]]}


def spherical_to_cartesian(position):
    """MapLibre's Light position: tile frame (x east, y south, z up)."""
    r, azimuthal, polar = position
    a, p = math.radians(azimuthal + 90), math.radians(polar)
    return [r * math.cos(a) * math.sin(p), r * math.sin(a) * math.sin(p), r * math.cos(p)]


def light_enu(light, bearing=0.0):
    r, azimuthal, polar = light["position"]
    a = math.radians(azimuthal + (bearing if light["anchor"] == "viewport" else 0))
    p = math.radians(polar)
    return [r * math.sin(a) * math.sin(p), r * math.cos(a) * math.sin(p), r * math.cos(p)]


def enu_to_model(v, heading):
    e, n, u = v
    h = math.radians(heading)
    e1, n1 = e * math.cos(h) - n * math.sin(h), e * math.sin(h) + n * math.cos(h)
    return [e1, u, -n1]


def model_to_enu(v, heading):
    x, y, z = v
    h = math.radians(heading)
    e1, n1 = x, -z
    return [e1 * math.cos(h) + n1 * math.sin(h), -e1 * math.sin(h) + n1 * math.cos(h), y]


def mix(a, b, t):
    return a + (b - a) * t


def clamp(v, lo, hi):
    return min(max(v, lo), hi)


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def wall_gradient(n, t, height_m, intensity, vertical_gradient=True):
    if not vertical_gradient:
        return 1.0
    lo = mix(0.7, 0.98, 1 - intensity)
    g = mix(lo, clamp(math.sqrt(max(height_m, 0) / 150), lo, 1), clamp(t, 0, 1))
    wall = 1 - abs(n[2]) if abs(n[1]) > 1e-5 else 0.0
    return mix(1.0, g, wall)


def shade(base, n, light, *, bearing=0.0, vertical_gradient=True, height_m=0.0, t=1.0):
    lum = dot(base, LUMA)
    i = light["intensity"]
    d = clamp(dot(n, light_enu(light, bearing)), 0, 1)
    d = mix(1 - i, max(1 - lum + i, 1), d)
    d *= wall_gradient(n, t, height_m, i, vertical_gradient)
    return [clamp((b + 0.03) * d * c, 0.3 * (1 - c), 1) for b, c in zip(base, light["color"])]


def style_light(preset):
    return json.loads(vmap.PRESETS[preset].local_copy.read_text()).get("light")


def rgb(hex_color):
    return list(vmap.parse_color(hex_color)[:3])


# ------------------------------------------------------------------ against MapLibre's code


def _maplibre_dot(edge_start, edge_end, light, bearing=0.0):
    """dot(N, u_lightpos) exactly as MapLibre 5.13 computes it for one wall: the bucket's
    normal perp(p[i] - p[i-1]) (perp: (x, y) -> (-y, x)) in tile coordinates, and the light
    from sphericalToCartesian, turned by the bearing when anchored to the viewport."""
    dx, dy = edge_end[0] - edge_start[0], edge_end[1] - edge_start[1]
    length = math.hypot(dx, dy)
    normal = (-dy / length, dx / length, 0.0)
    lx, ly, lz = spherical_to_cartesian(light["position"])
    if light["anchor"] == "viewport":
        b = math.radians(bearing)
        lx, ly = lx * math.cos(b) - ly * math.sin(b), lx * math.sin(b) + ly * math.cos(b)
    return dot(normal, (lx, ly, lz))


def _rect_tile_ring(angle_deg):
    """A 40 × 20 rectangle turned by angle_deg, in tile coordinates (y down), wound like an MVT
    exterior ring (positive surveyor's area, i.e. clockwise on screen)."""
    a = math.radians(angle_deg)
    pts = [(-20, -10), (20, -10), (20, 10), (-20, 10)]
    ring = [(x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)) for x, y in pts]
    area = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1])) / 2
    assert area > 0
    return ring


@pytest.mark.parametrize("light", [
    {"anchor": "map", "position": [1.15, 120, 50]},
    {"anchor": "map", "position": [1.15, 240, 80]},
    {"anchor": "map", "position": [1.5, 7, 33]},
    {"anchor": "viewport", "position": [1.15, 210, 30]},
])
@pytest.mark.parametrize("angle", [0, 17, 90, 133, 250])
@pytest.mark.parametrize("bearing", [0, 35, 180])
def test_light_vector_reproduces_maplibre_walls(light, angle, bearing):
    """MapLibre's wall normals point into the building and its azimuth is off by 90°; the ENU
    vector with outward normals gives the same dot product for every wall."""
    light = resolve(light)
    ring = _rect_tile_ring(angle)
    for p0, p1 in zip(ring, ring[1:] + ring[:1]):
        # Outward normal of the edge in east-north-up. Flipping y keeps the ring clockwise as
        # drawn (north up), so outward is the left-hand normal.
        e0, e1 = (p0[0], -p0[1]), (p1[0], -p1[1])
        de, dn = e1[0] - e0[0], e1[1] - e0[1]
        length = math.hypot(de, dn)
        outward = (-dn / length, de / length, 0.0)
        assert dot(outward, light_enu(light, bearing)) == pytest.approx(_maplibre_dot(p0, p1, light, bearing), abs=1e-12)
    assert light_enu(light, bearing)[2] == pytest.approx(spherical_to_cartesian(light["position"])[2])


def test_the_wall_facing_the_light_azimuth_is_the_brightest():
    light = resolve({"anchor": "map", "position": [1.15, 120, 50]})
    walls = {az: shade(rgb(BUILDING), [math.sin(math.radians(az)), math.cos(math.radians(az)), 0], light, vertical_gradient=False)
             for az in range(0, 360, 15)}
    assert max(walls, key=lambda az: sum(walls[az])) == 120


def test_viewport_light_turns_with_the_map():
    light = resolve({})  # spec defaults: anchor viewport, azimuth 210
    assert light_enu(light, 90) == pytest.approx(light_enu({**light, "anchor": "map", "position": [1.15, 300, 30]}))
    fixed = resolve({"anchor": "map"})
    assert light_enu(fixed, 90) == light_enu(fixed, 0)


@pytest.mark.parametrize("preset", ["dawn", "day", "dusk"])
def test_lit_side_faces_the_sun_that_casts_the_shadows(preset):
    """The landmark's light and the precomputed shadows read the same `light`: the light vector
    points at the sun, the shadow falls the other way."""
    light = resolve(style_light(preset))
    e, n, _ = light_enu(light)
    dlon, dlat = shadows.Sun.from_light(style_light(preset)).offset_deg(0.0)
    east, north = dlon * shadows.METERS_PER_DEG_LON_EQ, dlat * shadows.METERS_PER_DEG_LAT
    assert math.atan2(e, n) == pytest.approx(math.atan2(-east, -north))


# ------------------------------------------------------------------ model frame


@pytest.mark.parametrize("heading", [0, 35, 90, 180, 271.5])
def test_model_frame_round_trip_keeps_the_lighting(heading):
    light = resolve(style_light("dusk"))
    lenu = light_enu(light)
    lmodel = enu_to_model(lenu, heading)
    for n in ([1, 0, 0], [0, 1, 0], [0, 0, 1], [0.6, 0, -0.8], [0.3, 0.9, 0.316]):
        assert dot(n, lmodel) == pytest.approx(dot(model_to_enu(n, heading), lenu))
        assert enu_to_model(model_to_enu(n, heading), heading) == pytest.approx(n)


def test_model_axes_on_the_map():
    # glTF is Y-up: +X east, +Y up, front (+Z) south at heading 0.
    assert model_to_enu([1, 0, 0], 0) == pytest.approx([1, 0, 0])
    assert model_to_enu([0, 1, 0], 0) == pytest.approx([0, 0, 1])
    assert model_to_enu([0, 0, 1], 0) == pytest.approx([0, -1, 0])
    # heading turns the model clockwise seen from above: +X faces south at 90°.
    assert model_to_enu([1, 0, 0], 90) == pytest.approx([0, -1, 0])
    assert model_to_enu([0, 0, 1], 90) == pytest.approx([-1, 0, 0])


# ------------------------------------------------------------------ colour


def test_light_colour_is_raw_srgb_premultiplied():
    assert resolve({"color": "#feca8b"})["color"] == pytest.approx([254 / 255, 202 / 255, 139 / 255])
    assert resolve({"color": "rgba(255, 255, 255, 0.5)"})["color"] == pytest.approx([0.5, 0.5, 0.5])
    assert resolve({}) == {"anchor": "viewport", "color": [1.0, 1.0, 1.0], "intensity": 0.5, "position": [1.15, 210, 30]}


def test_wall_gradient():
    i = 0.2
    lo = mix(0.7, 0.98, 1 - i)
    south = [0, -1, 0]
    assert wall_gradient([0, 0, 1], 0, 336, i) == 1  # roofs never
    assert wall_gradient([1, 0, 0], 0, 336, i) == 1  # MapLibre: walls facing exactly east / west never either
    assert wall_gradient(south, 0, 336, i) == pytest.approx(lo)
    assert wall_gradient(south, 1, 336, i) == 1
    assert wall_gradient(south, 1, 24, i) == pytest.approx(lo)  # low building: sqrt(24 / 150) < lo
    assert wall_gradient(south, 0, 336, i, vertical_gradient=False) == 1
    slope = [0, -math.sqrt(0.5), math.sqrt(0.5)]
    assert wall_gradient(slope, 0, 336, i) == pytest.approx(mix(1, lo, 1 - math.sqrt(0.5)))


# Pixels MapLibre GL JS 5.13 drew in the preview (2026-10-16) for an opaque #ddcca9 extrusion,
# 336 m, vertical gradient on, under each VMAP style's light (repo copies). Faces: roof; walls
# facing south / north at t (height fraction); east (no gradient: MapLibre skips exact E / W walls).
MEASURED = {
    "dawn": {"roof": (206, 152, 87), "S0.5": (170, 125, 72), "S0.85": (178, 131, 75), "N0.5": (136, 100, 57),
             "N0.85": (142, 104, 60), "E": (208, 153, 88)},
    "day": {"roof": (229, 212, 177), "S0.5": (193, 179, 149), "S0.85": (199, 184, 153), "N0.5": (176, 163, 136),
            "N0.85": (181, 167, 140), "E": (183, 169, 141)},
    "dusk": {"roof": (191, 135, 78), "S0.5": (200, 141, 82), "S0.85": (206, 145, 84), "N0.5": (175, 124, 71),
             "N0.85": (180, 127, 73), "E": (182, 129, 74)},
    "night": {"roof": (58, 56, 59), "S0.5": (58, 56, 51), "N0.5": (58, 56, 51), "E": (58, 56, 51)},
    "dark": {"roof": (156, 152, 137), "S0.5": (137, 133, 121), "S0.85": (141, 137, 125), "N0.5": (118, 115, 104),
             "N0.85": (122, 118, 107), "E": (123, 120, 109)},
}
FACES = {"roof": [0, 0, 1], "S": [0, -1, 0], "N": [0, 1, 0], "E": [1, 0, 0]}


def _face(key):
    m = re.fullmatch(r"([A-Za-z]+)([\d.]*)", key)
    return FACES[m.group(1)], float(m.group(2) or 1)


@pytest.mark.parametrize("preset", PRESET_IDS)
def test_shade_matches_pixels_drawn_by_maplibre(preset):
    light = resolve(style_light(preset))
    for key, expected in MEASURED[preset].items():
        n, t = _face(key)
        got = [round(v * 255) for v in shade(rgb(BUILDING), n, light, height_m=336, t=t)]
        assert all(abs(g - e) <= 1 for g, e in zip(got, expected)), (key, got, expected)


def test_night_floors_every_wall_to_the_light_colour():
    """Under the night light (#3f4455) every wall of the building colour lands on MapLibre's
    floor 0.3 × (1 - light colour): a landmark's texture is flat there, only emissive shows."""
    light = resolve(style_light("night"))
    floor = [0.3 * (1 - c) for c in light["color"]]
    for n in ([0, -1, 0], [0, 1, 0], [1, 0, 0], [-1, 0, 0]):
        assert shade(rgb(BUILDING), n, light, height_m=336, t=1) == pytest.approx(floor)


def test_emissive_is_for_the_night_style_only():
    lit = {p: json.loads(vmap.PRESETS[p].local_copy.read_text()).get("metadata", {}).get("tasco:light-preset") == "night"
           for p in PRESET_IDS}
    assert lit == {"dawn": False, "day": False, "dusk": False, "night": True, "dark": False}


def test_shader_uses_the_same_constants():
    src = LIGHT_JS.read_text()
    frag = src[src.index("export const FRAGMENT_SHADER"):]
    for needle in ("vec3(0.2126, 0.7152, 0.0722)", "base + 0.03", "clamp(dot(n, u_lightpos), 0.0, 1.0)",
                   "mix(1.0 - u_lightintensity, max(1.0 - colorvalue + u_lightintensity, 1.0), directional)",
                   "0.3 * (1.0 - u_lightcolor)", "mix(0.7, 0.98, 1.0 - u_lightintensity)", "/ 150.0",
                   "abs(dot(n, u_north)) > 1e-5 ? 1.0 - abs(n.y) : 0.0"):
        assert needle in frag, needle


# ------------------------------------------------------------------ the JS module itself


@needs_node
def test_js_module_matches_this_port(tmp_path):
    shutil.copy(LIGHT_JS, tmp_path / "landmark-light.mjs")
    lights = [style_light(p) for p in PRESET_IDS] + [{}, {"color": "rgba(255,255,255,0.5)", "anchor": "viewport"}]
    normals = [[0, 0, 1], [0, -1, 0], [1, 0, 0], [0.6, -0.8, 0], [0, -0.6, 0.8]]
    cases = [{"light": l, "bearing": b, "heading": h, "n": n, "t": t}
             for l in lights for b in (0, 35) for h in (0, 35) for n in normals for t in (0.25, 1)]
    runner = tmp_path / "run.mjs"
    runner.write_text("""
import * as LL from "./landmark-light.mjs";
import { readFileSync } from "node:fs";
const cases = JSON.parse(readFileSync(0, "utf8"));
const base = LL.parseColor("%s").slice(0, 3);
console.log(JSON.stringify(cases.map((c) => {
  const light = LL.resolveLight(c.light);
  return {
    light: { anchor: light.anchor, color: light.color, intensity: light.intensity, position: light.position },
    enu: LL.lightVectorENU(light, c.bearing),
    uniforms: LL.landmarkUniforms(light, { heading: c.heading, bearing: c.bearing }),
    back: LL.modelToEnu(LL.enuToModel(c.n, c.heading), c.heading),
    shade: LL.shade(base, c.n, light, { bearing: c.bearing, heightM: 336, t: c.t }),
    night: LL.isNightStyle({ "tasco:light-preset": "night" }) && !LL.isNightStyle({ "tasco:style-variant": "dark" }),
  };
})));
""" % BUILDING)
    proc = subprocess.run(["node", str(runner)], input=json.dumps(cases), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout)
    for c, r in zip(cases, results):
        light = resolve(c["light"])
        assert r["light"]["anchor"] == light["anchor"]
        assert r["light"]["color"] == pytest.approx(light["color"])
        assert (r["light"]["intensity"], r["light"]["position"]) == (light["intensity"], light["position"])
        assert r["enu"] == pytest.approx(light_enu(light, c["bearing"]), abs=1e-12)
        assert r["uniforms"]["u_lightpos"] == pytest.approx(enu_to_model(light_enu(light, c["bearing"]), c["heading"]), abs=1e-12)
        assert r["uniforms"]["u_north"] == pytest.approx(enu_to_model([0, 1, 0], c["heading"]), abs=1e-12)
        assert r["back"] == pytest.approx(c["n"], abs=1e-12)
        expected = shade(rgb(BUILDING), c["n"], light, bearing=c["bearing"], height_m=336, t=c["t"])
        assert r["shade"] == pytest.approx(expected, abs=1e-12)
        assert r["night"] is True


# ------------------------------------------------------------------ footprint lookup

M_LON = shadows.METERS_PER_DEG_LON_EQ  # buildings near (0, 0)
M_LAT = shadows.METERS_PER_DEG_LAT


def _ring(*xy_m, x0=0.0):
    pts = [[x0 + x / M_LON, y / M_LAT] for x, y in xy_m]
    return pts + [pts[0]]


# L-shape: a 30 × 10 bar (centroid (15, 5)) plus a 10 × 20 leg on top of it (centroid (5, 20)):
# 500 m², centroid ((15·300 + 5·200) / 500, (5·300 + 20·200) / 500) = (11, 11).
L_SHAPE = [(0, 0), (30, 0), (30, 10), (10, 10), (10, 30), (0, 30)]
L_AREA = 500
L_CENTROID = (11, 11)


def test_footprint_of_pieces_is_the_whole_footprint():
    whole = landmark.footprint([[_ring(*L_SHAPE)]], 0.0, 0.0)
    assert whole["area_m2"] == pytest.approx(L_AREA, rel=1e-6)
    assert whole["centroid"][0] * M_LON == pytest.approx(L_CENTROID[0], abs=1e-3)
    assert whole["centroid"][1] * M_LAT == pytest.approx(L_CENTROID[1], abs=1e-3)
    assert whole["size_m"] == pytest.approx([30, 30], abs=1e-3)
    # The same building cut at x = 20 m, as two tiles would, wound the other way round.
    left = _ring((0, 0), (20, 0), (20, 10), (10, 10), (10, 30), (0, 30))[::-1]
    right = _ring((20, 0), (30, 0), (30, 10), (20, 10))
    cut = landmark.footprint([[left], [right]], 0.0, 0.0)
    assert cut["centroid"] == pytest.approx(whole["centroid"], abs=1e-9)
    assert cut["area_m2"] == pytest.approx(L_AREA, rel=1e-6)


def test_footprint_subtracts_holes():
    outer = _ring((0, 0), (40, 0), (40, 40), (0, 40))
    hole = _ring((0, 0), (20, 0), (20, 20), (0, 20))  # the south-west quarter is a courtyard
    fp = landmark.footprint([[outer, hole]], 0.0, 0.0)
    assert fp["area_m2"] == pytest.approx(1200, rel=1e-6)
    # Three quarters around (10,30), (30,30), (30,10): centroid (70/3, 70/3).
    assert fp["centroid"][0] * M_LON == pytest.approx(70 / 3, abs=1e-3)
    assert fp["centroid"][1] * M_LAT == pytest.approx(70 / 3, abs=1e-3)


def test_ids_compare_as_the_map_shows_them():
    assert landmark._key(12.0) == "12"
    assert landmark._key(12) == "12"
    assert landmark._key("01a1") == "01a1"
    assert landmark._key(None) is None


@pytest.fixture
def buildings(tmp_path):
    """A GeoJSON of buildings near (0, 0): the target cut into two pieces (as tiles would cut
    it), a tower made of parts, and a neighbour."""
    feats = [
        ({"bid": "L", "h": 336.0}, _ring((0, 0), (20, 0), (20, 10), (10, 10), (10, 30), (0, 30))),
        ({"bid": "L", "h": 336.0}, _ring((20, 0), (30, 0), (30, 10), (20, 10))),
        ({"bid": "T", "h": 4.0, "has_parts": True}, _ring((100, 0), (140, 0), (140, 40), (100, 40))),
        ({"bid": "T1", "parent": "T", "h": 90.0, "base": "0"}, _ring((100, 0), (120, 0), (120, 40), (100, 40))),
        ({"bid": "T2", "parent": "T", "h": 120.0, "base": "20"}, _ring((120, 0), (140, 0), (140, 40), (120, 40))),
        ({"bid": 77, "h": 12.0}, _ring((-60, 0), (-40, 0), (-40, 20), (-60, 20))),
    ]
    src = tmp_path / "b.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": props, "geometry": {"type": "Polygon", "coordinates": [ring]}}
        for props, ring in feats
    ]}))

    def _lookup(bid, **kw):
        req = landmark.Lookup(path=src, layer="b", zoom=None, id_field="bid", building_id=bid, lon=0.0, lat=0.0,
                              height="h", base="base", parent="parent", **kw)
        return landmark.lookup(req)

    return _lookup


@needs_gdal
def test_lookup_joins_the_pieces_of_a_building(buildings):
    r = buildings("L")
    assert (r["matched"], r["pieces"], r["height"], r["base"]) == ("id", 2, 336.0, 0.0)
    assert r["area_m2"] == pytest.approx(L_AREA, rel=1e-3)  # GeoJSON keeps 7 decimals
    assert r["centroid"][0] * M_LON == pytest.approx(L_CENTROID[0], abs=0.01)
    assert r["centroid"][1] * M_LAT == pytest.approx(L_CENTROID[1], abs=0.01)


@needs_gdal
def test_lookup_finds_numeric_ids_and_parts(buildings):
    assert buildings("77")["height"] == 12.0
    part = buildings("T2")
    assert (part["matched"], part["height"], part["base"]) == ("id", 120.0, 20.0)
    # The outline of a building with parts is found by its own id before its parts.
    tower = buildings("T")
    assert (tower["matched"], tower["pieces"]) == ("id", 1)
    assert tower["centroid"][0] * M_LON == pytest.approx(120, abs=0.01)


@needs_gdal
def test_lookup_falls_back_to_the_parts(tmp_path):
    src = tmp_path / "parts.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"bid": f"P{i}", "parent": "X", "h": h},
         "geometry": {"type": "Polygon", "coordinates": [_ring((x, 0), (x + 10, 0), (x + 10, 10), (x, 10))]}}
        for i, (x, h) in enumerate([(0, 30.0), (10, 60.0)])
    ]}))
    req = landmark.Lookup(path=src, layer="parts", zoom=None, id_field="bid", building_id="X", lon=0.0, lat=0.0,
                          height="h", parent="parent")
    r = landmark.lookup(req)
    assert (r["matched"], r["pieces"], r["height"]) == ("parent", 2, 60.0)
    assert r["centroid"][0] * M_LON == pytest.approx(10, abs=0.01)


@needs_gdal
def test_lookup_reports_a_missing_building(buildings):
    with pytest.raises(landmark.LandmarkError, match="Không thấy"):
        buildings("nope")


@pytest.mark.skipif(tools.which("tippecanoe") is None or tools.which("ogr2ogr") is None,
                    reason="needs tippecanoe and GDAL")
def test_lookup_in_pmtiles_across_a_tile_edge(tmp_path):
    """Read back from real tiles: the prime meridian is a tile edge at every zoom, so the
    building comes back in two clipped pieces whose centroid is the footprint's."""
    src = tmp_path / "b.geojson"
    ring = _ring(*L_SHAPE, x0=-15 / M_LON)  # straddles lon 0
    src.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"bid": "L", "h": 50}, "geometry": {"type": "Polygon", "coordinates": [ring]}}]}))
    out = tmp_path / "b.pmtiles"
    proc = tools.run_capture([tools.require("tippecanoe"), "-o", str(out), "-l", "b", "-Z", "17", "-z", "17",
                              "--no-feature-limit", "--no-tile-size-limit", "--force", "-q", str(src)])
    assert proc.returncode == 0, proc.stderr
    req = landmark.Lookup(path=out, layer="b", zoom=17, id_field="bid", building_id="L", lon=0.0, lat=0.0, height="h")
    r = landmark.lookup(req)
    assert r["pieces"] == 2
    assert r["area_m2"] == pytest.approx(L_AREA, rel=0.01)
    assert r["centroid"][0] * M_LON == pytest.approx(L_CENTROID[0] - 15, abs=0.2)
    assert r["centroid"][1] * M_LAT == pytest.approx(L_CENTROID[1], abs=0.2)
