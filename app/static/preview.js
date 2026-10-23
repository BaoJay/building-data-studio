// PMTiles 3D preview — MapLibre fill-extrusion, same styling approach as the mobile app.
/* global maplibregl, pmtiles */
import { COMPASS, basemapStyle, bindBasemapControls, initBasemap, loadVmapPresets, mapTheme, mobileBuilding, palette, recall, remember, vmap, vmapPreset } from "/static/basemap.js";

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const HEIGHT_ATTRS = ["h_m", "height_m", "height", "render_height"];
const BASE_ATTRS = ["min_height", "render_min_height", "min_height_m"];
const params = new URLSearchParams(location.search);
const token = params.get("f");
const PARENT_ATTRS = ["parent_building_id", "parent_id"];
const state = { layer: null, fields: {}, stats: {}, heightAttr: null, baseAttr: null, parentAttr: null, idCol: params.get("idcol"), selected: params.get("id"), colorBy: "height", minzoom: 0, maxzoom: 22 };
// Landmark check (landmark.js, loaded on demand): the building whose extrusion a GLB replaces,
// and where the user last clicked (the server looks for the building around it).
const landmark = { module: null, loading: false, hidden: null, clicked: null };

// ------------------------------------------------------------------ shadows (prototype)
// Ground shadows precomputed on the server (app/shadows.py) for the area around the view,
// cast along the sun of the light preset (day sun on the other basemaps). Drawn as one
// dissolved fill under the labels and the buildings.
const SHADOW_MINZOOM = 15;
// A convert output with shadows carries them as this layer (one set per preset, `preset` field).
const SHADOW_LAYER = "shadows";
const SHADOW_MAX_SPAN = 0.045; // degrees; the server caps a request at 0.05
const shadow = { key: null, bbox: null, data: null, info: "", download: "", appliedKey: null, seq: 0, timer: null };

function shadowPresetId() {
  return $("#baseSel").value === "vmap" ? vmap.preset : "day";
}

// Area around the centre: pitched views reach the horizon, so the box is capped at
// `reach` × the request limit. Shadows are fetched for a wider box than the one that must be
// covered, so small pans stay inside it.
function viewBbox(map, pad, reach) {
  const b = map.getBounds();
  const c = map.getCenter();
  const half = (SHADOW_MAX_SPAN / 2) * reach;
  let [w, s, e, n] = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()];
  const dx = (e - w) * pad;
  const dy = (n - s) * pad;
  w = Math.max(w - dx, c.lng - half); e = Math.min(e + dx, c.lng + half);
  s = Math.max(s - dy, c.lat - half); n = Math.min(n + dy, c.lat + half);
  return [w, s, e, n];
}

function syncShadows(map) {
  clearTimeout(shadow.timer);
  shadow.timer = setTimeout(() => updateShadows(map), 250);
}

async function updateShadows(map) {
  const note = (text) => { $("#shadowNote").textContent = text; };
  const look = vmapPreset(shadowPresetId())?.shadow;
  $("#shadowDownloadRow").hidden = true;
  $("#shadows").disabled = !state.heightAttr;
  let why = "";
  if (!state.heightAttr) why = "Layer không có cột chiều cao nên không tính được bóng.";
  else if (!$("#shadows").checked) why = " ";
  else if (!$("#threeD").checked) why = "Bật “Dựng 3D” để xem bóng.";
  else if (!look) why = vmap.presets.length ? `${vmapPreset(shadowPresetId()).label} không đổ bóng (không có nắng ban ngày).` : "Không tải được danh sách chế độ ánh sáng.";
  else if (map.getZoom() < SHADOW_MINZOOM) why = `Phóng to tới zoom ${SHADOW_MINZOOM} để tính bóng.`;
  if (why) {
    shadow.seq++;
    note(why.trim());
    if (map.getLayer("bldg-shadow")) map.setLayoutProperty("bldg-shadow", "visibility", "none");
    return;
  }
  if (state.bakedShadows) {
    // Shadows shipped in the PMTiles: the same layer + filter the mobile app would use.
    if (!map.getLayer("bldg-shadow")) {
      const before = map.getStyle().layers.find((l) => l.type === "symbol" && l.layout?.["text-field"])?.id;
      map.addLayer({ id: "bldg-shadow", type: "fill", source: "bldg", "source-layer": SHADOW_LAYER, paint: { "fill-antialias": false } }, before);
    }
    map.setFilter("bldg-shadow", ["==", ["get", "preset"], shadowPresetId()]);
    map.setPaintProperty("bldg-shadow", "fill-color", look.color);
    map.setPaintProperty("bldg-shadow", "fill-opacity", look.opacity);
    map.setLayoutProperty("bldg-shadow", "visibility", "visible");
    note(`Bóng có sẵn trong file (layer ${SHADOW_LAYER}, z${state.bakedShadows.minzoom ?? "?"}–${state.bakedShadows.maxzoom ?? "?"}), lọc theo preset = "${shadowPresetId()}" như trên app.`);
    return;
  }

  const params = new URLSearchParams({ f: token, layer: state.layer, z: String(state.maxzoom), h: state.heightAttr, preset: shadowPresetId() });
  if (state.baseAttr) params.set("base", state.baseAttr);
  if (!$("#hideParentsWrap").hidden && $("#hideParents").checked) params.set("parts", "has_parts");
  const key = params.toString();
  const [w, s, e, n] = viewBbox(map, 0, 0.6);
  const have = shadow.key === key && shadow.bbox && shadow.bbox[0] <= w && shadow.bbox[1] <= s && shadow.bbox[2] >= e && shadow.bbox[3] >= n;
  if (!have) {
    const seq = ++shadow.seq;
    if (map.getLayer("bldg-shadow")) map.setLayoutProperty("bldg-shadow", "visibility", shadow.key === key ? "visible" : "none");
    note("Đang tính bóng cho vùng đang xem…");
    const bbox = viewBbox(map, 0.5, 1);
    params.set("bbox", bbox.map((v) => v.toFixed(6)).join(","));
    let data;
    let res;
    try {
      res = await fetch(`/api/shadows?${params}`);
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `HTTP ${res.status}`);
      data = await res.json();
    } catch (err) {
      if (seq === shadow.seq) note(`Không tính được bóng: ${err.message}`);
      return;
    }
    if (seq !== shadow.seq) return;
    const az = Number(res.headers.get("X-Sun-Azimuth"));
    const alt = Number(res.headers.get("X-Sun-Altitude"));
    Object.assign(shadow, {
      key, bbox, data,
      info: `Nắng từ hướng ${COMPASS[Math.round(az / 45) % 8]}, cao ${alt}° → bóng dài ${(1 / Math.tan((alt * Math.PI) / 180)).toFixed(2)}× chiều cao. `
        + `${Number(res.headers.get("X-Shadow-Buildings")).toLocaleString("vi-VN")} building, ${res.headers.get("X-Shadow-Ms")} ms. Kéo ra ngoài vùng này sẽ tự tính lại.`,
      download: `/api/shadows?${params}&download=1`,
    });
  }
  showShadow(map, look);
  note(shadow.info);
  $("#shadowDownload").href = shadow.download;
  $("#shadowDownloadRow").hidden = false;
}

function showShadow(map, look) {
  const src = map.getSource("shadow");
  if (!src) {
    map.addSource("shadow", { type: "geojson", data: shadow.data });
  } else if (shadow.appliedKey !== shadow.key + shadow.bbox) {
    src.setData(shadow.data);
  }
  shadow.appliedKey = shadow.key + shadow.bbox;
  if (!map.getLayer("bldg-shadow")) {
    // Over roads and bridges, under the first label layer (icon-only symbols such as one-way
    // arrows sit between the roads). The building layers come after the labels, so above.
    const before = map.getStyle().layers.find((l) => l.type === "symbol" && l.layout?.["text-field"])?.id;
    map.addLayer({ id: "bldg-shadow", type: "fill", source: "shadow", paint: {} }, before);
  }
  map.setPaintProperty("bldg-shadow", "fill-color", look.color);
  map.setPaintProperty("bldg-shadow", "fill-opacity", look.opacity);
  map.setLayoutProperty("bldg-shadow", "visibility", "visible");
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
  state.bakedShadows = (meta.vector_layers || []).find((l) => l.id === SHADOW_LAYER && "preset" in (l.fields || {})) || null;
  const layers = (meta.vector_layers || []).map((l) => l.id).filter((id) => !(state.bakedShadows && id === SHADOW_LAYER));
  const name = params.get("name") || meta.name || "PMTiles";
  document.title = `${name} — 3D`;
  $("#title").textContent = name;
  $("#meta").textContent = `z${header.minZoom}–${header.maxZoom} · ${layers.length} layer${state.bakedShadows ? " + layer bóng" : ""}${meta.generator ? ` · ${meta.generator}` : ""}`;
  if (layers.length > 1) {
    $("#layerField").hidden = false;
    $("#layerSel").innerHTML = layers.map((l) => `<option>${esc(l)}</option>`).join("");
  }

  await loadVmapPresets();
  initBasemap();
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
    syncLandmarkUI(map);
  });
  map.on("zoom", () => updateZoomUI(map));
  map.on("moveend", () => updateZoomUI(map));
  map.on("click", (e) => onClick(map, e));
  ["bldg-3d", "bldg-2d"].forEach((id) => {
    map.on("mouseenter", id, () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", id, () => { map.getCanvas().style.cursor = ""; });
  });

  // Picking the mobile basemap also switches the buildings to the mobile look. A switch made
  // before the first "load" leaves the building layers to selectLayer (no layer chosen yet).
  bindBasemapControls(map, () => bldgSource, () => { if (!state.layer) return; addBuildingLayers(map); applyStyle(map); landmark.module?.ensureLayers(); }, (key) => {
    if (key === "vmap" && $("#colorSel").querySelector('option[value="mobile"]')) {
      state.colorBy = "mobile";
      $("#colorSel").value = "mobile";
    }
  });
  $("#shadows").checked = recall("shadows") !== "0";
  $("#shadows").addEventListener("change", () => {
    remember("shadows", $("#shadows").checked ? "1" : "0");
    syncShadows(map);
  });
  map.on("moveend", () => syncShadows(map));
  $("#layerSel").addEventListener("change", (e) => selectLayer(map, meta, e.target.value));
  $("#colorSel").addEventListener("change", (e) => { state.colorBy = e.target.value; applyStyle(map); });
  $("#threeD").addEventListener("change", () => applyStyle(map));
  $("#hideParents").addEventListener("change", () => applyStyle(map));
  $("#collapseBtn").addEventListener("click", () => {
    const p = $("#panel");
    p.classList.toggle("collapsed");
    $("#collapseBtn").textContent = p.classList.contains("collapsed") ? "Mở" : "Thu gọn";
  });
  $("#lmkControls").addEventListener("toggle", () => { if ($("#lmkControls").open) openLandmark(map); });
}

// ------------------------------------------------------------------ landmark (GLB) check
// The section loads landmark.js (and three.js) the first time it is opened.
async function openLandmark(map) {
  if (landmark.module || landmark.loading) return;
  landmark.loading = true;
  $("#lmkStatus").textContent = "Đang tải three.js…";
  try {
    const mod = await import("/static/landmark.js");
    landmark.module = mod.init({
      map,
      token,
      state,
      ready: () => Boolean(state.layer && state.idCol),
      selectedId: () => state.selected,
      lookAt: (id) => (id === state.selected && landmark.clicked) || map.getCenter(),
      setHidden: (id) => { landmark.hidden = id; applyStyle(map); },
      buildingFilter: (id) => ["all", partsFilter() || true, matchBuilding(id)],
      referenceColor: () => (state.colorBy === "mobile" && mobileBuilding() ? mobileBuilding().color : palette(mapTheme()).plain),
      libraryUrl: (id) => {
        const q = new URLSearchParams({ f: token, name: params.get("name") || document.title.replace(/ — 3D$/, "") });
        if (state.idCol) q.set("idcol", state.idCol);
        if (id) {
          // Where to look for it: the library reads its footprint around this point.
          const c = (id === state.selected && landmark.clicked) || map.getCenter();
          q.set("id", id);
          q.set("lon", c.lng.toFixed(7));
          q.set("lat", c.lat.toFixed(7));
        }
        return `/landmarks?${q}`;
      },
    });
    $("#lmkStatus").textContent = "";
  } catch (err) {
    $("#lmkStatus").textContent = `Không tải được phần xem landmark: ${err.message}`;
  } finally {
    landmark.loading = false;
  }
}

function syncLandmarkUI(map) {
  $("#lmkControls").hidden = !state.idCol;
  $("#lmkIdLabel").textContent = state.idCol || "building_id";
  if (landmark.module) landmark.module.syncButtons();
  else if ($("#lmkControls").open) openLandmark(map);
}

// The building itself, or one of its parts.
function matchBuilding(id) {
  const match = [["==", ["to-string", ["get", state.idCol]], id]];
  if (state.parentAttr) match.push(["==", ["to-string", ["get", state.parentAttr]], id]);
  return ["any", ...match];
}

function partsFilter() {
  const hideParents = !$("#hideParentsWrap").hidden && $("#hideParents").checked;
  return hideParents ? ["!", ["any", ["==", ["get", "has_parts"], true], ["==", ["get", "has_parts"], 1]]] : null;
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
  if (vmap.presets.length) options.push(["mobile", "Như app Mobile (theo ánh sáng bản đồ)"]);
  options.push(["plain", "Một màu"]);
  $("#colorSel").innerHTML = options.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
  state.colorBy = $("#baseSel").value === "vmap" && vmap.presets.length ? "mobile" : options[0][0];
  $("#colorSel").value = state.colorBy;

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
  } else if (by === "mobile" && mobileBuilding()) {
    const b = mobileBuilding();
    expr = b.color;
    const origin = b.origin === "env"
      ? "đặt qua BDS_VMAP_BUILDING_COLOR / BDS_VMAP_BUILDING_OPACITY"
      : "ước lượng từ ảnh chụp Tasco Map admin, style VMAP không công bố";
    legend = item(b.color, "Building (như app Mobile)")
      + `<div class="hint">${esc(b.color)}, độ mờ ${b.opacity} — ${origin}. Cùng một màu ở mọi chế độ; light của style tô sáng / tối các mặt.</div>`;
  } else {
    expr = pal.plain;
    legend = "";
  }
  if (state.selected && state.idCol) {
    // Also light up the parts of a multi-part building (its outline may be hidden).
    expr = ["case", matchBuilding(state.selected), pal.highlight, expr];
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
  const filter = partsFilter();
  const height = state.heightAttr ? ["to-number", ["get", state.heightAttr], 0] : 4;
  const base = state.baseAttr ? ["to-number", ["coalesce", ["get", state.baseAttr], 0], 0] : 0;

  // The mobile look is translucent (as in the app); the QA colourings stay near-opaque.
  const mobile = state.colorBy === "mobile" ? mobileBuilding() : null;

  map.setPaintProperty("bldg-3d", "fill-extrusion-color", expr);
  map.setPaintProperty("bldg-3d", "fill-extrusion-height", height);
  map.setPaintProperty("bldg-3d", "fill-extrusion-base", base);
  map.setPaintProperty("bldg-3d", "fill-extrusion-opacity", mobile ? mobile.opacity : 0.92);
  map.setPaintProperty("bldg-2d", "fill-color", expr);
  map.setPaintProperty("bldg-2d", "fill-opacity", mobile ? mobile.opacity : 0.85);
  ["bldg-2d", "bldg-line"].forEach((id) => map.setFilter(id, filter));
  // A placed landmark replaces the extrusion of its building (parts included). Its ground
  // shadow (bldg-shadow) stays.
  const replaced = landmark.hidden && state.idCol ? ["!", matchBuilding(landmark.hidden)] : null;
  map.setFilter("bldg-3d", replaced && filter ? ["all", filter, replaced] : replaced || filter);
  landmark.module?.syncReference();
  map.setLayoutProperty("bldg-3d", "visibility", threeD ? "visible" : "none");
  map.setLayoutProperty("bldg-2d", "visibility", threeD ? "none" : "visible");
  map.setLayoutProperty("bldg-line", "visibility", threeD ? "none" : "visible");
  if (!threeD && map.getPitch() > 0) map.easeTo({ pitch: 0 });
  if (threeD && map.getPitch() < 20) map.easeTo({ pitch: 55 });
  syncShadows(map);
}

function onClick(map, e) {
  const feats = map.queryRenderedFeatures(e.point, { layers: ["bldg-3d", "bldg-2d"].filter((id) => map.getLayoutProperty(id, "visibility") !== "none") });
  if (!feats.length) return;
  const f = feats[0];
  landmark.clicked = e.lngLat;
  if (state.idCol && f.properties[state.idCol] != null) {
    state.selected = String(f.properties[state.idCol]);
    applyStyle(map);
    landmark.module?.syncButtons();
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
  $("#shadowControls").hidden = true;
  $("#lmkControls").hidden = true;
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

  await loadVmapPresets();
  initBasemap();
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
  bindBasemapControls(map, () => bldgSource, () => addDiffLayers(map));
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
  // Re-entrant: "load" can fire after a basemap switch already added them.
  ["grid-fill", "grid-line", "diff-fill", "diff-line", "diff-3d", "diff-sel"].forEach((id) => { if (map.getLayer(id)) map.removeLayer(id); });
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
