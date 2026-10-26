// Landmark workspace of the 3D preview (/preview): the landmark library beside the LoD1 buildings
// of one PMTiles. preview.js loads it (with three.js) once the building layers are up, except in
// diff mode.
//
// Every landmark registered from this PMTiles with a version in use is drawn by one custom layer
// (landmark-model.js), its GLB loaded when it comes near the view (landmark-store.js). The
// extrusion of its building, parts included, is hidden once the GLB has loaded; a GLB that fails
// leaves the extrusion and the error shows in the drawer. Landmarks of other files are listed but
// never drawn here. Opening a landmark lets you look at another version, or a local file, with a
// draft placement: it stands in for the version in use until the landmark is closed.
//
// The drawer (landmark-drawer.js) carries everything of the library: list and search, building
// attributes and registration, GLB upload, versions, version in use, placement, history,
// manifest and trash. The URL keeps the landmark open (?lm=) or the building clicked (?id=) and
// the view (lon, lat, z), so reload and back / forward come back to them.
/* global maplibregl */
import { $, api, esc, reveal, toast } from "/static/util.js";
import { ModelStore, renderable } from "/static/landmark-store.js";
import { LandmarkLayer, disposeModel, parseModel } from "/static/landmark-model.js";
import * as ui from "/static/landmark-drawer.js";

const REF_LAYER_ID = "landmark-ref";
const HIT_LAYER_ID = "lm-hit";
const LINE_LAYER_ID = "lm-line";
const EARTH_M = 40075016.686;  // metres per pixel = EARTH_M · cos(lat) / (512 · 2^zoom)
const EMPTY = { type: "FeatureCollection", features: [] };
const PLACEMENT_KEYS = ["heading", "scale", "anchor", "lon", "lat"];
const round7 = (v) => Math.round(v * 1e7) / 1e7;
const lmUrl = (lid) => `/api/landmarks/${encodeURIComponent(lid)}`;
const glbUrl = (lid, n) => `${lmUrl(lid)}/versions/${n}/glb`;

// host (preview.js): map, token, params, state (layer, idCol, parentAttr, heightAttr, baseAttr,
// maxzoom), name(), threeD(), referenceColor(), buildingFilter(id), select(id) (the building lit
// up in the LoD1 colours), syncExtrusions() (bldg-3d filter, which reads hidden()), palette(),
// openDrawer(), previewUrl(f, name, lm). Call start() on the workspace returned.
export function create(host) {
  return new Workspace(host);
}

class Workspace {
  constructor(host) {
    this.host = host;
    this.map = host.map;
    this.lib = { dir: "", dirToken: null, landmarks: [], sources: [] };
    this.byId = new Map();
    this.byBuilding = new Map();  // building_id -> landmark of this PMTiles
    this.items = [];  // /api/landmarks/render: what is drawn
    this.sel = null;  // { kind: "landmark", rec } | { kind: "candidate", c: { id, parent, target, lngLat, props } }
    this.preview = null;  // what the open landmark shows: { kind: "version", n } | { kind: "local", name, file, buffer, seq }
    this.draft = null;  // its placement, as being edited
    this.place = null;  // building clicked: footprint read by /api/building, once a local file is tried on it
    this.overrideId = null;
    this.localSeq = 0;
    Object.assign(this, { search: "", picking: false, confirmUse: undefined, editing: false, uploading: false, compare: false, flat: false, qaOpen: false });
    this.marker = null;
    this.urlTimer = null;
    this.store = new ModelStore({ load: (e, signal) => this.loadModel(e, signal), dispose: disposeModel, onChange: () => this.onModels() });
    this.layer = new LandmarkLayer(this.map, {
      before: () => "bldg-3d",
      models: () => this.store.shown().map(({ entry, model }) => ({ model, placement: entry.placement, flat: entry.flat })),
      visible: () => host.threeD(),
      referenceColor: () => host.referenceColor(),
      onLight: () => ui.renderQa(this),
    });
  }

  // ---------------------------------------------------------------- start

  async start() {
    try {
      await Promise.all([this.loadLibrary(), this.loadRender()]);
    } catch (err) {
      $("#lmBody").innerHTML = `<div class="alert bad"><span class="icon">!</span><div>Không đọc được thư viện landmark: ${err.message}</div></div>`;
    }
    this.ensureLayers();
    this.viewChanged();
    this.bind();
    this.restore(this.host.params, true);
  }

  bind() {
    const map = this.map;
    map.on("moveend", () => {
      this.viewChanged();
      clearTimeout(this.urlTimer);
      this.urlTimer = setTimeout(() => this.writeUrl(false), 400);
    });
    map.on("zoom", () => this.syncReferenceOffset());
    map.on("rotate", () => { if (this.layer.light.anchor === "viewport") ui.renderQa(this); });
    for (const id of [HIT_LAYER_ID]) {
      map.on("mouseenter", id, () => { if (!this.picking) map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", id, () => { if (!this.picking) map.getCanvas().style.cursor = ""; });
    }
    const body = $("#lmBody");
    body.addEventListener("click", (e) => this.onDrawerClick(e));
    body.addEventListener("input", (e) => this.onDrawerInput(e));
    body.addEventListener("change", (e) => this.onDrawerChange(e));
    body.addEventListener("toggle", (e) => { if (e.target.id === "qaSec") this.qaOpen = e.target.open; }, true);
    $("#srcSel").addEventListener("change", (e) => this.openSource(e.target.value));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this.picking) {
        this.stopPicking();
        ui.syncPlacement(this);
      }
    });
    addEventListener("popstate", () => this.restore(new URLSearchParams(location.search), false));
    // The library may change in another tab: read it again when this one comes back.
    addEventListener("focus", () => this.refreshLists());
    addEventListener("beforeunload", (e) => { if (this.isDirty()) e.preventDefault(); });
  }

  // ---------------------------------------------------------------- library and what is drawn

  async loadLibrary() {
    const data = await api(`/api/landmarks?${new URLSearchParams({ f: this.host.token })}`);
    this.lib = { dir: data.dir, dirToken: data.dir_token, landmarks: data.landmarks, sources: data.sources };
    this.byId = new Map(data.landmarks.map((l) => [l.id, l]));
    this.byBuilding = new Map(data.landmarks.filter((l) => l.same_source).map((l) => [l.building_id, l]));
    this.renderSources();
    this.syncFootprints();
    // The list is refreshed in place, so the search box keeps its focus.
    if (!this.sel) {
      if ($("#lmItems")) ui.renderStatus(this); else ui.render(this);
    }
  }

  async loadRender() {
    const data = await api(`/api/landmarks/render?${new URLSearchParams({ f: this.host.token })}`);
    this.items = data.landmarks;
    this.applyItems();
  }

  async refreshLists() {
    try {
      await Promise.all([this.loadLibrary(), this.loadRender()]);
    } catch { /* the map keeps what it has */ }
  }

  applyItems() {
    const s = this.host.state;
    this.store.setItems(renderable(this.items, s).map((l) => ({
      id: l.id, building_id: l.building_id, bbox: l.bbox, centroid: l.centroid, key: l.glb, url: l.glb,
      label: `${l.original_name} (v${l.version})`, placement: l.placement,
    })));
    this.syncFootprints();
  }

  // Another layer of the PMTiles: nothing loaded belongs to it.
  layerChanged() {
    this.store.reset();
    this.applyItems();
    this.syncOverride();
    this.ensureLayers();
  }

  async loadModel(entry, signal) {
    if (entry.buffer) return parseModel(entry.buffer.slice(0), entry.label);
    const res = await fetch(entry.url, { signal });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `HTTP ${res.status}`);
    return parseModel(await res.arrayBuffer(), entry.label);
  }

  // The models drawn changed (one loaded, failed or left the cache).
  onModels() {
    this.host.syncExtrusions();
    this.map.triggerRepaint();
    ui.renderStatus(this);
  }

  hidden() {
    return this.store.hiddenBuildings();
  }

  viewChanged() {
    const b = this.map.getBounds();
    const c = this.map.getCenter();
    this.store.setView([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()], [c.lng, c.lat]);
  }

  sameSource(rec) {
    return Boolean(this.byId.get(rec.id)?.same_source);
  }

  // The PMTiles to switch to: the base of the city, the files landmarks were registered from.
  renderSources() {
    const opts = [...this.lib.sources];
    if (!opts.some((s) => s.token === this.host.token)) opts.unshift({ token: this.host.token, name: this.host.name() });
    // Files of the same name (outputs of several runs) are told apart by their folder.
    const twice = (s) => opts.filter((o) => o.name === s.name).length > 1;
    const label = (s) => (s.path && twice(s) ? s.path.split(/[\\/]/).slice(-2).join("/") : s.name) + (s.base ? " (base)" : "");
    $("#srcSel").innerHTML = opts.map((s) => `<option value="${esc(s.token)}">${esc(label(s))}</option>`).join("");
    $("#srcSel").value = this.host.token;
    $("#srcField").hidden = opts.length < 2;
  }

  openSource(token) {
    if (token === this.host.token || !this.leaveDraft()) {
      $("#srcSel").value = this.host.token;
      return;
    }
    location.assign(this.sourceUrl(token));
  }

  // /preview on a PMTiles (the base of the city opens without f, so the link outlives the
  // session's file tokens), with a landmark open.
  sourceUrl(token, lid = null) {
    const s = this.lib.sources.find((x) => x.token === token);
    return this.host.previewUrl(s?.base ? null : token, s?.base ? null : s?.name, lid);
  }

  // ---------------------------------------------------------------- map layers

  // Call after every (re)build of the building layers: a basemap or preset switch replaces the
  // whole style, custom layers included.
  ensureLayers() {
    const map = this.map;
    if (!map.getLayer("bldg-3d")) return;
    this.layer.ensureLayer();
    if (!map.getSource("lm")) map.addSource("lm", { type: "geojson", data: this.footprints() });
    // On top: the outline of the landmarks, and the invisible boxes that catch clicks on the
    // models (MapLibre cannot query a custom layer).
    [LINE_LAYER_ID, HIT_LAYER_ID].forEach((id) => { if (map.getLayer(id)) map.removeLayer(id); });
    map.addLayer({ id: LINE_LAYER_ID, type: "line", source: "lm", paint: {} });
    map.addLayer({ id: HIT_LAYER_ID, type: "fill-extrusion", source: "lm", paint: { "fill-extrusion-opacity": 0, "fill-extrusion-height": ["get", "h"] } });
    this.styleChanged();
    this.syncReference();
  }

  footprints() {
    const tall = new Map(this.items.map((l) => [l.id, l]));
    return {
      type: "FeatureCollection",
      features: this.lib.landmarks.filter((l) => l.same_source).map((l) => {
        const it = tall.get(l.id);
        const b = it?.model?.bounds;
        const modelH = b ? (b[1][1] - (it.placement.anchor === "bbox" ? b[0][1] : 0)) * it.placement.scale : 0;
        return { type: "Feature", geometry: l.geometry, properties: { id: l.id, h: Math.max(l.height ?? 10, modelH, 3) } };
      }),
    };
  }

  syncFootprints() {
    this.map.getSource("lm")?.setData(this.footprints());
  }

  // Colours follow the basemap; the hit boxes only in 3D (2D clicks go to the building fills).
  styleChanged() {
    const map = this.map;
    if (!map.getLayer(LINE_LAYER_ID)) return;
    const pal = this.host.palette();
    const sel = ["==", ["get", "id"], this.sel?.kind === "landmark" ? this.sel.rec.id : ""];
    map.setPaintProperty(LINE_LAYER_ID, "line-color", ["case", sel, pal.highlight, pal.ochre]);
    map.setPaintProperty(LINE_LAYER_ID, "line-width", ["case", sel, 3, 1.4]);
    map.setLayoutProperty(HIT_LAYER_ID, "visibility", this.host.threeD() ? "visible" : "none");
    this.syncReference();
    this.map.triggerRepaint();
  }

  // ---------------------------------------------------------------- clicks

  // A click on the map: picking the anchor, or a landmark (its model or footprint). true: handled.
  onClick(e) {
    if (this.picking) {
      this.setDraft({ lon: round7(e.lngLat.lng), lat: round7(e.lngLat.lat) });
      this.stopPicking();
      ui.syncPlacement(this);
      return true;
    }
    if (!this.map.getLayer(HIT_LAYER_ID) || this.map.getLayoutProperty(HIT_LAYER_ID, "visibility") === "none") return false;
    const f = this.map.queryRenderedFeatures(e.point, { layers: [HIT_LAYER_ID] })[0];
    if (!f) return false;
    this.select(f.properties.id, { fly: false });
    return true;
  }

  // A building of the LoD1 layer: its landmark (of the building or of its parent), else the
  // building itself, with its attributes and the form to register it.
  onBuilding({ id, parent, lngLat, props }) {
    const known = this.byBuilding.get(id) || (parent && this.byBuilding.get(parent));
    if (known) {
      this.select(known.id, { fly: false });
      return;
    }
    this.showCandidate({ id, parent, lngLat, props });
  }

  // ---------------------------------------------------------------- selection

  leaveDraft() {
    return !this.isDirty() || window.confirm(`Placement của v${this.preview.n} chưa lưu. Bỏ các thay đổi?`);
  }

  async select(lid, { fly = true, push = true } = {}) {
    if (lid === this.sel?.rec?.id) {
      if (fly) this.flyTo(this.sel.rec);
      this.host.openDrawer();
      return;
    }
    if (!this.leaveDraft()) {
      if (!push) this.writeUrl(true);
      return;
    }
    let rec;
    try {
      rec = await api(lmUrl(lid));
    } catch (err) {
      toast(err.message);
      return;
    }
    this.clearEdit();
    Object.assign(this, { sel: { kind: "landmark", rec }, confirmUse: undefined, editing: false });
    if (this.sameSource(rec)) {
      const shown = this.baseVersion();
      if (shown) this.showVersion(shown.version, { render: false });
      if (fly) this.flyTo(rec);
    }
    this.afterSelect(push);
  }

  // A part of a multi-part building is taken as the whole building unless asked otherwise.
  showCandidate(c, { push = true } = {}) {
    if (!this.leaveDraft()) {
      if (!push) this.writeUrl(true);
      return;
    }
    this.clearEdit();
    this.sel = { kind: "candidate", c: { ...c, target: c.target || c.parent || c.id } };
    this.afterSelect(push);
  }

  close({ push = true } = {}) {
    if (!this.sel) return;
    if (!this.leaveDraft()) {
      if (!push) this.writeUrl(true);
      return;
    }
    this.clearEdit();
    this.sel = null;
    this.afterSelect(push);
  }

  afterSelect(push) {
    const s = this.sel;
    this.host.select(s?.kind === "landmark" ? (this.sameSource(s.rec) ? s.rec.building_id : null) : s?.c.target ?? null);
    this.syncOverride();
    this.syncMarker();
    this.styleChanged();
    this.writeUrl(push);
    if (s) this.host.openDrawer();
    ui.render(this);
  }

  // Back to the version in use: the model being edited goes.
  clearEdit() {
    this.stopPicking();
    Object.assign(this, { preview: null, draft: null, place: null });
    this.syncOverride();
    this.syncMarker();
  }

  baseVersion() {
    const rec = this.sel?.rec;
    if (!rec) return null;
    return rec.versions.find((v) => v.version === rec.in_use) || rec.versions.at(-1) || null;
  }

  previewVersion() {
    return this.preview?.kind === "version" ? this.sel?.rec?.versions.find((v) => v.version === this.preview.n) || null : null;
  }

  // The footprint the model stands on: the landmark's, or the building clicked (read by /api/building).
  footprint() {
    if (this.sel?.kind === "landmark") return this.sel.rec.footprint;
    return this.place;
  }

  flyTo(rec) {
    const size = Math.max(...rec.footprint.size_m, 20);
    const zoom = Math.min(18.5, Math.max(15.5, 18.3 - Math.log2(size / 50)));
    this.map.easeTo({ center: rec.footprint.centroid, zoom, duration: 700 });
  }

  // ---------------------------------------------------------------- the model being edited

  showVersion(n, { render = true } = {}) {
    const v = this.sel.rec.versions.find((x) => x.version === n);
    if (!v) return;
    this.stopPicking();
    Object.assign(this, { preview: { kind: "version", n }, draft: { ...v.placement } });
    this.syncOverride();
    this.syncMarker();
    if (render) ui.render(this);
  }

  // What stands in for the version in use of the landmark open (or on the building clicked).
  overrideEntry() {
    const s = this.sel;
    const p = this.preview;
    if (!s || !p || !this.draft) return null;
    let base;
    if (s.kind === "landmark") {
      if (!this.sameSource(s.rec)) return null;
      const fp = s.rec.footprint;
      base = { id: s.rec.id, building_id: s.rec.building_id, bbox: fp.bbox, centroid: fp.centroid };
    } else {
      if (!this.place) return null;
      base = { id: `cand:${s.c.target}`, building_id: s.c.target, bbox: this.place.bbox, centroid: this.place.centroid };
    }
    const src = p.kind === "version"
      ? { key: glbUrl(base.id, p.n), url: glbUrl(base.id, p.n), label: `${this.previewVersion()?.original_name || "GLB"} (v${p.n})` }
      : { key: `local:${p.seq}`, buffer: p.buffer, label: p.name };
    return { ...base, ...src, placement: { ...this.draft }, flat: this.flat };
  }

  syncOverride() {
    const want = this.overrideEntry();
    if (this.overrideId && this.overrideId !== want?.id) this.store.clearOverride(this.overrideId);
    this.overrideId = want?.id ?? null;
    if (want) this.store.setOverride(want);
    this.syncReference();
    this.map.triggerRepaint();
  }

  // The model of the landmark being edited, once loaded.
  shownModel() {
    if (!this.overrideId) return null;
    const e = this.store.entry(this.overrideId);
    return this.store.shown().find((s) => s.entry === e)?.model || null;
  }

  loadNote() {
    if (!this.overrideId) return null;
    const st = this.store.status(this.overrideId);
    const what = this.preview?.kind === "version" ? `v${this.preview.n}` : this.preview?.name;
    if (st.state === "error") return { text: st.error, warn: true };
    if (st.state === "loading" || st.state === "queued") return { text: `Đang tải ${what}…` };
    return null;
  }

  isDirty() {
    const saved = this.previewVersion()?.placement;
    return Boolean(saved && this.draft && PLACEMENT_KEYS.some((k) => this.draft[k] !== saved[k]));
  }

  setDraft(patch) {
    if (!this.draft) return;
    this.draft = { ...this.draft, ...patch };
    if (this.overrideId) this.store.setPlacement(this.overrideId, this.draft);
    this.map.triggerRepaint();
    this.syncMarker();
    this.syncReference();
    ui.renderQa(this);
  }

  syncMarker() {
    const d = this.overrideEntry() && this.draft;
    if (!d) {
      this.marker?.remove();
      this.marker = null;
      return;
    }
    if (!this.marker) {
      const el = document.createElement("div");
      el.className = "lm-anchor";
      el.title = "Toạ độ neo: kéo để dời mô hình";
      this.marker = new maplibregl.Marker({ element: el, draggable: true }).setLngLat([d.lon, d.lat]).addTo(this.map);
      this.marker.on("drag", () => {
        const p = this.marker.getLngLat();
        this.setDraft({ lon: round7(p.lng), lat: round7(p.lat) });
        ui.syncPlacement(this);
      });
    } else {
      this.marker.setLngLat([d.lon, d.lat]);
    }
  }

  startPicking() {
    this.picking = true;
    this.map.getCanvas().style.cursor = "crosshair";
    $("#pickNote").hidden = false;
  }

  stopPicking() {
    if (!this.picking) return;
    this.picking = false;
    this.map.getCanvas().style.cursor = "";
    $("#pickNote").hidden = true;
  }

  // A .glb from this computer, drawn on the landmark (or the building clicked) and not uploaded.
  async tryLocal(file) {
    if (!file) return;
    let buffer;
    try {
      buffer = await file.arrayBuffer();
    } catch (err) {
      toast(`Không đọc được ${file.name}: ${err.message}`);
      return;
    }
    const s = this.sel;
    if (s.kind === "candidate" && !this.place) {
      toast("Đang tìm footprint của building…");
      try {
        this.place = await this.lookup(s.c);
      } catch (err) {
        toast(`Không đặt được: ${err.message}`);
        return;
      }
    }
    if (this.sel !== s) return;
    const fp = this.footprint();
    const start = this.draft || (s.kind === "landmark" && this.baseVersion()?.placement)
      || { heading: 0, scale: 1, anchor: "origin", lon: fp.centroid[0], lat: fp.centroid[1] };
    this.stopPicking();
    Object.assign(this, { preview: { kind: "local", name: file.name, file, buffer, seq: ++this.localSeq }, draft: { ...start } });
    this.syncOverride();
    this.syncMarker();
    ui.render(this);
  }

  dropLocal() {
    if (this.sel?.kind === "landmark") {
      const v = this.baseVersion();
      if (v) {
        this.showVersion(v.version);
        return;
      }
    }
    this.clearEdit();
    ui.render(this);
  }

  // Footprint of the building clicked, read around the click (as the landmark library does on
  // registration).
  async lookup(c) {
    const s = this.host.state;
    const q = new URLSearchParams({ f: this.host.token, layer: s.layer, idcol: s.idCol, id: c.target, lon: c.lngLat.lng.toFixed(7), lat: c.lngLat.lat.toFixed(7) });
    if (s.maxzoom != null) q.set("z", String(s.maxzoom));
    if (s.heightAttr) q.set("h", s.heightAttr);
    if (s.baseAttr) q.set("base", s.baseAttr);
    if (s.parentAttr) q.set("parent", s.parentAttr);
    return api(`/api/building?${q}`);
  }

  // ---------------------------------------------------------------- QA: the building's own extrusion beside the model

  syncReference() {
    const map = this.map;
    const e = this.overrideId && this.store.entry(this.overrideId);
    const on = this.compare && e && map.getLayer("bldg-3d");
    if (!on) {
      if (map.getLayer(REF_LAYER_ID)) map.removeLayer(REF_LAYER_ID);
      return;
    }
    if (!map.getLayer(REF_LAYER_ID)) {
      map.addLayer({ id: REF_LAYER_ID, type: "fill-extrusion", source: "bldg", "source-layer": this.host.state.layer, paint: {} }, this.layer.opts.id);
    }
    // Same geometry, height and gradient as bldg-3d, in the comparison colour, opaque (the
    // model is opaque too), moved sideways on screen.
    map.setFilter(REF_LAYER_ID, this.host.buildingFilter(e.building_id));
    map.setPaintProperty(REF_LAYER_ID, "fill-extrusion-color", this.host.referenceColor());
    map.setPaintProperty(REF_LAYER_ID, "fill-extrusion-opacity", 1);
    for (const p of ["fill-extrusion-height", "fill-extrusion-base", "fill-extrusion-vertical-gradient"]) {
      map.setPaintProperty(REF_LAYER_ID, p, map.getPaintProperty("bldg-3d", p));
    }
    map.setPaintProperty(REF_LAYER_ID, "fill-extrusion-translate-anchor", "viewport");
    map.setLayoutProperty(REF_LAYER_ID, "visibility", this.host.threeD() ? "visible" : "none");
    this.syncReferenceOffset();
  }

  // fill-extrusion-translate is in pixels: keep the copy one footprint diagonal (+ 15 m) to the
  // right of the building at every zoom.
  syncReferenceOffset() {
    const fp = this.footprint();
    if (!fp || !this.map.getLayer(REF_LAYER_ID)) return;
    const [w, d] = fp.size_m;
    const metres = Math.hypot(w, d) + 15;
    const mpp = (EARTH_M * Math.cos((fp.centroid[1] * Math.PI) / 180)) / (512 * 2 ** this.map.getZoom());
    this.map.setPaintProperty(REF_LAYER_ID, "fill-extrusion-translate", [metres / mpp, 0]);
  }

  // ---------------------------------------------------------------- URL

  // ?lm= (landmark open) or ?id= (building clicked), and the view; f / name / idcol stay.
  writeUrl(push) {
    const q = new URLSearchParams(location.search);
    const c = this.map.getCenter();
    q.set("lon", c.lng.toFixed(6));
    q.set("lat", c.lat.toFixed(6));
    q.set("z", this.map.getZoom().toFixed(2));
    if (this.host.state.idCol) q.set("idcol", this.host.state.idCol);
    q.delete("lm");
    q.delete("id");
    if (this.sel?.kind === "landmark") q.set("lm", this.sel.rec.id);
    else if (this.sel?.kind === "candidate") q.set("id", this.sel.c.target);
    const url = `${location.pathname}?${q}`;
    if (url === `${location.pathname}${location.search}`) return;
    if (push) history.pushState(null, "", url); else history.replaceState(null, "", url);
  }

  // Back / forward (and the first load): the source, the view and what was open.
  restore(q, initial) {
    if (!initial) {
      if ((q.get("f") || "") !== (this.host.params.get("f") || "")) {
        location.reload();
        return;
      }
      if (q.has("lon") && q.has("lat")) this.map.jumpTo({ center: [Number(q.get("lon")), Number(q.get("lat"))], zoom: Number(q.get("z") || this.map.getZoom()) });
    }
    const hasCenter = q.has("lon") && q.has("lat");
    const lm = q.get("lm");
    const id = q.get("id");
    if (lm) {
      if (this.byId.has(lm)) this.select(lm, { fly: !hasCenter, push: false });
      else toast(`Không có landmark ${lm} trong thư viện.`);
      return;
    }
    if (id && this.host.state.idCol) {
      const known = this.byBuilding.get(id);
      if (known) {
        this.select(known.id, { fly: !hasCenter, push: false });
        return;
      }
      const c = this.map.getCenter();
      const at = hasCenter ? { lng: Number(q.get("lon")), lat: Number(q.get("lat")) } : { lng: c.lng, lat: c.lat };
      this.showCandidate({ id, parent: null, target: id, lngLat: at, props: {} }, { push: false });
      this.readProps(id);
      return;
    }
    if (!initial) this.close({ push: false });
  }

  // Attributes of a building opened from the URL, once the tiles around it are loaded.
  readProps(id) {
    this.map.once("idle", () => {
      if (this.sel?.kind !== "candidate" || this.sel.c.target !== id) return;
      const s = this.host.state;
      const f = this.map.querySourceFeatures("bldg", { sourceLayer: s.layer, filter: ["==", ["to-string", ["get", s.idCol]], id] })[0];
      if (!f) return;
      this.sel.c.props = f.properties;
      ui.render(this);
    });
  }

  // ---------------------------------------------------------------- drawer events

  onDrawerClick(e) {
    const item = e.target.closest("[data-lm]");
    if (item) {
      this.select(item.dataset.lm);
      return;
    }
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const n = btn.dataset.v === "" ? null : Number(btn.dataset.v);
    switch (btn.dataset.act) {
      case "close": this.close(); break;
      case "open-dir": reveal(this.lib.dirToken); break;
      case "register": this.register(); break;
      case "upload": this.upload(); break;
      case "drop-local": this.dropLocal(); break;
      case "edit": this.editing = true; ui.render(this); break;
      case "edit-cancel": this.editing = false; ui.render(this); break;
      case "edit-save": this.saveEdit(); break;
      case "folder": reveal(this.sel.rec.folder_token); break;
      case "fly": this.flyTo(this.sel.rec); break;
      case "delete": this.removeLandmark(); break;
      case "show":
        if (this.leaveDraft()) this.showVersion(n);
        break;
      case "use":
        if (this.isDirty()) {
          toast(`Lưu hoặc hoàn tác placement của v${this.preview.n} trước.`);
          break;
        }
        this.confirmUse = n;
        ui.render(this);
        $("#useNote")?.focus();
        break;
      case "use-ok": this.useVersion(); break;
      case "use-cancel": this.confirmUse = undefined; ui.render(this); break;
      case "flip": this.setDraft({ heading: (this.draft.heading + 180) % 360 }); ui.syncPlacement(this); break;
      case "pick":
        if (this.picking) this.stopPicking(); else this.startPicking();
        ui.syncPlacement(this);
        break;
      case "centroid": {
        const [lon, lat] = this.footprint().centroid;
        this.setDraft({ lon, lat });
        ui.syncPlacement(this);
        break;
      }
      case "save-place": this.savePlacement(); break;
      case "reset-place":
        this.setDraft({ ...this.previewVersion().placement });
        ui.syncPlacement(this);
        break;
      default: break;
    }
  }

  onDrawerInput(e) {
    if (e.target.id === "lmSearch") {
      this.search = e.target.value;
      ui.renderStatus(this);
      return;
    }
    const key = e.target.dataset.p;
    if (!key || !this.draft) return;
    if (key === "anchor") {
      this.setDraft({ anchor: e.target.value });
    } else {
      const value = Number(e.target.value);
      if (e.target.value === "" || !Number.isFinite(value)) return;
      if (key === "scale" && value <= 0) return;
      this.setDraft({ [key]: key === "heading" ? ((value % 360) + 360) % 360 : value });
    }
    ui.syncPlacement(this);
  }

  onDrawerChange(e) {
    const t = e.target;
    if (t.name === "regTarget") {
      this.sel.c.target = t.value;
      this.clearEdit();
      this.afterSelect(false);
    } else if (t.id === "lmFile" && this.sel && (this.sel.kind === "candidate" || this.sameSource(this.sel.rec))) {
      this.tryLocal(t.files[0]);
    } else if (t.id === "lmkCompare") {
      this.compare = t.checked;
      this.syncReference();
      ui.renderQa(this);
    } else if (t.id === "lmkFlat") {
      this.flat = t.checked;
      this.syncOverride();
    }
  }

  // ---------------------------------------------------------------- actions

  async register() {
    const c = this.sel.c;
    const s = this.host.state;
    const body = {
      f: this.host.token, layer: s.layer, z: s.maxzoom, idcol: s.idCol, id: c.target, lon: c.lngLat.lng, lat: c.lngLat.lat,
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
    // A file being tried on the building stays, ready to upload as the first version.
    const local = this.preview?.kind === "local" ? { preview: this.preview, draft: this.draft } : null;
    await this.loadLibrary();
    this.clearEdit();
    this.sel = null;
    await this.select(rec.id, { fly: false });
    if (local && this.sel?.rec?.id === rec.id) {
      Object.assign(this, local);
      this.syncOverride();
      this.syncMarker();
      ui.render(this);
    }
    toast(`Đã đăng ký ${rec.name || rec.building_id}.`);
  }

  upload() {
    const file = this.preview?.kind === "local" ? this.preview.file : $("#lmFile")?.files[0];
    if (!file) {
      toast("Chọn một file .glb trước.");
      return;
    }
    const lid = this.sel.rec.id;
    const q = new URLSearchParams({ name: file.name, note: $("#upNote")?.value.trim() || "" });
    this.uploading = true;
    document.querySelector('[data-act="upload"]').disabled = true;
    $("#upBar").hidden = false;
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `${lmUrl(lid)}/versions?${q}`);
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable || !$("#upBar")) return;
      $("#upBar > span").style.width = `${(100 * e.loaded) / e.total}%`;
      $("#upStatus").textContent = e.loaded < e.total ? `${Math.round((100 * e.loaded) / e.total)}%` : "Đang kiểm tra GLB…";
    };
    const done = async (rec, error) => {
      this.uploading = false;
      if (error) {
        toast(`Upload thất bại: ${error}`);
        if (this.sel?.rec?.id === lid) ui.render(this);
        return;
      }
      await this.refreshLists();
      if (this.sel?.rec?.id !== lid) return;
      const draft = this.draft;
      this.sel.rec = rec;
      const v = rec.versions.at(-1);
      toast(`Đã lưu v${v.version}${v.same_as ? ` (trùng file với v${v.same_as})` : ""}.`);
      if (this.sameSource(rec)) {
        this.showVersion(v.version, { render: false });
        // The placement tried on the local file stays, unsaved, on the new version.
        if (draft) this.setDraft(draft);
      }
      ui.render(this);
    };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || "{}"); } catch { /* keep empty */ }
      if (xhr.status < 300) done(data); else done(null, data.error || `HTTP ${xhr.status}`);
    };
    xhr.onerror = () => done(null, "mất kết nối tới server");
    xhr.send(file);
  }

  async savePlacement() {
    const n = this.preview.n;
    let rec;
    try {
      rec = await api(`${lmUrl(this.sel.rec.id)}/versions/${n}/placement`, { method: "PUT", body: this.draft });
    } catch (err) {
      toast(`Không lưu được: ${err.message}`);
      return;
    }
    this.sel.rec = rec;
    this.setDraft({ ...this.previewVersion().placement });
    toast(`Đã lưu placement v${n}.`);
    ui.render(this);
    if (n === rec.in_use) this.refreshLists();  // the version in use moved: what is drawn follows
  }

  async useVersion() {
    const target = this.confirmUse;
    let rec;
    try {
      rec = await api(`${lmUrl(this.sel.rec.id)}/use`, { method: "POST", body: { version: target, note: $("#useNote").value.trim() } });
    } catch (err) {
      toast(err.message);
      return;
    }
    this.sel.rec = rec;
    this.confirmUse = undefined;
    await this.refreshLists();
    toast(target == null ? "Landmark không còn version nào đang dùng." : `Đang dùng v${target}.`);
    ui.render(this);
  }

  async saveEdit() {
    try {
      this.sel.rec = await api(lmUrl(this.sel.rec.id), { method: "PATCH", body: { name: $("#editName").value, link: $("#editLink").value } });
    } catch (err) {
      toast(err.message);
      return;
    }
    this.editing = false;
    await this.loadLibrary();
    ui.render(this);
  }

  async removeLandmark() {
    const rec = this.sel.rec;
    if (!window.confirm(`Xoá landmark ${rec.name || rec.building_id} khỏi thư viện?\n\nThư mục của nó (${rec.versions.length} file GLB) được chuyển vào _trash/, không bị xoá.`)) return;
    try {
      await api(lmUrl(rec.id), { method: "DELETE" });
    } catch (err) {
      toast(err.message);
      return;
    }
    this.draft = null;  // nothing left to save
    this.preview = null;
    this.close();
    await this.refreshLists();
    toast("Đã chuyển landmark vào _trash/.");
  }
}
