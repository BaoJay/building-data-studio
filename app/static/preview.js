// PMTiles 3D preview — MapLibre fill-extrusion, same styling approach as the mobile app — and the
// landmark workspace: every landmark of this PMTiles drawn over its building, the library
// in the drawer on the right (landmark-workspace.js, not loaded in diff mode).
/* global maplibregl, pmtiles */
import { extrusionFilter, matchBuildings } from "/static/landmark-store.js";
import { $, esc, fetchOk } from "/static/util.js";
import { COMPASS, basemapStyle, bindBasemapControls, initBasemap, loadVmapPresets, mapTheme, mobileBuilding, palette, recall, remember, vmap, vmapPreset } from "/static/basemap.js";
import { flyHome, runTour, tallest } from "/static/tour.js";


const HEIGHT_ATTRS = ["h_m", "height_m", "height", "render_height"];
const BASE_ATTRS = ["min_height", "render_min_height", "min_height_m"];
const params = new URLSearchParams(location.search);
let token = params.get("f");  // no f: the base of the city (see main)
const PARENT_ATTRS = ["parent_building_id", "parent_id"];
const state = { layer: null, fields: {}, stats: {}, heightAttr: null, baseAttr: null, parentAttr: null, idCol: params.get("idcol"), selected: params.get("id"), colorBy: "height", minzoom: 0, maxzoom: 22 };
// The landmark workspace (landmark-workspace.js), once loaded.
const landmark = { ws: null, failed: false };

// ------------------------------------------------------------------ shadows (prototype)
// Ground shadows precomputed on the server (app/shadows.py) for the area around the view,
// cast along the sun of the light preset (day sun on the other basemaps). Drawn as one
// dissolved fill under the labels and the buildings. A landmark with a version in use casts from
// its GLB mesh instead of its LoD1 box (app/landmark_shadow.py); night casts none.
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
    ["bldg-shadow", "lmk-shadow"].forEach((id) => { if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", "none"); });
    return;
  }
  const released = landmark.ws ? landmark.ws.released() : [];
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
    note(`Bóng có sẵn trong file (layer ${SHADOW_LAYER}, z${state.bakedShadows.minzoom ?? "?"}–${state.bakedShadows.maxzoom ?? "?"}), lọc theo preset = "${shadowPresetId()}" như trên app.`
      + (released.length ? ` ${released.length} landmark đổ bóng theo GLB đè lên, bóng hộp LoD1 của chúng trong file vẫn nằm dưới.` : ""));
    showLandmarkShadows(map, look, released);
    return;
  }

  const params = new URLSearchParams({ f: token, layer: state.layer, z: String(state.maxzoom), h: state.heightAttr, preset: shadowPresetId() });
  if (state.baseAttr) params.set("base", state.baseAttr);
  if (!$("#hideParentsWrap").hidden && $("#hideParents").checked) params.set("parts", "has_parts");
  // Not read by the server (it finds the released landmarks itself): a release or a placement
  // saved changes the key, so the shadows are asked for again.
  if (landmark.ws) params.set("lm", landmark.ws.shadowRev());
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
      res = await fetchOk(`/api/shadows?${params}`);
      data = await res.json();
    } catch (err) {
      if (seq === shadow.seq) note(`Không tính được bóng: ${err.message}`);
      return;
    }
    if (seq !== shadow.seq) return;
    const az = Number(res.headers.get("X-Sun-Azimuth"));
    const alt = Number(res.headers.get("X-Sun-Altitude"));
    const lms = Number(res.headers.get("X-Shadow-Landmarks") || 0);
    const lmErrors = res.headers.get("X-Shadow-Landmark-Errors");
    Object.assign(shadow, {
      key, bbox, data,
      info: `Nắng từ hướng ${COMPASS[Math.round(az / 45) % 8]}, cao ${alt}° → bóng dài ${(1 / Math.tan((alt * Math.PI) / 180)).toFixed(2)}× chiều cao. `
        + `${Number(res.headers.get("X-Shadow-Buildings")).toLocaleString("vi-VN")} building, ${res.headers.get("X-Shadow-Ms")} ms`
        + `${lms ? ` · ${lms} landmark đổ bóng theo GLB` : ""}. Kéo ra ngoài vùng này sẽ tự tính lại.`
        + `${lmErrors ? ` Giữ bóng LoD1: ${decodeURIComponent(lmErrors)}` : ""}`,
      download: `/api/shadows?${params}&download=1`,
    });
  }
  showShadow(map, look);
  note(shadow.info);
  $("#shadowDownload").href = shadow.download;
  $("#shadowDownloadRow").hidden = false;
}

// Baked shadows: the GLB shadow of each released landmark drawn over the file's own layer, with
// the same look. (On the fly, the server dissolves them into the area instead.)
const lmkShadow = { key: null, seq: 0 };
async function showLandmarkShadows(map, look, released) {
  const preset = shadowPresetId();
  const key = `${preset}|${landmark.ws?.shadowRev() ?? ""}`;
  if (lmkShadow.key !== key || !map.getSource("lmk-shadow")) {  // a style switch drops the source
    const seq = ++lmkShadow.seq;
    const files = await Promise.all(released.map((l) => fetch(`/api/landmarks/${encodeURIComponent(l.id)}/versions/${l.version}/shadow?preset=${preset}`)
      .then((r) => (r.ok ? r.json() : null)).catch(() => null)));
    if (seq !== lmkShadow.seq) return;
    const data = { type: "FeatureCollection", features: files.flatMap((f) => f?.features || []) };
    if (map.getSource("lmk-shadow")) map.getSource("lmk-shadow").setData(data);
    else map.addSource("lmk-shadow", { type: "geojson", data });
    lmkShadow.key = key;
  }
  if (!map.getLayer("lmk-shadow") && map.getSource("lmk-shadow")) {
    const before = map.getStyle().layers.find((l) => l.type === "symbol" && l.layout?.["text-field"])?.id;
    map.addLayer({ id: "lmk-shadow", type: "fill", source: "lmk-shadow", paint: { "fill-antialias": false } }, before);
  }
  if (!map.getLayer("lmk-shadow")) return;
  map.setPaintProperty("lmk-shadow", "fill-color", look.color);
  map.setPaintProperty("lmk-shadow", "fill-opacity", look.opacity);
  map.setLayoutProperty("lmk-shadow", "visibility", "visible");
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

// The OSM buildings of the city built by app/osmbase.py, shown when no file is given: the
// buildings new landmarks are registered on by default.
async function loadBase() {
  const st = await fetch("/api/base").then((r) => r.json()).catch(() => null);
  if (st?.base) return st.base;
  const building = ["queued", "running"].includes(st?.job?.status);
  $("#title").textContent = building ? `Base ${st.label} đang được tạo…` : `Chưa có base${st ? ` ${st.label}` : ""}.`;
  $("#meta").innerHTML = building
    ? "Tải lại trang sau vài phút, hoặc theo dõi tiến trình ở <a href=\"/\">trang chính</a>."
    : "Tạo base ở <a href=\"/\">trang chính</a>, hoặc mở một file .pmtiles ở đó.";
  return null;
}

async function main() {
  let base = null;
  if (!token) {
    base = await loadBase();
    if (!base) return;
    token = base.pmtiles_token;
  }
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
  const name = params.get("name") || base?.name || meta.name || "PMTiles";
  state.name = name;
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
  const center = hasCenter ? [Number(params.get("lon")), Number(params.get("lat"))] : base?.view.center || [header.centerLon, header.centerLat];
  const zoom = hasCenter ? Number(params.get("z") || 17) : base?.view.zoom || Math.max(header.centerZoom, header.minZoom + 1);
  const bldgSource = { type: "vector", url: `pmtiles://${url}`, attribution: meta.attribution || "" };

  const map = await createMap({ center, zoom, pitch: 55, bearing: -15 }, bldgSource, ["bldg-3d", "bldg-2d"]);

  map.on("load", () => {
    selectLayer(map, meta, layers[0]);
    updateZoomUI(map);
    openWorkspace(map);
  });
  map.on("zoom", () => updateZoomUI(map));
  map.on("moveend", () => updateZoomUI(map));
  map.on("click", (e) => onClick(map, e));

  // Picking the mobile basemap also switches the buildings to the mobile look. A switch made
  // before the first "load" leaves the building layers to selectLayer (no layer chosen yet).
  bindBasemapControls(map, () => bldgSource, () => { if (!state.layer) return; addBuildingLayers(map); applyStyle(map); landmark.ws?.ensureLayers(); }, (key) => {
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
  $("#collapseBtn").addEventListener("click", () => setPanelCollapsed(!$("#panel").classList.contains("collapsed")));
  bindDrawer(map);
  bindTour(map);
}

// ------------------------------------------------------------------ tour of the tallest
// The camera flies to the tallest buildings in view and circles each (tour.js), the building lit
// up as if clicked, a card at the bottom naming it. Esc, the Stop button or any touch of the map
// ends it; a tour that runs to the end flies back to where it started.
const tour = { ctrl: null, hint: "" };

function bindTour(map) {
  tour.hint = $("#tourNote").textContent;
  $("#tourBtn").addEventListener("click", () => (tour.ctrl ? tour.ctrl.abort("button") : startTour(map)));
  $("#tourStop").addEventListener("click", () => tour.ctrl?.abort("button"));
  addEventListener("keydown", (e) => { if (e.key === "Escape") tour.ctrl?.abort("button"); });
  // The user takes the camera: their gesture already stops the animation.
  ["mousedown", "touchstart", "wheel"].forEach((t) => map.on(t, () => tour.ctrl?.abort("user")));
}

async function startTour(map) {
  const b = map.getBounds();
  const stops = tallest(map.querySourceFeatures("bldg", { sourceLayer: state.layer }), {
    heightAttr: state.heightAttr, idCol: state.idCol, parentAttr: state.parentAttr,
    within: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()],
  });
  if (!stops.length) {
    $("#tourNote").textContent = "Không thấy building nào có chiều cao trong khung nhìn. Thử kéo tới vùng khác.";
    return;
  }
  const ctrl = (tour.ctrl = new AbortController());
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const home = { center: map.getCenter(), zoom: map.getZoom(), pitch: map.getPitch(), bearing: map.getBearing() };
  const selected = state.selected;
  if (!$("#threeD").checked) $("#threeD").checked = true;
  setTourUI(true, stops);
  try {
    await runTour(map, stops, {
      signal: ctrl.signal,
      reduced,
      onStop: (s, i) => {
        showTourStop(stops, i);
        state.selected = s.id;
        applyStyle(map);
      },
    });
    await flyHome(map, home, ctrl.signal, reduced);
  } catch (err) {
    if (!ctrl.signal.aborted) throw err;
    if (ctrl.signal.reason === "button") map.stop();
  } finally {
    tour.ctrl = null;
    setTourUI(false);
    state.selected = selected;
    applyStyle(map);
  }
}

function setTourUI(on, stops = []) {
  $("#tourCard").hidden = !on;
  $("#tourBtn").textContent = on ? "Dừng bay" : "Bay quanh toà cao nhất";
  $("#tourBtn").setAttribute("aria-pressed", String(on));
  if (!on) {
    $("#tourNote").textContent = tour.hint;
    return;
  }
  // The stops as a little skyline, tallest on the left, the current one lit like on the map.
  const max = stops[0].height;
  $("#tourSkyline").innerHTML = stops.map((s) => `<span style="height:${Math.max(12, (100 * s.height) / max)}%"></span>`).join("");
  $("#tourNote").textContent = `${stops.length} toà, cao nhất ${fmtM(max)}. Esc hoặc chạm vào bản đồ để dừng.`;
}

function showTourStop(stops, i) {
  const s = stops[i];
  [...$("#tourSkyline").children].forEach((el, j) => el.classList.toggle("on", j === i));
  $("#tourRank").textContent = `Cao thứ ${i + 1} / ${stops.length}`;
  $("#tourName").textContent = s.name || "Chưa có tên";
  $("#tourId").textContent = s.id;
  $("#tourH").textContent = fmtM(s.height);
}

const fmtM = (h) => `${h.toLocaleString("vi-VN", { maximumFractionDigits: 1 })} m`;

// ------------------------------------------------------------------ landmark workspace
// Loads landmark-workspace.js (and three.js) once the building layers are up.
async function openWorkspace(map) {
  try {
    const mod = await import("/static/landmark-workspace.js");
    landmark.ws = mod.create({
      map,
      token,
      params,
      state,
      name: () => state.name,
      threeD: () => $("#threeD").checked,
      palette: () => palette(mapTheme()),
      buildingFilter: (id) => ["all", partsFilter() || true, matchBuilding(id)],
      referenceColor: () => (state.colorBy === "mobile" && mobileBuilding() ? mobileBuilding().color : palette(mapTheme()).plain),
      select: (id) => {
        if (id === state.selected) return;
        state.selected = id;
        applyStyle(map);
      },
      syncExtrusions: () => syncExtrusions(map),
      syncShadows: () => { if (state.layer) syncShadows(map); },
      restyle: () => { if (state.layer) applyStyle(map); },
      openDrawer: () => setDrawer(map, true),
      previewUrl: (f, name, lm) => {
        const q = new URLSearchParams();
        if (f) q.set("f", f);
        if (name) q.set("name", name);
        if (lm) q.set("lm", lm);
        return `/preview${q.size ? `?${q}` : ""}`;
      },
    });
    window.lmWorkspace = landmark.ws;
    await landmark.ws.start();
  } catch (err) {
    landmark.failed = true;
    $("#lmBody").innerHTML = `<div class="alert bad"><span class="icon">!</span><div>Không tải được phần landmark: ${esc(err.message)}</div></div>`;
  }
}

// The drawer on the right: open or closed (remembered per browser), the map takes the room left.
// Closed by default on phones, where it is a bottom sheet over the map.
function bindDrawer(map) {
  const wanted = params.has("lm") || params.has("id");
  const saved = recall("drawer");
  const phone = window.matchMedia("(max-width: 640px)").matches;
  setDrawer(map, wanted || (saved ? saved === "1" : !phone), false);
  $("#drawerOpen").addEventListener("click", () => setDrawer(map, true));
  $("#drawerClose").addEventListener("click", () => setDrawer(map, false));
}

function setDrawer(map, open, save = true) {
  if ($("#lmDrawer").hidden !== open) return;
  $("#lmDrawer").hidden = !open;
  $("#drawerOpen").hidden = open;
  $("#drawerOpen").setAttribute("aria-expanded", String(open));
  if (save) remember("drawer", open ? "1" : "0");
  // On a phone the panel folds away while the sheet is open, so some map stays to work on.
  if (open && window.matchMedia("(max-width: 640px)").matches) setPanelCollapsed(true);
  map.resize();
}

// The map both page modes start from: view options, controls, and a pointer over the clickable layers.
async function createMap(view, bldgSource, hoverLayers) {
  const map = new maplibregl.Map({
    container: "map",
    maxPitch: 85,
    attributionControl: false,
    // Keeps the frame readable for screenshots / "save image" in the browser.
    canvasContextAttributes: { antialias: true, preserveDrawingBuffer: true },
    ...view,
    style: await basemapStyle($("#baseSel").value, bldgSource),
  });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
  map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
  window.map = map;
  hoverLayers.forEach((id) => {
    map.on("mouseenter", id, () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", id, () => { map.getCanvas().style.cursor = ""; });
  });
  return map;
}

function setPanelCollapsed(collapsed) {
  $("#panel").classList.toggle("collapsed", collapsed);
  $("#collapseBtn").textContent = collapsed ? "Mở" : "Thu gọn";
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
  // Polygons without a height (e.g. buffered roads from an Excel sheet) are flat: start in 2D.
  if (!state.heightAttr) $("#threeD").checked = false;
  state.baseAttr = BASE_ATTRS.find((a) => a in state.fields) || null;
  state.parentAttr = PARENT_ATTRS.find((a) => a in state.fields) || null;
  if (!state.idCol || !(state.idCol in state.fields)) {
    state.idCol = ["building_id", "id", "osm_id"].find((a) => a in state.fields) || null;
  }
  $("#hideParentsWrap").hidden = !("has_parts" in state.fields);
  $("#tourBtn").disabled = !state.heightAttr;

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
  landmark.ws?.layerChanged();
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
  const pending = landmark.ws && state.idCol ? landmark.ws.pending() : [];
  if (pending.length) {
    // Landmarks without a GLB yet keep their extrusion: coloured apart, to be found and clicked.
    expr = ["case", matchBuildings(pending, state.idCol, state.parentAttr), pal.accent, expr];
    legend += item(pal.accent, "Landmark chưa có GLB (click để upload)");
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

// The LoD1 extrusions: outlines with parts hidden (has_parts), and the buildings a landmark model
// stands on (parts included) left out once its GLB has loaded.
function syncExtrusions(map) {
  if (!map.getLayer("bldg-3d")) return;
  const hidden = landmark.ws ? landmark.ws.hidden() : [];
  map.setFilter("bldg-3d", extrusionFilter({ parts: partsFilter(), hidden, idCol: state.idCol, parentAttr: state.parentAttr }));
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
  // A landmark model replaces the extrusion of its building (parts included). With a version
  // in use its ground shadow is cast from the GLB, else it keeps its LoD1 box's (syncShadows).
  syncExtrusions(map);
  map.setLayoutProperty("bldg-3d", "visibility", threeD ? "visible" : "none");
  map.setLayoutProperty("bldg-2d", "visibility", threeD ? "none" : "visible");
  map.setLayoutProperty("bldg-line", "visibility", threeD ? "none" : "visible");
  if (!threeD && map.getPitch() > 0) map.easeTo({ pitch: 0 });
  if (threeD && map.getPitch() < 20) map.easeTo({ pitch: 55 });
  landmark.ws?.styleChanged();
  syncShadows(map);
}

// A landmark (its model or footprint) opens in the drawer; a building opens there with its
// attributes (a popup until the workspace is loaded, or when it could not be).
function onClick(map, e) {
  if (landmark.ws?.onClick(e)) return;
  const feats = map.queryRenderedFeatures(e.point, { layers: ["bldg-3d", "bldg-2d"].filter((id) => map.getLayoutProperty(id, "visibility") !== "none") });
  if (!feats.length) return;
  const props = feats[0].properties;
  const id = state.idCol && props[state.idCol] != null ? String(props[state.idCol]) : null;
  if (landmark.ws && id) {
    const parent = state.parentAttr && props[state.parentAttr] != null && String(props[state.parentAttr]) !== id ? String(props[state.parentAttr]) : null;
    landmark.ws.onBuilding({ id, parent, lngLat: e.lngLat, props });
    return;
  }
  if (id) {
    state.selected = id;
    applyStyle(map);
  }
  showPopup(map, e.lngLat, props);
}

function showPopup(map, lngLat, props) {
  const rows = Object.entries(props).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("");
  new maplibregl.Popup({ maxWidth: "380px" }).setLngLat(lngLat).setHTML(`<table class="props">${rows}</table>`).addTo(map);
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
  $("#tourControls").hidden = true;
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
  const map = await createMap({
    center: hasCenter ? [Number(params.get("lon")), Number(params.get("lat"))] : [header.centerLon, header.centerLat],
    zoom: hasCenter ? Number(params.get("z") || 16) : 5,
    pitch: 0,
  }, bldgSource, ["diff-fill", "diff-3d", "grid-fill"]);

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
  $("#collapseBtn").addEventListener("click", () => setPanelCollapsed(!$("#panel").classList.contains("collapsed")));
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

// ------------------------------------------------------------------ panel width
// The grip on the panel's right edge drags its width (remembered per browser); double-click
// or Home resets it, arrow keys nudge it.
const PANEL_W = { def: 300, min: 260, max: 900, step: 20 };

function bindPanelResize() {
  const panel = $("#panel");
  const grip = $("#panelGrip");
  const clamp = (w) => Math.round(Math.min(Math.max(w, PANEL_W.min), Math.min(PANEL_W.max, innerWidth - 80)));
  const place = () => {
    const r = panel.getBoundingClientRect();
    grip.style.left = `${r.right}px`;
    grip.style.top = `${r.top + r.height / 2}px`;
  };
  const setWidth = (w, save) => {
    const v = clamp(w);
    panel.style.setProperty("--panel-w", `${v}px`);
    grip.setAttribute("aria-valuenow", String(v));
    if (save) remember("panelWidth", String(v));
    place();
  };
  grip.setAttribute("aria-valuemin", String(PANEL_W.min));
  grip.setAttribute("aria-valuemax", String(PANEL_W.max));
  setWidth(Number(recall("panelWidth")) || PANEL_W.def, false);
  new ResizeObserver(place).observe(panel);
  addEventListener("resize", () => setWidth(panel.getBoundingClientRect().width, false));

  grip.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const startX = e.clientX;
    const startW = panel.getBoundingClientRect().width;
    grip.setPointerCapture(e.pointerId);
    grip.classList.add("dragging");
    document.body.classList.add("resizing-panel");
    const move = (ev) => setWidth(startW + ev.clientX - startX, false);
    const end = () => {
      grip.removeEventListener("pointermove", move);
      grip.removeEventListener("pointerup", end);
      grip.removeEventListener("pointercancel", end);
      grip.classList.remove("dragging");
      document.body.classList.remove("resizing-panel");
      setWidth(panel.getBoundingClientRect().width, true);
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", end);
    grip.addEventListener("pointercancel", end);
  });
  grip.addEventListener("dblclick", () => setWidth(PANEL_W.def, true));
  grip.addEventListener("keydown", (e) => {
    const w = panel.getBoundingClientRect().width;
    const step = e.shiftKey ? PANEL_W.step * 3 : PANEL_W.step;
    if (e.key === "ArrowRight") setWidth(w + step, true);
    else if (e.key === "ArrowLeft") setWidth(w - step, true);
    else if (e.key === "Home") setWidth(PANEL_W.def, true);
    else return;
    e.preventDefault();
  });
}

bindPanelResize();
main();
