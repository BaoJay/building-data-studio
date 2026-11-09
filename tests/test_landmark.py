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
PRESET_IDS = ["dawn", "day", "dusk", "night"]
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


def test_every_vmap_style_names_its_light_preset():
    """Emissive gains and glass follow metadata "tasco:light-preset" of the style."""
    named = {p: json.loads(vmap.PRESETS[p].local_copy.read_text()).get("metadata", {}).get("tasco:light-preset") for p in PRESET_IDS}
    assert named == {p: p for p in PRESET_IDS}


# ------------------------------------------------------------------ emissive by group, glass (handoff 03/11/2026)

EMISSIVE_GAIN = {
    "led": {"dawn": 0.4, "day": 0, "dusk": 0.9, "night": 1.0},
    "led_wash": {"dawn": 0.3, "day": 0, "dusk": 0.8, "night": 1.0},
    "lobby": {"dawn": 0.3, "day": 0, "dusk": 0.8, "night": 1.0},
    "windows": {"dawn": 0.3, "day": 0, "dusk": 0.8, "night": 1.0},
    "other": {"dawn": 0.3, "day": 0, "dusk": 0.6, "night": 1.0},
}
GLASS = {
    "night": (0.9, "#0B1424", "#3A3A52", "#0A0C12"),
    "dusk": (0.7, "#2B3A66", "#C98E6A", "#1A1A22"),
    "dawn": (0.6, "#5A7AB0", "#F2C49A", "#2A2A30"),
    "day": (0.0, "#7FA8E0", "#DCE6F2", "#5A5E66"),
}
GROUPS = [None, "", "led_warm", "led_white", "led_line", "glow", "sign_tasco", "logo", "led_wash", "lobby", "windows_01", "facade_x"]


def linear_to_srgb(c):
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def gain_row(group):
    g = group or ""
    if g in ("led_wash", "lobby"):
        return g
    if g.startswith("windows"):
        return "windows"
    if g in ("led_warm", "led_white", "led_line", "glow") or g.startswith(("sign", "logo")):
        return "led"
    return "other"


def emissive_mode(group):
    return 0 if not group else 2 if group == "led_wash" else 1


def material_emissive(preset, extras, factor, khr=1.0):
    group = extras.get("emissive_group") or ""
    mode = emissive_mode(group) if max(factor) > 0 else 0
    own = extras.get("emissive_strength")
    strength = own if isinstance(own, (int, float)) and own >= 0 else khr
    gain = EMISSIVE_GAIN[gain_row(group)][preset] * strength if mode and preset in PRESET_IDS else 0
    return {"mode": mode, "gain": gain, "factor": [linear_to_srgb(min(max(f, 0), 1)) for f in factor]}


def apply_emissive(color, em, texel=None):
    e = [f * (texel[i] if texel else 1) for i, f in enumerate(em["factor"])]
    m = max(texel) if texel else 1
    if em["mode"] == 1:
        return [mix(c, e[i], clamp(em["gain"] * m, 0, 1)) for i, c in enumerate(color)]
    if em["mode"] == 2:
        return [clamp(c + e[i] * em["gain"], 0, 1) for i, c in enumerate(color)]
    return color


def smoothstep(e0, e1, x):
    t = clamp((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def glass_reflect(color, n, v, preset):
    reflect, zenith, horizon, ground = GLASS.get(preset, (0.0, *GLASS["day"][1:]))
    if preset not in GLASS:
        reflect = 0.0
    zenith, horizon, ground = rgb(zenith), rgb(horizon), rgb(ground)
    d = dot(n, v)
    r = [2 * d * n[i] - v[i] for i in range(3)]
    f = 0.04 + 0.96 * (1 - max(d, 0)) ** 5
    if r[1] >= 0:
        sky = [mix(horizon[i], zenith[i], smoothstep(0, 0.6, r[1])) for i in range(3)]
    else:
        sky = [mix(horizon[i], ground[i], smoothstep(0, 0.3, -r[1])) for i in range(3)]
    t = clamp(f * reflect, 0, 1)
    return [mix(c, sky[i], t) for i, c in enumerate(color)]


def test_the_led_colour_is_turned_into_srgb_before_mixing():
    """[1, 0.8, 0.5] linear is [1, 0.906, 0.735] in sRGB: mixed raw it would turn orange."""
    assert material_emissive("night", {"emissive_group": "led_warm"}, [1, 0.8, 0.5])["factor"] == pytest.approx([1, 0.906, 0.735], abs=1e-3)


def test_replace_makes_the_line_led_its_own_colour_at_night():
    """D01_Frost_White (255, 242, 206) under the night light, plus [1, 0.8, 0.5]: added it
    clamped to white; replaced it is the LED colour."""
    light = resolve(style_light("night"))
    lit = shade(rgb("#fff2ce"), [0, -1, 0], light, height_m=110.6, t=0.5)
    em = material_emissive("night", {"emissive_group": "led_warm"}, [1, 0.8, 0.5])
    assert apply_emissive(lit, em) == pytest.approx([1, 0.906, 0.735], abs=1e-3)
    added = [clamp(c + f, 0, 1) for c, f in zip(lit, em["factor"])]
    assert added[0] == 1 and added[1] > 0.95  # the old sum: near white


def test_led_wash_adds_the_baked_light_and_is_nothing_by_day():
    light = resolve(style_light("night"))
    wall = shade(rgb("#8e8e8e"), [0, -1, 0], light, height_m=110.6, t=0.5)
    em = material_emissive("night", {"emissive_group": "led_wash"}, [1, 1, 1])
    assert apply_emissive(wall, em, [0.6, 0.5, 0.3]) == pytest.approx([clamp(c + t, 0, 1) for c, t in zip(wall, [0.6, 0.5, 0.3])])
    day = material_emissive("day", {"emissive_group": "led_wash"}, [1, 1, 1])
    assert apply_emissive(wall, day, [0.6, 0.5, 0.3]) == wall  # *_BAKED looks like its plain twin by day


def test_no_group_or_no_factor_never_glows():
    assert material_emissive("night", {}, [1, 1, 1])["mode"] == 0
    assert material_emissive("night", {"emissive_group": "lobby"}, [0, 0, 0])["mode"] == 0


def test_every_group_is_dark_by_day():
    for g in GROUPS:
        assert material_emissive("day", {"emissive_group": g}, [1, 1, 1])["gain"] == 0



def test_emissive_mask_can_use_texcoord_1():
    src = LIGHT_JS.read_text()
    vert = src[src.index("export const VERTEX_SHADER"):src.index("export const FRAGMENT_SHADER")]
    assert "in vec2 lmk_uv1;" in vert
    assert "vec3(lmk_uv1, 1.0)" in vert
    model = (LIGHT_JS.parent / "landmark-model.js").read_text()
    assert 'setAttribute("lmk_uv1"' in model
    assert "emissiveMap.channel === 1" in model


def test_shader_blends_emissive_by_group_and_reflects_glass():
    src = LIGHT_JS.read_text()
    frag = src[src.index("export const FRAGMENT_SHADER"):]
    for needle in ("uniform int u_emissive_mode;", "if (u_emissive_mode == 1) color = mix(color, E, clamp(gain * m, 0.0, 1.0));",
                   "else if (u_emissive_mode == 2) color = clamp(color + E * gain, 0.0, 1.0);", "m = max(max(t.r, t.g), t.b);",
                   "#ifdef LMK_EMISSIVE_MAP", "#ifdef LMK_GLASS", "vec3 R = reflect(-V, n);",
                   "float F = 0.04 + 0.96 * pow(1.0 - max(dot(n, V), 0.0), 5.0);",
                   "mix(u_sky_horizon, u_sky_zenith, smoothstep(0.0, 0.6, R.y))",
                   "mix(u_sky_horizon, u_sky_ground, smoothstep(0.0, 0.3, -R.y))",
                   "clamp(F * u_glass_reflect * on, 0.0, 1.0)", "float on = 1.0 - u_override.a;"):
        assert needle in frag, needle
    assert frag.index("glassReflect(color, n, on)") < frag.index("u_emissive_mode == 1"), "emissive goes on after the glass"
    model = (LIGHT_JS.parent / "landmark-model.js").read_text()
    for needle in ("material.userData = extras", "defines.LMK_GLASS", "defines.LMK_EMISSIVE_MAP", "LL.cameraInModel(",
                   "LL.materialEmissive(this.preset, m.userData"):
        assert needle in model, needle


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
    presets: ["dawn", "day", "dusk", "night"].map((p) => LL.lightPreset({ "tasco:light-preset": p })).concat(LL.lightPreset({ "tasco:style-variant": "dark" }), LL.lightPreset(undefined)),
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
        assert r["presets"] == ["dawn", "day", "dusk", "night", None, None]


@needs_node
def test_js_emissive_and_glass_match_this_port(tmp_path):
    shutil.copy(LIGHT_JS, tmp_path / "landmark-light.mjs")
    presets = [*PRESET_IDS, None]
    em_cases = [{"preset": p, "extras": {"emissive_group": g} | ({"emissive_strength": 2} if g == "led_wash" else {}),
                 "factor": f, "texel": t}
                for p in presets for g in GROUPS for f in ([1, 0.8, 0.5], [0, 0, 0]) for t in (None, [0.6, 0.5, 0.2])]
    s = math.sqrt(0.5)
    glass_cases = [{"preset": p, "n": n, "v": v}
                   for p in presets for n in ([0, 0, 1], [1, 0, 0], [0, 1, 0], [s, 0, s])
                   for v in ([0, 0, 1], [0.6, 0.0, 0.8], [0, 0.6, 0.8], [0, -0.6, 0.8], [0.8, 0.6, 0])]
    runner = tmp_path / "run.mjs"
    runner.write_text("""
import * as LL from "./landmark-light.mjs";
import { readFileSync } from "node:fs";
const { em, glass, mvp } = JSON.parse(readFileSync(0, "utf8"));
const base = [0.3, 0.32, 0.4];
console.log(JSON.stringify({
  em: em.map((c) => {
    const e = LL.materialEmissive(c.preset, c.extras, c.factor);
    return { mode: e.mode, gain: e.gain, factor: e.u_emissive_factor, color: LL.applyEmissive(base, e, c.texel) };
  }),
  glass: glass.map((c) => LL.glassReflect(base, c.n, c.v, c.preset)),
  uniforms: LL.landmarkUniforms(LL.resolveLight({}), { preset: "night" }),
  eye: LL.cameraInModel(mvp),
  ortho: LL.cameraInModel([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]),
  contract: LL.GLB_CONTRACT.map((c) => c.key),
}));
""")
    eye = [-80.0, 104.5, 212.8]
    mvp = _perspective_look_at(eye, [-4, 45, 4])
    proc = subprocess.run(["node", str(runner)], input=json.dumps({"em": em_cases, "glass": glass_cases, "mvp": mvp}),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    r = json.loads(proc.stdout)
    base = [0.3, 0.32, 0.4]
    for c, got in zip(em_cases, r["em"]):
        want = material_emissive(c["preset"], c["extras"], c["factor"])
        assert (got["mode"], got["gain"]) == (want["mode"], pytest.approx(want["gain"])), c
        assert got["factor"] == pytest.approx(want["factor"], abs=1e-12)
        assert got["color"] == pytest.approx(apply_emissive(base, want, c["texel"]), abs=1e-12), c
    for c, got in zip(glass_cases, r["glass"]):
        assert got == pytest.approx(glass_reflect(base, c["n"], c["v"], c["preset"]), abs=1e-12), c
    u = r["uniforms"]
    assert u["u_glass_reflect"] == 0.9 and u["u_sky_horizon"] == pytest.approx(rgb("#3A3A52"))
    assert r["eye"] == pytest.approx(eye, abs=1e-6)
    assert r["ortho"] is None
    assert {"anchor", "zoom_range", "emissive_group", "emissive_strength", "emissiveTexture", "TEXCOORD_1", "surface_type"} <= set(r["contract"])


def _perspective_look_at(eye, target, fov=40.0, near=1.0, far=5000.0):
    """projection × view (column-major, 16 numbers), as a GL camera at `eye` looking at `target`."""
    f = [t - e for t, e in zip(target, eye)]
    fl = math.sqrt(dot(f, f))
    f = [x / fl for x in f]
    up = [0, 1, 0]
    sx = [f[1] * up[2] - f[2] * up[1], f[2] * up[0] - f[0] * up[2], f[0] * up[1] - f[1] * up[0]]
    sl = math.sqrt(dot(sx, sx))
    sx = [x / sl for x in sx]
    u = [sx[1] * f[2] - sx[2] * f[1], sx[2] * f[0] - sx[0] * f[2], sx[0] * f[1] - sx[1] * f[0]]
    view = [[sx[0], sx[1], sx[2], -dot(sx, eye)], [u[0], u[1], u[2], -dot(u, eye)],
            [-f[0], -f[1], -f[2], dot(f, eye)], [0, 0, 0, 1]]
    k = 1 / math.tan(math.radians(fov) / 2)
    proj = [[k, 0, 0, 0], [0, k, 0, 0], [0, 0, (far + near) / (near - far), 2 * far * near / (near - far)], [0, 0, -1, 0]]
    m = [[sum(proj[i][k2] * view[k2][j] for k2 in range(4)) for j in range(4)] for i in range(4)]
    return [m[r][c] for c in range(4) for r in range(4)]  # column-major


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
