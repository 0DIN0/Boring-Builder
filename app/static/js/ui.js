/* Boring Builder — UI helpers: toasts, info "i" icons, theme toggle.
   Toast markup and classes match the chat app so both look identical. */
(function () {
  "use strict";

  // ---- Toasts (chat-compatible) ----
  function host() {
    let h = document.getElementById("toasts");
    if (!h) { h = document.createElement("div"); h.id = "toasts"; h.className = "toasts"; document.body.appendChild(h); }
    return h;
  }
  function toast(message, kind) {
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = message;
    host().appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      setTimeout(() => el.remove(), 220);
    }, 3000);
  }

  // ---- Info "i" popovers ----
  let activePop = null;
  function closePop() { if (activePop) { activePop.remove(); activePop = null; } }
  function escapeHtml(s) {
    return (s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function showPop(icon) {
    closePop();
    const text = icon.getAttribute("data-info-text");
    const title = icon.getAttribute("data-info-title");
    const pop = document.createElement("div");
    pop.className = "info-pop";
    pop.innerHTML = (title ? "<b>" + escapeHtml(title) + "</b><br>" : "") + escapeHtml(text);
    document.body.appendChild(pop);
    const r = icon.getBoundingClientRect();
    let top = r.bottom + 8, left = r.left;
    const pw = Math.min(300, window.innerWidth - 24);
    if (left + pw > window.innerWidth - 12) left = window.innerWidth - pw - 12;
    if (top + pop.offsetHeight > window.innerHeight - 12) top = r.top - pop.offsetHeight - 8;
    pop.style.top = Math.max(12, top) + "px";
    pop.style.left = Math.max(12, left) + "px";
    activePop = pop;
  }
  function upgradeInfoIcons(root) {
    (root || document).querySelectorAll("[data-info]").forEach((el) => {
      if (el._infoUpgraded) return;
      el._infoUpgraded = true;
      const icon = document.createElement("i");
      icon.className = "info-icon";
      icon.textContent = "i";
      icon.tabIndex = 0;
      icon.setAttribute("role", "button");
      icon.setAttribute("aria-label", "More information");
      icon.setAttribute("data-info-text", el.getAttribute("data-info"));
      const t = el.getAttribute("data-info-title");
      if (t) icon.setAttribute("data-info-title", t);
      icon.addEventListener("mouseenter", () => showPop(icon));
      icon.addEventListener("mouseleave", closePop);
      icon.addEventListener("focus", () => showPop(icon));
      icon.addEventListener("blur", closePop);
      icon.addEventListener("click", (e) => { e.stopPropagation(); activePop ? closePop() : showPop(icon); });
      el.appendChild(icon);
    });
  }
  document.addEventListener("click", closePop);

  // ---- Theme toggle (dark / light), persisted like the chat app ----
  function readPref() {
    try { return JSON.parse(localStorage.getItem("boringbuilder-appearance") || "{}") || {}; } catch (e) { return {}; }
  }
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    const meta = document.getElementById("themeColorMeta");
    if (meta) meta.setAttribute("content", theme === "light" ? "#ffffff" : "#05070e");
    document.querySelectorAll(".theme-dark-icon").forEach((i) => i.classList.toggle("hidden", theme === "light"));
    document.querySelectorAll(".theme-light-icon").forEach((i) => i.classList.toggle("hidden", theme !== "light"));
  }
  function initTheme() {
    const pref = readPref();
    applyTheme(pref.theme === "light" ? "light" : "dark");
    const btn = document.getElementById("themeBtn");
    if (btn) btn.addEventListener("click", () => {
      const now = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
      const p = readPref(); p.theme = now;
      try { localStorage.setItem("boringbuilder-appearance", JSON.stringify(p)); } catch (e) {}
      applyTheme(now);
    });
  }

  document.addEventListener("DOMContentLoaded", () => { upgradeInfoIcons(); initTheme(); });

  window.UI = { toast, upgradeInfoIcons };
})();
