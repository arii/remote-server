"""Append-only local run ledger for deep-agent/ (cf. buttons Ledger tab).

Each entry: timestamp, script, model, thread, duration, tool calls,
token usage, final preview. Usage totals power Nebius spend tracking.
Reader: python ledger.py [n]  (prints last n entries, compact)
"""

import fcntl
import json
import math
import os
import sys
import time

import cli
import pricing


# EVAL_LEDGER lets parallel eval gates isolate their ledgers; the eval
# scorer reads the same knob, so writer and scorer stay consistent.
def state_dir():
    """Writable state dir for the run ledger + traces.

    DEEP_AGENT_STATE_DIR points it at persisted storage (the buttons
    image sets /var/lib/buttons/deep-agent so Past-runs survive
    recreates); default is this source dir for dev.
    """
    return os.environ.get("DEEP_AGENT_STATE_DIR") or os.path.dirname(
        os.path.abspath(__file__)
    )


LEDGER = os.environ.get("EVAL_LEDGER") or os.path.join(state_dir(), "ledger.jsonl")


def _as_dict(obj):
    return obj if isinstance(obj, dict) else None


def _usage_total(messages):
    total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for m in messages or []:
        if m is None:
            continue
        d = _as_dict(m)
        u = (d.get("usage_metadata") if d else getattr(m, "usage_metadata", None)) or {}
        if not isinstance(u, dict):
            u = {}
        for k in total:
            total[k] += u.get(k, 0) or 0
        if not any(u.get(k) for k in total):
            # Fallback: Ollama-style counts surfaced via
            # response_metadata/generation_info instead of
            # usage_metadata (streaming chunks, older versions).
            r = (
                d.get("response_metadata")
                if d
                else getattr(m, "response_metadata", None)
            ) or {}
            if not isinstance(r, dict):
                continue
            prompt_n = r.get("prompt_eval_count") or 0
            eval_n = r.get("eval_count") or 0
            total["input_tokens"] += prompt_n
            total["output_tokens"] += eval_n
            total["total_tokens"] += prompt_n + eval_n
    cost = _reported_cost(messages)
    if cost is not None:
        total["reported_cost_usd"] = cost
    return total


def error_usage(error):
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) or {}
    return _usage_total([{"response_metadata": {"headers": dict(headers)}}])


def _model_provider(model):
    """Provider slot from a ledger model label, or None.

    Ledger labels read `{provider}:{model}` (deep_chat,
    ask_investigator already write these); registered providers
    count. A bare `qwen3:8b` keeps no provider rather than minting
    a bogus one (same rule as runs._row_provider). Never raises.
    """
    if isinstance(model, str):
        for prefix in (provider + ":" for provider in pricing.PROVIDERS):
            if model.startswith(prefix):
                return prefix[:-1]
    return None


def _reported_cost(messages):
    total = 0.0
    count = 0
    for message in messages or []:
        data = _as_dict(message)
        metadata = (
            data.get("response_metadata", {})
            if data is not None
            else getattr(message, "response_metadata", {})
        ) or {}
        if not isinstance(metadata, dict):
            return None
        kind = data.get("type") if data is not None else getattr(message, "type", None)
        if kind in ("human", "system", "tool"):
            continue
        count += 1
        headers = metadata.get("headers") or {}
        if not isinstance(headers, dict):
            headers = {}
        header = next(
            (
                value
                for key, value in headers.items()
                if isinstance(key, str) and key.lower() == "x-cost-usd"
            ),
            None,
        )
        usage = metadata.get("token_usage") or metadata.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        cost = None
        for value in (header, usage.get("cost")):
            if isinstance(value, bool):
                continue
            try:
                candidate = float(value)
            except (ValueError, TypeError, OverflowError):
                continue
            if math.isfinite(candidate) and candidate >= 0:
                cost = candidate
                break
        if cost is None:
            return None
        total += cost
    return total if count and math.isfinite(total) else None


def _is_test_thread(thread_id):
    """True for test-harness threads, which never meter.

    Opt-in by prefix, never by substring: harness threads MUST be
    named `test-*` (a substring rule would silently zero real
    production spend on threads like `latest-test-run`). Unit
    tests that forget to redirect LEDGER write here; test traffic
    must never fail-close the Nebius spend gate. Never raises.
    """
    try:
        tid = str(thread_id or "")
    except Exception:
        return False
    return tid == "test" or tid.startswith("test-")


def record(
    script,
    model,
    thread_id,
    messages,
    started,
    extra=None,
    usage=None,
    calls=None,
    provider=None,
):
    """Append one run entry; returns it. Never raises on odd inputs.

    usage: optional pre-aggregated {"input_tokens", "output_tokens",
    "total_tokens"} mapping. Streaming callers that discard message
    chunks (so _usage_total would see zeros) pass their own totals.
    calls: optional pre-aggregated [{"name", "args"}] list, used when
    the message list no longer carries the tool-call records
    (streaming reassembly, failure paths).
    provider: backend that ran the turn (ollama, openrouter, nebius).
    Explicit wins; otherwise derived from a `provider:model` label;
    bare labels stay provider-less (fail-closed: only an explicit
    ollama label or provider earns the free tag).
    Test-harness threads (see _is_test_thread) always file
    metered=False at zero cost — test traffic never meters.
    """
    try:
        derived = calls is None
        if derived:
            calls = []
        results = []
        for m in messages or []:
            if m is None:
                continue
            d = _as_dict(m)
            tool_calls = (
                d.get("tool_calls") if d else getattr(m, "tool_calls", [])
            ) or []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                if derived:
                    calls.append(
                        {"name": tc.get("name"), "args": str(tc.get("args"))[:300]}
                    )
            cls = d.get("type", "") if d else m.__class__.__name__
            if cls in ("ToolMessage", "tool"):
                name = d.get("name", "?") if d else getattr(m, "name", "?")
                content = d.get("content", "") if d else getattr(m, "content", "")
                if isinstance(content, list):
                    content = " ".join(
                        str(p.get("text", "")) if isinstance(p, dict) else str(p)
                        for p in content
                    )
                results.append({"name": name, "result": str(content)[:500]})
        try:
            duration = round(time.time() - float(started), 1)
        except (TypeError, ValueError):
            duration = -1
        final = ""
        if messages:
            last = messages[-1]
            if last is not None:
                d = _as_dict(last)
                content = d.get("content", "") if d else getattr(last, "content", "")
                if isinstance(content, list):
                    content = " ".join(
                        str(p.get("text", "")) if isinstance(p, dict) else str(p)
                        for p in content
                    )
                final = str(content)[:500]
        extra = dict(extra or {})
        # Cap stored prompts: Continue needs the text, not unbounded input.
        prompt = extra.get("prompt")
        if isinstance(prompt, str) and len(prompt) > 4000:
            extra["prompt"] = prompt[:4000]
        usage = dict(usage) if isinstance(usage, dict) else _usage_total(messages)
        # Record-time quote freezes the rate: rows stay true even
        # when the card moves. Old rows without these fields are
        # derived at read time (current-rate estimates, no rewrite).
        if provider is None:
            provider = _model_provider(model)
        if provider not in pricing.PROVIDERS:
            provider = None
        cost_usd, metered = pricing.quote(model, usage, provider=provider)
        label = f"{provider}:{model}" if provider else model
        if (
            extra.get("usage_missing")
            and metered
            and pricing.rate_for(label) != (0.0, 0.0)
        ):
            cost_usd, metered = None, True
        if provider == "twt":
            cost_usd, metered = _reported_cost(messages), True
            if "reported_cost_usd" in usage:
                value = usage["reported_cost_usd"]
                cost_usd = (
                    float(value)
                    if isinstance(value, (float, int))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    and value >= 0
                    else None
                )
        if provider == "ollama":
            # The resolved backend is truth: a locally-run turn is
            # free even when the model string carries no prefix.
            metered, cost_usd = False, 0.0
        if extra.get("llm_call") is False:
            metered, cost_usd = False, 0.0
        if _is_test_thread(thread_id):
            metered, cost_usd = False, 0.0
        entry = {
            "kind": "deep-agent-run",
            "ts": time.time(),
            "script": script,
            "model": model,
            "provider": provider,
            "thread_id": thread_id,
            "duration_s": duration,
            "tool_calls": calls,
            "tool_results": results,
            "usage": usage,
            "cost_usd": cost_usd,
            "metered": metered,
            "final_preview": final,
            "extra": extra,
        }
        parent = os.path.dirname(LEDGER)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(LEDGER, "a", encoding="utf-8") as f:
            # Matches runs._tombstone_lines: appends never interleave
            # with a delete/clear rewrite from another process.
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(json.dumps(entry) + "\n")
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return entry
    except Exception as exc:  # noqa: BLE001 - ledger must never break a run
        print(f"ledger record failed: {exc}", file=sys.stderr)
        return {}


def main() -> int:
    try:
        n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    except ValueError:
        return cli.usage_error("n must be an integer")
    if not os.path.exists(LEDGER):
        print("ledger empty")
        return 0
    with open(LEDGER, encoding="utf-8") as f:
        lines = f.readlines()
    shown = 0
    for line in lines[-n:] if n > 0 else []:
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            print("(skipping corrupt ledger line)", file=sys.stderr)
            continue
        if not isinstance(e, dict):
            continue
        u = e.get("usage", {}) or {}
        tools = [
            c.get("name") for c in e.get("tool_calls", []) or [] if isinstance(c, dict)
        ]
        print(
            f"{e.get('script')} thread={e.get('thread_id')} "
            f"dur={e.get('duration_s')}s "
            f"tools={tools} "
            f"tok={u.get('total_tokens') if isinstance(u, dict) else '?'} "
            f"final={str(e.get('final_preview', ''))[:120]!r}"
        )
        for r in (e.get("tool_results", []) or [])[:8]:
            if not isinstance(r, dict):
                continue
            print(f"    = {r.get('name')}: {str(r.get('result', ''))[:150]}")
        shown += 1
    if shown == 0:
        print("ledger empty")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
