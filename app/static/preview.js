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
  if (params.get("mode") === "diff") {
    await mainDiff(url, header, meta);
    return;
  }
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
    note.textContent = state.diff
      ? `Đang xem lưới thống kê — phóng to tới zoom ${state.minzoom} để thấy từng building.`
      : `Dữ liệu chỉ có từ zoom ${state.minzoom} — hãy phóng to để thấy building.`;
  } else {
    note.hidden = true;
  }
}

// ------------------------------------------------------------------ diff mode
// Building colours come from the tokens, like the swatches of the report: added = cat-1
// (street-sign blue), removed = map-outlier, changed = cat-2 (ochre). Blue / red / ochre
// stay apart for red-green colour blindness too.
const DIFF_KINDS = [
  ["added", (pal) => pal.cat[0], "Thêm mới (chỉ có ở B)"],
  ["removed", (pal) => pal.outlier, "Bị xoá (chỉ có ở A)"],
  ["changed", (pal) => pal.cat[1], "Có thay đổi"],
];
// The statistics grid fades out as the buildings come in.
const GRID_FADE = [10, 0.62, 12.5, 0.08];
const GRID_LINE_FADE = [10, 0.3, 12.5, 0.04];
const DIFF_FIELDS = ["diff_change", "diff_key", "diff_h", "diff_dh", "diff_geom", "diff_cols"];

async function mainDiff(url, header, meta) {
  state.diff = { show: new Set(DIFF_KINDS.map(([k]) => k)), grid: null, gridBy: "total" };
  const name = params.get("name") || meta.name || "Diff";
  document.title = `${name} — bản đồ diff`;
  $("#title").textContent = name;
  $("#meta").textContent = `Bản đồ diff A → B · building từ zoom ${header.minZoom}`;
  $("#colorField").hidden = true;
  $("#diffControls").hidden = false;
  $("#threeD").checked = false;
  $("#panelHint").textContent = "Click building hoặc ô lưới để xem chi tiết. Lưới mờ dần khi phóng to. Bản đồ nền cần internet.";
  state.idCol = "diff_key";

  const gridToken = params.get("grid");
  if (gridToken) {
    try {
      const res = await fetch(`/api/files/${encodeURIComponent(gridToken)}`);
      if (res.ok) state.diff.grid = await res.json();
    } catch { /* the map still works without the grid */ }
  }
  state.diff.totals = { added: 0, removed: 0, changed: 0 };
  for (const f of state.diff.grid?.features || []) {
    for (const k of Object.keys(state.diff.totals)) state.diff.totals[k] += f.properties[k] || 0;
  }
  if (!state.diff.grid) $("#gridSel").closest("label").hidden = true;

  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  $("#baseSel").value = dark ? "dark" : "positron";
  const hasCenter = params.has("lon") && params.has("lat");
  const bldgSource = { type: "vector", url: `pmtiles://${url}`, attribution: meta.attribution || "" };
  const map = new maplibregl.Map({
    container: "map",
    center: hasCenter ? [Number(params.get("lon")), Number(params.get("lat"))] : [header.centerLon, header.centerLat],
    zoom: hasCenter ? Number(params.get("z") || 16) : 5,
    pitch: 0,
    maxPitch: 85,
    attributionControl: false,
    canvasContextAttributes: { antialias: true, preserveDrawingBuffer: true },
    style: await basemapStyle($("#baseSel").value, bldgSource),
  });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
  map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
  window.map = map;

  map.on("load", () => {
    addDiffLayers(map);
    if (!hasCenter) fitGrid(map);
    updateZoomUI(map);
    if (hasCenter && state.selected) {
      map.once("idle", () => {
        const feats = map.querySourceFeatures("bldg", { sourceLayer: "diff", filter: ["==", ["to-string", ["get", "diff_key"]], state.selected] });
        if (feats.length) showPopup(map, map.getCenter(), pick(feats[0].properties));
      });
    }
  });
  map.on("zoom", () => updateZoomUI(map));
  map.on("click", (e) => onDiffClick(map, e));
  ["diff-fill", "diff-3d", "grid-fill"].forEach((id) => {
    map.on("mouseenter", id, () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", id, () => { map.getCanvas().style.cursor = ""; });
  });
  $("#baseSel").addEventListener("change", async (e) => {
    const style = await basemapStyle(e.target.value, bldgSource);
    map.once("style.load", () => addDiffLayers(map));
    map.setStyle(style, { diff: false });
  });
  $("#diffLegend").addEventListener("change", (e) => {
    const kind = e.target.dataset.kind;
    if (!kind) return;
    if (e.target.checked) state.diff.show.add(kind); else state.diff.show.delete(kind);
    applyDiffStyle(map);
  });
  $("#gridSel").addEventListener("change", (e) => { state.diff.gridBy = e.target.value; applyDiffStyle(map); });
  $("#showGrid").addEventListener("change", () => applyDiffStyle(map));
  $("#threeD").addEventListener("change", () => applyDiffStyle(map));
  $("#collapseBtn").addEventListener("click", () => {
    const p = $("#panel");
    p.classList.toggle("collapsed");
    $("#collapseBtn").textContent = p.classList.contains("collapsed") ? "Mở" : "Thu gọn";
  });
}

function addDiffLayers(map) {
  // Colours follow the basemap (light / dark set), so they are re-read after every style switch.
  const pal = palette(mapTheme());
  if (state.diff.grid && !map.getSource("grid")) map.addSource("grid", { type: "geojson", data: state.diff.grid });
  if (state.diff.grid) {
    map.addLayer({ id: "grid-fill", type: "fill", source: "grid", paint: { "fill-opacity": ["interpolate", ["linear"], ["zoom"], ...GRID_FADE] } });
    map.addLayer({ id: "grid-line", type: "line", source: "grid", paint: { "line-color": pal.outline, "line-width": 0.5, "line-opacity": ["interpolate", ["linear"], ["zoom"], ...GRID_LINE_FADE] } });
  }
  const color = ["match", ["get", "diff_change"], ...DIFF_KINDS.flatMap(([k, c]) => [k, c(pal)]), pal.neutral];
  map.addLayer({ id: "diff-fill", type: "fill", source: "bldg", "source-layer": "diff", paint: { "fill-color": color, "fill-opacity": 0.8 } });
  map.addLayer({ id: "diff-line", type: "line", source: "bldg", "source-layer": "diff", paint: { "line-color": color, "line-width": ["interpolate", ["linear"], ["zoom"], 12, 0.4, 17, 1.4] } });
  map.addLayer({ id: "diff-3d", type: "fill-extrusion", source: "bldg", "source-layer": "diff", paint: { "fill-extrusion-color": color, "fill-extrusion-opacity": 0.9, "fill-extrusion-height": ["to-number", ["coalesce", ["get", "diff_h"], 4], 4] } });
  map.addLayer({ id: "diff-sel", type: "line", source: "bldg", "source-layer": "diff", paint: { "line-color": pal.highlight, "line-width": 3 } });
  $("#diffLegend").innerHTML = DIFF_KINDS.map(([k, c, label]) => `
    <label class="item"><input type="checkbox" data-kind="${k}" ${state.diff.show.has(k) ? "checked" : ""}><span class="sw" style="background:${c(pal)}"></span>${esc(label)}
      <span class="n">${state.diff.grid ? state.diff.totals[k].toLocaleString("vi-VN") : ""}</span></label>`).join("");
  applyDiffStyle(map);
}

function applyDiffStyle(map) {
  const kinds = ["in", ["get", "diff_change"], ["literal", [...state.diff.show]]];
  ["diff-fill", "diff-line", "diff-3d"].forEach((id) => map.setFilter(id, kinds));
  map.setFilter("diff-sel", ["==", ["to-string", ["get", "diff_key"]], state.selected || "\u0000"]);
  const threeD = $("#threeD").checked;
  map.setLayoutProperty("diff-3d", "visibility", threeD ? "visible" : "none");
  map.setLayoutProperty("diff-fill", "visibility", threeD ? "none" : "visible");
  if (threeD && map.getPitch() < 20) map.easeTo({ pitch: 55 });
  if (!threeD && map.getPitch() > 0) map.easeTo({ pitch: 0 });
  if (!map.getLayer("grid-fill")) return;
  const showGrid = $("#showGrid").checked;
  ["grid-fill", "grid-line"].forEach((id) => map.setLayoutProperty(id, "visibility", showGrid ? "visible" : "none"));
  const { expr, legend } = gridColor(state.diff.gridBy);
  map.setPaintProperty("grid-fill", "fill-color", expr);
  $("#gridLegend").innerHTML = showGrid ? legend : "";
}

function gridColor(by) {
  const pal = palette(mapTheme());
  const values = (state.diff.grid?.features || []).map((f) => f.properties[by] || 0);
  if (by === "delta") {
    // Diverging: fewer buildings in B = the "removed" colour, more = the "added" colour.
    const diverging = [pal.outlier, pal.neutral, pal.cat[0]];
    const sorted = values.map(Math.abs).filter((v) => v > 0).sort((a, b) => a - b);
    const m = Math.max(1, sorted[Math.floor(sorted.length * 0.95)] || 1);
    return {
      expr: ["interpolate", ["linear"], ["get", "delta"], -m, diverging[0], 0, diverging[1], m, diverging[2]],
      legend: rampLegend(diverging, [`−${fmtCount(m)}`, "0", `+${fmtCount(m)}`], "B ít hơn A ← → B nhiều hơn A (số building / ô)"),
    };
  }
  const nonzero = values.filter((v) => v > 0).sort((a, b) => a - b);
  const q = (p) => nonzero[Math.min(nonzero.length - 1, Math.floor(p * nonzero.length))] || 1;
  // Quantile stops (strictly increasing) keep a few huge cells from washing out the rest.
  const raw = [0, q(0.25), q(0.6), q(0.9), Math.max(q(1), 1)];
  const stops = raw.map((v, i) => (i === 0 ? 0 : Math.max(v, i)));
  for (let i = 1; i < stops.length; i++) if (stops[i] <= stops[i - 1]) stops[i] = stops[i - 1] + 1;
  // Counts use the height ramp (paper yellow → roof brown): darker = more buildings in the cell.
  const ramp = [0, 2, 4, 6, 8].map((i) => pal.height[i][1]);
  return {
    expr: ["case", ["==", ["get", by], 0], "rgba(0,0,0,0)", ["interpolate", ["linear"], ["get", by], ...stops.flatMap((v, i) => [v, ramp[i]])]],
    legend: rampLegend(ramp, ["1", fmtCount(stops[2]), fmtCount(stops[4])], "số building trong ô (ô trống thay đổi = trong suốt)"),
  };
}

function fmtCount(v) {
  return Math.round(v).toLocaleString("vi-VN");
}

function rampLegend(colors, labels, note) {
  const grad = colors.map((c, i) => `${c} ${(100 * i) / (colors.length - 1)}%`).join(", ");
  return `<div class="ramp" style="background:linear-gradient(90deg, ${grad})"></div>
    <div class="ramp-labels">${labels.map((l) => `<span>${esc(l)}</span>`).join("")}</div><div class="hint">${esc(note)}</div>`;
}

function fitGrid(map) {
  const feats = state.diff.grid?.features || [];
  if (!feats.length) return;
  let [x0, y0, x1, y1] = [180, 90, -180, -90];
  for (const f of feats) {
    for (const [x, y] of f.geometry.coordinates[0]) {
      x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y);
    }
  }
  map.fitBounds([[x0, y0], [x1, y1]], { padding: 40, animate: false });
}

function pick(props) {
  const out = {};
  for (const k of DIFF_FIELDS) if (props[k] != null && props[k] !== "") out[k] = props[k];
  return out;
}

function onDiffClick(map, e) {
  const layers = ["diff-3d", "diff-fill"].filter((id) => map.getLayoutProperty(id, "visibility") !== "none");
  const feats = map.queryRenderedFeatures(e.point, { layers });
  if (feats.length) {
    const p = feats[0].properties;
    state.selected = p.diff_key != null ? String(p.diff_key) : null;
    applyDiffStyle(map);
    showPopup(map, e.lngLat, pick(p));
    return;
  }
  if (!map.getLayer("grid-fill") || map.getLayoutProperty("grid-fill", "visibility") === "none") return;
  const cells = map.queryRenderedFeatures(e.point, { layers: ["grid-fill"] });
  if (!cells.length) return;
  const c = cells[0].properties;
  const ring = cells[0].geometry.coordinates[0];
  const rows = [["Building ở A", c.n_a], ["Building ở B", c.n_b], ["Thêm mới", c.added], ["Bị xoá", c.removed], ["Có thay đổi", c.changed]]
    .map(([k, v]) => `<tr><td>${esc(k)}</td><td>${Number(v).toLocaleString("vi-VN")}</td></tr>`).join("");
  const popup = new maplibregl.Popup({ maxWidth: "320px" }).setLngLat(e.lngLat)
    .setHTML(`<table class="props">${rows}</table><button class="btn small" type="button" id="zoomCell" style="margin-top:8px">Phóng to ô này</button>`).addTo(map);
  popup.getElement().querySelector("#zoomCell").addEventListener("click", () => {
    popup.remove();
    map.fitBounds([ring[0], ring[2]], { padding: 20 });
  });
}

main();
