"""Per-turn cost quotes over ledger usage (issue #398). Stdlib-only.

quote(model, usage) -> (cost_usd | None, metered): ollama:* is
local/free; nebius:* is metered at the card below when the model is
known, otherwise cost is None (paid but unrated — the UI shows a
bare "metered" tag rather than an invented number). Anything else
is treated as metered-unknown: only an explicit ollama: prefix
earns the free tag.

Rates are $/1M tokens (input, output), verified 2026-11-03 against
the Token Factory catalog (dev.nebius.com cookbook + nebius.com
model pages). The catalog moves: METERED_RATE_IN_PER_1M /
METERED_RATE_OUT_PER_1M override the rate for models missing from
the card without a code push. Never raises.
"""

import os

# Model-id substring -> ($/1M in, $/1M out). Longest match wins so
# GLM-5.3-Flash never falls into the full-size GLM-5 bucket.
RATE_CARD = {
    "zai-org/GLM-5.3-Flash": (0.15, 0.50),
    "Qwen/Qwen3-235B-A22B-Instruct-2507": (0.20, 0.60),
    "Qwen/Qwen3-30B-A3B-Instruct-2507": (0.10, 0.30),
    "zai-org/GLM-5": (1.40, 4.40),
}

FREE_PREFIX = "ollama:"

SPEND_CAP_ENV = "NEBIUS_SPEND_CAP_USD"


def spend_cap():
    """Configured metered-spend cap in USD, or None when uncapped.

    Unset/blank/unparseable means uncapped (status quo); 0 blocks
    every metered call; negatives clamp to 0. Never raises.
    """
    try:
        raw = (os.environ.get(SPEND_CAP_ENV) or "").strip()
        if not raw:
            return None
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


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


def quote(model, usage):
    """(cost_usd | None, metered) for one turn. Never raises."""
    try:
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
