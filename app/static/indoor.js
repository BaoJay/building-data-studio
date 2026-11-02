// /indoor: indoor map editor (PoC). Draw the spaces, doors and POIs of each level of a venue on
// the map, see the IndoorGML dual graph they make, route through it, check and export.
// The model and everything derived from it live in indoor-model.js; this file is map + UI.

import { $, $$, esc, fmtNum, toast } from "/static/util.js";
import { basemapStyle, bindBasemapControls, initBasemap, loadVmapPresets, mapTheme, palette } from "/static/basemap.js";
import * as M from "/static/indoor-model.js";

const STORE = "bds.indoor.project";
const PLAN_STORE = "bds.indoor.plan.";
const SNAP_PX = 10;
const EDGE_PX = 8;
const DOOR_PX = 14;
const HANDLE_LAYERS = ["ind-handle"];
const PICK_LAYERS = ["ind-poi", "ind-door-pt", "ind-door", "ind-cell"];
const PICK_3D = ["ind-3d-on", "ind-3d-off"];
const VGROUP_PREFIX = { elevator: "TM", escalator: "TC", stairs: "TB" };
const VENUE_CATEGORIES = { mall: "Trung tâm thương mại", supermarket: "Siêu thị", office: "Văn phòng", hospital: "Bệnh viện", transit: "Nhà ga / sân bay", other: "Khác" };
const DEMO_MAX = [160, 64];  // metres: largest block the sample layout fills
const DOOR_KINDS ={ door: "Cửa", opening: "Lối mở (không cánh)", emergency: "Cửa thoát hiểm" };

let P = loadProject();
let G = M.buildGraph(P);
let issues = M.validate(P, G);
const hist = { undo: [], redo: [] };
const ui = {
  level: M.levelsSorted(P).find((l) => l.ordinal === 0)?.id || P.levels[0].id,
  tool: "select",
  sel: null,            // { kind: "cell" | "door" | "poi" | "venue" | "level", id }
  draft: [],            // lng/lat points of the polygon being drawn
  cursor: null,         // { at, kind } snapped cursor
  drag: null,           // { what, … } while dragging a handle / a feature / the plan image
  tab: "props",
  route: null,
  routeFrom: "",
  routeTo: "",
  avoid: false,
  plans: {},            // levelId -> { url, aspect, c: [x, y] m, w, rot, opacity }
};
let map;
let base = null;        // { layer, idField } of the base Hà Nội buildings, when built
let font = ["Noto Sans Regular"];
let hoverBldg = null;

// ---------- project state ----------

function loadProject() {
  try {
    const raw = localStorage.getItem(STORE);
    if (raw) return M.normalize(JSON.parse(raw));
  } catch { /* storage blocked or old data: start fresh */ }
  return M.emptyProject();
}

function save() {
  try { localStorage.setItem(STORE, JSON.stringify(P)); } catch { /* private mode */ }
}

/** Change the project: fn mutates P; one undo step if anything changed. */
function edit(fn) {
  const before = JSON.stringify(P);
  fn();
  if (JSON.stringify(P) === before) return refresh();
  hist.undo.push(before);
  if (hist.undo.length > 100) hist.undo.shift();
  hist.redo = [];
  refresh();
}

function undo(redo = false) {
  const [from, to] = redo ? [hist.redo, hist.undo] : [hist.undo, hist.redo];
  if (!from.length) return;
  to.push(JSON.stringify(P));
  P = M.normalize(JSON.parse(from.pop()));
  if (!M.levelById(P, ui.level)) ui.level = P.levels[0].id;
  if (ui.sel && !findSel()) ui.sel = null;
  refresh();
}

function replaceProject(next) {
  hist.undo.push(JSON.stringify(P));
  hist.redo = [];
  P = next;
  ui.level = M.levelsSorted(P).find((l) => l.ordinal === 0)?.id || M.levelsSorted(P)[0].id;
  ui.sel = null;
  ui.route = null;
  ui.routeFrom = ui.routeTo = "";
  refresh();
  fitVenue();
}

function refresh() {
  G = M.buildGraph(P);
  issues = M.validate(P, G);
  if (ui.routeFrom && ui.routeTo) runRoute(false);
  save();
  draw();
  renderPanel();
  renderDrawer();
}

const findCell = (id) => P.cells.find((c) => c.id === id);
function findSel() {
  const s = ui.sel;
  if (!s) return null;
  if (s.kind === "cell") return findCell(s.id);
  if (s.kind === "door") return P.doors.find((d) => d.id === s.id);
  if (s.kind === "poi") return P.pois.find((d) => d.id === s.id);
  if (s.kind === "level") return M.levelById(P, s.id);
  return P.venue;
}
const level = () => M.levelById(P, ui.level);
const xy = (ll) => G.frame.xy(ll);
const ll = (p) => G.frame.ll(p);

// ---------- colours ----------

function colors() {
  const pal = palette(mapTheme());
  const token = (t) => {
    const m = t.match(/^cat-(\d)$/);
    if (m) return pal.cat[Number(m[1]) - 1];
    if (t === "height-1") return pal.height[0][1];
    if (t === "map-ground") return pal.ground;
    return pal.neutral;
  };
  return { ...pal, type: Object.fromEntries(Object.entries(M.SPACE_TYPES).map(([k, v]) => [k, token(v.color)])) };
}

// ---------- map ----------

const EMPTY = { type: "FeatureCollection", features: [] };
const SOURCES = ["ind-foot", "ind-cells", "ind-doors", "ind-pois", "ind-graph", "ind-route", "ind-draft", "ind-handles", "ind-3d"];
const feat = (geometry, properties = {}) => ({ type: "Feature", geometry, properties });
const poly = (ring) => ({ type: "Polygon", coordinates: [[...ring, ring[0]]] });
const line = (coords) => ({ type: "LineString", coordinates: coords });
const pt = (c) => ({ type: "Point", coordinates: c });

function installLayers() {
  const c = colors();
  const style = map.getStyle();
  // Label font: the first fontstack the basemap already uses, so its glyphs are there.
  const fs = style.layers.map((l) => l.layout?.["text-font"]).find((f) => Array.isArray(f) && typeof f[0] === "string");
  if (fs) font = fs;
  const labels = Boolean(style.glyphs);
  for (const id of SOURCES) map.addSource(id, { type: "geojson", data: EMPTY });
  if (map.getSource("bldg") && base) {
    map.addLayer({ id: "bldg-fill", type: "fill", source: "bldg", "source-layer": base.layer, minzoom: 14,
      paint: { "fill-color": c.neutral, "fill-opacity": 0.35, "fill-outline-color": c.outline } });
    map.addLayer({ id: "bldg-hover", type: "line", source: "bldg", "source-layer": base.layer, filter: ["==", ["get", base.idField], ""],
      paint: { "line-color": c.highlight, "line-width": 3 } });
  }
  map.addLayer({ id: "ind-foot-fill", type: "fill", source: "ind-foot", paint: { "fill-color": c.ground, "fill-opacity": 0.85 } });
  if (ui.plans[ui.level]) addPlanLayer();
  map.addLayer({ id: "ind-ghost", type: "line", source: "ind-cells", filter: ["==", ["get", "ghost"], 1],
    paint: { "line-color": c.outline, "line-opacity": 0.35, "line-width": 1, "line-dasharray": [2, 2] } });
  map.addLayer({ id: "ind-cell", type: "fill", source: "ind-cells", filter: ["==", ["get", "ghost"], 0],
    paint: { "fill-color": ["get", "color"], "fill-opacity": ["case", ["==", ["get", "type"], "corridor"], 0.75, 0.6] } });
  map.addLayer({ id: "ind-cell-line", type: "line", source: "ind-cells", filter: ["==", ["get", "ghost"], 0],
    paint: { "line-color": c.outline, "line-width": 1, "line-opacity": 0.7 } });
  map.addLayer({ id: "ind-foot-line", type: "line", source: "ind-foot",
    paint: { "line-color": ["case", ["==", ["get", "sel"], 1], c.highlight, c.outline], "line-width": ["case", ["==", ["get", "sel"], 1], 3, 2] } });
  map.addLayer({ id: "ind-sel", type: "line", source: "ind-cells", filter: ["==", ["get", "sel"], 1],
    paint: { "line-color": c.highlight, "line-width": 3 } });
  map.addLayer({ id: "ind-edge", type: "line", source: "ind-graph", filter: ["==", ["geometry-type"], "LineString"],
    paint: { "line-color": c.outline, "line-width": 1.2, "line-opacity": 0.6, "line-dasharray": [3, 2] } });
  map.addLayer({ id: "ind-node", type: "circle", source: "ind-graph", filter: ["==", ["geometry-type"], "Point"],
    paint: { "circle-radius": ["case", ["==", ["get", "vertical"], 1], 5, 3], "circle-color": ["case", ["==", ["get", "vertical"], 1], c.cat[4], c.ground],
      "circle-stroke-color": c.outline, "circle-stroke-width": 1.2 } });
  map.addLayer({ id: "ind-door", type: "line", source: "ind-doors", filter: ["==", ["geometry-type"], "LineString"],
    layout: { "line-cap": "butt" },
    paint: { "line-color": ["case", ["==", ["get", "sel"], 1], c.highlight, ["==", ["get", "bad"], 1], c.outlier, ["==", ["get", "exterior"], 1], c.lime, c.accent],
      "line-width": 5 } });
  map.addLayer({ id: "ind-door-pt", type: "circle", source: "ind-doors", filter: ["==", ["geometry-type"], "Point"],
    paint: { "circle-radius": 3, "circle-color": ["case", ["==", ["get", "bad"], 1], c.outlier, c.accent], "circle-stroke-color": c.ground, "circle-stroke-width": 1 } });
  map.addLayer({ id: "ind-route-other", type: "line", source: "ind-route", filter: ["==", ["get", "on"], 0],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": c.outline, "line-width": 2, "line-opacity": 0.35, "line-dasharray": [1, 2] } });
  // The route is ink-on-paper with a casing: no space type uses that colour.
  map.addLayer({ id: "ind-route-casing", type: "line", source: "ind-route", filter: ["all", ["==", ["get", "on"], 1], ["==", ["geometry-type"], "LineString"]],
    layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": c.ground, "line-width": 9 } });
  map.addLayer({ id: "ind-route", type: "line", source: "ind-route", filter: ["all", ["==", ["get", "on"], 1], ["==", ["geometry-type"], "LineString"]],
    layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": c.outline, "line-width": 5 } });
  map.addLayer({ id: "ind-route-pt", type: "circle", source: "ind-route", filter: ["all", ["==", ["get", "on"], 1], ["==", ["geometry-type"], "Point"]],
    paint: { "circle-radius": 7, "circle-color": ["match", ["get", "role"], "start", c.accent, "end", c.highlight, c.lime],
      "circle-stroke-color": c.ground, "circle-stroke-width": 2 } });
  map.addLayer({ id: "ind-poi", type: "circle", source: "ind-pois",
    paint: { "circle-radius": 6, "circle-color": c.cat[1], "circle-stroke-color": ["case", ["==", ["get", "sel"], 1], c.highlight, c.outline],
      "circle-stroke-width": ["case", ["==", ["get", "sel"], 1], 3, 1.2] } });
  if (labels) {
    map.addLayer({ id: "ind-label", type: "symbol", source: "ind-cells", filter: ["all", ["==", ["get", "ghost"], 0], ["!=", ["get", "label"], ""]],
      layout: { "text-field": ["get", "label"], "text-font": font, "text-size": 11, "text-max-width": 7, "text-padding": 1 },
      paint: { "text-color": c.outline, "text-halo-color": c.ground, "text-halo-width": 1.4 } });
    map.addLayer({ id: "ind-poi-label", type: "symbol", source: "ind-pois",
      layout: { "text-field": ["get", "label"], "text-font": font, "text-size": 10.5, "text-offset": [0, 1.1], "text-anchor": "top", "text-optional": true },
      paint: { "text-color": c.cat[1], "text-halo-color": c.ground, "text-halo-width": 1.4 } });
    map.addLayer({ id: "ind-route-label", type: "symbol", source: "ind-route", filter: ["all", ["==", ["get", "on"], 1], ["has", "label"]],
      layout: { "text-field": ["get", "label"], "text-font": font, "text-size": 12, "text-offset": [0, -1.3], "text-anchor": "bottom" },
      paint: { "text-color": c.outline, "text-halo-color": c.ground, "text-halo-width": 2 } });
  }
  map.addLayer({ id: "ind-3d-off", type: "fill-extrusion", source: "ind-3d", filter: ["==", ["get", "on"], 0],
    paint: { "fill-extrusion-color": ["get", "color"], "fill-extrusion-base": ["get", "base"], "fill-extrusion-height": ["get", "top"], "fill-extrusion-opacity": 0.45 } });
  map.addLayer({ id: "ind-3d-on", type: "fill-extrusion", source: "ind-3d", filter: ["==", ["get", "on"], 1],
    paint: { "fill-extrusion-color": ["get", "color"], "fill-extrusion-base": ["get", "base"], "fill-extrusion-height": ["get", "top"], "fill-extrusion-opacity": 0.92 } });
  map.addLayer({ id: "ind-draft-fill", type: "fill", source: "ind-draft", filter: ["==", ["geometry-type"], "Polygon"],
    paint: { "fill-color": c.highlight, "fill-opacity": 0.15 } });
  map.addLayer({ id: "ind-draft-line", type: "line", source: "ind-draft", filter: ["!=", ["geometry-type"], "Point"],
    paint: { "line-color": c.highlight, "line-width": 2, "line-dasharray": [2, 1] } });
  map.addLayer({ id: "ind-draft-pt", type: "circle", source: "ind-draft", filter: ["==", ["geometry-type"], "Point"],
    paint: { "circle-radius": ["match", ["get", "kind"], "vertex", 6, "edge", 5, "door", 6, 4],
      "circle-color": ["match", ["get", "kind"], "vertex", c.highlight, "edge", c.lime, "door", c.accent, c.ground],
      "circle-stroke-color": c.outline, "circle-stroke-width": 1.5 } });
  map.addLayer({ id: "ind-handle", type: "circle", source: "ind-handles",
    paint: { "circle-radius": ["case", ["==", ["get", "mid"], 1], 3.5, 5.5], "circle-color": ["case", ["==", ["get", "mid"], 1], c.ground, c.highlight],
      "circle-stroke-color": c.outline, "circle-stroke-width": 1.5 } });
  draw();
}

/** Push the project into the map sources. */
function draw() {
  if (!map?.getSource("ind-cells")) return;
  const c = colors();
  const lv = level();
  const show = { graph: $("#showGraph").checked, labels: $("#showLabels").checked, ghost: $("#showGhost").checked };
  const threeD = $("#threeD").checked;
  const sel = ui.sel || {};
  const below = M.levelsSorted(P).filter((l) => l.ordinal < lv.ordinal).at(-1);
  const fp = P.venue.footprint;
  map.getSource("ind-foot").setData({ type: "FeatureCollection",
    features: fp.length >= 3 && !threeD ? [feat(poly(fp), { sel: sel.kind === "venue" ? 1 : 0 })] : fp.length >= 3 ? [feat(line([...fp, fp[0]]), { sel: 0 })] : [] });
  const cells = [];
  if (!threeD) {
    for (const cl of P.cells) {
      if (cl.ring.length < 3) continue;
      const ghost = cl.level !== lv.id;
      if (ghost && !(show.ghost && below && cl.level === below.id)) continue;
      const label = show.labels && !ghost && cl.type !== "corridor" ? (cl.name || "").replace(/^[\w-]+ · /, "") : "";
      cells.push(feat(poly(cl.ring), { id: cl.id, type: cl.type, color: c.type[cl.type], label, ghost: ghost ? 1 : 0,
        sel: sel.kind === "cell" && sel.id === cl.id ? 1 : 0 }));
    }
  }
  map.getSource("ind-cells").setData({ type: "FeatureCollection", features: cells });
  const doors = [];
  if (!threeD) {
    for (const b of G.boundaries.filter((x) => x.level === lv.id)) {
      const props = { id: b.door, exterior: b.exterior ? 1 : 0, bad: b.cells.length ? 0 : 1, sel: sel.kind === "door" && sel.id === b.door ? 1 : 0 };
      doors.push(feat(line(b.seg.map(ll)), props), feat(pt(ll(b.xy)), props));
    }
  }
  map.getSource("ind-doors").setData({ type: "FeatureCollection", features: doors });
  map.getSource("ind-pois").setData({ type: "FeatureCollection", features: threeD ? [] : P.pois.filter((o) => o.level === lv.id)
    .map((o) => feat(pt(o.at), { id: o.id, label: show.labels ? o.name || M.POI_TYPES[o.type] : "", sel: sel.kind === "poi" && sel.id === o.id ? 1 : 0 })) });
  const graph = [];
  if (show.graph && !threeD) {
    for (const n of G.nodes.filter((x) => x.level === lv.id)) graph.push(feat(pt(ll(n.xy)), { vertical: M.isVertical(findCell(n.cell)) ? 1 : 0 }));
    for (const e of G.edges.filter((x) => x.kind === "door" && x.level === lv.id)) {
      graph.push(feat(line([G.nodeById.get(e.from).xy, e.via, G.nodeById.get(e.to).xy].map(ll))));
    }
  }
  map.getSource("ind-graph").setData({ type: "FeatureCollection", features: graph });
  drawRoute();
  drawDraft();
  drawHandles();
  draw3D();
  renderLevelBar();
}

function drawRoute() {
  const r = ui.route;
  const out = [];
  if (r && !$("#threeD").checked) {
    r.legs.forEach((leg, i) => {
      const on = leg.level === ui.level ? 1 : 0;
      out.push(feat(line(leg.xy.map(ll)), { on }));
      if (i === 0) out.push(feat(pt(ll(leg.xy[0])), { on, role: "start", label: "Đi" }));
      if (i === r.legs.length - 1) out.push(feat(pt(ll(leg.xy.at(-1))), { on, role: "end", label: "Đến" }));
      if (i < r.legs.length - 1) {
        const next = M.levelById(P, r.legs[i + 1].level);
        out.push(feat(pt(ll(leg.xy.at(-1))), { on, role: "ride", label: `${r.legs[i + 1].ride === "elevator" ? "Thang máy" : r.legs[i + 1].ride === "escalator" ? "Thang cuốn" : "Thang bộ"} → ${next.short}` }));
      }
      if (i > 0) out.push(feat(pt(ll(leg.xy[0])), { on, role: "ride", label: `từ tầng ${M.levelById(P, r.legs[i - 1].level).short}` }));
    });
  }
  map.getSource("ind-route").setData({ type: "FeatureCollection", features: out });
}

function drawDraft() {
  const out = [];
  const cur = ui.cursor;
  if (ui.tool === "polygon" || ui.tool === "footprint") {
    const pts = [...ui.draft, ...(cur ? [cur.at] : [])];
    if (pts.length >= 3) out.push(feat(poly(pts)));
    else if (pts.length === 2) out.push(feat(line(pts)));
    for (const p of ui.draft) out.push(feat(pt(p), { kind: "draft" }));
  } else if (ui.tool === "rect" && ui.draft.length === 1 && cur) {
    out.push(feat(poly(rectRing(ui.draft[0], cur.at))));
  }
  if (cur && ["polygon", "footprint", "rect"].includes(ui.tool)) out.push(feat(pt(cur.at), { kind: cur.kind }));
  if (cur && ui.tool === "door" && cur.kind === "door") out.push(feat(pt(cur.at), { kind: "door" }));
  map.getSource("ind-draft").setData({ type: "FeatureCollection", features: out });
}

function selRing() {
  const s = ui.sel;
  if (s?.kind === "cell") return findCell(s.id)?.ring;
  if (s?.kind === "venue") return P.venue.footprint;
  return null;
}

function drawHandles() {
  const ring = ui.tool === "select" && !$("#threeD").checked ? selRing() : null;
  const out = [];
  if (ring && ring.length >= 3) {
    ring.forEach((p, i) => {
      out.push(feat(pt(p), { i, mid: 0 }));
      const q = ring[(i + 1) % ring.length];
      out.push(feat(pt([(p[0] + q[0]) / 2, (p[1] + q[1]) / 2]), { i, mid: 1 }));
    });
  }
  map.getSource("ind-handles").setData({ type: "FeatureCollection", features: out });
}

// 3D: every level stacked at its elevation (stretched by the "giãn tầng" factor); rooms are
// low blocks, corridors thin slabs, the route a ribbon on its levels, rides a column.
function draw3D() {
  const on3d = $("#threeD").checked;
  const out = [];
  if (on3d) {
    const c = colors();
    const k = Number($("#explode").value);
    const minE = Math.min(...P.levels.map((l) => l.elevation));
    const z = (id) => (M.levelById(P, id).elevation - minE) * k;
    for (const cl of P.cells) {
      if (cl.ring.length < 3) continue;
      const b = z(cl.level);
      const t = cl.type === "corridor" || cl.type === "entrance" || cl.type === "parking" ? 0.3 : cl.type === "void" ? 0.05 : M.isVertical(cl) ? 3 : 1.6;
      out.push(feat(poly(cl.ring), { id: cl.id, color: c.type[cl.type], base: b, top: b + t, on: cl.level === ui.level ? 1 : 0 }));
    }
    const r = ui.route;
    if (r) {
      for (const leg of r.legs) {
        const b = z(leg.level) + 1.8;
        for (let i = 1; i < leg.xy.length; i++) out.push(feat(poly(ribbon(leg.xy[i - 1], leg.xy[i], 1.4).map(ll)), { color: c.outline, base: b, top: b + 0.8, on: 1 }));
      }
      for (let i = 1; i < r.legs.length; i++) {
        const [a, b] = [r.legs[i - 1], r.legs[i]];
        const [za, zb] = [z(a.level), z(b.level)].sort((u, v) => u - v);
        out.push(feat(poly(square(b.xy[0], 1.2).map(ll)), { color: c.outline, base: za, top: zb + 2.6, on: 1 }));
      }
    }
  }
  map.getSource("ind-3d").setData({ type: "FeatureCollection", features: out });
}

function ribbon(a, b, w) {
  const l = M.dist(a, b) || 1;
  const n = [(-(b[1] - a[1]) / l) * (w / 2), ((b[0] - a[0]) / l) * (w / 2)];
  return [[a[0] + n[0], a[1] + n[1]], [b[0] + n[0], b[1] + n[1]], [b[0] - n[0], b[1] - n[1]], [a[0] - n[0], a[1] - n[1]]];
}
const square = (c, h) => [[c[0] - h, c[1] - h], [c[0] + h, c[1] - h], [c[0] + h, c[1] + h], [c[0] - h, c[1] + h]];

// ---------- snapping & drawing ----------

/** Axis of the building (radians): rectangles and Shift-locked lines follow it. */
function axis() {
  return P.venue.footprint.length >= 3 ? M.dominantAngle(P.venue.footprint.map(xy)) : 0;
}

function rectRing(a, b) {
  const t = axis();
  const [cos, sin] = [Math.cos(t), Math.sin(t)];
  const A = xy(a);
  const B = xy(b);
  const d = [B[0] - A[0], B[1] - A[1]];
  const u = d[0] * cos + d[1] * sin;   // along the axis
  const v = -d[0] * sin + d[1] * cos;  // across
  const P1 = [A[0] + u * cos, A[1] + u * sin];
  const P3 = [A[0] - v * sin, A[1] + v * cos];
  return [A, P1, B, P3].map(ll);
}

/** Rings on the current level the cursor snaps to (plus the footprint), except `skip`. */
function snapRings(skip) {
  const rings = P.cells.filter((c) => c.level === ui.level && c.id !== skip && c.ring.length >= 2).map((c) => c.ring);
  if (P.venue.footprint.length >= 2 && skip !== "venue") rings.push(P.venue.footprint);
  return rings;
}

/**
 * The point the cursor stands for: an existing vertex, else a wall, else (Shift) the direction
 * from the previous point locked to the building axis every 45°. Alt turns snapping off.
 */
function snap(e, { skip, prev, extra = [] } = {}) {
  const raw = [e.lngLat.lng, e.lngLat.lat];
  const oe = e.originalEvent;
  if (oe?.altKey) return { at: raw, kind: "free" };
  if (oe?.shiftKey && prev) {
    const t = axis();
    const a = xy(prev);
    const b = xy(raw);
    const ang = Math.atan2(b[1] - a[1], b[0] - a[0]) - t;
    const q = Math.round(ang / (Math.PI / 4)) * (Math.PI / 4) + t;
    const l = M.dist(a, b) * Math.cos(ang - (q - t));
    return { at: ll([a[0] + l * Math.cos(q), a[1] + l * Math.sin(q)]), kind: "axis" };
  }
  const p = e.point;
  const rings = snapRings(skip);
  let best = null;
  let bd = SNAP_PX;
  for (const v of [...rings.flat(), ...extra]) {
    const s = map.project(v);
    const d = Math.hypot(s.x - p.x, s.y - p.y);
    if (d < bd) { bd = d; best = v; }
  }
  if (best) return { at: best, kind: "vertex" };
  let bestE = null;
  bd = EDGE_PX;
  for (const ring of rings) {
    for (let i = 0; i < ring.length; i++) {
      const a = map.project(ring[i]);
      const b = map.project(ring[(i + 1) % ring.length]);
      const r = M.projectOnSegment([p.x, p.y], [a.x, a.y], [b.x, b.y]);
      if (r.d < bd) { bd = r.d; bestE = r.q; }
    }
  }
  if (bestE) {
    const q = map.unproject(bestE);
    return { at: [q.lng, q.lat], kind: "edge" };
  }
  return { at: raw, kind: "free" };
}

/** Where a door would go: the nearest wall of a navigable space of this level, in pixels. */
function doorSpot(e) {
  const p = e.point;
  let best = null;
  let bd = DOOR_PX;
  for (const c of P.cells) {
    if (c.level !== ui.level || !M.navigable(c)) continue;
    for (let i = 0; i < c.ring.length; i++) {
      const a = map.project(c.ring[i]);
      const b = map.project(c.ring[(i + 1) % c.ring.length]);
      const r = M.projectOnSegment([p.x, p.y], [a.x, a.y], [b.x, b.y]);
      if (r.d < bd) { bd = r.d; best = r.q; }
    }
  }
  if (!best) return null;
  const q = map.unproject(best);
  return { at: [q.lng, q.lat], kind: "door" };
}

function finishPolygon() {
  const ring = ui.draft.slice();
  ui.draft = [];
  ui.cursor = null;
  if (ring.length < 3) { drawDraft(); return; }
  if (ui.tool === "footprint") {
    edit(() => {
      P.venue.footprint = ring;
      if (!P.cells.length) P.origin = ll(M.centroid(ring.map(xy)));
    });
    ui.sel = { kind: "venue", id: P.venue.id };
    setTool("select");
    return;
  }
  addCell(ring);
}

function addCell(ring) {
  const type = $("#typeSel").value;
  const id = M.nextId(P, "C");
  edit(() => {
    const c = { id, level: ui.level, type, name: "", ring };
    if (M.SPACE_TYPES[type].vertical) c.vgroup = `${VGROUP_PREFIX[type]}-${id}`;
    P.cells.push(c);
  });
  ui.sel = { kind: "cell", id };
  ui.tab = "props";
  refresh();
  setTimeout(() => $("#fName")?.focus(), 0);
}

// ---------- tools ----------

const TOOL_NOTES = {
  select: "Click để chọn · kéo đỉnh / điểm giữa cạnh để sửa · Alt+click đỉnh để xoá · kéo trong hình để di chuyển · Del xoá",
  polygon: "Click thêm đỉnh · click đỉnh đầu, Enter hoặc nhấp đúp để đóng · Shift khoá góc theo trục toà · Alt tắt bắt điểm · Backspace bỏ đỉnh · Esc huỷ",
  footprint: "Vẽ outline toà nhà: click thêm đỉnh · Enter / nhấp đúp để đóng · Esc huỷ",
  rect: "Click góc thứ nhất rồi góc đối diện · hình chữ nhật theo trục toà nhà · Esc huỷ",
  door: "Click lên tường của không gian để đặt cửa (cửa nối mọi không gian chung bức tường đó)",
  poi: "Click để đặt POI",
  pick: "Click một building của base để lấy outline · Esc huỷ",
  plan: "Kéo để di chuyển ảnh mặt bằng · Esc xong",
};

function setTool(tool) {
  if (tool === "pick" && !base) { toast("Chưa có base Hà Nội: tạo ở trang chính, hoặc vẽ outline."); return; }
  ui.tool = tool;
  ui.draft = [];
  ui.cursor = null;
  if (tool !== "select") ui.drag = null;
  for (const b of $$("#toolbar [data-tool]")) b.setAttribute("aria-pressed", String(b.dataset.tool === tool));
  $("#pickBtn").setAttribute("aria-pressed", String(tool === "pick"));
  $("#footBtn").setAttribute("aria-pressed", String(tool === "footprint"));
  $("#planMove").setAttribute("aria-pressed", String(tool === "plan"));
  $("#poiSel").hidden = tool !== "poi";
  const note = $("#toolNote");
  note.textContent = TOOL_NOTES[tool] || "";
  note.hidden = !note.textContent || $("#threeD").checked;
  if (map) {
    if (["polygon", "footprint", "rect"].includes(tool)) map.doubleClickZoom.disable(); else map.doubleClickZoom.enable();
    map.getCanvas().style.cursor = tool === "select" ? "" : tool === "plan" ? "move" : "crosshair";
    if (map.getLayer("bldg-hover")) map.setFilter("bldg-hover", ["==", ["get", base.idField], ""]);
    draw();
  }
}

function onMove(e) {
  if (ui.drag) return onDrag(e);
  const t = ui.tool;
  if (t === "polygon" || t === "footprint") {
    ui.cursor = snap(e, { prev: ui.draft.at(-1), skip: t === "footprint" ? "venue" : null, extra: ui.draft.slice(0, 1) });
    drawDraft();
  } else if (t === "rect") {
    ui.cursor = snap(e, { prev: ui.draft[0] });
    drawDraft();
  } else if (t === "door") {
    ui.cursor = doorSpot(e);
    drawDraft();
  } else if (t === "pick" && map.getLayer("bldg-fill")) {
    const f = map.queryRenderedFeatures(e.point, { layers: ["bldg-fill"] })[0];
    const id = f?.properties?.[base.idField] ?? "";
    if (id !== hoverBldg) { hoverBldg = id; map.setFilter("bldg-hover", ["==", ["get", base.idField], id]); }
  } else if (t === "select") {
    const onHandle = map.queryRenderedFeatures(box(e.point, 5), { layers: HANDLE_LAYERS.filter((l) => map.getLayer(l)) }).length;
    const onFeat = map.queryRenderedFeatures(e.point, { layers: pickLayers() }).length;
    map.getCanvas().style.cursor = onHandle ? "grab" : onFeat ? "pointer" : "";
  }
}

const box = (p, r) => [[p.x - r, p.y - r], [p.x + r, p.y + r]];
const pickLayers = () => ($("#threeD").checked ? PICK_3D : PICK_LAYERS).filter((l) => map.getLayer(l));

function onClick(e) {
  const t = ui.tool;
  if (t === "polygon" || t === "footprint") {
    const s = snap(e, { prev: ui.draft.at(-1), skip: t === "footprint" ? "venue" : null, extra: ui.draft.slice(0, 1) });
    const first = ui.draft[0];
    if (ui.draft.length >= 3 && s.at[0] === first[0] && s.at[1] === first[1]) return finishPolygon();
    const last = ui.draft.at(-1);
    if (!last || last[0] !== s.at[0] || last[1] !== s.at[1]) ui.draft.push(s.at);
    drawDraft();
  } else if (t === "rect") {
    const s = snap(e, { prev: ui.draft[0] });
    if (!ui.draft.length) { ui.draft = [s.at]; return drawDraft(); }
    const ring = rectRing(ui.draft[0], s.at);
    ui.draft = [];
    if (M.dist(xy(ring[0]), xy(ring[1])) < 0.3 || M.dist(xy(ring[1]), xy(ring[2])) < 0.3) return drawDraft();
    addCell(ring);
  } else if (t === "door") {
    const s = doorSpot(e);
    if (!s) return toast("Click sát tường của một không gian trên tầng này.");
    const id = M.nextId(P, "D");
    edit(() => P.doors.push({ id, level: ui.level, at: s.at, kind: "door", width: 1.6 }));
    const b = G.boundaries.find((x) => x.door === id);
    if (b && b.cells.length < 2) toast(b.cells.length ? "Cửa chỉ chạm 1 không gian (cửa ra ngoài?)." : "Cửa không chạm không gian nào.");
  } else if (t === "poi") {
    const id = M.nextId(P, "P");
    const type = $("#poiSel").value;
    edit(() => P.pois.push({ id, level: ui.level, type, name: M.POI_TYPES[type], at: [e.lngLat.lng, e.lngLat.lat] }));
    ui.sel = { kind: "poi", id };
    refresh();
  } else if (t === "pick") {
    pickBuilding(e);
  } else if (t === "select") {
    if (ui.justDragged) { ui.justDragged = false; return; }
    selectAt(e.point);
  }
}

function selectAt(point) {
  const f = map.queryRenderedFeatures(box(point, 3), { layers: pickLayers() })[0];
  if (!f) {
    // The footprint outline itself selects the venue.
    const fp = P.venue.footprint;
    if (fp.length >= 3) {
      const p = [point.x, point.y];
      const near = fp.some((v, i) => {
        const a = map.project(v);
        const b = map.project(fp[(i + 1) % fp.length]);
        return M.projectOnSegment(p, [a.x, a.y], [b.x, b.y]).d < 6;
      });
      if (near) { ui.sel = { kind: "venue", id: P.venue.id }; ui.tab = "props"; return refresh(); }
    }
    ui.sel = null;
    return refresh();
  }
  const id = f.properties.id;
  if (!id) return;
  const kind = f.layer.id === "ind-poi" ? "poi" : f.layer.id.startsWith("ind-door") ? "door" : "cell";
  ui.sel = { kind, id };
  if (kind === "cell" && $("#threeD").checked) ui.level = findCell(id).level;
  ui.tab = "props";
  refresh();
}

function onDown(e) {
  if (e.originalEvent.button !== 0) return;
  if (ui.tool === "plan" && ui.plans[ui.level]) {
    e.preventDefault();
    ui.drag = { what: "plan", start: xy([e.lngLat.lng, e.lngLat.lat]), c0: ui.plans[ui.level].c.slice() };
    return;
  }
  if (ui.tool !== "select" || $("#threeD").checked) return;
  const h = map.queryRenderedFeatures(box(e.point, 6), { layers: HANDLE_LAYERS.filter((l) => map.getLayer(l)) })[0];
  const ring = selRing();
  if (h && ring) {
    e.preventDefault();
    const i = h.properties.i;
    if (e.originalEvent.altKey && !h.properties.mid) {
      if (ring.length <= 3) return toast("Đa giác cần ít nhất 3 đỉnh.");
      return edit(() => ring.splice(i, 1));
    }
    ui.drag = { what: "vertex", before: JSON.stringify(P), idx: h.properties.mid ? i + 1 : i, insert: Boolean(h.properties.mid), moved: false };
    return;
  }
  // Drag the selected feature as a whole.
  const f = map.queryRenderedFeatures(box(e.point, 3), { layers: pickLayers() })[0];
  if (f && ui.sel && f.properties.id === ui.sel.id) {
    e.preventDefault();
    ui.drag = { what: "move", before: JSON.stringify(P), start: [e.lngLat.lng, e.lngLat.lat], orig: JSON.stringify(findSel()), moved: false };
  }
}

function onDrag(e) {
  const d = ui.drag;
  const here = [e.lngLat.lng, e.lngLat.lat];
  if (d.what === "plan") {
    const pl = ui.plans[ui.level];
    const p = xy(here);
    pl.c = [d.c0[0] + p[0] - d.start[0], d.c0[1] + p[1] - d.start[1]];
    return updatePlan();
  }
  d.moved = true;
  const s = ui.sel;
  if (d.what === "vertex") {
    const ring = selRing();
    if (d.insert) { ring.splice(d.idx, 0, here); d.insert = false; }
    ring[d.idx] = snap(e, { skip: s.kind === "venue" ? "venue" : s.id, prev: ring[(d.idx - 1 + ring.length) % ring.length] }).at;
  } else if (d.what === "move") {
    const o = JSON.parse(d.orig);
    const [a, b] = [xy(d.start), xy(here)];
    const shift = (q) => { const p = xy(q); return ll([p[0] + b[0] - a[0], p[1] + b[1] - a[1]]); };
    const tgt = findSel();
    if (s.kind === "cell") tgt.ring = o.ring.map(shift);
    else if (s.kind === "door") tgt.at = (doorSpot(e) || { at: here }).at;
    else if (s.kind === "poi") tgt.at = here;
    else if (s.kind === "venue") tgt.footprint = o.footprint.map(shift);
  }
  G = M.buildGraph(P);
  draw();
}

function onUp() {
  const d = ui.drag;
  if (!d) return;
  ui.drag = null;
  if (d.what === "plan") return savePlan(ui.level);
  if (!d.moved) return;
  ui.justDragged = true;
  setTimeout(() => { ui.justDragged = false; }, 0);
  hist.undo.push(d.before);
  hist.redo = [];
  refresh();
}

function onKey(e) {
  if (e.target.closest("input, select, textarea")) return;
  const k = e.key;
  if ((e.ctrlKey || e.metaKey) && k.toLowerCase() === "z") { e.preventDefault(); return undo(e.shiftKey); }
  if ((e.ctrlKey || e.metaKey) && k.toLowerCase() === "y") { e.preventDefault(); return undo(true); }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (k === "Escape") {
    if (ui.draft.length) { ui.draft = []; return drawDraft(); }
    if (ui.tool !== "select") return setTool("select");
    ui.sel = null;
    return refresh();
  }
  if (k === "Enter" && (ui.tool === "polygon" || ui.tool === "footprint")) return finishPolygon();
  if (k === "Backspace" && ui.draft.length) { e.preventDefault(); ui.draft.pop(); return drawDraft(); }
  if ((k === "Delete" || k === "Backspace") && ui.sel) { e.preventDefault(); return deleteSel(); }
  if (k === "PageUp" || k === "PageDown") { e.preventDefault(); return stepLevel(k === "PageUp" ? 1 : -1); }
  if ($("#threeD").checked) return;
  const tool = { v: "select", p: "polygon", r: "rect", d: "door", i: "poi", b: "footprint" }[k.toLowerCase()];
  if (tool) setTool(tool);
}

function stepLevel(dir) {
  const ls = M.levelsSorted(P);
  const i = ls.findIndex((l) => l.id === ui.level);
  const next = ls[i + dir];
  if (next) setLevel(next.id);
}

function setLevel(id) {
  ui.level = id;
  ui.draft = [];
  if (ui.sel && ["cell", "door", "poi"].includes(ui.sel.kind) && findSel()?.level !== id) ui.sel = null;
  if (map) syncPlanLayer();
  refresh();
}

function deleteSel() {
  const s = ui.sel;
  if (!s) return;
  if (s.kind === "level") return deleteLevel(s.id);
  if (s.kind === "venue") {
    if (!confirm("Xoá outline toà nhà?")) return;
    edit(() => { P.venue.footprint = []; });
  } else {
    const list = { cell: "cells", door: "doors", poi: "pois" }[s.kind];
    edit(() => { P[list] = P[list].filter((o) => o.id !== s.id); });
  }
  ui.sel = null;
  refresh();
}

// ---------- building from the base ----------

function pickBuilding(e) {
  const f = map.queryRenderedFeatures(e.point, { layers: ["bldg-fill"] })[0];
  if (!f) return toast("Không có building ở đây (zoom ≥ 14).");
  const id = f.properties[base.idField];
  // A building cut by tile edges comes in pieces: keep the largest outer ring.
  const pieces = map.querySourceFeatures("bldg", { sourceLayer: base.layer, filter: ["==", ["get", base.idField], id] });
  let ring = null;
  let area = 0;
  for (const g of [f, ...pieces].map((x) => x.geometry)) {
    const polys = g.type === "Polygon" ? [g.coordinates] : g.type === "MultiPolygon" ? g.coordinates : [];
    for (const pg of polys) {
      const r = pg[0].slice(0, -1);
      const a = Math.abs(M.ringArea(r.map(M.frame(r[0]).xy)));
      if (a > area) { area = a; ring = r; }
    }
  }
  if (!ring) return;
  const props = f.properties;
  const floors = Number(props.num_floors) || 0;
  edit(() => {
    P.venue.footprint = ring.map(([x, y]) => [Math.round(x * 1e7) / 1e7, Math.round(y * 1e7) / 1e7]);
    P.venue.building_id = String(id);
    if (props.name) P.venue.name = props.name;
    if (!P.cells.length) {
      P.origin = M.frame(ring[0]).ll(M.centroid(ring.map(M.frame(ring[0]).xy)));
      // An empty project takes the floor count of the building.
      if (floors > 1 && P.levels.length === 1) {
        P.levels = Array.from({ length: Math.min(floors, 12) }, (_, i) => ({
          id: `L${i}`, name: `Tầng ${i + 1}`, short: String(i + 1), ordinal: i, elevation: i * 4.5, height: 4.5 }));
        ui.level = "L0";
      }
    }
  });
  ui.sel = { kind: "venue", id: P.venue.id };
  setTool("select");
  toast(`Đã lấy outline ${props.name || id}${floors ? ` · OSM: ${floors} tầng` : ""}${props.height ? ` · cao ${fmtNum(Number(props.height), 1)} m` : ""}.`);
  fitVenue();
}

function makeDemo() {
  if (P.cells.length && !confirm("Thay toàn bộ project bằng layout mẫu?")) return;
  const fp = P.venue.footprint;
  let next = M.demoProject({ center: map ? [map.getCenter().lng, map.getCenter().lat] : P.origin });
  let note = "Đã tạo layout mẫu: 4 tầng, thang máy, thang bộ, thang cuốn, cửa và lối vào.";
  if (fp.length >= 3) {
    const f = M.frame(fp[0]);
    const ring = fp.map(f.xy);
    const ob = M.orientedBox(ring);
    const [w, h, angle] = ob.w >= ob.h ? [ob.w, ob.h, ob.angle] : [ob.h, ob.w, ob.angle + Math.PI / 2];
    const name = P.venue.name !== M.emptyProject().venue.name ? P.venue.name : undefined;
    // One corridor with a row of units on each side reads well up to about 160 × 64 m. The block
    // is shrunk until it sits inside the outline, tried at the box centre and where the building
    // is widest (an L-shaped mall); the rest of a big building is left to draw.
    const outside = (p) => p.cells.filter((c) => c.ring.some((q) => { const x = f.xy(q); return !M.pointInRing(x, ring) && M.nearestEdge(x, ring).d > 0.3; })).length;
    let best = null;
    for (const mid of [ob.c, M.interiorPoint(ring)]) {
      for (let k = 1; k >= 0.5; k -= 0.05) {
        const bw = Math.max(Math.min(w, DEMO_MAX[0]) * k, 28);
        const bh = Math.max(Math.min(h, DEMO_MAX[1]) * k, 22);
        const p = M.demoProject({ center: f.ll(mid), w: bw, h: bh, angle, name });
        const n = outside(p);
        if (!best || n < best.n || (n === best.n && bw * bh > best.area)) best = { p, n, area: bw * bh, bw, bh };
        if (n === 0) break;
      }
      if (best.n === 0 && best.area >= Math.min(w * h, DEMO_MAX[0] * DEMO_MAX[1]) * 0.8) break;
    }
    next = best.p;
    next.venue.footprint = fp;
    next.venue.building_id = P.venue.building_id;
    if (best.bw * best.bh < w * h * 0.8) note = `Layout mẫu ${Math.round(best.bw)} × ${Math.round(best.bh)} m đặt trong toà; phần còn lại vẽ tiếp.`;
    if (best.n) note += ` ${best.n} không gian lọt ra ngoài outline (xem tab Kiểm tra).`;
  }
  replaceProject(next);
  toast(note);
}

// ---------- levels ----------

function addLevel(where, copyAll = false) {
  const ls = M.levelsSorted(P);
  const cur = level();
  const top = ls.at(-1);
  const bottom = ls[0];
  const id = M.nextId(P, "L");
  const above = where === "above";
  const ordinal = above ? top.ordinal + 1 : bottom.ordinal - 1;
  const lv = above
    ? { id, name: `Tầng ${ordinal + 1}`, short: String(ordinal + 1), ordinal, elevation: top.elevation + top.height, height: cur.height }
    : { id, name: `Tầng hầm B${-ordinal}`, short: `B${-ordinal}`, ordinal, elevation: bottom.elevation - 3.6, height: 3.6 };
  edit(() => {
    P.levels.push(lv);
    const src = P.cells.filter((c) => c.level === cur.id && (copyAll || M.isVertical(c)));
    copyCells(src, id, copyAll);
  });
  setLevel(id);
  toast(copyAll ? `Đã nhân bản ${cur.name} thành ${lv.name}.` : `Đã thêm ${lv.name}${P.cells.some((c) => c.level === id) ? ", copy các thang của " + cur.name : ""}.`);
}

/** Copy cells (and the doors on their walls) to another level; connectors keep their vgroup. */
function copyCells(src, levelId, withPois = false) {
  const f = G.frame;
  const ids = new Set(src.map((c) => c.id));
  for (const c of src) {
    const name = c.name && !M.isVertical(c) ? c.name.replace(/^[\w-]+ · /, "") : c.name;
    P.cells.push({ ...structuredClone(c), id: M.nextId(P, "C"), level: levelId, name });
  }
  for (const b of G.boundaries) {
    if (!b.cells.some((id) => ids.has(id))) continue;
    if (P.doors.some((d) => d.level === levelId && M.dist(f.xy(d.at), b.xy) < 0.2)) continue;
    const d = P.doors.find((x) => x.id === b.door);
    P.doors.push({ ...d, id: M.nextId(P, "D"), level: levelId });
  }
  if (withPois) {
    const from = src[0]?.level;
    for (const o of P.pois.filter((x) => x.level === from)) P.pois.push({ ...o, id: M.nextId(P, "P"), level: levelId });
  }
}

function deleteLevel(id) {
  if (P.levels.length <= 1) return toast("Cần ít nhất một tầng.");
  const lv = M.levelById(P, id);
  const n = P.cells.filter((c) => c.level === id).length;
  if (!confirm(`Xoá ${lv.name}${n ? ` cùng ${n} không gian` : ""}?`)) return;
  edit(() => {
    P.levels = P.levels.filter((l) => l.id !== id);
    for (const k of ["cells", "doors", "pois"]) P[k] = P[k].filter((o) => o.level !== id);
  });
  ui.sel = null;
  setLevel(M.levelsSorted(P)[0].id);
}

// ---------- routing ----------

function runRoute(announce = true) {
  if (!ui.routeFrom || !ui.routeTo) { ui.route = null; return; }
  const avoid = ui.avoid ? new Set(["stairs", "escalator"]) : new Set();
  ui.route = M.route(P, G, ui.routeFrom, ui.routeTo, { avoid });
  if (!announce) return;
  if (!ui.route) toast(ui.avoid ? "Không có đường đi khi tránh thang bộ / thang cuốn." : "Không có đường đi giữa hai điểm này.");
  else setLevel(ui.route.legs[0].level);
}

// ---------- floor plan image ----------

function planCorners(pl) {
  const h = pl.w * pl.aspect;
  const t = (-pl.rot * Math.PI) / 180;
  const [cos, sin] = [Math.cos(t), Math.sin(t)];
  return [[-pl.w / 2, h / 2], [pl.w / 2, h / 2], [pl.w / 2, -h / 2], [-pl.w / 2, -h / 2]]
    .map(([x, y]) => ll([pl.c[0] + x * cos - y * sin, pl.c[1] + x * sin + y * cos]));
}

function addPlanLayer() {
  const pl = ui.plans[ui.level];
  map.addSource("ind-plan", { type: "image", url: pl.url, coordinates: planCorners(pl) });
  map.addLayer({ id: "ind-plan", type: "raster", source: "ind-plan", paint: { "raster-opacity": pl.opacity, "raster-fade-duration": 0 } },
    map.getLayer("ind-ghost") ? "ind-ghost" : undefined);
}

function syncPlanLayer() {
  if (!map.isStyleLoaded()) return;
  if (map.getLayer("ind-plan")) map.removeLayer("ind-plan");
  if (map.getSource("ind-plan")) map.removeSource("ind-plan");
  if (ui.plans[ui.level]) addPlanLayer();
  renderPlanControls();
}

function updatePlan() {
  const pl = ui.plans[ui.level];
  if (!pl || !map.getSource("ind-plan")) return;
  map.getSource("ind-plan").setCoordinates(planCorners(pl));
  map.setPaintProperty("ind-plan", "raster-opacity", pl.opacity);
}

function savePlan(id) {
  const pl = ui.plans[id];
  try {
    if (pl) localStorage.setItem(PLAN_STORE + id, JSON.stringify(pl));
    else localStorage.removeItem(PLAN_STORE + id);
  } catch { toast("Ảnh quá lớn để lưu trong trình duyệt: sẽ mất khi tải lại trang."); }
}

function loadPlans() {
  for (const l of P.levels) {
    try {
      const raw = localStorage.getItem(PLAN_STORE + l.id);
      if (raw) ui.plans[l.id] = JSON.parse(raw);
    } catch { /* ignore */ }
  }
}

function fitPlan(pl) {
  const fp = P.venue.footprint;
  if (fp.length < 3) { const c = map.getCenter(); pl.c = xy([c.lng, c.lat]); return; }
  const ob = M.orientedBox(fp.map(xy));
  // Long side of the image along the long side of the building.
  const [w, h, ang] = ob.w >= ob.h ? [ob.w, ob.h, ob.angle] : [ob.h, ob.w, ob.angle + Math.PI / 2];
  pl.c = ob.c;
  pl.rot = -((ang * 180) / Math.PI);
  pl.w = Math.max(w, h / pl.aspect);  // the whole building fits on the image
}

function loadPlanFile(file) {
  const reader = new FileReader();
  reader.onload = () => {
    const img = new Image();
    img.onload = () => {
      const pl = { url: reader.result, aspect: img.height / img.width, c: [0, 0], w: 100, rot: 0, opacity: 0.6 };
      fitPlan(pl);
      ui.plans[ui.level] = pl;
      savePlan(ui.level);
      syncPlanLayer();
    };
    img.src = reader.result;
  };
  reader.readAsDataURL(file);
}

function renderPlanControls() {
  const pl = ui.plans[ui.level];
  for (const id of ["planFit", "planMove", "planDrop"]) $(`#${id}`).disabled = !pl;
  $("#planControls").hidden = !pl;
  if (!pl) { if (ui.tool === "plan") setTool("select"); return; }
  $("#planOpacity").value = pl.opacity;
  $("#planRot").value = pl.rot;
  $("#planRotVal").textContent = `${fmtNum(pl.rot, 1)}°`;
  $("#planWidth").value = Math.round(pl.w * 10) / 10;
}

// ---------- panel ----------

function renderPanel() {
  $("#title").textContent = P.venue.name;
  const nav = G.nodes.length;
  $("#meta").textContent = `IndoorGML 2.0 · ${P.levels.length} tầng · ${P.cells.length} không gian · ${nav} node · ${G.edges.length} edge`;
  document.title = `${P.venue.name} · Indoor Map Editor`;
  const ls = M.levelsSorted(P).reverse();
  $("#levelList").innerHTML = ls.map((l) => {
    const n = P.cells.filter((c) => c.level === l.id).length;
    return `<li data-level="${esc(l.id)}" class="${l.id === ui.level ? "on" : ""}">
      <span class="short">${esc(l.short)}</span>
      <span><div class="nm">${esc(l.name)}</div><div class="sub">${fmtNum(l.elevation, 1)} m · cao ${fmtNum(l.height, 1)} m · ${n} không gian</div></span>
      <button class="linkbtn" type="button" data-edit-level="${esc(l.id)}">Sửa</button></li>`;
  }).join("");
  $("#undoBtn").disabled = !hist.undo.length;
  $("#redoBtn").disabled = !hist.redo.length;
  const errs = issues.filter((i) => i.level === "error").length;
  $("#issueCount").textContent = issues.length ? String(issues.length) : "✓";
  $("#issueCount").className = errs ? "" : "ok";
  renderPlanControls();
}

function renderLevelBar() {
  const onRoute = new Set(ui.route?.legs.map((l) => l.level) || []);
  $("#levelBar").innerHTML = M.levelsSorted(P).reverse().map((l) => `<button type="button" data-level="${esc(l.id)}" aria-pressed="${l.id === ui.level}"
    class="${onRoute.has(l.id) ? "route" : ""}" title="${esc(l.name)}">${esc(l.short)}</button>`).join("");
}

// ---------- drawer ----------

function renderDrawer() {
  for (const b of $$("[data-dtab]")) b.setAttribute("aria-selected", String(b.dataset.dtab === ui.tab));
  const body = $("#dBody");
  if (ui.tab === "route") body.innerHTML = routeHtml();
  else if (ui.tab === "check") body.innerHTML = checkHtml();
  else body.innerHTML = propsHtml();
}

const opt = (v, label, cur) => `<option value="${esc(v)}" ${String(v) === String(cur ?? "") ? "selected" : ""}>${esc(label)}</option>`;
const field = (label, html) => `<label class="field"><span>${esc(label)}</span>${html}</label>`;
const text = (f, v, ph = "") => `<input type="text" data-f="${f}" value="${esc(v ?? "")}" placeholder="${esc(ph)}" ${f === "name" ? 'id="fName"' : ""}>`;
const num = (f, v, step = 0.1) => `<input type="number" data-f="${f}" value="${esc(v ?? "")}" step="${step}">`;

function propsHtml() {
  const s = ui.sel;
  const o = findSel();
  if (!o || !s) return venueHtml();
  if (s.kind === "venue") return venueHtml(true);
  if (s.kind === "level") {
    return `<div class="dsec"><h3>${esc(o.name)} <span class="hint">${esc(o.id)}</span></h3>
      ${field("Tên tầng", text("name", o.name))}
      <div class="row2">${field("Ký hiệu ngắn", text("short", o.short))}${field("Thứ tự (ordinal)", num("ordinal", o.ordinal, 1))}</div>
      <div class="row2">${field("Cao độ sàn (m)", num("elevation", o.elevation))}${field("Chiều cao tầng (m)", num("height", o.height))}</div>
      <div class="hint">Ordinal 0 là tầng trệt, âm là tầng hầm. Cao độ dùng cho 3D và chi phí đi thang.</div>
      <div class="btn-row"><button class="btn small danger" type="button" data-act="del">Xoá tầng</button></div></div>`;
  }
  if (s.kind === "cell") return cellHtml(o);
  if (s.kind === "door") {
    const b = G.boundaries.find((x) => x.door === o.id);
    const names = b.cells.map((id) => esc(M.cellLabel(findCell(id))));
    return `<div class="dsec"><h3>Cửa <span class="hint">${esc(o.id)} · core:CellBoundary → navi:ConnectionBoundary</span></h3>
      ${field("Loại", `<select data-f="kind">${Object.entries(DOOR_KINDS).map(([k, v]) => opt(k, v, o.kind)).join("")}</select>`)}
      ${field("Rộng (m)", num("width", o.width))}
      <dl class="kv"><dt>Nối</dt><dd>${names.length ? names.join(" ↔ ") : '<span style="color:var(--bad)">không chạm không gian nào</span>'}</dd>
        <dt>Dual</dt><dd>${b.cells.length >= 2 ? `Edge ${esc(G.edges.find((e) => e.door === o.id)?.id || "")}` : b.exterior ? "cửa ra ngoài: không có Edge" : "—"}</dd></dl>
      <div class="btn-row"><button class="btn small danger" type="button" data-act="del">Xoá cửa</button></div></div>`;
  }
  return `<div class="dsec"><h3>POI <span class="hint">${esc(o.id)}</span></h3>
    ${field("Loại", `<select data-f="type">${Object.entries(M.POI_TYPES).map(([k, v]) => opt(k, v, o.type)).join("")}</select>`)}
    ${field("Tên", text("name", o.name))}
    <div class="btn-row"><button class="btn small danger" type="button" data-act="del">Xoá POI</button></div></div>`;
}

function cellHtml(c) {
  const t = M.SPACE_TYPES[c.type];
  const area = Math.abs(M.ringArea(c.ring.map(xy)));
  const node = G.nodeById.get(`N-${c.id}`);
  const deg = node ? G.edges.filter((e) => e.from === node.id || e.to === node.id).length : 0;
  const doors = G.boundaries.filter((b) => b.cells.includes(c.id)).length;
  let vertical = "";
  if (t.vertical) {
    const peers = new Map(P.cells.filter((x) => x.vgroup && x.vgroup === c.vgroup).map((x) => [x.level, x]));
    vertical = `<div class="field"><span>Có trên các tầng (cùng nhóm <code>${esc(c.vgroup || "—")}</code>)</span>
      <div class="levelchecks">${M.levelsSorted(P).reverse().map((l) => `<label class="${l.id === c.level ? "self" : ""}">
        <input type="checkbox" data-vlevel="${esc(l.id)}" ${peers.has(l.id) ? "checked" : ""} ${l.id === c.level ? "disabled" : ""}> ${esc(l.short)}</label>`).join("")}</div>
      <span class="hint">Tick để copy thang (cùng hình, cùng cửa) sang tầng đó; các tầng liền nhau trong nhóm được nối bằng Edge đứng.</span></div>
      ${field("Nhóm thang (vgroup)", text("vgroup", c.vgroup, "vd TM-A"))}`;
  }
  const groups = { "Không gian": ["shop", "food", "supermarket", "service", "restroom", "parking", "staff"], "Lưu thông": ["corridor", "entrance", "elevator", "escalator", "stairs"], "Không đi được": ["void"] };
  const typeSel = `<select data-f="type">${Object.entries(groups).map(([g, ks]) => `<optgroup label="${esc(g)}">${ks.map((k) => opt(k, M.SPACE_TYPES[k].label, c.type)).join("")}</optgroup>`).join("")}</select>`;
  const props = c.props || {};
  return `<div class="dsec">
    <h3><span class="swatch" style="background:${colors().type[c.type]}"></span>${esc(M.cellLabel(c))} <span class="hint">${esc(c.id)}</span></h3>
    <div class="btn-row"><span class="tag">${t.nav ? `navi:${t.nav}` : "core:CellSpace"}</span><span class="tag">${esc(level().name)}</span><span class="tag">${fmtNum(area, 1)} m²</span></div>
    ${field("Tên", text("name", c.name, "vd 1-05 · Cà phê"))}
    ${field("Loại", typeSel)}
    ${vertical}
    <div class="row2">${field("Giờ mở cửa", text("p.hours", props.hours, "09:00–22:00"))}${field("Điện thoại", text("p.phone", props.phone))}</div>
    ${field("Mã thương hiệu / tenant", text("p.brand", props.brand))}
    <dl class="kv"><dt>Dual space</dt><dd>${node ? `Node ${esc(node.id)} · ${deg} edge` : "không có Node (không đi được)"}</dd>
      <dt>Cửa trên tường</dt><dd>${doors}</dd><dt>Số đỉnh</dt><dd>${c.ring.length}</dd></dl>
    <div class="btn-row">
      ${node ? `<button class="btn small" type="button" data-act="from">Đặt điểm đi</button><button class="btn small" type="button" data-act="to">Đặt điểm đến</button>` : ""}
      <button class="btn small" type="button" data-act="dup">Nhân bản</button>
      <button class="btn small danger" type="button" data-act="del">Xoá</button>
    </div></div>`;
}

function venueHtml(selected = false) {
  const v = P.venue;
  const count = {};
  for (const c of P.cells) count[c.type] = (count[c.type] || 0) + 1;
  const c = colors();
  const total = P.cells.reduce((s, x) => s + Math.abs(M.ringArea(x.ring.map(xy))), 0);
  const fpArea = v.footprint.length >= 3 ? Math.abs(M.ringArea(v.footprint.map(xy))) : 0;
  return `<div class="dsec">
    <h3>${esc(v.name)} <span class="hint">${esc(v.id)}${v.building_id ? ` · building ${esc(v.building_id)}` : ""}</span></h3>
    ${field("Tên toà nhà (venue)", text("name", v.name))}
    <div class="row2">${field("Loại", `<select data-f="category">${Object.entries(VENUE_CATEGORIES).map(([k, l]) => opt(k, l, v.category)).join("")}</select>`)}
      ${field("Địa chỉ", text("address", v.address))}</div>
    <dl class="kv"><dt>Outline</dt><dd>${fpArea ? `${fmtNum(fpArea, 0)} m² · ${v.footprint.length} đỉnh ${selected ? "" : '<button class="linkbtn" type="button" data-act="selvenue">sửa</button>'}` : "chưa có"}</dd>
      <dt>Tổng diện tích không gian</dt><dd>${fmtNum(total, 0)} m² trên ${P.levels.length} tầng</dd></dl>
    ${selected && fpArea ? '<div class="btn-row"><button class="btn small danger" type="button" data-act="del">Xoá outline</button></div>' : ""}
  </div>
  <div class="model-box">
    <b>Mô hình IndoorGML 2.0</b>
    <div class="cols2">
      <div>Primal space<br><b>${P.cells.length}</b> CellSpace<br><b>${G.boundaries.length}</b> CellBoundary</div>
      <div>Dual space (NRG)<br><b>${G.nodes.length}</b> Node<br><b>${G.edges.length}</b> Edge (${G.edges.filter((e) => e.kind !== "door").length} đứng)</div>
    </div>
    <span class="hint">Mỗi không gian đi được ↔ một Node; mỗi cửa nối hai không gian ↔ một Edge; thang cùng nhóm ở hai tầng liền nhau ↔ Edge đứng. Đồ thị sinh tự động, không vẽ tay.</span>
  </div>
  <div class="dsec"><h3>Chú giải</h3>
    <div class="typelegend">${Object.entries(M.SPACE_TYPES).map(([k, t]) => `<span><span class="swatch" style="background:${c.type[k]}"></span> ${esc(t.label)} <span class="n">${count[k] || ""}</span></span>`).join("")}</div>
  </div>`;
}

function routeOptions(cur) {
  const groups = M.levelsSorted(P).reverse().map((l) => {
    const cells = P.cells.filter((c) => c.level === l.id && M.navigable(c) && c.type !== "corridor")
      .sort((a, b) => M.cellLabel(a).localeCompare(M.cellLabel(b), "vi"));
    return cells.length ? `<optgroup label="${esc(l.name)}">${cells.map((c) => opt(c.id, M.cellLabel(c), cur)).join("")}</optgroup>` : "";
  });
  return `<option value="">— chọn —</option>${groups.join("")}`;
}

function routeHtml() {
  const r = ui.route;
  let result = "";
  if (ui.routeFrom && ui.routeTo) {
    result = r
      ? `<div class="dsec"><div><span class="route-km">${fmtNum(r.walk, 0)} m</span> <span class="hint">đi bộ · chi phí ${fmtNum(r.cost, 0)} (quy đổi thang)</span></div>
        <ol class="steps-route">${r.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
        <div class="hint">Đoạn trên tầng đang xem tô đậm; tầng có đường đi được đánh dấu trên thanh chọn tầng. Bật 3D để xem cả tuyến.</div></div>`
      : `<div class="alert bad"><span class="icon"></span>Không có đường đi${ui.avoid ? " khi tránh thang bộ / thang cuốn" : ""}. Kiểm tra cửa và nhóm thang ở tab Kiểm tra.</div>`;
  }
  return `<div class="dsec"><h3>Tìm đường trong nhà</h3>
    ${field("Từ", `<select id="rFrom">${routeOptions(ui.routeFrom)}</select>`)}
    ${field("Đến", `<select id="rTo">${routeOptions(ui.routeTo)}</select>`)}
    <label class="check"><input type="checkbox" id="rAvoid" ${ui.avoid ? "checked" : ""}> Tránh thang bộ và thang cuốn (xe lăn, xe đẩy)</label>
    <div class="btn-row"><button class="btn small" type="button" id="rSwap">Đảo chiều</button><button class="btn small" type="button" id="rClear">Xoá tuyến</button></div>
    <div class="hint">Dijkstra trên đồ thị dual space (Node / Edge). Trọng số = mét đi bộ; thang quy đổi: thang máy 25 m chờ + 1 m/m cao, thang cuốn 2,5 m/m, thang bộ 4 m/m.</div>
  </div>${result}`;
}

function checkHtml() {
  if (!issues.length) {
    return `<div class="alert"><span class="icon"></span>Không có lỗi: mọi không gian nằm trong outline, không chồng nhau, đều đi tới được từ lối vào.</div>`;
  }
  const errs = issues.filter((i) => i.level === "error").length;
  return `<div class="dsec"><h3>${errs} lỗi · ${issues.length - errs} cảnh báo</h3>
    <ul class="issues">${issues.map((it, i) => `<li class="${it.level}"><button type="button" data-issue="${i}"><span class="ic"></span><span>${esc(it.msg)}</span></button></li>`).join("")}</ul>
    <div class="hint">Lỗi làm IndoorGML sai hoặc tìm đường sai; cảnh báo là thiếu dữ liệu. Click để tới đối tượng.</div></div>`;
}

function onDrawerChange(e) {
  const el = e.target;
  if (el.id === "rFrom" || el.id === "rTo" || el.id === "rAvoid") {
    ui.routeFrom = $("#rFrom").value;
    ui.routeTo = $("#rTo").value;
    ui.avoid = $("#rAvoid").checked;
    runRoute();
    return refresh();
  }
  if (el.dataset.vlevel) return toggleVertical(el.dataset.vlevel, el.checked);
  const f = el.dataset.f;
  if (!f) return;
  const o = findSel() || P.venue;
  let v = el.type === "number" ? Number(el.value) : el.value.trim();
  if (el.type === "number" && !Number.isFinite(v)) return renderDrawer();
  edit(() => {
    if (f.startsWith("p.")) {
      o.props = { ...(o.props || {}), [f.slice(2)]: v || undefined };
      if (!Object.values(o.props).some(Boolean)) delete o.props;
    } else if (f === "type" && ui.sel?.kind === "cell") {
      o.type = v;
      if (M.SPACE_TYPES[v].vertical && !o.vgroup) o.vgroup = `${VGROUP_PREFIX[v]}-${o.id}`;
      if (!M.SPACE_TYPES[v].vertical) delete o.vgroup;
    } else if (f === "vgroup") {
      if (v) o.vgroup = v; else delete o.vgroup;
    } else {
      if (f === "short" && !v) v = o.short;
      o[f] = v;
    }
  });
}

function toggleVertical(levelId, on) {
  const c = findSel();
  edit(() => {
    if (!c.vgroup) c.vgroup = `${VGROUP_PREFIX[c.type]}-${c.id}`;
    if (on) copyCells([c], levelId);
    else P.cells = P.cells.filter((x) => !(x.level === levelId && x.vgroup === c.vgroup));
  });
}

function onDrawerClick(e) {
  const tab = e.target.closest("[data-dtab]");
  if (tab) { ui.tab = tab.dataset.dtab; return renderDrawer(); }
  const issue = e.target.closest("[data-issue]");
  if (issue) return gotoRef(issues[Number(issue.dataset.issue)].ref);
  if (e.target.id === "rSwap") { [ui.routeFrom, ui.routeTo] = [ui.routeTo, ui.routeFrom]; runRoute(); return refresh(); }
  if (e.target.id === "rClear") { ui.routeFrom = ui.routeTo = ""; ui.route = null; return refresh(); }
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (!act) return;
  if (act === "del") return deleteSel();
  if (act === "selvenue") { ui.sel = { kind: "venue", id: P.venue.id }; setTool("select"); return refresh(); }
  if (act === "from" || act === "to") {
    ui[act === "from" ? "routeFrom" : "routeTo"] = ui.sel.id;
    ui.tab = "route";
    runRoute(Boolean(ui.routeFrom && ui.routeTo));
    return refresh();
  }
  if (act === "dup") {
    const c = findSel();
    const id = M.nextId(P, "C");
    edit(() => {
      const shift = (q) => { const p = xy(q); return ll([p[0] + 2, p[1] - 2]); };
      P.cells.push({ ...structuredClone(c), id, name: c.name ? `${c.name} (2)` : "", ring: c.ring.map(shift), ...(c.vgroup ? { vgroup: `${VGROUP_PREFIX[c.type]}-${id}` } : {}) });
    });
    ui.sel = { kind: "cell", id };
    refresh();
  }
}

function gotoRef(ref) {
  if (ref.kind === "venue") { ui.sel = { kind: "venue", id: P.venue.id }; fitVenue(); }
  else {
    const o = ref.kind === "cell" ? findCell(ref.id) : P.doors.find((d) => d.id === ref.id);
    if (!o) return;
    ui.sel = { kind: ref.kind, id: ref.id };
    if (o.level !== ui.level) setLevel(o.level);
    const pts = o.ring || [o.at];
    const b = pts.reduce((bb, p) => bb.extend(p), new maplibregl.LngLatBounds(pts[0], pts[0]));
    map.fitBounds(b, { padding: 160, maxZoom: 20, duration: 600 });
  }
  ui.tab = "props";
  refresh();
}

// ---------- files ----------

function download(name, text, type) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
const slug = () => (P.venue.name || "indoor").normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/đ/gi, "d").replace(/[^\w]+/g, "-").replace(/^-|-$/g, "").toLowerCase() || "indoor";

function exportWith(kind) {
  const errs = issues.filter((i) => i.level === "error").length;
  if (errs && !confirm(`Còn ${errs} lỗi ở tab Kiểm tra. Vẫn xuất file?`)) return;
  if (kind === "gml") download(`${slug()}.indoorgml.gml`, M.toIndoorGMLXml(P, G), "application/gml+xml");
  if (kind === "json") download(`${slug()}.indoorgml.json`, JSON.stringify(M.toIndoorGMLJSON(P, G), null, 1), "application/json");
  if (kind === "geo") download(`${slug()}.indoor.geojson`, JSON.stringify(M.toGeoJSON(P, G)), "application/geo+json");
}

function openFile(file) {
  file.text().then((t) => {
    const data = JSON.parse(t);
    replaceProject(M.normalize(data));
    toast(`Đã mở ${file.name}.`);
  }).catch((e) => toast(`Không mở được: ${e.message}`));
}

// ---------- map setup ----------

function fitVenue() {
  if (!map) return;
  // The drawn spaces when there are any (a big mall may only be partly drawn), else the outline.
  const pts = P.cells.length ? P.cells.flatMap((c) => c.ring) : P.venue.footprint;
  if (!pts.length) return;
  const b = pts.reduce((bb, p) => bb.extend(p), new maplibregl.LngLatBounds(pts[0], pts[0]));
  const threeD = $("#threeD").checked;
  map.fitBounds(b, { padding: { top: 70, bottom: 50, left: 340, right: 70 }, maxZoom: 20, duration: 700, pitch: threeD ? 55 : 0, bearing: threeD ? -20 : 0 });
}

async function loadBase() {
  const st = await fetch("/api/base").then((r) => r.json()).catch(() => null);
  if (!st?.base) {
    $("#baseHint").textContent = "Chưa có base Hà Nội (tạo ở trang chính) — vẽ outline bằng tay hoặc dùng layout mẫu.";
    $("#pickBtn").disabled = true;
    return null;
  }
  const url = new URL(`/api/files/${encodeURIComponent(st.base.pmtiles_token)}`, location.href).href;
  const protocol = new pmtiles.Protocol();
  maplibregl.addProtocol("pmtiles", protocol.tile);
  const archive = new pmtiles.PMTiles(url);
  protocol.add(archive);
  const meta = (await archive.getMetadata().catch(() => null)) || {};
  const layer = meta.vector_layers?.[0]?.id || "buildings";
  $("#baseHint").textContent = `Base: ${st.base.name || st.label}. Zoom gần rồi bấm “Chọn toà từ base”.`;
  return { layer, idField: "building_id", view: st.base.view, source: { type: "vector", url: `pmtiles://${url}`, attribution: meta.attribution || "" } };
}

function bindUi() {
  const typeKeys = ["shop", "food", "supermarket", "service", "restroom", "parking", "staff", "corridor", "entrance", "elevator", "escalator", "stairs", "void"];
  $("#typeSel").innerHTML = typeKeys.map((k) => opt(k, M.SPACE_TYPES[k].label, "shop")).join("");
  $("#poiSel").innerHTML = Object.entries(M.POI_TYPES).map(([k, v]) => opt(k, v, "info")).join("");
  $("#toolbar").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.tool) setTool(b.dataset.tool);
    if (b.id === "undoBtn") undo();
    if (b.id === "redoBtn") undo(true);
  });
  $("#pickBtn").addEventListener("click", () => setTool(ui.tool === "pick" ? "select" : "pick"));
  $("#footBtn").addEventListener("click", () => setTool("footprint"));
  $("#demoBtn").addEventListener("click", makeDemo);
  $("#addAbove").addEventListener("click", () => addLevel("above"));
  $("#addBelow").addEventListener("click", () => addLevel("below"));
  $("#dupLevel").addEventListener("click", () => addLevel("above", true));
  $("#levelList").addEventListener("click", (e) => {
    const ed = e.target.closest("[data-edit-level]");
    if (ed) { ui.sel = { kind: "level", id: ed.dataset.editLevel }; ui.tab = "props"; return setLevel(ed.dataset.editLevel); }
    const li = e.target.closest("[data-level]");
    if (li) setLevel(li.dataset.level);
  });
  $("#levelBar").addEventListener("click", (e) => { const b = e.target.closest("[data-level]"); if (b) setLevel(b.dataset.level); });
  for (const id of ["showGraph", "showLabels", "showGhost"]) $(`#${id}`).addEventListener("change", draw);
  $("#threeD").addEventListener("change", () => {
    const on = $("#threeD").checked;
    $("#explodeField").hidden = !on;
    $("#toolbar").hidden = on;
    if (on) setTool("select");
    $("#toolNote").hidden = on;
    draw();
    fitVenue();
  });
  $("#explode").addEventListener("input", () => { $("#explodeVal").textContent = `×${$("#explode").value}`; draw3D(); });
  $("#explodeVal").textContent = `×${$("#explode").value}`;
  $("#planFile").addEventListener("change", (e) => { if (e.target.files[0]) loadPlanFile(e.target.files[0]); e.target.value = ""; });
  $("#planFit").addEventListener("click", () => { fitPlan(ui.plans[ui.level]); updatePlan(); renderPlanControls(); savePlan(ui.level); });
  $("#planMove").addEventListener("click", () => setTool(ui.tool === "plan" ? "select" : "plan"));
  $("#planDrop").addEventListener("click", () => { delete ui.plans[ui.level]; savePlan(ui.level); syncPlanLayer(); });
  const planInput = (id, key, fmt = Number) => $(`#${id}`).addEventListener("input", (e) => {
    const pl = ui.plans[ui.level];
    const v = fmt(e.target.value);
    if (!pl || !Number.isFinite(v)) return;
    pl[key] = v;
    $("#planRotVal").textContent = `${fmtNum(pl.rot, 1)}°`;
    updatePlan();
    savePlan(ui.level);
  });
  planInput("planOpacity", "opacity");
  planInput("planRot", "rot");
  planInput("planWidth", "w");
  $("#saveBtn").addEventListener("click", () => download(`${slug()}.indoor-project.json`, JSON.stringify(P, null, 1), "application/json"));
  $("#openFile").addEventListener("change", (e) => { if (e.target.files[0]) openFile(e.target.files[0]); e.target.value = ""; });
  $("#exGml").addEventListener("click", () => exportWith("gml"));
  $("#exJson").addEventListener("click", () => exportWith("json"));
  $("#exGeo").addEventListener("click", () => exportWith("geo"));
  $("#resetBtn").addEventListener("click", () => {
    if (!confirm("Xoá toàn bộ project (vẫn hoàn tác được bằng Ctrl+Z)?")) return;
    replaceProject(M.emptyProject(map ? [map.getCenter().lng, map.getCenter().lat] : P.origin));
  });
  const drawer = $("#drawer");
  drawer.addEventListener("click", onDrawerClick);
  drawer.addEventListener("change", onDrawerChange);
  drawer.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches("input[type=text], input[type=number]")) e.target.blur(); });
  document.addEventListener("keydown", onKey);
}

async function main() {
  bindUi();
  loadPlans();
  await loadVmapPresets();
  initBasemap("vmap");
  base = await loadBase();
  // ?lon=&lat=&z= opens the map there (e.g. on a building seen in /preview) instead of on the project.
  const q = new URLSearchParams(location.search);
  const at = q.has("lon") && q.has("lat") ? [Number(q.get("lon")), Number(q.get("lat"))] : null;
  const center = at || (P.venue.footprint.length || P.cells.length ? P.origin : base?.view?.center || P.origin);
  map = new maplibregl.Map({
    container: "map",
    style: await basemapStyle($("#baseSel").value, base?.source),
    center,
    zoom: Number(q.get("z")) || (base?.view && !P.venue.footprint.length ? 16 : 18),
    maxZoom: 22,
    attributionControl: { compact: true },
  });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
  map.once("style.load", () => { installLayers(); if (!at) fitVenue(); });
  bindBasemapControls(map, () => base?.source, installLayers);
  map.on("mousemove", onMove);
  map.on("click", onClick);
  map.on("dblclick", (e) => { if (ui.tool === "polygon" || ui.tool === "footprint") { e.preventDefault(); finishPolygon(); } });
  map.on("mousedown", onDown);
  map.on("mouseup", onUp);
  map.on("zoom", () => { $("#zBadge").textContent = `z ${map.getZoom().toFixed(1)}`; });
  $("#zBadge").textContent = `z ${map.getZoom().toFixed(1)}`;
  setTool("select");
  refresh();
  if (!P.cells.length && !P.venue.footprint.length) {
    toast(base ? "Bắt đầu: “Chọn toà từ base”, hoặc “Tạo layout mẫu” để xem demo." : "Bắt đầu: “Tạo layout mẫu” để xem demo, hoặc vẽ outline toà nhà.");
  }
}

main();
