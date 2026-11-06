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
// Emissive, by group (handoff 03/11/2026). The GLB says what glows and in which colour; the app
// says how bright, per preset. Each material reads material.extras.emissive_group (never its
// name): no group, or an emissiveFactor of 0, means no emissive at all. The group picks how the
// emissive colour E meets the lit colour (emissiveMode) and the gain per preset (emissiveGain,
// EMISSIVE_GAIN), multiplied by extras.emissive_strength when the modeller set one (else
// KHR_materials_emissive_strength, else 1):
//   E   = sRGB(emissiveFactor) × emissiveTexture (raw sRGB texel), or sRGB(emissiveFactor)
//   m   = max(texel.r, texel.g, texel.b) with a texture (mask coverage), else 1
//   REPLACE (1)  color = mix(lit, E, clamp(gain × m, 0, 1))  a surface that IS the light (line
//                LED, sign, logo, window mask): at night its colour is the light's, not added to
//                a near-white base colour, which clamped to white. A gain above 1 is clamped.
//   ADD     (2)  color = lit + E × gain                       led_wash: light baked by Cycles
//                onto the walls around the LED (lightmap JPEG on TEXCOORD_1), added on top.
// emissiveFactor is linear (glTF): it is turned into sRGB on the CPU (srgbEmissive), since the
// formula works on raw sRGB ([1, 0.8, 0.5] -> [1, 0.906, 0.735]; linear would turn orange).
// emissiveTexture may read TEXCOORD_1 (its texCoord: 1): the led_wash lightmap and window / LED
// masks do, while the base colour tiles on TEXCOORD_0. Sampling it with TEXCOORD_0 repeats the
// glow per tile: keep texCoord in a port. The preset comes from the style's metadata
// "tasco:light-preset"; a style without one has no emissive.
//
// Glass (material.extras.surface_type "glass"), a reflection cheap enough for mobile: no cubemap,
// no texture, no extra pass. R = reflect(-V, N), Schlick Fresnel with F0 = 0.04, a sky band
// (zenith / horizon / ground colours of the preset) looked up by R.y (+Y up), and
//   color = mix(lit, sky, clamp(F × reflect, 0, 1))          GLASS: night 0.9, dusk 0.7, dawn 0.6, day 0
// V needs the camera in the frame of v_pos and the normals (the glTF root frame here): the
// inverse of the model-view-projection matrix of the layer applied to (0, 0, 1, 0), divided by
// its w (cameraInModel). Emissive goes on after the glass.
//
// QA mode (one flat colour) turns emissive and glass off, so roofs and walls can be compared
// with the extrusion. Every value of the GLB this file reads is listed in GLB_CONTRACT (shown
// in the preview's drawer, with what the GLB on screen has).
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

// Everything the shader needs from the style and the camera, for one frame, shared by every
// material of a model.
//   light: resolveLight(map.getLight());  heading: the model's heading_deg;  bearing: map bearing
//   preset: lightPreset(style metadata) (glass sky colours and reflection)
export function landmarkUniforms(light, { heading = 0, bearing = 0, preset = null } = {}) {
  const enu = lightVectorENU(light, bearing);
  const glass = glassUniforms(preset);
  return {
    u_lightpos: enuToModel(enu, heading),
    u_lightcolor: light.color,
    u_lightintensity: light.intensity,
    u_north: enuToModel([0, 1, 0], heading),
    ...glass,
    lightposEnu: enu,
  };
}

export const PRESETS = ["dawn", "day", "dusk", "night"];

// The VMAP light preset of a style (metadata "tasco:light-preset"), or null.
export function lightPreset(metadata) {
  const p = (metadata || {})["tasco:light-preset"];
  return PRESETS.includes(p) ? p : null;
}

export function isNightStyle(metadata) {
  return lightPreset(metadata) === "night";
}

// ------------------------------------------------------------------ emissive by group

// Gain per row and preset (handoff 03/11/2026, the app has the final say). Day is 0 everywhere:
// by day the landmark looks like its SketchUp model.
export const EMISSIVE_GAIN = Object.freeze({
  led: Object.freeze({ dawn: 0.4, day: 0, dusk: 0.9, night: 1.0 }),       // led_warm, led_white, led_line, glow, sign*, logo*
  led_wash: Object.freeze({ dawn: 0.3, day: 0, dusk: 0.8, night: 1.0 }),  // baked LED light on the walls
  lobby: Object.freeze({ dawn: 0.3, day: 0, dusk: 0.6, night: 0.8 }),
  windows: Object.freeze({ dawn: 0, day: 0, dusk: 0, night: 0 }),         // windows*: off (Tasco has none)
  other: Object.freeze({ dawn: 0.3, day: 0, dusk: 0.6, night: 1.0 }),     // any other group
});

export const EMISSIVE_NONE = 0;
export const EMISSIVE_REPLACE = 1;
export const EMISSIVE_ADD = 2;
export const EMISSIVE_MODE_NAMES = ["none", "REPLACE", "ADD"];

// Which row of EMISSIVE_GAIN a group takes.
export function gainRow(group) {
  const g = String(group || "");
  if (g === "led_wash" || g === "lobby") return g;
  if (g.startsWith("windows")) return "windows";
  if (["led_warm", "led_white", "led_line", "glow"].includes(g) || g.startsWith("sign") || g.startsWith("logo")) return "led";
  return "other";
}

// 0 none (no group), 1 REPLACE, 2 ADD (led_wash). Unknown groups: REPLACE.
export function emissiveMode(group) {
  if (!group) return EMISSIVE_NONE;
  return group === "led_wash" ? EMISSIVE_ADD : EMISSIVE_REPLACE;
}

// Gain of a group under a preset (null: no preset, no emissive).
export function emissiveGain(preset, group) {
  if (!group || !PRESETS.includes(preset)) return 0;
  return EMISSIVE_GAIN[gainRow(group)][preset];
}

// emissiveFactor (linear, glTF) -> raw sRGB, the space E is mixed in.
export function srgbEmissive(factor) {
  return [0, 1, 2].map((i) => linearToSrgb(clamp01(Number(factor?.[i]) || 0)));
}

// How one material glows under a preset. extras: material.extras (emissive_group,
// emissive_strength); factor: emissiveFactor (linear); khrStrength: KHR_materials_emissive_strength
// (used when extras.emissive_strength is not set). Returns the per-material uniforms and the
// values behind them (for readouts).
export function materialEmissive(preset, extras, factor, khrStrength = 1) {
  const group = typeof extras?.emissive_group === "string" ? extras.emissive_group : "";
  const lit = Math.max(...[0, 1, 2].map((i) => Number(factor?.[i]) || 0)) > 0;
  const mode = lit ? emissiveMode(group) : EMISSIVE_NONE;
  const own = Number(extras?.emissive_strength);
  const strength = Number.isFinite(own) && own >= 0 ? own : Number.isFinite(khrStrength) ? khrStrength : 1;
  const gain = mode ? emissiveGain(preset, group) * strength : 0;
  return { group, mode, strength, gain, u_emissive_mode: mode, u_emissive_gain: gain, u_emissive_factor: srgbEmissive(factor) };
}

// ------------------------------------------------------------------ glass

// Reflection and sky band per preset (handoff 03/11/2026: suggested colours in the tone of each
// style's `light`, night #3f4455; the app has the final say). Day keeps its colours but
// reflects 0: by day the landmark looks like its SketchUp model (0.3 to try it).
export const GLASS = Object.freeze({
  night: Object.freeze({ reflect: 0.9, zenith: "#0B1424", horizon: "#3A3A52", ground: "#0A0C12" }),
  dusk: Object.freeze({ reflect: 0.7, zenith: "#2B3A66", horizon: "#C98E6A", ground: "#1A1A22" }),
  dawn: Object.freeze({ reflect: 0.6, zenith: "#5A7AB0", horizon: "#F2C49A", ground: "#2A2A30" }),
  day: Object.freeze({ reflect: 0, zenith: "#7FA8E0", horizon: "#DCE6F2", ground: "#5A5E66" }),
});
export const GLASS_F0 = 0.04;

export function isGlass(extras) {
  return extras?.surface_type === "glass";
}

// Shared glass uniforms of a preset (no preset: no reflection).
export function glassUniforms(preset) {
  const g = GLASS[preset] || GLASS.day;
  const rgb = (hex) => parseColor(hex).slice(0, 3);
  return {
    u_glass_reflect: GLASS[preset] ? g.reflect : 0,
    u_sky_zenith: rgb(g.zenith),
    u_sky_horizon: rgb(g.horizon),
    u_sky_ground: rgb(g.ground),
  };
}

// The camera in model coordinates, from the model-view-projection matrix (column-major, 16
// numbers): its inverse applied to (0, 0, 1, 0), divided by w. null when the projection has no
// eye point (orthographic) or the matrix cannot be inverted.
export function cameraInModel(mvp) {
  const inv = invert4(mvp);
  if (!inv) return null;
  const x = inv[8], y = inv[9], z = inv[10], w = inv[11];
  if (!(Math.abs(w) > 1e-300)) return null;
  const out = [x / w, y / w, z / w];
  return out.every(Number.isFinite) ? out : null;
}

// Glass reflection of one fragment (CPU mirror of the shader). color: lit colour (raw sRGB);
// n: unit normal and v: unit vector to the camera, both with +Y up; preset as glassUniforms.
export function glassReflect(color, n, v, preset) {
  const u = glassUniforms(preset);
  const d = dot(n, v);
  const r = [2 * d * n[0] - v[0], 2 * d * n[1] - v[1], 2 * d * n[2] - v[2]];  // reflect(-v, n)
  const f = GLASS_F0 + (1 - GLASS_F0) * (1 - Math.max(d, 0)) ** 5;
  const sky = r[1] >= 0
    ? u.u_sky_horizon.map((h, i) => mix(h, u.u_sky_zenith[i], smoothstep(0, 0.6, r[1])))
    : u.u_sky_horizon.map((h, i) => mix(h, u.u_sky_ground[i], smoothstep(0, 0.3, -r[1])));
  const t = clamp01(f * u.u_glass_reflect);
  return color.map((c, i) => mix(c, sky[i], t));
}

// Emissive of one fragment (CPU mirror of the shader). color: lit colour (raw sRGB); em:
// materialEmissive(); texel: emissiveTexture texel (raw sRGB) or null.
export function applyEmissive(color, em, texel = null) {
  const E = em.u_emissive_factor.map((f, i) => f * (texel ? texel[i] : 1));
  const m = texel ? Math.max(...texel) : 1;
  if (em.mode === EMISSIVE_REPLACE) return color.map((c, i) => mix(c, E[i], clamp01(em.gain * m)));
  if (em.mode === EMISSIVE_ADD) return color.map((c, i) => clamp01(c + E[i] * em.gain));
  return color;
}

// ------------------------------------------------------------------ the GLB contract

// Every value of a GLB the studio reads, how, and the rule a port follows. The preview's drawer
// lists it with what the GLB on screen holds; tests keep it in step with the shaders.
//   key: id;  where: place in the glTF JSON;  use: what it drives;  rule: what a port must do
export const GLB_CONTRACT = Object.freeze([
  { key: "anchor", where: "asset.extras.anchor {lat, lon}", use: "Đặt model", rule: "Góc xoay 0, gốc GLB đặt tại lat/lon (glTF +X Đông, −Z Bắc, +Y lên, mét)." },
  { key: "zoom_range", where: "asset.extras.lod_zoom, zoom_range [min, max|null]", use: "Đổi file theo zoom", rule: "Vẽ file có min ≤ zoom < max (max null: không giới hạn). Ngoài mọi khoảng: không vẽ, giữ extrusion LoD1." },
  { key: "emissive_group", where: "materials[].extras.emissive_group", use: "Chế độ trộn + gain", rule: "led_wash: ADD; nhóm khác: REPLACE; không có nhóm: không phát sáng. Không suy từ tên vật liệu." },
  { key: "emissive_strength", where: "materials[].extras.emissive_strength", use: "Nhân vào gain", rule: "Chỉ khi người dựng đặt > 100%. Có tác dụng với ADD; REPLACE kẹp gain × m ≤ 1. Thiếu: KHR_materials_emissive_strength, rồi 1." },
  { key: "emissiveFactor", where: "materials[].emissiveFactor", use: "Màu E", rule: "Linear: đổi sang sRGB trên CPU trước khi trộn. Bằng 0: không phát sáng." },
  { key: "emissiveTexture", where: "materials[].emissiveTexture {index, texCoord}", use: "Lightmap / mask", rule: "Texel sRGB thô nhân vào E; m = max(r, g, b). Giữ texCoord; JPEG, mipmap + lọc tuyến tính." },
  { key: "TEXCOORD_1", where: "meshes[].primitives[].attributes.TEXCOORD_1", use: "UV của lightmap", rule: "Đọc khi emissiveTexture.texCoord = 1 (chỉ primitive *_BAKED có). Lấy bằng TEXCOORD_0 thì vệt sáng lặp theo tile." },
  { key: "surface_type", where: "materials[].extras.surface_type", use: "Kính phản chiếu", rule: "\"glass\": Fresnel Schlick F0 0,04 + dải màu trời theo preset. Giá trị khác: không phản chiếu." },
  { key: "baseColor", where: "baseColorFactor × COLOR_0 × baseColorTexture (TEXCOORD_0)", use: "Màu nền", rule: "Đổi sang sRGB một lần rồi chiếu sáng bằng công thức fill-extrusion. Không sửa trong GLB để bù sáng." },
  { key: "NORMAL", where: "meshes[].primitives[].attributes.NORMAL", use: "Ánh sáng, Fresnel", rule: "Cùng hệ toạ độ với vị trí camera. Thiếu: tính phẳng theo mặt." },
  { key: "alpha", where: "alphaMode MASK / alphaCutoff, doubleSided", use: "Cắt alpha, hai mặt", rule: "MASK: bỏ pixel dưới alphaCutoff. doubleSided: lật pháp tuyến ở mặt sau." },
  { key: "texture_transform", where: "KHR_texture_transform (baseColor, emissive)", use: "UV", rule: "Áp vào UV trước khi lấy mẫu." },
]);

// What the studio does not read, on purpose (also listed in the drawer).
export const GLB_IGNORED = Object.freeze([
  "metallicFactor / roughnessFactor / metallicRoughnessTexture (extrusion không có)",
  "normalTexture, occlusionTexture",
  "extras.emissive_suffix, extras.lit (thông tin của pipeline)",
]);

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
function smoothstep(e0, e1, x) {
  const t = clamp01((x - e0) / (e1 - e0));
  return t * t * (3 - 2 * t);
}

// Inverse of a 4×4 matrix (column-major), or null when singular.
export function invert4(m) {
  const [a00, a01, a02, a03, a10, a11, a12, a13, a20, a21, a22, a23, a30, a31, a32, a33] = m;
  const b00 = a00 * a11 - a01 * a10, b01 = a00 * a12 - a02 * a10, b02 = a00 * a13 - a03 * a10;
  const b03 = a01 * a12 - a02 * a11, b04 = a01 * a13 - a03 * a11, b05 = a02 * a13 - a03 * a12;
  const b06 = a20 * a31 - a21 * a30, b07 = a20 * a32 - a22 * a30, b08 = a20 * a33 - a23 * a30;
  const b09 = a21 * a32 - a22 * a31, b10 = a21 * a33 - a23 * a31, b11 = a22 * a33 - a23 * a32;
  const det = b00 * b11 - b01 * b10 + b02 * b09 + b03 * b08 - b04 * b07 + b05 * b06;
  if (!det || !Number.isFinite(det)) return null;
  const k = 1 / det;
  return [
    (a11 * b11 - a12 * b10 + a13 * b09) * k, (a02 * b10 - a01 * b11 - a03 * b09) * k, (a31 * b05 - a32 * b04 + a33 * b03) * k, (a22 * b04 - a21 * b05 - a23 * b03) * k,
    (a12 * b08 - a10 * b11 - a13 * b07) * k, (a00 * b11 - a02 * b08 + a03 * b07) * k, (a32 * b02 - a30 * b05 - a33 * b01) * k, (a20 * b05 - a22 * b02 + a23 * b01) * k,
    (a10 * b10 - a11 * b08 + a13 * b06) * k, (a01 * b08 - a00 * b10 - a03 * b06) * k, (a30 * b04 - a31 * b02 + a33 * b00) * k, (a21 * b02 - a20 * b04 - a23 * b00) * k,
    (a11 * b07 - a10 * b09 - a12 * b06) * k, (a00 * b09 - a01 * b07 + a02 * b06) * k, (a31 * b01 - a30 * b03 - a32 * b00) * k, (a20 * b03 - a21 * b01 + a22 * b00) * k,
  ];
}

// ------------------------------------------------------------------ shaders (GLSL ES 3.00)
// Written for three.js ShaderMaterial (glslVersion GLSL3), which declares position / normal /
// uv, modelMatrix / modelViewMatrix / projectionMatrix / normalMatrix. The preview's camera
// has no view transform, so normalMatrix is the inverse transpose of modelMatrix and both map
// into the glTF root frame, the frame of v_pos and u_camera_pos too. Other renderers supply
// their own equivalents. extrusionLight(), wallGradient(), glassReflect() and the emissive block
// are the parts to copy verbatim.
//
// Per-material defines: LMK_VERTEX_COLOR (COLOR_0), LMK_EMISSIVE_MAP (an emissiveTexture on a
// material that glows), LMK_EMISSIVE_UV1 (that texture reads TEXCOORD_1), LMK_GLASS
// (surface_type "glass"). Per-material uniforms: u_emissive_mode, u_emissive_gain,
// u_emissive_factor (materialEmissive). The rest is shared by the model (landmarkUniforms).

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
out vec3 v_pos;                     // glTF root frame (glass: view vector)
out vec2 v_uv;
out vec2 v_uv_emissive;
out float v_t;                      // 0 at the ground, 1 at the top

void main() {
  vec4 p = modelMatrix * vec4(position, 1.0);
  v_pos = p.xyz;
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
uniform int u_emissive_mode;        // 0 none, 1 REPLACE, 2 ADD (emissiveMode of extras.emissive_group)
uniform float u_emissive_gain;      // emissiveGain(preset, group) x emissive strength
uniform vec3 u_emissive_factor;     // glTF emissiveFactor turned into sRGB on the CPU (srgbEmissive)
#ifdef LMK_EMISSIVE_MAP
uniform sampler2D u_emissive_map;   // glTF emissiveTexture, uploaded raw (sRGB-encoded bytes)
#endif
#ifdef LMK_GLASS
uniform float u_glass_reflect;      // GLASS[preset].reflect
uniform vec3 u_sky_zenith;          // raw sRGB
uniform vec3 u_sky_horizon;
uniform vec3 u_sky_ground;
uniform vec3 u_camera_pos;          // camera in the frame of v_pos (cameraInModel)
#endif
uniform float u_alpha_cutoff;       // alphaMode MASK, -1 otherwise
uniform vec4 u_override;            // QA: rgb = flat colour (raw sRGB) used instead of the model's, a = 1 on

in vec3 v_normal;
in vec3 v_pos;
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

#ifdef LMK_GLASS
// Glass: Schlick Fresnel (F0 0.04) towards a sky band looked up by the reflected ray (+Y up).
vec3 glassReflect(vec3 color, vec3 n, float on) {
  vec3 V = normalize(u_camera_pos - v_pos);
  vec3 R = reflect(-V, n);
  float F = 0.04 + 0.96 * pow(1.0 - max(dot(n, V), 0.0), 5.0);
  vec3 sky = R.y >= 0.0 ? mix(u_sky_horizon, u_sky_zenith, smoothstep(0.0, 0.6, R.y))
                        : mix(u_sky_horizon, u_sky_ground, smoothstep(0.0, 0.3, -R.y));
  return mix(color, sky, clamp(F * u_glass_reflect * on, 0.0, 1.0));
}
#endif

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
  vec3 color = extrusionLight(baseSrgb, n, wallGradient(n, v_t));
  float on = 1.0 - u_override.a;  // QA flat colour: no glass, no emissive

#ifdef LMK_GLASS
  color = glassReflect(color, n, on);
#endif

  // Emissive by group: REPLACE (the surface is the light) or ADD (baked light on the walls).
  vec3 E = u_emissive_factor;
  float m = 1.0;
#ifdef LMK_EMISSIVE_MAP
  vec3 t = texture(u_emissive_map, v_uv_emissive).rgb;
  E *= t;
  m = max(max(t.r, t.g), t.b);
#endif
  float gain = u_emissive_gain * on;
  if (u_emissive_mode == 1) color = mix(color, E, clamp(gain * m, 0.0, 1.0));
  else if (u_emissive_mode == 2) color = clamp(color + E * gain, 0.0, 1.0);
  fragColor = vec4(color, base.a);
}
`;
