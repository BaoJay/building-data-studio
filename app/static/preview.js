// PMTiles 3D preview — MapLibre fill-extrusion, same styling approach as the mobile app.
/* global maplibregl, pmtiles */

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const HEIGHT_ATTRS = ["h_m", "height_m", "height", "render_height"];
const BASE_ATTRS = ["min_height", "render_min_height", "min_height_m"];
// Map colours are the Studio Indochine tokens in tokens.css: height-1…9 sit on these
// breaks (paper yellow → roof brown), cat-1…8 are the categorical slots (anything past
// 8 values folds into map-neutral "khác").
const HEIGHT_BREAKS = [0, 6, 12, 20, 35, 60, 100, 200, 400];
const CAT_COUNT = 8;
const SRC_TOKENS = { height: "cat-1", levels: "cat-3", clamped: "cat-2", default: "map-neutral" };
// OpenFreeMap: free vector basemaps, no API key. Their own building layers are
// removed so they never overlap the buildings being checked.
const BASEMAPS = {
  positron: { style: "https://tiles.openfreemap.org/styles/positron" },
  dark: { style: "https://tiles.openfreemap.org/styles/dark" },
  liberty: { style: "https://tiles.openfreemap.org/styles/liberty" },
  osm: { raster: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], attribution: "© OpenStreetMap contributors" },
  none: {},
};

const params = new URLSearchParams(location.search);
const token = params.get("f");
const PARENT_ATTRS = ["parent_building_id", "parent_id"];
const state = { layer: null, fields: {}, stats: {}, heightAttr: null, baseAttr: null, parentAttr: null, idCol: params.get("idcol"), selected: params.get("id"), colorBy: "height", minzoom: 0, maxzoom: 22 };

// The colour set follows the basemap, not the page: dark tiles get the dark set, whose
// height ramp is reversed so tall buildings stay the most salient.
function mapTheme(key = $("#baseSel").value) {
  return key === "dark" || (key === "none" && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
}

const palettes = {};
function palette(theme) {
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
  };
  probe.remove();
  return (palettes[theme] = pal);
}

async function main() {
  if (!token) { $("#title").textContent = "Thiếu tham số f (token file PMTiles)."; return; }
  const url = new URL(`/api/files/${encodeURIComponent(token)}`, location.href).href;
  const protocol = new pmtiles.Protocol();
  maplibregl.addProtocol("pmtiles", protocol.tile);
  const archive = new pmtiles.PMTiles(url);
  protocol.add(archive);

  let header;
  let meta = {};
  try {
    header = await archive.getHeader();
    meta = (await archive.getMetadata()) || {};
  } catch (e) {
    $("#title").textContent = `Không đọc được PMTiles: ${e.message}`;
    return;
  }
  state.minzoom = header.minZoom;
  state.maxzoom = header.maxZoom;
  const layers = (meta.vector_layers || []).map((l) => l.id);
  const name = params.get("name") || meta.name || "PMTiles";
  document.title = `${name} — 3D`;
  $("#title").textContent = name;
  $("#meta").textContent = `z${header.minZoom}–${header.maxZoom} · ${layers.length} layer${meta.generator ? ` · ${meta.generator}` : ""}`;
  if (layers.length > 1) {
    $("#layerField").hidden = false;
    $("#layerSel").innerHTML = layers.map((l) => `<option>${esc(l)}</option>`).join("");
  }

  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  $("#baseSel").value = dark ? "dark" : "positron";
  const hasCenter = params.has("lon") && params.has("lat");
  const center = hasCenter ? [Number(params.get("lon")), Number(params.get("lat"))] : [header.centerLon, header.centerLat];
  const zoom = hasCenter ? Number(params.get("z") || 17) : Math.max(header.centerZoom, header.minZoom + 1);
  const bldgSource = { type: "vector", url: `pmtiles://${url}`, attribution: meta.attribution || "" };

  const map = new maplibregl.Map({
    container: "map",
    center,
    zoom,
    pitch: 55,
    bearing: -15,
    maxPitch: 85,
    attributionControl: false,
    // Keeps the frame readable for screenshots / "save image" in the browser.
    canvasContextAttributes: { antialias: true, preserveDrawingBuffer: true },
    style: await basemapStyle($("#baseSel").value, bldgSource),
  });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
  map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
  window.map = map;

  map.on("load", () => {
    selectLayer(map, meta, layers[0]);
    focusSelected(map, hasCenter ? center : null);
  });
  map.on("zoom", () => updateZoomUI(map));
  map.on("moveend", () => updateZoomUI(map));
  map.on("click", (e) => onClick(map, e));
  ["bldg-3d", "bldg-2d"].forEach((id) => {
    map.on("mouseenter", id, () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", id, () => { map.getCanvas().style.cursor = ""; });
  });

  $("#baseSel").addEventListener("change", async (e) => {
    const style = await basemapStyle(e.target.value, bldgSource);
    map.once("style.load", () => { addBuildingLayers(map); applyStyle(map); });
    map.setStyle(style, { diff: false });
  });
  $("#layerSel").addEventListener("change", (e) => selectLayer(map, meta, e.target.value));
  $("#colorSel").addEventListener("change", (e) => { state.colorBy = e.target.value; applyStyle(map); });
  $("#threeD").addEventListener("change", () => applyStyle(map));
  $("#hideParents").addEventListener("change", () => applyStyle(map));
  $("#collapseBtn").addEventListener("click", () => {
    const p = $("#panel");
    p.classList.toggle("collapsed");
    $("#collapseBtn").textContent = p.classList.contains("collapsed") ? "Mở" : "Thu gọn";
  });
}

async function basemapStyle(key, bldgSource) {
  const def = BASEMAPS[key] || BASEMAPS.none;
  const bg = { id: "bg", type: "background", paint: { "background-color": palette(mapTheme(key)).ground } };
  let style = { version: 8, sources: {}, layers: [bg] };
  $("#baseNote").textContent = "";
  try {
    if (def.style) {
      const res = await fetch(def.style);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      style = await res.json();
      style.layers = style.layers.filter((l) => l["source-layer"] !== "building");
    } else if (def.raster) {
      style.sources.base = { type: "raster", tiles: def.raster, tileSize: 256, attribution: def.attribution, maxzoom: 19 };
      style.layers.push({ id: "base", type: "raster", source: "base" });
    }
  } catch (e) {
    $("#baseNote").textContent = `Không tải được bản đồ nền (${e.message}) — đang dùng nền trống.`;
  }
  style.sources.bldg = bldgSource;
  return style;
}

function selectLayer(map, meta, layerId) {
  state.layer = layerId;
  const vl = (meta.vector_layers || []).find((l) => l.id === layerId) || { fields: {} };
  state.fields = vl.fields || {};
  const ts = (meta.tilestats?.layers || []).find((l) => l.layer === layerId);
  state.stats = Object.fromEntries((ts?.attributes || []).map((a) => [a.attribute, a]));
  state.heightAttr = HEIGHT_ATTRS.find((a) => a in state.fields) || null;
  state.baseAttr = BASE_ATTRS.find((a) => a in state.fields) || null;
  state.parentAttr = PARENT_ATTRS.find((a) => a in state.fields) || null;
  if (!state.idCol || !(state.idCol in state.fields)) {
    state.idCol = ["building_id", "id", "osm_id"].find((a) => a in state.fields) || null;
  }
  $("#hideParentsWrap").hidden = !("has_parts" in state.fields);

  const options = [];
  if (state.heightAttr) options.push([`height`, `Chiều cao (${state.heightAttr})`]);
  if ("h_src" in state.fields) options.push(["h_src", "Nguồn chiều cao (h_src)"]);
  if ("h_outlier" in state.fields) options.push(["outlier", "Outlier (h_outlier)"]);
  for (const [attr, s] of Object.entries(state.stats)) {
    if (s.type === "string" && !["h_src", state.idCol, "parent_building_id", "parent_id"].includes(attr) && (s.values || []).length <= 50) {
      options.push([`cat:${attr}`, `Theo ${attr}`]);
    }
  }
  options.push(["plain", "Một màu"]);
  $("#colorSel").innerHTML = options.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
  state.colorBy = options[0][0];

  addBuildingLayers(map);
  applyStyle(map);
}

function addBuildingLayers(map) {
  const layerId = state.layer;
  ["bldg-3d", "bldg-2d", "bldg-line"].forEach((id) => { if (map.getLayer(id)) map.removeLayer(id); });
  map.addLayer({ id: "bldg-2d", type: "fill", source: "bldg", "source-layer": layerId, paint: { "fill-opacity": 0.85 } });
  map.addLayer({ id: "bldg-line", type: "line", source: "bldg", "source-layer": layerId, paint: { "line-color": palette(mapTheme()).outline, "line-opacity": 0.3, "line-width": 0.5 } });
  map.addLayer({ id: "bldg-3d", type: "fill-extrusion", source: "bldg", "source-layer": layerId, paint: { "fill-extrusion-opacity": 0.92, "fill-extrusion-vertical-gradient": true } });
}

function colorExpression() {
  const by = state.colorBy;
  const pal = palette(mapTheme());
  let expr;
  let legend;
  if (by === "height" && state.heightAttr) {
    const stops = pal.height;
    expr = ["interpolate", ["linear"], ["to-number", ["get", state.heightAttr], 0], ...stops.flat()];
    const grad = stops.map(([, c], i) => `${c} ${(100 * i) / (stops.length - 1)}%`).join(", ");
    legend = `<div class="ramp" style="background:linear-gradient(90deg, ${grad})"></div>
      <div class="ramp-labels">${stops.filter((_, i) => i % 2 === 0).map(([v]) => `<span>${v}</span>`).join("")}</div>
      <div class="hint">mét (${esc(state.heightAttr)}), thang không tuyến tính</div>`;
  } else if (by === "h_src") {
    expr = ["match", ["get", "h_src"], ...Object.entries(pal.src).flat(), pal.neutral];
    legend = Object.entries(pal.src).map(([k, c]) => item(c, k)).join("");
  } else if (by === "outlier") {
    expr = ["case", ["any", ["==", ["get", "h_outlier"], 1], ["==", ["get", "h_outlier"], true]], pal.outlier, pal.neutral];
    legend = item(pal.outlier, "Outlier (giá trị gốc ngoài khoảng hợp lệ)") + item(pal.neutral, "Bình thường");
  } else if (by.startsWith("cat:")) {
    const attr = by.slice(4);
    const values = (state.stats[attr]?.values || []).slice(0, pal.cat.length);
    expr = values.length ? ["match", ["to-string", ["get", attr]], ...values.flatMap((v, i) => [String(v), pal.cat[i]]), pal.neutral] : pal.neutral;
    legend = values.map((v, i) => item(pal.cat[i], v)).join("") + ((state.stats[attr]?.values || []).length > values.length ? item(pal.neutral, "khác") : "");
  } else {
    expr = pal.plain;
    legend = "";
  }
  if (state.selected && state.idCol) {
    // Also light up the parts of a multi-part building (its outline may be hidden).
    const match = [["==", ["to-string", ["get", state.idCol]], state.selected]];
    if (state.parentAttr) match.push(["==", ["to-string", ["get", state.parentAttr]], state.selected]);
    expr = ["case", ["any", ...match], pal.highlight, expr];
  }
  return { expr, legend };
}

function item(color, label) {
  return `<div class="item"><span class="sw" style="background:${color}"></span>${esc(label)}</div>`;
}

function applyStyle(map) {
  const { expr, legend } = colorExpression();
  $("#legend").innerHTML = legend;
  const threeD = $("#threeD").checked;
  const hideParents = !$("#hideParentsWrap").hidden && $("#hideParents").checked;
  const filter = hideParents ? ["!", ["any", ["==", ["get", "has_parts"], true], ["==", ["get", "has_parts"], 1]]] : null;
  const height = state.heightAttr ? ["to-number", ["get", state.heightAttr], 0] : 4;
  const base = state.baseAttr ? ["to-number", ["coalesce", ["get", state.baseAttr], 0], 0] : 0;

  map.setPaintProperty("bldg-3d", "fill-extrusion-color", expr);
  map.setPaintProperty("bldg-3d", "fill-extrusion-height", height);
  map.setPaintProperty("bldg-3d", "fill-extrusion-base", base);
  map.setPaintProperty("bldg-2d", "fill-color", expr);
  ["bldg-3d", "bldg-2d", "bldg-line"].forEach((id) => map.setFilter(id, filter));
  map.setLayoutProperty("bldg-3d", "visibility", threeD ? "visible" : "none");
  map.setLayoutProperty("bldg-2d", "visibility", threeD ? "none" : "visible");
  map.setLayoutProperty("bldg-line", "visibility", threeD ? "none" : "visible");
  if (!threeD && map.getPitch() > 0) map.easeTo({ pitch: 0 });
  if (threeD && map.getPitch() < 20) map.easeTo({ pitch: 55 });
}

function onClick(map, e) {
  const feats = map.queryRenderedFeatures(e.point, { layers: ["bldg-3d", "bldg-2d"].filter((id) => map.getLayoutProperty(id, "visibility") !== "none") });
  if (!feats.length) return;
  const f = feats[0];
  if (state.idCol && f.properties[state.idCol] != null) {
    state.selected = String(f.properties[state.idCol]);
    applyStyle(map);
  }
  showPopup(map, e.lngLat, f.properties);
}

function showPopup(map, lngLat, props) {
  const rows = Object.entries(props).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("");
  new maplibregl.Popup({ maxWidth: "380px" }).setLngLat(lngLat).setHTML(`<table class="props">${rows}</table>`).addTo(map);
}

function focusSelected(map, center) {
  updateZoomUI(map);
  if (!center || !state.selected || !state.idCol) return;
  // Once tiles around the target are loaded, open the popup of the requested building.
  map.once("idle", () => {
    const feats = map.querySourceFeatures("bldg", {
      sourceLayer: state.layer,
      filter: ["==", ["to-string", ["get", state.idCol]], state.selected],
    });
    if (feats.length) showPopup(map, center, feats[0].properties);
  });
}

function updateZoomUI(map) {
  const z = map.getZoom();
  $("#zBadge").textContent = `z ${z.toFixed(1)}${z > state.maxzoom ? " (overzoom)" : ""}`;
  const note = $("#zoomNote");
  if (z < state.minzoom) {
    note.hidden = false;
    note.textContent = `Dữ liệu chỉ có từ zoom ${state.minzoom} — hãy phóng to để thấy building.`;
  } else {
    note.hidden = true;
  }
}

main();
