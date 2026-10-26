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

export class ModelStore {
  // load(entry, signal) -> Promise<model>; dispose(model); onChange(): the models shown changed.
  constructor({ load, dispose = () => {}, onChange = () => {}, concurrency = CONCURRENCY, capacity = CAPACITY, buffer = BUFFER } = {}) {
    Object.assign(this, { load, dispose, onChange, concurrency, capacity, buffer });
    this.items = new Map();  // id -> entry (version in use, else the newest)
    this.overrides = new Map();  // id -> entry (being edited)
    this.cache = new Map();  // key -> { model, used }
    this.pending = new Map();  // key -> AbortController
    this.queue = [];  // keys waiting for a free request
    this.errors = new Map();  // key -> message
    this.view = null;  // { bbox, center }
    this.wanted = [];  // entries the view asks for, nearest first
    this.gen = 0;
    this.clock = 0;
    this.signature = "";
  }

  // ---------------------------------------------------------------- entries

  // entry: { id, building_id, bbox: [w, s, e, n], centroid: [lon, lat], key, placement, ... }
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
    const e = this.entry(id);
    if (e && this.errors.delete(e.key)) this.refresh();
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

  // bbox: [w, s, e, n] of the map view; center: [lon, lat].
  setView(bbox, center) {
    this.view = { bbox, center };
    this.refresh();
  }

  // The entries drawn now, each with its model: an override whose model is not loaded yet
  // leaves the version drawn before on the map meanwhile (none once the override failed).
  shown() {
    const out = [];
    for (const e of this.wanted) {
      const hit = this.cache.get(e.key);
      if (hit) {
        out.push({ entry: e, model: hit.model });
        continue;
      }
      const base = this.overrides.get(e.id) === e ? this.items.get(e.id) : null;
      const fallback = base && !this.errors.has(e.key) && this.cache.get(base.key);
      if (fallback) out.push({ entry: base, model: fallback.model });
    }
    return out;
  }

  // building_ids whose extrusion a loaded model replaces.
  hiddenBuildings() {
    return [...new Set(this.shown().map((s) => s.entry.building_id))];
  }

  // "ready" | "loading" | "queued" | "error" | "away" (outside the view) | "none", and the error.
  status(id) {
    const e = this.entry(id);
    if (!e) return { state: "none" };
    if (this.cache.has(e.key)) return { state: "ready" };
    if (this.errors.has(e.key)) return { state: "error", error: this.errors.get(e.key) };
    if (this.pending.has(e.key)) return { state: "loading" };
    if (this.queue.includes(e.key)) return { state: "queued" };
    return { state: "away" };
  }

  // ---------------------------------------------------------------- loading

  refresh() {
    this.wanted = this.pickWanted();
    const keys = new Set(this.wanted.map((e) => e.key));
    const fallbacks = this.wanted.map((e) => this.items.get(e.id)?.key).filter(Boolean);
    for (const k of keys) {
      const hit = this.cache.get(k);
      if (hit) hit.used = ++this.clock;
    }
    // Requests not wanted any more stay queued no longer; those on the way finish into the cache.
    this.queue = this.queue.filter((k) => keys.has(k));
    for (const e of this.wanted) {
      if (!this.cache.has(e.key) && !this.pending.has(e.key) && !this.errors.has(e.key) && !this.queue.includes(e.key)) {
        this.queue.push(e.key);
      }
    }
    this.pump();
    this.evict(new Set([...keys, ...fallbacks]));
    this.notify();
  }

  pickWanted() {
    if (!this.view) return [];
    const area = padBbox(this.view.bbox, this.buffer);
    const [cx, cy] = this.view.center;
    const k = Math.cos((cy * Math.PI) / 180);
    const d = (e) => Math.hypot((e.centroid[0] - cx) * k, e.centroid[1] - cy);
    // The landmark being edited is always wanted, wherever the view is.
    const edited = [...this.overrides.values()];
    const near = this.entries().filter((e) => !this.overrides.has(e.id) && intersects(e.bbox, area)).sort((a, b) => d(a) - d(b));
    return [...edited, ...near].slice(0, this.capacity);
  }

  byKey(key) {
    return this.wanted.find((e) => e.key === key);
  }

  pump() {
    while (this.pending.size < this.concurrency && this.queue.length) {
      const key = this.queue.shift();
      const entry = this.byKey(key);
      if (entry) this.start(key, entry);
    }
  }

  start(key, entry) {
    const gen = this.gen;
    const ctl = new AbortController();
    this.pending.set(key, ctl);
    let job;
    try {
      job = Promise.resolve(this.load(entry, ctl.signal));
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
    const sig = this.shown().map((s) => `${s.entry.id}@${s.entry.key}`).join("|") + `#${[...this.errors.keys()].join("|")}`;
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

// Landmarks that can stand on the layer being viewed: registered on the same layer, with the
// same id column (older records without them are taken as is).
export function renderable(items, { layer, idCol }) {
  return items.filter((l) => (!l.layer || l.layer === layer) && (!l.id_field || l.id_field === idCol));
}
