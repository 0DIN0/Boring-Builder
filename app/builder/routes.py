"""The Builder — an admin area for generating projects and managing the models
that generate them. Cleanly separated from the chat app: its own blueprint, its
own settings, its own navigation.

Endpoints
  GET  /builder/                    the builder workspace page
  GET  /builder/settings            builder settings page
  POST /builder/run                 stream a build (plan -> files -> zip -> export)
  GET  /builder/<id>/download       download a finished build's zip
  GET  /builder/history             recent builds (JSON)
  GET  /builder/models              installed builder models (JSON)
  GET  /builder/models/bases        installed models usable as a FROM base (JSON)
  POST /builder/models/create       create a builder-* model from a Modelfile (stream)
  POST /builder/models/delete       delete a builder-* model
  GET  /builder/settings/data       current builder settings (JSON)
  POST /builder/settings/data       save builder settings
"""
import json
import os
import time
from functools import wraps
from pathlib import Path

from flask import (
    Blueprint, request, jsonify, Response, render_template,
    stream_with_context, send_file, abort, current_app,
)
from flask_login import login_required, current_user

from .. import settings as app_settings
from .orchestrator import ModelClient, BuildJob
from .workspace import Workspace, sweep_old_builds, MAX_FILES
from .models import BuilderModels, canonical_name, is_valid, STARTER_MODELFILE
from .history import BuildHistory
from .modelfile_store import ModelfileStore, modelfiles_root
from . import hardware

builder_bp = Blueprint("builder", __name__)


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not getattr(current_user, "is_authenticated", False) or not getattr(current_user, "is_admin", False):
            abort(403)
        return f(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------- #
# Config helpers
# --------------------------------------------------------------------------- #
def _ollama_host():
    return os.environ.get("OLLAMA_HOST",
                          current_app.config.get("OLLAMA_HOST", "http://localhost:11434"))


def _builds_root():
    data_dir = current_app.config.get("DATA_DIR", os.getcwd())
    root = Path(data_dir) / "builds"
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def _output_dir():
    """Where finished projects are exported. Operator-chosen via .env or
    Builder settings; falls back to <DATA_DIR>/output."""
    configured = (app_settings.get("builder_output_dir")
                  or os.environ.get("BUILDER_OUTPUT_DIR", "")).strip()
    if configured:
        return configured
    return str(Path(current_app.config.get("DATA_DIR", os.getcwd())) / "output")


def _history():
    return BuildHistory(_builds_root())


def _builder_models():
    return BuilderModels(_ollama_host())


def _default_model():
    return (app_settings.get("builder_model")
            or os.environ.get("BUILDER_MODEL", "qwen2.5-coder:3b"))


def _model_client(model=None):
    num_ctx = app_settings.get("builder_num_ctx") or int(os.environ.get("BUILDER_NUM_CTX", "8192"))
    temp = app_settings.get("builder_temperature") or os.environ.get("BUILDER_TEMPERATURE", "0.2")
    options = {
        "temperature": float(temp),
        "top_p": 0.9,
        "num_ctx": int(num_ctx),
        "repeat_penalty": 1.05,
    }
    timeout = int(os.environ.get("BUILDER_TIMEOUT", "900"))
    return ModelClient(_ollama_host(), model or _default_model(), timeout=timeout, options=options)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@builder_bp.route("/")
@login_required
def index():
    return render_template("builder.html", user=current_user,
                           default_model=_default_model(),
                           default_mode=app_settings.get("builder_default_mode"),
                           output_dir=_output_dir(),
                           starter_modelfile=STARTER_MODELFILE)


@builder_bp.route("/settings")
@login_required
@admin_required
def settings_page():
    return render_template("builder_settings.html", user=current_user,
                           starter_modelfile=STARTER_MODELFILE)


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
@builder_bp.route("/run", methods=["POST"])
@login_required
def run():
    data = request.get_json(silent=True) or {}
    spec = (data.get("spec") or "").strip()
    project = (data.get("project") or "project").strip()
    mode = data.get("mode") if data.get("mode") in ("manifest", "single") \
        else app_settings.get("builder_default_mode")
    model = (data.get("model") or "").strip() or None

    if len(spec) < 20:
        return jsonify({"error": "Describe what to build in a bit more detail."}), 400

    root = _builds_root()
    sweep_old_builds(root)
    ws = Workspace(root, project)
    client = _model_client(model)
    job = BuildJob(client, ws, spec, mode=mode)

    auto_export = app_settings.get("builder_auto_export")
    keep_unzipped = app_settings.get("builder_keep_unzipped")
    output_dir = _output_dir()
    used_model = model or _default_model()

    def stream():
        yield _sse({"event": "created", "build_id": ws.id})
        result_seen = None
        for evt in job.run():
            if evt.get("event") == "done":
                result_seen = evt
                # Optionally copy the finished project to the operator's folder.
                if auto_export:
                    exp = ws.export_to(output_dir, keep_unzipped=keep_unzipped)
                    evt["exported"] = exp
                # Record it in history for later download/re-run.
                try:
                    _history().add({
                        "id": ws.id,
                        "project": ws.project,
                        "spec": spec,
                        "model": used_model,
                        "mode": mode,
                        "files": evt.get("files", 0),
                        "bytes": evt.get("bytes", 0),
                        "seconds": evt.get("seconds", 0),
                        "zip": evt.get("zip", ""),
                        "exported": evt.get("exported", {}),
                        "created_at": time.time(),
                    })
                except Exception:
                    pass
            yield _sse(evt)

    return Response(stream_with_context(stream()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@builder_bp.route("/<build_id>/download")
@login_required
def download(build_id):
    if not build_id.isalnum():
        abort(400)
    build_dir = Path(_builds_root()) / build_id
    if not build_dir.is_dir():
        abort(404)
    zips = list(build_dir.glob("*.zip"))
    if not zips:
        abort(404)
    return send_file(str(zips[0]), as_attachment=True, download_name=zips[0].name)


@builder_bp.route("/history")
@login_required
def history():
    return jsonify({"builds": _history().list(), "output_dir": _output_dir()})


@builder_bp.route("/history/delete", methods=["POST"])
@login_required
@admin_required
def history_delete():
    bid = (request.get_json(silent=True) or {}).get("id", "")
    if bid.isalnum():
        _history().remove(bid)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Builder-model management
# --------------------------------------------------------------------------- #
@builder_bp.route("/models")
@login_required
def models_list():
    bm = _builder_models()
    return jsonify({"models": bm.list(), "default": _default_model()})


@builder_bp.route("/models/bases")
@login_required
def models_bases():
    return jsonify({"bases": _builder_models().all_base_models()})


@builder_bp.route("/models/create", methods=["POST"])
@login_required
@admin_required
def models_create():
    data = request.get_json(silent=True) or {}
    name = canonical_name(data.get("name", ""))
    modelfile = (data.get("modelfile") or "").strip()

    if not is_valid(name):
        return jsonify({"error": "Name must become a valid 'builder-<name>' "
                                 "using letters, numbers, dot, dash or underscore."}), 400
    if "FROM " not in modelfile:
        return jsonify({"error": "The Modelfile needs a FROM line naming a base model."}), 400

    bm = _builder_models()
    store = ModelfileStore(modelfiles_root(current_app.config), "builder")

    def stream():
        yield _sse({"event": "start", "name": name})
        errored = False
        try:
            for status in bm.create_stream(name, modelfile):
                if status.get("error"):
                    errored = True
                    yield _sse({"event": "error", "message": status["error"]})
                    return
                msg = status.get("status", "")
                if msg:
                    yield _sse({"event": "status", "message": msg})
        except Exception as e:  # noqa: BLE001
            errored = True
            yield _sse({"event": "error", "message": str(e)})
            return
        if not errored:
            # Persist the Modelfile so the UI and terminal share one source.
            saved_path = ""
            try:
                saved_path = store.save(name, modelfile)
            except OSError:
                pass
            yield _sse({"event": "done", "name": name, "saved": saved_path})

    return Response(stream_with_context(stream()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@builder_bp.route("/models/delete", methods=["POST"])
@login_required
@admin_required
def models_delete():
    name = (request.get_json(silent=True) or {}).get("name", "")
    if not name.startswith("builder-"):
        return jsonify({"error": "Only builder models can be removed here."}), 400
    ok = _builder_models().delete(name)
    try:
        ModelfileStore(modelfiles_root(current_app.config), "builder").delete(name)
    except OSError:
        pass
    return jsonify({"ok": ok})


# --------------------------------------------------------------------------- #
# Builder settings
# --------------------------------------------------------------------------- #
_BUILDER_KEYS = (
    "builder_model", "builder_output_dir", "builder_num_ctx", "builder_temperature",
    "builder_default_mode", "builder_max_files", "builder_keep_unzipped",
    "builder_auto_export",
)


@builder_bp.route("/settings/data")
@login_required
@admin_required
def settings_data():
    data = {k: app_settings.get(k) for k in _BUILDER_KEYS}
    data["resolved_output_dir"] = _output_dir()
    data["resolved_model"] = _default_model()
    return jsonify(data)


@builder_bp.route("/settings/data", methods=["POST"])
@login_required
@admin_required
def settings_save():
    data = request.get_json(silent=True) or {}
    patch = {key: data[key] for key in _BUILDER_KEYS if key in data}
    if patch:
        app_settings.set_many(patch)
    return jsonify({"ok": True})


@builder_bp.route("/hardware")
@login_required
def hardware_info():
    """Detect this machine and recommend models + settings for it."""
    try:
        snap = hardware.snapshot()
    except Exception as e:  # noqa: BLE001 - detection must never 500 the page
        snap = {"error": str(e), "has_gpu": False, "tips": [
            "Hardware detection failed on this system; recommendations unavailable."]}
    # what's actually installed, so we can mark recommendations as present/absent
    try:
        snap["installed"] = _builder_models().all_base_models()
    except Exception:  # noqa: BLE001
        snap["installed"] = []
    return jsonify(snap)


def _sse(obj) -> str:
    return "data: " + json.dumps(obj) + "\n\n"
