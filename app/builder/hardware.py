"""Detect the host's hardware and turn it into practical model recommendations.

Everything here is best-effort and dependency-free: it reads /proc, tries a few
well-known CLIs (nvidia-smi, rocm-smi, sysctl), and never raises — if something
can't be detected it's simply reported as unknown. The point is to give the
operator honest, specific guidance ("this model fits, this one won't") instead
of the trial-and-error that a 500 from Ollama forces.
"""
import os
import platform
import re
import shutil
import subprocess


def _run(cmd, timeout=4):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def total_ram_gb():
    # Linux: /proc/meminfo. Fallback: os.sysconf.
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return round(kb / (1024 * 1024), 1)
    except OSError:
        pass
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024**3), 1)
    except (ValueError, OSError):
        return 0.0


def cpu_info():
    cores = os.cpu_count() or 0
    model = platform.processor() or ""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if "model name" in line:
                    model = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    return {"cores": cores, "model": model}


def detect_gpus():
    """Return a list of {vendor, name, vram_gb, vram_free_gb}. Empty if none.

    Detection is layered because under systemd the PATH is minimal and
    `nvidia-smi` often isn't on it, even though the GPU works fine (Ollama talks
    to CUDA directly). We try, in order: nvidia-smi (PATH or known absolute
    paths), then lspci for at least the name, then a Vulkan/DRM hint. This means
    a working GPU is reported even when the smi binary can't be found.
    """
    gpus = []

    # 1) nvidia-smi, whether or not it's on PATH
    smi = _find_binary("nvidia-smi", [
        "/usr/bin/nvidia-smi", "/usr/local/bin/nvidia-smi",
        "/opt/nvidia/bin/nvidia-smi", "/usr/lib/wsl/lib/nvidia-smi",
    ])
    if smi:
        out = _run([smi, "--query-gpu=name,memory.total,memory.free",
                    "--format=csv,noheader,nounits"])
        for line in filter(None, out.splitlines()):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                gpus.append({
                    "vendor": "NVIDIA", "name": parts[0],
                    "vram_gb": _mb_to_gb(parts[1]),
                    "vram_free_gb": _mb_to_gb(parts[2]),
                })
        if gpus:
            return gpus

    # 2) lspci — gets us the card name and vendor even without the driver tools.
    #    VRAM is then estimated from a small built-in table by model name.
    lspci = _find_binary("lspci", ["/usr/bin/lspci", "/sbin/lspci", "/usr/sbin/lspci"])
    if lspci:
        out = _run([lspci])
        for line in out.splitlines():
            low = line.lower()
            if ("vga" in low or "3d controller" in low or "display" in low):
                if "nvidia" in low:
                    name = _clean_lspci_name(line, "NVIDIA")
                    gpus.append({"vendor": "NVIDIA", "name": name,
                                 "vram_gb": _guess_vram(name), "vram_free_gb": None,
                                 "estimated": True})
                elif "amd" in low or "radeon" in low or "advanced micro" in low:
                    name = _clean_lspci_name(line, "AMD")
                    gpus.append({"vendor": "AMD", "name": name,
                                 "vram_gb": _guess_vram(name), "vram_free_gb": None,
                                 "estimated": True})
        if gpus:
            return gpus

    # 3) AMD ROCm
    rocm = _find_binary("rocm-smi", ["/opt/rocm/bin/rocm-smi", "/usr/bin/rocm-smi"])
    if rocm:
        out = _run([rocm, "--showmeminfo", "vram", "--csv"])
        for line in out.splitlines():
            m = re.search(r"(\d+)", line)
            if m and "vram" in line.lower():
                gpus.append({"vendor": "AMD", "name": "AMD GPU",
                             "vram_gb": round(int(m.group(1)) / (1024**3), 1),
                             "vram_free_gb": None})
        if gpus:
            return gpus

    # 4) Apple Silicon (unified memory)
    if platform.system() == "Darwin":
        if "Apple" in _run(["sysctl", "-n", "machdep.cpu.brand_string"]):
            gpus.append({"vendor": "Apple", "name": "Apple Silicon (unified memory)",
                         "vram_gb": total_ram_gb(), "vram_free_gb": None, "unified": True})

    return gpus


def _find_binary(name, candidates):
    """shutil.which first (works when PATH is normal), then known absolute paths
    (works under systemd's minimal PATH)."""
    found = shutil.which(name)
    if found:
        return found
    for path in candidates:
        if os.path.exists(path) and os.access(path, os.X_OK):
            return path
    return None


def _clean_lspci_name(line, vendor):
    # "01:00.0 VGA compatible controller: NVIDIA Corporation TU117 [GeForce GTX 1650] (rev a1)"
    m = re.search(r"\[([^\]]+)\]", line)
    if m:
        return m.group(1)
    # fall back to text after the vendor word
    after = line.split(":", 2)[-1].strip()
    return after or (vendor + " GPU")


# Rough VRAM by GPU model name, used only when we can't read it from the driver.
# Conservative: better to under-state than to over-promise and OOM.
_VRAM_TABLE = [
    ("gtx 1650", 4.0), ("gtx 1660", 6.0), ("gtx 1050", 4.0), ("gtx 1060", 6.0),
    ("gtx 1070", 8.0), ("gtx 1080", 8.0), ("rtx 2060", 6.0), ("rtx 2070", 8.0),
    ("rtx 2080", 8.0), ("rtx 3050", 8.0), ("rtx 3060", 12.0), ("rtx 3070", 8.0),
    ("rtx 3080", 10.0), ("rtx 3090", 24.0), ("rtx 4060", 8.0), ("rtx 4070", 12.0),
    ("rtx 4080", 16.0), ("rtx 4090", 24.0), ("a100", 40.0), ("t4", 16.0),
    ("rx 6600", 8.0), ("rx 6700", 12.0), ("rx 6800", 16.0), ("rx 7900", 20.0),
]


def _guess_vram(name):
    low = (name or "").lower()
    for key, gb in _VRAM_TABLE:
        if key in low:
            return gb
    return 0.0  # unknown -> 0 so recommendations stay cautious


def _mb_to_gb(s):
    try:
        return round(float(s) / 1024, 1)
    except (ValueError, TypeError):
        return 0.0


def usable_vram_gb(gpus):
    """The number that actually matters for 'will this model load'. We use free
    VRAM when we have it (the desktop/browser eat into total), else a fraction
    of total as a realistic budget."""
    if not gpus:
        return 0.0
    g = gpus[0]
    if g.get("unified"):
        return round(g["vram_gb"] * 0.6, 1)
    if g.get("vram_free_gb"):
        return g["vram_free_gb"]
    # Estimated-from-name or total-only: budget ~80% for the model, since the
    # display/desktop consume the rest. Rounds down to stay cautious.
    total = g.get("vram_gb", 0) or 0
    return round(total * 0.8, 1)


# --------------------------------------------------------------------------- #
# Recommendations
# --------------------------------------------------------------------------- #
# Approx VRAM needed to run each model comfortably at Q4, with a small context.
# These are deliberately conservative — better to under-promise than 500.
_MODEL_CATALOG = [
    # (ollama name, rough params, vram needed GB, role, note)
    ("qwen2.5-coder:1.5b", "1.5B", 2.0, "code", "Tiny but coherent; last resort for <3 GB cards."),
    ("qwen2.5-coder:3b", "3B", 3.2, "code", "Best all-round coder for small GPUs. Recommended baseline."),
    ("qwen2.5-coder:7b", "7B", 6.5, "code", "Noticeably better; needs a mid-range GPU or CPU offload."),
    ("qwen2.5-coder:14b", "14B", 11.0, "code", "Strong; needs a large GPU (12 GB+)."),
    ("qwen2.5-coder:32b", "32B", 20.0, "code", "Near the top of local coding; 24 GB GPU territory."),
    ("llama3.2:3b", "3B", 3.0, "chat", "Light, fast general chat."),
    ("llama3.1:8b", "8B", 6.5, "chat", "Well-rounded general chat; mid-range GPU."),
    ("gemma3:4b", "4B", 4.0, "chat", "Capable small general model."),
    ("nomic-embed-text", "-", 0.6, "embed", "Embeddings for the knowledge base (RAG)."),
]


def recommend(role="code", usable_gb=None, ram_gb=None):
    """Split the catalog into fits / tight / too-big for this machine."""
    fits, tight, too_big = [], [], []
    for name, params, need, r, note in _MODEL_CATALOG:
        if r != role:
            continue
        entry = {"name": name, "params": params, "vram_needed_gb": need, "note": note}
        if usable_gb is None:
            fits.append(entry)
        elif need <= usable_gb:
            fits.append(entry)
        elif need <= usable_gb + 2 or (ram_gb and need <= ram_gb):
            entry["note"] += " (runs partly on CPU here — slower)."
            tight.append(entry)
        else:
            too_big.append(entry)
    return {"fits": fits, "tight": tight, "too_big": too_big}


def snapshot():
    """One call the UI/CLI uses: full picture plus recommendations and tips."""
    gpus = detect_gpus()
    ram = total_ram_gb()
    usable = usable_vram_gb(gpus)
    cpu = cpu_info()

    # A recommended default builder model + context for THIS machine.
    code = recommend("code", usable, ram)
    best_code = (code["fits"][-1]["name"] if code["fits"]
                 else (code["tight"][0]["name"] if code["tight"] else "qwen2.5-coder:1.5b"))
    # context sizing: keep it modest on small VRAM
    if usable >= 12:
        num_ctx = 16384
    elif usable >= 8:
        num_ctx = 8192
    elif usable >= 4:
        num_ctx = 4096
    else:
        num_ctx = 2048

    tips = _tips(gpus, usable, ram)

    return {
        "os": f"{platform.system()} {platform.release()}",
        "cpu": cpu,
        "ram_gb": ram,
        "gpus": gpus,
        "usable_vram_gb": usable,
        "has_gpu": bool(gpus),
        "recommended": {
            "builder_model": best_code,
            "num_ctx": num_ctx,
            "code": code,
            "chat": recommend("chat", usable, ram),
            "embed": recommend("embed", usable, ram),
        },
        "tips": tips,
    }


def _tips(gpus, usable, ram):
    tips = []
    if not gpus:
        tips.append("No GPU detected — models will run on CPU. Expect slow "
                    "generation; stick to 3B models and build one file at a time.")
    else:
        g = gpus[0]
        est = " (estimated from the GPU model — Ollama uses the real amount)" if g.get("estimated") else ""
        tips.append(f"Detected {g['vendor']} {g['name']} with about "
                    f"{usable} GB usable for models{est}.")
        if usable and usable < 4:
            tips.append("Under 4 GB usable: close other GPU apps (browsers "
                        "especially) before building, and keep num_ctx at 2048–4096.")
        if usable and usable < 6:
            tips.append("A 7B model will not fully fit — it spills to CPU and gets "
                        "slow, and can 500 if VRAM is exhausted. Prefer a 3B coder.")
    if ram and ram >= 16:
        tips.append(f"{ram} GB RAM lets slightly-too-big models offload to CPU "
                    "instead of failing — usable, just slower.")
    tips.append("For whole-app quality beyond local limits, point the builder at "
                "a hosted API (see Builder settings).")
    return tips
