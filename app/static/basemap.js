// Basemaps, the VMAP light presets and the map colours, shared by the 3D preview (preview.js) and
// the landmark library (landmarks.js). Both pages carry the same controls: #baseSel, #vmapControls,
// #presetSeg, #presetNote, #presetDownload and #baseNote.

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// Map colours are the Studio Indochine tokens in tokens.css: height-1…9 sit on these
// breaks (paper yellow → roof brown), cat-1…8 are the categorical slots (anything past
// 8 values folds into map-neutral "khác").
const HEIGHT_BREAKS = [0, 6, 12, 20, 35, 60, 100, 200, 400];
const CAT_COUNT = 8;
const SRC_TOKENS = { height: "cat-1", levels: "cat-3", clamped: "cat-2", default: "map-neutral" };
// OpenFreeMap: free vector basemaps, no API key. VMAP is the mobile app's own style, served
// through the local server (assets.vmaps.vn has no CORS): one published style per light preset.
// Every basemap's own building layers are removed so they never overlap the buildings being checked.
const BASEMAPS = {
  vmap: { style: "/api/vmap/style.json", vmap: true },
  positron: { style: "https://tiles.openfreemap.org/styles/positron" },
  dark: { style: "https://tiles.openfreemap.org/styles/dark" },
  liberty: { style: "https://tiles.openfreemap.org/styles/liberty" },
  osm: { raster: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], attribution: "© OpenStreetMap contributors" },
  none: {},
};
export const COMPASS = ["bắc", "đông bắc", "đông", "đông nam", "nam", "tây nam", "tây", "tây bắc"];

// VMAP light presets come from the server (app/vmap.py), the one place they are defined.
// `light` is the root light of the style last loaded for the current preset.
export const vmap = { presets: [], preset: "day", light: null };
let baseSeq = 0;

// The colour set follows the basemap, not the page: dark tiles get the dark set, whose
// height ramp is reversed so tall buildings stay the most salient.
export function mapTheme(key = $("#baseSel").value) {
  if (key === "vmap") return vmapPreset()?.dark ? "dark" : "light";
  return key === "dark" || (key === "none" && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
}

// The basemap and light preset are remembered per browser; the map works without storage.
export function remember(key, value) {
  try { localStorage.setItem(`bds.preview.${key}`, value); } catch { /* storage blocked */ }
}
export function recall(key) {
  try { return localStorage.getItem(`bds.preview.${key}`); } catch { return null; }
}

export async function loadVmapPresets() {
  try {
    const res = await fetch("/api/vmap/presets");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const info = await res.json();
    vmap.presets = info.presets;
    vmap.preset = info.presets.some((p) => p.id === recall("preset")) ? recall("preset") : info.default;
  } catch {
    $("#baseSel").querySelector('option[value="vmap"]').disabled = true;
  }
}

export function vmapPreset(id = vmap.preset) {
  return vmap.presets.find((p) => p.id === id);
}

// Building look of the mobile app: the active preset on the VMAP basemap, its day look elsewhere.
export function mobileBuilding() {
  return vmapPreset($("#baseSel").value === "vmap" ? vmap.preset : "day")?.building;
}

export function initBasemap(fallback) {
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const saved = recall("basemap");
  const usable = (k) => k && BASEMAPS[k] && !$("#baseSel").querySelector(`option[value="${k}"]`)?.disabled;
  $("#baseSel").value = usable(saved) ? saved : usable(fallback) ? fallback : dark ? "dark" : "positron";
  renderVmapControls();
}

function renderVmapControls() {
  const on = $("#baseSel").value === "vmap" && vmap.presets.length > 0;
  $("#vmapControls").hidden = !on;
  if (!on) return;
  $("#presetSeg").innerHTML = vmap.presets.map((p) => `<button type="button" data-preset="${esc(p.id)}" aria-pressed="${p.id === vmap.preset}"
    title="Style ${esc(p.style)} của VMAP">${esc(p.label)}</button>`).join("");
  const p = vmapPreset();
  $("#presetDownload").href = `/api/vmap/style.json?preset=${encodeURIComponent(p.id)}&download=1`;
  renderPresetNote();
}

// The style's own light, once it is loaded: it shades the buildings and casts the shadows.
function renderPresetNote() {
  const p = vmapPreset();
  const l = vmap.light;
  let light = "";
  if (l?.position) {
    const [, az, polar] = l.position;
    light = ` Light của style: ${l.color ?? "#ffffff"}, cường độ ${l.intensity ?? 0.5}, từ hướng ${COMPASS[Math.round(az / 45) % 8]}, cao ${90 - polar}°.`;
  }
  $("#presetNote").textContent = `${p.style}: ${p.note}${light}`;
}

// Switch basemap / preset (or building source). Only the latest request is applied, so fast
// clicks cannot leave the map on an older style than the one selected.
export async function reloadBasemap(map, bldgSource, onLoad) {
  remember("basemap", $("#baseSel").value);
  remember("preset", vmap.preset);
  vmap.light = null;
  renderVmapControls();
  const seq = ++baseSeq;
  const style = await basemapStyle($("#baseSel").value, bldgSource());
  if (seq !== baseSeq) return;
  map.once("style.load", onLoad);
  map.setStyle(style, { diff: false });
}

// bldgSource: () => the building source to keep across styles (or null).
export function bindBasemapControls(map, bldgSource, onLoad, onBaseChange = () => {}) {
  $("#baseSel").addEventListener("change", () => {
    onBaseChange($("#baseSel").value);
    reloadBasemap(map, bldgSource, onLoad);
  });
  $("#presetSeg").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-preset]");
    if (!btn || btn.dataset.preset === vmap.preset) return;
    vmap.preset = btn.dataset.preset;
    reloadBasemap(map, bldgSource, onLoad);
  });
}

const palettes = {};
export function palette(theme) {
  if (palettes[theme]) return palettes[theme];
  // Read the tokens off a probe element pinned to that theme.
  const probe = document.createElement("div");
  probe.dataset.theme = theme;
  probe.hidden = true;
  document.body.append(probe);
  const cs = getComputedStyle(probe);
  const v = (name) => cs.getPropertyValue(`--${name}`).trim();
  const pal = {
    height: HEIGHT_BREAKS.map((h, i) => [h, v(`height-${i + 1}`)]),
    cat: Array.from({ length: CAT_COUNT }, (_, i) => v(`cat-${i + 1}`)),
    src: Object.fromEntries(Object.entries(SRC_TOKENS).map(([k, t]) => [k, v(t)])),
    neutral: v("map-neutral"),
    outlier: v("map-outlier"),
    highlight: v("map-highlight"),
    plain: v("height-4"),
    ground: v("map-ground"),
    outline: v("ink"),
    ochre: v("ochre"),
    accent: v("accent"),
  };
  probe.remove();
  return (palettes[theme] = pal);
}

// The basemap style, plus the building source as "bldg" when there is one.
export async function basemapStyle(key, bldgSource) {
  const def = BASEMAPS[key] || BASEMAPS.none;
  const bg = { id: "bg", type: "background", paint: { "background-color": palette(mapTheme(key)).ground } };
  let style = { version: 8, sources: {}, layers: [bg] };
  $("#baseNote").textContent = "";
  try {
    if (def.style) {
      const preset = vmap.preset;
      const res = await fetch(def.vmap ? `${def.style}?preset=${encodeURIComponent(preset)}` : def.style);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      style = await res.json();
      if (def.vmap && preset === vmap.preset) {  // a slower answer for an earlier click is dropped anyway
        vmap.light = style.light || null;
        renderPresetNote();
        if (res.headers.get("X-VMAP-Source") === "repo") {
          $("#baseNote").textContent = `Không tải được style live từ assets.vmaps.vn — đang dùng bản lưu trong repo (map-styles/${vmapPreset().style}.style.json).`;
        }
      }
      style.layers = style.layers.filter((l) => l["source-layer"] !== "building");
    } else if (def.raster) {
      style.sources.base = { type: "raster", tiles: def.raster, tileSize: 256, attribution: def.attribution, maxzoom: 19 };
      style.layers.push({ id: "base", type: "raster", source: "base" });
    }
  } catch (e) {
    $("#baseNote").textContent = `Không tải được bản đồ nền (${e.message}) — đang dùng nền trống.`;
  }
  if (bldgSource) style.sources.bldg = bldgSource;
  return style;
}
