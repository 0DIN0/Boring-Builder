"""Where generated files land, and how they become a downloadable zip.

Everything here is defensive on purpose: the paths come from a language model,
so we treat them as untrusted. No absolute paths, no '..' escapes, hard caps on
file count and size, and every build is isolated in its own directory.
"""
import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path

# Hard limits. Tune in one place; the orchestrator reads these too.
MAX_FILES = 80
MAX_FILE_BYTES = 512 * 1024          # 512 KB per file
MAX_TOTAL_BYTES = 20 * 1024 * 1024   # 20 MB per build
BUILD_TTL_SECONDS = 6 * 60 * 60      # clean builds older than 6 hours

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")


def slugify(name: str, fallback: str = "project") -> str:
    slug = _SAFE_NAME.sub("-", (name or "").strip().lower()).strip("-")
    return slug or fallback


class WorkspaceError(Exception):
    pass


class Workspace:
    """An isolated directory for one build, plus zip packaging."""

    def __init__(self, root: str, project_name: str):
        self.id = uuid.uuid4().hex[:12]
        self.project = slugify(project_name)
        self.root = Path(root)
        # files live under <root>/<build id>/<project>/...
        self.build_dir = self.root / self.id
        self.project_dir = self.build_dir / self.project
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.total_bytes = 0
        self.file_count = 0

    # -- safe path handling -------------------------------------------------
    def _resolve(self, rel_path: str) -> Path:
        rel = (rel_path or "").strip().replace("\\", "/").lstrip("/")
        if not rel:
            raise WorkspaceError("empty path")
        # normalise and confirm the result stays inside the project dir
        target = (self.project_dir / rel).resolve()
        base = self.project_dir.resolve()
        if base != target and base not in target.parents:
            raise WorkspaceError(f"unsafe path rejected: {rel_path}")
        return target

    # -- writing ------------------------------------------------------------
    def write(self, rel_path: str, content: str) -> dict:
        if self.file_count >= MAX_FILES:
            raise WorkspaceError(f"file limit reached ({MAX_FILES})")

        data = (content or "").encode("utf-8", errors="replace")
        if len(data) > MAX_FILE_BYTES:
            data = data[:MAX_FILE_BYTES]
        if self.total_bytes + len(data) > MAX_TOTAL_BYTES:
            raise WorkspaceError("total size limit reached")

        target = self._resolve(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

        self.total_bytes += len(data)
        self.file_count += 1
        return {"path": rel_path, "bytes": len(data)}

    def list_files(self):
        out = []
        for p in sorted(self.project_dir.rglob("*")):
            if p.is_file():
                out.append(str(p.relative_to(self.project_dir)))
        return out

    def read(self, rel_path: str) -> str:
        try:
            return self._resolve(rel_path).read_text("utf-8", errors="replace")
        except (OSError, WorkspaceError):
            return ""

    # -- packaging ----------------------------------------------------------
    def zip(self) -> Path:
        zip_path = self.build_dir / f"{self.project}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(self.project_dir.rglob("*")):
                if p.is_file():
                    arc = Path(self.project) / p.relative_to(self.project_dir)
                    zf.write(p, arcname=str(arc))
        return zip_path

    def cleanup(self):
        shutil.rmtree(self.build_dir, ignore_errors=True)

    def export_to(self, output_dir: str, keep_unzipped: bool = True) -> dict:
        """Copy the finished build to a directory the operator controls.

        Returns a dict describing what was written. Never raises on a bad
        destination — it reports the problem so the build result still stands.
        """
        result = {"zip": None, "folder": None, "error": None}
        try:
            dest = Path(output_dir).expanduser()
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            result["error"] = f"output directory not usable: {e}"
            return result

        # Unique, timestamped names so repeated builds never clobber each other.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = f"{self.project}-{stamp}"

        zip_src = self.build_dir / f"{self.project}.zip"
        try:
            if zip_src.exists():
                zip_dest = dest / f"{base}.zip"
                shutil.copy2(zip_src, zip_dest)
                result["zip"] = str(zip_dest)
            if keep_unzipped:
                folder_dest = dest / base
                shutil.copytree(self.project_dir, folder_dest, dirs_exist_ok=True)
                result["folder"] = str(folder_dest)
        except OSError as e:
            result["error"] = f"could not write to output directory: {e}"
        return result


def sweep_old_builds(root: str):
    """Delete build directories older than the TTL. Cheap; call opportunistically."""
    base = Path(root)
    if not base.exists():
        return
    cutoff = time.time() - BUILD_TTL_SECONDS
    for child in base.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass
