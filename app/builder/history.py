"""A tiny, file-backed record of finished builds.

Deliberately not in the database: builds are an operator convenience, the data
is small, and keeping it as a JSON file next to the builds means the whole
builds directory is self-contained and easy to relocate or wipe. Access is
serialised with a lock so concurrent builds don't corrupt the file.
"""
import json
import threading
from pathlib import Path

_LOCK = threading.Lock()
MAX_ENTRIES = 100


class BuildHistory:
    def __init__(self, builds_root: str):
        self.root = Path(builds_root)
        self.path = self.root / "history.json"

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, entries):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(entries[:MAX_ENTRIES], f, indent=2)
        tmp.replace(self.path)

    def add(self, entry: dict):
        with _LOCK:
            entries = self._load()
            entries.insert(0, entry)
            self._save(entries)

    def list(self):
        # Return a trimmed view; the spec can be long, so cap it for the list.
        out = []
        for e in self._load():
            e = dict(e)
            spec = e.get("spec", "")
            e["spec_preview"] = (spec[:140] + "…") if len(spec) > 140 else spec
            out.append(e)
        return out

    def get(self, build_id: str):
        for e in self._load():
            if e.get("id") == build_id:
                return e
        return None

    def remove(self, build_id: str):
        with _LOCK:
            entries = [e for e in self._load() if e.get("id") != build_id]
            self._save(entries)
