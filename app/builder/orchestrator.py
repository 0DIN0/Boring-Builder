"""The engine that turns a one-line "build me X" into a folder of files.

Design goals, in order:

1. Reliable on modest hardware. A 3B-7B model on 4 GB of VRAM cannot emit a
   whole application correctly in a single response. So the default flow is
   two-phase — first PLAN the file list, then GENERATE each file in its own
   short request. Every request stays small, which fits in a tiny context
   window and dramatically improves quality.

2. Model-agnostic and swappable. All model access goes through one function,
   `ModelClient.chat`. Point it at Ollama today; to move to a hosted API for
   production, reimplement that one method. Nothing else changes.

3. Observable. `BuildJob.run` is a generator that yields event dicts, so the
   web layer can stream progress to the browser as it happens.
"""
import json
import re
import subprocess
import sys
import tempfile
import time

import requests

from . import protocol
from .workspace import Workspace, WorkspaceError, MAX_FILES


class ModelError(Exception):
    """A failure from the model backend, translated into plain, actionable text."""

    def __init__(self, model, status, detail=""):
        self.model = model
        self.status = status
        self.detail = detail or ""
        super().__init__(self._message())

    def _message(self):
        d = self.detail.lower()
        if any(k in d for k in ("out of memory", "cudamalloc", "oom",
                                "failed to allocate", "no available", "insufficient")):
            return (f"The model '{self.model}' ran out of memory — it's likely too "
                    f"large for your GPU. Use a smaller model (e.g. qwen2.5-coder:3b), "
                    f"lower the context size in Builder settings, and close other GPU "
                    f"apps. Builder settings → Hardware shows what fits.")
        if "not found" in d or "no such model" in d or self.status == 404:
            return (f"The model '{self.model}' isn't installed. Pull it with "
                    f"'ollama pull {self.model}', or choose an installed one in "
                    f"Builder settings.")
        if self.status == 500:
            return (f"The model backend failed (500) for '{self.model}'. This is "
                    f"most often not enough VRAM — try a smaller model or a lower "
                    f"context size (Builder settings → Hardware). "
                    f"Details: {self.detail or 'none'}")
        return f"Model error ({self.status}) for '{self.model}': {self.detail or 'unknown'}"


# --------------------------------------------------------------------------- #
# Model client — the single seam you change to swap AI providers.
# --------------------------------------------------------------------------- #
class ModelClient:
    """Talks to Ollama's /api/chat. Reimplement `chat` to use another backend."""

    def __init__(self, host: str, model: str, timeout: int = 600, options=None):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.options = options or {}

    def chat(self, system: str, user: str, stream_tokens=None) -> str:
        """Send one prompt, return the full text. If stream_tokens is given,
        it's called with each token chunk so the UI can show live output."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": True,
            "options": self.options,
        }
        parts = []
        with requests.post(
            f"{self.host}/api/chat", json=payload,
            stream=True, timeout=self.timeout,
        ) as resp:
            if resp.status_code >= 400:
                # Surface Ollama's actual message (out-of-memory, model missing,
                # etc.) instead of a bare status code.
                detail = ""
                try:
                    detail = (resp.json() or {}).get("error", "")
                except ValueError:
                    detail = (resp.text or "").strip()[:300]
                raise ModelError(self.model, resp.status_code, detail)
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if data.get("error"):
                    raise ModelError(self.model, 200, data["error"])
                chunk = (data.get("message") or {}).get("content", "")
                if chunk:
                    parts.append(chunk)
                    if stream_tokens:
                        stream_tokens(chunk)
                if data.get("done"):
                    break
        return "".join(parts)


# --------------------------------------------------------------------------- #
# Prompts. Kept terse and directive — small models follow short rules best.
# --------------------------------------------------------------------------- #
PLAN_SYSTEM = (
    "You are a senior software architect. You plan projects as a strict list "
    "of files. You output ONLY JSON — never prose, never markdown fences."
)

PLAN_USER = """Plan EVERY file needed to build the project below, then stop.

Output ONLY a JSON array. Each element is an object:
  {{"path": "relative/path", "purpose": "one short line"}}

Completeness rules — a missing file breaks the build, so be thorough:
- Relative paths only. No leading slash. Never use "..".
- If any page renders an HTML template, PLAN that template file.
- If any code imports a module, PLAN that module file.
- If pages link a stylesheet or script (e.g. static/css/style.css,
  static/js/main.js), PLAN those asset files.
- Include every supporting file: config, database models, schema, a README,
  a .env.example, the dependency file (requirements.txt or package.json), and
  any Docker/nginx/deploy files the spec asks for.
- Pick ONE architecture and list files that fit it. Do not mix a single-file
  app with a blueprint/module layout.
- Order: shared modules first (config, models, schema, shared CSS), then the
  code and pages that use them, then build/deploy files, README last.
- Do not exceed {max_files} files.

PROJECT SPECIFICATION:
{spec}
"""

FILE_SYSTEM = (
    "You are a code generation engine. You output the raw contents of exactly "
    "one file that fits into an existing project. No explanations. No markdown "
    "code fences. Just the file body."
)

FILE_USER = """Generate ONE file that must fit exactly into an existing project.

PROJECT (summary):
{spec}

ARCHITECTURE (follow this exactly — same layout, same import style):
{architecture}

COMPLETE FILE LIST (these files exist or will exist — reference them, and only
them; never import or link a file that is not in this list):
{manifest}

RELATED FILES ALREADY WRITTEN (match their names, imports, routes, table names,
CSS classes and template variables precisely):
{context}

Consistency rules:
- Only import modules, render templates, and link assets that appear in the
  FILE LIST above. If you need something that is not listed, do without it.
- Match the exact function names, class names, route names, database columns and
  template variables used in the RELATED FILES.
- Write the whole file with working code — no TODO stubs, no placeholder
  comments standing in for real logic, no "lorem ipsum".
- In dependency files (requirements.txt, package.json), list package NAMES
  ONLY — do NOT pin versions (no "==1.2.3"). Guessed version numbers are often
  wrong or mutually incompatible and break installation. Let the installer pick
  compatible versions.

Now output the COMPLETE contents of this file and nothing else.
PATH: {path}
PURPOSE: {purpose}

Raw file content only. No commentary. No ``` fences.
"""

# --- architecture step: pick one shape before writing any files ----------
ARCH_SYSTEM = (
    "You are a senior engineer. You describe a project's architecture in a few "
    "short, decisive lines so that many files can be written consistently. No "
    "code, no markdown — just the decisions."
)

ARCH_USER = """In 4-8 short lines, lock down the architecture for this project so
every file is written consistently. Decide and state plainly:
- Language/framework and the single layout style (e.g. "Flask, single app.py"
  OR "Flask app factory with blueprints in routes/"). Pick ONE.
- Where routes, models, templates and static assets live.
- The import convention (relative or absolute) and the entry point.
- Database and how config/secrets are loaded.
Be specific and brief. This is a contract every file must follow.

FILE LIST:
{manifest}

PROJECT SPECIFICATION:
{spec}
"""

SINGLE_SYSTEM = (
    "You are a code generation engine. You output a whole project as a series "
    "of files, each wrapped exactly like this:\n"
    "=== BAI-FILE: relative/path ===\n<file contents>\n=== BAI-END ===\n"
    "No prose outside the blocks. No markdown fences inside them."
)

SINGLE_USER = """Build the complete project below. Emit every file using the
=== BAI-FILE: path === / === BAI-END === format and nothing else.

PROJECT SPECIFICATION:
{spec}
"""

# Files worth feeding back in as "shared interfaces" when generating later files.
_INTERFACE_HINTS = (
    "config", "settings", "models", "schema", "database", "db",
    "requirements", "package.json", "__init__", "extensions", "types",
)
_CONTEXT_BUDGET = 6000  # chars of prior files to include as context


class BuildJob:
    def __init__(self, client: ModelClient, workspace: Workspace,
                 spec: str, mode: str = "manifest"):
        self.client = client
        self.ws = workspace
        self.spec = spec.strip()
        self.mode = mode  # "manifest" (default) or "single"
        self.manifest = []
        self.architecture = ""
        self.written = []  # [{path, content}]

    # -- event helpers ------------------------------------------------------
    @staticmethod
    def _ev(kind, **kw):
        e = {"event": kind}
        e.update(kw)
        return e

    # -- the run loop -------------------------------------------------------
    def run(self):
        started = time.time()
        try:
            yield self._ev("start", project=self.ws.project, mode=self.mode)

            if self.mode == "single":
                yield from self._run_single()
            else:
                yield from self._run_manifest()

            if self.ws.file_count == 0:
                yield self._ev("error", message="The model produced no usable files. "
                               "Try the manifest mode, a stronger coder model, or a clearer spec.")
                return

            zip_path = self.ws.zip()
            yield self._ev(
                "done",
                files=self.ws.file_count,
                bytes=self.ws.total_bytes,
                seconds=round(time.time() - started, 1),
                zip=str(zip_path.name),
            )
        except ModelError as e:
            # Already a plain, actionable message.
            yield self._ev("error", message=str(e))
        except requests.RequestException as e:
            yield self._ev("error", message=(
                "Couldn't reach Ollama. Check it's running (systemctl status "
                "ollama) and that OLLAMA_HOST is correct. Details: " + str(e)))
        except Exception as e:  # noqa: BLE001 - surface anything to the UI
            yield self._ev("error", message=str(e))

    # -- phase 1: plan ------------------------------------------------------
    def _plan(self):
        text = self.client.chat(
            PLAN_SYSTEM,
            PLAN_USER.format(spec=self.spec, max_files=MAX_FILES),
        )
        manifest = protocol.extract_json_manifest(text)
        return manifest[:MAX_FILES]

    def _run_manifest(self):
        yield self._ev("phase", name="planning", message="Planning the file list…")
        self.manifest = self._plan()
        if not self.manifest:
            yield self._ev("error", message="The model did not return a valid file plan. "
                           "Try again, or switch to single-shot mode.")
            return
        yield self._ev("planned", count=len(self.manifest),
                       files=[m["path"] for m in self.manifest])

        # Phase 1.5: lock the architecture once, so every file agrees on layout,
        # import style and entry point. This is the biggest fix for the "files
        # don't fit together" problem.
        yield self._ev("phase", name="architecture", message="Deciding the architecture…")
        self.architecture = self._decide_architecture()

        for i, item in enumerate(self.manifest, start=1):
            path, purpose = item["path"], item["purpose"]
            yield self._ev("file_start", index=i, total=len(self.manifest), path=path)
            yield from self._generate_one(path, purpose, i, len(self.manifest))

        # Phase 3: catch files that finished code references but the plan missed
        # (e.g. a template rendered by a route, a stylesheet linked in HTML).
        missing = self._find_missing_files()
        if missing:
            yield self._ev("phase", name="repair",
                           message=f"Adding {len(missing)} referenced file(s) the plan missed…")
            base = len(self.manifest)
            for j, path in enumerate(missing, start=1):
                purpose = "referenced by other files but missing from the plan"
                yield self._ev("file_start", index=base + j, total=base + len(missing), path=path)
                yield from self._generate_one(path, purpose, base + j, base + len(missing),
                                              repaired=True)

    def _generate_one(self, path, purpose, index, total, repaired=False):
        content = self.client.chat(
            FILE_SYSTEM,
            FILE_USER.format(
                spec=self._short_spec(),
                architecture=self.architecture or "(follow common conventions)",
                manifest=self._manifest_text(),
                context=self._context_blob(path),
                path=path,
                purpose=purpose,
            ),
        )
        content = protocol.clean_file_content(content)
        content = protocol.sanitize_for_path(path, content)
        try:
            info = self.ws.write(path, content)
        except WorkspaceError as e:
            yield self._ev("file_skip", path=path, reason=str(e))
            return
        self.written.append({"path": path, "content": content})
        note = self._validate(path, content)
        if repaired:
            note = (note + "; " if note else "") + "added in repair pass"
        yield self._ev("file_done", index=index, total=total,
                       path=path, bytes=info["bytes"], note=note)

    # -- alternative: single-shot ------------------------------------------
    def _run_single(self):
        yield self._ev("phase", name="generating",
                       message="Generating the whole project in one pass…")
        text = self.client.chat(SINGLE_SYSTEM, SINGLE_USER.format(spec=self.spec))
        blocks = protocol.parse_file_blocks(text)
        if not blocks:
            yield self._ev("error", message="No file blocks found in the response. "
                           "Single-shot needs a capable model; try manifest mode.")
            return
        for i, b in enumerate(blocks, start=1):
            b["content"] = protocol.sanitize_for_path(b["path"], b["content"])
            try:
                info = self.ws.write(b["path"], b["content"])
            except WorkspaceError as e:
                yield self._ev("file_skip", path=b["path"], reason=str(e))
                continue
            note = self._validate(b["path"], b["content"])
            yield self._ev("file_done", index=i, total=len(blocks),
                           path=b["path"], bytes=info["bytes"], note=note)

    # -- context management (the scalability trick) -------------------------
    def _short_spec(self):
        # Keep the spec that rides along on every file request compact.
        return self.spec[:2500]

    def _manifest_text(self):
        return "\n".join(f"- {m['path']}: {m['purpose']}" for m in self.manifest)

    def _decide_architecture(self):
        try:
            text = self.client.chat(
                ARCH_SYSTEM,
                ARCH_USER.format(spec=self._short_spec(), manifest=self._manifest_text()),
            )
            return (text or "").strip()[:1200]
        except Exception:  # noqa: BLE001 - architecture is best-effort
            return ""

    def _context_blob(self, current_path=""):
        """A budget-limited digest of already-written files to keep later files
        consistent. Prefers shared interfaces (config, models, schema) plus any
        file in the same directory as the current one, so, e.g., templates see
        the routes that render them."""
        cur_dir = current_path.rsplit("/", 1)[0] if "/" in current_path else ""
        scored = []
        for f in self.written:
            low = f["path"].lower()
            score = 0
            if any(h in low for h in _INTERFACE_HINTS):
                score += 3
            if cur_dir and f["path"].startswith(cur_dir + "/"):
                score += 2
            # base template / shared stylesheet are worth showing to pages
            if low.endswith(("base.html", "layout.html", "style.css", "styles.css", "main.css")):
                score += 2
            if score:
                scored.append((score, f))
        scored.sort(key=lambda x: x[0], reverse=True)

        picked, used = [], 0
        for _, f in scored:
            snippet = f["content"][:1600]
            if used + len(snippet) > _CONTEXT_BUDGET:
                continue
            picked.append(f"# ---- {f['path']} ----\n{snippet}")
            used += len(snippet)
        return "\n\n".join(picked) if picked else "(none yet)"

    # -- missing-file detection (repair pass) -------------------------------
    def _find_missing_files(self):
        """Scan written files for references to project files that were never
        created — rendered templates, linked static assets, imported modules —
        and return a de-duplicated list to generate in a repair pass."""
        have = {f["path"] for f in self.written}
        have_norm = {p.lstrip("./") for p in have}
        wanted = set()

        for f in self.written:
            path, text = f["path"], f["content"]
            for ref in protocol.referenced_paths(path, text):
                ref_norm = ref.lstrip("./")
                if ref_norm in have_norm or ref_norm in wanted:
                    continue
                # only chase references that look like real project files and
                # sit within our caps
                if "/" in ref_norm or "." in ref_norm:
                    wanted.add(ref_norm)

        # keep the repair pass bounded
        remaining = max(0, MAX_FILES - self.ws.file_count)
        return sorted(wanted)[:min(remaining, 15)]

    # -- optional validation ------------------------------------------------
    def _validate(self, path: str, content: str):
        """Cheap, safe static checks. Never executes project code — only runs
        the language's own syntax checker on the file in a temp copy."""
        try:
            if path.endswith(".py"):
                return self._check([sys.executable, "-m", "py_compile"], content, ".py")
            if path.endswith(".json"):
                json.loads(content)
                return None
        except Exception:  # noqa: BLE001
            pass
        return None

    @staticmethod
    def _check(cmd, content, suffix):
        with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=True) as tf:
            tf.write(content)
            tf.flush()
            proc = subprocess.run(
                cmd + [tf.name], capture_output=True, text=True, timeout=20,
            )
            if proc.returncode != 0:
                msg = (proc.stderr or proc.stdout).strip().splitlines()
                return "syntax warning: " + (msg[-1] if msg else "check failed")
        return None
