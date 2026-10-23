// Landmark check in the 3D preview: a .glb placed on one building_id — a local file, or the
// version in use of that building in the landmark library (/landmarks) — drawn by
// landmark-model.js and lit by the style's own `light` exactly like the fill-extrusions around it
// (the formula and shaders are in landmark-light.js). preview.js imports this module the first
// time the "Landmark 3D" section is opened.
import { LandmarkLayer, LL } from "/static/landmark-model.js";

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const REF_LAYER_ID = "landmark-ref";
const EARTH_M = 40075016.686;  // equator length: metres per pixel = EARTH_M · cos(lat) / (512 · 2^zoom)
const HEIGHT_TOLERANCE = { m: 2, ratio: 0.05 };  // the height check warns past both
const COMPASS = ["bắc", "đông", "nam", "tây"];

// preview.js hands over the map and a few hooks (see openLandmark there).
export function init(api) {
  const lmk = new Landmark(api);
  lmk.bindUI();
  return lmk;
}

class Landmark {
  constructor(api) {
    this.api = api;
    this.map = api.map;
    this.place = null;  // /api/building answer for the building the model stands on
    this.lib = null;  // the library version shown: { building, version, anchor: [lon, lat], label }
    this.library = new Map();  // building_id -> landmark summary of the library
    this.view = new LandmarkLayer(this.map, {
      visible: () => $("#threeD").checked,
      referenceColor: () => this.api.referenceColor(),
      flat: () => $("#lmkFlat").checked,
      onLight: () => this.renderInfo(),
    });
  }

  // ---------------------------------------------------------------- UI

  bindUI() {
    $("#lmkFile").addEventListener("change", (e) => this.loadFile(e.target.files[0]));
    $("#lmkPick").addEventListener("click", () => {
      if (this.api.selectedId()) $("#lmkId").value = this.api.selectedId();
      this.syncButtons();
    });
    $("#lmkId").addEventListener("input", () => this.syncButtons());
    $("#lmkPlace").addEventListener("click", () => this.placeOn($("#lmkId").value.trim()));
    $("#lmkRemove").addEventListener("click", () => this.remove());
    ["#lmkHeading", "#lmkScale", "#lmkAnchor"].forEach((s) => $(s).addEventListener("input", () => this.update()));
    $("#lmkCompare").addEventListener("change", () => this.syncReference());
    $("#lmkFlat").addEventListener("change", () => this.update());
    $("#lmkLib").addEventListener("click", (e) => { if (e.target.closest("#lmkUseLib")) this.useLibrary(); });
    this.map.on("zoom", () => this.syncReferenceOffset());
    this.map.on("rotate", () => { if (this.view.light.anchor === "viewport") this.renderInfo(); });
    // The library may change in its own tab: read it again when this one comes back.
    window.addEventListener("focus", () => this.loadLibrary());
    if (!$("#lmkId").value && this.api.selectedId()) $("#lmkId").value = this.api.selectedId();
    this.loadLibrary();
    this.syncButtons();
  }

  syncButtons() {
    $("#lmkPick").disabled = !this.api.selectedId();
    $("#lmkPlace").disabled = !$("#lmkId").value.trim() || !this.api.ready();
    $("#lmkRemove").disabled = !this.place && !this.view.model;
    this.renderLibrary();
  }

  params() {
    const num = (s, d) => (Number.isFinite(Number($(s).value)) && $(s).value !== "" ? Number($(s).value) : d);
    const scale = num("#lmkScale", 1);
    return { heading: num("#lmkHeading", 0), scale: scale > 0 ? scale : 1, anchor: $("#lmkAnchor").value };
  }

  // The lon / lat the model stands on: the anchor of the library version shown, else the
  // centroid of the footprint.
  anchorLngLat() {
    return this.lib && this.place && this.lib.building === this.place.id ? this.lib.anchor : this.place.centroid;
  }

  note(text, warn = false) {
    $("#lmkStatus").textContent = text;
    $("#lmkStatus").classList.toggle("warn", warn);
  }

  // ---------------------------------------------------------------- model

  async loadFile(file) {
    if (!file) return;
    this.note(`Đang đọc ${file.name}…`);
    try {
      await this.view.load(await file.arrayBuffer(), file.name);
    } catch (err) {
      this.note(err.message, true);
      return;
    }
    this.lib = null;
    this.note("");
    this.update();
    this.syncButtons();
  }

  // ---------------------------------------------------------------- landmark library

  async loadLibrary() {
    try {
      const res = await fetch("/api/landmarks");
      if (!res.ok) return;
      const data = await res.json();
      this.library = new Map(data.landmarks.map((l) => [l.building_id, l]));
    } catch {
      return;  // the check works without the library
    }
    this.renderLibrary();
  }

  libraryEntry() {
    const id = $("#lmkId").value.trim() || this.api.selectedId();
    return id ? { id, entry: this.library.get(id) } : null;
  }

  renderLibrary() {
    const box = $("#lmkLib");
    const found = this.libraryEntry();
    const open = (id) => `<a href="${esc(this.api.libraryUrl(id))}" target="_blank" rel="noopener">Mở thư viện landmark ↗</a>`;
    const row = (text, extra = "") => `<div>${text}</div><div class="btn-row">${extra}${open(found?.id)}</div>`;
    if (!found) {
      box.innerHTML = row("Thư viện lưu các file GLB đã dùng cho từng building.");
      return;
    }
    const { id, entry } = found;
    if (!entry) {
      box.innerHTML = row(`Building ${esc(id)} chưa có trong thư viện.`);
      return;
    }
    const name = esc(entry.name || entry.building_id);
    if (entry.in_use == null) {
      box.innerHTML = row(`Có trong thư viện: <b>${name}</b>, ${entry.versions} version, chưa dùng version nào.`);
      return;
    }
    box.innerHTML = row(`Có trong thư viện: <b>${name}</b>, đang dùng v${entry.in_use} (${entry.versions} version).`,
      `<button class="btn small" type="button" id="lmkUseLib">Xem v${entry.in_use} từ thư viện</button>`);
  }

  // The version in use of the selected building, with its saved placement.
  async useLibrary() {
    const found = this.libraryEntry();
    const entry = found?.entry;
    if (!entry || entry.in_use == null) return;
    const base = `/api/landmarks/${encodeURIComponent(entry.id)}`;
    this.note(`Đang tải v${entry.in_use} từ thư viện…`);
    let v;
    try {
      const res = await fetch(base);
      const rec = await res.json();
      if (!res.ok) throw new Error(rec.error || `HTTP ${res.status}`);
      v = rec.versions.find((x) => x.version === rec.in_use);
      const file = await fetch(`${base}/versions/${v.version}/glb`);
      if (!file.ok) throw new Error((await file.json().catch(() => ({}))).error || `HTTP ${file.status}`);
      await this.view.load(await file.arrayBuffer(), `${v.original_name} (v${v.version})`);
    } catch (err) {
      this.note(`Không tải được từ thư viện: ${err.message}`, true);
      return;
    }
    const p = v.placement;
    $("#lmkHeading").value = p.heading;
    $("#lmkScale").value = p.scale;
    $("#lmkAnchor").value = p.anchor;
    $("#lmkFile").value = "";
    $("#lmkId").value = entry.building_id;
    this.lib = { building: entry.building_id, version: v.version, anchor: [p.lon, p.lat], label: entry.name || entry.building_id };
    this.note("");
    await this.placeOn(entry.building_id, { lng: p.lon, lat: p.lat });
  }

  // ---------------------------------------------------------------- placement

  async placeOn(id, near = null) {
    if (!id) return;
    const s = this.api.state;
    const c = near || this.api.lookAt(id);
    const q = new URLSearchParams({ f: this.api.token, layer: s.layer, idcol: s.idCol, id, lon: c.lng.toFixed(7), lat: c.lat.toFixed(7) });
    if (s.maxzoom != null) q.set("z", String(s.maxzoom));
    if (s.heightAttr) q.set("h", s.heightAttr);
    if (s.baseAttr) q.set("base", s.baseAttr);
    if (s.parentAttr) q.set("parent", s.parentAttr);
    this.note("Đang tìm footprint của building…");
    let place;
    try {
      const res = await fetch(`/api/building?${q}`);
      place = await res.json();
      if (!res.ok) throw new Error(place.error || `HTTP ${res.status}`);
    } catch (err) {
      this.note(`Không đặt được: ${err.message}`, true);
      return;
    }
    this.place = place;
    this.api.setHidden(id);  // hides its extrusion (and its parts); its ground shadow stays
    this.note("");
    this.update();
    this.syncButtons();
  }

  remove() {
    this.place = null;
    this.lib = null;
    this.view.clear();
    this.api.setHidden(null);
    if (this.map.getLayer(REF_LAYER_ID)) this.map.removeLayer(REF_LAYER_ID);
    $("#lmkFile").value = "";
    $("#lmkInfo").innerHTML = "";
    $("#lmkUniforms").textContent = "";
    this.note("");
    this.syncButtons();
  }

  // Called by preview.js after every (re)build of its building layers: a basemap or preset
  // switch replaces the whole style, custom layers included.
  ensureLayers() {
    if (!this.place || !this.map.getLayer("bldg-3d")) return;
    this.view.ensureLayer();  // under the extrusions: translucent buildings in front blend over it
    this.syncReference();
  }

  update() {
    const p = this.params();
    this.view.setPlacement(this.place ? { ...p, lon: this.anchorLngLat()[0], lat: this.anchorLngLat()[1] } : null);
    if (this.view.model && this.place) this.ensureLayers();
    this.syncReferenceOffset();
    this.renderInfo();
  }

  // ---------------------------------------------------------------- QA: the building's own extrusion beside the model

  syncReference() {
    const map = this.map;
    const on = $("#lmkCompare").checked && this.place && map.getLayer("bldg-3d");
    if (!on) {
      if (map.getLayer(REF_LAYER_ID)) map.removeLayer(REF_LAYER_ID);
      return;
    }
    if (!map.getLayer(REF_LAYER_ID)) {
      const before = map.getLayer(this.view.opts.id) ? this.view.opts.id : "bldg-3d";
      map.addLayer({ id: REF_LAYER_ID, type: "fill-extrusion", source: "bldg", "source-layer": this.api.state.layer, paint: {} }, before);
    }
    // Same geometry, height and gradient as bldg-3d, in the comparison colour, opaque (the
    // model is opaque too), moved sideways on screen.
    map.setFilter(REF_LAYER_ID, this.api.buildingFilter(this.place.id));
    map.setPaintProperty(REF_LAYER_ID, "fill-extrusion-color", this.api.referenceColor());
    map.setPaintProperty(REF_LAYER_ID, "fill-extrusion-opacity", 1);
    for (const p of ["fill-extrusion-height", "fill-extrusion-base", "fill-extrusion-vertical-gradient"]) {
      map.setPaintProperty(REF_LAYER_ID, p, map.getPaintProperty("bldg-3d", p));
    }
    map.setPaintProperty(REF_LAYER_ID, "fill-extrusion-translate-anchor", "viewport");
    this.syncReferenceOffset();
    this.renderInfo();
  }

  // fill-extrusion-translate is in pixels: keep the copy one footprint diagonal (+ 15 m) to the
  // right of the building at every zoom.
  syncReferenceOffset() {
    if (!this.place || !this.map.getLayer(REF_LAYER_ID)) return;
    const [w, d] = this.place.size_m;
    const metres = Math.hypot(w, d) + 15;
    const mpp = (EARTH_M * Math.cos((this.place.centroid[1] * Math.PI) / 180)) / (512 * 2 ** this.map.getZoom());
    this.map.setPaintProperty(REF_LAYER_ID, "fill-extrusion-translate", [metres / mpp, 0]);
  }

  // ---------------------------------------------------------------- readouts

  renderInfo() {
    const info = $("#lmkInfo");
    if (!info) return;
    const model = this.view.model;
    const rows = [];
    if (model) {
      const b = model.bounds;
      const { scale } = this.params();
      const [sx, sz] = this.view.modelSize();
      rows.push(`<div><b>${esc(model.name)}</b>: ${model.meshes} mesh, ${model.materials.length} vật liệu. `
        + `Hộp bao ${fmt(sx)} × ${fmt(sz)} m mặt bằng, cao ${fmt(this.view.modelHeight())} m trên điểm neo.</div>`);
      const off = Math.hypot((b.min.x + b.max.x) / 2, b.min.y, (b.min.z + b.max.z) / 2) * scale;
      if (off > 1) rows.push(`<div>Gốc GLB cách tâm đáy hộp bao ${fmt(off)} m.</div>`);
    }
    if (this.place) {
      const p = this.place;
      const [lon, lat] = this.anchorLngLat();
      const where = this.lib && this.lib.building === p.id
        ? `toạ độ neo của v${this.lib.version} trong thư viện, cách tâm footprint ${fmt(distanceM(p.centroid, [lon, lat]))} m`
        : "tâm footprint";
      rows.push(`<div>Neo tại ${lat.toFixed(6)}, ${lon.toFixed(6)}: ${where}, ${p.matched === "parent" ? "các khối con của " : ""}building `
        + `${esc(p.id)} (${fmt(p.area_m2, 0)} m², ${p.pieces} mảnh tile). Extrusion của building đã ẩn, bóng vẫn giữ.</div>`);
    }
    if (model && this.place) rows.push(heightCheck(this.view.modelHeight(), this.place, this.params().scale));
    if (model || this.place) rows.push(this.lightRows());
    info.innerHTML = rows.join("");
    $("#lmkUniforms").textContent = model ? this.uniformDump() : "";
  }

  lightRows() {
    const l = this.view.light;
    const [r, az, polar] = l.position;
    const extra = l.anchor === "viewport" ? ` + bearing ${fmt(this.map.getBearing(), 0)}°` : "";
    const warn = l.problems.length ? ` <span class="warn">Không dùng được ${l.problems.join(", ")} của light (expression?): dùng giá trị mặc định.</span>` : "";
    const emissive = this.view.night ? "Emissive: bật (style ban đêm)." : "Emissive: tắt (chỉ bật với style ban đêm).";
    return `<div>Light của style (anchor ${l.anchor}): màu ${rgb(l.color)}, cường độ ${l.intensity}, `
      + `[${r}, ${az}°${extra}, ${polar}°]. ${emissive}${warn}</div>`
      + `<div class="lmk-swatches">Màu mong đợi với màu so sánh ${esc(this.api.referenceColor())}: ${this.expectedSwatches()}</div>`;
  }

  // Roof and walls facing N / E / S / W, top of the wall, through the CPU copy of the shader.
  expectedSwatches() {
    const base = (LL.parseColor(this.api.referenceColor()) || [1, 1, 1]).slice(0, 3);
    const opts = { bearing: this.map.getBearing(), heightM: this.place?.height ?? (this.view.model ? this.view.modelHeight() : 0), t: 1 };
    const faces = [["mái", [0, 0, 1]], ...COMPASS.map((name, i) => [`tường ${name}`, [Math.sin((i * Math.PI) / 2), Math.cos((i * Math.PI) / 2), 0]])];
    return faces.map(([name, n]) => {
      const c = LL.shade(base, n.map((v) => (Math.abs(v) < 1e-12 ? 0 : v)), this.view.light, opts);
      return `<span class="lmk-sw" title="${esc(name)}: ${rgb(c)}"><i style="background:${rgb(c)}"></i>${esc(name)}</span>`;
    }).join("");
  }

  uniformDump() {
    const { heading, scale } = this.params();
    const u = LL.landmarkUniforms(this.view.light, { heading, bearing: this.map.getBearing(), night: this.view.night });
    const v = (a) => `[${a.map((x) => x.toFixed(3)).join(", ")}]`;
    return [
      `heading_deg = ${heading}`,
      `scale = ${scale}`,
      `L_enu = ${v(u.lightposEnu)}`,
      `u_lightpos = ${v(u.u_lightpos)}`,
      `u_lightcolor = ${v(u.u_lightcolor)}`,
      `u_lightintensity = ${u.u_lightintensity}`,
      `u_north = ${v(u.u_north)}`,
      `u_height_m = ${this.view.modelHeight().toFixed(2)}`,
      `u_emissive_on = ${u.u_emissive_on}`,
    ].join("\n");
  }
}

function heightCheck(modelH, place, scale) {
  if (place.height == null) return `<div>Building không có cột chiều cao để so.</div>`;
  const diff = modelH - place.height;
  const bad = Math.abs(diff) > HEIGHT_TOLERANCE.m && Math.abs(diff) > HEIGHT_TOLERANCE.ratio * place.height;
  const fit = modelH > 0 ? (scale * place.height) / modelH : null;
  return `<div class="${bad ? "warn" : ""}">Chiều cao: mô hình ${fmt(modelH)} m, building ${fmt(place.height)} m (${esc(place.height_field)}) → lệch `
    + `${diff >= 0 ? "+" : ""}${fmt(diff)} m (${diff >= 0 ? "+" : ""}${fmt((100 * diff) / (place.height || 1), 1)}%).`
    + `${bad && fit ? ` Tỉ lệ khớp chiều cao: ${fit.toPrecision(4)}.` : ""}</div>`;
}

// Ground distance in metres between two [lon, lat] (short distances).
function distanceM(a, b) {
  const k = (EARTH_M / 360) * Math.cos((a[1] * Math.PI) / 180);
  return Math.hypot((b[0] - a[0]) * k, ((b[1] - a[1]) * EARTH_M) / 360);
}

function fmt(v, digits = 1) {
  return Number(v).toLocaleString("vi-VN", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

function rgb(c) {
  return `rgb(${c.map((v) => Math.round(v * 255)).join(", ")})`;
}
