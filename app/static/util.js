// Shared helpers for the main page (convert + diff tabs).

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
export const nf = new Intl.NumberFormat("vi-VN");
export const fmtInt = (n) => (n == null ? "—" : nf.format(n));
export const fmtNum = (n, d = 1) => (n == null ? "—" : new Intl.NumberFormat("vi-VN", { maximumFractionDigits: d }).format(n));
export const fmtBytes = (n) => {
  if (n == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
  return `${fmtNum(v, i ? 1 : 0)} ${units[i]}`;
};
export const fmtDur = (s) => (s == null ? "" : s < 60 ? `${fmtNum(s, 1)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`);

export async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

/** fetch() that throws the server's {error} message on a non-2xx answer. */
export async function fetchOk(url, init) {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `HTTP ${res.status}`);
  return res;
}

let toastTimer;
export function toast(message) {
  let el = $(".toast");
  if (!el) { el = document.createElement("div"); el.className = "toast"; el.setAttribute("role", "status"); document.body.append(el); }
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
}

export async function reveal(token) {
  try { await api("/api/reveal", { method: "POST", body: { token } }); } catch (e) { toast(e.message); }
}

export const previewUrl = (token, params = {}) => {
  const q = new URLSearchParams({ f: token, ...params });
  return `/preview?${q}`;
};

export function section(title, hint, body) {
  return `<div class="report-section"><h3>${esc(title)} ${hint ? `<span class="hint">${esc(hint)}</span>` : ""}</h3>${body}</div>`;
}

export function hbars(items) {
  const max = Math.max(...items.map((i) => i.count), 1);
  return `<div class="hbars">${items.map((i) => `
    <div class="hbar" title="${esc(i.label)}: ${fmtInt(i.count)} (${fmtNum(i.pct, 2)}%)">
      <span class="lbl">${esc(i.label)}</span>
      <span class="track"><span class="fill ${i.count ? "" : "zero"}" style="width:${(100 * i.count) / max}%"></span></span>
      <span class="val">${fmtInt(i.count)}<em>${fmtNum(i.pct, 2)}%</em></span>
    </div>`).join("")}</div>`;
}

export const kpi = (label, value, sub = "", cls = "") =>
  `<div class="kpi ${cls}"><div class="label">${esc(label)}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;

/** <option>s for picking one column of a probed file (value "" = none). */
export function colOptions(fields, selected, none = "— không dùng —") {
  const opts = [`<option value="">${esc(none)}</option>`];
  for (const f of fields) {
    opts.push(`<option value="${esc(f.name)}" ${f.name === selected ? "selected" : ""}>${esc(f.name)} (${esc(f.type)})</option>`);
  }
  return opts.join("");
}

/** Click, keyboard and drag-and-drop on a drop zone all hand the chosen file to onFile. */
export function bindDropzone(dz, input, onFile) {
  dz.addEventListener("click", () => input.click());
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  input.addEventListener("change", () => { if (input.files[0]) onFile(input.files[0]); input.value = ""; });
  ["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => dz.addEventListener(t, () => dz.classList.remove("over")));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    if (e.dataTransfer.files[0]) onFile(e.dataTransfer.files[0]);
  });
}

/** PUT a file (by default to /api/upload), reporting progress (0–100). Resolves to the JSON answer. */
export function uploadFile(file, onProgress, url = `/api/upload?name=${encodeURIComponent(file.name)}`) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress((100 * e.loaded) / e.total); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || "{}"); } catch { /* keep empty */ }
      if (xhr.status < 300) resolve(data); else reject(new Error(data.error || `HTTP ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("mất kết nối tới server"));
    xhr.send(file);
  });
}

/** Read and write a JSON object in localStorage; private mode / blocked storage just forgets. */
export function loadJSON(key, fallbackKey) {
  try {
    return JSON.parse(localStorage.getItem(key) || (fallbackKey && localStorage.getItem(fallbackKey)) || "{}") || {};
  } catch { return {}; }
}
export function saveJSON(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode: ignore */ }
}
