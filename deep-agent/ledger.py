"""Append-only local run ledger for deep-agent/ (cf. buttons Ledger tab).

Each entry: timestamp, script, model, thread, duration, tool calls,
token usage, final preview. Usage totals power Nebius spend tracking.
Reader: python ledger.py [n]  (prints last n entries, compact)
"""

import json
import os
import sys
import time

LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.jsonl")


def _usage_total(messages):
    total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for m in messages:
        u = getattr(m, "usage_metadata", None) or {}
        for k in total:
            total[k] += u.get(k, 0) or 0
    return total


def record(script, model, thread_id, messages, started, extra=None):
    """Append one run entry; returns it. Never raises on odd inputs."""
    try:
        calls = []
        for m in messages:
            for tc in getattr(m, "tool_calls", []) or []:
                calls.append(
                    {"name": tc.get("name"), "args": str(tc.get("args"))[:300]}
                )
        entry = {
            "kind": "deep-agent-run",
            "ts": time.time(),
            "script": script,
            "model": model,
            "thread_id": thread_id,
            "duration_s": round(time.time() - started, 1),
            "tool_calls": calls,
            "usage": _usage_total(messages),
            "final_preview": str(messages[-1].content)[:500] if messages else "",
            "extra": extra or {},
        }
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return entry
    except Exception as exc:  # noqa: BLE001 - ledger must never break a run
        print(f"ledger record failed: {exc}", file=sys.stderr)
        return {}


def main() -> int:
    try:
        n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    except ValueError:
        print("n must be an integer", file=sys.stderr)
        return 2
    if not os.path.exists(LEDGER):
        print("ledger empty")
        return 0
    with open(LEDGER, encoding="utf-8") as f:
        lines = f.readlines()
    for line in lines[-n:]:
        e = json.loads(line)
        u = e.get("usage", {})
        print(
            f"{e.get('script')} thread={e.get('thread_id')} "
            f"dur={e.get('duration_s')}s "
            f"tools={[c['name'] for c in e.get('tool_calls', [])]} "
            f"tok={u.get('total_tokens')} "
            f"final={e.get('final_preview', '')[:120]!r}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
