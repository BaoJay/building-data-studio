// UI style switch: VMAP (default, tokens.css) or the old Studio Indochine (tokens-indochine.css).
// Plain script in <head> so html[data-style] is set before first paint. The map palette is read
// from the tokens once per page (basemap.js), so a switch reloads the page.
(() => {
  const KEY = "bds-style";
  const read = () => { try { return localStorage.getItem(KEY); } catch { return null; } };
  const root = document.documentElement;
  if (read() === "indochine") root.dataset.style = "indochine";
  const label = () => (root.dataset.style === "indochine" ? "Style: Indochine" : "Style: VMAP");
  document.addEventListener("DOMContentLoaded", () => {
    for (const btn of document.querySelectorAll("[data-style-toggle]")) {
      btn.textContent = label();
      btn.title = "Chuyển giữa style VMAP (mới) và Studio Indochine (cũ)";
      btn.addEventListener("click", () => {
        const next = root.dataset.style === "indochine" ? "vmap" : "indochine";
        try { localStorage.setItem(KEY, next); } catch { /* private mode: falls back to VMAP next load */ }
        if (next === "indochine") root.dataset.style = "indochine"; else delete root.dataset.style;
        location.reload();
      });
    }
  });
})();
