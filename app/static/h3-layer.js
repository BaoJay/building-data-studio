// H3 r8 cells over the /preview map, to split the QC of a release between people: a grid of
// the view, a pasted list of cells (or the preset of the release), and a selection made by
// clicking cells, copied as "id, id, …". Cells come from the server (app/h3grid.py) as GeoJSON.
import { $, fetchOk, toast } from "/static/util.js";
import { recall, remember } from "/static/basemap.js";

const GRID_MINZOOM = 11.5;
const COLORS = { grid: "#1f9d55", list: "#2563eb", sel: "#7c3aed" };
const EMPTY = { type: "FeatureCollection", features: [] };

const h3 = {
  map: null,
  grid: EMPTY,
  list: EMPTY,
  feats: new Map(),  // cell -> feature, of every cell seen (grid or list), to draw the selection
  selected: new Set(), // in click order
  presets: [],
  gridSeq: 0,
  gridTimer: null,
};

export async function bindH3(map) {
  h3.map = map;
  try { h3.selected = new Set(JSON.parse(recall("h3.selected") || "[]")); } catch { /* fresh start */ }
  $("#h3Input").value = recall("h3.input") || "";
  $("#h3Grid").checked = recall("h3.grid") === "1";
  $("#h3Pick").checked = recall("h3.pick") === "1";

  $("#h3Grid").addEventListener("change", () => { remember("h3.grid", $("#h3Grid").checked ? "1" : "0"); syncGrid(); });
  $("#h3Pick").addEventListener("change", () => {
    remember("h3.pick", $("#h3Pick").checked ? "1" : "0");
    // Picking needs cells to click on: the grid, unless a list is shown.
    if ($("#h3Pick").checked && !$("#h3Grid").checked && !h3.list.features.length) {
      $("#h3Grid").checked = true;
      remember("h3.grid", "1");
      syncGrid();
    }
    syncNotes();
  });
  $("#h3Show").addEventListener("click", () => showList($("#h3Input").value, true));
  $("#h3ClearList").addEventListener("click", () => { $("#h3Input").value = ""; showList("", false); });
  $("#h3Copy").addEventListener("click", copySelection);
  $("#h3SelectList").addEventListener("click", () => {
    h3.list.features.forEach((f) => h3.selected.add(f.properties.h3));
    syncSelection();
  });
  $("#h3ClearSel").addEventListener("click", () => { h3.selected.clear(); syncSelection(); });
  $("#h3Preset").addEventListener("click", () => {
    const p = h3.presets[0];
    if (!p) return;
    $("#h3Input").value = p.cells.join(", ");
    showList($("#h3Input").value, true);
  });

  map.on("moveend", () => { clearTimeout(h3.gridTimer); h3.gridTimer = setTimeout(syncGrid, 250); });
  map.on("mousemove", (e) => {
    const cell = cellAt(e.point);
    $("#h3Hover").textContent = cell ? `Ô dưới chuột: ${cell}` : "";
    if ($("#h3Pick").checked) map.getCanvas().style.cursor = cell ? "pointer" : "";
  });

  fetchOk("/api/h3/presets").then((r) => r.json()).then((d) => {
    h3.presets = d.presets || [];
    const p = h3.presets[0];
    $("#h3Preset").hidden = !p;
    if (p) $("#h3Preset").textContent = `Nạp ${p.label}`;
  }, () => { $("#h3Preset").hidden = true; });

  ensureH3Layers();
  syncGrid();
  // The pasted list of the last visit comes back without moving the map.
  if ($("#h3Input").value.trim()) await showList($("#h3Input").value, false);
  else syncSelection();
}

// After the basemap style is swapped every source and layer is gone: add them back on top.
export function ensureH3Layers() {
  const map = h3.map;
  if (!map) return;
  for (const [id, data] of [["h3-grid", h3.grid], ["h3-list", h3.list], ["h3-sel", selectionData()]]) {
    if (!map.getSource(id)) map.addSource(id, { type: "geojson", data });
  }
  const add = (layer) => { if (!map.getLayer(layer.id)) map.addLayer(layer); };
  // Fills at 0 opacity still answer queryRenderedFeatures: they are what a click hits.
  add({ id: "h3-grid-fill", type: "fill", source: "h3-grid", paint: { "fill-color": COLORS.grid, "fill-opacity": 0 } });
  add({ id: "h3-grid-line", type: "line", source: "h3-grid", paint: { "line-color": COLORS.grid, "line-width": 1.6, "line-opacity": 0.85 } });
  add({ id: "h3-list-fill", type: "fill", source: "h3-list", paint: { "fill-color": COLORS.list, "fill-opacity": 0.12 } });
  add({ id: "h3-list-line", type: "line", source: "h3-list", paint: { "line-color": COLORS.list, "line-width": 2.2 } });
  add({ id: "h3-sel-fill", type: "fill", source: "h3-sel", paint: { "fill-color": COLORS.sel, "fill-opacity": 0.32 } });
  add({ id: "h3-sel-line", type: "line", source: "h3-sel", paint: { "line-color": COLORS.sel, "line-width": 2.6 } });
  // Kept above the buildings, which are re-added when the layer or the basemap changes.
  for (const id of ["h3-grid-fill", "h3-grid-line", "h3-list-fill", "h3-list-line", "h3-sel-fill", "h3-sel-line"]) map.moveLayer(id);
  syncGridVisibility();
}

// A click toggles the cell under it while "Click để chọn ô" is on; true when it was handled.
export function h3OnClick(e) {
  if (!$("#h3Pick").checked) return false;
  const cell = cellAt(e.point);
  if (!cell) {
    toast("Không có ô H3 ở đây: bật lưới H3 (phóng to ≥ 11.5) hoặc dán danh sách ô.");
    return true;
  }
  if (h3.selected.has(cell)) h3.selected.delete(cell); else h3.selected.add(cell);
  syncSelection();
  return true;
}

function cellAt(point) {
  const map = h3.map;
  const layers = ["h3-sel-fill", "h3-list-fill", "h3-grid-fill"].filter((id) => map.getLayer(id) && map.getLayoutProperty(id, "visibility") !== "none");
  if (!layers.length) return null;
  return map.queryRenderedFeatures(point, { layers })[0]?.properties.h3 || null;
}

function keep(feats) { feats.forEach((f) => h3.feats.set(f.properties.h3, f)); }

function selectionData() {
  return { type: "FeatureCollection", features: [...h3.selected].map((c) => h3.feats.get(c)).filter(Boolean) };
}

async function showList(text, fit) {
  remember("h3.input", text);
  let data = EMPTY;
  let invalid = [];
  if (text.trim()) {
    try {
      const res = await fetchOk("/api/h3/cells", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cells: text }) });
      data = await res.json();
      invalid = data.invalid || [];
    } catch (e) {
      $("#h3ListNote").textContent = `Không đọc được danh sách ô: ${e.message}`;
      return;
    }
  }
  h3.list = { type: "FeatureCollection", features: data.features };
  keep(h3.list.features);
  h3.map.getSource("h3-list")?.setData(h3.list);
  const n = h3.list.features.length;
  const res = [...new Set(h3.list.features.map((f) => f.properties.res))];
  $("#h3ListNote").textContent = !text.trim() ? "" : `${n} ô${res.length ? ` (r${res.join(", r")})` : ""} trên bản đồ`
    + (invalid.length ? ` · bỏ qua ${invalid.length} giá trị không phải ID H3: ${invalid.slice(0, 5).join(", ")}${invalid.length > 5 ? "…" : ""}` : "");
  $("#h3SelectList").disabled = !n;
  if (fit && n) fitTo(h3.list.features);
  await fetchMissing();
  syncSelection();
}

function fitTo(features) {
  let w = 180, s = 90, e = -180, n = -90;
  for (const f of features) for (const [x, y] of f.geometry.coordinates[0]) {
    w = Math.min(w, x); e = Math.max(e, x); s = Math.min(s, y); n = Math.max(n, y);
  }
  h3.map.fitBounds([[w, s], [e, n]], { padding: 60, pitch: 0, bearing: 0, maxZoom: 15, duration: 800 });
}

// Selected cells restored from an earlier visit may be neither in the grid nor in the list yet.
async function fetchMissing() {
  const missing = [...h3.selected].filter((c) => !h3.feats.has(c));
  if (!missing.length) return;
  try {
    const res = await fetchOk("/api/h3/cells", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cells: missing.join(" ") }) });
    keep((await res.json()).features);
  } catch { /* drawn once the grid reaches them */ }
}

function syncGridVisibility() {
  const on = $("#h3Grid").checked && h3.map.getZoom() >= GRID_MINZOOM;
  for (const id of ["h3-grid-fill", "h3-grid-line"]) {
    if (h3.map.getLayer(id)) h3.map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
  }
  return on;
}

async function syncGrid() {
  const map = h3.map;
  const on = syncGridVisibility();
  const seq = ++h3.gridSeq;
  if (!on) {
    $("#h3GridNote").textContent = $("#h3Grid").checked ? `Phóng to (≥ ${GRID_MINZOOM}) để hiện lưới H3 r8.` : "";
    return;
  }
  const b = map.getBounds();
  const bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map((v) => v.toFixed(5)).join(",");
  try {
    const res = await fetch(`/api/h3/grid?res=8&bbox=${bbox}`);
    const data = await res.json();
    if (seq !== h3.gridSeq) return;
    if (!res.ok) {
      $("#h3GridNote").textContent = data.error || `HTTP ${res.status}`;
      return;
    }
    h3.grid = data;
    keep(data.features);
    map.getSource("h3-grid")?.setData(data);
    $("#h3GridNote").textContent = `Lưới r8: ${data.features.length} ô trong khung nhìn.`;
    if (h3.selected.size) map.getSource("h3-sel")?.setData(selectionData());
  } catch (e) {
    if (seq === h3.gridSeq) $("#h3GridNote").textContent = `Không tải được lưới H3: ${e.message}`;
  }
}

function syncSelection() {
  remember("h3.selected", JSON.stringify([...h3.selected]));
  h3.map.getSource("h3-sel")?.setData(selectionData());
  const n = h3.selected.size;
  $("#h3Copy").textContent = `Copy ${n} ô đã chọn`;
  $("#h3Copy").disabled = !n;
  $("#h3ClearSel").disabled = !n;
  syncNotes();
}

function syncNotes() {
  const n = h3.selected.size;
  const inList = h3.list.features.length ? [...h3.selected].filter((c) => h3.list.features.some((f) => f.properties.h3 === c)).length : null;
  $("#h3SelNote").textContent = !n
    ? ($("#h3Pick").checked ? "Click ô trên bản đồ để chọn / bỏ chọn." : "Bật “Click để chọn ô” rồi click lên bản đồ.")
    : `${n} ô đã chọn${inList != null ? ` (${inList} trong danh sách đã dán)` : ""}.`;
}

async function copySelection() {
  const text = [...h3.selected].join(", ");
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = Object.assign(document.createElement("textarea"), { value: text });
    document.body.append(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast(`Đã copy ${h3.selected.size} ô H3.`);
}
