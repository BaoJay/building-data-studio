// Which landmark models the 3D preview draws, and loading them. Pure logic: no DOM, no three.js,
// no MapLibre, so tests/js/landmark-store.test.mjs runs it under Node.
//
// Every landmark of the PMTiles being viewed that has a GLB is an entry (its id, the
// building_id it replaces, the footprint box, the GLB to load and its placement). The landmark
// being edited may be overridden by another version, or a local file, with a draft placement:
// the override stands in for the version drawn, so the two never stand on the same building.
//
// Models load lazily: only entries whose footprint box meets the view grown by BUFFER on each
// side, nearest to the centre first, at most CAPACITY of them; CONCURRENCY requests at a time,
// one request per GLB (entries sharing a key share the model). Loaded models stay in an LRU cache
// of CAPACITY models. reset() (another layer or source) aborts the requests on the way and drops
// their answers. A GLB that fails is not drawn: its building keeps its extrusion.
//
// Levels of detail: an entry may carry `lods`, the files of one version per zoom range
// (asset.extras.zoom_range [min, max | null], min <= zoom < max). The file whose range holds the
// view's zoom is drawn; outside every range nothing is (the building keeps its extrusion), except
// for the landmark being edited, which shows the file nearest the zoom. While the file of a new
// range loads, a file of the same entry already loaded stays on the map.

export const BUFFER = 0.25;
export const CONCURRENCY = 3;
export const CAPACITY = 24;

// [w, s, e, n] grown by `ratio` of its width / height on each side.
export function padBbox([w, s, e, n], ratio = BUFFER) {
  const dx = (e - w) * ratio;
  const dy = (n - s) * ratio;
  return [w - dx, s - dy, e + dx, n + dy];
}

export function intersects(a, b) {
  return a[0] <= b[2] && b[0] <= a[2] && a[1] <= b[3] && b[1] <= a[3];
}

// ------------------------------------------------------------------ GLB asset.extras and zoom ranges

// The glTF JSON of a .glb (ArrayBuffer), or null when it is not one.
export function glbJson(buffer) {
  try {
    const dv = new DataView(buffer);
    if (dv.byteLength < 20 || dv.getUint32(0, true) !== 0x46546c67 || dv.getUint32(16, true) !== 0x4e4f534a) return null;
    const len = dv.getUint32(12, true);
    return JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 20, Math.min(len, dv.byteLength - 20))));
  } catch {
    return null;
  }
}

const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

// asset.extras.anchor {lat, lon} when valid, else null (same rules as app/glb.py anchor_of).
export function anchorOf(extras) {
  const a = extras?.anchor;
  const lat = num(a?.lat);
  const lon = num(a?.lon);
  if (lat === null || lon === null || Math.abs(lat) > 85 || Math.abs(lon) > 180) return null;
  return { lat, lon };
}

// { lod_zoom, zoom_range: [min, max | null] } of asset.extras, or null (app/glb.py lod_of).
export function lodOf(extras) {
  const r = extras?.zoom_range;
  if (!Array.isArray(r) || r.length !== 2) return null;
  const lo = num(r[0]);
  const hi = r[1] === null ? null : num(r[1]);
  if (lo === null || lo < 0 || lo > 30 || (r[1] !== null && (hi === null || hi <= lo))) return null;
  return { lod_zoom: num(extras.lod_zoom) ?? lo, zoom_range: [lo, hi] };
}

// What the studio reads of a local .glb before drawing it: anchor and zoom range.
export function glbInfo(buffer) {
  const extras = glbJson(buffer)?.asset?.extras;
  return { anchor: anchorOf(extras), lod: lodOf(extras) };
}

export function inZoomRange(zoom, range) {
  return zoom >= range[0] && (range[1] === null || zoom < range[1]);
}

// How far a zoom is from a range (0 inside).
export function zoomDistance(zoom, range) {
  if (zoom < range[0]) return range[0] - zoom;
  return range[1] !== null && zoom >= range[1] ? zoom - range[1] + 1e-9 : 0;
}

export function zoomLabel(range) {
  const f = (v) => String(Math.round(v * 100) / 100).replace(".", ",");
  return range[1] === null ? `≥ ${f(range[0])}` : `${f(range[0])}–${f(range[1])}`;
}

// The file of `lods` drawn at `zoom`, or null outside every range (nearest: the closest one).
export function pickLod(lods, zoom, nearest = false) {
  if (!lods?.length) return null;
  if (zoom == null) return lods[0];
  const hit = lods.find((l) => inZoomRange(zoom, l.zoom_range));
  if (hit || !nearest) return hit || null;
  return [...lods].sort((a, b) => zoomDistance(zoom, a.zoom_range) - zoomDistance(zoom, b.zoom_range))[0];
}

// Ranges that share a zoom: [[a, b], …] of the files given.
export function overlappingLods(lods) {
  const out = [];
  lods.forEach((a, i) => lods.slice(i + 1).forEach((b) => {
    const [a0, a1] = a.zoom_range;
    const [b0, b1] = b.zoom_range;
    if (a0 < (b1 ?? Infinity) && b0 < (a1 ?? Infinity)) out.push([a, b]);
  }));
  return out;
}

export class ModelStore {
  // load(entry, signal, file) -> Promise<model> (file: lodOf(entry), what to load: key, url or
  // buffer, label); dispose(model); onChange(): the models shown changed.
  constructor({ load, dispose = () => {}, onChange = () => {}, concurrency = CONCURRENCY, capacity = CAPACITY, buffer = BUFFER } = {}) {
    Object.assign(this, { load, dispose, onChange, concurrency, capacity, buffer });
    this.items = new Map();  // id -> entry (version in use, else the newest)
    this.overrides = new Map();  // id -> entry (being edited)
    this.cache = new Map();  // key -> { model, used }
    this.pending = new Map();  // key -> AbortController
    this.queue = [];  // keys waiting for a free request
    this.errors = new Map();  // key -> message
    this.view = null;  // { bbox, center, zoom }
    this.wanted = [];  // entries the view asks for, nearest first
    this.gen = 0;
    this.clock = 0;
    this.signature = "";
  }

  // ---------------------------------------------------------------- entries

  // entry: { id, building_id, bbox: [w, s, e, n], centroid: [lon, lat], key, url | buffer, label,
  // placement, lods?: [{ key, url | buffer, label, lod_zoom, zoom_range }], ... }
  setItems(entries) {
    this.items = new Map(entries.map((e) => [e.id, { ...e }]));
    this.refresh();
  }

  setOverride(entry) {
    this.overrides.set(entry.id, { ...entry });
    this.refresh();
  }

  clearOverride(id) {
    if (this.overrides.delete(id)) this.refresh();
  }

  // Placement of the entry drawn for this id (its override when there is one).
  setPlacement(id, placement) {
    const e = this.overrides.get(id) || this.items.get(id);
    if (e) e.placement = { ...placement };
  }

  // Another layer or source: nothing loaded or on the way belongs to it.
  reset() {
    this.gen++;
    this.pending.forEach((ctl) => ctl.abort());
    this.pending.clear();
    this.queue = [];
    this.cache.forEach(({ model }) => this.dispose(model));
    this.cache.clear();
    this.errors.clear();
    this.items.clear();
    this.overrides.clear();
    this.refresh();
  }

  // A failed GLB is tried again.
  retry(id) {
    const f = this.entry(id) && this.lodOf(this.entry(id));
    if (f && this.errors.delete(f.key)) this.refresh();
  }

  // The file of an entry drawn at the view's zoom: { key, url, buffer, label, lod_zoom,
  // zoom_range }, or null outside every zoom range of its lods. The landmark being edited
  // always has one (the nearest range).
  lodOf(e) {
    if (!e.lods?.length) return { key: e.key, url: e.url, buffer: e.buffer, label: e.label, lod_zoom: null, zoom_range: null };
    return pickLod(e.lods, this.view?.zoom ?? null, this.overrides.get(e.id) === e);
  }

  entry(id) {
    return this.overrides.get(id) || this.items.get(id) || null;
  }

  entries() {
    const out = [...this.overrides.values()];
    for (const e of this.items.values()) if (!this.overrides.has(e.id)) out.push(e);
    return out;
  }

  // ---------------------------------------------------------------- view

  // bbox: [w, s, e, n] of the map view; center: [lon, lat]; zoom: picks the file of entries
  // with lods (null: their first file).
  setView(bbox, center, zoom = null) {
    this.view = { bbox, center, zoom };
    this.refresh();
  }

  // A file of this entry already loaded, other than `key` (its current file), or null.
  loadedSibling(e, key) {
    for (const l of e.lods || []) {
      const hit = l.key !== key && this.cache.get(l.key);
      if (hit) return { file: l, model: hit.model };
    }
    return null;
  }

  // The entries drawn now, each with its model and the file it came from. While the file of
  // the zoom loads, another file of the entry already loaded stays; an override whose model is
  // not loaded yet leaves the version drawn before on the map meanwhile. Neither once the file
  // failed.
  shown() {
    const out = [];
    for (const e of this.wanted) {
      const file = this.lodOf(e);
      if (!file) continue;
      const hit = this.cache.get(file.key);
      if (hit) {
        out.push({ entry: e, model: hit.model, file });
        continue;
      }
      if (this.errors.has(file.key)) continue;
      const sibling = this.loadedSibling(e, file.key);
      if (sibling) {
        out.push({ entry: e, model: sibling.model, file: sibling.file });
        continue;
      }
      const base = this.overrides.get(e.id) === e ? this.items.get(e.id) : null;
      const baseFile = base && this.lodOf(base);
      const fallback = (baseFile && this.cache.get(baseFile.key) && { file: baseFile, model: this.cache.get(baseFile.key).model })
        || (base && this.loadedSibling(base, null));
      if (fallback) out.push({ entry: base, model: fallback.model, file: fallback.file });
    }
    return out;
  }

  // building_ids whose extrusion a loaded model replaces.
  hiddenBuildings() {
    return [...new Set(this.shown().map((s) => s.entry.building_id))];
  }

  // "ready" | "loading" | "queued" | "error" | "away" (outside the view) | "zoom" (outside every
  // zoom range) | "none", and the error. Of the file of the zoom (lodOf).
  status(id) {
    const e = this.entry(id);
    if (!e) return { state: "none" };
    const file = this.lodOf(e);
    if (!file) return { state: "zoom" };
    const k = file.key;
    if (this.cache.has(k)) return { state: "ready" };
    if (this.errors.has(k)) return { state: "error", error: this.errors.get(k) };
    if (this.pending.has(k)) return { state: "loading" };
    if (this.queue.includes(k)) return { state: "queued" };
    return { state: "away" };
  }

  // ---------------------------------------------------------------- loading

  refresh() {
    this.wanted = this.pickWanted();
    const keys = new Set(this.wanted.map((e) => this.lodOf(e).key));
    // Kept in the cache too: the version drawn under an override, and what is on the map now.
    const fallbacks = this.wanted.map((e) => this.items.get(e.id)).filter(Boolean).map((b) => this.lodOf(b)?.key).filter(Boolean);
    const onMap = this.shown().map((s) => s.file.key);
    for (const k of keys) {
      const hit = this.cache.get(k);
      if (hit) hit.used = ++this.clock;
    }
    // Requests not wanted any more stay queued no longer; those on the way finish into the cache.
    this.queue = this.queue.filter((k) => keys.has(k));
    for (const k of keys) {
      if (!this.cache.has(k) && !this.pending.has(k) && !this.errors.has(k) && !this.queue.includes(k)) this.queue.push(k);
    }
    this.pump();
    this.evict(new Set([...keys, ...fallbacks, ...onMap]));
    this.notify();
  }

  pickWanted() {
    if (!this.view) return [];
    const area = padBbox(this.view.bbox, this.buffer);
    const [cx, cy] = this.view.center;
    const k = Math.cos((cy * Math.PI) / 180);
    const d = (e) => Math.hypot((e.centroid[0] - cx) * k, e.centroid[1] - cy);
    // The landmark being edited is always wanted, wherever the view is; the others when near the
    // view and one of their files is for this zoom.
    const edited = [...this.overrides.values()];
    const near = this.entries().filter((e) => !this.overrides.has(e.id) && intersects(e.bbox, area) && this.lodOf(e))
      .sort((a, b) => d(a) - d(b));
    return [...edited, ...near].slice(0, this.capacity);
  }

  byKey(key) {
    return this.wanted.find((e) => this.lodOf(e).key === key);
  }

  pump() {
    while (this.pending.size < this.concurrency && this.queue.length) {
      const key = this.queue.shift();
      const entry = this.byKey(key);
      if (entry) this.start(key, entry, this.lodOf(entry));
    }
  }

  start(key, entry, file) {
    const gen = this.gen;
    const ctl = new AbortController();
    this.pending.set(key, ctl);
    let job;
    try {
      job = Promise.resolve(this.load(entry, ctl.signal, file));
    } catch (err) {
      job = Promise.reject(err);
    }
    job.then(
      (model) => {
        if (gen !== this.gen) {
          this.dispose(model);
          return;
        }
        this.pending.delete(key);
        this.cache.set(key, { model, used: ++this.clock });
        this.refresh();
      },
      (err) => {
        if (gen !== this.gen) return;
        this.pending.delete(key);
        this.errors.set(key, String(err?.message || err));
        this.refresh();
      },
    );
  }

  // Least recently used first, never a model the view still shows.
  evict(keep) {
    if (this.cache.size <= this.capacity) return;
    const old = [...this.cache.entries()].filter(([k]) => !keep.has(k)).sort((a, b) => a[1].used - b[1].used);
    for (const [k, { model }] of old) {
      if (this.cache.size <= this.capacity) break;
      this.cache.delete(k);
      this.dispose(model);
    }
  }

  notify() {
    const sig = this.shown().map((s) => `${s.entry.id}@${s.file.key}`).join("|") + `#${[...this.errors.keys()].join("|")}`;
    if (sig === this.signature) return;
    this.signature = sig;
    this.onChange();
  }
}

// ------------------------------------------------------------------ map filters (MapLibre expressions)

// The buildings with these ids, or their parts (parentAttr: the parts' parent id column).
export function matchBuildings(ids, idCol, parentAttr = null) {
  const list = ["literal", [...ids].map(String)];
  const match = [["in", ["to-string", ["get", idCol]], list]];
  if (parentAttr) match.push(["in", ["to-string", ["get", parentAttr]], list]);
  return ["any", ...match];
}

// Filter of the LoD1 extrusions: the parts filter (outlines with has_parts hidden) and every
// building replaced by a landmark model left out. null: no filter.
export function extrusionFilter({ parts = null, hidden = [], idCol = null, parentAttr = null } = {}) {
  const all = [];
  if (parts) all.push(parts);
  if (hidden.length && idCol) all.push(["!", matchBuildings(hidden, idCol, parentAttr)]);
  return all.length > 1 ? ["all", ...all] : all[0] || null;
}

// How the preview shows a landmark whose GLB has loaded, to compare it with the LoD1 box of its
// building: "glb" the model alone, "lod1" the extrusion alone (the model not drawn), "both" the
// model with the extrusion see-through over it.
export const LANDMARK_VIEWS = ["glb", "lod1", "both"];

export function landmarkView(value) {
  return LANDMARK_VIEWS.includes(value) ? value : "glb";
}

// Filters of the LoD1 extrusions (bldg-3d) and of the see-through overlay of the buildings under
// a landmark model (null: no overlay) for one landmark view. hidden: the buildings a loaded model
// stands on (ModelStore.hiddenBuildings()).
export function landmarkFilters(view, { parts = null, hidden = [], idCol = null, parentAttr = null } = {}) {
  const extrusion = extrusionFilter({ parts, hidden: view === "lod1" ? [] : hidden, idCol, parentAttr });
  if (view !== "both" || !hidden.length || !idCol) return { extrusion, overlay: null };
  const under = matchBuildings(hidden, idCol, parentAttr);
  return { extrusion, overlay: parts ? ["all", parts, under] : under };
}

// Landmarks that can stand on the layer being viewed: registered on the same layer, with the
// same id column (older records without them are taken as is).
export function renderable(items, { layer, idCol }) {
  return items.filter((l) => (!l.layer || l.layer === layer) && (!l.id_field || l.id_field === idCol));
}
