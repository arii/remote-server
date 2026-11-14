"""Shared turn engine: one process model for chat and agent turns.

The rebuild audit (Blueprint art_OYSkk5K4, D4) found the chat runner
and the one-shot agent runner duplicating the timeout/grace/kill
constants — three copies that drifted. This module is the single
source: env-resolvable turn timeouts sized for ~6 tok/s local decode,
the kill-escalation ladder (SIGTERM → bounded grace → SIGKILL), the
slow-turn heartbeat interval, and the one default model name.

Stdlib-only (same contract as engine.py/context_budget.py): the
deep-agent child imports it, and system python can too. The SSE
streaming, runs-registry, and reattach durability stay in their
routers — this is the process-model layer both routers share.
"""

from __future__ import annotations

import asyncio
import os

# The one default model name (D2): qwen3:8b. model_router surfaces it
# through ASK_MODEL; no other module may define a competing default.
DEFAULT_MODEL = "qwen3:8b"

# Bounded wait after SIGTERM before SIGKILL, in seconds. Chat and
# agent turns share one number: a half-dead child is a half-dead
# child whichever runner spawned it.
KILL_GRACE = 5

# A slow local turn must not read as a hang (D2): when no token has
# streamed for this long, the chat stream emits a `heartbeat` SSE
# frame with elapsed and budget-left. Sized well under the ~6 tok/s
# decode stall a CPU box can produce mid-generation. Receivers that
# do not know the event simply ignore it (named SSE events with no
# listener are dropped), and the frontend's stall tracker already
# treats any frame as liveness.
HEARTBEAT_INTERVAL = 20.0


def resolve_timeout(default: int, env_name: str) -> int:
    """Effective turn budget in seconds. Pure env contract, one
    implementation for every runner.

    A positive-int env override wins (per-deploy flip, no code —
    sized against measured ~6 tok/s local decode, where one full
    agentic turn with tool calls can legitimately run ten-plus
    minutes). Unset, blank, garbage, and non-positive values fall
    back to the runner's default.
    """
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def sync_stop_proc(proc, grace: int = KILL_GRACE) -> None:
    """Terminate, escalate to kill, reap bounded. Never raises, and
    returns at once when the process already exited."""
    try:
        if proc.poll() is not None:
            return
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=grace)
    except Exception:
        pass
    try:
        alive = proc.poll() is None
    except Exception:
        alive = True
    if alive:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=grace)
        except Exception:
            pass


async def async_stop_proc(proc, grace: int = KILL_GRACE) -> None:
    """Async twin of sync_stop_proc (event-loop runners)."""
    try:
        if proc.poll() is not None:
            return
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        await asyncio.wait_for(asyncio.to_thread(proc.wait), grace)
    except (asyncio.TimeoutError, Exception):
        pass
    if proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(asyncio.to_thread(proc.wait), grace)
        except (asyncio.TimeoutError, Exception):
            pass
