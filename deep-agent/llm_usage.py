"""Per-call ledger and trace accounting for tool-using agents."""

import time

from backend.shared import ledger
import observe


def usage_callback(spec, thread_id, *, script, stage, phase):
    from langchain_core.callbacks import BaseCallbackHandler

    class Usage(BaseCallbackHandler):
        def __init__(self):
            self.active = {}
            self.messages = []

        def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
            recorder = observe.TraceRecorder(
                observe.default_trace_path(script.removesuffix(".py"), thread_id)
            )
            recorder.run_start(script, thread_id, spec["model"])
            recorder.model_request(len(messages[0]) if messages else 0)
            self.active[run_id] = (time.time(), recorder)

        def finish(self, run_id, message=None, error=None):
            active = self.active.pop(run_id, None)
            if active is None:
                return
            started, recorder = active
            messages = [message] if message is not None else []
            self.messages.extend(messages)
            usage = ledger._usage_total(messages)
            if error is not None:
                usage = ledger.error_usage(error)
            known = bool(getattr(message, "usage_metadata", None)) or bool(
                (getattr(message, "response_metadata", None) or {}).get(
                    "prompt_eval_count"
                )
            )
            recorder.model_response(usage)
            recorder.event(
                "spec_response" if stage == "spec" else "llm_response",
                phase=phase,
                tool_calls=getattr(message, "tool_calls", []) or [],
                metadata={
                    key: value
                    for key, value in (
                        getattr(message, "response_metadata", {}) or {}
                    ).items()
                    if key
                    in (
                        "token_usage",
                        "prompt_eval_count",
                        "eval_count",
                        "finish_reason",
                        "model_name",
                    )
                },
            )
            recorder.run_end(ok=error is None)
            recorder.close()
            ledger.record(
                script,
                spec["model"],
                thread_id,
                messages,
                started,
                usage=usage,
                provider=spec.get("provider"),
                extra={
                    "stage": stage,
                    "phase": phase,
                    "llm_call": usage.get("not_dispatched") is not True,
                    "usage_missing": not known,
                    "trace_path": recorder.path,
                    **({"error": type(error).__name__} if error is not None else {}),
                },
            )

        def on_llm_end(self, response, *, run_id, **kwargs):
            self.finish(run_id, response.generations[0][0].message)

        def on_llm_error(self, error, *, run_id, **kwargs):
            self.finish(run_id, error=error)

        def close(self, error):
            for run_id in list(self.active):
                self.finish(run_id, error=error)

    return Usage()
