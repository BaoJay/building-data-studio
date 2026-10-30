// The landmark drawer of the 3D preview: HTML of its views (list, building clicked, landmark) and
// the readouts that follow the map and the model. State and actions live in landmark-workspace.js;
// this module only reads them.
import { $, esc, fmtBytes, fmtInt, fmtNum } from "/static/util.js";
import { LL, anchorPoint, modelHeight, modelSize } from "/static/landmark-model.js";

const HEIGHT_TOLERANCE = { m: 2, ratio: 0.05 };  // the height check warns past both
const EARTH_M = 40075016.686;  // equator length
const COMPASS = ["bắc", "đông", "nam", "tây"];

// ------------------------------------------------------------------ whole drawer

export function render(ws) {
  $("#lmCount").textContent = ws.lib.landmarks.length ? fmtInt(ws.lib.landmarks.length) : "";
  const s = ws.sel;
  $("#lmBody").innerHTML = s?.kind === "landmark" ? landmarkHtml(ws, s.rec) : s?.kind === "candidate" ? candidateHtml(ws, s.c) : listHtml(ws);
  if (!s) renderList(ws);
  if (ws.draft && $("#placeSec")) syncPlacement(ws);
  renderQa(ws);
}

// What follows the models loading: list badges, the load note, the readouts.
export function renderStatus(ws) {
  if (!ws.sel) renderList(ws);
  if (ws.draft && $("#placeSec")) syncPlacement(ws);
  renderQa(ws);
}

// ------------------------------------------------------------------ list

function listHtml(ws) {
  return `<section class="lm-sec">
    <input type="text" id="lmSearch" value="${esc(ws.search)}" placeholder="Tìm tên, building_id, ID liên kết" autocomplete="off" spellcheck="false" aria-label="Tìm landmark">
    <div class="hint"><span id="lmHere"></span> Click một building trên bản đồ để xem thuộc tính và đăng ký landmark.</div>
    <ul class="lm-items" id="lmItems"></ul>
  </section>
  <section class="lm-sec lm-list-foot">
    <div class="hint">Thư mục lưu: <span class="mono">${esc(ws.lib.dir)}</span></div>
    <div class="btn-row">
      <button class="btn small" type="button" data-act="open-dir">Mở thư mục</button>
      <a class="btn small" href="/api/landmarks/manifest.json?download=1" download title="Version đang dùng của mọi landmark, kèm hướng và toạ độ neo">Tải manifest</a>
    </div>
  </section>`;
}

function renderList(ws) {
  const box = $("#lmItems");
  if (!box) return;
  const q = ws.search.trim().toLowerCase();
  const all = ws.lib.landmarks;
  const here = all.filter((l) => l.same_source);
  const drawn = here.filter((l) => l.versions).length;
  const noGlb = here.length - drawn;
  $("#lmCount").textContent = all.length ? fmtInt(all.length) : "";
  $("#lmHere").textContent = here.length
    ? `${fmtInt(here.length)} landmark trên PMTiles này, ${fmtInt(drawn)} có GLB được vẽ trên bản đồ${noGlb ? `, ${fmtInt(noGlb)} chưa có GLB (tô màu riêng trên bản đồ)` : ""}.`
    : "PMTiles này chưa có landmark nào.";
  // The landmarks of this PMTiles first; the others open read-only (they are not drawn here).
  const items = all.filter((l) => !q || [l.name, l.building_id, l.link].some((v) => String(v || "").toLowerCase().includes(q)))
    .sort((a, b) => Number(Boolean(b.same_source)) - Number(Boolean(a.same_source)));
  box.innerHTML = items.map((l) => {
    const badge = l.in_use != null ? `<span class="badge done">v${l.in_use}</span>` : `<span class="badge">${l.versions ? "chưa dùng" : "chưa có GLB"}</span>`;
    let meta = l.in_use != null ? `v${l.in_use} đang dùng · ${l.versions} version` : `${l.versions} version`;
    if (l.same_source && l.in_use == null && l.versions) meta += ` · bản đồ hiện v${l.versions} (mới nhất)`;
    if (!l.same_source) meta += ` · PMTiles khác (${l.source_name || "?"})`;
    else if (l.versions) meta += loadState(ws, l.id);
    return `<li><button type="button" data-lm="${esc(l.id)}" class="${l.same_source ? "" : "lm-other"}">
      <span class="nm">${esc(l.name || l.building_id)}</span>${badge}
      <span class="meta">${l.name ? `<span class="mono">${esc(l.building_id)}</span> · ` : ""}${esc(meta)}</span></button></li>`;
  }).join("") || `<li class="hint lm-empty">${all.length ? "Không có landmark khớp." : "Chưa có landmark nào."}</li>`;
}

function loadState(ws, id) {
  const st = ws.store.status(id).state;
  return st === "loading" || st === "queued" ? " · đang tải GLB" : st === "error" ? " · lỗi GLB" : "";
}

// ------------------------------------------------------------------ building clicked

function backRow(title, kicker) {
  return `<div class="lm-kicker">${kicker}</div>
    <div class="lm-title-row">${title}<button class="btn small" type="button" data-act="close" title="Về danh sách landmark">← Danh sách</button></div>`;
}

function candidateHtml(ws, c) {
  const s = ws.host.state;
  const rows = Object.entries(c.props).slice(0, 30).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("");
  const name = c.props.name || c.props["name:vi"] || "";
  const target = c.parent
    ? `<div class="field"><span>Chọn</span>
        <label class="check"><input type="radio" name="regTarget" value="${esc(c.parent)}" ${c.target === c.parent ? "checked" : ""}> Cả building (${esc(s.parentAttr)} = <code>${esc(c.parent)}</code>)</label>
        <label class="check"><input type="radio" name="regTarget" value="${esc(c.id)}" ${c.target === c.id ? "checked" : ""}> Chỉ khối này (<code>${esc(c.id)}</code>)</label></div>`
    : "";
  return `<section class="lm-sec">
    ${backRow(`<h2 class="lm-title mono">${esc(c.target)}</h2>`, "Building chưa có trong thư viện")}
    ${target}
    ${rows ? `<table class="props">${rows}</table>` : `<div class="hint">Đang đọc thuộc tính…</div>`}
  </section>
  <section class="lm-sec">
    <h3>Đăng ký landmark</h3>
    <label class="field"><span>Tên landmark</span><input type="text" id="regName" value="${esc(name)}" placeholder="vd. Nhà Quốc Hội Việt Nam"></label>
    <label class="field"><span>ID liên kết (tuỳ chọn)</span><input type="text" id="regLink" spellcheck="false" placeholder="canonical id MapOps, way/…, mã nội bộ"></label>
    <div class="btn-row"><button class="btn primary" type="button" data-act="register">Đăng ký landmark</button></div>
    <div class="hint">App đọc footprint của building trong <b>${esc(ws.host.name())}</b> (zoom ${s.maxzoom}) và lưu cùng landmark.</div>
  </section>
  ${tryHtml(ws, false)}
  ${ws.draft ? placementHtml(ws, null) : ""}
  ${qaHtml(ws)}`;
}

// ------------------------------------------------------------------ landmark

const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString("vi-VN", { dateStyle: "short", timeStyle: "short" }) : "");
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleDateString("vi-VN") : "");

function statusBadge(v) {
  if (v.status === "in_use") return `<span class="badge done">đang dùng</span>`;
  if (v.status === "used") {
    const until = v.used.at(-1)?.[1];
    return `<span class="badge" title="Đã dùng ${v.used.map(([a, b]) => `${fmtTime(a)} → ${fmtTime(b)}`).join("; ")}">đã dùng${until ? ` đến ${fmtDate(until)}` : ""}</span>`;
  }
  return `<span class="badge lm-unused">chưa dùng</span>`;
}

// Model height above its anchor, from the box the server read in the GLB.
function serverHeight(v, p = v.placement) {
  const b = v.model.bounds;
  return b ? (b[1][1] - (p.anchor === "bbox" ? b[0][1] : 0)) * p.scale : null;
}

function versionRows(ws, rec, here) {
  return [...rec.versions].reverse().map((v) => {
    const n = v.version;
    const showing = here && ws.preview?.kind === "version" && ws.preview.n === n;
    const h = serverHeight(v);
    const flags = [
      v.model.unreadable.length ? `<div class="warn">Nén ${esc(v.model.unreadable.join(", "))}: bản đồ không đọc được</div>` : "",
      v.same_as ? `<div class="hint">Trùng file với v${v.same_as}</div>` : "",
    ].join("");
    const use = v.status === "in_use"
      ? `<button class="btn small" type="button" data-act="use" data-v="">Bỏ dùng</button>`
      : `<button class="btn small primary" type="button" data-act="use" data-v="${n}">Dùng</button>`;
    const show = !here ? "" : showing ? `<span class="hint">Đang xem</span>` : `<button class="btn small" type="button" data-act="show" data-v="${n}">Xem</button>`;
    const model = [fmtBytes(v.size), `${fmtInt(v.model.triangles)} tam giác`, h == null ? "" : `cao ${fmtNum(h, 1)} m`].filter(Boolean).join(" · ");
    let row = `<tr class="${showing ? "showing" : ""}">
      <td><b>v${n}</b><div>${statusBadge(v)}</div></td>
      <td class="lm-wrap"><div>${esc(fmtTime(v.uploaded_at))}${v.by ? ` · ${esc(v.by)}` : ""}</div>
        <div class="mono lm-file" title="${esc(v.original_name)}">${esc(v.original_name)}</div>
        <div class="hint">${model}</div>${flags}
        ${v.note ? `<div class="lm-note">${esc(v.note)}</div>` : ""}</td>
      <td><div class="lm-acts">${show}${use}
        <a class="btn small" href="/api/landmarks/${encodeURIComponent(rec.id)}/versions/${n}/glb?download=1" download>Tải</a>
      </div></td></tr>`;
    if (ws.confirmUse !== undefined && (ws.confirmUse === n || (ws.confirmUse === null && v.status === "in_use"))) {
      const what = ws.confirmUse === null ? `Bỏ dùng v${n}? Landmark sẽ không có version nào đang dùng.`
        : rec.in_use != null ? `Dùng v${n} thay cho v${rec.in_use}?` : `Dùng v${n}?`;
      row += `<tr class="lm-confirm"><td colspan="3"><div class="lm-confirm-body"><b>${what}</b>
        <input type="text" id="useNote" placeholder="Ghi chú cho lịch sử (tuỳ chọn)">
        <div class="btn-row"><button class="btn small primary" type="button" data-act="use-ok">Xác nhận</button>
        <button class="btn small" type="button" data-act="use-cancel">Huỷ</button></div></div></td></tr>`;
    }
    return row;
  }).join("");
}

function historyHtml(rec) {
  const label = (ev) => {
    switch (ev.action) {
      case "register": return "Đăng ký landmark";
      case "upload": return `Upload v${ev.version}`;
      case "placement": return `Lưu placement v${ev.version}`;
      case "edit": return `Sửa ${ev.fields.map((f) => (f === "name" ? "tên" : "ID liên kết")).join(", ")}`;
      case "use":
        if (ev.to == null) return `Bỏ dùng v${ev.from}`;
        return ev.from == null ? `Dùng v${ev.to}` : `Dùng v${ev.to} thay v${ev.from}`;
      default: return ev.action;
    }
  };
  return [...rec.history].reverse().map((ev) => `<li class="${ev.action === "use" ? "use" : ""}">
    <span class="what">${esc(label(ev))}</span><span class="when">${esc(fmtTime(ev.at))}${ev.by ? ` · ${esc(ev.by)}` : ""}</span>
    ${ev.note ? `<div class="lm-note">${esc(ev.note)}</div>` : ""}</li>`).join("");
}

function landmarkHtml(ws, rec) {
  const fp = rec.footprint;
  const here = ws.sameSource(rec);
  const inUse = rec.in_use != null ? `<span class="badge done">v${rec.in_use} đang dùng</span>` : `<span class="badge">chưa dùng version nào</span>`;
  const head = ws.editing
    ? `<label class="field"><span>Tên landmark</span><input type="text" id="editName" value="${esc(rec.name)}"></label>
       <label class="field"><span>ID liên kết</span><input type="text" id="editLink" value="${esc(rec.link)}" spellcheck="false"></label>
       <div class="btn-row"><button class="btn small primary" type="button" data-act="edit-save">Lưu</button><button class="btn small" type="button" data-act="edit-cancel">Huỷ</button></div>`
    : `<dl class="kv">
        <dt>building_id</dt><dd class="mono">${esc(rec.building_id)}</dd>
        <dt>ID liên kết</dt><dd class="mono">${rec.link ? esc(rec.link) : `<span class="hint">—</span>`}</dd>
        <dt>Footprint</dt><dd>${fmtNum(fp.area_m2, 0)} m² · ${fp.height == null ? "không có chiều cao" : `cao ${fmtNum(fp.height, 1)} m`}
          <span class="hint">${fp.matched === "parent" ? "(gộp các khối con)" : ""}</span></dd>
        <dt>Tâm</dt><dd class="mono">${fp.centroid[1].toFixed(7)}, ${fp.centroid[0].toFixed(7)}</dd>
        <dt>Nguồn</dt><dd>${esc(rec.source?.name || "—")} <span class="hint">${esc(rec.source?.layer || "")}</span></dd>
        <dt>Đăng ký</dt><dd>${esc(fmtTime(rec.created_at))}</dd>
      </dl>
      <div class="btn-row"><button class="btn small" type="button" data-act="edit">Sửa tên / ID liên kết</button>
        <button class="btn small" type="button" data-act="folder">Mở thư mục</button>
        ${here ? `<button class="btn small" type="button" data-act="fly">Tới landmark</button>` : ""}</div>`;
  const other = here ? "" : otherSourceHtml(ws, rec);
  const editing = ws.preview?.kind === "version" ? rec.versions.find((v) => v.version === ws.preview.n) : null;
  return `<section class="lm-sec">
    ${backRow(`<h2 class="lm-title">${esc(rec.name || rec.building_id)}</h2>`, `Landmark · ${rec.versions.length} version`)}
    <div>${inUse}</div>
    ${here ? drawnNote(rec) : ""}
    ${other}
    ${head}
  </section>
  ${here ? tryHtml(ws, true) : uploadOnlyHtml(ws)}
  ${rec.versions.length ? `<section class="lm-sec">
    <h3>Version</h3>
    <div class="tbl-wrap"><table class="lm-versions"><thead><tr><th>Version</th><th>File</th><th></th></tr></thead>
      <tbody>${versionRows(ws, rec, here)}</tbody></table></div>
  </section>` : ""}
  ${here && ws.draft ? placementHtml(ws, editing) : ""}
  ${here ? qaHtml(ws) : ""}
  <section class="lm-sec">
    <h3>Lịch sử</h3>
    <ol class="lm-history">${historyHtml(rec)}</ol>
  </section>
  <section class="lm-sec lm-foot">
    <button class="btn small danger" type="button" data-act="delete">Xoá landmark…</button>
    <span class="hint">Thư mục được chuyển vào <code>_trash/</code>, không file nào bị xoá.</span>
  </section>`;
}

// What the map shows of a landmark that has no version in use.
function drawnNote(rec) {
  if (rec.in_use != null) return "";
  if (!rec.versions.length) return `<div class="hint">Chưa có file GLB: building vẫn hiện bằng extrusion, tô màu riêng. Upload GLB ở mục <b>File GLB</b> bên dưới.</div>`;
  const v = rec.versions.at(-1);
  return `<div class="hint">Bản đồ đang hiện v${v.version} (version mới nhất) vì chưa dùng version nào. Bấm <b>Dùng</b> để đưa nó vào manifest cho Mobile.</div>`;
}

// A landmark registered from another PMTiles: kept in the list, never drawn on this one.
function otherSourceHtml(ws, rec) {
  const summary = ws.byId.get(rec.id);
  const link = summary?.source_token ? ws.sourceUrl(summary.source_token, rec.id) : null;
  return `<div class="alert"><span class="icon">i</span><div>Landmark này đăng ký trên PMTiles khác (<b>${esc(rec.source?.name || "?")}</b>), nên không vẽ trên bản đồ này.
    ${link ? `<a href="${esc(link)}">Mở trên PMTiles của nó</a>.` : "File PMTiles đó không còn trên máy."}</div></div>`;
}

// GLB of the landmark (or of the building clicked): a local file is only drawn in this browser
// until "Upload version mới" is pressed.
function tryHtml(ws, canUpload) {
  const local = ws.preview?.kind === "local" ? ws.preview : null;
  const base = ws.sel?.kind === "landmark" ? ws.baseVersion() : null;
  return `<section class="lm-sec">
    <h3>${canUpload ? "File GLB" : "Thử file GLB"}</h3>
    <label class="field"><span>Chọn file .glb để xem thử trên bản đồ (chỉ đọc trong trình duyệt${canUpload ? ", chưa upload" : ""})</span>
      <input type="file" id="lmFile" accept=".glb,model/gltf-binary"></label>
    ${local ? `<div class="hint">Đang xem thử <b class="mono">${esc(local.name)}</b> (${fmtBytes(local.file.size)}), chưa upload.</div>` : ""}
    ${canUpload ? `<input type="text" id="upNote" placeholder="Ghi chú cho version mới (tuỳ chọn)">` : ""}
    <div class="btn-row">
      ${canUpload ? `<button class="btn primary" type="button" data-act="upload" ${local && !ws.uploading ? "" : "disabled"}>Upload version mới</button>` : ""}
      ${local ? `<button class="btn" type="button" data-act="drop-local">Bỏ file thử</button>` : ""}
      <span class="hint" id="upStatus"></span></div>
    <div class="progress" id="upBar" hidden><span></span></div>
    ${canUpload ? `<div class="hint">${base ? `Version mới bắt đầu với placement của v${base.version}; placement đang chỉnh (nếu khác) giữ lại để bạn lưu.` : "Version đầu tiên đặt ở tâm footprint, hướng 0."}</div>`
      : `<div class="hint">Mô hình đặt ở tâm footprint của building. Đăng ký landmark để upload file và lưu placement.</div>`}
  </section>`;
}

function uploadOnlyHtml(ws) {
  return `<section class="lm-sec">
    <h3>Upload GLB</h3>
    <input type="file" id="lmFile" accept=".glb,model/gltf-binary" aria-label="File GLB">
    <input type="text" id="upNote" placeholder="Ghi chú (tuỳ chọn)">
    <div class="btn-row"><button class="btn primary" type="button" data-act="upload" ${ws.uploading ? "disabled" : ""}>Upload version mới</button>
      <span class="hint" id="upStatus"></span></div>
    <div class="progress" id="upBar" hidden><span></span></div>
  </section>`;
}

// ------------------------------------------------------------------ placement

// v: the version whose placement is edited (saved with it); null: a local file, nothing to save.
function placementHtml(ws, v) {
  const title = v ? `Placement v${v.version}` : `Placement thử (${esc(ws.preview?.name || "")})`;
  return `<section class="lm-sec lm-place" id="placeSec">
    <h3>${title} <span class="badge running" id="placeDirty" hidden>chưa lưu</span></h3>
    <div class="field"><span>Hướng (độ, theo chiều kim đồng hồ nhìn từ trên xuống)</span>
      <div class="lm-heading">
        <input type="range" min="0" max="360" step="0.5" data-p="heading" aria-label="Hướng">
        <input type="number" step="0.5" data-p="heading" aria-label="Hướng (độ)">
        <button class="btn small" type="button" data-act="flip" title="Cộng 180°">Xoay 180°</button>
      </div>
      <div class="hint">Hướng 0: trục +X của GLB chỉ hướng đông, mặt trước (+Z) quay về hướng nam.</div>
    </div>
    <div class="row2">
      <label class="field"><span>Tỉ lệ</span><input type="number" step="0.01" min="0" data-p="scale"></label>
      <label class="field"><span>Điểm neo trên mô hình</span>
        <select data-p="anchor"><option value="origin">Gốc GLB</option><option value="bbox">Tâm đáy hộp bao</option></select></label>
    </div>
    <div class="field"><span>Toạ độ neo (lon, lat)</span>
      <div class="row2">
        <input type="number" step="0.0000001" data-p="lon" aria-label="Kinh độ (lon)">
        <input type="number" step="0.0000001" data-p="lat" aria-label="Vĩ độ (lat)">
      </div>
      <div class="btn-row">
        <button class="btn small" type="button" data-act="pick" aria-pressed="false">Chọn trên bản đồ</button>
        <button class="btn small" type="button" data-act="centroid">Về tâm footprint</button>
      </div>
      <div class="hint">Điểm neo trên mô hình đứng ở toạ độ này, trên mặt đất. Có thể kéo điểm ◆ trên bản đồ.</div>
    </div>
    <div class="hint lm-readout" id="placeReadout"></div>
    ${v ? `<div class="btn-row">
      <button class="btn primary" type="button" data-act="save-place">Lưu placement</button>
      <button class="btn" type="button" data-act="reset-place">Hoàn tác</button>
    </div>` : ""}
  </section>`;
}

// Values, readouts and buttons of the placement editor, without rebuilding it (keeps the focus
// and the slider drag).
export function syncPlacement(ws) {
  const box = $("#placeSec");
  if (!box || !ws.draft) return;
  const d = ws.draft;
  for (const el of box.querySelectorAll("[data-p]")) {
    if (el === document.activeElement && el.type !== "range") continue;
    const value = d[el.dataset.p];
    el.value = el.dataset.p === "heading" && el.type === "number" ? String(Math.round(value * 1000) / 1000) : String(value);
  }
  const dirty = ws.isDirty();
  $("#placeDirty").hidden = !dirty;
  box.querySelector('[data-act="save-place"]')?.toggleAttribute("disabled", !dirty);
  box.querySelector('[data-act="reset-place"]')?.toggleAttribute("disabled", !dirty);
  box.querySelector('[data-act="pick"]').setAttribute("aria-pressed", String(ws.picking));
  const fp = ws.footprint();
  const rows = [];
  const note = ws.loadNote();
  if (note) rows.push(`<div class="${note.warn ? "warn" : ""}">${esc(note.text)}</div>`);
  if (fp) rows.push(`<div>Cách tâm footprint ${fmtNum(distanceM(fp.centroid, [d.lon, d.lat]), 1)} m.</div>`);
  const model = ws.shownModel();
  const v = ws.previewVersion();
  const h = model ? modelHeight(model, d) : v ? serverHeight(v, d) : null;
  if (h != null && fp) rows.push(heightCheck(h, fp, d.scale));
  $("#placeReadout").innerHTML = rows.join("");
}

function heightCheck(modelH, fp, scale) {
  if (fp.height == null) return `<div>Mô hình cao ${fmtNum(modelH, 1)} m trên điểm neo. Building không có chiều cao để so.</div>`;
  const diff = modelH - fp.height;
  const bad = Math.abs(diff) > HEIGHT_TOLERANCE.m && Math.abs(diff) > HEIGHT_TOLERANCE.ratio * fp.height;
  const fit = modelH > 0 ? (scale * fp.height) / modelH : null;
  const sign = diff >= 0 ? "+" : "";
  return `<div class="${bad ? "warn" : ""}">Mô hình cao ${fmtNum(modelH, 1)} m, building ${fmtNum(fp.height, 1)} m (${esc(fp.height_field || "chiều cao")}) → lệch `
    + `${sign}${fmtNum(diff, 1)} m (${sign}${fmtNum((100 * diff) / (fp.height || 1), 1)}%).${bad && fit ? ` Tỉ lệ khớp chiều cao: ${fit.toPrecision(4)}.` : ""}</div>`;
}

// Ground distance in metres between two [lon, lat] (short distances).
export function distanceM(a, b) {
  const k = (EARTH_M / 360) * Math.cos((a[1] * Math.PI) / 180);
  return Math.hypot((b[0] - a[0]) * k, ((b[1] - a[1]) * EARTH_M) / 360);
}

// ------------------------------------------------------------------ QA: light and the building's own extrusion

function qaHtml(ws) {
  if (!ws.draft) return "";
  return `<section class="lm-sec">
    <details class="lmk" id="qaSec" ${ws.qaOpen ? "open" : ""}>
      <summary>Kiểm tra ánh sáng (QA)</summary>
      <div class="lmk-body">
        <div class="hint">Mô hình đọc <code>light</code> của style như extrusion xung quanh. Công thức: <code>app/static/landmark-light.js</code>.</div>
        <label class="check"><input type="checkbox" id="lmkCompare" ${ws.compare ? "checked" : ""}> So sánh: hiện extrusion gốc của building bên cạnh (đục, màu so sánh)</label>
        <label class="check"><input type="checkbox" id="lmkFlat" ${ws.flat ? "checked" : ""}> Tô landmark một màu (màu so sánh) để so mái / tường với extrusion</label>
        <div class="hint lmk-info" id="lmkInfo"></div>
        <details class="lmk-uniforms"><summary>Uniform ánh sáng (để đối chiếu khi port)</summary><pre id="lmkUniforms"></pre></details>
      </div>
    </details>
  </section>`;
}

export function renderQa(ws) {
  const info = $("#lmkInfo");
  if (!info) return;
  const model = ws.shownModel();
  const d = ws.draft;
  const fp = ws.footprint();
  const rows = [];
  if (model && d) {
    const b = model.bounds;
    const [sx, sz] = modelSize(model, d);
    rows.push(`<div><b>${esc(model.name)}</b>: ${model.meshes} mesh, ${model.materials.length} vật liệu. `
      + `Hộp bao ${fmtNum(sx, 1)} × ${fmtNum(sz, 1)} m mặt bằng, cao ${fmtNum(modelHeight(model, d), 1)} m trên điểm neo.</div>`);
    const a = anchorPoint(model, { anchor: "bbox" });
    const off = Math.hypot(a.x, b.min.y, a.z) * d.scale;
    if (off > 1) rows.push(`<div>Gốc GLB cách tâm đáy hộp bao ${fmtNum(off, 1)} m.</div>`);
  }
  if (fp && d) {
    rows.push(`<div>Neo tại ${d.lat.toFixed(6)}, ${d.lon.toFixed(6)}, cách tâm footprint ${fmtNum(distanceM(fp.centroid, [d.lon, d.lat]), 1)} m. `
      + `Extrusion của ${fp.matched === "parent" ? "các khối con của " : ""}building đã ẩn khi mô hình tải xong.</div>`);
  }
  if (d) rows.push(shadowRow(ws));
  if (d) rows.push(lightRows(ws));
  info.innerHTML = rows.join("");
  $("#lmkUniforms").textContent = model && d ? uniformDump(ws, d) : "";
}

// Which ground shadow the landmark casts (app/landmark_shadow.py): its GLB once a version is in
// use, else the LoD1 box of its building.
function shadowRow(ws) {
  const rec = ws.sel?.kind === "landmark" ? ws.sel.rec : null;
  const [, az, polar] = ws.layer.light.position;
  const sun = `nắng từ ${fmtNum(az, 0)}°, cao ${fmtNum(90 - polar, 0)}°`;
  if (ws.layer.night) return `<div>Bóng mặt đất: không có (ban đêm không đổ bóng).</div>`;
  if (!rec || rec.in_use === null) return `<div>Bóng mặt đất: theo hộp LoD1 của building (chưa có version đang dùng).</div>`;
  const viewing = ws.preview?.kind === "version" && ws.preview.n === rec.in_use && !ws.isDirty() ? "" : ` <span class="warn">Bản đang xem chưa đổ bóng: bóng đi theo v${rec.in_use} với placement đã lưu.</span>`;
  return `<div>Bóng mặt đất: theo mesh GLB v${rec.in_use} (${sun}), gộp với bóng LoD1 xung quanh. Ban đêm và nền tối không đổ bóng.${viewing}</div>`;
}

function lightRows(ws) {
  const l = ws.layer.light;
  const [r, az, polar] = l.position;
  const extra = l.anchor === "viewport" ? ` + bearing ${fmtNum(ws.map.getBearing(), 0)}°` : "";
  const warn = l.problems.length ? ` <span class="warn">Không dùng được ${l.problems.join(", ")} của light (expression?): dùng giá trị mặc định.</span>` : "";
  const level = ws.layer.emissive;
  const emissive = level > 0 ? `Emissive: mức ${fmtNum(level, 1)} theo preset (dawn 0,3 · day 0 · dusk 0,6 · night 1).` : "Emissive: tắt (day và nền tối; bật ở dawn 0,3 · dusk 0,6 · night 1).";
  return `<div>Light của style (anchor ${l.anchor}): màu ${rgb(l.color)}, cường độ ${l.intensity}, `
    + `[${r}, ${az}°${extra}, ${polar}°]. ${emissive}${warn}</div>`
    + `<div class="lmk-swatches">Màu mong đợi với màu so sánh ${esc(ws.host.referenceColor())}: ${expectedSwatches(ws)}</div>`;
}

// Roof and walls facing N / E / S / W, top of the wall, through the CPU copy of the shader.
function expectedSwatches(ws) {
  const base = (LL.parseColor(ws.host.referenceColor()) || [1, 1, 1]).slice(0, 3);
  const model = ws.shownModel();
  const heightM = ws.footprint()?.height ?? (model ? modelHeight(model, ws.draft) : 0);
  const opts = { bearing: ws.map.getBearing(), heightM, t: 1 };
  const faces = [["mái", [0, 0, 1]], ...COMPASS.map((name, i) => [`tường ${name}`, [Math.sin((i * Math.PI) / 2), Math.cos((i * Math.PI) / 2), 0]])];
  return faces.map(([name, n]) => {
    const c = LL.shade(base, n.map((v) => (Math.abs(v) < 1e-12 ? 0 : v)), ws.layer.light, opts);
    return `<span class="lmk-sw" title="${esc(name)}: ${rgb(c)}"><i style="background:${rgb(c)}"></i>${esc(name)}</span>`;
  }).join("");
}

function uniformDump(ws, d) {
  const u = ws.layer.uniforms(d.heading);
  const v = (a) => `[${a.map((x) => x.toFixed(3)).join(", ")}]`;
  return [
    `heading_deg = ${d.heading}`,
    `scale = ${d.scale}`,
    `L_enu = ${v(u.lightposEnu)}`,
    `u_lightpos = ${v(u.u_lightpos)}`,
    `u_lightcolor = ${v(u.u_lightcolor)}`,
    `u_lightintensity = ${u.u_lightintensity}`,
    `u_north = ${v(u.u_north)}`,
    `u_height_m = ${modelHeight(ws.shownModel(), d).toFixed(2)}`,
    `u_emissive_on = ${u.u_emissive_on}`,
  ].join("\n");
}

function rgb(c) {
  return `rgb(${c.map((v) => Math.round(v * 255)).join(", ")})`;
}
