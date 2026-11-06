// node --test "tests/js/*.test.mjs"   (also run by tests/test_preview_js.py)
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ModelStore, anchorOf, extrusionFilter, glbInfo, inZoomRange, intersects, landmarkFilters, landmarkView, lodOf, matchBuildings,
  overlappingLods, padBbox, pickLod, renderable, zoomLabel,
} from "../../app/static/landmark-store.js";

// A landmark every 0.001° east of (105.8, 21), footprint 0.0002° wide.
function entry(i, extra = {}) {
  const lon = 105.8 + i * 0.001;
  return {
    id: `lm${i}`, building_id: `b${i}`, key: `/glb/${i}`, centroid: [lon + 0.0001, 21.0001],
    bbox: [lon, 21, lon + 0.0002, 21.0002], placement: { heading: 0, scale: 1, anchor: "origin", lon, lat: 21 }, ...extra,
  };
}

// Loads that finish only when the test says so.
function harness(opts = {}) {
  const calls = [];
  const disposed = [];
  let changes = 0;
  const store = new ModelStore({
    load: (e, signal) => new Promise((resolve, reject) => {
      const call = { key: e.key, signal, resolve: () => resolve({ key: e.key }), reject: (msg) => reject(new Error(msg)) };
      signal.addEventListener("abort", () => reject(new Error("aborted")));
      calls.push(call);
    }),
    dispose: (m) => disposed.push(m.key),
    onChange: () => { changes++; },
    ...opts,
  });
  return { store, calls, disposed, changes: () => changes };
}

const settle = () => new Promise((r) => setTimeout(r, 0));
const WIDE = [105.79, 20.99, 105.9, 21.01];
const CENTER = [105.8, 21];

test("bbox helpers", () => {
  assert.deepEqual(padBbox([0, 0, 4, 2], 0.25), [-1, -0.5, 5, 2.5]);
  assert.ok(intersects([0, 0, 1, 1], [1, 1, 2, 2]));
  assert.ok(!intersects([0, 0, 1, 1], [1.1, 0, 2, 1]));
});

test("only entries in the view plus a 25% buffer load", async () => {
  const { store, calls } = harness();
  // view 105.80 .. 105.804: buffer 0.001 each side -> up to 105.805 (lm0..lm5), not lm6
  store.setItems([0, 1, 2, 3, 4, 5, 6, 7].map((i) => entry(i)));
  store.setView([105.8, 21, 105.804, 21.0002], CENTER);
  await settle();
  const wanted = store.wanted.map((e) => e.id);
  assert.deepEqual(wanted, ["lm0", "lm1", "lm2", "lm3", "lm4", "lm5"]);
  assert.equal(calls.length, 3, "three requests at a time");
  assert.equal(store.status("lm7").state, "away");
});

test("at most three requests at a time, the rest follow", async () => {
  const { store, calls } = harness();
  store.setItems([0, 1, 2, 3, 4].map((i) => entry(i)));
  store.setView(WIDE, CENTER);
  await settle();
  assert.deepEqual(calls.map((c) => c.key), ["/glb/0", "/glb/1", "/glb/2"]);
  assert.equal(store.status("lm3").state, "queued");
  calls[1].resolve();
  await settle();
  assert.equal(calls.length, 4);
  assert.equal(store.status("lm1").state, "ready");
  assert.equal(store.pending.size, 3);
});

test("entries sharing a GLB share one request", async () => {
  const { store, calls } = harness();
  store.setItems([entry(0), entry(1, { key: "/glb/0" })]);
  store.setView(WIDE, CENTER);
  await settle();
  assert.equal(calls.length, 1);
  calls[0].resolve();
  await settle();
  assert.deepEqual(store.hiddenBuildings().sort(), ["b0", "b1"]);
});

test("the cache keeps at most 24 models, least recently used out first", async () => {
  const { store, calls, disposed } = harness({ capacity: 4, concurrency: 10 });
  store.setItems([0, 1, 2, 3, 4, 5].map((i) => entry(i)));
  store.setView([105.8, 21, 105.801, 21.0002], CENTER);  // lm0, lm1 (+ buffer: none further)
  await settle();
  calls.forEach((c) => c.resolve());
  await settle();
  // Move east: lm4, lm5 come in, lm0, lm1 leave the view but stay cached (4 models).
  store.setView([105.804, 21, 105.8052, 21.0002], [105.805, 21]);
  await settle();
  calls.slice(2).forEach((c) => c.resolve());
  await settle();
  assert.equal(store.cache.size, 4);
  assert.deepEqual(disposed, []);
  // Further east: lm5 stays, lm6 loads, lm0 (oldest) goes.
  store.setItems([0, 1, 2, 3, 4, 5, 6].map((i) => entry(i)));
  store.setView([105.8052, 21, 105.8062, 21.0002], [105.806, 21]);
  await settle();
  calls.filter((c) => c.key === "/glb/6").forEach((c) => c.resolve());
  await settle();
  assert.equal(store.cache.size, 4);
  assert.ok(disposed.includes("/glb/0"));
  assert.ok(store.cache.has("/glb/5") && store.cache.has("/glb/6"));
});

test("the view never asks for more models than the cache holds", async () => {
  const { store } = harness({ capacity: 3 });
  store.setItems([0, 1, 2, 3, 4].map((i) => entry(i)));
  store.setView(WIDE, [105.8035, 21]);
  assert.deepEqual(store.wanted.map((e) => e.id), ["lm3", "lm4", "lm2"]);
});

test("a reset in the middle of loading drops the late answers", async () => {
  const { store, calls, disposed } = harness();
  store.setItems([entry(0), entry(1)]);
  store.setView(WIDE, CENTER);
  await settle();
  const late = calls.slice();
  store.reset();
  assert.ok(late.every((c) => c.signal.aborted));
  store.setItems([entry(2)]);
  await settle();
  late[0].resolve();  // answered anyway
  await settle();
  assert.ok(!store.cache.has("/glb/0"));
  assert.ok(disposed.includes("/glb/0") || late[0].signal.aborted);
  assert.deepEqual(store.wanted.map((e) => e.id), ["lm2"]);
  assert.equal(store.pending.size, 1);
});

test("a GLB that fails keeps its extrusion and reports the error", async () => {
  const { store, calls, changes } = harness();
  store.setItems([entry(0), entry(1)]);
  store.setView(WIDE, CENTER);
  await settle();
  calls[0].reject("Không đọc được v1.glb");
  calls[1].resolve();
  await settle();
  assert.deepEqual(store.hiddenBuildings(), ["b1"]);
  assert.deepEqual(store.status("lm0"), { state: "error", error: "Không đọc được v1.glb" });
  assert.ok(changes() >= 2);
  // Not tried again by itself, only on retry.
  store.setView(WIDE, CENTER);
  await settle();
  assert.equal(calls.length, 2);
  store.retry("lm0");
  await settle();
  assert.equal(calls.length, 3);
});

test("an override stands in for the version in use, never beside it", async () => {
  const { store, calls } = harness();
  store.setItems([entry(0)]);
  store.setView(WIDE, CENTER);
  await settle();
  calls[0].resolve();
  await settle();
  store.setOverride(entry(0, { key: "/glb/0/v2", placement: { heading: 90, scale: 1, anchor: "bbox", lon: 105.8, lat: 21 } }));
  // While v2 loads, v1 stays (one model on the building).
  let shown = store.shown();
  assert.equal(shown.length, 1);
  assert.equal(shown[0].entry.key, "/glb/0");
  await settle();
  calls[1].resolve();
  await settle();
  shown = store.shown();
  assert.equal(shown.length, 1);
  assert.equal(shown[0].entry.key, "/glb/0/v2");
  assert.equal(shown[0].entry.placement.heading, 90);
  store.setPlacement("lm0", { heading: 45, scale: 2, anchor: "bbox", lon: 105.8, lat: 21 });
  assert.equal(store.shown()[0].entry.placement.heading, 45);
  // Closing restores the version in use with its saved placement.
  store.clearOverride("lm0");
  shown = store.shown();
  assert.equal(shown.length, 1);
  assert.equal(shown[0].entry.key, "/glb/0");
  assert.equal(shown[0].entry.placement.heading, 0);
});

test("a failed override leaves the building to its extrusion", async () => {
  const { store, calls } = harness();
  store.setItems([entry(0)]);
  store.setView(WIDE, CENTER);
  await settle();
  calls[0].resolve();
  await settle();
  store.setOverride(entry(0, { key: "local:1" }));
  await settle();
  calls[1].reject("bad file");
  await settle();
  assert.deepEqual(store.shown(), []);
  assert.deepEqual(store.hiddenBuildings(), []);
});

test("an override far from the view still loads", async () => {
  const { store, calls } = harness();
  store.setView([100, 10, 100.01, 10.01], [100, 10]);
  store.setOverride(entry(0, { id: "cand:b0" }));
  await settle();
  assert.equal(calls.length, 1);
});

test("the extrusion filter leaves out buildings and their parts", () => {
  const parts = ["!", ["==", ["get", "has_parts"], true]];
  assert.equal(extrusionFilter({}), null);
  assert.deepEqual(extrusionFilter({ parts }), parts);
  assert.deepEqual(extrusionFilter({ hidden: ["7"], idCol: "building_id" }),
    ["!", ["any", ["in", ["to-string", ["get", "building_id"]], ["literal", ["7"]]]]]);
  assert.deepEqual(extrusionFilter({ parts, hidden: ["7", 8], idCol: "building_id", parentAttr: "parent_building_id" }), [
    "all", parts,
    ["!", ["any",
      ["in", ["to-string", ["get", "building_id"]], ["literal", ["7", "8"]]],
      ["in", ["to-string", ["get", "parent_building_id"]], ["literal", ["7", "8"]]]]],
  ]);
  assert.deepEqual(matchBuildings(["1"], "id"), ["any", ["in", ["to-string", ["get", "id"]], ["literal", ["1"]]]]);
});

test("landmark view: the model alone, its LoD1 box alone, or both", () => {
  const parts = ["!", ["==", ["get", "has_parts"], true]];
  const opts = { parts, hidden: ["7"], idCol: "building_id" };
  const under = ["any", ["in", ["to-string", ["get", "building_id"]], ["literal", ["7"]]]];
  assert.deepEqual(landmarkFilters("glb", opts), { extrusion: ["all", parts, ["!", under]], overlay: null });
  // LoD1: the extrusion comes back, the model is not drawn.
  assert.deepEqual(landmarkFilters("lod1", opts), { extrusion: parts, overlay: null });
  // Both: the extrusion stays out of bldg-3d, drawn see-through by the overlay instead.
  assert.deepEqual(landmarkFilters("both", opts), { extrusion: ["all", parts, ["!", under]], overlay: ["all", parts, under] });
  assert.deepEqual(landmarkFilters("both", { hidden: ["7"], idCol: "building_id" }).overlay, under);
  // No model loaded, or no id column: nothing to overlay.
  assert.equal(landmarkFilters("both", { parts, idCol: "building_id" }).overlay, null);
  assert.equal(landmarkFilters("both", { hidden: ["7"] }).overlay, null);
  assert.equal(landmarkView("lod1"), "lod1");
  assert.equal(landmarkView("nope"), "glb");
  assert.equal(landmarkView(null), "glb");
});

test("landmarks of another layer or id column are not drawn", () => {
  const items = [{ id: "a", layer: "buildings", id_field: "building_id" }, { id: "b", layer: "other", id_field: "building_id" },
    { id: "c", layer: "buildings", id_field: "osm_id" }, { id: "d" }];
  assert.deepEqual(renderable(items, { layer: "buildings", idCol: "building_id" }).map((l) => l.id), ["a", "d"]);
});

// ------------------------------------------------------------------ levels of detail (zoom ranges)

const LODS = (i) => [
  { key: `/glb/${i}`, url: `/glb/${i}`, lod_zoom: 17, zoom_range: [17, null] },
  { key: `/glb/${i}?z=16`, url: `/glb/${i}?z=16`, lod_zoom: 16, zoom_range: [16, 17] },
  { key: `/glb/${i}?z=15`, url: `/glb/${i}?z=15`, lod_zoom: 15, zoom_range: [15, 16] },
];

// Like harness(), but loads the file of the zoom (load's third argument).
function lodHarness() {
  const calls = [];
  const store = new ModelStore({
    load: (e, signal, file) => new Promise((resolve, reject) => {
      calls.push({ key: file.key, resolve: () => resolve({ key: file.key }), reject: (msg) => reject(new Error(msg)) });
      signal.addEventListener("abort", () => reject(new Error("aborted")));
    }),
  });
  return { store, calls };
}

test("zoom ranges: min <= zoom < max, null max is open", () => {
  assert.ok(inZoomRange(17, [17, null]) && inZoomRange(30, [17, null]));
  assert.ok(inZoomRange(16, [16, 17]) && inZoomRange(16.99, [16, 17]) && !inZoomRange(17, [16, 17]));
  const lods = LODS(0);
  assert.equal(pickLod(lods, 18.2).lod_zoom, 17);
  assert.equal(pickLod(lods, 16.5).lod_zoom, 16);
  assert.equal(pickLod(lods, 15).lod_zoom, 15);
  assert.equal(pickLod(lods, 14.9), null, "outside every range: nothing");
  assert.equal(pickLod(lods, 14.9, true).lod_zoom, 15, "nearest for the landmark being edited");
  assert.equal(pickLod(lods, null).lod_zoom, 17);
  assert.equal(zoomLabel([17, null]), "≥ 17");
  assert.equal(zoomLabel([15.5, 16]), "15,5–16");
  assert.deepEqual(overlappingLods(lods), []);
  assert.equal(overlappingLods([...lods, { zoom_range: [16.5, 18] }]).length, 2);
});

test("the file of the zoom is drawn, the one before stays while it loads", async () => {
  const { store, calls } = lodHarness();
  store.setItems([entry(0, { lods: LODS(0) })]);
  store.setView(WIDE, CENTER, 17.4);
  await settle();
  assert.deepEqual(calls.map((c) => c.key), ["/glb/0"]);
  calls[0].resolve();
  await settle();
  assert.equal(store.shown()[0].file.lod_zoom, 17);
  store.setView(WIDE, CENTER, 16.2);
  await settle();
  assert.equal(calls.at(-1).key, "/glb/0?z=16");
  assert.equal(store.status("lm0").state, "loading");
  // Meanwhile the z17 model stays on the map: the extrusion does not flash.
  assert.equal(store.shown()[0].model.key, "/glb/0");
  assert.deepEqual(store.hiddenBuildings(), ["b0"]);
  calls.at(-1).resolve();
  await settle();
  assert.equal(store.shown()[0].model.key, "/glb/0?z=16");
  assert.equal(store.shown()[0].file.lod_zoom, 16);
});

test("outside every zoom range a landmark is not drawn and keeps its extrusion", async () => {
  const { store, calls } = lodHarness();
  store.setItems([entry(0, { lods: LODS(0) }), entry(1)]);  // lm1: one file, every zoom
  store.setView(WIDE, CENTER, 14);
  await settle();
  assert.deepEqual(calls.map((c) => c.key), ["/glb/1"]);
  calls[0].resolve();
  await settle();
  assert.equal(store.status("lm0").state, "zoom");
  assert.deepEqual(store.hiddenBuildings(), ["b1"]);
});

test("the landmark being edited shows the file nearest the zoom", async () => {
  const { store, calls } = lodHarness();
  store.setItems([]);
  store.setOverride(entry(0, { lods: LODS(0) }));
  store.setView(WIDE, CENTER, 13);
  await settle();
  assert.deepEqual(calls.map((c) => c.key), ["/glb/0?z=15"]);
});

test("a file of the zoom that fails is not replaced by another one", async () => {
  const { store, calls } = lodHarness();
  store.setItems([entry(0, { lods: LODS(0) })]);
  store.setView(WIDE, CENTER, 17);
  await settle();
  calls[0].resolve();
  await settle();
  store.setView(WIDE, CENTER, 16);
  await settle();
  calls.at(-1).reject("hỏng");
  await settle();
  assert.equal(store.status("lm0").state, "error");
  assert.deepEqual(store.shown(), []);
});

// A .glb holding only its JSON chunk.
function glbBuffer(json) {
  let data = new TextEncoder().encode(JSON.stringify(json));
  const pad = (4 - (data.length % 4)) % 4;
  const bytes = new Uint8Array(20 + data.length + pad);
  const dv = new DataView(bytes.buffer);
  dv.setUint32(0, 0x46546c67, true);
  dv.setUint32(4, 2, true);
  dv.setUint32(8, bytes.length, true);
  dv.setUint32(12, data.length + pad, true);
  dv.setUint32(16, 0x4e4f534a, true);
  bytes.set(data, 20);
  bytes.fill(0x20, 20 + data.length);
  return bytes.buffer;
}

test("asset.extras of a local .glb: anchor and zoom range", () => {
  const extras = { anchor: { lat: 21.0156627, lon: 105.7828616 }, lod_zoom: 16, zoom_range: [16, 17] };
  assert.deepEqual(glbInfo(glbBuffer({ asset: { version: "2.0", extras } })),
    { anchor: { lat: 21.0156627, lon: 105.7828616 }, lod: { lod_zoom: 16, zoom_range: [16, 17] } });
  assert.deepEqual(glbInfo(glbBuffer({ asset: { version: "2.0" } })), { anchor: null, lod: null });
  assert.deepEqual(glbInfo(new ArrayBuffer(8)), { anchor: null, lod: null });
  assert.equal(anchorOf({ anchor: { lat: 105.78, lon: 21.01 } }), null, "lat and lon swapped: lat beyond 85 is refused");
  assert.equal(anchorOf({ anchor: [21, 105] }), null);
  assert.deepEqual(lodOf({ zoom_range: [17, null] }), { lod_zoom: 17, zoom_range: [17, null] });
  assert.equal(lodOf({ zoom_range: [17, 16] }), null);
  assert.equal(lodOf({ zoom_range: [17] }), null);
  assert.equal(lodOf({ zoom_range: ["17", null] }), null);
});
