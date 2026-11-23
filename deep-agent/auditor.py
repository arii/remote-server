"""AuditorGraph: gated audit loop (deterministic gate + LLM + HITL).

hard_deterministic_gate -> ux_audit -> human_review -> persist -> END
Gate failures fail closed immediately (zero LLM tokens). Approved
audits persist the report via decisions.py + ledger.py.

Only stdlib at module top; langgraph/langchain lazy (deep-agent venv).
"""

import copy
import json
import os
import re
import subprocess
import sys
import time

import skills_registry
import verdicts
import literal_checks
from backend.shared.s5tools import verification_env

MAX_AUDIT_LOOPS_DEFAULT = 2


def parse_review(value):
    if isinstance(value, verdicts.Verdict):
        is_verified = value.verdict in ("READY", "APPROVE")
        findings = [
            {
                "severity": (
                    "blocking" if value.verdict in ("NEEDS-WORK", "BLOCK") else "low"
                ),
                "description": f"{f.file}:{f.line}: {f.note}",
                "file": f.file,
                "line": f.line,
                "note": f.note,
            }
            for f in value.findings
        ]
        return {
            "verified": is_verified,
            "findings": findings,
            "verdict": value.verdict,
            "summary": value.summary,
        }
    if isinstance(value, dict):
        if "verdict" in value and "verified" not in value:
            ok, v = verdicts.parse_verdict(json.dumps(value))
            if ok:
                return parse_review(v)
        return value
    if isinstance(value, str):
        text = value.strip()
        ok, v = verdicts.parse_verdict(text)
        if ok:
            return parse_review(v)
        fence = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
        try:
            parsed = json.loads(fence.group(1).strip() if fence else text)
            if isinstance(parsed, dict):
                if "verdict" in parsed and "verified" not in parsed:
                    ok2, v2 = verdicts.parse_verdict(json.dumps(parsed))
                    if ok2:
                        return parse_review(v2)
                return parsed
        except ValueError:
            return {}
    return {}


def review_verified(value):
    review = parse_review(value)
    findings = review.get("findings")
    return (
        review.get("verified") is True
        and isinstance(findings, list)
        and all(
            isinstance(item, dict)
            and isinstance(item.get("severity"), str)
            and item.get("severity")
            in {"low", "medium", "high", "critical", "blocking"}
            and isinstance(item.get("description"), str)
            for item in findings
        )
    )


def _literal_failure_lines(failures):
    """Human-readable evidence lines for unsatisfied literal criteria."""
    lines = []
    for failure in failures or []:
        criterion = failure.get("criterion", {})
        occurrences = failure.get("occurrences") or []
        first = occurrences[0] if occurrences else {}
        where = f"{first.get('path')}:{first.get('line')}" if first.get("path") else ""
        line = f"unsatisfied literal acceptance criterion: {criterion.get('text', '')}"
        if where:
            line += f" (first of {failure.get('total', len(occurrences))} at {where})"
        lines.append(line)
    return lines


def blocking_findings(findings):
    if isinstance(findings, str):
        text = findings.strip()
        if text.startswith("```"):
            text = "\n".join(text.splitlines()[1:-1])
        try:
            findings = json.loads(text)
        except ValueError:
            return bool(
                re.search(
                    r"(?im)^\s*(?:[-*]|\d+[.)])?\s*[*\[]*(?:high|critical|blocking)\b(?!-level\b)"
                    r'|"severity"\s*:\s*"(?:high|critical|blocking)"',
                    text,
                )
            )
    if isinstance(findings, dict):
        if str(findings.get("severity", "")).strip().lower() in {
            "high",
            "critical",
            "blocking",
        }:
            return True
        return blocking_findings(findings.get("findings", []))
    if isinstance(findings, list):
        return any(blocking_findings(item) for item in findings)
    return False


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
    "search_docs",
    "read_repo_file",
    "grep_repo",
    "pr_state",
    "run_repo_command",
    "fetch_url",
    "search_web",
    "search_turns",
    "run_ux_capture",
    "diff_versions",
    "ask_diff",
    "lighthouse_read",
)


class ModelUnavailableError(RuntimeError):
    """Resolve/build-model failure (fail-closed, no model to ask).

    Caught by the ask driver for the sorry/invalid shape. Deliberately
    distinct from RuntimeError so agent/tool bugs never route here
    (issue #1157).
    """


def _message_tool_calls(message):
    """Tool-call dicts on one agent message (object or plain dict)."""
    calls = (
        message.get("tool_calls")
        if isinstance(message, dict)
        else getattr(message, "tool_calls", None)
    ) or []
    return [c for c in calls if isinstance(c, dict)]


def _message_text(message):
    """Readable text of one agent message (object or plain dict)."""
    content = (
        message.get("content")
        if isinstance(message, dict)
        else getattr(message, "content", "")
    )
    if isinstance(content, list):
        content = " ".join(
            str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content
        )
    return str(content or "")


def _is_tool_message(message):
    if isinstance(message, dict):
        return message.get("type") == "tool" or "tool_call_id" in message
    cls = message.__class__.__name__
    return cls == "ToolMessage" or hasattr(message, "tool_call_id")


def run_td_gate(command, worktree_root, timeout=300, runner=None, extra_args=None):
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
                ["td", *argv, "--json"], capture_output=True, text=True, timeout=timeout
            )
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
                    f"{item.get('rule')}: {item.get('message')}"
                )
            else:
                errors.append(f"{name}: {item}")
        if not payload.get("violations") and payload.get("passed") is False:
            errors.append(f"{name}: gate reported passed=false")
    return errors


class AuditorGraph:
    """Gated audit loop. Inject audit_fn/td_runner for tests."""

    def __init__(
        self,
        worktree_root,
        audit_fn=None,
        td_runner=None,
        max_loops=MAX_AUDIT_LOOPS_DEFAULT,
        thread_id=None,
        spec_file=None,
        verification_commands=None,
        preview_url=None,
        verification_evidence=None,
        issue_id=None,
    ):
        self.issue_id = issue_id
        self.worktree_root = os.path.abspath(worktree_root)
        self.audit_fn = audit_fn
        self.td_runner = td_runner
        self.max_loops = max_loops
        self.thread_id = thread_id or f"audit-{int(time.time())}"
        self.spec_file = spec_file
        self.verification_commands = list(verification_commands or [])
        self.verification_evidence = list(verification_evidence or [])
        self.preview_url = preview_url
        self.last_model_spec = None
        self.last_review_usage = None
        self.audit_truncated = False
        self.review_calls_metered = False

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
            ok, payload = run_td_gate(
                name, self.worktree_root, runner=self.td_runner, extra_args=extra
            )
            if not ok:
                return False, payloads, [payload]
            payloads[name] = payload
            # Short-circuit: a failing gate blocks later gates outright.
            gate_errors = collect_gate_violations([(name, payload)])
            if gate_errors:
                return False, payloads, gate_errors
        if self.spec_file:
            # The scope gate allows exactly this issue's generated spec
            # (docs/specs/issue-<N>.md) when it knows the issue.
            scope_args = ["--spec-file", self.spec_file]
            if self.issue_id:
                scope_args += ["--issue", str(self.issue_id)]
            ok, payload = run_td_gate(
                "verify-scope",
                self.worktree_root,
                runner=self.td_runner,
                extra_args=scope_args,
            )
            if not ok:
                return False, payloads, [payload]
            payloads["verify-scope"] = payload
            if payload.get("passed") is False:
                errors.append("verify-scope: gate reported passed=false")
        for argv in self.verification_commands:
            try:
                with verification_env(self.worktree_root) as env:
                    proc = subprocess.run(
                        list(argv),
                        capture_output=True,
                        text=True,
                        timeout=600,
                        cwd=self.worktree_root,
                        env=env,
                        check=False,
                    )
            except Exception as exc:
                return False, payloads, [f"verify {' '.join(argv)}: {exc}"]
            verification = payloads.setdefault(
                "verification", {"passed": True, "commands": []}
            )
            verification["commands"].append(
                {
                    "command": list(argv),
                    "exit": proc.returncode,
                    "output": ((proc.stdout or "") + (proc.stderr or ""))[-4000:],
                }
            )
            if proc.returncode != 0:
                verification["passed"] = False
                tail = (proc.stderr or proc.stdout or "")[-500:]
                return (
                    False,
                    payloads,
                    [f"verify {' '.join(argv)}: exit {proc.returncode}: {tail}"],
                )
        failed = [
            (n, p)
            for n, p in payloads.items()
            if isinstance(p, dict) and (p.get("violations") or p.get("passed") is False)
        ]
        errors.extend(collect_gate_violations(failed))
        if errors:
            return False, payloads, errors
        return True, payloads, []

    def build_verdict(self, state):
        """Structured audit verdict (status + evidence)."""
        verified = review_verified(state.get("findings", ""))
        # Literal criteria fail closed (issue #1375): a semantic or
        # human approval must not relax an explicit literal criterion
        # whose forbidden string survives in the worktree.
        literal = state.get("literal_checks") or []
        approved = (
            bool(state.get("approved"))
            and verified
            and not blocking_findings(state.get("findings", ""))
            and not literal
        )
        gate = state.get("gate", {}) or {}
        impacted = []
        for payload in gate.values():
            if isinstance(payload, dict):
                impacted.extend(payload.get("impacted_files") or [])
        return {
            "status": "APPROVED" if approved else "CHANGES_REQUESTED",
            "verified": verified,
            "valid": state.get("valid"),
            "impacted_files": sorted(set(impacted)),
            "findings": state.get("findings", ""),
            "errors": list(state.get("errors", [])) + _literal_failure_lines(literal),
            "literal_checks": literal,
        }

    def semantic_prompt(self, payload):
        user_content = (
            "Audit these changes for rule violations and risky blast "
            "radius against the supplied issue spec (Goal, Definition of Done and "
            "acceptance criteria) and actual diff.\n\n"
            + verdicts.verdict_prompt_hint()
            + "\n\n"
            "Set verdict to APPROVE or READY only after verifying the diff against the spec. "
            "Missing evidence, incomplete checks or uncertainty require NEEDS-WORK or BLOCK. "
            "Stale check evidence does not verify the final diff. "
            "Deterministic gates alone are not semantic verification.\n\n"
            f"Audit payload:\n{json.dumps(payload)}"
        )
        return skills_registry.SkillsRegistry().inject_skills_for_node(
            "deep_semantic_auditor_node", user_content
        )

    def bound_verification(self, payload):
        from backend.shared import model_router

        def fits():
            return (
                len(json.dumps({"content": self.semantic_prompt(payload)})) + 1024
                <= model_router.PIPELINE_HISTORY_CHARS
            )

        if fits():
            return payload
        records = []

        def collect(value):
            if isinstance(value, dict):
                if isinstance(value.get("output"), str):
                    records.append(
                        (value, value["output"], value.get("output_truncated", False))
                    )
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(payload["verification"])
        collect(payload["gate"].get("verification", {}))

        def truncate(limit):
            marker = "\n[check output truncated]\n"
            for record, output, was_truncated in records:
                cut = len(output) > limit
                if cut:
                    keep = max(0, limit - len(marker)) // 2
                    record["output"] = (
                        output[:keep] + marker + output[-keep:] if keep else ""
                    )
                else:
                    record["output"] = output
                if cut or was_truncated:
                    record["output_truncated"] = True
                else:
                    record.pop("output_truncated", None)

        truncate(0)
        if not fits():
            raise ValueError(
                "Semantic audit diff/spec exceed the input budget even without check output"
            )
        low, high = 0, max((len(output) for _, output, _ in records), default=0)
        while low < high:
            middle = (low + high + 1) // 2
            truncate(middle)
            if fits():
                low = middle
            else:
                high = middle - 1
        truncate(low)
        return payload

    def check_literal_criteria(self):
        """Deterministic zero-occurrence criteria scan (issue #1375).

        Returns [] when no spec file is configured, the spec names no
        literal criteria, or every criterion is satisfied. Each failure
        carries the criterion and its surviving occurrences so the
        semantic audit and the human review see the same evidence.
        """
        if not self.spec_file:
            return []
        spec_path = os.path.join(self.worktree_root, self.spec_file)
        try:
            with open(spec_path, encoding="utf-8") as handle:
                spec = handle.read()
        except OSError:
            return []
        criteria = literal_checks.find_literal_criteria(spec)
        if not criteria:
            return []
        return literal_checks.unsatisfied_criteria(self.worktree_root, criteria)

    def semantic_payload(self, gate_payload):
        if not self.spec_file:
            raise ValueError("Semantic audit requires an issue spec file")
        spec_path = os.path.join(self.worktree_root, self.spec_file)
        with open(spec_path, encoding="utf-8") as handle:
            spec = handle.read()
        paths = [
            ".",
            ":(exclude,glob)**/.venv",
            ":(exclude,glob)**/.venv/**",
            ":(exclude,glob)**/node_modules",
            ":(exclude,glob)**/node_modules/**",
        ]
        subprocess.run(
            [
                "git",
                "add",
                "--intent-to-add",
                "--all",
                "--",
                *paths,
            ],
            cwd=self.worktree_root,
            capture_output=True,
            text=True,
            check=True,
        )
        diff = subprocess.run(
            [
                "git",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "origin/main",
                "--",
                *paths,
            ],
            cwd=self.worktree_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        if not diff.strip():
            raise ValueError("Semantic audit has no diff to verify")
        return self.bound_verification(
            {
                "diff": diff,
                "spec": spec,
                "gate": copy.deepcopy(gate_payload),
                "verification": copy.deepcopy(self.verification_evidence),
                "literal_checks": self.check_literal_criteria(),
            }
        )

    def audit_once(self, gate_payload):
        """LLM audit pass (injected in tests)."""
        if self.audit_fn is not None:
            return self.audit_fn(gate_payload)
        from backend.shared import model_router
        import ask_investigator
        from issue_generator import InvestigationTruncated, investigation_tool_guard
        from langgraph.errors import GraphRecursionError
        from llm_usage import usage_callback

        self.audit_truncated = False
        limit = int(os.environ.get("S5_AUDIT_RECURSION_LIMIT", "25"))
        if limit <= 0:
            raise ValueError("S5_AUDIT_RECURSION_LIMIT must be a positive integer")
        payload = self.semantic_payload(gate_payload)

        override = os.environ.get("S5_AUDIT_MODEL", "").strip()
        spec = model_router.resolve_llm()
        if override:
            spec = dict(spec, model=override)
        model, _spec = model_router.build_chat_model(spec=spec, stage="audit")
        self.last_model_spec = dict(_spec)
        callback = usage_callback(
            self.last_model_spec,
            self.thread_id,
            script="auditor.py",
            stage="audit",
            phase="semantic",
        )
        self.review_calls_metered = True
        user_content = self.semantic_prompt(payload)

        # Single-shot tool-free audit bundle pass (issue #1400):
        # When diff is within bundle bounds (<= 12,000 chars) and tool loop not forced,
        # invoke the model directly in 1 turn without create_agent tool overhead.
        review = None
        diff_len = len(payload.get("diff", ""))
        use_single_shot = (
            diff_len <= 12000
            and not os.environ.get("S5_AUDIT_FORCE_TOOLS")
            and hasattr(model, "invoke")
        )
        if use_single_shot:
            from langchain_core.messages import HumanMessage

            try:
                reply = model.invoke(
                    [HumanMessage(content=user_content)],
                    config={"callbacks": [callback]},
                )
                if isinstance(reply, verdicts.Verdict):
                    review = parse_review(reply)
                else:
                    tool_calls = getattr(reply, "tool_calls", None)
                    if not tool_calls:
                        text = getattr(reply, "content", "")
                        review = parse_review(text)
                        if not review:
                            try:
                                structured_model, _ = (
                                    model_router.build_structured_chat_model(
                                        spec=self.last_model_spec,
                                        schema=verdicts.Verdict,
                                        stage="audit",
                                    )
                                )
                                v = structured_model.invoke(
                                    [HumanMessage(content=user_content)]
                                )
                                if isinstance(v, verdicts.Verdict):
                                    review = parse_review(v)
                            except Exception:
                                pass
            except BaseException as exc:
                callback.close(exc)
                raise

        # Fall back to tool-enabled ReAct agent only if single-shot was skipped or failed
        if review is None:
            from langchain.agents import create_agent

            evidence = []
            agent = create_agent(
                model,
                [
                    ask_investigator.search_docs,
                    ask_investigator.read_repo_file,
                    ask_investigator.grep_repo,
                ],
                middleware=[investigation_tool_guard(evidence)],
            )
            previous = ask_investigator._checkout
            ask_investigator._checkout = self.worktree_root
            error = None
            try:
                result = agent.invoke(
                    {"messages": [{"role": "user", "content": user_content}]},
                    config={"recursion_limit": limit, "callbacks": [callback]},
                )
            except (InvestigationTruncated, GraphRecursionError) as exc:
                self.audit_truncated = True
                text = next(
                    (
                        m.content
                        for m in reversed(callback.messages)
                        if not getattr(m, "tool_calls", []) and m.content
                    ),
                    "",
                )
                return json.dumps(
                    {
                        "status": "CHANGES_REQUESTED",
                        "verified": False,
                        "truncated": True,
                        "findings": [
                            {
                                "severity": "high",
                                "description": f"Semantic audit truncated ({type(exc).__name__}: {exc}); verification incomplete, human review required.",
                                "evidence": evidence,
                                "last_ai_text": text,
                            }
                        ],
                    }
                )
            except BaseException as exc:
                error = exc
                raise
            finally:
                callback.close(
                    error or RuntimeError("audit ended with an unfinished model call")
                )
                self.record_review_usage(callback.messages)
                ask_investigator._checkout = previous
            messages = result.get("messages", []) if isinstance(result, dict) else []
            self.record_review_usage(messages)
            final = messages[-1] if messages else None
            text = (
                getattr(final, "content", "")
                if getattr(final, "type", "") == "ai"
                and not getattr(final, "tool_calls", [])
                else ""
            )
            review = parse_review(text)
            if not review:
                try:
                    from langchain_core.messages import HumanMessage

                    structured_model, _ = model_router.build_structured_chat_model(
                        spec=self.last_model_spec,
                        schema=verdicts.Verdict,
                        stage="audit",
                    )
                    v = structured_model.invoke([HumanMessage(content=user_content)])
                    if isinstance(v, verdicts.Verdict):
                        review = parse_review(v)
                except Exception:
                    pass
        else:
            callback.close(None)
            self.record_review_usage(callback.messages)

        return json.dumps(
            {
                "verified": review_verified(review),
                "findings": review.get("findings")
                if isinstance(review.get("findings"), list)
                else [
                    {
                        "severity": "high",
                        "description": "Semantic audit returned no final AI verdict in the required JSON schema; human review required.",
                    }
                ],
            }
        )

    def record_review_usage(self, messages):
        """Keep aggregate usage as verdict metadata; calls are ledgered separately."""
        try:
            from backend.shared import ledger

            self.last_review_usage = ledger._usage_total(messages)
        except Exception:
            self.last_review_usage = None
        return self.last_review_usage

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
        from backend.shared import s5tools

        if not s5tools.valid_repo(repo):
            raise ValueError(f"bad repo: {repo!r}")
        # The ask tools read repo scope from module globals (the old
        # CLI's pattern); restore on the way out so concurrent or later
        # consumers never inherit eval scope (issue #1157).
        prev_repo = getattr(ask_investigator, "_repo", "")
        prev_checkout = getattr(ask_investigator, "_checkout", "")
        ask_investigator._repo = repo
        ask_investigator._checkout = s5tools.checkout_for(repo)
        try:
            tools = [getattr(ask_investigator, name) for name in ASK_TOOL_NAMES]
            from backend.exec.engine import ALLOWED_TD_COMMANDS as allowed_td

            system = f"Repo: {repo}. " + ASK_SYSTEM.format(
                allowed_td="\n".join(allowed_td)
            )
            if agent_factory is None:
                from backend.shared import model_router

                try:
                    spec = model_router.resolve_llm()
                except RuntimeError as exc:
                    raise ModelUnavailableError(str(exc)) from exc
                override = os.environ.get("S5_AUDIT_MODEL", "").strip()
                if override:
                    spec = dict(spec, model=override)
                think = os.environ.get("ASK_THINK", "") == "1"
                try:
                    model, resolved = model_router.build_chat_model(
                        spec=spec, think=think, stage="audit"
                    )
                except RuntimeError as exc:
                    raise ModelUnavailableError(str(exc)) from exc
                model_label = f"{resolved.get('provider')}:{resolved.get('model')}"
                from langchain.agents import create_agent

                agent = create_agent(model=model, tools=tools, system_prompt=system)
            else:
                model_label = "test:model"
                agent = agent_factory(tools, system)
            result = agent.invoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={"recursion_limit": 20},
            )
        finally:
            ask_investigator._repo = prev_repo
            ask_investigator._checkout = prev_checkout
        if not isinstance(result, dict) or not isinstance(result.get("messages"), list):
            # Flag, don't silently ledger an empty clean row: the
            # driver records this as the run error (issue #1157).
            raise ValueError(f"malformed agent result: {type(result).__name__}")
        messages = result["messages"]
        tool_names = [
            c.get("name", "?") for m in messages for c in _message_tool_calls(m)
        ]
        texts = [_message_text(m) for m in messages if not _is_tool_message(m)]
        reply = next((t for t in reversed(texts) if t.strip()), "")
        return {
            "reply": reply,
            "tool_names": tool_names,
            "messages": messages,
            "model_label": model_label,
        }

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
            literal_checks: list

        def hard_gate_node(state):
            valid, payloads, errors = outer.hard_gate()
            return {
                "valid": valid,
                "gate": payloads,
                "errors": errors,
                "attempts": state.get("attempts", 0) + 1,
                "literal_checks": outer.check_literal_criteria(),
            }

        def ux_audit_node(state):
            if state.get("findings") and not state.get("errors"):
                return {}
            return {"findings": outer.audit_once(state.get("gate", {}))}

        def human_review_node(state):
            if state.get("approved"):
                return {}
            gate = state.get("gate", {}) or {}
            impacted = sorted(
                {
                    f
                    for p in gate.values()
                    if isinstance(p, dict)
                    for f in (p.get("impacted_files") or [])
                }
            )
            literal = state.get("literal_checks") or []
            literal_lines = _literal_failure_lines(literal)
            reply = interrupt(
                {
                    "findings": state.get("findings", ""),
                    "impacted": impacted,
                    "errors": list(state.get("errors", [])) + literal_lines,
                }
            )
            decision = (reply or {}).get("decision", "approve")
            if (
                decision == "approve"
                and review_verified(state.get("findings"))
                and not literal
            ):
                return {"approved": True}
            notes = (reply or {}).get("notes", "")
            errors = list(state.get("errors", [])) + literal_lines
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
            from backend.shared import ledger

            verdict = outer.build_verdict(dict(state))
            calls = [
                {"name": "td audit-code + td audit-impact", "args": outer.worktree_root}
            ]
            lines = decisions.build_decisions(
                calls,
                [f"valid={state.get('valid')}"],
                (state.get("findings", "") or "")[:500],
                reason="audit loop closed",
            )
            model_spec = outer.last_model_spec or {}
            model_label = model_spec.get("model") or os.environ.get(
                "S5_AUDIT_MODEL", "qwen3:8b"
            )
            ledger.record(
                "auditor.py",
                model_label,
                outer.thread_id,
                [],
                time.time(),
                extra={
                    "valid": state.get("valid"),
                    "stage": "audit",
                    "phase": "summary",
                    "llm_call": False,
                    "worktree_root": outer.worktree_root,
                    "decisions": lines,
                    "verdict": verdict,
                    "gate": state.get("gate", {}),
                },
                usage=outer.last_review_usage,
                provider=model_spec.get("provider"),
            )
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
            {"ux_audit": "ux_audit", END: END},
        )
        builder.add_edge("ux_audit", "human_review")
        builder.add_conditional_edges(
            "human_review",
            route_review,
            {"persist": "persist", "gate": "hard_gate", END: END},
        )
        builder.add_edge("persist", END)
        return builder.compile(checkpointer=self._checkpointer)

    def run(self, resume=None):
        """Invoke to completion (or interrupt); resume via Command."""
        from langgraph.types import Command

        graph = self.build_graph()
        config = {"configurable": {"thread_id": self.thread_id}}
        if resume is None:
            return graph.invoke(
                {"errors": [], "attempts": 0, "valid": False, "approved": False}, config
            )
        return graph.invoke(Command(resume=resume), config)


def _short_line(content, limit=160):
    """First line of a tool result, capped for phone-sized tails."""
    line = str(content).strip().splitlines()[0] if str(content).strip() else ""
    return line if len(line) <= limit else line[:limit] + "…"


def main(argv=None) -> int:
    """Ask driver: auditor.py owner/repo <b64-prompt> [thread].

    Eval entry for the AuditorGraph tool path (issue #1139). Prints
    the old ask print contract (banner, tool_calls_made, done,
    REPORT, FINAL:) so eval_ask's parsers keep working, and records
    one ledger row under script "auditor.py" for tool attribution.
    """
    from backend.shared import cli

    cli.bootstrap()
    from backend.shared import ledger
    from backend.shared import s5tools

    argv = sys.argv if argv is None else argv
    if len(argv) < 3 or not s5tools.valid_repo(argv[1]):
        return cli.usage_error("usage: auditor.py owner/repo <b64-prompt> [thread]")
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
        out = AuditorGraph(worktree_root=os.getcwd()).ask_once(prompt, repo)
    except ModelUnavailableError as exc:
        # No model to ask (resolve/build fail-closed): sorry on
        # stdout + exit 1 with no ledger row is the documented
        # invalid shape (eval_lib.INVALID_RE), not an agent failure.
        return cli.sorry(f"Sorry, no model is available right now: {exc}")
    except RuntimeError as exc:
        # Tool-impl / create_agent failure: a real agent failure, not
        # infra. Clean error line (no traceback) + error row, so the
        # gate verdicts FAIL instead of masking as invalid (#1157).
        ledger.record(
            "auditor.py",
            "unknown:model",
            thread,
            [],
            started,
            extra={
                "repo": repo,
                "mode": "ask",
                "prompt": prompt,
                "error": str(exc)[:200],
            },
        )
        print(f"error: {exc}", flush=True)
        return 1
    except Exception as exc:
        try:
            ledger.record(
                "auditor.py",
                "unknown:model",
                thread,
                [],
                started,
                extra={
                    "repo": repo,
                    "mode": "ask",
                    "prompt": prompt,
                    "error": str(exc)[:200],
                },
            )
        finally:
            raise
    messages = out["messages"]
    ledger.record(
        "auditor.py",
        out["model_label"],
        thread,
        messages,
        started,
        extra={"repo": repo, "mode": "ask", "prompt": prompt},
    )
    calls = sum(len(_message_tool_calls(m)) for m in messages)
    print(f"tool_calls_made: {calls}")
    elapsed = time.time() - started
    print(f"done in {elapsed:.1f}s")
    outcomes = [_short_line(_message_text(m)) for m in messages if _is_tool_message(m)]
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
