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
import math
import os
import re
import subprocess
import tempfile
import time
from uuid import uuid4

from backend import ledger as history_ledger
from backend.shared import ledger
from backend.shared import s5tools
from backend.shared import twt_receipts

UNVERIFIED_CODER = "pipeline coder did not pass verification"
MAX_AUDIT_CYCLES_DEFAULT = 2
MAX_REPAIR_CYCLES_DEFAULT = 2
MAX_ESCALATIONS_DEFAULT = 1
DRIVE_LIMIT_DEFAULT = 25

APPROVED = "APPROVED"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
BLOCKED = "BLOCKED_DETERMINISTIC"
BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
# failure_class for coder call-cap exhaustion (#1638). A capped run ends
# BUDGET_EXHAUSTED and MAX_CALLS_PER_PARENT stays absolute, so the escalation
# applies to the next queue attempt (via COUNTED_STATUSES), not in-run.
CALL_CAP = "call_cap"
BUDGET_UNSPLITTABLE = "BUDGET_UNSPLITTABLE"

# Coder budget (planner owns fitting work to the model-call cap). A brief
# over either size budget is split into sequential sub-briefs; a cap hit
# resumes a bounded number of times; the parent total is a hard stop.
# BRIEF_LINE_BUDGET: briefs under ~60 lines succeeded fast in operator runs.
# BRIEF_FILE_BUDGET: an unvalidated guess; tune from the budget report.
BRIEF_LINE_BUDGET = 60
BRIEF_FILE_BUDGET = 4
SPLIT_MAX_PARTS = 3
MAX_CONTINUATIONS_PER_SUBBRIEF = 1
# SPLIT_MAX_PARTS x INITIAL_MODEL_LIMIT (coder.py); reported as the >40 bucket.
MAX_CALLS_PER_PARENT = 60
SPLIT_CONTEXT_LINES = 20

AGENT_PAUSED = "PAUSED_BY_USER"
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
TEST_COMMANDS = (
    ("pytest",),
    ("python3", "-m", "pytest"),
    ("python3", "-m", "unittest"),
    ("pnpm", "exec", "vitest"),
    ("pnpm", "test"),
    ("npm", "test"),
    ("go", "test"),
)


def _is_test_command(argv):
    return (
        any(tuple(argv[: len(head)]) == head for head in TEST_COMMANDS)
        or len(argv) >= 2
        and argv[0] == "python3"
        and bool(s5tools.TEST_FILE_RE.fullmatch(argv[1]))
        or len(argv) >= 3
        and argv[0] in ("npm", "pnpm")
        and argv[1] == "run"
        and (argv[2] == "test" or argv[2].startswith("test:"))
    )


def _count_run_rows(thread_id: str, trace_ids: set[str]):
    thread_prefix = json.dumps(thread_id)[:-1]
    exact_ids = [json.dumps(trace) for trace in trace_ids]
    candidates = re.compile("|".join(map(re.escape, [thread_prefix, *exact_ids])))
    count = 0
    try:
        with open(ledger.LEDGER, encoding="utf-8") as handle:
            for line in handle:
                if not candidates.search(line):
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("kind") != "deep-agent-run":
                    continue
                thread = row.get("thread_id")
                usage = row.get("usage")
                traces = usage.get("twt_trace_ids") if isinstance(usage, dict) else None
                if (
                    isinstance(thread, str)
                    and (thread == thread_id or thread.startswith(f"{thread_id}-"))
                    or isinstance(traces, list)
                    and any(
                        isinstance(trace, str) and trace in trace_ids
                        for trace in traces
                    )
                ):
                    count += 1
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        return None
    return count


_ACCEPTANCE_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*)?\**\s*(?:acceptance|done[ -]when)\b[^\n]*$", re.I
)
_HEADING = re.compile(r"^\s*#{1,6}\s+\S")
_ITEM = re.compile(r"^\s*[-*+]\s+(?:\[[ xX]\]\s*)?(\S.*)$")


def brief_title(brief):
    for line in brief.splitlines():
        if line.strip():
            return line.strip().lstrip("#").strip()[:120]
    return ""


def acceptance_items(brief):
    """Bullet or checkbox items under an Acceptance / Done when heading. Pure."""
    items, active = [], False
    for line in brief.splitlines():
        if _ACCEPTANCE_HEADING.match(line):
            active = True
        elif _HEADING.match(line):
            active = False
        elif active and (match := _ITEM.match(line)):
            items.append(match[1].strip())
    return items


def brief_files(brief, exclude=None):
    """Distinct files a brief names or allows, minus the spec file. Pure."""
    import coder
    import issue_generator

    names = [*coder.files_named(brief), *issue_generator.explicit_allowed_paths(brief)]
    skip = os.path.normpath(exclude) if exclude else None
    return sorted({n for n in names if os.path.normpath(n) != skip})


def brief_size(brief, exclude=None):
    """(non-blank lines, distinct files) of a brief. Pure."""
    lines = sum(1 for line in brief.splitlines() if line.strip())
    return lines, len(brief_files(brief, exclude))


def over_budget(brief, exclude=None):
    lines, files = brief_size(brief, exclude)
    return lines > BRIEF_LINE_BUDGET or files > BRIEF_FILE_BUDGET


def split_brief(brief):
    """Deterministically split a brief by its acceptance items. Pure, no model.

    Returns up to SPLIT_MAX_PARTS sequential [{id, title, brief,
    acceptance}] (contiguous acceptance groups), or None when the brief has
    fewer than 2 acceptance items. Each sub-brief carries the title, a
    bounded context preamble, its own acceptance list, and every file the
    parent allows (the coder gate rejects writes outside the allowed list).
    """
    import issue_generator

    items = acceptance_items(brief)
    if len(items) < 2:
        return None
    title = brief_title(brief)
    context = []
    for line in brief.splitlines()[1:]:
        if _HEADING.match(line) or _ACCEPTANCE_HEADING.match(line):
            break
        context.append(line)
    context = "\n".join(context).strip().splitlines()[:SPLIT_CONTEXT_LINES]
    allowed = issue_generator.explicit_allowed_paths(brief)
    parts = min(SPLIT_MAX_PARTS, len(items))
    size = math.ceil(len(items) / parts)
    groups = [items[i : i + size] for i in range(0, len(items), size)]
    subs = []
    for n, group in enumerate(groups, 1):
        files = list(dict.fromkeys([*allowed, *brief_files("\n".join(group))]))
        text = [f"# {title} (part {n} of {len(groups)})", *context]
        text += ["", "## Acceptance", *[f"- {item}" for item in group]]
        if files:
            text += ["", "### Allowed Files", *[f"- `{path}`" for path in files]]
        subs.append(
            {
                "id": n,
                "title": f"{title} (part {n} of {len(groups)})",
                "brief": "\n".join(text) + "\n",
                "acceptance": group,
            }
        )
    return subs


def new_budget(sub_briefs):
    return {
        "parent": {
            "calls_used": 0,
            "max_calls": MAX_CALLS_PER_PARENT,
            "exhausted": False,
            "verified": False,
        },
        "sub_briefs": [
            {
                "id": sub["id"],
                "title": sub["title"],
                "acceptance": list(sub["acceptance"]),
                "status": "pending",
                "calls": 0,
                "continuations": 0,
                "verified": False,
                "verification": "",
            }
            for sub in sub_briefs
        ],
    }


def budget_state_path(thread_id):
    """S5_BUDGET_STATE, else <state dir>/budget-<thread_id>.json."""
    configured = os.environ.get("S5_BUDGET_STATE")
    if configured:
        return configured
    from backend.shared import ledger

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", thread_id)[:64]
    return os.path.join(ledger.state_dir(), f"budget-{safe}.json")


def write_budget(path, budget):
    """Atomic JSON write (temp file + rename in the same directory)."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(budget, out, indent=2)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def choose_model_tier(
    attempt: int,
    failure_class: str | None,
    escalations_used: int = 0,
    max_escalations: int = 1,
) -> tuple[str, int]:
    """Select model tier ('fast' vs 'strong') based on failure classification and retry attempt.

    Rules:
    - Attempt <= 1 or failure_class == 'mechanical' always stays on 'fast'.
    - Attempt >= 2 with 'test_failure', 'audit_semantic' or 'call_cap' escalates to 'strong' if
      escalations_used < max_escalations.
    - Strong-tier escalations are capped at max_escalations (default 1).
    Returns (tier, new_escalations_used).
    """
    if attempt <= 1 or failure_class == "mechanical" or not failure_class:
        return "fast", escalations_used
    if failure_class in ("test_failure", "audit_semantic", CALL_CAP):
        if escalations_used < max_escalations:
            return "strong", escalations_used + 1
    return "fast", escalations_used


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
        thread_id=None,
        verification_commands=None,
        drive_limit=DRIVE_LIMIT_DEFAULT,
        preview_url=None,
        keep_worktree=False,
        pre_audit_fn=None,
        max_repair_cycles=None,
        max_escalations=MAX_ESCALATIONS_DEFAULT,
        cli_fallback=None,
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
        self.pre_audit_fn = pre_audit_fn
        self.max_repair_cycles = (
            max_repair_cycles
            if max_repair_cycles is not None
            else int(os.environ.get("S5_MAX_REPAIR_CYCLES", MAX_REPAIR_CYCLES_DEFAULT))
        )
        self.max_escalations = max_escalations
        self.start_model_tier = os.environ.get("S5_START_MODEL_TIER") or "fast"
        if self.start_model_tier not in ("fast", "strong"):
            raise ValueError("S5_START_MODEL_TIER must be 'fast' or 'strong'")
        self.current_model_tier = self.start_model_tier
        self.escalations_used = 0
        self.thread_id = thread_id or f"pipeline-{uuid4().hex}"
        self.verification_commands = list(verification_commands or [])
        self.verification_evidence = []
        self._test_checks = {}
        self._opened_pr_url = ""
        self.drive_limit = drive_limit
        self.preview_url = preview_url
        self.events = []
        self.budget = None
        # Opt-in CLI fallback (issue #1610): off by default, enabled
        # only when the operator constructs an enabled config (or the
        # PIPELINE_CLI_FALLBACK env switch is set).
        if cli_fallback is None:
            import cli_fallback

            cli_fallback = cli_fallback.CliFallbackConfig()
        self.cli_fallback = cli_fallback
        self._current_sub = None
        self.budget_path = budget_state_path(self.thread_id)
        # Per stage_code run; implement (or a test double) fills these.
        self.stage_calls = 0
        self.stage_continuations = 0
        self.stage_exhausted = False

    def _repo_slug(self):
        """owner/name for the checkout, '' when unknown (default verify policy)."""
        return s5tools.repo_for_checkout(self.repo)

    def log(self, stage, message):
        self.events.append({"stage": stage, "message": message})

    # -- stage runners ------------------------------------------------

    def stage_init(self):
        if (
            os.environ.get("S5_ISSUE_PROVIDER") == "twt"
            and os.environ.get("TWT_PREFLIGHT_REQUIRED") == "1"
        ):
            try:
                from backend.shared import model_router

                if hasattr(model_router, "preflight_gateway"):
                    model_router.preflight_gateway(stage="coder")
            except Exception as exc:
                return False, f"twt gateway preflight failed: {exc}"
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
            if getattr(self, "current_model_tier", None):
                kwargs["model_tier"] = self.current_model_tier
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
            else:
                kwargs["first_edit"] = coder.first_edit_targets(
                    root, plan, exclude=(spec_path,)
                )
            meter = {}
            try:
                result = coder.run_coder(
                    root,
                    prompt,
                    coder.build_system(root, "", self._repo_slug()),
                    f"{self.thread_id}-edit-{time.time_ns()}",
                    coder._timeout(),
                    repo=self._repo_slug(),
                    continuations=max(
                        0,
                        MAX_CONTINUATIONS_PER_SUBBRIEF
                        - self._sub_continuations()
                        - self.stage_continuations,
                    ),
                    call_budget=self.calls_left(),
                    meter=meter,
                    cli_fallback=self.cli_fallback,
                    issue=(
                        f"{self._repo_slug()}#{self.issue_id}"
                        if self.issue_id and self._repo_slug()
                        else None
                    ),
                    run_id=self.thread_id,
                    stage="code",
                    cli_verify_fn=self.pre_audit_check,
                    **kwargs,
                )
            except Exception as exc:
                self.verification_evidence = coder.trace_verification_evidence(
                    meter.get("trace"), self._repo_slug()
                )
                if coder.is_call_cap(exc):
                    self.stage_exhausted = True
                    raise RuntimeError(
                        "coder model-call budget exhausted after "
                        f"{meter.get('calls', 0)} calls in this turn "
                        f"({meter.get('continuations', 0)} continuations)"
                    ) from exc
                if not isinstance(exc, RuntimeError):
                    raise
                if kwargs.get("repair") and errors and coder.NO_WRITES in str(exc):
                    # Still fail closed, but keep the deterministic gate
                    # failure that triggered the repair visible (#1372).
                    raise RuntimeError(
                        f"{exc}; unresolved gate errors: {'; '.join(map(str, errors))}"
                    ) from exc
                raise
            finally:
                self.stage_calls += meter.get("calls", 0)
                self.stage_continuations += meter.get("continuations", 0)
            summary, usage, calls, started, label, trace, verified, _ = result
            self.verification_evidence = coder.trace_verification_evidence(
                trace, self._repo_slug()
            )
            provider, _, model = label.partition(":")
            graph.last_model_spec = {"provider": provider, "model": model}
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
            context = {**self.answers, **context, "first_edit_plan": not repair_notes}
            if repair_notes:
                context["repair_notes"] = repair_notes
            if self._repo_slug():
                context.setdefault("repo", self._repo_slug())
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

    # -- coder budget -------------------------------------------------

    def calls_left(self):
        used = self.budget["parent"]["calls_used"] if self.budget else 0
        return MAX_CALLS_PER_PARENT - used - self.stage_calls

    def _sub_continuations(self):
        return self._current_sub["continuations"] if self._current_sub else 0

    def _save_budget(self):
        if self.budget:
            write_budget(self.budget_path, self.budget)

    def _begin_stage(self, sub):
        self._current_sub = sub
        self.stage_calls = 0
        self.stage_continuations = 0
        self.stage_exhausted = False
        self.verification_evidence = []
        self._test_checks = {}

    def _end_stage(self, sub):
        """Fold this stage_code run's calls into the sub-brief and parent."""
        if sub is not None:
            sub["calls"] += self.stage_calls
            sub["continuations"] += self.stage_continuations
        parent = self.budget["parent"]
        parent["calls_used"] += self.stage_calls
        parent["exhausted"] = parent["exhausted"] or self.stage_exhausted
        self._save_budget()

    def _mark(self, sub, status, verification=""):
        sub["status"] = status
        sub["verified"] = status == "done"
        sub["verification"] = verification
        self._save_budget()

    def _partial(self):
        subs = self.budget["sub_briefs"]
        return {
            "done": [
                a for sub in subs if sub["status"] == "done" for a in sub["acceptance"]
            ],
            "not_done": [
                a for sub in subs if sub["status"] != "done" for a in sub["acceptance"]
            ],
        }

    def _coder_stopped(self, sub, reason, worktree, audit_cycles, started, **extra):
        """Result for a failed or budget-stopped coder stage (no PR)."""
        if (
            self.stage_exhausted
            or self.budget["parent"]["calls_used"] >= MAX_CALLS_PER_PARENT
        ):
            self._mark(sub, "exhausted", reason)
            self.budget["parent"]["exhausted"] = True
            self._save_budget()
            status = BUDGET_EXHAUSTED
            reason = (
                f"{reason}; {self.budget['parent']['calls_used']}/"
                f"{MAX_CALLS_PER_PARENT} calls used. Needs operator approval: "
                "retry with --retry to start a fresh parent budget"
            )
        else:
            self._mark(sub, "failed", str(reason)[:200])
            status = BLOCKED
        split = len(self.budget["sub_briefs"]) > 1
        partial = self._partial() if split or status != BLOCKED else None
        return self._result(
            status, worktree, reason, audit_cycles, started, partial=partial, **extra
        )

    def pre_audit_check(self, worktree_root):
        """Run fast, deterministic pre-audit checks before invoking AuditorGraph.

        Checks:
        1. Python AST/syntax on modified .py files -> 'mechanical' failure
        2. Verification commands (diff-scoped tests/linter) -> 'test_failure' or 'mechanical'

        Returns (passed: bool, failure_class: str | None, reason: str).
        """
        if self.pre_audit_fn is not None:
            return self.pre_audit_fn(worktree_root)
        import ast

        # 1. AST syntax validation on modified .py files
        try:
            diff_proc = subprocess.run(
                ["git", "diff", "--name-only", "origin/main", "--", "."],
                cwd=worktree_root,
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception as exc:
            return False, "mechanical", f"could not list changed files: {exc}"
        if diff_proc.returncode != 0:
            detail = (diff_proc.stderr or "").strip()[:200]
            return (
                False,
                "mechanical",
                f"could not list changed files (git diff exit {diff_proc.returncode}): {detail}",
            )
        changed_files = [
            f.strip() for f in (diff_proc.stdout or "").splitlines() if f.strip()
        ]

        for rel_path in changed_files:
            if rel_path.endswith(".py"):
                full_path = os.path.join(worktree_root, rel_path)
                if os.path.isfile(full_path):
                    try:
                        with open(full_path, "r", encoding="utf-8") as handle:
                            ast.parse(handle.read(), filename=rel_path)
                    except SyntaxError as syn_err:
                        err_msg = (
                            f"SyntaxError in {rel_path}:{syn_err.lineno}: {syn_err.msg}"
                        )
                        return False, "mechanical", err_msg

        # 2. Deterministic verification commands
        for argv in self.verification_commands:
            try:
                proc = subprocess.run(
                    list(argv),
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=worktree_root,
                    check=False,
                )
                if _is_test_command(argv):
                    self._test_checks[tuple(argv)] = proc.returncode == 0
                if proc.returncode != 0:
                    output = ((proc.stderr or "") + (proc.stdout or ""))[-2000:]
                    cmd_str = " ".join(argv)
                    f_class = "test_failure"
                    if "SyntaxError" in output or "IndentationError" in output:
                        f_class = "mechanical"
                    return (
                        False,
                        f_class,
                        f"Command '{cmd_str}' failed (exit {proc.returncode}):\n{output}",
                    )
            except Exception as exc:
                if _is_test_command(argv):
                    self._test_checks[tuple(argv)] = False
                return (
                    False,
                    "mechanical",
                    f"Verification command {argv} execution error: {exc}",
                )

        return True, None, ""

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
            issue_id=self.issue_id,
            verification_commands=self.verification_commands,
            verification_evidence=self.verification_evidence,
            thread_id=f"{self.thread_id}-audit-{int(time.time())}",
        )
        state = drive_to_settled(
            gen.run, gen.run, self._audit_policy, limit=self.drive_limit
        )
        if isinstance(state, dict):
            for check in (
                (state.get("gate") or {}).get("verification", {}).get("commands", [])
            ):
                argv = check["command"]
                if _is_test_command(argv):
                    self._test_checks[tuple(argv)] = check["exit"] == 0
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
        if isinstance(url, str) and PR_URL.fullmatch(url):
            self._opened_pr_url = url
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
        started = time.time()
        receipts = None
        try:
            with twt_receipts.run(self.thread_id) as receipts:
                result = self._run_guarded()
            if receipts.error:
                result.update(
                    status=BLOCKED,
                    detail=f"TwT receipt reconciliation failed: {receipts.error}",
                )
        except Exception as exc:
            result = self._result(
                BLOCKED,
                "",
                f"TwT receipt settlement failed: {exc}",
                0,
                started,
            )
        self._record_outcome(result, receipts.ids if receipts is not None else set())
        return result

    def _record_outcome(self, result, trace_ids):
        checks = {}
        repo = self._repo_slug()
        for check in self.verification_evidence:
            if check.get("stale"):
                continue
            argv = s5tools.parse_check_command(check["command"], repo)
            if _is_test_command(argv):
                checks[tuple(argv)] = check["passed"]
        checks.update(self._test_checks)
        url = self._opened_pr_url or result["submit_url"]
        history_ledger.append(
            {
                "kind": "pipeline-run",
                "repo": repo.removeprefix("arii/"),
                "prompt": brief_title(self.intent),
                "status": result["status"],
                "test_status": ("passed" if all(checks.values()) else "failed")
                if checks
                else "not run",
                "submit_url": url
                if isinstance(url, str) and PR_URL.fullmatch(url)
                else "",
                "ledger_rows": _count_run_rows(self.thread_id, trace_ids),
            }
        )

    def _run_guarded(self):
        started = time.time()
        try:
            return self._run(started)
        except Exception as exc:
            self.log("pipeline", f"EXC {type(exc).__name__}: {exc}")
            return self._result(BLOCKED, "", f"pipeline error: {exc}", 0, started)

    def _next_tier(self, attempt, failure_class):
        """choose_model_tier, except a strong start never drops back to fast."""
        tier, used = choose_model_tier(
            attempt=attempt,
            failure_class=failure_class,
            escalations_used=self.escalations_used,
            max_escalations=self.max_escalations,
        )
        return ("strong" if self.start_model_tier == "strong" else tier), used

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
        repair_cycles = 0
        # One refactor away from a NameError: the first loop pass
        # reads `detail` only when audit_cycles is nonzero, so it
        # survived uninitialized — initialize it outright. (#1136)
        detail = ""

        subs = None
        if over_budget(spec, self.spec_path):
            subs = split_brief(spec)
            if subs is None:
                lines, files = brief_size(spec, self.spec_path)
                self.budget = new_budget(
                    [{"id": 1, "title": brief_title(spec), "acceptance": []}]
                )
                self._mark(self.budget["sub_briefs"][0], "failed", "unsplittable")
                reason = (
                    f"brief is over budget ({lines} lines, {files} files; limits "
                    f"{BRIEF_LINE_BUDGET} lines, {BRIEF_FILE_BUDGET} files) and has "
                    "fewer than 2 acceptance items so it cannot be split. Add an "
                    "'Acceptance' list with at least 2 independent items, or split "
                    "the issue by hand."
                )
                self.log("split", f"BLOCKED: {reason}")
                return self._result(BUDGET_UNSPLITTABLE, "", reason, 0, started)
        if subs is None:
            subs = [
                {
                    "id": 1,
                    "title": brief_title(spec),
                    "brief": spec,
                    "acceptance": acceptance_items(spec) or [brief_title(spec)],
                }
            ]
        self.budget = new_budget(subs)
        self._save_budget()
        split = len(subs) > 1
        if split:
            self.log("split", f"{len(subs)} sub-briefs")
            for sub, record in zip(subs, self.budget["sub_briefs"]):
                self._await_if_paused()
                if self.budget["parent"]["calls_used"] >= MAX_CALLS_PER_PARENT:
                    self.stage_exhausted = True
                    return self._coder_stopped(
                        record, "parent call cap reached", worktree, 0, started
                    )
                self._begin_stage(record)
                ok, _code_state, root = self.stage_code(
                    sub["brief"], worktree_root=worktree or None
                )
                self._end_stage(record)
                if not ok:
                    reason = (
                        _code_state if isinstance(_code_state, str) else "coder failed"
                    )
                    self.log("code", f"BLOCKED (part {record['id']}): {reason}")
                    return self._coder_stopped(
                        record,
                        reason,
                        root if isinstance(root, str) else worktree,
                        0,
                        started,
                    )
                worktree = root
                pre_ok, failure_class, reason = self.pre_audit_check(worktree)
                if not pre_ok:
                    self.log("pre_audit", f"FAILED ({failure_class}): {reason[:200]}")
                    self._mark(
                        record, "failed", f"pre-audit {failure_class}: {reason}"[:200]
                    )
                    return self._result(
                        BLOCKED,
                        worktree,
                        f"sub-brief {record['id']} failed pre-audit {failure_class}: {reason}",
                        0,
                        started,
                        partial=self._partial(),
                    )
                self._mark(record, "done", "run_check passed; pre-audit passed")
                self.log("code", f"part {record['id']} done worktree={worktree}")
            # The auditor reads the spec file: restore the whole brief.
            import issue_generator

            issue_generator.write_spec(os.path.join(worktree, self.spec_path), spec)
        precoded = split
        while True:
            self._await_if_paused()
            if precoded:
                precoded = False
            else:
                record = self.budget["sub_briefs"][-1]
                if self.budget["parent"]["calls_used"] >= MAX_CALLS_PER_PARENT:
                    self.stage_exhausted = True
                    return self._coder_stopped(
                        record,
                        "parent call cap reached",
                        worktree,
                        audit_cycles,
                        started,
                        repair_cycles=repair_cycles,
                    )
                self._begin_stage(None if split else record)
                ok, _code_state, worktree = self.stage_code(
                    spec,
                    worktree_root=worktree or None,
                    repair_notes=detail if repair_cycles else "",
                )
                self._end_stage(None if split else record)
                if not ok:
                    reason = (
                        _code_state if isinstance(_code_state, str) else "coder failed"
                    )
                    self.log("code", f"BLOCKED: {reason}")
                    return self._coder_stopped(
                        record,
                        reason,
                        worktree,
                        audit_cycles,
                        started,
                        repair_cycles=repair_cycles,
                    )
                self.log("code", f"worktree={worktree}")
                self._await_if_paused()

                # Deterministic pre-audit gate (#1403)
                pre_ok, failure_class, reason = self.pre_audit_check(worktree)
                if not pre_ok:
                    repair_cycles += 1
                    self.log("pre_audit", f"FAILED ({failure_class}): {reason[:200]}")
                    if repair_cycles >= self.max_repair_cycles:
                        return self._result(
                            CHANGES_REQUESTED,
                            worktree,
                            f"repair cycles exhausted ({repair_cycles}/{self.max_repair_cycles}): pre-audit {failure_class}: {reason}",
                            audit_cycles,
                            started,
                            repair_cycles=repair_cycles,
                        )
                    self.current_model_tier, self.escalations_used = self._next_tier(
                        repair_cycles + 1, failure_class
                    )
                    detail = f"Pre-audit {failure_class} failure:\n{reason}"
                    continue

            status, _audit_state, detail = self.stage_audit(worktree)
            if status == APPROVED:
                self.log("audit", "APPROVED")
                for record in self.budget["sub_briefs"]:
                    if record["status"] != "done":
                        self._mark(record, "done", "audit approved")
                self.budget["parent"]["verified"] = True
                self._save_budget()
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
            audit_cycles += 1
            repair_cycles += 1
            if (
                repair_cycles >= self.max_repair_cycles
                or audit_cycles > self.max_audit_cycles
            ):
                return self._result(
                    CHANGES_REQUESTED,
                    worktree,
                    detail,
                    audit_cycles,
                    started,
                    repair_cycles=repair_cycles,
                )
            self.current_model_tier, self.escalations_used = self._next_tier(
                repair_cycles + 1, "audit_semantic"
            )

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
        repair_cycles=None,
        partial=None,
    ):
        result = {
            "status": status,
            "worktree": worktree,
            "detail": (detail if isinstance(detail, str) else json.dumps(detail))[:500],
            "audit_cycles": audit_cycles,
            "repair_cycles": repair_cycles
            if repair_cycles is not None
            else audit_cycles,
            "submit_url": submit_url,
            "cleaned": cleaned,
            "elapsed_s": round(time.time() - started, 2),
            "events": list(self.events),
            "budget": self.budget,
        }
        if partial is not None:
            result["partial"] = partial
        return result
