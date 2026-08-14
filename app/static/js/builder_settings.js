/* Builder settings page controller. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const KEYS = ["builder_model", "builder_output_dir", "builder_num_ctx",
    "builder_temperature", "builder_default_mode", "builder_max_files",
    "builder_keep_unzipped", "builder_auto_export"];

  async function load() {
    let d;
    try { d = await (await fetch("/builder/settings/data")).json(); } catch (e) { return; }

    // populate the model dropdown from installed builder models
    try {
      const md = await (await fetch("/builder/models")).json();
      const sel = $("s_builder_model");
      sel.innerHTML = '<option value="">Use .env default (' + (d.resolved_model || "") + ')</option>' +
        md.models.map((m) => '<option value="' + m.name + '">' + m.name + '</option>').join("");
    } catch (e) {}

    KEYS.forEach((k) => {
      const el = $("s_" + k);
      if (!el) return;
      if (el.type === "checkbox") el.checked = !!d[k];
      else el.value = (d[k] === 0 || d[k] === "0") ? "" : (d[k] ?? "");
    });
    $("resolvedOut").textContent = "Currently saving to: " + (d.resolved_output_dir || "");
  }

  async function save() {
    const body = {};
    KEYS.forEach((k) => {
      const el = $("s_" + k);
      if (!el) return;
      if (el.type === "checkbox") body[k] = el.checked;
      else if (el.type === "number") body[k] = el.value === "" ? 0 : parseInt(el.value, 10);
      else body[k] = el.value;
    });
    await fetch("/builder/settings/data", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const s = $("bsSaved"); s.style.display = "inline"; setTimeout(() => (s.style.display = "none"), 1800);
    load();
  }

  $("bsSave").addEventListener("click", save);

  async function poll() {
    try {
      const { online } = await (await fetch("/api/status")).json();
      const s = $("daemonStatus");
      s.className = "status topbar-status " + (online ? "online" : "offline");
      s.querySelector(".label").textContent = online ? "daemon online" : "daemon offline";
    } catch (e) {}
  }
  async function loadHardware() {
    const body = document.getElementById("hwBody");
    if (!body) return;
    let h;
    try { h = await (await fetch("/builder/hardware")).json(); }
    catch (e) { body.innerHTML = '<p class="hint-text">Hardware detection unavailable.</p>'; return; }

    const gpu = (h.gpus && h.gpus[0]) ? (h.gpus[0].vendor + " " + h.gpus[0].name) : "No GPU (CPU only)";
    const installed = new Set(h.installed || []);
    const rec = h.recommended || {};
    const fits = (rec.code && rec.code.fits) ? rec.code.fits : [];
    const tight = (rec.code && rec.code.tight) ? rec.code.tight : [];

    function modelRow(m, label) {
      const have = installed.has(m.name);
      return '<li><code class="mono">' + m.name + '</code> · ' + m.params +
        (have ? ' · <span style="color:var(--success)">installed</span>'
              : ' · <span class="hint-text">ollama pull ' + m.name + '</span>') +
        (label ? ' · <span class="hint-text">' + label + '</span>' : '') + '</li>';
    }

    body.innerHTML =
      '<div style="display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:13px;margin-bottom:12px">' +
        '<span class="hint-text">GPU</span><span>' + gpu + '</span>' +
        '<span class="hint-text">Usable VRAM</span><span>' + (h.usable_vram_gb || 0) + ' GB</span>' +
        '<span class="hint-text">RAM</span><span>' + (h.ram_gb || '?') + ' GB</span>' +
        '<span class="hint-text">CPU cores</span><span>' + ((h.cpu && h.cpu.cores) || '?') + '</span>' +
      '</div>' +
      '<p style="font-size:13px;margin:0 0 6px"><strong>Recommended for you:</strong> ' +
        '<code class="mono">' + (rec.builder_model || '') + '</code> with context ' + (rec.num_ctx || 4096) + '.</p>' +
      (fits.length ? '<div class="section-label" style="margin-top:10px">Fits your GPU</div><ul class="tips">' + fits.map(m => modelRow(m)).join("") + '</ul>' : '') +
      (tight.length ? '<div class="section-label" style="margin-top:8px">Runs, but slower (CPU offload)</div><ul class="tips">' + tight.map(m => modelRow(m)).join("") + '</ul>' : '') +
      (h.tips && h.tips.length ? '<div class="section-label" style="margin-top:8px">Tips</div><ul class="tips">' + h.tips.map(t => '<li>' + t + '</li>').join("") + '</ul>' : '');

    const applyBtn = document.getElementById("hwApply");
    if (applyBtn && rec.builder_model) {
      applyBtn.style.display = "inline-flex";
      applyBtn.onclick = () => {
        const ctx = document.getElementById("s_builder_num_ctx");
        if (ctx) ctx.value = rec.num_ctx || 4096;
        const sel = document.getElementById("s_builder_model");
        if (sel) {
          // add the option if it isn't listed yet, then select it
          if (![...sel.options].some(o => o.value === rec.builder_model))
            sel.add(new Option(rec.builder_model + " (recommended)", rec.builder_model));
          sel.value = rec.builder_model;
        }
        if (window.UI) window.UI.toast("Applied — review and Save");
      };
    }
  }

  load(); loadHardware(); poll(); setInterval(poll, 15000);
})();
