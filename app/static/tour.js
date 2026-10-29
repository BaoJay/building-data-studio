// "Bay quanh toà cao nhất" on /preview: the camera flies to the tallest buildings in view, one
// after another, and circles each. The ranking (tallest) and the camera of each stop (stopCamera)
// are pure, tested by tests/js/tour.test.mjs; runTour drives a MapLibre map through them.

const M_PER_DEG = 111_320;
const ORBIT_DEG = 120;     // how far the camera circles a building…
const ORBIT_MS = 8000;     // …and how long it takes
const HOLD_MS = 4000;      // reduced motion: a still view per building instead
const PITCH = 60;
// Pixels below the centre where the foot of the building sits, so the tower rises into the frame.
const FOOT_OFFSET_Y = 90;

const truthy = (v) => v === true || v === 1 || v === "1" || v === "true";

function eachCoord(geom, fn) {
  const walk = (c) => (typeof c[0] === "number" ? fn(c) : c.forEach(walk));
  if (geom?.coordinates) walk(geom.coordinates);
}

export function distanceM([x0, y0], [x1, y1]) {
  const k = Math.cos((((y0 + y1) / 2) * Math.PI) / 180);
  return Math.hypot((x1 - x0) * k, y1 - y0) * M_PER_DEG;
}

/** The n tallest buildings among tile features (map.querySourceFeatures), tallest first:
 *  [{id, name, height, bbox, center}]. The pieces of a building cut by tile edges, and the parts
 *  of a multi-part building (parentAttr), count as one building. Outliers (h_outlier) are left
 *  out, and so is a building closer than minGapM to a taller one already picked (twin towers are
 *  one stop). within: [w, s, e, n] the centre must fall in. */
export function tallest(features, { heightAttr, idCol, parentAttr, n = 6, minGapM = 120, within = null } = {}) {
  const groups = new Map();
  for (const f of features) {
    const p = f.properties || {};
    if (truthy(p.h_outlier)) continue;
    const h = Number(p[heightAttr]);
    if (!Number.isFinite(h) || h <= 0) continue;
    const parent = parentAttr && p[parentAttr] != null && p[parentAttr] !== "" ? String(p[parentAttr]) : null;
    const own = idCol && p[idCol] != null && p[idCol] !== "" ? String(p[idCol]) : null;
    const id = parent || own || (f.id != null ? `#${f.id}` : null);
    if (!id) continue;
    let g = groups.get(id);
    if (!g) groups.set(id, (g = { id, name: "", height: 0, bbox: [Infinity, Infinity, -Infinity, -Infinity] }));
    g.height = Math.max(g.height, h);
    if (!g.name && p.name) g.name = String(p.name);
    eachCoord(f.geometry, ([x, y]) => {
      g.bbox = [Math.min(g.bbox[0], x), Math.min(g.bbox[1], y), Math.max(g.bbox[2], x), Math.max(g.bbox[3], y)];
    });
  }
  const ranked = [...groups.values()]
    .filter((g) => Number.isFinite(g.bbox[0]))
    .map((g) => ({ ...g, center: [(g.bbox[0] + g.bbox[2]) / 2, (g.bbox[1] + g.bbox[3]) / 2] }))
    .filter((g) => !within || (g.center[0] >= within[0] && g.center[0] <= within[2] && g.center[1] >= within[1] && g.center[1] <= within[3]))
    .sort((a, b) => b.height - a.height || (a.id < b.id ? -1 : 1));
  const out = [];
  for (const g of ranked) {
    if (out.length >= n) break;
    if (!out.some((o) => distanceM(o.center, g.center) < minGapM)) out.push(g);
  }
  return out;
}

/** The camera that frames one building: closer for a low one, farther for a tower. */
export function stopCamera(stop, bearing = 0) {
  const zoom = Math.min(18, Math.max(15, 16.6 - Math.log2(stop.height / 100)));
  return { center: stop.center, zoom, pitch: PITCH, bearing, offset: [0, FOOT_OFFSET_Y] };
}

// Starts an animation and resolves when it ends. A running animation is stopped first (stopping
// fires its moveend, which must not count as ours), and the listener goes on before the start,
// since a camera change without animation (reduced motion) ends before easeTo / flyTo return.
function animate(map, signal, start) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(signal.reason); return; }
    map.stop();
    const done = () => { signal.removeEventListener("abort", stop); resolve(); };
    const stop = () => { map.off("moveend", done); reject(signal.reason); };
    signal.addEventListener("abort", stop, { once: true });
    map.once("moveend", done);
    start();
  });
}

function wait(ms, signal) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => { clearTimeout(t); reject(signal.reason); }, { once: true });
  });
}

/** Flies to each stop and circles it; onStop(stop, i) before each flight. Rejects with the
 *  signal's reason once aborted (the caller decides whether to stop the camera). */
export async function runTour(map, stops, { signal, onStop = () => {}, reduced = false }) {
  for (const [i, stop] of stops.entries()) {
    onStop(stop, i);
    const cam = stopCamera(stop, map.getBearing());
    if (reduced) {
      await animate(map, signal, () => map.easeTo({ ...cam, duration: 0 }));
      await wait(HOLD_MS, signal);
      continue;
    }
    await animate(map, signal, () => map.flyTo({ ...cam, speed: 0.8, curve: 1.42, maxDuration: 7000, essential: true }));
    await animate(map, signal, () => map.easeTo({
      center: cam.center, offset: cam.offset, bearing: cam.bearing + ORBIT_DEG, duration: ORBIT_MS, easing: (t) => t, essential: true,
    }));
  }
}

/** Back to where the tour started. */
export function flyHome(map, home, signal, reduced = false) {
  return animate(map, signal, () => (reduced ? map.jumpTo(home) : map.flyTo({ ...home, speed: 1.1, essential: true })));
}
