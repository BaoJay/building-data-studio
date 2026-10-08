// Building Data Studio — UI logic (vanilla JS, no build step).

import { $, $$, api, esc, fmtBytes, fmtDur, fmtInt, fmtNum, hbars, loadJSON, previewUrl, reveal, saveJSON, section, toast, uploadFile } from "./util.js";
import { initDiff, renderDiffReport, setDiffHealth } from "./diff.js";

const GENERATED_HEIGHT = ["h_m", "h_src", "h_outlier"];
const GENERATED_PARTS = ["has_parts", "is_part"];
const CRS_PRESETS = [
  ["EPSG:4326", "EPSG:4326 — WGS 84 (mặc định)"],
  ["EPSG:3857", "EPSG:3857 — Web Mercator"],
  ["EPSG:32648", "EPSG:32648 — WGS 84 / UTM 48N"],
  ["EPSG:32649", "EPSG:32649 — WGS 84 / UTM 49N"],
  ["EPSG:3405", "EPSG:3405 — VN-2000 / UTM 48N"],
  ["EPSG:3406", "EPSG:3406 — VN-2000 / UTM 49N"],
];
const STATUS_LABEL = { queued: "Đang chờ", running: "Đang chạy", done: "Hoàn tất", failed: "Lỗi", cancelled: "Đã huỷ" };
const KIND_LABEL = { convert: "Convert", diff: "So sánh" };
const TAB_KEY = "building-data-studio.tab.v1";
const PREF_KEY = "building-data-studio.prefs.v1";
const LEGACY_PREF_KEY = "building-converter.prefs.v1"; // before the rename
const DEFAULTS = {
  want_gpkg: true, want_pmtiles: true, out_dir: "", gpkg_layer: "buildings", gpkg_crs: "EPSG:4326",
  m_per_level: 3.5, default_m: 4, min_valid_m: 2, max_valid_m: 500, max_levels: 200, outlier_mode: "fallback",
  decimals: 1, tiles_layer: "buildings", minzoom: 13, maxzoom: 15, drop: "drop-densest", extend_zooms: true,
  simplification: "", max_tile_kb: 500, shared_borders: false, attribution: "", makevalid: false, keep_temp: false,
};

const state = {
  health: null,
  probe: null,
  jobId: null,
  logSince: 0,
  pollTimer: null,
  tileAttrs: new Set(),
  catCols: new Set(),
  reportKey: null,
  outputsKey: null,
  prefs: { ...DEFAULTS, ...loadJSON(PREF_KEY, LEGACY_PREF_KEY) },
};

const savePrefs = (prefs) => saveJSON(PREF_KEY, prefs);

// ------------------------------------------------------------------ init
async function init() {
  setupTabs();
  setupDropzone();
  initDiff({ showJob });
  $("#pathBtn").addEventListener("click", () => openPath($("#pathInput").value));
  $("#pathInput").addEventListener("keydown", (e) => { if (e.key === "Enter") openPath(e.target.value); });
  $("#runBtn").addEventListener("click", runJob);
  $("#resetBtn").addEventListener("click", () => {
    state.prefs = { ...DEFAULTS, out_dir: state.health?.defaults.output_dir || "" };
    savePrefs({});
    if (state.probe?.kind === "vector") renderOptions(state.probe);
  });
  document.addEventListener("click", onGlobalClick);
  try {
    state.health = await api("/api/health");
    if (!state.prefs.out_dir) state.prefs.out_dir = state.health.defaults.output_dir;
    $("#fileInput").accept = state.health.accept.join(",");
    renderTools(state.health);
    setDiffHealth(state.health);
  } catch (e) {
    toast(`Không kết nối được server: ${e.message}`);
  }
  loadHistory();
}

function renderTools(health) {
  const label = (name, info) => {
    if (!info.path) return `Thiếu ${name}`;
    const v = (info.version || "").split(",")[0].trim();
    if (name === "ogr2ogr") return v.replace(/\s*".*"/, "");
    return v.toLowerCase().startsWith(name) ? v : `${name} ${v}`;
  };
  const shown = ["ogr2ogr", "tippecanoe", "pmtiles"];
  $("#toolStatus").innerHTML = shown.map((name) => {
    const info = health.tools[name];
    const cls = info.path ? "" : name === "pmtiles" ? "warn" : "bad";
    return `<span class="chip ${cls}" title="${esc(info.path || "chưa cài")}"><span class="dot"></span>${esc(label(name, info))}</span>`;
  }).join("");
  if (health.missing.length) {
    toast(`Thiếu công cụ: ${health.missing.join(", ")} — cài bằng: brew install gdal tippecanoe pmtiles`);
  }
}

// ------------------------------------------------------------------ tabs
function setupTabs() {
  $$("[data-tab-btn]").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tabBtn, true)));
  const fromHash = location.hash === "#diff" ? "diff" : location.hash === "#convert" ? "convert" : null;
  setTab(fromHash || loadJSON(TAB_KEY).tab || "convert");
  window.addEventListener("hashchange", () => { if (["#diff", "#convert"].includes(location.hash)) setTab(location.hash.slice(1)); });
}

function setTab(tab, remember = false) {
  document.body.dataset.tab = tab;
  $$("[data-tab-btn]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tabBtn === tab)));
  if (remember) {
    saveJSON(TAB_KEY, { tab });
    history.replaceState(null, "", `#${tab}`);
  }
}

function onGlobalClick(e) {
  const btn = e.target.closest("[data-reveal]");
  if (btn) { e.preventDefault(); reveal(btn.dataset.reveal); }
}

// ------------------------------------------------------------------ input
function setupDropzone() {
  const dz = $("#dropzone");
  const input = $("#fileInput");
  dz.addEventListener("click", () => input.click());
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  input.addEventListener("change", () => { if (input.files[0]) handleFile(input.files[0]); input.value = ""; });
  ["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => dz.addEventListener(t, () => dz.classList.remove("over")));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    const file = e.dataTransfer.files[0];
    if (file) handleFile(file);
  });
  // A file dropped outside the zones must not navigate the tab away. On the convert tab it is
  // taken as the input; on the diff tab there are two slots, so it is ignored.
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => {
    e.preventDefault();
    if (document.body.dataset.tab !== "convert" || e.target.closest(".dropzone")) return;
    if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
  });
}

async function handleFile(file) {
  const ext = `.${file.name.split(".").pop().toLowerCase()}`;
  if (state.health && !state.health.accept.includes(ext)) {
    toast(`Định dạng ${ext} chưa hỗ trợ.`);
    return;
  }
  const status = $("#uploadStatus");
  status.hidden = false;
  const bar = $("#uploadBar > span");
  $("#uploadText").textContent = `Đang upload ${file.name} (${fmtBytes(file.size)}) vào thư mục uploads/…`;
  try {
    const res = await uploadFile(file, (pct) => { bar.style.width = `${pct}%`; });
    bar.style.width = "100%";
    status.hidden = true;
    $("#pathInput").value = res.path;
    await probePath(res.path);
  } catch (e) {
    $("#uploadText").textContent = `Lỗi upload: ${e.message}`;
  }
}

function openPath(raw) {
  const path = raw.trim().replace(/^["']|["']$/g, "");
  if (!path) { toast("Nhập đường dẫn file."); return; }
  probePath(path);
}

async function probePath(path, extra = {}) {
  const box = $("#fileInfo");
  box.innerHTML = `<div class="upload-status"><div>Đang đọc file…</div><div class="progress indeterminate"><span></span></div></div>`;
  try {
    const res = await api("/api/probe", { method: "POST", body: { path, ...extra } });
    state.probe = res;
    renderFileInfo(res);
    if (res.kind === "vector") {
      state.tileAttrs = new Set(res.suggest.pmtiles_attrs);
      state.catCols = new Set(res.suggest.category_cols);
      renderOptions(res);
      $("#optionsCard").hidden = false;
    } else {
      $("#optionsCard").hidden = true;
    }
  } catch (e) {
    state.probe = null;
    $("#optionsCard").hidden = true;
    box.innerHTML = `<div class="alert bad"><span class="icon">!</span><div>${esc(e.message)}</div></div>`;
  }
}

function renderFileInfo(p) {
  const kv = (k, v) => `<div><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`;
  const alerts = (p.warnings || []).map((w) => `<div class="alert warn"><span class="icon">!</span><div>${esc(w)}</div></div>`).join("");
  if (p.kind === "pmtiles") {
    const h = p.header || {};
    const layers = (p.vector_layers || []).map((l) => l.id).join(", ") || "—";
    $("#fileInfo").innerHTML = `
      <div class="info-grid">
        ${kv("Tên file", esc(p.name))}
        ${kv("Dung lượng", fmtBytes(p.size))}
        ${kv("Zoom", h.minzoom != null ? `${h.minzoom}–${h.maxzoom}` : "—")}
        ${kv("Layer", esc(layers))}
        ${kv("Bounds", h.bounds ? h.bounds.map((b) => fmtNum(b, 4)).join(", ") : "—")}
      </div>
      ${alerts}
      <div class="out-actions">
        <a class="btn primary" href="${previewUrl(p.token, { name: p.name })}" target="_blank" rel="noopener">Xem bản đồ 3D</a>
        <button class="btn" type="button" data-reveal="${esc(p.token)}">Hiện trong Finder</button>
      </div>`;
    return;
  }
  const g = p.geometry;
  const crs = p.crs.missing
    ? `<span style="color:var(--warn-ink)">Không khai báo</span>`
    : esc(p.crs.label + (p.crs.name && p.crs.name !== p.crs.label ? ` (${p.crs.name})` : ""));
  const types = g.sampled_types?.length ? g.sampled_types.join("/") : g.type;
  const cols = p.fields.map((f) => `<span class="chip">${esc(f.name)} <em>${esc(f.subtype || f.type)}</em></span>`).join("");
  $("#fileInfo").innerHTML = `
    <div class="info-grid">
      ${kv("Tên file", esc(p.name))}
      ${kv("Dung lượng", fmtBytes(p.size))}
      ${kv("Định dạng", esc(p.driver))}
      ${kv("Số feature", fmtInt(p.feature_count))}
      ${kv("Geometry", `<span class="mono">${esc(g.column || "(geometry)")}</span> · ${esc(types)}`)}
      ${kv("CRS", crs)}
      ${kv("Extent", p.extent ? p.extent.map((b) => fmtNum(b, 4)).join(", ") : "—")}
      ${p.layers.length > 1 ? kv("Layer", `<select id="layerSelect">${p.layers.map((l) => `<option ${l === p.layer ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`) : ""}
    </div>
    ${alerts}
    <div class="cols" aria-label="Các cột">${cols}</div>`;
  $("#layerSelect")?.addEventListener("change", (e) => probePath(p.path, { layer: e.target.value }));
}

// ------------------------------------------------------------------ options form
function colOptions(fields, selected, none = "— không dùng —") {
  const opts = [`<option value="">${esc(none)}</option>`];
  for (const f of fields) {
    opts.push(`<option value="${esc(f.name)}" ${f.name === selected ? "selected" : ""}>${esc(f.name)} (${esc(f.type)})</option>`);
  }
  return opts.join("");
}

function renderOptions(p) {
  const pr = state.prefs;
  const s = p.suggest;
  const fields = p.fields.filter((f) => !f.generated);
  const valueCols = fields.filter((f) => ["Integer", "Integer64", "Real", "String"].includes(f.type));
  const textCols = fields.filter((f) => f.type === "String");
  const isParquet = p.driver === "Parquet";
  const crsCustom = !CRS_PRESETS.some(([v]) => v === pr.gpkg_crs);
  const num = (name, value, attrs = "") => `<input type="number" name="${name}" value="${esc(value)}" ${attrs}>`;
  const chk = (name, on) => `<input type="checkbox" name="${name}" ${on ? "checked" : ""}>`;

  const geomField = isParquet && (p.geometry_candidates.length > 1 || p.open_options.GEOM_POSSIBLE_NAMES)
    ? `<label class="field"><span>Cột geometry (WKB/WKT)</span><select name="geom_column">${
      [...new Set([g(p), ...p.geometry_candidates])].filter(Boolean).map((c) => `<option ${c === g(p) ? "selected" : ""}>${esc(c)}</option>`).join("")
    }</select></label>`
    : `<div class="field"><span>Cột geometry</span><div class="mono">${esc(p.geometry.column || "(mặc định)")}</div></div>`;

  const crsField = p.crs.missing
    ? `<label class="field"><span>CRS nguồn <b style="color:var(--warn-ink)">— cần xác nhận</b></span>
         <input type="text" name="src_crs" value="${esc(p.crs.suggested || "")}" placeholder="vd: EPSG:4326">
       </label>
       <span class="hint">File không ghi CRS. ${p.crs.suggested ? "Extent là kinh/vĩ độ nên đề xuất EPSG:4326 — sửa nếu dữ liệu ở hệ khác (vd VN-2000)." : "Bắt buộc nhập."}</span>`
    : `<div class="field"><span>CRS nguồn</span><div>${esc(p.crs.label)} <span class="hint">(đọc từ file)</span></div></div>`;

  $("#optForm").innerHTML = `
  <div class="opt-grid">
    <fieldset>
      <legend>Output</legend>
      <label class="check">${chk("want_gpkg", pr.want_gpkg)} GeoPackage (.gpkg) <span class="hint">— kiểm tra trên QGIS</span></label>
      <label class="check">${chk("want_pmtiles", pr.want_pmtiles)} PMTiles (.pmtiles) <span class="hint">— publish cho app mobile</span></label>
      <label class="field"><span>Tên file output</span><input type="text" name="out_name" value="${esc(p.name.replace(/\.[^.]+$/, ""))}"></label>
      <label class="field"><span>Thư mục output</span><input type="text" name="out_dir" value="${esc(pr.out_dir)}"></label>
      <span class="hint">Mỗi lần chạy tạo thư mục con <code>tên__ngày-giờ</code>, không ghi đè lần chạy trước.</span>
    </fieldset>

    <fieldset>
      <legend>Nguồn dữ liệu</legend>
      ${geomField}
      ${crsField}
      <span class="hint">Dữ liệu được chuẩn hoá về EPSG:4326 trước khi xuất.</span>
    </fieldset>

    <fieldset id="fsHeight">
      <legend><label class="check">${chk("height_enabled", true)} Chiều cao <code>h_m</code></label></legend>
      <div class="row2">
        <label class="field"><span>Cột chiều cao (m)</span><select name="height_col">${colOptions(valueCols, s.height_col)}</select></label>
        <label class="field"><span>Cột số tầng</span><select name="levels_col">${colOptions(valueCols, s.levels_col)}</select></label>
      </div>
      <div class="row3">
        <label class="field"><span>Mét / tầng</span>${num("m_per_level", pr.m_per_level, 'step="0.1" min="0.1"')}</label>
        <label class="field"><span>Mặc định (m)</span>${num("default_m", pr.default_m, 'step="0.5" min="0"')}</label>
        <label class="field"><span>Làm tròn</span><select name="decimals">${[0, 1, 2].map((d) => `<option value="${d}" ${+pr.decimals === d ? "selected" : ""}>${d} số lẻ</option>`).join("")}</select></label>
      </div>
      <div class="row3">
        <label class="field"><span>Hợp lệ từ (m)</span>${num("min_valid_m", pr.min_valid_m, 'step="0.5" min="0"')}</label>
        <label class="field"><span>đến (m)</span>${num("max_valid_m", pr.max_valid_m, 'step="10" min="1"')}</label>
        <label class="field"><span>Số tầng tối đa</span>${num("max_levels", pr.max_levels, 'step="1" min="1"')}</label>
      </div>
      <label class="field"><span>Giá trị ngoài khoảng hợp lệ</span>
        <select name="outlier_mode">
          <option value="fallback" ${pr.outlier_mode === "fallback" ? "selected" : ""}>Bỏ qua, dùng số tầng / mặc định (khuyên dùng)</option>
          <option value="clamp" ${pr.outlier_mode === "clamp" ? "selected" : ""}>Quá cao → kẹp về giá trị tối đa</option>
          <option value="keep" ${pr.outlier_mode === "keep" ? "selected" : ""}>Giữ nguyên, chỉ đánh dấu h_outlier</option>
        </select>
      </label>
      <div class="row2">
        <label class="field"><span>Cột nguồn chiều cao</span><select name="prov_col">${colOptions(textCols, s.prov_col)}</select></label>
        <label class="field"><span>Giá trị = không có số liệu thật</span><input type="text" name="prov_missing" value="${esc(s.prov_missing.join(", "))}" placeholder="vd: default"></label>
      </div>
      <div class="formula" id="formula"></div>
    </fieldset>

    <fieldset id="fsParts">
      <legend><label class="check">${chk("parts_enabled", Boolean(s.parent_col && s.id_col))} Toà nhà nhiều khối</label></legend>
      <div class="row2">
        <label class="field"><span>Cột ID building</span><select name="id_col">${colOptions(fields, s.id_col, "— chọn —")}</select></label>
        <label class="field"><span>Cột ID cha (parent)</span><select name="parent_col">${colOptions(fields, s.parent_col, "— chọn —")}</select></label>
      </div>
      <span class="hint">Thêm <code>has_parts</code> = outline có khối con (nên ẩn khi dựng 3D để không chồng hình) và <code>is_part</code> = là khối con.</span>
    </fieldset>

    <fieldset id="fsGpkg">
      <legend>GeoPackage</legend>
      <div class="row2">
        <label class="field"><span>Tên layer</span><input type="text" name="gpkg_layer" value="${esc(pr.gpkg_layer)}"></label>
        <label class="field"><span>Hệ toạ độ output</span>
          <select name="gpkg_crs_sel">
            ${CRS_PRESETS.map(([v, l]) => `<option value="${v}" ${v === pr.gpkg_crs ? "selected" : ""}>${esc(l)}</option>`).join("")}
            <option value="custom" ${crsCustom ? "selected" : ""}>Khác…</option>
          </select>
        </label>
      </div>
      <label class="field" id="crsCustomField" ${crsCustom ? "" : "hidden"}><span>CRS khác (EPSG:xxxx, WKT hoặc PROJ)</span><input type="text" name="gpkg_crs_custom" value="${esc(crsCustom ? pr.gpkg_crs : "")}"></label>
      <label class="field"><span>Lọc bản ghi (SQL WHERE, tuỳ chọn)</span><input type="text" name="where" placeholder="vd: status = 'active'"></label>
      <label class="check">${chk("makevalid", pr.makevalid)} Sửa geometry lỗi (<code>-makevalid</code>)</label>
    </fieldset>

    <fieldset id="fsTiles">
      <legend>PMTiles</legend>
      <div class="row3">
        <label class="field"><span>Tên layer</span><input type="text" name="tiles_layer" value="${esc(pr.tiles_layer)}"></label>
        <label class="field"><span>Min zoom</span>${num("minzoom", pr.minzoom, 'min="0" max="22"')}</label>
        <label class="field"><span>Max zoom</span>${num("maxzoom", pr.maxzoom, 'min="0" max="22"')}</label>
      </div>
      <label class="field"><span>Khi tile vượt dung lượng</span>
        <select name="drop">
          <option value="drop-densest" ${pr.drop === "drop-densest" ? "selected" : ""}>Bỏ bớt feature ở vùng dày đặc (drop-densest)</option>
          <option value="coalesce-densest" ${pr.drop === "coalesce-densest" ? "selected" : ""}>Gộp feature ở vùng dày đặc (coalesce-densest)</option>
          <option value="none" ${pr.drop === "none" ? "selected" : ""}>Không xử lý (tile lỗi nếu quá lớn)</option>
        </select>
      </label>
      <div class="row2">
        <label class="field"><span>Dung lượng tile tối đa (KB)</span>${num("max_tile_kb", pr.max_tile_kb, 'min="50" step="50"')}</label>
        <label class="field"><span>Simplification (trống = mặc định)</span>${num("simplification", pr.simplification, 'min="0" step="0.5" placeholder="1"')}</label>
      </div>
      <label class="check">${chk("extend_zooms", pr.extend_zooms)} Tự tăng max zoom nếu vẫn phải bỏ feature</label>
      <label class="check">${chk("shared_borders", pr.shared_borders)} Giữ khớp cạnh chung giữa polygon (<code>--detect-shared-borders</code>)</label>
      <label class="field"><span>Attribution</span><input type="text" name="attribution" value="${esc(pr.attribution)}" placeholder="vd: © OpenStreetMap contributors"></label>
    </fieldset>

    <fieldset class="wide">
      <legend>Cột giữ lại trong GeoPackage</legend>
      <div class="list-head"><span class="hint" id="colCount"></span>
        <span><button type="button" class="linkbtn" data-all="col">Chọn tất cả</button> · <button type="button" class="linkbtn" data-none="col">Bỏ hết</button></span></div>
      <div class="checklist" id="colList">${fields.map((f) => `<label><input type="checkbox" name="col" value="${esc(f.name)}" checked>${esc(f.name)}</label>`).join("")}</div>
    </fieldset>

    <fieldset class="wide">
      <legend>Thuộc tính đưa vào PMTiles</legend>
      <div class="list-head"><span class="hint">Càng ít thuộc tính, tile càng nhẹ. Viền nét đứt = cột do app tính ra.</span>
        <span><button type="button" class="linkbtn" data-all="tattr">Chọn tất cả</button> · <button type="button" class="linkbtn" data-none="tattr">Bỏ hết</button></span></div>
      <div class="checklist" id="attrList"></div>
    </fieldset>

    <fieldset class="wide">
      <legend>Báo cáo</legend>
      <span class="hint">Thống kê chiều cao theo các cột phân loại:</span>
      <div class="checklist" id="catList"></div>
      <label class="check">${chk("keep_temp", pr.keep_temp)} Giữ file tạm sau khi chạy (để debug)</label>
    </fieldset>
  </div>`;

  const form = $("#optForm");
  form.oninput = form.onchange = (e) => onFormChange(e);
  form.onclick = (e) => {
    const all = e.target.closest("[data-all]");
    const none = e.target.closest("[data-none]");
    if (!all && !none) return;
    const name = (all || none).dataset.all || (all || none).dataset.none;
    $$(`input[name="${name}"]`, form).forEach((cb) => {
      cb.checked = Boolean(all);
      if (name === "tattr") all ? state.tileAttrs.add(cb.value) : state.tileAttrs.delete(cb.value);
    });
    onFormChange();
  };
  form.elements.geom_column?.addEventListener("change", (e) => probePath(p.path, { geom_column: e.target.value, layer: p.layer }));
  onFormChange();
}

function g(p) { return p.open_options?.GEOM_POSSIBLE_NAMES || p.geometry.column; }

function onFormChange(e) {
  const form = $("#optForm");
  const el = form.elements;
  if (e?.target?.name === "tattr") {
    e.target.checked ? state.tileAttrs.add(e.target.value) : state.tileAttrs.delete(e.target.value);
  }
  if (e?.target?.name === "cat") {
    e.target.checked ? state.catCols.add(e.target.value) : state.catCols.delete(e.target.value);
  }
  const heightOn = el.height_enabled.checked;
  const partsOn = el.parts_enabled.checked;
  $("#fsHeight").classList.toggle("off", !heightOn);
  $("#fsParts").classList.toggle("off", !partsOn);
  $("#fsGpkg").classList.toggle("off", !el.want_gpkg.checked);
  $("#fsTiles").classList.toggle("off", !el.want_pmtiles.checked);
  $("#crsCustomField").hidden = el.gpkg_crs_sel.value !== "custom";

  const kept = $$('input[name="col"]', form).filter((c) => c.checked).map((c) => c.value);
  $("#colCount").textContent = `${kept.length}/${$$('input[name="col"]', form).length} cột`;

  // Tile attributes = kept columns + generated ones (dashed).
  const generated = [...(heightOn ? GENERATED_HEIGHT : []), ...(partsOn ? GENERATED_PARTS : [])];
  const available = [...new Set([...kept.filter((c) => !generated.includes(c)), ...generated])];
  if (partsOn) [el.id_col.value, el.parent_col.value].forEach((c) => { if (c && !available.includes(c)) available.push(c); });
  $("#attrList").innerHTML = available.map((a) =>
    `<label class="${generated.includes(a) ? "gen" : ""}"><input type="checkbox" name="tattr" value="${esc(a)}" ${state.tileAttrs.has(a) ? "checked" : ""}>${esc(a)}</label>`).join("");

  // Category candidates: text columns that are not identifiers (ids make useless breakdowns).
  const textFields = state.probe.fields
    .filter((f) => f.type === "String" && !f.generated && !/(^|_)(id|uuid)$/i.test(f.name))
    .map((f) => f.name);
  const cats = [...(heightOn ? ["h_src"] : []), ...textFields];
  $("#catList").innerHTML = cats.map((c) =>
    `<label class="${c === "h_src" ? "gen" : ""}"><input type="checkbox" name="cat" value="${esc(c)}" ${state.catCols.has(c) ? "checked" : ""}>${esc(c)}</label>`).join("");

  $("#formula").innerHTML = formulaText(el);
  const missingCrs = state.probe.crs.missing && !el.src_crs?.value.trim();
  $("#runHint").textContent = missingCrs ? "Cần nhập CRS nguồn trước khi chạy." : "";
}

function formulaText(el) {
  const h = el.height_col.value;
  const l = el.levels_col.value;
  const prov = el.prov_col.value;
  const missing = el.prov_missing.value.trim();
  const parts = [];
  if (h) {
    let cond = `${el.min_valid_m.value}–${el.max_valid_m.value} m`;
    if (prov && missing) cond += `, ${prov} ∉ {${missing}}`;
    parts.push(`<code>${esc(h)}</code> <span class="hint">(${esc(cond)})</span>`);
  }
  if (l) parts.push(`<code>${esc(l)}</code> × ${esc(el.m_per_level.value)} m <span class="hint">(1–${esc(el.max_levels.value)} tầng)</span>`);
  parts.push(`${esc(el.default_m.value)} m`);
  return `<b>h_m</b> = ${parts.join(" → ")}<br><span class="hint">Lấy giá trị đầu tiên hợp lệ. <code>h_src</code> ghi nguồn đã dùng; <code>h_outlier</code> = 1 khi giá trị gốc nằm ngoài khoảng hợp lệ.</span>`;
}

function collectConfig() {
  const p = state.probe;
  const el = $("#optForm").elements;
  const val = (n) => (el[n]?.value ?? "").trim();
  const numv = (n) => Number(val(n));
  const gpkgCrs = el.gpkg_crs_sel.value === "custom" ? val("gpkg_crs_custom") : el.gpkg_crs_sel.value;
  const prefs = {
    want_gpkg: el.want_gpkg.checked, want_pmtiles: el.want_pmtiles.checked, out_dir: val("out_dir"),
    gpkg_layer: val("gpkg_layer"), gpkg_crs: gpkgCrs, m_per_level: numv("m_per_level"), default_m: numv("default_m"),
    min_valid_m: numv("min_valid_m"), max_valid_m: numv("max_valid_m"), max_levels: numv("max_levels"),
    outlier_mode: val("outlier_mode"), decimals: numv("decimals"), tiles_layer: val("tiles_layer"),
    minzoom: numv("minzoom"), maxzoom: numv("maxzoom"), drop: val("drop"), extend_zooms: el.extend_zooms.checked,
    simplification: val("simplification"), max_tile_kb: numv("max_tile_kb"), shared_borders: el.shared_borders.checked,
    attribution: val("attribution"), makevalid: el.makevalid.checked, keep_temp: el.keep_temp.checked,
  };
  const config = {
    input: { path: p.path, layer: p.layer, geom_column: el.geom_column ? val("geom_column") : null, src_crs: el.src_crs ? val("src_crs") : null },
    output: { dir: prefs.out_dir, name: val("out_name"), gpkg: prefs.want_gpkg, pmtiles: prefs.want_pmtiles, gpkg_layer: prefs.gpkg_layer, gpkg_crs: gpkgCrs, keep_temp: prefs.keep_temp },
    attributes: { columns: $$('input[name="col"]:checked').map((c) => c.value), where: val("where"), makevalid: prefs.makevalid },
    height: {
      enabled: el.height_enabled.checked, height_col: val("height_col") || null, levels_col: val("levels_col") || null,
      m_per_level: prefs.m_per_level, default_m: prefs.default_m, min_valid_m: prefs.min_valid_m, max_valid_m: prefs.max_valid_m,
      max_levels: prefs.max_levels, outlier_mode: prefs.outlier_mode, decimals: prefs.decimals,
      prov_col: val("prov_col") || null, prov_missing: val("prov_missing").split(",").map((s) => s.trim()).filter(Boolean),
    },
    parts: { enabled: el.parts_enabled.checked, id_col: val("id_col") || null, parent_col: val("parent_col") || null },
    tiles: {
      layer: prefs.tiles_layer, minzoom: prefs.minzoom, maxzoom: prefs.maxzoom,
      attributes: $$('input[name="tattr"]:checked').map((c) => c.value), drop: prefs.drop, extend_zooms: prefs.extend_zooms,
      simplification: prefs.simplification === "" ? null : Number(prefs.simplification), max_tile_kb: prefs.max_tile_kb,
      shared_borders: prefs.shared_borders, attribution: prefs.attribution,
    },
    report: { category_cols: $$('input[name="cat"]:checked').map((c) => c.value), id_col: val("id_col") || null },
  };
  return { config, prefs };
}

async function runJob() {
  if (!state.probe || state.probe.kind !== "vector") return;
  const { config, prefs } = collectConfig();
  if (!config.output.gpkg && !config.output.pmtiles) { toast("Chọn ít nhất một output."); return; }
  if (state.probe.crs.missing && !config.input.src_crs) { toast("Cần nhập CRS nguồn."); return; }
  if (config.parts.enabled && (!config.parts.id_col || !config.parts.parent_col)) { toast("Chọn cột ID và cột parent cho toà nhà nhiều khối."); return; }
  state.prefs = { ...state.prefs, ...prefs };
  savePrefs(state.prefs);
  const btn = $("#runBtn");
  btn.disabled = true;
  try {
    const { id } = await api("/api/jobs", { method: "POST", body: config });
    showJob(id, true);
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
  }
}

// ------------------------------------------------------------------ job progress
function showJob(id, scroll = false) {
  clearTimeout(state.pollTimer);
  state.jobId = id;
  state.logSince = 0;
  state.reportKey = null;
  state.outputsKey = null;
  $("#log").textContent = "";
  $("#jobCard").hidden = false;
  $("#reportCard").hidden = true;
  $("#diffReportCard").hidden = true;
  if (scroll) $("#jobCard").scrollIntoView({ behavior: "smooth", block: "start" });
  poll();
}

async function poll() {
  const id = state.jobId;
  let job;
  try {
    job = await api(`/api/jobs/${id}?since=${state.logSince}`);
  } catch (e) {
    toast(e.message);
    return;
  }
  if (id !== state.jobId) return;
  appendLog(job.log);
  state.logSince = job.log_total;
  renderJob(job);
  const final = ["done", "failed", "cancelled"].includes(job.status);
  if (final) loadHistory();
  else state.pollTimer = setTimeout(poll, 700);
}

function appendLog(lines) {
  if (!lines.length) return;
  const pre = $("#log");
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
  pre.textContent += lines.join("\n") + "\n";
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

function renderJob(job) {
  const now = Date.now() / 1000;
  const elapsed = job.started ? (job.ended || now) - job.started : null;
  const active = job.status === "running" || job.status === "queued";
  $("#jobHead").innerHTML = `
    <span class="badge kind">${esc(KIND_LABEL[job.kind] || job.kind)}</span>
    <span class="name">${esc(job.name)}</span>
    <span class="badge ${job.status}">${STATUS_LABEL[job.status] || job.status}</span>
    <span class="hint">${elapsed != null ? fmtDur(elapsed) : ""}</span>
    ${active ? `<button class="btn small danger" type="button" id="cancelBtn">Huỷ</button>` : ""}`;
  $("#cancelBtn")?.addEventListener("click", async () => {
    await api(`/api/jobs/${job.id}/cancel`, { method: "POST" }).catch((e) => toast(e.message));
  });

  $("#steps").innerHTML = job.steps.map((s) => {
    const icon = { pending: "○", running: '<span class="spinner"></span>', done: "✓", failed: "✕", skipped: "–" }[s.status] || "○";
    const dur = s.started ? fmtDur((s.ended || now) - s.started) : "";
    const bar = s.status === "running"
      ? `<div class="progress ${s.progress == null ? "indeterminate" : ""}"><span style="width:${s.progress ?? 0}%"></span></div>` : "";
    return `<li class="${s.status}"><span class="st">${icon}</span><div>${esc(s.title)}${s.detail ? ` <span class="detail">· ${esc(s.detail)}</span>` : ""}${bar}</div><span class="t">${dur}</span></li>`;
  }).join("");

  $("#jobError").innerHTML = job.status === "failed" && job.error
    ? `<div class="alert bad"><span class="icon">!</span><div>${esc(job.error)}<br><span class="hint">Xem “Log chi tiết” bên dưới. File trung gian được giữ trong thư mục _work.</span></div></div>` : "";

  const outputsKey = JSON.stringify([job.outputs, job.status]);
  if (outputsKey !== state.outputsKey) {
    state.outputsKey = outputsKey;
    renderOutputs(job);
  }
  if (job.report) {
    const key = `${job.id}:${job.status}`;
    if (key !== state.reportKey) {
      state.reportKey = key;
      if (job.kind === "diff") renderDiffReport(job);
      else renderReport(job);
    }
  }
}

function renderOutputs(job) {
  const order = ["gpkg", "pmtiles", "grid", "report_md", "report_json", "log"];
  const icons = { gpkg: "GPKG", pmtiles: "PMT", geojson: "JSON", report: "MD", log: "LOG" };
  const outs = order.map((k) => job.outputs[k]).filter(Boolean);
  $("#outputs").innerHTML = outs.map((o) => `
    <div class="out-row">
      <div class="ico ${o.kind}">${o.key === "report_json" ? "JSON" : icons[o.kind] || "FILE"}</div>
      <div><div class="nm">${esc(o.name)}</div><div class="meta">${esc(o.label)} · ${fmtBytes(o.size)}</div></div>
      <div class="btn-row">
        <a class="btn small" href="/api/files/${esc(o.token)}?download=1" download>Tải về</a>
        <button class="btn small" type="button" data-reveal="${esc(o.token)}">Hiện trong Finder</button>
      </div>
    </div>`).join("");
  const pm = job.outputs.pmtiles;
  const map = job.kind === "diff"
    ? previewUrl(pm?.token, { mode: "diff", grid: job.outputs.grid?.token || "", name: job.name })
    : previewUrl(pm?.token, { name: pm?.name });
  $("#outActions").innerHTML = job.output_dir_token ? `
    <button class="btn" type="button" data-reveal="${esc(job.output_dir_token)}">Mở thư mục output</button>
    ${pm && job.status === "done" ? `<a class="btn primary" href="${map}" target="_blank" rel="noopener">${job.kind === "diff" ? "Xem bản đồ diff" : "Xem bản đồ 3D"}</a>` : ""}
    <span class="hint mono">${esc(job.output_dir || "")}</span>` : "";
}

// ------------------------------------------------------------------ report
function renderReport(job) {
  const r = job.report;
  const h = r.height;
  const pm = job.outputs.pmtiles;
  const acc = r.accounting || {};
  const idCol = r.id_col || null;
  $("#reportCard").hidden = false;

  const kpi = (label, value, sub = "") => `<div class="kpi"><div class="label">${esc(label)}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
  let html = `<div class="kpis">${kpi("Tổng building", fmtInt(r.total))}`;
  if (h) {
    html += kpi("Có chiều cao thực", fmtInt(h.real_count), `${fmtNum(h.real_pct, 2)}% · từ cột chiều cao/số tầng`)
      + kpi("Outlier", fmtInt(h.outlier_count), "giá trị gốc ngoài khoảng hợp lệ")
      + kpi("h_m trung vị", `${fmtNum(h.stats.median)} m`, `TB ${fmtNum(h.stats.mean)} · p90 ${fmtNum(h.stats.p90)} · p99 ${fmtNum(h.stats.p99)}`)
      + kpi("h_m cao nhất", `${fmtNum(h.stats.max)} m`, `thấp nhất ${fmtNum(h.stats.min)} m`);
  }
  html += `</div>`;

  const flow = [`Input ${fmtInt(acc.input_count)}`, `GPKG ${fmtInt(acc.normalized_count)}`];
  if (acc.pmtiles_features != null) flow.push(`PMTiles ${fmtInt(acc.pmtiles_features)} feature`);
  const tiles = r.pmtiles;
  html += `<p class="accounting">Đối soát: ${flow.join(" → ")}${acc.filtered_out ? ` · lọc bỏ ${fmtInt(acc.filtered_out)}` : ""}${acc.has_parts != null ? ` · ${fmtInt(acc.has_parts)} toà có khối con, ${fmtInt(acc.is_part)} khối con` : ""}${tiles ? ` · ${fmtInt(tiles.addressed_tiles)} tile z${tiles.minzoom}–${tiles.maxzoom}, ${fmtBytes(tiles.size_bytes)}${tiles.verified ? " · verify OK" : ""}` : ""}</p>`;

  if (h) {
    html += section("Phân bố h_m", "số building theo khoảng chiều cao", hbars(h.histogram.map((b) => ({ label: b.label, count: b.count, pct: b.pct }))));
    html += section("Nguồn chiều cao (h_src)", "height = cột chiều cao · levels = số tầng · default = mặc định", hbars(h.by_source.map((s) => ({ label: s.src, count: s.count, pct: s.pct }))));
    if (h.raw) {
      html += `<p class="accounting">Cột gốc <code>${esc(h.raw.column)}</code>: trống ${fmtInt(h.raw.null)} · ≤ 0 m: ${fmtInt(h.raw.zero_or_negative)} · min ${fmtNum(h.raw.min)} · max ${fmtNum(h.raw.max)} m</p>`;
    }
  }

  for (const cat of r.categories) {
    const max = Math.max(...cat.rows.map((x) => x.count), 1);
    const rows = cat.rows.map((x) => `<tr>
      <td>${x.value == null ? '<span class="hint">(trống)</span>' : esc(x.value)}</td>
      <td class="n">${fmtInt(x.count)}</td>
      <td class="n"><span class="inbar" style="width:${Math.max(2, (60 * x.count) / max)}px"></span>${fmtNum(x.pct, 2)}%</td>
      ${h ? `<td class="n">${fmtNum(x.mean_h)}</td><td class="n">${fmtNum(x.max_h)}</td><td class="n">${fmtNum(x.default_pct)}%</td>` : ""}
    </tr>`).join("");
    const head = `<tr><th>Giá trị</th><th class="n">Số lượng</th><th class="n">%</th>${h ? '<th class="n">h_m TB</th><th class="n">h_m max</th><th class="n">% mặc định</th>' : ""}</tr>`;
    const more = cat.other_count > 0 ? `<p class="hint">+ ${fmtInt(cat.other_count)} building thuộc các giá trị khác</p>` : "";
    html += section(`Theo ${cat.column}`, "", `<div class="tbl-wrap"><table><thead>${head}</thead><tbody>${rows}</tbody></table></div>${more}`);
  }

  if (h) {
    html += section(`Outlier (${fmtInt(h.outlier_count)})`, "giá trị gốc ngoài khoảng hợp lệ — cần kiểm tra lại dữ liệu nguồn", featureTable(r.outliers, pm, idCol, true));
    html += section("Cao nhất (hợp lệ)", "top 20 theo h_m", featureTable(r.tallest, pm, idCol, false));
  }
  const md = job.outputs.report_md;
  if (md) html += `<div class="out-actions"><a class="btn small" href="/api/files/${esc(md.token)}?download=1" download>Tải báo cáo .md</a><a class="btn small" href="/api/files/${esc(job.outputs.report_json.token)}?download=1" download>Tải báo cáo .json</a></div>`;
  $("#report").innerHTML = html;
}

function featureTable(rows, pm, idCol, isOutlier) {
  if (!rows?.length) return `<p class="hint">Không có.</p>`;
  const skip = new Set(["bbox", "center", "fid"]);
  const keys = Object.keys(rows[0]).filter((k) => !skip.has(k));
  const head = `<tr>${keys.map((k) => `<th class="${typeof rows[0][k] === "number" ? "n" : ""}">${esc(k)}${k === "h_m" && isOutlier ? " (sau xử lý)" : ""}</th>`).join("")}<th></th></tr>`;
  const body = rows.map((r) => {
    const cells = keys.map((k) => {
      const v = r[k];
      if (typeof v === "number") return `<td class="n">${fmtNum(v, 2)}</td>`;
      return `<td class="${k === idCol ? "id" : ""}" title="${esc(v)}">${v == null ? "" : esc(v)}</td>`;
    }).join("");
    let link = "";
    if (r.center) {
      const params = { name: pm?.name || "", lon: r.center[0].toFixed(6), lat: r.center[1].toFixed(6), z: "17.5" };
      if (idCol && r[idCol] != null) Object.assign(params, { idcol: idCol, id: String(r[idCol]) });
      link = pm ? `<a class="btn small" href="${previewUrl(pm.token, params)}" target="_blank" rel="noopener">Xem 3D</a>`
        : `<span class="hint mono">${r.center[1].toFixed(5)}, ${r.center[0].toFixed(5)}</span>`;
    }
    return `<tr>${cells}<td>${link}</td></tr>`;
  }).join("");
  return `<div class="tbl-wrap scroll-y"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>`;
}

// ------------------------------------------------------------------ history
async function loadHistory() {
  let list;
  try { list = await api("/api/jobs"); } catch { return; }
  $("#historyCard").hidden = list.length === 0;
  $("#history").innerHTML = list.map((j) => `
    <li><button type="button" data-job="${esc(j.id)}" class="${j.id === state.jobId ? "active" : ""}">
      <span><span class="badge kind">${esc(KIND_LABEL[j.kind] || j.kind)}</span> ${esc(j.name)} <span class="badge ${j.status}">${STATUS_LABEL[j.status] || j.status}</span></span>
      <span class="when">${new Date(j.created * 1000).toLocaleTimeString("vi-VN")}</span>
    </button></li>`).join("");
  $$("#history [data-job]").forEach((b) => b.addEventListener("click", () => showJob(b.dataset.job, true)));
}

init();
