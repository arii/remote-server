"""Pipeline: 4-stage autonomous loop (init -> spec -> code -> audit).

Stage 1 runs ``td init``; Stage 2 drives ``IssueGeneratorGraph`` to a
validated spec; Stage 3 drives ``InitCoderGraph`` in an isolated
worktree; Stage 4 drives ``AuditorGraph``. Auditor rejections
(``CHANGES_REQUESTED``) and deterministic blocks route the repair plan
back into a repair ``InitCoderGraph`` bound to the SAME worktree (up to
``max_audit_cycles``); ``APPROVED`` optionally submits via
``td submit-pr`` and then removes the worktree.

Stdlib-only at import; langgraph loads lazily inside the graph
modules. All model/tool seams are injectable for deterministic evals.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

MAX_AUDIT_CYCLES_DEFAULT = 2
DRIVE_LIMIT_DEFAULT = 25

APPROVED = "APPROVED"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
BLOCKED = "BLOCKED_DETERMINISTIC"


def run_td(argv, cwd=None, timeout=300):
    """Run the td CLI; returns (returncode, stdout). Never raises."""
    try:
        proc = subprocess.run(
            ["td", *argv], capture_output=True, text=True,
            timeout=timeout, cwd=cwd)
    except FileNotFoundError:
        return 127, "td CLI not found on PATH"
    except Exception as exc:
        return 1, f"td {' '.join(argv)} failed: {exc}"
    return proc.returncode, (proc.stdout or "").strip()


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
    return last.get("value", {}) if isinstance(last, dict) else {}


class Pipeline:
    """Autonomous 4-stage loop. Inject fakes for deterministic tests."""

    def __init__(self, repo, intent, issue_id=None, answers=None,
                 td_run=None, investigate_fn=None, plan_fn=None,
                 implement_fn=None, audit_fn=None, review_fn=None,
                 spec_model_fn=None, auto_approve_plan=True,
                 submit_pr=False, max_audit_cycles=MAX_AUDIT_CYCLES_DEFAULT,
                 thread_id=None, verification_commands=None,
                 drive_limit=DRIVE_LIMIT_DEFAULT):
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
        self.max_audit_cycles = max_audit_cycles
        self.thread_id = thread_id or f"pipeline-{int(time.time())}"
        self.verification_commands = list(verification_commands or [])
        self.drive_limit = drive_limit
        self.events = []

    def log(self, stage, message):
        self.events.append({"stage": stage, "message": message})

    # -- stage runners ------------------------------------------------

    def stage_init(self):
        code, out = self.td_run(["init", "--repo-root", self.repo,
                                 "--json"], self.repo)
        payload, error = parse_json_output(out)
        if code != 0 or error or (payload or {}).get("status") not in (
                "ready", "success"):
            reason = error or (payload or {}).get("message") or out[:300]
            return False, f"td init failed: {reason}"
        return True, payload

    def _spec_policy(self, interrupts, _state):
        asked = final_interrupt(interrupts).get("questions", [])
        if isinstance(asked, list):
            return {q: self.answers.get(q, "") for q in asked
                    if isinstance(q, str)}
        return dict(self.answers)

    def stage_spec(self):
        import issue_generator
        gen = issue_generator.IssueGeneratorGraph(
            repo_root=self.repo, investigate_fn=self.investigate_fn,
            model_fn=self.spec_model_fn,
            td_runner=lambda args: self.td_run(list(args), self.repo),
            thread_id=f"{self.thread_id}-spec")
        state = drive_to_settled(
            lambda: gen.run(self.intent, answers=dict(self.answers)),
            gen.run, self._spec_policy, limit=self.drive_limit)
        if not isinstance(state, dict) or state.get("drive_exhausted"):
            return False, {}, "spec drive exhausted"
        if not state.get("valid"):
            return False, state, (state.get("errors") or ["invalid spec"])
        parts = [state.get("draft", "")]
        if state.get("evidence"):
            parts.append("## Investigation Evidence\n" + state["evidence"])
        if state.get("answers"):
            parts.append("## Q&A Context\n" + "\n".join(
                f"- **{k}**: {v}"
                for k, v in sorted(state["answers"].items())))
        return True, state, "\n\n".join(p for p in parts if p)

    def _plan_policy(self, interrupts, _state):
        if self.auto_approve_plan:
            return {"decision": "approve"}
        payload = final_interrupt(interrupts)
        return {"decision": "approve", "plan": payload.get("plan", "")}

    def make_coder(self, brief, worktree_root=None, repair_notes=""):
        """Build an InitCoderGraph; repair cycles reuse worktree_root."""
        import coder
        context = dict(self.answers)
        if repair_notes:
            context["repair_notes"] = repair_notes
        graph = coder.InitCoderGraph(
            self.repo, self.issue_id or 0, brief,
            plan_fn=self.plan_fn, implement_fn=self.implement_fn,
            td_runner=lambda args, cwd: self.td_run(
                list(args), cwd or self.repo),
            thread_id=f"{self.thread_id}-coder-{int(time.time())}")
        if worktree_root:
            # Same-worktree repair: skip provisioning, reuse the root
            # (coder.py is frozen by Issue 7 scope; this rebind is the
            # documented repair seam, covered by tests).
            graph.provision_worktree = (
                lambda _root=worktree_root: (_root, None))
        graph._pipeline_context = context
        return graph

    def stage_code(self, brief, worktree_root=None, repair_notes=""):
        graph = self.make_coder(brief, worktree_root, repair_notes)
        state = drive_to_settled(
            graph.run, graph.run, self._plan_policy,
            limit=self.drive_limit)
        if not isinstance(state, dict) or state.get("drive_exhausted"):
            return False, state, "coder drive exhausted"
        if state.get("fatal"):
            return False, state, state["fatal"]
        if not state.get("valid"):
            return False, state, (state.get("errors") or ["coder invalid"])
        return True, state, state.get("worktree_root", "") or worktree_root

    def _audit_policy(self, interrupts, state):
        if self.review_fn is not None:
            return self.review_fn(final_interrupt(interrupts), state)
        if state.get("valid") and not state.get("errors"):
            return {"decision": "approve"}
        notes = "; ".join(state.get("errors", []) or ["audit failed"])
        return {"decision": "reject", "notes": notes}

    def stage_audit(self, worktree_root):
        import auditor
        gen = auditor.AuditorGraph(
            worktree_root, audit_fn=self.audit_fn,
            td_runner=lambda args: self.td_run(
                list(args), worktree_root),
            # Single review pass: a rejection ends the stage as
            # CHANGES_REQUESTED so the pipeline (not the auditor's
            # internal loop) owns the repair cycle.
            max_loops=1,
            thread_id=f"{self.thread_id}-audit-{int(time.time())}")
        state = drive_to_settled(
            gen.run, gen.run, self._audit_policy, limit=self.drive_limit)
        if not isinstance(state, dict) or state.get("drive_exhausted"):
            return BLOCKED, state, "audit drive exhausted"
        if not state.get("valid"):
            return BLOCKED, state, (state.get("errors") or ["gate failed"])
        verdict = state.get("verdict", {}) or gen.build_verdict(dict(state))
        status = verdict.get("status", CHANGES_REQUESTED)
        detail = verdict.get("findings", "") or "; ".join(
            verdict.get("errors", []))
        return status, state, detail

    def stage_submit(self, worktree_root, title, body=""):
        """Submit via td agent submit-pr. Returns (ok, url-or-error)."""
        argv = ["agent", "submit-pr", "--worktree", worktree_root,
                "--title", title, "--body", body, "--json"]
        if self.issue_id:
            argv += ["--issue", str(self.issue_id)]
        code, out = self.td_run(argv, self.repo)
        payload, error = parse_json_output(out)
        if code != 0 or error:
            return False, error or out[:300]
        url = (payload or {}).get("url") or (payload or {}).get("pr_url")
        return True, url or json.dumps(payload)

    def stage_cleanup(self, worktree_root):
        """Remove the worktree. Returns (ok, error-or-empty)."""
        code, out = self.td_run(
            ["worktree", "remove", worktree_root, "--json"], self.repo)
        if code != 0:
            return False, out[:300]
        return True, ""

    def run(self):
        """Execute all stages; returns a result dict (never raises)."""
        started = time.time()
        try:
            return self._run(started)
        except Exception as exc:
            self.log("pipeline", f"EXC {type(exc).__name__}: {exc}")
            return self._result(BLOCKED, "", f"pipeline error: {exc}",
                                0, started)

    def _run(self, started):
        ok, reason = self.stage_init()
        if not ok:
            self.log("init", f"BLOCKED: {reason}")
            return self._result(BLOCKED, "", reason, 0, started)
        self.log("init", "td init ready")

        ok, _spec_state, spec = self.stage_spec()
        if not ok:
            reason = _spec_state if isinstance(_spec_state, str) else (
                "; ".join(_spec_state.get("errors", []))
                if isinstance(_spec_state, dict) else "spec failed")
            self.log("spec", f"BLOCKED: {reason}")
            return self._result(BLOCKED, "", reason, 0, started)
        self.log("spec", f"validated ({len(spec)} chars)")

        worktree = ""
        audit_cycles = 0
        while True:
            ok, _code_state, worktree = self.stage_code(
                spec, worktree_root=worktree or None,
                repair_notes=detail if audit_cycles else "")
            if not ok:
                reason = (_code_state if isinstance(_code_state, str)
                          else "coder failed")
                self.log("code", f"BLOCKED: {reason}")
                return self._result(BLOCKED, worktree, reason,
                                    audit_cycles, started)
            self.log("code", f"worktree={worktree}")
            status, _audit_state, detail = self.stage_audit(worktree)
            if status == APPROVED:
                self.log("audit", "APPROVED")
                break
            self.log("audit", f"{status}: {detail[:200]}")
            if status == BLOCKED:
                return self._result(BLOCKED, worktree, detail,
                                    audit_cycles, started)
            audit_cycles += 1
            if audit_cycles > self.max_audit_cycles:
                return self._result(CHANGES_REQUESTED, worktree, detail,
                                    audit_cycles - 1, started)

        submit_url = ""
        if self.submit_pr:
            title = (self.intent[:70] + "...") if len(
                self.intent) > 70 else self.intent
            ok, submit_url = self.stage_submit(worktree, title, spec[:500])
            self.log("submit", submit_url if ok else f"FAILED: {submit_url}")
            if not ok:
                return self._result(
                    CHANGES_REQUESTED, worktree,
                    f"submit failed: {submit_url}", audit_cycles, started,
                    submit_url="")
        ok, error = self.stage_cleanup(worktree)
        self.log("cleanup", "removed" if ok else f"FAILED: {error}")
        return self._result(APPROVED, worktree, detail, audit_cycles,
                            started, submit_url=submit_url,
                            cleaned=ok)

    def _result(self, status, worktree, detail, audit_cycles, started,
                submit_url="", cleaned=False):
        return {"status": status, "worktree": worktree,
                "detail": (detail if isinstance(detail, str)
                           else json.dumps(detail)[:500]),
                "audit_cycles": audit_cycles,
                "submit_url": submit_url, "cleaned": cleaned,
                "elapsed_s": round(time.time() - started, 2),
                "events": list(self.events)}