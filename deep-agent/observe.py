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
  model_response {usage: {input_tokens, output_tokens, total_tokens}}
  tool_call  {name, args_preview, duration_s, ok, result_preview|error}
  run_end    {duration_s, ok}

Previews are capped at 2000 chars. The recorder never raises:
observability must not break runs.
"""

import json
import os
import re
import time

PREVIEW_LIMIT = 2000
_THREAD_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _preview(value):
    text = value if isinstance(value, str) else json.dumps(
        value, default=str, ensure_ascii=False)
    if len(text) > PREVIEW_LIMIT:
        return text[:PREVIEW_LIMIT] + "…[truncated]"
    return text


def default_trace_path(script, thread_id):
    """deep-agent/traces/<script>_<thread>_<ts>.jsonl (dir created)."""
    safe = _THREAD_SAFE.sub("_", thread_id or "run")[:64] or "run"
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "traces")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        pass
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return os.path.join(base, f"{script}_{safe}_{stamp}.jsonl")


class TraceRecorder:
    """Append-only JSONL trace writer. Never raises."""

    def __init__(self, path):
        self.path = path
        self._fh = None
        self.started = time.time()
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
        self.event("model_response", usage={
            k: (usage or {}).get(k, 0) or 0
            for k in ("input_tokens", "output_tokens", "total_tokens")})

    def tool_call(self, name, args, duration_s, ok, result=None,
                  error=None):
        payload = {"name": name, "args_preview": _preview(args or ""),
                   "duration_s": round(duration_s, 3), "ok": ok}
        if ok:
            payload["result_preview"] = _preview(result or "")
        else:
            payload["error"] = str(error or "")[:500]
        self.event("tool_call", **payload)

    def run_end(self, ok=True):
        self.event("run_end", duration_s=round(time.time() - self.started, 3),
                   ok=ok)

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
                usage = getattr(messages[-1], "usage_metadata", None)
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
            recorder.tool_call(name, args, time.time() - t0, False,
                               error=exc)
            raise
        content = getattr(result, "content", result)
        recorder.tool_call(name, args, time.time() - t0, True,
                           result=content)
        return result

    return [_log_request, _log_response, _log_tool]
