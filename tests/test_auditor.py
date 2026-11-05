"""Tests for deep-agent/auditor.py (stdlib only, no model).

Graph integration cases need langgraph (deep-agent venv) and skip
otherwise. Run agent cases with: deep-agent/.venv/bin/python
tests/test_auditor.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import auditor

try:
    import langgraph  # noqa: F401

    HAS_AGENT_DEPS = True
except ImportError:
    HAS_AGENT_DEPS = False


def _gate_ok(args):
    return 0, json.dumps({"status": "success", "passed": True,
                          "impacted_files": ["a.py"],
                          "dependent_tests": [], "violations": []})


class TestRunTdAuditImpact(unittest.TestCase):
    def test_parses_payload(self):
        ok, payload = auditor.run_td_audit_impact(
            "/tmp", runner=_gate_ok)
        self.assertTrue(ok)
        self.assertEqual(payload["impacted_files"], ["a.py"])

    def test_non_json_fails(self):
        ok, error = auditor.run_td_audit_impact(
            "/tmp", runner=lambda args: (0, "not json"))
        self.assertFalse(ok)
        self.assertIn("non-JSON", error)

    def test_missing_td_fails_closed(self):
        def runner(args):
            raise FileNotFoundError("no td")
        ok, error = auditor.run_td_audit_impact("/tmp", runner=runner)
        self.assertFalse(ok)
        self.assertIn("not found", error)

    def test_error_exit_fails(self):
        ok, error = auditor.run_td_audit_impact(
            "/tmp", runner=lambda args: (1, "boom"))
        self.assertFalse(ok)
        self.assertTrue(error)


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv (langgraph)")
class TestAuditorGraph(unittest.TestCase):
    def setUp(self):
        import ledger
        self._ledger_tmp = tempfile.TemporaryDirectory()
        self._ledger_prev = ledger.LEDGER
        ledger.LEDGER = os.path.join(self._ledger_tmp.name, "ledger.jsonl")

    def tearDown(self):
        import ledger
        ledger.LEDGER = self._ledger_prev
        self._ledger_tmp.cleanup()

    def _gen(self, tmp, td_runner=None, thread="audit-test-1"):
        return auditor.AuditorGraph(
            tmp, audit_fn=lambda gate: "1. Fix the naming.",
            td_runner=td_runner or _gate_ok, thread_id=thread)

    def test_violations_fail_closed_no_llm(self):
        seen = []

        def td_runner(args):
            seen.append(args[0])
            return 1, json.dumps(
                {"status": "success", "passed": False,
                 "impacted_files": ["a.py"], "dependent_tests": [],
                 "violations": [{"file": "a.py", "line": 2,
                                 "rule": "r", "message": "m"}]})

        with tempfile.TemporaryDirectory() as tmp:
            gen = self._gen(tmp, td_runner=td_runner,
                            thread="audit-test-fail")
            final = gen.run()
            self.assertFalse(final.get("valid"))
            self.assertNotIn("__interrupt__", final)
            joined = "\n".join(final.get("errors", []))
            self.assertIn("audit-code:a.py:2: r: m", joined)
            # audit-code failure blocks before audit-impact runs.
            self.assertNotIn("audit-impact", seen)

    def test_verify_scope_runs_with_spec(self):
        seen = []

        def td_runner(args):
            seen.append(args[0])
            return _gate_ok(args)

        with tempfile.TemporaryDirectory() as tmp:
            spec = os.path.join(tmp, "spec.md")
            with open(spec, "w", encoding="utf-8") as handle:
                handle.write("# spec\n")
            gen = auditor.AuditorGraph(
                tmp, audit_fn=lambda gate: "ok",
                td_runner=td_runner, thread_id="audit-test-spec",
                spec_file=spec)
            state = gen.run()
            self.assertIn("__interrupt__", state)
            self.assertIn("verify-scope", seen)

    def test_verdict_approved_on_approve(self):
        import ledger
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = os.path.join(tmp, "ledger.jsonl")
            old = ledger.LEDGER
            ledger.LEDGER = ledger_path
            try:
                gen = self._gen(tmp, thread="audit-test-verdict")
                gen.run()
                final = gen.run(resume={"decision": "approve"})
            finally:
                ledger.LEDGER = old
            self.assertEqual(final.get("verdict", {}).get("status"),
                             "APPROVED")

    def test_approve_persists(self):
        import ledger
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = os.path.join(tmp, "ledger.jsonl")
            old = ledger.LEDGER
            ledger.LEDGER = ledger_path
            try:
                gen = self._gen(tmp, thread="audit-test-ok")
                state = gen.run()
                self.assertIn("__interrupt__", state)
                final = gen.run(resume={"decision": "approve"})
                self.assertTrue(final.get("persisted"))
            finally:
                ledger.LEDGER = old
            with open(ledger_path, encoding="utf-8") as handle:
                entry = json.loads(
                    handle.read().strip().splitlines()[-1])
            self.assertEqual(entry.get("script"), "auditor.py")

    def test_reject_reruns_gate_then_persists(self):
        seen = []

        def td_runner(args):
            seen.append(1)
            return _gate_ok(args)

        with tempfile.TemporaryDirectory() as tmp:
            gen = self._gen(tmp, td_runner=td_runner,
                            thread="audit-test-reject")
            state = gen.run()
            state = gen.run(resume={"decision": "reject",
                                    "notes": "say more"})
            self.assertIn("__interrupt__", state)
            final = gen.run(resume={"decision": "approve"})
            self.assertTrue(final.get("persisted"))
            self.assertGreaterEqual(len(seen), 2)


if __name__ == "__main__":
    unittest.main()
