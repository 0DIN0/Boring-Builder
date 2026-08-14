"""Save the Modelfiles created through the UI to a folder on disk, so a model
built in the browser is never work you have to redo in the terminal — the
Modelfile is right there to inspect, version, edit, or rebuild with
`ollama create <name> -f <file>`.

Two libraries live side by side:
  - builder Modelfiles  -> <root>/modelfiles/builder/
  - chat Modelfiles     -> <root>/modelfiles/chat/

The root defaults to the app directory (so the repo's existing `modelfiles/`
folder is used) but can be redirected with MODELFILES_DIR in .env.
"""
import os
import re
import time
from pathlib import Path

_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


class ModelfileStore:
    def __init__(self, root: str, kind: str):
        # kind is "builder" or "chat"
        self.kind = "chat" if kind == "chat" else "builder"
        self.dir = Path(root) / "modelfiles" / self.kind
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        safe = _SAFE.sub("-", (name or "").strip()).strip("-") or "model"
        return self.dir / f"{safe}.Modelfile"

    def save(self, name: str, modelfile: str) -> str:
        """Write (or overwrite) the Modelfile for `name`. Returns the path."""
        path = self._path(name)
        header = (f"# Saved by Boring AI on {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                  f"# Rebuild with:  ollama create {name} -f {path.name}\n\n")
        # don't double-write our header if the text already has one
        body = modelfile if modelfile.lstrip().startswith("#") else header + modelfile
        path.write_text(body)
        return str(path)

    def read(self, name: str) -> str:
        try:
            return self._path(name).read_text()
        except OSError:
            return ""

    def delete(self, name: str) -> bool:
        try:
            self._path(name).unlink()
            return True
        except OSError:
            return False

    def list(self):
        out = []
        for p in sorted(self.dir.glob("*.Modelfile")):
            out.append({"name": p.stem, "path": str(p),
                        "modified": p.stat().st_mtime})
        return out


def modelfiles_root(app_config):
    configured = os.environ.get("MODELFILES_DIR", "").strip()
    if configured:
        return configured
    # default: the application directory (where the repo's modelfiles/ lives)
    return app_config.get("APP_ROOT") or os.getcwd()
