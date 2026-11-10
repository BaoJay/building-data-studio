// The landmark drawer of the 3D preview: HTML of its views (list, building clicked, landmark) and
// the readouts that follow the map and the model. State and actions live in landmark-workspace.js;
// this module only reads them.
import { $, esc, fmtBytes, fmtInt, fmtNum } from "/static/util.js";
import { LL, anchorPoint, modelHeight, modelSize } from "/static/landmark-model.js";
import { LOD_SLOTS, zoomLabel } from "/static/landmark-store.js";

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
    const glbUrl = `/api/landmarks/${encodeURIComponent(rec.id)}/versions/${n}/glb`;
    const lods = v.model.lod ? [{ ...v.model.lod, own: true, size: v.size }, ...(v.lods || [])].sort((a, b) => b.zoom_range[0] - a.zoom_range[0]) : [];
    const dl = (x) => `${glbUrl}?${x.own ? "" : `z=${x.lod_zoom}&`}download=1`;
    const lodLinks = lods.length ? `<div class="hint">Theo zoom: ${lods.map((x) => `<a href="${dl(x)}" download title="Tải file z${x.lod_zoom} (${fmtBytes(x.size)})">z${x.lod_zoom}</a> ${esc(zoomLabel(x.zoom_range))}`).join(" · ")}</div>` : "";
    const groups = Object.entries(v.model.emissive_groups || {}).map(([g, c]) => `${esc(g)} ${c}`);
    if (v.model.glass) groups.push(`kính ${v.model.glass}`);
    const extras = [groups.join(" · "), v.model.anchor ? "có anchor" : ""].filter(Boolean).join(" · ");
    let row = `<tr class="${showing ? "showing" : ""}">
      <td><b>v${n}</b><div>${statusBadge(v)}</div></td>
      <td class="lm-wrap"><div>${esc(fmtTime(v.uploaded_at))}${v.by ? ` · ${esc(v.by)}` : ""}</div>
        <div class="mono lm-file" title="${esc(v.original_name)}">${esc(v.original_name)}</div>
        <div class="hint">${model}</div>${lodLinks}${extras ? `<div class="hint">${extras}</div>` : ""}${flags}
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
      case "lod": return `Thêm file z${ev.lod_zoom} cho v${ev.version}`;
      case "edit": return `Sửa ${ev.fields.map((f) => (f === "name" ? "tên" : "ID liên kết")).join(", ")}`;
      case "bind": return `Gắn với canonical ${ev.dataset}: ${ev.to} (trước là ${ev.from}, build ${ev.build})`;
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
    <label class="field"><span>Chọn file .glb để xem thử trên bản đồ (chỉ đọc trong trình duyệt${canUpload ? ", chưa upload" : ""}). Chọn cả bộ z15 / z16 / z17 để đổi file theo zoom.</span>
      <input type="file" id="lmFile" accept=".glb,model/gltf-binary" multiple></label>
    ${lodSlotsHtml(ws)}
    ${local ? `<div class="hint">Đang xem thử ${localFiles(local)}, chưa upload.</div>` : ""}
    ${canUpload ? `<input type="text" id="upNote" placeholder="Ghi chú cho version mới (tuỳ chọn)">` : ""}
    <div class="btn-row">
      ${canUpload ? `<button class="btn primary" type="button" data-act="upload" ${local && !ws.uploading ? "" : "disabled"}>Upload version mới</button>` : ""}
      ${local ? `<button class="btn" type="button" data-act="drop-local">Bỏ file thử</button>` : ""}
      <span class="hint" id="upStatus"></span></div>
    <div class="progress" id="upBar" hidden><span></span></div>
    ${canUpload ? `<div class="hint">${base ? `Version mới bắt đầu với placement của v${base.version}; placement đang chỉnh (nếu khác) giữ lại để bạn lưu.` : "Version đầu tiên đặt ở tâm footprint, hướng 0."}
      GLB có <code>asset.extras.anchor</code> thì đặt tại anchor, hướng 0. Bộ nhiều file: file chi tiết nhất là version, các file còn lại thêm vào nó theo <code>zoom_range</code>.</div>`
      : `<div class="hint">Mô hình đặt ở tâm footprint của building. Đăng ký landmark để upload file và lưu placement.</div>`}
  </section>`;
}

// One file per level of detail: each slot draws its file at the slot's zoom range, whatever the
// GLB says, so a set without asset.extras.zoom_range can be uploaded too (ws.lodFiles).
function lodSlotsHtml(ws) {
  const rows = LOD_SLOTS.map(({ lod_zoom: z, zoom_range: r }) => {
    const f = ws.lodFiles[z];
    return `<div class="lm-lod-slot">
      <span><b>z${z}</b> <span class="hint">zoom ${esc(zoomLabel(r))}</span></span>
      <span class="mono lm-wrap">${f ? `${esc(f.name)} <span class="hint">${fmtBytes(f.size)}</span>` : `<span class="hint">chưa chọn</span>`}</span>
      <span class="btn-row"><label class="btn small">${f ? "Đổi" : "Chọn"}<input type="file" data-lod="${z}" accept=".glb,model/gltf-binary" hidden></label>
        ${f ? `<button class="btn small" type="button" data-act="lod-clear" data-lod="${z}" aria-label="Bỏ file z${z}">Bỏ</button>` : ""}</span>
    </div>`;
  }).join("");
  return `<div class="lm-lod-slots"><div class="hint">Hoặc chọn từng file theo mức zoom (LOD): file chi tiết nhất là version, các file còn lại thêm vào nó.</div>${rows}</div>`;
}

export function renderLodSlots(ws) {
  const el = document.querySelector(".lm-lod-slots");
  if (el) el.outerHTML = lodSlotsHtml(ws);
}

// The local files tried: name, zoom range, size.
function localFiles(local) {
  return local.files.map((f) => `<b class="mono">${esc(f.name)}</b>${f.info.lod ? ` (z${f.info.lod.lod_zoom}, zoom ${esc(zoomLabel(f.info.lod.zoom_range))})` : ""} ${fmtBytes(f.file.size)}`).join(", ");
}

function uploadOnlyHtml(ws) {
  return `<section class="lm-sec">
    <h3>Upload GLB</h3>
    <input type="file" id="lmFile" accept=".glb,model/gltf-binary" multiple aria-label="File GLB (một file hoặc cả bộ theo zoom)">
    ${lodSlotsHtml(ws)}
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
        <button class="btn small" type="button" data-act="glb-anchor" hidden title="Hướng 0, tỉ lệ 1, gốc GLB tại asset.extras.anchor">Đặt theo anchor của GLB</button>
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
  const anchor = ws.glbAnchor();
  box.querySelector('[data-act="glb-anchor"]').hidden = !anchor;
  const fp = ws.footprint();
  const rows = [];
  const note = ws.loadNote();
  if (note) rows.push(`<div class="${note.warn ? "warn" : ""}">${esc(note.text)}</div>`);
  if (anchor) rows.push(anchorRow(anchor, d));
  rows.push(lodRow(ws));
  if (fp) rows.push(`<div>Cách tâm footprint ${fmtNum(distanceM(fp.centroid, [d.lon, d.lat]), 1)} m.</div>`);
  const model = ws.shownModel();
  const v = ws.previewVersion();
  const h = model ? modelHeight(model, d) : v ? serverHeight(v, d) : null;
  if (h != null && fp) rows.push(heightCheck(h, fp, d.scale));
  $("#placeReadout").innerHTML = rows.join("");
}

// asset.extras.anchor of the GLB against the placement being edited.
function anchorRow(a, d) {
  const off = distanceM([a.lon, a.lat], [d.lon, d.lat]);
  const same = off < 0.05 && d.heading === 0 && d.scale === 1 && d.anchor === "origin";
  return same
    ? `<div>Đặt đúng <code>asset.extras.anchor</code> của GLB (${a.lat.toFixed(7)}, ${a.lon.toFixed(7)}), hướng 0.</div>`
    : `<div class="warn">GLB ghi anchor ${a.lat.toFixed(7)}, ${a.lon.toFixed(7)} (hướng 0, gốc GLB); placement đang lệch ${fmtNum(off, 1)} m`
      + `${d.heading !== 0 ? `, hướng ${fmtNum(d.heading, 1)}°` : ""}${d.scale !== 1 ? `, tỉ lệ ${d.scale}` : ""}${d.anchor !== "origin" ? ", neo tâm đáy hộp" : ""}. `
      + `Bấm <b>Đặt theo anchor của GLB</b>.</div>`;
}

// Which file of the landmark being edited the map draws at this zoom.
function lodRow(ws) {
  const f = ws.editedFiles();
  const z = fmtNum(ws.map.getZoom(), 2);
  if (!f?.lods) return `<div>Một file cho mọi zoom (GLB không ghi <code>zoom_range</code>).</div>`;
  const list = f.lods.map((x) => `${x === f.file ? "<b>" : ""}z${x.lod_zoom} ${esc(zoomLabel(x.zoom_range))}${x === f.file ? "</b>" : ""}`).join(" · ");
  const inside = f.lods.some((x) => x.zoom_range[0] <= ws.map.getZoom() && (x.zoom_range[1] === null || ws.map.getZoom() < x.zoom_range[1]));
  const out = inside ? "" : ` <span class="warn">Zoom ${z} ngoài mọi khoảng: app không vẽ landmark (giữ extrusion LoD1); ở đây vẫn hiện z${f.file?.lod_zoom} để chỉnh.</span>`;
  return `<div>Zoom ${z}: vẽ file <b>z${f.file?.lod_zoom}</b>. Các file: ${list}.${out}</div>`;
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
  $("#glbOpen").hidden = !ws.draft;
  renderContract(ws);
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
  return `<div>Bóng mặt đất: theo mesh GLB v${rec.in_use} (${sun}), gộp với bóng LoD1 xung quanh. Ban đêm không đổ bóng.${viewing}</div>`;
}

function lightRows(ws) {
  const l = ws.layer.light;
  const [r, az, polar] = l.position;
  const extra = l.anchor === "viewport" ? ` + bearing ${fmtNum(ws.map.getBearing(), 0)}°` : "";
  const warn = l.problems.length ? ` <span class="warn">Không dùng được ${l.problems.join(", ")} của light (expression?): dùng giá trị mặc định.</span>` : "";
  const preset = ws.layer.preset;
  const g = (row) => fmtNum(LL.EMISSIVE_GAIN[row][preset], 1);
  const emissive = !preset ? "Emissive và kính: tắt (style không có <code>tasco:light-preset</code>)."
    : `Preset <b>${preset}</b>: gain emissive LED ${g("led")} · led_wash ${g("led_wash")} · lobby ${g("lobby")} · windows ${g("windows")} · nhóm khác ${g("other")}; `
      + `kính phản chiếu ${fmtNum(LL.GLASS[preset].reflect, 1)}.`;
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
    `u_sky_zenith = ${v(u.u_sky_zenith)}`,
    `u_sky_horizon = ${v(u.u_sky_horizon)}`,
    `u_sky_ground = ${v(u.u_sky_ground)}`,
    `u_camera_pos = ${v(ws.shownModel().shared.u_camera_pos.value.toArray())}`,
    `preset = ${ws.layer.preset ?? "—"}, u_glass_reflect = ${u.u_glass_reflect}`,
  ].join("\n");
}

function rgb(c) {
  return `rgb(${c.map((v) => Math.round(v * 255)).join(", ")})`;
}

// ------------------------------------------------------------------ GLB contract (for the app)

const yes = (n, what = "") => (n ? `${n}${what ? ` ${what}` : ""}` : "—");

// What the GLB on screen holds of each value of LL.GLB_CONTRACT (plain text).
function contractValues(ws, model) {
  const info = model.materialInfo;
  const extras = model.asset?.extras || {};
  const count = (f) => info.filter(f).length;
  const tally = (key) => Object.entries(info.reduce((m, x) => (x[key] ? { ...m, [x[key]]: (m[x[key]] || 0) + 1 } : m), {}))
    .sort((a, b) => b[1] - a[1]).map(([k, n]) => `${k} ${n}`).join(" · ");
  const files = ws.editedFiles();
  const a = extras.anchor;
  const lod = extras.zoom_range ? `lod_zoom ${extras.lod_zoom ?? "—"}, zoom_range [${extras.zoom_range.map((x) => x ?? "null").join(", ")}]` : "";
  return {
    anchor: a ? `${a.lat}, ${a.lon}` : "—",
    zoom_range: [lod, files?.lods?.length > 1 ? `bộ ${files.lods.length} file: ${files.lods.map((x) => `z${x.lod_zoom} ${zoomLabel(x.zoom_range)}`).join(" · ")}` : ""].filter(Boolean).join("; ") || "—",
    emissive_group: tally("group") || "—",
    emissive_strength: yes(count((x) => x.mode && x.strength !== 1), "vật liệu ≠ 1"),
    emissiveFactor: yes(count((x) => Math.max(...x.factor) > 0), "vật liệu > 0"),
    emissiveTexture: yes(count((x) => x.emissiveMap), `vật liệu (texCoord 1: ${count((x) => x.texCoord === 1)})`),
    TEXCOORD_1: yes(count((x) => x.hasUv1), "vật liệu"),
    surface_type: tally("surfaceType") || "—",
    baseColor: `${info.length} vật liệu (texture: ${count((x) => x.baseMap)})`,
    NORMAL: model.flatNormals ? `thiếu ở ${model.flatNormals} mesh (tính phẳng)` : "có ở mọi mesh",
    alpha: [count((x) => x.alphaMask) && `MASK ${count((x) => x.alphaMask)}`, count((x) => x.doubleSided) && `doubleSided ${count((x) => x.doubleSided)}`].filter(Boolean).join(" · ") || "—",
    texture_transform: yes(count((x) => x.transform), "vật liệu"),
  };
}

// One row per material: group, blend, gain under the preset, surface, texCoord.
function materialRows(ws, model) {
  const preset = ws.layer.preset;
  return model.materialInfo.map((x) => {
    const e = LL.materialEmissive(preset, { emissive_group: x.group, emissive_strength: x.strength }, x.factor, x.khrStrength);
    return {
      name: x.name || "(không tên)", group: x.group || "—", mode: LL.EMISSIVE_MODE_NAMES[x.mode],
      gain: x.mode ? fmtNum(e.gain, 2) : "—", surface: x.surfaceType || "—", texCoord: x.emissiveMap ? String(x.texCoord) : "—",
      glass: x.glass,
    };
  }).sort((a, b) => (a.group === "—") - (b.group === "—") || a.group.localeCompare(b.group) || a.name.localeCompare(b.name));
}

export function renderContract(ws) {
  const box = $("#lmkContract");
  if (!box) return;
  const model = ws.shownModel();
  const values = model ? contractValues(ws, model) : {};
  const preset = ws.layer.preset;
  const contract = `<div class="tbl-wrap"><table class="props lmk-contract lmk-main"><thead><tr><th>Thuộc tính</th><th>Dùng để / quy tắc</th><th>GLB này</th></tr></thead><tbody>`
    + LL.GLB_CONTRACT.map((c) => `<tr><td><code>${esc(c.where).replace(/([.,]) ?/g, "$1<wbr>")}</code></td><td><b>${esc(c.use)}.</b> ${esc(c.rule)}</td><td>${esc(values[c.key] ?? "—")}</td></tr>`).join("")
    + `</tbody></table></div>`;
  const gains = gainsHtml(preset);
  $("#gainBody").innerHTML = gains;
  $("#gainPreset").textContent = preset || "";
  $("#gainPanel").hidden = false;
  const mats = model ? `<details class="lmk-uniforms"><summary>${model.materialInfo.length} vật liệu của GLB này (nhóm, cách trộn, gain ở preset ${esc(preset || "—")})</summary>
      <div class="tbl-wrap"><table class="props lmk-contract"><thead><tr><th>Vật liệu</th><th>emissive_group</th><th>Trộn</th><th>Gain</th><th>surface_type</th><th>texCoord</th></tr></thead><tbody>`
    + materialRows(ws, model).map((r) => `<tr><td class="mono">${esc(r.name)}</td><td>${esc(r.group)}</td><td>${esc(r.mode)}</td><td>${esc(r.gain)}</td><td>${esc(r.surface)}</td><td>${esc(r.texCoord)}</td></tr>`).join("")
    + `</tbody></table></div></details>` : `<div class="hint">Cột “GLB này” điền khi mô hình tải xong.</div>`;
  const ignored = `<div class="hint">Không đọc (cố ý): ${LL.GLB_IGNORED.map(esc).join("; ")}.</div>`;
  const open = box.querySelector("details")?.open;  // the material list stays open across redraws
  box.innerHTML = contract + gains + mats + ignored;
  if (open) box.querySelector("details")?.setAttribute("open", "");
}

// Emissive gains and glass of the four VMAP light presets, the one in use marked. Also shown
// outside the contract, in the map's gain panel (it follows the preset, not the landmark).
function gainsHtml(preset) {
  return `<div class="tbl-wrap"><table class="props lmk-contract"><thead><tr><th>Gain emissive</th>${LL.PRESETS.map((p) => `<th${p === preset ? ' class="on"' : ""}>${p}</th>`).join("")}</tr></thead><tbody>`
    + Object.entries(GAIN_ROWS).map(([row, label]) => `<tr><td>${esc(label)}</td>${LL.PRESETS.map((p) => `<td${p === preset ? ' class="on"' : ""}>${fmtNum(LL.EMISSIVE_GAIN[row][p], 1)}</td>`).join("")}</tr>`).join("")
    + `<tr><td>Kính: phản chiếu</td>${LL.PRESETS.map((p) => `<td${p === preset ? ' class="on"' : ""}>${fmtNum(LL.GLASS[p].reflect, 1)}</td>`).join("")}</tr>`
    + `<tr><td>Kính: zenith / horizon / ground</td>${LL.PRESETS.map((p) => `<td${p === preset ? ' class="on"' : ""}>${["zenith", "horizon", "ground"].map((k) => `<i class="lmk-dot" style="background:${LL.GLASS[p][k]}" title="${k} ${LL.GLASS[p][k]}"></i>`).join("")}</td>`).join("")}</tr>`
    + `</tbody></table></div>`;
}

const GAIN_ROWS = {
  led: "led_warm, led_white, led_line, glow, sign*, logo* (REPLACE)",
  led_wash: "led_wash (ADD)",
  lobby: "lobby (REPLACE)",
  windows: "windows* (REPLACE)",
  other: "nhóm khác (REPLACE)",
};

// The list as Markdown, for the app team.
export function contractMarkdown(ws) {
  const model = ws.shownModel();
  const values = model ? contractValues(ws, model) : {};
  const cell = (t) => String(t).replace(/\|/g, "\\|");
  const lines = [
    `# Thuộc tính GLB landmark mà renderer phải đọc`,
    "",
    `Nguồn: Building Data Studio, \`app/static/landmark-light.js\`${model ? `. GLB: ${model.name}` : ""}.`,
    "",
    "| Thuộc tính | Dùng để | Quy tắc | GLB này |",
    "|---|---|---|---|",
    ...LL.GLB_CONTRACT.map((c) => `| \`${cell(c.where)}\` | ${cell(c.use)} | ${cell(c.rule)} | ${cell(values[c.key] ?? "—")} |`),
    "",
    `| Gain emissive | ${LL.PRESETS.join(" | ")} |`,
    `|---|${LL.PRESETS.map(() => "---").join("|")}|`,
    ...Object.entries(GAIN_ROWS).map(([row, label]) => `| ${cell(label)} | ${LL.PRESETS.map((p) => LL.EMISSIVE_GAIN[row][p]).join(" | ")} |`),
    `| Kính: phản chiếu | ${LL.PRESETS.map((p) => LL.GLASS[p].reflect).join(" | ")} |`,
    `| Kính: zenith / horizon / ground | ${LL.PRESETS.map((p) => `${LL.GLASS[p].zenith} / ${LL.GLASS[p].horizon} / ${LL.GLASS[p].ground}`).join(" | ")} |`,
  ];
  if (model) {
    lines.push("", "| Vật liệu | emissive_group | Trộn | surface_type | texCoord |", "|---|---|---|---|---|",
      ...materialRows(ws, model).map((r) => `| ${cell(r.name)} | ${cell(r.group)} | ${r.mode} | ${cell(r.surface)} | ${r.texCoord} |`));
  }
  lines.push("", `Không đọc (cố ý): ${LL.GLB_IGNORED.join("; ")}.`);
  return lines.join("\n") + "\n";
}

export async function copyContract(ws) {
  const text = contractMarkdown(ws);
  const area = $("#contractText");
  try {
    await navigator.clipboard.writeText(text);
    $("#contractCopied").textContent = "Đã sao chép.";
    area.hidden = true;
  } catch {
    // No clipboard (permission): show the text to copy by hand.
    area.value = text;
    area.hidden = false;
    area.select();
    $("#contractCopied").textContent = "Trình duyệt chặn clipboard: chọn và sao chép trong ô dưới.";
  }
}
