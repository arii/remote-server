"""Per-turn cost quotes over ledger usage (issue #398). Stdlib-only.

quote(model, usage) -> (cost_usd | None, metered): ollama:* is
local/free; nebius:* is metered at the card below when the model is
known, otherwise cost is None (paid but unrated — the UI shows a
bare "metered" tag rather than an invented number). Anything else
is treated as metered-unknown: only an explicit ollama: prefix
earns the free tag.

Rates are $/1M tokens (input, output), re-verified 2026-11-06
against the live Token Factory catalog
(GET /v1/models?verbose=true, per-model pricing.prompt /
pricing.completion per token x 1M). The catalog moves:
METERED_RATE_IN_PER_1M / METERED_RATE_OUT_PER_1M override the
rate for models missing from the card without a code push.
Never raises.
"""

import os

# Model-id substring -> ($/1M in, $/1M out). Longest match wins so
# GLM-5.3-Flash never falls into the full-size GLM-5 bucket.
RATE_CARD = {
    "zai-org/GLM-5.3-Flash": (0.15, 0.50),
    "Qwen/Qwen3-235B-A22B-Instruct-2507": (0.20, 0.60),
    "Qwen/Qwen3-30B-A3B-Instruct-2507": (0.10, 0.30),
    "zai-org/GLM-5": (1.40, 4.40),
    # Nemotron rates verified 2026-11-06 against the live Token
    # Factory catalog (per-token pricing.prompt/completion x 1M).
    # Missing these fail-closed the whole Nebius fallback on one
    # unrated vision row (Nemotron-3_5-Lightning).
    "nvidia/Nemotron-3_5-Lightning": (0.06, 0.24),
    "nvidia/Nemotron-3.5-Super-VL": (0.30, 0.90),
}

FREE_PREFIX = "ollama:"

# Sorry-sentinel model labels: the harness records these when no
# model was ever resolved, so no model call could have spent
# anything (coder._implement_and_finish pre-resolve crashes file
# model="unknown"; auditor failure rows file "unknown:model").
# With an explicit all-zero token triple they quote $0 non-metered
# (#1159: zero-token sorry rows must not fail-close the spend
# gate). Anything else with these labels — real tokens, or a
# missing/partial usage that proves nothing — stays fail-closed.
SORRY_MODELS = frozenset({"unknown", "unknown:model"})

ZERO_TRIPLE = ("input_tokens", "output_tokens", "total_tokens")

SPEND_CAP_ENV = "NEBIUS_SPEND_CAP_USD"

# Fail-closed default when NEBIUS_SPEND_CAP_USD is unset/blank
# (#885): an Ollama outage must not silently route unlimited paid
# traffic. Explicit-set behavior is unchanged — only the unset
# default moved from uncapped to this conservative cap.
DEFAULT_SPEND_CAP_USD = 5.0


def spend_cap():
    """Configured metered-spend cap in USD. Never None, never raises.

    Unset/blank means the conservative DEFAULT_SPEND_CAP_USD; 0
    blocks every metered call; negatives clamp to 0. Unparseable
    fails closed to 0 (a typo'd cap must block, never silently
    uncap).
    """
    try:
        raw = (os.environ.get(SPEND_CAP_ENV) or "").strip()
        if not raw:
            return DEFAULT_SPEND_CAP_USD
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def _env_rates():
    """Override rate for uncarded models, or None. Never raises."""
    try:
        raw_in = os.environ.get("METERED_RATE_IN_PER_1M")
        raw_out = os.environ.get("METERED_RATE_OUT_PER_1M")
        if not raw_in or not raw_out:
            return None
        return (float(raw_in), float(raw_out))
    except (TypeError, ValueError):
        return None


def _card_rate(model):
    best = None
    for key, rate in RATE_CARD.items():
        if key in model and (best is None or len(key) > len(best[0])):
            best = (key, rate)
    return best[1] if best else None


def _tokens(usage):
    """(input, output) ints. Total-only usage bills at the output
    (higher) rate: spend estimates never understate. Never raises."""
    try:
        usage = usage or {}
        n_in = int(usage.get("input_tokens") or 0)
        n_out = int(usage.get("output_tokens") or 0)
        if n_in == 0 and n_out == 0:
            n_out = int(usage.get("total_tokens") or 0)
        return max(n_in, 0), max(n_out, 0)
    except (TypeError, ValueError):
        return 0, 0


def rate_for(model):
    """(in, out) $/1M card rate for a metered model, else None.

    Same precedence as quote(): longest card substring wins, then
    the METERED_RATE_* env override. Free (ollama:) models and
    unrated models yield None — the caller shows 'free' or a bare
    'metered' tag instead of an invented number. Never raises."""
    try:
        if not isinstance(model, str) or not model:
            return None
        if model.startswith(FREE_PREFIX):
            return None
        return _card_rate(model) or _env_rates()
    except Exception:
        return None


def _is_sorry_zero(model, usage):
    """True for a sorry-sentinel label with explicit zero tokens.

    Per-token rates make 0 tokens $0 on every backend, so there is
    no spend to rate — unlike a bare real-model label with zeros
    (#1121: usage may have been lost in a crash, stays
    fail-closed). Never raises."""
    try:
        if model not in SORRY_MODELS:
            return False
        if not isinstance(usage, dict):
            return False
        return all(int(usage.get(key) or 0) == 0
                   and key in usage for key in ZERO_TRIPLE)
    except (TypeError, ValueError, AttributeError):
        return False


def quote(model, usage):
    """(cost_usd | None, metered) for one turn. Never raises."""
    try:
        if _is_sorry_zero(model, usage):
            return 0.0, False
        if not isinstance(model, str) or not model:
            return None, True
        if model.startswith(FREE_PREFIX):
            return 0.0, False
        rate = _card_rate(model) or _env_rates()
        if rate is None:
            return None, True
        n_in, n_out = _tokens(usage)
        cost = n_in / 1e6 * rate[0] + n_out / 1e6 * rate[1]
        return round(cost, 6), True
    except Exception:
        return None, True
