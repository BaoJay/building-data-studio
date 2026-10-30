// Landmark lighting: MapLibre's fill-extrusion light applied to a 3D landmark model.
//
// One formula for every renderer that draws a landmark (GLB in this preview and in MapOps, LMK3
// in the mobile app) among MapLibre fill-extrusions: MapLibre's own, fed by the style's root
// `light`. A landmark is then lit like the buildings around it in every VMAP preset (dawn, day,
// dusk, night). The module has no dependencies: the preview imports it, the tests run it
// under Node, other teams port it line by line.
//
// Source: MapLibre GL JS 5.13 (app/static/vendor/maplibre-gl.js): the fill-extrusion vertex
// shader, fillExtrusionUniformValues, Light `position` (sphericalToCartesian) and the
// fill-extrusion bucket (how wall normals are built). Every value is raw sRGB 0..1, the space
// style colours are written in. Nothing is linearised and nothing is tone mapped. Per channel:
//
//   lum = 0.2126 r + 0.7152 g + 0.0722 b         of the base colour, before the ambient term
//   c   = base + 0.03                            ambient
//   d   = clamp(dot(N, L), 0, 1)                 N: unit outward normal. L: light vector, length r (1.15), not normalised
//   d   = mix(1 - I, max(1 - lum + I, 1), d)     I: light.intensity
//   d  *= wall gradient (below)                  fill-extrusion-vertical-gradient, true by default
//   out = clamp(c * d * lightColor, 0.3 * (1 - lightColor), 1)
//
// Light vector. MapLibre computes sphericalToCartesian([r, a, p]): a += 90°, x = r cos a sin p,
// y = r sin a sin p, z = r cos p, in tile coordinates (x east, y SOUTH, z up), and rotates it by
// the map bearing when light.anchor is "viewport" (the spec default; VMAP styles use "map").
// The bucket builds wall normals as perp(p[i] - p[i-1]) on rings that are clockwise in tile
// coordinates (MVT exterior rings), so they point INTO the building. The two flips cancel. With
// outward normals in east-north-up (ENU) metres, the same dot product is:
//
//   L_enu = r * (sin α sin p, cos α sin p, cos p),   α = azimuth (+ bearing if anchor "viewport")
//
// so the wall facing azimuth α (where the light sits, clockwise from north) is the brightest.
// Roofs: dot = r cos p, often above 1 before the clamp.
//
// Model frame. glTF is Y-up: model (x, y, z) -> ENU (x, -z, y), so +X is east, +Y up and the
// glTF front (+Z) faces south. heading_deg then turns the model clockwise seen from above (90:
// +X faces south). The shader works in the model's frame, so the light is brought into it by
// undoing the heading and the Y-up -> Z-up turn (enuToModel). A uniform positive scale does not
// change directions.
//
// Wall gradient (fill-extrusion-vertical-gradient). MapLibre darkens walls towards the ground:
// per vertex, t = 0 at the base and 1 at the top, building height H (m), base height 0:
//   lo = mix(0.7, 0.98, 1 - I);  g = mix(lo, clamp(sqrt(H / 150), lo, 1), t);  d *= g
// It applies where the normal has a north component (normal.y != 0 in tile coordinates): roofs
// never, and (a MapLibre quirk) walls facing exactly east or west never either. MapLibre only
// has flat roofs and vertical walls. For sloped faces of a model we blend by 1 - |N.up| (our
// choice). MapLibre interpolates the clamped vertex colours. We clamp per fragment, which
// gives the same result on flat faces.
//
// Base colour. The glTF base colour is baseColorFactor (linear) × COLOR_0 (linear) ×
// baseColorTexture (sRGB-encoded). It is turned into sRGB once (linearToSrgb) and fed to the
// formula like a style colour. With a factor of 1 and no vertex colours that is just the texel.
// Upload textures as plain RGBA8 (not SRGB8_ALPHA8 / .rgba8Unorm_srgb, no sRGB framebuffer).
// THREE.Color and three's sRGB textures linearise by default: the preview turns that off.
// Normal maps, metallic / roughness and occlusion are ignored on purpose: extrusions have none.
//
// Emissive (optional). emissiveFactor × emissiveTexture, in sRGB, × the material's strength,
// × the gain of its group in the preset, added after the light and clamped. emissiveTexture may
// read TEXCOORD_1 (its texCoord: 1), e.g. a window mask spanning 4×4 facade modules while the
// base colour tiles one module on TEXCOORD_0: keep texCoord in a port.
//   strength  KHR_materials_emissive_strength × material.extras.emissive_strength (both 1 when
//             absent). The pipeline (glb_lit.py) writes emissive_strength only above 100%
//             (e.g. suffix _LED_WARM_150); levels below 100% are already in emissiveFactor.
//   gain      EMISSIVE_GAIN[group][preset] (emissiveGain): preset from the style's metadata
//             "tasco:light-preset", group from material.extras.emissive_group (led_warm,
//             windows_tower, ...; three's GLTFLoader copies extras to material.userData). Never
//             infer the group from the material name. Day is 0 for every group: by day the model
//             looks as in SketchUp. A style without a preset stays at 0.
// The output is clamped at 1, so gain × strength above 1 only washes the colour towards white
// (warm [1, 0.8, 0.5] × 1.5 -> [1, 1, 0.75]); it takes bloom to look brighter.
//
// Reading the style's `light` (refresh on every style change, and also every frame when the
// anchor is "viewport", since the bearing then moves the light):
//   GL JS     map.getLight() on "styledata". Missing keys take the spec defaults (LIGHT_DEFAULTS).
//   Android   org.maplibre.android.style.light.Position has no getters, so parse the `light`
//             object of Style.getJson() instead (same keys as the style JSON).
//   iOS       MLNStyle.light (MLNLight): position is an MLNSphericalPosition {radial, azimuthal,
//             polar}, color a UIColor (read its sRGB components, multiply by alpha), intensity,
//             anchor (MLNLightAnchorMap / MLNLightAnchorViewport).
// Light colours are premultiplied by their alpha, as MapLibre does (rgba(255,255,255,0.5) is
// [0.5, 0.5, 0.5]).
//
// Check a port with the preview's QA mode (one flat colour on the landmark, the building's
// own extrusion next to it): roofs and walls facing the same way must match.

export const LIGHT_DEFAULTS = Object.freeze({ anchor: "viewport", color: "#ffffff", intensity: 0.5, position: [1.15, 210, 30] });
export const LUMA = [0.2126, 0.7152, 0.0722];
export const AMBIENT = 0.03;
const DEG = Math.PI / 180;

// ------------------------------------------------------------------ light

// #rgb / #rgba / #rrggbb / #rrggbbaa / rgb[a]() / hsl[a]() -> [r, g, b, a], sRGB 0..1, or null.
// Same rules as parse_color in app/vmap.py.
export function parseColor(value) {
  const s = String(value ?? "").trim();
  let m = /^#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.exec(s);
  if (m) {
    let h = m[1];
    if (h.length <= 4) h = [...h].map((ch) => ch + ch).join("");
    const c = h.match(/../g).map((x) => parseInt(x, 16) / 255);
    return [c[0], c[1], c[2], c.length === 4 ? c[3] : 1];
  }
  m = /^(rgba?|hsla?)\(([^)]*)\)$/.exec(s.toLowerCase());
  if (!m) return null;
  const parts = m[2].trim().split(/[\s,/]+/).filter(Boolean);
  if (parts.length !== 3 && parts.length !== 4) return null;
  const ch = (t, scale) => (t.endsWith("%") ? parseFloat(t) / 100 : parseFloat(t) / scale);
  const alpha = parts.length === 4 ? ch(parts[3], 1) : 1;
  let rgb;
  if (m[1].startsWith("rgb")) {
    rgb = parts.slice(0, 3).map((p) => ch(p, 255));
  } else {
    const hue = ((parseFloat(parts[0]) % 360) + 360) % 360;
    rgb = hslToRgb(hue, ch(parts[1], 100), ch(parts[2], 100));
  }
  const out = [...rgb, alpha];
  return out.some((v) => !Number.isFinite(v)) ? null : out.map(clamp01);
}

function hslToRgb(h, s, l) {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = l - c / 2;
  const [r, g, b] = [[c, x, 0], [x, c, 0], [0, c, x], [0, x, c], [x, 0, c], [c, 0, x]][Math.floor(h / 60) % 6];
  return [r + m, g + m, b + m];
}

// The style's `light` (as written, possibly partial) with the spec defaults filled in. The
// colour comes out raw sRGB, premultiplied by alpha. `problems` lists values that were not
// literals MapLibre could use as is (zoom expressions are not supported here).
export function resolveLight(light) {
  const src = light || {};
  const problems = [];
  const pick = (key, ok) => {
    if (src[key] === undefined) return LIGHT_DEFAULTS[key];
    if (ok(src[key])) return src[key];
    problems.push(key);
    return LIGHT_DEFAULTS[key];
  };
  const anchor = pick("anchor", (v) => v === "map" || v === "viewport");
  const intensity = Number(pick("intensity", (v) => Number.isFinite(Number(v)) && typeof v !== "object"));
  const position = pick("position", (v) => Array.isArray(v) && v.length === 3 && v.every((x) => Number.isFinite(Number(x)))).map(Number);
  let rgba = typeof src.color === "string" ? parseColor(src.color) : null;
  if (src.color !== undefined && !rgba) problems.push("color");
  rgba = rgba || parseColor(LIGHT_DEFAULTS.color);
  return { anchor, color: [rgba[0] * rgba[3], rgba[1] * rgba[3], rgba[2] * rgba[3]], intensity, position, problems };
}

// MapLibre's own conversion (tile frame: x east, y south, z up). Kept for the tests and as the
// reference for ports.
export function sphericalToCartesian([r, azimuthal, polar]) {
  const a = (azimuthal + 90) * DEG;
  const p = polar * DEG;
  return [r * Math.cos(a) * Math.sin(p), r * Math.sin(a) * Math.sin(p), r * Math.cos(p)];
}

// Light vector in east-north-up, for outward normals. `light` is resolveLight()'s result.
// bearingDeg: map bearing, used only with anchor "viewport".
export function lightVectorENU(light, bearingDeg = 0) {
  const [r, azimuthal, polar] = light.position;
  const a = (azimuthal + (light.anchor === "viewport" ? bearingDeg : 0)) * DEG;
  const p = polar * DEG;
  return [r * Math.sin(a) * Math.sin(p), r * Math.cos(a) * Math.sin(p), r * Math.cos(p)];
}

// ENU vector -> glTF model frame: undo heading_deg (clockwise from north), then Z-up -> Y-up.
export function enuToModel([e, n, u], headingDeg = 0) {
  const h = headingDeg * DEG;
  const e1 = e * Math.cos(h) - n * Math.sin(h);
  const n1 = e * Math.sin(h) + n * Math.cos(h);
  return [e1, u, -n1];
}

// glTF model frame vector -> ENU (the inverse of enuToModel).
export function modelToEnu([x, y, z], headingDeg = 0) {
  const h = headingDeg * DEG;
  const e1 = x;
  const n1 = -z;
  return [e1 * Math.cos(h) + n1 * Math.sin(h), -e1 * Math.sin(h) + n1 * Math.cos(h), y];
}

// Everything the shader needs from the style and the camera, for one frame (the per-material
// u_emissive_gain comes from emissiveGain).
//   light: resolveLight(map.getLight());  heading: the model's heading_deg;  bearing: map bearing
export function landmarkUniforms(light, { heading = 0, bearing = 0 } = {}) {
  const enu = lightVectorENU(light, bearing);
  return {
    u_lightpos: enuToModel(enu, heading),
    u_lightcolor: light.color,
    u_lightintensity: light.intensity,
    u_north: enuToModel([0, 1, 0], heading),
    lightposEnu: enu,
  };
}

export function isNightStyle(metadata) {
  return (metadata || {})["tasco:light-preset"] === "night";
}

// The VMAP light preset of a style (dawn / day / dusk / night), or null.
export function lightPreset(metadata) {
  return (metadata || {})["tasco:light-preset"] ?? null;
}

// Emissive gain per group (material.extras.emissive_group) and light preset. A key ending in
// "*" matches by prefix (windows_tower -> "windows*"), so a building's config can name groups
// in detail; "*" is every other group and a material without one. LED strips are the landmark's
// signature and light up from dusk; windows stay well below them (photos of Tasco by night).
export const EMISSIVE_GAIN = Object.freeze({
  "led_warm":  Object.freeze({ dawn: 0.4,  day: 0, dusk: 0.9,  night: 1.0 }),
  "led_line":  Object.freeze({ dawn: 0.4,  day: 0, dusk: 0.9,  night: 1.0 }),
  "led_white": Object.freeze({ dawn: 0.4,  day: 0, dusk: 0.9,  night: 1.0 }),
  "glow":      Object.freeze({ dawn: 0.4,  day: 0, dusk: 0.9,  night: 1.0 }),
  "sign*":     Object.freeze({ dawn: 0.4,  day: 0, dusk: 0.9,  night: 1.0 }),
  "logo*":     Object.freeze({ dawn: 0.4,  day: 0, dusk: 0.9,  night: 1.0 }),
  "lobby":     Object.freeze({ dawn: 0.3,  day: 0, dusk: 0.6,  night: 0.8 }),
  "windows*":  Object.freeze({ dawn: 0.15, day: 0, dusk: 0.35, night: 0.5 }),
  "*":         Object.freeze({ dawn: 0.3,  day: 0, dusk: 0.6,  night: 1.0 }),
});

// The EMISSIVE_GAIN row a group falls into: its own name, else the longest matching prefix key.
export function emissiveGroupKey(group) {
  const g = typeof group === "string" ? group : "";
  if (g && g !== "*" && Object.hasOwn(EMISSIVE_GAIN, g)) return g;
  let best = "*";
  for (const k of Object.keys(EMISSIVE_GAIN)) {
    if (k.length > 1 && k.endsWith("*") && g.startsWith(k.slice(0, -1)) && k.length > best.length) best = k;
  }
  return best;
}

// Gain of a group in a preset (preset: lightPreset(style metadata)). An unknown preset or none: 0.
export function emissiveGain(preset, group) {
  const row = EMISSIVE_GAIN[emissiveGroupKey(group)];
  return Object.hasOwn(row, preset ?? "") ? row[preset] : 0;
}

// material.extras.emissive_strength as a multiplier: a positive number, else 1.
export function emissiveStrength(extras) {
  const v = Number((extras || {}).emissive_strength);
  return Number.isFinite(v) && v > 0 ? v : 1;
}

// ------------------------------------------------------------------ colour (CPU mirror of the shader)

export function srgbToLinear(c) {
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

export function linearToSrgb(c) {
  return c <= 0.0031308 ? c * 12.92 : 1.055 * c ** (1 / 2.4) - 0.055;
}

// fill-extrusion-vertical-gradient factor (1 where it does not apply). nEnu: unit outward
// normal, t: 0 at the ground .. 1 at the top, heightM: the model's height in metres.
export function wallGradient(nEnu, t, heightM, intensity, verticalGradient = true) {
  if (!verticalGradient) return 1;
  const lo = mix(0.7, 0.98, 1 - intensity);
  const g = mix(lo, clamp(Math.sqrt(Math.max(heightM, 0) / 150), lo, 1), clamp01(t));
  const wall = Math.abs(nEnu[1]) > 1e-5 ? 1 - Math.abs(nEnu[2]) : 0;
  return mix(1, g, wall);
}

// MapLibre's extrusion light for one colour. base: raw sRGB [r, g, b]; nEnu: unit outward
// normal in ENU; light: resolveLight(); options as in landmarkUniforms plus the wall gradient.
export function shade(base, nEnu, light, { bearing = 0, verticalGradient = true, heightM = 0, t = 1 } = {}) {
  const L = lightVectorENU(light, bearing);
  const lum = dot(base, LUMA);
  const I = light.intensity;
  let d = clamp01(dot(nEnu, L));
  d = mix(1 - I, Math.max(1 - lum + I, 1), d);
  d *= wallGradient(nEnu, t, heightM, I, verticalGradient);
  return base.map((b, i) => clamp((b + AMBIENT) * d * light.color[i], 0.3 * (1 - light.color[i]), 1));
}

function dot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
function mix(a, b, t) { return a + (b - a) * t; }
function clamp(v, lo, hi) { return Math.min(Math.max(v, lo), hi); }
function clamp01(v) { return clamp(v, 0, 1); }

// ------------------------------------------------------------------ shaders (GLSL ES 3.00)
// Written for three.js ShaderMaterial (glslVersion GLSL3), which declares position / normal /
// uv, modelMatrix / modelViewMatrix / projectionMatrix / normalMatrix. The preview's camera
// has no view transform, so normalMatrix is the inverse transpose of modelMatrix and both map
// into the glTF root frame. Other renderers supply their own equivalents. extrusionLight()
// and wallGradient() are the parts to copy verbatim.

export const VERTEX_SHADER = /* glsl */ `
uniform mat3 u_map_transform;       // KHR_texture_transform of the base colour texture
uniform mat3 u_emissive_transform;
uniform float u_ground;             // model-frame y of the ground (the anchor)
uniform float u_top;                // model-frame y of the top of the model
#ifdef LMK_VERTEX_COLOR
in vec4 color;                      // glTF COLOR_0 (linear)
out vec4 v_color;
#endif
#ifdef LMK_EMISSIVE_UV1
in vec2 lmk_uv1;                    // glTF TEXCOORD_1, for an emissiveTexture with texCoord 1 (own name: three declares uv1 itself)
#endif
out vec3 v_normal;                  // glTF root frame
out vec2 v_uv;
out vec2 v_uv_emissive;
out float v_t;                      // 0 at the ground, 1 at the top

void main() {
  vec4 p = modelMatrix * vec4(position, 1.0);
  v_normal = normalMatrix * normal;
  v_uv = (u_map_transform * vec3(uv, 1.0)).xy;
#ifdef LMK_EMISSIVE_UV1
  v_uv_emissive = (u_emissive_transform * vec3(lmk_uv1, 1.0)).xy;
#else
  v_uv_emissive = (u_emissive_transform * vec3(uv, 1.0)).xy;
#endif
  v_t = clamp((p.y - u_ground) / max(u_top - u_ground, 1e-6), 0.0, 1.0);
#ifdef LMK_VERTEX_COLOR
  v_color = color;
#endif
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`;

export const FRAGMENT_SHADER = /* glsl */ `
precision highp float;

uniform vec3 u_lightpos;            // light vector in the model frame (landmarkUniforms), length = light radial
uniform vec3 u_lightcolor;          // raw sRGB, premultiplied by alpha
uniform float u_lightintensity;
uniform float u_vertical_gradient;  // 1: fill-extrusion-vertical-gradient true (MapLibre default)
uniform float u_height_m;           // model height in metres
uniform vec3 u_north;               // map north in the model frame
uniform vec4 u_base_factor;         // glTF baseColorFactor (linear)
uniform sampler2D u_base_map;       // glTF baseColorTexture, uploaded raw (sRGB-encoded bytes)
uniform float u_has_base_map;
uniform vec3 u_emissive;            // glTF emissiveFactor (linear)
uniform sampler2D u_emissive_map;
uniform float u_has_emissive_map;
uniform float u_emissive_intensity; // KHR_materials_emissive_strength × extras.emissive_strength (emissiveStrength)
uniform float u_emissive_gain;      // gain of the material's emissive_group in the preset (emissiveGain)
uniform float u_alpha_cutoff;       // alphaMode MASK, -1 otherwise
uniform vec4 u_override;            // QA: rgb = flat colour (raw sRGB) used instead of the model's, a = 1 on

in vec3 v_normal;
in vec2 v_uv;
in vec2 v_uv_emissive;
in float v_t;
#ifdef LMK_VERTEX_COLOR
in vec4 v_color;
#endif
out vec4 fragColor;

vec3 srgbToLinear(vec3 c) {
  return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(vec3(0.04045), c));
}

vec3 linearToSrgb(vec3 c) {
  return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(vec3(0.0031308), c));
}

// fill-extrusion-vertical-gradient. n: unit outward normal, model frame (y up).
float wallGradient(vec3 n, float t) {
  float lo = mix(0.7, 0.98, 1.0 - u_lightintensity);
  float g = mix(lo, clamp(sqrt(max(u_height_m, 0.0) / 150.0), lo, 1.0), t);
  float wall = abs(dot(n, u_north)) > 1e-5 ? 1.0 - abs(n.y) : 0.0;
  return mix(1.0, mix(1.0, g, u_vertical_gradient), wall);
}

// MapLibre fill-extrusion light. base: raw sRGB; n: unit outward normal, same frame as u_lightpos.
vec3 extrusionLight(vec3 base, vec3 n, float gradient) {
  float colorvalue = dot(base, vec3(0.2126, 0.7152, 0.0722));
  vec3 color = base + 0.03;
  float directional = clamp(dot(n, u_lightpos), 0.0, 1.0);
  directional = mix(1.0 - u_lightintensity, max(1.0 - colorvalue + u_lightintensity, 1.0), directional);
  directional *= gradient;
  return clamp(color * directional * u_lightcolor, 0.3 * (1.0 - u_lightcolor), vec3(1.0));
}

void main() {
  vec4 base = u_base_factor;
#ifdef LMK_VERTEX_COLOR
  base *= v_color;
#endif
  if (u_has_base_map > 0.5) {
    vec4 texel = texture(u_base_map, v_uv);
    base *= vec4(srgbToLinear(texel.rgb), texel.a);
  }
  if (base.a < u_alpha_cutoff) discard;
  vec3 baseSrgb = mix(linearToSrgb(clamp(base.rgb, 0.0, 1.0)), u_override.rgb, u_override.a);

  vec3 n = normalize(v_normal);
  if (!gl_FrontFacing) n = -n;  // double-sided materials
  vec3 lit = extrusionLight(baseSrgb, n, wallGradient(n, v_t));

  vec3 emissive = u_emissive;
  if (u_has_emissive_map > 0.5) emissive *= srgbToLinear(texture(u_emissive_map, v_uv_emissive).rgb);
  lit = clamp(lit + linearToSrgb(clamp(emissive, 0.0, 1.0)) * u_emissive_intensity * u_emissive_gain * (1.0 - u_override.a), 0.0, 1.0);
  fragColor = vec4(lit, base.a);
}
`;
