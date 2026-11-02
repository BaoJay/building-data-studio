// node --test "tests/js/*.test.mjs"   (also run by tests/test_preview_js.py)
import { test } from "node:test";
import assert from "node:assert/strict";
import * as M from "../../app/static/indoor-model.js";

// Two 10 × 10 m rooms side by side on one level, a door on the shared wall.
function twoRooms() {
  const p = M.emptyProject([105.8, 21]);
  const f = M.frame(p.origin);
  const rect = (x0, y0, x1, y1) => [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map(f.ll);
  p.venue.footprint = rect(0, 0, 20, 10);
  p.cells = [
    { id: "C1", level: "L0", type: "entrance", name: "Sảnh", ring: rect(0, 0, 10, 10) },
    { id: "C2", level: "L0", type: "shop", name: "Shop", ring: rect(10, 0, 20, 10) },
  ];
  p.doors = [{ id: "D1", level: "L0", at: f.ll([10, 5]), kind: "door", width: 2 }];
  return { p, f, rect };
}

test("a door on a shared wall makes one edge between the two nodes", () => {
  const { p } = twoRooms();
  const g = M.buildGraph(p);
  assert.equal(g.nodes.length, 2);
  assert.equal(g.edges.length, 1);
  assert.deepEqual([g.edges[0].from, g.edges[0].to], ["N-C1", "N-C2"]);
  assert.ok(Math.abs(g.edges[0].weight - 10) < 0.01);
  // The CellBoundary lies on the wall, as wide as the door.
  const b = g.boundaries[0];
  assert.deepEqual(b.cells, ["C1", "C2"]);
  assert.ok(Math.abs(M.dist(...b.seg) - 2) < 0.01);
  assert.deepEqual(M.validate(p, g), []);
});

test("checks: overlap, outside the footprint, unreachable space, stray door", () => {
  const { p, f, rect } = twoRooms();
  p.cells.push({ id: "C3", level: "L0", type: "shop", name: "Chồng", ring: rect(15, 5, 25, 8) });
  p.doors.push({ id: "D2", level: "L0", at: f.ll([5, 30]), kind: "door", width: 1 });
  const msgs = M.validate(p).map((i) => `${i.level}:${i.ref.id}:${i.msg}`);
  assert.ok(msgs.some((m) => m.startsWith("error:C3") && m.includes("ngoài outline")));
  assert.ok(msgs.some((m) => m.includes("chồng lên")));
  assert.ok(msgs.some((m) => m.startsWith("error:C3") && m.includes("không có đường đi")));
  assert.ok(msgs.some((m) => m.startsWith("error:D2")));
});

test("shared walls rounded to 1e-7° on a turned layout are not overlaps", () => {
  const p = M.demoProject({ angle: 0.37 });
  assert.deepEqual(M.validate(p).filter((i) => i.level === "error"), []);
});

test("vertical connectors join consecutive levels; routing can avoid stairs", () => {
  const p = M.demoProject();
  const g = M.buildGraph(p);
  const vertical = g.edges.filter((e) => e.kind !== "door");
  // 4 connector groups across 4 levels (3 hops each) + the escalator across 3 levels (2 hops).
  assert.equal(vertical.length, 4 * 3 + 2);
  const parking = p.cells.find((c) => c.type === "parking");
  const food = p.cells.find((c) => c.level === "L4" && c.type === "food");
  const r = M.route(p, g, parking.id, food.id);
  assert.ok(r);
  assert.equal(r.legs[0].level, "L1");
  assert.equal(r.legs.at(-1).level, "L4");
  const step = M.route(p, g, parking.id, food.id, { avoid: new Set(["stairs", "escalator"]) });
  assert.ok(step.steps.some((s) => s.includes("Thang máy")));
  assert.ok(!step.steps.some((s) => s.includes("Thang bộ")));
});

test("no route without a door", () => {
  const { p } = twoRooms();
  p.doors = [];
  const g = M.buildGraph(p);
  assert.equal(M.route(p, g, "C1", "C2"), null);
});

test("exports keep IndoorGML dualities and lng/lat order", () => {
  const { p } = twoRooms();
  const g = M.buildGraph(p);
  const doc = M.toIndoorGMLJSON(p, g);
  const tl = doc.thematicLayer[0];
  assert.equal(tl.theme, "Physical");
  const cells = tl.primalSpace.cellSpaceMember;
  assert.deepEqual(cells.map((c) => c.featureType), ["AnchorSpace", "GeneralSpace"]);
  assert.equal(cells[0].duality, "N-C1");
  assert.deepEqual(cells[0].boundedBy, ["B-D1"]);
  assert.equal(tl.primalSpace.cellBoundaryMember[0].duality, tl.dualSpace.edgeMember[0].id);
  assert.equal(tl.dualSpace.edgeMember[0].duality, "B-D1");
  const xml = M.toIndoorGMLXml(p, g);
  assert.match(xml, /<navi:AnchorSpace gml:id="C1">/);
  assert.match(xml, /<core:duality xlink:href="#N-C1"\/>/);
  // GML posList in EPSG:4326 is lat lng.
  assert.match(xml, /<gml:posList>21 105\.8 /);
  const gj = M.toGeoJSON(p, g);
  const types = gj.features.map((x) => x.properties.feature_type);
  for (const t of ["venue", "level", "unit", "opening", "node", "edge"]) assert.ok(types.includes(t), t);
  assert.deepEqual(gj.features.find((x) => x.id === "C1").geometry.coordinates[0][0], [105.8, 21]);
});

test("normalize accepts saved projects and drops closing vertices", () => {
  const { p } = twoRooms();
  const saved = JSON.parse(JSON.stringify(p));
  saved.cells[0].ring.push(saved.cells[0].ring[0]);
  const back = M.normalize(saved);
  assert.equal(back.cells[0].ring.length, 4);
  assert.throws(() => M.normalize({ type: "FeatureCollection" }));
  assert.equal(M.nextId(back, "C"), "C3");
});
