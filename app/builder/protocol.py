"""The contract between the model and the file writer.

A language model only ever produces text. To turn that text into real files we
make the model follow one of two simple, machine-readable formats and parse the
result here. Nothing about this is model-specific, so the same parser works
whether you run a small local coder model or a hosted API later.

Two formats are supported:

1. A JSON *manifest* — a plan of which files the project needs. Used in the
   recommended two-phase flow (plan first, then generate each file), which is
   far more reliable on modest hardware than asking for a whole app at once.

2. File *blocks* — used in single-shot mode, where the model emits every file
   in one response wrapped in sentinels that are very unlikely to collide with
   real code:

       === BAI-FILE: relative/path.py ===
       <raw file contents>
       === BAI-END ===
"""
import json
import re

FILE_BEGIN = "=== BAI-FILE:"
FILE_END = "=== BAI-END ==="

_BLOCK_RE = re.compile(
    r"===\s*BAI-FILE:\s*(?P<path>.+?)\s*===\r?\n"
    r"(?P<body>.*?)"
    r"\r?\n===\s*BAI-END\s*===",
    re.DOTALL,
)


def strip_code_fences(text: str) -> str:
    """Remove a single wrapping ```lang ... ``` fence if the model added one.

    Small models love to wrap file contents in markdown fences even when told
    not to. We only strip a fence that wraps the *entire* body, never fences
    that are legitimately part of the file (e.g. inside a README).
    """
    t = text.strip()
    if not t.startswith("```"):
        return text
    # drop the opening fence line (``` or ```python) and the closing fence
    first_newline = t.find("\n")
    if first_newline == -1:
        return text
    opening = t[:first_newline].strip()
    if not re.fullmatch(r"```[\w.+-]*", opening):
        return text
    rest = t[first_newline + 1:]
    if rest.rstrip().endswith("```"):
        rest = rest.rstrip()[:-3]
    return rest.rstrip("\n") + "\n"


def extract_json_manifest(text: str):
    """Pull a list of {path, purpose} objects out of a model response.

    Tolerates the usual noise: prose before/after, ```json fences, and trailing
    commas. Returns [] if nothing parseable is found so the caller can retry.
    """
    if not text:
        return []

    candidates = []

    # 1) fenced ```json ... ``` block
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))

    # 2) the first bracketed array in the text
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start:end + 1])

    for raw in candidates:
        parsed = _try_load_array(raw)
        if parsed is not None:
            return _normalise_manifest(parsed)

    return []


def _try_load_array(raw: str):
    for attempt in (raw, _remove_trailing_commas(raw)):
        try:
            data = json.loads(attempt)
            if isinstance(data, list):
                return data
        except (ValueError, TypeError):
            continue
    return None


def _remove_trailing_commas(s: str) -> str:
    return re.sub(r",(\s*[\]}])", r"\1", s)


def _normalise_manifest(items):
    out = []
    seen = set()
    for item in items:
        if isinstance(item, str):
            path, purpose = item, ""
        elif isinstance(item, dict):
            path = item.get("path") or item.get("file") or item.get("name") or ""
            purpose = item.get("purpose") or item.get("description") or ""
        else:
            continue
        path = (path or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        out.append({"path": path, "purpose": (purpose or "").strip()})
    return out


def parse_file_blocks(text: str):
    """Parse single-shot output into [{path, content}, ...]."""
    blocks = []
    for m in _BLOCK_RE.finditer(text or ""):
        path = m.group("path").strip().strip("`").strip()
        body = strip_code_fences(m.group("body"))
        if path:
            blocks.append({"path": path, "content": body})
    return blocks


def clean_file_content(text: str) -> str:
    """Final tidy of a single generated file: drop wrapping fences and any
    stray 'PATH:' / commentary lines a chatty model prepended."""
    body = strip_code_fences(text or "")
    lines = body.split("\n")
    # trim leading lines that are obviously not file content
    while lines and re.match(r"^\s*(PATH|FILE|PURPOSE|Here('|`)?s|```)\b", lines[0], re.I):
        lines.pop(0)
    return "\n".join(lines).strip("\n") + "\n"


def sanitize_for_path(path: str, content: str) -> str:
    """Deterministic, per-file-type cleanups applied after generation.

    Currently: strip version pins from requirements.txt. Small models routinely
    invent version numbers that don't exist on PyPI (e.g. Flask-Migrate==3.1.1),
    which breaks `pip install`. Unpinned names let pip resolve compatible
    versions, so we remove the pins regardless of what the model emitted.
    """
    name = path.rsplit("/", 1)[-1].lower()
    if name == "requirements.txt":
        return _strip_requirement_pins(content)
    return content


def _strip_requirement_pins(content: str) -> str:
    out = []
    for line in (content or "").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            out.append(line)
            continue
        # keep VCS/URL/editable installs and environment markers untouched
        if raw.startswith("-") or "://" in raw or "@" in raw:
            out.append(line)
            continue
        # split off any inline comment, drop the version specifier, keep extras
        comment = ""
        if " #" in line:
            code, comment = line.split(" #", 1)
            comment = " #" + comment
        else:
            code = line
        # cut at the first version operator: == >= <= ~= != > <
        pkg = re.split(r"[<>=!~]=?|===", code.strip())[0].strip()
        out.append(pkg + comment if pkg else line)
    return "\n".join(out).rstrip("\n") + "\n"


# --------------------------------------------------------------------------- #
# Reference scanning — used by the orchestrator's repair pass to find files
# that generated code points at but the plan never created.
# --------------------------------------------------------------------------- #
import posixpath

# Jinja: render_template("x.html")  /  render_template('sub/x.html')
_RE_RENDER = re.compile(r"""render_template\(\s*['"]([^'"]+\.html)['"]""")
# Flask static: url_for('static', filename='css/style.css')
_RE_STATIC = re.compile(
    r"""url_for\(\s*['"]static['"]\s*,\s*filename\s*=\s*['"]([^'"]+)['"]"""
)
# Plain HTML asset links: href="..." / src="..." to local css/js/assets
_RE_HTML_ASSET = re.compile(
    r"""(?:href|src)\s*=\s*['"](?!https?:|//|#|mailto:|data:)([^'"]+\.(?:css|js|svg|png|jpg|jpeg|webp|ico))['"]""",
    re.I,
)
# Python imports of local modules: from x.y import z  /  import x
_RE_PYIMPORT = re.compile(r"""^\s*(?:from\s+([.\w]+)\s+import|import\s+([.\w]+))""", re.M)


def referenced_paths(path: str, text: str):
    """Best-effort list of project-relative files that `text` refers to.

    Returns paths normalised relative to the project root. Conservative on
    purpose: only patterns that clearly denote a project file are returned, so
    the repair pass never invents spurious files.
    """
    text = text or ""
    out = set()
    src_dir = posixpath.dirname(path)

    # Jinja rendered templates -> live under templates/
    for m in _RE_RENDER.finditer(text):
        out.add(_norm(posixpath.join("templates", m.group(1))))

    # Flask static files -> live under static/
    for m in _RE_STATIC.finditer(text):
        out.add(_norm(posixpath.join("static", m.group(1))))

    # Direct HTML asset links (resolve relative to the file, strip static/ dance)
    if path.endswith((".html", ".htm")):
        for m in _RE_HTML_ASSET.finditer(text):
            ref = m.group(1).lstrip("/")
            # a link like ../static/css/x.css or css/x.css -> keep the tail from static/
            if "static/" in ref:
                ref = "static/" + ref.split("static/", 1)[1]
            out.add(_norm(ref))

    # Local Python imports -> module files, but only if they look local
    if path.endswith(".py"):
        for m in _RE_PYIMPORT.finditer(text):
            mod = (m.group(1) or m.group(2) or "").strip()
            if not mod or mod.startswith(("flask", "werkzeug", "sqlalchemy", "os", "sys",
                                          "json", "datetime", "typing", "re", "pathlib",
                                          "wtforms", "dotenv", "requests", "jinja2")):
                continue
            rel = mod.lstrip(".").replace(".", "/")
            if not rel:
                continue
            if mod.startswith("."):  # relative import, resolve against this file's dir
                base = src_dir
                candidate = _norm(posixpath.join(base, rel) + ".py")
            else:
                candidate = _norm(rel + ".py")
            out.add(candidate)

    # never point at ourselves
    out.discard(_norm(path))
    return sorted(p for p in out if p and ".." not in p)


def _norm(p: str) -> str:
    return posixpath.normpath(p.replace("\\", "/")).lstrip("./")
