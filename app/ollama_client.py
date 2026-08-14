"""Thin wrapper around the Ollama REST API.

Docs: https://github.com/ollama/ollama/blob/main/docs/api.md
"""
import json
import requests
from flask import current_app


class OllamaError(Exception):
    """Raised when the Ollama daemon is unreachable or returns an error."""


def _host() -> str:
    return current_app.config["OLLAMA_HOST"].rstrip("/")


def _timeout() -> int:
    return current_app.config["OLLAMA_TIMEOUT"]


def is_online() -> bool:
    try:
        r = requests.get(f"{_host()}/api/version", timeout=3)
        return r.ok
    except requests.RequestException:
        return False


def version() -> dict:
    try:
        r = requests.get(f"{_host()}/api/version", timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        raise OllamaError(str(exc)) from exc


def list_models() -> list:
    """Return locally installed models with human-friendly metadata."""
    try:
        r = requests.get(f"{_host()}/api/tags", timeout=10)
        r.raise_for_status()
    except requests.RequestException as exc:
        raise OllamaError(str(exc)) from exc

    models = []
    for m in r.json().get("models", []):
        details = m.get("details", {}) or {}
        models.append(
            {
                "name": m.get("name"),
                "size": m.get("size", 0),
                "size_human": _human_size(m.get("size", 0)),
                "modified_at": m.get("modified_at"),
                "family": details.get("family"),
                "parameter_size": details.get("parameter_size"),
                "quantization": details.get("quantization_level"),
            }
        )
    models.sort(key=lambda x: (x["name"] or "").lower())
    return models


def running_models() -> list:
    try:
        r = requests.get(f"{_host()}/api/ps", timeout=10)
        r.raise_for_status()
        return r.json().get("models", [])
    except requests.RequestException as exc:
        raise OllamaError(str(exc)) from exc


def show_model(name: str) -> dict:
    """Return details for a model, including its Modelfile, parameters and
    system prompt where available."""
    try:
        r = requests.post(f"{_host()}/api/show", json={"name": name}, timeout=15)
        r.raise_for_status()
    except requests.RequestException as exc:
        raise OllamaError(str(exc)) from exc

    data = r.json()
    details = data.get("details", {}) or {}
    return {
        "name": name,
        "modelfile": data.get("modelfile", ""),
        "parameters": data.get("parameters", ""),
        "template": data.get("template", ""),
        "system": data.get("system", ""),
        "family": details.get("family"),
        "parameter_size": details.get("parameter_size"),
        "quantization": details.get("quantization_level"),
    }


def copy_model(source: str, destination: str) -> None:
    try:
        r = requests.post(
            f"{_host()}/api/copy",
            json={"source": source, "destination": destination},
            timeout=30,
        )
        r.raise_for_status()
    except requests.RequestException as exc:
        raise OllamaError(str(exc)) from exc


def delete_model(name: str) -> None:
    try:
        r = requests.delete(f"{_host()}/api/delete", json={"name": name}, timeout=30)
        r.raise_for_status()
    except requests.RequestException as exc:
        raise OllamaError(str(exc)) from exc


def pull_model_stream(name: str):
    """Generator yielding NDJSON progress dicts while pulling a model."""
    try:
        with requests.post(
            f"{_host()}/api/pull",
            json={"name": name, "stream": True},
            stream=True,
            timeout=_timeout(),
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except requests.RequestException as exc:
        yield {"error": str(exc)}


def build_modelfile(base: str, system: str = "", parameters: dict = None,
                    template: str = "") -> str:
    """Assemble a Modelfile string from parts."""
    lines = [f"FROM {base}"]
    for key, value in (parameters or {}).items():
        if value is None or value == "":
            continue
        lines.append(f"PARAMETER {key} {value}")
    if template:
        lines.append('TEMPLATE """' + template + '"""')
    if system:
        lines.append('SYSTEM """' + system + '"""')
    return "\n".join(lines)


def create_model_stream(name: str, modelfile: str):
    """Generator yielding NDJSON progress dicts while creating/modifying a
    model from a Modelfile. Sends both the legacy ``modelfile`` field and the
    ``name`` field for broad version compatibility."""
    try:
        with requests.post(
            f"{_host()}/api/create",
            json={"name": name, "model": name, "modelfile": modelfile, "stream": True},
            stream=True,
            timeout=_timeout(),
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except requests.RequestException as exc:
        yield {"error": str(exc)}


def embed(texts, model: str):
    """Return embeddings for a list of texts using Ollama.

    Tries the newer batch /api/embed endpoint first, falling back to the
    older single-input /api/embeddings. Returns a list of float-lists, or
    raises OllamaError.
    """
    if isinstance(texts, str):
        texts = [texts]
    host = _host()
    try:
        r = requests.post(
            f"{host}/api/embed",
            json={"model": model, "input": texts},
            timeout=_timeout(),
        )
        if r.ok:
            data = r.json()
            if data.get("embeddings"):
                return data["embeddings"]
    except requests.RequestException:
        pass

    # Fallback: older endpoint, one request per text.
    out = []
    try:
        for t in texts:
            r = requests.post(
                f"{host}/api/embeddings",
                json={"model": model, "prompt": t},
                timeout=_timeout(),
            )
            r.raise_for_status()
            out.append(r.json().get("embedding", []))
    except requests.RequestException as exc:
        raise OllamaError(str(exc)) from exc
    return out


def chat_stream(model: str, messages: list, options=None, images=None):
    """Generator yielding assistant content chunks from /api/chat.

    ``options`` is passed straight through to Ollama; an empty/None options
    dict means the model runs with its own defaults.
    """
    msgs = [dict(m) for m in messages]
    if images:
        # Attach images to the most recent user message for vision models.
        for m in reversed(msgs):
            if m.get("role") == "user":
                m["images"] = images
                break
    payload = {
        "model": model,
        "messages": msgs,
        "stream": True,
    }
    if options:
        payload["options"] = options
    try:
        with requests.post(
            f"{_host()}/api/chat",
            json=payload,
            stream=True,
            timeout=_timeout(),
        ) as r:
            if r.status_code == 404:
                yield {"error": f"Model '{model}' not found. Pull it first.", "done": True}
                return
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "error" in data:
                    yield {"error": data["error"], "done": True}
                    return
                chunk = data.get("message", {}).get("content", "")
                yield {
                    "content": chunk,
                    "done": data.get("done", False),
                    "total_duration": data.get("total_duration"),
                    "eval_count": data.get("eval_count"),
                }
    except requests.RequestException as exc:
        yield {"error": f"Could not reach Ollama: {exc}", "done": True}


def _human_size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}" if unit != "B" else f"{num} B"
        num /= 1024.0
    return f"{num:.1f} PB"
