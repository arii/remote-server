"""Deterministic-first model routing for S5 agent slices.

Greenfield rule: try deterministic commands first (td/gh/subprocess JSON)
and reach for an LLM only for judgment/explanation/summarization. When an
LLM is needed, this module picks the cheapest feasible one:

  1. explicit ``prefer`` (or NEBIUS_ONLY=1) wins,
  2. otherwise local Ollama (``ASK_MODEL``, default qwen3:8b) when reachable,
  3. otherwise Nebius (``NEBIUS_MODEL``, default zai-org/GLM-5.3-Flash —
     proven in harness bring-up, Flash tier = cheap) when a key exists,
  4. otherwise raise with an actionable message.

Stdlib only on purpose: system-python tests import this without the
deep-agent venv. Never touches secrets — no API keys in specs, kwargs, or
logs; the caller passes its own key to ChatOpenAI from the environment.

Env contract:
  ASK_MODEL (default qwen3:8b), OLLAMA_URL (default http://ollama:11434),
  NEBIUS_MODEL (default zai-org/GLM-5.3-Flash),
  NEBIUS_BASE_URL (default https://api.tokenfactory.nebius.com/v1),
  NEBIUS_API_KEY (required for the nebius path),
  NEBIUS_ONLY=1 (force cloud), OLLAMA_STRICT=1 (fail instead of falling back).

Routing ladder (policy, 2026-11-02):
  deterministic command -> Ollama qwen3:8b -> Ollama qwen3:14b ->
  API-token LLM. The 14b tier is selected today by setting
  ASK_MODEL=qwen3:14b (no separate code path); it becomes the
  default significant-local tier once tok/s is measured on the
  5850U after the terminal rebuild (#246).
  Route to the API-token model only when the task needs
  write-mode multi-step execution, context the 8b window cannot
  hold, or judged output the 8b eval gate does not cover.
  OLLAMA_STRICT=1 keeps the box usable offline (fail, never cloud).
"""

import os
import urllib.request

DEFAULT_OLLAMA_MODEL = "qwen3:8b"
DEFAULT_OLLAMA_URL = "http://ollama:11434"
DEFAULT_NEBIUS_MODEL = "zai-org/GLM-5.3-Flash"
DEFAULT_NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1"


def _env(name, default=""):
    val = os.environ.get(name, default)
    if not isinstance(val, str) or not val.strip():
        return default
    return val


def ollama_reachable(base_url=None, timeout=5):
    """True when the Ollama API answers, False on any failure."""
    base = (base_url or _env("OLLAMA_URL", DEFAULT_OLLAMA_URL)).rstrip("/")
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=timeout) as res:
            return res.status == 200
    except Exception:
        return False


def _ollama_spec():
    return {
        "provider": "ollama",
        "model": _env("ASK_MODEL", DEFAULT_OLLAMA_MODEL),
        "base_url": _env("OLLAMA_URL", DEFAULT_OLLAMA_URL),
    }


def _nebius_spec():
    if not _env("NEBIUS_API_KEY"):
        raise RuntimeError(
            "Nebius fallback needs NEBIUS_API_KEY in the environment"
        )
    return {
        "provider": "nebius",
        "model": _env("NEBIUS_MODEL", DEFAULT_NEBIUS_MODEL),
        "base_url": _env("NEBIUS_BASE_URL", DEFAULT_NEBIUS_BASE_URL),
    }


def resolve_llm(prefer=None):
    """Pick the cheapest feasible LLM. Returns {"provider","model","base_url"}.

    ``prefer`` is "ollama" or "nebius" (strict: no fallback). With no
    prefer: Ollama when reachable, else Nebius when keyed, else raise.
    OLLAMA_STRICT=1 disables the fallback on the default path too.
    """
    only_nebius = _env("NEBIUS_ONLY") == "1"
    strict_ollama = _env("OLLAMA_STRICT") == "1"
    want = (prefer or ("nebius" if only_nebius else "")).strip().lower()
    if want and want not in ("ollama", "nebius"):
        raise ValueError(f"bad provider {prefer!r}: want 'ollama' or 'nebius'")
    if want == "nebius":
        return _nebius_spec()
    if want == "ollama":
        if ollama_reachable():
            return _ollama_spec()
        raise RuntimeError("Ollama unreachable at " + _env("OLLAMA_URL", DEFAULT_OLLAMA_URL))
    if ollama_reachable():
        return _ollama_spec()
    if strict_ollama:
        raise RuntimeError(
            "Ollama unreachable and OLLAMA_STRICT=1 refuses the Nebius fallback"
        )
    return _nebius_spec()


def chat_kwargs(spec):
    """Constructor kwargs for ChatOllama / ChatOpenAI. No secrets inside —
    the caller supplies api_key itself, and specs must never be logged."""
    if spec.get("provider") == "ollama":
        return {
            "model": spec["model"],
            "base_url": spec["base_url"],
            "temperature": 0,
        }
    if spec.get("provider") == "nebius":
        return {
            "model": spec["model"],
            "base_url": spec["base_url"],
            "temperature": 0,
        }
    raise ValueError(f"bad spec provider: {spec.get('provider')!r}")
