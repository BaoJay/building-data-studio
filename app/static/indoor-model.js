// Indoor map model of the /indoor editor (PoC): one venue (a building) with its levels, the spaces,
// doors and POIs drawn on each level, and what follows from them in OGC IndoorGML 2.0 terms:
// spaces are CellSpace, doors are CellBoundary, and the dual navigation graph (Node / Edge) is
// derived, never drawn. Routes, checks and the exports all read that graph.
// Pure functions without DOM: shared by indoor.js and tests/js/indoor-model.test.mjs.

export const SCHEMA = "bds-indoor/1";

// Space types of the editor. `nav` is the IndoorGML 2.0 Navigation class written on export
// (GeneralSpace / TransitionSpace / ConnectionSpace / AnchorSpace); null = not navigable
// (a plain core:CellSpace, no node in the graph). `color` is a map token of tokens.css.
export const SPACE_TYPES = {
  shop: { label: "Cửa hàng", nav: "GeneralSpace", color: "cat-1", fn: "retail" },
  food: { label: "Ăn uống (F&B)", nav: "GeneralSpace", color: "cat-4", fn: "food_and_beverage" },
  supermarket: { label: "Siêu thị", nav: "GeneralSpace", color: "cat-3", fn: "supermarket" },
  service: { label: "Dịch vụ / tiện ích", nav: "GeneralSpace", color: "cat-6", fn: "service" },
  restroom: { label: "Nhà vệ sinh", nav: "GeneralSpace", color: "cat-6", fn: "restroom" },
  parking: { label: "Bãi đỗ xe", nav: "GeneralSpace", color: "cat-7", fn: "parking" },
  corridor: { label: "Hành lang / sảnh", nav: "TransitionSpace", color: "height-1", fn: "corridor" },
  entrance: { label: "Lối vào", nav: "AnchorSpace", color: "cat-2", fn: "entrance" },
  elevator: { label: "Thang máy", nav: "TransitionSpace", color: "cat-5", fn: "elevator", vertical: true },
  escalator: { label: "Thang cuốn", nav: "TransitionSpace", color: "cat-5", fn: "escalator", vertical: true },
  stairs: { label: "Thang bộ", nav: "TransitionSpace", color: "cat-5", fn: "stairs", vertical: true },
  staff: { label: "Kỹ thuật / nhân viên", nav: "GeneralSpace", color: "map-neutral", fn: "back_of_house", usage: "restricted" },
  void: { label: "Giếng trời / kết cấu", nav: null, color: "map-ground", fn: "void" },
};

export const POI_TYPES = {
  atm: "ATM",
  info: "Quầy thông tin",
  wc: "WC",
  exit: "Lối thoát hiểm",
  fire: "Bình chữa cháy",
  kiosk: "Kiosk",
  other: "Khác",
};

// Walking metres a vertical move "costs" per metre of height, plus a fixed wait (elevator).
const VERTICAL_COST = { elevator: [25, 1], escalator: [0, 2.5], stairs: [0, 4] };
export const DOOR_SNAP_M = 0.3;  // a door belongs to every space whose wall is this close

export const navigable = (cell) => Boolean(SPACE_TYPES[cell.type]?.nav);
export const isVertical = (cell) => Boolean(SPACE_TYPES[cell.type]?.vertical);

// ---------- local metric frame ----------

const M_PER_DEG_LAT = 110574;
const M_PER_DEG_LNG = 111320;

/** Equirectangular frame around origin: lng/lat <-> metres east/north. Fine at building scale. */
export function frame(origin) {
  const [lng0, lat0] = origin;
  const kx = M_PER_DEG_LNG * Math.cos((lat0 * Math.PI) / 180);
  return {
    xy: ([lng, lat]) => [(lng - lng0) * kx, (lat - lat0) * M_PER_DEG_LAT],
    ll: ([x, y]) => [round7(lng0 + x / kx), round7(lat0 + y / M_PER_DEG_LAT)],
  };
}
const round7 = (v) => Math.round(v * 1e7) / 1e7;

// ---------- planar geometry (metres, open rings) ----------

export const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);

export function ringArea(ring) {
  let s = 0;
  for (let i = 0; i < ring.length; i++) {
    const [x1, y1] = ring[i];
    const [x2, y2] = ring[(i + 1) % ring.length];
    s += x1 * y2 - x2 * y1;
  }
  return s / 2;
}

export function centroid(ring) {
  const a = ringArea(ring);
  if (Math.abs(a) < 1e-9) {
    const n = ring.length || 1;
    return [ring.reduce((s, p) => s + p[0], 0) / n, ring.reduce((s, p) => s + p[1], 0) / n];
  }
  let cx = 0;
  let cy = 0;
  for (let i = 0; i < ring.length; i++) {
    const [x1, y1] = ring[i];
    const [x2, y2] = ring[(i + 1) % ring.length];
    const f = x1 * y2 - x2 * y1;
    cx += (x1 + x2) * f;
    cy += (y1 + y2) * f;
  }
  return [cx / (6 * a), cy / (6 * a)];
}

export function pointInRing([x, y], ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** Nearest point of segment ab to p: { d, t, q }. */
export function projectOnSegment(p, a, b) {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const len2 = dx * dx + dy * dy;
  const t = len2 ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2)) : 0;
  const q = [a[0] + t * dx, a[1] + t * dy];
  return { d: dist(p, q), t, q };
}

/** Nearest edge of a ring to p: { d, i (edge index i -> i+1), q }. */
export function nearestEdge(p, ring) {
  let best = { d: Infinity, i: -1, q: null };
  for (let i = 0; i < ring.length; i++) {
    const r = projectOnSegment(p, ring[i], ring[(i + 1) % ring.length]);
    if (r.d < best.d) best = { d: r.d, i, q: r.q };
  }
  return best;
}

// Proper crossing of segments ab and cd: each one's ends lie more than eps metres on either side
// of the other. Touching ends and shared walls (collinear up to coordinate rounding) do not count.
function crosses(a, b, c, d, eps = 0.05) {
  // Signed distance of r from the line pq.
  const o = (p, q, r) => ((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])) / (dist(p, q) || 1);
  const d1 = o(a, b, c);
  const d2 = o(a, b, d);
  const d3 = o(c, d, a);
  const d4 = o(c, d, b);
  return ((d1 > eps && d2 < -eps) || (d1 < -eps && d2 > eps)) && ((d3 > eps && d4 < -eps) || (d3 < -eps && d4 > eps));
}

export function selfIntersects(ring) {
  const n = ring.length;
  for (let i = 0; i < n; i++) {
    for (let j = i + 2; j < n; j++) {
      if (i === 0 && j === n - 1) continue;  // neighbours through the closing edge
      if (crosses(ring[i], ring[(i + 1) % n], ring[j], ring[(j + 1) % n])) return true;
    }
  }
  return false;
}

/** Do two rings overlap by more than a shared wall? Edge crossings, or one vertex deep inside the other. */
export function ringsOverlap(a, b, tol = 0.2) {
  for (let i = 0; i < a.length; i++) {
    for (let j = 0; j < b.length; j++) {
      if (crosses(a[i], a[(i + 1) % a.length], b[j], b[(j + 1) % b.length])) return true;
    }
  }
  const deepInside = (p, ring) => pointInRing(p, ring) && nearestEdge(p, ring).d > tol;
  return a.some((p) => deepInside(p, b)) || b.some((p) => deepInside(p, a)) || deepInside(centroid(a), b);
}

/** A point well inside the ring (for labels and graph nodes): the centroid when it is inside,
 *  otherwise the grid point farthest from the walls (a cheap polylabel). */
export function interiorPoint(ring) {
  const c = centroid(ring);
  if (pointInRing(c, ring) && nearestEdge(c, ring).d > 1) return c;
  const xs = ring.map((p) => p[0]);
  const ys = ring.map((p) => p[1]);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  let best = c;
  let bestD = -1;
  const n = 24;
  for (let i = 0; i <= n; i++) {
    for (let j = 0; j <= n; j++) {
      const p = [x0 + ((x1 - x0) * i) / n, y0 + ((y1 - y0) * j) / n];
      if (!pointInRing(p, ring)) continue;
      const d = nearestEdge(p, ring).d;
      if (d > bestD) { bestD = d; best = p; }
    }
  }
  return best;
}

/** Angle (radians, in [0, π/2)) of the longest edge: the axis rectangles and Shift-drawing follow. */
export function dominantAngle(ring) {
  let best = 0;
  let len = 0;
  for (let i = 0; i < ring.length; i++) {
    const a = ring[i];
    const b = ring[(i + 1) % ring.length];
    const l = dist(a, b);
    if (l > len) { len = l; best = Math.atan2(b[1] - a[1], b[0] - a[0]); }
  }
  const q = Math.PI / 2;
  return ((best % q) + q) % q;
}

/** Oriented bounding box along dominantAngle: { c, w, h, angle }. */
export function orientedBox(ring) {
  const angle = dominantAngle(ring);
  const [cos, sin] = [Math.cos(-angle), Math.sin(-angle)];
  const rot = ring.map(([x, y]) => [x * cos - y * sin, x * sin + y * cos]);
  const xs = rot.map((p) => p[0]);
  const ys = rot.map((p) => p[1]);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const cr = [(x0 + x1) / 2, (y0 + y1) / 2];
  const c = [cr[0] * Math.cos(angle) - cr[1] * Math.sin(angle), cr[0] * Math.sin(angle) + cr[1] * Math.cos(angle)];
  return { c, w: x1 - x0, h: y1 - y0, angle };
}

// ---------- project ----------

export function emptyProject(origin = [105.8342, 21.0278]) {
  return {
    schema: SCHEMA,
    venue: { id: "V1", name: "Toà nhà mới", category: "mall", address: "", building_id: null, footprint: [] },
    origin,
    levels: [{ id: "L0", name: "Tầng 1", short: "1", ordinal: 0, elevation: 0, height: 4.5 }],
    cells: [],
    doors: [],
    pois: [],
  };
}

/** Next free id with this prefix ("C" -> C1, C2, …). */
export function nextId(p, prefix) {
  let max = 0;
  for (const list of [p.levels, p.cells, p.doors, p.pois]) {
    for (const o of list) {
      const m = String(o.id).match(new RegExp(`^${prefix}(\\d+)$`));
      if (m) max = Math.max(max, Number(m[1]));
    }
  }
  return `${prefix}${max + 1}`;
}

export const levelById = (p, id) => p.levels.find((l) => l.id === id);
export const levelsSorted = (p) => [...p.levels].sort((a, b) => a.ordinal - b.ordinal);
export const cellLabel = (c) => c.name || SPACE_TYPES[c.type]?.label || c.id;

/** Keep what came from a file usable: known types, open rings, levels that exist. */
export function normalize(raw) {
  if (!raw || raw.schema !== SCHEMA) throw new Error(`Không phải project indoor (${SCHEMA}).`);
  const p = { ...emptyProject(raw.origin), ...raw };
  const open = (ring) => {
    const r = (ring || []).map(([x, y]) => [Number(x), Number(y)]);
    if (r.length > 1 && r[0][0] === r.at(-1)[0] && r[0][1] === r.at(-1)[1]) r.pop();
    return r;
  };
  p.venue = { ...emptyProject().venue, ...raw.venue, footprint: open(raw.venue?.footprint) };
  const lv = new Set(p.levels.map((l) => l.id));
  p.cells = (p.cells || []).filter((c) => lv.has(c.level)).map((c) => ({ ...c, type: SPACE_TYPES[c.type] ? c.type : "shop", ring: open(c.ring) }));
  p.doors = (p.doors || []).filter((d) => lv.has(d.level));
  p.pois = (p.pois || []).filter((d) => lv.has(d.level));
  return p;
}

// ---------- dual space: the navigation graph ----------

/**
 * The IndoorGML dual graph of the project:
 *  - one Node per navigable CellSpace, at a point inside it;
 *  - one CellBoundary per door, on the wall it sits on; an Edge per pair of spaces the door joins;
 *  - vertical Edges between connector spaces (elevator / escalator / stairs) of one `vgroup`
 *    on consecutive levels.
 * Coordinates are in metres of frame(p.origin).
 */
export function buildGraph(p) {
  const f = frame(p.origin);
  const lvl = new Map(p.levels.map((l) => [l.id, l]));
  const cells = p.cells.filter((c) => lvl.has(c.level) && c.ring.length >= 3);
  const ringOf = new Map(cells.map((c) => [c.id, c.ring.map(f.xy)]));
  const nodes = [];
  const nodeOf = new Map();
  for (const c of cells) {
    if (!navigable(c)) continue;
    const n = { id: `N-${c.id}`, cell: c.id, level: c.level, xy: interiorPoint(ringOf.get(c.id)) };
    nodes.push(n);
    nodeOf.set(c.id, n);
  }
  const edges = [];
  const boundaries = [];
  for (const d of p.doors) {
    const pt = f.xy(d.at);
    const touching = cells
      .filter((c) => c.level === d.level && navigable(c))
      .map((c) => ({ c, e: nearestEdge(pt, ringOf.get(c.id)) }))
      .filter((t) => t.e.d <= DOOR_SNAP_M)
      .sort((a, b) => a.e.d - b.e.d);
    let seg = [pt, pt];
    if (touching.length) {
      const ring = ringOf.get(touching[0].c.id);
      const { i, q } = touching[0].e;
      const a = ring[i];
      const b = ring[(i + 1) % ring.length];
      const l = dist(a, b) || 1;
      const u = [(b[0] - a[0]) / l, (b[1] - a[1]) / l];
      const h = Math.min((d.width || 1.5) / 2, l / 2);
      seg = [[q[0] - u[0] * h, q[1] - u[1] * h], [q[0] + u[0] * h, q[1] + u[1] * h]];
    }
    const joined = touching.map((t) => t.c.id);
    boundaries.push({ id: `B-${d.id}`, door: d.id, level: d.level, xy: pt, seg, cells: joined, exterior: joined.length === 1 });
    for (let i = 0; i < joined.length; i++) {
      for (let j = i + 1; j < joined.length; j++) {
        const a = nodeOf.get(joined[i]);
        const b = nodeOf.get(joined[j]);
        edges.push({
          id: `E-${d.id}-${i}${j}`, kind: "door", door: d.id, from: a.id, to: b.id, level: d.level, via: pt,
          weight: dist(a.xy, pt) + dist(pt, b.xy),
        });
      }
    }
  }
  // Vertical connectors: the same vgroup on consecutive levels (in ordinal order).
  const groups = new Map();
  for (const c of cells) {
    if (!isVertical(c) || !c.vgroup) continue;
    if (!groups.has(c.vgroup)) groups.set(c.vgroup, []);
    groups.get(c.vgroup).push(c);
  }
  for (const [g, list] of groups) {
    list.sort((a, b) => lvl.get(a.level).ordinal - lvl.get(b.level).ordinal);
    for (let i = 0; i + 1 < list.length; i++) {
      const [a, b] = [list[i], list[i + 1]];
      if (a.level === b.level) continue;
      const dz = Math.abs(lvl.get(b.level).elevation - lvl.get(a.level).elevation) || 4;
      const [wait, perM] = VERTICAL_COST[a.type] || VERTICAL_COST.stairs;
      edges.push({
        id: `E-${a.id}-${b.id}`, kind: a.type, vgroup: g, from: nodeOf.get(a.id).id, to: nodeOf.get(b.id).id,
        weight: wait + perM * dz, dz,
      });
    }
  }
  return { frame: f, nodes, edges, boundaries, nodeById: new Map(nodes.map((n) => [n.id, n])) };
}

/**
 * Shortest route between two spaces (Dijkstra on the dual graph).
 * opts.avoid: Set of connector types not to use (e.g. stairs, escalator for wheelchairs).
 * Returns null when there is no route, else { length, legs: [{ level, xy: [[x,y]…] }], steps, edges }.
 */
export function route(p, g, fromCell, toCell, opts = {}) {
  const avoid = opts.avoid || new Set();
  const start = `N-${fromCell}`;
  const goal = `N-${toCell}`;
  if (!g.nodeById.has(start) || !g.nodeById.has(goal)) return null;
  const adj = new Map(g.nodes.map((n) => [n.id, []]));
  for (const e of g.edges) {
    if (avoid.has(e.kind)) continue;
    adj.get(e.from).push([e.to, e]);
    adj.get(e.to).push([e.from, e]);
  }
  const distTo = new Map([[start, 0]]);
  const prev = new Map();
  const done = new Set();
  // Graphs here have hundreds of nodes, so a linear scan for the minimum is plenty.
  const open = new Set([start]);
  while (open.size) {
    let u = null;
    for (const id of open) if (u === null || distTo.get(id) < distTo.get(u)) u = id;
    open.delete(u);
    if (u === goal) break;
    done.add(u);
    for (const [v, e] of adj.get(u)) {
      if (done.has(v)) continue;
      const nd = distTo.get(u) + e.weight;
      if (nd < (distTo.get(v) ?? Infinity)) { distTo.set(v, nd); prev.set(v, [u, e]); open.add(v); }
    }
  }
  if (!distTo.has(goal)) return null;
  const hops = [];
  for (let v = goal; v !== start; v = prev.get(v)[0]) hops.unshift({ node: v, edge: prev.get(v)[1] });

  const node = (id) => g.nodeById.get(id);
  const cell = (id) => p.cells.find((c) => c.id === node(id).cell);
  const lvlName = (id) => levelById(p, id)?.name || id;
  const legs = [{ level: node(start).level, xy: [node(start).xy] }];
  const steps = [`Xuất phát: ${cellLabel(cell(start))} (${lvlName(node(start).level)})`];
  let walk = 0;
  for (let i = 0; i < hops.length; i++) {
    const { node: v, edge: e } = hops[i];
    const leg = legs.at(-1);
    if (e.kind === "door") {
      leg.xy.push(e.via);
      const c = cell(v);
      if (c.type !== "corridor" || i === hops.length - 1) steps.push(`Qua cửa vào ${cellLabel(c)}`);
    } else {
      // A ride: walk into the connector, then continue from it on the arrival level.
      const u = i === 0 ? start : hops[i - 1].node;
      leg.xy.push(node(u).xy);
      let j = i;
      while (j + 1 < hops.length && hops[j + 1].edge.kind === e.kind && hops[j + 1].edge.vgroup === e.vgroup) j++;
      const arrive = hops[j].node;
      const conn = cell(u);
      const up = levelById(p, node(arrive).level).ordinal > levelById(p, node(u).level).ordinal;
      steps.push(`Đi ${conn.name || `${SPACE_TYPES[e.kind].label.toLowerCase()} ${e.vgroup}`} ${up ? "lên" : "xuống"} ${lvlName(node(arrive).level)}`);
      legs.push({ level: node(arrive).level, xy: [node(arrive).xy], ride: e.kind });
      i = j;
    }
  }
  legs.at(-1).xy.push(node(goal).xy);
  for (const leg of legs) for (let k = 1; k < leg.xy.length; k++) walk += dist(leg.xy[k - 1], leg.xy[k]);
  steps.push(`Đến: ${cellLabel(cell(goal))} (${lvlName(node(goal).level)})`);
  return { cost: distTo.get(goal), walk, legs, steps, edges: hops.map((h) => h.edge.id) };
}

// ---------- checks ----------

/**
 * Data checks before export: { level: "error" | "warn", msg, ref: {kind, id} }.
 * Errors make the IndoorGML invalid or the routing wrong; warnings are data gaps.
 */
export function validate(p, g = buildGraph(p)) {
  const out = [];
  const f = g.frame;
  const add = (level, msg, kind, id) => out.push({ level, msg, ref: { kind, id } });
  const foot = p.venue.footprint.length >= 3 ? p.venue.footprint.map(f.xy) : null;
  if (!foot) add("warn", "Chưa có outline toà nhà (vẽ hoặc chọn từ base).", "venue", p.venue.id);
  const byLevel = new Map();
  for (const c of p.cells) {
    if (!byLevel.has(c.level)) byLevel.set(c.level, []);
    byLevel.get(c.level).push(c);
  }
  const ringOf = new Map(p.cells.map((c) => [c.id, c.ring.map(f.xy)]));
  for (const c of p.cells) {
    const r = ringOf.get(c.id);
    const name = cellLabel(c);
    if (r.length < 3) { add("error", `${name}: polygon ít hơn 3 đỉnh.`, "cell", c.id); continue; }
    if (selfIntersects(r)) add("error", `${name}: polygon tự cắt.`, "cell", c.id);
    const area = Math.abs(ringArea(r));
    if (area < 1) add("warn", `${name}: diện tích ${area.toFixed(1)} m², quá nhỏ.`, "cell", c.id);
    if (foot && r.some((pt) => !pointInRing(pt, foot) && nearestEdge(pt, foot).d > 0.3)) {
      add("error", `${name}: nằm ngoài outline toà nhà.`, "cell", c.id);
    }
    if (["shop", "food", "supermarket"].includes(c.type) && !c.name) add("warn", `${c.id} (${SPACE_TYPES[c.type].label}): chưa có tên.`, "cell", c.id);
    if (isVertical(c)) {
      const peers = p.cells.filter((o) => o.vgroup && o.vgroup === c.vgroup && o.level !== c.level);
      if (!c.vgroup || !peers.length) add("warn", `${name}: chưa nối sang tầng khác (nhóm thang).`, "cell", c.id);
    }
  }
  for (const list of byLevel.values()) {
    for (let i = 0; i < list.length; i++) {
      for (let j = i + 1; j < list.length; j++) {
        const [a, b] = [list[i], list[j]];
        if (a.ring.length < 3 || b.ring.length < 3) continue;
        if (ringsOverlap(ringOf.get(a.id), ringOf.get(b.id))) add("error", `${cellLabel(a)} chồng lên ${cellLabel(b)}.`, "cell", a.id);
      }
    }
  }
  for (const b of g.boundaries) {
    if (!b.cells.length) add("error", `Cửa ${b.door}: không nằm trên tường của không gian nào.`, "door", b.door);
    else if (b.exterior) {
      const c = p.cells.find((x) => x.id === b.cells[0]);
      if (!["entrance", "parking"].includes(c.type)) add("warn", `Cửa ${b.door}: chỉ chạm ${cellLabel(c)} (cửa ra ngoài trời?).`, "door", b.door);
    }
  }
  // Reachability from the entrances (or, without any, from the largest component).
  const adj = new Map(g.nodes.map((n) => [n.id, []]));
  for (const e of g.edges) { adj.get(e.from).push(e.to); adj.get(e.to).push(e.from); }
  const entrances = g.nodes.filter((n) => p.cells.find((c) => c.id === n.cell)?.type === "entrance");
  if (g.nodes.length && !entrances.length) add("warn", "Chưa có không gian Lối vào (AnchorSpace) nối ra ngoài.", "venue", p.venue.id);
  const seen = new Set(entrances.map((n) => n.id));
  const stack = [...seen];
  while (stack.length) for (const v of adj.get(stack.pop())) if (!seen.has(v)) { seen.add(v); stack.push(v); }
  if (entrances.length) {
    for (const n of g.nodes) {
      if (!seen.has(n.id)) {
        const c = p.cells.find((x) => x.id === n.cell);
        add("error", `${cellLabel(c)} (${levelById(p, c.level)?.name}): không có đường đi từ lối vào.`, "cell", c.id);
      }
    }
  }
  return out;
}

// ---------- exports ----------

const closeRing = (ring) => [...ring, ring[0]];

/** Flat GeoJSON for tiles (tippecanoe -> PMTiles, or PostGIS -> Martin): one feature per object,
 *  `feature_type` venue | level | unit | opening | poi | node | edge, with level_id / ordinal on each. */
export function toGeoJSON(p, g = buildGraph(p)) {
  const f = g.frame;
  const lv = new Map(p.levels.map((l) => [l.id, l]));
  const onLevel = (id) => ({ level_id: id, ordinal: lv.get(id)?.ordinal, level_name: lv.get(id)?.name });
  const feats = [];
  const fp = p.venue.footprint;
  if (fp.length >= 3) {
    feats.push({ type: "Feature", id: p.venue.id, geometry: { type: "Polygon", coordinates: [closeRing(fp)] },
      properties: { feature_type: "venue", id: p.venue.id, name: p.venue.name, category: p.venue.category,
        address: p.venue.address, building_id: p.venue.building_id, levels: p.levels.length } });
    for (const l of p.levels) {
      feats.push({ type: "Feature", id: l.id, geometry: { type: "Polygon", coordinates: [closeRing(fp)] },
        properties: { feature_type: "level", id: l.id, name: l.name, short_name: l.short, ...onLevel(l.id),
          elevation: l.elevation, height: l.height } });
    }
  }
  for (const c of p.cells) {
    if (c.ring.length < 3) continue;
    const t = SPACE_TYPES[c.type];
    const [x, y] = interiorPoint(c.ring.map(f.xy));
    feats.push({ type: "Feature", id: c.id, geometry: { type: "Polygon", coordinates: [closeRing(c.ring)] },
      properties: { feature_type: "unit", id: c.id, name: c.name || null, category: c.type, indoorgml_class: t.nav || "CellSpace",
        navigable: Boolean(t.nav), vgroup: c.vgroup || null, ...onLevel(c.level), display_point: f.ll([x, y]),
        area_m2: Math.round(Math.abs(ringArea(c.ring.map(f.xy))) * 10) / 10, ...(c.props || {}) } });
  }
  for (const b of g.boundaries) {
    const d = p.doors.find((x) => x.id === b.door);
    feats.push({ type: "Feature", id: d.id, geometry: { type: "LineString", coordinates: b.seg.map(f.ll) },
      properties: { feature_type: "opening", id: d.id, category: d.kind || "door", width: d.width, connects: b.cells, exterior: b.exterior, ...onLevel(d.level) } });
  }
  for (const o of p.pois) {
    feats.push({ type: "Feature", id: o.id, geometry: { type: "Point", coordinates: o.at },
      properties: { feature_type: "poi", id: o.id, name: o.name || POI_TYPES[o.type], category: o.type, ...onLevel(o.level) } });
  }
  for (const n of g.nodes) {
    feats.push({ type: "Feature", id: n.id, geometry: { type: "Point", coordinates: f.ll(n.xy) },
      properties: { feature_type: "node", id: n.id, cell: n.cell, ...onLevel(n.level) } });
  }
  for (const e of g.edges) {
    const a = g.nodeById.get(e.from);
    const b = g.nodeById.get(e.to);
    const coords = e.via ? [a.xy, e.via, b.xy] : [a.xy, b.xy];
    feats.push({ type: "Feature", id: e.id, geometry: { type: "LineString", coordinates: coords.map(f.ll) },
      properties: { feature_type: "edge", id: e.id, kind: e.kind, from: e.from, to: e.to, weight: Math.round(e.weight * 10) / 10,
        ...(e.kind === "door" ? onLevel(e.level) : { level_id: null, from_level: a.level, to_level: b.level }) } });
  }
  return { type: "FeatureCollection", name: p.venue.name, features: feats };
}

const navInfo = (c) => {
  const t = SPACE_TYPES[c.type];
  return { class: c.type, function: t.fn, usage: t.usage || "public" };
};

/**
 * IndoorGML 2.0 structure as JSON: IndoorFeatures > ThematicLayer (theme Physical) >
 * PrimalSpaceLayer (CellSpace / CellBoundary) and DualSpaceLayer (Node / Edge), with the
 * duality links between them. Geometry is GeoJSON in EPSG:4326 (lng, lat).
 */
export function toIndoorGMLJSON(p, g = buildGraph(p)) {
  const f = g.frame;
  const lv = new Map(p.levels.map((l) => [l.id, l]));
  const boundaryOfCell = new Map();
  for (const b of g.boundaries) for (const c of b.cells) boundaryOfCell.set(c, [...(boundaryOfCell.get(c) || []), b.id]);
  const edgeOfDoor = new Map(g.edges.filter((e) => e.door).map((e) => [e.door, e.id]));
  const cellSpaces = p.cells.filter((c) => c.ring.length >= 3).map((c) => ({
    featureType: SPACE_TYPES[c.type].nav || "CellSpace",
    id: c.id,
    cellSpaceName: c.name || null,
    level: lv.get(c.level)?.short ?? c.level,
    cellSpaceGeom: { geometry2D: { type: "Polygon", coordinates: [closeRing(c.ring)] } },
    duality: navigable(c) ? `N-${c.id}` : null,
    boundedBy: boundaryOfCell.get(c.id) || [],
    ...(SPACE_TYPES[c.type].nav ? navInfo(c) : {}),
    ...(c.vgroup ? { verticalGroup: c.vgroup } : {}),
  }));
  const cellBoundaries = g.boundaries.map((b) => ({
    featureType: "ConnectionBoundary",
    id: b.id,
    cellBoundaryGeom: { geometry2D: { type: "LineString", coordinates: b.seg.map(f.ll) } },
    duality: edgeOfDoor.get(b.door) || null,
    isVirtual: p.doors.find((d) => d.id === b.door)?.kind === "opening",
  }));
  const nodes = g.nodes.map((n) => ({ featureType: "Node", id: n.id, geometry: { type: "Point", coordinates: f.ll(n.xy) }, duality: n.cell }));
  const edges = g.edges.map((e) => {
    const a = g.nodeById.get(e.from);
    const b = g.nodeById.get(e.to);
    return {
      featureType: "Edge", id: e.id, weight: Math.round(e.weight * 100) / 100, connects: [e.from, e.to],
      geometry: { type: "LineString", coordinates: (e.via ? [a.xy, e.via, b.xy] : [a.xy, b.xy]).map(f.ll) },
      duality: e.door ? `B-${e.door}` : null,
      ...(e.door ? {} : { verticalConnector: e.kind }),
    };
  });
  return {
    featureType: "IndoorFeatures",
    id: p.venue.id,
    name: p.venue.name,
    crs: "EPSG:4326",
    levels: levelsSorted(p).map((l) => ({ id: l.id, name: l.name, short: l.short, ordinal: l.ordinal, elevation: l.elevation, height: l.height })),
    thematicLayer: [{
      featureType: "ThematicLayer",
      id: "TL1",
      semanticExtension: true,
      theme: "Physical",
      primalSpace: { featureType: "PrimalSpaceLayer", id: "PS1", cellSpaceMember: cellSpaces, cellBoundaryMember: cellBoundaries },
      dualSpace: { featureType: "DualSpaceLayer", id: "DS1", isDirected: false, nodeMember: nodes, edgeMember: edges },
    }],
    interLayerConnectionMember: [],
  };
}

const xmlEsc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
// GML in EPSG:4326 lists lat before lng.
const posList = (coords) => coords.map(([lng, lat]) => `${lat} ${lng}`).join(" ");

/** The same document as IndoorGML 2.0 GML (draft: written by hand, not validated against the XSD). */
export function toIndoorGMLXml(p, g = buildGraph(p)) {
  const doc = toIndoorGMLJSON(p, g);
  const tl = doc.thematicLayer[0];
  const srs = 'srsName="http://www.opengis.net/def/crs/EPSG/0/4326" srsDimension="2"';
  const out = [];
  const w = (s) => out.push(s);
  w('<?xml version="1.0" encoding="UTF-8"?>');
  w(`<core:IndoorFeatures gml:id="${xmlEsc(doc.id)}" xmlns:core="http://www.opengis.net/indoorgml/2.0/core" xmlns:navi="http://www.opengis.net/indoorgml/2.0/navigation" xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:xlink="http://www.w3.org/1999/xlink">`);
  w(`  <gml:name>${xmlEsc(doc.name)}</gml:name>`);
  w("  <core:thematicLayer>");
  w(`    <core:ThematicLayer gml:id="${tl.id}">`);
  w("      <core:semanticExtension>true</core:semanticExtension>");
  w(`      <core:theme>${tl.theme}</core:theme>`);
  w(`      <core:primalSpace><core:PrimalSpaceLayer gml:id="${tl.primalSpace.id}">`);
  for (const c of tl.primalSpace.cellSpaceMember) {
    const ns = c.featureType === "CellSpace" ? "core" : "navi";
    w(`        <core:cellSpaceMember><${ns}:${c.featureType} gml:id="${xmlEsc(c.id)}">`);
    if (c.cellSpaceName) w(`          <gml:name>${xmlEsc(c.cellSpaceName)}</gml:name>`);
    w(`          <core:cellSpaceGeom><core:geometry2D><gml:Polygon gml:id="${xmlEsc(c.id)}-g" ${srs}><gml:exterior><gml:LinearRing><gml:posList>${posList(c.cellSpaceGeom.geometry2D.coordinates[0])}</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></core:geometry2D></core:cellSpaceGeom>`);
    if (c.duality) w(`          <core:duality xlink:href="#${xmlEsc(c.duality)}"/>`);
    for (const b of c.boundedBy) w(`          <core:boundedBy xlink:href="#${xmlEsc(b)}"/>`);
    w(`          <core:level>${xmlEsc(c.level)}</core:level>`);
    if (ns === "navi") {
      w(`          <navi:class>${xmlEsc(c.class)}</navi:class>`);
      w(`          <navi:function>${xmlEsc(c.function)}</navi:function>`);
      w(`          <navi:usage>${xmlEsc(c.usage)}</navi:usage>`);
    }
    w(`        </${ns}:${c.featureType}></core:cellSpaceMember>`);
  }
  for (const b of tl.primalSpace.cellBoundaryMember) {
    w(`        <core:cellBoundaryMember><navi:${b.featureType} gml:id="${xmlEsc(b.id)}">`);
    w(`          <core:cellBoundaryGeom><core:geometry2D><gml:LineString gml:id="${xmlEsc(b.id)}-g" ${srs}><gml:posList>${posList(b.cellBoundaryGeom.geometry2D.coordinates)}</gml:posList></gml:LineString></core:geometry2D></core:cellBoundaryGeom>`);
    if (b.duality) w(`          <core:duality xlink:href="#${xmlEsc(b.duality)}"/>`);
    w(`          <core:isVirtual>${b.isVirtual}</core:isVirtual>`);
    w(`        </navi:${b.featureType}></core:cellBoundaryMember>`);
  }
  w("      </core:PrimalSpaceLayer></core:primalSpace>");
  w(`      <core:dualSpace><core:DualSpaceLayer gml:id="${tl.dualSpace.id}">`);
  w("        <core:isDirected>false</core:isDirected>");
  for (const n of tl.dualSpace.nodeMember) {
    const [lng, lat] = n.geometry.coordinates;
    w(`        <core:nodeMember><core:Node gml:id="${xmlEsc(n.id)}"><core:geometry><gml:Point gml:id="${xmlEsc(n.id)}-g" ${srs}><gml:pos>${lat} ${lng}</gml:pos></gml:Point></core:geometry><core:duality xlink:href="#${xmlEsc(n.duality)}"/></core:Node></core:nodeMember>`);
  }
  for (const e of tl.dualSpace.edgeMember) {
    const dual = e.duality ? `<core:duality xlink:href="#${xmlEsc(e.duality)}"/>` : "";
    w(`        <core:edgeMember><core:Edge gml:id="${xmlEsc(e.id)}"><core:weight>${e.weight}</core:weight><core:connects xlink:href="#${xmlEsc(e.connects[0])}"/><core:connects xlink:href="#${xmlEsc(e.connects[1])}"/><core:geometry><gml:LineString gml:id="${xmlEsc(e.id)}-g" ${srs}><gml:posList>${posList(e.geometry.coordinates)}</gml:posList></gml:LineString></core:geometry>${dual}</core:Edge></core:edgeMember>`);
  }
  w("      </core:DualSpaceLayer></core:dualSpace>");
  w("    </core:ThematicLayer>");
  w("  </core:thematicLayer>");
  w("</core:IndoorFeatures>");
  return out.join("\n");
}

// ---------- demo ----------

/**
 * A sample shopping centre to show the workflow: basement parking, 3 retail levels on a central
 * corridor with a plaza and escalator in the middle, two cores (elevator, stairs, WC) and two
 * entrances. It fills a w × h m rectangle turned `angle` radians at `center`: a picked building's
 * box, or the map centre.
 */
export function demoProject({ center = [105.8342, 21.0278], w = 120, h = 64, angle = 0, name = "TTTM mẫu (PoC)" } = {}) {
  const p = emptyProject(center);
  p.venue = { ...p.venue, name, category: "mall", address: "Dữ liệu mẫu" };
  const f = frame(center);
  const [cos, sin] = [Math.cos(angle), Math.sin(angle)];
  const ll = ([x, y]) => f.ll([x * cos - y * sin, x * sin + y * cos]);
  const rect = (x0, y0, x1, y1) => [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map(ll);
  const W = w / 2;
  const H = h / 2;
  const cw = Math.min(4, H / 5);    // half corridor width
  const core = Math.min(12, W / 4); // core block length at each end
  const cd = Math.min(8, (H - cw) / 2); // core depth (elevator / stairs / WC), staff room behind
  const plaza = Math.min(8, W / 6); // half width of the central plaza
  const pd = Math.min(12, (H - cw) / 2); // plaza depth
  p.venue.footprint = rect(-W, -H, W, H);
  p.levels = [
    { id: "L1", name: "Tầng hầm B1", short: "B1", ordinal: -1, elevation: -3.6, height: 3.6 },
    { id: "L2", name: "Tầng 1", short: "1", ordinal: 0, elevation: 0, height: 5 },
    { id: "L3", name: "Tầng 2", short: "2", ordinal: 1, elevation: 5, height: 4.5 },
    { id: "L4", name: "Tầng 3", short: "3", ordinal: 2, elevation: 9.5, height: 4.5 },
  ];
  let nc = 0;
  let nd = 0;
  const cell = (level, type, cname, ring, extra = {}) => p.cells.push({ id: `C${++nc}`, level, type, name: cname, ring, ...extra });
  const door = (level, x, y, kind = "door", width = 1.6) => p.doors.push({ id: `D${++nd}`, level, at: ll([x, y]), kind, width });
  const names = {
    L2: ["Thời trang nữ", "Giày dép", "Mỹ phẩm", "Đồng hồ", "Điện thoại", "Trang sức", "Túi xách", "Nhà thuốc", "Cà phê", "Ngân hàng", "Quà tặng", "Kính mắt", "Nước hoa", "Đồ da", "Thời trang công sở", "Áo dài", "Tiệm bánh", "Hoa tươi", "Đổi ngoại tệ", "Đồ ngủ"],
    L3: ["Thời trang nam", "Thể thao", "Nội thất", "Sách", "Đồ chơi", "Mẹ & bé", "Điện máy", "Trẻ em", "Văn phòng phẩm", "Đồ lót", "Du lịch", "Âm nhạc", "Gia dụng", "Máy tính", "Máy ảnh", "Đồ gốm", "Chăn ga", "Xe đạp", "Thú cưng", "Spa"],
    L4: ["Phở", "Lẩu", "Bánh mì", "Trà sữa", "Cơm tấm", "Bún chả", "Kem", "Sushi", "Gà rán", "Bún bò", "Pizza", "Dimsum", "Bánh cuốn", "Nướng", "Chè", "Nước ép", "Khu vui chơi", "Rạp chiếu phim", "Bowling"],
  };
  // The two cores: west on the north side, east on the south side. s = which side of the corridor.
  const cores = [{ x0: -W, s: 1, tag: "A" }, { x0: W - core, s: -1, tag: "B" }];
  const yr = (s, a, b) => [s * a, s * b].sort((u, v) => u - v);  // [ymin, ymax] of a band a..b off the axis
  for (const lv of p.levels) {
    const L = lv.id;
    const k = core / 3;
    for (const c of cores) {
      const [y0, y1] = yr(c.s, cw, cw + cd);
      const [b0, b1] = yr(c.s, cw + cd, H);
      cell(L, "elevator", `Thang máy ${c.tag}`, rect(c.x0, y0, c.x0 + k, y1), { vgroup: `TM-${c.tag}` });
      cell(L, "stairs", `Thang bộ ${c.tag}`, rect(c.x0 + k, y0, c.x0 + 2 * k, y1), { vgroup: `TB-${c.tag}` });
      cell(L, L === "L1" ? "staff" : "restroom", L === "L1" ? `Phòng bơm ${c.tag}` : `WC ${lv.short}${c.tag}`, rect(c.x0 + 2 * k, y0, c.x0 + core, y1));
      cell(L, "staff", `Kho kỹ thuật ${lv.short}${c.tag}`, rect(c.x0, b0, c.x0 + core, b1));
      for (let i = 0; i < 3; i++) door(L, c.x0 + k * (i + 0.5), c.s * cw, "door", i === 0 ? 1.8 : 1.4);
      door(L, c.x0 + 1.5 * k, c.s * (cw + cd), "door", 1.2);  // the store room opens on the stairwell
    }
    if (L === "L1") {
      // Basement: parking everywhere but the two core corners, and the car ramp on the east wall.
      cell(L, "parking", "Bãi đỗ xe B1", [[-W, -H], [W - core, -H], [W - core, -cw], [W, -cw], [W, H], [-W + core, H], [-W + core, cw], [-W, cw]].map(ll));
      door(L, W, (cw + H) / 2, "door", 7);
      continue;
    }
    // Corridor with a plaza bulging north in the middle; on level 1 the ends are the entrances.
    const e = L === "L2" ? core * 0.7 : 0;
    cell(L, "corridor", L === "L2" ? "Sảnh & hành lang tầng 1" : `Hành lang tầng ${lv.short}`,
      [[-W + e, -cw], [W - e, -cw], [W - e, cw], [plaza, cw], [plaza, cw + pd], [-plaza, cw + pd], [-plaza, cw], [-W + e, cw]].map(ll));
    if (e) {
      cell(L, "entrance", "Lối vào Tây", rect(-W, -cw, -W + e, cw));
      cell(L, "entrance", "Lối vào Đông", rect(W - e, -cw, W, cw));
      door(L, -W + e, 0, "opening", 2 * cw);
      door(L, W - e, 0, "opening", 2 * cw);
      door(L, -W, 0, "door", 6);
      door(L, W, 0, "door", 6);
    }
    // Plaza: the escalator behind it, a small shop on either side.
    cell(L, "escalator", "Thang cuốn", rect(-plaza / 4, cw + pd, plaza / 4, H), { vgroup: "TC" });
    door(L, 0, cw + pd, "opening", plaza / 2);
    const pool = [...names[L]];
    let unit = 0;
    const shop = (x0, x1, y0, y1, doorY, type) => {
      // Past the list, names come round again numbered ("Phở 2").
      const kind = names[L][unit % names[L].length];
      const nm = pool.shift() || `${kind} ${Math.floor(unit / names[L].length) + 1}`;
      unit++;
      const t = type || (L === "L4" && !["Rạp chiếu phim", "Khu vui chơi", "Bowling"].includes(kind) ? "food" : kind === "Ngân hàng" ? "service" : "shop");
      cell(L, t, `${lv.short}-${String(unit).padStart(2, "0")} · ${t === "supermarket" ? "Siêu thị" : nm}`, rect(x0, y0, x1, y1));
      door(L, (x0 + x1) / 2, doorY, "door", t === "supermarket" ? 6 : 2);
    };
    shop(-plaza, -plaza / 4, cw + pd, H, cw + pd);
    shop(plaza / 4, plaza, cw + pd, H, cw + pd);
    // Rows of units along the corridor, split into even widths of about 11 m.
    const row = (x0, x1, s) => {
      const [y0, y1] = yr(s, cw, H);
      const n = Math.max(1, Math.round((x1 - x0) / 11));
      for (let i = 0; i < n; i++) shop(x0 + ((x1 - x0) * i) / n, x0 + ((x1 - x0) * (i + 1)) / n, y0, y1, s * cw);
    };
    row(-W + core, -plaza, 1);
    row(plaza, W, 1);
    if (L === "L2") {
      // The supermarket anchors level 1.
      const sx = Math.min(36, W);
      shop(-W, -W + sx, -H, -cw, -cw, "supermarket");
      row(-W + sx, W - core, -1);
    } else {
      row(-W, W - core, -1);
    }
  }
  p.pois.push({ id: "P1", level: "L2", type: "info", name: "Quầy thông tin", at: ll([0, cw + 2]) });
  p.pois.push({ id: "P2", level: "L2", type: "atm", name: "ATM", at: ll([plaza + 3, cw - 1]) });
  p.pois.push({ id: "P3", level: "L1", type: "exit", name: "Lối thoát hiểm", at: ll([-W + core + 2, H - 2]) });
  return p;
}
