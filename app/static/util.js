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

/** PUT a dropped file to /api/upload, reporting progress (0–100). Resolves to {path, size}. */
export function uploadFile(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/upload?name=${encodeURIComponent(file.name)}`);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress((100 * e.loaded) / e.total); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || "{}"); } catch { /* keep empty */ }
      if (xhr.status < 300) resolve(data); else reject(new Error(data.error || `HTTP ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("Upload thất bại."));
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
