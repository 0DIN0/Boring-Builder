/* Boring AI — Builder workspace controller.
   Handles the four hash-routed views (build / models / history / guide),
   streams builds and model creation, and manages history. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const CTX = JSON.parse($("bctx").textContent);
  let controller = null;

  // ---- view routing (hash-based, no reload) ------------------------------
  const VIEWS = ["build", "models", "history", "guide"];
  const CRUMB = { build: "Build a project", models: "Builder models", history: "Build history", guide: "How to use" };
  function showView(name) {
    if (!VIEWS.includes(name)) name = "build";
    VIEWS.forEach((v) => {
      const el = $("view-" + v);
      if (el) el.classList.toggle("hidden", v !== name);
    });
    document.querySelectorAll(".bnav").forEach((a) => {
      const h = (a.getAttribute("href") || "").split("#")[1] || "build";
      if (a.id) a.classList.toggle("active", h === name);
    });
    const crumb = $("builderCrumb");
    if (crumb) crumb.textContent = CRUMB[name] || "Builder";
    if (name === "models") loadModels();
    if (name === "history") loadHistory();
  }
  function currentHash() { return (location.hash || "#build").replace("#", ""); }
  window.addEventListener("hashchange", () => showView(currentHash()));

  // ---- spec templates ----------------------------------------------------
  const TEMPLATES = {
    auth: "Build a Flask + MySQL module for user accounts: signup with email OTP verification, login that requires one OTP per day, secure sessions, password hashing (bcrypt), and SQLAlchemy models for User and OtpToken. Read all secrets from .env. Include input validation and clear error handling.",
    payments: "Build a Yoco payment module for a Flask app: a Course model, a purchase/checkout flow using Yoco's API, a secure webhook endpoint that verifies the signature and marks a course as owned by a user, and idempotent handling so a course is never double-granted. Secrets from .env.",
    admin: "Build an admin-only Flask blueprint: a dashboard listing users with search, view/edit/delete a user, block/unblock, and create/edit/delete courses. Protect every route so only users with is_admin can reach it. Server-side templates, no inline secrets.",
    deploy: "Create a deployment stack for a Flask + MySQL app: a production Dockerfile (gunicorn), docker-compose.yml wiring Flask + MySQL + nginx, an nginx.conf reverse proxy with sensible timeouts and gzip, and a cloudflared tunnel config. All tokens and credentials referenced from .env, with a .env.example.",
    crud: "Build a clean REST CRUD API in Flask for a single resource (e.g. Note): SQLAlchemy model, list/create/read/update/delete endpoints with validation, pagination on the list endpoint, JSON error responses, and a small pytest test file. MySQL via .env.",
  };
  document.querySelectorAll(".tpl-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const t = TEMPLATES[chip.dataset.tpl];
      if (!t) return;
      $("bSpec").value = t;
      if ($("bProject").value === "my-project" || !$("bProject").value)
        $("bProject").value = chip.dataset.tpl + "-module";
      $("bSpec").focus();
    });
  });

  // ---- build streaming ---------------------------------------------------
  function logLine(host, text, kind) {
    const row = document.createElement("div");
    row.className = "build-line" + (kind ? " " + kind : "");
    row.textContent = text;
    host.appendChild(row);
    host.scrollTop = host.scrollHeight;
  }

  async function readSSE(res, onEvent) {
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) {
        const raw = part.replace(/^data: /, "").trim();
        if (!raw) continue;
        try { onEvent(JSON.parse(raw)); } catch (e) {}
      }
    }
  }

  async function build() {
    const spec = $("bSpec").value.trim();
    if (spec.length < 20) { alert("Describe what to build in a bit more detail."); return; }
    const log = $("bLog");
    log.innerHTML = "";
    $("bProgressCard").classList.remove("hidden");
    $("bDownload").classList.add("hidden");
    $("bFill").style.width = "0%";
    $("bRun").disabled = true; $("bStop").disabled = false;
    controller = new AbortController();
    let planned = 0, done = 0, buildId = null;

    try {
      const res = await fetch("/builder/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          spec, project: $("bProject").value.trim() || "project",
          model: $("bModel").value, mode: $("bMode").value,
        }),
        signal: controller.signal,
      });
      if (!res.ok) { logLine(log, "Request failed: " + res.status, "err"); return; }

      await readSSE(res, (e) => {
        if (e.event === "created") buildId = e.build_id;
        else if (e.event === "start") logLine(log, "▸ Building “" + e.project + "” (" + e.mode + " mode)");
        else if (e.event === "phase") logLine(log, "• " + e.message);
        else if (e.event === "planned") {
          planned = e.count;
          logLine(log, "✓ Planned " + e.count + " files:", "ok");
          e.files.forEach((f) => logLine(log, "    " + f, "dim"));
        }
        else if (e.event === "file_start") logLine(log, "  → " + e.path + "  (" + e.index + "/" + e.total + ")");
        else if (e.event === "file_done") {
          done = e.index || done + 1;
          const total = e.total || planned || done;
          $("bFill").style.width = Math.round((done / total) * 100) + "%";
          logLine(log, "  ✓ " + e.path + "  (" + e.bytes + " B)" + (e.note ? "  — " + e.note : ""), e.note ? "warn" : "ok");
        }
        else if (e.event === "file_skip") logLine(log, "  ⨯ skipped " + e.path + " — " + e.reason, "err");
        else if (e.event === "error") logLine(log, "Error: " + e.message, "err");
        else if (e.event === "done") {
          $("bFill").style.width = "100%";
          logLine(log, "✓ Done — " + e.files + " files, " + (e.bytes / 1024).toFixed(1) + " KB, " + e.seconds + "s", "ok");
          if (e.exported) {
            if (e.exported.error) logLine(log, "  ! output folder: " + e.exported.error, "warn");
            else {
              if (e.exported.zip) logLine(log, "  ✓ saved: " + e.exported.zip, "ok");
              if (e.exported.folder) logLine(log, "  ✓ saved: " + e.exported.folder + "/", "ok");
            }
          }
          if (buildId) { const a = $("bDownload"); a.href = "/builder/" + buildId + "/download"; a.classList.remove("hidden"); }
        }
      });
    } catch (err) {
      logLine(log, err.name === "AbortError" ? "Stopped." : "Error: " + err.message, err.name === "AbortError" ? "warn" : "err");
    } finally {
      $("bRun").disabled = false; $("bStop").disabled = true;
    }
  }
  $("bRun").addEventListener("click", build);
  $("bStop").addEventListener("click", () => { if (controller) controller.abort(); });

  // ---- models ------------------------------------------------------------
  async function loadModels() {
    // populate the build-tab model picker too
    let data;
    try { data = await (await fetch("/builder/models")).json(); } catch (e) { return; }
    const grid = $("mGrid");
    const empty = $("mEmpty");
    if (grid) {
      grid.innerHTML = "";
      if (!data.models.length) { empty.style.display = "block"; }
      else {
        empty.style.display = "none";
        data.models.forEach((m) => grid.appendChild(modelCard(m, data.default)));
      }
    }
    // build-tab + settings dropdowns
    const sel = $("bModel");
    if (sel) {
      const keep = sel.value;
      sel.innerHTML = '<option value="">Default (' + CTX.defaultModel + ')</option>' +
        data.models.map((m) => '<option value="' + m.name + '">' + m.name + '</option>').join("");
      sel.value = keep;
    }
  }

  function modelCard(m, def) {
    const el = document.createElement("div");
    el.className = "bmodel-card" + (m.name === def ? " is-default" : "");
    el.innerHTML =
      '<div class="bmodel-name">' + m.name + (m.name === def ? ' <span class="tag active">default</span>' : "") + '</div>' +
      '<div class="bmodel-specs">' +
        (m.parameter_size ? '<span class="chip">' + m.parameter_size + '</span>' : "") +
        (m.quantization ? '<span class="chip">' + m.quantization + '</span>' : "") +
        (m.size_human ? '<span class="chip">' + m.size_human + '</span>' : "") +
      '</div>' +
      '<div class="bmodel-actions">' +
        '<button class="btn btn-sm" data-use="' + m.name + '">Use for next build</button>' +
        '<button class="btn btn-sm btn-danger" data-del="' + m.name + '">Delete</button>' +
      '</div>';
    el.querySelector("[data-use]").addEventListener("click", () => {
      const sel = $("bModel"); if (sel) sel.value = m.name;
      location.hash = "#build";
      if (window.UI) window.UI.toast("Next build will use " + m.name);
    });
    el.querySelector("[data-del]").addEventListener("click", async () => {
      if (!confirm("Delete " + m.name + "? This removes the model from Ollama.")) return;
      await fetch("/builder/models/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: m.name }) });
      loadModels();
    });
    return el;
  }

  $("mRefresh") && $("mRefresh").addEventListener("click", loadModels);

  // new-model modal
  const STARTER = (function () { try { return JSON.parse($("mfStarter").textContent); } catch (e) { return ""; } })();
  async function openModelModal() {
    $("mLog").classList.add("hidden"); $("mLog").innerHTML = "";
    $("mName").value = ""; $("mNamePreview").textContent = "";
    let bases = [];
    try { bases = (await (await fetch("/builder/models/bases")).json()).bases || []; } catch (e) {}
    const baseSel = $("mBase");
    baseSel.innerHTML = bases.map((b) => '<option value="' + b + '">' + b + '</option>').join("");
    const preferred = bases.find((b) => /coder|code/i.test(b)) || bases[0] || "qwen2.5-coder:3b";
    baseSel.value = preferred;
    setModelfile(preferred);
    $("mModal").classList.add("show");
  }
  function setModelfile(base) {
    $("mModelfile").value = STARTER.replace("{base}", base || "qwen2.5-coder:3b");
  }
  function nameToBuilder(v) {
    let raw = (v || "").trim().toLowerCase();
    if (raw.startsWith("builder-")) raw = raw.slice(8);
    const slug = raw.replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
    return slug ? "builder-" + slug : "";
  }
  $("mNew") && $("mNew").addEventListener("click", openModelModal);
  $("mClose").addEventListener("click", () => $("mModal").classList.remove("show"));
  $("mCancel").addEventListener("click", () => $("mModal").classList.remove("show"));
  $("mModal").addEventListener("click", (e) => { if (e.target === $("mModal")) $("mModal").classList.remove("show"); });
  $("mName").addEventListener("input", () => { $("mNamePreview").textContent = nameToBuilder($("mName").value) || "(enter a name)"; });
  $("mBase").addEventListener("change", () => {
    // only rewrite FROM line if the editor still matches the starter shape
    const cur = $("mModelfile").value;
    const rewritten = cur.replace(/^FROM\s+.*$/m, "FROM " + $("mBase").value);
    $("mModelfile").value = rewritten;
  });

  $("mCreate").addEventListener("click", async () => {
    const name = $("mName").value.trim();
    const modelfile = $("mModelfile").value;
    if (!name) { alert("Give the model a name."); return; }
    const log = $("mLog"); log.classList.remove("hidden"); log.innerHTML = "";
    $("mCreate").disabled = true;
    try {
      const res = await fetch("/builder/models/create", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, modelfile }),
      });
      if (!res.ok) { const e = await res.json().catch(() => ({})); logLine(log, e.error || ("Failed: " + res.status), "err"); return; }
      let ok = false;
      await readSSE(res, (e) => {
        if (e.event === "start") logLine(log, "Creating " + e.name + "…");
        else if (e.event === "status") logLine(log, "  " + e.message, "dim");
        else if (e.event === "error") logLine(log, "Error: " + e.message, "err");
        else if (e.event === "done") {
          logLine(log, "✓ Created " + e.name, "ok");
          if (e.saved) logLine(log, "  ✓ Modelfile saved: " + e.saved, "dim");
          ok = true;
        }
      });
      if (ok) { if (window.UI) window.UI.toast("Model created"); setTimeout(() => { $("mModal").classList.remove("show"); loadModels(); }, 700); }
    } catch (err) {
      logLine(log, "Error: " + err.message, "err");
    } finally {
      $("mCreate").disabled = false;
    }
  });

  // ---- history -----------------------------------------------------------
  async function loadHistory() {
    let data;
    try { data = await (await fetch("/builder/history")).json(); } catch (e) { return; }
    $("hOutDir").textContent = data.output_dir;
    const list = $("hList"), empty = $("hEmpty");
    list.innerHTML = "";
    if (!data.builds.length) { empty.style.display = "block"; return; }
    empty.style.display = "none";
    data.builds.forEach((b) => list.appendChild(historyItem(b)));
  }

  function historyItem(b) {
    const when = b.created_at ? new Date(b.created_at * 1000).toLocaleString() : "";
    const el = document.createElement("div");
    el.className = "bhist-item";
    const exported = b.exported && (b.exported.folder || b.exported.zip);
    el.innerHTML =
      '<div class="bhist-body">' +
        '<div class="bhist-title">' + esc(b.project) + '</div>' +
        '<div class="bhist-meta">' + (b.files || 0) + ' files · ' + b.model + ' · ' + (b.mode || "") + ' · ' + when + '</div>' +
        '<div class="bhist-spec">' + esc(b.spec_preview || "") + '</div>' +
        (exported ? '<div class="bhist-export">saved: ' + esc(b.exported.folder || b.exported.zip) + '</div>' : "") +
      '</div>' +
      '<div class="bhist-actions">' +
        '<button class="btn btn-sm" data-rerun>Re-run</button>' +
        '<a class="btn btn-sm" href="/builder/' + b.id + '/download" download>Zip</a>' +
        '<button class="btn btn-sm btn-danger" data-forget title="Remove from history">✕</button>' +
      '</div>';
    el.querySelector("[data-rerun]").addEventListener("click", () => {
      $("bSpec").value = b.spec || b.spec_preview || "";
      $("bProject").value = b.project || "project";
      $("bMode").value = b.mode || "manifest";
      const sel = $("bModel");
      if (sel && b.model) { [...sel.options].forEach((o) => { if (o.value === b.model) sel.value = b.model; }); }
      location.hash = "#build";
      if (window.UI) window.UI.toast("Loaded — press Build to re-run");
    });
    el.querySelector("[data-forget]").addEventListener("click", async () => {
      await fetch("/builder/history/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: b.id }) });
      loadHistory();
    });
    return el;
  }

  function esc(s) { const d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }

  // ---- daemon status -----------------------------------------------------
  async function poll() {
    try {
      const { online } = await (await fetch("/api/status")).json();
      const s = $("daemonStatus");
      s.className = "status topbar-status " + (online ? "online" : "offline");
      s.querySelector(".label").textContent = online ? "daemon online" : "daemon offline";
    } catch (e) {}
  }

  // ---- boot --------------------------------------------------------------
  loadModels();
  poll(); setInterval(poll, 15000);
  showView(currentHash());
})();
