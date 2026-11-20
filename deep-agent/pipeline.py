"""Pipeline: 4-stage autonomous loop (init -> spec -> code -> audit).

Stage 1 runs ``td init``; Stage 2 drives ``IssueGeneratorGraph`` to a
validated spec; Stage 3 drives ``InitCoderGraph`` in an isolated
worktree; Stage 4 drives ``AuditorGraph``. Before any auditor turn, a
deterministic pre-audit gate (AST/syntax, lint, diff-scoped pytest)
sends broken patches straight back to the coder without spending an
auditor LLM call (issue #1403). Auditor rejections
(``CHANGES_REQUESTED``) and deterministic blocks route the repair plan
back into a repair ``InitCoderGraph`` bound to the SAME worktree (up
to ``max_audit_cycles``); total repair passes are hard-capped by
``max_repair_cycles`` and model-tier escalation is gated on failure
class and attempt number; ``APPROVED`` optionally submits via
``td submit-pr`` and then removes the worktree.

Stdlib-only at import; langgraph loads lazily inside the graph
modules. All model/tool seams are injectable for deterministic evals.
"""

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager

import twt_receipts

UNVERIFIED_CODER = "pipeline coder did not pass verification"
MAX_AUDIT_CYCLES_DEFAULT = 2
MAX_REPAIR_CYCLES_DEFAULT = 2
MAX_ESCALATIONS_DEFAULT = 1
DRIVE_LIMIT_DEFAULT = 25
PREAUDIT_SUMMARY_LIMIT = 2000

APPROVED = "APPROVED"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
BLOCKED = "BLOCKED_DETERMINISTIC"
REPAIR_BUDGET_EXHAUSTED = "REPAIR_BUDGET_EXHAUSTED"

# Failure classes (issue #1403): pre-audit syntax/lint blocks are
# mechanical, diff-scoped pytest blocks are test failures, and auditor
# CHANGES_REQUESTED verdicts are semantic.
MECHANICAL = "mechanical"
TEST_FAILURE = "test_failure"
AUDIT_SEMANTIC = "audit_semantic"

FAST_TIER = "fast"
STRONG_TIER = "strong"
STRONG_TIER_MODEL_DEFAULT = "qwen3:14b"

AGENT_PAUSED = "PAUSED_BY_USER"


def control_state_path():
    """Co-browsing state file shared with the backend agent_control
    router (AGENT_CONTROL_STATE_FILE overrides the default)."""
    return os.environ.get("AGENT_CONTROL_STATE_FILE", "/tmp/agent_control_state.json")


def read_pause_state(path=None):
    """Return (paused, requires_refresh); missing/corrupt files mean
    "not paused" so a deleted or half-written state never wedges a run."""
    try:
        with open(path or control_state_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return False, False
    if not isinstance(data, dict):
        return False, False
    return (data.get("state") == AGENT_PAUSED, bool(data.get("requires_dom_refresh")))


def clear_refresh_flag(path=None):
    """Best-effort reset of requires_dom_refresh after resumption."""
    target = path or control_state_path()
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not data.get("requires_dom_refresh"):
            return
        data["requires_dom_refresh"] = False
        tmp = f"{target}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, target)
    except Exception:
        pass


def run_td(argv, cwd=None, timeout=300):
    """Run the td CLI; returns (returncode, stdout). Never raises."""
    try:
        proc = subprocess.run(
            ["td", *argv], capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
    except FileNotFoundError:
        return 127, "td CLI not found on PATH"
    except Exception as exc:
        return 1, f"td {' '.join(argv)} failed: {exc}"
    stdout = (proc.stdout or "").strip()
    if proc.returncode:
        try:
            payload = json.loads(stdout)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            payload["_td_diagnostics"] = {
                "exit_code": proc.returncode,
                "stderr": (proc.stderr or "").strip(),
            }
            return proc.returncode, json.dumps(payload)
        stderr = (proc.stderr or "").strip()
        return proc.returncode, (
            f"td exited with code {proc.returncode}\nstdout: {stdout}\nstderr: {stderr}"
        )
    return proc.returncode, stdout


def parse_json_output(out):
    """Parse CLI stdout; returns (payload-or-None, error-or-None)."""
    try:
        payload = json.loads(out) if out else None
    except ValueError:
        return None, f"non-JSON output: {out[:200]}"
    if not isinstance(payload, dict):
        return None, f"non-object output: {out[:200]}"
    return payload, None


def drive_to_settled(first, resume_fn, policy, limit=DRIVE_LIMIT_DEFAULT):
    """Drive a graph through interrupts until settled or limit.

    first() starts the run; policy(interrupts, state) builds each
    resume payload; resume_fn(resume) continues. Returns the final
    state, or the last interrupt state when the limit is hit (with
    ``drive_exhausted=True`` set on a copy).
    """
    state = first()
    for _ in range(limit):
        if not isinstance(state, dict):
            return state
        interrupts = state.get("__interrupt__")
        if not interrupts:
            return state
        state = resume_fn(policy(interrupts, state))
    if isinstance(state, dict):
        state = dict(state)
        state["drive_exhausted"] = True
    return state


def final_interrupt(interrupts):
    """Newest interrupt payload from a settled-on-interrupt state."""
    if not interrupts:
        return {}
    last = interrupts[-1]
    return (
        last.get("value", {}) if isinstance(last, dict) else getattr(last, "value", {})
    )


def prepare_worktree_for_submit(
    repo_root, worktree_root, base_ref="origin/main", log_fn=None
):
    """Prepare worktree for PR submission (#1394).

    1. Rebases the worktree cleanly onto origin/main (with --autostash and
       aborting on conflict so worktree is never left broken).
    2. Atomically mirrors .agent_tool_receipt.json from repo root into worktree
       root if present so td agent submit-pr finds it without failing
       mandatory tool receipts check.
    """
    import shutil

    def _log(msg):
        if log_fn:
            log_fn(msg)

    # 1. Fetch origin/main and rebase worktree branch
    try:
        subprocess.run(
            ["git", "-C", worktree_root, "fetch", "origin", "main"],
            capture_output=True,
            text=True,
            check=False,
        )
        rebase = subprocess.run(
            ["git", "-C", worktree_root, "rebase", "--autostash", base_ref],
            capture_output=True,
            text=True,
            check=False,
        )
        if rebase.returncode != 0:
            subprocess.run(
                ["git", "-C", worktree_root, "rebase", "--abort"],
                capture_output=True,
                text=True,
                check=False,
            )
            _log(
                f"warning: auto-rebase onto {base_ref} failed: {rebase.stderr.strip()}"
            )
    except Exception as exc:
        _log(f"warning: auto-rebase error: {exc}")

    # 2. Mirror .agent_tool_receipt.json from repo root into worktree root
    try:
        repo_receipt = os.path.join(repo_root, ".agent_tool_receipt.json")
        worktree_receipt = os.path.join(worktree_root, ".agent_tool_receipt.json")
        if os.path.isfile(repo_receipt) and os.path.abspath(
            repo_receipt
        ) != os.path.abspath(worktree_receipt):
            tmp_receipt = worktree_receipt + ".tmp"
            shutil.copy2(repo_receipt, tmp_receipt)
            os.replace(tmp_receipt, worktree_receipt)
            _log("mirrored .agent_tool_receipt.json to worktree root")
    except Exception as exc:
        _log(f"warning: receipt mirror error: {exc}")

    return True


def choose_model_tier(
    attempt, failure_class, escalations_used, max_escalations=MAX_ESCALATIONS_DEFAULT
):
    """Failure-class-aware tier gate (issue #1403).

    First-pass attempts and mechanical failures always stay on the
    fast tier; a strong-tier escalation is allowed only from attempt 2
    onward for test_failure / audit_semantic blocks, capped at
    ``max_escalations`` per issue.
    """
    if (
        attempt >= 2
        and failure_class in (TEST_FAILURE, AUDIT_SEMANTIC)
        and escalations_used < max_escalations
    ):
        return STRONG_TIER
    return FAST_TIER


def _head_tail(text, limit=PREAUDIT_SUMMARY_LIMIT):
    """Head/tail truncate an error summary for coder repair notes."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return f"{text[:head]}\n[{len(text) - limit} chars omitted]\n{text[-tail:]}"


def _run_module(cwd, module, *args, timeout=120):
    """``python -m module args``; returns (exit_code, combined_output).

    Exit code None means the module is not installed, so the caller
    skips that check instead of blocking on a missing tool. A timeout
    or launch failure reports as exit code 1 with the reason.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", module, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return 1, f"{module} timed out after {timeout}s"
    except Exception as exc:
        return 1, f"{module} failed to run: {exc}"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode and f"No module named {module}" in out:
        return None, out
    return proc.returncode, out


def changed_python_files(worktree_root):
    """Changed + untracked *.py files in the worktree (never raises)."""
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                worktree_root,
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    names = []
    for line in (proc.stdout or "").splitlines():
        path = line[3:] if len(line) > 3 else ""
        if " -> " in path:  # porcelain rename: "old -> new"
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if not path.endswith(".py") or "__pycache__" in path.split("/"):
            continue
        names.append(path)
    return sorted(dict.fromkeys(names))


def scoped_test_files(worktree_root, changed):
    """Diff-scoped pytest targets: each changed file's own test file."""
    targets = []
    for rel in changed:
        base = os.path.basename(rel)
        if base.startswith("test_"):
            candidates = [rel]
        else:
            stem = base[: -len(".py")]
            candidates = [
                os.path.join(os.path.dirname(rel), f"test_{stem}.py"),
                os.path.join("tests", f"test_{stem}.py"),
            ]
        for candidate in candidates:
            if (
                candidate
                and os.path.isfile(os.path.join(worktree_root, candidate))
                and candidate not in targets
            ):
                targets.append(candidate)
    return targets


def run_preaudit_gate(worktree_root, log_fn=None):
    """Deterministic pre-audit checks before any auditor LLM turn (#1403).

    AST/syntax (py_compile), ruff lint, and diff-scoped ``pytest -x``
    over the worktree's changed Python files. Returns
    ``(ok, failure_class, summary)``: syntax/lint blocks classify as
    ``mechanical`` and pytest blocks as ``test_failure``, with a
    head/tail-truncated error summary for the coder's repair notes.
    Missing tools skip their check; git/tooling errors never raise.
    """

    def _log(msg):
        if log_fn:
            log_fn(msg)

    changed = changed_python_files(worktree_root)
    if not changed:
        return True, "", ""
    for rel in changed:
        code, out = _run_module(
            worktree_root, "py_compile", os.path.join(worktree_root, rel), timeout=60
        )
        if code:
            return False, MECHANICAL, _head_tail(f"syntax: {rel}: {out}")
    code, out = _run_module(
        worktree_root, "ruff", "check", "--select", "F,E4,E7,E9", *changed
    )
    if code is None:
        _log("pre-audit: ruff not installed; lint check skipped")
    elif code:
        return False, MECHANICAL, _head_tail(f"lint: {out}")
    tests = scoped_test_files(worktree_root, changed)
    if tests:
        code, out = _run_module(
            worktree_root, "pytest", "-x", "-q", *tests, timeout=300
        )
        if code is None:
            _log("pre-audit: pytest not installed; test check skipped")
        elif code == 5:
            _log("pre-audit: scoped tests collected none; check skipped")
        elif code:
            return False, TEST_FAILURE, _head_tail(f"pytest: {' '.join(tests)}: {out}")
    return True, "", ""


class Pipeline:
    """Autonomous 4-stage loop. Inject fakes for deterministic tests."""

    def __init__(
        self,
        repo,
        intent,
        issue_id=None,
        answers=None,
        td_run=None,
        investigate_fn=None,
        plan_fn=None,
        implement_fn=None,
        audit_fn=None,
        review_fn=None,
        spec_model_fn=None,
        auto_approve_plan=True,
        submit_pr=False,
        max_audit_cycles=MAX_AUDIT_CYCLES_DEFAULT,
        max_repair_cycles=MAX_REPAIR_CYCLES_DEFAULT,
        max_escalations=MAX_ESCALATIONS_DEFAULT,
        strong_tier_model=None,
        preaudit_fn=None,
        thread_id=None,
        verification_commands=None,
        drive_limit=DRIVE_LIMIT_DEFAULT,
        preview_url=None,
        keep_worktree=False,
    ):
        self.repo = os.path.abspath(repo)
        self.intent = intent
        self.issue_id = issue_id
        self.answers = dict(answers or {})
        self.td_run = td_run or run_td
        self.investigate_fn = investigate_fn
        self.plan_fn = plan_fn
        self.implement_fn = implement_fn
        self.audit_fn = audit_fn
        self.review_fn = review_fn
        self.spec_model_fn = spec_model_fn
        self.auto_approve_plan = auto_approve_plan
        self.submit_pr = submit_pr
        self.keep_worktree = keep_worktree
        self.max_audit_cycles = max_audit_cycles
        # Repair budget + escalation policy (issue #1403).
        self.max_repair_cycles = max_repair_cycles
        self.max_escalations = max_escalations
        self.strong_tier_model = (
            strong_tier_model
            or os.environ.get("S5_STRONG_TIER_MODEL", "").strip()
            or STRONG_TIER_MODEL_DEFAULT
        )
        self.preaudit_fn = preaudit_fn
        self.escalations_used = 0
        self.thread_id = thread_id or f"pipeline-{int(time.time())}"
        self.verification_commands = list(verification_commands or [])
        self.verification_evidence = []
        self.drive_limit = drive_limit
        self.preview_url = preview_url
        self.events = []

    def log(self, stage, message):
        self.events.append({"stage": stage, "message": message})

    # -- stage runners ------------------------------------------------

    def stage_init(self):
        code, out = self.td_run(["init", "--repo-root", self.repo, "--json"], self.repo)
        payload, error = parse_json_output(out)
        if (
            code != 0
            or error
            or (payload or {}).get("status") not in ("ready", "success")
        ):
            reason = error or (payload or {}).get("message") or out[:300]
            return False, f"td init failed: {reason}"
        return True, payload

    def _spec_policy(self, interrupts, _state):
        asked = final_interrupt(interrupts).get("questions", [])
        if isinstance(asked, list):
            return {q: self.answers.get(q, "") for q in asked if isinstance(q, str)}
        return dict(self.answers)

    def stage_spec(self):
        import issue_generator

        gen = issue_generator.IssueGeneratorGraph(
            repo_root=self.repo,
            investigate_fn=self.investigate_fn,
            model_fn=self.spec_model_fn,
            td_runner=lambda args: self.td_run(list(args), self.repo),
            thread_id=f"{self.thread_id}-spec",
            event_fn=lambda reason: self.log(
                "investigation", f"investigation was truncated: {reason}"
            ),
        )
        state = drive_to_settled(
            lambda: gen.run(self.intent, answers=dict(self.answers)),
            lambda reply: gen.run(self.intent, resume=reply),
            self._spec_policy,
            limit=self.drive_limit,
        )
        if not isinstance(state, dict) or state.get("drive_exhausted"):
            return False, {}, "spec drive exhausted"
        if not state.get("valid"):
            return False, state, (state.get("errors") or ["invalid spec"])
        return (
            True,
            state,
            issue_generator.completed_spec(state, allowed_path=self.spec_path),
        )

    @property
    def spec_path(self):
        return f"docs/specs/issue-{self.issue_id or 0}.md"

    def _plan_policy(self, interrupts, _state):
        if self.auto_approve_plan:
            return {"decision": "approve"}
        payload = final_interrupt(interrupts)
        return {"decision": "approve", "plan": payload.get("plan", "")}

    def make_coder(self, brief, worktree_root=None, repair_notes=""):
        """Build an InitCoderGraph; repair cycles reuse worktree_root."""
        import coder
        import issue_generator

        spec_path = self.spec_path

        def implement(task, plan, root, errors):
            prompt = (
                f"{task}\n\nPlan:\n{plan}\n\n"
                f"Repair notes:\n{repair_notes}\n\nErrors:\n{errors}"
            )
            kwargs = {}
            if errors or repair_notes:
                kwargs["repair"] = True
                kwargs["repair_files"] = [
                    path
                    for path in dict.fromkeys(
                        [
                            *coder.files_named(plan),
                            *issue_generator.explicit_allowed_paths(brief),
                        ]
                    )
                    if os.path.normpath(path) != os.path.normpath(spec_path)
                ]
            result = coder.run_coder(
                root,
                prompt,
                coder.build_system(root, ""),
                f"{self.thread_id}-edit-{time.time_ns()}",
                coder._timeout(),
                **kwargs,
            )
            summary, usage, calls, started, label, trace, verified, _ = result
            self.verification_evidence = coder.trace_verification_evidence(trace)
            provider, _, model = label.partition(":")
            graph.last_model_spec = {"provider": provider, "model": model}
            import ledger

            ledger.record(
                "pipeline.py",
                label,
                f"{self.thread_id}-edit",
                [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": summary},
                ],
                started,
                extra={
                    "worktree": root,
                    "trace": trace,
                    "verified": verified,
                    **({} if verified else {"error": UNVERIFIED_CODER}),
                },
                usage=usage,
                calls=calls,
            )
            if not verified:
                raise RuntimeError(UNVERIFIED_CODER)
            return summary

        def plan(task, root, context):
            context = {**self.answers, **context}
            if repair_notes:
                context["repair_notes"] = repair_notes
            result = (self.plan_fn or coder._default_plan_fn)(task, root, context)
            result = dict(result)
            result["allowed_files"] = list(
                dict.fromkeys(
                    [
                        *result.get("allowed_files", []),
                        *issue_generator.explicit_allowed_paths(brief),
                    ]
                )
            )
            return result

        context = dict(self.answers)
        if repair_notes:
            context["repair_notes"] = repair_notes
        brief_with_rag = brief
        try:
            import retrieval

            rag_hits = retrieval.search_td(brief, self.repo, top_k=5)
            if (
                rag_hits
                and not rag_hits.startswith("no doc hits")
                and not rag_hits.startswith("doc search unavailable")
            ):
                brief_with_rag = (
                    f"{brief}\n\n### Grounded Code Context (RAG):\n{rag_hits}"
                )
        except Exception:
            brief_with_rag = brief
        graph = coder.InitCoderGraph(
            self.repo,
            self.issue_id or 0,
            brief_with_rag,
            plan_fn=plan,
            implement_fn=self.implement_fn or implement,
            td_runner=lambda args, cwd: self.td_run(list(args), cwd or self.repo),
            thread_id=f"{self.thread_id}-coder-{int(time.time())}",
            **({"max_fix_loops": 0, "max_replans": 0} if repair_notes else {}),
        )
        if worktree_root:
            # Same-worktree repair: skip provisioning, reuse the root
            # (coder.py is frozen by Issue 7 scope; this rebind is the
            # documented repair seam, covered by tests).
            graph.provision_worktree = lambda _root=worktree_root: (_root, None)
        provision = graph.provision_worktree

        def provision_with_spec():
            root, error = provision()
            if not error and not worktree_root:
                issue_generator.write_spec(os.path.join(root, spec_path), brief)
            return root, error

        graph.provision_worktree = provision_with_spec
        graph._pipeline_context = context
        return graph

    def stage_code(self, brief, worktree_root=None, repair_notes=""):
        graph = self.make_coder(brief, worktree_root, repair_notes)
        state = drive_to_settled(
            graph.run, graph.run, self._plan_policy, limit=self.drive_limit
        )
        if not isinstance(state, dict) or state.get("drive_exhausted"):
            return False, state, "coder drive exhausted"
        if state.get("fatal"):
            return False, state, state["fatal"]
        if not state.get("valid"):
            return False, state, (state.get("errors") or ["coder invalid"])
        return True, state, state.get("worktree_root", "") or worktree_root

    def _audit_policy(self, interrupts, state):
        import auditor

        if not auditor.review_verified(state.get("findings")):
            return {
                "decision": "reject",
                "notes": "semantic review did not verify the changes",
            }
        if auditor.blocking_findings(state.get("findings", "")):
            return {"decision": "reject", "notes": "blocking semantic findings"}
        if self.review_fn is not None:
            return self.review_fn(final_interrupt(interrupts), state)
        if state.get("valid") and not state.get("errors"):
            return {"decision": "approve"}
        notes = "; ".join(state.get("errors", []) or ["audit failed"])
        return {"decision": "reject", "notes": notes}

    def stage_audit(self, worktree_root):
        import auditor

        gen = auditor.AuditorGraph(
            worktree_root,
            audit_fn=self.audit_fn,
            td_runner=lambda args: self.td_run(list(args), worktree_root),
            # Single review pass: a rejection ends the stage as
            # CHANGES_REQUESTED so the pipeline (not the auditor's
            # internal loop) owns the repair cycle.
            max_loops=1,
            preview_url=self.preview_url,
            spec_file=self.spec_path,
            verification_commands=self.verification_commands,
            verification_evidence=self.verification_evidence,
            thread_id=f"{self.thread_id}-audit-{int(time.time())}",
        )
        state = drive_to_settled(
            gen.run, gen.run, self._audit_policy, limit=self.drive_limit
        )
        if not isinstance(state, dict) or state.get("drive_exhausted"):
            return BLOCKED, state, "audit drive exhausted"
        if not state.get("valid"):
            return BLOCKED, state, (state.get("errors") or ["gate failed"])
        verdict = state.get("verdict", {}) or gen.build_verdict(dict(state))
        status = verdict.get("status", CHANGES_REQUESTED)
        detail = verdict.get("findings", "") or "; ".join(verdict.get("errors", []))
        try:
            semantic = json.loads(detail)
        except (ValueError, TypeError):
            semantic = {}
        if isinstance(semantic, dict) and semantic.get("truncated") is True:
            return BLOCKED, state, detail
        return status, state, detail

    @contextmanager
    def _coder_tier(self, tier):
        """Apply the gated model tier to the next coder pass (#1403).

        The coder resolves its model through the CODER_MODEL override
        seam (coder.build_model); a strong tier sets it for exactly one
        pass and always restores the prior environment.
        """
        if tier != STRONG_TIER:
            yield
            return
        previous = os.environ.get("CODER_MODEL")
        os.environ["CODER_MODEL"] = self.strong_tier_model
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("CODER_MODEL", None)
            else:
                os.environ["CODER_MODEL"] = previous

    def stage_submit(self, worktree_root, title, body=""):
        """Submit via td agent submit-pr. Returns (ok, url-or-error)."""
        prepare_worktree_for_submit(
            self.repo, worktree_root, log_fn=lambda msg: self.log("submit", msg)
        )
        argv = [
            "--json",
            "agent",
            "submit-pr",
            "--worktree",
            worktree_root,
            "--title",
            title,
            "--body",
            body,
        ]
        if self.issue_id:
            argv += ["--issue", str(self.issue_id)]
        code, out = self.td_run(argv, self.repo)
        if code != 0:
            return False, out or f"td agent submit-pr exited with code {code}"
        payload, error = parse_json_output(out)
        if error:
            return False, error
        url = (payload or {}).get("url") or (payload or {}).get("pr_url")
        return True, url or json.dumps(payload)

    def stage_cleanup(self, worktree_root):
        """Remove the worktree. Returns (ok, error-or-empty)."""
        code, out = self.td_run(
            ["worktree", "remove", worktree_root, "--json"], self.repo
        )
        if code != 0:
            return False, out[:300]
        return True, ""

    def _await_if_paused(self, poll_s=0.5, timeout_s=30.0):
        """Block while a human has taken over the shared browser head.

        The pause signal comes from POST /api/agent/pause (first
        pointerdown/keydown on the co-browsing canvas). No browser
        mutations run until resume. On resume the refresh flag is
        cleared; no snapshot is captured here because this layer holds
        no element references -- stage_audit re-gathers impact fresh
        on the post-takeover DOM. Raises TimeoutError when timeout_s
        expires while still paused.
        """
        start = time.time()
        paused, _refresh = read_pause_state()
        was_paused = paused
        while paused:
            if timeout_s is not None and time.time() - start > timeout_s:
                raise TimeoutError("agent still paused by user")
            self.log("paused", "manual takeover active; standing by")
            time.sleep(poll_s)
            paused, _refresh = read_pause_state()
        _paused, refresh = read_pause_state()
        if refresh or was_paused:
            clear_refresh_flag()
            self.log("resumed", "post-takeover state accepted; continuing")

    def run(self):
        """Execute all stages; returns a result dict (never raises)."""
        with twt_receipts.run(self.thread_id) as receipts:
            result = self._run_guarded()
        if receipts.error:
            result.update(
                status=BLOCKED,
                detail=f"TwT receipt reconciliation failed: {receipts.error}",
            )
        return result

    def _run_guarded(self):
        started = time.time()
        try:
            return self._run(started)
        except Exception as exc:
            self.log("pipeline", f"EXC {type(exc).__name__}: {exc}")
            return self._result(BLOCKED, "", f"pipeline error: {exc}", 0, started)

    def _run(self, started):
        ok, reason = self.stage_init()
        if not ok:
            self.log("init", f"BLOCKED: {reason}")
            return self._result(BLOCKED, "", reason, 0, started)
        self.log("init", "td init ready")

        ok, _spec_state, spec = self.stage_spec()
        if not ok:
            errors = (
                _spec_state.get("errors")
                if isinstance(_spec_state, dict)
                else _spec_state
            )
            reason = errors or spec or "spec failed"
            if isinstance(reason, (list, tuple)):
                reason = "; ".join(str(error) for error in reason) or "spec failed"
            self.log("spec", f"BLOCKED: {reason}")
            return self._result(BLOCKED, "", reason, 0, started)
        self.log("spec", f"validated ({len(spec)} chars)")

        worktree = ""
        audit_cycles = 0
        # Repair budget + escalation state (issue #1403): every pass
        # after the first is a repair, whatever routed it back (the
        # deterministic pre-audit gate or an auditor rejection).
        repair_cycles = 0
        attempt = 0
        failure_class = ""
        # One refactor away from a NameError: the first loop pass
        # reads `detail` only when audit_cycles is nonzero, so it
        # survived uninitialized — initialize it outright. (#1136)
        detail = ""
        while True:
            self._await_if_paused()
            attempt += 1
            tier = choose_model_tier(
                attempt, failure_class, self.escalations_used, self.max_escalations
            )
            if tier == STRONG_TIER:
                self.escalations_used += 1
                self.log(
                    "escalation",
                    f"strong tier for attempt {attempt} after {failure_class}",
                )
            with self._coder_tier(tier):
                ok, _code_state, worktree = self.stage_code(
                    spec,
                    worktree_root=worktree or None,
                    repair_notes=detail if repair_cycles else "",
                )
            if not ok:
                reason = _code_state if isinstance(_code_state, str) else "coder failed"
                self.log("code", f"BLOCKED: {reason}")
                return self._result(
                    BLOCKED,
                    worktree,
                    reason,
                    audit_cycles,
                    started,
                    repair_cycles=repair_cycles,
                )
            self.log("code", f"worktree={worktree}")
            self._await_if_paused()
            gate = self.preaudit_fn or run_preaudit_gate
            gate_ok, gate_class, gate_summary = gate(worktree)
            if not gate_ok:
                # Broken patch: route straight back to the coder with
                # the truncated summary; never spend an auditor turn.
                self.log("preaudit", f"{gate_class}: {gate_summary[:200]}")
                status, detail = CHANGES_REQUESTED, gate_summary
                failure_class = gate_class
            else:
                status, _audit_state, detail = self.stage_audit(worktree)
                if status == APPROVED:
                    self.log("audit", "APPROVED")
                    break
                self.log("audit", f"{status}: {detail[:200]}")
                if status == BLOCKED:
                    return self._result(
                        BLOCKED,
                        worktree,
                        detail,
                        audit_cycles,
                        started,
                        repair_cycles=repair_cycles,
                    )
                failure_class = AUDIT_SEMANTIC
                audit_cycles += 1
                if audit_cycles > self.max_audit_cycles:
                    return self._result(
                        CHANGES_REQUESTED,
                        worktree,
                        detail,
                        audit_cycles - 1,
                        started,
                        repair_cycles=repair_cycles,
                    )
            if repair_cycles >= self.max_repair_cycles:
                self.log("repair", f"budget exhausted ({repair_cycles} cycles)")
                return self._result(
                    REPAIR_BUDGET_EXHAUSTED,
                    worktree,
                    detail,
                    audit_cycles,
                    started,
                    repair_cycles=repair_cycles,
                )
            repair_cycles += 1

        receipts = twt_receipts.current.get()
        if receipts is not None:
            receipts.reconcile()
            if receipts.error:
                return self._result(
                    BLOCKED,
                    worktree,
                    f"TwT receipt reconciliation failed: {receipts.error}",
                    audit_cycles,
                    started,
                    repair_cycles=repair_cycles,
                )

        submit_url = ""
        if self.submit_pr:
            title = (self.intent[:70] + "...") if len(self.intent) > 70 else self.intent
            ok, submit_url = self.stage_submit(worktree, title, spec[:500])
            self.log("submit", submit_url if ok else f"FAILED: {submit_url}")
            if not ok:
                return self._result(
                    CHANGES_REQUESTED,
                    worktree,
                    f"submit failed: {submit_url}",
                    audit_cycles,
                    started,
                    submit_url="",
                    repair_cycles=repair_cycles,
                )
        ok = False
        if self.keep_worktree:
            self.log("cleanup", "retained")
        else:
            ok, error = self.stage_cleanup(worktree)
            self.log("cleanup", "removed" if ok else f"FAILED: {error}")
        return self._result(
            APPROVED,
            worktree,
            detail,
            audit_cycles,
            started,
            submit_url=submit_url,
            cleaned=ok,
            repair_cycles=repair_cycles,
        )

    def _result(
        self,
        status,
        worktree,
        detail,
        audit_cycles,
        started,
        submit_url="",
        cleaned=False,
        repair_cycles=0,
    ):
        return {
            "status": status,
            "worktree": worktree,
            "detail": (detail if isinstance(detail, str) else json.dumps(detail))[:500],
            "audit_cycles": audit_cycles,
            "repair_cycles": repair_cycles,
            "escalations_used": self.escalations_used,
            "submit_url": submit_url,
            "cleaned": cleaned,
            "elapsed_s": round(time.time() - started, 2),
            "events": list(self.events),
        }
