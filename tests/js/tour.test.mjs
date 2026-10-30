// node --test "tests/js/*.test.mjs"   (also run by tests/test_preview_js.py)
import { test } from "node:test";
import assert from "node:assert/strict";
import { distanceM, stopCamera, tallest } from "../../app/static/tour.js";

// A square footprint 0.0002° wide at (lon, lat), as a tile feature.
function bldg(props, lon, lat = 21) {
  const ring = [[lon, lat], [lon + 0.0002, lat], [lon + 0.0002, lat + 0.0002], [lon, lat + 0.0002], [lon, lat]];
  return { properties: props, geometry: { type: "Polygon", coordinates: [ring] } };
}

const OPTS = { heightAttr: "h_m", idCol: "building_id", parentAttr: "parent_building_id" };

test("tallest first, at most n", () => {
  const feats = [10, 80, 30, 55, 120, 5].map((h, i) => bldg({ building_id: `b${i}`, h_m: h }, 105.8 + i * 0.01));
  const out = tallest(feats, { ...OPTS, n: 3 });
  assert.deepEqual(out.map((s) => [s.id, s.height]), [["b4", 120], ["b1", 80], ["b3", 55]]);
  assert.deepEqual(out[0].center.map((v) => +v.toFixed(5)), [105.8401, 21.0001]);
});

test("pieces cut by tile edges and the parts of a building are one stop", () => {
  const feats = [
    bldg({ building_id: "way/1", h_m: 60, name: "Tháp A" }, 105.8),
    bldg({ building_id: "way/1", h_m: 60 }, 105.8002),            // same building, next tile
    bldg({ building_id: "p1", parent_building_id: "way/1", h_m: 95, is_part: 1 }, 105.8001),
    bldg({ building_id: "way/2", h_m: 70 }, 105.85),
  ];
  const out = tallest(feats, OPTS);
  assert.equal(out.length, 2);
  assert.equal(out[0].id, "way/1");
  assert.equal(out[0].height, 95);
  assert.equal(out[0].name, "Tháp A");
  assert.deepEqual(out[0].bbox.map((v) => +v.toFixed(4)), [105.8, 21, 105.8004, 21.0002]);
});

test("outliers, missing heights and buildings with no id are left out", () => {
  const feats = [
    bldg({ building_id: "a", h_m: 900, h_outlier: 1 }, 105.8),
    bldg({ building_id: "b", h_m: 0 }, 105.81),
    bldg({ building_id: "c", h_m: "abc" }, 105.82),
    bldg({ h_m: 50 }, 105.83),
    { id: 7, ...bldg({ h_m: 40 }, 105.84) },                       // tile feature id instead
    bldg({ building_id: "d", h_m: "33.5" }, 105.85),
  ];
  assert.deepEqual(tallest(feats, OPTS).map((s) => [s.id, s.height]), [["#7", 40], ["d", 33.5]]);
});

test("a building next to a taller one already picked is skipped", () => {
  const feats = [
    bldg({ building_id: "t1", h_m: 300 }, 105.8),
    bldg({ building_id: "t2", h_m: 290 }, 105.8005),             // ~50 m away: twin tower
    bldg({ building_id: "far", h_m: 100 }, 105.81),
  ];
  assert.deepEqual(tallest(feats, OPTS).map((s) => s.id), ["t1", "far"]);
  assert.deepEqual(tallest(feats, { ...OPTS, minGapM: 0 }).map((s) => s.id), ["t1", "t2", "far"]);
});

test("within keeps the buildings whose centre is in the box", () => {
  const feats = [bldg({ building_id: "in", h_m: 20 }, 105.8), bldg({ building_id: "out", h_m: 99 }, 106)];
  assert.deepEqual(tallest(feats, { ...OPTS, within: [105.7, 20.9, 105.9, 21.1] }).map((s) => s.id), ["in"]);
});

test("the camera backs off for a tower, within zoom 15–18", () => {
  const z = (height) => stopCamera({ center: [105.8, 21], height }).zoom;
  assert.equal(z(100), 16.6);
  assert.ok(z(200) < z(100));
  assert.equal(z(20), 18);
  assert.equal(z(336), 15);
  const cam = stopCamera({ center: [105.8, 21], height: 50 }, 30);
  assert.equal(cam.bearing, 30);
  assert.deepEqual(cam.center, [105.8, 21]);
});

test("distanceM", () => {
  assert.ok(Math.abs(distanceM([105.8, 21], [105.8, 21.001]) - 111.32) < 0.01);
  assert.ok(Math.abs(distanceM([105.8, 21], [105.801, 21]) - 111.32 * Math.cos((21 * Math.PI) / 180)) < 0.01);
});
