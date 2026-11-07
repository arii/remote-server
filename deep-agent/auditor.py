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

    def build_graph(self):
        """Compile the audit loop (needs langgraph)."""
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import Command, interrupt
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
            model_label = ((outer.last_model_spec or {}).get("model")
                           or os.environ.get("S5_AUDIT_MODEL", "qwen3:8b"))
            ledger.record(
                "auditor.py", model_label,
                outer.thread_id, [], time.time(),
                extra={"valid": state.get("valid"),
                       "worktree_root": outer.worktree_root,
                       "decisions": lines,
                       "verdict": verdict,
                       "gate": state.get("gate", {})})
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
