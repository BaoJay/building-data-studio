// node --test "tests/js/*.test.mjs"   (also run by tests/test_preview_js.py)
import { test } from "node:test";
import assert from "node:assert/strict";
import { ModelStore, extrusionFilter, intersects, matchBuildings, padBbox, renderable } from "../../app/static/landmark-store.js";

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

test("landmarks of another layer or id column are not drawn", () => {
  const items = [{ id: "a", layer: "buildings", id_field: "building_id" }, { id: "b", layer: "other", id_field: "building_id" },
    { id: "c", layer: "buildings", id_field: "osm_id" }, { id: "d" }];
  assert.deepEqual(renderable(items, { layer: "buildings", idCol: "building_id" }).map((l) => l.id), ["a", "d"]);
});
