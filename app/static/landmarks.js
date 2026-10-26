// Landmark library page (/landmarks). One landmark per building_id, with the footprint read when
// it was registered; every GLB uploaded for it kept as a version (app/landmark_library.py); the
// version in use and the history of those choices. Each version has its own placement — heading,
// scale, the model point on the ground and the lon / lat it goes to — edited here on the map and
// saved with the version. The model is drawn by landmark-model.js, lit like the 3D preview.
/* global maplibregl, pmtiles */
import { $, api, esc, fmtBytes, fmtInt, fmtNum, reveal, toast } from "/static/util.js";
import { basemapStyle, bindBasemapControls, initBasemap, loadVmapPresets, mapTheme, mobileBuilding, palette, reloadBasemap } from "/static/basemap.js";
import { LandmarkLayer, disposeModel, parseModel } from "/static/landmark-model.js";

const HEIGHT_ATTRS = ["h_m", "height_m", "height", "render_height"];
const BASE_ATTRS = ["min_height", "render_min_height", "min_height_m"];
const PARENT_ATTRS = ["parent_building_id", "parent_id"];
const ID_ATTRS = ["building_id", "id", "osm_id"];
const SHADOW_LAYER = "shadows";
const HOME = { center: [105.8342, 21.0278], zoom: 12 };  // Hà Nội, when there is nothing to show yet
const HEIGHT_TOLERANCE = { m: 2, ratio: 0.05 };  // the height check of the 3D preview
const EARTH_M = 40075016.686;
const EMPTY = { type: "FeatureCollection", features: [] };

const params = new URLSearchParams(location.search);
const state = {
  dirToken: null,
  landmarks: [],  // summaries from /api/landmarks
  byBuilding: new Map(),
  sources: [],  // PMTiles landmarks were registered from
  src: null,  // the PMTiles whose buildings are shown (see openSource)
  rec: null,  // the selected landmark, in full
  candidate: null,  // a clicked building not in the library: { id, parent, target, lngLat, props }
  preview: null,  // the version shown on the map
  draft: null,  // its placement, as being edited
  loadNote: "",
  picking: false,
  confirmUse: undefined,  // version waiting for "Dùng" to be confirmed (null: stop using)
  editing: false,
  uploading: false,
};
let map;
let glb;  // LandmarkLayer
let model = null;  // the GLB of the version shown, parsed
let marker;  // the anchor of the version shown, draggable
let loadChain = Promise.resolve();
const protocol = new pmtiles.Protocol();
maplibregl.addProtocol("pmtiles", protocol.tile);

// ------------------------------------------------------------------ library data

async function loadLibrary() {
  const data = await api("/api/landmarks");
  state.dirToken = data.dir_token;
  state.landmarks = data.landmarks;
  state.byBuilding = new Map(data.landmarks.map((l) => [l.building_id, l]));
  state.sources = data.sources;
  $("#lmDir").textContent = data.dir;
  renderList();
  syncFootprints();
}

const lmUrl = (lid) => `/api/landmarks/${encodeURIComponent(lid)}`;
const glbUrl = (lid, n) => `${lmUrl(lid)}/versions/${n}/glb`;
const version = (n) => state.rec?.versions.find((v) => v.version === n) || null;

// ------------------------------------------------------------------ PMTiles source

// The PMTiles whose buildings are shown and can be registered: ?f= (opened from the main page or
// the 3D preview), then the base of the city (app/osmbase.py, the 3D preview's default too), then
// the files landmarks were registered from.
function sourceOptions() {
  const list = [...state.sources];
  const f = params.get("f");
  if (f && !list.some((s) => s.token === f)) list.unshift({ token: f, name: params.get("name") || "PMTiles đang mở" });
  return list;
}

async function openSource(token) {
  const opt = sourceOptions().find((s) => s.token === token) || { token };
  const url = new URL(`/api/files/${encodeURIComponent(token)}`, location.href).href;
  const archive = new pmtiles.PMTiles(url);
  protocol.add(archive);
  const header = await archive.getHeader();
  const meta = (await archive.getMetadata()) || {};
  const layers = (meta.vector_layers || []).filter((l) => l.id !== SHADOW_LAYER);
  const vl = layers.find((l) => l.id === opt.layer) || layers[0];
  if (!vl) throw new Error("file không có layer vector nào");
  const fields = vl.fields || {};
  return {
    token,
    url,
    name: opt.name || meta.name || "PMTiles",
    layer: vl.id,
    idCol: [params.get("idcol"), opt.id_field, ...ID_ATTRS].find((a) => a && a in fields) || null,
    heightAttr: HEIGHT_ATTRS.find((a) => a in fields) || null,
    baseAttr: BASE_ATTRS.find((a) => a in fields) || null,
    parentAttr: PARENT_ATTRS.find((a) => a in fields) || null,
    hasParts: "has_parts" in fields,
    maxzoom: header.maxZoom,
    view: opt.view || { center: [header.centerLon, header.centerLat], zoom: Math.max(header.centerZoom, header.minZoom + 1) },
  };
}

async function useSource(token) {
  state.src = null;
  if (token) {
    try {
      state.src = await openSource(token);
    } catch (err) {
      toast(`Không mở được PMTiles: ${err.message}`);
    }
  }
  renderSources();
}

function renderSources() {
  const opts = sourceOptions();
  $("#srcSel").innerHTML = opts.map((s) => `<option value="${esc(s.token)}">${esc(s.name)}</option>`).join("")
    + `<option value="">Không dùng PMTiles (chỉ footprint đã lưu)</option>`;
  $("#srcSel").value = state.src?.token || "";
  const s = state.src;
  $("#srcNote").textContent = s
    ? (s.idCol ? `Layer ${s.layer}, id ${s.idCol}${s.heightAttr ? `, chiều cao ${s.heightAttr}` : ""}. Click building để đăng ký.` : `Layer ${s.layer} không có cột id (building_id / id / osm_id): không đăng ký được.`)
    : opts.length ? "Chọn PMTiles để click building và đăng ký landmark mới."
      : "Tạo base Hà Nội ở trang chính, hoặc mở một file .pmtiles ở đó rồi bấm “Landmark 3D”, để đăng ký building mới.";
}

const bldgSource = () => (state.src ? { type: "vector", url: `pmtiles://${state.src.url}` } : null);

// ------------------------------------------------------------------ map

function initialView() {
  const lm = state.landmarks.find((l) => l.id === params.get("lm")) || state.byBuilding.get(params.get("id"));
  if (lm) return { center: lm.centroid, zoom: 17 };
  if (params.has("lon") && params.has("lat")) return { center: [Number(params.get("lon")), Number(params.get("lat"))], zoom: 17 };
  if (state.src) return state.src.view;
  if (state.landmarks.length) return { center: state.landmarks[0].centroid, zoom: 15 };
  return HOME;
}

function addLayers() {
  ["lm-fp-fill", "lm-fp-3d", "bldg-3d", "lm-fp-line", "lm-centroid"].forEach((id) => { if (map.getLayer(id)) map.removeLayer(id); });
  if (!map.getSource("lm")) map.addSource("lm", { type: "geojson", data: footprints() });
  if (!map.getSource("lm-pt")) map.addSource("lm-pt", { type: "geojson", data: EMPTY });
  map.addLayer({ id: "lm-fp-fill", type: "fill", source: "lm", paint: { "fill-opacity": 0.3 } });
  map.addLayer({ id: "lm-fp-3d", type: "fill-extrusion", source: "lm", paint: { "fill-extrusion-height": ["coalesce", ["get", "height"], 10], "fill-extrusion-opacity": 0.85 } });
  if (state.src && map.getSource("bldg")) {
    map.addLayer({ id: "bldg-3d", type: "fill-extrusion", source: "bldg", "source-layer": state.src.layer, paint: { "fill-extrusion-vertical-gradient": true } });
  }
  map.addLayer({ id: "lm-fp-line", type: "line", source: "lm", paint: {} });
  map.addLayer({ id: "lm-centroid", type: "circle", source: "lm-pt", paint: { "circle-radius": 3.5, "circle-stroke-width": 1.5 } });
  syncFootprints();
  applyStyle();
  glb.ensureLayer();
}

function footprints() {
  return {
    type: "FeatureCollection",
    features: state.landmarks.map((l) => ({ type: "Feature", geometry: l.geometry, properties: { id: l.id, height: l.height } })),
  };
}

function syncFootprints() {
  map?.getSource("lm")?.setData(footprints());
  const c = state.rec?.footprint.centroid;
  map?.getSource("lm-pt")?.setData(c ? { type: "Feature", geometry: { type: "Point", coordinates: c }, properties: {} } : EMPTY);
}

// The building itself, or one of its parts.
function matchBuilding(id, src = state.src) {
  const match = [["==", ["to-string", ["get", src.idCol]], id]];
  if (src.parentAttr) match.push(["==", ["to-string", ["get", src.parentAttr]], id]);
  return ["any", ...match];
}

function applyStyle() {
  if (!map?.getLayer("lm-fp-fill")) return;
  const pal = palette(mapTheme());
  const sel = ["==", ["get", "id"], state.rec?.id ?? ""];
  // The landmark whose model stands on the map: its building is not drawn under it.
  const shown = state.rec && model && state.draft ? state.rec : null;
  map.setPaintProperty("lm-fp-fill", "fill-color", ["case", sel, pal.accent, pal.ochre]);
  map.setPaintProperty("lm-fp-line", "line-color", ["case", sel, pal.highlight, pal.ochre]);
  map.setPaintProperty("lm-fp-line", "line-width", ["case", sel, 2.5, 1.2]);
  map.setPaintProperty("lm-fp-3d", "fill-extrusion-color", ["case", sel, pal.accent, pal.ochre]);
  map.setPaintProperty("lm-centroid", "circle-color", pal.outline);
  map.setPaintProperty("lm-centroid", "circle-stroke-color", pal.ground);
  // Without a PMTiles, the saved footprints stand in for the buildings.
  map.setLayoutProperty("lm-fp-3d", "visibility", map.getLayer("bldg-3d") ? "none" : "visible");
  map.setFilter("lm-fp-3d", shown ? ["!=", ["get", "id"], shown.id] : null);
  if (map.getLayer("bldg-3d")) {
    const s = state.src;
    const mobile = $("#baseSel").value === "vmap" ? mobileBuilding() : null;
    let color = mobile ? mobile.color : pal.neutral;
    if (s.idCol) {
      const ids = ["literal", state.landmarks.map((l) => l.building_id)];
      const registered = [["in", ["to-string", ["get", s.idCol]], ids]];
      if (s.parentAttr) registered.push(["in", ["to-string", ["get", s.parentAttr]], ids]);
      color = ["case", ["any", ...registered], pal.ochre, color];
      if (state.candidate) color = ["case", matchBuilding(state.candidate.target), pal.highlight, color];
    }
    const filters = [];
    if (s.hasParts) filters.push(["!", ["any", ["==", ["get", "has_parts"], true], ["==", ["get", "has_parts"], 1]]]);
    if (shown && s.idCol) filters.push(["!", matchBuilding(shown.building_id)]);
    map.setFilter("bldg-3d", filters.length ? ["all", ...filters] : null);
    map.setPaintProperty("bldg-3d", "fill-extrusion-color", color);
    map.setPaintProperty("bldg-3d", "fill-extrusion-height", s.heightAttr ? ["to-number", ["get", s.heightAttr], 0] : 4);
    map.setPaintProperty("bldg-3d", "fill-extrusion-base", s.baseAttr ? ["to-number", ["coalesce", ["get", s.baseAttr], 0], 0] : 0);
    map.setPaintProperty("bldg-3d", "fill-extrusion-opacity", mobile ? mobile.opacity : 0.9);
  }
  $("#mapLegend").innerHTML = `<span><i style="background:${pal.ochre}"></i>Building đã đăng ký</span>`
    + (state.src ? `<span><i style="background:${pal.highlight}"></i>Building vừa click</span>` : "")
    + `<span><i class="lm-anchor-sw"></i>Toạ độ neo (kéo để dời)</span><span><i class="dot" style="background:${pal.outline}"></i>Tâm footprint</span>`;
}

function flyTo(rec) {
  const size = Math.max(...rec.footprint.size_m, 20);
  const zoom = Math.min(18.5, Math.max(15.5, 18.3 - Math.log2(size / 50)));
  map.easeTo({ center: rec.footprint.centroid, zoom, duration: 700 });
}

function onClick(e) {
  if (state.picking) {
    setDraft({ lon: round7(e.lngLat.lng), lat: round7(e.lngLat.lat) });
    stopPicking();
    syncPlacementUI();
    return;
  }
  const layers = ["bldg-3d", "lm-fp-3d", "lm-fp-fill"].filter((id) => map.getLayer(id) && map.getLayoutProperty(id, "visibility") !== "none");
  const f = map.queryRenderedFeatures(e.point, { layers })[0];
  if (!f) return;
  if (f.layer.id !== "bldg-3d") {
    select(f.properties.id, { fly: false });
    return;
  }
  const s = state.src;
  const props = f.properties;
  if (!s.idCol || props[s.idCol] == null) return;
  const id = String(props[s.idCol]);
  const parent = s.parentAttr && props[s.parentAttr] != null && String(props[s.parentAttr]) !== id ? String(props[s.parentAttr]) : null;
  const known = state.byBuilding.get(id) || (parent && state.byBuilding.get(parent));
  if (known) {
    select(known.id, { fly: false });
    return;
  }
  showCandidate({ id, parent, lngLat: e.lngLat, props });
}

// ------------------------------------------------------------------ selection

function leaveDraft() {
  return !isDirty() || window.confirm(`Placement của v${state.preview} chưa lưu. Bỏ các thay đổi?`);
}

function setUrl() {
  if (state.rec) params.set("lm", state.rec.id); else params.delete("lm");
  ["id", "lon", "lat"].forEach((k) => params.delete(k));  // where the 3D preview pointed: read once
  history.replaceState(null, "", `?${params}`);
}

async function select(lid, { fly = true } = {}) {
  if (lid === state.rec?.id) {
    if (fly) flyTo(state.rec);
    return;
  }
  if (!leaveDraft()) return;
  let rec;
  try {
    rec = await api(lmUrl(lid));
  } catch (err) {
    toast(err.message);
    return;
  }
  clearModel();
  Object.assign(state, { rec, candidate: null, confirmUse: undefined, editing: false });
  setUrl();
  renderList();
  syncFootprints();
  if (fly) flyTo(rec);
  const shown = version(rec.in_use) || rec.versions.at(-1);
  showVersion(shown ? shown.version : null);
}

// A part of a multi-part building is registered as the whole building unless asked otherwise.
function showCandidate(c) {
  if (!leaveDraft()) return;
  Object.assign(state, { rec: null, candidate: { ...c, target: c.parent || c.id } });
  setUrl();
  clearModel();
  renderList();
  syncFootprints();
  renderDetail();
  applyStyle();
}

function closeDetail() {
  if (!leaveDraft()) return;
  Object.assign(state, { rec: null, candidate: null });
  setUrl();
  clearModel();
  renderList();
  syncFootprints();
  renderDetail();
  applyStyle();
}

function clearModel() {
  stopPicking();
  Object.assign(state, { preview: null, draft: null, loadNote: "" });
  clearGlb();
  syncMarker();
}

// Show one version on the map, with its saved placement. Loads run one after another and a
// version no longer wanted is skipped, so fast clicks end on the last one.
function showVersion(n) {
  const v = version(n);
  Object.assign(state, { preview: v ? n : null, draft: v ? { ...v.placement } : null, loadNote: v ? `Đang tải v${n}…` : "" });
  stopPicking();
  if (!v) clearGlb();
  syncMarker();
  renderDetail();
  applyStyle();
  if (!v) return;
  const lid = state.rec.id;
  loadChain = loadChain.then(async () => {
    if (state.rec?.id !== lid || state.preview !== n) return;
    try {
      const res = await fetch(glbUrl(lid, n));
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `HTTP ${res.status}`);
      const buf = await res.arrayBuffer();
      if (state.rec?.id !== lid || state.preview !== n) return;
      const parsed = await parseModel(buf, v.original_name);
      clearGlb();
      model = parsed;
      glb.update();
      state.loadNote = "";
    } catch (err) {
      clearGlb();
      state.loadNote = err.message;
    }
    syncMarker();
    applyStyle();
    syncPlacementUI();
  });
}

// ------------------------------------------------------------------ placement being edited

const PLACEMENT_KEYS = ["heading", "scale", "anchor", "lon", "lat"];
const round7 = (v) => Math.round(v * 1e7) / 1e7;

function isDirty() {
  const saved = version(state.preview)?.placement;
  return Boolean(saved && state.draft && PLACEMENT_KEYS.some((k) => state.draft[k] !== saved[k]));
}

function setDraft(patch) {
  if (!state.draft) return;
  state.draft = { ...state.draft, ...patch };
  map.triggerRepaint();
  syncMarker();
}

function clearGlb() {
  disposeModel(model);
  model = null;
  map?.triggerRepaint();
}

function syncMarker() {
  const d = state.rec && state.draft;
  if (!d) {
    marker?.remove();
    marker = null;
    return;
  }
  if (!marker) {
    const el = document.createElement("div");
    el.className = "lm-anchor";
    el.title = "Toạ độ neo: kéo để dời mô hình";
    marker = new maplibregl.Marker({ element: el, draggable: true }).setLngLat([d.lon, d.lat]).addTo(map);
    marker.on("drag", () => {
      const p = marker.getLngLat();
      setDraft({ lon: round7(p.lng), lat: round7(p.lat) });
      syncPlacementUI();
    });
  } else {
    marker.setLngLat([d.lon, d.lat]);
  }
}

function startPicking() {
  state.picking = true;
  map.getCanvas().style.cursor = "crosshair";
  $("#pickNote").hidden = false;
  syncPlacementUI();
}

function stopPicking() {
  if (!state.picking) return;
  state.picking = false;
  if (map) map.getCanvas().style.cursor = "";
  $("#pickNote").hidden = true;
}

// Model height above its anchor, from the box the server read in the GLB.
function modelHeight(v, p = v.placement) {
  const b = v.model.bounds;
  return b ? (b[1][1] - (p.anchor === "bbox" ? b[0][1] : 0)) * p.scale : null;
}

function distanceM(a, b) {
  const k = (EARTH_M / 360) * Math.cos((a[1] * Math.PI) / 180);
  return Math.hypot((b[0] - a[0]) * k, ((b[1] - a[1]) * EARTH_M) / 360);
}

// Values, readouts and buttons of the placement editor, without rebuilding it (keeps the
// focus and the slider drag).
function syncPlacementUI() {
  const box = $("#placeSec");
  if (!box) return;
  const d = state.draft;
  const v = version(state.preview);
  for (const el of box.querySelectorAll("[data-p]")) {
    if (el === document.activeElement && el.type !== "range") continue;
    const value = d[el.dataset.p];
    el.value = el.dataset.p === "heading" && el.type === "number" ? String(Math.round(value * 1000) / 1000) : String(value);
  }
  const dirty = isDirty();
  $("#placeDirty").hidden = !dirty;
  box.querySelector('[data-act="save-place"]').disabled = !dirty;
  box.querySelector('[data-act="reset-place"]').disabled = !dirty;
  box.querySelector('[data-act="pick"]').setAttribute("aria-pressed", String(state.picking));
  const fp = state.rec.footprint;
  const rows = [];
  if (state.loadNote) rows.push(`<div class="${state.loadNote.startsWith("Đang") ? "" : "warn"}">${esc(state.loadNote)}</div>`);
  rows.push(`<div>Cách tâm footprint ${fmtNum(distanceM(fp.centroid, [d.lon, d.lat]), 1)} m.</div>`);
  const h = modelHeight(v, d);
  if (h != null) {
    if (fp.height == null) {
      rows.push(`<div>Mô hình cao ${fmtNum(h, 1)} m trên điểm neo. Building không có chiều cao để so.</div>`);
    } else {
      const diff = h - fp.height;
      const bad = Math.abs(diff) > HEIGHT_TOLERANCE.m && Math.abs(diff) > HEIGHT_TOLERANCE.ratio * fp.height;
      const sign = diff >= 0 ? "+" : "";
      rows.push(`<div class="${bad ? "warn" : ""}">Mô hình cao ${fmtNum(h, 1)} m, building ${fmtNum(fp.height, 1)} m (${esc(fp.height_field || "chiều cao")}) → lệch ${sign}${fmtNum(diff, 1)} m (${sign}${fmtNum((100 * diff) / (fp.height || 1), 1)}%).</div>`);
    }
  }
  $("#placeReadout").innerHTML = rows.join("");
}

// ------------------------------------------------------------------ rendering

function renderList() {
  const q = $("#lmSearch").value.trim().toLowerCase();
  const items = state.landmarks.filter((l) => !q || [l.name, l.building_id, l.link].some((v) => String(v || "").toLowerCase().includes(q)));
  $("#lmCount").textContent = state.landmarks.length ? fmtInt(state.landmarks.length) : "";
  $("#lmItems").innerHTML = items.map((l) => {
    const badge = l.in_use != null ? `<span class="badge done">v${l.in_use}</span>` : `<span class="badge">${l.versions ? "chưa dùng" : "chưa có GLB"}</span>`;
    const meta = l.in_use != null ? `v${l.in_use} đang dùng · ${l.versions} version` : `${l.versions} version`;
    return `<li><button type="button" data-lm="${esc(l.id)}" aria-current="${l.id === state.rec?.id}">
      <span class="nm">${esc(l.name || l.building_id)}</span>${badge}
      <span class="meta">${l.name ? `<span class="mono">${esc(l.building_id)}</span> · ` : ""}${meta}</span></button></li>`;
  }).join("") || `<li class="hint lm-empty">${state.landmarks.length ? "Không có landmark khớp." : "Chưa có landmark nào. Click một building trên bản đồ để đăng ký."}</li>`;
}

function renderDetail() {
  const box = $("#lmDetail");
  box.innerHTML = state.rec ? landmarkHtml(state.rec) : state.candidate ? candidateHtml(state.candidate) : introHtml();
  if (state.rec && state.draft) syncPlacementUI();
}

function introHtml() {
  return `<section class="lm-sec">
    <h2 class="lm-title">Thư viện landmark</h2>
    <p class="muted">Mỗi landmark gắn với một <code>building_id</code> và footprint của nó. Mỗi lần upload GLB là một version mới, file cũ được giữ nguyên. Bạn chọn version đang dùng, và chỉnh hướng, toạ độ neo riêng cho từng version.</p>
    <ol class="notes">
      <li>Chọn file PMTiles ở ô <b>Bản đồ</b>, rồi click một building.</li>
      <li>Bấm <b>Đăng ký landmark</b>, kèm ID liên kết nếu có.</li>
      <li>Upload GLB, chỉnh hướng và toạ độ neo, lưu placement, rồi bấm <b>Dùng</b>.</li>
    </ol>
    <p class="hint">Hoặc chọn một landmark ở danh sách bên trái.</p>
  </section>`;
}

function candidateHtml(c) {
  const s = state.src;
  const rows = Object.entries(c.props).slice(0, 14).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("");
  const name = c.props.name || c.props["name:vi"] || "";
  const target = c.parent
    ? `<div class="field"><span>Đăng ký cho</span>
        <label class="check"><input type="radio" name="regTarget" value="${esc(c.parent)}" checked> Cả building (${esc(s.parentAttr)} = <code>${esc(c.parent)}</code>)</label>
        <label class="check"><input type="radio" name="regTarget" value="${esc(c.id)}"> Chỉ khối này (<code>${esc(c.id)}</code>)</label></div>`
    : "";
  return `<section class="lm-sec">
    <div class="lm-kicker">Building chưa có trong thư viện</div>
    <div class="lm-title-row"><h2 class="lm-title mono">${esc(c.id)}</h2><button class="btn small" type="button" data-act="close" aria-label="Đóng">×</button></div>
    ${target}
    <table class="props">${rows}</table>
  </section>
  <section class="lm-sec">
    <h3>Đăng ký landmark</h3>
    <label class="field"><span>Tên landmark</span><input type="text" id="regName" value="${esc(name)}" placeholder="vd. Nhà Quốc Hội Việt Nam"></label>
    <label class="field"><span>ID liên kết (tuỳ chọn)</span><input type="text" id="regLink" spellcheck="false" placeholder="canonical id MapOps, way/…, mã nội bộ"></label>
    <div class="btn-row"><button class="btn primary" type="button" data-act="register">Đăng ký landmark</button></div>
    <div class="hint">App đọc footprint của building trong <b>${esc(s.name)}</b> (zoom ${s.maxzoom}) và lưu cùng landmark, nên sau này mở thư viện không cần file này.</div>
  </section>`;
}

const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString("vi-VN", { dateStyle: "short", timeStyle: "short" }) : "");
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleDateString("vi-VN") : "");

function statusBadge(v) {
  if (v.status === "in_use") return `<span class="badge done">đang dùng</span>`;
  if (v.status === "used") {
    const until = v.used.at(-1)?.[1];
    return `<span class="badge" title="Đã dùng ${v.used.map(([a, b]) => `${fmtTime(a)} → ${fmtTime(b)}`).join("; ")}">đã dùng${until ? ` đến ${fmtDate(until)}` : ""}</span>`;
  }
  return `<span class="badge lm-unused">chưa dùng</span>`;
}

function versionRows(rec) {
  return [...rec.versions].reverse().map((v) => {
    const n = v.version;
    const showing = n === state.preview;
    const h = modelHeight(v);
    const flags = [
      v.model.unreadable.length ? `<div class="warn">Nén ${esc(v.model.unreadable.join(", "))}: bản đồ không đọc được</div>` : "",
      v.same_as ? `<div class="hint">Trùng file với v${v.same_as}</div>` : "",
    ].join("");
    const use = v.status === "in_use"
      ? `<button class="btn small" type="button" data-act="use" data-v="">Bỏ dùng</button>`
      : `<button class="btn small primary" type="button" data-act="use" data-v="${n}">Dùng</button>`;
    const model = [fmtBytes(v.size), `${fmtInt(v.model.triangles)} tam giác`, h == null ? "" : `cao ${fmtNum(h, 1)} m`].filter(Boolean).join(" · ");
    let row = `<tr class="${showing ? "showing" : ""}">
      <td><b>v${n}</b><div>${statusBadge(v)}</div></td>
      <td class="lm-wrap"><div>${esc(fmtTime(v.uploaded_at))}${v.by ? ` · ${esc(v.by)}` : ""}</div>
        <div class="mono lm-file" title="${esc(v.original_name)}">${esc(v.original_name)}</div>
        <div class="hint">${model}</div>${flags}
        ${v.note ? `<div class="lm-note">${esc(v.note)}</div>` : ""}</td>
      <td><div class="lm-acts">
        ${showing ? `<span class="hint">Đang xem</span>` : `<button class="btn small" type="button" data-act="show" data-v="${n}">Xem</button>`}
        ${use}
        <a class="btn small" href="${glbUrl(rec.id, n)}?download=1" download>Tải</a>
      </div></td></tr>`;
    if (state.confirmUse !== undefined && (state.confirmUse === n || (state.confirmUse === null && v.status === "in_use"))) {
      const what = state.confirmUse === null ? `Bỏ dùng v${n}? Landmark sẽ không có version nào đang dùng.`
        : rec.in_use != null ? `Dùng v${n} thay cho v${rec.in_use}?` : `Dùng v${n}?`;
      row += `<tr class="lm-confirm"><td colspan="3"><div class="lm-confirm-body"><b>${what}</b>
        <input type="text" id="useNote" placeholder="Ghi chú cho lịch sử (tuỳ chọn)">
        <div class="btn-row"><button class="btn small primary" type="button" data-act="use-ok">Xác nhận</button>
        <button class="btn small" type="button" data-act="use-cancel">Huỷ</button></div></div></td></tr>`;
    }
    return row;
  }).join("");
}

function historyHtml(rec) {
  const label = (ev) => {
    switch (ev.action) {
      case "register": return "Đăng ký landmark";
      case "upload": return `Upload v${ev.version}`;
      case "placement": return `Lưu placement v${ev.version}`;
      case "edit": return `Sửa ${ev.fields.map((f) => (f === "name" ? "tên" : "ID liên kết")).join(", ")}`;
      case "use":
        if (ev.to == null) return `Bỏ dùng v${ev.from}`;
        return ev.from == null ? `Dùng v${ev.to}` : `Dùng v${ev.to} thay v${ev.from}`;
      default: return ev.action;
    }
  };
  return [...rec.history].reverse().map((ev) => `<li class="${ev.action === "use" ? "use" : ""}">
    <span class="what">${esc(label(ev))}</span><span class="when">${esc(fmtTime(ev.at))}${ev.by ? ` · ${esc(ev.by)}` : ""}</span>
    ${ev.note ? `<div class="lm-note">${esc(ev.note)}</div>` : ""}</li>`).join("");
}

function landmarkHtml(rec) {
  const fp = rec.footprint;
  const inUse = rec.in_use != null ? `<span class="badge done">v${rec.in_use} đang dùng</span>` : `<span class="badge">chưa dùng version nào</span>`;
  const head = state.editing
    ? `<label class="field"><span>Tên landmark</span><input type="text" id="editName" value="${esc(rec.name)}"></label>
       <label class="field"><span>ID liên kết</span><input type="text" id="editLink" value="${esc(rec.link)}" spellcheck="false"></label>
       <div class="btn-row"><button class="btn small primary" type="button" data-act="edit-save">Lưu</button><button class="btn small" type="button" data-act="edit-cancel">Huỷ</button></div>`
    : `<dl class="kv">
        <dt>building_id</dt><dd class="mono">${esc(rec.building_id)}</dd>
        <dt>ID liên kết</dt><dd class="mono">${rec.link ? esc(rec.link) : `<span class="hint">—</span>`}</dd>
        <dt>Footprint</dt><dd>${fmtNum(fp.area_m2, 0)} m² · ${fp.height == null ? "không có chiều cao" : `cao ${fmtNum(fp.height, 1)} m`}
          <span class="hint">${fp.matched === "parent" ? "(gộp các khối con)" : ""}</span></dd>
        <dt>Tâm</dt><dd class="mono">${fp.centroid[1].toFixed(7)}, ${fp.centroid[0].toFixed(7)}</dd>
        <dt>Nguồn</dt><dd>${esc(rec.source?.name || "—")} <span class="hint">${esc(rec.source?.layer || "")}</span></dd>
        <dt>Đăng ký</dt><dd>${esc(fmtTime(rec.created_at))}</dd>
      </dl>
      <div class="btn-row"><button class="btn small" type="button" data-act="edit">Sửa tên / ID liên kết</button>
        <button class="btn small" type="button" data-act="folder">Mở thư mục</button></div>`;
  const base = version(rec.in_use) || rec.versions.at(-1);
  const v = version(state.preview);
  return `<section class="lm-sec">
    <div class="lm-kicker">Landmark · ${rec.versions.length} version</div>
    <div class="lm-title-row"><h2 class="lm-title">${esc(rec.name || rec.building_id)}</h2>
      <button class="btn small" type="button" data-act="close" aria-label="Đóng">×</button></div>
    <div>${inUse}</div>
    ${head}
  </section>
  <section class="lm-sec">
    <h3>Upload GLB</h3>
    <input type="file" id="upFile" accept=".glb,model/gltf-binary" aria-label="File GLB">
    <input type="text" id="upNote" placeholder="Ghi chú (tuỳ chọn)">
    <div class="btn-row"><button class="btn primary" type="button" data-act="upload" ${state.uploading ? "disabled" : ""}>Upload version mới</button>
      <span class="hint" id="upStatus"></span></div>
    <div class="progress" id="upBar" hidden><span></span></div>
    <div class="hint">${base ? `Version mới bắt đầu với placement của v${base.version}.` : "Version đầu tiên đặt ở tâm footprint, hướng 0."}</div>
  </section>
  ${rec.versions.length ? `<section class="lm-sec">
    <h3>Version</h3>
    <div class="tbl-wrap"><table class="lm-versions"><thead><tr><th>Version</th><th>File</th><th></th></tr></thead>
      <tbody>${versionRows(rec)}</tbody></table></div>
  </section>` : ""}
  ${v ? placementHtml(v) : ""}
  <section class="lm-sec">
    <h3>Lịch sử</h3>
    <ol class="lm-history">${historyHtml(rec)}</ol>
  </section>
  <section class="lm-sec lm-foot">
    <button class="btn small danger" type="button" data-act="delete">Xoá landmark…</button>
    <span class="hint">Thư mục được chuyển vào <code>_trash/</code>, không file nào bị xoá.</span>
  </section>`;
}

function placementHtml(v) {
  return `<section class="lm-sec lm-place" id="placeSec">
    <h3>Placement v${v.version} <span class="badge running" id="placeDirty" hidden>chưa lưu</span></h3>
    <div class="field"><span>Hướng (độ, theo chiều kim đồng hồ nhìn từ trên xuống)</span>
      <div class="lm-heading">
        <input type="range" min="0" max="360" step="0.5" data-p="heading" aria-label="Hướng">
        <input type="number" step="0.5" data-p="heading" aria-label="Hướng (độ)">
        <button class="btn small" type="button" data-act="flip" title="Cộng 180°">Xoay 180°</button>
      </div>
      <div class="hint">Hướng 0: trục +X của GLB chỉ hướng đông, mặt trước (+Z) quay về hướng nam.</div>
    </div>
    <div class="row2">
      <label class="field"><span>Tỉ lệ</span><input type="number" step="0.01" min="0" data-p="scale"></label>
      <label class="field"><span>Điểm neo trên mô hình</span>
        <select data-p="anchor"><option value="origin">Gốc GLB</option><option value="bbox">Tâm đáy hộp bao</option></select></label>
    </div>
    <div class="field"><span>Toạ độ neo (lon, lat)</span>
      <div class="row2">
        <input type="number" step="0.0000001" data-p="lon" aria-label="Kinh độ (lon)">
        <input type="number" step="0.0000001" data-p="lat" aria-label="Vĩ độ (lat)">
      </div>
      <div class="btn-row">
        <button class="btn small" type="button" data-act="pick" aria-pressed="false">Chọn trên bản đồ</button>
        <button class="btn small" type="button" data-act="centroid">Về tâm footprint</button>
      </div>
      <div class="hint">Điểm neo trên mô hình đứng ở toạ độ này, trên mặt đất. Có thể kéo điểm ◆ trên bản đồ.</div>
    </div>
    <div class="hint lm-readout" id="placeReadout"></div>
    <div class="btn-row">
      <button class="btn primary" type="button" data-act="save-place">Lưu placement</button>
      <button class="btn" type="button" data-act="reset-place">Hoàn tác</button>
    </div>
  </section>`;
}

// ------------------------------------------------------------------ actions

async function register() {
  const c = state.candidate;
  const s = state.src;
  const body = {
    f: s.token, layer: s.layer, z: s.maxzoom, idcol: s.idCol, id: c.target, lon: c.lngLat.lng, lat: c.lngLat.lat,
    h: s.heightAttr, base: s.baseAttr, parent: s.parentAttr, name: $("#regName").value, link: $("#regLink").value,
  };
  const btn = document.querySelector('[data-act="register"]');
  btn.disabled = true;
  btn.textContent = "Đang đọc footprint…";
  let rec;
  try {
    rec = await api("/api/landmarks", { method: "POST", body });
  } catch (err) {
    toast(`Không đăng ký được: ${err.message}`);
    btn.disabled = false;
    btn.textContent = "Đăng ký landmark";
    return;
  }
  await loadLibrary();
  state.candidate = null;
  toast(`Đã đăng ký ${rec.name || rec.building_id}.`);
  select(rec.id, { fly: false });
}

function upload() {
  const file = $("#upFile").files[0];
  if (!file) {
    toast("Chọn một file .glb trước.");
    return;
  }
  const lid = state.rec.id;
  const q = new URLSearchParams({ name: file.name, note: $("#upNote").value.trim() });
  state.uploading = true;
  document.querySelector('[data-act="upload"]').disabled = true;
  $("#upBar").hidden = false;
  const xhr = new XMLHttpRequest();
  xhr.open("PUT", `${lmUrl(lid)}/versions?${q}`);
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable || !$("#upBar")) return;
    $("#upBar > span").style.width = `${(100 * e.loaded) / e.total}%`;
    $("#upStatus").textContent = e.loaded < e.total ? `${fmtBytes(e.loaded)} / ${fmtBytes(e.total)}` : "Đang kiểm tra GLB…";
  };
  const done = async (rec, error) => {
    state.uploading = false;
    if (error) {
      toast(`Upload thất bại: ${error}`);
      if (state.rec?.id === lid) renderDetail();
      return;
    }
    await loadLibrary();
    if (state.rec?.id !== lid) return;
    state.rec = rec;
    const v = rec.versions.at(-1);
    toast(`Đã lưu v${v.version}${v.same_as ? ` (trùng file với v${v.same_as})` : ""}.`);
    showVersion(v.version);
  };
  xhr.onload = () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText || "{}"); } catch { /* keep empty */ }
    if (xhr.status < 300) done(data); else done(null, data.error || `HTTP ${xhr.status}`);
  };
  xhr.onerror = () => done(null, "mất kết nối tới server");
  xhr.send(file);
}

async function savePlacement() {
  const n = state.preview;
  let rec;
  try {
    rec = await api(`${lmUrl(state.rec.id)}/versions/${n}/placement`, { method: "PUT", body: state.draft });
  } catch (err) {
    toast(`Không lưu được: ${err.message}`);
    return;
  }
  state.rec = rec;
  state.draft = { ...version(n).placement };
  map.triggerRepaint();
  syncMarker();
  toast(`Đã lưu placement v${n}.`);
  renderDetail();
}

async function confirmUse() {
  const target = state.confirmUse;
  let rec;
  try {
    rec = await api(`${lmUrl(state.rec.id)}/use`, { method: "POST", body: { version: target, note: $("#useNote").value.trim() } });
  } catch (err) {
    toast(err.message);
    return;
  }
  state.rec = rec;
  state.confirmUse = undefined;
  await loadLibrary();
  toast(target == null ? "Landmark không còn version nào đang dùng." : `Đang dùng v${target}.`);
  renderDetail();
}

async function saveEdit() {
  try {
    state.rec = await api(lmUrl(state.rec.id), { method: "PATCH", body: { name: $("#editName").value, link: $("#editLink").value } });
  } catch (err) {
    toast(err.message);
    return;
  }
  state.editing = false;
  await loadLibrary();
  renderDetail();
}

async function removeLandmark() {
  const rec = state.rec;
  if (!window.confirm(`Xoá landmark ${rec.name || rec.building_id} khỏi thư viện?\n\nThư mục của nó (${rec.versions.length} file GLB) được chuyển vào _trash/, không bị xoá.`)) return;
  try {
    await api(lmUrl(rec.id), { method: "DELETE" });
  } catch (err) {
    toast(err.message);
    return;
  }
  state.draft = null;  // nothing left to save
  closeDetail();
  await loadLibrary();
  applyStyle();
  toast("Đã chuyển landmark vào _trash/.");
}

function onDetailClick(e) {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;
  const n = btn.dataset.v === "" ? null : Number(btn.dataset.v);
  switch (act) {
    case "close": closeDetail(); break;
    case "register": register(); break;
    case "upload": upload(); break;
    case "edit": state.editing = true; renderDetail(); break;
    case "edit-cancel": state.editing = false; renderDetail(); break;
    case "edit-save": saveEdit(); break;
    case "folder": reveal(state.rec.folder_token); break;
    case "delete": removeLandmark(); break;
    case "show":
      if (leaveDraft()) showVersion(n);
      break;
    case "use":
      if (isDirty()) {
        toast(`Lưu hoặc hoàn tác placement của v${state.preview} trước.`);
        break;
      }
      state.confirmUse = n;
      renderDetail();
      $("#useNote")?.focus();
      break;
    case "use-ok": confirmUse(); break;
    case "use-cancel": state.confirmUse = undefined; renderDetail(); break;
    case "flip": setDraft({ heading: (state.draft.heading + 180) % 360 }); syncPlacementUI(); break;
    case "pick":
      if (state.picking) stopPicking(); else startPicking();
      syncPlacementUI();
      break;
    case "centroid": {
      const [lon, lat] = state.rec.footprint.centroid;
      setDraft({ lon, lat });
      syncPlacementUI();
      break;
    }
    case "save-place": savePlacement(); break;
    case "reset-place":
      setDraft({ ...version(state.preview).placement });
      syncPlacementUI();
      break;
    default: break;
  }
}

function onDetailInput(e) {
  if (e.target.name === "regTarget") {
    state.candidate.target = e.target.value;
    applyStyle();
    return;
  }
  const key = e.target.dataset.p;
  if (!key || !state.draft) return;
  if (key === "anchor") {
    setDraft({ anchor: e.target.value });
  } else {
    const value = Number(e.target.value);
    if (e.target.value === "" || !Number.isFinite(value)) return;
    if (key === "scale" && value <= 0) return;
    setDraft({ [key]: key === "heading" ? ((value % 360) + 360) % 360 : value });
  }
  syncPlacementUI();
}

// ------------------------------------------------------------------ start

async function main() {
  try {
    await loadLibrary();
  } catch (err) {
    $("#lmDetail").innerHTML = `<div class="alert bad"><span class="icon">!</span><div>Không đọc được thư viện: ${esc(err.message)}</div></div>`;
    return;
  }
  const first = params.get("f") || state.sources[0]?.token || "";
  await useSource(first);
  if (window.matchMedia("(max-width: 640px)").matches) $("#mapCtl").open = false;  // leave the map visible on phones
  await loadVmapPresets();
  initBasemap("vmap");
  const view = initialView();
  map = new maplibregl.Map({
    container: "map",
    center: view.center,
    zoom: view.zoom,
    pitch: 55,
    bearing: -15,
    maxPitch: 85,
    attributionControl: false,
    canvasContextAttributes: { antialias: true, preserveDrawingBuffer: true },
    style: await basemapStyle($("#baseSel").value, bldgSource()),
  });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
  map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
  window.map = map;
  glb = new LandmarkLayer(map, {
    before: () => (map.getLayer("bldg-3d") ? "bldg-3d" : "lm-fp-line"),
    models: () => (model && state.draft ? [{ model, placement: state.draft }] : []),
  });

  map.on("load", () => {
    addLayers();
    const lm = state.landmarks.find((l) => l.id === params.get("lm")) || state.byBuilding.get(params.get("id"));
    if (lm) {
      select(lm.id, { fly: false });
    } else if (params.get("id") && params.has("lon") && params.has("lat") && state.src?.idCol) {
      // Opened from the 3D preview on a building not registered yet.
      showCandidate({ id: params.get("id"), parent: null, lngLat: { lng: Number(params.get("lon")), lat: Number(params.get("lat")) }, props: {} });
    } else {
      renderDetail();
    }
  });
  map.on("click", onClick);
  for (const id of ["bldg-3d", "lm-fp-3d", "lm-fp-fill"]) {
    map.on("mouseenter", id, () => { if (!state.picking) map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", id, () => { if (!state.picking) map.getCanvas().style.cursor = ""; });
  }
  bindBasemapControls(map, bldgSource, addLayers);

  $("#srcSel").addEventListener("change", async (e) => {
    await useSource(e.target.value);
    state.candidate = null;
    if (!state.rec) renderDetail();
    if (state.src && !state.rec) map.jumpTo(state.src.view);
    reloadBasemap(map, bldgSource, addLayers);
  });
  $("#lmSearch").addEventListener("input", renderList);
  $("#lmItems").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-lm]");
    if (btn) select(btn.dataset.lm);
  });
  $("#lmOpenDir").addEventListener("click", () => reveal(state.dirToken));
  $("#lmDetail").addEventListener("click", onDetailClick);
  $("#lmDetail").addEventListener("input", onDetailInput);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.picking) {
      stopPicking();
      syncPlacementUI();
    }
  });
  window.addEventListener("beforeunload", (e) => { if (isDirty()) e.preventDefault(); });
}

main();
