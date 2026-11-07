"""Deterministic E2E evals for the 4-stage pipeline.

Each eval builds a self-contained git fixture (bare ``origin`` +
clone + sample module + pytest suite) and drives ``Pipeline`` with
deterministic fake model functions. Git operations (worktree add /
remove, status, commits) and ``pytest`` verification commands are
REAL; model calls (investigate/plan/implement/audit) and the ``td``
JSON gates are fixture-scoped fakes with faithful pass/fail logic
documented below. No network, no GPU/ollama, no GitHub calls.

Fake-gate semantics:
- ``td init`` -> ready.
- ``td validate-issue`` -> valid iff the spec file contains an
  ``## Acceptance Criteria`` section.
- ``td worktree create --issue N`` -> real ``git worktree add`` on a
  new ``agent/issue-N`` branch; ``td worktree remove`` removes it.
- ``td verify-scope`` -> real ``git status --porcelain`` in the
  worktree must be a subset of the spec's Allowed Files list.
- ``td audit-code`` -> pass (AST/slop coverage lives in boomtick's
  own ``test_ast_audit.py`` suite).
- ``td audit-impact`` -> real dependents: changed files plus worktree
  files importing them; violations only when the changed set is
  empty (nothing to audit) -- never in these evals.

``main()`` runs all evals and prints a JSON report; exit 0 iff all
pass. Used by ``deep-agent/cli.py eval-pipeline``.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pipeline as pipeline_mod

CALC_BUGGY = '''"""Sample module (buggy)."""


def add(a, b):
    """Return the sum (BUG: subtracts)."""
    return a - b
'''

CALC_FIXED = '''"""Sample module (fixed)."""


def add(a, b):
    """Return the sum."""
    return a + b
'''

TEST_CALC = '''"""Acceptance suite for calc.add."""
import calc


def test_add_sums():
    assert calc.add(2, 3) == 5
'''

SPEC_DRAFT = """# Fix calc.add

## Problem Statement
calc.add subtracts instead of adding.

## Goal
calc.add returns the sum.

## Acceptance Criteria
- [ ] `calc.add(2, 3) == 5` with `file:line` evidence in calc.py
"""


def _git(repo, *args):
    proc = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True,
        timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr[:300]}")
    return (proc.stdout or "").strip()


def build_fixture():
    """Create bare origin + seeded clone. Returns dict of paths."""
    tmp = tempfile.mkdtemp(prefix="eval-pipe-")
    origin = os.path.join(tmp, "origin.git")
    repo = os.path.join(tmp, "repo")
    subprocess.run(["git", "init", "--bare", "-q", origin], check=True)
    subprocess.run(["git", "clone", "-q", origin, repo], check=True)
    _git(repo, "config", "user.email", "eval@test")
    _git(repo, "config", "user.name", "eval")
    _git(repo, "checkout", "-q", "-b", "main")
    with open(os.path.join(repo, "calc.py"), "w",
              encoding="utf-8") as handle:
        handle.write(CALC_BUGGY)
    with open(os.path.join(repo, "test_calc.py"), "w",
              encoding="utf-8") as handle:
        handle.write(TEST_CALC)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed buggy calc")
    _git(repo, "push", "-q", "origin", "main")
    return {"tmp": tmp, "origin": origin, "repo": repo}


class FixtureTd:
    """Fixture-scoped td runner: real git, faithful gate logic."""

    def __init__(self, repo, fail_gates=None):
        self.repo = repo
        self.fail_gates = set(fail_gates or [])
        self.calls = []

    def __call__(self, argv, cwd=None):
        self.calls.append((list(argv), cwd))
        name = argv[0] if argv else ""
        if name == "init":
            return 0, json.dumps({"status": "ready"})
        if name == "validate-issue":
            path = argv[argv.index("--file") + 1]
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            if "## Acceptance Criteria" in text:
                return 0, json.dumps({"status": "success", "valid": True,
                                      "data": {}, "errors": []})
            return 1, json.dumps(
                {"status": "success", "valid": False, "data": {},
                 "errors": ["missing Acceptance Criteria section"]})
        if name == "worktree":
            return self._worktree(argv, cwd)
        if name == "verify-scope":
            return self._verify_scope(argv)
        if name == "audit-code":
            if "audit-code" in self.fail_gates:
                return 1, json.dumps(
                    {"status": "success", "passed": False,
                     "ast_violations": [{"file": "calc.py", "line": 1,
                                         "rule": "slop",
                                         "message": "scripted failure"}],
                     "slop_findings": []})
            return 0, json.dumps({"status": "success", "passed": True,
                                  "ast_violations": [],
                                  "slop_findings": []})
        if name == "audit-impact":
            return self._audit_impact(argv)
        return 1, json.dumps({"status": "error",
                              "message": f"unknown gate {name}"})

    def _worktree(self, argv, cwd):
        root = cwd or self.repo
        if argv[1] == "create":
            issue = argv[argv.index("--issue") + 1]
            path = os.path.join(root, ".worktrees", f"issue-{issue}")
            _git(root, "worktree", "add", path, "-b",
                 f"agent/issue-{issue}", "origin/main")
            return 0, json.dumps({"status": "success", "path": path,
                                  "branch": f"agent/issue-{issue}"})
        if argv[1] == "remove":
            path = argv[2]
            _git(self.repo, "worktree", "remove", "--force", path)
            return 0, json.dumps({"status": "success",
                                  "removed": path})
        return 1, json.dumps({"status": "error",
                              "message": f"unknown worktree {argv[1]}"})

    def _changed(self, worktree):
        out = _git(worktree, "status", "--porcelain")
        names = []
        for line in out.splitlines():
            parts = line.strip().split()
            if len(parts) < 2:
                continue
            name = os.path.basename(parts[-1].rstrip("/"))
            if not name or name == "__pycache__" or name.endswith(
                    (".pyc", ".pyo")):
                continue
            names.append(name)
        return names

    def _verify_scope(self, argv):
        spec = argv[argv.index("--spec-file") + 1]
        worktree = argv[argv.index("--worktree") + 1]
        with open(spec, encoding="utf-8") as handle:
            text = handle.read()
        allowed = set(re.findall(r"- `([^`]+)`", text))
        changed = set(self._changed(worktree))
        violations = sorted(c for c in changed if c not in allowed)
        passed = not violations
        code = 0 if passed else 2
        return code, json.dumps(
            {"status": "success", "passed": passed,
             "changed_files": sorted(changed),
             "allowed_files": sorted(allowed),
             "violations": [{"file": v, "rule": "scope"} for v in
                            violations]})

    def _audit_impact(self, argv):
        worktree = argv[argv.index("--worktree") + 1]
        changed = self._changed(worktree)
        impacted = set(changed)
        for name in os.listdir(worktree):
            if not name.endswith(".py") or name in impacted:
                continue
            with open(os.path.join(worktree, name), encoding="utf-8",
                      errors="replace") as handle:
                text = handle.read()
            for changed_file in changed:
                mod = os.path.splitext(changed_file)[0]
                if re.search(rf"^\s*(import|from)\s+{re.escape(mod)}\b",
                             text, re.M):
                    impacted.add(name)
        return 0, json.dumps(
            {"status": "success", "passed": True,
             "impacted_files": sorted(impacted),
             "dependent_tests": sorted(
                 f for f in impacted if f.startswith("test_")),
             "violations": []})


def _plan(brief, worktree_root, context):
    return {"plan": f"Fix per brief: {brief[:80]}",
            "allowed_files": ["calc.py"],
            "verification_commands": ["pytest test_calc.py -q"]}


def _implement_ok(brief, plan, worktree_root, errors):
    with open(os.path.join(worktree_root, "calc.py"), "w",
              encoding="utf-8") as handle:
        handle.write(CALC_FIXED)
    return "replaced subtract with add"


class _FakeModel:
    def invoke(self, _prompt):
        return SPEC_DRAFT


def _model_ok():
    return _FakeModel()


def _investigate_ok(messages, tools):
    return SPEC_DRAFT


def _audit_ok(gate):
    return "1. calc.add now sums; test_calc.py covers it."


def _run_pipeline(fixture, **overrides):
    params = {"repo": fixture["repo"],
              "intent": "calc.add subtracts; make it add",
              "issue_id": 42, "investigate_fn": _investigate_ok,
              "spec_model_fn": _model_ok,
              "plan_fn": _plan, "implement_fn": _implement_ok,
              "audit_fn": _audit_ok}
    params.update(overrides)
    pipe = pipeline_mod.Pipeline(**params)
    return pipe, pipe.run()


def eval_full_bugfix():
    """Eval 1: vague intent -> spec -> fix -> APPROVED, pytest green."""
    fixture = build_fixture()
    td = FixtureTd(fixture["repo"])
    pipe, result = _run_pipeline(fixture, td_run=td)
    worktrees = _git(fixture["repo"], "worktree", "list", "--porcelain")
    cleaned = ".worktrees/issue-42" not in worktrees
    passed = (result["status"] == pipeline_mod.APPROVED
              and result["worktree"] and cleaned)
    return {"name": "full_bugfix", "passed": passed,
            "detail": f"status={result['status']} "
                      f"worktree_cleaned={cleaned} "
                      f"events={len(pipe.events)}"}


def eval_coder_replan():
    """Eval 2: first implement writes nothing -> gate fails ->
    fix_loop retries with errors -> passes on the next turn."""
    fixture = build_fixture()
    td = FixtureTd(fixture["repo"])
    attempts = []

    def implement_flaky(brief, plan, worktree_root, errors):
        attempts.append(list(errors))
        if not errors:
            return "no-op first attempt (simulated model miss)"
        return _implement_ok(brief, plan, worktree_root, errors)

    pipe, result = _run_pipeline(fixture, td_run=td,
                                 implement_fn=implement_flaky)
    retried = len(attempts) >= 2 and bool(attempts[1])
    passed = (result["status"] == pipeline_mod.APPROVED and retried)
    return {"name": "coder_replan", "passed": passed,
            "detail": f"status={result['status']} "
                      f"implement_attempts={len(attempts)} "
                      f"second_saw_errors={retried}"}


def eval_auditor_reject():
    """Eval 3: coder passes gates but misses the requirement ->
    AuditorGraph CHANGES_REQUESTED -> repair in the SAME worktree ->
    APPROVED on audit cycle 2."""
    fixture = build_fixture()
    td = FixtureTd(fixture["repo"])

    reviews = []

    def review_strict(payload, state):
        reviews.append(1)
        if len(reviews) == 1:
            return {"decision": "reject",
                    "notes": "acceptance evidence missing for add()"}
        return {"decision": "approve"}

    seen_worktrees = []

    real_make_coder = pipeline_mod.Pipeline.make_coder

    def spy_make_coder(self, brief, worktree_root=None,
                       repair_notes=""):
        graph = real_make_coder(self, brief, worktree_root,
                                repair_notes)
        orig_run = graph.run

        def spy_run(resume=None):
            state = orig_run(resume)
            if (isinstance(state, dict)
                    and state.get("worktree_root")
                    and not state.get("__interrupt__")):
                seen_worktrees.append(state["worktree_root"])
            return state

        graph.run = spy_run
        return graph

    pipe = pipeline_mod.Pipeline(
        repo=fixture["repo"], intent="calc.add subtracts; make it add",
        issue_id=42, investigate_fn=_investigate_ok,
        spec_model_fn=_model_ok, plan_fn=_plan,
        implement_fn=_implement_ok, audit_fn=_audit_ok,
        review_fn=review_strict, td_run=td)
    pipeline_mod.Pipeline.make_coder = spy_make_coder
    try:
        result = pipe.run()
    finally:
        pipeline_mod.Pipeline.make_coder = real_make_coder
    same_worktree = (len(seen_worktrees) >= 2
                     and len(set(seen_worktrees)) == 1)
    passed = (result["status"] == pipeline_mod.APPROVED
              and result.get("audit_cycles", 0) >= 1
              and same_worktree)
    return {"name": "auditor_reject", "passed": passed,
            "detail": f"status={result['status']} "
                      f"audit_cycles={result.get('audit_cycles')} "
                      f"same_worktree={same_worktree}"}


EVALS = (eval_full_bugfix, eval_coder_replan, eval_auditor_reject)


def run_all():
    """Run all evals; returns (results, all_passed)."""
    results = []
    for fn in EVALS:
        try:
            results.append(fn())
        except Exception as exc:  # fail closed, never raise
            results.append({"name": fn.__name__, "passed": False,
                            "detail": f"EXC {type(exc).__name__}: {exc}"})
    return results, all(r["passed"] for r in results)


def main(argv=None):
    results, ok = run_all()
    print(json.dumps({"evals": results, "passed": ok}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
