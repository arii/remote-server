"""Tests for coder.InitCoderGraph (stdlib only, no model).

Graph integration cases need langgraph (deep-agent venv) and skip
otherwise. Run agent cases with: deep-agent/.venv/bin/python
tests/test_coder.py
"""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import coder

try:
    import langgraph  # noqa: F401

    HAS_AGENT_DEPS = True
except ImportError:
    HAS_AGENT_DEPS = False


def _gate_ok(args, cwd):
    cmd = " ".join(args)
    if "verify-scope" in cmd:
        return 0, json.dumps({"status": "success", "passed": True,
                              "modified_files": [], "violations": []})
    if "audit-code" in cmd:
        return 0, json.dumps({"status": "success", "passed": True,
                              "slop_findings": [], "suppressions_added": [],
                              "ast_violations": []})
    if "worktree" in cmd:
        return 0, json.dumps({"status": "ready", "path": cwd,
                              "branch": "agent/issue-1",
                              "worktreeId": "issue-1"})
    raise AssertionError(f"unexpected td call: {cmd}")


class TestWorktreeGuard(unittest.TestCase):
    def test_blocks_escape(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                coder.assert_inside(root, "../outside.txt")
            with self.assertRaises(ValueError):
                coder.assert_inside(root, "/etc/passwd")
            with self.assertRaises(ValueError):
                coder.read_worktree_file(root, "../../x")

    def test_allows_inside(self):
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "sub", "f.py")
            os.makedirs(os.path.dirname(target))
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("x = 1\n")
            self.assertEqual(
                coder.read_worktree_file(root, "sub/f.py"), "x = 1\n")

    def test_gate_spec_markdown_shape(self):
        spec = coder.gate_spec_markdown("Fix it", ["a.py", "b.py"])
        for section in ("Problem Statement", "Goal", "Scope",
                        "Definition of Done", "Scope & File Boundaries",
                        "Acceptance Criteria"):
            self.assertIn(section, spec)
        self.assertIn("`a.py`", spec)


class TestProvisionAndGate(unittest.TestCase):
    def test_provision_bad_payload(self):
        def td_runner(args, cwd):
            return 0, "not json"
        gen = coder.InitCoderGraph(
            repo="/tmp", issue_id=9, brief="x", td_runner=td_runner)
        root, error = gen.provision_worktree()
        self.assertIsNone(root)
        self.assertIn("non-JSON", error)

    def test_gate_collects_all_failures(self):
        def td_runner(args, cwd):
            cmd = " ".join(args)
            if "verify-scope" in cmd:
                return 1, json.dumps(
                    {"status": "success", "passed": False,
                     "modified_files": ["evil.py"], "violations": ["evil.py"]})
            if "audit-code" in cmd:
                return 1, json.dumps(
                    {"status": "success", "passed": False,
                     "slop_findings": [], "suppressions_added": [],
                     "ast_violations": [{"file": "a.py", "line": 1,
                                         "rule": "r", "message": "m"}]})
            raise AssertionError(cmd)

        def cmd_runner(checkout, command):
            return "exit=1\nboom"

        gen = coder.InitCoderGraph(
            repo="/tmp", issue_id=9, brief="Fix it", td_runner=td_runner,
            cmd_runner=cmd_runner)
        gate = gen.run_gate("/tmp", ["a.py"], ["make check"])
        self.assertFalse(gate["passed"])
        joined = "\n".join(gate["errors"])
        self.assertIn("scope violation: evil.py", joined)
        self.assertIn("audit: a.py:1: r: m", joined)
        self.assertIn("exit=1", joined)

    def test_gate_passes_clean(self):
        with tempfile.TemporaryDirectory() as root:
            gen = coder.InitCoderGraph(
                repo="/tmp", issue_id=9, brief="Fix it",
                td_runner=_gate_ok,
                cmd_runner=lambda c, cmd: "exit=0\nok")
            gate = gen.run_gate(root, ["a.py"], ["make check"])
            self.assertTrue(gate["passed"])
            self.assertEqual(gate["errors"], [])


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv (langgraph)")
class TestCoderLoop(unittest.TestCase):
    def _gen(self, tmp, plan=None, implement="done",
             td_runner=None, cmds=None, thread="test-coder-1"):
        def plan_fn(brief, root, context):
            return dict(plan or {"plan": "P",
                                 "allowed_files": [],
                                 "verification_commands": []})

        def implement_fn(brief, plan_text, root, errors):
            return implement

        def cmd_runner(checkout, command):
            return (cmds or {}).get(command, "exit=0\nok")

        return coder.InitCoderGraph(
            repo=tmp, issue_id=7, brief="Fix it", plan_fn=plan_fn,
            implement_fn=implement_fn,
            td_runner=td_runner or _gate_ok, cmd_runner=cmd_runner,
            max_fix_loops=2, max_replans=1, thread_id=thread)

    def test_approve_implement_gate_persist(self):
        import ledger
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = os.path.join(tmp, "ledger.jsonl")
            old = ledger.LEDGER
            ledger.LEDGER = ledger_path
            try:
                gen = self._gen(tmp)
                state = gen.run()
                self.assertIn("__interrupt__", state)
                final = gen.run(resume={"decision": "approve"})
                self.assertTrue(final.get("valid"))
                self.assertTrue(final.get("persisted"))
            finally:
                ledger.LEDGER = old
            with open(ledger_path, encoding="utf-8") as handle:
                entry = json.loads(
                    handle.read().strip().splitlines()[-1])
            self.assertEqual(entry.get("script"), "coder.py")

    def test_gate_failure_fix_loops_then_replans(self):
        attempts = {"n": 0}

        def td_runner(args, cwd):
            # NOTE: verify-scope/audit-code carry a --worktree flag, so
            # match them before the bare worktree-create command.
            cmd = " ".join(args)
            if "verify-scope" in cmd:
                attempts["n"] += 1
                if attempts["n"] < 4:
                    return 1, json.dumps(
                        {"status": "success", "passed": False,
                         "modified_files": [], "violations": ["x.py"]})
                return _gate_ok(args, cwd)
            if "audit-code" in cmd:
                return _gate_ok(args, cwd)
            if "worktree" in cmd:
                return 0, json.dumps({"status": "ready", "path": cwd,
                                      "branch": "b", "worktreeId": "w"})
            raise AssertionError(f"unexpected td call: {cmd}")

        with tempfile.TemporaryDirectory() as tmp:
            gen = self._gen(tmp, td_runner=td_runner,
                            thread="test-coder-2")
            state = gen.run()
            state = gen.run(resume={"decision": "approve"})
            if "__interrupt__" in state:
                # Replan re-validates the new plan via HITL.
                state = gen.run(resume={"decision": "approve"})
            self.assertTrue(state.get("valid"))
            self.assertGreaterEqual(state.get("replans", 0), 1)

    def test_exhausted_loops_fail_closed(self):
        def td_runner(args, cwd):
            cmd = " ".join(args)
            if "verify-scope" in cmd or "audit-code" in cmd:
                return 1, json.dumps({"status": "error",
                                      "message": "always broken"})
            if "worktree" in cmd:
                return 0, json.dumps({"status": "ready", "path": cwd,
                                      "branch": "b", "worktreeId": "w"})
            raise AssertionError(f"unexpected td call: {cmd}")

        with tempfile.TemporaryDirectory() as tmp:
            gen = self._gen(tmp, td_runner=td_runner,
                            thread="test-coder-3")
            gen.run()
            final = gen.run(resume={"decision": "approve"})
            self.assertFalse(final.get("valid"))

    def test_provision_failure_ends(self):
        def td_runner(args, cwd):
            raise RuntimeError("no td here")

        with tempfile.TemporaryDirectory() as tmp:
            gen = coder.InitCoderGraph(
                repo=tmp, issue_id=7, brief="x", td_runner=td_runner,
                plan_fn=lambda b, r, c: {"plan": "P"},
                implement_fn=lambda b, p, r, e: "done",
                thread_id="test-coder-4")
            final = gen.run()
            self.assertIn("td worktree create failed", final.get("fatal"))


class FakeModel:
    def __init__(self, text):
        self.text = text
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.text


class TestCoderDefaults(unittest.TestCase):
    """Out-of-the-box fns (#1103): stage_code runs without test stubs."""

    def test_defaults_wired_without_stubs(self):
        gen = coder.InitCoderGraph(
            repo="/tmp", issue_id=7, brief="Fix it",
            td_runner=lambda args, cwd: (0, "{}"))
        self.assertIs(gen.plan_fn, coder._default_plan_fn)
        self.assertIs(gen.implement_fn, coder._default_implement_fn)

    def test_explicit_fns_still_win(self):
        plan = lambda b, r, c: {"plan": "P"}  # noqa: E731
        gen = coder.InitCoderGraph(
            repo="/tmp", issue_id=7, brief="Fix it", plan_fn=plan,
            td_runner=lambda args, cwd: (0, "{}"))
        self.assertIs(gen.plan_fn, plan)

    def test_default_plan_parses_files(self):
        fake = FakeModel("1. Add check [files: a.py, b.py]\n2. Wire it up")
        with mock.patch.object(
                coder, "build_model", return_value=(fake, {})):
            out = coder._default_plan_fn("Fix it", "/tmp/wt", {"errors": []})
        self.assertIn("Add check", out["plan"])
        self.assertEqual(sorted(out["allowed_files"]), ["a.py", "b.py"])
        self.assertEqual(out["verification_commands"], [])

    def test_default_implement_returns_notes(self):
        fake = FakeModel("implemented + verified")
        with mock.patch.object(
                coder, "build_model", return_value=(fake, {})):
            notes = coder._default_implement_fn(
                "Fix it", "plan", "/tmp/wt", [])
        self.assertEqual(notes, "implemented + verified")

    def test_replan_queries_registry_for_node(self):
        seen = []

        class Registry:
            def inject_skills_for_node(self, node, base):
                seen.append((node, base))
                return base + "\n## skills"

        fake_mod = types.ModuleType("skills_registry")
        fake_mod.SkillsRegistry = Registry
        with mock.patch.dict(sys.modules, {"skills_registry": fake_mod}):
            text = coder._inject_replanner_skills()
        self.assertTrue(seen)
        node, base = seen[0]
        self.assertEqual(node, "validated_replanner_node")
        self.assertIn("## skills", text)

    def test_replan_injection_failure_is_silent(self):
        fake_mod = types.ModuleType("skills_registry")
        with mock.patch.dict(sys.modules, {"skills_registry": fake_mod}):
            self.assertEqual(coder._inject_replanner_skills(), "")


if __name__ == "__main__":
    unittest.main()
