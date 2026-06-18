# services/cookbook_lite.py
"""
Lite Cookbook — Odysseus Lite overlay (Phase 3).

"Runs on 4GB, no GPU" describes the app shell, not a usable AI. The upstream
Cookbook is a 270-model hardware scanner; that's too heavy and too noisy for the
non-technical lite user. This overlay gives them the one thing they actually
need on first run: a concrete, honest model recommendation for *their* machine.

Hardware detection is CPU/RAM-only: it reads /proc/meminfo and /proc/cpuinfo
(psutil fallback) and never spawns nvidia-smi/rocm-smi — has_gpu is always
False. Recommendations come from a tiny curated, RAM-tiered table
(cookbook_lite_models.json). No 270-model scan, no GPU probe.

Also provides an OPT-IN first-run bootstrap: if Ollama is reachable and no model
is configured, it can pull a sensible small default — but only when the operator
sets LITE_AUTOPULL_MODEL=true AND the caller passes an explicit confirm. Nothing
is ever downloaded silently.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_MODELS_JSON = os.path.join(os.path.dirname(__file__), "cookbook_lite_models.json")


@lru_cache(maxsize=1)
def _load_table() -> Dict:
    with open(_MODELS_JSON, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Hardware detection — CPU/RAM only, no GPU probing
#
# The lite cookbook is for CPU-only 4 GB hosts. nvidia-smi / rocm-smi
# subprocess calls (via hwfit.detect_system) have no place here: they add
# latency, produce confusing log noise on GPU-less boxes, and the
# resulting has_gpu flag is never surfaced in the lite UI anyway.
# We read RAM from /proc/meminfo (Linux) or psutil (fallback) and CPU count
# from os; no subprocess is ever spawned.
# ---------------------------------------------------------------------------
def _read_proc_meminfo() -> tuple[float, float]:
    """Return (total_ram_gb, available_ram_gb) from /proc/meminfo, or (0,0)."""
    try:
        data: Dict[str, int] = {}
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    try:
                        data[key] = int(parts[1])
                    except ValueError:
                        pass
        total_kb = data.get("MemTotal", 0)
        avail_kb = data.get("MemAvailable", data.get("MemFree", 0))
        return round(total_kb / 1024 / 1024, 1), round(avail_kb / 1024 / 1024, 1)
    except Exception:
        return 0.0, 0.0


def _read_cpu_name() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.startswith("model name"):
                    return line.split(":", 1)[-1].strip()
    except Exception:
        pass
    return "unknown"


def detect_hardware() -> Dict:
    """Return {total_ram_gb, available_ram_gb, cpu_cores, cpu_name, has_gpu}.

    Lite-safe: reads /proc/meminfo and /proc/cpuinfo only.  No nvidia-smi,
    no rocm-smi, no subprocesses.  has_gpu is always False — the lite
    cookbook is CPU-only and never surfaces GPU options.
    """
    total_ram, avail_ram = _read_proc_meminfo()

    if total_ram == 0.0:
        try:
            import psutil
            total_ram = round(psutil.virtual_memory().total / 1024 ** 3, 1)
            avail_ram = round(psutil.virtual_memory().available / 1024 ** 3, 1)
        except Exception as e:
            logger.debug("psutil unavailable for RAM detection: %s", e)

    return {
        "total_ram_gb": total_ram,
        "available_ram_gb": avail_ram,
        "cpu_cores": os.cpu_count() or 1,
        "cpu_name": _read_cpu_name(),
        "has_gpu": False,
    }


def _tier_for_ram(total_ram_gb: float) -> Dict:
    """Pick the curated tier whose RAM band contains total_ram_gb."""
    table = _load_table()
    tiers = table["tiers"]
    # If detection failed (0), default to the balanced tier rather than minimal,
    # since most laptops have >=4GB; the UI also shows that detection was unknown.
    if not total_ram_gb:
        return next((t for t in tiers if t["id"] == "balanced"), tiers[0])
    for tier in tiers:
        if tier["min_ram_gb"] <= total_ram_gb < tier["max_ram_gb"]:
            return tier
    return tiers[-1]


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------
def recommend(hardware: Optional[Dict] = None) -> Dict:
    """Return a first-run recommendation for the detected (or given) hardware."""
    hw = hardware or detect_hardware()
    table = _load_table()
    tier = _tier_for_ram(hw["total_ram_gb"])

    # The headline pick: the first model of the tier (curated order = preference).
    models: List[Dict] = tier.get("models", [])
    primary = models[0] if models else None

    # Recommend API mode when local is genuinely uncomfortable (very low RAM and
    # detection succeeded).
    suggest_api = bool(hw["total_ram_gb"]) and hw["total_ram_gb"] < 3

    return {
        "hardware": hw,
        "detection_ok": bool(hw["total_ram_gb"]),
        "tier": {
            "id": tier["id"],
            "label": tier["label"],
            "summary": tier["summary"],
            "tokens_per_sec": tier["tokens_per_sec"],
        },
        "primary": primary,
        "models": models,
        "suggest_api": suggest_api,
        "api_fallback": table["api_fallback"],
        "autopull_enabled": _env_bool("LITE_AUTOPULL_MODEL", False),
    }


# ---------------------------------------------------------------------------
# Ollama bootstrap (opt-in, never silent)
# ---------------------------------------------------------------------------
def _ollama_base_url() -> str:
    in_docker = os.path.exists("/.dockerenv")
    base = (
        os.getenv("OLLAMA_BASE_URL")
        or os.getenv("OLLAMA_URL")
        or ("http://host.docker.internal:11434" if in_docker else "http://127.0.0.1:11434")
    )
    # Callers may have set the OpenAI-compat ".../v1" form; normalize to the root.
    return base.rstrip("/").removesuffix("/v1")


def ollama_status() -> Dict:
    """Probe Ollama: {reachable: bool, models: [names], base_url}."""
    import httpx
    base = _ollama_base_url()
    try:
        r = httpx.get(f"{base}/api/tags", timeout=3)
        r.raise_for_status()
        data = r.json()
        names = [m.get("name") for m in data.get("models", []) if m.get("name")]
        return {"reachable": True, "models": names, "base_url": base}
    except Exception as e:
        logger.debug("Ollama not reachable at %s: %s", base, e)
        return {"reachable": False, "models": [], "base_url": base, "error": str(e)}


def can_autopull() -> Dict:
    """Whether a first-run autopull is permitted right now (and why/why not)."""
    enabled = _env_bool("LITE_AUTOPULL_MODEL", False)
    status = ollama_status()
    has_any = bool(status["models"])
    reason = None
    if not enabled:
        reason = "LITE_AUTOPULL_MODEL is not enabled"
    elif not status["reachable"]:
        reason = "Ollama is not reachable"
    elif has_any:
        reason = "a model is already installed"
    return {
        "allowed": enabled and status["reachable"] and not has_any,
        "reason": reason,
        "ollama": status,
    }


def autopull(model: Optional[str] = None, confirm: bool = False) -> Dict:
    """Pull a small default model via Ollama. Refuses unless explicitly allowed.

    Guard rails (all must hold):
      - LITE_AUTOPULL_MODEL=true (operator opt-in), AND
      - the caller passes confirm=True (explicit user action), AND
      - Ollama is reachable.

    Returns {"ok": bool, "model": str, "message": str}. Never downloads silently.
    """
    if not confirm:
        return {"ok": False, "message": "Refused: explicit user confirmation required."}
    gate = can_autopull()
    if not _env_bool("LITE_AUTOPULL_MODEL", False):
        return {"ok": False, "message": "Refused: LITE_AUTOPULL_MODEL is not enabled."}
    if not gate["ollama"]["reachable"]:
        return {"ok": False, "message": "Refused: Ollama is not reachable."}

    # Default model = the primary pick for the detected tier.
    chosen = model or (recommend()["primary"] or {}).get("name")
    if not chosen:
        return {"ok": False, "message": "No suitable default model for this hardware."}

    import httpx
    base = _ollama_base_url()
    try:
        # stream=False keeps it a single blocking call; the caller should warn
        # the user about size/time before invoking this.
        with httpx.stream("POST", f"{base}/api/pull",
                          json={"name": chosen, "stream": False}, timeout=None) as resp:
            resp.raise_for_status()
            for _ in resp.iter_lines():
                pass
        return {"ok": True, "model": chosen, "message": f"Pulled {chosen} via Ollama."}
    except Exception as e:
        return {"ok": False, "model": chosen, "message": f"Pull failed: {e}"}
