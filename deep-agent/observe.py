"""Run-trace observability for S5 agent slices (deep-agent/).

Middleware factory (`trace_middleware`) built on the documented
langchain hooks (`wrap_tool_call`, `before_model`, `after_model`) plus
a stdlib-only JSONL recorder. Every run records: boundaries, model
calls with token usage, and tool calls with args/result/duration.

The trace file is the API surface: the backend streams it live as SSE
`trace` events and serves it with run detail (see the note the deep
agent session sends the API owner — shapes below are that contract).

Event shapes (one JSON object per line, `ts` epoch seconds always):
  run_start  {script, thread, model}
  model_request  {n_messages}
  model_response {usage: {input_tokens, output_tokens, total_tokens},
                  input_tokens, cost_usd (null when unreported)}
  tool_call  {name, call_hash, args_preview, duration_s, ok,
              result_preview|error}
  termination {reason}  reason in TERMINATION_REASONS
  run_end    {duration_s, ok}

`call_hash` is a stable hash of the tool name and whitespace-normalized
args, so identical repeated calls share one hash. `termination` is
recorded explicitly (a successful run_end records `success` itself).

Previews are capped at 2000 chars; run_check also retains 4000 chars
of bounded head/tail evidence. The recorder never raises:
observability must not break runs.
"""

import hashlib
import json
import os
import re
import secrets
import time

from backend.shared import ledger

PREVIEW_LIMIT = 2000
TERMINATION_REASONS = (
    "gave_up",
    "loop_detected",
    "budget_cost",
    "budget_calls",
    "infra_error",
    "success",
)
_COST_MARKERS = ("budget", "402", "spend limit", "insufficient credit")
_THREAD_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def classify_termination(error):
    """Map a run-ending exception to a TERMINATION_REASONS value.

    Call caps and tool-call budgets are budget_calls; repeated-action
    blocks are loop_detected; spend refusals are budget_cost; anything
    else (timeouts, 429/5xx, resets, crashes) is infra_error.
    """
    names = {c.__name__ for c in type(error).__mro__}
    msg = str(error).lower()
    if "ModelCallLimitExceededError" in names or "tool budget exhausted" in msg:
        return "budget_calls"
    if "BudgetExceeded" in " ".join(names) or msg.startswith("blocked:"):
        if "repeated" in msg or "dithering" in msg:
            return "loop_detected"
        if "names no usable" in msg:
            return "gave_up"
        return "budget_calls"
    if any(m in msg for m in _COST_MARKERS):
        return "budget_cost"
    return "infra_error"


def _preview(value):
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, default=str, ensure_ascii=False)
    )
    if len(text) > PREVIEW_LIMIT:
        return text[:PREVIEW_LIMIT] + "…[truncated]"
    return text


def call_hash(name, args):
    """Stable short hash of a tool call; whitespace in strings is normalized."""

    def norm(value):
        if isinstance(value, str):
            return " ".join(value.split())
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        return value

    text = json.dumps(
        [name, norm(args)], sort_keys=True, default=str, ensure_ascii=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def default_trace_path(script, thread_id):
    """<state>/traces/<script>_<thread>_<ts>_<pid>_<rand>.jsonl (dir created).

    The pid+random suffix gives every run its own trace file: two runs
    of the same script+thread starting within the same second must never
    share (and interleave) one file. The timestamp keeps its position so
    names stay chronologically sortable.
    """
    from backend.shared import ledger

    safe = _THREAD_SAFE.sub("_", thread_id or "run")[:64] or "run"
    base = os.path.join(ledger.state_dir(), "traces")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        pass
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    uniq = f"{os.getpid()}-{secrets.token_hex(4)}"
    return os.path.join(base, f"{script}_{safe}_{stamp}_{uniq}.jsonl")


class TraceRecorder:
    """Append-only JSONL trace writer. Never raises."""

    def __init__(self, path):
        self.path = path
        self._fh = None
        self.started = time.time()
        self._usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self._calls = []
        self._reported_cost = 0.0
        self._cost_complete = True
        self._responses = 0
        self._twt_trace_ids = set()
        self._terminated = False
        try:
            self._fh = open(path, "a", encoding="utf-8")
        except OSError:
            self._fh = None

    def event(self, type, **fields):
        if self._fh is None:
            return
        try:
            fields["ts"] = round(time.time(), 3)
            fields["type"] = type
            self._fh.write(json.dumps(fields, ensure_ascii=False) + "\n")
            self._fh.flush()
        except (OSError, ValueError, TypeError):
            pass

    def run_start(self, script, thread, model):
        self.event("run_start", script=script, thread=thread, model=model)

    def model_request(self, n_messages):
        self.event("model_request", n_messages=n_messages)

    def model_response(self, usage):
        self._twt_trace_ids.update((usage or {}).get("twt_trace_ids", []))
        self._responses += 1
        if (usage or {}).get("reported_cost_usd") is None:
            self._cost_complete = False
        else:
            self._reported_cost += usage["reported_cost_usd"]
        snap = {
            k: (usage or {}).get(k, 0) or 0
            for k in ("input_tokens", "output_tokens", "total_tokens")
        }
        self.event(
            "model_response",
            usage=snap,
            input_tokens=snap["input_tokens"],
            cost_usd=(usage or {}).get("reported_cost_usd"),
        )
        for k in self._usage:
            self._usage[k] += snap[k]

    def tool_call(self, name, args, duration_s, ok, result=None, error=None):
        payload = {
            "name": name,
            "call_hash": call_hash(name, args or ""),
            "args_preview": _preview(args or ""),
            "duration_s": round(duration_s, 3),
            "ok": ok,
        }
        if ok:
            payload["result_preview"] = _preview(result or "")
            if name == "run_check" and isinstance(result, str):
                marker = "\n[check output truncated]\n"
                keep = (2 * PREVIEW_LIMIT - len(marker)) // 2
                truncated = len(result) > 2 * PREVIEW_LIMIT
                payload["check_output"] = (
                    result[:keep] + marker + result[-keep:] if truncated else result
                )
                payload["check_output_truncated"] = truncated
        else:
            payload["error"] = str(error or "")[:500]
        self.event("tool_call", **payload)
        self._calls.append({"name": name, "args": str(args or "")[:300]})

    @property
    def model_calls(self):
        """Model responses recorded so far (what the call limit counts)."""
        return self._responses

    def totals(self):
        """Aggregated (usage, tool_calls) for ledger rows. Never raises."""
        try:
            usage = dict(self._usage)
            if self._twt_trace_ids:
                usage["twt_trace_ids"] = sorted(self._twt_trace_ids)
            if self._cost_complete and self._responses:
                usage["reported_cost_usd"] = self._reported_cost
            return usage, list(self._calls)
        except Exception:  # noqa: BLE001 - totals must never break a run
            return ({"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, [])

    @property
    def terminated(self):
        return self._terminated

    def termination(self, reason):
        """Record why the run ended; reason must be in TERMINATION_REASONS."""
        if reason not in TERMINATION_REASONS:
            raise ValueError(f"unknown termination reason: {reason!r}")
        self._terminated = True
        self.event("termination", reason=reason)

    def run_end(self, ok=True):
        if ok and not self._terminated:
            self.termination("success")
        self.event("run_end", duration_s=round(time.time() - self.started, 3), ok=ok)

    def close(self):
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass


def trace_middleware(recorder):
    """[before_model, after_model, wrap_tool_call] bound to recorder.

    Venv-only (imports langchain hooks). Pass the list as
    `middleware=` to create_agent / create_deep_agent.
    """
    from langchain.agents.middleware import (  # noqa: E402
        after_model,
        before_model,
        wrap_tool_call,
    )

    @before_model
    def _log_request(state, runtime):
        try:
            n = len((state or {}).get("messages", []) or [])
        except TypeError:
            n = 0
        recorder.model_request(n)

    @after_model
    def _log_response(state, runtime):
        usage = None
        try:
            messages = (state or {}).get("messages", []) or []
            if messages:
                usage = ledger._usage_total([messages[-1]])
        except (TypeError, IndexError, AttributeError):
            usage = None
        recorder.model_response(usage or {})

    @wrap_tool_call
    def _log_tool(request, handler):
        call = getattr(request, "tool_call", None) or {}
        if not isinstance(call, dict):
            try:
                call = dict(call)
            except TypeError:
                call = {}
        name = call.get("name", "?")
        args = call.get("args", "")
        t0 = time.time()
        try:
            result = handler(request)
        except Exception as exc:
            recorder.tool_call(name, args, time.time() - t0, False, error=exc)
            raise
        content = getattr(result, "content", result)
        recorder.tool_call(name, args, time.time() - t0, True, result=content)
        return result

    return [_log_request, _log_response, _log_tool]
