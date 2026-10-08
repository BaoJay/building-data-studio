// "So sánh 2 file" tab: pick A (production) and B (release candidate), set the release gate, read the diff report.

import { $, $$, api, esc, fmtBytes, fmtInt, fmtNum, hbars, loadJSON, previewUrl, saveJSON, section, toast, uploadFile } from "./util.js";

const PREF_KEY = "building-data-studio.diff.v1";
const SIDES = ["a", "b"];
const SIDE_NAME = { a: "A (production)", b: "B (release)" };
// Columns that change on every build (same list as app/diffgate.py DEFAULT_IGNORE_COLS).
const DEFAULT_IGNORE = ["build_id", "updated_at", "updated_by", "description", "winning_observation_id", "config_version"];
const SUPERSEDED = ["superseded_by", "superseded_by_id", "replaced_by"];
const KEY_TYPES = ["String", "Integer", "Integer64", "Real"]; // PMTiles store numeric IDs as Real
// name: [label, unit, default, step]
const THRESHOLDS = {
  fail_count_pct: ["Số building lệch quá", "%", 10, 0.5],
  fail_removed_pct: ["Building bị xoá quá", "% của A", 5, 0.5],
  warn_count_pct: ["Số building lệch quá", "%", 2, 0.5],
  warn_removed_pct: ["Building bị xoá quá", "% của A", 1, 0.5],
  warn_geom_major_pct: ["Đổi geometry đáng kể quá", "% building khớp", 2, 0.5],
  warn_real_drop_pts: ["% có chiều cao thật giảm quá", "điểm %", 1, 0.5],
  warn_null_increase_pts: ["Tỉ lệ null của 1 cột tăng quá", "điểm %", 5, 1],
  geom_area_pct: ["Geometry đáng kể: diện tích lệch >", "%", 20, 1],
  geom_shift_m: ["… hoặc tâm dịch >", "m", 10, 1],
  height_tol_m: ["Chiều cao đổi khi lệch >", "m", 0.5, 0.1],
};
const GROUPS = [
  ["Fail — chặn release", ["fail_count_pct", "fail_removed_pct"]],
  ["Warn — cần xem lại", ["warn_count_pct", "warn_removed_pct", "warn_geom_major_pct", "warn_real_drop_pts", "warn_null_increase_pts"]],
  ["Thế nào là “có thay đổi”", ["geom_area_pct", "geom_shift_m", "height_tol_m"]],
];
const GRID_CHOICES = [[0.01, "0,01° (~1 km)"], [0.05, "0,05° (~5,5 km)"], [0.1, "0,1° (~11 km)"], [0.25, "0,25° (~28 km)"]];
const COLUMN_ROLES = [
  ["height_col", "Cột chiều cao (m)"], ["levels_col", "Cột số tầng"], ["prov_col", "Cột nguồn chiều cao"],
  ["parent_col", "Cột ID cha (khối con)"], ["superseded_col", "Cột superseded_by"],
];
const DEFAULTS = {
  thresholds: Object.fromEntries(Object.entries(THRESHOLDS).map(([k, v]) => [k, v[2]])),
  fail_layer_change: true, grid_deg: 0.05, want_tiles: true, keep_temp: false, out_dir: "", last_a: "",
};
const VERDICT = { pass: "✓ Pass", warn: "! Warn", fail: "✕ Fail" };
const VERDICT_TEXT = { pass: "Không vi phạm ngưỡng nào", warn: "Có điểm cần xem lại trước khi release", fail: "Không nên release" };
const LEVEL_LABEL = { fail: "Fail", warn: "Warn", info: "Info" };
const levelBadge = (level) => `<span class="badge ${level === "info" ? "" : `st-${level}`}">${LEVEL_LABEL[level]}</span>`;
const GEOM_CLASSES = [["identical", "Giống hệt"], ["minor", "Khác nhỏ"], ["moderate", "Khác vừa"], ["major", "Đáng kể"]];

const ds = {
  a: null, b: null, health: null, ctx: null, prefs: { ...DEFAULTS, ...loadJSON(PREF_KEY) },
};

// ------------------------------------------------------------------ init
export function initDiff(ctx) {
  ds.ctx = ctx;
  ds.prefs.thresholds = { ...DEFAULTS.thresholds, ...(ds.prefs.thresholds || {}) };
  SIDES.forEach(setupSlot);
  if (ds.prefs.last_a) $('.slot[data-side="a"] .path-row input').value = ds.prefs.last_a;
  $("#swapBtn").addEventListener("click", swap);
  $("#diffRunBtn").addEventListener("click", run);
  $("#diffResetBtn").addEventListener("click", () => {
    ds.prefs = { ...DEFAULTS, thresholds: { ...DEFAULTS.thresholds }, last_a: ds.prefs.last_a, out_dir: ds.health?.defaults.output_dir || "" };
    savePrefs();
    renderForm();
  });
}

export function setDiffHealth(health) {
  ds.health = health;
  if (!ds.prefs.out_dir) ds.prefs.out_dir = health.defaults.output_dir;
  $$(".slot input[type=file]").forEach((i) => { i.accept = health.accept.join(","); });
}

function savePrefs() { saveJSON(PREF_KEY, ds.prefs); }

// ------------------------------------------------------------------ slots
const slot = (side) => $(`.slot[data-side="${side}"]`);

function setupSlot(side) {
  const el = slot(side);
  const dz = $(".dropzone", el);
  const input = $("input[type=file]", el);
  const path = $(".path-row input", el);
  dz.addEventListener("click", () => input.click());
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  input.addEventListener("change", () => { if (input.files[0]) handleFile(side, input.files[0]); input.value = ""; });
  ["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => dz.addEventListener(t, () => dz.classList.remove("over")));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    if (e.dataTransfer.files[0]) handleFile(side, e.dataTransfer.files[0]);
  });
  const open = () => {
    const value = path.value.trim().replace(/^["']|["']$/g, "");
    if (value) probe(side, value); else toast(`Nhập đường dẫn file ${SIDE_NAME[side]}.`);
  };
  $(".path-row button", el).addEventListener("click", open);
  path.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
}

async function handleFile(side, file) {
  const ext = `.${file.name.split(".").pop().toLowerCase()}`;
  if (ds.health && !ds.health.accept.includes(ext)) { toast(`Định dạng ${ext} chưa hỗ trợ.`); return; }
  const el = slot(side);
  const status = $(".upload-status", el);
  const bar = $(".progress > span", status);
  status.hidden = false;
  $(".up-text", status).textContent = `Đang upload ${file.name} (${fmtBytes(file.size)}) vào uploads/…`;
  try {
    const res = await uploadFile(file, (pct) => { bar.style.width = `${pct}%`; });
    status.hidden = true;
    $(".path-row input", el).value = res.path;
    await probe(side, res.path);
  } catch (e) {
    $(".up-text", status).textContent = `Lỗi upload: ${e.message}`;
  }
}

async function probe(side, path, extra = {}) {
  const info = $(".slot-info", slot(side));
  info.innerHTML = `<div class="upload-status"><div>Đang đọc file…</div><div class="progress indeterminate"><span></span></div></div>`;
  try {
    ds[side] = await api("/api/probe", { method: "POST", body: { path, ...extra } });
    renderSlot(side);
  } catch (e) {
    ds[side] = null;
    info.innerHTML = `<div class="alert bad"><span class="icon">!</span><div>${esc(e.message)}</div></div>`;
  }
  slot(side).classList.toggle("ready", Boolean(ds[side]));
  renderForm();
}

function renderSlot(side) {
  const p = ds[side];
  const rows = [];
  const kv = (k, v) => rows.push(`<dt>${esc(k)}</dt><dd>${v}</dd>`);
  kv("File", `${esc(p.name)} <span class="hint">· ${fmtBytes(p.size)}</span>`);
  if (p.kind === "pmtiles") {
    const h = p.header || {};
    kv("Định dạng", `PMTiles · z${h.minzoom ?? "?"}–${h.maxzoom ?? "?"}`);
    kv("Building", p.feature_count != null ? `${fmtInt(p.feature_count)} <span class="hint">(tilestats)</span>` : "—");
  } else {
    kv("Định dạng", esc(p.driver || "—"));
    kv("Building", fmtInt(p.feature_count));
    const crs = p.crs.missing
      ? `<input type="text" class="crs-input" value="${esc(p.crs.suggested || "")}" placeholder="vd: EPSG:4326" aria-label="CRS nguồn ${side.toUpperCase()}">
         <div class="hint">File không ghi CRS${p.crs.suggested ? " — extent là kinh/vĩ độ nên đề xuất EPSG:4326" : ""}.</div>`
      : esc(p.crs.label);
    kv("CRS", crs);
  }
  const layers = p.layers || [];
  if (layers.length > 1) {
    kv("Layer", `<select class="layer-select">${layers.map((l) => `<option ${l === p.layer ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`);
  } else if (p.kind === "pmtiles") {
    kv("Layer", `<code>${esc(p.layer || "—")}</code>`);
  }
  kv("Field", `${fmtInt(p.fields.length)}${p.suggest?.id_col ? ` <span class="hint">· ID: <code>${esc(p.suggest.id_col)}</code></span>` : ""}`);
  const warnings = (p.warnings || []).filter((w) => !/CRS/.test(w) || p.kind === "pmtiles")
    .map((w) => `<div class="alert warn"><span class="icon">!</span><div>${esc(w)}</div></div>`).join("");
  $(".slot-info", slot(side)).innerHTML = `<dl class="kv">${rows.join("")}</dl>${warnings}`;
  $(".layer-select", slot(side))?.addEventListener("change", (e) => probe(side, p.path, { layer: e.target.value }));
}

function swap() {
  [ds.a, ds.b] = [ds.b, ds.a];
  const inputs = SIDES.map((s) => $(".path-row input", slot(s)));
  [inputs[0].value, inputs[1].value] = [inputs[1].value, inputs[0].value];
  const infos = SIDES.map((s) => $(".slot-info", slot(s)));
  [infos[0].innerHTML, infos[1].innerHTML] = [infos[1].innerHTML, infos[0].innerHTML];
  SIDES.forEach((s) => {
    if (ds[s]) renderSlot(s);
    slot(s).classList.toggle("ready", Boolean(ds[s]));
  });
  renderForm();
}

// ------------------------------------------------------------------ options
const stemTail = (name) => name.replace(/\.[^.]+$/, "").slice(-40);
const fieldNames = (p) => p.fields.map((f) => f.name);

function matchChoices(a, b) {
  const types = (p) => Object.fromEntries(p.fields.map((f) => [f.name, f.type]));
  const ta = types(a);
  const tb = types(b);
  const common = fieldNames(a).filter((n) => n in tb && KEY_TYPES.includes(ta[n]) && KEY_TYPES.includes(tb[n]));
  const rank = (n) => (n === a.suggest?.id_col ? 0 : /(^|_)(id|uuid)$/i.test(n) ? 1 : 2);
  return common.sort((x, y) => rank(x) - rank(y) || x.localeCompare(y));
}

function defaultIgnore(mode, common) {
  return new Set(mode === "loc" ? common : common.filter((c) => DEFAULT_IGNORE.includes(c)));
}

function colSelect(p, side, role, value) {
  const opts = [`<option value="">— không dùng —</option>`, ...p.fields.map((f) =>
    `<option value="${esc(f.name)}" ${f.name === value ? "selected" : ""}>${esc(f.name)} (${esc(f.type)})</option>`)];
  return `<select name="col_${side}_${role}">${opts.join("")}</select>`;
}

function renderForm() {
  const card = $("#diffOptionsCard");
  const { a, b } = ds;
  if (!a || !b) { card.hidden = true; return; }
  card.hidden = false;
  const pr = ds.prefs;
  const names = { a: fieldNames(a), b: fieldNames(b) };
  const common = names.a.filter((n) => names.b.includes(n));
  const keys = matchChoices(a, b);
  const defaultMatch = a.suggest?.id_col && a.suggest.id_col === b.suggest?.id_col ? `key:${a.suggest.id_col}` : "loc";
  const num = (name, value, step) => `<input type="number" name="${name}" value="${esc(value)}" min="0" step="${step}">`;
  const chk = (name, on) => `<input type="checkbox" name="${name}" ${on ? "checked" : ""}>`;
  const colDefault = (p, role) => (role === "superseded_col" ? SUPERSEDED.find((n) => fieldNames(p).includes(n)) || "" : p.suggest?.[role] || "");
  const columnRows = COLUMN_ROLES.map(([role, label]) => `<tr><th>${esc(label)}</th>
      <td>${colSelect(a, "a", role, colDefault(a, role))}</td><td>${colSelect(b, "b", role, colDefault(b, role))}</td></tr>`).join("")
    + `<tr><th>Giá trị = không có số liệu thật</th>
      <td><input type="text" name="col_a_prov_missing" value="${esc((a.suggest?.prov_missing || []).join(", "))}" placeholder="vd: default"></td>
      <td><input type="text" name="col_b_prov_missing" value="${esc((b.suggest?.prov_missing || []).join(", "))}" placeholder="vd: default"></td></tr>`;

  $("#diffForm").innerHTML = `
  <div class="opt-grid">
    <fieldset>
      <legend>Khớp building giữa A và B</legend>
      <label class="field"><span>Khớp theo</span>
        <select name="match">
          ${keys.map((k) => `<option value="key:${esc(k)}" ${`key:${k}` === defaultMatch ? "selected" : ""}>ID: ${esc(k)}${k === a.suggest?.id_col ? " (tự nhận)" : ""}</option>`).join("")}
          <option value="loc" ${defaultMatch === "loc" ? "selected" : ""}>Vị trí (không dùng ID)</option>
        </select>
      </label>
      <div class="formula" id="matchHint"></div>
    </fieldset>

    <fieldset>
      <legend>Output</legend>
      <label class="field"><span>Tên báo cáo</span><input type="text" name="out_name" value="${esc(`diff__${stemTail(b.name)}__vs__${stemTail(a.name)}`)}"></label>
      <label class="field"><span>Thư mục output</span><input type="text" name="out_dir" value="${esc(pr.out_dir)}"></label>
      <label class="field"><span>Ô lưới thống kê theo khu vực</span>
        <select name="grid_deg">${GRID_CHOICES.map(([v, l]) => `<option value="${v}" ${+pr.grid_deg === v ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>
      </label>
      <label class="check">${chk("want_tiles", pr.want_tiles)} Tạo PMTiles để xem bản đồ diff</label>
      <label class="check">${chk("keep_temp", pr.keep_temp)} Giữ file tạm (để debug)</label>
    </fieldset>

    <fieldset class="wide">
      <legend>Release gate — ngưỡng</legend>
      ${GROUPS.map(([title, keys]) => `
        <div class="subhead">${esc(title)}</div>
        <div class="thr-grid">${keys.map((k) => {
          const [label, unit, , step] = THRESHOLDS[k];
          return `<label class="field"><span>${esc(label)} <span class="hint">(${esc(unit)})</span></span>${num(k, pr.thresholds[k], step)}</label>`;
        }).join("")}</div>`).join("")}
      <label class="check">${chk("fail_layer_change", pr.fail_layer_change)} Đổi tên layer PMTiles là Fail (style app dùng <code>source-layer</code> cũ sẽ không hiện building)</label>
      <span class="hint">Luôn Fail: B thiếu field bắt buộc hoặc field đổi kiểu không tương thích · ID trùng / rỗng (khi khớp theo ID) · geometry NULL / rỗng · CRS hoặc kiểu geometry khác A.</span>
    </fieldset>

    <fieldset class="wide">
      <legend>Field bắt buộc phải còn ở B</legend>
      <div class="list-head"><span class="hint" id="reqCount"></span>
        <span><button type="button" class="linkbtn" data-all="req">Chọn tất cả</button> · <button type="button" class="linkbtn" data-none="req">Bỏ hết</button></span></div>
      <span class="hint">B thiếu field bắt buộc → Fail; thiếu field khác → Warn. Khi đổi schema (vd vmap → canonical), chỉ giữ các field mà app mobile thật sự dùng.</span>
      <div class="checklist scroll" id="reqList">${names.a.map((n) => `<label class="${names.b.includes(n) ? "" : "gone"}"><input type="checkbox" name="req" value="${esc(n)}" checked>${esc(n)}</label>`).join("")}</div>
    </fieldset>

    <fieldset class="wide">
      <legend>Bỏ qua khi xét building “có thay đổi”</legend>
      <div class="list-head"><span class="hint">Cột chung của A và B. Cột bị bỏ qua vẫn được đếm trong báo cáo, chỉ không làm building bị tô cam.</span>
        <span><button type="button" class="linkbtn" data-all="ign">Chọn tất cả</button> · <button type="button" class="linkbtn" data-none="ign">Bỏ hết</button></span></div>
      <div class="checklist" id="ignList">${common.length ? common.map((n) => `<label><input type="checkbox" name="ign" value="${esc(n)}">${esc(n)}</label>`).join("") : '<span class="hint">A và B không có cột chung.</span>'}</div>
    </fieldset>

    <fieldset class="wide">
      <details>
        <summary>Cột dùng để tính chiều cao &amp; chất lượng <span class="hint">(tự nhận — sửa nếu sai)</span></summary>
        <div class="tbl-wrap cols-map"><table>
          <thead><tr><th></th><th>A · ${esc(a.name)}</th><th>B · ${esc(b.name)}</th></tr></thead>
          <tbody>${columnRows}</tbody>
        </table></div>
        <span class="hint">Nếu file có sẵn cột <code>h_m</code> thì dùng luôn (đó là giá trị app đang vẽ); nếu không, chiều cao được tính theo quy tắc của bước convert (cột chiều cao → số tầng × 3,5 m → mặc định 4 m).</span>
      </details>
    </fieldset>
  </div>`;

  const form = $("#diffForm");
  const applyIgnoreDefaults = () => {
    const ignored = defaultIgnore(form.elements.match.value === "loc" ? "loc" : "key", common);
    $$('input[name="ign"]', form).forEach((cb) => { cb.checked = ignored.has(cb.value); });
  };
  form.onchange = (e) => {
    if (e.target.name === "match") applyIgnoreDefaults();
    updateHints();
  };
  form.onclick = (e) => {
    const btn = e.target.closest("[data-all], [data-none]");
    if (!btn) return;
    const name = btn.dataset.all || btn.dataset.none;
    $$(`input[name="${name}"]`, form).forEach((cb) => { cb.checked = Boolean(btn.dataset.all); });
    updateHints();
  };
  applyIgnoreDefaults();
  updateHints();
}

function updateHints() {
  const form = $("#diffForm");
  const match = form.elements.match.value;
  $("#matchHint").innerHTML = match === "loc"
    ? "Ghép building có <b>tâm gần nhau</b> (3–50 m tuỳ kích thước) và <b>diện tích lệch không quá 2 lần</b>. Dùng khi A và B không có ID chung, vd đang chuyển vmap → canonical. Số thêm / xoá là ước lượng."
    : `Building có cùng <code>${esc(match.slice(4))}</code> ở A và B được xem là một. Nếu ID không ổn định giữa 2 bản build, báo cáo sẽ đếm số cặp “xoá + thêm” nằm cùng vị trí.`;
  const req = $$('input[name="req"]', form);
  const on = req.filter((c) => c.checked).length;
  const gone = req.filter((c) => c.checked && c.parentElement.classList.contains("gone")).length;
  $("#reqCount").innerHTML = `${fmtInt(on)}/${fmtInt(req.length)} field bắt buộc${gone ? ` · <b class="bad-text">${fmtInt(gone)} field bắt buộc không có ở B</b>` : ""}`;
  const missingCrs = SIDES.some((s) => ds[s]?.crs?.missing && !$(".crs-input", slot(s))?.value.trim());
  $("#diffHint").textContent = missingCrs ? "Cần nhập CRS nguồn cho file không khai báo CRS." : "";
}

function collect() {
  const form = $("#diffForm");
  const el = form.elements;
  const val = (n) => (el[n]?.value ?? "").trim();
  const thresholds = Object.fromEntries(Object.keys(THRESHOLDS).map((k) => [k, Number(val(k))]));
  const req = $$('input[name="req"]', form);
  const reqOn = req.filter((c) => c.checked).map((c) => c.value);
  const columns = {};
  for (const s of SIDES) {
    columns[s] = Object.fromEntries(COLUMN_ROLES.map(([role]) => [role, val(`col_${s}_${role}`) || null]));
    columns[s].prov_missing = val(`col_${s}_prov_missing`).split(",").map((x) => x.trim()).filter(Boolean);
  }
  const side = (s) => ({
    path: ds[s].path,
    layer: ds[s].layer,
    src_crs: ds[s].crs?.missing ? ($(".crs-input", slot(s))?.value.trim() || null) : null,
  });
  const match = val("match");
  const prefs = {
    thresholds, fail_layer_change: el.fail_layer_change.checked, grid_deg: Number(val("grid_deg")),
    want_tiles: el.want_tiles.checked, keep_temp: el.keep_temp.checked, out_dir: val("out_dir"),
  };
  return {
    prefs,
    config: {
      kind: "diff",
      a: side("a"),
      b: side("b"),
      match: match === "loc" ? { mode: "location" } : { mode: "key", key: match.slice(4) },
      columns,
      ignore_cols: $$('input[name="ign"]:checked', form).map((c) => c.value),
      required_fields: reqOn.length === req.length ? null : reqOn,
      thresholds: { ...thresholds, fail_layer_change: prefs.fail_layer_change },
      grid_deg: prefs.grid_deg,
      output: { dir: prefs.out_dir, name: val("out_name"), pmtiles: prefs.want_tiles, keep_temp: prefs.keep_temp },
    },
  };
}

async function run() {
  if (!ds.a || !ds.b) return;
  const { config, prefs } = collect();
  for (const s of SIDES) {
    if (ds[s].crs?.missing && !config[s].src_crs) { toast(`Nhập CRS nguồn cho ${SIDE_NAME[s]}.`); return; }
  }
  ds.prefs = { ...ds.prefs, ...prefs, last_a: ds.a.path };
  savePrefs();
  const btn = $("#diffRunBtn");
  btn.disabled = true;
  try {
    const { id } = await api("/api/jobs", { method: "POST", body: config });
    ds.ctx.showJob(id, true);
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
  }
}

// ------------------------------------------------------------------ report
const pct = (n, d = 2) => (n == null ? "—" : `${fmtNum(n, d)}%`);
const signed = (n, d = 2) => (n == null ? "—" : `${n > 0 ? "+" : ""}${fmtNum(n, d)}`);

export function renderDiffReport(job) {
  const r = job.report;
  const g = r.gate;
  const m = r.match;
  const sa = r.sides.a;
  const sb = r.sides.b;
  const pm = job.outputs.pmtiles;
  const grid = job.outputs.grid;
  const mapUrl = (params = {}) => (pm ? previewUrl(pm.token, { mode: "diff", grid: grid?.token || "", name: job.name, ...params }) : null);
  const look = (lon, lat, z, id) => {
    const url = mapUrl({ lon: lon.toFixed(6), lat: lat.toFixed(6), z: String(z), ...(id ? { id } : {}) });
    return url ? `<a class="btn small" href="${url}" target="_blank" rel="noopener">Xem</a>`
      : `<span class="hint mono">${lat.toFixed(5)}, ${lon.toFixed(5)}</span>`;
  };
  $("#diffReportCard").hidden = false;

  const deltaPct = sa.buildings ? (100 * (sb.buildings - sa.buildings)) / sa.buildings : null;
  const counts = `${g.counts.fail} fail · ${g.counts.warn} warn · ${g.counts.info} thông tin`;
  let html = `
    <div class="verdict ${g.verdict}">
      <span class="badge st-${g.verdict}">${VERDICT[g.verdict]}</span>
      <div class="grow"><div class="vt">${esc(VERDICT_TEXT[g.verdict])}</div>
        <div class="meta">${esc(counts)} · khớp theo ${m.mode === "key" ? `ID <code>${esc(m.key_a)}</code>` : "vị trí"}</div></div>
      ${mapUrl() && job.status === "done" ? `<a class="btn primary" href="${mapUrl()}" target="_blank" rel="noopener">Xem bản đồ diff</a>` : ""}
    </div>
    <div class="ab-names"><span><span class="side-badge a sm">A</span> ${esc(sa.name)}</span><span>→</span><span><span class="side-badge b sm">B</span> ${esc(sb.name)}</span></div>`;

  html += `<div class="findings">${g.findings.length ? g.findings.map((f) => `
      <div class="finding">${levelBadge(f.level)}
        <div><div class="t">${esc(f.title)}</div>${f.detail ? `<div class="d">${esc(f.detail)}</div>` : ""}</div></div>`).join("")
    : `<div class="finding"><span class="badge st-pass">Pass</span><div class="t">Không vi phạm ngưỡng nào.</div></div>`}</div>`;

  const kpi = (label, value, sub = "", cls = "") => `<div class="kpi ${cls}"><div class="label">${esc(label)}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
  html += `<div class="kpis diff-kpis">
    ${kpi("Building A", fmtInt(sa.buildings), esc(sa.kind === "pmtiles" ? `PMTiles z${sa.zoom}` : sa.driver || ""))}
    ${kpi("Building B", fmtInt(sb.buildings), `${signed(deltaPct)}% so với A`)}
    ${kpi("Khớp", fmtInt(m.matched), `không đổi ${fmtInt(m.unchanged)}`)}
    ${kpi("Có thay đổi", fmtInt(m.changed), "geometry / chiều cao / thuộc tính", "changed")}
    ${kpi("Thêm mới", fmtInt(m.added), "chỉ có ở B", "added")}
    ${kpi("Bị xoá", fmtInt(m.removed), `chỉ có ở A · ${pct(sa.buildings ? (100 * m.removed) / sa.buildings : null)}`, "removed")}
  </div>`;

  html += section("1. Tổng quan", "", overviewTable(sa, sb));
  html += section("2. Schema", `A ${r.schema.fields_a} field · B ${r.schema.fields_b} · chung ${r.schema.common}`, schemaBlock(r.schema));
  html += section("3. Khớp building", "", matchBlock(m, sa, sb));
  html += section("4. Thay đổi ở building khớp", `${fmtInt(m.matched)} building khớp`, changesBlock(r.changes, m.matched));
  html += section("5. Chất lượng A và B", "", qualityBlock(r.quality, r.columns));
  html += section(`6. Theo khu vực (ô ${fmtNum(r.grid.cell_deg, 3)}°)`,
    `${fmtInt(r.grid.cells)} ô có building · ${fmtInt(r.grid.only_a)} ô chỉ có ở A · ${fmtInt(r.grid.only_b)} ô chỉ có ở B`,
    gridTable(r.grid.top, look));
  html += section("Mẫu building bị xoá", "lớn nhất trước — geometry A", sampleTable(r.samples.removed, look));
  html += section("Mẫu building thêm mới", "lớn nhất trước — geometry B", sampleTable(r.samples.added, look));
  html += section("Mẫu building có thay đổi", "đổi geometry đáng kể trước, rồi theo |Δh|", sampleTable(r.samples.changed, look));

  const md = job.outputs.report_md;
  const json = job.outputs.report_json;
  const gpkg = job.outputs.gpkg;
  html += `<div class="out-actions">
    ${md ? `<a class="btn small" href="/api/files/${esc(md.token)}?download=1" download>Tải diff_report.md</a>` : ""}
    ${json ? `<a class="btn small" href="/api/files/${esc(json.token)}?download=1" download>Tải diff_report.json</a>` : ""}
    ${gpkg ? `<button class="btn small" type="button" data-reveal="${esc(gpkg.token)}">GeoPackage thay đổi (QGIS)</button>` : ""}
  </div>`;
  $("#diffReport").innerHTML = html;
}

function overviewTable(sa, sb) {
  const rows = [];
  const row = (label, a, b, cls = "") => rows.push(`<tr><th>${esc(label)}</th><td class="${cls}">${a}</td><td class="${cls}">${b}</td></tr>`);
  const kind = (s) => (s.kind === "pmtiles" ? `PMTiles${s.generator ? ` <span class="hint">· ${esc(s.generator)}</span>` : ""}` : esc(s.driver || "vector"));
  row("Định dạng", kind(sa), kind(sb));
  row("Dung lượng", fmtBytes(sa.size), fmtBytes(sb.size), "n");
  row("Building", fmtInt(sa.buildings), fmtInt(sb.buildings), "n");
  if (sa.kind === "pmtiles" || sb.kind === "pmtiles") {
    const tiles = (s) => (s.kind === "pmtiles" ? `${fmtInt(s.pieces)} mảnh → ${fmtInt(s.buildings)} <span class="hint">(tilestats ${fmtInt(s.tilestats_count)})</span>` : "—");
    row("Mảnh tile ghép lại", tiles(sa), tiles(sb), "n");
    const lz = (s) => (s.kind === "pmtiles" ? `<code>${esc(s.layer)}</code> · z${s.minzoom}–${s.maxzoom}` : "—");
    row("Layer · zoom", lz(sa), lz(sb));
  }
  const crs = (s) => `${esc(s.crs || "—")}${s.crs_declared === false ? ' <span class="hint">(không khai báo, đã xác nhận)</span>' : ""}`;
  row("CRS", crs(sa), crs(sb));
  const fam = (s) => Object.entries(s.geometry_families || {}).map(([k, n]) => `${esc(k)} ${fmtInt(n)}`).join(" · ") || "—";
  row("Kiểu geometry", fam(sa), fam(sb));
  const bad = (s) => `${fmtInt(s.null_geometry)} / ${fmtInt(s.empty_geometry)} / ${fmtInt(s.invalid_geometry)}`;
  row("Geometry NULL / rỗng / lỗi", bad(sa), bad(sb), "n");
  const ext = (s) => (s.extent ? s.extent.map((v) => fmtNum(v, 3)).join(", ") : "—");
  row("Extent (lon/lat)", ext(sa), ext(sb));
  row("Tổng diện tích", `${fmtNum((sa.total_area_m2 || 0) / 1e6, 2)} km²`, `${fmtNum((sb.total_area_m2 || 0) / 1e6, 2)} km²`, "n");
  for (const col of [...new Set([...Object.keys(sa.values || {}), ...Object.keys(sb.values || {})])]) {
    const v = (s) => {
      const x = s.values?.[col];
      if (!x) return "—";
      return `${fmtInt(x.distinct)} giá trị<div class="hint mono">${x.top.slice(0, 3).map((t) => `${esc(t.value)} (${fmtInt(t.count)})`).join("<br>")}</div>`;
    };
    row(col, v(sa), v(sb));
  }
  return abTable(rows);
}

function abTable(rows) {
  return `<div class="tbl-wrap"><table class="ab"><thead><tr><th></th><th><span class="side-badge a sm">A</span> Production</th><th><span class="side-badge b sm">B</span> Release</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}

function schemaBlock(s) {
  const chips = (items, cls) => `<div class="cols">${items.map((i) => `<span class="chip ${cls(i)}" title="${i.a_filled_pct != null ? `A có dữ liệu ở ${fmtNum(i.a_filled_pct, 2)}% building` : i.b_filled_pct != null ? `B có dữ liệu ở ${fmtNum(i.b_filled_pct, 2)}% building` : ""}">${esc(i.name)} <em>${esc(i.type)}${i.a_filled_pct != null ? ` · ${fmtNum(i.a_filled_pct, 1)}%` : i.b_filled_pct != null ? ` · ${fmtNum(i.b_filled_pct, 1)}%` : ""}</em></span>`).join("")}</div>`;
  let html = "";
  if (!s.missing.length && !s.added.length && !s.type_changed.length) html += `<p class="hint">A và B cùng danh sách field và cùng kiểu.</p>`;
  if (s.missing.length) {
    const req = s.missing.filter((x) => x.required).length;
    html += `<p class="accounting"><b>B thiếu ${fmtInt(s.missing.length)} field</b>${req ? ` — <span class="bad-text">${fmtInt(req)} bắt buộc</span>` : ""} <span class="hint">(% = tỉ lệ building ở A có dữ liệu)</span></p>`
      + chips(s.missing.slice(0, 300), (i) => (i.required ? "bad" : "warn"));
    if (s.missing.length > 300) html += `<p class="hint">… và ${fmtInt(s.missing.length - 300)} field nữa (xem diff_report.json).</p>`;
  }
  if (s.added.length) html += `<p class="accounting"><b>B có ${fmtInt(s.added.length)} field mới</b> <span class="hint">(% = tỉ lệ building ở B có dữ liệu)</span></p>` + chips(s.added, () => "");
  if (s.type_changed.length) {
    html += `<div class="tbl-wrap"><table><thead><tr><th>Field</th><th>A</th><th>B</th><th>Mức</th><th>Ghi chú</th></tr></thead><tbody>${s.type_changed.map((c) => `
      <tr><td class="mono">${esc(c.name)}</td><td>${esc(c.type_a)}</td><td>${esc(c.type_b)}</td><td>${levelBadge(c.level)}</td><td>${esc(c.note)}</td></tr>`).join("")}</tbody></table></div>`;
  }
  if (s.null_changes.length) {
    html += `<p class="accounting"><b>Tỉ lệ null thay đổi</b> <span class="hint">(cột chung, sắp theo mức tăng)</span></p>
      <div class="tbl-wrap scroll-y"><table><thead><tr><th>Field</th><th class="n">Null ở A</th><th class="n">Null ở B</th><th class="n">Δ (điểm %)</th></tr></thead><tbody>${s.null_changes.slice(0, 40).map((c) => `
      <tr><td class="mono">${esc(c.name)}</td><td class="n">${pct(c.a_null_pct)}</td><td class="n">${pct(c.b_null_pct)}</td><td class="n ${c.delta_pts > 0 ? "up" : ""}">${signed(c.delta_pts)}</td></tr>`).join("")}</tbody></table></div>`;
  }
  return html;
}

function matchBlock(m, sa, sb) {
  const total = Math.max(sa.buildings || 0, 1);
  const bars = hbars([
    { label: "Khớp", count: m.matched, pct: (100 * m.matched) / total },
    { label: "Bị xoá", count: m.removed, pct: (100 * m.removed) / total },
    { label: "Thêm mới", count: m.added, pct: (100 * m.added) / Math.max(sb.buildings || 0, 1) },
  ]);
  const notes = [];
  if (m.mode === "key") {
    for (const s of ["a", "b"]) {
      if (m[`dup_keys_${s}`] || m[`empty_keys_${s}`]) {
        notes.push(`${s.toUpperCase()}: ${fmtInt(m[`dup_keys_${s}`])} ID trùng (${fmtInt(m[`dup_rows_${s}`])} building) · ${fmtInt(m[`empty_keys_${s}`])} building không có ID`);
      }
    }
    if (m.reid_same_location) notes.push(`${fmtInt(m.reid_same_location)} cặp “xoá + thêm” nằm cùng vị trí — có thể là building bị đổi ID`);
    if (m.reid_same_source) notes.push(`${fmtInt(m.reid_same_source)} building bị xoá có <code>${esc(m.reid_source_col)}</code> xuất hiện lại ở B dưới ID khác`);
  } else {
    notes.push("Khớp theo vị trí: số liệu là ước lượng, building gộp / tách giữa 2 bản sẽ hiện thành xoá + thêm.");
  }
  return bars + (notes.length ? `<ul class="notes">${notes.map((n) => `<li>${n}</li>`).join("")}</ul>` : "");
}

function changesBlock(c, matched) {
  const total = Math.max(matched, 1);
  const reasons = c.by_reason || {};
  let html = `<p class="accounting">Lý do: geometry ${fmtInt(reasons.geometry)} · chiều cao ${fmtInt(reasons.height)} · thuộc tính ${fmtInt(reasons.attributes)} <span class="hint">(một building có thể có nhiều lý do)</span></p>`;
  html += `<div class="two-col"><div><div class="subhead">Geometry</div>${hbars(GEOM_CLASSES.map(([k, label]) => ({ label, count: c.geometry[k] || 0, pct: (100 * (c.geometry[k] || 0)) / total })))}
    <p class="hint">Khác nhỏ: diện tích lệch ≤ 5 % và tâm dịch ≤ 1 m — nhiễu số / lượng tử hoá tile, không tính là thay đổi. Đáng kể: vượt ngưỡng geometry của release gate.</p></div>
    <div><div class="subhead">Diện tích lệch (building geometry khác)</div>${hbars(c.area_pct_hist.map((b) => ({ label: b.label, count: b.count, pct: (100 * b.count) / total })))}
    <div class="subhead">Tâm dịch</div>${hbars(c.shift_hist.map((b) => ({ label: b.label, count: b.count, pct: (100 * b.count) / total })))}</div></div>`;
  const h = c.height;
  html += `<div class="subhead">Chiều cao</div>
    <p class="accounting">Đổi ${fmtInt(h.changed)} building (cao lên ${fmtInt(h.taller)} · thấp xuống ${fmtInt(h.lower)}) · mặc định → có số đo ${fmtInt(h.default_to_real)} · có số đo → mặc định ${fmtInt(h.real_to_default)} · Δh TB ${signed(h.mean_dh_changed, 1)} m · |Δh| lớn nhất ${fmtNum(h.max_abs_dh, 1)} m</p>
    ${hbars(c.dh_hist.map((b) => ({ label: b.label, count: b.count, pct: (100 * b.count) / total })))}`;
  const cols = c.columns.filter((x) => x.changed);
  html += `<div class="subhead">Thuộc tính (số building khớp có giá trị khác)</div>`;
  html += cols.length ? `<div class="tbl-wrap scroll-y"><table><thead><tr><th>Cột</th><th class="n">Building đổi</th><th class="n">%</th><th></th></tr></thead><tbody>${cols.map((x) => `
      <tr><td class="mono">${esc(x.column)}</td><td class="n">${fmtInt(x.changed)}</td><td class="n">${pct(x.pct)}</td><td>${x.ignored ? '<span class="badge">bỏ qua</span>' : ""}</td></tr>`).join("")}</tbody></table></div>`
    : `<p class="hint">Không cột chung nào đổi giá trị.</p>`;
  return html;
}

function qualityBlock(q, columns) {
  const qa = q.a;
  const qb = q.b;
  const rows = [];
  const row = (label, a, b) => rows.push(`<tr><th>${esc(label)}</th><td class="n">${a}</td><td class="n">${b}</td></tr>`);
  const real = (x) => (x.real_pct == null ? '<span class="hint">không xác định — file không có cột nguồn chiều cao</span>'
    : `${fmtInt(x.real_height)} <span class="hint">(${pct(x.real_pct)})</span>`);
  row("Có chiều cao thật", real(qa), real(qb));
  row("Outlier chiều cao", fmtInt(qa.outliers), fmtInt(qb.outliers));
  row("Chiều cao trung vị / cao nhất", `${fmtNum(qa.median_h)} / ${fmtNum(qa.max_h)} m`, `${fmtNum(qb.median_h)} / ${fmtNum(qb.max_h)} m`);
  row("Khối con mồ côi (parent không tồn tại)", fmtInt(qa.orphan_parts), fmtInt(qb.orphan_parts));
  row("superseded_by trỏ tới ID không tồn tại", fmtInt(qa.dangling_superseded), fmtInt(qb.dangling_superseded));
  const method = (s) => (columns?.[s]?.height_from === "h_m" ? "cột h_m có sẵn" : `quy tắc convert${columns?.[s]?.height_col ? ` (${columns[s].height_col})` : ""}`);
  row("Chiều cao lấy từ", esc(method("a")), esc(method("b")));
  let html = abTable(rows);
  const cats = new Map();
  for (const [s, side] of [["a", qa], ["b", qb]]) {
    for (const cat of side.categories || []) {
      if (!cats.has(cat.column)) cats.set(cat.column, { a: [], b: [] });
      cats.get(cat.column)[s] = cat.rows;
    }
  }
  for (const [col, v] of cats) {
    const values = [...new Set([...v.a.map((x) => x.value), ...v.b.map((x) => x.value)])];
    const get = (list, value) => list.find((x) => x.value === value);
    html += `<div class="subhead">Phân bố ${esc(col)}</div><div class="tbl-wrap"><table><thead><tr><th>Giá trị</th><th class="n">A</th><th class="n">B</th><th class="n">Δ (điểm %)</th></tr></thead><tbody>${values.map((value) => {
      const a = get(v.a, value);
      const b = get(v.b, value);
      const d = a && b ? b.pct - a.pct : null;
      return `<tr><td>${value == null ? '<span class="hint">(trống)</span>' : esc(value)}</td>
        <td class="n">${a ? `${fmtInt(a.count)} <span class="hint">${pct(a.pct)}</span>` : "—"}</td>
        <td class="n">${b ? `${fmtInt(b.count)} <span class="hint">${pct(b.pct)}</span>` : "—"}</td>
        <td class="n">${signed(d)}</td></tr>`;
    }).join("")}</tbody></table></div>`;
  }
  return html;
}

function gridTable(cells, look) {
  if (!cells?.length) return `<p class="hint">Không có.</p>`;
  return `<div class="tbl-wrap scroll-y"><table><thead><tr><th>Tâm ô (lat, lon)</th><th class="n">A</th><th class="n">B</th>
    <th class="n"><span class="dot-sw added"></span>Thêm</th><th class="n"><span class="dot-sw removed"></span>Xoá</th><th class="n"><span class="dot-sw changed"></span>Đổi</th><th></th></tr></thead>
    <tbody>${cells.map((c) => `<tr><td class="mono">${c.center[1].toFixed(3)}, ${c.center[0].toFixed(3)}</td>
      <td class="n">${fmtInt(c.n_a)}</td><td class="n">${fmtInt(c.n_b)}</td><td class="n">${fmtInt(c.added)}</td>
      <td class="n">${fmtInt(c.removed)}</td><td class="n">${fmtInt(c.changed)}</td><td>${look(c.center[0], c.center[1], 13)}</td></tr>`).join("")}</tbody></table></div>`;
}

function sampleTable(rows, look) {
  if (!rows?.length) return `<p class="hint">Không có.</p>`;
  const skip = new Set(["lon", "lat"]);
  const keys = Object.keys(rows[0]).filter((k) => !skip.has(k));
  const labels = { key: "ID", area_m2: "Diện tích (m²)", h: "h (m)", geom: "Geometry", area_pct: "Δ diện tích %", shift_m: "Tâm dịch (m)", h_a: "h A", h_b: "h B", cols: "Cột đổi" };
  const head = `<tr>${keys.map((k) => `<th class="${typeof rows[0][k] === "number" ? "n" : ""}">${esc(labels[k] || k)}</th>`).join("")}<th></th></tr>`;
  const body = rows.map((r) => `<tr>${keys.map((k) => {
    const v = r[k];
    if (typeof v === "number") return `<td class="n">${fmtNum(v, 2)}</td>`;
    return `<td class="${k === "key" ? "id" : ""}" title="${esc(v)}">${v == null ? "" : esc(v)}</td>`;
  }).join("")}<td>${r.lon != null ? look(r.lon, r.lat, 18, r.key) : ""}</td></tr>`).join("");
  return `<div class="tbl-wrap scroll-y"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>`;
}
