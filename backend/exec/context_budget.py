"""One derived context budget for chat turns (Buttons rebuild D2).

The rebuild audit (Blueprint art_OYSkk5K4) found three uncoordinated
budgets squeezing the 8b model's window: OLLAMA_NUM_CTX (8192 in code
while .env.example already shipped 16384), a flat 40-message history
trim, and a 3000-char prompt cap. This module is the single
derivation: one num_ctx, carved into explicit shares — system,
tools, history, skills, prompt.

Overflow policy: a turn's variable inputs (prompt, skills, history
after trim) failing to fit their share raise ContextBudgetError
naming the share, measured tokens, and budget — the turn fails
loudly, it is never silently evicted. Static inputs (system prompt,
tool schemas) warn on share overage and fail only when the window
could never fit them at all.

Stdlib-only, under the same contract as engine.py: the deep-agent
child imports it lazily (repo root on the child PYTHONPATH via
deep_agent_env), and system python can import it for validators.
"""

from __future__ import annotations

import os

# 16k is the deploy-shipped value (.env.example OLLAMA_NUM_CTX=16384,
# pull-models.sh builds 16k Modelfiles) — the code default was left
# behind at 8192. Memory headroom on the 14G ollama container: ~5.2G
# weights (qwen3:8b Q4) + ~2.4G KV at 16k + compute buffers fits with
# room; OLLAMA_NUM_PARALLEL=1 keeps a single KV sequence.
DEFAULT_NUM_CTX = 16384
NUM_CTX_ENV = "OLLAMA_NUM_CTX"

# Explicit shares of num_ctx. Sum must be 1.0 — derive_budget
# validates it, so a future share edit cannot silently leak budget.
SHARES: dict[str, float] = {
    "system": 0.30,  # SYSTEM prompt + intent hint preamble
    "tools": 0.20,  # tool schemas (the schema weight that locks 8b up)
    "history": 0.30,  # checkpoint window after trim
    "skills": 0.05,  # intent-keyed injected skills block
    "prompt": 0.15,  # the user's turn input
}

# Conservative token estimate: ~4 chars/token for the English + code
# mix this console sees. Overestimating is the safe direction — it
# rejects a turn the window might have squeezed through rather than
# blowing the real window.
_CHARS_PER_TOKEN = 4


class ContextBudgetError(ValueError):
    """A turn's inputs exceed their budget share. Names the share,
    the measured token count, and the budget so the failure is
    actionable (re-prompt, trim, or reconfigure) — never silent."""

    def __init__(self, share: str, measured: int, budget: int, ctx: int = 0):
        self.share = share
        self.measured = measured
        self.budget = budget
        self.ctx = ctx
        window = f" (num_ctx {ctx})" if ctx else ""
        super().__init__(
            f"{share} needs {measured} tokens, share budget is {budget}"
            f"{window}: trim the input or raise the share"
        )


def num_ctx() -> int:
    """Effective context window. Positive env override wins; unset,
    blank, garbage, and non-positive values fall back to the default
    (same env contract as agent_turn.turn_timeout)."""
    raw = os.environ.get(NUM_CTX_ENV, "").strip()
    if not raw:
        return DEFAULT_NUM_CTX
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_NUM_CTX
    return value if value > 0 else DEFAULT_NUM_CTX


def token_estimate(text: str) -> int:
    """Estimated tokens for one text blob. Empty is zero."""
    if not text:
        return 0
    return max(1, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


def derive_budget(ctx: int | None = None) -> dict[str, int]:
    """Share budgets in tokens. Pure; validates the shares sum so a
    bad share edit fails here, not in a silently squeezed window."""
    ctx = num_ctx() if ctx is None else ctx
    total = sum(SHARES.values())
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"context shares must sum to 1.0, got {total}")
    return {share: int(ctx * fraction) for share, fraction in SHARES.items()}


def require_fit(share: str, text: str, ctx: int | None = None) -> None:
    """Loud gate: text must fit its share. Raises ContextBudgetError
    naming the share, measured tokens, and budget."""
    budget = derive_budget(ctx)[share]
    window = num_ctx() if ctx is None else ctx
    measured = token_estimate(text)
    if measured > budget:
        raise ContextBudgetError(share, measured, budget, ctx=window)


def plan_group_trim(group_tokens: list[int], budget_tokens: int) -> tuple[int, bool]:
    """How many oldest groups to drop so the kept tail fits. Pure.

    Groups are ordered oldest-first, each a token count (deep_chat
    owns the message semantics; this owns the arithmetic). Returns
    (drop_count, fits): groups are dropped from the front until the
    remaining tail fits; fits=False when even the newest group alone
    exceeds the budget — the caller fails the turn loudly.
    """
    total = sum(group_tokens)
    drop = 0
    while total > budget_tokens and drop < len(group_tokens) - 1:
        total -= group_tokens[drop]
        drop += 1
    return drop, total <= budget_tokens


def check_static(
    system_text: str, tools_text: str, ctx: int | None = None
) -> tuple[list[str], str | None]:
    """Static-input (system prompt, tool schemas) budget report. Pure.

    Returns (warnings, fatal): a share overage warns on stderr — the
    schema weight is exactly what D1 slims, so visibility beats a
    bricked turn — while a total that cannot fit the window at all is
    fatal (every turn would fail; say so at build time instead).
    """
    window = num_ctx() if ctx is None else ctx
    budget = derive_budget(window)
    warnings: list[str] = []
    system_tokens = token_estimate(system_text)
    tools_tokens = token_estimate(tools_text)
    if system_tokens > budget["system"]:
        warnings.append(
            f"system prompt ~{system_tokens} tok over share "
            f"{budget['system']} (num_ctx {window})"
        )
    if tools_tokens > budget["tools"]:
        warnings.append(
            f"tool schemas ~{tools_tokens} tok over share "
            f"{budget['tools']} (num_ctx {window})"
        )
    total = system_tokens + tools_tokens
    fatal = None
    if total > window:
        fatal = (
            f"static turn inputs ~{total} tok exceed the {window} tok "
            "window before any history or prompt — the turn can never fit"
        )
    return warnings, fatal
