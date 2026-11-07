"""AuditorGraph: gated audit loop (deterministic gate + LLM + HITL).

hard_deterministic_gate -> ux_audit -> human_review -> persist -> END
Gate failures fail closed immediately (zero LLM tokens). Approved
audits persist the report via decisions.py + ledger.py.

Only stdlib at module top; langgraph/langchain lazy (deep-agent venv).
"""
import json
import os
import subprocess
import sys
import time

import skills_registry

MAX_AUDIT_LOOPS_DEFAULT = 3

# Ask voice for the eval_ask gate (issue #1139): the pre-#1131 ask CLI
# system prompt, recovered verbatim. The PROPOSE protocol shapes below
# are what eval_ask's propose-* cases anchor on; the td shapes stay
# single-sourced from backend.exec.engine (imported lazily at prompt
# build time, never mirrored — see tests/test_eval_ask.py).
ASK_SYSTEM = (
    "You are helping Ari from his phone. Be terse, no filler. "
    "Investigate read-only with your tools; never propose running "
    "anything except via the PROPOSE protocol below.\n"
    "If the instruction asks for a write action, investigate first, "
    "then propose the exact change and STOP. Allowed proposal kinds: "
    "creating a GitHub issue, running one allowlisted td command, or "
    "dispatching implementation to a worktree session.\n"
    "Emit a proposal ONLY in exactly this form, on its own lines:\n"
    "PROPOSE issue\nTITLE: <one line>\nBODY:\n<markdown>\nEND PROPOSE\n"
    "or\nPROPOSE td\nCOMMAND: <exact td command>\nEND PROPOSE\n"
    "or\nPROPOSE dispatch\nREPO: <owner/name>\nBRANCH: <base branch>\n"
    "TASK:\n<implementation brief>\nEND PROPOSE\n"
    "A PROPOSE td COMMAND must be one of the allowed shapes "
    "verbatim, changing only N to digits — never invent td "
    "subcommands or flags.\n"
    "Allowed td commands (exact shapes):\n"
    "{allowed_td}\n"
    "If no write is needed, just answer — no PROPOSE block. "
    "Always invoke tools through the function-calling interface. "
    "Never write a tool call out as JSON text. "
    "Keep replies readable on a phone: short paragraphs or a short "
    "list, no wide tables."
)

# Same tool set the deleted ask CLI wired (issue #1139): the eval_ask
# cases anchor on grep_repo/read_repo_file/pr_state behavior.
ASK_TOOL_NAMES = (
    "search_docs", "read_repo_file", "grep_repo", "pr_state",
    "run_repo_command", "fetch_url", "search_web", "search_turns",
    "run_ux_capture", "diff_versions", "ask_diff", "lighthouse_read",
)


def _message_tool_calls(message):
    """Tool-call dicts on one agent message (object or plain dict)."""
    calls = (message.get("tool_calls") if isinstance(message, dict)
             else getattr(message, "tool_calls", None)) or []
    return [c for c in calls if isinstance(c, dict)]


def _message_text(message):
    """Readable text of one agent message (object or plain dict)."""
    content = (message.get("content") if isinstance(message, dict)
               else getattr(message, "content", ""))
    if isinstance(content, list):
        content = " ".join(
            str(p.get("text", "")) if isinstance(p, dict) else str(p)
            for p in content)
    return str(content or "")


def _is_tool_message(message):
    if isinstance(message, dict):
        return (message.get("type") == "tool"
                or "tool_call_id" in message)
    cls = message.__class__.__name__
    return cls == "ToolMessage" or hasattr(message, "tool_call_id")


def run_td_gate(command, worktree_root, timeout=300, runner=None,
                extra_args=None):
    """Run `td <command> --worktree <root> [--json]`; (ok, payload|error).

    Fail-closed: missing td, non-JSON, or status=error all return False.
    A parsed gate payload (even with passed=False / violations) returns
    True so the gate node can format the failure deterministically.
    """
    argv = [command, "--worktree", worktree_root]
    argv.extend(extra_args or [])
    if runner is not None:
        try:
            code, out = runner(argv)
        except FileNotFoundError:
            return False, "td CLI not found on PATH"
        except Exception as exc:
            return False, f"td {command} failed: {exc}"
    else:
        try:
            proc = subprocess.run(
                ["td", *argv, "--json"],
                capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return False, "td CLI not found on PATH"
        except Exception as exc:
            return False, f"td {command} failed: {exc}"
        code, out = proc.returncode, (proc.stdout or "").strip()
    try:
        payload = json.loads(out) if out else None
    except ValueError:
        return False, f"td {command} returned non-JSON: {out[:200]}"
    if not isinstance(payload, dict):
        return False, out[:300] or f"exit {code}"
    if code != 0 and payload.get("status") == "error":
        return False, payload.get("message") or out[:300]
    return True, payload


def run_td_audit_impact(worktree_root, top_k=25, timeout=300,
                        runner=None, preview_url=None):
    """td audit-impact --json; returns (ok, payload-or-error)."""
    extra = ["--top-k", str(top_k)]
    if preview_url:
        extra += ["--preview-url", preview_url]
    return run_td_gate("audit-impact", worktree_root, timeout=timeout,
                       runner=runner, extra_args=extra)


def collect_gate_violations(named_payloads):
    """Format [(name, payload)] gate failures as file:line error lines."""
    errors = []
    for name, payload in named_payloads:
        if not isinstance(payload, dict):
            errors.append(f"{name}: {payload}")
            continue
        for item in payload.get("violations") or []:
            if isinstance(item, dict):
                errors.append(
                    f"{name}:{item.get('file')}:{item.get('line')}: "
                    f"{item.get('rule')}: {item.get('message')}")
            else:
                errors.append(f"{name}: {item}")
        if not payload.get("violations") and payload.get("passed") is False:
            errors.append(f"{name}: gate reported passed=false")
    return errors


class AuditorGraph:
    """Gated audit loop. Inject audit_fn/td_runner for tests."""

    def __init__(self, worktree_root, audit_fn=None, td_runner=None,
                 max_loops=MAX_AUDIT_LOOPS_DEFAULT, thread_id=None,
                 spec_file=None, verification_commands=None,
                 preview_url=None):
        self.worktree_root = os.path.abspath(worktree_root)
        self.audit_fn = audit_fn
        self.td_runner = td_runner
        self.max_loops = max_loops
        self.thread_id = thread_id or f"audit-{int(time.time())}"
        self.spec_file = spec_file
        self.verification_commands = list(verification_commands or [])
        self.preview_url = preview_url
        self.last_model_spec = None

    def hard_gate(self):
        """Zero-LLM deterministic gate.

        Runs td audit-code + td audit-impact (+ td verify-scope when a
        spec file is configured, + verification commands). Returns
        (valid, gate_payload, errors).
        """
        checks = ["audit-code", "audit-impact"]
        payloads, errors = {}, []
        for name in checks:
            extra = None
            if name == "audit-impact" and self.preview_url:
                extra = ["--preview-url", self.preview_url]
            ok, payload = run_td_gate(name, self.worktree_root,
                                      runner=self.td_runner,
                                      extra_args=extra)
            if not ok:
                return False, payloads, [payload]
            payloads[name] = payload
            # Short-circuit: a failing gate blocks later gates outright.
            gate_errors = collect_gate_violations([(name, payload)])
            if gate_errors:
                return False, payloads, gate_errors
        if self.spec_file:
            ok, payload = run_td_gate(
                "verify-scope", self.worktree_root,
                runner=self.td_runner,
                extra_args=["--spec-file", self.spec_file])
            if not ok:
                return False, payloads, [payload]
            payloads["verify-scope"] = payload
            if payload.get("passed") is False:
                errors.append("verify-scope: gate reported passed=false")
        for argv in self.verification_commands:
            try:
                proc = subprocess.run(
                    list(argv), capture_output=True, text=True,
                    timeout=600, cwd=self.worktree_root)
            except Exception as exc:
                return False, payloads, [f"verify {' '.join(argv)}: {exc}"]
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "")[-500:]
                return False, payloads, [
                    f"verify {' '.join(argv)}: exit "
                    f"{proc.returncode}: {tail}"]
        failed = [(n, p) for n, p in payloads.items()
                  if isinstance(p, dict) and
                  (p.get("violations") or p.get("passed") is False)]
        errors.extend(collect_gate_violations(failed))
        if errors:
            return False, payloads, errors
        return True, payloads, []

    def build_verdict(self, state):
        """Structured audit verdict (status + evidence)."""
        approved = bool(state.get("approved"))
        gate = state.get("gate", {}) or {}
        impacted = []
        for payload in gate.values():
            if isinstance(payload, dict):
                impacted.extend(payload.get("impacted_files") or [])
        return {"status": "APPROVED" if approved else "CHANGES_REQUESTED",
                "valid": state.get("valid"),
                "impacted_files": sorted(set(impacted)),
                "findings": state.get("findings", ""),
                "errors": state.get("errors", [])}

    def audit_once(self, gate_payload):
        """LLM audit pass (injected in tests)."""
        if self.audit_fn is not None:
            return self.audit_fn(gate_payload)
        from langchain.agents import create_agent
        import model_router
        import ask_investigator
        override = os.environ.get("S5_AUDIT_MODEL", "").strip()
        spec = model_router.resolve_llm()
        if override:
            spec = dict(spec, model=override)
        model, _spec = model_router.build_chat_model(spec=spec)
        self.last_model_spec = dict(_spec)
        agent = create_agent(
            model, [ask_investigator.search_docs,
                    ask_investigator.read_repo_file,
                    ask_investigator.grep_repo])
        user_content = (
            "Audit these changes for rule violations and risky blast "
            "radius. Reply with numbered findings.\n\n"
            f"Gate payload:\n{json.dumps(gate_payload)[:4000]}")
        user_content = skills_registry.SkillsRegistry().inject_skills_for_node(
            "deep_semantic_auditor_node", user_content)
        result = agent.invoke(
            {"messages": [{"role": "user", "content": user_content}]})
        messages = result.get("messages", []) if isinstance(
            result, dict) else []
        texts = [getattr(m, "content", "") or "" for m in messages]
        return "\n\n".join(t for t in texts if t) or "no findings"

    def ask_once(self, prompt, repo="", agent_factory=None):
        """One ask through the AuditorGraph tool path (issue #1139).

        Reuses the audit agent wiring (create_agent + ask tool
        functions) with the ask voice instead of the audit voice, so
        the eval_ask gate keeps a live entry now that the ask CLI is
        gone. Returns {"reply", "tool_names", "messages",
        "model_label"}. agent_factory(tools, system_prompt) injects a
        fake agent for stdlib-only tests; production builds the
        create_agent loop (recursion_limit 20, same budget the old CLI
        measured for ~4 tool calls).
        """
        import ask_investigator
        import s5tools

        if not s5tools.valid_repo(repo):
            raise ValueError(f"bad repo: {repo!r}")
        ask_investigator._repo = repo
        ask_investigator._checkout = s5tools.checkout_for(repo)
        tools = [getattr(ask_investigator, name)
                 for name in ASK_TOOL_NAMES]
        from backend.exec.engine import (
            ALLOWED_TD_COMMANDS as allowed_td)
        system = (f"Repo: {repo}. " + ASK_SYSTEM.format(
            allowed_td="\n".join(allowed_td)))
        if agent_factory is None:
            import model_router

            spec = model_router.resolve_llm()
            override = os.environ.get("S5_AUDIT_MODEL", "").strip()
            if override:
                spec = dict(spec, model=override)
            think = os.environ.get("ASK_THINK", "") == "1"
            model, resolved = model_router.build_chat_model(
                spec=spec, think=think)
            model_label = (f"{resolved.get('provider')}:"
                           f"{resolved.get('model')}")
            from langchain.agents import create_agent

            agent = create_agent(model=model, tools=tools,
                                 system_prompt=system)
        else:
            model_label = "test:model"
            agent = agent_factory(tools, system)
        result = agent.invoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config={"recursion_limit": 20},
        )
        messages = result.get("messages", []) if isinstance(
            result, dict) else []
        tool_names = [c.get("name", "?")
                      for m in messages
                      for c in _message_tool_calls(m)]
        texts = [_message_text(m) for m in messages
                 if not _is_tool_message(m)]
        reply = next((t for t in reversed(texts) if t.strip()),
                     "")
        return {"reply": reply, "tool_names": tool_names,
                "messages": messages, "model_label": model_label}

    def build_graph(self):
        """Compile the audit loop (needs langgraph)."""
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import interrupt
        from typing_extensions import TypedDict

        if getattr(self, "_checkpointer", None) is None:
            self._checkpointer = InMemorySaver()
        outer = self

        class AuditState(TypedDict, total=False):
            findings: str
            gate: dict
            errors: list
            attempts: int
            approved: bool
            valid: bool
            persisted: bool
            verdict: dict

        def hard_gate_node(state):
            valid, payloads, errors = outer.hard_gate()
            return {"valid": valid, "gate": payloads, "errors": errors,
                    "attempts": state.get("attempts", 0) + 1}

        def ux_audit_node(state):
            if state.get("findings") and not state.get("errors"):
                return {}
            return {"findings": outer.audit_once(state.get("gate", {}))}

        def human_review_node(state):
            if state.get("approved"):
                return {}
            gate = state.get("gate", {}) or {}
            impacted = sorted({
                f for p in gate.values() if isinstance(p, dict)
                for f in (p.get("impacted_files") or [])})
            reply = interrupt({"findings": state.get("findings", ""),
                               "impacted": impacted,
                               "errors": state.get("errors", [])})
            decision = (reply or {}).get("decision", "approve")
            if decision == "approve":
                return {"approved": True}
            notes = (reply or {}).get("notes", "")
            errors = list(state.get("errors", []))
            if notes:
                errors.append(f"reviewer requested changes: {notes}")
            return {"approved": False, "errors": errors}

        def route_review(state):
            if state.get("approved"):
                return "persist"
            if state.get("attempts", 0) >= outer.max_loops:
                return END
            return "gate"

        def persist_node(state):
            import decisions
            import ledger
            verdict = outer.build_verdict(dict(state))
            calls = [{"name": "td audit-code + td audit-impact",
                      "args": outer.worktree_root}]
            lines = decisions.build_decisions(
                calls, [f"valid={state.get('valid')}"],
                (state.get("findings", "") or "")[:500],
                reason="audit loop closed")
            model_spec = outer.last_model_spec or {}
            model_label = (model_spec.get("model")
                           or os.environ.get("S5_AUDIT_MODEL", "qwen3:8b"))
            ledger.record(
                "auditor.py", model_label,
                outer.thread_id, [], time.time(),
                extra={"valid": state.get("valid"),
                       "worktree_root": outer.worktree_root,
                       "decisions": lines,
                       "verdict": verdict,
                       "gate": state.get("gate", {})},
                provider=model_spec.get("provider"))
            return {"persisted": True, "verdict": verdict}

        builder = StateGraph(AuditState)
        builder.add_node("hard_gate", hard_gate_node)
        builder.add_node("ux_audit", ux_audit_node)
        builder.add_node("human_review", human_review_node)
        builder.add_node("persist", persist_node)
        builder.add_edge(START, "hard_gate")
        builder.add_conditional_edges(
            "hard_gate",
            lambda s: "ux_audit" if s.get("valid") else END,
            {"ux_audit": "ux_audit", END: END})
        builder.add_edge("ux_audit", "human_review")
        builder.add_conditional_edges(
            "human_review", route_review,
            {"persist": "persist", "gate": "hard_gate", END: END})
        builder.add_edge("persist", END)
        return builder.compile(checkpointer=self._checkpointer)

    def run(self, resume=None):
        """Invoke to completion (or interrupt); resume via Command."""
        from langgraph.types import Command
        graph = self.build_graph()
        config = {"configurable": {"thread_id": self.thread_id}}
        if resume is None:
            return graph.invoke({"errors": [], "attempts": 0,
                                 "valid": False, "approved": False},
                                config)
        return graph.invoke(Command(resume=resume), config)


def _short_line(content, limit=160):
    """First line of a tool result, capped for phone-sized tails."""
    line = str(content).strip().splitlines()[0] \
        if str(content).strip() else ""
    return line if len(line) <= limit else line[:limit] + "…"


def main(argv=None) -> int:
    """Ask driver: auditor.py owner/repo <b64-prompt> [thread].

    Eval entry for the AuditorGraph tool path (issue #1139). Prints
    the old ask print contract (banner, tool_calls_made, done,
    REPORT, FINAL:) so eval_ask's parsers keep working, and records
    one ledger row under script "auditor.py" for tool attribution.
    """
    import cli

    cli.bootstrap()
    import ledger
    import s5tools

    argv = sys.argv if argv is None else argv
    if len(argv) < 3 or not s5tools.valid_repo(argv[1]):
        return cli.usage_error(
            "usage: auditor.py owner/repo <b64-prompt> [thread]")
    repo = argv[1]
    if len(argv) > 3:
        thread, raw = argv[2], argv[3]
    else:
        thread = "test-ask-" + repo.replace("/", "-")
        raw = argv[2]
    prompt = cli.b64decode(raw)
    if prompt is None or not prompt.strip() or len(prompt) > 2000:
        return cli.usage_error("bad base64 prompt")
    started = time.time()
    print(f"Asking {repo} (auditor tool path)…", flush=True)
    try:
        out = AuditorGraph(worktree_root=os.getcwd()).ask_once(
            prompt, repo)
    except RuntimeError as exc:
        # Model unavailable (resolve_llm fail-closed, dead backend
        # mid-run): sorry on stdout + exit 1 with no ledger row is
        # the documented invalid shape (eval_lib.INVALID_RE), not an
        # agent failure.
        return cli.sorry(
            f"Sorry, no model is available right now: {exc}")
    except Exception as exc:
        try:
            ledger.record(
                "auditor.py", "unknown:model", thread,
                [], started,
                extra={"repo": repo, "mode": "ask", "prompt": prompt,
                       "error": str(exc)[:200]},
            )
        finally:
            raise
    messages = out["messages"]
    ledger.record(
        "auditor.py", out["model_label"], thread, messages, started,
        extra={"repo": repo, "mode": "ask", "prompt": prompt},
    )
    calls = sum(len(_message_tool_calls(m)) for m in messages)
    print(f"tool_calls_made: {calls}")
    elapsed = time.time() - started
    print(f"done in {elapsed:.1f}s")
    outcomes = [_short_line(_message_text(m))
                for m in messages if _is_tool_message(m)]
    outcomes = [line for line in outcomes if line]
    print("REPORT (authoritative, from tool results):")
    if outcomes:
        for line in outcomes:
            print(f"  {line}")
    else:
        print("  (no tools called)")
    print(f"FINAL: {out['reply']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
