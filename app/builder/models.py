"""Create, list and delete *builder* models from Modelfiles.

A builder model is just an Ollama model that follows the plan/generate output
contract (see orchestrator.py). To keep them tidy and recognisable we enforce a
naming convention: every model this section creates is prefixed ``builder-``.
That prefix is also how we know which of your installed models to show in the
builder's model picker, so they never get mixed up with your chat models.
"""
import re

import requests

BUILDER_PREFIX = "builder-"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,40}$")


def canonical_name(name: str) -> str:
    """Normalise a user-supplied name to `builder-<slug>`.

    'Course Writer' -> 'builder-course-writer'
    'builder-fast'  -> 'builder-fast'  (already prefixed)
    """
    raw = (name or "").strip().lower()
    if raw.startswith(BUILDER_PREFIX):
        raw = raw[len(BUILDER_PREFIX):]
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-")
    return BUILDER_PREFIX + slug if slug else ""


def is_valid(name: str) -> bool:
    return bool(_NAME_RE.match(name or "")) and name.startswith(BUILDER_PREFIX)


class BuilderModels:
    """Thin wrapper over the Ollama REST API for builder-model lifecycle."""

    def __init__(self, host: str, timeout: int = 30):
        self.host = host.rstrip("/")
        self.timeout = timeout

    # -- read ---------------------------------------------------------------
    def list(self):
        """Return installed models tagged as builders, newest first."""
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=self.timeout)
            r.raise_for_status()
            models = r.json().get("models", [])
        except requests.RequestException:
            return []
        out = []
        for m in models:
            name = m.get("name", "")
            if not name.startswith(BUILDER_PREFIX):
                continue
            details = m.get("details", {}) or {}
            out.append({
                "name": name,
                "size": m.get("size", 0),
                "size_human": _human(m.get("size", 0)),
                "family": details.get("family", ""),
                "parameter_size": details.get("parameter_size", ""),
                "quantization": details.get("quantization_level", ""),
                "modified": m.get("modified_at", ""),
            })
        out.sort(key=lambda x: x.get("modified", ""), reverse=True)
        return out

    def all_base_models(self):
        """Every installed model (for choosing a FROM base), builders excluded."""
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=self.timeout)
            r.raise_for_status()
            models = r.json().get("models", [])
        except requests.RequestException:
            return []
        return sorted(
            m.get("name", "") for m in models
            if m.get("name") and not m.get("name", "").startswith(BUILDER_PREFIX)
        )

    def show(self, name: str):
        try:
            r = requests.post(f"{self.host}/api/show", json={"name": name},
                              timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            return {}

    # -- write (streaming) --------------------------------------------------
    def create_stream(self, name: str, modelfile: str):
        """Create a model from Modelfile text. Yields status dicts from Ollama.

        Newer Ollama (0.1.30+) replaced the single ``modelfile`` string on
        /api/create with structured fields (``from``, ``system``,
        ``parameters``, ``template``, ``license``). Older Ollama only accepts
        the string. We parse the Modelfile and send the modern payload, then
        fall back to the legacy payload if the server is old.
        """
        import json as _json

        parsed = parse_modelfile(modelfile)
        if not parsed.get("from"):
            yield {"error": "The Modelfile needs a FROM line naming a base model."}
            return

        modern = {"model": name, "stream": True, "from": parsed["from"]}
        if parsed.get("system"):
            modern["system"] = parsed["system"]
        if parsed.get("template"):
            modern["template"] = parsed["template"]
        if parsed.get("parameters"):
            modern["parameters"] = parsed["parameters"]
        if parsed.get("license"):
            modern["license"] = parsed["license"]

        legacy = {"name": name, "modelfile": modelfile, "stream": True}

        for payload in (modern, legacy):
            ok = True
            try:
                with requests.post(f"{self.host}/api/create", json=payload,
                                   stream=True, timeout=None) as resp:
                    if resp.status_code >= 400:
                        # Read the body for a useful message, then try the next shape.
                        detail = _read_error(resp)
                        ok = False
                        last_error = detail or f"HTTP {resp.status_code}"
                        # A 400 usually means "wrong payload shape" -> try fallback.
                        if resp.status_code == 400 and payload is modern:
                            continue
                        yield {"error": last_error}
                        return
                    for line in resp.iter_lines():
                        if not line:
                            continue
                        try:
                            yield _json.loads(line)
                        except ValueError:
                            continue
            except requests.RequestException as e:
                ok = False
                last_error = str(e)
                if payload is modern:
                    continue
                yield {"error": last_error}
                return
            if ok:
                return

    def delete(self, name: str) -> bool:
        try:
            r = requests.delete(f"{self.host}/api/delete", json={"name": name},
                                timeout=self.timeout)
            return r.status_code in (200, 404)
        except requests.RequestException:
            return False



def parse_modelfile(text: str) -> dict:
    """Turn Modelfile text into the structured fields the modern create API wants.

    Handles: FROM, SYSTEM (plain or triple-quoted), TEMPLATE (triple-quoted),
    LICENSE, and PARAMETER lines (collected into a dict; repeated keys become a
    list, which is how Ollama expects e.g. multiple `stop` values).
    """
    out = {"from": "", "system": "", "template": "", "license": "", "parameters": {}}
    lines = (text or "").splitlines()
    i = 0

    def read_block(first_after_kw: str):
        nonlocal i
        val = first_after_kw.strip()
        if val.startswith('"""'):
            val = val[3:]
            if val.rstrip().endswith('"""'):
                return val.rstrip()[:-3].strip()
            collected = [val]
            i += 1
            while i < len(lines):
                if lines[i].rstrip().endswith('"""'):
                    collected.append(lines[i].rstrip()[:-3])
                    break
                collected.append(lines[i])
                i += 1
            return "\n".join(collected).strip("\n")
        return val.strip().strip('"')

    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        parts = stripped.split(None, 1)
        kw = parts[0].upper()
        rest = parts[1] if len(parts) > 1 else ""
        if kw == "FROM":
            out["from"] = rest.strip()
        elif kw == "SYSTEM":
            out["system"] = read_block(rest)
        elif kw == "TEMPLATE":
            out["template"] = read_block(rest)
        elif kw == "LICENSE":
            out["license"] = read_block(rest)
        elif kw == "PARAMETER":
            pparts = rest.split(None, 1)
            if len(pparts) == 2:
                _add_param(out["parameters"], pparts[0], pparts[1].strip().strip('"'))
        i += 1
    return out


def _add_param(params: dict, key: str, value: str):
    coerced = _coerce(value)
    if key in params:
        if not isinstance(params[key], list):
            params[key] = [params[key]]
        params[key].append(coerced)
    else:
        params[key] = coerced


def _coerce(value: str):
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return value


def _read_error(resp) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            return data["error"]
    except ValueError:
        pass
    try:
        return (resp.text or "").strip()[:300]
    except Exception:
        return ""


def _human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


# The starting Modelfile shown when creating a new builder model. It already
# contains the output contract the orchestrator relies on, so a new builder
# works out of the box — the operator just picks a FROM base and tweaks tone.
STARTER_MODELFILE = """FROM {base}

# Deterministic and tidy — code generation wants low randomness.
PARAMETER temperature 0.2
PARAMETER top_p 0.9
PARAMETER num_ctx 8192
PARAMETER repeat_penalty 1.05
PARAMETER stop "=== BAI-END ==="

SYSTEM \"\"\"
You are a precise code-generation engine that produces complete, runnable,
production-quality projects. You follow the caller's OUTPUT CONTRACT exactly.

PLAN MODE (asked to plan files): output ONLY a JSON array of
{{"path": "...", "purpose": "..."}}. Relative paths only, never "..". Be
COMPLETE: if a route renders a template, or code imports a module, or a page
links a stylesheet or script, that file MUST be in the plan. Pick ONE
architecture and list files that fit it — never mix a single-file app with a
blueprint/module layout. Order: config, models and schema first, then the code
and pages that use them, then Docker/nginx/deploy, README last. No prose, no
markdown fences.

FILE MODE (given one PATH): output ONLY that file's raw contents. No commentary,
no markdown fences. Only import modules, render templates and link assets that
exist in the file list you were shown. Match the exact names, routes, database
columns, CSS classes and template variables of the related files you were shown.
Write complete working code — no TODO stubs, no placeholder logic, no lorem ipsum.

Always: read secrets from environment/.env, parameterise SQL, validate input,
hash passwords, keep handlers stateless and consistent with the other files.
\"\"\"
"""
